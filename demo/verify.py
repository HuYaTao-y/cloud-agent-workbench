"""Exercise the running workspace with real CLI jobs and save their outputs."""
import argparse
import json
import time
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent


def verify(base, evidence):
    def request(path, data=None, method=None):
        if isinstance(data, dict):
            data = json.dumps(data).encode()
        with urlopen(Request(base + path, data=data, method=method), timeout=20) as response:
            return response.read()

    def api(path, data=None):
        return json.loads(request(path, data))

    def create(title):
        return "/api/sessions/" + api("/api/sessions", {"title": title})["id"]

    def upload(session, filename):
        return request(session + "/files?name=" + quote(filename), (HERE / filename).read_bytes(), "POST")

    def start(session, prompt):
        return api(session + "/runs", {"prompt": prompt})["id"]

    def finish(session, run_id, expected="completed"):
        seen = []
        deadline = time.monotonic() + 630
        while time.monotonic() < deadline:
            detail = api(session)
            run = next(run for run in detail["runs"] if run["id"] == run_id)
            sample = {"status": run["status"], "events": len(run["events"])}
            if not seen or sample != seen[-1]:
                seen.append(sample)
                print(f"run {run_id}: {sample}", flush=True)
            if run["status"] not in {"queued", "running"}:
                evidence.mkdir(parents=True, exist_ok=True)
                (evidence / f"run-{run_id}.json").write_text(
                    json.dumps({"observations": seen, "run": run, "files": detail["files"]}, ensure_ascii=False, indent=2) + "\n"
                )
                assert run["status"] == expected, run
                return run
            time.sleep(1)

        raise TimeoutError(run_id)

    def download(session, name):
        data = request(session + "/files?name=" + quote(name))
        (evidence / name).write_bytes(data)
        return data.decode()

    code = create("CSV 汇总工具")
    office = create("上线周会")
    upload(code, "expenses.csv")
    upload(office, "meeting.txt")

    initial = start(code, "Create summarize.py using only the Python standard library. It takes a CSV path as a positional argument and prints category totals sorted alphabetically as Category,12.34, one per line, without a header. Run python3 summarize.py expenses.csv to verify. Keep expenses.csv unchanged.")
    notes = start(office, "阅读 meeting.txt 并生成中文 report.md。列出负责人、实际截止事项和日期，以及待定事项。不要编造日期。用 Markdown 表格呈现。")
    first = finish(code, initial)
    assert any(event["kind"] == "command" and "30.00" in event["text"] and "12.75" in event["text"]
               for event in first["events"]), first
    assert "csv" in download(code, "summarize.py")
    finish(office, notes)
    report = download(office, "report.md")
    assert all(name in report for name in ["林悦", "陈帆", "周航", "23", "24"]), report

    follow_up = start(code, "Modify the existing tool to support an optional --total flag that appends TOTAL,42.75 for this input. Preserve existing behavior without the flag. Run python3 summarize.py expenses.csv --total to verify.")
    follow = finish(code, follow_up)
    assert any(event["kind"] == "command" and "TOTAL,42.75" in event["text"] for event in follow["events"]), follow
    download(code, "summarize.py")
    assert len(api(code)["runs"]) == 2
    assert len(api(office)["runs"]) == 1
    assert {code.rsplit("/", 1)[1], office.rsplit("/", 1)[1]} <= {session["id"] for session in api("/api/sessions")}

    print(f"Verified real CLI runs and artifacts in {evidence}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", type=Path, default=HERE / "evidence")
    options = parser.parse_args()
    verify(options.url.rstrip("/"), options.output)
