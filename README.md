```
 ██████   █████  ██             █████  ████
 ██   ██  ██     ██             ██      ██
 ██   ██  ████   ██    ════    ████    ██
 ██   ██  ██     ██             ██      ██
 ██████   █████  █████          ██     ████

          ██   █  █████  █████
          ███  █  ██       ██
          ██ █ █  ████     ██
          ██  ██  ██       ██
          ██   █  █████    ██

         community AI oracle over LoRa mesh radio
```

# Del-Fi

**A daemon that bridges LoRa mesh radio networks with locally-hosted LLMs and a compiled knowledge base.**

Drop documents into a folder, connect a $30 radio, and your community has an AI oracle that answers questions over mesh — no internet, no cloud, no accounts. Just radio waves and local knowledge.

Runs on **Meshtastic** radios today. A **MeshCore** adapter is scaffolded behind the same pluggable interface but does not talk to a radio yet (see [Roadmap](#roadmap)).

<!-- TODO: photo of real hardware here — Pi + LoRa radio, hand-labeled project box -->

---

## How It Works

```
[LoRa Radio] <--serial/tcp/ble--> [Mesh Adapter] <--> [Dispatcher] <--> [Router]
                                                    rate limit, queue,      |
                                                    one LLM call at a time  |
        ┌───────────────────────────────────────────────────────────────────┘
        ├─ Tier 0  sensor facts        (no LLM — exact readings)
        ├─ Tier 1  compiled wiki + LLM (your documents)
        ├─ Tier 2  trusted peer answers (labelled [via PEER]; sync on roadmap)
        └─ Tier 3  gossip referrals    ("Try VALLEY-ORACLE — covers geology")
                         |
                 [Formatter] ≤ 230 bytes per message, !more for the rest
```

Someone on the mesh sends your node a DM. Del-Fi finds the relevant passages in your documents, hands just those to a local LLM, and sends back a concise answer — within the ~230-byte LoRa message limit. No internet required. Everything runs on your hardware.

Before serving, `--build-wiki` compiles your documents into a small **wiki**: one index page per document, with titles, tags and cross-references. At question time the wiki is used to *find* the right documents, and the most relevant sections of those documents are what the model actually reads.

---

## What You Need

**Compute (pick one):**

| Hardware | Speed | Power | Cost | Best For |
|---|---|---|---|---|
| Raspberry Pi 5 8GB | ~5 tok/s (3B) | ~10W | ~$80 | Budget field nodes — start with a 1B model |
| Jetson Orin Nano Super | ~30 tok/s (3B) | ~15W | ~$249 | Solar field nodes |
| Mac Mini M4 | ~18 tok/s (7B) | ~30W | ~$499 | Powered stations |

**Radio:**
- Any [Meshtastic-supported LoRa radio](https://meshtastic.org/docs/hardware/devices/) — Heltec V3 (~$20) works great
- Antenna placement matters more than radio choice for range

**Software:**
- Python 3.10+
- [Ollama](https://ollama.com/) (manages local LLM inference)
- A Meshtastic radio flashed with current firmware

---

## Install

```bash
# 1. Install Ollama (if you haven't)
curl -fsSL https://ollama.com/install.sh | sh

# 2. Pull models
ollama pull gemma4:e4b             # serving model (pick your size)
ollama pull nomic-embed-text       # embeddings (optional semantic search)

# Raspberry Pi recommendation: a 1B model runs well on Pi hardware.
# ollama pull llama3.2:1b

# 3. Clone Del-Fi
git clone https://github.com/geodesic-glitch/del-fi.git
cd del-fi

# 4. Create a virtual environment and install dependencies
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 5. Set up config and knowledge
mkdir -p ~/del-fi/knowledge
cp config.example.yaml ~/del-fi/config.yaml
# Edit ~/del-fi/config.yaml — set node_name, model, and radio_port
cp your-documents/*.md ~/del-fi/knowledge/

# 6. Compile the wiki, then run
python main.py --build-wiki
python main.py
```

The config file has one required field (`node_name`). Everything else has a sensible default (`model` defaults to `gemma4:e4b`). A bad config prints a human-readable error, not a traceback. Del-Fi looks for `config.yaml` next to `main.py` first, then `~/del-fi/config.yaml`; pass `--config PATH` to be explicit.

> **Raspberry Pi / Debian note:** Modern Raspberry Pi OS (Bookworm+) marks the system Python as externally managed (PEP 668), so `pip install` outside a venv will fail. The virtual environment in step 4 handles this. If you see `error: externally-managed-environment`, make sure you activated the venv (`source venv/bin/activate`) before running pip. You may also need `sudo apt install python3-full` if `python3 -m venv` isn't available.

> **Important:** You must activate the venv **every time** you open a new shell before running Del-Fi. If you see `No module named 'ollama'` (or any other import error), you forgot to `source venv/bin/activate`. The `ollama` CLI is a separate system binary — it will work without the venv, but the Python package won't. For a headless Pi you can use a systemd service instead (see [Running as a Service](#running-as-a-service)).

**Simulator mode** (no radio needed — for development and testing):

```bash
python main.py --simulator
```

This reads from stdin and writes to stdout, so you can test the whole pipeline without hardware. Type `!a1b2c3d4> your question` to talk as a specific sender. Logs go to `del_fi.log` next to your config.

---

## Add Knowledge

Drop `.txt` or `.md` files into your knowledge folder (`~/del-fi/knowledge/` above):

```bash
cp field-guide-to-edible-plants.txt ~/del-fi/knowledge/
cp emergency-procedures.md ~/del-fi/knowledge/
python main.py --build-wiki
```

`--build-wiki` compiles each changed file into a wiki page. It can use a bigger model than the one serving answers — set `wiki_builder_model` (e.g. a 12B model on your desktop), build there, and copy the folder to the node; unchanged files are not rebuilt.

While the daemon runs, it also watches the folder: new or edited files are compiled within a minute using the serving model (or `wiki_patch_model`), and pages for deleted files are removed. Run `python main.py --lint-wiki` to check the wiki's health.

File names become your topic list: `wilderness-first-aid.md` shows up as "Wilderness First Aid" in `!topics`. See [examples/GUIDE.md](examples/GUIDE.md) for how to write documents that answer well, and [examples/](examples/) for complete starter deployments.

---

## Use It

From any Meshtastic app, DM the Del-Fi node:

```
You:   What animals have been spotted this week?
Node:  CAM-1 logged elk herd (8 cows, 2 calves)
       Feb 14. Coyote pair at 06:12 Feb 15.
       Mountain lion — single adult, heading west.
Node:  CAM-2 (Creek Crossing): mule deer Feb 12,
       ermine Feb 16 at dawn. Fox near willows
       Feb 17 18:44.
Node:  CAM-3 (Spruce Hollow): gray wolf-like canid
       Feb 17 23:11 — pending ID confirmation.
       Total: 9 species, 21 events this week.
```

Responses up to 3 messages are delivered automatically. If the answer is longer than that, the last message ends with `[!more]`:

```
You:   Tell me about mountain lions in detail
Node:  Mountain lion (Puma concolor) — apex predator
       at Ridgeline Station. Mostly nocturnal;
       active dawn and dusk.
Node:  Prey: elk calves, mule deer, snowshoe hare.
       Territory 80-200 sq mi. Tracks: 3" round,
       no claw marks (retractable).
Node:  Feb 15 sighting: adult, ~120 lbs, heading
       west along the ridge. Typical of winter
       range expansion. [!more]
You:   !more
Node:  Avoid corner situations on trail. Make noise.
       Do not run. If approached: stand tall, make
       eye contact, back away slowly.
```

### Commands

| Command | What it does |
|---|---|
| `!help` | Usage instructions and available commands |
| `!topics` | List loaded knowledge base topics |
| `!status` | Node health, model info, uptime, page count |
| `!board` | Read the community message board (recent posts) |
| `!board <term>` | Search the board for posts matching `<term>` |
| `!post <text>` | Post a message to the community board |
| `!unpost` | Remove all of your board posts |
| `!data` | Current sensor readings with their age |
| `!more` | Next part of a long response |
| `!more 2` | Re-send part 2 (if a message was lost) |
| `!retry` | Re-ask your last question, bypassing the cache |
| `!forget` | Clear your conversation history on this node |
| `!ping` | Liveness check |
| `!peers` | Other Del-Fi nodes this one has heard (needs gossip on) |

Commands are answered immediately, even while the node is busy thinking about someone else's question. Questions are limited to one every 30 seconds per sender (configurable, and `!retry` counts as a question); a sender who asks too fast gets a single "one question per 30s" reply rather than silence. If the node is busy, you're told your place in line.

---

## Configuration

`~/del-fi/config.yaml` — the most useful settings (defaults shown). [`config.example.yaml`](config.example.yaml) lists more, and [`.claude/spec-config.md`](.claude/spec-config.md) documents every key.

```yaml
# Required
node_name: "FARM-ORACLE"

# Optional — defaults shown
model: "gemma4:e4b"
personality: "You are a helpful and concise community assistant."
knowledge_folder: ./knowledge   # relative to this file
radio_connection: serial        # serial | tcp | ble
radio_port: /dev/ttyUSB0        # or host[:port] for TCP
want_ack: true                  # radio retries DMs until acknowledged
rate_limit_seconds: 30          # one question per sender per 30s
query_queue_size: 10            # questions waiting for the LLM
busy_notice: true               # tell queued users they're in line
auto_send_chunks: 3             # auto-send first N parts; !more beyond that
response_cache_ttl: 300
max_context_tokens: 1500        # how much of your documents the model reads
ollama_host: "http://localhost:11434"
ollama_timeout: 120
log_level: info
log_file: ""                    # e.g. delfi.log — rotated at 1 MB
```

Small models get tuned defaults automatically (a *profile* matched on the model name): `gemma3:1b`, `llama3.2:1b` and `gemma4:e2b` read less context with a shorter prompt.

### Sensor Data (Tier 0)

Scripts can write live readings to `cache/sensor_feed.json` (next to your config). Questions like "what's the temperature?" are then answered straight from the feed — no LLM, no hallucination — with the reading's age, or `STALE` if it's old. See [`examples/sensor_feed.example.json`](examples/sensor_feed.example.json); timestamps can be Unix seconds or ISO-8601.

### Web GUI

```bash
pip install flask   # if not already installed
python main.py --gui
```

A local control panel for editing config, building the wiki, chatting with the oracle, and reading the board and logs. It listens on `127.0.0.1` only; on a headless Pi, use `ssh -L 5174:localhost:5174 pi@yourpi` and open `http://localhost:5174`. Saving config keeps settings the form doesn't show and backs up the previous file to `config.yaml.bak`; comments in the file are not kept.

### MeshCore Configuration

```yaml
mesh_protocol: meshcore
meshcore:
  port: "/dev/ttyUSB0"        # serial port or host:port
  connection: serial           # serial | tcp
  baud_rate: 115200
```

> **Note:** The MeshCore adapter is a stub: it has the full scaffolding but `connect()` does not yet talk to a radio. See `del_fi/mesh/meshcore_adapter.py` if you want to bring it online.

---

## Mesh Knowledge (Optional)

Del-Fi nodes can know about each other. Everything here is **off by default** — privacy first.

```yaml
mesh_knowledge:
  gossip:
    enabled: true
    announce_interval: 4h       # minimum 15m
    directory_ttl: 24h
    channel: 0

  peers:                        # trusted by hardware node ID, never by name
    - node_id: "!a1b2c3d4"
      name: "MARINA-ORACLE"
```

### Three Tiers of Knowledge

```
Tier 1 — Operator Knowledge     Your docs. Sacrosanct. Never overridden.
Tier 2 — Peered Knowledge       Cached Q&A from trusted peers. Tagged with source.
Tier 3 — Mesh Gossip            Metadata only. Enables referrals, not answers.
```

### Gossip (Tier 3) — works today

With gossip enabled, your node broadcasts a short announcement of its topics every few hours, and listens for other nodes' announcements. When someone asks about something you don't cover but a nearby node does, they get a pointer instead of a dead end:

```
Try MARINA-ORACLE (!a1b2c3d4) — covers fishing-regulations,
species-id, bait-guide
```

No knowledge is transferred and no trust is required — just a pointer, including the node ID so the user can DM the right node even if another one copies its name. `!peers` lists the nodes your node has heard.

### Peering (Tier 2) — on the roadmap

Peering is a trust decision between humans: meet the other operator, exchange **hardware node IDs** (like `!a1b2c3d4` — display names aren't authenticated), and add each other under `mesh_knowledge.peers`.

The cache that would hold peers' answers — only from those node IDs, always tagged `[via MARINA-ORACLE]` — is built and tested, but **the sync protocol that fills it is not implemented yet**. Adding peers today records the trust relationship for when it lands.

What peering will never do:

- Mesh knowledge never masquerades as your docs
- Nodes never automatically forward queries
- Peering is never automatic
- Local docs always win in conflicts

---

## Troubleshooting

**"Radio not detected"**
- Check USB connection: `ls /dev/ttyUSB*` or `ls /dev/ttyACM*`
- Verify Meshtastic firmware is flashed
- Try `radio_connection: tcp` with the radio's IP if serial is flaky
- Run with `--simulator` to test everything else while you debug the radio
- If the radio drops later (unplugged, rebooted), Del-Fi reconnects on its own — watch the log for "attempting radio reconnect"

**"Ollama not available"**
- Is Ollama running? `curl http://localhost:11434/api/tags`
- Is the model pulled? `ollama list` should show your configured model
- Del-Fi retries every 30 seconds. Commands work while waiting, and questions get an honest "my language model isn't reachable" reply instead of a wrong answer

**"No wiki pages loaded"**
- Run `python main.py --build-wiki` after adding documents
- Files must be `.txt` or `.md`; convert PDFs to text first
- Check the knowledge folder path in your config (relative paths are relative to the config file)
- `python main.py --lint-wiki` reports orphaned pages, missing sources and stale pages

**Slow responses**
- On a Pi, use a 1B model (`gemma3:1b` or `llama3.2:1b`); larger models answer better but much more slowly
- Lower `max_context_tokens` — the model reads less, answers sooner
- Large documents are fine: only their relevant sections are sent to the model

**Messages getting cut off**
- LoRa limit is ~230 bytes. Long responses are split and auto-sent up to 3 messages in a row.
- If there are more parts, the last auto-sent message ends with `[!more]` — send `!more` to continue.
- If a part was lost, `!more 2` re-sends part 2.

---

## Use Cases

**Trail Oracle** — Solar node at a trailhead. Plant ID, trail conditions, wildlife, emergency procedures. No cell signal needed.

**Farm Oracle** — Planting calendars, livestock medicine, equipment repair. The knowledge in one person's head, available to everyone on the property.

**Emergency Response** — Triage protocols, shelter locations, phrase books. Works when cell towers don't.

**Interactive Fiction** — Text adventures over radio. 230 bytes forces Zork-density prose. `!more` becomes "look around." Geocaching crossover: hide a solar node with a story.

**Festival Concierge** — Schedules, vendor maps, food guides at a maker faire. No cell service required.

**Museum Docent** — Local history, oral histories, old maps. Works across the whole property. Cheaper than a touchscreen kiosk.

**The Dead Drop** — Mysterious node appears on mesh. Cryptic name, oddly specific local knowledge. No one knows who runs it. Part art installation, part folklore.

**Neighborhood Mesh** — HOA rules, garbage schedule, business hours. "When's bulk trash pickup?"

---

## Architecture

```
main.py                  Entry point: daemon, --simulator, --build-wiki, --lint-wiki, --gui
del_fi/
  config.py              YAML loading, validation, defaults, model profiles
  core/
    dispatcher.py        Main loop: rate limit, question queue, worker thread
    router.py            Commands, tier hierarchy, response cache, !more buffers
    knowledge.py         WikiEngine: build, passage retrieval, lint, watcher
    formatter.py         Markdown stripping, 230-byte chunking
    facts.py             Sensor feed (Tier 0)
    peers.py             Peer cache (Tier 2) + gossip directory (Tier 3)
    memory.py            Per-sender conversation history
    board.py             Community message board, injection filtering
    text.py, fsutil.py   Shared tokenizer; atomic, fsync'd file writes
  mesh/
    base.py              MeshAdapter interface
    meshtastic_adapter.py  Meshtastic radios (serial/TCP/BLE), auto-reconnect
    meshcore_adapter.py    MeshCore (stub)
    simulator.py         stdin/stdout for development
  gui/                   Optional Flask control panel
tests/                   unittest suite — no radio or Ollama needed
```

Dependencies: `pyyaml`, `ollama`, `meshtastic`, plus optional `chromadb` (semantic search) and `flask` (GUI).

### Adding a New Mesh Protocol

1. Create `del_fi/mesh/<protocol>_adapter.py` with a class that inherits from `MeshAdapter`
2. Implement `connect()`, `send_dm()`, `close()` and `reconnect_loop()` (and `send_broadcast()` for gossip)
3. Register it in `del_fi/mesh/__init__.py` → `ADAPTERS` and in `SUPPORTED_PROTOCOLS` in `config.py`
4. Add tests in `tests/test_mesh.py` — see [`.claude/spec-mesh.md`](.claude/spec-mesh.md)

### Startup Sequence

```
1. Config        Load YAML, validate, exit with a readable error (the one crash)
2. Wiki engine   Connect to Ollama (or keep retrying), open ChromaDB if present
3. Stores        Sensor facts, peer cache, gossip directory, board, memory
4. Radio         Connect; a supervisor thread reconnects whenever the link drops
5. Background    Wiki watcher, Ollama health check, cache flush, sensor feed,
                 gossip announcements (if enabled)
6. Dispatcher    Commands answered inline; questions queued for one worker thread
```

Principle: **always start, never block.** A missing radio or unavailable Ollama doesn't prevent launch. Components come online as they become available.

---

## Running as a Service

On a headless Pi, run Del-Fi via systemd so it starts on boot and always uses the correct venv — no SSH session required.

Create `/etc/systemd/system/delfi.service`:

```ini
[Unit]
Description=Del-Fi mesh oracle
After=network.target ollama.service
Wants=ollama.service

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/del-fi
ExecStart=/home/pi/del-fi/venv/bin/python main.py --config /home/pi/del-fi/config.yaml
Restart=on-failure
RestartSec=10

CPUQuota=80%
MemoryMax=75%

[Install]
WantedBy=multi-user.target
```

> **Key detail:** `ExecStart` points directly at the venv's Python binary (`venv/bin/python`), so you don't need to activate the venv — systemd handles it.

```bash
sudo systemctl daemon-reload
sudo systemctl enable delfi          # start on boot
sudo systemctl start delfi           # start now
journalctl -u delfi -f               # tail logs
```

Adjust `User`, `WorkingDirectory`, and paths if your clone or config is somewhere else.

### Pi Thermal Tips

If your Pi is running hot during inference:

- **Use a smaller model** — `gemma3:1b` or `llama3.2:1b` instead of 4B+
- **Raise `rate_limit_seconds`** (default 30) to 60 or more to give the CPU recovery time between questions
- **Lower `max_context_tokens`** (e.g. 512) and **`num_predict`** (e.g. 128) to reduce per-request compute
- **Keep `memory_max_turns` low** (it's off by default) — each remembered turn makes the prompt longer
- **Add a heatsink + fan** — the official Pi 5 active cooler makes a big difference
- **Cap inference, not Del-Fi** — the model runs in the `ollama` service, so `CPUQuota` in `delfi.service` doesn't limit it. Run `sudo systemctl edit ollama` and add `CPUQuota=300%` under `[Service]` to leave one of the Pi's four cores free

---

## Running Tests

```bash
python -m unittest discover -s tests -t .          # all tests
python -m unittest tests.test_router               # one module
python -m pytest tests/                            # pytest works too
```

No radio, Ollama or ChromaDB needed; the GUI tests run when Flask is installed. CI runs the suite on Python 3.10–3.13.

---

## Contributing

Del-Fi follows the "boring technology" principle. Before adding a dependency, ask: can this be done with stdlib? Before adding a feature, ask: does this make the first-run experience harder?

The four non-negotiable constraints:

1. **Don't crash the daemon.** Every error caught, every failure recovered.
2. **Be honest.** No answer? Say so. Peer answer? Say where it came from.
3. **Fit in LoRa.** ≤ 230 bytes per message. No exceptions.
4. **Be readable.** Plain text. No markdown artifacts. No "as an AI language model."

Everything else is fair game.

---

## Roadmap

- **Peer Q&A sync (Tier 2)** — fill the peer cache from trusted nodes over the mesh during quiet hours.
- **MeshCore adapter** — bring the stub online against the MeshCore library.
- **Constrained wiki patches** — let the watcher update a page incrementally instead of recompiling it with the serving model.

**Meshmouth** — LLM-native wire format for oracle-to-oracle traffic.

Today gossip uses human-readable strings, and peer sync would too. Both ends are language models — they don't need `key=value` headers and full English sentences on the wire. Meshmouth is a compact encoding that LLMs can produce and consume natively: fixed token-budget preambles, lossy semantic compression, symbolic shorthand, and negotiated per-pair codebooks. The goal is 3–5× more meaning in the same 230-byte LoRa frame when oracles talk to each other, while still decompressing cleanly for human questioners. Think of it as a pidgin the oracles converge on — not a hand-designed binary protocol, but a model-discovered compressed language.

---

## License

GPL-3.0 — matching the Meshtastic ecosystem.
