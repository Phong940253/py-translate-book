"""Crawl raw Chinese chapters of a book from fanqienovel.com (番茄小说).

Fanqie is the official platform that hosts, e.g., book 7478055015341378585 ==
娇娇青梅一红眼，野戾太子爷轻哄 (229 chapters, 完本).

Verified endpoints:
- TOC:   GET /page/{bid} -> window.__INITIAL_STATE__ JSON with
         page.bookName, page.author and page.chapterListWithVolume
         (list of volumes; every chapter listed, needPay=0 on free books).
- Page:  GET /reader/{itemId} -> __INITIAL_STATE__.reader.chapterData
         {title, needPay, content}.

Two text problems, two solutions:
1. Logged-out reader pages only carry a ~5-paragraph preview of the chapter
   (content far shorter than chapterWordNumber). The old
   /api/reader/full endpoint is dead. Whenever somebody archived the whole
   book on the Internet Archive (items named fanqienovel-{bid} with a .txt
   dump), we use that as the chapter text - it is fanqie's own plain
   simplified text, split on the TOC titles. The reader page stays as a
   fallback for books without an archive dump.
2. The reader page content is obfuscated: ~360 of the most frequent
   characters (plus some ASCII letters/digits) are replaced by PUA
   codepoints (U+E3E8-U+E55B) drawn from a subset of Source Han Sans. The
   codepoint -> character table lives next to this module in
   fanqie_pua_map.json (recovered by glyph bitmap-matching against Noto
   Sans CJK and verified against known chapter text, contexts and glyph
   advance widths). If fanqie rotates its font, unknown codepoints are
   reported as warnings and the map has to be re-derived.
"""

import html as htmllib
import json
import os
import re
import time
from urllib.parse import quote

import requests

BASE = "https://fanqienovel.com"
ARCHIVE_META = "https://archive.org/metadata/fanqienovel-{bid}"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

_STATE_RE = re.compile(r"window\.__INITIAL_STATE__\s*=\s*")
ARCHIVE_MIN_BYTES = 100_000  # a full book dump is >1 MB; smaller = broken node
_PUA_RE = re.compile(r"[\ue000-\uf8ff]")
_P_RE = re.compile(r"<p[^>]*>([\s\S]*?)</p>")
_TAG_RE = re.compile(r"<[^>]+>")
# source titles run together ("第1章重生"); add the missing space after 第N.
# A few fanqie titles omit 章 entirely ("第38这是他亲手养大的女孩"), and some
# already have it spaced ("第77章 沈梨初...") - _norm_title handles all three
# without a backtracking optional group (that produced "第77 章 ...").
_NUM_RE = re.compile(r"^第\d+")

_MAP = None
_unknown_pua = set()
_archive = {}  # item_id -> [paragraphs] from the Internet Archive dump


def _norm_title(title):
    m = _NUM_RE.match(title)
    if not m:
        return title  # 番外 / if线 / other non-numbered titles stay untouched
    head, rest = m.group(0), title[m.end() :]
    if rest.startswith("章"):
        head, rest = head + "章", rest[1:]
    if rest and not rest[0].isspace():
        return head + " " + rest
    return title


def _load_map():
    global _MAP
    if _MAP is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fanqie_pua_map.json")
        with open(path, encoding="utf-8") as f:
            _MAP = {int(k, 16): v for k, v in json.load(f).items()}
    return _MAP


def new_session():
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
    )
    return s


def get(session, url, timeout=20):
    r = session.get(url, timeout=timeout)
    r.encoding = "utf-8"
    r.raise_for_status()
    return r


def extract_state(html):
    """Parse the JSON of window.__INITIAL_STATE__ = {...} out of a page."""
    m = _STATE_RE.search(html)
    if not m:
        raise RuntimeError("no __INITIAL_STATE__ in page")
    obj, _ = json.JSONDecoder().raw_decode(html[html.index("{", m.start()) :])
    return obj


def decode_content(content_html):
    """Replace PUA codepoints and split the chapter HTML into paragraphs."""
    cmap = _load_map()

    def repl(m):
        cp = ord(m.group(0))
        ch = cmap.get(cp)
        if ch is None:
            if cp not in _unknown_pua:
                _unknown_pua.add(cp)
                print(f"[fanqie] WARNING: unknown PUA {hex(cp)} - font map outdated?")
            return m.group(0)
        return ch

    text = _PUA_RE.sub(repl, content_html)
    paras = []
    for p in _P_RE.findall(text):
        p = _TAG_RE.sub("", p)
        p = htmllib.unescape(p).replace("\xa0", " ").strip()
        if p:
            paras.append(p)
    return paras


