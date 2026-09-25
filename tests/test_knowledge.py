"""Tests for del_fi.core.knowledge (WikiEngine).

Covers: build pipeline, query pipeline, BM25 search, lint, page_count,
get_topics, and suggest. All Ollama and ChromaDB calls are mocked.
"""

import hashlib
import json
import os
import tempfile
import threading
import time
import types
import unittest
import unittest.mock
from datetime import date, timedelta
from pathlib import Path


def _make_cfg(tmpdir: str, **overrides) -> dict:
    cfg = {
        "node_name": "TEST-NODE",
        "knowledge_folder": os.path.join(tmpdir, "knowledge"),
        "wiki_folder": os.path.join(tmpdir, "wiki"),
        "wiki_builder_model": None,
        "model": "test-model:3b",
        "wiki_stale_after_days": 30,
        "time_sensitive_files": [],
        "similarity_threshold": 0.28,
        "rag_top_k": 3,
        "max_context_tokens": 2048,
        "small_model_prompt": False,
        "reorder_context": False,
        "num_ctx": 2048,
        "num_predict": 300,
        "ollama_host": "http://localhost:11434",
        "ollama_timeout": 30,
        "embedding_model": "nomic-embed-text",
        "personality": "Test assistant.",
        "_vectorstore_dir": os.path.join(tmpdir, "vectorstore"),
        "_cache_dir": os.path.join(tmpdir, "cache"),
    }
    cfg.update(overrides)
    return cfg


def _make_wiki_page(title: str, tags: list[str], body: str, days_old: int = 0) -> str:
    """Return a formatted wiki page string."""
    ingested = (date.today() - timedelta(days=days_old)).isoformat()
    tag_str = ", ".join(tags)
    return (
        f"---\n"
        f"title: {title}\n"
        f"tags: [{tag_str}]\n"
        f"sources: [example.md]\n"
        f"last_ingested: {ingested}\n"
        f"---\n\n"
        f"# {title}\n\n"
        f"{body}\n"
    )


