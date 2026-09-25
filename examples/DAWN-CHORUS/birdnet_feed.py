#!/usr/bin/env python3
"""Summarise BirdNET-Pi detections as Del-Fi sensor facts (Tier 0).

BirdNET-Pi logs every bird it identifies to an SQLite database. This
script reads that log and writes five facts to the sensor feed Del-Fi
polls (cache/sensor_feed.json next to config.yaml), so questions like
"what's singing right now?" are answered straight from the station:

  singing_now           species detected in the last 15 minutes
  birds_detected_today  species count since midnight and the most frequent
  owls_tonight          owls since 18:00 (last night's, before 18:00)
  dawn_chorus           songbirds heard between 04:00 and 09:00 (owls are
                        reported under owls_tonight)
  new_arrivals          species heard in the last 14 days but not in the
                        four months before (returning migrants)

Other facts already in the feed file are kept. Run it every minute from
cron (crontab -e):

  * * * * * /usr/bin/python3 /home/pi/del-fi/examples/DAWN-CHORUS/birdnet_feed.py --out /home/pi/del-fi/cache/sensor_feed.json

Standard library only. Expects BirdNET-Pi's `detections` table (Date,
Time, Com_Name, Confidence, ...), with dates and times in local time.
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import tempfile
from collections import Counter
from datetime import datetime, time, timedelta
from pathlib import Path

DEFAULT_DB = "~/BirdNET-Pi/scripts/birds.db"
DEFAULT_OUT = "cache/sensor_feed.json"
SOURCE = "birdnet-pi"
MIN_CONFIDENCE = 0.7

SINGING_WINDOW = timedelta(minutes=15)
EVENING = time(18)
CHORUS_START, CHORUS_END = time(4), time(9)
ARRIVAL_DAYS = 14          # "new" if first heard within this many days...
ABSENT_DAYS = 120          # ...and not heard in the window before that

_OWL = re.compile(r"\bowl\b", re.IGNORECASE)


def _clock(when: datetime) -> str:
    return f"{when.hour}:{when.minute:02d}"


def _fact(value: str, now: datetime, stale_after: int) -> dict:
    return {
        "value": value,
        "timestamp": int(now.timestamp()),
        "source": SOURCE,
        "stale_after_seconds": stale_after,
    }


def load(db_path: Path, now: datetime, min_conf: float):
    """Detections since the earliest window start, and each species' first
    detection date within the arrivals window."""
    evening = datetime.combine(now.date(), EVENING)
    owl_start = evening if now >= evening else evening - timedelta(days=1)
    since = min(owl_start, datetime.combine(now.date() - timedelta(days=1), CHORUS_START))
    arrivals_since = now.date() - timedelta(days=ABSENT_DAYS + ARRIVAL_DAYS)

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT Date, Time, Com_Name, Confidence FROM detections"
            " WHERE Date >= ? AND Confidence >= ?",
            (since.date().isoformat(), min_conf),
        ).fetchall()
        first_seen = dict(con.execute(
            "SELECT Com_Name, MIN(Date) FROM detections"
            " WHERE Date >= ? AND Confidence >= ? GROUP BY Com_Name",
            (arrivals_since.isoformat(), min_conf),
        ).fetchall())
    finally:
        con.close()

    detections = []
    for day, clock, name, _conf in rows:
        try:
            when = datetime.fromisoformat(f"{day} {clock}")
        except (TypeError, ValueError):
            continue
        if since <= when <= now:
            detections.append((when, name))
    detections.sort()
    return detections, first_seen, owl_start


def build_facts(detections, first_seen, owl_start, now: datetime) -> dict:
    facts = {}

    latest: dict[str, datetime] = {}
    for when, name in detections:
        if when >= now - SINGING_WINDOW:
            latest[name] = when
    if latest:
        recent = sorted(latest.items(), key=lambda item: item[1], reverse=True)
        value = ", ".join(f"{name} {_clock(when)}" for name, when in recent[:6])
    else:
        value = "quiet, nothing detected in the last 15 minutes"
    facts["singing_now"] = _fact(value, now, 900)

    today = Counter(name for when, name in detections if when.date() == now.date())
    if today:
        top = ", ".join(f"{name} ({n})" for name, n in today.most_common(4))
        value = f"{len(today)} species, most often {top}"
    else:
        value = "none yet today"
    facts["birds_detected_today"] = _fact(value, now, 3600)

    owls = [(when, name) for when, name in detections if when >= owl_start and _OWL.search(name)]
    if owls:
        counts = Counter(name for _, name in owls)
        listed = ", ".join(f"{name} ({n})" for name, n in counts.most_common())
        value = f"since {_clock(owl_start)}: {listed}; last at {_clock(owls[-1][0])}"
    else:
        value = f"none since {_clock(owl_start)}"
    facts["owls_tonight"] = _fact(value, now, 3600)

    day = now.date() if now.time() >= CHORUS_START else now.date() - timedelta(days=1)
    start, end = datetime.combine(day, CHORUS_START), datetime.combine(day, CHORUS_END)
    chorus = [(when, name) for when, name in detections
              if start <= when < end and not _OWL.search(name)]
    if chorus:
        counts = Counter(name for _, name in chorus)
        loudest, n = counts.most_common(1)[0]
        first_when, first_name = chorus[0]
        value = (f"{len(counts)} species 4-9 am, first {first_name} at {_clock(first_when)}, "
                 f"most often {loudest} ({n})")
    else:
        value = "no birds detected 4-9 am"
    facts["dawn_chorus"] = _fact(value, now, 3600)

    cutoff = now.date() - timedelta(days=ARRIVAL_DAYS - 1)
    arrivals = []
    for name, first in first_seen.items():
        try:
            first_day = datetime.fromisoformat(str(first)).date()
        except ValueError:
            continue
        if first_day >= cutoff:
            arrivals.append((first_day, name))
    arrivals.sort(reverse=True)
    value = (", ".join(f"{name} ({d:%b} {d.day})" for d, name in arrivals[:6])
             or f"none in the last {ARRIVAL_DAYS} days")
    facts["new_arrivals"] = _fact(value, now, 3600)

    return facts


def write_feed(path: Path, facts: dict) -> None:
    """Merge *facts* into the feed file atomically, keeping other facts."""
    path.parent.mkdir(parents=True, exist_ok=True)
    feed = {}
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(existing, dict):
            feed = existing
    except (OSError, ValueError):
        pass
    feed.update(facts)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".sensor_feed.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(feed, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", default=DEFAULT_DB, help=f"BirdNET-Pi database (default {DEFAULT_DB})")
    parser.add_argument("--out", default=DEFAULT_OUT, help=f"Del-Fi sensor feed (default {DEFAULT_OUT})")
    parser.add_argument("--min-confidence", type=float, default=MIN_CONFIDENCE)
    parser.add_argument("--now", help="pretend it is this local time (YYYY-MM-DD HH:MM), for testing")
    args = parser.parse_args(argv)

    db_path = Path(args.db).expanduser()
    if not db_path.is_file():
        print(f"birdnet_feed: no BirdNET-Pi database at {db_path}", file=sys.stderr)
        return 1
    now = datetime.fromisoformat(args.now) if args.now else datetime.now().replace(microsecond=0)
    try:
        detections, first_seen, owl_start = load(db_path, now, args.min_confidence)
    except sqlite3.Error as e:
        print(f"birdnet_feed: could not read detections from {db_path}: {e}", file=sys.stderr)
        return 1
    write_feed(Path(args.out).expanduser(), build_facts(detections, first_seen, owl_start, now))
    return 0


if __name__ == "__main__":
    sys.exit(main())