def split_archive_text(text, titles):
    """Split a full-book plain-text dump into {title: [paragraphs]}.

    Chapter headers are the TOC title lines; the dump keeps the source form
    ("第1章重生") while chapters carry the normalized one ("第1章 重生"), so
    lines are matched with whitespace removed. Anything before the first
    header (uploader preamble) is ignored.
    """

    def compact(s):
        return re.sub(r"\s+", "", s)

    canon = {compact(t): t for t in titles}
    sections = {}
    current = None
    for line in text.splitlines():
        s = line.strip()
        key = canon.get(compact(s)) if s else None
        if key is not None:
            current = key
            sections[current] = []
        elif s and current is not None:
            sections[current].append(s)
    return sections


def load_archive_dump(session, bid, chapters):
    """Best-effort: fill _archive from the Internet Archive dump of the book.

    The archive.org CDN rotates nodes and individual nodes do fail, so the
    download is retried and finally attempted straight from the primary
    storage node (metadata d1).
    """
    meta = None
    for attempt in range(3):
        try:
            meta = get(session, ARCHIVE_META.format(bid=bid), timeout=30).json()
            break
        except Exception as e:
            print(f"[fanqie] archive.org metadata failed ({e!r}), retrying")
            time.sleep(2 * (attempt + 1))
    if not meta:
        print("[fanqie] archive.org dump unavailable; using reader pages")
        return
    names = [f["name"] for f in meta.get("files", []) if f.get("name", "").endswith(".txt")]
    if not names:
        return
    urls = [f"https://archive.org/download/fanqienovel-{bid}/{quote(names[0])}"]
    if meta.get("d1"):
        urls.append(f"https://{meta['d1']}/0/items/fanqienovel-{bid}/{quote(names[0])}")
    text = None
    for url in urls:
        for attempt in range(3):
            try:
                body = get(session, url, timeout=180).text
                if len(body) >= ARCHIVE_MIN_BYTES:  # a full book dump is >1 MB
                    text = body
                    break
                print(f"[fanqie] archive dump suspiciously small ({len(body)} bytes)")
            except Exception as e:
                print(f"[fanqie] archive download failed ({e!r})")
            time.sleep(2 * (attempt + 1))
        if text is not None:
            break
    if text is None:
        print("[fanqie] archive.org dump unavailable; using reader pages")
        return
    sections = split_archive_text(text, [c["title"] for c in chapters])
    for ch in chapters:
        body = sections.get(ch["title"])
        if body:
            item_id = ch["url"].rstrip("/").rsplit("/", 1)[-1]
            _archive[item_id] = body
    print(f"[fanqie] archive.org full text: {len(_archive)}/{len(chapters)} chapters")


def parse_toc_html(html):
    """Return (book_title, author, chapters) from the /page/{bid} page."""
    state = extract_state(html)
    page = state.get("page", {})
    book_title = page.get("bookName") or "fanqie-book"
    author = page.get("author") or ""
    rows = []
    for vol in page.get("chapterListWithVolume", []):
        for ch in vol:
            rows.append(
                {
                    "order": int(ch.get("realChapterOrder") or 0),
                    "title": _norm_title(ch.get("title", "")),
                    "item_id": str(ch["itemId"]),
                    "need_pay": int(ch.get("needPay") or 0),
                }
            )
    rows.sort(key=lambda r: (r["order"], r["item_id"]))
    chapters = [
        {"ordernum": str(r["order"]), "title": r["title"], "url": f"{BASE}/reader/{r['item_id']}"}
        for r in rows
    ]
    return book_title, author, chapters


def parse_chapter_html(html):
    """Return (title, paragraphs) for one /reader/{itemId} page."""
    state = extract_state(html)
    data = state.get("reader", {}).get("chapterData") or {}
    if not data:
        raise RuntimeError("no chapterData in reader page")
    if data.get("needPay"):
        raise RuntimeError(f"chapter is paid/locked (itemId {data.get('itemId')})")
    title = _norm_title(data.get("title", ""))
    paras = decode_content(data.get("content") or "")
    if not paras:
        raise RuntimeError(f"empty chapter body (itemId {data.get('itemId')})")
    words = sum(len(p) for p in paras)
    expect = int(data.get("chapterWordNumber") or 0)
    if expect and words < expect * 0.5:
        print(
            f"[fanqie] WARNING: {title} looks truncated ({words} vs {expect} chars) "
            "- logged-out preview? Check the archive.org dump."
        )
    return title, paras


def fetch_toc(session, bid):
    """Return (book_title, author, chapters)."""
    r = get(session, f"{BASE}/page/{bid}")
    book_title, author, chapters = parse_toc_html(r.text)
    if not chapters:
        raise RuntimeError(f"no chapters parsed for book {bid}")
    _archive.clear()
    load_archive_dump(session, bid, chapters)
    return book_title, author, chapters


def fetch_chapter(session, url, timeout=20):
    """Return (page_title, paragraphs)."""
    item_id = url.rstrip("/").rsplit("/", 1)[-1]
    cached = _archive.get(item_id)
    if cached is not None:
        # full text already in hand - no reader request needed
        return None, list(cached)
    r = get(session, url, timeout=timeout)
    return parse_chapter_html(r.text)
