"""FactStore: structured sensor data layer for Tier 0 queries.

External scripts write sensor readings to cache/sensor_feed.json.
FactStore ingests that file, tracks freshness, and answers exact-value
queries directly — bypassing the LLM entirely to eliminate hallucination
on time-sensitive measurements.

Feed schema (cache/sensor_feed.json) — see examples/sensor_feed.example.json:
  {
    "<fact_key>": {
      "value":               <scalar>,                          required
      "timestamp":           <unix seconds> | "<ISO-8601>",     required
      "source":              "<string>",                        required
      "unit":                "<string>",
      "stale_after_seconds": <int>,                             default 3600
      "confidence":          <0.0–1.0> | "<label, e.g. measured>"
    }
  }

Timestamps: Unix seconds are unambiguous and preferred. ISO-8601 strings
may end in "Z" or carry an offset; a naive ISO string is taken as the
node's local time (what datetime.now().isoformat() writes on this host).
"""

import json
import logging
import math
import os
import re
import threading
import time
from datetime import datetime, timezone

from del_fi.core.fsutil import write_atomic

log = logging.getLogger("del_fi.core.facts")

REQUIRED_FIELDS = {"value", "timestamp", "source"}

# Fact-key words too generic to decide a match on their own: a key like
# "current_temp" must match on "temp", not on "current".
_GENERIC_KEY_WORDS = frozenset({
    "current", "latest", "last", "now", "today", "reading", "readings",
    "value", "level", "status", "sensor", "data",
})


