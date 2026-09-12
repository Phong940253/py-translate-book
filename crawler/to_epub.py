"""Build a Chinese raw EPUB from crawled chapters (one file per chapter)."""

import html
import os

from ebooklib import epub


def _chapter_html(title, paragraphs):
    body = [f"<h2>{html.escape(title)}</h2>"]
    for p in paragraphs:
        body.append(f"<p>{html.escape(p)}</p>")
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        "<!DOCTYPE html>\n"
        '<html xmlns="http://www.w3.org/1999/xhtml" lang="zh-CN">\n'
        "<head><title>" + html.escape(title) + "</title></head>\n"
        "<body>" + "".join(body) + "</body>\n</html>"
    )


def build_epub(book_title, author, chapters, output_path):
    """chapters: list of {title, paragraphs}. Writes output_path (.epub)."""
    book = epub.EpubBook()
    book.set_identifier(f"ixdzs-{book_title}")
    book.set_title(book_title)
    book.set_language("zh")
    if author:
        book.add_author(author)

    items = []
    for i, ch in enumerate(chapters, 1):
        title = ch["title"] or f"第{i}章"
        item = epub.EpubHtml(
            title=title,
            file_name=f"chap_{i:04d}.xhtml",
            lang="zh",
        )
        item.content = _chapter_html(title, ch["paragraphs"]).encode("utf-8")
        book.add_item(item)
        items.append(item)

    book.toc = items
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav"] + items

    tmp = output_path + ".part"
    epub.write_epub(tmp, book, {})
    os.replace(tmp, output_path)
    return output_path
