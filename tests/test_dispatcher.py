"""Tests for del_fi/core/dispatcher.py — the daemon's main loop."""

import os
import queue
import tempfile
import threading
import unittest

from del_fi.core.dispatcher import Dispatcher, command_name
from del_fi.core.router import Router


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeRouter:
    """Stands in for Router: classifies like the real one, answers instantly
    unless told to block, and records what it was asked."""

    def __init__(self):
        self.asked: list[tuple[str, str]] = []
        self.last_query: dict[str, str] = {}
        self.block = threading.Event()
        self.block.set()           # clear() to make the next question hang
        self.started = threading.Event()
        self.fail_on: set[str] = set()

    def classify(self, text):
        text = text.strip()
        if not text:
            return "empty"
        if text.startswith("!"):
            return "command"
        if text.startswith("DEL-FI:"):
            return "gossip"
        return "query"

    def route(self, sender, text):
        self.asked.append((sender, text))
        return None

    def route_multi(self, sender, text):
        self.asked.append((sender, text))
        if text in self.fail_on:
            raise RuntimeError("boom")
        if text.startswith("!"):
            return [f"reply to {text}"]
        self.last_query[sender] = text
        self.started.set()
        self.block.wait(5)
        return [f"answer: {text}", "part 2"]

    def prepare_retry(self, sender):
        return self.last_query.get(sender)

    def busy_message(self, position):
        return "yours is next" if position <= 1 else f"{position} ahead"


def make_dispatcher(router=None, **cfg_overrides):
    cfg = {
        "node_name": "TEST-NODE",
        "rate_limit_seconds": 30,
        "busy_notice": True,
        "query_queue_size": 10,
    }
    cfg.update(cfg_overrides)
    sent: list[tuple[str, str]] = []
    sleeps: list[float] = []
    clock = FakeClock()
    d = Dispatcher(
        cfg, router or FakeRouter(), lambda dest, text: sent.append((dest, text)),
        clock=clock, sleep=sleeps.append,
    )
    return d, sent, clock, sleeps


def texts_to(sent, dest):
    return [t for d, t in sent if d == dest]


class TestCommandsAndGossip(unittest.TestCase):
    def test_command_answered_inline_not_queued(self):
        d, sent, _, _ = make_dispatcher()
        d.handle("!a", "!ping")
        self.assertEqual(sent, [("!a", "reply to !ping")])
        self.assertEqual(d.query_queue.qsize(), 0)

    def test_commands_are_not_rate_limited(self):
        d, sent, _, _ = make_dispatcher()
        for _ in range(5):
            d.handle("!a", "!status")
        self.assertEqual(len(sent), 5)

    def test_gossip_handled_silently(self):
        router = FakeRouter()
        d, sent, _, _ = make_dispatcher(router)
        d.handle("!peer", "DEL-FI:1:ANNOUNCE:VALLEY:topics=a")
        self.assertEqual(sent, [])
        self.assertEqual(router.asked, [("!peer", "DEL-FI:1:ANNOUNCE:VALLEY:topics=a")])

    def test_empty_message_ignored(self):
        d, sent, _, _ = make_dispatcher()
        d.handle("!a", "   ")
        self.assertEqual(sent, [])
        self.assertEqual(d.query_queue.qsize(), 0)

    def test_command_error_gets_reply_and_never_raises(self):
        router = FakeRouter()
        router.fail_on.add("!status")
        d, sent, _, _ = make_dispatcher(router)
        d.handle("!a", "!status")
        self.assertIn("error", sent[0][1].lower())

    def test_command_name(self):
        self.assertEqual(command_name("!MORE 2"), "!more")
        self.assertEqual(command_name(""), "")


class TestRateLimit(unittest.TestCase):
    def test_second_question_in_window_gets_one_notice(self):
        d, sent, clock, _ = make_dispatcher()
        d.handle("!a", "first question")
        clock.now += 5
        d.handle("!a", "second question")
        clock.now += 5
        d.handle("!a", "third question")
        self.assertEqual(d.query_queue.qsize(), 1)
        notices = texts_to(sent, "!a")
        self.assertEqual(len(notices), 1, notices)
        self.assertIn("One question per 30s", notices[0])
        self.assertIn("Try again in 25s", notices[0])

    def test_question_accepted_after_window(self):
        d, sent, clock, _ = make_dispatcher()
        d.handle("!a", "first question")
        clock.now += 31
        d.handle("!a", "second question")
        self.assertEqual(d.query_queue.qsize(), 2)
        self.assertEqual(sent, [])

    def test_notice_rearms_after_next_accepted_question(self):
        d, sent, clock, _ = make_dispatcher()
        d.handle("!a", "q1")
        d.handle("!a", "q2")          # notice
        clock.now += 31
        d.handle("!a", "q3")          # accepted
        d.handle("!a", "q4")          # notice again
        self.assertEqual(len(texts_to(sent, "!a")), 2)

    def test_notice_can_be_disabled(self):
        d, sent, _, _ = make_dispatcher(rate_limit_notice=False)
        d.handle("!a", "q1")
        d.handle("!a", "q2")
        self.assertEqual(sent, [])
        self.assertEqual(d.query_queue.qsize(), 1)

    def test_senders_are_independent(self):
        d, sent, _, _ = make_dispatcher()
        for i in range(5):
            d.handle(f"!user{i}", "a question")
        self.assertEqual(d.query_queue.qsize(), 5)
        self.assertEqual(sent, [])

    def test_zero_disables_rate_limit(self):
        d, sent, _, _ = make_dispatcher(rate_limit_seconds=0)
        for _ in range(3):
            d.handle("!a", "a question")
        self.assertEqual(d.query_queue.qsize(), 3)


