"""EPUB reader backend: chapter/paragraph extraction + the local TTS engine.

Serves the ``/read`` page:

* ``load_book`` parses an EPUB once into plain chapters/items (paragraph-level
  granularity so the UI can highlight and jump per paragraph) and caches the
  result keyed on ``(path, mtime)`` — nav then costs nothing.
* ``TTSEngine`` wraps VieNeu-TTS (local, CPU/ONNX). It is loaded lazily in a
  background thread so the first page load never blocks, and synthesis is
  serialized behind a lock (one request at a time; the reader prefetches the
  next paragraph while the current one plays).
"""

import io
import os
import re
import threading
import time
import urllib.parse
import wave
import zipfile

from bs4 import BeautifulSoup, Tag

from translator.epub_utils import iter_chapters, load_soup

# Items the reader treats as "blocks": they get their own <p> in the DOM, can be
# clicked/jumped to, and are spoken as one TTS unit.
_BLOCK_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "blockquote", "pre", "li"}
_SKIP_TAGS = {"script", "style", "noscript", "nav", "head"}
_IMG_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp")

# Cache: at most a few books parsed at once (a novel is a few MB of text).
_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()
_CACHE_MAX = 4


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


class BookDoc:
    """Parsed EPUB: flat chapter list with paragraph-level items.

    ``root`` is the OPF's folder inside the zip (``OEBPS``): ebooklib strips
    that prefix from ``item.get_name()``, so reading a file back out of the zip
    needs it again.
    """

    def __init__(self, path, title, author, language, chapters, root=""):
        self.path = path
        self.title = title
        self.author = author
        self.language = language
        self.chapters = chapters  # [{"n", "title", "items": [{type, text|src}]}]
        self.root = root          # zip folder holding the OPF ("" = zip root)

    def meta(self):
        return {
            "path": self.path,
            "title": self.title,
            "author": self.author,
            "language": self.language,
            "total": len(self.chapters),
            "chapters": [{"n": c["n"], "title": c["title"]} for c in self.chapters],
        }

    def chapter(self, n):
        if not isinstance(n, int) or not (1 <= n <= len(self.chapters)):
            raise ValueError(f"Chapter {n} out of range (1..{len(self.chapters)})")
        ch = self.chapters[n - 1]
        return {
            "n": ch["n"],
            "title": ch["title"],
            "items": ch["items"],
            "prev": n - 1 if n > 1 else None,
            "next": n + 1 if n < len(self.chapters) else None,
            "total": len(self.chapters),
        }


def _chapter_title(soup, index: int) -> str:
    for tag in ("h1", "h2", "h3", "h4"):
        el = soup.find(tag)
        if el:
            text = _collapse(el.get_text(" ", strip=True))
            if text:
                return text
    if soup.title:
        text = _collapse(soup.title.get_text(" ", strip=True))
        if text:
            return text
    return f"Chương {index}"


def _resolve_href(base_file: str, href: str) -> str:
    """Resolve ``href`` against the chapter's zip path (``EPUB/chap_001.xhtml``)."""
    href = (href or "").split("#")[0].split("?")[0]
    if not href:
        return ""
    base_dir = os.path.dirname(base_file).replace("\\", "/")
    return urllib.parse.urljoin(base_dir + "/", href)


def _collect_images(node, base_file: str, items):
    for img in node.find_all("img"):
        src = _resolve_href(base_file, img.get("src") or "")
        if src:
            items.append({"type": "img", "src": src})


def _extract_items(soup, base_file: str):
    """Flatten a chapter body into blocks (paragraphs + images) in document order.

    The outermost block wins (a ``blockquote``/``li`` becomes one item, its inner
    ``<p>``s are not emitted twice); images nested anywhere are always collected.
    A wrapper with no block children (``<div>line<br>line</div>``) is itself a
    paragraph, otherwise its text would be lost.
    """
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
                src = _resolve_href(base_file, child.get("src") or "")
                if src:
                    items.append({"type": "img", "src": src})
                continue
            if name in _BLOCK_TAGS:
                text = _collapse(child.get_text(" ", strip=True))
                if text:
                    items.append({"type": "p", "text": text})
                _collect_images(child, base_file, items)
                continue  # nested blocks belong to the parent
            if not has_block_descendant(child) and not child.find("img"):
                text = _collapse(child.get_text(" ", strip=True))
                if text:
                    items.append({"type": "p", "text": text})
                continue
            walk(child)

    walk(body)
    return items


def _is_nav_doc(soup) -> bool:
    """True for the EPUB navigation document (the table of contents page)."""
    nav = soup.find("nav")
    if nav is None:
        return False
    kind = (nav.get("epub:type") or nav.get("type") or "").lower()
    return not kind or "toc" in kind or "landmarks" in kind or "page-list" in kind


