"""Book library helpers: discover EPUBs, count chapters, preview a chapter."""

import json
import os
import re
import threading
import time

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


# --------------------------------------------------------------------------
# Translation-status classification
#
# A book in the library is either the *source* or the *output* of a translation
# run. We classify each EPUB by priority:
#   1) checkpoint file (<output>.epub.checkpoint.json) — records `input`, `output`,
#      `completed`, `last_completed_chapter`, `to_chapter` (most reliable);
#   2) web-UI job with status `done` and a matching `params.output`;
#   3) filename translation markers (`-dich`, `.translated`, `-dich-gpt`, `-vn`, …)
#      as a heuristic for old outputs whose checkpoint is gone.
# --------------------------------------------------------------------------

_TRANSLATION_MARKER = re.compile(
    r"(?:[-.\s_])?(?:dich|bản\s*dịch|ban-?dich|translated|trans|gpt|vn)"
    r"(?:\([^)]*\)|[-.\s_]?\d+)*$",
    re.I,
)

_STATUS_LABELS = {
    "done": "Đã dịch",
    "partial": "Dịch dở",
    "assumed": "Nghi bản dịch",
    "untranslated": "Nguồn",
}


def _strip_translation_marker(fn: str) -> str:
    """Remove translation-output suffixes repeatedly so a source and all of its
    translation outputs share the same base name (e.g. ``X-dich-1.epub`` and
    ``X-vn.epub`` both normalize to ``X``)."""
    stem = os.path.splitext(fn)[0]
    while True:
        new = _TRANSLATION_MARKER.sub("", stem).rstrip(" -._()")
        if new == stem:
            return stem
        stem = new


def _read_checkpoint(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def _normalize_path(p: str):
    if not p:
        return None
    if not os.path.isabs(p):
        p = os.path.join(PROJECT_ROOT, p)
    return os.path.normcase(os.path.abspath(p))


def _job_done_outputs() -> set:
    """Absolute paths whose web-UI job finished with status ``done``."""
    job_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jobs")
    done = set()
    if not os.path.isdir(job_dir):
        return done
    for fn in os.listdir(job_dir):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(job_dir, fn), "r", encoding="utf-8") as f:
                m = json.load(f)
        except Exception:  # noqa: BLE001
            continue
        if m.get("status") == "done":
            out = _normalize_path((m.get("params") or {}).get("output"))
            if out:
                done.add(out)
    return done


def _is_done(cp: dict) -> bool:
    if cp.get("completed") is True:
        return True
    last = int(cp.get("last_completed_chapter") or 0)
    to = cp.get("to_chapter")
    return bool(to) and last >= int(to)


def _progress_from_checkpoint(cp: dict, e: dict) -> dict:
    last = int(cp.get("last_completed_chapter") or 0)
    total = None
    if cp.get("to_chapter"):
        total = int(cp["to_chapter"])
    if not total:
        total = chapter_count(e.get("source_path") or e["path"]) or 0
    pct = round(last * 100 / total) if total else 0
    return {"chapter": last, "total": total, "pct": min(pct, 100)}


def classify_books(dirs=None) -> list:
    """Attach translation ``kind``/``status`` to every discovered EPUB.

    Returns the entries from :func:`list_epubs` enriched with ``base_name``,
    ``kind`` (source|translated|assumed), ``status`` (untranslated|done|partial|
    assumed), ``label``, ``progress`` (for partial), ``source_path`` and the
    ``translations`` a source produced (absolute paths).
    """
    eps = list_epubs(dirs=dirs, with_meta=False)
    done_outputs = _job_done_outputs()
    for e in eps:
        e["path"] = os.path.abspath(e["path"])
        e["abs"] = _normalize_path(e["path"])
        e["base_name"] = _strip_translation_marker(e["name"])
        e["kind"] = "source"
        e["status"] = "untranslated"
        e["label"] = _STATUS_LABELS["untranslated"]
        e["progress"] = None
        e["source_path"] = None
        e["translations"] = []
    by_abs = {e["abs"]: e for e in eps}

    # 1) Checkpoints: the file holding the checkpoint is the translation output;
    #    its `input` names the source.
    for e in eps:
        cp = _read_checkpoint(e["path"] + ".checkpoint.json")
        if not cp:
            continue
        if _normalize_path(cp.get("output")) != e["abs"]:
            continue
        e["kind"] = "translated"
        e["source_path"] = cp.get("input")
        e["status"] = "done" if _is_done(cp) else "partial"
        e["label"] = _STATUS_LABELS[e["status"]]
        if e["status"] == "partial":
            e["progress"] = _progress_from_checkpoint(cp, e)
        src_abs = _normalize_path(cp.get("input"))
        if src_abs in by_abs:
            by_abs[src_abs]["translations"].append(e["abs"])

    # 2) Web-UI finished jobs.
    for e in eps:
        if e["abs"] in done_outputs:
            e["kind"] = "translated"
            e["status"] = "done"
            e["label"] = _STATUS_LABELS["done"]

    # 3) Filename heuristic (no checkpoint / job): name carries a translation marker.
    for e in eps:
        if e["kind"] == "source" and _strip_translation_marker(e["name"]) != os.path.splitext(e["name"])[0]:
            e["kind"] = "assumed"
            e["status"] = "assumed"
            e["label"] = _STATUS_LABELS["assumed"]

    return eps


