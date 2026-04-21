import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup
import re
import shutil
import zipfile
from itertools import count


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


def save_epub(book, path, source_path=None):
    if source_path is None:
        normalize_book_toc(book)
        epub.write_epub(path, book, {})
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

        with zipfile.ZipFile(path, "w") as output_zip:
            for info in source_zip.infolist():
                data = replacements.get(info.filename)
                if data is None:
                    data = source_zip.read(info.filename)

                output_zip.writestr(info, data)

            for file_name, content in additions.items():
                if file_name in archive_names:
                    continue
                output_zip.writestr(file_name, content)
        return

    normalize_book_toc(book)
    epub.write_epub(path, book, {})
