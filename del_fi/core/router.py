"""Query routing and command dispatch for Del-Fi.

Routes incoming messages to commands (inline) or the tier hierarchy:
  Tier 0 — FactStore (sensor facts, no LLM)
  Tier 1 — WikiEngine (BM25 + LLM on compiled wiki pages)
  Tier 2 — PeerCache (trusted peer Q&A)
  Tier 3 — GossipDirectory (referrals only)
  Fallback — fallback_message config value

Long responses (answers and command output alike) are buffered per
sender: the first few chunks are auto-sent and !more fetches the rest.

The dispatcher thread (commands) and the query worker both call into the
Router, so shared state is guarded by self._lock. The lock is never held
across an LLM call.
"""

import json
import logging
import os
import threading
import time

from del_fi.core.board import Board
from del_fi.core.facts import FactStore
from del_fi.core.fsutil import write_atomic
from del_fi.core.formatter import (
    MORE_TAG,
    byte_len,
    format_response,
    paginate,
    truncate_at_sentence,
)
from del_fi.core.knowledge import LLMError, WikiEngine
from del_fi.core.memory import ConversationMemory
from del_fi.core.peers import GossipDirectory, PeerCache

log = logging.getLogger("del_fi.core.router")

# !more buffers expire after 10 minutes of inactivity
MORE_BUFFER_TTL = 600

# Default auto-send window (config key: auto_send_chunks)
AUTO_SEND_CHUNKS = 3

# Response cache size cap (entries); expired entries are dropped first.
MAX_CACHE_ENTRIES = 100

GREETINGS = frozenset({
    "hi", "hello", "hey", "yo", "sup", "howdy", "hola", "greetings"
})


class MoreBuffer:
    """Per-sender buffer for chunked responses.

    Tracks all chunks from format_response() and a cursor pointing to
    the last sent chunk.  Supports !more (next) and !more N (specific,
    1-indexed).
    """

    def __init__(self, chunks: list[str], timestamp: float):
        self.chunks = chunks
        self.cursor = 0
        self.timestamp = timestamp

    def next_chunk(self) -> str | None:
        """Return the next unsent chunk, or None if exhausted."""
        self.cursor += 1
        if self.cursor < len(self.chunks):
            chunk = self.chunks[self.cursor]
            if self.cursor < len(self.chunks) - 1:
                chunk += MORE_TAG
            return chunk
        return None

    def get_chunk(self, n: int) -> str | None:
        """Return a specific chunk by 1-based index."""
        idx = n - 1
        if 0 <= idx < len(self.chunks):
            self.cursor = idx
            chunk = self.chunks[idx]
            if idx < len(self.chunks) - 1:
                chunk += MORE_TAG
            return chunk
        return None

    @property
    def expired(self) -> bool:
        return (time.time() - self.timestamp) > MORE_BUFFER_TTL

    @property
    def total_chunks(self) -> int:
        return len(self.chunks)


