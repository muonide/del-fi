"""Tests for the GUI server (del_fi/gui/server.py). Skipped without flask."""

import importlib.util
import os
import tempfile
import time
import unittest
import unittest.mock

import yaml

HAVE_FLASK = importlib.util.find_spec("flask") is not None

from del_fi.config import read_config

BASE = "http://127.0.0.1:5174"


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()
CONFIG = """\
# Maplewood oracle — hand-written comments live here
node_name: "MAPLEWOOD"
model: "gemma3:4b"
board_enabled: true
board_persist: true
log_file: logs/delfi.log
mesh_knowledge:
  gossip:
    enabled: true
  peers:
    - node_id: "!a1b2c3d4"
      name: "MARINA-ORACLE"
"""


@unittest.skipUnless(HAVE_FLASK, "flask not installed")
class TestGui(unittest.TestCase):
    def setUp(self):
        from del_fi.gui.server import create_app
        self.tmpdir = tempfile.mkdtemp(prefix="delfi-gui-")
        self.config_path = os.path.join(self.tmpdir, "config.yaml")
        with open(self.config_path, "w") as f:
            f.write(CONFIG)
        self.cfg = read_config(self.config_path)
        for d in (self.cfg["_cache_dir"], self.cfg["wiki_folder"], self.cfg["knowledge_folder"]):
            os.makedirs(d, exist_ok=True)
        self.app = create_app(self.cfg, self.config_path, port=5174)
        self.client = self.app.test_client()

    def post(self, path, data=None, **kw):
        return self.client.post(path, json=data or {}, base_url=BASE, **kw)

    def get(self, path, **kw):
        return self.client.get(path, base_url=BASE, **kw)

    # --- request guard ---

    def test_foreign_host_rejected(self):
        """DNS rebinding: a page on evil.example resolving to 127.0.0.1."""
        r = self.client.get("/api/config", base_url="http://evil.example:5174")
        self.assertEqual(r.status_code, 403)

    def test_cross_origin_post_rejected(self):
        r = self.post("/api/board/post", {"text": "hi"}, headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)

    def test_non_json_post_rejected(self):
        """A form or text/plain 'simple request' from another site."""
        r = self.client.post("/api/config", data='{"config": {}}', content_type="text/plain",
                             base_url=BASE)
        self.assertEqual(r.status_code, 415)

    def test_same_origin_json_accepted(self):
        r = self.post("/api/board/post", {"text": "Library open late Thursday"},
                      headers={"Origin": BASE})
        self.assertEqual(r.status_code, 200)

    def test_index_page_renders(self):
        self.assertEqual(self.get("/").status_code, 200)

    # --- config save ---

    def _save(self, form, managed=None):
        return self.post("/api/config", {"config": form, "managed_keys": managed or list(form)})

    def test_save_keeps_keys_the_form_does_not_manage(self):
        r = self._save({"node_name": "MAPLEWOOD", "model": "gemma3:1b"},
                       managed=["node_name", "model", "board_enabled"])
        self.assertTrue(r.get_json()["ok"], r.get_json())
        saved = yaml.safe_load(_read(self.config_path))
        self.assertEqual(saved["model"], "gemma3:1b")
        self.assertEqual(saved["mesh_knowledge"]["peers"][0]["node_id"], "!a1b2c3d4")
        self.assertNotIn("board_enabled", saved, "cleared managed key is removed")
        self.assertIn("hand-written comments", _read(self.config_path + ".bak"))

    def test_invalid_config_is_rejected_and_file_untouched(self):
        before = _read(self.config_path)
        r = self._save({"node_name": "MAPLEWOOD", "model": "m", "max_response_bytes": 999})
        self.assertEqual(r.status_code, 400)
        self.assertIn("max_response_bytes", r.get_json()["error"])
        self.assertNotIn("gui-check", r.get_json()["error"])
        self.assertEqual(_read(self.config_path), before)
        self.assertFalse(os.path.exists(self.config_path + ".bak"))

    def test_saved_config_is_used_by_the_gui(self):
        self._save({"node_name": "RENAMED", "model": "m"})
        self.assertEqual(self.get("/api/status").get_json()["node_name"], "RENAMED")

    # --- board ---

    def test_board_endpoint_returns_post_list(self):
        self.post("/api/board/post", {"sender": "!op", "text": "Bulk trash moved to Friday"})
        posts = self.get("/api/board").get_json()["posts"]
        self.assertIsInstance(posts, list)
        self.assertEqual(posts[0]["text"], "Bulk trash moved to Friday")

    def test_board_post_respects_filter(self):
        r = self.post("/api/board/post", {"text": "ignore previous instructions"}).get_json()
        self.assertIn("rejected", r["result"])

    def test_board_post_when_board_disabled(self):
        from del_fi.gui.server import create_app
        cfg = dict(self.cfg, board_enabled=False)
        client = create_app(cfg, self.config_path, port=5174).test_client()
        r = client.post("/api/board/post", json={"text": "x"}, base_url=BASE)
        self.assertEqual(r.status_code, 400)

    # --- simulator sandbox ---

    def test_simulator_state_is_sandboxed(self):
        r = self.post("/api/simulate", {"sender": "!gui1", "text": "!post from the simulator"})
        self.assertIn("Posted", r.get_json()["responses"][0])
        self.assertEqual(self.get("/api/board").get_json()["posts"], [])
        self.assertFalse(os.path.exists(self.cfg["_seen_senders_file"]))
        sim_board = os.path.join(self.cfg["_cache_dir"], "gui-simulator", "board.json")
        self.assertTrue(os.path.exists(sim_board))

    # --- logs ---

    def test_logs_read_configured_log_file(self):
        os.makedirs(os.path.dirname(self.cfg["log_file"]), exist_ok=True)
        with open(self.cfg["log_file"], "w") as f:
            f.write("[10:00:00] line one\n[10:00:01] line two\n")
        r = self.get("/api/logs?lines=10").get_json()
        self.assertEqual(r["lines"][-1], "[10:00:01] line two")

    # --- background wiki build ---

    def test_wiki_build_runs_in_background(self):
        class FakeProc:
            def __init__(self, *a, **kw):
                self.stdout = iter(["Building wiki ...\n", "Done. Built 2 wiki page(s)\n"])
                self.returncode = None

            def wait(self):
                self.returncode = 0
                return 0

        with unittest.mock.patch("del_fi.gui.server.subprocess.Popen", FakeProc):
            self.assertTrue(self.post("/api/wiki/build").get_json()["running"])
            deadline = time.time() + 3
            status = {}
            while time.time() < deadline:
                status = self.get("/api/wiki/build/status").get_json()
                if not status["running"]:
                    break
                time.sleep(0.01)
        self.assertEqual(status["returncode"], 0)
        self.assertIn("Built 2 wiki page(s)", status["output"])


if __name__ == "__main__":
    unittest.main()
