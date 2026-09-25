# Del-Fi — Configuration Specification

<!-- Parent: .claude/claude.md §9 -->
<!-- Source of truth: DEFAULTS / ORACLE_PROFILES / MESH_DEFAULTS in del_fi/config.py -->
<!-- Related: config.example.yaml, all spec-*.md files (cross-reference config keys) -->

---

## 1. Config File Loading

```
1. Path: --config PATH, else config.yaml next to main.py, else ~/del-fi/config.yaml
2. Parse YAML (must be a mapping); node_name is required
3. Warn (don't fail) about unknown top-level keys — usually typos
4. Merge DEFAULTS; apply the oracle profile for the model (§4)
5. Resolve paths against the config file's directory (§5)
6. Build mesh_knowledge (defaults + v0.2 legacy keys, §2.10)
7. Validate (§3)
```

`read_config(path)` raises `ConfigError` with an operator-readable message;
`load_config(path)` prints it (`[del-fi] Config error: …`) and exits 1 — the
one place where crashing is correct. The GUI uses `read_config` so a bad
edit is rejected without killing the server. Config is read once at startup;
there is no live reload.

The resolved path is recorded as `cfg["_config_path"]`; the GUI edits that
file, never a guess.

---

## 2. Key Reference

Every key is optional except `node_name`.

### 2.1 Identity

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `node_name` | str | — (**required**) | `ALL-CAPS-HYPHENATED`. Appears in replies and gossip. |
| `personality` | str | "You are a helpful and concise community assistant." | Added to the system prompt. |
| `fallback_message` | str | `""` | Reply when every tier misses. Empty = list known topics instead. |
| `oracle_type` | str | — | Shown in the GUI only. |

### 2.2 Model & Ollama

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `model` | str | `gemma4:4b` | Serving model (answers). Selects an oracle profile (§4). |
| `ollama_host` | str | `http://localhost:11434` | |
| `ollama_timeout` | number | `120` | Seconds per answer before giving up ("that took too long"). |
| `num_predict` | int ≥ 16 | `300` | Max output tokens per answer. |
| `num_ctx` | int ≥ 512 or empty | derived | Context window. Empty = derived from `max_context_tokens` and fixed for the process (see spec-knowledge §7.2). |
| `embedding_model` | str | `nomic-embed-text` | For optional ChromaDB semantic search. |

### 2.3 Knowledge / Wiki

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `knowledge_folder` | path | `./knowledge` | Raw source documents. |
| `wiki_folder` | path | `./wiki` | Compiled wiki. |
| `wiki_builder_model` | str | = `model` | Model for `--build-wiki` (can be much larger). |
| `wiki_build_timeout` | number | `600` | Seconds per page for builds. |
| `wiki_rebuild_on_start` | bool | `false` | Run a build at daemon startup (blocking). |
| `wiki_watch_enabled` | bool | `true` | Background watcher: rebuild changed pages, prune deleted ones. |
| `wiki_watch_interval_seconds` | int | `60` | Watcher poll interval. |
| `wiki_patch_model` | str | = `model` | Model the watcher rebuilds with. Never defaults to `wiki_builder_model`. |
| `wiki_stale_after_days` | int | `30` | `--lint-wiki` stale threshold. |
| `time_sensitive_files` | list[str] | `[weather-station.md, trail-camera-log.md]` | Sources whose passages get a "last updated" header. |

### 2.4 Retrieval

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `max_context_tokens` | int ≥ 64 or empty | 1500 (profiles: 512 / 3000) | Budget for retrieved passages. |
| `similarity_threshold` | float | `0.28` | Min cosine similarity for semantic page matches. |
| `rag_top_k` | int | `4` | Semantic search result count. |
| `small_model_prompt` | bool | `false` | Shorter system prompt (1B/2B profiles). |
| `reorder_context` | bool | `false` | Most relevant page last (1B/2B profiles). |

### 2.5 Mesh / Radio

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `mesh_protocol` | `meshtastic` \| `meshcore` | `meshtastic` | MeshCore is a stub. |
| `radio_connection` | `serial` \| `tcp` \| `ble` | `serial` | Meshtastic transport. |
| `radio_port` | str | `/dev/ttyUSB0` | Serial device, `host[:port]`, `[ipv6]:port`, or BLE address. |
| `want_ack` | bool | `true` | Send DMs with wantAck (firmware retries across hops). |
| `max_response_bytes` | int 50–256 | `230` | Hard per-message limit. Keep 230 unless you know your firmware's payload limit. |
| `auto_send_chunks` | int ≥ 1 | `3` | Chunks sent before requiring `!more`. |
| `meshcore` | mapping | `{port, connection, baud_rate}` | MeshCore stub settings. |

### 2.6 Dispatcher: rate limit & queue

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `rate_limit_seconds` | number ≥ 0 | `30` | One question per sender per window (`!retry` counts). 0 = off. |
| `rate_limit_notice` | bool | `true` | Reply once per window to a rate-limited sender. |
| `query_queue_size` | int ≥ 1 | `10` | Max questions waiting for the LLM; extra ones get a "too many queued" reply. |
| `busy_notice` | bool | `true` | Tell queued senders they're in line. |

### 2.7 Response cache

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `response_cache_ttl` | number ≥ 0 | `300` | Seconds a cached answer is reused (history-free questions only). |
| `persistent_cache` | bool | `true` | Persist to `cache/response_cache.json` (flushed every 60 s). |

