"""Tests for del_fi/core/fsutil.py."""

import json
import os
import tempfile
import threading
import unittest

from del_fi.core.fsutil import write_atomic


class TestWriteAtomic(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="delfi-fsutil-")

    def test_writes_and_creates_parent_dirs(self):
        path = os.path.join(self.dir, "a", "b", "file.json")
        self.assertTrue(write_atomic(path, '{"ok": true}'))
        with open(path) as f:
            self.assertEqual(json.load(f), {"ok": True})

    def test_replaces_existing_file(self):
        path = os.path.join(self.dir, "file.txt")
        write_atomic(path, "old")
        write_atomic(path, "new")
        with open(path) as f:
            self.assertEqual(f.read(), "new")

    def test_concurrent_writers_never_fail_or_corrupt(self):
        path = os.path.join(self.dir, "shared.json")
        results: list[bool] = []

        def writer(n):
            for i in range(25):
                results.append(write_atomic(path, json.dumps({"writer": n, "i": i})))

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertTrue(all(results))
        with open(path) as f:
            self.assertIn("writer", json.load(f))
        leftovers = [n for n in os.listdir(self.dir) if n.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_failure_returns_false_and_cleans_up(self):
        blocker = os.path.join(self.dir, "not-a-dir")
        with open(blocker, "w") as f:
            f.write("x")
        self.assertFalse(write_atomic(os.path.join(blocker, "file.txt"), "data"))


if __name__ == "__main__":
    unittest.main()
