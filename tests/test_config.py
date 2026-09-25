"""Tests for del_fi/config.py — config loading and validation."""

import os
import tempfile
import unittest

from del_fi.config import load_config


def _write_config(tmpdir: str, content: str) -> str:
    """Write a config string to a temp file and return the path."""
    cfg_file = os.path.join(tmpdir, "config.yaml")
    with open(cfg_file, "w", encoding="utf-8") as f:
        f.write(content)
    return cfg_file


class TestValidConfig(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="delfi-cfgtest-")

    def test_minimal_config(self):
        path = _write_config(self.tmpdir, 'node_name: "TEST-1"\nmodel: "qwen3:4b"\n')
        cfg = load_config(path)
        self.assertEqual(cfg["node_name"], "TEST-1")
        self.assertEqual(cfg["model"], "qwen3:4b")
        self.assertEqual(cfg["max_response_bytes"], 230)
        self.assertEqual(cfg["rate_limit_seconds"], 30)
        self.assertEqual(cfg["radio_connection"], "serial")

    def test_all_fields(self):
        content = """
node_name: "MY-NODE"
model: "llama3:8b"
personality: "Grumpy librarian."
knowledge_folder: /tmp/knowledge
max_response_bytes: 200
radio_connection: tcp
radio_port: "192.168.1.100:4403"
rate_limit_seconds: 30
response_cache_ttl: 600
embedding_model: "nomic-embed-text"
ollama_timeout: 60
log_level: debug
"""
        path = _write_config(self.tmpdir, content)
        cfg = load_config(path)
        self.assertEqual(cfg["node_name"], "MY-NODE")
        self.assertEqual(cfg["personality"], "Grumpy librarian.")
        self.assertEqual(cfg["max_response_bytes"], 200)
        self.assertEqual(cfg["radio_connection"], "tcp")
        self.assertEqual(cfg["log_level"], "debug")

    def test_log_level_normalized(self):
        path = _write_config(self.tmpdir, 'node_name: "T"\nmodel: "m"\nlog_level: WARNING\n')
        cfg = load_config(path)
        self.assertEqual(cfg["log_level"], "warning")


class TestInvalidConfig(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="delfi-cfgtest-")

    def test_missing_file(self):
        with self.assertRaises(SystemExit):
            load_config(os.path.join(self.tmpdir, "nonexistent.yaml"))

    def test_empty_file(self):
        path = _write_config(self.tmpdir, "")
        with self.assertRaises(SystemExit):
            load_config(path)

    def test_missing_node_name(self):
        path = _write_config(self.tmpdir, 'model: "qwen2.5:7b"\n')
        with self.assertRaises(SystemExit):
            load_config(path)

    def test_missing_model_uses_default(self):
        path = _write_config(self.tmpdir, 'node_name: "TEST"\n')
        cfg = load_config(path)
        self.assertIn("model", cfg)

    def test_wrong_type_max_bytes(self):
        path = _write_config(
            self.tmpdir,
            'node_name: "T"\nmodel: "m"\nmax_response_bytes: "not a number"\n',
        )
        with self.assertRaises(SystemExit):
            load_config(path)

    def test_invalid_mesh_protocol(self):
        path = _write_config(
            self.tmpdir, 'node_name: "T"\nmodel: "m"\nmesh_protocol: "wifi"\n'
        )
        with self.assertRaises(SystemExit):
            load_config(path)

    def test_invalid_rate_limit(self):
        path = _write_config(
            self.tmpdir, 'node_name: "T"\nmodel: "m"\nrate_limit_seconds: -5\n'
        )
        with self.assertRaises(SystemExit):
            load_config(path)

    def test_negative_max_bytes(self):
        path = _write_config(
            self.tmpdir, 'node_name: "T"\nmodel: "m"\nmax_response_bytes: -1\n'
        )
        with self.assertRaises(SystemExit):
            load_config(path)

    def test_invalid_yaml(self):
        path = _write_config(self.tmpdir, ":\n  :\n    [invalid yaml]]]")
        with self.assertRaises(SystemExit):
            load_config(path)

    def test_non_mapping_yaml(self):
        path = _write_config(self.tmpdir, "- a list\n- not a mapping\n")
        with self.assertRaises(SystemExit):
            load_config(path)


