"""Tests for facts.py — FactStore ingestion, staleness, Tier 0 routing."""

import json
import os
import tempfile
import time
import unittest

from del_fi.core.facts import FactStore, _age, _age_label
from del_fi.core.router import Router
from tests._support import function_suite


# --- Helpers ---


def _make_cfg(tmpdir: str) -> dict:
    """Minimal config dict with all paths pointing to tmpdir."""
    return {
        "node_name": "TEST-NODE",
        "model": "test-model:3b",
        "max_response_bytes": 230,
        "rate_limit_seconds": 30,
        "response_cache_ttl": 300,
        "personality": "Test assistant.",
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
        "fact_feed_file": "",
        "fact_watch_interval_seconds": 30,
        "time_sensitive_files": ["weather-station.md", "trail-camera-log.md"],
        "fact_query_keywords": [
            "temperature", "temp", "humidity", "wind", "pressure",
            "barometer", "snow", "conditions", "current", "right now", "latest",
            "camera", "detected", "detection", "spotted", "sighted",
            "last seen", "cam-1", "cam-2", "cam-3", "cam1", "cam2", "cam3",
        ],
    }


def _fresh_ts() -> str:
    """ISO-8601 timestamp for 5 minutes ago (fresh)."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 300))


def _stale_ts() -> str:
    """ISO-8601 timestamp for 48 hours ago (well past any stale_after_seconds)."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 172800))


# --- FactStore unit tests ---


def test_ingest_valid_payload():
    with tempfile.TemporaryDirectory(prefix="delfi-test-") as tmpdir:
        fs = FactStore(_make_cfg(tmpdir))
        count, errors = fs.ingest({
            "temperature_f": {
                "value": -4.2,
                "unit": "°F",
                "timestamp": _fresh_ts(),
                "source": "weather-station",
                "stale_after_seconds": 3600,
            }
        })
        assert count == 1
        assert errors == []


def test_get_returns_correct_value():
    with tempfile.TemporaryDirectory(prefix="delfi-test-") as tmpdir:
        fs = FactStore(_make_cfg(tmpdir))
        ts = _fresh_ts()
        fs.ingest({
            "humidity_pct": {
                "value": 72,
                "unit": "%",
                "timestamp": ts,
                "source": "weather-station",
            }
        })
        f = fs.get("humidity_pct")
        assert f is not None
        assert f["value"] == 72
        assert f["unit"] == "%"
        assert f["source"] == "weather-station"
        assert f["is_stale"] is False


def test_get_unknown_key_returns_none():
    with tempfile.TemporaryDirectory(prefix="delfi-test-") as tmpdir:
        fs = FactStore(_make_cfg(tmpdir))
        assert fs.get("nonexistent_key") is None


def test_stale_fact_detected():
    with tempfile.TemporaryDirectory(prefix="delfi-test-") as tmpdir:
        fs = FactStore(_make_cfg(tmpdir))
        fs.ingest({
            "temperature_f": {
                "value": -4.2,
                "unit": "°F",
                "timestamp": _stale_ts(),
                "source": "weather-station",
                "stale_after_seconds": 3600,  # 1 hour; our ts is 48 hours ago
            }
        })
        f = fs.get("temperature_f")
        assert f is not None
        assert f["is_stale"] is True
        assert f["age_seconds"] > 86400


def test_fresh_fact_not_stale():
    with tempfile.TemporaryDirectory(prefix="delfi-test-") as tmpdir:
        fs = FactStore(_make_cfg(tmpdir))
        fs.ingest({
            "wind_mph": {
                "value": 12,
                "unit": "mph",
                "timestamp": _fresh_ts(),
                "source": "weather-station",
                "stale_after_seconds": 3600,
            }
        })
        f = fs.get("wind_mph")
        assert f is not None
        assert f["is_stale"] is False


def test_ingest_missing_required_fields_reported():
    with tempfile.TemporaryDirectory(prefix="delfi-test-") as tmpdir:
        fs = FactStore(_make_cfg(tmpdir))
        count, errors = fs.ingest({
            "bad_fact": {"value": 1}  # missing timestamp and source
        })
        assert count == 0
        assert len(errors) == 1
        assert "bad_fact" in errors[0]


