"""WikiEngine: LLM-compiled knowledge base for Del-Fi.

Three layers:
  knowledge/   — raw source documents (human-owned, gitignored)
  wiki/        — LLM-compiled pages (rebuilt via --build-wiki, gitignored)
  .claude/     — wiki schema / spec (always tracked in git)

Build pipeline (--build-wiki):
  1. Scan knowledge/ for .md and .txt files; drop pages whose source is gone
  2. Skip unchanged files (MD5 hash check, keyed by filename)
  3. For each changed file: prompt the LLM to extract entities and write
     a structured wiki page with YAML frontmatter (a search index)
  4. Write wiki/<slug>.md, update wiki/index.md, append to wiki/log.md

Query pipeline (Tier 1):
  1. Find pages: BM25 on wiki/index.md, then vector search, then page bodies
  2. Split the top pages' source files into passages (one section each)
  3. Rank passages against the question and fill the context budget
  4. Ask the serving LLM to answer from those passages only
"""

import hashlib
import json
import logging
import math
import re
import threading
import time
from collections import Counter
from datetime import date
from pathlib import Path

from del_fi.core.fsutil import write_atomic
from del_fi.core.text import tokenize

log = logging.getLogger("del_fi.core.knowledge")

# Context sizing. Token counts are estimated at CHARS_PER_TOKEN chars each.
CHARS_PER_TOKEN = 4
DEFAULT_CONTEXT_TOKENS = 1500      # retrieved passages, when max_context_tokens is unset
PROMPT_OVERHEAD_TOKENS = 1024      # system prompt, history, board posts, question
MIN_CONTEXT_CHARS = 600
MAX_CONTEXT_PAGES = 3
PASSAGE_CHARS = 700
_RANK_WEIGHTS = (1.0, 0.8, 0.65)   # passage score multiplier by page rank

# Build sizing: only this much of a source goes into the build prompt, with a
# fixed context window so Ollama never truncates the prompt (or reloads the
# model because num_ctx changed between files).
BUILD_SOURCE_CHARS = 12000
BUILD_NUM_CTX = 8192

# Compact system prompt for small models
SMALL_MODEL_SYSTEM = (
    "You are {name}, a community assistant. {personality} "
    "You are given excerpts from local documents. "
    "Answer using ONLY the excerpts below. "
    "If they do not directly answer the question, share the most relevant "
    "information from them and note what they cover. Never state facts not in them. "
    "Be brief. 1-3 sentences maximum."
)

STANDARD_SYSTEM = (
    "You are {name}, a community assistant. {personality} "
    "You are given excerpts from local documents. "
    "Answer using ONLY the provided excerpts. "
    "If they do not directly answer the question, share the closest relevant "
    "information they do contain and briefly note what topic they cover. "
    "Never state facts not in the excerpts. "
    "Be concise and factual. Cite the source document name when relevant."
)

# Short phrases that indicate the LLM refused to answer from context.
# Used to detect useless responses and fall through to the next tier.
_IDK_PATTERNS = (
    "i don't know",
    "i do not know",
    "i'm not sure",
    "i am not sure",
    "don't have information",
    "do not have information",
    "not mentioned in the context",
    "not in the context",
    "not provided in the context",
    "context does not contain",
    "context doesn't contain",
    "context doesn't mention",
    "context does not mention",
    "cannot answer",
    "can't answer",
    "no information available",
)

_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---[ \t]*\n?", re.DOTALL)
_HEADING = re.compile(r"^#{1,6}\s+\S")
_FIRST_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


class LLMError(Exception):
    """Answer generation failed.

    kind is "unavailable" (Ollama unreachable), "timeout" (model too slow)
    or "error" (anything else, e.g. model not pulled). The router turns
    these into an honest reply instead of claiming it has no docs.
    """

    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind


def _classify_llm_error(exc: Exception) -> str:
    # ollama raises builtin ConnectionError when it cannot connect and
    # httpx.*Timeout on slow responses; match by name to avoid importing httpx.
    if isinstance(exc, TimeoutError) or "Timeout" in type(exc).__name__:
        return "timeout"
    if isinstance(exc, ConnectionError) or "Connect" in type(exc).__name__:
        return "unavailable"
    return "error"


# Build prompt: given raw source content, produce a structured wiki page
WIKI_BUILD_PROMPT = """\
You are a knowledge compiler. Your job is to turn a raw source document into
a SEARCH INDEX page. This wiki page will be used to FIND the source document
at query time — the raw source is always available for full detail. Therefore:

- Maximise keyword and entity coverage. Every name, place, date, organisation,
  measurement, and topic must appear somewhere in the page — even if only
  in a heading or tag — so keyword search can find it.
- Section headings should name the topic precisely (e.g. "Spring Clean-Up Day"
  not "Events").
- Use [[page-slug]] cross-references freely. They are how the system navigates
  between topics at query time.
- Summaries can be brief (1-2 sentences). Full detail lives in the source file.

Format EXACTLY as shown:

---
title: <page title>
tags: [tag1, tag2, tag3, ...]
sources: [{filename}]
last_ingested: {today}
---

# <page title>

## <Precise Section Topic>

<1-2 sentences. Key facts, dates, measurements. [[cross-ref]] any related pages.>

## <Another Precise Section Topic>

...

Rules:
- Be factual. No filler.
- If the source contradicts an existing claim, annotate the old text:
  > [superseded {today} by {filename}]
- Keep the total page under 500 words.
- tags: must include every major searchable keyword from the document.
- Do NOT include meta-commentary, only the wiki content.

Source document ({filename}):
---
{content}
---

Respond with ONLY the wiki page (YAML frontmatter + body). No preamble."""


