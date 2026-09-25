# Del-Fi — Router Specification

<!-- Parent: .claude/claude.md §7, §8 -->
<!-- Related: spec-knowledge.md (Tier 1), spec-memory.md (board/facts), spec-formatter.md -->

---

## 1. Message Classification

The dispatcher classifies every incoming message before routing.

### 1.1 Classification rules (in order)

```
1. Empty / whitespace-only       → discard, no reply
2. Command: starts with "!"      → command handler (inline, dispatcher thread)
3. Gossip: matches pattern       → mesh_knowledge.receive(), no reply
4. Query: everything else        → query_worker (thread, via queue)
```

### 1.2 Gossip pattern

```python
GOSSIP_PATTERN = re.compile(
    r"^DEL-FI:\d+:ANNOUNCE:[A-Z0-9\-]+:.*$"
)
```

Example: `DEL-FI:1:ANNOUNCE:RIDGE-ORACLE:topics=wildlife,weather:model=gemma3:4b:uptime=3d`

Gossip messages are forwarded to `GossipDirectory.receive()` with the sender ID.
No reply is sent to the mesh.

### 1.3 Classification is stateless

The classifier does not maintain per-sender state. Conversation context is
managed by `ConversationMemory`. The classifier cannot be tricked into treating
a query as a command by injecting `!` after a preamble — only leading `!` triggers
command dispatch.

---

## 2. Command Dispatch

Commands run **inline in the dispatcher thread**. They must return quickly.
Do not perform LLM calls or disk I/O that could block for > 100ms from a command handler.
`!retry` is an exception — it re-queues to the worker thread.

### 2.1 COMMAND_REGISTRY

```python
COMMAND_REGISTRY: dict[str, Callable] = {
    "help":    self._cmd_help,
    "topics":  self._cmd_topics,
    "status":  self._cmd_status,
    "board":   self._cmd_board,
    "post":    self._cmd_post,
    "unpost":  self._cmd_unpost,
    "more":    self._cmd_more,
    "retry":   self._cmd_retry,
    "forget":  self._cmd_forget,
    "peers":   self._cmd_peers,
    "data":    self._cmd_data,
    "ping":    self._cmd_ping,
}
```

Dispatch:

```python
def _dispatch_command(self, sender: str, text: str) -> str:
    parts = text[1:].split(maxsplit=1)   # strip leading "!"
    cmd = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""
    handler = COMMAND_REGISTRY.get(cmd)
    if handler is None:
        return f"Unknown command: !{cmd}. Try !help"
    return handler(sender, args)
```

### 2.2 Command implementations

All commands return a string that is passed through the Formatter before sending.

#### `!help`

```python
def _cmd_help(self, sender: str, args: str) -> str:
    lines = [
        "!help !topics !status !data !ping",
        "!board [query] !post <text> !unpost",
        "!more [N] !retry !forget !peers",
    ]
    return " | ".join(lines)
```

The help text is designed to fit in ≤ 230 bytes as-is.

#### `!topics`

Returns a comma-separated list of wiki page titles from `wiki/index.md`.
Falls back to `knowledge/` filenames if wiki has not been built.

#### `!status`

Returns: `{node_name} | {model} | up {uptime} | {page_count} pages | ollama:{ok/err}`

#### `!board [query]`

If `args` is empty: return the last 3 board posts.
If `args` given: search board posts containing the keywords.

#### `!post <text>`

Delegates to `MessageBoard.post(sender, args)`. Returns confirmation or error.

#### `!unpost`

Delegates to `MessageBoard.unpost(sender)`. Returns confirmation.

#### `!more [N]`

See §4 below.

#### `!retry`

Re-queues the sender's last query to the worker thread, bypassing the response cache.
If the sender has no remembered query, returns: `"No recent query to retry."`

#### `!forget`

Calls `ConversationMemory.forget(sender)`. Returns: `"Conversation cleared."`

#### `!peers`

Returns the gossip directory contents:
`VALLEY-ORACLE: fishing, lake-levels | FARM-ORACLE: livestock, planting`
Truncated to fit 230 bytes.

#### `!data`

Returns `FactStore.snapshot()` — all current sensor readings with age annotations.

#### `!ping`

Returns: `"{node_name} online"`

---

## 3. Response Cache

Stores question → answer pairs so a repeated question skips the LLM.

### 3.1 Cache key

```python
cache_key = " ".join(query.lower().split()).strip(" ?!.,;:")
```