def test_ingest_partial_success():
    """Valid facts are ingested even when some are malformed."""
    with tempfile.TemporaryDirectory(prefix="delfi-test-") as tmpdir:
        fs = FactStore(_make_cfg(tmpdir))
        count, errors = fs.ingest({
            "good": {
                "value": 99,
                "timestamp": _fresh_ts(),
                "source": "sensor",
            },
            "bad": {"oops": True},
        })
        assert count == 1
        assert len(errors) == 1
        assert fs.get("good") is not None
        assert fs.get("bad") is None


def test_format_value_fresh():
    with tempfile.TemporaryDirectory(prefix="delfi-test-") as tmpdir:
        fs = FactStore(_make_cfg(tmpdir))
        fs.ingest({
            "temperature_f": {
                "value": -4.2,
                "unit": "°F",
                "timestamp": _fresh_ts(),
                "source": "weather-station",
                "stale_after_seconds": 3600,
            }
        })
        formatted = fs.format_value("temperature_f")
        assert formatted is not None
        assert "-4.2" in formatted
        assert "°F" in formatted
        assert "weather-station" in formatted
        assert "STALE" not in formatted


def test_format_value_stale_includes_caveat():
    with tempfile.TemporaryDirectory(prefix="delfi-test-") as tmpdir:
        fs = FactStore(_make_cfg(tmpdir))
        fs.ingest({
            "temperature_f": {
                "value": -4.2,
                "unit": "°F",
                "timestamp": _stale_ts(),
                "source": "weather-station",
                "stale_after_seconds": 3600,
            }
        })
        formatted = fs.format_value("temperature_f")
        assert formatted is not None
        assert "STALE" in formatted


def test_format_value_with_confidence():
    with tempfile.TemporaryDirectory(prefix="delfi-test-") as tmpdir:
        fs = FactStore(_make_cfg(tmpdir))
        fs.ingest({
            "cam1_last_detection": {
                "value": "2 elk",
                "timestamp": _fresh_ts(),
                "source": "CAM-1",
                "confidence": 0.94,
            }
        })
        formatted = fs.format_value("cam1_last_detection")
        assert "94% conf" in formatted


def test_format_snapshot_empty():
    with tempfile.TemporaryDirectory(prefix="delfi-test-") as tmpdir:
        fs = FactStore(_make_cfg(tmpdir))
        assert "No sensor data" in fs.format_snapshot()


def test_format_snapshot_shows_stale_tag():
    with tempfile.TemporaryDirectory(prefix="delfi-test-") as tmpdir:
        fs = FactStore(_make_cfg(tmpdir))
        fs.ingest({
            "temperature_f": {
                "value": -4.2,
                "unit": "°F",
                "timestamp": _stale_ts(),
                "source": "weather-station",
                "stale_after_seconds": 3600,
            }
        })
        snapshot = fs.format_snapshot()
        assert "STALE" in snapshot


def test_has_facts():
    with tempfile.TemporaryDirectory(prefix="delfi-test-") as tmpdir:
        fs = FactStore(_make_cfg(tmpdir))
        assert not fs.has_facts()
        fs.ingest({
            "x": {"value": 1, "timestamp": _fresh_ts(), "source": "s"}
        })
        assert fs.has_facts()


def test_persistence_round_trip():
    """Facts are persisted on ingest and reloaded by a new FactStore instance."""
    with tempfile.TemporaryDirectory(prefix="delfi-test-") as tmpdir:
        cfg = _make_cfg(tmpdir)
        os.makedirs(cfg["_cache_dir"], exist_ok=True)

        fs1 = FactStore(cfg)
        ts = _fresh_ts()
        fs1.ingest({
            "snow_depth_in": {
                "value": 34,
                "unit": "in",
                "timestamp": ts,
                "source": "weather-station",
            }
        })

        # New instance should load the persisted data
        fs2 = FactStore(cfg)
        f = fs2.get("snow_depth_in")
        assert f is not None
        assert f["value"] == 34


