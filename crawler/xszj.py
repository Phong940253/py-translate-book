"""Crawl raw Chinese chapters of a book from xszj.org (小说之家).

xszj re-hosts finished fanqie/qidian books as plain UTF-8 HTML (no JS
challenge, no font obfuscation), e.g. book 336003 ==
好烦啊，高冷校花天天倒追我 (89 chapters, 完本).

Verified endpoints:
- TOC:   GET https://xszj.org/b/{bid}/cs/1 -> full chapter list in
         div#content_1, links /b/{bid}/c/{chapter_id} (ascending ids
         follow chapter order).
- Meta:  GET https://xszj.org/b/{bid} -> h1 title + author link /z/...
- Page:  GET /b/{bid}/c/{chapter_id} ; a chapter may be split across
         several screens, the "下一页" link points at ?page={N}.
- Body:  div#booktxt > p (clean simplified Chinese, site prefixes
         paragraphs with ideographic spaces).
"""

import re
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE = "https://xszj.org"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

_MAX_CHAPTER_PAGES = 20  # safety: no real chapter is longer than this

# xszj splits a long chapter across screens mid-sentence and marks the cut
# with a lone "：" paragraph. Also site furniture, not story text.
_JUNK_PARAS = {"：", ":", "本站所有小说均搜集于互联网免费分享"}

# Anti-copy watermark the site appends to random paragraphs: "M.Ι." (Latin M,
# ASCII dots, Greek iota U+0399), a fullwidth "Ｅ", or a bare ASCII period.
# Chinese text never ends a sentence with those, so trailing runs are safe to
# strip.
_WATERMARK_RE = re.compile(r"(?:M\.Ι\.|[.Ｅ])+\s*$")

# a paragraph that ends a sentence (used to decide whether a screen boundary
# is a mid-sentence cut that must be stitched back together)
_SENTENCE_END_RE = re.compile(r"[。！？…”〕]$")
_SEQ_PREFIX_RE = re.compile(r"^第\d+章\s+")


def _norm(s):
    return re.sub(r"\s+", "", s)


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


def parse_toc_html(html, bid):
    """Return chapter list [{ordernum,title,url}] from the /cs/1 page."""
    soup = BeautifulSoup(html, "html.parser")
    seen = set()
    rows = []
    for a in soup.find_all("a", href=True):
        m = re.match(rf"^/b/{re.escape(str(bid))}/c/(\d+)$", a["href"])
        if not m or a.get("rel") != ["chapter"]:
            continue
        cid = m.group(1)
        if cid in seen:
            continue
        dd = a.find("dd")
        title = dd.get_text(strip=True) if dd else ""
        # dd is "第N章 {title}" — drop the site's own sequence prefix only when
        # the real title already carries a "第xx章" of its own.
        m2 = _SEQ_PREFIX_RE.match(title)
        if m2 and _SEQ_PREFIX_RE.match(title[m2.end() :]):
            title = title[m2.end() :]
        rows.append((int(cid), {"cid": cid, "title": title}))
        seen.add(cid)
    rows.sort(key=lambda r: r[0])  # chapter ids ascend with chapter order
    chapters = []
    for i, (_, row) in enumerate(rows, start=1):
        chapters.append(
            {
                "ordernum": str(i),
                "title": row["title"],
                "url": f"{BASE}/b/{bid}/c/{row['cid']}",
            }
        )
    return chapters


def parse_meta_html(html, bid):
    """Return (book_title, author) from the book page."""
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.find("h1")
    book_title = h1.get_text(strip=True) if h1 else f"book-{bid}"
    author = ""
    info = soup.find(id="info")
    if info:
        p = info.find("a", href=re.compile(r"^/z/"))
        if p:
            author = p.get_text(strip=True)
    return book_title, author


def parse_chapter_page(html):
    """Return ``(paras, next_url, cut)`` for one screen of a chapter.

    ``cut`` is True when the screen ends with the lone "：" marker — the
    site truncated a sentence here and the next screen continues it.
    """
    soup = BeautifulSoup(html, "html.parser")
    body = soup.find(id="booktxt") or soup.find(id="content")
    paras = []
    cut = False
    if body is not None:
        for p in body.find_all("p"):
            text = p.get_text().strip()
            if not text:
                continue
            if text == "：" or text == ":":
                cut = True      # page-boundary marker, not story text
                continue
            text = _WATERMARK_RE.sub("", text).strip()  # anti-copy junk
            if not text or text in _JUNK_PARAS:
                continue
            paras.append(text)
    nxt = None
    for a in soup.find_all("a", href=True):
        if a.get_text(strip=True) == "下一页" and a["href"].startswith("/"):
            nxt = a["href"]
            break
    return paras, nxt, cut


def chapter_title_from_html(html):
    """Return the clean chapter title from <title> ("第01章 xxx-书名...").

    The tag reads "{chapter title}-{book title}第{N}章({x}/{y})-小说之家";
    everything after the first dash is site furniture.
    """
    m = re.search(r"<title>([^<]+)</title>", html)
    if not m:
        return ""
    return m.group(1).split("-")[0].strip()


def fetch_toc(session, bid):
    """Return (book_title, author, chapters)."""
    r = get(session, f"{BASE}/b/{bid}/cs/1")
    chapters = parse_toc_html(r.text, bid)
    if not chapters:
        raise RuntimeError(f"no chapters parsed for book {bid}")
    info = get(session, f"{BASE}/b/{bid}")
    book_title, author = parse_meta_html(info.text, bid)
    return book_title, author, chapters


def fetch_chapter(session, url, timeout=20):
    """Return (page_title, paragraphs). Follows ?page=N screens."""
    r = get(session, url, timeout=timeout)
    title = chapter_title_from_html(r.text)
    page_paras, nxt, cut = parse_chapter_page(r.text)
    paras = list(page_paras)
    visited = {url}
    pages = 1
    while nxt and pages < _MAX_CHAPTER_PAGES:
        next_url = urljoin(BASE, nxt)
        if next_url in visited:
            break
        visited.add(next_url)
        time.sleep(0.4)  # be gentle with the extra screens
        r = get(session, next_url, timeout=timeout)
        page_paras, nxt, next_cut = parse_chapter_page(r.text)
        # The site splits mid-sentence and marks the cut with a lone "：";
        # stitch the two halves back into one paragraph.
        if cut and paras and page_paras and not _SENTENCE_END_RE.search(paras[-1]):
            paras[-1] = paras[-1] + page_paras[0]
            page_paras = page_paras[1:]
        paras.extend(page_paras)
        cut = next_cut
        pages += 1
    if not paras:
        raise RuntimeError(f"empty chapter body: {url}")
    return title, paras
