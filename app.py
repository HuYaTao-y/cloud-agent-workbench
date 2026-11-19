import json
import os
import queue
import shutil
import signal
import sqlite3
import stat
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

DATA = Path(os.environ.get("WORKBENCH_DATA", "./data")).resolve()
WEB = Path(__file__).resolve().parent / "web"
MODEL = os.environ.get("AGENT_MODEL", "linext-5.3-luna")
UPSTREAM = os.environ.get("AGENT_BASE_URL", "").rstrip("/")
KEY = os.environ.get("AGENT_API_KEY", "")
PORT = int(os.environ.get("PORT", "8000"))
PROXY_TOKEN = uuid.uuid4().hex
MAX_UPLOAD = 20 * 1024 * 1024
JOBS = queue.Queue()


def adapt(payload):
    if "tools" in payload:
        payload["tools"] = [tool for tool in payload["tools"] if tool["type"] in {"function", "custom"}]
    return payload


def workspace_file(root, name):
    relative = Path(name)
    path = root / relative
    if relative.is_absolute() or not relative.parts or any(part.startswith(".") for part in relative.parts):
        raise ValueError("文件路径无效")
    return path


@contextmanager
def open_download(root, path):
    parts = path.relative_to(root).parts
    directory = os.open(root, os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise FileNotFoundError(path)
            yield handle
    finally:
        os.close(directory)


@contextmanager
def db():
    connection = sqlite3.connect(DATA / "workbench.sqlite3")
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize():
    DATA.mkdir(parents=True, exist_ok=True)
    database = DATA / "workbench.sqlite3"
    database.touch(mode=0o600, exist_ok=True)
    database.chmod(0o600)
    with db() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, thread TEXT, created REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY, session TEXT NOT NULL, prompt TEXT NOT NULL,
                status TEXT NOT NULL, events TEXT NOT NULL DEFAULT '[]'
            );
            UPDATE runs SET status = 'interrupted' WHERE status IN ('queued', 'running');
        """)


def agent_identity(sid):
    if os.getuid() == 0:
        uid = int(sid[:7], 16) + 10000
        return {"user": uid, "group": uid, "extra_groups": []}
    return {}


def create_session(title):
    sid = uuid.uuid4().hex
    root = DATA / sid
    root.mkdir(mode=0o711)
    identity = agent_identity(sid)
    for name in ("workspace", "agent"):
        path = root / name
        path.mkdir(mode=0o700)
        if identity:
            os.chown(path, identity["user"], identity["group"])
    with db() as con:
        con.execute("INSERT INTO sessions VALUES (?, ?, NULL, ?)", (sid, title, time.time()))
    return sid


def event(run_id, kind, message):
    with db() as con:
        con.execute(
            "UPDATE runs SET events = json_insert(events, '$[#]', json(?)) WHERE id = ?",
            (json.dumps({"kind": kind, "text": str(message)[:12000]}, ensure_ascii=False), run_id),
        )


def execute(run):
    run_id, sid, prompt = run
    root = DATA / sid
    workspace = root / "workspace"
    with db() as con:
        thread = con.execute("SELECT thread FROM sessions WHERE id = ?", (sid,)).fetchone()["thread"]
        con.execute("UPDATE runs SET status = 'running' WHERE id = ?", (run_id,))

    env = {
        "PATH": os.environ["PATH"], "HOME": str(root / "agent"),
        "CODEX_HOME": str(root / "agent"), "WORKBENCH_PROXY_TOKEN": PROXY_TOKEN,
        "LANG": "C.UTF-8",
    }
    config = {
        "model_provider": "workbench",
        "model_providers.workbench.name": "Workbench proxy",
        "model_providers.workbench.base_url": f"http://127.0.0.1:{PORT}/provider/v1",
        "model_providers.workbench.env_key": "WORKBENCH_PROXY_TOKEN",
        "model_providers.workbench.wire_api": "responses",
        "approval_policy": "never",
        "sandbox_mode": os.environ.get("AGENT_SANDBOX", "workspace-write"),
        "shell_environment_policy.exclude": ["WORKBENCH_PROXY_TOKEN"],
        "web_search": "disabled",
    }
    command = [shutil.which("codex") or "codex", "exec", "--ignore-user-config"]
    for key, value in config.items():
        command += ["-c", f"{key}={json.dumps(value)}"]
    command += ["--skip-git-repo-check", "--json", "-m", MODEL]
    if thread:
        command += ["resume", thread]
    command += ["-"]

    status = "failed"
    process = None
    timer = None
    try:
        process = subprocess.Popen(
            command, cwd=workspace, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
            start_new_session=True, **agent_identity(sid),
        )

        def terminate():
            try:
                os.killpg(process.pid, signal.SIGKILL)
                event(run_id, "error", "执行超过 10 分钟，已终止。")
            except ProcessLookupError:
                pass

        timer = threading.Timer(600, terminate)
        timer.start()
        process.stdin.write(prompt)
        process.stdin.close()
        completed = False
        for line in process.stdout:
            line = line.strip().replace(PROXY_TOKEN, "[已隐藏]")
            try:
                message = json.loads(line)
            except ValueError:
                if line:
                    event(run_id, "log", line)
                continue
            kind = message.get("type")
            if kind == "thread.started":
                with db() as con:
                    con.execute("UPDATE sessions SET thread = ? WHERE id = ?", (message["thread_id"], sid))
            elif kind == "turn.completed":
                completed = True
            elif kind in {"error", "turn.failed"}:
                event(run_id, "error", message.get("message") or message.get("error", {}).get("message"))
            elif kind in {"item.started", "item.completed"}:
                item = message.get("item", {})
                item_type = item.get("type")
                if item_type == "agent_message":
                    event(run_id, "reply", item.get("text", ""))
                elif item_type == "command_execution":
                    label = "执行" if kind == "item.started" else f"退出码 {item.get('exit_code')}"
                    event(run_id, "command", f"{label} · {item.get('command', '')}\n{item.get('aggregated_output', '')}")
                elif item_type == "file_change":
                    event(run_id, "file", ", ".join(change["path"] for change in item.get("changes", [])))
                elif item_type == "error":
                    event(run_id, "log", item.get("message", ""))
        if process.wait() == 0 and completed:
            status = "completed"
        elif not completed:
            event(run_id, "error", "Agent 未完成本轮。请查看运行记录后重试。")
    except Exception as exc:
        event(run_id, "error", str(exc))
    finally:
        if timer:
            timer.cancel()
        if process and process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        with db() as con:
            con.execute("UPDATE runs SET status = ? WHERE id = ?", (status, run_id))


def worker():
    while True:
        execute(JOBS.get())


class Handler(BaseHTTPRequestHandler):
    def respond(self, value, status=200):
        self.send_bytes(json.dumps(value, ensure_ascii=False).encode(), "application/json", status)

    def send_bytes(self, data, content_type, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def read_body(self, limit):
        size = int(self.headers.get("Content-Length", 0))
        if not 0 <= size <= limit:
            raise ValueError("请求或上传文件过大")
        return self.rfile.read(size)

    def json_body(self):
        obj = json.loads(self.read_body(65536))
        if not isinstance(obj, dict):
            raise ValueError("请求必须是 JSON 对象")
        return obj

    def do_GET(self):
        self.route(False)

    def do_POST(self):
        self.route(True)

    def route(self, post):
        url = urlsplit(self.path)
        try:
            if url.path == "/provider/v1/responses" and post:
                self.proxy()
                return
            host = self.headers.get("Host", "")
            if urlsplit("//" + host).hostname not in {"127.0.0.1", "localhost", "::1"}:
                self.respond({"error": "请通过本机地址访问"}, 403)
                return
            if post:
                origin = self.headers.get("Origin")
                if origin and urlsplit(origin).netloc != host:
                    self.respond({"error": "请求来源无效"}, 403)
                    return
            if url.path == "/api/sessions":
                if post:
                    title = self.json_body().get("title", "新会话")
                    if not isinstance(title, str):
                        raise ValueError("会话名称无效")
                    self.respond({"id": create_session(title.strip()[:100] or "新会话")}, 201)
                else:
                    with db() as con:
                        rows = con.execute("""
                            SELECT id, title, (SELECT status FROM runs WHERE session = sessions.id
                            ORDER BY id DESC LIMIT 1) AS status FROM sessions ORDER BY created DESC
                        """).fetchall()
                    self.respond([dict(row) for row in rows])
                return
            parts = url.path.strip("/").split("/")
            if parts[:2] == ["api", "sessions"] and len(parts) >= 3:
                self.session_request(parts[2], parts[3:], parse_qs(url.query), post)
                return
            assets = {"/": ("index.html", "text/html"), "/style.css": ("style.css", "text/css"),
                      "/ui.js": ("ui.js", "text/javascript")}
            if not post and url.path in assets:
                name, content_type = assets[url.path]
                self.send_bytes((WEB / name).read_bytes(), content_type + "; charset=utf-8")
                return
            self.respond({"error": "资源不存在"}, 404)
        except (ValueError, UnicodeError) as exc:
            self.respond({"error": str(exc)}, 400)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except FileNotFoundError:
            self.respond({"error": "文件不存在"}, 404)
        except OSError:
            self.respond({"error": "文件操作失败"}, 500)

    def session_request(self, sid, action, query, post):
        with db() as con:
            session = con.execute("SELECT id, title FROM sessions WHERE id = ?", (sid,)).fetchone()
        if session is None:
            self.respond({"error": "会话不存在"}, 404)
            return
        root = DATA / sid / "workspace"
        if post and action == ["runs"]:
            prompt = self.json_body().get("prompt")
            if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 12000:
                raise ValueError("请输入 1–12000 字的任务")
            with db() as con:
                cursor = con.execute(
                    "INSERT INTO runs (session, prompt, status) VALUES (?, ?, 'queued')", (sid, prompt.strip())
                )
                run_id = cursor.lastrowid
            JOBS.put((run_id, sid, prompt.strip()))
            self.respond({"id": run_id}, 202)
        elif post and action == ["files"]:
            name = query.get("name", [""])[0]
            if not name or name != Path(name).name or name.startswith(".") or len(name.encode()) > 255:
                raise ValueError("文件名无效")
            data = self.read_body(MAX_UPLOAD)
            destination = workspace_file(root, name)
            try:
                with destination.open("xb") as handle:
                    identity = agent_identity(sid)
                    if identity:
                        os.fchown(handle.fileno(), identity["user"], identity["group"])
                    handle.write(data)
            except FileExistsError:
                self.respond({"error": "同名文件已存在"}, 409)
                return
            self.respond({"name": name}, 201)
        elif not post and action == ["files"]:
            path = workspace_file(root, query.get("name", [""])[0])
            if not path.is_file():
                self.respond({"error": "文件不存在"}, 404)
                return
            with open_download(root, path) as handle:
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(path.name))
                self.send_header("Content-Length", str(os.fstat(handle.fileno()).st_size))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                shutil.copyfileobj(handle, self.wfile)
        elif not post and not action:
            with db() as con:
                rows = con.execute("SELECT * FROM runs WHERE session = ? ORDER BY id", (sid,)).fetchall()
            runs = [dict(row) | {"events": json.loads(row["events"])} for row in rows]
            files = []
            for directory, dirs, names in os.walk(root):
                dirs[:] = [name for name in dirs if not name.startswith(".") and name not in {"node_modules", "__pycache__"}]
                for name in names:
                    path = Path(directory) / name
                    if not name.startswith(".") and not path.is_symlink() and path.is_file():
                        files.append(str(path.relative_to(root)))
            self.respond(dict(session) | {"runs": runs, "files": sorted(files)})
        else:
            self.respond({"error": "资源不存在"}, 404)

    def proxy(self):
        if self.headers.get("Authorization") != "Bearer " + PROXY_TOKEN:
            self.respond({"error": "Unauthorized"}, 401)
            return
        if not UPSTREAM or not KEY:
            self.respond({"error": "请配置 AGENT_BASE_URL 与 AGENT_API_KEY"}, 503)
            return
        payload = adapt(json.loads(self.read_body(16 * 1024 * 1024)))
        req = urllib.request.Request(
            UPSTREAM + "/responses", data=json.dumps(payload).encode(),
            headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json",
                     "Accept": "text/event-stream"},
        )
        try:
            upstream = urllib.request.urlopen(req, timeout=180)
        except urllib.error.HTTPError as exc:
            with exc:
                body = exc.read(8192).replace(KEY.encode(), b"[redacted]")
                self.send_bytes(body, "application/json", exc.code)
            return
        except OSError:
            self.respond({"error": "模型服务连接失败"}, 502)
            return
        with upstream:
            self.send_response(upstream.status)
            self.send_header("Content-Type", upstream.headers.get("Content-Type", "text/event-stream"))
            self.end_headers()
            for line in upstream:
                self.wfile.write(line)
                self.wfile.flush()


if __name__ == "__main__":
    initialize()
    threading.Thread(target=worker, daemon=True).start()
    ThreadingHTTPServer((os.environ.get("BIND", "127.0.0.1"), PORT), Handler).serve_forever()
