"""Shared Hán-Việt calque detection (word-by-word Sino-Vietnamese output).

A calque "translates" a chunk by reading every Chinese character as its
Sino-Vietnamese sound while keeping Chinese word order (的 -> đích, 就 ->
tựu, 我 -> ngã used as grammar particles).  It contains no Chinese
characters at all, so the echo/residue guards accept it -- these markers,
which never occur standalone in real Vietnamese prose, catch it instead.
Lookaheads exclude the few legitimate compounds (đích thân, vấn đề,
khởi hành, hòa bình, chi phí, ...).

The Translator guard (``translator.translator``) and offline verify
scripts both import from here, so a chapter checked outside a job can
never drift from what the job itself rejects.

Usage::

    from translator.calque import detect_hanviet_calque, find_calque_markers

    detect_hanviet_calque(source_chapter, translated_chapter)
    # -> ['càn', 'đích', ...] when the output must be rejected, [] otherwise
    find_calque_markers(translated_chapter)
    # -> every marker occurrence, for reporting even below the thresholds
"""

import re

# Tag stripping lives here so the module stays importable on its own
# (no circular import with translator.translator, which imports us).
HTML_TAG_PATTERN = re.compile(r"</?[A-Za-z][^>]*>|<![^>]*>|<\?[^>]*\?>")
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

CALQUE_MARKER_RE = re.compile(
    r"\b(?:"
    r"đích(?!\s+(?:thân|danh|thực))"
    r"|tựu|trứ|liễu|bả|nại|dã|tùng|tiếu|khán|càn|đáo"
    r"|hòa(?!\s+(?:bình|hợp|tan|nhạc|hòa|vui|quyện))"
    r"|nhi(?!\s+(?:đồng|liệt|hội|tử))"
    r"|tha(?!\s+(?:thứ|hương|biệt))"
    r"|vấn(?!\s+(?:đề|nhục))"
    r"|khởi(?!\s+(?:phong|hành|động|điểm|nghiệp))"
    r"|chi(?!\s+(?:phí|tiêu|phần|hội|cơ))"
    r")\b",
    re.IGNORECASE,
)
# A chunk is flagged only when the markers pile up: a good translation may
# brush one of them ("đích thân", a surname "Liễu") but never four hits
# across two different markers.
CALQUE_MIN_HITS = 4
CALQUE_MIN_DISTINCT = 2


def find_calque_markers(text: str) -> list[str]:
    """Every marker occurrence in *text* (HTML tags stripped), in order.

    Useful for reporting what a text contains even when it stays below the
    rejection thresholds.
    """
    if not text:
        return []
    return CALQUE_MARKER_RE.findall(HTML_TAG_PATTERN.sub("", text))


def detect_hanviet_calque(source_text: str, output_text: str) -> list[str]:
    """Marker words proving *output_text* is a word-by-word Hán-Việt calque.

    Only Chinese-source chunks are checked (the markers are Vietnamese
    readings of Chinese function words, so they cannot appear from any
    other source), and both thresholds must hold: a good translation may
    brush one marker ("đích thân", a surname "Liễu") but never four hits
    across two different markers.  Returns the sorted marker set when the
    chunk must be rejected, [] when it is acceptable.
    """
    if not source_text or not output_text:
        return []
    if not _CJK_RE.search(HTML_TAG_PATTERN.sub("", source_text)):
        return []                          # not a Chinese source at all
    hits = find_calque_markers(output_text)
    if len(hits) < CALQUE_MIN_HITS or len(set(hits)) < CALQUE_MIN_DISTINCT:
        return []
    return sorted(set(hits))
