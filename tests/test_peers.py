"""Tests for del_fi/core/peers.py — PeerCache (Tier 2) and GossipDirectory (Tier 3)."""

import json
import os
import tempfile
import threading
import time
import unittest
import unittest.mock

from del_fi.core import peers as peers_mod
from del_fi.core.peers import GossipDirectory, PeerCache, parse_announcement


def _cfg(tmpdir, gossip=None, peers=None, sync=None, **extra):
    mk = {
        "gossip": {"enabled": True, "announce_interval": 14400, "directory_ttl": 86400,
                   "channel": 0, **(gossip or {})},
        "peers": peers or [],
        "sync": {"max_cache_age": 86400.0, "max_cache_entries": 500, **(sync or {})},
    }
    cfg = {
        "node_name": "RIDGELINE",
        "model": "gemma3:4b-it-qat",
        "max_response_bytes": 230,
        "wiki_folder": os.path.join(tmpdir, "wiki"),
        "_cache_dir": os.path.join(tmpdir, "cache"),
        "_gossip_dir": os.path.join(tmpdir, "gossip"),
        "mesh_knowledge": mk,
    }
    cfg.update(extra)
    return cfg


def _write_index(tmpdir, slugs):
    wiki = os.path.join(tmpdir, "wiki")
    os.makedirs(wiki, exist_ok=True)
    rows = "\n".join(f"| [[{s}]] | See [[not-a-topic]]. | tag | 2026-01-01 |" for s in slugs)
    with open(os.path.join(wiki, "index.md"), "w") as f:
        f.write("| Page | Summary | Tags | Updated |\n|---|---|---|---|\n" + rows + "\n")


ANNOUNCE = "DEL-FI:1:ANNOUNCE:VALLEY-ORACLE:topics=geology,mining,local-history:model=llama3.2:3b"


class TestPeerCache(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="delfi-peers-")

    def _cache(self, **kw):
        return PeerCache(_cfg(self.tmpdir, peers=[{"node_id": "!A1B2C3D4", "name": "MARINA"}], **kw))

    def test_only_trusted_node_ids_are_stored(self):
        cache = self._cache()
        self.assertFalse(cache.store("q", "a", "!deadbeef", "MARINA"), "name alone must not grant trust")
        self.assertTrue(cache.store("bass limit february", "6 per day.", "!a1b2c3d4", "MARINA"))
        self.assertEqual(cache.entry_count, 1)

    def test_lookup_by_keyword_overlap(self):
        cache = self._cache()
        cache.store("smallmouth bass limit february", "6 per day, 12in minimum.", "!a1b2c3d4", "MARINA")
        hit = cache.lookup("what is the smallmouth bass limit in february")
        self.assertEqual(hit["peer_name"], "MARINA")
        self.assertIsNone(cache.lookup("trout season opening"))

    def test_expired_answers_ignored(self):
        cache = self._cache(sync={"max_cache_age": 0.01})
        cache.store("bass limit", "6.", "!a1b2c3d4", "MARINA")
        time.sleep(0.05)
        self.assertIsNone(cache.lookup("bass limit"))

    def test_prune_caps_entries(self):
        cache = self._cache(sync={"max_cache_entries": 3})
        for i in range(6):
            cache.store(f"question {i}", "answer", "!a1b2c3d4", "MARINA")
        self.assertEqual(cache.entry_count, 3)


class TestAnnouncementParsing(unittest.TestCase):
    def test_model_with_colons_parsed_whole(self):
        self.assertEqual(
            parse_announcement(ANNOUNCE),
            ("VALLEY-ORACLE", ["geology", "mining", "local-history"], "llama3.2:3b"),
        )

    def test_missing_fields(self):
        self.assertEqual(parse_announcement("DEL-FI:1:ANNOUNCE:NODE"), ("NODE", [], "unknown"))
        self.assertIsNone(parse_announcement("DEL-FI:1:ANNOUNCE::topics=a"))
        self.assertIsNone(parse_announcement("hello there"))

    def test_untrusted_fields_are_sanitised(self):
        name, topics, model = parse_announcement(
            "DEL-FI:1:ANNOUNCE:evil <b>node</b>:topics=A B,../../etc,,x" + ",t" * 30 + ":model=m\nx"
        )
        self.assertEqual(name, "EVILBNODEB")
        self.assertEqual(topics[:3], ["a-b", "etc", "x"])
        self.assertLessEqual(len(topics), peers_mod.MAX_TOPICS_PER_NODE)
        self.assertEqual(model, "mx")