### 2.8 Conversation memory

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `memory_max_turns` | int ≥ 0 | `0` | Turns remembered per sender. 0 = off. Capped at 50. |
| `memory_ttl` | int | `3600` | Seconds of inactivity before a conversation is forgotten. |
| `persistent_memory` | bool | `false` | Persist to `cache/conversation_memory.json`. |

### 2.9 Board

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `board_enabled` | bool | `false` | `!board`, `!post`, `!unpost`. |
| `board_max_posts` | int | `50` | Capped at 500. |
| `board_post_ttl` | int | `86400` | Seconds a post lives. |
| `board_show_count` | int | `5` | Posts per `!board` (split with `!more`). |
| `board_persist` | bool | `true` | Persist to `cache/board.json`. |
| `board_rate_limit` / `board_rate_window` | int / int | `3` / `3600` | Posts per sender per window. |
| `board_blocked_patterns` | list[regex] | `[]` | Extra patterns rejected on post. |

### 2.10 Peers & gossip (`mesh_knowledge`)

```yaml
mesh_knowledge:
  gossip:
    enabled: false          # announce + listen; also gates Tier 3 referrals
    announce_interval: 4h   # seconds or 30s/15m/4h/7d; minimum 15m
    directory_ttl: 24h
    channel: 0              # 0-7
  peers:                    # trusted for Tier 2, by hardware node ID only
    - node_id: "!a1b2c3d4"
      name: "MARINA-ORACLE"
  sync:                     # Tier 2 Q&A sync — reserved, not implemented
    max_cache_age: 7d
    max_cache_entries: 500
```

v0.2 top-level keys are still read, with a deprecation warning:
`trusted_peers` (node IDs kept, names dropped), `peer_cache_ttl` →
`sync.max_cache_age`, `max_cache_entries` → `sync.max_cache_entries`,
`gossip_announce_interval` → `gossip.announce_interval`.

### 2.11 Sensors (FactStore)

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `fact_feed_file` | path | `cache/sensor_feed.json` | Sensor feed (schema: spec-memory.md §3). |
| `fact_watch_interval_seconds` | int | `30` | Feed poll interval. |
| `fact_query_keywords` | list[str] | weather / camera words | Whole-word gate for Tier 0. |

### 2.12 Logging

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `log_level` | `debug` \| `info` \| `warning` \| `error` \| `critical` | `info` | |
| `log_file` | path | `""` | Also log to this file (rotated at 1 MB × 3). The simulator logs to `del_fi.log` next to the config when unset. |

---

## 3. Validation Rules

Violations raise `ConfigError` (exit 1 from the CLI):

| Rule | Message mentions |
|------|------------------|
| file missing / not YAML / not a mapping | the path |
| `node_name` missing or empty | `node_name` |
| `model` not a non-empty string | `model` |
| `mesh_protocol` not supported | supported protocols |
| `radio_connection` not serial/tcp/ble | `radio_connection` |
| `max_response_bytes` not an int 50–256 | the LoRa limit |
| `rate_limit_seconds`, `response_cache_ttl` negative or non-numeric | the key |
| `auto_send_chunks` < 1, `num_predict` < 16, `num_ctx` < 512, `max_context_tokens` < 64, `memory_max_turns` < 0 | the key |
| `query_queue_size` < 1, `ollama_timeout` ≤ 0 | the key |
| `log_level` unknown | valid levels |
| `mesh_knowledge.gossip.announce_interval` < 15 min, bad `directory_ttl`, `channel` not 0–7 | the key |
| a `mesh_knowledge.peers` entry without a `!xxxxxxxx` node ID | node IDs, not names |
| bad `sync.max_cache_age` / `max_cache_entries` | the key |

Unknown top-level keys are logged as a warning, not an error.

---

## 4. Oracle Profiles

Per-model defaults, applied by case-insensitive substring match on `model`
(first match wins). Keys set explicitly in config.yaml always win.

| Profile | Overrides |
|---------|-----------|
| `gemma4:2b`, `gemma3:1b`, `llama3.2:1b` | `similarity_threshold: 0.35`, `rag_top_k: 2`, `max_context_tokens: 512`, `small_model_prompt: true`, `reorder_context: true` |
| `gemma4:4b`, `gemma3:4b`, `qwen2.5:3b` | `similarity_threshold: 0.28`, `rag_top_k: 4` |
| `gemma4:12b` | `similarity_threshold: 0.25`, `rag_top_k: 5`, `max_context_tokens: 3000` |
| anything else | config values as-is |

`gemma3:1b` matches `gemma3:1b-it-qat`; it does not match `gemma3:12b`.

---

## 5. Path Resolution

`knowledge_folder`, `wiki_folder` and `log_file` are resolved relative to the
**directory containing config.yaml** (after `~` expansion), not the working
directory. Runtime state always lives next to the config:

| Derived key | Path |
|-------------|------|
| `_config_path` | the config file itself |
| `_cache_dir` | `cache/` (response cache, board, memory, facts, peer DB) |
| `_gossip_dir` | `gossip/` |
| `_vectorstore_dir` | `vectorstore/` |
| `_seen_senders_file` | `seen_senders.txt` |

So `python main.py --config /etc/del-fi/config.yaml` works from anywhere.

---

## 6. config.example.yaml

The committed template. It must list every key a typical operator changes,
with defaults shown and short comments, and contain nothing deployment-
specific beyond the example node name. When a key is added to `DEFAULTS`,
add it here, to §2, and — if the GUI should edit it — to the GUI form.

---

<!-- End of spec-config.md -->
