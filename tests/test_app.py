import unittest

import app


class AdapterTests(unittest.TestCase):
    def test_only_client_tools_reach_provider(self):
        payload = {"tools": [{"type": "web_search"}, {"type": "function", "name": "shell"}, {"type": "custom", "name": "patch"}]}
        self.assertEqual([tool["type"] for tool in app.adapt(payload)["tools"]], ["function", "custom"])

    def test_workspace_rejects_parent_paths(self):
        from pathlib import Path
        with self.assertRaises(ValueError):
            app.workspace_file(Path("/tmp/workbench"), "../secret")

    def test_download_refuses_symbolic_link_to_external_file(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as temp:
            parent = Path(temp)
            root = parent / "workspace"
            root.mkdir()
            target = parent / "outside.txt"
            target.write_text("private")
            link = root / "link.txt"
            link.symlink_to(target)
            with self.assertRaises(OSError):
                with app.open_download(root, link):
                    self.fail("download followed a symlink")

    def test_tool_flag_cannot_supply_multiple_arguments(self):
        with self.assertRaises(ValueError):
            app.tool_flag("--total --extra")
        self.assertEqual(app.tool_flag("--total"), "--total")

    def test_model_config_public_view_withholds_key(self):
        config = {"url": "https://example.com/v1", "model": "test", "key": "private"}
        self.assertEqual(app.public_model(config), {"url": config["url"], "model": "test", "hasKey": True})

    def test_candidate_file_scan_ignores_test_and_packaging_scripts(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "tests").mkdir()
            for name in ("json2csv.py", "setup.py", "test_helper.py", "tests/fixtures.py"):
                (root / name).touch()
            self.assertEqual(app.workspace_files(root, for_tools=True), ["json2csv.py"])

    def test_empty_agent_turn_is_marked_failed_and_retryable(self):
        import json
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from unittest.mock import patch

        with TemporaryDirectory() as temp, patch.object(app, "DATA", Path(temp)):
            app.initialize()
            session = app.create_session("Empty response")
            with app.db() as con:
                run = con.execute("INSERT INTO runs(session,prompt,status) VALUES (?, 'test', 'queued')",
                                  (session,)).lastrowid
            Path(temp).chmod(0o755)
            cli = Path(temp) / "codex"
            cli.write_text("#!/bin/sh\ncat >/dev/null\n" + "echo '" + json.dumps({"type": "turn.completed"}) + "'\n")
            cli.chmod(0o755)
            with patch.object(app.shutil, "which", return_value=str(cli)):
                app.execute((run, session, "test"))
            with app.db() as con:
                result = con.execute("SELECT status, events FROM runs WHERE id = ?", (run,)).fetchone()
            self.assertEqual(result["status"], "failed")
            self.assertIn("空结果", json.loads(result["events"])[-1]["text"])

    def test_model_request_rejects_cross_origin_redirect(self):
        from urllib.error import HTTPError
        from urllib.request import Request

        request = Request("https://source.invalid/models", headers={"Authorization": "Bearer synthetic"})
        with self.assertRaises(HTTPError):
            app.ModelRedirectHandler().redirect_request(
                request, None, 302, "Found", {}, "https://other.invalid/models"
            )

    def test_changed_script_is_discovered_and_updates_saved_tool(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from unittest.mock import patch

        with TemporaryDirectory() as temp, patch.object(app, "DATA", Path(temp)):
            app.initialize()
            session = app.create_session("Tools")
            with app.db() as con:
                con.execute("INSERT INTO runs(session, prompt, status) VALUES (?, 'test', 'completed')", (session,))
            source = Path(temp) / session / "workspace" / "converter.py"
            source.write_text("print(1)")
            payload = {"source": source.name, "name": "Converter"}
            tool = app.register_tool(session, payload)
            self.assertEqual(app.tool_candidates(), [])
            source.write_text("print(2)")
            self.assertEqual(app.tool_candidates()[0]["updateId"], tool)
            self.assertEqual(app.register_tool(session, payload | {"updateId": tool}), tool)
            self.assertEqual(app.tool_candidates(), [])
            with app.db() as con:
                self.assertEqual(con.execute("SELECT script FROM tools").fetchone()["script"], b"print(2)")