_RANK_ORDER = {"untranslated": 0, "partial": 1, "assumed": 2, "done": 3}
_RANK_LABELS = {
    "untranslated": "Chưa dịch",
    "partial": "Dịch dở",
    "assumed": "Nghi bản dịch",
    "done": "Đã dịch",
}


def _group_rank(entries: list) -> str:
    states = {e["status"] for e in entries if e["kind"] != "source"}
    for status in ("done", "partial", "assumed"):
        if status in states:
            return status
    return "untranslated"


def build_library(dirs=None, with_meta: bool = True, with_chapters: bool = False,
                  use_cache: bool = True, refresh: bool = False) -> dict:
    """Group source EPUBs and their translation outputs by title.

    Returns ``{"groups": [...], "stats": {...}}`` where each group has
    ``base_name``, ``title``, ``source`` (source entry or None), ``entries``
    (all files of the title) and ``translations`` (the outputs), plus a summary
    ``rank``/``rank_label``. Groups sort so untranslated books surface first.

    The build itself is expensive (it reads every EPUB twice: metadata and, when
    ``with_chapters``, chapter counts), so results are cached by a cheap
    signature of the epub/checkpoint/job files. ``use_cache=False`` or
    ``refresh=True`` forces a rebuild.
    """
    target = dirs or DEFAULT_LIBRARY_DIRS
    sig = _library_signature(target)
    key = (tuple(os.path.abspath(d) for d in target), with_meta, with_chapters)
    now = time.monotonic()
    with _LIB_LOCK:
        cached = _LIB_CACHE.get(key)
        if use_cache and not refresh and cached:
            # Signature unchanged + fresh → serve. A jobs/checkpoint change
            # (different signature) rebuilds immediately; TTL only bounds how
            # long an entry survives edits that did not touch mtime.
            if cached["sig"] == sig and (now - cached["ts"]) < _LIB_TTL:
                return cached["payload"]
        payload = _build_uncached(target, with_meta, with_chapters)
        if use_cache:
            _LIB_CACHE[key] = {"sig": sig, "payload": payload, "ts": now}
        return payload


_LIB_CACHE: dict = {}
_LIB_LOCK = threading.Lock()
_LIB_TTL = 300.0  # seconds


def _library_signature(dirs: list) -> tuple:
    """Cheap fingerprint of everything that feeds the library so the expensive
    build only reruns when a file actually changed (new EPUB, checkpoint/job
    update, …).

    Job files only invalidate on a *finished* (``done``) state, and the mtime
    is intentionally ignored for them: the frequent progress writes of a
    running job must not trigger library rebuilds, while a job reaching ``done``
    (which ``classify_books`` consumes) must.
    """
    sig = []
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if fn.lower().endswith(".epub") or fn.endswith(".checkpoint.json"):
                try:
                    st = os.stat(os.path.join(d, fn))
                    sig.append((fn, st.st_mtime_ns))
                except OSError:  # noqa: BLE001
                    pass
    job_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jobs")
    if os.path.isdir(job_dir):
        for fn in sorted(os.listdir(job_dir)):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(job_dir, fn), "r", encoding="utf-8") as f:
                    m = json.load(f)
            except Exception:  # noqa: BLE001
                continue
            if m.get("status") == "done":
                sig.append((fn, "done"))
    return tuple(sig)


def _build_uncached(dirs: list, with_meta: bool, with_chapters: bool) -> dict:
    eps = classify_books(dirs)
    groups: dict[str, dict] = {}
    for e in eps:
        if with_meta:
            e["meta"] = get_meta(e["path"])
        if with_chapters:
            e["chapters"] = chapter_count(e["path"])
        b = e["base_name"]
        if b not in groups:
            groups[b] = {
                "base_name": b,
                "title": b,
                "source": None,
                "entries": [],
                "translations": [],
                "rank": "untranslated",
                "rank_label": _RANK_LABELS["untranslated"],
            }
        groups[b]["entries"].append(e)

    result = []
    for g in groups.values():
        entries = g["entries"]
        entries.sort(key=lambda e: {"source": 0, "translated": 1, "assumed": 2}[e["kind"]])
        for e in entries:
            if e["kind"] == "source":
                g["source"] = e
            else:
                g["translations"].append(e)
        title = None
        if g["source"] is not None:
            title = (g["source"].get("meta") or {}).get("title")
        if not title:
            for e in entries:
                title = (e.get("meta") or {}).get("title")
                if title:
                    break
        g["title"] = title or g["base_name"]
        g["rank"] = _group_rank(entries)
        g["rank_label"] = _RANK_LABELS[g["rank"]]
        result.append(g)

    result.sort(key=lambda g: (_RANK_ORDER.get(g["rank"], 9), (g["title"] or "").lower()))
    stats = {k: sum(1 for g in result if g["rank"] == k) for k in _RANK_ORDER}
    stats["total"] = len(result)
    stats["files"] = len(eps)
    return {"groups": result, "stats": stats}
