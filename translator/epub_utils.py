import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup


def iter_chapters(book):
    for item in book.get_items():
        if item.get_type() == ebooklib.ITEM_DOCUMENT:
            yield item


def load_soup(item):
    return BeautifulSoup(item.content, "html.parser")


def save_epub(book, path):
    epub.write_epub(path, book, {})
