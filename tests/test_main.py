"""Tests for main.py — logging setup, model warm-up, command line."""

import io
import logging
import os
import sys
import tempfile
import unittest
import unittest.mock

import main


class TestLogFormatter(unittest.TestCase):
    def _logger(self):
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setFormatter(main._DelFiFormatter())
        logger = logging.getLogger(f"test_main.{id(buf)}")
        logger.addHandler(handler)
        logger.propagate = False
        return logger, buf

    def test_plain_message_has_timestamp_prefix(self):
        logger, buf = self._logger()
        logger.warning("radio not connected")
        self.assertRegex(buf.getvalue(), r"^\[\d\d:\d\d:\d\d\] radio not connected\n$")

    def test_exception_includes_traceback(self):
        logger, buf = self._logger()
        try:
            {}["missing"]
        except KeyError:
            logger.exception("disk cache save failed")
        out = buf.getvalue()
        self.assertIn("disk cache save failed", out)
        self.assertIn("Traceback (most recent call last)", out)
        self.assertIn("KeyError: 'missing'", out)


class TestSetupLogging(unittest.TestCase):
    def test_httpx_request_logs_suppressed_at_info(self):
        root = logging.getLogger()
        before, level = list(root.handlers), root.level
        httpx_level = logging.getLogger("httpx").level
        try:
            main.setup_logging("info")
            self.assertEqual(logging.getLogger("httpx").level, logging.WARNING)
        finally:
            for h in list(root.handlers):
                if h not in before:
                    root.removeHandler(h)
            root.setLevel(level)
            logging.getLogger("httpx").setLevel(httpx_level)


class _FakeWiki:
    def __init__(self, available):
        self.available = available
        self.warm_ups = 0

    def warm_up(self):
        self.warm_ups += 1

    def check_ollama(self):
        self.available = True
        return True


class _Stop:
    """stop.wait() returns these values in turn, then True (stop)."""

    def __init__(self, *waits):
        self.waits = list(waits)

    def wait(self, timeout=None):
        return self.waits.pop(0) if self.waits else True


class TestHealthCheck(unittest.TestCase):
    def test_model_is_loaded_at_startup(self):
        wiki = _FakeWiki(available=True)
        main.ollama_health_check(wiki, _Stop())
        self.assertEqual(wiki.warm_ups, 1)

    def test_model_is_loaded_when_ollama_comes_back(self):
        wiki = _FakeWiki(available=False)
        main.ollama_health_check(wiki, _Stop(False))
        self.assertTrue(wiki.available)
        self.assertEqual(wiki.warm_ups, 1)


class TestCommandLine(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="delfi-main-")
        self.config = os.path.join(self.tmpdir, "config.yaml")
        with open(self.config, "w", encoding="utf-8") as f:
            f.write('node_name: "T"\nmodel: "gemma4:e4b"\n')
        root = logging.getLogger()
        handlers, level = list(root.handlers), root.level
        delfi_level = logging.getLogger("del_fi").level

        def restore():
            for h in list(root.handlers):
                if h not in handlers:
                    root.removeHandler(h)
            root.setLevel(level)
            logging.getLogger("del_fi").setLevel(delfi_level)

        self.addCleanup(restore)

    def _main(self, *args):
        with unittest.mock.patch.object(sys, "argv", ["main.py", "--config", self.config, *args]), \
                unittest.mock.patch("del_fi.bench.run", return_value=0) as run, \
                self.assertRaises(SystemExit) as exit_:
            main.main()
        return exit_.exception.code, run

    def test_bench_with_questions_and_model(self):
        code, run = self._main("--bench", "questions.txt", "--model", "gemma3:1b")
        self.assertEqual(code, 0)
        cfg, path = run.call_args.args
        self.assertEqual(path, "questions.txt")
        self.assertEqual(cfg["model"], "gemma3:1b")
        self.assertEqual(cfg["max_context_tokens"], 512)  # gemma3:1b's profile
        self.assertEqual(logging.getLogger("del_fi").level, logging.WARNING)

    def test_bench_without_a_file_uses_topics(self):
        code, run = self._main("--bench")
        self.assertEqual(run.call_args.args[1], "")
        self.assertEqual(run.call_args.args[0]["model"], "gemma4:e4b")


if __name__ == "__main__":
    unittest.main()