class WikiEngine:
    """LLM-compiled wiki knowledge base.

    Public interface
    ----------------
    build(file=None, model=None)      compile knowledge/ → wiki/
    prune_removed_sources()           drop pages whose source was deleted
    query(q, peer_ctx, history, ...)  retrieve passages + LLM → (answer, had_context)
    lint()                            health check → list of issue strings
    watch(interval, stop)             background knowledge watcher
    available                         True when Ollama is reachable
    wiki_available                    True when wiki/ has pages
    page_count                        number of wiki pages
    get_topics()                      list of page titles from index
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._wiki_dir = Path(cfg["wiki_folder"])
        self._knowledge_dir = Path(cfg.get("knowledge_folder", "./knowledge"))
        self._ollama = None
        self._ollama_build = None
        self._ollama_available = False
        self._collection = None
        self._rag_available = False
        self._lock = threading.Lock()
        self._file_hashes: dict[str, str] = {}   # source filename -> md5
        self._hash_cache_file = self._wiki_dir / ".hash_cache.json"

        self._init_ollama()
        self._init_vectorstore()
        self._load_hash_cache()

    # --- Initialization ---

    def _init_ollama(self):
        try:
            from ollama import Client
            self._ollama = Client(
                host=self.cfg["ollama_host"],
                timeout=self.cfg["ollama_timeout"],
            )
            self._ollama.list()
            self._ollama_available = True
            log.info(f"ollama connected at {self.cfg['ollama_host']}")
            # Separate client with a longer timeout for --build-wiki.
            # Large builder models (e.g. 26B) can take several minutes per page.
            build_timeout = self.cfg.get("wiki_build_timeout", 600)
            self._ollama_build = Client(
                host=self.cfg["ollama_host"],
                timeout=build_timeout,
            )
        except Exception as e:
            log.warning(f"ollama not available (will retry): {e}")
            self._ollama_available = False
            self._ollama_build = None

    def _init_vectorstore(self):
        """Initialize ChromaDB for wiki-page semantic search. Optional."""
        try:
            import chromadb
            from chromadb.config import Settings
            db_path = self.cfg["_vectorstore_dir"]
            Path(db_path).mkdir(parents=True, exist_ok=True)
            client = chromadb.PersistentClient(
                path=db_path,
                settings=Settings(anonymized_telemetry=False),
            )
            self._collection = client.get_or_create_collection(
                name="del_fi_wiki",
                metadata={"hnsw:space": "cosine"},
            )
            self._rag_available = True
            log.info(f"vectorstore ready ({self._collection.count()} wiki pages indexed)")
        except Exception as e:
            log.warning(f"chromadb init failed — semantic search disabled: {e}")
            self._rag_available = False

    def check_ollama(self) -> bool:
        """Re-check Ollama availability. Called by health-check thread."""
        if self._ollama_available:
            return True
        self._init_ollama()
        return self._ollama_available

    # --- Properties ---

    @property
    def available(self) -> bool:
        return self._ollama_available

    @property
    def rag_available(self) -> bool:
        return self._rag_available

    @property
    def wiki_available(self) -> bool:
        index = self._wiki_dir / "index.md"
        return index.exists() and index.stat().st_size > 0

    @property
    def page_count(self) -> int:
        if not self._wiki_dir.exists():
            return 0
        return sum(
            1 for f in self._wiki_dir.glob("*.md")
            if f.name not in ("index.md", "log.md")
        )

    # --- Build pipeline ---

    def build(self, file: str | None = None, model: str | None = None) -> int:
        """Compile knowledge/ → wiki/.

        If *file* is given, only (re)process that file. A full build also
        removes pages whose source file has been deleted. *model* overrides
        wiki_builder_model (the watcher passes the serving model).
        Returns the number of wiki pages written.
        """
        self._wiki_dir.mkdir(parents=True, exist_ok=True)

        if not self._ollama_available:
            log.error("ollama not available — cannot build wiki")
            return 0

        builder_model = model or self.cfg.get("wiki_builder_model") or self.cfg["model"]
        log.info(f"building wiki with model {builder_model!r}")

        if file:
            targets = [Path(file)]
        else:
            self.prune_removed_sources()
            targets = self._source_files()

        written: list[str] = []
        for path in targets:
            try:
                slug = self._build_page(path, builder_model)
            except Exception:
                log.exception(f"build failed for {path.name}")
                continue
            if slug:
                written.append(slug)

        self._save_hash_cache()

        if written:
            log.info(f"wiki build complete: {len(written)} page(s) written")
            self._embed_wiki_pages(written)

        return len(written)

    # Build token budget: enough for a complete wiki page (≤600 words ≈ 800 tokens)
    # plus frontmatter. We use 1600 to give headroom; the larger builder model
    # handles this without issue. A second pass is attempted if truncation is detected.
    _BUILD_NUM_PREDICT = 1600
    _BUILD_NUM_PREDICT_RETRY = 2400  # wider budget for retry pass

    def _is_truncated(self, text: str) -> bool:
        """Heuristic: True when the LLM output appears to have been cut
        mid-generation. Only used when Ollama does not report done_reason."""
        if not text:
            return True
        stripped = text.rstrip()
        # Sentence ends with a terminal punctuation mark, a closing code fence,
        # or a markdown list item end. Mid-word / mid-sentence cuts do not.
        terminal = (".", "!", "?", "```", ">", "*", "-")
        for t in terminal:
            if stripped.endswith(t):
                return False
        # Also accept lines that end with a closing bracket/paren (cross-refs)
        if stripped.endswith((")", "]", "]]")):
            return False
        return True

    def _generate_wiki_page(
        self, filename: str, prompt: str, model: str, max_retries: int = 2
    ) -> str | None:
        """Generate a wiki page with truncation detection and retry.

        Returns the completed wiki text, or None on unrecoverable failure.
        """
        budgets = [self._BUILD_NUM_PREDICT] + [self._BUILD_NUM_PREDICT_RETRY] * max_retries

        client = self._ollama_build or self._ollama
        text = ""
        for attempt, budget in enumerate(budgets):
            try:
                response = client.generate(
                    model=model,
                    prompt=prompt,
                    options={
                        "num_predict": budget,
                        "num_ctx": BUILD_NUM_CTX,
                        "temperature": 0.1,
                    },
                )
                text = _strip_code_fence(response.response.strip())
                done_reason = getattr(response, "done_reason", None)
            except Exception as e:
                log.error(f"LLM build failed for {filename} (attempt {attempt + 1}): {e}")
                return None

            # Ollama reports done_reason="length" when it hit num_predict;
            # fall back to a punctuation heuristic for clients that don't.
            truncated = done_reason == "length" if done_reason else self._is_truncated(text)
            if not truncated:
                if attempt > 0:
                    log.info(f"  {filename}: truncation resolved on attempt {attempt + 1}")
                return text

            log.warning(
                f"  {filename}: output appears truncated at {len(text)} chars "
                f"(attempt {attempt + 1}/{len(budgets)}) — retrying with budget {budget} → "
                f"{budgets[attempt + 1] if attempt + 1 < len(budgets) else 'N/A'}"
            )

        # Final attempt exhausted — log and return what we have rather than failing
        log.warning(
            f"  {filename}: could not resolve truncation after {max_retries + 1} attempts; "
            f"saving best result ({len(text)} chars)"
        )
        return text

    def _build_page(self, source_path: Path, model: str) -> str | None:
        """Build one wiki page from a source file. Returns its slug if written."""
        try:
            content = source_path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            log.warning(f"cannot read {source_path.name}: {e}")
            return None

        content_hash = hashlib.md5(content.encode()).hexdigest()
        key = source_path.name
        with self._lock:
            if self._file_hashes.get(key) == content_hash:
                return None  # unchanged

        if len(content) > BUILD_SOURCE_CHARS:
            log.warning(
                f"  {source_path.name}: {len(content)} chars — only the first "
                f"{BUILD_SOURCE_CHARS} are indexed; split the file so keyword "
                f"search can find the rest"
            )

        today = date.today().isoformat()
        prompt = WIKI_BUILD_PROMPT.format(
            filename=source_path.name,
            today=today,
            content=content[:BUILD_SOURCE_CHARS],
        )

        log.info(f"  compiling {source_path.name} → wiki...")
        wiki_text = self._generate_wiki_page(source_path.name, prompt, model)
        if wiki_text is None:
            return None  # hash not recorded, so it is retried next time
        wiki_text = _normalise_frontmatter(wiki_text, source_path, today)

        slug = self._slug_for(source_path)
        wiki_path = self._wiki_dir / f"{slug}.md"
        if not write_atomic(str(wiki_path), wiki_text):
            return None

        self._update_index(slug, wiki_path, today)
        self._append_log(f"[{today}] ingest | {source_path.name}\nWrote: {slug}.md")
        with self._lock:
            self._file_hashes[key] = content_hash
        log.info(f"  wrote wiki/{slug}.md")
        return slug

    def _slug_for(self, source: Path) -> str:
        slug = re.sub(r"[^\w-]", "-", source.stem.lower()).strip("-") or "page"
        if source.suffix.lower() == ".txt" and (self._knowledge_dir / f"{source.stem}.md").exists():
            slug += "-txt"  # foo.md and foo.txt would otherwise share wiki/foo.md
        return slug

    def _source_files(self) -> list[Path]:
        if not self._knowledge_dir.is_dir():
            return []
        return sorted(
            p for ext in ("*.md", "*.txt") for p in self._knowledge_dir.glob(ext)
            if p.is_file() and not p.name.startswith(".")
        )

    def _update_index(self, slug: str, wiki_path: Path, today: str):
        """Insert or replace the wiki/index.md table row for this page."""
        index_path = self._wiki_dir / "index.md"

        text = wiki_path.read_text(encoding="utf-8", errors="replace")
        tags = ""
        summary = ""
        fm_match = _FRONTMATTER.match(text)
        if fm_match:
            tg = re.search(r"^tags:\s*\[(.+)\]", fm_match.group(1), re.MULTILINE)
            if tg:
                tags = tg.group(1).strip()

        # First sentence of the body is the summary
        body = text[fm_match.end():] if fm_match else text
        s = re.search(r"[A-Z][^.!?\n]{10,}[.!?]", body)
        if s:
            summary = s.group(0)[:100]

        row = f"| [[{slug}]] | {_table_cell(summary)} | {_table_cell(tags)} | {today} |"

        if not index_path.exists():
            write_atomic(
                str(index_path),
                "# Wiki Index\n\n"
                "| Page | Summary | Tags | Updated |\n"
                "|------|---------|------|--------|\n"
                f"{row}\n",
            )
            return

        content = index_path.read_text(encoding="utf-8")
        pattern = re.compile(rf"^\| \[\[{re.escape(slug)}\]\].*$", re.MULTILINE)
        if pattern.search(content):
            # A function, not a string: LLM text may contain backslashes
            # that re.sub would treat as group references.
            content = pattern.sub(lambda _m: row, content)
        else:
            content = content.rstrip() + f"\n{row}\n"
        write_atomic(str(index_path), content)

    def _remove_index_row(self, slug: str):
        index_path = self._wiki_dir / "index.md"
        if not index_path.exists():
            return
        content = index_path.read_text(encoding="utf-8")
        updated = re.sub(
            rf"^\|\s*\[\[{re.escape(slug)}\]\].*\n?", "", content, flags=re.MULTILINE
        )
        if updated != content:
            write_atomic(str(index_path), updated)

    def _append_log(self, entry: str):
        """Append an entry to wiki/log.md."""
        self._wiki_dir.mkdir(parents=True, exist_ok=True)
        with open(self._wiki_dir / "log.md", "a", encoding="utf-8") as f:
            f.write(f"\n## {entry}\n")

    def prune_removed_sources(self) -> list[str]:
        """Remove wiki pages whose source file was deleted from knowledge/.

        Only sources this wiki was built from (tracked in the hash cache) are
        considered, and nothing is pruned while knowledge/ is empty, so a
        wiki deployed without its raw sources is never wiped. Returns the
        slugs removed.
        """
        present = {p.name for p in self._source_files()}
        if not present:
            return []
        with self._lock:
            gone = sorted(set(self._file_hashes) - present)
        if not gone:
            return []

        today = date.today().isoformat()
        removed: list[str] = []
        for name in gone:
            slug = self._slug_for(Path(name))
            page = self._wiki_dir / f"{slug}.md"
            if page.exists():
                sources = self._extract_sources(
                    page.read_text(encoding="utf-8", errors="replace")
                )
                if not any((self._knowledge_dir / Path(s).name).is_file() for s in sources):
                    page.unlink()
                    self._remove_index_row(slug)
                    self._delete_embedding(slug)
                    removed.append(slug)
                    self._append_log(f"[{today}] prune | {name}\nRemoved: {slug}.md")
                    log.info(f"source {name} deleted — removed wiki/{slug}.md")
            with self._lock:
                self._file_hashes.pop(name, None)

        self._save_hash_cache()
        return removed

    # --- Query pipeline ---

    def query(
        self,
        q: str,
        peer_ctx: str = "",
        history: str = "",
        board_context: str = "",
    ) -> tuple[str, bool]:
        """Answer a query from the compiled wiki.

        Returns (answer, had_context). If had_context is False the caller
        should not cache the result and should consider falling through to
        Tier 2. Raises LLMError if the model could not be reached or failed.
        """
        if not self._ollama_available:
            return "", False

        page_slugs = self._find_pages(q)
        if not page_slugs:
            return "", False

        budget = self._context_budget_chars(history, board_context, peer_ctx)
        context = self._build_context(q, page_slugs[:MAX_CONTEXT_PAGES], budget)
        if not context:
            return "", False

        answer = self._generate(q, context, peer_ctx=peer_ctx, history=history,
                                board_context=board_context)
        # _generate returns "" when the model declines (IDK) — treat as no
        # match so the router falls through to the next tier rather than
        # caching a dead response. Generation failures raise LLMError.
        if not answer:
            return "", False
        return answer, True

    def _find_pages(self, q: str) -> list[str]:
        """Rank wiki pages for a question: index keywords, then semantic
        similarity, then words in the page bodies."""
        slugs = self._bm25_search(q)
        if not slugs and self._rag_available:
            slugs = self._vector_search(q)
        if not slugs:
            slugs = self._content_search(q)
        return slugs

    def _context_tokens(self) -> int:
        return int(self.cfg.get("max_context_tokens") or DEFAULT_CONTEXT_TOKENS)

    def _num_predict(self) -> int:
        return int(self.cfg.get("num_predict") or 300)

    def num_ctx(self) -> int:
        """Context window sent to Ollama for answers.

        num_ctx from config if set; otherwise derived from the context budget
        and fixed for the life of the process (a num_ctx that changes between
        requests makes Ollama reload the model).
        """
        configured = self.cfg.get("num_ctx")
        if configured:
            return int(configured)
        need = (self._context_tokens() + PROMPT_OVERHEAD_TOKENS + self._num_predict()) * 1.15
        return max(2048, math.ceil(need / 512) * 512)

    def _context_budget_chars(self, *extras: str) -> int:
        """Chars of retrieved passages that fit alongside the other prompt parts."""
        wanted = self._context_tokens() * CHARS_PER_TOKEN
        window = (self.num_ctx() - self._num_predict() - 256) * CHARS_PER_TOKEN
        available = window - sum(len(x) for x in extras if x)
        return max(MIN_CONTEXT_CHARS, min(wanted, available))

    def _build_context(self, q: str, slugs: list[str], budget: int) -> str:
        """Assemble the most relevant passages from the pages' sources.

        Each page's source files (or the page itself, if its sources are not
        on this node) are split into section passages, ranked against the
        question with BM25, and added best-first until *budget* chars are
        used. Output keeps document order within each page.
        """
        ts_files = self.cfg.get("time_sensitive_files") or []
        pages: list[tuple[int, str]] = []                 # (rank, header)
        flat: list[tuple[int, str, int, str]] = []        # (rank, source, order, text)

        for rank, slug in enumerate(slugs):
            page_path = self._wiki_dir / f"{slug}.md"
            if not page_path.exists():
                continue
            wiki_text = page_path.read_text(encoding="utf-8", errors="replace")

            passages: list[tuple[str, int, str]] = []
            src_paths: list[Path] = []
            for src in self._extract_sources(wiki_text):
                # Basename only: frontmatter is LLM-written, never a path.
                src_path = self._knowledge_dir / Path(src).name
                if not src_path.is_file():
                    continue
                try:
                    raw = src_path.read_text(encoding="utf-8", errors="replace")
                except OSError as exc:
                    log.warning(f"could not read source {src}: {exc}")
                    continue
                src_paths.append(src_path)
                passages += [(src_path.name, i, p) for i, p in enumerate(split_passages(raw))]

            if not passages:  # sources not on this node: use the wiki page itself
                passages = [(page_path.name, i, p) for i, p in enumerate(split_passages(wiki_text))]
            if not passages:
                continue

            header = f"[{slug}"
            if any(Path(f).stem and Path(f).stem in slug for f in ts_files):
                note = self._staleness_note(page_path, src_paths)
                if note:
                    header += f" — {note}"
            pages.append((rank, header + "]"))
            flat += [(rank, src, i, text) for src, i, text in passages]

        if not pages:
            return ""

        q_terms = tokenize(q)
        corpus = [(str(i), tokenize(text)) for i, (_, _, _, text) in enumerate(flat)]
        scores = _bm25_scores(q_terms, corpus) if q_terms else [0.0] * len(flat)
        weights = [
            s * _RANK_WEIGHTS[min(rank, len(_RANK_WEIGHTS) - 1)]
            for s, (rank, _, _, _) in zip(scores, flat)
        ]

        ranked = sorted((i for i, w in enumerate(weights) if w > 0), key=lambda i: -weights[i])
        in_doc_order = not ranked
        if in_doc_order:
            # Found by semantic or fuzzy match with no shared keyword: read the
            # top page from the start.
            ranked = [i for i, (rank, _, _, _) in enumerate(flat) if rank == pages[0][0]]

        chosen: list[int] = []
        used = 0
        for i in ranked:
            size = len(flat[i][3]) + 2
            if used + size > budget:
                if in_doc_order:
                    break
                continue
            chosen.append(i)
            used += size
        if not chosen:  # best passage alone exceeds the budget: truncate it
            i = ranked[0]
            flat[i] = (*flat[i][:3], flat[i][3][:budget])
            chosen = [i]

        by_rank: dict[int, list[int]] = {}
        for i in chosen:
            by_rank.setdefault(flat[i][0], []).append(i)
        order = [(rank, header) for rank, header in pages if rank in by_rank]
        if self.cfg.get("reorder_context"):
            order.reverse()  # small models: most relevant page next to the question

        parts = []
        for rank, header in order:
            idxs = sorted(by_rank[rank], key=lambda i: (flat[i][1], flat[i][2]))
            parts.append(header + "\n" + "\n\n".join(flat[i][3] for i in idxs))
        return "\n\n---\n\n".join(parts)

    def _generate(
        self,
        query: str,
        context: str,
        peer_ctx: str = "",
        history: str = "",
        board_context: str = "",
    ) -> str:
        """Call Ollama to generate an answer from context."""
        name = self.cfg["node_name"]
        personality = self.cfg.get("personality", "")

        if self.cfg.get("small_model_prompt"):
            system = SMALL_MODEL_SYSTEM.format(name=name, personality=personality)
        else:
            system = STANDARD_SYSTEM.format(name=name, personality=personality)

        parts = [f"Context:\n{context}"]
        if peer_ctx:
            parts.append(f"Peer data:\n{peer_ctx}")
        if board_context:
            parts.append(board_context)
        if history:
            parts.append(history)
        parts.append(f"Question: {query}")

        prompt = "\n\n".join(parts)

        options = {"num_predict": self._num_predict(), "num_ctx": self.num_ctx()}

        try:
            response = self._ollama.generate(
                model=self.cfg["model"],
                system=system,
                prompt=prompt,
                options=options,
            )
            text = response.response.strip()
        except Exception as e:
            kind = _classify_llm_error(e)
            if kind == "unavailable":
                # Let the health-check loop take over until Ollama is back.
                self._ollama_available = False
            log.error(f"LLM generation failed ({kind}): {e}")
            raise LLMError(kind, str(e)) from e

        # If the LLM refused to answer from context, return "" so the caller
        # falls through rather than caching a useless response.
        if self._is_idk_response(text):
            log.info("LLM returned IDK response — falling through")
            return ""
        return text

    def _is_idk_response(self, text: str) -> bool:
        """True when the response is a bare refusal ("I don't know.").

        Only the first sentence is checked, so an answer that merely hedges
        later ("The trail is 3 mi. I'm not sure about ice.") is kept.
        """
        if not text:
            return True
        if len(text) > 180:
            return False  # long responses are probably useful
        first = _FIRST_SENTENCE_END.split(text.strip(), maxsplit=1)[0].lower()
        if " but " in first:
            return False  # "I'm not sure, but the log shows…" is an answer
        return any(p in first for p in _IDK_PATTERNS)

    def suggest(self, query: str) -> str:
        """Return a soft suggestion when no wiki page matches well."""
        topics = self.get_topics()
        if not topics:
            return ""
        name = self.cfg["node_name"]
        topic_str = ", ".join(topics[:8])
        return (
            f"{name}: I don't have specific info on that. "
            f"I know about: {topic_str}. Try !topics for full list."
        )

    # --- Keyword search ---

    def _bm25_search(self, query: str) -> list[str]:
        """BM25 keyword search on wiki/index.md. Returns ranked slug list."""
        index_path = self._wiki_dir / "index.md"
        if not index_path.exists():
            return []

        content = index_path.read_text(encoding="utf-8", errors="replace")
        query_terms = tokenize(query)
        if not query_terms:
            return []

        # Parse index rows: | [[slug]] | summary | tags | date |
        rows = re.findall(
            r"^\|\s*\[\[([^\]]+)\]\]\s*\|([^|]*)\|([^|]*)\|[^|]*\|",
            content,
            re.MULTILINE,
        )
        if not rows:
            return []

        # Build corpus: one document per row (slug + summary + tags)
        corpus = [
            (slug.strip(), tokenize(f"{slug} {summary} {tags}"))
            for slug, summary, tags in rows
        ]
        scores = _bm25_scores(query_terms, corpus)
        ranked = sorted(zip(scores, [slug for slug, _ in corpus]), reverse=True)
        return [slug for score, slug in ranked if score > 0.0]

    def _extract_sources(self, wiki_text: str) -> list[str]:
        """Parse the sources: [...] list from a wiki page's YAML frontmatter."""
        fm_match = _FRONTMATTER.match(wiki_text)
        if not fm_match:
            return []
        src_m = re.search(r"^sources:\s*\[(.+)\]", fm_match.group(1), re.MULTILINE)
        if not src_m:
            return []
        return [
            s.strip().strip("\"'")
            for s in src_m.group(1).split(",")
            if s.strip()
        ]

    def _content_search(self, query: str) -> list[str]:
        """Last-resort fallback: rank wiki page bodies by how often the
        question's words occur in them (whole words, not substrings)."""
        query_terms = set(tokenize(query))
        if not query_terms:
            return []

        scored: list[tuple[int, str]] = []
        for page_path in self._wiki_dir.glob("*.md"):
            if page_path.name in ("index.md", "log.md"):
                continue
            try:
                counts = Counter(tokenize(page_path.read_text(encoding="utf-8", errors="replace")))
            except OSError:
                continue
            score = sum(counts[t] for t in query_terms)
            if score > 0:
                scored.append((score, page_path.stem))

        scored.sort(reverse=True)
        return [slug for _, slug in scored]

    # --- Vector search ---

    def _vector_search(self, query: str) -> list[str]:
        """Semantic search on ChromaDB wiki-page embeddings."""
        if not self._rag_available or not self._ollama_available:
            return []

        try:
            embedding = self._embed_text(query)
            if not embedding:
                return []

            top_k = self.cfg.get("rag_top_k", 4)
            results = self._collection.query(
                query_embeddings=[embedding],
                n_results=min(top_k, max(1, self._collection.count())),
                include=["metadatas", "distances"],
            )

            threshold = self.cfg.get("similarity_threshold", 0.28)
            slugs = []
            for meta, dist in zip(
                results["metadatas"][0], results["distances"][0]
            ):
                similarity = 1.0 - dist
                if similarity >= threshold:
                    slugs.append(meta["slug"])

            return slugs

        except Exception as e:
            log.warning(f"vector search failed: {e}")
            return []

    def _embed_text(self, text: str) -> list[float] | None:
        try:
            resp = self._ollama.embeddings(
                model=self.cfg["embedding_model"],
                prompt=text,
            )
            return resp.embedding
        except Exception as e:
            log.warning(f"embedding failed: {e}")
            return None

    def _embed_wiki_pages(self, slugs: list[str] | None = None):
        """Embed wiki pages into ChromaDB (all pages, or just *slugs*)."""
        if not self._rag_available or not self._ollama_available:
            return

        if slugs is None:
            pages = [
                f for f in self._wiki_dir.glob("*.md")
                if f.name not in ("index.md", "log.md")
            ]
        else:
            pages = [self._wiki_dir / f"{s}.md" for s in slugs]
        pages = [p for p in pages if p.exists()]
        if not pages:
            return

        log.info(f"embedding {len(pages)} wiki page(s)...")
        for page_path in pages:
            try:
                text = page_path.read_text(encoding="utf-8", errors="replace")
                slug = page_path.stem
                emb = self._embed_text(text[:4000])  # cap embedding input
                if not emb:
                    continue
                self._collection.upsert(
                    ids=[slug],
                    embeddings=[emb],
                    documents=[text[:2000]],
                    metadatas=[{"slug": slug, "file": page_path.name}],
                )
            except Exception as e:
                log.warning(f"embedding failed for {page_path.name}: {e}")

        log.info("wiki embedding complete")

    def _delete_embedding(self, slug: str):
        if not self._rag_available:
            return
        try:
            self._collection.delete(ids=[slug])
        except Exception as e:
            log.warning(f"could not delete embedding for {slug}: {e}")

    # --- Lint ---

    # Valid wiki page slug: lowercase letters, digits, hyphens only.
    # Inline mentions like [[Apr 22, 2026]], [[Birdhouse Coffee]], [[cityelectric.gov/outage]]
    # are intentionally excluded — they are narrative references, not page links.
    _SLUG_PAT = re.compile(r"^[a-z0-9][a-z0-9-]*$")

    @staticmethod
    def _is_page_slug(ref: str) -> bool:
        return bool(WikiEngine._SLUG_PAT.match(ref))

    @staticmethod
    def _normalise_ref(ref: str) -> str:
        """Strip .md suffix so [[area-overview.md]] resolves like [[area-overview]]."""
        return ref[:-3] if ref.endswith(".md") else ref

    def lint(self) -> list[str]:
        """Check wiki health. Returns list of issue strings."""
        issues: list[str] = []

        if not self._wiki_dir.exists():
            return ["wiki/ directory does not exist — run --build-wiki"]

        index_path = self._wiki_dir / "index.md"
        if not index_path.exists():
            return ["wiki/index.md missing — run --build-wiki"]

        index_content = index_path.read_text(encoding="utf-8", errors="replace")

        # Page slugs come only from the first column of index table rows: an
        # unclosed [[ref in a summary cell could otherwise swallow the next row.
        indexed_slugs: set[str] = set(_index_slugs(index_content))

        pages = {
            f.stem
            for f in self._wiki_dir.glob("*.md")
            if f.name not in ("index.md", "log.md")
        }

        # Orphan pages: in wiki/ but not in index.md
        orphans = pages - indexed_slugs
        for slug in sorted(orphans):
            issues.append(f"orphan page: {slug}.md (not in index)")

        # Missing pages: in index.md but not in wiki/
        missing = indexed_slugs - pages
        for slug in sorted(missing):
            issues.append(f"missing page: [[{slug}]] is in index but file not found")

        # Stale pages
        stale_after = self.cfg.get("wiki_stale_after_days", 30)
        for slug in sorted(pages):
            page_path = self._wiki_dir / f"{slug}.md"
            text = page_path.read_text(encoding="utf-8", errors="replace")
            m = re.search(r"^last_ingested:\s*(.+)$", text, re.MULTILINE)
            if m:
                try:
                    ingested = date.fromisoformat(m.group(1).strip())
                    age_days = (date.today() - ingested).days
                    if age_days > stale_after:
                        issues.append(
                            f"stale page: {slug}.md "
                            f"(last ingested {age_days}d ago)"
                        )
                except ValueError:
                    pass

        # Missing sources: only meaningful when this node has its knowledge/
        # folder (a wiki can legitimately be deployed without raw sources).
        if self._source_files():
            for slug in sorted(pages):
                text = (self._wiki_dir / f"{slug}.md").read_text(encoding="utf-8", errors="replace")
                for src in self._extract_sources(text):
                    if not (self._knowledge_dir / Path(src).name).is_file():
                        issues.append(f"missing source: {slug}.md lists {src}, not in knowledge/")

        # Missing cross-refs: [[slug]] in a page body that looks like a page link
        # but has no corresponding wiki file.  Skip inline mentions (dates, proper
        # nouns, URLs) — only check refs that are valid kebab-case page slugs.
        for slug in sorted(pages):
            page_path = self._wiki_dir / f"{slug}.md"
            text = page_path.read_text(encoding="utf-8", errors="replace")
            refs = set(re.findall(r"\[\[([^\]]+)\]\]", text))
            for ref in sorted(refs):
                norm = self._normalise_ref(ref)
                if not self._is_page_slug(norm):
                    continue   # inline mention — not a page link
                if norm not in pages:
                    issues.append(f"missing cross-ref: [[{ref}]] in {slug}.md")

        today = date.today().isoformat()
        self._append_log(
            f"[{today}] lint\n"
            f"Issues: {len(issues)} total. "
            f"{sum(1 for i in issues if 'orphan' in i)} orphan, "
            f"{sum(1 for i in issues if 'stale' in i)} stale, "
            f"{sum(1 for i in issues if 'cross-ref' in i)} missing cross-refs, "
            f"{sum(1 for i in issues if 'missing source' in i)} missing sources."
        )

        return issues

    # --- Watch ---

    def watch(self, interval: int, stop: threading.Event):
        """Background watcher: rebuild pages when knowledge/ files change and
        drop pages whose source was deleted.

        Uses wiki_patch_model, or the serving model — never the (possibly
        much larger) wiki_builder_model, which may not fit on the node.
        Disabled by wiki_watch_enabled: false.
        """
        if not self.cfg.get("wiki_watch_enabled", True):
            log.info("wiki watcher disabled (wiki_watch_enabled: false)")
            return
        model = self.cfg.get("wiki_patch_model") or self.cfg["model"]

        def _watcher():
            while not stop.is_set():
                try:
                    self.prune_removed_sources()
                    changed = self._detect_changes()
                    if changed and self._ollama_available:
                        log.info(f"knowledge change detected ({len(changed)} file(s))")
                        for f in changed:
                            self.build(file=f, model=model)
                except Exception:
                    log.exception("wiki watcher error")
                stop.wait(interval)

        threading.Thread(target=_watcher, name="wiki-watcher", daemon=True).start()
        log.info(f"wiki watcher started (poll every {interval}s, model {model!r})")

    def _detect_changes(self) -> list[str]:
        """Return knowledge file paths whose content changed since last build."""
        changed = []
        for path in self._source_files():
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            h = hashlib.md5(content.encode()).hexdigest()
            with self._lock:
                if self._file_hashes.get(path.name) != h:
                    changed.append(str(path))
        return changed

    # --- Topics ---

    def get_topics(self) -> list[str]:
        """Return readable page titles from index.md, in index order."""
        index_path = self._wiki_dir / "index.md"
        if not index_path.exists():
            return []
        content = index_path.read_text(encoding="utf-8", errors="replace")
        return [s.replace("-", " ").title() for s in _index_slugs(content)]

    # --- Staleness annotation ---

    def _staleness_note(self, page_path: Path, sources: list[Path]) -> str:
        """How old a time-sensitive page's data is: the newest source file's
        modification time, or the page's last_ingested date."""
        try:
            if sources:
                age = time.time() - max(p.stat().st_mtime for p in sources)
                return f"last updated {_age_phrase(age)}"
            text = page_path.read_text(encoding="utf-8", errors="replace")
            m = re.search(r"^last_ingested:\s*(\S+)", text, re.MULTILINE)
            if m:
                days = (date.today() - date.fromisoformat(m.group(1))).days
                return "ingested today" if days <= 0 else f"ingested {days}d ago"
        except (OSError, ValueError):
            pass
        return ""

    # --- Hash cache persistence ---

    def _load_hash_cache(self):
        try:
            if self._hash_cache_file.exists():
                with open(self._hash_cache_file) as f:
                    data = json.load(f)
                # v0.2 keyed hashes by absolute path, so a wiki built on one
                # machine looked entirely changed on another. Key by filename.
                with self._lock:
                    self._file_hashes = {Path(k).name: v for k, v in data.items()}
        except Exception as e:
            log.warning(f"could not load wiki hash cache: {e}")

    def _save_hash_cache(self):
        with self._lock:
            data = dict(self._file_hashes)
        write_atomic(str(self._hash_cache_file), json.dumps(data, sort_keys=True))