def test_feed_file_ingested_on_poll():
    """Writing a JSON feed file triggers ingest on the next poll."""
    with tempfile.TemporaryDirectory(prefix="delfi-test-") as tmpdir:
        cfg = _make_cfg(tmpdir)
        os.makedirs(cfg["_cache_dir"], exist_ok=True)

        feed_path = os.path.join(cfg["_cache_dir"], "sensor_feed.json")
        cfg["fact_feed_file"] = feed_path

        payload = {
            "humidity_pct": {
                "value": 65,
                "unit": "%",
                "timestamp": _fresh_ts(),
                "source": "weather-station",
            }
        }
        with open(feed_path, "w") as fh:
            json.dump(payload, fh)

        fs = FactStore(cfg)
        # Manually trigger poll (watcher thread is not running in tests)
        fs._poll_feed_file()

        f = fs.get("humidity_pct")
        assert f is not None
        assert f["value"] == 65


def test_feed_file_not_reingested_if_unchanged():
    """Polling an unchanged feed file (same mtime) does not re-ingest."""
    with tempfile.TemporaryDirectory(prefix="delfi-test-") as tmpdir:
        cfg = _make_cfg(tmpdir)
        os.makedirs(cfg["_cache_dir"], exist_ok=True)

        feed_path = os.path.join(cfg["_cache_dir"], "sensor_feed.json")
        cfg["fact_feed_file"] = feed_path

        payload = {"x": {"value": 1, "timestamp": _fresh_ts(), "source": "s"}}
        with open(feed_path, "w") as fh:
            json.dump(payload, fh)

        fs = FactStore(cfg)
        fs._poll_feed_file()  # first poll — ingests
        assert fs.has_facts()

        # Overwrite store to simulate "reset" (would normally not happen, just testing guard)
        with fs._lock:
            fs._facts = {}

        # Second poll with same mtime — should NOT re-ingest
        fs._poll_feed_file()
        assert not fs.has_facts()


# --- Helper function tests ---


def test_age_fresh():
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 120))
    age = _age(ts)
    assert 100 < age < 200


def test_age_invalid_returns_inf():
    # Bad timestamps are treated as infinitely stale (not fresh)
    import math
    assert math.isinf(_age("not-a-date"))


def test_format_age_seconds():
    assert _age_label(45) == "now"


def test_format_age_minutes():
    assert _age_label(180) == "3m ago"


def test_format_age_hours():
    assert _age_label(7200) == "2h ago"


def test_format_age_days():
    assert _age_label(172800) == "2d ago"


# --- Tier 0 routing tests (via Router) ---


class MockWiki:
    """Minimal mock WikiEngine — tracks query() calls."""

    def __init__(self):
        self.query_called = False

    @property
    def available(self):
        return True

    @property
    def rag_available(self):
        return True

    @property
    def page_count(self):
        return 3

    def get_topics(self):
        return ["wildlife-guide", "trail-camera-log", "weather-station"]

    def query(self, text, peer_ctx="", history="", board_context=""):
        self.query_called = True
        return "Mock LLM response.", True

    def suggest(self, text):
        return None


class MockPeerCache:
    def lookup(self, q): return None
    def store(self, *a, **kw): pass


class MockGossipDir:
    enabled = True
    peer_count = 0
    def list_peers(self): return []
    def receive(self, nid, txt): pass
    def referral(self, q): return None
    def announce(self): return ""


def _make_router_with_facts(tmpdir: str, facts: dict | None = None) -> tuple:
    """Create a Router+FactStore pair with pre-loaded facts."""
    from del_fi.core.router import Router
    cfg = _make_cfg(tmpdir)
    wiki = MockWiki()
    fs = FactStore(cfg)
    if facts:
        fs.ingest(facts)
    router = Router(cfg, wiki, MockPeerCache(), MockGossipDir(), fact_store=fs)
    return router, wiki, fs


def test_tier0_intercepts_temperature_query():
    """Temperature query hits FactStore directly — no wiki.query() call."""
    with tempfile.TemporaryDirectory(prefix="delfi-test-") as tmpdir:
        router, wiki, _ = _make_router_with_facts(tmpdir, {
            "temperature_f": {
                "value": -4.2,
                "unit": "°F",
                "timestamp": _fresh_ts(),
                "source": "weather-station",
                "stale_after_seconds": 3600,
            }
        })
        response = router.route("sender1", "what is the temperature right now")
        assert response is not None
        assert "-4.2" in response
        assert wiki.query_called is False


