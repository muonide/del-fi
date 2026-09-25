# Del-Fi — Knowledge System Specification

<!-- Parent: .claude/claude.md §4, §6 -->
<!-- Related: spec-config.md §wiki_* keys, spec-router.md §7 Tier 1 -->

---

## 1. Three-Layer Architecture

Based on the Karpathy LLM Wiki pattern. The insight: retrieval quality improves
dramatically when the LLM compiles raw sources into a structured wiki **once**
rather than ingesting raw chunks at query time.

```
knowledge/          ← Human-owned raw sources (gitignored, deployment-specific)
    wildlife-guide.md
    weather-station.md
    trail-camera-log.md
         │
         │  --build-wiki (offline, pre-deployment, wiki_builder_model)
         ▼
wiki/               ← LLM-compiled wiki pages (gitignored, rebuilt per deployment)
    index.md
    log.md
    wildlife-guide.md
    weather-station.md
    trail-camera-log.md
         │
         │  query time (WikiEngine.query, serving model)
         ▼
context string assembled → Ollama generation → response
```

**Invariants:**
- `knowledge/` is read-only to the code — humans edit it, the code only reads.
- `wiki/` is write-only from `--build-wiki` — the daemon reads it; humans do not edit it.
- The schema (how wiki pages are structured) lives in this spec and `claude.md`.

---

## 2. WikiEngine Class Interface

`del_fi/core/knowledge.py`. Ollama is reached through the `ollama` Python
client; tests substitute a fake client object (see `tests/test_knowledge.py`).

```python
class WikiEngine:
    def __init__(self, cfg: dict) -> None: ...

    def build(self, file: str | None = None, model: str | None = None) -> int:
        """Compile knowledge/ → wiki/. Returns pages written.
        file: rebuild only that source. model: overrides wiki_builder_model
        (the watcher passes wiki_patch_model or the serving model).
        A full build first prunes pages whose source was deleted."""

    def prune_removed_sources(self) -> list[str]:
        """Remove pages (and index rows, embeddings) whose tracked source file
        is gone. Never prunes while knowledge/ is empty."""

    def query(self, q, peer_ctx="", history="", board_context="") -> tuple[str, bool]:
        """(answer, had_context). had_context=False → no page matched or the
        model declined (IDK). Raises LLMError when generation fails."""

    def lint(self) -> list[str]: ...
    def watch(self, interval: int, stop: threading.Event) -> None: ...
    def num_ctx(self) -> int: ...          # context window sent with answers
    def get_topics(self) -> list[str]: ...
    def suggest(self, query: str) -> str: ...

    available: bool        # Ollama reachable
    rag_available: bool    # ChromaDB usable
    wiki_available: bool   # wiki/index.md exists and is non-empty
    page_count: int

class LLMError(Exception):
    kind: str              # "unavailable" | "timeout" | "error"
```

---

## 3. Wiki Page Format

### 3.1 Frontmatter

```yaml
---
title: Wildlife Guide
tags: [wildlife, elk, mountain-lion, coyote, mule-deer, identification]
sources: [wildlife-guide.md]           # raw knowledge files that contributed
last_ingested: 2026-04-22              # date of last successful build from source
---
```

All frontmatter fields are required. `tags` and `sources` must be non-empty lists.

### 3.2 Body

- GFM markdown.
- Top-level heading `# <title>` required.
- Sub-sections with `##` headings for major topics.
- Internal cross-links as `[[wiki-page-name]]` (wiki link syntax, no `.md` extension).
- Factual claims should be dense and specific — the LLM's job is to distil
  source material, not to paraphrase it loosely.
- Data points: include numbers, dates, units, and sources where the raw document
  provides them.

### 3.3 Contradiction annotation

When new source material contradicts an existing claim:

```markdown
Elk calving season: mid-May to mid-June (below 8500 ft).
> [superseded 2026-04-15 by trail-camera-log.md — see camera 3 data]
Elk calving season: late May to late June (revised upward based on 3yr camera data).
```

The old claim is kept for audit trail, marked as superseded.

### 3.4 Staleness notation

For time-sensitive pages, the query pipeline injects a freshness header:

```
[weather-station — last ingested 6h ago]
```

If age > `wiki_stale_after_days` config value, the header reads:
```
[STALE: weather-station — last ingested 45 days ago, run --build-wiki]
```

---

## 4. wiki/index.md Format

