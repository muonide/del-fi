"""Tests for the example deployments in examples/."""

import importlib.util
import json
import os
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from del_fi.config import read_config
from del_fi.core.facts import FactStore
from del_fi.core.router import Router
from tests.test_router import MockGossipDir, MockPeerCache, MockWiki

ROOT = Path(__file__).resolve().parent.parent
DAWN = ROOT / "examples" / "DAWN-CHORUS"


def _load_birdnet_feed():
    spec = importlib.util.spec_from_file_location("birdnet_feed", DAWN / "birdnet_feed.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


birdnet_feed = _load_birdnet_feed()

# BirdNET-Pi's table, as created by its scripts/createdb.sh
_SCHEMA = """CREATE TABLE detections (
  Date DATE, Time TIME, Sci_Name VARCHAR(100) NOT NULL, Com_Name VARCHAR(100) NOT NULL,
  Confidence FLOAT, Lat FLOAT, Lon FLOAT, Cutoff FLOAT, Week INT, Sens FLOAT,
  Overlap FLOAT, File_Name VARCHAR(100) NOT NULL)"""


def _make_db(path: str, detections) -> str:
    """detections: (datetime, common name[, confidence])."""
    con = sqlite3.connect(path)
    con.execute(_SCHEMA)
    con.executemany(
        "INSERT INTO detections VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [(when.date().isoformat(), when.strftime("%H:%M:%S"), "-", name,
          conf[0] if conf else 0.9, 47.6, -122.0, 0.7, 20, 1.25, 0.0, "clip.mp3")
         for when, name, *conf in detections],
    )
    con.commit()
    con.close()
    return path


def _dt(text: str) -> datetime:
    return datetime.fromisoformat(text)


class TestBirdnetFeed(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="delfi-birdnet-")
        self.db = os.path.join(self.tmpdir, "birds.db")

    def _values(self, now: str, detections) -> dict:
        _make_db(self.db, detections)
        loaded = birdnet_feed.load(Path(self.db), _dt(now), 0.7)
        facts = birdnet_feed.build_facts(*loaded, _dt(now))
        return {key: fact["value"] for key, fact in facts.items()}

    def test_singing_now_lists_recent_species_newest_first(self):
        values = self._values("2026-05-20 07:30", [
            (_dt("2026-05-20 07:10"), "Song Sparrow"),        # older than 15 min
            (_dt("2026-05-20 07:25"), "Swainson's Thrush"),
            (_dt("2026-05-20 07:28"), "Pacific Wren"),
            (_dt("2026-05-20 07:29"), "Red-tailed Hawk", 0.41),  # below 0.7
        ])
        self.assertEqual(values["singing_now"], "Pacific Wren 7:28, Swainson's Thrush 7:25")

    def test_quiet_and_empty_values(self):
        values = self._values("2026-05-20 07:30", [(_dt("2026-01-02 12:00"), "Pacific Wren")])
        self.assertIn("quiet", values["singing_now"])
        self.assertEqual(values["birds_detected_today"], "none yet today")
        self.assertEqual(values["owls_tonight"], "none since 18:00")
        self.assertEqual(values["new_arrivals"], "none in the last 14 days")

    def test_owls_before_evening_are_last_nights(self):
        values = self._values("2026-05-20 07:30", [
            (_dt("2026-05-19 17:00"), "Great Horned Owl"),    # before 18:00
            (_dt("2026-05-19 22:10"), "Barred Owl"),
            (_dt("2026-05-20 04:12"), "Barred Owl"),
        ])
        self.assertEqual(values["owls_tonight"], "since 18:00: Barred Owl (2); last at 4:12")

    def test_owls_after_evening_are_tonights(self):
        values = self._values("2026-05-20 21:00", [
            (_dt("2026-05-20 04:12"), "Barred Owl"),
            (_dt("2026-05-20 20:30"), "Western Screech-Owl"),
        ])
        self.assertEqual(values["owls_tonight"], "since 18:00: Western Screech-Owl (1); last at 20:30")

    def test_dawn_chorus_is_songbirds_between_4_and_9(self):
        values = self._values("2026-05-20 10:00", [
            (_dt("2026-05-20 04:05"), "Barred Owl"),
            (_dt("2026-05-20 04:48"), "American Robin"),
            (_dt("2026-05-20 05:00"), "Pacific Wren"),
            (_dt("2026-05-20 05:30"), "Pacific Wren"),
            (_dt("2026-05-20 09:30"), "Song Sparrow"),        # after 9 am
        ])
        self.assertEqual(
            values["dawn_chorus"],
            "2 species 4-9 am, first American Robin at 4:48, most often Pacific Wren (2)",
        )

    def test_new_arrivals_need_a_long_absence(self):
        values = self._values("2026-05-20 08:00", [
            (_dt("2026-02-01 08:00"), "Pacific Wren"),        # resident
            (_dt("2026-05-19 08:00"), "Pacific Wren"),
            (_dt("2026-02-10 08:00"), "Varied Thrush"),       # absent only 3 months
            (_dt("2026-05-18 08:00"), "Varied Thrush"),
            (_dt("2026-05-15 21:00"), "Swainson's Thrush"),   # back after winter
            (_dt("2026-05-19 06:00"), "Swainson's Thrush"),
        ])
        self.assertEqual(values["new_arrivals"], "Swainson's Thrush (May 15)")

    def test_main_merges_into_existing_feed(self):
        _make_db(self.db, [(_dt("2026-05-20 07:28"), "Pacific Wren")])
        out = os.path.join(self.tmpdir, "cache", "sensor_feed.json")
        os.makedirs(os.path.dirname(out))
        other = {"value": 2.1, "unit": "ft", "timestamp": 1790000000, "source": "creek-gauge"}
        with open(out, "w") as f:
            json.dump({"creek_level": other}, f)

        code = birdnet_feed.main(["--db", self.db, "--out", out, "--now", "2026-05-20 07:30"])

        self.assertEqual(code, 0)
        with open(out) as f:
            written = json.load(f)
        self.assertEqual(written["creek_level"], other)
        for key in ("singing_now", "birds_detected_today", "owls_tonight", "dawn_chorus", "new_arrivals"):
            self.assertEqual(written[key]["source"], "birdnet-pi")
            self.assertIn("stale_after_seconds", written[key])
        self.assertEqual(os.listdir(os.path.dirname(out)), ["sensor_feed.json"])  # no temp files left

    def test_missing_database_exits_with_error(self):
        out = os.path.join(self.tmpdir, "feed.json")
        code = birdnet_feed.main(["--db", os.path.join(self.tmpdir, "nope.db"), "--out", out])
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(out))