class TestRetry(unittest.TestCase):
    def test_retry_runs_on_worker_and_is_rate_limited(self):
        router = FakeRouter()
        router.last_query["!a"] = "where is camp"
        d, sent, clock, _ = make_dispatcher(router)
        d.handle("!a", "!retry")
        self.assertEqual(d.query_queue.get_nowait(), ("!a", "where is camp"))
        d.handle("!a", "!retry")
        self.assertIn("One question per", texts_to(sent, "!a")[0])

    def test_retry_without_history_replies_immediately(self):
        d, sent, _, _ = make_dispatcher()
        d.handle("!a", "!retry")
        self.assertIn("No previous query", sent[0][1])
        self.assertEqual(d.query_queue.qsize(), 0)


class TestWorker(unittest.TestCase):
    def test_worker_answers_with_delay_between_chunks(self):
        d, sent, _, sleeps = make_dispatcher()
        d.start()
        try:
            d.handle("!a", "where is camp")
            d.query_queue.join()
        finally:
            d.stop()
        self.assertEqual(texts_to(sent, "!a"), ["answer: where is camp", "part 2"])
        self.assertEqual(sleeps, [0.5])

    def test_worker_error_replies_and_releases_sender(self):
        router = FakeRouter()
        router.fail_on.add("bad question")
        d, sent, clock, _ = make_dispatcher(router)
        d.start()
        try:
            d.handle("!a", "bad question")
            d.query_queue.join()
        finally:
            d.stop()
        self.assertIn("error", texts_to(sent, "!a")[0].lower())
        self.assertEqual(d._pending, {})

    def test_busy_notice_once_per_waiting_sender(self):
        router = FakeRouter()
        router.block.clear()
        d, sent, _, _ = make_dispatcher(router, rate_limit_seconds=0)
        d.start()
        try:
            d.handle("!a", "slow question")
            self.assertTrue(router.started.wait(2))
            d.handle("!b", "question b1")
            d.handle("!b", "question b2")     # already waiting: no second notice
            d.handle("!c", "question c")
            router.block.set()
            d.query_queue.join()
        finally:
            d.stop()
        self.assertEqual(texts_to(sent, "!b")[:1], ["yours is next"])
        self.assertEqual(sum(t == "yours is next" for t in texts_to(sent, "!b")), 1)
        self.assertIn("3 ahead", texts_to(sent, "!c"))  # !a in flight + b1 + b2
        self.assertIn("answer: question b2", texts_to(sent, "!b"))

    def test_no_busy_notice_when_disabled(self):
        router = FakeRouter()
        router.block.clear()
        d, sent, _, _ = make_dispatcher(router, busy_notice=False)
        d.start()
        try:
            d.handle("!a", "slow question")
            self.assertTrue(router.started.wait(2))
            d.handle("!b", "question b")
            router.block.set()
            d.query_queue.join()
        finally:
            d.stop()
        self.assertNotIn("yours is next", texts_to(sent, "!b"))

    def test_queue_full_turns_sender_away(self):
        d, sent, _, _ = make_dispatcher(query_queue_size=2)
        for i in range(3):
            d.handle(f"!user{i}", "a question")
        self.assertEqual(d.query_queue.qsize(), 2)
        self.assertIn("Too many questions", texts_to(sent, "!user2")[0])

    def test_run_loop_stops(self):
        d, sent, _, _ = make_dispatcher()
        inbox: queue.Queue = queue.Queue()
        inbox.put(("!a", "!ping"))
        t = threading.Thread(target=d.run, args=(inbox,))
        t.start()
        while inbox.qsize():
            pass
        d.stop()
        t.join(timeout=3)
        self.assertFalse(t.is_alive())
        self.assertEqual(sent, [("!a", "reply to !ping")])


class TestWithRealRouter(unittest.TestCase):
    """Integration: real Router + Dispatcher, mocked knowledge engine."""

    def test_question_command_and_retry_end_to_end(self):
        from tests.test_router import MockGossipDir, MockPeerCache, MockWiki, _make_cfg

        class CountingWiki(MockWiki):
            calls = 0

            def query(self, text, peer_ctx="", history="", board_context=""):
                CountingWiki.calls += 1
                return f"Answer #{CountingWiki.calls}.", True

        tmpdir = tempfile.mkdtemp(prefix="delfi-dispatch-")
        cfg = _make_cfg(tmpdir, rate_limit_seconds=0)
        router = Router(cfg, CountingWiki(), MockPeerCache(), MockGossipDir())
        d, sent, _, _ = make_dispatcher(router, rate_limit_seconds=0)
        d.start()
        try:
            d.handle("!a", "where is the trailhead")
            d.query_queue.join()
            d.handle("!a", "!ping")
            d.handle("!a", "!retry")
            d.query_queue.join()
        finally:
            d.stop()
        replies = texts_to(sent, "!a")
        self.assertTrue(replies[0].startswith("Answer #1."))
        self.assertIn("pong from TEST-NODE", replies)
        self.assertTrue(replies[-1].startswith("Answer #2."))  # cache bypassed
        self.assertTrue(os.path.isdir(tmpdir))


if __name__ == "__main__":
    unittest.main()