class FactStore:
    """Manages structured sensor facts with freshness tracking.

    Thread-safe: all reads and writes go through _lock.
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._facts: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._feed_mtime: float = 0.0
        self._keyword_cache: tuple[tuple[str, ...], re.Pattern | None] = ((), None)

        feed_file = cfg.get("fact_feed_file", "")
        self._feed_file = (
            feed_file
            if feed_file
            else os.path.join(cfg["_cache_dir"], "sensor_feed.json")
        )
        self._store_file = os.path.join(cfg["_cache_dir"], "facts.json")
        self._load_persistent()

    # --- Public API ---

    def ingest(self, payload: dict) -> tuple[int, list[str]]:
        """Upsert facts from a payload dict. Returns (count_updated, errors)."""
        errors: list[str] = []
        count = 0

        for key, data in payload.items():
            fact, error = _validate_fact(key, data)
            if error:
                errors.append(error)
                continue
            fact["ingested_at"] = time.time()
            with self._lock:
                self._facts[key] = fact
            count += 1

        if count:
            self._save_persistent()
            log.info(f"facts: ingested {count} fact(s)")

        for err in errors:
            log.warning(f"facts: ingest error — {err}")

        return count, errors

    def get(self, key: str) -> dict | None:
        """Return a single fact enriched with is_stale and age_seconds.

        age_seconds is math.inf when the timestamp cannot be parsed; such a
        fact is always stale.
        """
        with self._lock:
            fact = self._facts.get(key)
        if fact is None:
            return None

        age = _age(fact["timestamp"])
        is_stale = age > fact["stale_after_seconds"]
        return {**fact, "is_stale": is_stale, "age_seconds": age}

    def get_all(self) -> dict[str, dict]:
        """Return all facts enriched with freshness info. Snapshot copy."""
        with self._lock:
            keys = list(self._facts.keys())
        result = {}
        for k in keys:
            f = self.get(k)
            if f is not None:
                result[k] = f
        return result

    def has_facts(self) -> bool:
        with self._lock:
            return bool(self._facts)

    def format_value(self, key: str) -> str | None:
        """Format a single fact as a human-readable string for radio."""
        f = self.get(key)
        if f is None:
            return None

        label = key.replace("_", " ").title()
        value = f["value"]
        unit = f" {f['unit']}" if f.get("unit") else ""
        source = f["source"]
        conf = f.get("confidence")
        conf_str = ""
        if isinstance(conf, (int, float)) and not isinstance(conf, bool):
            conf_str = f", {int(conf * 100)}% conf"
        elif isinstance(conf, str) and conf.strip():
            conf_str = f", {conf.strip()}"

        if f["is_stale"]:
            ts_str = _iso_short(f["timestamp"])
            return (
                f"{label}: {value}{unit} ({source}, as of {ts_str} — STALE{conf_str})"
            )

        return f"{label}: {value}{unit} ({source}, {_age_label(f['age_seconds'])}{conf_str})"

    def format_snapshot(self) -> str:
        """Return all facts as a multi-line radio-friendly summary."""
        all_facts = self.get_all()
        if not all_facts:
            return "No sensor data."

        lines = []
        for key in sorted(all_facts):
            line = self.format_value(key)
            if line:
                lines.append(line)

        return "\n".join(lines)

    def lookup(self, query: str) -> str | None:
        """Tier 0 keyword lookup. Returns formatted facts or None.

        Two gates, both on whole words: the question must contain one of
        fact_query_keywords, and a fact key must share a specific word with
        it ("temp" also matches a "temperature" key).
        """
        if not self.has_facts():
            return None

        q_lower = query.lower()
        gate = self._keyword_pattern()
        if gate is None or not gate.search(q_lower):
            return None

        q_words = set(re.sub(r"[^\w]", " ", q_lower).replace("_", " ").split())
        matched_keys = []
        for key in self.get_all():
            key_words = set(key.lower().replace("_", " ").replace("-", " ").split())
            key_words -= _GENERIC_KEY_WORDS
            if any(_word_matches(q, k) for q in q_words for k in key_words):
                matched_keys.append(key)

        if not matched_keys:
            return None

        lines = [line for line in (self.format_value(k) for k in sorted(matched_keys)) if line]
        if not lines:
            return None

        name = self.cfg["node_name"]
        return name + ": " + " | ".join(lines)

    def watch(self, stop: threading.Event):
        """Start background file-poll thread."""
        interval = self.cfg.get("fact_watch_interval_seconds", 30)

        def _watcher():
            while not stop.is_set():
                try:
                    self._poll_feed_file()
                except Exception:
                    log.exception("fact watcher error")
                stop.wait(interval)

        threading.Thread(target=_watcher, name="fact-watcher", daemon=True).start()
        log.info(f"fact watcher started (poll every {interval}s)")

    # --- Internal ---

    def _keyword_pattern(self) -> re.Pattern | None:
        keywords = tuple(
            str(k).lower().strip() for k in self.cfg.get("fact_query_keywords", []) if str(k).strip()
        )
        cached_for, pattern = self._keyword_cache
        if keywords != cached_for:
            pattern = (
                re.compile(r"\b(?:" + "|".join(re.escape(k) for k in keywords) + r")\b")
                if keywords else None
            )
            self._keyword_cache = (keywords, pattern)
        return pattern

    def _poll_feed_file(self):
        """Ingest sensor_feed.json if it has changed since last poll."""
        if not os.path.exists(self._feed_file):
            return

        mtime = os.path.getmtime(self._feed_file)
        if mtime <= self._feed_mtime:
            return

        try:
            with open(self._feed_file) as f:
                payload = json.load(f)
            if isinstance(payload, dict):
                self.ingest(payload)
            else:
                log.warning(f"sensor feed must be a JSON object: {self._feed_file}")
            self._feed_mtime = mtime
        except Exception as e:
            # Most likely a partial write; retried on the next poll.
            log.warning(f"could not read sensor feed: {e}")

    def _load_persistent(self):
        try:
            if not os.path.exists(self._store_file):
                return
            with open(self._store_file) as f:
                data = json.load(f)
            facts = {}
            for key, entry in (data.items() if isinstance(data, dict) else []):
                fact, error = _validate_fact(key, entry)
                if error:
                    log.warning(f"facts: skipping persisted {error}")
                    continue
                fact["ingested_at"] = entry.get("ingested_at", 0.0)
                facts[key] = fact
            with self._lock:
                self._facts = facts
            log.info(f"facts: loaded {len(facts)} persisted fact(s)")
        except Exception as e:
            log.warning(f"could not load persisted facts: {e}")

    def _save_persistent(self):
        with self._lock:
            data = dict(self._facts)
        write_atomic(self._store_file, json.dumps(data, indent=2))


# --- Helpers ---


def _validate_fact(key: str, data) -> tuple[dict | None, str | None]:
    """Return (fact, None) or (None, error message)."""
    if not isinstance(data, dict):
        return None, f"{key}: value must be a JSON object"
    missing = REQUIRED_FIELDS - set(data.keys())
    if missing:
        return None, f"{key}: missing required fields {sorted(missing)}"
    try:
        stale_after = int(data.get("stale_after_seconds", 3600))
    except (TypeError, ValueError):
        return None, f"{key}: stale_after_seconds must be a number of seconds"
    confidence = data.get("confidence")
    if confidence is not None and (
        not isinstance(confidence, (int, float, str)) or isinstance(confidence, bool)
    ):
        return None, f"{key}: confidence must be 0.0–1.0 or a label like \"measured\""
    return {
        "value": data["value"],
        "unit": data.get("unit", "") or "",
        "timestamp": data["timestamp"],
        "source": data["source"],
        "stale_after_seconds": stale_after,
        "confidence": confidence,
    }, None


def _word_matches(query_word: str, key_word: str) -> bool:
    """Whole-word match, plus abbreviations ("temp" → "temperature") and
    simple plurals ("temperatures" → "temperature")."""
    if query_word == key_word:
        return True
    if len(query_word) >= 4 and key_word.startswith(query_word):
        return True
    return query_word.endswith("s") and query_word[:-1] == key_word


def _parse_timestamp(timestamp) -> datetime | None:
    """Parse Unix seconds or an ISO-8601 string into an aware datetime."""
    if isinstance(timestamp, bool):
        return None
    if isinstance(timestamp, (int, float)):
        if not math.isfinite(timestamp):
            return None
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    if not isinstance(timestamp, str) or not timestamp.strip():
        return None
    text = timestamp.strip()
    try:
        return datetime.fromtimestamp(float(text), tz=timezone.utc)
    except ValueError:
        pass
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"  # fromisoformat() rejects "Z" before 3.11
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()  # naive → this node's local time
    return dt


def _age(timestamp) -> float:
    """Age in seconds of a fact timestamp; math.inf if it can't be parsed.

    Timestamps slightly in the future (clock skew) count as age 0.
    """
    dt = _parse_timestamp(timestamp)
    if dt is None:
        log.warning(f"could not parse fact timestamp: {timestamp!r} — treating as stale")
        return math.inf
    return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())


def _age_label(age_seconds: float) -> str:
    if not math.isfinite(age_seconds):
        return "age unknown"
    if age_seconds < 90:
        return "now"
    if age_seconds < 3600:
        return f"{int(age_seconds / 60)}m ago"
    if age_seconds < 86400:
        return f"{int(age_seconds / 3600)}h ago"
    return f"{int(age_seconds / 86400)}d ago"


def _iso_short(timestamp) -> str:
    """Format a fact timestamp as a short local-time string."""
    dt = _parse_timestamp(timestamp)
    if dt is None:
        return "unknown time"
    return dt.astimezone().strftime("%b %d %H:%M")
