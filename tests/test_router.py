"""Tests for router.py — command parsing, !more cursor, edge cases."""

import os
import time
import tempfile
import unittest

from del_fi.core.router import MoreBuffer, Router
from tests._support import function_suite


# --- MoreBuffer ---


def test_more_buffer_next_chunk():
    buf = MoreBuffer(["chunk1", "chunk2", "chunk3"], time.time())
    # cursor starts at 0 (first chunk already sent)
    c1 = buf.next_chunk()
    assert c1 is not None
    assert "chunk2" in c1
    assert "[!more]" in c1  # more chunks remain

    c2 = buf.next_chunk()
    assert c2 is not None
    assert "chunk3" in c2
    assert "[!more]" not in c2  # last chunk, no more indicator

    c3 = buf.next_chunk()
    assert c3 is None  # exhausted


def test_more_buffer_specific_chunk():
    buf = MoreBuffer(["one", "two", "three"], time.time())

    c = buf.get_chunk(2)  # 1-indexed
    assert c is not None
    assert "two" in c
    assert "[!more]" in c  # chunk 3 still exists

    c = buf.get_chunk(3)
    assert c is not None
    assert "three" in c
    assert "[!more]" not in c  # last chunk

    c = buf.get_chunk(4)
    assert c is None  # out of range

    c = buf.get_chunk(0)
    assert c is None  # 0 is invalid (1-indexed)


def test_more_buffer_expiry():
    # Buffer with old timestamp should be expired
    buf = MoreBuffer(["a", "b"], time.time() - 700)
    assert buf.expired

    buf2 = MoreBuffer(["a", "b"], time.time())
    assert not buf2.expired


def test_more_buffer_total_chunks():
    buf = MoreBuffer(["a", "b", "c", "d"], time.time())
    assert buf.total_chunks == 4


# --- Router command parsing (with mock WikiEngine/peers) ---


class MockWiki:
    """Minimal mock for WikiEngine."""

    def __init__(self):
        self._ollama_available = True
        self._rag_available = True
        self._page_count = 5

    @property
    def available(self):
        return self._ollama_available

    @property
    def rag_available(self):
        return self._rag_available

    @property
    def page_count(self):
        return self._page_count

    def get_topics(self):
        return ["solar-power", "trail-guide", "first-aid"]

    def query(self, text, peer_ctx="", history="", board_context=""):
        return "Mock LLM response about your question.", True

    def suggest(self, text):
        return None


class MockPeerCache:
    """Minimal mock for PeerCache."""
    def lookup(self, query):
        return None
    def store(self, *a, **kw):
        pass


class MockGossipDir:
    """Minimal mock for GossipDirectory."""
    enabled = True

    @property
    def peer_count(self):
        return 0
    def list_peers(self):
        return []
    def receive(self, node_id, text):
        pass
    def referral(self, query):
        return None
    def announce(self):
        return ""


def _make_cfg(tmpdir: str, **overrides) -> dict:
    cfg = {
        "node_name": "TEST-NODE",
        "model": "test-model:3b",
        "max_response_bytes": 230,
        "rate_limit_seconds": 30,
        "response_cache_ttl": 300,
        "personality": "Helpful test assistant.",
        "knowledge_folder": os.path.join(tmpdir, "knowledge"),
        "_seen_senders_file": os.path.join(tmpdir, "seen-senders.txt"),
        "_base_dir": tmpdir,
        "_cache_dir": os.path.join(tmpdir, "cache"),
        "_gossip_dir": os.path.join(tmpdir, "gossip"),
        "_vectorstore_dir": os.path.join(tmpdir, "vectorstore"),
        "embedding_model": "nomic-embed-text",
        "ollama_host": "http://localhost:11434",
        "ollama_timeout": 120,
        "persistent_cache": False,
        "fallback_message": "I don't have docs on that. Try !topics.",
    }
    cfg.update(overrides)
    for d in ("knowledge", "cache", "gossip", "vectorstore"):
        os.makedirs(os.path.join(tmpdir, d), exist_ok=True)
    return cfg


def _make_router(**cfg_overrides):
    """Create a Router with mock dependencies and isolated temp state."""
    tmpdir = tempfile.mkdtemp(prefix="delfi-test-")
    cfg = _make_cfg(tmpdir, **cfg_overrides)
    return Router(cfg, MockWiki(), MockPeerCache(), MockGossipDir())


