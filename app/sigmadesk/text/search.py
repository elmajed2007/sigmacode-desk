"""Arabic lexical search helpers. Pure Python (no Odoo imports).

Index text and query use the same pipeline:
    normalize_for_search -> tokenize -> (surface token, light stem)
and PostgreSQL's ``simple`` text-search config (no extension needed). The
built-in ``arabic`` snowball stemmer leaves forms like «والاعتماد» untouched,
so a light stemmer (prefix/suffix stripping in the spirit of Light10) is used.

Tokens are restricted to letters/digits, so query strings can never inject
tsquery syntax.
"""
from __future__ import annotations

import re

from .chunker import normalize_for_search

_TOKEN = re.compile(r"[0-9a-zء-ي٠-٩]+")

# After normalization (ا ي ه forms). Ordered longest-first.
_PREFIXES = ("وبال", "وكال", "فبال", "وال", "بال", "كال", "فال", "لل", "ال")
_SUFFIXES = ("هما", "كما", "تين", "تان", "ات", "ون", "ين", "ان", "يه", "ها", "هم", "هن", "كم", "نا", "ه", "ي")

STOPWORDS = frozenset(
    """في من الي علي عن ان او ثم مع هذا هذه ذلك تلك التي الذي الذين اللتي كان كانت
    يكون تكون قد لقد لا لم لن ما ماذا متي اين كيف هل بين حتي اذا عند كل بعض غير
    و او هو هي هم هن انا نحن انت انتم ايضا فقط به بها له لها منه منها عليه عليها
    الا لكن بل اي""".split()
)


def light_stem(token: str) -> str:
    """Strip one definite-article prefix (or one conjunction/preposition) and one
    suffix, keeping at least 3 letters. Digits/Latin pass through."""
    if not token or not ("ء" <= token[0] <= "ي"):
        return token
    t = token
    for p in _PREFIXES:
        if t.startswith(p) and len(t) - len(p) >= 3:
            t = t[len(p):]
            break
    else:
        # only the conjunction و; stripping single ب/ل/ك/ف mangles roots (برنامج→رنامج)
        if t[0] == "و" and len(t) - 1 >= 4:
            t = t[1:]
    for s in _SUFFIXES:
        if t.endswith(s) and len(t) - len(s) >= 3:
            t = t[: -len(s)]
            break
    return t


def tokens(text: str) -> list[str]:
    return _TOKEN.findall(normalize_for_search(text or ""))


def index_text(text: str) -> str:
    """Space-separated surface tokens + stems (deduplicated per position)."""
    out = []
    for tok in tokens(text):
        out.append(tok)
        stem = light_stem(tok)
        if stem != tok:
            out.append(stem)
    return " ".join(out)


def query_terms(query: str) -> list[str]:
    """Distinct stems of the meaningful query words (stopwords removed)."""
    seen, terms = set(), []
    for tok in tokens(query):
        if tok in STOPWORDS or len(tok) < 2:
            continue
        stem = light_stem(tok)
        if stem not in seen:
            seen.add(stem)
            terms.append(stem)
    return terms


def tsquery(query: str) -> str | None:
    """OR-query over stems plus the original surface words (exact forms rank
    higher) for to_tsquery('simple', ...); None when nothing searchable."""
    terms = list(query_terms(query))
    for tok in tokens(query):
        if tok not in STOPWORDS and len(tok) >= 2 and tok not in terms:
            terms.append(tok)
    return " | ".join(terms) if terms else None


def coverage(query: str, text: str) -> float:
    """Share of distinct query stems present in ``text`` (0..1)."""
    terms = query_terms(query)
    if not terms:
        return 0.0
    have = set(index_text(text).split())
    return sum(1 for t in terms if t in have) / len(terms)


def snippet(query: str, text: str, radius: int = 18) -> str:
    """~2*radius words around the densest cluster of query matches."""
    words = (text or "").split()
    if not words:
        return ""
    terms = set(query_terms(query))
    hits = [i for i, w in enumerate(words) if any(t in terms for t in (index_text(w).split() or [""]))]
    if not hits:
        return " ".join(words[: 2 * radius]) + (" …" if len(words) > 2 * radius else "")
    best = max(hits, key=lambda h: sum(1 for x in hits if abs(x - h) <= radius))
    a, b = max(0, best - radius), min(len(words), best + radius)
    return ("… " if a else "") + " ".join(words[a:b]) + (" …" if b < len(words) else "")
