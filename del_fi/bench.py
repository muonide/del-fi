"""--bench: time Tier 1 answers on this machine, with this config.

Asks the knowledge base each question directly (no radio, no cache, no
sensor facts) and prints where the time went: prompt and output tokens,
their speed, and how many mesh messages each answer takes. Run it with
different --model values to choose a model for your hardware.
"""

import statistics
import sys
import textwrap
from pathlib import Path

from del_fi.core.formatter import format_response
from del_fi.core.knowledge import LLMError, WikiEngine

DEFAULT_QUESTION_COUNT = 5


def load_questions(path: str) -> list[str]:
    """One question per line; blank lines and # comments are skipped."""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]


def default_questions(wiki: WikiEngine) -> list[str]:
    """A question per wiki topic, for a quick run without a questions file."""
    return [f"Tell me about {topic.lower()}" for topic in wiki.get_topics()[:DEFAULT_QUESTION_COUNT]]


def run(cfg: dict, questions_path: str = "", out=sys.stdout) -> int:
    """Run the benchmark. Returns the process exit code."""
    def say(text: str = "") -> None:
        print(text, file=out, flush=True)

    wiki = WikiEngine(cfg)
    model = cfg["model"]
    if not wiki.available:
        say(f"ERROR: Ollama is not reachable at {cfg['ollama_host']}. Start it and try again.")
        return 1
    if not wiki.has_model(model):
        say(f"ERROR: model {model!r} is not pulled. Run: ollama pull {model}")
        return 1
    if not wiki.wiki_available:
        say("ERROR: wiki/ is empty. Run: python main.py --build-wiki")
        return 1

    if questions_path:
        questions = load_questions(questions_path)
        source = Path(questions_path).name
    else:
        questions = default_questions(wiki)
        source = "wiki topics"
    if not questions:
        say(f"ERROR: no questions in {questions_path or 'the wiki index'}")
        return 1

    info = wiki.model_info(model)
    about = [info.parameter_size] if info and info.parameter_size else []
    if info and info.thinks:
        about.append("thinking off")
    say(f"{cfg['node_name']} bench · {model}" + (f" ({', '.join(about)})" if about else "")
        + f" · profile {cfg.get('_profile') or 'default'}")
    say(f"context {wiki._context_tokens()} tok · num_ctx {wiki.num_ctx()} · "
        f"num_predict {wiki._num_predict()} · {len(questions)} questions from {source}")

    say(f"loading {model}...")
    loaded = wiki.warm_up()
    say(f"  loaded in {loaded:.1f}s" if loaded is not None
        else "  could not preload it; the first answer includes loading time")

    results = []
    for n, question in enumerate(questions, 1):
        say()
        say(f"[{n}/{len(questions)}] {question}")
        wiki.last_stats = None
        try:
            answer, had_context = wiki.query(question)
        except LLMError as e:
            say(f"  error ({e.kind}): {e}")
            continue
        stats = wiki.last_stats
        if stats is None:
            say("  no matching pages: nothing sent to the model")
            continue
        results.append(stats)
        say("  " + _timing_line(stats, answer, cfg["max_response_bytes"]))
        text = answer if had_context else "(declined: the model said it didn't know)"
        for line in textwrap.wrap(text, 76):
            say(f"  │ {line}")

    say()
    say(_summary(results, len(questions)))
    return 0


def _timing_line(stats, answer: str, max_bytes: int) -> str:
    parts = [f"{stats.retrieve_s + stats.llm_s:.1f}s"]
    for label, tokens, rate in (("prompt", stats.prompt_tokens, stats.prompt_rate),
                                ("output", stats.output_tokens, stats.output_rate)):
        if tokens is not None:
            parts.append(f"{label} {tokens} tok" + (f" at {rate:.1f} tok/s" if rate else ""))
    if stats.load_s and stats.load_s >= 1:
        parts.append(f"incl. {stats.load_s:.1f}s loading")
    if answer:
        parts.append(f"{len(format_response(answer, max_bytes=max_bytes)[1])} msg(s)")
    if stats.done_reason == "length":
        parts.append("stopped at num_predict")
    return " · ".join(parts)


def _summary(results: list, asked: int) -> str:
    if not results:
        return f"{asked} questions · none reached the model"
    totals = [s.retrieve_s + s.llm_s for s in results]
    slowest = max(range(len(totals)), key=totals.__getitem__)
    lines = [
        f"{len(results)} of {asked} answered by the model · median {statistics.median(totals):.1f}s"
        f" · mean {statistics.mean(totals):.1f}s · slowest {totals[slowest]:.1f}s"
    ]
    rates = []
    for label, tokens, seconds in (
        ("prompt", [s.prompt_tokens for s in results], [s.prompt_s for s in results]),
        ("output", [s.output_tokens for s in results], [s.output_s for s in results]),
    ):
        pairs = [(t, d) for t, d in zip(tokens, seconds) if t and d]
        if pairs:
            rates.append(f"{label} {sum(t for t, _ in pairs) / sum(d for _, d in pairs):.1f} tok/s")
    cut = sum(1 for s in results if s.done_reason == "length")
    if cut:
        rates.append(f"{cut} stopped at num_predict")
    if rates:
        lines.append(" · ".join(rates))
    return "\n".join(lines)