def test_tier0_intercepts_camera_query():
    """Camera detection query hits FactStore directly."""
    with tempfile.TemporaryDirectory(prefix="delfi-test-") as tmpdir:
        router, wiki, _ = _make_router_with_facts(tmpdir, {
            "cam1_last_detection": {
                "value": "7 elk",
                "timestamp": _fresh_ts(),
                "source": "CAM-1",
                "stale_after_seconds": 86400,
            }
        })
        response = router.route("sender1", "what did cam1 detect last")
        assert response is not None
        assert "elk" in response
        assert wiki.query_called is False


def test_tier0_stale_fact_includes_caveat():
    """Stale sensor value includes a staleness caveat in the direct response."""
    with tempfile.TemporaryDirectory(prefix="delfi-test-") as tmpdir:
        router, _, _ = _make_router_with_facts(tmpdir, {
            "temperature_f": {
                "value": -4.2,
                "unit": "°F",
                "timestamp": _stale_ts(),
                "source": "weather-station",
                "stale_after_seconds": 3600,
            }
        })
        response = router.route("sender1", "temperature")
        assert response is not None
        assert "STALE" in response


def test_tier0_misses_non_sensor_query():
    """Non-sensor query falls through Tier 0 to wiki."""
    with tempfile.TemporaryDirectory(prefix="delfi-test-") as tmpdir:
        router, wiki, _ = _make_router_with_facts(tmpdir, {
            "temperature_f": {
                "value": -4.2,
                "unit": "°F",
                "timestamp": _fresh_ts(),
                "source": "weather-station",
            }
        })
        response = router.route("sender1", "tell me about elk migration patterns")
        assert response is not None
        assert "-4.2" not in response


def test_tier0_no_facts_falls_through():
    """When FactStore is empty, query falls through to wiki."""
    with tempfile.TemporaryDirectory(prefix="delfi-test-") as tmpdir:
        router, wiki, _ = _make_router_with_facts(tmpdir, facts=None)
        response = router.route("sender1", "what is the temperature")
        assert response is not None
        # wiki.query should have been called since Tier 0 had no facts
        assert wiki.query_called is True


def test_cmd_data_no_facts():
    with tempfile.TemporaryDirectory(prefix="delfi-test-") as tmpdir:
        router, _, _ = _make_router_with_facts(tmpdir)
        response = router.route("sender1", "!data")
        assert response is not None
        assert "No sensor data" in response


def test_cmd_data_with_facts():
    with tempfile.TemporaryDirectory(prefix="delfi-test-") as tmpdir:
        router, _, _ = _make_router_with_facts(tmpdir, {
            "temperature_f": {
                "value": -4.2,
                "unit": "°F",
                "timestamp": _fresh_ts(),
                "source": "weather-station",
            }
        })
        response = router.route("sender1", "!data")
        assert response is not None
        assert "Temperature F" in response
        assert "-4.2" in response


def test_router_without_fact_store_still_works():
    """Router without fact_store kwarg must still work."""
    with tempfile.TemporaryDirectory(prefix="delfi-test-") as tmpdir:
        cfg = _make_cfg(tmpdir)
        # No fact_store kwarg — should default to None
        router = Router(cfg, MockWiki(), MockPeerCache(), MockGossipDir())
        response = router.route("sender1", "!ping")
        assert "pong" in response.lower()


# --- v0.3: timestamp contract ---


def _store(**cfg):
    tmpdir = tempfile.mkdtemp(prefix="delfi-facts-")
    base = {"_cache_dir": os.path.join(tmpdir, "cache"), "node_name": "RIDGELINE",
            "fact_query_keywords": ["temperature", "temp", "wind", "current", "cam1"]}
    base.update(cfg)
    return FactStore(base)


def test_unix_float_timestamp_from_spec_works():
    """v0.2 crashed with OverflowError on the spec's own feed format."""
    fs = _store()
    fs.ingest({"temperature": {"value": 41.2, "unit": "F", "source": "davis",
                               "timestamp": time.time() - 30}})
    assert fs.lookup("what's the temperature?") == "RIDGELINE: Temperature: 41.2 F (davis, now)"


