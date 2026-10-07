"""EPUB reader backend: lazy book/chapter loading, sanitized XHTML for the
reader iframe, and the local TTS engine.

Serves the ``/read`` page:

* ``load_book`` reads only ``container.xml`` + the OPF plus a cheap regex scan
  per spine document (title, emptiness, character count) — a 1000-chapter book
  opens in ~0.15 s instead of parsing every document up front. Chapter XHTML is
  parsed on demand (nh3 → BeautifulSoup) and cached (LRU).
* ``BookDoc.doc_html`` returns a self-contained document for the chapter:
  URLs rewritten to ``/api/reader/res`` / ``/api/reader/doc``, dangerous markup
  removed by nh3 (html5ever semantics → the browser builds the same DOM), a
  strict CSP and an injected style sheet. The client loads it in a sandboxed
  same-origin iframe and keeps DOM access for highlighting and CFI positions.
* ``TTSEngine`` wraps VieNeu-TTS (local, CPU/ONNX). It is loaded lazily in a
  background thread so the first page load never blocks, and synthesis is
  serialized behind a lock (one request at a time; the reader prefetches the
  next paragraph while the current one plays).
"""

import io
import json
import os
import re
import tempfile
import threading
import time
import urllib.parse
import warnings
import wave
import zipfile
from collections import OrderedDict
from html import unescape as html_unescape

import nh3
from bs4 import BeautifulSoup, Tag
from bs4 import XMLParsedAsHTMLWarning

# The NCX/XHTML navigation files are XML; html.parser handles them fine (we only
# read structure) but warns loudly. lxml itself is available for the OPF.
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

# Items the reader treats as "blocks": they get their own element in the DOM,
# can be clicked/jumped to, and are spoken as one TTS unit.
_BLOCK_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "blockquote", "pre", "li"}
# nh3 flattens <head> into the top level, so head-ish tags must be skipped
# explicitly or the chapter <title> shows up as a readable paragraph.
_SKIP_TAGS = {"script", "style", "noscript", "nav", "head", "title", "meta", "link"}
_IMG_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp")

# Cache: at most a few books parsed at once (a novel is a few MB of text).
_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()
_CACHE_MAX = 4
# Parsed chapters kept per book (items + rendered HTML).
_CH_CACHE_MAX = 8
# Plain chapter texts kept per book for search (a whole novel fits easily).
_PLAIN_MAX = 4096

# Endpoints the sanitizer rewrites in-chapter URLs to. Must match app.py.
_RES_URL = "/api/reader/res"
_DOC_URL = "/api/reader/doc"

# Served inside the chapter page AND as a response header: the iframe runs
# sandboxed without allow-scripts, this blocks external requests on top of that.
CHAPTER_CSP = (
    "default-src 'none'; img-src 'self' data: blob:; font-src 'self' data:; "
    "style-src 'self' 'unsafe-inline'; script-src 'none'; connect-src 'none'; "
    "media-src 'self' blob:; object-src 'none'; frame-src 'none'; "
    "form-action 'none'; base-uri 'none'"
)

# nh3 allowlist. Anything missing is unwrapped (text kept) except
# clean_content_tags below, whose *content* is dropped as well.
_ALLOWED_TAGS = {
    # document (re-wrapped by us after sanitizing)
    "html", "head", "body", "title", "style", "link",
    # text
    "h1", "h2", "h3", "h4", "h5", "h6", "p", "div", "span", "br", "hr",
    "blockquote", "pre", "code", "kbd", "samp", "var", "cite", "q", "dfn",
    "abbr", "b", "i", "u", "s", "strong", "em", "small", "mark", "sub", "sup",
    # lists
    "ul", "ol", "li", "dl", "dt", "dd",
    # tables
    "table", "caption", "colgroup", "col", "thead", "tbody", "tfoot", "tr",
    "td", "th",
    # links / media
    "a", "img", "picture", "source", "audio", "video", "track",
    # sections
    "article", "aside", "section", "header", "footer", "main", "nav",
    "details", "summary", "figure", "figcaption",
    # ruby
    "ruby", "rt", "rp", "rtc",
    # mathml (subset)
    "math", "mi", "mn", "mo", "ms", "mtext", "mfrac", "msqrt", "mroot",
    "msub", "msup", "msubsup", "munder", "mover", "mtable", "mtr", "mtd",
    "mstyle", "mspace", "mphantom", "menclose", "maction", "annotation",
    "annotation-xml",
    # svg (subset — case is restored by the browser's foreign-content rules)
    "svg", "g", "path", "rect", "circle", "ellipse", "line", "polyline",
    "polygon", "defs", "use", "symbol", "marker", "lineargradient",
    "radialgradient", "stop", "clippath", "mask", "pattern", "text", "tspan",
    "foreignobject", "switch", "desc", "view",
}
_ALLOWED_ATTRS = frozenset({
    "class", "id", "style", "title", "lang", "xml:lang", "dir", "role",
    "href", "src", "srcset", "alt", "width", "height", "loading", "decoding",
    "colspan", "rowspan", "span", "start", "type", "rel", "media", "sizes",
    "charset", "xmlns", "xlink:href", "viewbox", "preserveaspectratio", "fill",
    "stroke", "stroke-width", "transform", "d", "opacity", "points", "cx",
    "cy", "r", "rx", "ry", "x", "y", "x1", "y1", "x2", "y2", "offset",
    "stop-color", "stop-opacity", "text-anchor", "font-size", "font-family",
    "aria-label", "aria-hidden", "open", "datetime", "cite", "abbr",
    "controls", "loop", "muted", "preload", "value", "label", "headers",
    "scope", "align", "valign", "bgcolor", "border", "summary",
})
_URL_ATTRS = {"href", "src", "poster", "xlink:href"}
_DROPPED_ATTRS = {
    "srcdoc", "formaction", "action", "ping", "http-equiv", "integrity",
    "crossorigin", "onclickvalue",
}
_CSS_URL_RX = re.compile(r"""url\(\s*(['"]?)([^'")]+)\1\s*\)""", re.IGNORECASE)
_CSS_IMPORT_RX = re.compile(r"""@import\s+(['"])([^'"]+)\1""", re.IGNORECASE)

_MIME = {
    ".css": "text/css",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".bmp": "image/bmp",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}

# Cheap per-document scan (regex, no DOM): title / emptiness / char count.
_H_RX = re.compile(r"<h[1-4][^>]*>(.*?)</h[1-4]>", re.IGNORECASE | re.DOTALL)
_TITLE_RX = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_TAG_RX = re.compile(r"<[^>]+>")
_NAV_OPEN_RX = re.compile(r"<nav\b([^>]*)>", re.IGNORECASE)
_TYPE_RX = re.compile(r"""(?:epub:)?type\s*=\s*["']([^"']*)["']""", re.IGNORECASE)
_SCHEME_RX = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*):")
_SCRIPT_STYLE_RX = re.compile(
    r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL
)


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return data.decode("gb18030")
        except UnicodeDecodeError:
            return data.decode("utf-8", "replace")


