"""Word-level diff for the current translation chunk (web UI monitor).

Produces a structured line+token diff so the UI can render a "standard" diff with
added/removed words highlighted, without pulling in a client-side diff library.
Only stdlib (``re``, ``difflib``) is used.
"""
import re
from difflib import SequenceMatcher

_TAG = r"</?[A-Za-z][^>]*>"
_TAG_RE = re.compile(_TAG, re.IGNORECASE)
_TOKEN_RE = re.compile(rf"{_TAG}|\s+|[^\s<]+")
_SPAN_RE = re.compile(r"<span\b[^>]*>", re.IGNORECASE)

_BLOCK_SPLIT = re.compile(
    r"(</p>|</div>|</section>|</li>|</h[1-6]>|<br\s*/?>)",
    re.IGNORECASE,
)


def tokenize_words(text):
    """Split text into word-level tokens; HTML tags are kept as single tokens."""
    return _TOKEN_RE.findall(text or "")


def _is_word_token(tok):
    stripped = tok.strip()
    return stripped != "" and not stripped.startswith("<")


def split_html_lines(text):
    """Split an HTML chunk into logical lines on block-tag boundaries.

    The delimiter is kept attached to the preceding segment so each line is a
    self-contained (tagged) fragment that aligns well in a line diff.
    """
    if not text:
        return []
    parts = _BLOCK_SPLIT.split(text)
    lines = []
    for i, piece in enumerate(parts):
        if i % 2 == 1:  # delimiter (captured group)
            if lines:
                lines[-1] = lines[-1] + piece
            else:
                lines.append(piece)
        else:  # content segment
            lines.append(piece)
    return [ln for ln in lines if ln != ""] or [""]


def _token_diff(src_line, tgt_line):
    """Word-level diff of a single replaced line -> list of {op, text} parts."""
    sm = SequenceMatcher(None, tokenize_words(src_line), tokenize_words(tgt_line))
    parts = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            for t in tokenize_words(src_line)[i1:i2]:
                parts.append({"op": "eq", "text": t})
        elif op == "delete":
            for t in tokenize_words(src_line)[i1:i2]:
                parts.append({"op": "del", "text": t})
        elif op == "insert":
            for t in tokenize_words(tgt_line)[j1:j2]:
                parts.append({"op": "ins", "text": t})
        else:  # replace
            for t in tokenize_words(src_line)[i1:i2]:
                parts.append({"op": "del", "text": t})
            for t in tokenize_words(tgt_line)[j1:j2]:
                parts.append({"op": "ins", "text": t})
    return parts


def diff_chunk(source, translated):
    """Return a structured word-level diff between source and translated chunks.

    Result: ``{"lines": [...], "added_words": int, "removed_words": int}`` where
    each line is ``{"kind": "equal"|"del"|"ins"|"change", "parts":[{op, text}]}``.
    """
    src_lines = split_html_lines(source)
    tgt_lines = split_html_lines(translated)
    sm = SequenceMatcher(None, src_lines, tgt_lines)
    lines = []
    added_words = 0
    removed_words = 0

    def _count_words(line):
        return sum(1 for t in tokenize_words(line) if _is_word_token(t))

    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            for ln in src_lines[i1:i2]:
                lines.append({"kind": "equal", "parts": [{"op": "eq", "text": ln}]})
        elif op == "delete":
            for ln in src_lines[i1:i2]:
                lines.append({"kind": "del", "parts": [{"op": "del", "text": ln}]})
                removed_words += _count_words(ln)
        elif op == "insert":
            for ln in tgt_lines[j1:j2]:
                lines.append({"kind": "ins", "parts": [{"op": "ins", "text": ln}]})
                added_words += _count_words(ln)
        else:  # replace
            if (i2 - i1) == 1 and (j2 - j1) == 1:
                parts = _token_diff(src_lines[i1], tgt_lines[j1])
                lines.append({"kind": "change", "parts": parts})
                for p in parts:
                    if p["op"] == "ins" and _is_word_token(p["text"]):
                        added_words += 1
                    elif p["op"] == "del" and _is_word_token(p["text"]):
                        removed_words += 1
            else:
                for ln in src_lines[i1:i2]:
                    lines.append({"kind": "del", "parts": [{"op": "del", "text": ln}]})
                    removed_words += _count_words(ln)
                for ln in tgt_lines[j1:j2]:
                    lines.append({"kind": "ins", "parts": [{"op": "ins", "text": ln}]})
                    added_words += _count_words(ln)

    return {"lines": lines, "added_words": added_words, "removed_words": removed_words}


def _tag_token(tag):
    """Normalize an HTML tag into a structure signature token (text stripped).

    Examples: ``<p>`` -> ``<p>``, ``</p>`` -> ``</p>``, ``<br/>`` -> ``<br>``,
    ``<span class="koboSpan" id="kobo.146.2">`` -> ``<span#kobo.146.2>``,
    ``<div class="sec">`` -> ``<div.sec>``.
    """
    name_m = re.match(r"</?([A-Za-z][A-Za-z0-9]*)", tag)
    name = name_m.group(1).lower() if name_m else "?"
    if tag.lstrip().startswith("</"):
        return f"</{name}>"
    id_m = re.search(r'\bid="([^"]*)"', tag)
    if id_m:
        return f"<{name}#{id_m.group(1)}>"
    cls_m = re.search(r'\bclass="([^"]*)"', tag)
    if cls_m:
        return f"<{name}.{cls_m.group(1)}>"
    return f"<{name}>"


def structure_signature(html):
    """Ordered list of normalized tag tokens for a chunk (text ignored)."""
    return [_tag_token(t) for t in _TAG_RE.findall(html or "")]


def _span_ids(html):
    """Return ``id`` values of every ``<span class="...koboSpan...">`` in html."""
    ids = []
    for span in _SPAN_RE.findall(html or ""):
        cls_m = re.search(r'\bclass="([^"]*)"', span)
        if cls_m and "koboSpan" in cls_m.group(1):
            id_m = re.search(r'\bid="([^"]*)"', span)
            if id_m:
                ids.append(id_m.group(1))
    return ids


def chunk_structure(source, translated):
    """Compare the HTML structure / koboSpan coverage of source vs translated.

    Returns ``{"same", "tag_diff", "coverage", "source_tags", "translated_tags"}``.
    ``tag_diff`` lists only structural divergences (``del`` = tag only in source,
    ``ins`` = tag only in translated); equal tags are omitted to reduce noise.
    ``coverage.missing`` are koboSpan ids present in source but dropped in the
    translation -- exactly the kind of drift that makes a chunk fail the
    "HTML structure changed" guard.
    """
    sig_s = structure_signature(source)
    sig_t = structure_signature(translated)
    sm = SequenceMatcher(None, sig_s, sig_t)
    tag_diff = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            continue
        for t in sig_s[i1:i2]:
            tag_diff.append({"op": "del", "tag": t})
        for t in sig_t[j1:j2]:
            tag_diff.append({"op": "ins", "tag": t})

    src_ids = _span_ids(source)
    tgt_ids = _span_ids(translated)
    missing = sorted(set(src_ids) - set(tgt_ids))
    extra = sorted(set(tgt_ids) - set(src_ids))

    same = (not tag_diff) and (not missing) and (not extra)
    return {
        "same": same,
        "tag_diff": tag_diff,
        "coverage": {
            "missing": missing,
            "extra": extra,
            "total_source": len(src_ids),
            "total_translated": len(tgt_ids),
        },
        "source_tags": len(sig_s),
        "translated_tags": len(sig_t),
    }
