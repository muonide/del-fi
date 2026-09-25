"""Tests for main.py — logging setup."""

import io
import logging
import unittest

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


if __name__ == "__main__":
    unittest.main()