def _quick_scan(text: str):
    """Classify a spine document without building a DOM.

    Returns ``(kind, title, chars)`` where kind is ``"nav"`` (the table of
    contents page), ``"empty"`` (nothing readable) or ``"doc"``.
    Mirrors the old ``_is_nav_doc`` + "no items" checks so chapter numbering
    stays identical to the previous full-parse implementation.
    """
    m = _NAV_OPEN_RX.search(text)
    if m:
        tm = _TYPE_RX.search(m.group(1))
        kind = tm.group(1).lower() if tm else ""
        if (not kind) or ("toc" in kind) or ("landmarks" in kind) or (
            "page-list" in kind
        ):
            return "nav", None, 0

    stripped = _TAG_RX.sub(" ", text)
    has_text = bool(stripped.strip())
    low = text.lower()
    has_media = ("<img" in low) or ("<image" in low) or ("<svg" in low)
    if not (has_text or has_media):
        return "empty", None, 0

    title = None
    m = _H_RX.search(text) or _TITLE_RX.search(text)
    if m:
        title = _collapse(html_unescape(_TAG_RX.sub(" ", m.group(1))))
        if not title:
            title = None
    return "doc", title, len(_collapse(stripped))


def _plain_of(text: str) -> str:
    """Visible text of a chapter document for search: tags out, ws collapsed."""
    text = _SCRIPT_STYLE_RX.sub(" ", text)
    return _collapse(html_unescape(_TAG_RX.sub(" ", text)))


def _normalize_ref(base_dir: str, href: str):
    """Resolve ``href`` against ``base_dir`` → ``(zip_path, fragment)``.

    Returns ``("", "")`` for empty or scheme-qualified (external) hrefs.
    """
    href = (href or "").strip()
    if not href:
        return "", ""
    if _SCHEME_RX.match(href):
        return "", ""
    if href.startswith("#"):
        return "", href[1:]
    file_part, _, frag = href.partition("#")
    file_part = file_part.split("?", 1)[0]
    if not file_part:
        return "", frag
    joined = urllib.parse.urljoin(
        "http://b/" + base_dir.strip("/") + "/", file_part
    )
    path = urllib.parse.unquote(joined[len("http://b/"):])
    return path.replace("\\", "/"), frag


# --------------------------------------------------------------------------
# Sanitizing (nh3) + URL rewriting
# --------------------------------------------------------------------------


def _sanitize(text: str, book_path: str, cur_n: int, base_dir: str, n_by_zip):
    """nh3-clean a chapter and rewrite every URL to a same-origin endpoint.

    nh3 (html5ever) normalizes markup exactly like the browser will, so the
    DOM the client builds from this output matches what we extracted.
    """
    book_q = urllib.parse.quote(book_path, safe="")
    res_prefix = _RES_URL + "?path=" + book_q + "&ref="
    doc_prefix = _DOC_URL + "?path=" + book_q + "&n="

    def rewrite(tag, name, value):
        n = name.lower()
        if n.startswith("on") or n in _DROPPED_ATTRS:
            return None
        if n == "srcset":
            return _rewrite_srcset(value)
        if n in _URL_ATTRS:
            return _rewrite_url(tag, value)
        return value

    def _rewrite_url(tag, value):
        v = (value or "").strip()
        if not v:
            return None
        low = v.lower()
        if v.startswith("#"):
            return v  # same-document reference (footnotes, svg use ids)
        sm = _SCHEME_RX.match(v)
        if sm:
            scheme = sm.group(1).lower()
            if scheme in ("http", "https", "mailto"):
                return v  # external: the client intercepts <a>, CSP blocks the rest
            if scheme == "data":
                if low.startswith("data:image/") and tag in (
                    "img", "image", "source", "video", "audio"
                ):
                    return v
                return None
            return None  # javascript:, vbscript:, file:, ...
        ref, frag = _normalize_ref(base_dir, v)
        if not ref:
            return ("#" + frag) if frag else None
        if tag == "a":
            cn = (n_by_zip or {}).get(ref)
            if cn is None:
                return None  # non-spine target: dead link instead of raw XHTML
            if cn == cur_n:
                return "#" + frag
            return doc_prefix + str(cn) + (("#" + frag) if frag else "")
        return res_prefix + urllib.parse.quote(ref, safe="/")

    def _rewrite_srcset(value):
        if "data:" in value:
            return value  # data URLs contain commas; leave them alone
        out = []
        for cand in re.split(r"\s*,\s*", value.strip()):
            if not cand:
                continue
            parts = cand.split(None, 1)
            url = _rewrite_url("img", parts[0])
            if url is None:
                continue
            out.append(url + ((" " + parts[1]) if len(parts) > 1 else ""))
        return ", ".join(out) or None

    return nh3.clean(
        text,
        tags=_ALLOWED_TAGS,
        attributes={t: _ALLOWED_ATTRS for t in _ALLOWED_TAGS},
        attribute_filter=rewrite,
        generic_attribute_prefixes={"epub:", "data-", "aria-"},
        url_schemes={"http", "https", "mailto", "data"},
        strip_comments=True,
        link_rel=None,
        clean_content_tags={
            "script", "noscript", "iframe", "frame", "frameset", "object",
            "embed", "applet",
        },
    )


