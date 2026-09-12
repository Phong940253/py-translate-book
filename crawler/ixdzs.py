"""Crawl raw Chinese chapters of a book from ixdzs mirror.

ixdzs re-hosts fanqie books as plain HTML (no font obfuscation), e.g.
book 546610 == fanqie 7296670623839816740
(反派：迎娶盲人未婚妻，疯狂恩爱).

Verified endpoints:
- TOC:  POST https://ixdzs8.com/novel/clist/  data={bid} -> {"rs":200,"data":[{ctype,title,ordernum}]}
         chapter URL = /read/{bid}/p{ordernum}.html
- Page: GET /read/{bid}/p{N}.html ; first visit may return a JS challenge
         ("let token = ...") -> follow with ?challenge={token} once per session.
- Body: article.page-content > section > p  (clean simplified Chinese)
"""

import re
import time

import requests
from bs4 import BeautifulSoup

BASE = "https://ixdzs8.com"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

_CHALLENGE_RE = re.compile(r'let token = "([^"]+)"')

# ixdzs template artifacts, not story text.
_JUNK_PARAS = {"app2;", "chaptererror;"}


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
    """GET with one-shot JS-challenge bypass (?challenge=token)."""
    r = session.get(url, timeout=timeout)
    m = _CHALLENGE_RE.search(r.text)
    if m:
        time.sleep(0.5)
        r = session.get(
            url + "?challenge=" + m.group(1),
            timeout=timeout,
            headers={"Referer": url},
        )
    r.raise_for_status()
    return r


def fetch_toc(session, bid):
    """Return (book_title, author, chapters) where chapters=[{ordernum,title,url}]."""
    # Warm up session cookies (handles challenge if present).
    get(session, f"{BASE}/read/{bid}/")
    r = session.post(
        f"{BASE}/novel/clist/",
        data={"bid": str(bid)},
        timeout=20,
        headers={
            "Origin": BASE,
            "Referer": f"{BASE}/read/{bid}/",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        },
    )
    r.raise_for_status()
    payload = r.json()
    if payload.get("rs") != 200:
        raise RuntimeError(f"clist failed: {r.text[:300]}")
    chapters = []
    for item in payload["data"]:
        if str(item.get("ctype")) == "1":  # volume header, not a chapter
            continue
        ordernum = str(item["ordernum"])
        chapters.append(
            {
                "ordernum": ordernum,
                "title": item["title"],
                "url": f"{BASE}/read/{bid}/p{ordernum}.html",
            }
        )
    info = get(session, f"{BASE}/read/{bid}/")
    soup = BeautifulSoup(info.text, "html.parser")
    h1 = soup.find("h1")
    book_title = h1.get_text(strip=True) if h1 else f"book-{bid}"
    author = ""
    author_link = soup.find("a", class_="bauthor")
    if author_link:
        author = author_link.get_text(strip=True)
    if not author:
        m = re.search(r"作者[:：]\s*([^\s_]+)", soup.get_text())
        if m:
            author = m.group(1)
    return book_title, author, chapters


def fetch_chapter(session, url, timeout=20):
    """Return (page_title, paragraphs). Raises on failure."""
    r = get(session, url, timeout=timeout)
    soup = BeautifulSoup(r.text, "html.parser")
    h1 = soup.find("h1", class_="page-d-name")
    page_title = h1.get_text(strip=True) if h1 else ""
    article = soup.find("article", class_="page-content")
    if article is None:
        raise RuntimeError(f"no article found: {url}")
    section = article.find("section") or article
    paras = []
    for p in section.find_all("p"):
        text = p.get_text().strip()
        if not text:
            continue
        if text.strip().lower() in _JUNK_PARAS:
            continue
        paras.append(text)
    # ixdzs repeats the chapter title as the first <p> on most pages
    if paras and page_title and _norm(paras[0]) == _norm(page_title):
        paras = paras[1:]
    if not paras:
        raise RuntimeError(f"empty chapter body: {url}")
    return page_title, paras
