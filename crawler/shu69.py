"""Crawl raw Chinese chapters of a book from 69shuba (69书吧).

Why a browser: www.69shuba.com sits behind Cloudflare. Plain requests get a
403 "Just a moment..." page and even the clearance cookie is not reusable from
requests. We open the site once with undetected-chromedriver, then read every
chapter with an in-page fetch() - same origin, same TLS fingerprint, so the
challenge never fires. Verified: ~0.3s/chapter.

Verified endpoints (book 84642 = 重生：开局逮到高冷校花超市偷窃, 1002 chapters):
- TOC:   GET https://www.69shuba.com/book/{bid}/   (utf-8)
         div#catalog > ul > li[data-num] > a[href="/txt/{bid}/{cid}"]
- Meta:  GET https://www.69shuba.com/book/{bid}.htm (utf-8) -> title/author
- Page:  GET https://www.69shuba.com/txt/{bid}/{cid} (gbk)
         body: div.txtnav = h1 title + div.txtinfo + text separated by <br>
"""

import base64
import re
import time
import urllib.parse

from bs4 import BeautifulSoup

# undetected_chromedriver is imported lazily in Session.launch() so the parsing
# helpers below stay usable (and testable) without a browser installed.
uc = None

# uc's __del__ re-quits an already-dead driver and dumps a traceback at
# interpreter exit; swallow that so crawl logs stay readable.
_uc_del_patched = False


def _load_uc():
    global uc, _uc_del_patched
    if uc is None:
        import undetected_chromedriver as _uc

        if not _uc_del_patched:
            _orig_del = getattr(_uc.Chrome, "__del__", None)

            def _quiet_del(self, _orig=_orig_del):
                try:
                    if _orig:
                        _orig(self)
                except Exception:  # noqa: BLE001 - browser already gone
                    pass

            _uc.Chrome.__del__ = _quiet_del
            _uc_del_patched = True
        uc = _uc
    return uc


BASE = "https://www.69shuba.com"
CHROME_MAJOR = 153  # matches the installed Chrome; bump when Chrome updates

# Fetch the raw bytes in the page context and hand them back base64-encoded:
# chapter pages are gbk, and fetch().text() would decode them as utf-8.
_FETCH_JS = """
const url = arguments[0];
const done = arguments[arguments.length - 1];
fetch(url, {credentials: 'include'})
  .then(r => r.arrayBuffer())
  .then(buf => {
    const bytes = new Uint8Array(buf);
    let bin = '';
    const chunk = 0x8000;
    for (let i = 0; i < bytes.length; i += chunk) {
      bin += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
    }
    done('OK\\n' + btoa(bin));
  })
  .catch(e => done('ERR ' + e));
"""

_TEXT_JUNK = {"(本章完)", "本章完", "记住本站最新域名www。69shuba。com"}


def _norm(s):
    return re.sub(r"\s+", "", s)


def _title_key(s):
    """Compare a heading ignoring whitespace and punctuation.

    69shuba's <h1> and the first body line are the same heading written twice,
    but they drift: the site prefixes some <h1>s with the sequence number
    ("45.第44章 ...") and the body line may carry different decoration
    ("第50章 ...——", "第999章 ...·终", "第449章 你是1" vs body "我是-1").
    """
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", s)


def _decode(raw):
    """Decode response bytes using the charset declared in the HTML head."""
    m = re.search(rb"""charset=["']?([\w-]+)""", raw[:4000], re.I)
    enc = (m.group(1).decode("ascii", "ignore") if m else "utf-8").lower()
    if enc in ("gbk", "gb2312", "gb18030"):
        enc = "gb18030"
    try:
        return raw.decode(enc, "replace")
    except LookupError:
        return raw.decode("utf-8", "replace")


def parse_toc_html(html, bid):
    """Return [{ordernum,title,url}] from the /book/{bid}/ catalogue page."""
    soup = BeautifulSoup(html, "html.parser")
    box = soup.find("div", id="catalog")
    if box is None:
        if "Just a moment" in html:
            raise RuntimeError("cloudflare challenge on TOC")
        raise RuntimeError("no div#catalog on TOC page")
    chapters = []
    for li in box.find_all("li", attrs={"data-num": True}):
        a = li.find("a", href=True)
        if not a:
            continue
        url = a["href"].strip()
        if "/txt/" not in url:
            continue
        if url.startswith("/"):
            url = BASE + url
        chapters.append(
            {
                "ordernum": str(int(li["data-num"])),
                "title": a.get_text(strip=True),
                "url": url,
            }
        )
    if not chapters:
        raise RuntimeError("empty TOC")
    return chapters


def parse_meta_html(html):
    """Return (book_title, author) from the /book/{bid}.htm page."""
    soup = BeautifulSoup(html, "html.parser")
    book_title, author = "", ""
    h1 = soup.find("h1")
    if h1:
        book_title = h1.get_text(strip=True)
    if not book_title and soup.title:
        book_title = re.sub(r"[_\-].*$", "", soup.title.get_text(strip=True))
    m = re.search(r"作者[：:]\s*([^<\s|]+)", soup.get_text())
    if m:
        author = m.group(1)
    if not author:
        m = re.search(r"author\s*:\s*'([^']+)'", html)
        if m:
            author = m.group(1)
    return book_title, author