# In-page stylesheet: themes/appearance are driven by CSS variables the parent
# sets on <html> of the iframe document; pagination toggles html.pg.
_INJECT_CSS = """
:root{--rd-fg:#1b1e22;--rd-bg:#fff;--rd-size:18px;--rd-lh:1.75;
--rd-fam:Georgia,'Times New Roman',serif;--rd-w:760px;--rd-ls:0px;
--rd-padx:16px;--rd-pad-y:24px;--rd-align:left;--rd-ow:normal;
--rd-pgw:640px;--rd-pggap:32px}
*{box-sizing:border-box}
html,body{margin:0;padding:0;background:var(--rd-bg);color:var(--rd-fg)}
body{font-family:var(--rd-fam);font-size:var(--rd-size);line-height:var(--rd-lh);
letter-spacing:var(--rd-ls);text-align:var(--rd-align);overflow-wrap:var(--rd-ow);
-webkit-text-size-adjust:100%}
.rd-content{max-width:var(--rd-w);margin:0 auto;padding:20px var(--rd-padx) 72px}
.rd-content>:first-child{margin-top:0}
h1,h2,h3,h4,h5,h6{line-height:1.3;margin:1.1em 0 .6em}
p{margin:0 0 .8em}
img,picture{max-width:100%;height:auto}
table{max-width:100%;border-collapse:collapse}
pre{overflow-x:auto;white-space:pre-wrap}
a{color:inherit}
/* paragraph affordance + card-style highlight (matches the pre-P1 reader:
   padding, rounded corners, left border; hover only on selectable blocks) */
.rd-content :is(p,h1,h2,h3,h4,h5,h6,blockquote,pre,li){border-radius:5px;
  transition:background .12s ease}
.rd-content :is(p,h1,h2,h3,h4,h5,h6,blockquote,pre,li):not(.rd-cur):not(.rd-speaking):hover{
  background:rgb(127 127 127 / .10)}
.rd-cur{padding:4px 8px;margin-left:-8px;margin-right:-8px;
  background:rgb(127 127 127 / .18);box-shadow:inset 3px 0 0 currentColor;
  border-radius:5px}
.rd-speaking{padding:4px 8px;margin-left:-8px;margin-right:-8px;
  background:rgb(127 145 255 / .20);box-shadow:inset 3px 0 0 #6f8cff;
  border-radius:5px}
.rd-fnmark{background:rgb(255 200 80 / .35);border-radius:3px}
/* search hits (transient) + saved annotation highlights */
.rd-qhit{background:rgb(255 196 64 / .50);border-radius:3px;
  box-shadow:0 0 0 1px rgb(255 170 40 / .45)}
.rd-qhit.rd-qcur{background:rgb(255 160 40 / .75);
  box-shadow:0 0 0 2px rgb(255 140 20)}
/* current TTS sentence — follows the playhead during read-aloud */
.rd-sent{background:rgb(111 140 255 / .38);border-radius:3px;
  box-shadow:0 0 0 1px rgb(111 140 255 / .45)}
.rd-hl{border-radius:3px;padding:1px 0;box-decoration-break:clone;
  -webkit-box-decoration-break:clone}
.rd-hl-yellow{background:rgb(255 214 91 / .55)}
.rd-hl-green{background:rgb(120 220 140 / .45)}
.rd-hl-blue{background:rgb(110 180 255 / .45)}
.rd-hl-pink{background:rgb(255 140 200 / .45)}
.rd-content .rd-hl[data-ann]{cursor:pointer}
html.rd-force body,html.rd-force body *:not(svg):not(svg *):not(img){
  font-family:var(--rd-fam) !important}
html.pg{overflow:hidden;height:100%}
html.pg body{height:100vh;overflow-x:auto;overflow-y:hidden;scrollbar-width:none}
html.pg body::-webkit-scrollbar{display:none}
html.pg .rd-content{max-width:none;height:100vh;padding:var(--rd-pad-y) var(--rd-padx);
column-width:var(--rd-pgw);column-gap:var(--rd-pggap);column-fill:auto}
html.pg .rd-content img{max-height:calc(100vh - 2 * var(--rd-pad-y));object-fit:contain}
"""

_SKELETON = (
    '<!DOCTYPE html><html><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    "</head><body></body></html>"
)


def _build_page(soup: BeautifulSoup, title: str, lang: str) -> str:
    """Wrap sanitized chapter content in the reader's own document shell."""
    doc = BeautifulSoup(_SKELETON, "html.parser")
    if lang:
        doc.html["lang"] = lang
    csp = doc.new_tag("meta")
    csp["http-equiv"] = "Content-Security-Policy"
    csp["content"] = CHAPTER_CSP
    doc.head.append(csp)
    style = doc.new_tag("style")
    style.string = _INJECT_CSS
    if title:
        t = doc.new_tag("title")
        t.string = title
        doc.head.append(t)

    # nh3 flattens html/head/body: hoist stylesheet tags, body the rest.
    for child in list(soup.contents):
        if isinstance(child, Tag) and child.name in ("style", "link"):
            doc.head.append(child)
        elif isinstance(child, Tag) and child.name == "title":
            child.decompose()  # we emit our own <title> above
        else:
            doc.body.append(child)

    # Our sheet goes last so the reader's theme/size win ties against the book.
    doc.head.append(style)

    wrap = doc.new_tag("div")
    wrap["class"] = "rd-content"
    for child in list(doc.body.contents):
        wrap.append(child)
    doc.body.append(wrap)
    doc.body["class"] = "rd-doc"
    return str(doc)


# --------------------------------------------------------------------------
# Chapter items (paragraph-level, for TTS + position mapping)
# --------------------------------------------------------------------------


def _res_ref(src: str):
    """Extract the ``ref`` from a rewritten ``/api/reader/res?...`` URL."""
    if not src or not src.startswith(_RES_URL + "?"):
        return None
    m = re.search(r"[?&]ref=([^&]+)", src)
    if not m:
        return None
    return urllib.parse.unquote(m.group(1))


def _collect_images(node, items):
    for img in node.find_all("img"):
        ref = _res_ref(img.get("src") or "")
        if ref:
            items.append({"type": "img", "src": ref})


def _extract_items(soup, base_file: str):
    """Flatten a chapter body into blocks (paragraphs + images) in document order.

    The outermost block wins (a ``blockquote``/``li`` becomes one item, its inner
    ``<p>``s are not emitted twice); images nested anywhere are always collected.
    A wrapper with no block children (``<div>line<br>line</div>``) is itself a
    paragraph, otherwise its text would be lost. ``base_file`` is kept for
    signature stability — srcs arrive already rewritten to resource URLs.
    """
    del base_file
    body = soup.body or soup
    items = []

    def has_block_descendant(node):
        for t in node.find_all(True):
            if (t.name or "").lower() in _BLOCK_TAGS:
                return True
        return False

    def walk(node):
        for child in node.children:
            if not isinstance(child, Tag):
                continue
            name = (child.name or "").lower()
            if name in _SKIP_TAGS:
                continue
            if name == "img":
                ref = _res_ref(child.get("src") or "")
                if ref:
                    items.append({"type": "img", "src": ref})
                continue
            if name in _BLOCK_TAGS:
                text = _collapse(child.get_text(" ", strip=True))
                if text:
                    items.append({"type": "p", "text": text})
                _collect_images(child, items)
                continue  # nested blocks belong to the parent
            if not has_block_descendant(child) and not child.find("img"):
                text = _collapse(child.get_text(" ", strip=True))
                if text:
                    items.append({"type": "p", "text": text})
                continue
            walk(child)

    walk(body)
    return items


# --------------------------------------------------------------------------
# OPF / navigation parsing
# --------------------------------------------------------------------------