```markdown
# Del-Fi Wiki Index

Last updated: 2026-04-22

## Index

| Page | Summary | Tags | Updated |
|------|---------|------|---------|
| [[wildlife-guide]] | Species ID — mountain lion, elk, mule deer, coyote | wildlife, species, id | 2026-04-22 |
| [[weather-station]] | Station readings, thresholds, historical norms (Davis VP2) | weather, temperature, wind, precip | 2026-04-22 |
| [[trail-camera-log]] | Camera trap summary — active cameras, notable captures | wildlife, cameras, evidence | 2026-04-22 |
```

The `Summary` column is the primary BM25 search target alongside `Tags`.
The builder LLM should write summaries as dense keyword phrases, not prose.

---

## 5. wiki/log.md Format

Append-only. One `##` entry per build or lint run.

```markdown
# Del-Fi Wiki Build Log

## [2026-04-22] build | wildlife-guide.md
Model: qwen2.5:7b. Pages touched: wildlife-guide (updated). 2 new cross-refs added.

## [2026-04-22] build | weather-station.md
Model: qwen2.5:7b. Pages touched: weather-station (created), trail-camera-log (updated tags).

## [2026-04-22] lint
Orphan pages: none. Stale pages: 0. Missing cross-refs: 2 (flora-guide→trail-camera-log,
flora-guide→wildlife-guide). Data gaps flagged: 0.
```

---

## 6. Build Pipeline Detail

### 6.1 Entry point: `python main.py --build-wiki`

The build command does **not** start the radio listener. It is a batch job.

### 6.2 Per-file processing

```
full build: prune pages whose tracked source was deleted (see §6.6)
for each .md/.txt file in knowledge/ (sorted, dotfiles skipped):
    md5(content) == wiki/.hash_cache.json[filename]  →  skip (unchanged)
    prompt = WIKI_BUILD_PROMPT(first 12,000 chars of the source)
    page = builder_model.generate(prompt, num_ctx=8192)
        retry with a larger num_predict while done_reason == "length"
    strip a ```markdown fence; force frontmatter sources: [<filename>]
        and last_ingested: <today>, whatever the model wrote
    write wiki/<slug>.md, update its index.md row, append to log.md
    record the hash (only now, so a failure anywhere above is retried)
```

- Hash cache keys are **filenames**, not absolute paths, so a wiki built on a
  desktop and copied to the node is not rebuilt there. v0.2 absolute-path
  keys are migrated on load.
- Slug = kebab-cased filename stem. `notes.txt` next to `notes.md` gets
  `notes-txt` so the two do not overwrite each other.
- Sources longer than 12,000 chars are indexed only up to that point (a
  warning is logged); answers still read the whole file (§7).
- The index row is replaced with a function, not a template string, so
  backslashes in LLM-written tags cannot break `re.sub`; `|` in cells
  becomes `/`.

### 6.3 Build prompt

The build prompt is constructed as:

```
SYSTEM:
You are a knowledge compiler for a field deployment named {node_name}.
You convert raw field documents into structured wiki pages.

Each wiki page you produce must be in this format:
---
title: <Page Title>
tags: [comma, separated, tags]
sources: [source-filename.md]
last_ingested: {today}
---

# <Page Title>

<body — dense facts, measurements, dates, cross-links as [[page-name]]>

Rules:
- Extract and condense — do not paraphrase loosely.
- Include numbers, dates, and units wherever the source provides them.
- Use [[wikilinks]] for internal cross-references.
- Contradictions with existing pages: keep old text, mark as superseded, add new claim.
- Write tag summaries as keyword phrases, not prose.
- Output ONLY wiki page blocks in the format above. No commentary.

EXISTING WIKI PAGES (for context and cross-reference):
{existing_wiki_index}

SOURCE DOCUMENT TO COMPILE:
Filename: {filename}
---
{source_content}
```

### 6.4 Atomic writes

Wiki pages, `index.md` and `.hash_cache.json` are written with
`fsutil.write_atomic()`: a temp file unique to the process and thread,
fsync, then rename. An interrupted build never leaves a partial file.

### 6.5 No network during build

The build uses Ollama (local). It does not call any external service.

### 6.6 Deleted sources

When a source file that the wiki was built from disappears from
`knowledge/`, the next full build (or watcher cycle) deletes its wiki page,
index row and embedding, and logs `prune | <file>` to `log.md` — unless the
page lists another source that still exists. If `knowledge/` is empty,
nothing is pruned: a node may legitimately run from a deployed `wiki/` alone.

---

## 7. Query Pipeline Detail

### 7.1 Full sequence

```
1. Find pages (first strategy that returns anything):
   a. BM25 over wiki/index.md rows (slug + summary + tags)
   b. ChromaDB similarity over page embeddings (≥ similarity_threshold)
   c. whole-word counts over wiki page bodies
   Keep the top 3 pages.