# --- Helpers ---


def split_passages(text: str, max_chars: int = PASSAGE_CHARS) -> list[str]:
    """Split a markdown or plain-text document into passages of about
    max_chars.

    Sections start at markdown headings, and a passage never spans two
    sections. The section heading is repeated at the top of every passage
    cut from it, so each passage makes sense on its own.
    """
    body = _FRONTMATTER.sub("", text.replace("\r\n", "\n"), count=1)
    sections: list[tuple[str, list[str]]] = []
    heading, paragraphs, lines = "", [], []

    for raw in body.split("\n"):
        line = raw.rstrip()
        if _HEADING.match(line):
            if lines:
                paragraphs.append("\n".join(lines))
                lines = []
            sections.append((heading, paragraphs))
            heading, paragraphs = line.strip(), []
        elif not line.strip():
            if lines:
                paragraphs.append("\n".join(lines))
                lines = []
        else:
            lines.append(line)
    if lines:
        paragraphs.append("\n".join(lines))
    sections.append((heading, paragraphs))

    passages: list[str] = []
    for heading, paras in sections:
        if not paras:
            continue
        budget = max(max_chars - len(heading) - 1, max_chars // 2)
        current = ""
        for piece in _fit_pieces(paras, budget):
            candidate = f"{current}\n\n{piece}" if current else piece
            if len(candidate) <= budget:
                current = candidate
            else:
                passages.append(f"{heading}\n{current}" if heading else current)
                current = piece
        if current:
            passages.append(f"{heading}\n{current}" if heading else current)
    return passages


def _fit_pieces(paragraphs: list[str], budget: int):
    """Yield paragraphs, splitting any longer than budget by line, then by
    sentence or word."""
    for para in paragraphs:
        if len(para) <= budget:
            yield para
            continue
        current = ""
        for line in para.split("\n"):
            for piece in _split_long_line(line, budget):
                candidate = f"{current}\n{piece}" if current else piece
                if len(candidate) <= budget:
                    current = candidate
                else:
                    if current:
                        yield current
                    current = piece
        if current:
            yield current


def _split_long_line(line: str, budget: int) -> list[str]:
    out: list[str] = []
    rest = line.strip()
    while len(rest) > budget:
        cut = max(rest.rfind(". ", 0, budget), rest.rfind("; ", 0, budget))
        if cut < budget // 3:
            cut = rest.rfind(" ", 0, budget)
        if cut <= 0:
            cut = budget - 1
        out.append(rest[: cut + 1].strip())
        rest = rest[cut + 1:].strip()
    if rest:
        out.append(rest)
    return out


def _index_slugs(index_content: str) -> list[str]:
    """Page slugs from the first column of wiki/index.md rows, in order."""
    seen: dict[str, None] = {}
    for slug in re.findall(r"^\|\s*\[\[([\w-]+)\]\]", index_content, re.MULTILINE):
        seen.setdefault(slug, None)
    return list(seen)


def _table_cell(text: str) -> str:
    """Make text safe for one markdown table cell."""
    return " ".join(text.split()).replace("|", "/")


def _strip_code_fence(text: str) -> str:
    """Remove a ```markdown ... ``` wrapper small models sometimes add."""
    if text.startswith("```"):
        lines = text.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return text


def _normalise_frontmatter(wiki_text: str, source: Path, today: str) -> str:
    """Make sure a generated page names its real source file and build date.

    Query-time source lookup and pruning rely on sources:, so it is set to
    the actual filename whatever the model wrote.
    """
    sources_line = f"sources: [{source.name}]"
    ingested_line = f"last_ingested: {today}"
    fm = _FRONTMATTER.match(wiki_text)
    if not fm:
        title = source.stem.replace("-", " ").replace("_", " ").title()
        return (
            f"---\ntitle: {title}\ntags: []\n{sources_line}\n{ingested_line}\n---\n\n"
            + wiki_text.strip() + "\n"
        )
    lines = [
        ln for ln in fm.group(1).split("\n")
        if not ln.startswith(("sources:", "last_ingested:"))
    ]
    lines += [sources_line, ingested_line]
    return "---\n" + "\n".join(lines) + "\n---\n" + wiki_text[fm.end():]


def _age_phrase(seconds: float) -> str:
    hours = int(seconds // 3600)
    if hours < 1:
        return "< 1 hr ago"
    if hours < 24:
        return f"{hours} hrs ago"
    return f"{hours // 24}d ago"


def _bm25_scores(
    query_terms: list[str],
    corpus: list[tuple[str, list[str]]],
    k1: float = 1.5,
    b: float = 0.75,
) -> list[float]:
    """Compute BM25 scores for query_terms against each document in corpus."""
    n = len(corpus)
    if n == 0:
        return []

    avg_dl = (sum(len(doc) for _, doc in corpus) / n) or 1.0
    counts = [Counter(doc) for _, doc in corpus]
    df = {t: sum(1 for c in counts if t in c) for t in set(query_terms)}

    scores = []
    for (_, doc), tf_counts in zip(corpus, counts):
        dl = len(doc)
        score = 0.0
        for term in query_terms:
            tf = tf_counts.get(term, 0)
            if tf == 0:
                continue
            idf = math.log((n - df[term] + 0.5) / (df[term] + 0.5) + 1)
            tf_norm = (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl / avg_dl))
            score += idf * tf_norm
        scores.append(score)

    return scores