Case, whitespace and trailing punctuation are normalised. No fuzzy matching.

### 3.2 When the cache is used

- Only for questions asked **without conversation history**. When memory is
  enabled and the sender has history, the answer depends on that history, so
  it is neither read from nor written to the shared cache — otherwise one
  sender's conversation could be served to another.
- Tier 0 (facts) bypasses the cache: freshness is the point.
- Populated with Tier 1 and Tier 2 answers. Fallbacks, referrals and error
  replies are never cached.
- `!retry` evicts the sender's last question before re-running it.
- Commands neither read nor populate the cache.

### 3.3 Storage

In memory, capped at 100 entries (expired entries dropped first, then the
oldest). Persisted to `cache/response_cache.json` every 60 s when dirty and on
shutdown, via an atomic write:

```json
{"where is the trailhead": {"response": "…", "provenance": null, "ts": 1714000000.0}}
```

`provenance` holds the peer name for Tier 2 answers, so a cached peer answer
is still labelled `[via PEER]` when served again.

### 3.4 TTL

Config key: `response_cache_ttl` (default: 300 seconds).

---

## 4. `!more` Buffer

### 4.1 Lifecycle

1. An answer or command output longer than one message is split into chunks
   (`format_response()` for answers; line-aware `paginate()` for command
   output, so board posts and sensor lines are not cut mid-line) and stored
   as the sender's buffer.
2. The first `auto_send_chunks` (default 3) chunks are sent immediately. If
   more remain, the last auto-sent chunk ends with ` [!more]`.
3. Extra chunks are only ever auto-sent from a buffer created by the current
   message — never from an older answer still in the buffer.
4. A new **answer** always replaces or clears the sender's buffer. Short
   **command** output leaves it alone, so `!status` between two `!more`s does
   not lose the pending answer; long command output replaces it.
5. `!more` → exactly one next chunk. `!more N` → re-send chunk N (1-indexed),
   for chunks lost on a lossy mesh.
6. Buffers expire 10 minutes after the last `!more`.

### 4.2 Replies

| Situation | Reply |
|-----------|-------|
| No buffer (never set or expired) | `No pending response. Send a question first.` |
| All chunks sent | `End of response. No more chunks.` |
| `!more N` out of range | `No chunk N. Response has M parts.` |

---

## 5. Dispatcher and Query Worker

`del_fi/core/dispatcher.py` — the daemon's main loop, unit-tested directly
(`tests/test_dispatcher.py`). `main.py` only wires it up.

### 5.1 Inbound handling (dispatcher thread)

| Message | Handling |
|---------|----------|
| empty | ignored |
| gossip (`DEL-FI:`) | `router.route()` inline; no reply |
| command (except `!retry`) | `router.route_multi()` inline; replies sent immediately |
| question, or `!retry` | rate limit → queue → worker |

`!retry` asks `router.prepare_retry(sender)` for the sender's last question
(evicting its cached answer) and then goes through the same path as a new
question, so it runs on the worker and counts against the rate limit.

### 5.2 Rate limit and queue

1. **Rate limit** (`rate_limit_seconds`, default 30, 0 = off): one accepted
   question per sender per window, measured with a monotonic clock (immune
   to the wall-clock jumps a Pi without an RTC makes when NTP syncs). A
   rate-limited question gets **one** reply per window —
   `"NODE: One question per 30s, please. Try again in 12s. Commands still work."`
   — and further ones are dropped silently (`rate_limit_notice: false`
   silences the reply too).
2. **Queue depth** (`query_queue_size`, default 10): when full, the new
   question is turned away with `"NODE: Too many questions queued right now.
   Try again in a few minutes."`. Queued questions are never dropped.
3. **Busy notice** (`busy_notice`, default on): if the worker is busy, a
   sender with nothing else pending gets `router.busy_message(position)` —
   "yours is next" or "N questions ahead of yours".

### 5.3 Worker

One worker thread, one LLM call at a time (small hardware cannot afford
more). For each question it sends `router.route_multi()`'s messages with a
0.5 s pause between chunks. Any exception is logged with its traceback and
the sender gets `"I hit an error processing that. Try again."`.

### 5.4 Shutdown