class Router:
    """Routes incoming messages to commands or the tier hierarchy."""

    def __init__(
        self,
        cfg: dict,
        wiki: WikiEngine,
        peer_cache: PeerCache,
        gossip_dir: GossipDirectory,
        fact_store: FactStore | None = None,
    ):
        self.cfg = cfg
        self.wiki = wiki
        self.peer_cache = peer_cache
        self.gossip_dir = gossip_dir
        self.facts: FactStore | None = fact_store

        self._lock = threading.RLock()
        self._io_lock = threading.Lock()  # orders snapshot+write to disk
        self._more_buffers: dict[str, MoreBuffer] = {}
        # key -> (response, provenance, timestamp)
        self._response_cache: dict[str, tuple[str, str | None, float]] = {}
        self._seen_senders: set[str] = set()
        self._last_query: dict[str, str] = {}
        self._start_time = time.time()
        self._query_count = 0
        self._cache_file = os.path.join(cfg["_cache_dir"], "response_cache.json")
        self._cache_dirty = False

        self._commands = {
            "!help": self._cmd_help,
            "!topics": self._cmd_topics,
            "!status": self._cmd_status,
            "!board": self._cmd_board,
            "!post": self._cmd_post,
            "!unpost": self._cmd_unpost,
            "!forget": self._cmd_forget,
            "!peers": self._cmd_peers,
            "!data": self._cmd_data,
            "!ping": self._cmd_ping,
        }

        self._load_seen_senders()
        if cfg.get("persistent_cache", True):
            self._load_disk_cache()

        self.memory: ConversationMemory | None = None
        if cfg.get("memory_max_turns", 0) > 0:
            self.memory = ConversationMemory(cfg)
            log.info(
                f"conversation memory enabled "
                f"(max {self.memory.max_turns} turns, ttl {self.memory.ttl}s)"
            )

        self.board: Board | None = None
        if cfg.get("board_enabled", False):
            self.board = Board(cfg)
            log.info(
                f"board enabled "
                f"(max {self.board.max_posts} posts, ttl {self.board.post_ttl}s)"
            )

    # --- Classification ---

    def classify(self, text: str) -> str:
        """Return 'empty', 'command', 'gossip', or 'query'."""
        text = text.strip()
        if not text:
            return "empty"
        if text.startswith("!"):
            return "command"
        if text.startswith("DEL-FI:"):
            return "gossip"
        return "query"

    def busy_message(self, position: int) -> str:
        name = self.cfg["node_name"]
        if position <= 1:
            return f"{name}: Working on another question, yours is next."
        return f"{name}: {position} questions ahead of yours, hang tight."

    # --- Main entry points ---

    def route(self, sender_id: str, text: str) -> str | None:
        """Route a message and return the first message to send, or None."""
        return self._route(sender_id, text)[0]

    def route_multi(self, sender_id: str, text: str) -> list[str] | None:
        """Route and return up to auto_send_chunks messages, in send order.

        Extra chunks only ever come from a !more buffer created by this
        call, never from an older answer still sitting in the buffer.
        """
        first, buf = self._route(sender_id, text)
        if first is None:
            return None

        n_auto = self.cfg.get("auto_send_chunks", AUTO_SEND_CHUNKS)
        if buf is None or n_auto <= 1:
            return [first]

        auto_msgs = [first[: -len(MORE_TAG)] if first.endswith(MORE_TAG) else first]
        with self._lock:
            while len(auto_msgs) < n_auto:
                chunk = buf.next_chunk()
                if chunk is None:
                    break
                is_last_slot = len(auto_msgs) == n_auto - 1
                if not is_last_slot and chunk.endswith(MORE_TAG):
                    chunk = chunk[: -len(MORE_TAG)].rstrip()
                auto_msgs.append(chunk)
        return auto_msgs

    def prepare_retry(self, sender_id: str) -> str | None:
        """Evict the cached answer to the sender's last question and return
        that question, or None if they have not asked one."""
        with self._lock:
            last = self._last_query.get(sender_id)
            if last is None:
                return None
            if self._response_cache.pop(self._cache_key(last), None) is not None:
                self._cache_dirty = True
                log.info(f"cache evicted for retry: {last[:40]}")
        return last

    def _route(self, sender_id: str, text: str) -> tuple[str | None, MoreBuffer | None]:
        """Return (first message, !more buffer created by this call or None)."""
        text = text.strip()
        if not text:
            return None, None

        self._clean_expired_buffers()

        if text.startswith("!"):
            return self._route_command(sender_id, text)

        if text.startswith("DEL-FI:"):
            self.gossip_dir.receive(sender_id, text)
            return None, None

        response, provenance = self._handle_query(sender_id, text)
        return self._finalize(sender_id, response, provenance)

    def _enforce_limit(self, text: str | None) -> str | None:
        if text is None:
            return None
        max_bytes = self.cfg["max_response_bytes"]
        if byte_len(text) <= max_bytes:
            return text
        return truncate_at_sentence(text, max_bytes)

    # --- Command dispatch ---

    def _route_command(
        self, sender_id: str, text: str
    ) -> tuple[str | None, MoreBuffer | None]:
        parts = text.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if cmd == "!more":
            return self._enforce_limit(self._cmd_more(sender_id, arg)), None
        if cmd == "!retry":
            return self._cmd_retry(sender_id, arg)

        handler = self._commands.get(cmd)
        if handler is None:
            return self._enforce_limit(f"Unknown command: {cmd[:24]}. Try !help"), None
        return self._paginate(sender_id, handler(sender_id, arg))

    def _cmd_help(self, sender_id: str, arg: str) -> str:
        name = self.cfg["node_name"]
        pages = self.wiki.page_count
        return (
            f"{name} · AI oracle · {pages} wiki pages\n"
            f"Ask anything in plain text.\n"
            f"!topics !status !board !post !unpost\n"
            f"!more !retry !forget !ping !peers !data"
        )

    def _cmd_topics(self, sender_id: str, arg: str) -> str:
        topics = self.wiki.get_topics()
        if not topics:
            return (
                "No wiki pages loaded. Run: python main.py --build-wiki"
            )
        return "Topics: " + ", ".join(topics)

    def _cmd_status(self, sender_id: str, arg: str) -> str:
        name = self.cfg["node_name"]
        model = self.cfg["model"]
        pages = self.wiki.page_count
        uptime = self._format_uptime()
        ollama_ok = "+" if self.wiki.available else "-"
        rag_ok = "+" if self.wiki.rag_available else "-"
        peers = self.gossip_dir.peer_count
        with self._lock:
            queries = self._query_count
        return (
            f"{name} up {uptime} · {model}\n"
            f"{pages} wiki pages · {queries} queries\n"
            f"ollama:{ollama_ok} rag:{rag_ok} peers:{peers}"
        )

    def _cmd_board(self, sender_id: str, arg: str) -> str:
        if not self.board:
            return "The board is not enabled on this node."
        return self.board.read(arg)

    def _cmd_post(self, sender_id: str, arg: str) -> str:
        if not self.board:
            return "The board is not enabled on this node."
        return self.board.post(sender_id, arg)

    def _cmd_unpost(self, sender_id: str, arg: str) -> str:
        if not self.board:
            return "The board is not enabled on this node."
        return self.board.clear(sender_id)

    def _cmd_more(self, sender_id: str, arg: str) -> str:
        with self._lock:
            buf = self._more_buffers.get(sender_id)
            if not buf or buf.expired:
                return "No pending response. Send a question first."
            buf.timestamp = time.time()  # expiry counts from last activity

            if arg.strip().isdigit():
                n = int(arg.strip())
                chunk = buf.get_chunk(n)
                if chunk:
                    return chunk
                return f"No chunk {n}. Response has {buf.total_chunks} parts."

            chunk = buf.next_chunk()
            if chunk:
                return chunk
            return "End of response. No more chunks."

    def _cmd_retry(
        self, sender_id: str, arg: str
    ) -> tuple[str | None, MoreBuffer | None]:
        # The daemon's Dispatcher intercepts !retry and runs it on the query
        # worker; this inline path serves the GUI simulator and tests.
        last = self.prepare_retry(sender_id)
        if last is None:
            return "No previous query to retry. Ask a question first.", None
        response, provenance = self._handle_query(sender_id, last)
        return self._finalize(sender_id, response, provenance)

    def _cmd_forget(self, sender_id: str, arg: str) -> str:
        if not self.memory:
            return "Conversation memory is not enabled on this node."
        self.memory.clear(sender_id)
        return "Memory cleared. I won't remember our previous conversation."

    def _cmd_peers(self, sender_id: str, arg: str) -> str:
        peers = self.gossip_dir.list_peers()
        if not peers:
            return "No other Del-Fi nodes seen yet."
        lines = []
        for p in peers:
            topics = ", ".join(p.get("topics", [])[:4])
            lines.append(f"{p['node_name']}: {topics}")
        return "\n".join(lines)

    def _cmd_data(self, sender_id: str, arg: str) -> str:
        if not self.facts or not self.facts.has_facts():
            return (
                "No sensor data yet. Operators: write cache/sensor_feed.json "
                "(see examples/sensor_feed.example.json)."
            )
        return self.facts.format_snapshot()

    def _cmd_ping(self, sender_id: str, arg: str) -> str:
        return f"pong from {self.cfg['node_name']}"

    # --- Query pipeline ---

    def _handle_query(self, sender_id: str, text: str) -> tuple[str, str | None]:
        """Answer a question. Returns (response text, provenance or None)."""
        with self._lock:
            self._query_count += 1
            self._last_query[sender_id] = text
            first_contact = sender_id not in self._seen_senders

        name = self.cfg["node_name"]

        # Welcome greeting for first-time senders
        if first_contact and self._is_greeting(text):
            self._mark_seen(sender_id)
            pages = self.wiki.page_count
            return (
                f"Hi from {name}. I answer questions using local docs.\n"
                f"{pages} wiki pages loaded. Try !help or !topics."
            ), None

        # Tier 0: FactStore (sensor / measurement queries, no LLM)
        # Bypasses the response cache — freshness is the whole point.
        if self.facts and self.facts.has_facts():
            fact_response = self.facts.lookup(text)
            if fact_response is not None:
                log.info("tier0: fact match")
                return fact_response, None

        history = self.memory.format_for_prompt(sender_id) if self.memory else ""

        # Response cache. Only for questions asked without conversation
        # history: an answer shaped by one sender's history must never be
        # served to another sender.
        if not history:
            cached = self._check_cache(text)
            if cached:
                log.info("cache hit")
                answer, provenance = cached
                if self.memory:
                    self.memory.add_turn(sender_id, text, answer)
                return answer, provenance

        if not self.wiki.available:
            return self._llm_down_reply(), None

        board_ctx = self.board.format_for_context(query=text) if self.board else ""

        # Tier 1: WikiEngine (BM25 + LLM)
        try:
            answer, had_context = self.wiki.query(
                text, history=history, board_context=board_ctx
            )
        except LLMError as e:
            return self._llm_error_reply(e), None

        provenance: str | None = None
        if not had_context:
            # Tier 2: PeerCache — a trusted peer's answer, labelled as such.
            peer_result = self.peer_cache.lookup(text)
            if not peer_result:
                # Tier 3: GossipDirectory (referral only), then fallback.
                referral = self.gossip_dir.referral(text)
                if referral:
                    return referral, None
                return self._fallback(text), None
            provenance = peer_result["peer_name"]
            answer = peer_result["response"]
            log.info(f"tier2: peer match from {provenance}")

        if not history:
            self._cache_response(text, answer, provenance)
        if self.memory:
            self.memory.add_turn(sender_id, text, answer)
        return answer, provenance

    def _fallback(self, text: str) -> str:
        fallback = self.cfg.get("fallback_message", "")
        if fallback:
            return fallback
        return self.wiki.suggest(text) or (
            f"{self.cfg['node_name']}: I don't have docs on that. "
            f"Try !topics to see what I know."
        )

    def _llm_down_reply(self) -> str:
        return (
            f"{self.cfg['node_name']}: My language model isn't reachable right "
            f"now. Commands still work — try again in a few minutes."
        )

    def _llm_error_reply(self, err: LLMError) -> str:
        if err.kind == "unavailable":
            return self._llm_down_reply()
        if err.kind == "timeout":
            return (
                f"{self.cfg['node_name']}: That took too long to answer. "
                f"Try a shorter question, or !retry in a minute."
            )
        return f"{self.cfg['node_name']}: I hit an error answering that. Try again later."

    def _finalize(
        self, sender_id: str, text: str, provenance: str | None = None
    ) -> tuple[str, MoreBuffer | None]:
        """Format an answer for the radio and reset the sender's !more buffer."""
        max_bytes = self.cfg["max_response_bytes"]
        first_msg, all_chunks, is_truncated = format_response(
            text, max_bytes=max_bytes, provenance=provenance
        )

        if is_truncated:
            buf = MoreBuffer(all_chunks, time.time())
            with self._lock:
                self._more_buffers[sender_id] = buf
            return first_msg, buf

        with self._lock:
            # A new answer supersedes any unfinished one.
            self._more_buffers.pop(sender_id, None)
            first_contact = sender_id not in self._seen_senders
        if first_contact:
            footer = f"\n---\nDel-Fi oracle · {self.wiki.page_count} pages · !help !topics"
            if byte_len(first_msg + footer) <= max_bytes:
                first_msg += footer
                self._mark_seen(sender_id)
        return first_msg, None

    def _paginate(
        self, sender_id: str, text: str | None
    ) -> tuple[str | None, MoreBuffer | None]:
        """Chunk command output. Short output leaves any pending answer's
        !more buffer alone; long output replaces it."""
        if text is None:
            return None, None
        first, chunks, is_truncated = paginate(text, self.cfg["max_response_bytes"])
        if not is_truncated:
            return first, None
        buf = MoreBuffer(chunks, time.time())
        with self._lock:
            self._more_buffers[sender_id] = buf
        return first, buf

    # --- Helpers ---

    def _is_greeting(self, text: str) -> bool:
        return text.lower().strip().rstrip("!.,?") in GREETINGS

    @staticmethod
    def _cache_key(query: str) -> str:
        """Normalise a question so trivial variants share a cache entry."""
        return " ".join(query.lower().split()).strip(" ?!.,;:")

    def _check_cache(self, query: str) -> tuple[str, str | None] | None:
        key = self._cache_key(query)
        with self._lock:
            entry = self._response_cache.get(key)
            if entry is None:
                return None
            response, provenance, ts = entry
            if time.time() - ts < self.cfg["response_cache_ttl"]:
                return response, provenance
            del self._response_cache[key]
            self._cache_dirty = True
        return None

    def _cache_response(self, query: str, response: str, provenance: str | None = None):
        key = self._cache_key(query)
        now = time.time()
        with self._lock:
            self._response_cache[key] = (response, provenance, now)
            self._cache_dirty = True
            if len(self._response_cache) > MAX_CACHE_ENTRIES:
                ttl = self.cfg["response_cache_ttl"]
                live = sorted(
                    (item for item in self._response_cache.items() if now - item[1][2] < ttl),
                    key=lambda item: item[1][2],
                )
                self._response_cache = dict(live[-MAX_CACHE_ENTRIES:])

    def flush_cache(self):
        """Write the response cache to disk if dirty. Called periodically."""
        if not self.cfg.get("persistent_cache", True):
            return
        with self._io_lock:
            with self._lock:
                if not self._cache_dirty:
                    return
                data = {
                    k: {"response": r, "provenance": p, "ts": t}
                    for k, (r, p, t) in self._response_cache.items()
                }
                self._cache_dirty = False
            if not write_atomic(self._cache_file, json.dumps(data)):
                with self._lock:
                    self._cache_dirty = True

    def _load_disk_cache(self):
        try:
            if not os.path.exists(self._cache_file):
                return
            with open(self._cache_file) as f:
                data = json.load(f)
            now = time.time()
            ttl = self.cfg["response_cache_ttl"]
            for key, entry in data.items():
                try:
                    ts = float(entry["ts"])
                    if now - ts < ttl:
                        self._response_cache[key] = (
                            str(entry["response"]), entry.get("provenance"), ts
                        )
                except (KeyError, TypeError, ValueError):
                    continue
            if self._response_cache:
                log.info(f"loaded {len(self._response_cache)} cached responses from disk")
        except Exception as e:
            log.warning(f"could not load response cache: {e}")

    def _mark_seen(self, sender_id: str):
        with self._lock:
            if sender_id in self._seen_senders:
                return
            self._seen_senders.add(sender_id)
        with self._io_lock:
            with self._lock:
                snapshot = sorted(self._seen_senders)
            write_atomic(self.cfg["_seen_senders_file"], "".join(s + "\n" for s in snapshot))

    def _load_seen_senders(self):
        path = self.cfg["_seen_senders_file"]
        try:
            if os.path.exists(path):
                with open(path) as f:
                    self._seen_senders = {line.strip() for line in f if line.strip()}
        except Exception as e:
            log.warning(f"could not load seen senders: {e}")

    def _clean_expired_buffers(self):
        with self._lock:
            expired = [k for k, v in self._more_buffers.items() if v.expired]
            for k in expired:
                del self._more_buffers[k]

    def _format_uptime(self) -> str:
        elapsed = int(time.time() - self._start_time)
        days = elapsed // 86400
        hours = (elapsed % 86400) // 3600
        if days > 0:
            return f"{days}d {hours}h"
        minutes = (elapsed % 3600) // 60
        return f"{hours}h {minutes}m"