def _container_opf(zf: zipfile.ZipFile):
    """``(opf_zip_path, opf_dir)`` from META-INF/container.xml (fallback: search)."""
    try:
        data = zf.read("META-INF/container.xml")
    except KeyError:
        data = b""
    m = re.search(rb"""full-path\s*=\s*["']([^"']+)["']""", data)
    if m:
        opf = urllib.parse.unquote(m.group(1).decode("utf-8", "replace"))
        return opf, os.path.dirname(opf).replace("\\", "/")
    for name in zf.namelist():
        if name.lower().endswith(".opf"):
            return name, os.path.dirname(name).replace("\\", "/")
    raise ValueError("no OPF package file in EPUB")


def _html_soup(text: str):
    """html.parser, silencing bs4's XML-prolog complaint (NCX/nav are XHTML)."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
        return BeautifulSoup(text, "html.parser")


def _walk_ol(ol, map_href, want_type=False):
    """Nested TOC entries from an EPUB3 nav ``<ol>``."""
    nodes = []
    for li in ol.find_all("li", recursive=False):
        a = li.find("a", recursive=False) or li.find("a")
        title = _collapse(a.get_text(" ", strip=True)) if a else ""
        href = a.get("href") if a else None
        n, frag = map_href(href)
        node = {"title": title, "n": n, "frag": frag, "children": []}
        if want_type and a is not None:
            node["type"] = (a.get("epub:type") or a.get("type") or "").lower()
        child_ol = li.find("ol", recursive=False)
        if child_ol:
            node["children"] = _walk_ol(child_ol, map_href, want_type)
        nodes.append(node)
    return nodes


def _parse_nav_doc(text: str, nav_dir: str, n_by_zip):
    """EPUB3 navigation document → ``(toc, landmarks, page_list)``."""
    def map_href(href):
        if not href:
            return None, None
        ref, frag = _normalize_ref(nav_dir, href)
        n = n_by_zip.get(ref) if ref else None
        return n, frag or None

    soup = _html_soup(text)   # nav doc (XHTML)
    toc, landmarks, pages = [], [], []
    for nav_el in soup.find_all("nav"):
        kind = (nav_el.get("epub:type") or nav_el.get("type") or "").lower()
        ol = nav_el.find("ol")
        if ol is None:
            continue
        if (not toc) and "toc" in kind:
            toc = _walk_ol(ol, map_href)
        elif "landmark" in kind:
            landmarks = _walk_ol(ol, map_href, want_type=True)
        elif "page-list" in kind:
            pages = _walk_ol(ol, map_href)
    return toc, landmarks, pages


def _parse_ncx(text: str, ncx_dir: str, n_by_zip):
    """EPUB2 NCX navMap → nested TOC entries."""

    def map_href(href):
        if not href:
            return None, None
        ref, frag = _normalize_ref(ncx_dir, href)
        n = n_by_zip.get(ref) if ref else None
        return n, frag or None

    soup = _html_soup(text)   # NCX is XML but flat-tagged; html.parser is fine
    navmap = soup.find("navmap")
    if navmap is None:
        return []

    def walk(container):
        out = []
        for np in container.find_all("navpoint", recursive=False):
            label = np.find("text")
            content = np.find("content")
            href = content.get("src") if content else None
            n, frag = map_href(href)
            out.append({
                "title": _collapse(label.get_text(" ", strip=True)) if label else "",
                "n": n,
                "frag": frag,
                "children": walk(np),
            })
        return out

    return walk(navmap)


def _retitle(nodes, chapters):
    """Prefer the document's own (migrated) title over the nav label."""
    for nd in nodes:
        if nd.get("n"):
            doc_title = chapters[nd["n"] - 1]["title"]
            if doc_title:
                nd["title"] = doc_title
        if nd.get("children"):
            _retitle(nd["children"], chapters)


def _count_nodes(nodes) -> int:
    return sum(1 + _count_nodes(n["children"]) for n in nodes)


class BookDoc:
    """Lazily parsed EPUB: metadata + light chapter index up front, XHTML on demand.

    ``root`` is to the OPF's folder inside the zip (``OEBPS``): resource lookup
    needs it again because manifest hrefs are OPF-relative.
    """

    def __init__(self, path, title, author, language, chapters, toc, landmarks,
                 pages, root=""):
        self.path = path
        self.title = title
        self.author = author
        self.language = language
        self.chapters = chapters   # [{"n", "title", "zip", "chars"}]
        self.toc = toc             # nested [{title, n, frag, children}]
        self.landmarks = landmarks
        self.pages = pages
        self.root = root
        self._n_by_zip = {c["zip"]: c["n"] for c in chapters}
        self._ch: "OrderedDict[int, dict]" = OrderedDict()
        self._ch_lock = threading.Lock()
        # Plain (searchable) chapter text, filled on demand by full-book scans.
        self._plain: "OrderedDict[int, str]" = OrderedDict()
        self._plain_lock = threading.Lock()

    def meta(self):
        return {
            "path": self.path,
            "title": self.title,
            "author": self.author,
            "language": self.language,
            "total": len(self.chapters),
            "chars": sum(c["chars"] for c in self.chapters),
            "chapters": [{"n": c["n"], "title": c["title"]} for c in self.chapters],
        }

    def toc_payload(self):
        return {
            "toc": self.toc,
            "landmarks": self.landmarks,
            "pages": self.pages,
        }

    def _check(self, n):
        if not isinstance(n, int) or not (1 <= n <= len(self.chapters)):
            raise ValueError(f"Chapter {n} out of range (1..{len(self.chapters)})")
        return n

    def _chapter_data(self, n):
        with self._ch_lock:
            data = self._ch.get(n)
            if data is not None:
                self._ch.move_to_end(n)
                return data
            data = self._build_chapter(n)
            self._ch[n] = data
            while len(self._ch) > _CH_CACHE_MAX:
                self._ch.popitem(last=False)
            return data

    def _build_chapter(self, n):
        ch = self.chapters[n - 1]
        try:
            with zipfile.ZipFile(self.path) as zf:
                raw = zf.read(ch["zip"])
        except KeyError as e:
            raise ValueError(f"Missing document {ch['zip']} in EPUB") from e
        text = _decode(raw)
        cleaned = _sanitize(text, self.path, n, os.path.dirname(ch["zip"]), self._n_by_zip)
        soup = BeautifulSoup(cleaned, "html.parser")
        items = _extract_items(soup, ch["zip"])
        page = _build_page(soup, ch["title"], self.language or "")
        return {"items": items, "html": page}

    def chapter(self, n):
        n = self._check(n)
        data = self._chapter_data(n)
        return {
            "n": n,
            "title": self.chapters[n - 1]["title"],
            "items": data["items"],
            "chars": self.chapters[n - 1]["chars"],
            "prev": n - 1 if n > 1 else None,
            "next": n + 1 if n < len(self.chapters) else None,
            "total": len(self.chapters),
        }

    def doc_html(self, n):
        n = self._check(n)
        return self._chapter_data(n)["html"]

    def plain_text(self, n) -> str:
        """Visible text of chapter ``n`` for search (regex strip, no DOM).

        Cached per book — a first full-book scan fills it, later searches are
        memory-only. Text matches the items pipeline closely: tags removed,
        entities decoded, whitespace collapsed to single spaces.
        """
        n = self._check(n)
        with self._plain_lock:
            text = self._plain.get(n)
            if text is not None:
                self._plain.move_to_end(n)
                return text
        ch = self.chapters[n - 1]
        try:
            with zipfile.ZipFile(self.path) as zf:
                raw = zf.read(ch["zip"])
        except KeyError as e:
            raise ValueError(f"Missing document {ch['zip']} in EPUB") from e
        text = _plain_of(_decode(raw))
        with self._plain_lock:
            self._plain[n] = text
            while len(self._plain) > _PLAIN_MAX:
                self._plain.popitem(last=False)
        return text


