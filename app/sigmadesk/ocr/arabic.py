"""Arabic post-processing for OCR output (ported from the SigmaCode OCR worker).

PaddleOCR's Arabic recognizer emits text in *visual* order and under-emits word
spaces. These helpers restore logical order (auto-detected per document), fold
presentation forms and fix Arabic punctuation spacing.
"""
from __future__ import annotations

import html
import re
import unicodedata

_ARABIC_CHAR = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]")
_PRESENTATION_FORMS = re.compile(r"[ﭐ-﷿ﹰ-﻿]+")
_LATIN_TOKEN = re.compile(r"[A-Za-z0-9]+(?:[ .:/%+*,-]+[A-Za-z0-9]+)*")
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([:.،؛!?؟])")
_NO_SPACE_AFTER_PUNCT = re.compile(r"([:،؛])(?=[؀-ۿ])")


def pred_reverse(text: str) -> str:
    """Visual -> logical order. Latin/digit runs stay intact. Involution."""
    parts, i = [], 0
    while i < len(text):
        m = _LATIN_TOKEN.match(text, i)
        if m:
            parts.append(m.group(0))
            i = m.end()
        else:
            parts.append(text[i])
            i += 1
    return "".join(parts[::-1])


def normalize_arabic(text: str) -> str:
    """Fold Arabic presentation forms (e.g. U+FEFB) to base letters."""
    return _PRESENTATION_FORMS.sub(lambda m: unicodedata.normalize("NFKC", m.group(0)), text)


def tidy_punctuation(text: str) -> str:
    text = _SPACE_BEFORE_PUNCT.sub(r"\1", text)
    return _NO_SPACE_AFTER_PUNCT.sub(r"\1 ", text)


def is_rtl(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    return sum(1 for c in letters if _ARABIC_CHAR.match(c)) / len(letters) >= 0.5


def needs_reverse(texts: list[str]) -> bool:
    """Correct Arabic has many words starting with «ال»; reversed text ends with «لا»."""
    words = [w for t in texts for w in normalize_arabic(t).split()]
    forward = sum(1 for w in words if len(w) > 2 and w.startswith("ال"))
    backward = sum(1 for w in words if len(w) > 2 and w.endswith("لا"))
    return backward > forward


def arabic_letter_count(text: str) -> int:
    return sum(1 for c in text if _ARABIC_CHAR.match(c))


def looks_merged(text: str, min_len: int) -> bool:
    return any(arabic_letter_count(tok) >= min_len for tok in text.split())


def letters(text: str) -> str:
    return "".join(c for c in text if c.isalnum())


def group_lines(items: list[dict]) -> list[list[dict]]:
    """Group recognized boxes into visual lines by vertical overlap."""
    items = sorted(items, key=lambda it: (it["bbox"][1] + it["bbox"][3]) / 2)
    lines: list[list[dict]] = []
    for it in items:
        y1, y2 = it["bbox"][1], it["bbox"][3]
        if lines:
            last = lines[-1]
            ly1 = min(i["bbox"][1] for i in last)
            ly2 = max(i["bbox"][3] for i in last)
            overlap = min(y2, ly2) - max(y1, ly1)
            if overlap > 0.5 * min(y2 - y1, ly2 - ly1):
                last.append(it)
                continue
        lines.append([it])
    return lines


def order_line(line: list[dict]) -> list[dict]:
    rtl = is_rtl(" ".join(i["text"] for i in line))
    return sorted(line, key=lambda i: i["bbox"][0], reverse=rtl)


def build_pages(raw_pages: list[dict], reverse_mode: str = "auto") -> dict:
    """raw_pages: [{"page": n, "items": [{"text", "score", "bbox"}]}] -> pages with ordered lines.

    Returns dict(pages=[{page, lines=[{text, score, bbox, rtl}], text}], confidence, reverse_applied)."""
    all_texts = [it["text"] for p in raw_pages for it in p["items"]]
    if reverse_mode == "on":
        reverse = True
    elif reverse_mode == "off":
        reverse = False
    else:
        reverse = needs_reverse(all_texts)
    pages_out, weighted, total = [], 0.0, 0
    for page in raw_pages:
        for it in page["items"]:
            if reverse and is_rtl(it["text"]):
                it["text"] = pred_reverse(it["text"])
            it["text"] = normalize_arabic(it["text"])
            if is_rtl(it["text"]):
                it["text"] = tidy_punctuation(it["text"])
        lines = []
        for line in group_lines(page["items"]):
            ordered = order_line(line)
            text = " ".join(i["text"] for i in ordered)
            n = max(1, sum(len(i["text"]) for i in ordered))
            score = sum(i["score"] * len(i["text"]) for i in ordered) / n
            bbox = [min(i["bbox"][0] for i in ordered), min(i["bbox"][1] for i in ordered),
                    max(i["bbox"][2] for i in ordered), max(i["bbox"][3] for i in ordered)]
            lines.append({"text": text, "score": round(score, 4), "bbox": bbox, "rtl": is_rtl(text)})
            weighted += score * len(text)
            total += len(text)
        pages_out.append({"page": page["page"], "lines": lines,
                          "text": "\n".join(l["text"] for l in lines)})
    return {"pages": pages_out, "confidence": round(weighted / total, 4) if total else 0.0,
            "reverse_applied": reverse}


def pages_html(pages: list[dict]) -> str:
    out = []
    for p in pages:
        body = "".join('<p dir="%s">%s</p>' % ("rtl" if l["rtl"] else "ltr", html.escape(l["text"]))
                       for l in p["lines"])
        out.append('<section data-page="%d">%s</section>' % (p["page"], body))
    return "".join(out)