class TestMeshProtocol(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="delfi-cfgtest-")

    def test_default_protocol_is_meshtastic(self):
        path = _write_config(self.tmpdir, 'node_name: "T"\nmodel: "m"\n')
        cfg = load_config(path)
        self.assertEqual(cfg["mesh_protocol"], "meshtastic")

    def test_meshcore_protocol(self):
        content = 'node_name: "T"\nmodel: "m"\nmesh_protocol: meshcore\n'
        path = _write_config(self.tmpdir, content)
        cfg = load_config(path)
        self.assertEqual(cfg["mesh_protocol"], "meshcore")

    def test_invalid_protocol(self):
        path = _write_config(
            self.tmpdir, 'node_name: "T"\nmodel: "m"\nmesh_protocol: zigbee\n'
        )
        with self.assertRaises(SystemExit):
            load_config(path)


class TestWikiConfig(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="delfi-cfgtest-")

    def test_wiki_defaults_present(self):
        path = _write_config(self.tmpdir, 'node_name: "T"\nmodel: "m"\n')
        cfg = load_config(path)
        self.assertIn("wiki_folder", cfg)
        self.assertFalse(cfg.get("wiki_rebuild_on_start", True))
        self.assertEqual(cfg.get("wiki_stale_after_days", 30), 30)

    def test_wiki_builder_model_override(self):
        content = 'node_name: "T"\nmodel: "gemma3:1b"\nwiki_builder_model: "qwen2.5:7b"\n'
        path = _write_config(self.tmpdir, content)
        cfg = load_config(path)
        self.assertEqual(cfg["wiki_builder_model"], "qwen2.5:7b")


class TestValidationV03(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="delfi-cfgtest-")

    def _read(self, content):
        from del_fi.config import read_config
        return read_config(_write_config(self.tmpdir, content))

    def _error(self, content) -> str:
        from del_fi.config import ConfigError
        with self.assertRaises(ConfigError) as ctx:
            self._read(content)
        return str(ctx.exception)

    def test_read_config_raises_instead_of_exiting(self):
        self.assertIn("node_name", self._error("model: m\n"))

    def test_non_mapping_yaml(self):
        self.assertIn("mapping", self._error("- just\n- a list\n"))

    def test_model_must_be_a_name(self):
        self.assertIn("model", self._error('node_name: "T"\nmodel: null\n'))

    def test_radio_connection_checked(self):
        self.assertIn("radio_connection", self._error('node_name: "T"\nradio_connection: wifi\n'))

    def test_log_level_checked(self):
        self.assertIn("log_level", self._error('node_name: "T"\nlog_level: loud\n'))

    def test_integer_settings_checked(self):
        self.assertIn("auto_send_chunks", self._error('node_name: "T"\nauto_send_chunks: 0\n'))
        self.assertIn("num_ctx", self._error('node_name: "T"\nnum_ctx: 100\n'))

    def test_unknown_keys_warned(self):
        with self.assertLogs("del_fi.config", level="WARNING") as logs:
            self._read('node_name: "T"\nrate_limit_second: 5\n')
        self.assertIn("rate_limit_second", logs.output[0])

    def test_config_path_and_log_file_resolved(self):
        cfg = self._read('node_name: "T"\nlog_file: logs/delfi.log\n')
        self.assertEqual(cfg["_config_path"], os.path.join(os.path.realpath(self.tmpdir), "config.yaml"))
        self.assertEqual(cfg["log_file"], os.path.join(os.path.realpath(self.tmpdir), "logs", "delfi.log"))

    def test_relative_paths_normalised(self):
        cfg = self._read('node_name: "T"\nwiki_folder: ./wiki\nknowledge_folder: ./kb/../knowledge\n')
        root = os.path.realpath(self.tmpdir)
        self.assertEqual(cfg["wiki_folder"], os.path.join(root, "wiki"))
        self.assertEqual(cfg["knowledge_folder"], os.path.join(root, "knowledge"))

    def test_node_name_coerced_to_string(self):
        self.assertEqual(self._read("node_name: 42\n")["node_name"], "42")


