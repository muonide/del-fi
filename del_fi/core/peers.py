"""Peer knowledge layers for Del-Fi.

PeerCache (Tier 2)
------------------
SQLite-backed cache of Q&A answers from trusted peer nodes, matched by
keyword overlap and always labelled with the peer's name. Trust is by
hardware node ID (mesh_knowledge.peers[].node_id), never by display name.
Nothing populates it yet: peer Q&A sync is on the roadmap.

GossipDirectory (Tier 3)
------------------------
Directory of other Del-Fi nodes heard on the mesh, built from their
broadcast announcements. Never caches answers; only provides referrals:
"Try VALLEY-ORACLE (!a1b2c3d4) — covers geology, mining".
Opt-in via mesh_knowledge.gossip.enabled.

Gossip announcement protocol
------------------------------
  DEL-FI:1:ANNOUNCE:<node_name>:topics=<t1,t2,...>:model=<model>
  Example:
  DEL-FI:1:ANNOUNCE:VALLEY-ORACLE:topics=geology,mining,local-history:model=llama3.2:3b

model= is always last and runs to the end of the message (model names
contain colons). The protocol version (1) allows future breaking changes
without ambiguity.
"""

import json
import logging
import os
import random
import re
import sqlite3
import threading
import time
from collections.abc import Callable

from del_fi.core.formatter import byte_len
from del_fi.core.fsutil import write_atomic
from del_fi.core.knowledge import index_slugs
from del_fi.core.text import tokenize

log = logging.getLogger("del_fi.core.peers")

PROTOCOL_VERSION = 1
ANNOUNCE_PREFIX = f"DEL-FI:{PROTOCOL_VERSION}:ANNOUNCE:"
GOSSIP_TTL_SECONDS = 86400  # default directory TTL (24 hours)
DEFAULT_ANNOUNCE_INTERVAL = 14400
JACCARD_THRESHOLD = 0.5
MAX_CACHE_ENTRIES_DEFAULT = 500

MAX_DIRECTORY_NODES = 64
MAX_TOPICS_PER_NODE = 12
MAX_TOPIC_LEN = 32
MAX_NAME_LEN = 32
MAX_MODEL_LEN = 40
# Re-save the directory when only last_seen moved, at most this often, so
# entries survive a restart without an SD-card write per announcement.
LAST_SEEN_SAVE_INTERVAL = 3600

# Topic words too generic to justify a referral on their own.
_GENERIC_TOPIC_WORDS = frozenset({
    "guide", "guides", "log", "logs", "notes", "info", "overview", "area",
    "local", "general", "misc", "faq", "index", "page", "pages",
})


def _gossip_cfg(cfg: dict) -> dict:
    return (cfg.get("mesh_knowledge") or {}).get("gossip") or {}


# ─────────────────────────── PeerCache ────────────────────────────────────

