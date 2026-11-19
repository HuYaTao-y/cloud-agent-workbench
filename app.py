import csv
import io
import json
import os
import queue
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import tempfile
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
PROXY_SESSIONS = {}
TOOL_SLOT = threading.BoundedSemaphore(1)
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
            CREATE TABLE IF NOT EXISTS model_config (
                id INTEGER PRIMARY KEY CHECK (id = 1), url TEXT NOT NULL, model TEXT NOT NULL, key TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tools (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
                source TEXT NOT NULL, script BLOB NOT NULL, accept TEXT NOT NULL,
                flag TEXT NOT NULL, flag_label TEXT NOT NULL, created REAL NOT NULL,
                output_format TEXT NOT NULL DEFAULT 'text'
            );
            UPDATE runs SET status = 'interrupted' WHERE status IN ('queued', 'running');
        """)
        columns = {row["name"] for row in con.execute("PRAGMA table_info(tools)")}
        if "session_id" not in columns:
            con.execute("ALTER TABLE tools ADD COLUMN session_id TEXT")
        if "output_format" not in columns:
            con.execute("ALTER TABLE tools ADD COLUMN output_format TEXT NOT NULL DEFAULT 'text'")
        link_legacy_snapshots(con)


def model_config():
    with db() as con:
        row = con.execute("SELECT url, model, key FROM model_config WHERE id = 1").fetchone()
    return dict(row) if row else {"url": UPSTREAM, "model": MODEL, "key": KEY}


def public_model(config):
    return {"url": config["url"], "model": config["model"], "hasKey": bool(config["key"])}


def model_candidate(body):
    current = model_config()
    candidate = {}
    for field, limit in (("url", 2048), ("model", 200), ("key", 4096)):
        value = body.get(field, "" if field == "key" else current[field])
        if not isinstance(value, str) or len(value) > limit:
            raise ValueError("模型配置字段无效")
        candidate[field] = value.strip()
    candidate["url"] = candidate["url"].rstrip("/")
    if not candidate["key"]:
        if candidate["url"] != current["url"].rstrip("/"):
            raise ValueError("更换 API 地址时请填写该服务的 Key")
        candidate["key"] = current["key"]
    parsed = urlsplit(candidate["url"])
    if (parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username is not None
            or parsed.query or parsed.fragment or not candidate["model"] or not candidate["key"]):
        raise ValueError("请填写有效的 API 地址、模型和 Key")
    return candidate


class ModelRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urlsplit(req.full_url), urlsplit(newurl)
        default_ports = {"http": 80, "https": 443}
        try:
            old_origin = (old.scheme, old.hostname, old.port or default_ports.get(old.scheme))
            new_origin = (new.scheme, new.hostname, new.port or default_ports.get(new.scheme))
        except ValueError:
            new_origin = None
            old_origin = ()
        if old_origin != new_origin or new.username is not None:
            raise urllib.error.HTTPError(req.full_url, code, "模型请求拒绝跨来源重定向", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


MODEL_HTTP = urllib.request.build_opener(ModelRedirectHandler())


def test_model_connection(config):
    request = urllib.request.Request(
        config["url"] + "/models", headers={"Authorization": "Bearer " + config["key"]},
    )
    try:
        with MODEL_HTTP.open(request, timeout=12) as response:
            data = json.loads(response.read(1024 * 1024))
        models = [row["id"] for row in data["data"] if isinstance(row.get("id"), str)]
    except urllib.error.HTTPError as exc:
        exc.close()
        if 300 <= exc.code < 400:
            raise ValueError("模型服务重定向失败，请填写最终 API 地址") from None
        raise ValueError(f"模型服务返回 HTTP {exc.code}") from None
    except (OSError, ValueError, KeyError, AttributeError, TypeError):
        raise ValueError("无法读取模型列表，请检查地址和连接") from None
    if config["model"] not in models:
        raise ValueError("连接成功，但模型列表中没有所选模型")
    return {"model": config["model"], "available": len(models)}


def tool_flag(value):
    if not isinstance(value, str) or (value and not re.fullmatch(r"--[a-zA-Z][a-zA-Z0-9-]{0,48}", value)):
        raise ValueError("开关格式应类似 --total")
    return value


def text_field(body, key, limit, default=""):
    value = body.get(key, default)
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError("工具配置字段无效")
    return value.strip()


def register_tool(sid, body):
    source = text_field(body, "source", 512)
    update_id = text_field(body, "updateId", 32)
    name = text_field(body, "name", 80)
    description = text_field(body, "description", 500)
    accept = text_field(body, "accept", 20)
    flag = tool_flag(text_field(body, "flag", 50))
    flag_label = text_field(body, "flagLabel", 50)
    output_format = text_field(body, "outputFormat", 10, "text")
    if output_format not in {"text", "csv"}:
        raise ValueError("输出格式无效")
    if not source.endswith(".py") or not name or (flag and not flag_label):
        raise ValueError("请填写工具名称、选择 Python 脚本，并为可选开关填写标签")
    if accept and not re.fullmatch(r"\.[a-zA-Z0-9]{1,12}", accept):
        raise ValueError("文件扩展名格式应类似 .csv")
    root = DATA / sid / "workspace"
    with open_download(root, workspace_file(root, source)) as handle:
        script = handle.read(262145)
    if len(script) > 262144:
        raise ValueError("脚本大小不能超过 256 KiB")
    try:
        compile(script, source, "exec")
    except (SyntaxError, ValueError):
        raise ValueError("Python 脚本存在语法错误") from None
    tool_id = uuid.uuid4().hex
    with db() as con:
        if update_id:
            updated = con.execute("""
                UPDATE tools SET name = ?, description = ?, script = ?, accept = ?, flag = ?,
                    flag_label = ?, created = ?, output_format = ?, session_id = ?
                WHERE id = ? AND source = ? AND (session_id = ? OR session_id IS NULL)
            """, (name, description, script, accept, flag, flag_label, time.time(), output_format,
                  sid, update_id, source, sid))
            if not updated.rowcount:
                raise ValueError("待更新工具已失效")
            return update_id
        con.execute("""INSERT INTO tools (id, name, description, source, script, accept, flag, flag_label,
                    created, output_format, session_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (tool_id, name, description, source, script, accept, flag, flag_label, time.time(), output_format, sid))
    return tool_id


def workspace_files(root, *, for_tools=False):
    files = []
    excluded = {"node_modules", "__pycache__"}
    if for_tools:
        excluded.update({"tests", "test"})
    for directory, dirs, names in os.walk(root):
        dirs[:] = [name for name in dirs if not name.startswith(".") and name not in excluded]
        for name in names:
            if name.startswith("."):
                continue
            if for_tools and (not name.endswith(".py") or name.startswith("test_") or name.endswith("_test.py")
                              or name in {"setup.py", "conftest.py", "__init__.py"}):
                continue
            path = Path(directory) / name
            if not path.is_symlink() and path.is_file():
                files.append(str(path.relative_to(root)))
    return sorted(files)


def link_legacy_snapshots(con):
    tools = con.execute("SELECT id, source, script FROM tools WHERE session_id IS NULL").fetchall()
    if not tools:
        return
    sessions = con.execute("SELECT id FROM sessions").fetchall()
    for tool in tools:
        matches = []
        for session in sessions:
            sid = session["id"]
            root = DATA / sid / "workspace"
            try:
                with open_download(root, workspace_file(root, tool["source"])) as handle:
                    if handle.read(262145) == tool["script"]:
                        matches.append(sid)
            except (OSError, ValueError):
                continue
        if len(matches) == 1:
            con.execute("UPDATE tools SET session_id = ? WHERE id = ?", (matches[0], tool["id"]))


def tool_candidates():
    with db() as con:
        sessions = con.execute("""
            SELECT id, title FROM sessions WHERE EXISTS (
                SELECT 1 FROM runs WHERE session = sessions.id AND status = 'completed'
            ) ORDER BY created DESC
        """).fetchall()
        snapshots = con.execute(
            "SELECT id, session_id, source, script FROM tools ORDER BY created DESC, id DESC"
        ).fetchall()
    saved = {}
    legacy = {}
    for row in snapshots:
        if row["session_id"] is None:
            legacy.setdefault(row["source"], []).append(row["script"])
        else:
            saved.setdefault((row["session_id"], row["source"]), row)
    candidates = []
    for session in sessions:
        sid = session["id"]
        root = DATA / sid / "workspace"
        for source in workspace_files(root, for_tools=True):
            snapshot = saved.get((sid, source))
            if snapshot or source in legacy:
                try:
                    with open_download(root, workspace_file(root, source)) as handle:
                        current = handle.read(262145)
                except OSError:
                    continue
                if snapshot and current == snapshot["script"]:
                    continue
                if not snapshot and current in legacy.get(source, []):
                    continue
            candidate = {"session": sid, "title": session["title"], "source": source}
            if snapshot:
                candidate["updateId"] = snapshot["id"]
            candidates.append(candidate)
    return candidates


def run_tool(tool, filename, data, enable_flag):
    if (not filename or filename != Path(filename).name or filename.startswith(".")
            or len(filename.encode()) > 240 or "\x00" in filename):
        raise ValueError("文件名无效")
    if tool["accept"] and Path(filename).suffix.lower() != tool["accept"].lower():
        raise ValueError(f"请选择 {tool['accept']} 文件")
    if not TOOL_SLOT.acquire(blocking=False):
        return {"ok": False, "error": "有工具正在运行，请稍后重试"}
    try:
        with tempfile.TemporaryDirectory(prefix="workbench-tool-") as temp:
            folder = Path(temp)
            inputs = folder / "inputs"
            inputs.mkdir()
            (inputs / filename).write_bytes(data)
            script = folder / "tool.py"
            script.write_bytes(tool["script"])
            identity = agent_identity(uuid.uuid4().hex)
            if identity:
                os.chown(folder, identity["user"], identity["group"])
                inputs.chmod(0o555)
                script.chmod(0o444)
            command = [sys.executable, "-I", "-c",
                       "import os,resource,sys; resource.setrlimit(resource.RLIMIT_FSIZE,(1048576,1048576)); "
                       "os.execv(sys.executable,[sys.executable,'-I',*sys.argv[1:]])",
                       str(script), str(Path("inputs") / filename)]
            if enable_flag and tool["flag"]:
                command.append(tool["flag"])
            with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                process = subprocess.Popen(
                    command, cwd=folder, stdout=stdout, stderr=stderr, stdin=subprocess.DEVNULL,
                    env={"PATH": os.defpath, "LANG": "C.UTF-8", "HOME": temp, "TMPDIR": temp},
                    start_new_session=True, **identity,
                )
                timed_out = False
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    timed_out = True
                finally:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                stdout.seek(0)
                stderr.seek(0)
                output = stdout.read().decode("utf-8", errors="replace")
                rows = []
                if tool["output_format"] == "csv" and process.returncode == 0:
                    try:
                        for index, row in enumerate(csv.reader(io.StringIO(output), strict=True)):
                            if index == 25:
                                break
                            rows.append(row[:20])
                    except csv.Error:
                        rows = []
                return {
                    "rows": rows,
                    "ok": process.returncode == 0 and not timed_out,
                    "output": output,
                    "diagnostics": stderr.read().decode("utf-8", errors="replace"),
                    "error": "运行超过 30 秒，已终止" if timed_out else
                             (f"脚本退出码 {process.returncode}" if process.returncode else ""),
                }
    finally:
        TOOL_SLOT.release()


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

    provider = model_config()
    proxy_token = uuid.uuid4().hex
    env = {
        "PATH": os.environ["PATH"], "HOME": str(root / "agent"),
        "CODEX_HOME": str(root / "agent"), "WORKBENCH_PROXY_TOKEN": proxy_token,
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
    command += ["--skip-git-repo-check", "--json", "-m", provider["model"]]
    if thread:
        command += ["resume", thread]
    command += ["-"]

    status = "failed"
    process = None
    timer = None
    PROXY_SESSIONS[proxy_token] = provider
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
        produced_result = False
        for line in process.stdout:
            line = line.strip().replace(proxy_token, "[已隐藏]")
            if provider["key"]:
                line = line.replace(provider["key"], "[已隐藏]")
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
                    reply = item.get("text") or ""
                    produced_result |= kind == "item.completed" and bool(reply.strip())
                    event(run_id, "reply", reply)
                elif item_type == "command_execution":
                    produced_result |= kind == "item.completed" and item.get("exit_code") == 0
                    label = "执行" if kind == "item.started" else f"退出码 {item.get('exit_code')}"
                    event(run_id, "command", f"{label} · {item.get('command', '')}\n{item.get('aggregated_output', '')}")
                elif item_type == "file_change":
                    produced_result |= (kind == "item.completed" and item.get("status") != "failed"
                                        and bool(item.get("changes")))
                    event(run_id, "file", ", ".join(change["path"] for change in item.get("changes", [])))
                elif item_type == "error":
                    event(run_id, "log", item.get("message", ""))
        returncode = process.wait()
        if returncode == 0 and completed and produced_result:
            status = "completed"
        elif returncode == 0 and completed:
            event(run_id, "error", "Agent 返回了空结果。请重试本轮。")
        else:
            event(run_id, "error", "Agent 未完成本轮。请查看运行记录后重试。")
    except Exception as exc:
        event(run_id, "error", str(exc))
    finally:
        PROXY_SESSIONS.pop(proxy_token, None)
        if timer:
            timer.cancel()
        if process:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            process.stdin.close()
            process.stdout.close()
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
            if url.path in {"/api/model", "/api/model/test"}:
                if post:
                    candidate = model_candidate(self.json_body())
                    if url.path.endswith("/test"):
                        self.respond(test_model_connection(candidate))
                    else:
                        with db() as con:
                            con.execute("REPLACE INTO model_config VALUES (1, ?, ?, ?)",
                                        (candidate["url"], candidate["model"], candidate["key"]))
                        self.respond(public_model(candidate))
                elif url.path == "/api/model":
                    self.respond(public_model(model_config()))
                else:
                    self.respond({"error": "资源不存在"}, 404)
                return
            if url.path == "/api/tools/candidates" and not post:
                self.respond(tool_candidates())
                return
            if url.path == "/api/tools" and not post:
                with db() as con:
                    rows = con.execute("SELECT id, name, description, source, accept, flag, flag_label, output_format FROM tools ORDER BY created DESC").fetchall()
                self.respond([dict(row) for row in rows])
                return
            tool_match = re.fullmatch(r"/api/tools/([a-f0-9]{32})/run", url.path)
            if tool_match and post:
                with db() as con:
                    tool = con.execute("SELECT * FROM tools WHERE id = ?", (tool_match[1],)).fetchone()
                if tool is None:
                    self.respond({"error": "工具不存在"}, 404)
                else:
                    query = parse_qs(url.query)
                    self.respond(run_tool(tool, query.get("name", [""])[0], self.read_body(MAX_UPLOAD),
                                          query.get("flag") == ["1"]))
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
                      "/ui.js": ("ui.js", "text/javascript"), "/tools.js": ("tools.js", "text/javascript")}
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
        if post and action == ["tools"]:
            self.respond({"id": register_tool(sid, self.json_body())}, 201)
        elif post and action == ["runs"]:
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
            self.respond(dict(session) | {"runs": runs, "files": workspace_files(root)})
        else:
            self.respond({"error": "资源不存在"}, 404)

    def proxy(self):
        authorization = self.headers.get("Authorization", "")
        config = PROXY_SESSIONS.get(authorization.removeprefix("Bearer "))
        if not authorization.startswith("Bearer ") or config is None:
            self.respond({"error": "Unauthorized"}, 401)
            return
        if not config["url"] or not config["key"]:
            self.respond({"error": "请配置 AGENT_BASE_URL 与 AGENT_API_KEY"}, 503)
            return
        payload = adapt(json.loads(self.read_body(16 * 1024 * 1024)))
        req = urllib.request.Request(
            config["url"] + "/responses", data=json.dumps(payload).encode(),
            headers={"Authorization": "Bearer " + config["key"], "Content-Type": "application/json",
                     "Accept": "text/event-stream"},
        )
        try:
            upstream = MODEL_HTTP.open(req, timeout=180)
        except urllib.error.HTTPError as exc:
            with exc:
                if 300 <= exc.code < 400:
                    self.respond({"error": "模型服务重定向失败，请填写最终 API 地址"}, 502)
                    return
                body = exc.read(8192).replace(config["key"].encode(), b"[redacted]")
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
