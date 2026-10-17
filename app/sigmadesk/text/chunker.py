"""Arabic-aware chunker with page provenance. Pure Python (no Odoo imports).

Input
-----
text   : the approved document text (possibly corrected by a reviewer).
pages  : OCR structure from the worker, ``[{"page": n, "lines": [{"text", "score"}]}]``
         (``raw.pages`` of the approved job). Optional.

Page mapping survives reviewer edits: approved text lines are aligned to OCR
lines with difflib; inserted/rewritten lines inherit the page of their aligned
or preceding OCR line. Without OCR pages, blank-line-separated blocks are
treated as pages (the worker joins pages with a blank line).

Output
------
list of dicts: content, page_start, page_end, char_start, char_end,
confidence (length-weighted OCR line score over scored lines, None if none),
min_score (weakest OCR line), unscored_ratio (share of characters without an
OCR score), normalized.
``text[char_start:char_end]`` is the chunk's span in the approved text
(content may differ only by collapsed whitespace).
"""
from __future__ import annotations

import difflib
import re
import unicodedata

# Sentence end: Arabic/Latin terminal punctuation followed by space/end.
_SENT_END = re.compile(r"[.!?؟؛…]+(?=\s|$)")
_WS = re.compile(r"\s+")
_TASHKEEL = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭ]")
_TATWEEL = "ـ"
_ALEF = re.compile(r"[إأآٱ]")


def normalize_for_search(text: str) -> str:
    """Search key: strip tashkeel/tatweel, unify alef forms, ى→ي, ة→ه, fold
    presentation forms, collapse whitespace. Never shown to users."""
    text = unicodedata.normalize("NFKC", text)
    text = _TASHKEEL.sub("", text).replace(_TATWEEL, "")
    text = _ALEF.sub("ا", text).replace("ى", "ي").replace("ة", "ه")
    return _WS.sub(" ", text).strip().lower()


def _norm_line(s: str) -> str:
    return _WS.sub(" ", s).strip()