def parse_book(path: str) -> BookDoc:
    """Parse the EPUB index (metadata + spine + TOC). Throws on a broken file."""
    with zipfile.ZipFile(path) as zf:
        opf_name, opf_dir = _container_opf(zf)
        opf = BeautifulSoup(_decode(zf.read(opf_name)), "xml")

        def meta(name):
            el = opf.find(name)
            return _collapse(el.get_text()) if el else None

        manifest = {}
        for it in opf.find_all("item"):
            iid = it.get("id")
            href = it.get("href")
            if not iid or not href:
                continue
            ref, _ = _normalize_ref(opf_dir, href)
            manifest[iid] = {
                "zip": ref,
                "mt": (it.get("media-type") or "").lower(),
                "props": set((it.get("properties") or "").lower().split()),
            }

        spine_el = opf.find("spine")
        idrefs = []
        if spine_el is not None:
            idrefs = [el.get("idref") for el in spine_el.find_all("itemref")
                      if el.get("idref")]
        if not idrefs:  # broken/absent spine → fall back to manifest order
            idrefs = list(manifest)

        chapters = []
        nav_ref = None
        ncx_ref = None
        n_by_zip = {}
        # Some generators write <itemref idref="ch1.xhtml"/> (an href) instead of
        # the manifest id — accept both or those spines read as empty books.
        by_ref = {}
        for entry in manifest.values():
            by_ref.setdefault(entry["zip"], entry)
            by_ref.setdefault(os.path.basename(entry["zip"]), entry)
        for iid in idrefs:
            entry = manifest.get(iid) or by_ref.get(iid) or by_ref.get(
                os.path.basename(iid)
            )
            if not entry:
                continue
            if "nav" in entry["props"]:
                nav_ref = entry["zip"]
                continue
            if entry["mt"] == "application/x-dtbncx+xml":
                ncx_ref = entry["zip"]
                continue
            try:
                text = _decode(zf.read(entry["zip"]))
            except KeyError:
                continue
            kind, title, chars = _quick_scan(text)
            if kind != "doc":
                continue
            n_by_zip[entry["zip"]] = len(chapters) + 1
            chapters.append({
                "n": len(chapters) + 1,
                "title": title or f"Chương {len(chapters) + 1}",
                "zip": entry["zip"],
                "chars": chars,
            })

        if not ncx_ref:
            for entry in manifest.values():
                if entry["mt"] == "application/x-dtbncx+xml":
                    ncx_ref = entry["zip"]
                    break

        toc, landmarks, pages = [], [], []
        if nav_ref:
            try:
                toc, landmarks, pages = _parse_nav_doc(
                    _decode(zf.read(nav_ref)), os.path.dirname(nav_ref), n_by_zip
                )
            except KeyError:
                pass
        if not toc and ncx_ref:
            try:
                toc = _parse_ncx(
                    _decode(zf.read(ncx_ref)), os.path.dirname(ncx_ref), n_by_zip
                )
            except KeyError:
                pass
        for group in (toc, landmarks, pages):
            _retitle(group, chapters)

        _, root = _container_opf(zf)

    if not chapters:
        raise ValueError("no readable chapters in EPUB")

    return BookDoc(
        path=path,
        title=meta("title") or os.path.basename(path),
        author=meta("creator"),
        language=meta("language"),
        chapters=chapters,
        toc=toc,
        landmarks=landmarks,
        pages=pages,
        root=root,
    )


def load_book(path: str) -> BookDoc:
    """Cached ``parse_book`` — re-parses only when the file changed on disk."""
    try:
        mtime = os.path.getmtime(path)
    except OSError as e:
        raise ValueError(f"Cannot stat EPUB: {e}") from e

    key = (os.path.abspath(path), mtime)
    with _CACHE_LOCK:
        doc = _CACHE.get(key)
    if doc is not None:
        return doc

    doc = parse_book(path)

    with _CACHE_LOCK:
        while len(_CACHE) >= _CACHE_MAX:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key] = doc
    return doc


# --------------------------------------------------------------------------
# Full-book search (plain-text scan over the spine)
# --------------------------------------------------------------------------


def search_book(doc: BookDoc, query: str, *, max_hits: int = 200,
                per_chapter: int = 8) -> dict:
    """Scan every chapter for ``query`` (case-insensitive).

    Returns ``{query, total, truncated, chapters: [{n, title, count, hits:
    [{off, snip}]}]}`` — snippets carry ~40/60 chars of context with ellipses.
    The client re-finds the match in the rendered chapter itself, so ``off`` is
    only used for ordering.
    """
    query = _collapse(query)
    if not query:
        raise ValueError("q is required")
    if len(query) > 200:
        raise ValueError("q too long (max 200)")
    needle = query.casefold()
    max_hits = max(1, min(int(max_hits), 1000))
    chapters = []
    total = 0
    truncated = False

    for ch in doc.chapters:
        text = doc.plain_text(ch["n"])
        low = text.casefold()
        count = 0
        hits = []
        start = 0
        while True:
            i = low.find(needle, start)
            if i < 0:
                break
            count += 1
            total += 1
            if len(hits) < per_chapter:
                a = max(0, i - 40)
                b = min(len(text), i + len(query) + 60)
                snip = (("…" if a > 0 else "") + text[a:b]
                        + ("…" if b < len(text) else ""))
                hits.append({"off": i, "snip": snip})
            start = i + max(1, len(needle))
            if total >= max_hits:
                truncated = True
                break
        if count:
            chapters.append(
                {"n": ch["n"], "title": ch["title"], "count": count, "hits": hits}
            )
        if truncated:
            break

    return {
        "query": query,
        "total": total,
        "truncated": truncated,
        "chapters": chapters,
    }


# --------------------------------------------------------------------------
# Resources (CSS / images / fonts) out of the zip
# --------------------------------------------------------------------------


