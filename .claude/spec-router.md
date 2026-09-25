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

## 5. Query Worker

### 5.1 Architecture

A single background `threading.Thread` reads from `msg_queue: queue.Queue`.
Using a single worker provides:
- Natural FIFO ordering per sender
- No concurrent LLM calls (which would exceed memory budget on small hardware)
- Simple backpressure: queue.Full drops the oldest item with a log warning

```python
msg_queue = queue.Queue(maxsize=20)  # configurable: query_queue_size
```

If queue is full when a new query arrives: discard the oldest item and enqueue
the new one. Log: `"queue full — dropped oldest query from %s"`

### 5.2 Worker loop

```python
def _query_worker(self) -> None:
    while not self._shutdown.is_set():
        try:
            item = self._msg_queue.get(timeout=1.0)
        except queue.Empty:
            continue
        
        sender, query = item
        try:
            response = self._run_tier_hierarchy(sender, query)
            response = self._formatter.format(response)
            chunks = self._formatter.chunk(response)
            self._store_more_buffer(sender, query, chunks)
            for i, chunk in enumerate(chunks[:self._auto_send_chunks]):
                self._adapter.send_dm(sender, chunk)
                if i < len(chunks) - 1:
                    time.sleep(self._chunk_delay_seconds)
        except Exception:
            log.exception("Query worker error for sender %s", sender)
            self._adapter.send_dm(sender, self._config.get("error_message", "Error."))
        finally:
            self._msg_queue.task_done()
```

### 5.3 Shutdown

`self._shutdown` is a `threading.Event`. Set on SIGINT/SIGTERM. The worker exits
cleanly after completing the current in-flight item.

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

### 7.1 Announcement format

```
DEL-FI:{version}:ANNOUNCE:{NODE_NAME}:topics={t1},{t2}:model={model}:uptime={Xd}:docs={N}
```

- `version`: protocol integer (currently `1`)
- `NODE_NAME`: `ALL-CAPS-HYPHENATED` node name
- `topics`: comma-separated list of wiki page titles (or knowledge folder names)
- `uptime`: human-readable days
- `docs`: integer count of knowledge files

Announcement is broadcast (not DM) at `gossip_interval_seconds` (default: 14400 = 4h).
Announcements are short: must fit in 230 bytes.

### 7.2 Gossip directory TTL

Received announcements expire after `gossip_ttl_seconds` (default: 86400 = 24h).
Expired entries are pruned on each receive and on each `!peers` query.

### 7.3 Topic matching for referrals

```python
def referral(self, query: str) -> str | None:
    """
    Find a peer node whose topics overlap with query keywords.
    Returns referral string or None.
    """
    query_words = set(query.lower().split()) - STOP_WORDS
    best_node = None
    best_score = 0
    for node, entry in self._directory.items():
        topic_words = set(" ".join(entry["topics"]).lower().split())
        score = len(query_words & topic_words)
        if score > best_score:
            best_score = score
            best_node = node
    if best_node and best_score > 0:
        topics = ", ".join(self._directory[best_node]["topics"][:3])
        return f"Try {best_node} — covers {topics}"
    return None
```

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