def test_cmd_ping():
    router = _make_router()
    response = router.route("!sender1", "!ping")
    assert "pong" in response.lower()
    assert "TEST-NODE" in response


def test_cmd_help():
    router = _make_router()
    response = router.route("!sender1", "!help")
    assert "TEST-NODE" in response
    assert "!topics" in response
    assert "!more" in response


def test_cmd_status():
    router = _make_router()
    response = router.route("!sender1", "!status")
    assert "TEST-NODE" in response
    assert "test-model:3b" in response
    assert "wiki pages" in response


def test_cmd_topics():
    router = _make_router()
    response = router.route("!sender1", "!topics")
    assert "solar-power" in response
    assert "trail-guide" in response
    assert "first-aid" in response


def test_cmd_unknown():
    router = _make_router()
    response = router.route("!sender1", "!foobar")
    assert "Unknown command" in response
    assert "!help" in response


def test_cmd_peers_empty():
    router = _make_router()
    response = router.route("!sender1", "!peers")
    # MockGossipDir returns [] → "no other nodes" message
    assert "No other" in response or "no" in response.lower()


def test_cmd_more_no_buffer():
    router = _make_router()
    response = router.route("!sender1", "!more")
    assert "No pending" in response


def test_cmd_more_with_buffer():
    router = _make_router()
    # Manually inject a buffer
    router._more_buffers["!sender1"] = MoreBuffer(
        ["first chunk", "second chunk", "third chunk"], time.time()
    )
    response = router.route("!sender1", "!more")
    assert "second chunk" in response

    response2 = router.route("!sender1", "!more")
    assert "third chunk" in response2

    response3 = router.route("!sender1", "!more")
    assert "End of response" in response3


def test_cmd_more_specific_chunk():
    router = _make_router()
    router._more_buffers["!sender1"] = MoreBuffer(
        ["one", "two", "three"], time.time()
    )
    response = router.route("!sender1", "!more 2")
    assert "two" in response


def test_cmd_more_invalid_chunk():
    router = _make_router()
    router._more_buffers["!sender1"] = MoreBuffer(["one", "two"], time.time())
    response = router.route("!sender1", "!more 5")
    assert "No chunk 5" in response


def test_cmd_case_insensitive():
    router = _make_router()
    r1 = router.route("!sender1", "!PING")
    assert "pong" in r1.lower()

    r2 = router.route("!sender1", "!Help")
    assert "!topics" in r2


# --- Greeting detection ---


def test_greeting_first_contact():
    router = _make_router()
    response = router.route("!newsender", "hello")
    assert "Hi from TEST-NODE" in response


def test_greeting_returning_user():
    router = _make_router()
    # First contact triggers greeting
    router.route("!sender1", "hello")
    # Second message: should get LLM response, not intro
    response = router.route("!sender1", "hello")
    assert "Mock LLM response" in response or "Hi from" not in response


# --- Empty / whitespace ---


def test_empty_message():
    router = _make_router()
    response = router.route("!sender1", "")
    assert response is None


def test_whitespace_message():
    router = _make_router()
    response = router.route("!sender1", "   ")
    assert response is None


# --- classify() ---


def test_classify_empty():
    router = _make_router()
    assert router.classify("") == "empty"
    assert router.classify("   ") == "empty"


def test_classify_command():
    router = _make_router()
    assert router.classify("!help") == "command"
    assert router.classify("!PING") == "command"
    assert router.classify("!more 2") == "command"


def test_classify_gossip():
    router = _make_router()
    assert router.classify("DEL-FI:1:ANNOUNCE:RIDGE:topics=weather") == "gossip"


def test_classify_query():
    router = _make_router()
    assert router.classify("What time is the concert?") == "query"
    assert router.classify("hello") == "query"


# --- busy_message() ---


def test_busy_message_next():
    router = _make_router()
    msg = router.busy_message(1)
    assert "TEST-NODE" in msg
    assert "next" in msg.lower()


def test_busy_message_queued():
    router = _make_router()
    msg = router.busy_message(3)
    assert "TEST-NODE" in msg
    assert "3" in msg
    assert "hang tight" in msg.lower()


# --- Byte-limit enforcement ---


def test_enforce_limit_truncates_oversized_command():
    from del_fi.core.formatter import byte_len
    router = _make_router()
    long_text = "A" * 250
    result = router._enforce_limit(long_text)
    assert byte_len(result) <= 230


def test_enforce_limit_passes_short_text():
    router = _make_router()
    short = "Hello world."
    assert router._enforce_limit(short) == short


