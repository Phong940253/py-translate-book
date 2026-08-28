import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup
import re
import os
import shutil
import zipfile
from itertools import count


_EXT_MEDIA_TYPE = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".css": "text/css",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}


def _infer_media_type(file_name: str) -> str:
    ext = os.path.splitext(file_name or "")[1].lower()
    return _EXT_MEDIA_TYPE.get(ext, "application/octet-stream")


def _inject_manifest_entries(opf_text: str, additions: dict) -> str:
    """Insert <item> manifest entries for newly added files (e.g. illustrations)
    so they are declared in content.opf and render in EPUB readers."""
    manifest_close = re.search(r"</manifest\s*>", opf_text, re.IGNORECASE)
    if not manifest_close:
        return opf_text

    # Skip files that are already declared in the manifest.
    existing = set(
        m.group(1)
        for m in re.finditer(r'<item\b[^>]*\bhref="([^"]*)"', opf_text, re.IGNORECASE)
    )

    items = []
    for file_name in additions:
        if file_name in existing:
            continue
        item_id = "added-" + re.sub(r"[^A-Za-z0-9_.-]", "-", str(file_name))
        media = _infer_media_type(file_name)
        items.append(
            f'    <item id="{item_id}" href="{file_name}" media-type="{media}"/>\n'
        )

    if not items:
        return opf_text

    pos = manifest_close.start()
    return opf_text[:pos] + "".join(items) + opf_text[pos:]


def iter_chapters(book):
    for item in book.get_items():
        if item.get_type() == ebooklib.ITEM_DOCUMENT:
            yield item


def load_soup(item):
    return BeautifulSoup(item.content, "html.parser")


def _normalize_uid(value, fallback_prefix, uid_counter):
    if value is not None and str(value).strip():
        cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value)).strip("-")
        if cleaned:
            return cleaned
    return f"{fallback_prefix}-{next(uid_counter)}"


def _normalize_toc_entry(entry, uid_counter):
    if isinstance(entry, tuple):
        section, children = entry
        if getattr(section, "uid", None) is None:
            section.uid = _normalize_uid(
                getattr(section, "href", None) or getattr(section, "title", None),
                "section",
                uid_counter,
            )
        return (
            section,
            [_normalize_toc_entry(child, uid_counter) for child in children],
        )

    if getattr(entry, "uid", None) is None:
        entry.uid = _normalize_uid(
            getattr(entry, "id", None)
            or getattr(entry, "href", None)
            or getattr(entry, "title", None),
            "toc",
            uid_counter,
        )
    return entry


def normalize_book_toc(book):
    toc = getattr(book, "toc", None)
    if not toc:
        return

    uid_counter = count(1)
    book.toc = [_normalize_toc_entry(entry, uid_counter) for entry in toc]


def _to_bytes(content):
    if isinstance(content, bytes):
        return content
    if isinstance(content, bytearray):
        return bytes(content)
    if isinstance(content, str):
        return content.encode("utf-8")
    return bytes(content)


def _resolve_archive_name(archive_names, file_name):
    if file_name in archive_names:
        return file_name

    suffix = f"/{file_name}"
    matches = [name for name in archive_names if name.endswith(suffix)]
    if len(matches) == 1:
        return matches[0]
    if matches:
        return matches[0]

    return None


def _merge_translated_document(original_content, translated_content):
    original_soup = BeautifulSoup(_to_bytes(original_content), "html.parser")
    translated_soup = BeautifulSoup(_to_bytes(translated_content), "html.parser")

    original_container = original_soup.find("body") or original_soup.find("div")
    translated_container = translated_soup.find("body") or translated_soup.find("div")

    if original_container is None or translated_container is None:
        return _to_bytes(translated_content)

    original_container.clear()
    for child in list(translated_container.contents):
        original_container.append(child)

    return original_soup.encode("utf-8")


def _atomic_write_epub(path, book):
    """Write an EPUB atomically (temp file + os.replace) so a writer that is
    killed mid-write (e.g. a job terminated via multiprocessing terminate())
    never leaves a half-written / corrupt output file behind."""
    part_path = path + ".part"
    try:
        epub.write_epub(part_path, book, {})
        os.replace(part_path, path)
    finally:
        if os.path.exists(part_path):
            try:
                os.remove(part_path)
            except OSError:
                pass


def save_epub(book, path, source_path=None):
    if source_path is None:
        normalize_book_toc(book)
        _atomic_write_epub(path, book)
        return

    with zipfile.ZipFile(source_path, "r") as source_zip:
        archive_names = set(source_zip.namelist())
        replacements = {}
        additions = {}

        for item in book.get_items():
            file_name = getattr(item, "file_name", None)
            content = getattr(item, "content", None)
            if not file_name or content is None:
                continue

            archive_name = _resolve_archive_name(archive_names, file_name)
            if archive_name is None:
                additions[file_name] = _to_bytes(content)
                continue

            original_content = source_zip.read(archive_name)
            new_content = _to_bytes(content)
            if new_content == original_content:
                continue

            replacements[archive_name] = _merge_translated_document(
                original_content,
                new_content,
            )

        if not replacements and not additions:
            shutil.copyfile(source_path, path)
            return

        # New items (e.g. generated illustrations) are added to the zip but the
        # original OPF manifest does not know about them. Inject manifest entries
        # so readers render them and the EPUB stays valid.
        if additions:
            opf_names = [n for n in archive_names if n.lower().endswith(".opf")]
            if opf_names:
                opf_name = opf_names[0]
                opf_bytes = source_zip.read(opf_name)
                updated_opf = _inject_manifest_entries(
                    opf_bytes.decode("utf-8", errors="ignore"),
                    additions,
                )
                replacements[opf_name] = updated_opf.encode("utf-8")

        part_path = path + ".part"
        try:
            with zipfile.ZipFile(part_path, "w") as output_zip:
                for info in source_zip.infolist():
                    data = replacements.get(info.filename)
                    if data is None:
                        data = source_zip.read(info.filename)

                    output_zip.writestr(info, data)

                for file_name, content in additions.items():
                    if file_name in archive_names:
                        continue
                    output_zip.writestr(file_name, content)
            os.replace(part_path, path)
        finally:
            if os.path.exists(part_path):
                try:
                    os.remove(part_path)
                except OSError:
                    pass
        return

    normalize_book_toc(book)
    _atomic_write_epub(path, book)