class PeerCache:
    """Stores Q&A answers received from trusted peer nodes.

    Thread-safe SQLite WAL database. Trusted peers are the node IDs listed
    in mesh_knowledge.peers.
    """

    CREATE_DDL = """
        CREATE TABLE IF NOT EXISTS peer_cache (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            peer_id     TEXT    NOT NULL,
            peer_name   TEXT    NOT NULL,
            query       TEXT    NOT NULL,
            response    TEXT    NOT NULL,
            timestamp   REAL    NOT NULL,
            ttl         REAL    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_peer_cache_ts ON peer_cache(timestamp);
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg
        mk = cfg.get("mesh_knowledge") or {}
        sync = mk.get("sync") or {}
        self._trusted: set[str] = {
            str(p["node_id"]).lower()
            for p in mk.get("peers") or []
            if isinstance(p, dict) and p.get("node_id")
        }
        self._ttl: float = float(sync.get("max_cache_age") or GOSSIP_TTL_SECONDS)
        self._max_entries: int = int(sync.get("max_cache_entries") or MAX_CACHE_ENTRIES_DEFAULT)
        cache_dir = cfg.get("_cache_dir", ".")
        os.makedirs(cache_dir, exist_ok=True)
        self._db_path = os.path.join(cache_dir, "mesh-answers.db")
        self._lock = threading.Lock()
        self._db: sqlite3.Connection | None = None
        self._init_db()

    # --- Public API ---

    def is_trusted(self, peer_id: str) -> bool:
        return str(peer_id).lower() in self._trusted

    def lookup(self, query: str) -> dict | None:
        """Return the best matching cached answer for *query*, or None.

        Matching uses Jaccard similarity on word tokens; returns the
        highest-scoring result above JACCARD_THRESHOLD.
        """
        if self._db is None:
            return None
        query_tokens = tokenize(query)
        if not query_tokens:
            return None

        now = time.time()
        with self._lock:
            rows = self._db.execute(
                "SELECT peer_id, peer_name, query, response, timestamp "
                "FROM peer_cache WHERE timestamp + ttl > ?",
                (now,),
            ).fetchall()

        best_score = 0.0
        best_row = None
        for row in rows:
            score = _jaccard(query_tokens, tokenize(row[2]))
            if score > best_score:
                best_score = score
                best_row = row

        if best_score >= JACCARD_THRESHOLD and best_row is not None:
            peer_id, peer_name, q, response, ts = best_row
            return {
                "peer_id": peer_id,
                "peer_name": peer_name,
                "query": q,
                "response": response,
                "score": best_score,
                "timestamp": ts,
            }
        return None

    def store(self, query: str, answer: str, peer_id: str, peer_name: str) -> bool:
        """Cache an answer from a peer node. Only accepts trusted node IDs."""
        if self._db is None:
            return False
        if not self.is_trusted(peer_id):
            log.debug(f"ignoring answer from untrusted peer {peer_id!r} ({peer_name!r})")
            return False

        now = time.time()
        with self._lock:
            self._db.execute(
                "INSERT INTO peer_cache "
                "(peer_id, peer_name, query, response, timestamp, ttl) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (peer_id, peer_name, query, answer, now, self._ttl),
            )
            self._db.commit()
        self.prune()
        return True

    def prune(self):
        """Remove expired entries and enforce max_cache_entries."""
        if self._db is None:
            return
        now = time.time()
        with self._lock:
            self._db.execute(
                "DELETE FROM peer_cache WHERE timestamp + ttl <= ?", (now,)
            )
            count = self._db.execute("SELECT COUNT(*) FROM peer_cache").fetchone()[0]
            if count > self._max_entries:
                self._db.execute(
                    "DELETE FROM peer_cache WHERE id IN "
                    "(SELECT id FROM peer_cache ORDER BY timestamp ASC LIMIT ?)",
                    (count - self._max_entries,),
                )
            self._db.commit()

    @property
    def entry_count(self) -> int:
        if self._db is None:
            return 0
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM peer_cache").fetchone()[0]

    # --- Internal ---

    def _init_db(self):
        try:
            conn = sqlite3.connect(self._db_path, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(self.CREATE_DDL)
            conn.commit()
            self._db = conn
            log.info(f"peer cache ready at {self._db_path}")
        except Exception as e:
            log.error(f"could not init peer cache DB: {e} — peer cache disabled")
            self._db = None


# ─────────────────────────── GossipDirectory ──────────────────────────────

class GossipDirectory:
    """Directory of other Del-Fi nodes on the mesh.

    Built from announcements; entries are keyed by the sender's node ID and
    expire after directory_ttl. Never stores knowledge — only metadata about
    other nodes and their topics. Announcements are unauthenticated, so
    everything in them is sanitised and the directory is size-capped.
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg
        gossip = _gossip_cfg(cfg)
        self.enabled: bool = bool(gossip.get("enabled", False))
        self.announce_interval: float = float(
            gossip.get("announce_interval") or DEFAULT_ANNOUNCE_INTERVAL
        )
        self.channel: int = int(gossip.get("channel") or 0)
        self._ttl: float = float(gossip.get("directory_ttl") or GOSSIP_TTL_SECONDS)
        self._gossip_dir = cfg.get("_gossip_dir", "gossip")
        self._dir_file = os.path.join(self._gossip_dir, "node-directory.json")
        self._nodes: dict[str, dict] = {}     # node_id -> entry
        self._saved_at: dict[str, float] = {}  # node_id -> last_seen when last saved
        self._lock = threading.Lock()
        os.makedirs(self._gossip_dir, exist_ok=True)
        if self.enabled:
            self._load_disk()

    # --- Public API ---

    def receive(self, node_id: str, announcement_text: str) -> bool:
        """Parse and store a node announcement. Returns True if stored."""
        if not self.enabled:
            return False
        parsed = parse_announcement(announcement_text)
        if parsed is None:
            log.debug(f"gossip: unparseable announcement from {node_id}")
            return False
        node_name, topics, model = parsed
        now = time.time()

        with self._lock:
            old = self._nodes.get(node_id)
            changed = (
                old is None
                or old.get("node_name") != node_name
                or old.get("topics") != topics
                or old.get("model") != model
            )
            self._nodes[node_id] = {
                "node_id": node_id,
                "node_name": node_name,
                "topics": topics,
                "model": model,
                "last_seen": now,
            }
            if len(self._nodes) > MAX_DIRECTORY_NODES:
                oldest = min(self._nodes, key=lambda k: self._nodes[k]["last_seen"])
                del self._nodes[oldest]
            due = now - self._saved_at.get(node_id, 0.0) > LAST_SEEN_SAVE_INTERVAL

        if changed or due:
            self._save_disk()
        if changed:
            log.info(f"gossip: {node_name} ({node_id}) announces {len(topics)} topic(s)")
        return True

    def referral(self, query: str) -> str | None:
        """Return a referral if another node covers the query topic.

        Example: "Try VALLEY-ORACLE (!a1b2c3d4) — covers geology, mining"
        """
        if not self.enabled:
            return None
        query_tokens = set(tokenize(query)) - _GENERIC_TOPIC_WORDS
        if not query_tokens:
            return None

        best_node = None
        best_overlap = 0
        for node in self.list_peers():
            topic_tokens = set(tokenize(" ".join(node.get("topics", [])))) - _GENERIC_TOPIC_WORDS
            overlap = len(query_tokens & topic_tokens)
            if overlap > best_overlap:
                best_overlap = overlap
                best_node = node

        if best_node is None:
            return None
        topics = ", ".join(best_node["topics"][:5])
        return f"Try {best_node['node_name']} ({best_node['node_id']}) — covers {topics}"

    def announce(self) -> str:
        """This node's announcement, trimmed to fit one message."""
        name = _clean_name(self.cfg.get("node_name", "")) or "UNNAMED"
        model = _clean_model(self.cfg.get("model", "")) or "unknown"
        max_bytes = self.cfg.get("max_response_bytes", 230)
        topics: list[str] = []
        for topic in self.local_topics():
            candidate = _format_announcement(name, topics + [topic], model)
            if byte_len(candidate) > max_bytes:
                break
            topics.append(topic)
        return _format_announcement(name, topics, model)

    def local_topics(self) -> list[str]:
        """This node's topics: wiki page slugs from wiki/index.md."""
        try:
            index = os.path.join(self.cfg.get("wiki_folder", "./wiki"), "index.md")
            if os.path.exists(index):
                with open(index, encoding="utf-8") as f:
                    return _clean_topics(index_slugs(f.read()))
        except OSError as e:
            log.warning(f"could not read local topics: {e}")
        return []

    def announce_loop(self, send_broadcast: Callable[[str, int], bool], stop: threading.Event):
        """Broadcast this node's announcement every announce_interval seconds.

        The first one waits a random 1–5 minutes, and each interval is
        jittered ±10%, so nodes that reboot together after a power cut do
        not all transmit at once. Nodes with no wiki topics stay quiet.
        """
        if not self.enabled:
            return
        if stop.wait(random.uniform(60, 300)):
            return
        while True:
            if self.local_topics():
                if send_broadcast(self.announce(), self.channel):
                    log.info("gossip: announcement sent")
            else:
                log.debug("gossip: no wiki topics yet — not announcing")
            if stop.wait(self.announce_interval * random.uniform(0.9, 1.1)):
                return

    def list_peers(self) -> list[dict]:
        """Return list of active (non-expired) peer entries."""
        self._expire()
        with self._lock:
            return list(self._nodes.values())

    @property
    def peer_count(self) -> int:
        return len(self.list_peers())

    # --- Internal ---

    def _expire(self):
        now = time.time()
        with self._lock:
            expired = [
                k for k, v in self._nodes.items()
                if now - v["last_seen"] > self._ttl
            ]
            for k in expired:
                del self._nodes[k]
        if expired:
            self._save_disk()

    def _load_disk(self):
        try:
            if not os.path.exists(self._dir_file):
                return
            with open(self._dir_file) as f:
                data = json.load(f)
            nodes = {}
            for node_id, entry in (data.items() if isinstance(data, dict) else []):
                try:
                    nodes[str(entry["node_id"])] = {
                        "node_id": str(entry["node_id"]),
                        "node_name": _clean_name(entry["node_name"]),
                        "topics": _clean_topics(entry.get("topics", [])),
                        "model": _clean_model(entry.get("model", "")),
                        "last_seen": float(entry["last_seen"]),
                    }
                except (KeyError, TypeError, ValueError):
                    continue  # v0.2 name-keyed or malformed entry
            with self._lock:
                self._nodes = nodes
                self._saved_at = {k: v["last_seen"] for k, v in nodes.items()}
            self._expire()
            log.info(f"gossip directory loaded ({len(self._nodes)} node(s))")
        except Exception as e:
            log.warning(f"could not load gossip directory: {e}")

    def _save_disk(self):
        with self._lock:
            data = dict(self._nodes)
            self._saved_at = {k: v["last_seen"] for k, v in data.items()}
        write_atomic(self._dir_file, json.dumps(data, indent=2))


