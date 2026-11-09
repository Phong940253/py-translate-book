"""Shared Hán-Việt calque detection (word-by-word Sino-Vietnamese output).

A calque "translates" a chunk by reading every Chinese character as its
Sino-Vietnamese sound while keeping Chinese word order (的 -> đích, 就 ->
tựu, 我 -> ngã used as grammar particles).  It contains no Chinese
characters at all, so the echo/residue guards accept it -- these markers,
which never occur standalone in real Vietnamese prose, catch it instead.

The whitelist in ``_MARKER_SPECS`` was built by scanning all 318 chapters
of a fully translated book: every legitimate Vietnamese compound that
shares a marker syllable (mục đích, chi tiết, ôn hòa, chu đáo, phỏng
vấn, ...) is excluded, everything else stays flagged.  Text is
NFC-normalized before matching because the book contains decomposed
diacritics ("bản thân" written as "bản" + combining circumflex) that
would otherwise match the bare marker.

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
import unicodedata

# Tag stripping lives here so the module stays importable on its own
# (no circular import with translator.translator, which imports us).
HTML_TAG_PATTERN = re.compile(r"</?[A-Za-z][^>]*>|<![^>]*>|<\?[^>]*\?>")
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

# Legitimate compounds discovered by scanning the whole translated book,
# per marker: (marker, words BEFORE it, words AFTER it).  A marker preceded
# by "behind" is the second syllable of the compound (mục đích); followed
# by an "ahead" word it is the first (chi tiết).  Only whole words count,
# matched case-insensitively; a lookbehind needs a single \\s (Python
# lookbehind must be fixed-width), the lookahead allows \\s+ like before.
_MARKER_SPECS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    # đích: mục đích, đến đích, đích đến (destination), đích thị (exactly);
    # the rest (Gia đích trách, hậu đích kỷ, Vi đích chủ, ...) is genuine 的
    # calque.
    ("đích", ("mục", "đến"), ("thân", "danh", "thực", "thị", "đến")),
    # tựu: thành tựu, tựu trường; everything else is genuine 就 calque.
    ("tựu", ("thành",), ("trường",)),
    # trứ: no standalone Vietnamese word -- genuine 着 calque stays flagged.
    ("trứ", (), ()),
    # liễu: kết/liễu (finish), lộ liễu (obvious), roi/gậy/cội liễu (willow);
    # the rest is genuine 了 calque.
    ("liễu", ("kết", "lộ", "roi", "gậy", "cội"), ()),
    # bả: bả vai (shoulder); the rest is genuine 把 calque.
    ("bả", (), ("vai",)),
    # nại: nhẫn nại (endure); vô nại is genuine 无奈.
    ("nại", ("nhẫn",), ()),
    # dã: hoang/dân dã, dã ngoại (field trip); ngã dã / đích dã thị are
    # genuine 也 calque, "dã hoa" (野花 -> should be "hoa dại") stays flagged.
    ("dã", ("hoang", "dân"), ("ngoại",)),
    # tùng: phục/tiệc tùng (obey / revelry), tùng tùng (sound, either beat).
    ("tùng", ("phục", "tiệc", "tùng"), ("tùng",)),
    # tiếu: hủ tiếu (noodle soup), đàm tiếu (ridicule).
    ("tiếu", ("hủ", "đàm"), ()),
    # khán: khán giả (audience), khán đài (stand); the rest is 看 calque.
    ("khán", (), ("giả", "đài")),
    # càn: càn quét/quấy (sweep / pester), càn khôn (heaven and earth),
    # làm càn (act wildly).
    ("càn", ("làm",), ("quét", "quấy", "khôn")),
    # khởi: khởi đầu/sắc/tố/xướng + the old phong/hành/động/điểm/nghiệp
    # exclusions; khấm/hứng/phấn khởi (perk up).
    ("khởi", ("khấm", "hứng", "phấn"),
             ("phong", "hành", "động", "điểm", "nghiệp",
              "đầu", "sắc", "tố", "xướng")),
    # nhi: thiếu/nam nhi (children / young man), đồng/liệt/hội/tử nhi.
    ("nhi", ("thiếu", "nam"), ("đồng", "liệt", "hội", "tử")),
    # vấn: chất/phỏng/tự/tư/nghi/vương/truy/thẩm/học/cố vấn.
    ("vấn", ("chất", "phỏng", "tự", "tư", "nghi", "vương", "truy",
             "thẩm", "học", "cố"), ("đề", "nhục")),
    # hòa: ôn/điều/hài/hiền/bất/giao/bão/tiểu/nhân/làm hòa,
    # hòa nhập/thuận/vi/giải/lẫn/vào/nhã/đồng/mình/cùng/hoãn, dĩ hòa vi.
    ("hòa", ("ôn", "điều", "hài", "hiền", "bất", "giao", "bão", "tiểu",
             "nhân", "làm"),
            ("bình", "hợp", "tan", "nhạc", "hòa", "vui", "quyện",
             "nhập", "thuận", "vi", "giải", "lẫn", "vào", "nhã",
             "đồng", "mình", "cùng", "hoãn", "với")),
    # tha: buông/xin/tạm/định/nên/thướt/không tha (spare/forgive),
    # tha cho/hồ/thiết/lỗi.
    ("tha", ("buông", "xin", "tạm", "định", "nên", "thướt", "không"),
            ("thứ", "hương", "biệt", "cho", "hồ", "thiết", "lỗi")),
    # chi: chi tiết/nhánh/bằng/phối/li/chít/tiền/trả, huống/hèn/tứ/nói chi.
    ("chi", ("huống", "hèn", "tứ", "nói"),
            ("phí", "tiêu", "phần", "hội", "cơ", "tiết", "nhánh",
             "bằng", "phối", "li", "chít", "tiền", "trả")),
    # chu/kín/thấu/độc đáo (careful / discreet / thorough / unique);
    # lai đáo / tiên đáo / vãng đáo are genuine 到 calque.
    ("đáo", ("chu", "kín", "thấu", "độc"), ()),
)


def _build_calque_pattern() -> re.Pattern[str]:
    """Compose the marker regex from ``_MARKER_SPECS`` (see module docs)."""
    branches = []
    for marker, behind, ahead in _MARKER_SPECS:
        branch = "".join(r"(?<!%s\s)" % re.escape(w) for w in behind)
        branch += re.escape(marker)
        if ahead:
            branch += r"(?!\s+(?:%s))" % "|".join(
                re.escape(w) for w in ahead
            )
        branches.append(branch)
    return re.compile(r"\b(?:%s)\b" % "|".join(branches), re.IGNORECASE)


CALQUE_MARKER_RE = _build_calque_pattern()
# A chunk is flagged only when the markers pile up: a good translation may
# brush one of them ("đích thân", a surname "Liễu") but never four hits
# across two different markers.
CALQUE_MIN_HITS = 4
CALQUE_MIN_DISTINCT = 2


def _visible(text: str) -> str:
    """Visible text of *text*, tags stripped and NFC-normalized.

    NFC composes the decomposed diacritics the book contains ("bản thân"
    written as "bản" + combining circumflex), which would otherwise match
    the bare marker syllable ("tha", "chi").
    """
    return unicodedata.normalize("NFC", HTML_TAG_PATTERN.sub("", text or ""))


def find_calque_markers(text: str) -> list[str]:
    """Every marker occurrence in *text*, in order.

    Useful for reporting what a text contains even when it stays below the
    rejection thresholds.
    """
    if not text:
        return []
    return CALQUE_MARKER_RE.findall(_visible(text))


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
    if not _CJK_RE.search(_visible(source_text)):
        return []                          # not a Chinese source at all
    hits = find_calque_markers(output_text)
    if len(hits) < CALQUE_MIN_HITS or len(set(hits)) < CALQUE_MIN_DISTINCT:
        return []
    return sorted(set(hits))