SIGINT/SIGTERM set the stop flag; `Dispatcher.run()` returns within half a
second, then `main.py` flushes the response cache and closes the radio. An
in-flight LLM call is abandoned (waiting up to `ollama_timeout` would exceed
systemd's default stop timeout).

---

## 6. Tier Hierarchy — Full Flow

See `.claude/claude.md §7` for the overview. Router-specific detail:

### 6.1 Tier 0 — FactStore

`facts.lookup(query)` is a keyword match, not a semantic search. When it
returns a reading, that string is the answer: no LLM call, no cache.

### 6.2 Response cache, then Tier 1 — WikiEngine

```python
answer, had_context = wiki.query(query, history=history, board_context=board_ctx)
```

`had_context=False` means no wiki page matched, or the model declined to
answer from the pages it was given; the router falls through to Tier 2.
If Ollama is not available, the reply says so honestly (no tiers are tried).

### 6.3 Generation failures

`wiki.query()` raises `LLMError` when generation fails. The router never
turns a failure into "I don't have docs on that":

| `LLMError.kind` | Cause | Reply |
|-----------------|-------|-------|
| `unavailable` | Ollama unreachable (also marks it down so the health loop takes over) | "My language model isn't reachable right now…" |
| `timeout` | Model too slow for `ollama_timeout` | "That took too long… or !retry in a minute." |
| `error` | Anything else (e.g. model not pulled) | "I hit an error answering that…" |

### 6.4 Tier 2 — PeerCache

```python
peer = peer_cache.lookup(query)
if peer:
    return peer["response"], peer["peer_name"]   # rendered as "[via NODE] …"
```

The peer's answer is returned as-is with its provenance label; there is no
second LLM call to re-synthesise it.

### 6.5 Tier 3 — GossipDirectory

```python
referral = gossip_dir.referral(query)
if referral:
    return referral      # e.g. "Try VALLEY-ORACLE — covers fishing, lake-levels"
```

### 6.6 Fallback

The `fallback_message` config value if set; otherwise a suggestion listing
known topics (`wiki.suggest()`); otherwise
`"<NODE>: I don't have docs on that. Try !topics to see what I know."`

---

## 7. Gossip Announcement Protocol

Opt-in: `mesh_knowledge.gossip.enabled: true` turns on both announcing and
listening. When off, announcements are ignored, `!peers` says gossip is
off, and Tier 3 referrals never fire.

### 7.1 Announcement format

```
DEL-FI:1:ANNOUNCE:{NODE_NAME}:topics={t1},{t2}:model={model}
```

- `NODE_NAME`: `A-Z0-9-`, max 32 chars
- `topics`: wiki page slugs from this node's `wiki/index.md` (first column
  only), as many as fit in one message (`max_response_bytes`)
- `model`: always last; runs to the end of the message because model names
  contain colons (`llama3.2:3b`)

Broadcast on `gossip.channel` (default 0) every `gossip.announce_interval`
(default 4 h, minimum 15 min) ± 10%, the first one 1–5 minutes after
startup so nodes rebooting together after a power cut don't transmit at
once. A node with no wiki topics does not announce. Announcements arrive
as broadcasts (the Meshtastic adapter forwards `DEL-FI:` broadcasts) or
DMs; both are handled inline, without rate limiting or a reply.

### 7.2 Directory

- Keyed by the **sender's node ID**, not the announced name, so a second
  node claiming a name cannot overwrite the first.
- Announcements are unauthenticated: names, topics (max 12, `a-z0-9-`,
  max 32 chars each) and model are sanitised; the directory holds at most
  64 nodes (oldest evicted).
- Entries expire after `gossip.directory_ttl` (default 24 h).
- Saved to `gossip/node-directory.json` only when an entry is new or
  changed, or its last-seen time is over an hour stale — not on every
  announcement (SD card wear).

### 7.3 Referrals (Tier 3)

The node sharing the most question words with its topics wins; generic
topic words (`guide`, `log`, `notes`, `overview`, `area`, …) don't count.

```
Try VALLEY-ORACLE (!a1b2c3d4) — covers geology, mining, local-history
```

The node ID is included so the user can DM the right node even if another
node uses the same display name.

---

## 8. "Seen Senders" First-Contact Tracking

A sender's first single-message answer gets a footer:

```
---
Del-Fi oracle · 12 pages · !help !topics
```

The footer is only added when the answer plus footer fits in one message; the
sender is marked as seen once they have received the footer (or the greeting
reply to "hi"/"hello"). Seen IDs are stored one per line in
`seen_senders.txt`, rewritten atomically on each new sender.

---

<!-- End of spec-router.md -->