def _find_entry(path: str, ref: str, root: str):
    """Locate ``ref`` inside the zip (exact, OPF-prefixed, then suffix)."""
    ref = (ref or "").replace("\\", "/").strip().lstrip("/")
    if not ref or ".." in ref.split("/"):
        return None
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        candidates = [ref]
        if root:
            candidates.append(f"{root}/{ref}")
        entry = next((c for c in candidates if c in names), None)
        if entry is None:
            entry = next((n for n in names if n.endswith("/" + ref)), None)
        return entry


def find_image(doc: BookDoc, src: str):
    """Sanitize a reader ``src`` into an OPF-relative image href (or ``None``)."""
    src = (src or "").split("#")[0].split("?")[0].strip().lstrip("/")
    if not src or ".." in src.split("/"):
        return None
    if not src.lower().endswith(_IMG_EXTS):
        return None
    return src


def read_image(path: str, src: str):
    """Read an image straight out of the EPUB zip. Returns ``(bytes, media_type)``.

    ebooklib reports item paths relative to the OPF (``Images/x.jpg``) while the
    zip stores them under the OPF folder (``OEBPS/Images/x.jpg``), so try the
    exact path, the OPF-prefixed path, and finally any suffix match.
    """
    doc = load_book(path)
    name = find_image(doc, src)
    if not name:
        return None, None
    entry = _find_entry(path, name, doc.root)
    if entry is None:
        return None, None
    with zipfile.ZipFile(path) as zf:
        data = zf.read(entry)
    ext = os.path.splitext(entry)[1].lower()
    return data, _MIME.get(ext, "application/octet-stream")


def _rewrite_css_urls(css: str, css_dir: str) -> str:
    """Point ``url(...)`` / ``@import`` inside a stylesheet at the resource endpoint."""

    def repl(m):
        quote, value = m.group(1), m.group(2).strip()
        return "url(" + _css_value(value, css_dir) + ")"

    def imp(m):
        return "@import " + _css_value(m.group(2).strip(), css_dir)

    out = _CSS_URL_RX.sub(repl, css)
    return _CSS_IMPORT_RX.sub(imp, out)


def _css_value(value: str, css_dir: str) -> str:
    if not value or value.startswith("#") or _SCHEME_RX.match(value):
        return value  # fragments and external/data URLs stay as they are
    ref, _ = _normalize_ref(css_dir, value)
    if not ref:
        return value
    return _RES_URL + "?ref=" + urllib.parse.quote(ref, safe="/")


def read_resource(path: str, ref: str):
    """Read a CSS/image/font out of the EPUB. Returns ``(bytes, media_type)``.

    Chapter documents (xhtml/html/js/…) are refused: the reader gets those from
    ``/api/reader/doc`` (sanitized), never raw.
    """
    doc = load_book(path)
    ref = (ref or "").split("#", 1)[0].split("?", 1)[0]
    ext = os.path.splitext(ref)[1].lower()
    if ext not in _MIME:
        return None, None
    entry = _find_entry(path, ref, doc.root)
    if entry is None:
        return None, None
    with zipfile.ZipFile(path) as zf:
        data = zf.read(entry)
    mime = _MIME[ext]
    if ext == ".css":
        css_dir = os.path.dirname(entry).replace("\\", "/")
        data = _rewrite_css_urls(_decode(data), css_dir).encode("utf-8")
    return data, mime


# --------------------------------------------------------------------------
# Reading state sidecar (<book>.reader.json)
# --------------------------------------------------------------------------

_STATE_MAX_BYTES = 512 * 1024


def state_path_for(path: str) -> str:
    return os.path.splitext(path)[0] + ".reader.json"


def load_state(path: str) -> dict:
    try:
        with open(state_path_for(path), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path: str, data: dict) -> None:
    raw = json.dumps(data, ensure_ascii=False)
    if len(raw.encode("utf-8")) > _STATE_MAX_BYTES:
        raise ValueError("reader state too large")
    _atomic_write(state_path_for(path), raw)