2. Split each page's source files (sources: frontmatter, resolved by
   basename inside knowledge/ only) into passages: one markdown section
   each, ≈700 chars max, with the section heading repeated on every cut.
   If a page's sources are not on this node, split the wiki page itself.

3. Score every passage against the question with BM25, weighted by page
   rank (1.0, 0.8, 0.65). Add passages best-first while they fit the
   budget. If no passage shares a word with the question (a semantic
   match), take the top page from its start instead.

4. Output passages grouped by page, in document order, each page headed
   [slug] — or [slug — last updated 3 hrs ago] for time_sensitive_files.

5. Generate with the serving model; return (answer, had_context).
```

### 7.2 Context budget and num_ctx

| Setting | Meaning |
|---------|---------|
| `max_context_tokens` | Retrieved-passage budget (default 1500 tokens ≈ 6000 chars; 1B/2B profiles 512) |
| `num_ctx` | Context window sent to Ollama. Unset → derived once from the budget: `(max_context_tokens + 1024 + num_predict) × 1.15`, rounded up to 512, min 2048 (3584 by default) |

The passage budget also shrinks by the size of history, board posts and peer
data, so the whole prompt fits the window. num_ctx is **fixed for the life
of the process**: a value that changes between requests makes Ollama reload
the model. Builds use their own fixed window (8192).

### 7.3 System prompts

Standard and small-model (`small_model_prompt: true`) variants both say:
answer ONLY from the excerpts; if they don't directly answer, share the
closest relevant information and say what topic they cover; never state
facts not in them. The small variant also caps answers at 1–3 sentences.

A reply is treated as a refusal (→ `had_context=False`, next tier) only if
it is ≤180 chars and its **first sentence** contains an "I don't know"
phrase without "but" — so "The trail is 3 mi. I'm not sure about ice." is
kept as an answer.

### 7.4 Context reordering

When `reorder_context: true` (1B/2B profiles), pages are output in reverse
rank order so the most relevant one sits next to the question. Small
models attend better to the end of the context. Reordering happens after
passage selection, so it cannot exceed the budget.

---

## 8. ChromaDB Integration

### 8.1 What changes from v0.1

| v0.1 (rag.py) | v0.2 (knowledge.py) |
|---------------|---------------------|
| Embeds raw document chunks (1024-char windows) | Embeds whole wiki pages |
| Many embeddings per source file | One embedding per wiki page |
| Updated on file change | Updated on `--build-wiki` |
| Collection: `del_fi_knowledge` | Collection: `del_fi_wiki` |
| Metadata: `{source, chunk_index, heading}` | Metadata: `{page, tags, last_ingested}` |

### 8.2 Collection parameters

```python
collection = chroma_client.get_or_create_collection(
    name="del_fi_wiki",
    metadata={"hnsw:space": "cosine"},
)
```

One document per wiki page. ID is the wiki page filename (without `.md`).

### 8.3 Embedding model

`nomic-embed-text` via Ollama. No change from v0.1.

### 8.4 ChromaDB failure mode

If ChromaDB is unavailable at startup, WikiEngine logs a warning and continues.
Vector search (`_vector_search`) returns empty list. BM25 search still works.
The daemon does not crash.

---

## 9. Staleness Model

| Config key | Default | Purpose |
|------------|---------|---------|
| `wiki_stale_after_days` | 30 | `--lint-wiki` stale page threshold |
| `time_sensitive_files` | `[]` in examples | Source filenames whose pages get an age header at query time |

For those pages the context header reads `[weather-station — last updated
3 hrs ago]`, computed from the newest source file's modification time (the
data's real age), or `ingested Nd ago` from `last_ingested` when the source
is not on this node.

---

## 10. Lint (`--lint-wiki`)

Runs as: `python main.py --lint-wiki`. Does not start the radio listener.

### Lint checks

| Check | Description |
|-------|-------------|
| Orphan page | A page file in `wiki/` with no row in `index.md` |
| Missing page | An `index.md` row with no page file |
| Stale page | `last_ingested` older than `wiki_stale_after_days` |
| Missing source | `sources:` lists a file not in `knowledge/` (checked only when `knowledge/` has files) |
| Missing cross-ref | A kebab-case `[[slug]]` in a page body with no matching page (inline mentions like `[[Apr 22]]` are ignored) |

Exit code: 0 = clean, 1 = issues found. CI/CD can gate on it.

### Lint output format

```
WARN  orphan-page: flora-guide (no incoming links)
WARN  missing-cross-ref: trail-camera-log → weather-station (see line 47: "overnight temperatures")
INFO  stale: 0 pages stale (threshold: 30d)
INFO  index: 5 pages, all consistent
LINT RESULT: 2 warnings, 0 errors
```

---

## 11. Migration from rag.py

### What changes

| Concern | v0.1 rag.py | v0.2 knowledge.py |
|---------|-------------|-------------------|
| Ingestion trigger | File change → immediate re-chunk and embed | File change → `build()` → wiki update → re-embed whole page |
| Retrieval unit | 1024-char chunk | Whole wiki page |
| LLM reads | Raw document fragments | Synthesised wiki page |
| Index | ChromaDB only | `wiki/index.md` (BM25) + ChromaDB (vector) |
| Contradiction handling | None (duplicate chunks) | superseded annotation |
| Staleness | None | `last_ingested` frontmatter + lint check |
| Build model | Same as serving model | `wiki_builder_model` (can be larger) |

### What stays the same

- ChromaDB with SQLite backend at `vectorstore/`.
- Embedding model: `nomic-embed-text` via Ollama.
- Ollama generation API endpoint.
- `similarity_threshold`, `rag_top_k` config keys (semantics unchanged; now apply to wiki pages).
- The `query()` interface that `Router` calls.

### Migration path (Phase 2)

1. `WikiEngine` is implemented alongside `RAGEngine` initially.
2. `Router` is updated to call `wiki_engine.query()` instead of `rag_engine.retrieve()`.
3. Old ChromaDB collection (`del_fi_knowledge`) is left on disk but not written to.
4. After a `--build-wiki` run, the new collection (`del_fi_wiki`) is populated.
5. `rag.py` is removed in a follow-up commit once tests pass.

---

## 13. Background Watcher (and the planned patch())

### 13.1 v0.3 behaviour

`watch(interval, stop)` runs a thread (disabled by `wiki_watch_enabled:
false`) that every `wiki_watch_interval_seconds`:

1. prunes pages whose source was deleted (§6.6), and
2. rebuilds the page of every changed or new source with
   **`wiki_patch_model`, else the serving model** — never
   `wiki_builder_model`, which may be far too large for the node (the
   example config suggests a 12B builder for a Pi-class server).

A rebuild is a full single-page compile with the build prompt.

### 13.2 Planned: constrained patch()

Not implemented. The idea: give the serving model the existing page plus
the changed content and ask it to add/update only the changed facts,
preserving the structure the builder model produced, with a fallback to
`build(file)`. Until then, pages rebuilt by the watcher are written by the
serving model, so re-run `--build-wiki` with the big model after large
changes.

### 13.3 Peer knowledge — trust boundary

Peer-sourced answers (from `PeerCache`, Tier 2) do **not** flow into the wiki
automatically. Reasons:
- Trust: a peer node may hallucinate; its answer should never become a local
  wiki fact presented as ground truth.
- Scope: the wiki represents **this node's knowledge**. Peer knowledge is
  explicitly labelled `[via PEER-NODE]` at query time.

If a deployment needs to incorporate verified content from a trusted peer node,
the operator must manually copy it into `knowledge/` and run `--build-wiki`.
There is no automated wiki injection from peer sources.

---

## 12. `wiki/` Directory Conventions

- Page filenames: the source filename's kebab-cased stem + `.md`.
- Reserved filenames: `index.md`, `log.md` (generated automatically — do not create manually).
- `.hash_cache.json`: `{source filename: md5}` for change detection.
- Temp files: `<name>.<pid>.<thread>.tmp` exist only mid-write.
- Only one process should build at a time: don't run `--build-wiki` while
  the daemon's watcher is rebuilding.

---

<!-- End of spec-knowledge.md -->