def _zip_root(path: str) -> str:
    """Folder of the OPF inside the zip (``OEBPS``), or "" when at the root."""
    try:
        with zipfile.ZipFile(path) as zf:
            try:
                data = zf.read("META-INF/container.xml")
            except KeyError:
                return ""
    except (OSError, zipfile.BadZipFile):
        return ""
    match = re.search(rb"""full-path\s*=\s*["']([^"']+)["']""", data)
    if not match:
        return ""
    opf = match.group(1).decode("utf-8", "replace")
    return os.path.dirname(opf).replace("\\", "/")


def parse_book(path: str) -> BookDoc:
    """Parse the whole EPUB (throws on a broken file — caller reports the error)."""
    from ebooklib import epub

    book = epub.read_epub(path)
    chapters = []
    for item in iter_chapters(book):
        soup = load_soup(item)
        if _is_nav_doc(soup):
            continue  # the TOC page is not a chapter
        items = _extract_items(soup, item.get_name())
        if not items:
            continue  # nothing to read (blank placeholder document)
        chapters.append(
            {
                "n": len(chapters) + 1,
                "title": _chapter_title(soup, len(chapters) + 1),
                "items": items,
            }
        )

    def meta(ns, key):
        try:
            vals = book.get_metadata(ns, key)
            return vals[0][0] if vals else None
        except Exception:  # noqa: BLE001
            return None

    return BookDoc(
        path=path,
        title=meta("DC", "title") or os.path.basename(path),
        author=meta("DC", "creator"),
        language=meta("DC", "language"),
        chapters=chapters,
        root=_zip_root(path),
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


def find_image(doc: BookDoc, src: str):
    """Sanitize a reader ``src`` into an OPF-relative image href (or ``None``)."""
    del doc  # kept for signature stability / future validation
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
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        candidates = [name]
        if doc.root:
            candidates.append(f"{doc.root}/{name}")
        entry = next((c for c in candidates if c in names), None)
        if entry is None:
            entry = next((n for n in names if n.endswith("/" + name)), None)
        if entry is None:
            return None, None
        data = zf.read(entry)
    ext = os.path.splitext(entry)[1].lower()
    mime = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".svg": "image/svg+xml",
        ".bmp": "image/bmp",
    }.get(ext, "application/octet-stream")
    return data, mime


# --------------------------------------------------------------------------
# TTS (VieNeu-TTS, local CPU)
# --------------------------------------------------------------------------

# Sentences longer than this are split again: ONNX blows up ("bad allocation")
# when one call is handed several hundred characters at once.
_CHUNK_CHARS = 180
MAX_TEXT = 6000
_SAMPLE_RATE = 48_000

_SPLIT_RX = re.compile(r"(?<=[.!?…:])\s+|(?<=[。！？；])|(?<=[,;])\s+")


def split_for_tts(text: str, limit: int = _CHUNK_CHARS):
    """Split into synthesis-sized pieces, breaking on sentence/punctuation."""
    text = _collapse(text)
    if not text:
        return []
    parts = []
    for piece in _SPLIT_RX.split(text):
        piece = piece.strip()
        while len(piece) > limit:
            cut = piece.rfind(" ", 0, limit)
            if cut < limit // 2:
                cut = limit
            parts.append(piece[:cut].strip())
            piece = piece[cut:].strip()
        if piece:
            parts.append(piece)
    return [p for p in parts if p]


def _to_wav(chunks, sample_rate: int) -> bytes:
    """Concatenate float32 waveforms (+ short gaps) into 16-bit mono WAV bytes."""
    import numpy as np

    gap = np.zeros(int(0.12 * sample_rate), dtype=np.float32)
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
    def synthesize(self, text: str, voice=None):
        """Return WAV bytes for ``text`` (raises RuntimeError when not ready)."""
        text = _collapse(text)[:MAX_TEXT]
        if not text:
            raise ValueError("empty text")

        with self._state_lock:
            tts = self._tts
            ready = self.state == "ready"
            default = self.default_voice
            known = {v["id"] for v in self.voices}
        if not ready or tts is None:
            raise RuntimeError(f"tts not ready (state={self.state})")

        if voice not in known:  # unknown/None preset → fall back to the default
            voice = default

        parts = split_for_tts(text)
        with self._lock:
            wavs = [tts.infer(part, voice=voice or default) for part in parts]
        return _to_wav(wavs, getattr(tts, "sample_rate", _SAMPLE_RATE))


ENGINE = TTSEngine()
