"""Offline tests for the shared translation orchestration (translator.job).

No AI calls: a stub engine translates text in-process and we assert the
orchestration (chapter loop, save_epub, checkpoint, stats, progress_cb) works.
"""

import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ebooklib import epub
import ebooklib

from translator.job import run_translation, _default_checkpoint_file, _load_checkpoint
from translator.engines.base import TranslationEngine


class StubEngine(TranslationEngine):
    def __init__(self):
        super().__init__(from_lang="EN", to_lang="VI")
        self.translated = 0

    def supports_batch(self):
        return False

    def translate(self, text):
        self.translated += 1
        return text.replace("Hello", "Xin chào").replace("world", "thế giới")


def _make_epub(path, n_chapters=2):
    book = epub.EpubBook()
    book.set_identifier("test-001")
    book.set_title("Test Book")
    book.set_language("en")
    for i in range(n_chapters):
        ch = epub.EpubHtml(
            title=f"Chapter {i + 1}",
            file_name=f"ch{i + 1}.xhtml",
            lang="en",
        )
        ch.content = (
            f"<html><body><p>Hello world chapter {i + 1} sample text here</p>"
            f"<p>Second paragraph of chapter {i + 1}.</p></body></html>"
        ).encode("utf-8")
        book.add_item(ch)
        book.toc.append(ch)
    book.spine = ["nav"] + [f"ch{i + 1}" for i in range(n_chapters)]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    epub.write_epub(path, book, {})


class TestRunTranslationCore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.src = os.path.join(self.tmp, "in.epub")
        self.out = os.path.join(self.tmp, "out.epub")
        _make_epub(self.src, n_chapters=2)

    def _read_chapter(self, path, file_name):
        book = epub.read_epub(path)
        for item in book.get_items():
            if item.get_type() == ebooklib.ITEM_DOCUMENT and item.file_name == file_name:
                return item.content.decode("utf-8")
        raise AssertionError(f"chapter {file_name} not found")

    def test_runs_and_translates(self):
        engine = StubEngine()
        stats = run_translation(
            config={},
            input=self.src,
            output=self.out,
            engine="openai",
            engine_obj=engine,
            from_chapter=1,
            to_chapter=2,
        )
        self.assertTrue(os.path.exists(self.out))
        self.assertGreater(engine.translated, 0)
        text = self._read_chapter(self.out, "ch1.xhtml")
        self.assertIn("Xin chào", text)
        self.assertIn("thế giới", text)
        self.assertEqual(stats["chunks_translated"], engine.translated)

    def test_progress_cb_events(self):
        events = []
        run_translation(
            config={},
            input=self.src,
            output=self.out,
            engine="openai",
            engine_obj=StubEngine(),
            from_chapter=1,
            to_chapter=2,
            progress_cb=lambda ev, data: events.append(ev),
        )
        self.assertIn("job_started", events)
        self.assertIn("chapter_start", events)
        self.assertIn("chapter_done", events)
        self.assertIn("job_done", events)

    def test_checkpoint_written_and_resume(self):
        cp = _default_checkpoint_file(self.out)
        run_translation(
            config={},
            input=self.src,
            output=self.out,
            engine="openai",
            engine_obj=StubEngine(),
            from_chapter=1,
            to_chapter=2,
        )
        self.assertTrue(os.path.exists(cp))
        data = _load_checkpoint(cp)
        self.assertEqual(data.get("last_completed_chapter"), 2)
        self.assertTrue(data.get("completed"))

        # Resume from chapter 2: only chapter 2 should be (re)translated.
        engine = StubEngine()
        run_translation(
            config={},
            input=self.src,
            output=self.out,
            engine="openai",
            engine_obj=engine,
            from_chapter=1,
            to_chapter=2,
        )
        # effective_start = 3 (last_completed + 1) > end(2) -> nothing to do,
        # so engine.translated stays 0.
        self.assertEqual(engine.translated, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