def parse_chapter_html(html):
    """Return (page_title, paragraphs) from a /txt/{bid}/{cid} page."""
    if "Just a moment" in html or "cf-browser-verification" in html:
        raise RuntimeError("cloudflare challenge on chapter")
    soup = BeautifulSoup(html, "html.parser")
    node = soup.find("div", class_="txtnav")
    if node is None:
        raise RuntimeError("no div.txtnav")
    h1 = node.find("h1")
    page_title = h1.get_text(strip=True) if h1 else ""
    if h1:
        h1.extract()
    # some <h1>s are prefixed with the catalogue sequence number: "45.第44章 ..."
    page_title = re.sub(r"^\s*\d+\s*[.、]\s*", "", page_title)
    for tag in node.find_all(["script", "style", "iframe", "noscript"]):
        tag.decompose()
    info = node.find("div", class_="txtinfo")
    if info:
        info.extract()
    for ad in node.find_all(id=re.compile(r"^(txtright|ads?|content_ad)$", re.I)):
        ad.extract()

    paras = []
    for chunk in re.split(r"<br\s*/?>", str(node), flags=re.I):
        text = BeautifulSoup(chunk, "html.parser").get_text()
        text = re.sub(r"[\s\u3000]+", " ", text).strip()
        if not text or text in _TEXT_JUNK:
            continue
        paras.append(text)
    # 69shuba repeats the chapter title as the first line of the body
    if page_title:
        key = _title_key(page_title)
        while paras and _title_key(paras[0]) == key:
            paras = paras[1:]
    if not paras:
        raise RuntimeError("empty chapter body")
    return page_title, paras


class Session:
    """A Chrome window parked on the book page, used as a same-origin proxy."""

    def __init__(self, headless=False):
        self.headless = headless
        self.driver = None

    def ensure(self, url):
        if self.driver is not None:
            try:
                self.driver.title
                return
            except Exception:  # noqa: BLE001 - dead session, relaunch below
                self.driver = None
        self.launch(url)

    def launch(self, url):
        uc = _load_uc()
        self.close()
        opts = uc.ChromeOptions()
        if self.headless:
            opts.add_argument("--headless=new")
        else:
            opts.add_argument("--start-minimized")
        opts.add_argument("--disable-gpu")
        self.driver = uc.Chrome(options=opts, version_main=CHROME_MAJOR)
        self.driver.get(url)
        for _ in range(30):
            time.sleep(2)
            if self.driver.title and "Just a moment" not in self.driver.title:
                return
        raise RuntimeError(f"cloudflare not cleared: {url}")

    def fetch_bytes(self, url, timeout=60):
        """GET url inside the browser, return raw bytes."""
        res = self.driver.execute_async_script(_FETCH_JS, url)
        if not isinstance(res, str) or not res.startswith("OK\n"):
            raise RuntimeError(f"in-page fetch failed {url}: {str(res)[:120]}")
        return base64.b64decode(res.split("\n", 1)[1])

    def close(self):
        if self.driver is not None:
            try:
                self.driver.quit()
            except Exception:  # noqa: BLE001 - already gone
                pass
            self.driver = None


def new_session(headless=False):
    return Session(headless=headless)


def fetch_toc(session, bid):
    """Return (book_title, author, chapters) for book id `bid`."""
    toc_url = f"{BASE}/book/{bid}/"
    session.ensure(toc_url)
    html = session.driver.page_source
    chapters = parse_toc_html(html, bid)

    meta_url = f"{BASE}/book/{bid}.htm"
    try:
        book_title, author = parse_meta_html(_decode(session.fetch_bytes(meta_url)))
    except Exception:  # noqa: BLE001 - meta is nice to have, TOC already works
        book_title, author = f"book-{bid}", ""
    if not book_title:
        book_title = f"book-{bid}"
    return book_title, author, chapters


def fetch_chapter(session, url, timeout=60):
    """Return (page_title, paragraphs).

    Fetching is retried with a fresh Chrome when the browser dies mid-book or
    Cloudflare re-asks for a challenge; parse errors are raised straight away
    (relaunching would not help them).
    """
    parts = urllib.parse.urlparse(url).path.split("/")  # /txt/{bid}/{cid}
    book_url = f"{BASE}/book/{parts[2]}/"
    last_err = None
    for attempt in range(3):
        try:
            session.ensure(book_url)  # relaunches Chrome if it is gone
            html = _decode(session.fetch_bytes(url, timeout=timeout))
            if "Just a moment" in html or "cf-browser-verification" in html:
                raise RuntimeError("cloudflare challenge on chapter")
            break
        except Exception as e:  # noqa: BLE001 - browser/network trouble only
            last_err = e
            session.close()  # drop the broken session, next loop relaunches
            time.sleep(1.5 * (attempt + 1))
    else:
        raise RuntimeError(f"could not fetch {url}: {last_err}")
    return parse_chapter_html(html)
