"""Tests for del_fi/bench.py (python main.py --bench)."""

import io
import os
import tempfile
import unittest
import unittest.mock

from del_fi import bench
from tests.test_knowledge import _FakeOllamaClient, _make_engine, _ModelClient, _setup_pages, _write_file

TIMED = dict(prompt_eval_count=600, prompt_eval_duration=10_000_000_000,
             eval_count=70, eval_duration=20_000_000_000, done_reason="stop")


class TestBench(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="delfi-bench-")
        _setup_pages(self.tmpdir, {
            "songs": (["songs", "chickadee"], "## Fee-bee\n\nThe chickadee sings fee-bee."),
            "trails": (["trails", "dogs"], "## Dogs\n\nLeashed dogs are welcome on the Creek Loop."),
        })

    def _run(self, client, questions=None, **cfg):
        engine = _make_engine(self.tmpdir, ollama_client=client, max_response_bytes=230, **cfg)
        path = ""
        if questions is not None:
            path = os.path.join(self.tmpdir, "questions.txt")
            _write_file(path, questions)
        out = io.StringIO()
        with unittest.mock.patch("del_fi.bench.WikiEngine", return_value=engine):
            code = bench.run(engine.cfg, path, out=out)
        return code, out.getvalue(), engine

    def test_load_questions_skips_comments_and_blank_lines(self):
        path = os.path.join(self.tmpdir, "q.txt")
        _write_file(path, "# birding questions\n\nwhat sings fee-bee?\n  # indented comment\n can I bring my dog? \n")
        self.assertEqual(bench.load_questions(path), ["what sings fee-bee?", "can I bring my dog?"])

    def test_reports_timing_answers_and_a_summary(self):
        client = _ModelClient("A Black-capped Chickadee.", **TIMED)
        code, out, _ = self._run(client, "what sings fee-bee, a chickadee?\ncan I bring my dogs?\n")
        self.assertEqual(code, 0)
        self.assertIn("TEST-NODE bench · test-model:3b (4.0B) · profile default", out)
        self.assertIn("2 questions from questions.txt", out)
        self.assertIn("loaded in", out)
        self.assertIn("[1/2] what sings fee-bee, a chickadee?", out)
        self.assertIn("prompt 600 tok at 60.0 tok/s · output 70 tok at 3.5 tok/s · 1 msg(s)", out)
        self.assertIn("│ A Black-capped Chickadee.", out)
        self.assertIn("2 of 2 answered by the model · median", out)
        self.assertIn("prompt 60.0 tok/s · output 3.5 tok/s", out)

    def test_long_answers_count_their_mesh_messages(self):
        long_answer = "The chickadee sings a clear two-note whistle, fee-bee. " * 9
        code, out, _ = self._run(_ModelClient(long_answer), "chickadee song?\n")
        self.assertRegex(out, r"· 3 msg\(s\)")

    def test_questions_without_pages_and_declines_are_labelled(self):
        client = _ModelClient("I don't know.")
        code, out, _ = self._run(client, "zzz qqq\nchickadee song?\n")
        self.assertIn("no matching pages: nothing sent to the model", out)
        self.assertIn("(declined: the model said it didn't know)", out)
        self.assertIn("1 of 2 answered by the model", out)

    def test_errors_are_reported_and_the_run_continues(self):
        class Slow(_FakeOllamaClient):
            def generate(self, **kwargs):
                raise TimeoutError("timed out")

        code, out, _ = self._run(Slow(), "chickadee song?\ndogs on trails?\n")
        self.assertEqual(code, 0)
        self.assertIn("could not preload it", out)
        self.assertEqual(out.count("error (timeout)"), 2)
        self.assertIn("none reached the model", out)

    def test_cut_answers_are_counted(self):
        client = _ModelClient("The chickadee", eval_count=300, eval_duration=10**10, done_reason="length")
        _, out, _ = self._run(client, "chickadee song?\n")
        self.assertIn("stopped at num_predict", out)
        self.assertIn("1 stopped at num_predict", out)

    def test_default_questions_come_from_topics(self):
        _, out, _ = self._run(_ModelClient("Answer."))
        self.assertIn("2 questions from wiki topics", out)
        self.assertIn("Tell me about songs", out)

    def test_setup_problems_exit_1(self):
        cases = [
            ("_ollama_available", False, "not reachable"),
            ("_pulled", set(), "ollama pull test-model:3b"),
        ]
        for attr, value, message in cases:
            engine = _make_engine(self.tmpdir, ollama_client=_ModelClient())
            setattr(engine, attr, value)
            out = io.StringIO()
            with unittest.mock.patch("del_fi.bench.WikiEngine", return_value=engine):
                self.assertEqual(bench.run(engine.cfg, "", out=out), 1)
            self.assertIn(message, out.getvalue())

    def test_empty_wiki_exits_1(self):
        os.remove(os.path.join(self.tmpdir, "wiki", "index.md"))
        code, out, _ = self._run(_ModelClient())
        self.assertEqual(code, 1)
        self.assertIn("--build-wiki", out)


if __name__ == "__main__":
    unittest.main()