class TestGossipDirectory(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="delfi-gossip-")

    def _dir(self, **gossip):
        return GossipDirectory(_cfg(self.tmpdir, gossip=gossip))

    def test_disabled_directory_ignores_everything(self):
        d = self._dir(enabled=False)
        self.assertFalse(d.receive("!a1b2c3d4", ANNOUNCE))
        self.assertIsNone(d.referral("geology of the valley"))
        self.assertEqual(d.list_peers(), [])

    def test_entries_keyed_by_node_id_not_name(self):
        d = self._dir()
        d.receive("!a1b2c3d4", ANNOUNCE)
        d.receive("!deadbeef", ANNOUNCE.replace("geology,mining", "everything"))
        by_id = {p["node_id"]: p["topics"] for p in d.list_peers()}
        self.assertEqual(by_id["!a1b2c3d4"], ["geology", "mining", "local-history"])
        self.assertEqual(len(by_id), 2, "a second node claiming the same name must not overwrite")

    def test_referral_names_node_id(self):
        d = self._dir()
        d.receive("!a1b2c3d4", ANNOUNCE)
        self.assertEqual(
            d.referral("any mining claims near here?"),
            "Try VALLEY-ORACLE (!a1b2c3d4) — covers geology, mining, local-history",
        )

    def test_generic_topic_words_do_not_refer(self):
        d = self._dir()
        d.receive("!a1b2c3d4", "DEL-FI:1:ANNOUNCE:X:topics=wildlife-guide,area-overview:model=m")
        self.assertIsNone(d.referral("where is the field guide"))
        self.assertIsNotNone(d.referral("wildlife near the lake"))

    def test_directory_is_capped(self):
        d = self._dir()
        with unittest.mock.patch.object(peers_mod, "MAX_DIRECTORY_NODES", 3):
            for i in range(5):
                d.receive(f"!0000000{i}", f"DEL-FI:1:ANNOUNCE:N{i}:topics=t{i}:model=m")
                time.sleep(0.001)
            ids = {p["node_id"] for p in d.list_peers()}
        self.assertEqual(ids, {"!00000002", "!00000003", "!00000004"})

    def test_entries_expire_after_configured_ttl(self):
        d = self._dir(directory_ttl=0.01)
        d.receive("!a1b2c3d4", ANNOUNCE)
        time.sleep(0.05)
        self.assertEqual(d.peer_count, 0)

    def test_repeated_identical_announcements_do_not_rewrite_disk(self):
        d = self._dir()
        with unittest.mock.patch.object(peers_mod, "write_atomic", return_value=True) as w:
            for _ in range(5):
                d.receive("!a1b2c3d4", ANNOUNCE)
        self.assertEqual(w.call_count, 1)

    def test_round_trip_and_v02_file_tolerated(self):
        d = self._dir()
        d.receive("!a1b2c3d4", ANNOUNCE)
        self.assertEqual(self._dir().peer_count, 1)

        path = os.path.join(self.tmpdir, "gossip", "node-directory.json")
        with open(path, "w") as f:  # v0.2 layout: keyed by name, no node_id
            json.dump({"OLD": {"node_name": "OLD", "topics": ["x"], "last_seen": time.time()}}, f)
        self.assertEqual(self._dir().peer_count, 0)

    def test_announce_fits_one_message_and_parses_back(self):
        _write_index(self.tmpdir, [f"topic-number-{i:02d}" for i in range(40)])
        d = self._dir()
        text = d.announce()
        self.assertLessEqual(len(text.encode("utf-8")), 230)
        name, topics, model = parse_announcement(text)
        self.assertEqual((name, model), ("RIDGELINE", "gemma3:4b-it-qat"))
        self.assertTrue(topics and all(t.startswith("topic-number-") for t in topics))
        self.assertNotIn("not-a-topic", text)

    def test_announce_loop_broadcasts_on_channel_until_stopped(self):
        _write_index(self.tmpdir, ["geology"])
        d = self._dir(channel=2)
        sent: list[tuple[str, int]] = []
        stop = threading.Event()

        def send(text, channel):
            sent.append((text, channel))
            if len(sent) == 2:
                stop.set()
            return True

        with unittest.mock.patch.object(peers_mod.random, "uniform", return_value=0.0):
            t = threading.Thread(target=d.announce_loop, args=(send, stop))
            t.start()
            t.join(timeout=3)
        self.assertFalse(t.is_alive())
        self.assertEqual([c for _, c in sent], [2, 2])
        self.assertTrue(sent[0][0].startswith("DEL-FI:1:ANNOUNCE:RIDGELINE:topics=geology"))

    def test_no_topics_no_announcement(self):
        d = self._dir()
        stop = threading.Event()
        sent = []

        def send(text, channel):
            sent.append(text)
            return True

        with unittest.mock.patch.object(peers_mod.random, "uniform", return_value=0.0):
            t = threading.Thread(target=d.announce_loop, args=(send, stop))
            t.start()
            time.sleep(0.05)
            stop.set()
            t.join(timeout=3)
        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