# ----------------------------------------------------------------------
# 1. Line -> page/score mapping
# ----------------------------------------------------------------------
def map_lines(text: str, pages: list | None) -> list[tuple[int, int, int | None, float | None]]:
    """Return [(start, end, page, score)] for every non-empty line of ``text``
    (offsets into ``text``)."""
    spans = []
    pos = 0
    for raw_line in text.splitlines(keepends=True):
        content = raw_line.rstrip("\r\n")
        if content.strip():
            lead = len(content) - len(content.lstrip())
            spans.append((pos + lead, pos + len(content.rstrip()), content))
        pos += len(raw_line)

    if not spans:
        return []

    if pages:
        ocr = [
            (p.get("page"), _norm_line(l.get("text", "")), l.get("score"))
            for p in pages
            for l in (p.get("lines") or [])
            if _norm_line(l.get("text", ""))
        ]
        a = [o[1] for o in ocr]
        b = [_norm_line(s[2]) for s in spans]
        assigned: list[tuple[int | None, float | None]] = [(None, None)] * len(b)
        sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                for k in range(j2 - j1):
                    page, _, score = ocr[i1 + k]
                    assigned[j1 + k] = (page, score)
            elif tag == "replace":
                # proportional alignment of rewritten lines; corrected lines keep the
                # page but their OCR score no longer describes them (reviewed text)
                for k in range(j2 - j1):
                    src = i1 + min(i2 - i1 - 1, (k * (i2 - i1)) // max(1, j2 - j1))
                    assigned[j1 + k] = (ocr[src][0], None)
            # 'insert' handled below (inherit previous page); 'delete' ignored
        last_page = ocr[0][0] if ocr else None
        out = []
        for (start, end, _), (page, score) in zip(spans, assigned):
            if page is None:
                page = last_page
            last_page = page
            out.append((start, end, page, score))
        return out

    # Fallback: blank-line separated blocks are pages.
    out, page = [], 1
    prev_end = None
    for start, end, _ in spans:
        if prev_end is not None and text.count("\n", prev_end, start) >= 2:
            page += 1
        out.append((start, end, page, None))
        prev_end = end
    return out


# ----------------------------------------------------------------------
# 2. Sentence units with provenance
# ----------------------------------------------------------------------
def _sentences(text: str, line_map) -> list[dict]:
    """Split into sentences across line breaks. A sentence ends at terminal
    punctuation, at a page change, or at a blank line (paragraph break)."""
    units = []
    cur = None
    prev_end = None
    lengths = sorted(e - s for s, e, _, _ in line_map)
    median = lengths[len(lengths) // 2] if lengths else 0
    for idx, (start, end, page, score) in enumerate(line_map):
        paragraph_break = prev_end is not None and text.count("\n", prev_end, start) >= 2
        if cur and (paragraph_break or (page is not None and cur["page_end"] != page)):
            units.append(cur)
            cur = None
        line = text[start:end]
        cursor = 0
        for m in _SENT_END.finditer(line):
            seg_end = m.end()
            cur = _extend(cur, start + cursor, start + seg_end, page, score, text)
            units.append(cur)
            cur = None
            cursor = seg_end
            while cursor < len(line) and line[cursor].isspace():
                cursor += 1
        if cursor < len(line):
            cur = _extend(cur, start + cursor, end, page, score, text)
            # heading: a short whole line without terminal punctuation, followed by more text
            is_last = idx == len(line_map) - 1
            if cursor == 0 and not is_last and median and (end - start) < 0.6 * median:
                cur["heading"] = True
                units.append(cur)
                cur = None
        prev_end = end
    if cur:
        units.append(cur)
    return units


def _extend(cur, s, e, page, score, text):
    piece = text[s:e]
    weight = len(piece.strip())
    if cur is None:
        return {
            "start": s, "end": e, "page_start": page, "page_end": page,
            "w": weight if score is not None else 0,
            "ws": (score or 0.0) * weight if score is not None else 0.0,
            "uw": weight if score is None else 0,
            "min": score,
        }
    cur["end"] = e
    cur["page_end"] = page if page is not None else cur["page_end"]
    if score is not None:
        cur["w"] += weight
        cur["ws"] += score * weight
        cur["min"] = score if cur["min"] is None else min(cur["min"], score)
    else:
        cur["uw"] += weight
    return cur


# ----------------------------------------------------------------------
# 3. Packing
# ----------------------------------------------------------------------
def _split_long(unit: dict, text: str, limit: int) -> list[dict]:
    """Split an over-long unit. Real OCR often drops sentence-final punctuation,
    so prefer an OCR line break, then a word boundary, never mid-word."""
    out = []
    s, e = unit["start"], unit["end"]
    while e - s > limit:
        window_end = s + limit
        cut = text.rfind("\n", s + limit // 3, window_end)
        if cut <= s:
            cut = text.rfind(" ", s, window_end)
        if cut <= s:
            cut = window_end
        out.append({**unit, "start": s, "end": cut, "heading": False})
        s = cut
        while s < e and text[s].isspace():
            s += 1
    if s < e:
        out.append({**unit, "start": s, "end": e})
    return out


def chunk(text: str, pages: list | None = None, size: int = 900, overlap: int = 150) -> list[dict]:
    """Chunks are ~``size`` chars; a heading pulled forward can add up to one unit."""
    if size <= 0:
        raise ValueError("size must be positive")
    overlap = max(0, min(overlap, size // 2))
    text = text or ""
    units = []
    for u in _sentences(text, map_lines(text, pages)):
        units.extend(_split_long(u, text, size) if u["end"] - u["start"] > size else [u])
    if not units:
        return []

    chunks, i = [], 0
    while i < len(units):
        j = i
        length = 0
        while j < len(units):
            ulen = units[j]["end"] - units[j]["start"]
            if j > i and length + ulen > size:
                break
            # a heading starts a new chunk once the current one is reasonably full
            if j > i and units[j].get("heading") and length >= size * 0.4:
                break
            length += ulen + 1
            j += 1
        # never end a chunk on a heading: it belongs with the text that follows
        if j < len(units) and j - 1 > i and units[j - 1].get("heading"):
            j -= 1
        # a lone heading takes the next unit along, even past the soft size limit
        if j == i + 1 and units[i].get("heading") and j < len(units):
            j += 1
        group = units[i:j]
        chunks.append(_make_chunk(group, text))
        if j >= len(units):
            break
        # overlap: re-use trailing whole sentences totalling <= overlap chars
        back, k = 0, j
        if units[j].get("heading"):
            i = j  # never carry overlap across a heading
            continue
        while k - 1 > i and back + (units[k - 1]["end"] - units[k - 1]["start"]) <= overlap:
            k -= 1
            back += units[k]["end"] - units[k]["start"]
        i = k if k > i else j
    return chunks


def _make_chunk(group: list[dict], text: str) -> dict:
    start, end = group[0]["start"], group[-1]["end"]
    content = _WS.sub(" ", " ".join(text[u["start"]:u["end"]].strip() for u in group)).strip()
    w = sum(u["w"] for u in group)
    pages = [p for u in group for p in (u["page_start"], u["page_end"]) if p is not None]
    return {
        "content": content,
        "page_start": min(pages) if pages else None,
        "page_end": max(pages) if pages else None,
        "char_start": start,
        "char_end": end,
        "confidence": round(sum(u["ws"] for u in group) / w, 4) if w else None,
        "min_score": min((u["min"] for u in group if u["min"] is not None), default=None),
        # share of the chunk's characters with no OCR score (reviewer-corrected or no page data)
        "unscored_ratio": round(sum(u["uw"] for u in group) / max(1, w + sum(u["uw"] for u in group)), 4),
        "normalized": normalize_for_search(content),
    }