def test_enforce_limit_none():
    router = _make_router()
    assert router._enforce_limit(None) is None


def test_all_commands_fit_byte_limit():
    """Every built-in command response fits within max_response_bytes."""
    from del_fi.core.formatter import byte_len
    router = _make_router()
    max_bytes = router.cfg["max_response_bytes"]

    commands = [
        "!help", "!status", "!topics", "!ping", "!peers",
        "!more", "!retry", "!data", "!foobar",
    ]
    for cmd in commands:
        response = router.route("!testlimit", cmd)
        if response is not None:
            assert byte_len(response) <= max_bytes, (
                f"{cmd} response is {byte_len(response)}B, "
                f"exceeds {max_bytes}B limit: {response!r}"
            )


# --- route_multi() ---


def _make_router_with_long_answer(text: str, max_bytes: int = 100):
    """Router with a wiki that always returns a specific long answer."""
    tmpdir = tempfile.mkdtemp(prefix="delfi-test-")

    class _LongWiki(MockWiki):
        def query(self, q, peer_ctx="", history="", board_context=""):
            return text, True

    cfg = _make_cfg(tmpdir, max_response_bytes=max_bytes, rate_limit_seconds=0)
    return Router(cfg, _LongWiki(), MockPeerCache(), MockGossipDir())


def test_route_multi_single_chunk_returns_list():
    """Short response returns a 1-element list."""
    router = _make_router()
    result = router.route_multi("!sender1", "!ping")
    assert isinstance(result, list)
    assert len(result) == 1
    assert "pong" in result[0].lower()


def test_route_multi_none_on_empty():
    router = _make_router()
    result = router.route_multi("!sender1", "")
    assert result is None


def test_route_multi_auto_sends_two_chunks():
    """2-chunk response → both auto-sent, no [!more] on last."""
    answer = ("A " * 35 + ". ") + ("B " * 35 + ".")
    router = _make_router_with_long_answer(answer, max_bytes=80)
    result = router.route_multi("!testuser", "tell me something")
    assert isinstance(result, list)
    assert len(result) == 2, f"expected 2, got {len(result)}: {result}"
    assert "[!more]" not in result[-1]


def test_route_multi_auto_sends_three_chunks():
    """3-chunk response → all auto-sent."""
    part = "Word " * 14 + ". "
    answer = part + part + part
    router = _make_router_with_long_answer(answer, max_bytes=80)
    result = router.route_multi("!testuser", "tell me something")
    assert isinstance(result, list)
    assert len(result) >= 2
    assert "[!more]" not in result[-1]


def test_route_multi_prompts_more_beyond_window():
    """4+ chunk response → last auto-sent chunk ends with [!more]."""
    sentence = "This is a sentence about the topic at hand. "
    answer = sentence * 8
    router = _make_router_with_long_answer(answer, max_bytes=80)
    result = router.route_multi("!testuser", "tell me everything")
    assert isinstance(result, list)
    assert len(result) == 3
    assert result[-1].endswith("[!more]"), (
        f"expected [!more] on last auto-sent chunk: {result[-1]!r}"
    )
    assert "[!more]" not in result[0]
    assert "[!more]" not in result[1]


def test_route_multi_more_buffer_cursor_advanced():
    """After route_multi(), !more returns chunk 4, not chunk 2."""
    sentence = "This is a sentence about the topic at hand. "
    answer = sentence * 8
    router = _make_router_with_long_answer(answer, max_bytes=80)
    router.route_multi("!testuser", "tell me everything")

    # Buffer cursor should be at chunk 2 (0-indexed) after 3 auto-sends
    more = router.route("!testuser", "!more")
    assert more is not None
    assert "No pending" not in more  # a real chunk came back


def test_route_multi_config_override():
    """auto_send_chunks=1 in config behaves like the old single-send."""
    sentence = "This is a sentence about the topic at hand. "
    answer = sentence * 8
    router = _make_router_with_long_answer(answer, max_bytes=80)
    router.cfg["auto_send_chunks"] = 1
    result = router.route_multi("!testuser", "tell me everything")
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0].endswith("[!more]")


# --- Dispatcher integration ---


def test_dispatcher_fast_vs_slow_classification():
    """Commands are classified as fast, queries as slow."""
    router = _make_router()
    fast = ["!help", "!ping", "!status", "!topics", "!more", "!retry"]
    for cmd in fast:
        assert router.classify(cmd) == "command", f"{cmd} should be 'command'"

    slow = ["What is solar power?", "hello", "tell me about first aid"]
    for q in slow:
        assert router.classify(q) == "query", f"{q!r} should be 'query'"