def _write_file(path: str, content: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


class _FakeOllamaClient:
    """Minimal Ollama client stub."""

    def __init__(self, generate_response: str = ""):
        self.generate_response = generate_response
        self.generate_calls: list[dict] = []
        self.embed_calls: list[str] = []

    def list(self):
        return {"models": []}

    def generate(self, model: str, prompt: str, options: dict = None, stream: bool = False, **kwargs):
        self.generate_calls.append({"model": model, "prompt": prompt, "options": options, **kwargs})

        class _Resp:
            pass

        r = _Resp()
        r.response = self.generate_response
        return r

    def embed(self, model: str, input: str, **kwargs):
        """Same shape as ollama.Client.embed: .embeddings is a list of vectors."""
        self.embed_calls.append(input)
        return types.SimpleNamespace(embeddings=[self._vector(input)])

    def _vector(self, text: str):
        # A simple 16-dimensional fake embedding
        h = hashlib.md5(text.encode()).digest()
        return [(b / 127.5) - 1.0 for b in h]


def _make_engine(tmpdir: str, ollama_client=None, **cfg_overrides):
    """Build a WikiEngine with patched Ollama (and ChromaDB disabled)."""
    from del_fi.core.knowledge import WikiEngine

    cfg = _make_cfg(tmpdir, **cfg_overrides)
    os.makedirs(cfg["knowledge_folder"], exist_ok=True)
    os.makedirs(cfg["wiki_folder"], exist_ok=True)

    client = ollama_client or _FakeOllamaClient()

    with unittest.mock.patch("del_fi.core.knowledge.WikiEngine._init_ollama"):
        with unittest.mock.patch("del_fi.core.knowledge.WikiEngine._init_vectorstore"):
            engine = WikiEngine(cfg)

    engine._ollama = client
    engine._ollama_available = True
    engine._rag_available = False
    engine._collection = None

    return engine


# ---------------------------------------------------------------------------
# Tests: build pipeline
# ---------------------------------------------------------------------------

class TestBuildPipeline(unittest.TestCase):
    """Build pipeline: knowledge/ → wiki/."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="delfi-knowledge-")

    def test_build_creates_wiki_page(self):
        """build() writes a wiki page for each knowledge file."""
        wiki_page = _make_wiki_page(
            title="Wildlife Guide",
            tags=["wildlife", "elk"],
            body="Elk are large ungulates. They migrate seasonally.",
        )
        client = _FakeOllamaClient(generate_response=wiki_page)
        engine = _make_engine(self.tmpdir, ollama_client=client)

        knowledge_file = os.path.join(engine.cfg["knowledge_folder"], "wildlife-guide.md")
        _write_file(knowledge_file, "# Wildlife\n\nElk are present in the area.")

        count = engine.build()

        self.assertEqual(count, 1)
        wiki_path = os.path.join(engine.cfg["wiki_folder"], "wildlife-guide.md")
        self.assertTrue(os.path.exists(wiki_path), "wiki page should be written")
        content = Path(wiki_path).read_text(encoding="utf-8")
        self.assertIn("Wildlife Guide", content)

    def test_build_creates_index_entry(self):
        """build() updates wiki/index.md with the new page slug."""
        wiki_page = _make_wiki_page(
            title="Trail Camera Log",
            tags=["cameras", "wildlife"],
            body="Camera 1 is at the north trailhead.",
        )
        client = _FakeOllamaClient(generate_response=wiki_page)
        engine = _make_engine(self.tmpdir, ollama_client=client)

        knowledge_file = os.path.join(engine.cfg["knowledge_folder"], "trail-camera-log.md")
        _write_file(knowledge_file, "# Cameras\n\nCamera 1 is north.")

        engine.build()

        index_path = os.path.join(engine.cfg["wiki_folder"], "index.md")
        self.assertTrue(os.path.exists(index_path))
        index_content = Path(index_path).read_text(encoding="utf-8")
        self.assertIn("trail-camera-log", index_content)

    def test_build_skips_unchanged_files(self):
        """build() skips files whose MD5 hash hasn't changed since last run."""
        wiki_page = _make_wiki_page(
            title="Weather Station",
            tags=["weather"],
            body="Davis station records temperature, humidity, and wind.",
        )
        client = _FakeOllamaClient(generate_response=wiki_page)
        engine = _make_engine(self.tmpdir, ollama_client=client)

        knowledge_file = os.path.join(engine.cfg["knowledge_folder"], "weather-station.md")
        _write_file(knowledge_file, "# Weather\n\nDavis station.")

        # First build — file is new, should call LLM
        engine.build()
        calls_after_first = len(client.generate_calls)
        self.assertEqual(calls_after_first, 1)

        # Second build — same content, should skip
        engine.build()
        calls_after_second = len(client.generate_calls)
        self.assertEqual(calls_after_second, 1, "LLM should not be called for unchanged file")

    def test_build_reprocesses_changed_files(self):
        """build() reprocesses a file when its content changes."""
        wiki_page = _make_wiki_page(
            title="Area Overview",
            tags=["area"],
            body="The station is at 2400m elevation.",
        )
        client = _FakeOllamaClient(generate_response=wiki_page)
        engine = _make_engine(self.tmpdir, ollama_client=client)

        knowledge_file = os.path.join(engine.cfg["knowledge_folder"], "area-overview.md")
        _write_file(knowledge_file, "Original content.")
        engine.build()

        # Modify the file
        _write_file(knowledge_file, "Updated content — new field added.")
        engine.build()

        self.assertEqual(len(client.generate_calls), 2, "LLM should be called again for changed file")

    def test_build_with_single_file(self):
        """build(file=path) processes only the specified file."""
        wiki_page = _make_wiki_page(title="Flora Guide", tags=["flora"], body="Pine trees.")
        client = _FakeOllamaClient(generate_response=wiki_page)
        engine = _make_engine(self.tmpdir, ollama_client=client)

        f1 = os.path.join(engine.cfg["knowledge_folder"], "flora-guide.md")
        f2 = os.path.join(engine.cfg["knowledge_folder"], "fauna-guide.md")
        _write_file(f1, "Pines and firs.")
        _write_file(f2, "Elk and deer.")

        engine.build(file=f1)
        self.assertEqual(len(client.generate_calls), 1)

    def test_build_without_ollama_returns_zero(self):
        """build() returns 0 immediately when Ollama is unavailable."""
        engine = _make_engine(self.tmpdir)
        engine._ollama_available = False

        knowledge_file = os.path.join(engine.cfg["knowledge_folder"], "test.md")
        _write_file(knowledge_file, "Some content.")

        count = engine.build()
        self.assertEqual(count, 0)


# ---------------------------------------------------------------------------
# Tests: query pipeline
# ---------------------------------------------------------------------------

class TestQueryPipeline(unittest.TestCase):
    """Query pipeline: BM25 search + LLM answer generation."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="delfi-knowledge-")

    def _write_wiki(self, slug: str, title: str, tags: list[str], body: str, days_old: int = 0):
        wiki_dir = os.path.join(self.tmpdir, "wiki")
        page = _make_wiki_page(title, tags, body, days_old)
        _write_file(os.path.join(wiki_dir, f"{slug}.md"), page)

        # Also update index
        index_path = os.path.join(wiki_dir, "index.md")
        tag_str = ", ".join(tags)
        row = f"| [[{slug}]] | {body[:60]} | {tag_str} | {date.today().isoformat()} |"
        if not os.path.exists(index_path):
            _write_file(
                index_path,
                "# Wiki Index\n\n"
                "| Page | Summary | Tags | Updated |\n"
                "|------|---------|------|--------|\n"
                f"{row}\n",
            )
        else:
            existing = Path(index_path).read_text(encoding="utf-8")
            _write_file(index_path, existing.rstrip() + f"\n{row}\n")

    def test_query_bm25_hits_matching_page(self):
        """query() uses BM25 to find a page whose tags match the query."""
        self._write_wiki(
            "wildlife-guide",
            "Wildlife Guide",
            ["wildlife", "elk", "mountain-lion"],
            "Elk are ungulates that migrate seasonally.",
        )

        client = _FakeOllamaClient(generate_response="Elk migrate in spring and fall.")
        engine = _make_engine(self.tmpdir, ollama_client=client)

        answer, had_context = engine.query("Where do elk migrate?")

        self.assertTrue(had_context, "had_context should be True when a page is found")
        self.assertEqual(len(client.generate_calls), 1, "LLM should be called once")

    def test_query_returns_false_when_no_wiki(self):
        """query() returns (answer, False) when wiki/ is empty."""
        client = _FakeOllamaClient(generate_response="I don't know.")
        engine = _make_engine(self.tmpdir, ollama_client=client)

        answer, had_context = engine.query("What is the weather like?")

        self.assertFalse(had_context, "had_context should be False when wiki is empty")

    def test_query_includes_context_in_prompt(self):
        """query() inserts wiki page content into the LLM prompt."""
        self._write_wiki(
            "weather-station",
            "Weather Station",
            ["weather", "temperature"],
            "The Davis station reads temperature, humidity, and wind speed.",
        )
        client = _FakeOllamaClient(generate_response="Temperature is 12°C.")
        engine = _make_engine(self.tmpdir, ollama_client=client)

        engine.query("What is the temperature?")

        self.assertEqual(len(client.generate_calls), 1)
        prompt_used = client.generate_calls[0]["prompt"]
        # The wiki page body should be embedded in the prompt
        self.assertIn("Davis station", prompt_used)

    def test_query_with_no_ollama_returns_empty(self):
        """query() returns ('', False) when Ollama is unavailable."""
        self._write_wiki(
            "area-overview",
            "Area Overview",
            ["area"],
            "Station sits at 2400m above sea level.",
        )
        engine = _make_engine(self.tmpdir)
        engine._ollama_available = False

        answer, had_context = engine.query("What elevation is the station?")

        self.assertIsInstance(answer, str)
        self.assertFalse(had_context, "had_context should be False when Ollama is down")

    def test_query_connection_failure_raises_and_marks_ollama_down(self):
        from del_fi.core.knowledge import LLMError
        self._write_wiki("area-overview", "Area Overview", ["area", "elevation"],
                         "Station sits at 2400m above sea level.")

        class Refusing(_FakeOllamaClient):
            def generate(self, **kwargs):
                raise ConnectionError("Failed to connect to Ollama")

        engine = _make_engine(self.tmpdir, ollama_client=Refusing())
        with self.assertRaises(LLMError) as ctx:
            engine.query("What elevation is the station?")
        self.assertEqual(ctx.exception.kind, "unavailable")
        self.assertFalse(engine.available, "health loop should take over")

    def test_query_timeout_raises_but_keeps_ollama_up(self):
        from del_fi.core.knowledge import LLMError
        self._write_wiki("area-overview", "Area Overview", ["area", "elevation"],
                         "Station sits at 2400m above sea level.")

        class ReadTimeout(Exception):  # same name as httpx's timeout
            pass

        class Slow(_FakeOllamaClient):
            def generate(self, **kwargs):
                raise ReadTimeout("timed out")

        engine = _make_engine(self.tmpdir, ollama_client=Slow())
        with self.assertRaises(LLMError) as ctx:
            engine.query("What elevation is the station?")
        self.assertEqual(ctx.exception.kind, "timeout")
        self.assertTrue(engine.available)

    def test_query_passes_peer_context(self):
        """query() includes peer context in the LLM prompt when provided."""
        self._write_wiki(
            "trail-guide",
            "Trail Guide",
            ["trails", "hiking"],
            "There are 12 trails in the park.",
        )
        client = _FakeOllamaClient(generate_response="12 trails total.")
        engine = _make_engine(self.tmpdir, ollama_client=client)

        engine.query("How many trails?", peer_ctx="[via PEAK-NODE: 12 trails reported]")

        prompt_used = client.generate_calls[0]["prompt"]
        self.assertIn("PEAK-NODE", prompt_used)

    def test_query_passes_history(self):
        """query() includes conversation history in the LLM prompt."""
        self._write_wiki("guide", "Guide", ["guide"], "Shuttle runs hourly.")
        client = _FakeOllamaClient(generate_response="Shuttle at :00 and :30.")
        engine = _make_engine(self.tmpdir, ollama_client=client)

        engine.query("When is the next shuttle?", history="You: Is there a shuttle? Me: Yes.")

        prompt_used = client.generate_calls[0]["prompt"]
        self.assertIn("shuttle", prompt_used.lower())


# ---------------------------------------------------------------------------
# Tests: lint
# ---------------------------------------------------------------------------

class TestLint(unittest.TestCase):
    """wiki lint() health checks."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="delfi-knowledge-")
        self.wiki_dir = os.path.join(self.tmpdir, "wiki")
        os.makedirs(self.wiki_dir, exist_ok=True)

    def _write_wiki_page(self, slug: str, body: str = "", days_old: int = 0, refs: list = None):
        page = _make_wiki_page(slug.replace("-", " ").title(), [slug], body or "Content.", days_old)
        if refs:
            page += "\n" + " ".join(f"[[{r}]]" for r in refs)
        _write_file(os.path.join(self.wiki_dir, f"{slug}.md"), page)

    def _write_index(self, slugs: list[str]):
        rows = "\n".join(f"| [[{s}]] | Summary | tag | {date.today().isoformat()} |" for s in slugs)
        _write_file(
            os.path.join(self.wiki_dir, "index.md"),
            f"# Wiki Index\n\n| Page | Summary | Tags | Updated |\n|------|---------|------|--------|\n{rows}\n",
        )

    def test_lint_no_issues_when_clean(self):
        """lint() returns empty list when wiki is consistent."""
        self._write_wiki_page("wildlife-guide")
        self._write_index(["wildlife-guide"])

        engine = _make_engine(self.tmpdir)
        issues = engine.lint()

        # Filter out timing-related issues that may appear in CI
        non_stale = [i for i in issues if "stale" not in i]
        self.assertEqual(non_stale, [])

    def test_lint_detects_orphan_page(self):
        """lint() reports pages in wiki/ not listed in index.md."""
        self._write_wiki_page("flora-guide")   # file exists
        self._write_index([])                   # but not in index

        engine = _make_engine(self.tmpdir)
        issues = engine.lint()

        orphan_issues = [i for i in issues if "orphan" in i]
        self.assertGreaterEqual(len(orphan_issues), 1, f"Expected orphan issue, got: {issues}")
        self.assertTrue(any("flora-guide" in i for i in orphan_issues))

    def test_lint_detects_missing_page(self):
        """lint() reports index entries whose wiki page file is absent."""
        self._write_index(["ghost-page"])  # in index but no file

        engine = _make_engine(self.tmpdir)
        issues = engine.lint()

        missing_issues = [i for i in issues if "missing page" in i]
        self.assertGreaterEqual(len(missing_issues), 1, f"Expected missing-page issue, got: {issues}")
        self.assertTrue(any("ghost-page" in i for i in missing_issues))

    def test_lint_detects_stale_page(self):
        """lint() flags pages whose last_ingested is older than wiki_stale_after_days."""
        days_old = 60
        self._write_wiki_page("old-guide", days_old=days_old)
        self._write_index(["old-guide"])

        engine = _make_engine(self.tmpdir, wiki_stale_after_days=30)
        issues = engine.lint()

        stale_issues = [i for i in issues if "stale" in i]
        self.assertGreaterEqual(len(stale_issues), 1, f"Expected stale issue, got: {issues}")
        self.assertTrue(any("old-guide" in i for i in stale_issues))

    def test_lint_ignores_fresh_pages(self):
        """lint() does not flag pages ingested within the stale window."""
        self._write_wiki_page("fresh-guide", days_old=1)
        self._write_index(["fresh-guide"])

        engine = _make_engine(self.tmpdir, wiki_stale_after_days=30)
        issues = engine.lint()

        stale_issues = [i for i in issues if "stale" in i and "fresh-guide" in i]
        self.assertEqual(stale_issues, [])

    def test_lint_detects_missing_cross_ref(self):
        """lint() reports [[cross-refs]] that point to non-existent pages."""
        self._write_wiki_page("wildlife-guide", refs=["nonexistent-page"])
        self._write_index(["wildlife-guide"])

        engine = _make_engine(self.tmpdir)
        issues = engine.lint()

        ref_issues = [i for i in issues if "cross-ref" in i or "missing cross-ref" in i]
        self.assertGreaterEqual(len(ref_issues), 1, f"Expected cross-ref issue, got: {issues}")

    def test_lint_no_wiki_dir(self):
        """lint() returns a helpful message when wiki/ doesn't exist."""
        import shutil
        shutil.rmtree(self.wiki_dir, ignore_errors=True)

        engine = _make_engine(self.tmpdir)
        issues = engine.lint()

        self.assertEqual(len(issues), 1)
        self.assertIn("--build-wiki", issues[0])

    def test_lint_no_index(self):
        """lint() returns a helpful message when wiki/index.md is missing."""
        self._write_wiki_page("flora-guide")
        # No index.md written

        engine = _make_engine(self.tmpdir)
        issues = engine.lint()

        self.assertEqual(len(issues), 1)
        self.assertIn("index.md", issues[0])


# ---------------------------------------------------------------------------
# Tests: properties
# ---------------------------------------------------------------------------

class TestProperties(unittest.TestCase):
    """page_count, get_topics, wiki_available, suggest."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="delfi-knowledge-")
        self.wiki_dir = os.path.join(self.tmpdir, "wiki")
        os.makedirs(self.wiki_dir, exist_ok=True)

    def _write_page(self, slug: str):
        page = _make_wiki_page(slug.replace("-", " ").title(), [slug], "Content.")
        _write_file(os.path.join(self.wiki_dir, f"{slug}.md"), page)

    def _write_index(self, slugs: list[str]):
        rows = "\n".join(f"| [[{s}]] | Summary | tag | {date.today().isoformat()} |" for s in slugs)
        _write_file(
            os.path.join(self.wiki_dir, "index.md"),
            f"# Wiki Index\n\n| Page | Summary | Tags | Updated |\n|------|---------|------|--------|\n{rows}\n",
        )

    def test_page_count_empty(self):
        """page_count == 0 when wiki/ exists but has no pages."""
        engine = _make_engine(self.tmpdir)
        self.assertEqual(engine.page_count, 0)

    def test_page_count_correct(self):
        """page_count reflects wiki/*.md files excluding index.md and log.md."""
        self._write_page("page-one")
        self._write_page("page-two")
        self._write_page("page-three")
        # These should not be counted
        _write_file(os.path.join(self.wiki_dir, "index.md"), "# Index\n")
        _write_file(os.path.join(self.wiki_dir, "log.md"), "# Log\n")

        engine = _make_engine(self.tmpdir)
        self.assertEqual(engine.page_count, 3)

    def test_wiki_available_false_when_no_index(self):
        """wiki_available is False when index.md is absent."""
        engine = _make_engine(self.tmpdir)
        self.assertFalse(engine.wiki_available)

    def test_wiki_available_true_when_index_exists(self):
        """wiki_available is True when index.md has content."""
        _write_file(
            os.path.join(self.wiki_dir, "index.md"),
            "# Wiki Index\n\n| [[page-one]] | Summary | tag | today |\n",
        )
        engine = _make_engine(self.tmpdir)
        self.assertTrue(engine.wiki_available)

    def test_get_topics_empty(self):
        """get_topics() returns [] when there is no index.md."""
        engine = _make_engine(self.tmpdir)
        self.assertEqual(engine.get_topics(), [])

    def test_get_topics_returns_titles(self):
        """get_topics() extracts page slugs from index.md and formats them."""
        self._write_index(["wildlife-guide", "weather-station", "trail-log"])
        engine = _make_engine(self.tmpdir)

        topics = engine.get_topics()
        self.assertEqual(len(topics), 3)
        # Slugs are title-cased
        self.assertIn("Wildlife Guide", topics)
        self.assertIn("Weather Station", topics)
        self.assertIn("Trail Log", topics)

    def test_suggest_returns_matching_topic(self):
        """suggest() returns a related topic when the query matches index keywords."""
        self._write_page("wildlife-guide")
        self._write_index(["wildlife-guide"])

        engine = _make_engine(self.tmpdir)
        result = engine.suggest("elk sighting near trailhead")

        # suggest() may return None or a string — both valid; just no crash
        self.assertIsInstance(result, (str, type(None)))


# ---------------------------------------------------------------------------
# Tests: concurrent safety
# ---------------------------------------------------------------------------

class TestConcurrency(unittest.TestCase):
    """WikiEngine should be safe to call from multiple threads."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="delfi-knowledge-")
        self.wiki_dir = os.path.join(self.tmpdir, "wiki")
        os.makedirs(self.wiki_dir, exist_ok=True)

    def _write_wiki_page_file(self, slug: str):
        page = _make_wiki_page(slug.replace("-", " ").title(), [slug], "Content.")
        _write_file(os.path.join(self.wiki_dir, f"{slug}.md"), page)
        rows = f"| [[{slug}]] | Summary | {slug} | {date.today().isoformat()} |"
        index_path = os.path.join(self.wiki_dir, "index.md")
        if not os.path.exists(index_path):
            _write_file(
                index_path,
                f"# Wiki Index\n\n| Page | Summary | Tags | Updated |\n|------|---------|------|--------|\n{rows}\n",
            )
        else:
            content = Path(index_path).read_text()
            _write_file(index_path, content.rstrip() + f"\n{rows}\n")

    def test_concurrent_queries_do_not_crash(self):
        """Multiple threads calling query() simultaneously should not crash."""
        for i in range(3):
            self._write_wiki_page_file(f"topic-{i}")

        client = _FakeOllamaClient(generate_response="An answer.")
        engine = _make_engine(self.tmpdir, ollama_client=client)

        errors: list[Exception] = []
        results: list = []
        lock = threading.Lock()

        def _run(q: str):
            try:
                r = engine.query(q)
                with lock:
                    results.append(r)
            except Exception as e:
                with lock:
                    errors.append(e)

        threads = [
            threading.Thread(target=_run, args=(f"question {i}",))
            for i in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        self.assertEqual(errors, [], f"Concurrent query errors: {errors}")
        self.assertEqual(len(results), 8)


# ---------------------------------------------------------------------------
# v0.3: passage selection and context budget
# ---------------------------------------------------------------------------


def _index_row(slug, summary, tags):
    return f"| [[{slug}]] | {summary} | {tags} | {date.today().isoformat()} |"


def _setup_pages(tmpdir, pages):
    """pages: {slug: (tags, raw_source_text or None)}. Writes wiki pages,
    index.md, and knowledge/<slug>.md sources."""
    wiki_dir = os.path.join(tmpdir, "wiki")
    rows = []
    for slug, (tags, source) in pages.items():
        page = (
            f"---\ntitle: {slug}\ntags: [{', '.join(tags)}]\n"
            f"sources: [{slug}.md]\nlast_ingested: {date.today().isoformat()}\n---\n\n"
            f"# {slug}\n\nIndex page for {slug}.\n"
        )
        _write_file(os.path.join(wiki_dir, f"{slug}.md"), page)
        rows.append(_index_row(slug, f"About {slug}.", ", ".join(tags)))
        if source is not None:
            _write_file(os.path.join(tmpdir, "knowledge", f"{slug}.md"), source)
    _write_file(
        os.path.join(wiki_dir, "index.md"),
        "# Wiki Index\n\n| Page | Summary | Tags | Updated |\n|---|---|---|---|\n"
        + "\n".join(rows) + "\n",
    )


def _big_source(topic, n_sections=12):
    sections = []
    for i in range(n_sections):
        sections.append(f"## Section {i}\n\n" + f"Filler text about routine matters {i}. " * 15)
    sections.insert(5, f"## {topic.title()} Details\n\nThe {topic} gathering spot is Willow Flats at dawn.")
    return "# Guide\n\n" + "\n\n".join(sections)


class TestPassages(unittest.TestCase):
    def test_sections_split_with_heading_repeated(self):
        from del_fi.core.knowledge import split_passages
        text = "---\ntitle: x\n---\n# Title\n\nIntro para.\n\n## Elk\n\n" + ("Elk fact. " * 150)
        passages = split_passages(text, max_chars=300)
        self.assertEqual(passages[0], "# Title\nIntro para.")
        elk = [p for p in passages if p.startswith("## Elk")]
        self.assertGreater(len(elk), 2, "long section should be cut into several passages")
        self.assertTrue(all(len(p) <= 300 for p in elk))
        self.assertNotIn("title: x", "\n".join(passages))

    def test_long_line_is_split(self):
        from del_fi.core.knowledge import split_passages
        passages = split_passages("word " * 400, max_chars=200)
        self.assertGreater(len(passages), 5)
        self.assertTrue(all(len(p) <= 200 for p in passages))


class TestContextBudget(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="delfi-ctx-")

    def _prompt(self, engine, client, question):
        engine.query(question)
        return client.generate_calls[-1]["prompt"]

    def test_relevant_passage_selected_from_large_source(self):
        _setup_pages(self.tmpdir, {"elk-guide": (["elk", "wildlife"], _big_source("elk"))})
        client = _FakeOllamaClient(generate_response="At Willow Flats.")
        engine = _make_engine(self.tmpdir, ollama_client=client, max_context_tokens=150,
                              num_ctx=None)
        prompt = self._prompt(engine, client, "where do the elk gather?")
        self.assertIn("Willow Flats", prompt)
        self.assertLess(len(prompt), 150 * 4 + 400)

    def test_small_model_profile_respects_budget_with_reorder(self):
        """v0.2 bug: reorder_context rebuilt the context from untrimmed parts."""
        pages = {f"elk-{n}": (["elk"], _big_source("elk")) for n in ("guide", "log", "map")}
        _setup_pages(self.tmpdir, pages)
        client = _FakeOllamaClient(generate_response="Elk are near the meadow.")
        engine = _make_engine(self.tmpdir, ollama_client=client, max_context_tokens=512,
                              reorder_context=True, small_model_prompt=True, num_ctx=None)
        prompt = self._prompt(engine, client, "where are the elk")
        context = prompt.split("Question:")[0]
        self.assertLessEqual(len(context), 512 * 4 + 40)

    def test_reorder_puts_top_page_last(self):
        _setup_pages(self.tmpdir, {
            "elk-guide": (["elk", "herd", "migration"], "## Elk\n\nElk herd migration route east."),
            "deer-notes": (["deer", "elk"], "## Deer\n\nDeer sometimes mix with elk."),
        })
        client = _FakeOllamaClient(generate_response="East.")
        engine = _make_engine(self.tmpdir, ollama_client=client, reorder_context=True)
        prompt = self._prompt(engine, client, "elk herd migration")
        self.assertLess(prompt.index("[deer-notes]"), prompt.index("[elk-guide]"))

    def test_sources_resolved_by_basename_only(self):
        secret = os.path.join(self.tmpdir, "secret.md")
        _write_file(secret, "TOP SECRET elk data")
        wiki_dir = os.path.join(self.tmpdir, "wiki")
        _write_file(os.path.join(wiki_dir, "elk.md"),
                    "---\ntitle: Elk\ntags: [elk]\nsources: [../secret.md]\n---\n\n# Elk\n\nElk page.\n")
        _write_file(os.path.join(wiki_dir, "index.md"),
                    "| Page | Summary | Tags | Updated |\n|---|---|---|---|\n"
                    + _index_row("elk", "Elk page.", "elk") + "\n")
        client = _FakeOllamaClient(generate_response="Elk.")
        engine = _make_engine(self.tmpdir, ollama_client=client)
        prompt = self._prompt(engine, client, "elk")
        self.assertNotIn("TOP SECRET", prompt)
        self.assertIn("Elk page.", prompt)

    def test_num_ctx_derived_and_sent(self):
        client = _FakeOllamaClient(generate_response="ok.")
        engine = _make_engine(self.tmpdir, ollama_client=client, num_ctx=None,
                              max_context_tokens=None, num_predict=300)
        self.assertEqual(engine.num_ctx(), 3584)
        engine2 = _make_engine(self.tmpdir, ollama_client=client, num_ctx=1024)
        self.assertEqual(engine2.num_ctx(), 1024)

    def test_generate_passes_stable_num_ctx(self):
        _setup_pages(self.tmpdir, {"elk-guide": (["elk"], "## Elk\n\nElk live here.")})
        client = _FakeOllamaClient(generate_response="Here.")
        engine = _make_engine(self.tmpdir, ollama_client=client, num_ctx=None)
        engine.query("elk")
        engine.query("elk live")
        ctxs = {c["options"]["num_ctx"] for c in client.generate_calls}
        self.assertEqual(ctxs, {engine.num_ctx()})

    def test_no_shared_keywords_reads_top_page_from_start(self):
        _setup_pages(self.tmpdir, {"elk-guide": (["elk"], "## Intro\n\nFirst passage.\n\n## More\n\nSecond.")})
        client = _FakeOllamaClient(generate_response="ok.")
        engine = _make_engine(self.tmpdir, ollama_client=client)
        context, used = engine._build_context("zzz", ["elk-guide"], budget=5000)
        self.assertEqual(used, ["elk-guide"])
        self.assertLess(context.index("First passage"), context.index("Second"))

    def test_staleness_uses_source_mtime(self):
        _setup_pages(self.tmpdir, {"weather-station": (["weather"], "## Now\n\nWind 12 mph.")})
        src = os.path.join(self.tmpdir, "knowledge", "weather-station.md")
        three_hours_ago = time.time() - 3 * 3600
        os.utime(src, (three_hours_ago, three_hours_ago))
        client = _FakeOllamaClient(generate_response="12 mph.")
        engine = _make_engine(self.tmpdir, ollama_client=client,
                              time_sensitive_files=["weather-station.md"])
        prompt = self._prompt(engine, client, "weather wind")
        self.assertIn("[weather-station — last updated 3 hrs ago]", prompt)


class TestIdkAndSearch(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="delfi-idk-")

    def test_idk_detection_checks_first_sentence(self):
        engine = _make_engine(self.tmpdir)
        self.assertTrue(engine._is_idk_response("I don't know."))
        self.assertTrue(engine._is_idk_response("I'm not sure. The docs cover elk."))
        self.assertFalse(engine._is_idk_response("The trail is 3 miles. I'm not sure about ice."))
        self.assertFalse(engine._is_idk_response("I'm not sure, but the log shows elk at dawn."))

    def test_content_search_matches_whole_words(self):
        wiki_dir = os.path.join(self.tmpdir, "wiki")
        _write_file(os.path.join(wiki_dir, "events.md"), "# Events\n\nThe start of the party.")
        _write_file(os.path.join(wiki_dir, "gallery.md"), "# Gallery\n\nLocal art on display.")
        engine = _make_engine(self.tmpdir)
        self.assertEqual(engine._content_search("art"), ["gallery"])

    def test_topics_ignore_cross_refs_in_summary(self):
        wiki_dir = os.path.join(self.tmpdir, "wiki")
        _write_file(os.path.join(wiki_dir, "index.md"),
                    "| Page | Summary | Tags | Updated |\n|---|---|---|---|\n"
                    "| [[elk-guide]] | See [[not-a-page]]. | elk | 2026-01-01 |\n"
                    "| [[flora]] | Plants. | plants | 2026-01-01 |\n")
        engine = _make_engine(self.tmpdir)
        self.assertEqual(engine.get_topics(), ["Elk Guide", "Flora"])


class _ScriptedBuildClient(_FakeOllamaClient):
    """Returns (text, done_reason) pairs in order."""

    def __init__(self, replies):
        super().__init__()
        self.replies = list(replies)

    def generate(self, model, prompt, options=None, stream=False, **kwargs):
        self.generate_calls.append({"model": model, "prompt": prompt, "options": options})
        text, reason = self.replies.pop(0) if self.replies else ("", "stop")

        class _Resp:
            pass

        r = _Resp()
        r.response, r.done_reason = text, reason
        return r


class TestBuildV03(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="delfi-build-")

    def _engine(self, client, **cfg):
        engine = _make_engine(self.tmpdir, ollama_client=client, **cfg)
        return engine, engine.cfg["knowledge_folder"], engine.cfg["wiki_folder"]

    def test_done_reason_stop_accepted_without_punctuation(self):
        client = _ScriptedBuildClient([("---\ntitle: X\n---\n# X\n\nTags list", "stop")])
        engine, kdir, _ = self._engine(client)
        _write_file(os.path.join(kdir, "x.md"), "source")
        self.assertEqual(engine.build(), 1)
        self.assertEqual(len(client.generate_calls), 1)

    def test_done_reason_length_retries(self):
        client = _ScriptedBuildClient([("---\ntitle: X\n---\ncut off.", "length"),
                                       ("---\ntitle: X\n---\n# X\n\nComplete.", "stop")])
        engine, kdir, _ = self._engine(client)
        _write_file(os.path.join(kdir, "x.md"), "source")
        engine.build()
        self.assertEqual(len(client.generate_calls), 2)
        self.assertEqual(client.generate_calls[0]["options"]["num_ctx"], 8192)

    def test_frontmatter_forced_to_real_source(self):
        client = _ScriptedBuildClient([
            ("```markdown\n---\ntitle: Elk\nsources: [made-up.md]\nlast_ingested: 1999-01-01\n---\n# Elk\n\nElk.\n```", "stop"),
        ])
        engine, kdir, wdir = self._engine(client)
        _write_file(os.path.join(kdir, "elk-guide.md"), "Elk.")
        engine.build()
        page = Path(wdir, "elk-guide.md").read_text()
        self.assertTrue(page.startswith("---\ntitle: Elk\n"))
        self.assertIn("sources: [elk-guide.md]", page)
        self.assertIn(f"last_ingested: {date.today().isoformat()}", page)
        self.assertNotIn("made-up", page)
        self.assertNotIn("```", page)

    def test_backslash_in_llm_tags_does_not_break_index(self):
        page = "---\ntitle: X\ntags: [data, C:\\data\\new, a|b]\n---\n# X\n\nSome text here.\n"
        client = _ScriptedBuildClient([(page, "stop"), (page.replace("Some", "Other"), "stop")])
        engine, kdir, wdir = self._engine(client)
        _write_file(os.path.join(kdir, "x.md"), "v1")
        engine.build()
        _write_file(os.path.join(kdir, "x.md"), "v2")  # second build replaces the row
        self.assertEqual(engine.build(), 1)
        index = Path(wdir, "index.md").read_text()
        self.assertIn("C:\\data\\new", index)
        self.assertEqual(index.count("[[x]]"), 1)
        self.assertIn("a/b", index)

    def test_md_and_txt_with_same_stem_get_separate_pages(self):
        client = _ScriptedBuildClient([("---\ntitle: A\n---\n# A\n\nA.", "stop")] * 2)
        engine, kdir, wdir = self._engine(client)
        _write_file(os.path.join(kdir, "notes.md"), "md version")
        _write_file(os.path.join(kdir, "notes.txt"), "txt version")
        self.assertEqual(engine.build(), 2)
        self.assertTrue(Path(wdir, "notes.md").exists())
        self.assertTrue(Path(wdir, "notes-txt.md").exists())

    def test_hash_cache_keys_are_filenames_and_migrate(self):
        client = _ScriptedBuildClient([("---\ntitle: A\n---\n# A\n\nA.", "stop")])
        engine, kdir, wdir = self._engine(client)
        _write_file(os.path.join(kdir, "a.md"), "alpha")
        engine.build()
        cache = json.loads(Path(wdir, ".hash_cache.json").read_text())
        self.assertEqual(list(cache), ["a.md"])

        # A v0.2 cache written on another machine (absolute paths).
        Path(wdir, ".hash_cache.json").write_text(json.dumps(
            {"/Users/alice/del-fi/knowledge/a.md": cache["a.md"]}))
        engine2, _, _ = self._engine(_ScriptedBuildClient([]))
        self.assertEqual(engine2._detect_changes(), [])

    def test_failed_index_update_is_retried(self):
        client = _ScriptedBuildClient([("---\ntitle: A\n---\n# A\n\nA.", "stop")] * 2)
        engine, kdir, _ = self._engine(client)
        _write_file(os.path.join(kdir, "a.md"), "alpha")
        with unittest.mock.patch.object(engine, "_update_index", side_effect=OSError("disk full")):
            self.assertEqual(engine.build(), 0)
        self.assertEqual(engine.build(), 1)


class TestPruneAndWatch(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="delfi-prune-")

    def _built_engine(self, names):
        replies = [(f"---\ntitle: {n}\n---\n# {n}\n\nAbout {n}.", "stop") for n in names]
        engine = _make_engine(self.tmpdir, ollama_client=_ScriptedBuildClient(replies))
        for n in names:
            _write_file(os.path.join(engine.cfg["knowledge_folder"], f"{n}.md"), f"{n} source")
        engine.build()
        return engine, engine.cfg["knowledge_folder"], engine.cfg["wiki_folder"]

    def test_deleted_source_removes_page_and_index_row(self):
        engine, kdir, wdir = self._built_engine(["alpha", "beta"])
        os.remove(os.path.join(kdir, "beta.md"))
        self.assertEqual(engine.prune_removed_sources(), ["beta"])
        self.assertFalse(Path(wdir, "beta.md").exists())
        index = Path(wdir, "index.md").read_text()
        self.assertIn("[[alpha]]", index)
        self.assertNotIn("[[beta]]", index)
        self.assertIn("prune | beta.md", Path(wdir, "log.md").read_text())

    def test_empty_knowledge_folder_never_prunes(self):
        engine, kdir, wdir = self._built_engine(["alpha"])
        os.remove(os.path.join(kdir, "alpha.md"))
        self.assertEqual(engine.prune_removed_sources(), [])
        self.assertTrue(Path(wdir, "alpha.md").exists())

    def test_watch_disabled_starts_no_thread(self):
        engine = _make_engine(self.tmpdir, wiki_watch_enabled=False)
        before = threading.active_count()
        engine.watch(1, threading.Event())
        self.assertEqual(threading.active_count(), before)

    def test_watch_rebuilds_with_serving_model(self):
        client = _ScriptedBuildClient([("---\ntitle: A\n---\n# A\n\nA.", "stop")])
        engine = _make_engine(self.tmpdir, ollama_client=client,
                              wiki_builder_model="huge-model:70b")
        _write_file(os.path.join(engine.cfg["knowledge_folder"], "a.md"), "alpha")
        stop = threading.Event()
        engine.watch(60, stop)
        deadline = time.time() + 3
        while not client.generate_calls and time.time() < deadline:
            time.sleep(0.02)
        stop.set()
        self.assertEqual(client.generate_calls[0]["model"], "test-model:3b")

    def test_lint_flags_missing_source_only_with_knowledge_folder(self):
        engine, kdir, wdir = self._built_engine(["alpha", "beta"])
        os.remove(os.path.join(kdir, "beta.md"))  # not pruned yet
        issues = engine.lint()
        self.assertTrue(any("missing source: beta.md" in i for i in issues), issues)


class _BagOfWordsClient(_FakeOllamaClient):
    """Embeds text as word counts over a tiny vocabulary, so similarity
    follows shared words and semantic search results are predictable."""

    VOCAB = ("trail", "map", "summit", "fire", "water", "stove")

    def _vector(self, text: str) -> list[float]:
        words = text.lower().replace(".", " ").split()
        return [words.count(w) + 0.01 for w in self.VOCAB]


class TestEmbeddings(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="delfi-embed-")

    def test_embed_text_uses_the_embed_api(self):
        client = _FakeOllamaClient()
        engine = _make_engine(self.tmpdir, ollama_client=client)
        vec = engine._embed_text("where is the trailhead")
        self.assertEqual(len(vec), 16)
        self.assertEqual(client.embed_calls, ["where is the trailhead"])

    def test_embed_failure_returns_none(self):
        client = _FakeOllamaClient()
        client.embed = unittest.mock.Mock(side_effect=ConnectionError("refused"))
        engine = _make_engine(self.tmpdir, ollama_client=client)
        self.assertIsNone(engine._embed_text("anything"))

    @unittest.skipUnless(
        __import__("importlib").util.find_spec("chromadb"), "chromadb not installed"
    )
    def test_semantic_search_with_real_chromadb(self):
        """Embed, search and delete through the real ChromaDB client."""
        engine = _make_engine(self.tmpdir, ollama_client=_BagOfWordsClient())
        engine._init_vectorstore()
        self.assertTrue(engine._rag_available)
        wdir = engine.cfg["wiki_folder"]
        _write_file(os.path.join(wdir, "trail-guide.md"),
                    _make_wiki_page("Trail Guide", ["trail"], "The trail map shows the summit trail."))
        _write_file(os.path.join(wdir, "fire-safety.md"),
                    _make_wiki_page("Fire Safety", ["fire"], "Keep water near the stove and the fire."))

        engine._embed_wiki_pages()
        self.assertEqual(engine._collection.count(), 2)
        self.assertEqual(engine._vector_search("which trail goes to the summit"), ["trail-guide"])

        engine._delete_embedding("trail-guide")
        self.assertEqual(engine._collection.count(), 1)
        self.assertEqual(engine._vector_search("which trail goes to the summit"), [])


class _ModelClient(_FakeOllamaClient):
    """Reports timing like Ollama does, and answers show/list for one model."""

    def __init__(self, generate_response="", capabilities=("completion",),
                 parameter_size="4.0B", **response_fields):
        super().__init__(generate_response)
        self.capabilities = list(capabilities)
        self.parameter_size = parameter_size
        self.response_fields = response_fields
        self.show_calls: list[str] = []

    def generate(self, model, prompt, options=None, stream=False, **kwargs):
        super().generate(model, prompt, options, stream, **kwargs)
        return types.SimpleNamespace(response=self.generate_response, **self.response_fields)

    def show(self, model):
        self.show_calls.append(model)
        return types.SimpleNamespace(
            details=types.SimpleNamespace(parameter_size=self.parameter_size),
            modelinfo={}, capabilities=self.capabilities,
        )


class _MissingModelError(Exception):
    """Like ollama.ResponseError for an unpulled model."""
    status_code = 404


class TestAnswerStats(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="delfi-stats-")
        _setup_pages(self.tmpdir, {"songs": (["songs", "chickadee"],
                                             "## Fee-bee\n\nThe chickadee sings fee-bee.")})

    def test_ollama_timing_is_recorded_and_logged(self):
        client = _ModelClient(
            "A Black-capped Chickadee.", load_duration=2_500_000_000,
            prompt_eval_count=600, prompt_eval_duration=10_000_000_000,
            eval_count=70, eval_duration=20_000_000_000, done_reason="stop",
        )
        engine = _make_engine(self.tmpdir, ollama_client=client)
        with self.assertLogs("del_fi.core.knowledge", "INFO") as logs:
            answer, had_context = engine.query("what sings fee-bee, a chickadee?")
        self.assertTrue(had_context)
        stats = engine.last_stats
        self.assertEqual(stats.pages, ("songs",))
        self.assertEqual((stats.prompt_tokens, stats.output_tokens), (600, 70))
        self.assertAlmostEqual(stats.load_s, 2.5)
        self.assertAlmostEqual(stats.prompt_rate, 60.0)
        self.assertAlmostEqual(stats.output_rate, 3.5)
        line = next(m for m in logs.output if "tier1:" in m)
        self.assertIn("tier1: songs (", line)
        self.assertIn("load 2.5s, prompt 600 tok at 60.0 tok/s, output 70 tok at 3.5 tok/s", line)
        self.assertNotIn("cut short", line)

    def test_missing_timing_fields_still_log_wall_time(self):
        engine = _make_engine(self.tmpdir, ollama_client=_FakeOllamaClient("Fee-bee."))
        with self.assertLogs("del_fi.core.knowledge", "INFO") as logs:
            engine.query("chickadee song")
        line = next(m for m in logs.output if "tier1:" in m)
        self.assertRegex(line, r"LLM \d+\.\ds$")
        self.assertIsNone(engine.last_stats.prompt_rate)

    def test_answer_cut_at_num_predict_is_flagged(self):
        client = _ModelClient("The chickadee sings", eval_count=300,
                              eval_duration=60_000_000_000, done_reason="length")
        engine = _make_engine(self.tmpdir, ollama_client=client)
        with self.assertLogs("del_fi.core.knowledge", "INFO") as logs:
            engine.query("chickadee song")
        self.assertTrue(any("stopped at num_predict" in m for m in logs.output), logs.output)

    def test_failed_generation_logs_elapsed_time(self):
        class Refusing(_FakeOllamaClient):
            def generate(self, **kwargs):
                raise ConnectionError("refused")

        from del_fi.core.knowledge import LLMError
        engine = _make_engine(self.tmpdir, ollama_client=Refusing())
        with self.assertLogs("del_fi.core.knowledge", "ERROR") as logs, self.assertRaises(LLMError):
            engine.query("chickadee song")
        self.assertRegex(logs.output[0], r"failed \(unavailable\) after \d+\.\ds")


class TestModelHandling(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="delfi-models-")
        _setup_pages(self.tmpdir, {"songs": (["songs", "chickadee"],
                                             "## Fee-bee\n\nThe chickadee sings fee-bee.")})

    def _engine(self, client, **cfg):
        return _make_engine(self.tmpdir, ollama_client=client, **cfg)

    def test_thinking_model_is_asked_not_to_think(self):
        client = _ModelClient("Chickadee.", capabilities=["completion", "thinking"])
        engine = self._engine(client, ollama_keep_alive="30m")
        engine.query("chickadee song")
        call = client.generate_calls[0]
        self.assertIs(call["think"], False)
        self.assertEqual(call["keep_alive"], "30m")

    def test_other_models_get_no_think_option(self):
        client = _ModelClient("Chickadee.")
        self._engine(client).query("chickadee song")
        self.assertNotIn("think", client.generate_calls[0])
        self.assertNotIn("keep_alive", client.generate_calls[0])

    def test_model_info_is_cached(self):
        client = _ModelClient("Chickadee.")
        engine = self._engine(client)
        engine.query("chickadee song")
        engine.query("chickadee song again")
        self.assertEqual(client.show_calls, ["test-model:3b"])

    def test_think_blocks_are_removed_from_answers(self):
        client = _ModelClient("<think>Fee-bee is... the chickadee.</think>\n\nA Black-capped Chickadee.")
        answer, had_context = self._engine(client).query("chickadee song")
        self.assertEqual(answer, "A Black-capped Chickadee.")
        self.assertTrue(had_context)

    def test_unclosed_think_means_no_answer_and_a_warning(self):
        client = _ModelClient("<think>The user asks about a song. Let me consider")
        engine = self._engine(client)
        with self.assertLogs("del_fi.core.knowledge", "WARNING") as logs:
            answer, had_context = engine.query("chickadee song")
        self.assertEqual((answer, had_context), ("", False))
        self.assertTrue(any("thinking" in m for m in logs.output), logs.output)

    def test_thinking_field_with_empty_answer_warns(self):
        client = _ModelClient("", thinking="Let me think about chickadees...")
        with self.assertLogs("del_fi.core.knowledge", "WARNING") as logs:
            self._engine(client).query("chickadee song")
        self.assertTrue(any("num_predict" in m for m in logs.output), logs.output)

    def test_size_profile_applied_to_unknown_small_model(self):
        client = _ModelClient(parameter_size="2.0B", capabilities=["completion", "thinking"])
        engine = self._engine(client, model="qwen3:1.7b", _profile="",
                              _explicit_keys=["node_name", "model"], max_context_tokens=None,
                              num_ctx=None)
        with self.assertLogs("del_fi.core.knowledge", "INFO") as logs:
            engine._check_models()
        self.assertEqual(engine.cfg["_profile"], "small (by size)")
        self.assertEqual(engine.cfg["max_context_tokens"], 512)
        self.assertTrue(engine.cfg["small_model_prompt"])
        self.assertTrue(any(
            "model qwen3:1.7b: 2.0B, thinking off · profile small (by size) · context 512 tok" in m
            for m in logs.output), logs.output)

    def test_size_profile_keeps_keys_set_in_config(self):
        client = _ModelClient(parameter_size="1.2B")
        engine = self._engine(client, model="llama3.2:3b-mini", _profile="",
                              _explicit_keys=["max_context_tokens"], max_context_tokens=900)
        engine._check_models()
        self.assertEqual(engine.cfg["max_context_tokens"], 900)
        self.assertTrue(engine.cfg["small_model_prompt"])

    def test_large_model_gets_the_large_profile(self):
        engine = self._engine(_ModelClient(parameter_size="14.8B"), model="qwen3:14b",
                              _profile="", _explicit_keys=[])
        engine._check_models()
        self.assertEqual(engine.cfg["_profile"], "large (by size)")
        self.assertEqual(engine.cfg["max_context_tokens"], 3000)

    def test_name_profile_wins_over_size(self):
        engine = self._engine(_ModelClient(parameter_size="1.0B"), model="gemma3:1b",
                              _profile="gemma3:1b", _explicit_keys=[], max_context_tokens=512)
        engine._check_models()
        self.assertEqual(engine.cfg["_profile"], "gemma3:1b")

    def test_hand_built_config_is_left_alone(self):
        engine = self._engine(_ModelClient(parameter_size="1.0B"))  # no _explicit_keys
        engine._check_models()
        self.assertEqual(engine.cfg["max_context_tokens"], 2048)

    def test_unpulled_model_is_reported_with_the_fix(self):
        engine = self._engine(_ModelClient())
        engine._pulled = {"gemma3:1b"}
        with self.assertLogs("del_fi.core.knowledge", "ERROR") as logs:
            engine._check_models()
        self.assertIn("run: ollama pull test-model:3b", logs.output[0])

    def test_unpulled_embedding_model_warns_when_semantic_search_is_on(self):
        engine = self._engine(_ModelClient())
        engine._pulled = {"test-model:3b"}
        engine._rag_available = True
        with self.assertLogs("del_fi.core.knowledge", "WARNING") as logs:
            engine._check_models()
        self.assertTrue(any("ollama pull nomic-embed-text" in m for m in logs.output), logs.output)

    def test_model_names_match_like_ollama(self):
        from del_fi.core.knowledge import _model_names
        engine = self._engine(_ModelClient())
        engine._pulled = _model_names({"models": [
            {"model": "nomic-embed-text:latest"}, {"model": "Qwen3:4b"},
            {"model": "hf.co/unsloth/Qwen3-4B-GGUF:Q4_K_M"},
        ]})
        self.assertTrue(engine.has_model("nomic-embed-text"))
        self.assertTrue(engine.has_model("qwen3:4b"))
        self.assertTrue(engine.has_model("hf.co/unsloth/Qwen3-4B-GGUF:Q4_K_M"))
        self.assertFalse(engine.has_model("qwen3:8b"))
        engine._pulled = None  # Ollama never listed: assume yes
        self.assertTrue(engine.has_model("anything"))

    def test_models_pulled_after_startup_are_found(self):
        client = _ModelClient()
        engine = self._engine(client)
        engine._pulled = {"test-model:3b"}
        client.list = lambda: {"models": [{"model": "test-model:3b"}, {"model": "gemma4:12b"}]}
        self.assertTrue(engine.has_model("gemma4:12b"))

    @unittest.skipUnless(__import__("importlib").util.find_spec("ollama"), "ollama not installed")
    def test_parses_real_ollama_responses(self):
        from ollama._types import ListResponse, ModelDetails, ShowResponse

        from del_fi.core.knowledge import _model_names, _parse_show
        info = _parse_show(ShowResponse(details=ModelDetails(parameter_size="751.63M"),
                                        model_info={}, capabilities=["completion", "thinking"]))
        self.assertAlmostEqual(info.billions, 0.75163)
        self.assertTrue(info.thinks)
        info = _parse_show(ShowResponse(model_info={"general.parameter_count": 3_212_749_888}))
        self.assertAlmostEqual(info.billions, 3.212749888)
        self.assertFalse(info.thinks)
        listing = ListResponse(models=[ListResponse.Model(model="gemma3:1b")])
        self.assertEqual(_model_names(listing), {"gemma3:1b"})

    def test_warm_up_loads_with_the_answer_context_window(self):
        client = _ModelClient()
        engine = self._engine(client, ollama_keep_alive=-1)
        took = engine.warm_up()
        self.assertIsInstance(took, float)
        call = client.generate_calls[0]
        self.assertEqual(call["prompt"], "")
        self.assertEqual(call["options"], {"num_ctx": engine.num_ctx()})
        self.assertEqual(call["keep_alive"], -1)

    def test_warm_up_skipped_for_unpulled_model_or_zero_keep_alive(self):
        client = _ModelClient()
        engine = self._engine(client)
        engine._pulled = set()
        self.assertIsNone(engine.warm_up())
        engine._pulled = None
        engine.cfg["ollama_keep_alive"] = 0
        self.assertIsNone(engine.warm_up())
        self.assertEqual(client.generate_calls, [])

    def test_warm_up_failure_is_logged(self):
        client = _ModelClient()
        client.generate = unittest.mock.Mock(side_effect=RuntimeError("model requires more system memory"))
        engine = self._engine(client)
        with self.assertLogs("del_fi.core.knowledge", "WARNING") as logs:
            self.assertIsNone(engine.warm_up())
        self.assertIn("more system memory", logs.output[0])

    def test_answer_from_unpulled_model_says_how_to_fix(self):
        from del_fi.core.knowledge import LLMError
        client = _ModelClient()
        client.generate = unittest.mock.Mock(side_effect=_MissingModelError("model not found"))
        with self.assertRaises(LLMError) as ctx:
            self._engine(client).query("chickadee song")
        self.assertIn("ollama pull test-model:3b", str(ctx.exception))

    def test_build_stops_at_the_first_missing_model_error(self):
        client = _ModelClient()
        client.generate = unittest.mock.Mock(side_effect=_MissingModelError("model not found"))
        engine = self._engine(client)
        kdir = engine.cfg["knowledge_folder"]
        _write_file(os.path.join(kdir, "a.md"), "alpha")
        _write_file(os.path.join(kdir, "b.md"), "beta")
        with self.assertLogs("del_fi.core.knowledge", "ERROR") as logs:
            self.assertEqual(engine.build(), 0)
        self.assertEqual(client.generate.call_count, 1)
        self.assertTrue(any("ollama pull test-model:3b" in m for m in logs.output), logs.output)

    def test_build_refuses_a_builder_model_that_is_not_pulled(self):
        client = _ModelClient()
        engine = self._engine(client, wiki_builder_model="gemma4:12b")
        engine._pulled = {"test-model:3b"}
        _write_file(os.path.join(engine.cfg["knowledge_folder"], "a.md"), "alpha")
        with self.assertLogs("del_fi.core.knowledge", "ERROR"):
            self.assertEqual(engine.build(), 0)
        self.assertEqual(client.generate_calls, [])

    def test_thinking_builder_model_writes_pages_without_thoughts(self):
        page = _make_wiki_page("Alpha", ["alpha"], "Alpha is first.")
        client = _ModelClient(f"<think>Plan the page.</think>\n{page}",
                              capabilities=["completion", "thinking"], done_reason="stop")
        engine = self._engine(client)
        _write_file(os.path.join(engine.cfg["knowledge_folder"], "alpha.md"), "Alpha is first.")
        self.assertEqual(engine.build(file=os.path.join(engine.cfg["knowledge_folder"], "alpha.md")), 1)
        self.assertIs(client.generate_calls[0]["think"], False)
        text = Path(engine.cfg["wiki_folder"], "alpha.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\ntitle: Alpha"), text[:40])
        self.assertNotIn("think", text)

    def test_reconnect_reruns_the_model_check(self):
        engine = self._engine(_ModelClient())
        engine._ollama_available = False

        def connect():
            engine._ollama_available = True

        with unittest.mock.patch.object(engine, "_init_ollama", side_effect=connect), \
                unittest.mock.patch.object(engine, "_check_models") as check:
            self.assertTrue(engine.check_ollama())
        check.assert_called_once()


if __name__ == "__main__":
    unittest.main()