class TestMeshKnowledge(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="delfi-cfgtest-")

    def _load(self, extra: str):
        return load_config(_write_config(self.tmpdir, 'node_name: "T"\nmodel: "m"\n' + extra))

    def _dies(self, extra: str):
        with self.assertRaises(SystemExit):
            self._load(extra)

    def test_defaults_present_when_block_absent(self):
        mk = self._load("")["mesh_knowledge"]
        self.assertFalse(mk["gossip"]["enabled"])
        self.assertEqual(mk["gossip"]["announce_interval"], 14400)
        self.assertEqual(mk["peers"], [])
        self.assertEqual(mk["sync"]["max_cache_age"], 7 * 86400)

    def test_documented_block_parsed(self):
        mk = self._load("""
mesh_knowledge:
  gossip:
    enabled: true
    announce_interval: 6h
    channel: 1
  peers:
    - node_id: "!a1b2c3d4"
      name: "MARINA-ORACLE"
""")["mesh_knowledge"]
        self.assertTrue(mk["gossip"]["enabled"])
        self.assertEqual(mk["gossip"]["announce_interval"], 6 * 3600)
        self.assertEqual(mk["gossip"]["channel"], 1)
        self.assertEqual(mk["gossip"]["directory_ttl"], 86400)  # default kept
        self.assertEqual(mk["peers"][0]["node_id"], "!a1b2c3d4")

    def test_legacy_top_level_keys_mapped(self):
        with self.assertLogs("del_fi.config", level="WARNING"):
            mk = self._load("""
trusted_peers:
  - "!a1b2c3d4"
  - MARINA-ORACLE
gossip_announce_interval: 7200
max_cache_entries: 50
""")["mesh_knowledge"]
        self.assertEqual(mk["peers"], [{"node_id": "!a1b2c3d4"}])  # names dropped
        self.assertEqual(mk["gossip"]["announce_interval"], 7200)
        self.assertEqual(mk["sync"]["max_cache_entries"], 50)

    def test_announce_interval_floor(self):
        self._dies("mesh_knowledge:\n  gossip:\n    announce_interval: 60\n")

    def test_peer_needs_node_id(self):
        self._dies("mesh_knowledge:\n  peers:\n    - name: MARINA-ORACLE\n")

    def test_bad_channel_rejected(self):
        self._dies("mesh_knowledge:\n  gossip:\n    channel: 9\n")

    def test_bad_duration_rejected(self):
        self._dies("mesh_knowledge:\n  sync:\n    max_cache_age: soon\n")


class TestParseDuration(unittest.TestCase):
    def test_units(self):
        from del_fi.config import parse_duration
        self.assertEqual(parse_duration("30s"), 30)
        self.assertEqual(parse_duration("15m"), 900)
        self.assertEqual(parse_duration("12h"), 43200)
        self.assertEqual(parse_duration("7d"), 604800)
        self.assertEqual(parse_duration(3600), 3600)
        self.assertIsNone(parse_duration("soon"))
        self.assertIsNone(parse_duration(True))
        self.assertIsNone(parse_duration(-5))


class TestOracleProfiles(unittest.TestCase):
    """The default used to be gemma4:4b, which is not an Ollama tag
    (Gemma 4 ships as gemma4:e2b, e4b, 12b, 26b, 31b)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="delfi-cfgtest-")

    def test_default_model_is_a_real_tag_with_a_profile(self):
        from del_fi.config import DEFAULTS, ORACLE_PROFILES, _match_profile
        self.assertEqual(DEFAULTS["model"], "gemma4:e4b")
        self.assertIs(_match_profile(DEFAULTS["model"]), ORACLE_PROFILES["gemma4:e4b"])

    def test_gemma4_e2b_gets_the_small_model_profile(self):
        path = _write_config(self.tmpdir, 'node_name: "T"\nmodel: "gemma4:e2b"\n')
        cfg = load_config(path)
        self.assertEqual(cfg["max_context_tokens"], 512)
        self.assertTrue(cfg["small_model_prompt"])

    def test_tag_variants_match_their_profile(self):
        from del_fi.config import ORACLE_PROFILES, _match_profile
        self.assertIs(_match_profile("gemma4:e4b-it-qat"), ORACLE_PROFILES["gemma4:e4b"])
        self.assertIs(_match_profile("GEMMA4:E2B"), ORACLE_PROFILES["gemma4:e2b"])
        self.assertIsNone(_match_profile("gemma4:26b"))


if __name__ == "__main__":
    unittest.main()