# --- v0.3 regressions: !more buffer lifecycle ---


_LONG = " ".join(f"Sentence number {i} has some useful trail facts in it." for i in range(40))


class _ScriptedWiki(MockWiki):
    """Returns queued answers in order, then a default."""

    def __init__(self, *answers):
        super().__init__()
        self.answers = list(answers)
        self.calls = []

    def query(self, text, peer_ctx="", history="", board_context=""):
        self.calls.append({"text": text, "history": history, "board": board_context})
        answer = self.answers.pop(0) if self.answers else "Default answer."
        if isinstance(answer, Exception):
            raise answer
        return answer, True


def _router_with(wiki, **overrides):
    tmpdir = tempfile.mkdtemp(prefix="delfi-test-")
    router = Router(_make_cfg(tmpdir, **overrides), wiki, MockPeerCache(), MockGossipDir())
    return router


def test_command_after_long_answer_sends_only_its_own_reply():
    router = _router_with(_ScriptedWiki(_LONG))
    router.route_multi("!a", "tell me everything")
    assert router.route_multi("!a", "!ping") == ["pong from TEST-NODE"]


def test_short_answer_after_long_answer_is_not_padded_with_old_chunks():
    router = _router_with(_ScriptedWiki(_LONG, "Short answer."))
    router.route_multi("!a", "tell me everything")
    msgs = router.route_multi("!a", "is the gate open")
    assert len(msgs) == 1 and msgs[0].startswith("Short answer.")
    assert "Sentence number" not in msgs[0]


def test_new_answer_clears_unfinished_buffer():
    router = _router_with(_ScriptedWiki(_LONG, "Short answer."))
    router.route_multi("!a", "tell me everything")
    router.route_multi("!a", "is the gate open")
    assert "No pending" in router.route("!a", "!more")


def test_more_returns_exactly_one_chunk():
    router = _router_with(_ScriptedWiki(_LONG))
    router.route_multi("!a", "tell me everything")
    assert len(router.route_multi("!a", "!more")) == 1
    assert len(router.route_multi("!a", "!more 2")) == 1


def test_short_command_keeps_pending_answer_resumable():
    router = _router_with(_ScriptedWiki(_LONG))
    sent = router.route_multi("!a", "tell me everything")
    router.route_multi("!a", "!status")
    nxt = router.route("!a", "!more")
    assert "Sentence number" in nxt
    assert nxt not in sent


def test_more_buffers_isolated_per_sender():
    router = _router_with(_ScriptedWiki(_LONG))
    router.route_multi("!a", "tell me everything")
    assert "No pending" in router.route("!b", "!more")


# --- v0.3: long command output is paginated ---


class _BigTopicsWiki(MockWiki):
    def get_topics(self):
        return [f"topic-number-{i}" for i in range(60)]


def test_long_command_output_is_chunked_with_more():
    tmpdir = tempfile.mkdtemp(prefix="delfi-test-")
    router = Router(_make_cfg(tmpdir), _BigTopicsWiki(), MockPeerCache(), MockGossipDir())
    msgs = router.route_multi("!a", "!topics")
    assert len(msgs) == 3 and msgs[-1].endswith("[!more]")
    rest = router.route("!a", "!more")
    assert "topic-number" in rest
    everything = " ".join(msgs) + " " + rest
    assert "topic-number-0" in everything


def test_board_output_splits_on_post_boundaries():
    from del_fi.core.formatter import byte_len
    router = _make_router(board_enabled=True, board_persist=False, board_rate_limit=100)
    for i in range(5):
        router.route("!poster", f"!post Post {i}: " + "news " * 30)
    msgs = router.route_multi("!reader", "!board")
    assert len(msgs) >= 2
    for m in msgs:
        assert byte_len(m) <= 230
        assert m.startswith("[!post ")  # each message starts at a post


# --- v0.3: response cache isolation ---


def test_cache_not_shared_when_sender_has_history():
    class HistoryWiki(MockWiki):
        def query(self, text, peer_ctx="", history="", board_context=""):
            return (f"Answer using: {history[-40:]}" if history else "No history."), True

    router = _router_with(HistoryWiki(), memory_max_turns=5, memory_ttl=3600)
    router.route("!alice", "my campsite is 14B near the creek")
    router.route("!alice", "remind me what I said")
    bob = router.route("!bob", "remind me what I said")
    assert "14B" not in bob


