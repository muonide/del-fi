# Changelog

## Unreleased

Tools for testing on real hardware: see where each answer's time goes,
whether each reply reached its recipient, and which model suits the machine.

### Measuring

- **Answer timing in the log.** Every answer logs the pages it used and the
  model's time: loading, reading the prompt and writing the answer, with
  token counts and speeds reported by Ollama. The response line adds the
  total time and how long the question waited in the queue.
- **Delivery reports.** With `want_ack: true`, each message part is
  followed to the end: delivered (the recipient's ACK), relayed by a
  neighbour, or not delivered with the radio's reason (`MAX_RETRANSMIT`,
  `NO_ROUTE`, ...). Parts never confirmed are reported after 3 minutes.
- **`python main.py --bench [FILE]`** asks a list of questions (default: one
  per wiki topic) and prints each answer with its timing and mesh message
  count, then the median and slowest times. `--model NAME` replaces the
  configured model in any mode, so models can be compared without editing
  the config. `examples/DAWN-CHORUS/bench-questions.txt` is a sample list.

### Models

- **Any Ollama model.** When it connects to Ollama, Del-Fi checks that the
  serving and embedding models are pulled (logging the `ollama pull`
  command if not) and reads the serving model's size and capabilities.
  `--build-wiki` stops with the same advice when the builder model is
  missing, instead of failing on every file.
- **Size profiles.** Models without a profile by name (Qwen, Phi, Mistral,
  ...) get one by parameter count: up to 2.5B like `gemma3:1b`, up to 9B
  like `gemma4:e4b`, larger like `gemma4:12b`. Keys set in config.yaml win.
- **Thinking off.** Reasoning models such as `qwen3` are asked not to think:
  thinking would spend the whole answer budget before the answer. Any
  `<think>` text that still comes back is removed from answers and wiki
  pages.
- **No cold starts.** The model is loaded at startup and whenever Ollama
  comes back, with the same context window as answers, and stays loaded
  (`ollama_keep_alive`, default `-1`).

### Upgrading

- Ollama now keeps the serving model in memory between questions. To free
  the memory when the node is idle, set `ollama_keep_alive: "30m"` (Ollama's
  own default is 5 minutes).

## 0.3 — "Make it true" (2026-09-25)

v0.3 makes the documentation true: every feature the README describes either
works or is clearly marked as not built yet, and every bug found in a full
review of v0.2 is fixed with a regression test.

### Answers

- **Relevant passages instead of whole files.** Questions are answered from
  the best-matching sections of your documents, within a context budget
  (`max_context_tokens`, default 1500). v0.2 sent whole source files (tens of
  KB), which Ollama silently truncated; the 1B/2B profiles ended up with
  ~15× their intended budget. `num_ctx` is now derived and stays fixed, so
  Ollama never reloads the model between questions.
- **Honest failures.** If Ollama is down or times out, the reply says so
  ("my language model isn't reachable", "that took too long — !retry")
  instead of claiming there are no docs on a topic the node covers.
- **No cross-user leaks.** With conversation memory on, one sender's
  history-shaped answer could be served from the cache to another sender.
  The cache is now used only for history-free questions.
- **`!more` fixed.** After a long answer, the next command or short answer
  no longer carries leftover parts of the old one. `!more` returns exactly
  one part. Long command output (`!board`, `!topics`, `!data`, `!peers`) is
  split into parts instead of cut off.
- Hedged answers ("The trail is 3 mi. I'm not sure about ice.") are no longer
  thrown away as "I don't know".

### Radio and daemon

- **Auto-reconnect.** A radio that drops after startup (USB blip, TCP reset,
  radio reboot) is reconnected by a supervisor thread. v0.2 stayed running
  but deaf, so systemd never restarted it.
- **DMs use `wantAck`** (`want_ack: true`), so the firmware retries each part
  across hops.
- **Rate limiting in one place.** Senders who ask too fast get one "one
  question per 30s" reply per window instead of silence. `!retry` now counts
  as a question (it could bypass the limit). The question queue is bounded
  (`query_queue_size`), with an honest "too many questions queued" reply.
- `radio_port: host:port` is honoured for TCP (the port was ignored).
- Tracebacks are logged again (the log formatter discarded them).
- Optional rotating log file: `log_file`.

### Knowledge base

- A wiki built on one machine and copied to another is no longer rebuilt
  from scratch there (the change cache used absolute paths).