# ─────────────────────────── helpers ──────────────────────────────────────


def parse_announcement(text: str) -> tuple[str, list[str], str] | None:
    """'DEL-FI:1:ANNOUNCE:NAME:topics=a,b:model=m:3b' -> (name, topics, model)."""
    text = text.strip()
    if not text.startswith(ANNOUNCE_PREFIX):
        return None
    name, _, fields = text[len(ANNOUNCE_PREFIX):].partition(":")
    name = _clean_name(name)
    if not name:
        return None

    model = ""
    m = re.search(r"(?:^|:)model=(.*)\Z", fields, re.DOTALL)
    if m:
        model = m.group(1)
        fields = fields[: m.start()]
    topics: list[str] = []
    t = re.search(r"(?:^|:)topics=([^:]*)", fields)
    if t:
        topics = t.group(1).split(",")
    return name, _clean_topics(topics), _clean_model(model) or "unknown"


def _format_announcement(name: str, topics: list[str], model: str) -> str:
    return f"{ANNOUNCE_PREFIX}{name}:topics={','.join(topics)}:model={model}"


def _clean_name(name) -> str:
    return re.sub(r"[^A-Z0-9-]", "", str(name).upper())[:MAX_NAME_LEN]


def _clean_topics(topics) -> list[str]:
    cleaned: list[str] = []
    for t in topics if isinstance(topics, list) else []:
        slug = re.sub(r"[^a-z0-9-]+", "-", str(t).lower()).strip("-")[:MAX_TOPIC_LEN]
        if slug and slug not in cleaned:
            cleaned.append(slug)
        if len(cleaned) >= MAX_TOPICS_PER_NODE:
            break
    return cleaned


def _clean_model(model) -> str:
    return "".join(ch for ch in str(model) if ch.isprintable() and not ch.isspace())[:MAX_MODEL_LEN]


def _jaccard(a: list[str], b: list[str]) -> float:
    sa = set(a)
    sb = set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)