def test_numeric_string_timestamp():
    fs = _store()
    fs.ingest({"temperature": {"value": 1, "source": "s", "timestamp": str(time.time() - 600)}})
    assert "10m ago" in fs.format_value("temperature")


def test_iso_z_and_offset_timestamps():
    from del_fi.core.facts import _parse_timestamp
    z = _parse_timestamp("2026-04-22T10:00:00Z")
    off = _parse_timestamp("2026-04-22T04:00:00-06:00")
    assert z is not None and z == off


def test_naive_iso_is_local_time():
    if not hasattr(time, "tzset"):
        return
    old = os.environ.get("TZ")
    os.environ["TZ"] = "America/Denver"
    time.tzset()
    try:
        from datetime import datetime
        age = _age(datetime.now().isoformat())
        assert age < 60, f"naive local timestamp read as {age:.0f}s old"
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


def test_confidence_label_or_number():
    fs = _store()
    fs.ingest({"wind": {"value": 5, "source": "s", "timestamp": time.time(), "confidence": "measured"},
               "temperature": {"value": 3, "source": "s", "timestamp": time.time(), "confidence": 0.9}})
    assert "measured" in fs.format_value("wind")
    assert "90% conf" in fs.format_value("temperature")


def test_future_timestamp_counts_as_now():
    assert _age(time.time() + 120) == 0.0


def test_unparseable_timestamp_is_stale_not_a_crash():
    fs = _store()
    fs.ingest({"temperature": {"value": 3, "source": "s", "timestamp": "yesterday-ish"}})
    line = fs.format_value("temperature")
    assert "STALE" in line and "unknown time" in line
    assert "STALE" in fs.format_snapshot()


def test_bad_stale_after_rejects_only_that_fact():
    fs = _store()
    count, errors = fs.ingest({
        "wind": {"value": 5, "source": "s", "timestamp": time.time(), "stale_after_seconds": "soon"},
        "temperature": {"value": 3, "source": "s", "timestamp": time.time()},
    })
    assert count == 1 and len(errors) == 1 and "stale_after_seconds" in errors[0]


def test_persisted_malformed_entries_skipped():
    fs = _store()
    fs.ingest({"temperature": {"value": 3, "source": "s", "timestamp": time.time()}})
    with open(fs._store_file) as f:
        data = json.load(f)
    data["broken"] = {"value": 1}
    with open(fs._store_file, "w") as f:
        json.dump(data, f)
    fs2 = FactStore(fs.cfg)
    assert list(fs2.get_all()) == ["temperature"]


# --- v0.3: whole-word keyword matching ---


def test_keyword_gate_is_whole_word():
    fs = _store()
    fs.ingest({"temperature": {"value": 3, "source": "s", "timestamp": time.time()}})
    assert fs.lookup("when is the temple open?") is None


def test_abbreviation_matches_key():
    fs = _store()
    fs.ingest({"temperature_f": {"value": 3, "source": "s", "timestamp": time.time()}})
    assert "Temperature F: 3" in fs.lookup("what's the temp")


def test_generic_key_words_do_not_match():
    fs = _store()
    fs.ingest({"current_temp": {"value": 3, "source": "s", "timestamp": time.time()}})
    assert fs.lookup("what is the current bulk trash schedule") is None
    assert "Current Temp: 3" in fs.lookup("current temp please")


def test_window_is_not_wind():
    fs = _store()
    fs.ingest({"wind_speed": {"value": 12, "source": "s", "timestamp": time.time()}})
    assert fs.lookup("is the current window schedule posted?") is None
    assert "Wind Speed: 12" in fs.lookup("wind speed?")


def test_cmd_data_with_unparseable_timestamp_replies():
    """v0.2: !data raised on bad timestamps, so the sender got no reply."""
    with tempfile.TemporaryDirectory(prefix="delfi-test-") as tmpdir:
        router, _, _ = _make_router_with_facts(tmpdir, {
            "temperature": {"value": 3, "source": "s", "timestamp": "garbage"},
        })
        assert "STALE" in router.route("sender1", "!data")


# ---------------------------------------------------------------------------
# unittest discovery — collects every bare test_ function in this module
# ---------------------------------------------------------------------------


def load_tests(loader, standard_tests, pattern):
    return function_suite(globals(), standard_tests)

if __name__ == "__main__":
    unittest.main()
