# DAWN-CHORUS — Deployment Guide

A birding oracle for Salmonberry Creek Preserve, a fictional 160-acre wetland and forest preserve in the Puget Sound lowlands of western Washington. The birds are real. Hikers message the node from the trail to identify what they are hearing ("what sounds like a flute spiralling upward?"), sort out look-alikes, find the best spots, and ask what a live BirdNET listening station is detecting right now. It uses every part of Del-Fi: live sensor facts (Tier 0), a compiled knowledge base (Tier 1), conversation memory, the board and gossip.

---

## Oracle Profile

| Property | Value |
|----------|-------|
| Node name | `DAWN-CHORUS` |
| Oracle type | `observatory` |
| Hardware | Raspberry Pi 5 + Meshtastic radio, plus a Raspberry Pi running [BirdNET-Pi](https://github.com/Nachtzuster/BirdNET-Pi) with a USB microphone |
| Location | Beaver Pond Blind, Salmonberry Creek Preserve (fictional), western Washington |
| Serving model | `gemma4:e4b` (on a Pi 5, start with `gemma3:1b`; see the README) |
| Builder model | `gemma4:12b`, run on a desktop |
| Live data | BirdNET-Pi detections via `birdnet_feed.py` |

**Persona:** a warm, enthusiastic naturalist. Leads with the bird's name, then the one field mark or sound that settles it; names the likeliest bird when a description is vague and says what to check next. Never invents sightings.

**Why people love it:** it turns "what's that bird?" into a conversation on the trail with no cell signal. The listening station makes it feel alive ("any owls tonight?"), and mnemonics like "quick, three beers!" and "who cooks for you?" stick.

---

## Files

```
examples/DAWN-CHORUS/
  DAWN-CHORUS.md       this guide
  birdnet_feed.py      BirdNET-Pi detections → Del-Fi sensor facts (run every minute)
  knowledge/           the source documents below
```

| Knowledge file | Answers questions like |
|---|---|
| `songs-and-calls.md` | "what bird goes fee-bee?", "something hoots who cooks for you" |
| `forest-songbirds.md` | "tell me about the Varied Thrush", "what's the tiny brown bird with its tail up?" |
| `marsh-and-water-birds.md` | "what ducks are on the pond?", "what's grunting in the cattails?" |
| `owls-and-raptors.md` | "what owls live here?", "is that an eagle or a vulture?" |
| `woodpeckers-and-hummingbirds.md` | "big black woodpecker with a red crest", "hummingbird feeder recipe" |
| `look-alikes.md` | "Downy or Hairy woodpecker?", "Cooper's or Sharp-shinned hawk?" |
| `seasons-and-migration.md` | "when do the swallows come back?", "what is the dawn chorus?" |
| `preserve-guide.md` | "where's the best place to see herons?", "can I bring my dog?" |
| `birding-basics.md` | "how do I identify birds by sound?", "how do I use this node?" |
| `found-a-bird.md` | "I found a baby bird on the ground", "a bird hit my window" |
| `birdnet-station.md` | "how accurate is the listening station?" |
| `sighting-log.md` | "what has been seen lately?" (dated, updated weekly) |

Songs are organised by what they sound like, not by species, because that is how people ask. Each section names the sound and one or two birds, so a question about a sound retrieves exactly that passage.

---

## Suggested config.yaml

```yaml
# Birding oracle for Salmonberry Creek Preserve, with live BirdNET detections.
node_name: "DAWN-CHORUS"
oracle_type: "observatory"

personality: >
  You are DAWN-CHORUS, the friendly naturalist of Salmonberry Creek Preserve.
  You love birds and help people identify what they see and hear. Lead with
  the bird's name, then the one clue that settles it. If a description fits
  several birds, name the likeliest and say what to check. Never invent
  sightings: live detections come only from the listening station.

model: "gemma4:e4b"                # on a Raspberry Pi 5, start with gemma3:1b
wiki_builder_model: "gemma4:12b"   # build on a desktop, then copy wiki/ over

knowledge_folder: ./knowledge
wiki_folder: ./wiki

# The sighting log changes weekly; its passages get a "last updated" header
time_sensitive_files:
  - sighting-log.md

# Remember a few turns, so "it had a red crest" follows up a question
memory_max_turns: 3

# Visitors share sightings with !post and read them with !board
board_enabled: true
board_post_ttl: 86400

fallback_message: "Not sure about that one. Tell me its size, colours and sound, or try !topics."
welcome_footer: "DAWN-CHORUS: birds of Salmonberry Creek. !data = live detections"

# Tier 0: questions with these words are answered straight from the
# listening station's facts (birdnet_feed.py), without the language model
fact_query_keywords:
  - right now
  - this morning
  - tonight
  - last night
  - today
  - so far
  - detected
  - arrivals

# Hear other Del-Fi nodes and refer out-of-scope questions to them
mesh_knowledge:
  gossip:
    enabled: true
```

---

## Live Detections (Tier 0)

[BirdNET-Pi](https://github.com/Nachtzuster/BirdNET-Pi) listens through a USB microphone around the clock and identifies birds with BirdNET, logging each detection to an SQLite database. `birdnet_feed.py` (standard library only) summarises that log into five facts in Del-Fi's sensor feed, `cache/sensor_feed.json` next to your config. Del-Fi polls the feed every 30 seconds. Other facts already in the feed, such as a weather sensor's, are kept.

| Fact | What it holds | Example question |
|---|---|---|
| `singing_now` | species detected in the last 15 minutes | "what's singing right now?" |
| `birds_detected_today` | species count since midnight, the most frequent | "what birds were detected today?" |
| `owls_tonight` | owls since 18:00 (last night's, before 18:00) | "any owls tonight?" |
| `dawn_chorus` | songbirds heard 4–9 am | "how was the dawn chorus this morning?" |
| `new_arrivals` | heard in the last 14 days, not in the 4 months before | "any new arrivals?" |

Run it every minute from cron on the BirdNET-Pi (`crontab -e`):

```
* * * * * python3 /home/pi/birdnet_feed.py --out /home/pi/del-fi/cache/sensor_feed.json
```

If BirdNET-Pi runs on a separate Pi, write the feed locally and copy it across, for example `--out /tmp/sensor_feed.json && scp -q /tmp/sensor_feed.json pi@dawn-chorus.local:del-fi/cache/`. The default database path is `~/BirdNET-Pi/scripts/birds.db`; pass `--db` if yours differs. `--now "2026-09-25 07:45"` pretends it is that time, for testing against old data.

**Choosing trigger words:** a question is answered from the station only if it contains one of `fact_query_keywords` *and* shares a word with a fact name. The words above are all about time ("right now", "tonight"), so identification questions ("I heard a bird that goes fee-bee") still reach the knowledge base. Avoid adding words like "bird" or "heard": they would send ID questions to the station.

If the station stops reporting, each fact is marked STALE after 15 minutes (singing now) or an hour (the others), so the node never presents old detections as current.

---

## Example Conversation

The station answers below are real Del-Fi output, from `birdnet_feed.py` run on a test database. The knowledge-base answers show the intended style; your model's wording will differ.

```
You:   what's singing right now?
Node:  DAWN-CHORUS: Singing Now: Pacific Wren 7:44, Song Sparrow 7:43,
       Chestnut-backed Chickadee 7:43, Spotted Towhee 7:42, Red-breasted
       Nuthatch 7:41, Black-capped Chickadee 7:40 (birdnet-pi, now)

You:   I heard a bird that goes fee-bee
Node:  That's a Black-capped Chickadee: a clear two-note whistle, the
       second note lower. Males sing it from January on. Whistle it back
       and one may answer.

You:   something is making a long eerie whistle in the cedars
Node:  Likely a Varied Thrush: one long, buzzy whistle on a single pitch,
       then a pause and another on a new pitch. The first of the autumn
       was heard on the Cedar Trail on Sep 23.

You:   any owls tonight?
Node:  DAWN-CHORUS: Owls Tonight: since 18:00: Barred Owl (3), Western
       Screech-Owl (1); last at 4:12 (birdnet-pi, now)

You:   big woodpecker on a dead tree, what is it?
Node:  Could be a Pileated Woodpecker or a Northern Flicker. Pileated is
       crow-sized and black with a red crest; a flicker is brown with a
       spotted belly. Which was it?
You:   black with a red crest
Node:  Black with a red crest: that's a Pileated Woodpecker, the biggest
       woodpecker here. A pair has been chopping a big new hole by Cedar
       Trail footbridge 2 this month.

You:   found a fluffy baby bird hopping on the trail
Node:  That's a fledgling, and it's normal: its parents are feeding it
       nearby. Leave it, keep dogs and people back, and watch from a
       distance. Only move it into a nearby shrub if it's in danger.

You:   !post Pileated Woodpecker at Cedar Trail footbridge 2, 8:15am
You:   !board
Node:  [!7c3e just now]: Pileated Woodpecker at Cedar Trail footbridge 2,
       8:15am
```

---

## Hardware and Placement

- **Microphone:** an omnidirectional USB microphone in a rain shield, about 10 ft up under the eaves of the Beaver Pond Blind, away from dripping water and the wind. It hears the pond, the marsh and the forest edge; the knowledge base says so, so users know what it cannot hear.
- **Computers:** BirdNET-Pi is happiest on its own Raspberry Pi (a Pi 4 is enough), with Del-Fi and Ollama on a Raspberry Pi 5 8GB, in one weatherproof box linked by a short Ethernet cable. Running both on one Pi 5 may work with a 1B model; measure it before relying on it.
- **Radio:** any Meshtastic radio with an outdoor antenna on the blind's roof. A 160-acre preserve is well within one node's range.
- **Power:** two Pis and a radio draw very roughly 10–15 W around the clock. Solar through a Northwest winter needs a large panel and battery, so measure your draw with a USB power meter before sizing it, or use mains power if the site has it.

---

## Keeping It Fresh

- **Weekly:** copy notable `!board` posts and station highlights into `sighting-log.md`, newest first, each with a date. The watcher recompiles the page within a minute, and answers from it show how old it is.
- **Seasonally:** check `seasons-and-migration.md` against your own arrival dates; eBird's bar charts for your county are the best source.
- **Rebuilds:** after big edits, run `python main.py --build-wiki` with the builder model on a desktop and copy `wiki/` to the node.

---

## Adapting It to Your Area

Keep the structure and swap the birds: a sounds-first songs file, species accounts by habitat, look-alikes, a month-by-month calendar, a site guide and a dated log. BirdNET-Pi's species list for your location and eBird's county bar charts tell you which birds to cover. Write sections the way people ask ("sounds like a flute", "can I bring my dog?"), and use both singular and plural forms of key words, since Del-Fi matches words exactly.