def test_cache_used_for_history_free_questions():
    wiki = _ScriptedWiki("First answer.", "Second answer.")
    router = _router_with(wiki)
    a = router.route("!a", "Where is the trailhead?")
    b = router.route("!b", "where is the trailhead")  # normalised key
    assert "First answer." in a and "First answer." in b
    assert len(wiki.calls) == 1


def test_cache_keeps_peer_provenance():
    class PeerOnlyWiki(MockWiki):
        def query(self, text, peer_ctx="", history="", board_context=""):
            return "", False

    class OnePeer(MockPeerCache):
        def lookup(self, query):
            return {"peer_name": "MARINA-ORACLE", "response": "Limit is 6 bass per day."}

    tmpdir = tempfile.mkdtemp(prefix="delfi-test-")
    router = Router(_make_cfg(tmpdir), PeerOnlyWiki(), OnePeer(), MockGossipDir())
    first = router.route("!a", "bass limit?")
    again = router.route("!b", "bass limit?")
    assert first.startswith("[via MARINA-ORACLE]")
    assert again.startswith("[via MARINA-ORACLE]")


def test_cache_size_is_capped():
    from del_fi.core import router as router_mod
    router = _router_with(MockWiki())
    for i in range(router_mod.MAX_CACHE_ENTRIES + 25):
        router._cache_response(f"question {i}", "answer")
    assert len(router._response_cache) == router_mod.MAX_CACHE_ENTRIES
    assert router._check_cache(f"question {router_mod.MAX_CACHE_ENTRIES + 24}")


def test_flush_cache_round_trip():
    tmpdir = tempfile.mkdtemp(prefix="delfi-test-")
    cfg = _make_cfg(tmpdir, persistent_cache=True)
    r1 = Router(cfg, MockWiki(), MockPeerCache(), MockGossipDir())
    r1._cache_response("Where is camp?", "By the lake.", "PEER-X")
    r1.flush_cache()
    r2 = Router(cfg, MockWiki(), MockPeerCache(), MockGossipDir())
    assert r2._check_cache("where is camp") == ("By the lake.", "PEER-X")


# --- v0.3: honest replies when the LLM fails ---


def test_llm_unreachable_is_reported_honestly():
    from del_fi.core.knowledge import LLMError
    router = _router_with(_ScriptedWiki(LLMError("unavailable", "refused")))
    reply = router.route("!a", "where are the elk")
    assert "language model" in reply and "don't have" not in reply


def test_llm_timeout_suggests_retry():
    from del_fi.core.knowledge import LLMError
    router = _router_with(_ScriptedWiki(LLMError("timeout")))
    assert "!retry" in router.route("!a", "where are the elk")


def test_llm_error_is_not_cached():
    from del_fi.core.knowledge import LLMError
    wiki = _ScriptedWiki(LLMError("error", "model not found"), "Real answer.")
    router = _router_with(wiki)
    router.route("!a", "where are the elk")
    assert "Real answer." in router.route("!b", "where are the elk")


# --- v0.3: !retry ---


def test_retry_inline_bypasses_cache():
    wiki = _ScriptedWiki("First.", "Second.")
    router = _router_with(wiki)
    router.route("!a", "what is the trail like")
    assert "Second." in router.route("!a", "!retry")


# --- v0.3: first-contact footer ---


def test_footer_not_added_to_multi_chunk_answer():
    router = _router_with(_ScriptedWiki(_LONG))
    msgs = router.route_multi("!new", "tell me everything")
    assert not any("Del-Fi oracle" in m for m in msgs)


def test_custom_welcome_footer():
    router = _router_with(_ScriptedWiki("Yes."), welcome_footer="RIDGELINE — ask about trails.")
    assert router.route("!new", "is it open").endswith("---\nRIDGELINE — ask about trails.")


def test_footer_added_once_to_short_answer():
    router = _router_with(_ScriptedWiki("Yes.", "No."))
    assert "Del-Fi oracle" in router.route("!new", "is it open")
    assert "Del-Fi oracle" not in router.route("!new", "is it closed")


# ---------------------------------------------------------------------------
# unittest discovery — collects every bare test_ function in this module
# ---------------------------------------------------------------------------


def load_tests(loader, standard_tests, pattern):
    return function_suite(globals(), standard_tests)

if __name__ == "__main__":
    unittest.main()
