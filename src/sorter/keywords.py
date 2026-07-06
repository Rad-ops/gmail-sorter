"""Word-boundary keyword matching used by classification and ad scoring.

Substring matching was the original matcher and produced real misclassifications
("exam" matched "example.com", "class" matched "classification"). The helpers
here apply \\b boundaries to word-like keywords and fall back to escaped
substring matching for keywords containing punctuation, where \\b does not
behave around non-word characters.
"""

from __future__ import annotations

import re


def contains_any(text: str, keywords: list[str]) -> list[str]:
    """Substring keyword match (legacy).

    Kept for the few places where a literal substring is genuinely intended.
    Classification and ad scoring use :func:`keyword_hits` instead.
    """

    lowered = text.lower()
    return [keyword for keyword in keywords if keyword in lowered]


_WORDLIKE_RE = re.compile(r"^[a-z0-9]+(?:[\s'][a-z0-9]+)*[a-z0-9]$|^[a-z0-9]$")
_KEYWORD_CACHE: dict[tuple[str, ...], re.Pattern[str]] = {}


def _keyword_to_pattern(keyword: str) -> str:
    """One keyword -> a regex alternation piece with boundary when word-like."""

    if _WORDLIKE_RE.match(keyword):
        return rf"\b{re.escape(keyword)}\b"
    return re.escape(keyword)


def compile_keywords(keywords: list[str]) -> re.Pattern[str]:
    """Compile a keyword list into one fast alternation regex with boundaries."""

    key = tuple(sorted(keywords))
    cached = _KEYWORD_CACHE.get(key)
    if cached is not None:
        return cached
    pieces = [_keyword_to_pattern(kw) for kw in key if kw]
    pattern = re.compile("|".join(pieces) or "(?!)", re.IGNORECASE)
    _KEYWORD_CACHE[key] = pattern
    return pattern


def keyword_hits(text: str, keywords: list[str]) -> list[str]:
    """Return the keywords present in text using word-boundary matching.

    Word-like keywords get \\b boundaries; keywords containing punctuation are
    matched as escaped substrings. Each keyword is reported once, in input
    order, so callers can build readable reason strings from the hits.
    """

    if not text or not keywords:
        return []
    lowered = text.lower()
    return [keyword for keyword in keywords if re.search(_keyword_to_pattern(keyword), lowered)]


def regex_hits(text: str, patterns: list[str]) -> list[str]:
    """Return the regex patterns that match text (case-insensitive)."""

    lowered = text.lower()
    return [pattern for pattern in patterns if re.search(pattern, lowered)]
