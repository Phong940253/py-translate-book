"""Book library helpers: discover EPUBs, count chapters, preview a chapter."""

import os
import re

from ebooklib import epub

from translator.epub_utils import iter_chapters, load_soup

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_LIBRARY_DIRS = [PROJECT_ROOT]


def list_epubs(dirs=None, with_meta: bool = False) -> list:
    dirs = dirs or DEFAULT_LIBRARY_DIRS
    results = []
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if fn.lower().endswith(".epub"):
                path = os.path.join(d, fn)
                cp = path + ".checkpoint.json"
                entry = {
                    "name": fn,
                    "path": path,
                    "size_mb": round(os.path.getsize(path) / (1024 * 1024), 2),
                    "has_checkpoint": os.path.exists(cp),
                }
                if with_meta:
                    entry["meta"] = get_meta(path)
                results.append(entry)
    return results


def _mime_for_href(href: str) -> str:
    ext = os.path.splitext(href or "")[1].lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".svg": "image/svg+xml",
    }.get(ext, "application/octet-stream")


def get_cover(path: str):
    """Return ``(image_bytes, mimetype)`` for the EPUB cover, or ``None``.

    Resolution order: OPF ``<meta name="cover">`` id -> an image item carrying the
    ``cover-image`` property -> an item whose id is ``cover``/``cover-image`` -> the
    first image in the manifest. EPUBs without any image yield ``None``.
    """
    try:
        book = epub.read_epub(path)
    except Exception:  # noqa: BLE001
        return None

    item = None
    meta = book.get_metadata("OPF", "cover")
    if meta:
        # OPF <meta name="cover" content="<manifest-id>">: the id is the value.
        cover_id = meta[0][0]
        if cover_id:
            item = book.get_item_with_id(cover_id)

    images = [
        it
        for it in book.get_items()
        if (getattr(it, "media_type", "") or "").startswith("image/")
    ]
    if item is None:
        for it in images:
            if "cover-image" in (getattr(it, "properties", None) or []):
                item = it
                break
    if item is None:
        for it in images:
            if it.id in ("cover", "cover-image"):
                item = it
                break
    if item is None and images:
        item = images[0]
    if item is None:
        return None

    data = item.get_content()
    if not data:
        return None
    return data, _mime_for_href(item.file_name)


def get_meta(path: str) -> dict:
    """Return basic Dublin Core metadata: ``title`` / ``creator`` / ``language``."""
    out = {"title": None, "creator": None, "language": None}
    try:
        book = epub.read_epub(path)
    except Exception:  # noqa: BLE001
        return out

    def first(ns: str, key: str):
        try:
            vals = book.get_metadata(ns, key)
            if vals:
                return vals[0][0]
        except Exception:  # noqa: BLE001
            return None
        return None

    out["title"] = first("DC", "title")
    out["creator"] = first("DC", "creator")
    out["language"] = first("DC", "language")
    return out


def chapter_count(path: str):
    try:
        book = epub.read_epub(path)
        return len(list(iter_chapters(book)))
    except Exception:  # noqa: BLE001
        return None


def preview_chapter(path: str, chapter: int, max_chars: int = 4000):
    book = epub.read_epub(path)
    chapters = list(iter_chapters(book))
    if not (1 <= chapter <= len(chapters)):
        raise ValueError(
            f"Chapter {chapter} out of range (book has {len(chapters)} chapters)"
        )
    soup = load_soup(chapters[chapter - 1])
    text = soup.get_text("\n", strip=True)
    text = re.sub(r"\n{2,}", "\n", text)
    return text[:max_chars], len(text)
