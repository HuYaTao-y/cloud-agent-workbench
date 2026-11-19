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