- Deleting a document removes its wiki page, index row and embedding.
- `wiki_watch_enabled: false` works; the watcher rebuilds with
  `wiki_patch_model` or the serving model, never the (larger) builder model.
- Build truncation uses Ollama's `done_reason`; builds get an 8K window so
  long sources aren't cut; backslashes in model output no longer break the
  index; `notes.md` and `notes.txt` no longer overwrite each other's page.

### Sensor facts (Tier 0)

- Feeds written as the spec documents them (Unix-seconds timestamps) crashed
  every sensor query in v0.2. Unix seconds, ISO-8601 with `Z`/offset, and
  naive ISO (node-local time) all work now; bad timestamps show as STALE.
- Keywords match whole words ("temp" no longer matches "temple").
- New: `examples/sensor_feed.example.json`.

### Board

- Only posts relevant to a question reach the LLM, inside markers with a
  random per-prompt nonce, so a post can't break out of the "untrusted"
  block. The injection filter catches more phrasings.

### Gossip and peers

- **Gossip works (opt-in).** Nodes announce their topics and refer
  questions to each other: "Try VALLEY-ORACLE (!a1b2c3d4) — covers geology".
  The directory is keyed by node ID, sanitised and size-capped.
- Peers are configured under `mesh_knowledge.peers` by hardware node ID.
  Peer Q&A sync (what fills Tier 2) is **not implemented yet** — see the
  roadmap.

### GUI

- Saving config no longer deletes settings the form doesn't show
  (`mesh_knowledge`, `meshcore`, board filters): it merges, validates first,
  and backs up the previous file to `config.yaml.bak`. Comments in the file
  are not kept.
- Other websites can no longer change your config through the GUI (CSRF /
  DNS-rebinding guard).
- The chat simulator uses a sandbox, so testing never affects what radio
  users get. The Board tab shows and posts to the live board. Wiki builds
  run in the background.

### Project

- Tests: 25 board tests had never run under `unittest` (11 were failing);
  all test functions are now collected. CI runs on Python 3.10–3.13.
- The daemon loop moved from `main.py` into a tested `Dispatcher`.
- README, specs and examples match the code. `rag.py` (v0.1) removed.
- New example, `examples/DAWN-CHORUS`: a birding oracle for a nature preserve,
  with a sounds-first knowledge base and live BirdNET-Pi detections as
  Tier 0 facts (`birdnet_feed.py`).

### Upgrading from 0.2

- **Start command:** `python main.py` (the README said `delfi.py`, which
  doesn't exist). Update systemd `ExecStart` lines accordingly.
- **Default model:** `gemma4:e4b`. v0.2's default and example configs said
  `gemma4:4b`, which isn't an Ollama tag (Gemma 4 ships as `gemma4:e2b`,
  `e4b`, `12b`, `26b` and `31b`). If your `config.yaml` names `gemma4:4b` or
  `gemma4:2b`, change it to `gemma4:e4b` or `gemma4:e2b`; the model profiles
  now match those tags.
- **Dependencies:** `requirements.txt` asks for the current releases
  (ollama 0.6.2, meshtastic 2.7.11, chromadb 1.5.9, Flask 3.1.3,
  PyYAML 6.0.3). Update with `pip install -U -r requirements.txt`. Embeddings
  now use Ollama's `embed` call (the old `embeddings` call is deprecated);
  pages you already embedded keep working.
- **Peering config:** move `trusted_peers`, `peer_cache_ttl`,
  `max_cache_entries` and `gossip_announce_interval` into the
  `mesh_knowledge` block (see `config.example.yaml`). The old keys still work
  with a warning, except that trusted peers given by *name* are dropped —
  use node IDs.
- **Removed keys:** `channels`, `description`, `node_description`,
  `enable_suggestions_fallback`, `wiki_patch_threshold_pct` did nothing and
  now trigger an "unknown key" warning. `mesh_adapter` in the v0.2 example
  guides was never a key: use `mesh_protocol`, or `--simulator`.
- **Stricter validation:** bad `radio_connection`, `log_level`, `model`, or
  integer settings now stop startup with a clear message.
- **Airtime:** `want_ack: true` means the radio retries DMs until they are
  acknowledged. Set `want_ack: false` for v0.2 behaviour.
- **knowledge/ ships empty:** v0.2 committed two NEIGHBORHOOD example files
  there. If you relied on them, copy them from
  `examples/NEIGHBORHOOD/knowledge/`.
- The wiki cache migrates automatically; no rebuild is needed.
