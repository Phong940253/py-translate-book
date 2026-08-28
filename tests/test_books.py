"""Tests for webui.books cover/metadata discovery helpers."""

import base64
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ebooklib import epub

from webui.books import get_cover, get_meta, list_epubs

# 1x1 transparent PNG.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
)


def _make_epub(path, with_cover=True, with_cover_meta=True,
               title="Sample Book", creator="A. Author", language="en"):
    book = epub.EpubBook()
    book.set_identifier("cover-test")
    book.set_title(title)
    book.set_language(language)
    book.add_metadata("DC", "creator", creator, others={})
    if with_cover:
        img = epub.EpubImage()
        img.id = "cover"
        img.file_name = "cover.png"
        img.media_type = "image/png"
        img.content = _PNG
        # Real EPUBs usually mark the cover via the `cover-image` property (or the
        # OPF <meta name="cover"> id). ebooklib round-trips the property reliably,
        # so we use it as the primary signal in tests.
        if with_cover_meta:
            img.properties = ["cover-image"]
        book.add_item(img)
    ch = epub.EpubHtml(title="Ch1", file_name="ch1.xhtml", lang="en")
    ch.content = b"<html><body><p>Hello</p></body></html>"
    book.add_item(ch)
    book.spine = ["nav", "ch1.xhtml"]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    epub.write_epub(path, book, {})


class TestBooksCoverMeta(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_get_cover_with_meta(self):
        p = os.path.join(self.tmp, "c.epub")
        _make_epub(p, with_cover=True, with_cover_meta=True)
        cover = get_cover(p)
        self.assertIsNotNone(cover)
        data, mime = cover
        self.assertEqual(data, _PNG)
        self.assertEqual(mime, "image/png")

    def test_get_cover_fallback_by_id(self):
        # Cover image present but no OPF <meta name="cover">: found via id "cover".
        p = os.path.join(self.tmp, "c.epub")
        _make_epub(p, with_cover=True, with_cover_meta=False)
        cover = get_cover(p)
        self.assertIsNotNone(cover)
        self.assertEqual(cover[0], _PNG)

    def test_get_cover_missing(self):
        p = os.path.join(self.tmp, "nocov.epub")
        _make_epub(p, with_cover=False)
        self.assertIsNone(get_cover(p))

    def test_get_cover_bad_path(self):
        self.assertIsNone(get_cover("/nonexistent/file.epub"))

    def test_get_meta(self):
        p = os.path.join(self.tmp, "m.epub")
        _make_epub(p, title="My Title", creator="Jane Doe", language="vi")
        meta = get_meta(p)
        self.assertEqual(meta["title"], "My Title")
        self.assertEqual(meta["creator"], "Jane Doe")
        self.assertEqual(meta["language"], "vi")

    def test_get_meta_missing(self):
        self.assertEqual(
            get_meta("/nonexistent/file.epub"),
            {"title": None, "creator": None, "language": None},
        )

    def test_list_epubs_with_meta(self):
        p = os.path.join(self.tmp, "lib.epub")
        _make_epub(p, title="Lib Book", creator="Lib Author")
        items = list_epubs(dirs=[self.tmp], with_meta=True)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["meta"]["title"], "Lib Book")
        self.assertEqual(items[0]["meta"]["creator"], "Lib Author")


if __name__ == "__main__":
    unittest.main(verbosity=2)
