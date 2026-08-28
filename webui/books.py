"""Book library helpers: discover EPUBs, count chapters, preview a chapter."""

import os
import re

from ebooklib import epub

from translator.epub_utils import iter_chapters, load_soup

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_LIBRARY_DIRS = [PROJECT_ROOT]


def list_epubs(dirs=None) -> list:
    dirs = dirs or DEFAULT_LIBRARY_DIRS
    results = []
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if fn.lower().endswith(".epub"):
                path = os.path.join(d, fn)
                cp = path + ".checkpoint.json"
                results.append(
                    {
                        "name": fn,
                        "path": path,
                        "size_mb": round(os.path.getsize(path) / (1024 * 1024), 2),
                        "has_checkpoint": os.path.exists(cp),
                    }
                )
    return results


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
