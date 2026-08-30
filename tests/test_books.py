"""Tests for webui.books cover/metadata discovery helpers."""

import base64
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ebooklib import epub

from webui.books import (
    build_library,
    _strip_translation_marker,
    get_cover,
    get_meta,
    list_epubs,
)

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


class TestBookClassification(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _epub(self, name, title=None):
        p = os.path.join(self.tmp, name)
        _make_epub(p, title=title or name)
        return p

    def _cp(self, out_path, data):
        with open(out_path + ".checkpoint.json", "w", encoding="utf-8") as f:
            json.dump(data, f)

    def test_strip_translation_marker(self):
        cases = {
            "86—EIGHTY-SIX - LN 09-dich.epub": "86—EIGHTY-SIX - LN 09",
            "86—EIGHTY-SIX - LN 09-dich-1.epub": "86—EIGHTY-SIX - LN 09",
            "86—EIGHTY-SIX - LN 09-VN.epub": "86—EIGHTY-SIX - LN 09",
            "cao-lanh-giao-hoa-tai-sinh-690-dich-gpt.epub": "cao-lanh-giao-hoa-tai-sinh-690",
            "book.translated.epub": "book",
            "Chitose is in the Ramune Bottle - Volume 05 [Yen Press][Kobo]-dich.epub": (
                "Chitose is in the Ramune Bottle - Volume 05 [Yen Press][Kobo]"
            ),
            "The Detective is Already Dead - Volume 11 [Yen Press]{Volume 11}[Kobo]-dich-1.epub": (
                "The Detective is Already Dead - Volume 11 [Yen Press]{Volume 11}[Kobo]"
            ),
            "plain book.epub": "plain book",
            "86—EIGHTY-SIX - LN 10.epub": "86—EIGHTY-SIX - LN 10",
        }
        for fn, expected in cases.items():
            self.assertEqual(_strip_translation_marker(fn), expected, fn)

    def test_library_statuses(self):
        src = self._epub("Alpha.epub", title="Alpha Book")
        done_out = self._epub("Alpha-dich.epub")
        part_src = self._epub("Beta.epub", title="Beta Book")
        part_out = self._epub("Beta-dich.epub")
        untr = self._epub("Gamma.epub", title="Gamma Book")

        self._cp(done_out, {"input": src, "output": done_out, "completed": True,
                            "last_completed_chapter": 9, "to_chapter": 9})
        self._cp(part_out, {"input": part_src, "output": part_out, "completed": False,
                            "last_completed_chapter": 5, "to_chapter": 20})

        lib = build_library(dirs=[self.tmp])
        by_title = {g["title"]: g for g in lib["groups"]}
        self.assertEqual(set(by_title), {"Alpha Book", "Beta Book", "Gamma Book"})

        alpha_src = by_title["Alpha Book"]["source"]
        self.assertEqual(alpha_src["kind"], "source")
        self.assertEqual(by_title["Alpha Book"]["rank"], "done")
        done_e = [e for e in by_title["Alpha Book"]["entries"] if e["kind"] == "translated"][0]
        self.assertEqual(done_e["status"], "done")

        self.assertEqual(by_title["Beta Book"]["rank"], "partial")
        part_e = [e for e in by_title["Beta Book"]["entries"] if e["kind"] == "translated"][0]
        self.assertEqual(part_e["status"], "partial")
        self.assertEqual(part_e["progress"], {"chapter": 5, "total": 20, "pct": 25})

        self.assertEqual(by_title["Gamma Book"]["rank"], "untranslated")
        self.assertEqual(alpha_src["translations"],
                         [os.path.normcase(os.path.abspath(done_out))])

        self.assertEqual(lib["stats"]["total"], 3)
        self.assertEqual(lib["stats"]["files"], 5)
        self.assertEqual(lib["stats"]["done"], 1)
        self.assertEqual(lib["stats"]["partial"], 1)
        self.assertEqual(lib["stats"]["untranslated"], 1)
        # Untranslated books surface first.
        self.assertEqual([g["rank"] for g in lib["groups"]],
                         ["untranslated", "partial", "done"])

    def test_partial_progress_without_to_chapter(self):
        src = self._epub("Only.epub", title="Only Book")
        out = self._epub("Only-dich.epub")
        # Old checkpoint: no `to_chapter`, no `completed` -> falls back to counting
        # the source chapters (fixture has 1 chapter, 0 are completed).
        self._cp(out, {"input": src, "output": out, "last_completed_chapter": 0})
        lib = build_library(dirs=[self.tmp], with_chapters=True)
        g = lib["groups"][0]
        self.assertEqual(g["rank"], "partial")
        e = [x for x in g["entries"] if x["kind"] == "translated"][0]
        source_chapters = next(x for x in g["entries"] if x["kind"] == "source")["chapters"]
        self.assertEqual(e["progress"]["chapter"], 0)
        self.assertEqual(e["progress"]["total"], source_chapters)
        self.assertEqual(e["progress"]["total"], 2)  # fixture: nav + 1 chapter

    def test_multiple_translations_group(self):
        src = self._epub("Multi.epub", title="Multi Book")
        t1 = self._epub("Multi-dich.epub")
        t2 = self._epub("Multi-dich-1.epub")
        self._cp(t1, {"input": src, "output": t1, "completed": True,
                      "last_completed_chapter": 10, "to_chapter": 10})
        self._cp(t2, {"input": src, "output": t2, "completed": False,
                      "last_completed_chapter": 3, "to_chapter": 10})
        lib = build_library(dirs=[self.tmp])
        self.assertEqual(len(lib["groups"]), 1)
        g = lib["groups"][0]
        self.assertEqual(g["rank"], "done")
        self.assertEqual(len(g["translations"]), 2)
        self.assertEqual(sorted(x["kind"] for x in g["entries"]),
                         ["source", "translated", "translated"])

    def test_assumed_translation_without_checkpoint(self):
        self._epub("Anh.epub", title="Anh Book")
        self._epub("Anh-vn.epub")  # old output, no checkpoint
        lib = build_library(dirs=[self.tmp])
        self.assertEqual(len(lib["groups"]), 1)  # same base name -> one group
        g = lib["groups"][0]
        self.assertEqual(g["rank"], "assumed")
        assumed = [e for e in g["entries"] if e["kind"] == "assumed"][0]
        self.assertEqual(assumed["status"], "assumed")

    def test_corrupt_checkpoint_ignored(self):
        out = self._epub("Corrupt-dich.epub")
        with open(out + ".checkpoint.json", "w", encoding="utf-8") as f:
            f.write("{not json")
        lib = build_library(dirs=[self.tmp])
        g = lib["groups"][0]
        self.assertEqual(g["rank"], "assumed")

    def test_build_library_caches_until_change(self):
        import webui.books as books_mod

        src = self._epub("Cached.epub", title="Cached Book")
        books_mod._LIB_CACHE.clear()
        calls = []
        orig = books_mod._build_uncached

        def counting(dirs, with_meta, with_chapters):
            calls.append(1)
            return orig(dirs, with_meta, with_chapters)

        books_mod._build_uncached = counting
        try:
            books_mod.build_library(dirs=[self.tmp])
            books_mod.build_library(dirs=[self.tmp])
            self.assertEqual(len(calls), 1, "identical build must reuse cache")

            # A new output + checkpoint changes the signature -> rebuild.
            out = self._epub("Cached-dich.epub")
            self._cp(out, {"input": src, "output": out, "completed": True,
                           "last_completed_chapter": 1, "to_chapter": 1})
            books_mod.build_library(dirs=[self.tmp])
            self.assertEqual(len(calls), 2, "new checkpoint must invalidate cache")
        finally:
            books_mod._build_uncached = orig

    def test_build_library_refresh_forces_rebuild(self):
        import webui.books as books_mod

        self._epub("Refreshed.epub", title="Refreshed Book")
        books_mod._LIB_CACHE.clear()
        calls = []
        orig = books_mod._build_uncached

        def counting(dirs, with_meta, with_chapters):
            calls.append(1)
            return orig(dirs, with_meta, with_chapters)

        books_mod._build_uncached = counting
        try:
            books_mod.build_library(dirs=[self.tmp])
            books_mod.build_library(dirs=[self.tmp], refresh=True)
            books_mod.build_library(dirs=[self.tmp])
            self.assertEqual(len(calls), 2, "refresh=True must bypass cache")
        finally:
            books_mod._build_uncached = orig


if __name__ == "__main__":
    unittest.main(verbosity=2)
