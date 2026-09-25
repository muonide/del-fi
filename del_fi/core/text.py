"""Small text helpers shared by Del-Fi's keyword matchers."""

import re

STOP_WORDS = frozenset({
    "a", "an", "the", "is", "it", "in", "on", "at", "to", "for",
    "of", "and", "or", "not", "be", "are", "was", "were", "do",
    "does", "did", "has", "have", "had", "can", "could", "will",
    "would", "should", "may", "might", "i", "me", "my", "you",
    "your", "we", "our", "they", "them", "their", "what", "where",
    "when", "how", "who", "which", "that", "this", "there",
    "here", "with", "from", "about", "into", "if", "so", "than",
    "but", "just", "any", "some", "all", "no", "yes",
})

_NON_WORD = re.compile(r"[^\w\s]")


def tokenize(text: str) -> list[str]:
    """Lowercase, strip punctuation, drop stop words and 1-character tokens."""
    words = _NON_WORD.sub(" ", text.lower()).split()
    return [w for w in words if w not in STOP_WORDS and len(w) > 1]


def one_line(text: str) -> str:
    """Collapse all whitespace to single spaces and drop non-printable chars."""
    collapsed = " ".join(str(text).split())
    return "".join(ch for ch in collapsed if ch.isprintable())