class TestDawnChorusNode(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="delfi-dawn-")

    def _config_path(self) -> str:
        guide = (DAWN / "DAWN-CHORUS.md").read_text(encoding="utf-8")
        section = guide.split("## Suggested config.yaml", 1)[1]
        block = re.search(r"```yaml\n(.*?)```", section, re.DOTALL).group(1)
        path = os.path.join(self.tmpdir, "config.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(block)
        return path

    def test_guide_config_is_valid(self):
        cfg = read_config(self._config_path())
        self.assertEqual(cfg["node_name"], "DAWN-CHORUS")
        self.assertTrue(cfg["board_enabled"])
        self.assertEqual(cfg["memory_max_turns"], 3)
        self.assertIn("right now", cfg["fact_query_keywords"])

    def test_station_questions_use_tier0_and_id_questions_do_not(self):
        cfg = read_config(self._config_path())
        now = datetime.now().replace(microsecond=0)
        db = _make_db(os.path.join(self.tmpdir, "birds.db"), [
            (now - timedelta(minutes=5), "Pacific Wren"),
            (now - timedelta(minutes=3), "Song Sparrow"),
        ])
        os.makedirs(cfg["_cache_dir"], exist_ok=True)
        feed_path = os.path.join(cfg["_cache_dir"], "sensor_feed.json")
        self.assertEqual(birdnet_feed.main(["--db", db, "--out", feed_path]), 0)

        facts = FactStore(cfg)
        facts._poll_feed_file()

        class Wiki(MockWiki):
            def query(self, text, peer_ctx="", history="", board_context=""):
                return "TIER1", True

        router = Router(cfg, Wiki(), MockPeerCache(), MockGossipDir(), fact_store=facts)
        expected = {
            "what's singing right now?": "Singing Now:",
            "any owls tonight?": "Owls Tonight:",
            "how was the dawn chorus this morning?": "Dawn Chorus:",
            "what birds were detected today?": "Birds Detected Today:",
            "any new arrivals?": "New Arrivals:",
            "I heard a bird that goes fee-bee": "TIER1",
            "where should I go birding this morning?": "TIER1",
            "what owls live at the preserve?": "TIER1",
        }
        for i, (question, marker) in enumerate(expected.items()):
            reply = " ".join(router.route_multi(f"!a{i:07d}", question))
            self.assertIn(marker, reply, question)
            self.assertNotIn("STALE", reply, question)

    def test_bench_questions_load(self):
        from del_fi.bench import load_questions
        questions = load_questions(DAWN / "bench-questions.txt")
        self.assertGreaterEqual(len(questions), 8)
        self.assertIn("I heard a bird that goes fee-bee", questions)

    def test_cross_referenced_files_exist(self):
        docs = list((DAWN / "knowledge").glob("*.md"))
        self.assertGreaterEqual(len(docs), 10)
        names = {d.name for d in docs}
        for text in [d.read_text(encoding="utf-8") for d in docs] + [
            (DAWN / "DAWN-CHORUS.md").read_text(encoding="utf-8")
        ]:
            for ref in re.findall(r"\b([a-z0-9-]+\.md)\b", text):
                self.assertIn(ref, names | {"DAWN-CHORUS.md"}, f"missing {ref}")


if __name__ == "__main__":
    unittest.main()
