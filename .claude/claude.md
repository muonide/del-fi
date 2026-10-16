# Del-Fi — Project Specification

<!-- Version: 0.3 | Date: 2026-09-25 -->
<!-- Authoritative spec for all Del-Fi development. -->
<!-- Sub-specs: .claude/spec-*.md | Brand: .claude/brand.md -->
<!-- AI agent onboarding: .github/COPILOT.md -->

---

## Contents

1. [Purpose](#1-purpose)
2. [Non-Negotiable Constraints](#2-non-negotiable-constraints)
3. [Architecture Overview](#3-architecture-overview)
4. [Knowledge System — LLM Wiki](#4-knowledge-system--llm-wiki)
5. [CLI Workflows](#5-cli-workflows)
6. [Module Responsibilities](#6-module-responsibilities)
7. [Tier Hierarchy](#7-tier-hierarchy)
8. [Command Registry](#8-command-registry)
9. [Configuration Summary](#9-configuration-summary)
10. [Project Layout](#10-project-layout)
11. [Development Conventions](#11-development-conventions)
12. [Testing Contract](#12-testing-contract)
13. [Sub-Spec Index](#13-sub-spec-index)

---

## 1. Purpose

Del-Fi is an offline AI oracle daemon that bridges LoRa mesh radio networks with
locally-hosted LLMs and a compiled wiki knowledge base. It enables communities to
ask questions to an AI system running entirely on local hardware — no internet,
no cloud, no accounts.

**Core loop:**

1. Someone on the mesh sends a DM to the Del-Fi node.
2. The router classifies the message (command / gossip / query).
3. Commands are handled inline. Queries pass through the knowledge tier hierarchy.
4. The formatter enforces the 230-byte limit and chunks if needed.
5. The response is sent back over the radio as one or more DMs.

**Deployment contexts:** wilderness observatories, neighborhood community hubs,
emergency response nodes, event oracles, trade/workshop nodes, lore/art installations.
See `.claude/brand.md` for oracle persona types.

**Hardware target:** Small ARM or x86 machines (Raspberry Pi 5, Jetson Orin,
Mac Mini) running Ollama with 1B–7B parameter models. The system degrades
gracefully to smaller models; oracle profiles auto-tune retrieval parameters.

---

## 2. Non-Negotiable Constraints

These three constraints are invariant. No feature, optimisation, or shortcut may
violate them.

### 2.1 The 230-Byte LoRa Limit

Every message delivered to the radio **must be ≤ 230 bytes** (LoRa practical
payload, leaving protocol headroom below the 256-byte physical limit).

- The `Formatter` is the sole enforcer. It runs last, always, before every send.
- Long answers are chunked. First N chunks auto-send (default 3); `!more` fetches rest.
- Truncation must respect UTF-8 character boundaries — never cut a multi-byte sequence.
- The constraint is a feature: it forces concision, which improves answer quality.

### 2.2 Offline-First

Del-Fi must operate with **zero internet connectivity**. Every external dependency
must have a local fallback or graceful degradation path.

- LLM inference: Ollama, local. If Ollama is unreachable at startup, the daemon
  starts anyway; a health-check retry loop runs in the background.
- Embeddings: Ollama (`nomic-embed-text`), local.
- Knowledge: `wiki/` compiled locally from `knowledge/` source files via `--build-wiki`.
- Peer discovery: gossip protocol over the mesh itself, no DNS or HTTP.
- No remote config fetch, no telemetry, no optional cloud features.

### 2.3 Never Hallucinate

Del-Fi must **refuse to answer from the LLM's training knowledge** when the
question cannot be answered from the local knowledge base.

- If retrieval finds nothing: the next tier answers, and finally the
  `fallback_message` (or a list of known topics) — never an LLM-fabricated answer.
- The system prompt instructs the LLM to answer **only** from the provided
  excerpts; when they don't directly answer, it may share the closest relevant
  facts they contain and say what they cover. A bare refusal ("I don't know")
  falls through to the next tier.
- If the LLM itself fails, the node says so ("my language model isn't
  reachable") — it never reports a failure as "I don't have docs on that".
- Sensor facts (FactStore Tier 0) are ground truth; the LLM does not reason
  about them — they are inserted as-is into the response.
- Peer-cached answers (Tier 2) are always labelled with the source node.

---

## 3. Architecture Overview

```
[LoRa Radio]
     │  serial / tcp / ble
     ▼
[MeshAdapter]          ← pluggable: meshtastic | meshcore | simulator
     │  msg_queue (threading.Queue)
     ▼
[Dispatcher]
     │
     ├─ Commands ──► inline handler ──► [Formatter] ──► MeshAdapter.send_dm()
     │
     └─ Queries  ──► query_worker (thread) ──► [Tier Hierarchy]
                                                      │
                          ┌───────────────────────────┴──────────────────────────┐
                          │ Tier 0  FactStore      sensor_feed.json, no LLM      │
                          │ Tier 1  WikiEngine     compiled wiki pages + Ollama   │
                          │ Tier 2  PeerCache      SQLite, trusted peer Q&A       │
                          │ Tier 3  GossipDir      referrals only, no answers     │
                          └──────────────────────────────────────────────────────┘
                                                      │
                                             [Formatter + Chunker]
                                                      │
                                             MeshAdapter.send_dm()
```

### 3.1 Concurrency Model

Threading-based. The main thread runs the `Dispatcher` loop; one worker thread
answers questions (one LLM call at a time). Background threads: radio
supervisor (reconnect), wiki watcher, Ollama health check, maintenance (cache
flush, memory expiry), fact watcher, gossip announcer (when enabled).

> **Note:** An asyncio rewrite is planned as Phase 3. Until then, all concurrency
> uses `threading` + `queue.Queue`. Do not introduce `asyncio` into the current
> codebase.

### 3.2 Message Flow

```
                    ┌──────────────────────────────────────────────┐
receive DM          │ Dispatcher (main thread)                     │
sender + text  ───► │  classify: command | gossip | query | empty  │
                    │  command → router inline → send              │
                    │  gossip  → gossip directory (no reply)       │
                    │  query / !retry → rate limit → bounded queue │
                    └──────────────────────────────────────────────┘
                                        │ query_queue
                    ┌───────────────────▼──────────────────────────┐
                    │ worker thread → Router                       │
                    │  1. first-contact greeting ("hi")            │
                    │  2. Tier 0: FactStore.lookup(query)          │
                    │  3. response cache (history-free questions)  │
                    │  4. Tier 1: WikiEngine.query(query)          │
                    │  5. Tier 2: PeerCache.lookup(query)          │
                    │  6. Tier 3: GossipDirectory.referral(query)  │
                    │  7. fallback_message / known topics          │
                    │  8. format, chunk, auto-send ≤3, !more rest  │
                    └──────────────────────────────────────────────┘
```

Full detail: `.claude/spec-router.md`.

---

## 4. Knowledge System — LLM Wiki

The primary architectural change in v0.2. Full specification: `.claude/spec-knowledge.md`.

### 4.1 Three Layers

The knowledge system follows the Karpathy LLM Wiki pattern: instead of
retrieving from raw documents at query time, the LLM **compiles** raw sources
into a structured wiki once at ingest time. The wiki is the retrieval unit.

| Layer | Path | Owner | In git? | Description |
|-------|------|-------|---------|-------------|
| Raw sources | `knowledge/` | Human | No — gitignored, deployment-specific | Original documents: field guides, logs, procedures, etc. |
| Compiled wiki | `wiki/` | LLM | No — gitignored, rebuilt per deployment | Synthesised entity pages with frontmatter, cross-links, summaries |
| Schema | `.claude/claude.md` | Human + LLM | Yes — always tracked | Conventions the LLM follows to write and maintain the wiki |

### 4.2 Key Properties

- Raw sources are **immutable** — the LLM reads them but never modifies them.
- Wiki pages are **LLM-owned** — do not hand-edit them; re-run `--build-wiki`.
- The wiki **compounds**: each new source updates existing pages and adds new ones.
- Query time uses the wiki, not the raw sources; the compilation work is done once.

### 4.3 Wiki Page Format

```yaml
---
title: Wildlife Guide
tags: [wildlife, elk, mountain-lion, coyote, mule-deer, identification]
sources: [wildlife-guide.md]
last_ingested: 2026-04-22
---

# Wildlife Guide

## Mountain Lion (Puma concolor)

Apex predator at the station. Mostly nocturnal; active at dawn/dusk.
Prey: elk calves, mule deer, snowshoe hare. Territory 80–200 sq mi.
Tracks: 3" round, no claw marks (retractable). → [[trail-camera-log]]

...
```

### 4.4 `wiki/index.md` Format

Updated on every `--build-wiki` run. This is the primary navigation file:

```markdown
## Index

| Page | Summary | Tags | Updated |
|------|---------|------|---------|
| [[wildlife-guide]] | Species reference — elk, mountain lion, mule deer | wildlife, species, id | 2026-04-22 |
| [[weather-station]] | Davis station readings, thresholds, historical norms | weather, temperature, wind | 2026-04-22 |
```

### 4.5 `wiki/log.md` Format

Append-only. One entry per ingest or lint run:

```markdown
## [2026-04-22] ingest | wildlife-guide.md
Updated: wildlife-guide. Added elk calving season note. Pages touched: 2.

## [2026-04-22] lint
Issues: 1 orphan page (flora-guide). 0 stale pages. 2 missing cross-refs.
```

### 4.6 Build Pipeline (`--build-wiki`)

Uses `wiki_builder_model` (config key). Intended to run with a larger model
before deployment; falls back to `model` if `wiki_builder_model` is unset.

1. Scan `knowledge/` for `.md` and `.txt` files (skip unchanged by MD5 hash).
2. For each changed file: prompt the builder LLM with the source content.
3. LLM extracts entities, summaries, cross-references, and writes `wiki/<topic>.md`.
4. Update `wiki/index.md` entry for the page.
5. Append to `wiki/log.md`.

Contradiction policy: if the new source contradicts an existing claim, the new
claim replaces it and the old text is annotated:
`> [superseded 2026-04-22 by weather-station.md]`

### 4.7 Query Pipeline (Tier 1)

1. Find pages: BM25 over `wiki/index.md` rows (slug, summary, tags), then
   ChromaDB similarity over page embeddings, then whole-word search of page
   bodies. Keep the top 3.
2. Split those pages' **source files** into section passages and rank them
   against the question (BM25, weighted by page rank).
3. Fill the context budget (`max_context_tokens`) best-first; output in
   document order per page.
4. Generate with the serving model (`model`), with a fixed `num_ctx`.

The wiki is the *index*; the raw sources are what the model reads — but only
the passages that matter, so a 1B model on a Pi gets a few hundred tokens,
not whole files. Full detail: `.claude/spec-knowledge.md §7`.

### 4.8 Staleness

Wiki pages include `last_ingested:` frontmatter. Pages mapped to
`time_sensitive_files` config list include age in the context header:
`[weather-station — last updated 2 hrs ago]`

### 4.9 Lint (`--lint-wiki`)

Checks the wiki for: orphan pages, missing cross-refs, stale pages
(last_ingested > `wiki_stale_after_days`), data gaps flagged in the log.

---

## 5. CLI Workflows

```bash
# Normal operation
python main.py [--config PATH] [--simulator]

# Build wiki — run before deployment, preferably with a larger model
python main.py --build-wiki [--config PATH]
# Uses: wiki_builder_model (config); falls back to model.
# Reads: knowledge/   Writes: wiki/

# Lint wiki — health check on the compiled knowledge base
python main.py --lint-wiki [--config PATH]
# Reads: wiki/   Outputs: lint report to stdout

# Simulator mode — no radio hardware, stdin/stdout
python main.py --simulator [--config PATH]
# Supports sender prefix: !a1b2c3d4> message text

# Benchmark — time answers on this hardware (no radio)
python main.py --bench [QUESTIONS_FILE] [--config PATH] [--model NAME]
# One question per line; default: one per wiki topic. --model works in
# every mode and overrides model from config.yaml (with its profile).

# Web control panel (localhost only)
python main.py --gui [--config PATH] [--gui-port 5174] [--no-browser]
```

`--build-wiki`, `--lint-wiki` and `--bench` do not start the radio listener.
`--build-wiki` and `--lint-wiki` are non-destructive and safe to re-run at
any time.

---

## 6. Module Responsibilities

### Layout (v0.3)

| File | Class | Responsibility |
|------|-------|----------------|
| `main.py` | — | Entrypoint: daemon wiring, `--simulator`, `--build-wiki`, `--lint-wiki`, `--bench`, `--gui` |
| `del_fi/config.py` | — | YAML loading, validation, defaults, oracle and size profiles |
| `del_fi/bench.py` | — | `--bench`: timed answers to a question list |
| `del_fi/core/dispatcher.py` | `Dispatcher` | Main loop: classify, rate limit, bounded queue, worker |
| `del_fi/core/router.py` | `Router` | Commands, tier hierarchy, response cache, `!more` buffers |
| `del_fi/core/knowledge.py` | `WikiEngine` | Wiki build, passage retrieval, lint, watcher, model check, answer timing |
| `del_fi/core/formatter.py` | — | 230-byte enforcement, markdown stripping, chunking |
| `del_fi/core/facts.py` | `FactStore` | Sensor feed, freshness, Tier 0 |
| `del_fi/core/peers.py` | `PeerCache`, `GossipDirectory` | Tier 2 storage, Tier 3 gossip |
| `del_fi/core/memory.py` | `ConversationMemory` | Per-sender history with TTL |
| `del_fi/core/board.py` | `Board` | Community board, rate limiting, injection filter |
| `del_fi/core/text.py`, `fsutil.py` | — | Shared tokenizer; atomic fsync'd writes |
| `del_fi/mesh/base.py` | `MeshAdapter` | Abstract adapter interface |
| `del_fi/mesh/meshtastic_adapter.py` | `MeshtasticAdapter` | Meshtastic driver with reconnect supervisor |
| `del_fi/mesh/meshcore_adapter.py` | `MeshCoreAdapter` | MeshCore stub |
| `del_fi/mesh/simulator.py` | `SimulatorAdapter` | stdin/stdout for development |
| `del_fi/gui/server.py` | — | Optional Flask control panel (localhost) |

### Module contracts

| Module | Key public methods |
|--------|-------------------|
| `Dispatcher` | `start()`, `run(inbox)`, `handle(sender, text)`, `stop()` |
| `Router` | `classify(text)`, `route(sender, text) → str|None`, `route_multi(sender, text) → list[str]|None`, `prepare_retry(sender)`, `flush_cache()` |
| `WikiEngine` | `build(file=None, model=None) → int`, `prune_removed_sources()`, `query(q, peer_ctx="", history="", board_context="") → (str, bool)` (raises `LLMError`), `lint() → list[str]`, `watch(interval, stop)` |
| `formatter` | `format_response(text, max_bytes, provenance) → (first, chunks, truncated)`, `paginate(...)`, `chunk_text(...)` |
| `PeerCache` | `lookup(q) → dict|None`, `store(q, answer, peer_id, peer_name) → bool`, `prune()` |
| `GossipDirectory` | `receive(node_id, text) → bool`, `referral(q) → str|None`, `announce() → str`, `announce_loop(send_broadcast, stop)` |
| `ConversationMemory` | `add_turn(sender, user, asst)`, `format_for_prompt(sender) → str`, `clear(sender)` |
| `Board` | `post(sender, text) → str`, `read(query="") → str`, `clear(sender) → str`, `format_for_context(query, max_posts) → str` |
| `FactStore` | `watch(stop)`, `lookup(query) → str|None`, `format_snapshot() → str` |
| `MeshAdapter` | `connect() → bool`, `send_dm(dest, text) → bool`, `send_broadcast(text, channel) → bool`, `close()`, `reconnect_loop()` |

---

## 7. Tier Hierarchy

Queries pass through tiers in order, stopping at the first match:

```
Tier 0 — FactStore
  Condition: query contains any keyword in fact_query_keywords config list
  Action:    return sensor fact string directly (no LLM call, no latency)
  Source:    sensor_feed.json, current readings with freshness annotation

Tier 1 — WikiEngine (local compiled knowledge)
  Condition: always attempted unless Tier 0 matched
  Action:    BM25 on wiki/index.md → read top wiki pages → LLM generates answer
  Source:    wiki/ compiled from knowledge/ by --build-wiki
  Fallback:  if similarity below threshold, continue to Tier 2

Tier 2 — PeerCache (trusted peer Q&A)
  Condition: Tier 1 had no context AND a cached peer answer matches
  Action:    return that answer as-is, labelled "[via PEER-NODE]"
  Source:    SQLite cache; only node IDs in mesh_knowledge.peers are trusted
  Status:    storage built; the sync protocol that fills it is on the roadmap

Tier 3 — GossipDirectory (referrals)
  Condition: Tier 2 miss AND gossip is enabled AND a heard node's topics match
  Action:    return "Try VALLEY-ORACLE (!a1b2c3d4) — covers [topics]"
  Source:    gossip announcements (opt-in), keyed by sender node ID, 24 h TTL
  Note:      never transfers knowledge, only points to other nodes

Fallback
  Condition: all tiers missed
  Action:    return fallback_message config value
  Default:   a list of known topics, or "I don't have docs on that. Try !topics…"
```

Commands bypass all tiers and run inline in the dispatcher thread.

---

## 8. Command Registry

| Command | Args | Description |
|---------|------|-------------|
| `!help` | — | Command list with one-line descriptions |
| `!topics` | — | List of wiki pages available on this node |
| `!status` | — | Node name, model, uptime, wiki page count, Ollama health |
| `!board` | `[query]` | Recent board posts, or search if query given |
| `!post` | `<text>` | Add a post to the board (200 char max) |
| `!unpost` | — | Remove all of sender's board posts |
| `!more` | `[N]` | Next chunk of last response, or chunk N specifically |
| `!retry` | — | Re-run last query, bypassing the response cache |
| `!forget` | — | Clear conversation memory for this sender |
| `!peers` | — | List nodes in the gossip directory with their topics |
| `!data` | — | Snapshot of all FactStore readings with ages |
| `!ping` | — | Liveness check; responds with node name |

All command responses pass through the Formatter before being sent; long
output (`!board`, `!topics`, `!data`, `!peers`) is split on line boundaries
and continued with `!more`.

---

## 9. Configuration Summary

Full schema with all keys: `.claude/spec-config.md`

**Required fields:**

```yaml
node_name: "RIDGELINE"
model: "gemma3:4b-it-qat"
```

**Wiki system:**

```yaml
wiki_folder: ./wiki                  # path to compiled wiki; default ./wiki
wiki_builder_model: "qwen2.5:7b"     # model for --build-wiki; falls back to model
wiki_rebuild_on_start: false         # run --build-wiki automatically at daemon startup
wiki_stale_after_days: 30            # days before --lint-wiki flags a page as stale
wiki_watch_enabled: true             # watcher: recompile changed files, drop deleted
wiki_patch_model: ""                 # model the watcher uses; defaults to model
max_context_tokens: 1500             # passage budget per answer
```

**Dispatcher (v0.3):** `rate_limit_seconds`, `rate_limit_notice`,
`query_queue_size`, `busy_notice`. **Peers & gossip:** the `mesh_knowledge`
block (opt-in). All keys: `.claude/spec-config.md`.

**Oracle profiles** — auto-applied by substring match on `model`:

| Match | Parameters overridden |
|-------|----------------------|
| `gemma4:e2b`, `gemma3:1b`, `llama3.2:1b` | `similarity_threshold: 0.35`, `rag_top_k: 2`, `max_context_tokens: 512`, `small_model_prompt: true`, `reorder_context: true` |
| `gemma4:e4b`, `gemma3:4b`, `qwen2.5:3b` | `similarity_threshold: 0.28`, `rag_top_k: 4` |
| `gemma4:12b` | `similarity_threshold: 0.25`, `rag_top_k: 5`, `max_context_tokens: 3000` |
| (no match) | By parameter count from Ollama: ≤2.5B like `gemma3:1b`, ≤9B like `gemma4:e4b`, larger like `gemma4:12b` |

---

## 10. Project Layout

```
del-fi/
├── main.py                      ← entrypoint
├── del_fi/                      ← importable package
│   ├── __init__.py              ← __version__
│   ├── config.py
│   ├── bench.py                 ← --bench
│   ├── core/
│   │   ├── dispatcher.py        ← main loop, rate limit, worker
│   │   ├── router.py
│   │   ├── knowledge.py         ← WikiEngine
│   │   ├── formatter.py
│   │   ├── facts.py
│   │   ├── peers.py             ← PeerCache + GossipDirectory
│   │   ├── memory.py
│   │   ├── board.py
│   │   ├── text.py              ← shared tokenizer
│   │   └── fsutil.py            ← atomic writes
│   ├── mesh/
│   │   ├── base.py
│   │   ├── meshtastic_adapter.py
│   │   ├── meshcore_adapter.py
│   │   └── simulator.py
│   └── gui/                     ← optional Flask control panel
├── tests/                       ← one test_X.py per module, plus test_stress.py
├── knowledge/                   ← gitignored; deployment-specific raw sources
├── wiki/                        ← gitignored; LLM-compiled, rebuilt via --build-wiki
├── examples/                    ← GUIDE.md, DAWN-CHORUS/, RIDGELINE/, NEIGHBORHOOD/,
│                                   knowledge-dungeon/, sensor_feed.example.json
├── docs/index.html              ← project landing page
├── config.example.yaml          ← portable template; always commit
├── CHANGELOG.md
├── requirements.txt
├── .claude/                     ← specs (always tracked)
├── .github/                     ← COPILOT.md, CONTRIBUTING.md, ISSUE_TEMPLATE/, workflows/
└── SECURITY.md
```

**Principles:**
- `del_fi/` is the importable package. `main.py` is the script entrypoint.
- `core/` has no radio dependencies. `mesh/` imports from `core/`, not vice versa.
- `tests/` mirrors `del_fi/` structure: `tests/test_X.py` tests `del_fi/core/X.py`
  (`test_mesh.py` covers `mesh/`, `test_gui.py` the GUI, `test_main.py` main.py).
- `examples/` contains deployment templates — never imported at runtime.
- `wiki/` and `knowledge/` are gitignored; they are per-deployment artifacts.
- `.claude/` and `.github/` are always tracked in git.

---

## 11. Development Conventions

### Code style

- Python 3.10+. No walrus operator (`:=`) for readability.
- Type hints on all public method signatures.
- Module-level logger: `log = logging.getLogger(__name__)`. No `print()` in library code.
- Max line length: 100 characters.
- Imports: stdlib → third-party → local. One blank line between groups.

### Error handling

- Catch only what you can handle. Let unexpected exceptions propagate to the
  dispatcher's top-level try/except.
- Never swallow exceptions silently. Minimum: `log.exception("context")`.
- Radio send failures: log and discard — never crash the daemon for a failed send.
- Ollama failures at runtime: log warning, mark health down, let the retry loop recover.
- ChromaDB failure at startup: log warning, continue without RAG (degrade gracefully).

### Naming

| Scope | Convention | Example |
|-------|-----------|---------|
| Files | `snake_case.py` | `knowledge.py` |
| Classes | `PascalCase` | `WikiEngine` |
| Methods / functions | `snake_case` | `build_wiki()` |
| Constants | `UPPER_SNAKE_CASE` | `DEFAULT_MAX_BYTES` |
| Config keys (YAML) | `snake_case` | `wiki_builder_model` |
| Node names | `ALL-CAPS-HYPHENATED` | `RIDGELINE`, `VALLEY-ORACLE` |
| Wiki files | `kebab-case.md` | `wildlife-guide.md` |
| Knowledge files | `kebab-case.md` or `.txt` | `trail-camera-log.md` |

### Git

- Commits: imperative mood, ≤ 72 chars (`add WikiEngine.query() BM25 search`)
- Never commit: `config.yaml`, `knowledge/`, `wiki/`, `vectorstore/`, `*.log`, `cache/`
- Always commit: `.github/`, `.claude/`, `examples/`, `tests/`, `config.example.yaml`

### Adding a new mesh adapter

1. Create `del_fi/mesh/<name>_adapter.py`
2. Subclass `MeshAdapter` from `del_fi/mesh/base.py`
3. Implement all abstract methods: `connect()`, `send_dm(dest, text)`, `close()`
4. Optionally implement `reconnect_loop()` for automatic recovery
5. Register adapter name in `config.py` → `MESH_ADAPTERS` dict
6. Add tests in `tests/test_mesh.py`

### Adding a new command

1. Add entry to `COMMAND_REGISTRY` in `router.py`
2. Implement `_cmd_<name>(self, sender: str, args: str) -> str`
3. Return value is passed through the Formatter — it will be truncated if needed
4. Update `_cmd_help()` to include the new command
5. Add tests in `tests/test_router.py`

---

## 12. Testing Contract

### Requirements

- All modules in `del_fi/core/` require unit tests.
- All `MeshAdapter` subclasses require tests covering: connect, send_dm, rate
  limiting, and deduplication.
- `Formatter` requires: 230-byte enforcement, UTF-8 boundary safety, chunking.
- `WikiEngine` requires: build (mocked LLM), query (mocked index), lint.

### Conventions

- Use `unittest` (stdlib). No pytest dependency. Bare `def test_...()` functions
  are fine: each module ends with a `load_tests` hook (`tests/_support.py`)
  that collects them, wherever they are defined in the file.
- Mock Ollama calls at the HTTP level — no real inference in the test suite.
- Mock ChromaDB with an in-memory collection.
- Mock mesh radio with `SimulatorAdapter` pointing to a `StringIO` buffer.
- Test files mirror source layout: `tests/test_knowledge.py` tests `del_fi/core/knowledge.py`.

### Test categories

| File | What it covers |
|------|----------------|
| `test_config.py` | Loading, validation, oracle profile application |
| `test_knowledge.py` | Build pipeline, query pipeline, BM25 search, lint |
| `test_router.py` | Command dispatch, tier selection, response cache, `!more` |
| `test_formatter.py` | Truncation at sentence/clause/word, UTF-8 safety, chunking |
| `test_memory.py` | Ring buffer, TTL expiry, disk persistence |
| `test_board.py` | Posting, rate limiting, content injection detection |
| `test_facts.py` | Sensor feed parsing, freshness tracking, Tier 0 fast path |
| `test_mesh.py` | Adapter contract, Meshtastic connect/reconnect, dedup, broadcasts |
| `test_dispatcher.py` | Rate limit + notice, queue bound, busy notice, `!retry`, worker |
| `test_peers.py` | Peer cache trust, gossip parsing, directory, referrals, announcer |
| `test_gui.py` | Request guard, config merge/validation, board, sandbox (needs flask) |
| `test_fsutil.py`, `test_main.py` | Atomic writes; log formatter |
| `test_stress.py` | Concurrent queries, queue saturation, response time budget |

### Running tests

```bash
# All tests (no hardware or Ollama required; CI: Python 3.10–3.13)
python -m unittest discover -s tests -t .

# Single module
python -m unittest tests.test_knowledge
```

---

## 13. Sub-Spec Index

| Spec | File | Topics |
|------|------|--------|
| Knowledge System | `.claude/spec-knowledge.md` | LLM Wiki pattern, WikiEngine class, build pipeline, query pipeline, BM25, ChromaDB integration, passage retrieval, context budget, staleness, lint, pruning, watcher |
| Mesh Adapters | `.claude/spec-mesh.md` | MeshAdapter ABC, Meshtastic (serial/TCP/BLE), MeshCore stub, Simulator, dedup, reconnect supervisor |
| Router | `.claude/spec-router.md` | Message classification, commands, tier hierarchy, response cache, `!more` buffer, Dispatcher (rate limit, queue, worker), gossip protocol |
| Formatter | `.claude/spec-formatter.md` | 230-byte algorithm, truncation priority, UTF-8 safety, markdown stripping rules, chunking |
| Memory / Board / FactStore | `.claude/spec-memory.md` | ConversationMemory ring buffer, MessageBoard injection filter, FactStore sensor schema, freshness lifecycle |
| Configuration | `.claude/spec-config.md` | All config keys with types, defaults, validation, oracle profiles |
| Brand | `.claude/brand.md` | Visual identity, CSS palette, response tone, node naming, oracle persona types |

---

<!-- End of Del-Fi Project Specification v0.3 -->