def _atomic_write(target: str, raw: str) -> None:
    """Crash-safe JSON write: unique temp file + replace with backoff retries.

    Unique temp name per writer: two concurrent writers (the debounced save
    racing a keepalive flush on unload) used to share one ".part" file, so the
    second os.replace raised FileNotFoundError -> HTTP 500.
    """
    fd, part = tempfile.mkstemp(
        dir=os.path.dirname(os.path.abspath(target)) or ".",
        prefix=".rdstate-",
        suffix=".part",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(raw)
        last: OSError | None = None
        for i in range(6):
            try:
                os.replace(part, target)
                return
            except OSError as e:  # target momentarily locked by a reader/writer
                last = e
                time.sleep(0.05 * (i + 1))
        raise last  # type: ignore[misc]
    finally:
        if os.path.exists(part):
            try:
                os.remove(part)
            except OSError:
                pass


# --------------------------------------------------------------------------
# Annotations sidecar (<book>.ann.json): highlights + notes
# --------------------------------------------------------------------------

_ANN_COLORS = ("yellow", "green", "blue", "pink")
_ANN_MAX = 2000
_ANN_FILE_MAX = 2 * 1024 * 1024
_QUOTE_MAX = 500
_NOTE_MAX = 2000
_CTX_MAX = 80
# Read-modify-write of the annotations list is serialized process-wide: two
# quick saves (color tweak + note edit) would otherwise drop one of them.
_ANN_LOCK = threading.Lock()


def ann_path_for(path: str) -> str:
    return os.path.splitext(path)[0] + ".ann.json"


def load_anns(path: str) -> list:
    try:
        with open(ann_path_for(path), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    if isinstance(data, dict):
        data = data.get("anns")
    return data if isinstance(data, list) else []


def save_anns(path: str, anns: list) -> None:
    if len(anns) > _ANN_MAX:
        raise ValueError("too many annotations")
    raw = json.dumps({"v": 1, "anns": anns}, ensure_ascii=False)
    if len(raw.encode("utf-8")) > _ANN_FILE_MAX:
        raise ValueError("annotations too large")
    _atomic_write(ann_path_for(path), raw)


def ann_validate(ann, *, total: int) -> dict:
    """Normalized annotation copy (raises ValueError with a clean message)."""
    if not isinstance(ann, dict):
        raise ValueError("annotation object is required")
    quote = ann.get("quote")
    if not isinstance(quote, str) or not quote.strip():
        raise ValueError("quote is required")
    if len(quote) > _QUOTE_MAX:
        raise ValueError(f"quote too long (max {_QUOTE_MAX})")
    ch = ann.get("ch")
    if not isinstance(ch, int) or isinstance(ch, bool) or not (1 <= ch <= total):
        raise ValueError(f"ch must be between 1 and {total}")
    color = ann.get("color", "yellow")
    if color not in _ANN_COLORS:
        raise ValueError("bad color")
    note = ann.get("note", "")
    if not isinstance(note, str):
        note = ""
    item = ann.get("item")
    if not isinstance(item, int) or isinstance(item, bool) or item < 0:
        item = None

    def ctx(key):
        v = ann.get(key)
        return v[:_CTX_MAX] if isinstance(v, str) and v else ""

    out = {
        "quote": quote.strip()[:_QUOTE_MAX],
        "ch": ch,
        "color": color,
        "note": note[:_NOTE_MAX],
        "item": item,
        "pre": ctx("pre"),
        "post": ctx("post"),
    }
    if isinstance(ann.get("id"), str) and ann["id"]:
        out["id"] = ann["id"][:32]
    return out


# --------------------------------------------------------------------------
# TTS (VieNeu-TTS, local CPU)
# --------------------------------------------------------------------------

# One infer call is capped: ONNX blows up ("bad allocation") when a single
# call is handed several hundred characters at once. Text is cut at sentence
# and comma boundaries first; any span still over this cap is cut at spaces.
_CHUNK_CHARS = 180
MAX_TEXT = 6000
_SAMPLE_RATE = 48_000
_GAP_SECS = 0.12           # silence between synthesized pieces: _to_wav bakes it
                           # into the whole-paragraph WAV, reader.js (PLAYER_GAP)
                           # reproduces it when scheduling streamed pieces
_CACHE_MAX_ENTRIES = 64
_CACHE_MAX_BYTES = 64 * 1024 * 1024   # cap the synthesis LRU at 64 MB

# Cut after every sentence end AND after every comma/semicolon — the split the
# reader shipped with. c47d63c narrowed this to sentence-first (clauses only on
# overflow), which measurably changed the delivery: on a fixed 4-sentence ch373
# passage the same voice read at ~146 Hz mean pitch instead of ~173 Hz, with
# piece-to-piece jumps of 188 → 84 Hz (f0 std 40 vs 30), and an A/B of the
# rendered WAVs picked the pre-c47d63c cut as "the original voice". Clause
# fragments also keep each infer() call short, where the model resets its
# intonation the way the listener remembers.
_SPLIT_RX = re.compile(r"(?<=[.!?…:])\s+|(?<=[。！？；])|(?<=[,;])\s+")


def split_for_tts_seg(text: str, limit: int = _CHUNK_CHARS):
    """[(piece, start, end)] — synthesis pieces with offsets into the
    collapsed text (trim-adjusted), same cut algorithm as split_for_tts.

    Positions come from finditer spans: re.split would discard the matched
    separators and shift every later offset."""
    text = _collapse(text)
    if not text:
        return []
    spans, pos = [], 0
    for m in _SPLIT_RX.finditer(text):
        spans.append((pos, m.start()))
        pos = m.end()
    spans.append((pos, len(text)))

    out = []
    for s0, s1 in spans:
        raw = text[s0:s1]
        work = raw.strip()
        if work:
            base = s0 + (len(raw) - len(raw.lstrip()))
            w, W = 0, len(work)
            while True:
                if W - w <= limit:
                    sub = work[w:]
                    lead = len(sub) - len(sub.lstrip())
                    sub = sub.strip()
                    if sub:
                        out.append((sub, base + w + lead, base + w + lead + len(sub)))
                    break
                cut = work.rfind(" ", w, w + limit)
                if cut < w + limit // 2:
                    cut = w + limit
                sub = work[w:cut]
                lead = len(sub) - len(sub.lstrip())
                sub = sub.strip()
                if sub:
                    out.append((sub, base + w + lead, base + w + lead + len(sub)))
                w = cut
    return out


def split_for_tts(text: str, limit: int = _CHUNK_CHARS):
    """Split into synthesis-sized pieces, breaking on sentence/punctuation."""
    return [p for p, _, _ in split_for_tts_seg(text, limit)]


# Anti-hallucination guard: normal speech runs ≈0.048–0.060 s/char plus
# ~0.3 s of lead/tail silence, but the model occasionally speaks a phrase one
# time more than the text does (a free-running copy adds ≈+0.9–1.3 s). The
# threshold keeps ≈25–30 % headroom over normal speech so only such overruns
# trip it — calibrated on 164 measured piece renders (0 false positives, the
# one real extra-copy render flagged).
_GUARD_SEC_PER_CHAR = 0.065
_GUARD_OVERHEAD = 0.3
_GUARD_FLOOR = 0.75

# Text the Vietnamese phonemizer cannot read (leftover Chinese paragraphs, a
# lone full-width "？", …) synthesizes to zero frames. Shipping that out as a
# header-only WAV makes the browser fire audio.onerror and kill the whole
# read-aloud session, so unreadable pieces become proportional silence
# instead — playback and the sentence highlight simply move past them.
_MIN_AUDIO_SEC = 0.05          # model output below this is effectively empty
_SILENCE_SEC_PER_CHAR = 0.05   # reading pace for unreadable text
_SILENCE_MIN = 0.4             # even a single "？" holds the line briefly


def _suspects_extra_copy(part: str, seconds: float) -> bool:
    """True when *seconds* of audio for *part* is long enough that the model
    very likely repeated a phrase the text does not repeat (extra copy ≈
    +0.9–1.3 s). Used to re-synthesize a suspect piece once."""
    if seconds <= 0:
        return False
    expected = max(len(part) * _GUARD_SEC_PER_CHAR + _GUARD_OVERHEAD,
                   _GUARD_FLOOR)
    return seconds > expected


def _to_wav(chunks, sample_rate: int) -> bytes:
    """Concatenate float32 waveforms (+ short gaps) into 16-bit mono WAV bytes."""
    import numpy as np

    gap = np.zeros(int(_GAP_SECS * sample_rate), dtype=np.float32)
    joined = []
    for i, wav in enumerate(chunks):
        if i:
            joined.append(gap)
        joined.append(np.asarray(wav, dtype=np.float32).reshape(-1))
    audio = np.concatenate(joined) if joined else np.zeros(0, dtype=np.float32)
    pcm = np.clip(audio, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype("<i2")

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm.tobytes())
    return buf.getvalue()


class TTSEngine:
    """Lazy VieNeu-TTS wrapper: warm-up in a thread, serialized synthesis."""

    def __init__(self):
        self._lock = threading.Lock()      # one synthesis at a time
        self._state_lock = threading.Lock()
        self._cache = OrderedDict()             # (voice, text) → (wav, segs, bytes)
        self._cache_lock = threading.Lock()
        self._cache_bytes = 0
        self._thread = None
        self._tts = None
        self.state = "idle"                # idle | loading | ready | error
        self.error = None
        self.voices = []                   # [{"id", "label", "gender", "region"}]
        self.default_voice = None
        self.load_seconds = None

    # -- lifecycle ---------------------------------------------------------
    def ensure_warm(self):
        """Start the background load once (never blocks the request)."""
        with self._state_lock:
            if self.state in ("loading", "ready"):
                return
            self.state = "loading"
            self._thread = threading.Thread(
                target=self._load, name="tts-warmup", daemon=True
            )
            self._thread.start()

    def _load(self):
        started = time.time()
        try:
            from vieneu import Vieneu

            tts = Vieneu()
            voices = []
            for desc, vid in tts.list_preset_voices():
                info = tts._preset_voices.get(vid) or {}
                voices.append(
                    {
                        "id": vid,
                        # preset key = short name ("Hải Đăng"); the first element
                        # is the marketing line ("⭐ Hải Đăng — Nam · Bắc · …")
                        "label": vid or desc,
                        "desc": desc,
                        "gender": info.get("gender"),
                        "region": info.get("region"),
                        "style": info.get("style"),
                    }
                )
            with self._state_lock:
                self._tts = tts
                self.voices = voices
                self.default_voice = tts._default_voice or (
                    voices[0]["id"] if voices else None
                )
                self.load_seconds = round(time.time() - started, 1)
                self.state = "ready"
                self.error = None
        except Exception as e:  # noqa: BLE001
            with self._state_lock:
                self.state = "error"
                self.error = f"{type(e).__name__}: {e}"

    def status(self) -> dict:
        with self._state_lock:
            return {
                "state": self.state,
                "error": self.error,
                "load_seconds": self.load_seconds,
                "voices": list(self.voices),
                "default": self.default_voice,
            }

    # -- synthesis ---------------------------------------------------------
    def _cache_get(self, key):
        with self._cache_lock:
            hit = self._cache.get(key)
            if hit is not None:
                self._cache.move_to_end(key)
                return hit[0], hit[1]
            return None

    def _cache_put(self, key, wav, segs, size=None):
        if size is None:
            size = len(wav)
        if size > _CACHE_MAX_BYTES // 4:   # one giant item must not evict all
            return
        with self._cache_lock:
            self._cache[key] = (wav, segs, size)
            self._cache.move_to_end(key)
            self._cache_bytes += size
            while self._cache and (
                len(self._cache) > _CACHE_MAX_ENTRIES
                or self._cache_bytes > _CACHE_MAX_BYTES
            ):
                _k, dropped = self._cache.popitem(last=False)
                self._cache_bytes -= dropped[2]

    def _session(self, voice):
        """Resolve ``(tts, sample_rate, voice)`` for a request; raises
        RuntimeError when the engine is not ready and falls back to the
        default voice for unknown presets."""
        with self._state_lock:
            tts = self._tts
            ready = self.state == "ready"
            default = self.default_voice
            known = {v["id"] for v in self.voices}
        if not ready or tts is None:
            raise RuntimeError(f"tts not ready (state={self.state})")

        if voice not in known:  # unknown/None preset → fall back to the default
            voice = default
        voice = voice or default
        sr = float(getattr(tts, "sample_rate", _SAMPLE_RATE))
        return tts, sr, voice

    def _render_piece(self, part: str, voice, tts, sr: float):
        """Synthesize one split piece → audio samples (list/ndarray).

        Piece-level LRU so both the whole-paragraph and the streaming paths
        re-read instantly; silence fallback for text the model cannot read
        (a 0-frame WAV would stop the browser session, a punctuation-only
        piece would turn into junk speech); one extra-copy retry.
        """
        key = (voice, "p", part)
        hit = self._cache_get(key)
        if hit is not None:
            return hit[0]

        wav = None
        with self._lock:
            if any(c.isalnum() for c in part):
                wav = tts.infer(part, voice=voice)
                if float(len(wav)) / sr < _MIN_AUDIO_SEC:
                    # Unreadable text → no phonemes → a 0-frame WAV the
                    # browser cannot decode (it would stop the session).
                    wav = None
                elif _suspects_extra_copy(part, float(len(wav)) / sr):
                    # Model occasionally speaks a phrase one time more than
                    # the text (free-running copy). A fresh sample almost
                    # never repeats the mistake — take it when it's clean,
                    # else keep the shorter of the two.
                    alt = tts.infer(part, voice=voice)
                    if (not _suspects_extra_copy(part, float(len(alt)) / sr)
                            or len(alt) < len(wav)):
                        wav = alt
        if wav is None:
            # Nothing to pronounce ("……", Chinese, lone "?") → a proportional
            # pause instead of letting the model invent speech.
            import numpy as np

            secs = max(len(part) * _SILENCE_SEC_PER_CHAR, _SILENCE_MIN)
            wav = np.zeros(int(secs * sr), dtype=np.float32)
        # Cache stores samples (not WAV bytes): size = bytes, ≈4B/sample.
        size = int(getattr(wav, "nbytes", None) or len(wav) * 4)
        self._cache_put(key, wav, None, size=size)
        return wav

    def synthesize(self, text: str, voice=None):
        """Return ``(wav_bytes, segs)`` — segs maps audio time onto the
        collapsed text as ``[[start_c, end_c, t_seconds], …]`` for
        sentence-level highlighting. Raises RuntimeError when not ready."""
        text = _collapse(text)[:MAX_TEXT]
        if not text:
            raise ValueError("empty text")

        tts, sr, voice = self._session(voice)

        hit = self._cache_get((voice, text))
        if hit is not None:
            return hit

        import numpy as np

        pieces = split_for_tts_seg(text)
        wavs = [self._render_piece(part, voice, tts, sr)
                for part, _s, _e in pieces]
        wav = _to_wav(wavs, int(sr))

        segs, t = [], 0.0
        for (_p, s, e), chunk in zip(pieces, wavs):
            segs.append([s, e, round(t, 3)])
            t += float(np.asarray(chunk).size) / sr + _GAP_SECS

        self._cache_put((voice, text), wav, segs)
        return wav, segs

    def synthesize_stream(self, text: str, voice=None):
        """Yield ``(header, wav_bytes)`` per split piece as soon as it is
        rendered — the streaming playback path.

        header = ``{i, s, e, d, sr, final}`` where ``s``/``e`` are char
        offsets in the collapsed text (for sentence highlighting) and
        ``final`` marks the last piece of the paragraph. Raises
        RuntimeError/ValueError before the first yield.
        """
        text = _collapse(text)[:MAX_TEXT]
        if not text:
            raise ValueError("empty text")

        tts, sr, voice = self._session(voice)
        pieces = split_for_tts_seg(text)
        n = len(pieces)
        for idx, (part, s, e) in enumerate(pieces):
            chunk = self._render_piece(part, voice, tts, sr)
            header = {
                "i": idx, "s": s, "e": e,
                "d": round(len(chunk) / sr, 3),
                "sr": int(sr),
                "final": idx == n - 1,
            }
            yield header, _to_wav([chunk], int(sr))


ENGINE = TTSEngine()
