"""Offline tests for the shared translation orchestration (translator.job).

No AI calls: a stub engine translates text in-process and we assert the
orchestration (chapter loop, save_epub, checkpoint, stats, progress_cb) works.
"""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ebooklib import epub
import ebooklib

from translator.job import (
    run_translation,
    _default_checkpoint_file,
    _load_checkpoint,
    _restore_existing_output_content,
)
from translator.epub_utils import iter_chapters
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
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
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


class RecordingEngine(TranslationEngine):
    """Engine that remembers every source text it was handed."""

    def __init__(self):
        super().__init__(from_lang="EN", to_lang="VI")
        self.seen = []

    def supports_batch(self):
        return False

    def translate(self, text):
        self.seen.append(text)
        return text.replace("Hello", "Xin chào").replace("world", "thế giới")


class TestResumeTranslatesFromTheOriginal(unittest.TestCase):
    """A resumed run must read the chapters it still has to do from the INPUT.

    The output file may hold an interrupted / over-limit / simply bad copy of
    such a chapter (that is why the checkpoint gets reopened to redo it), and
    translating from that copy compares the book with itself: the diff pane
    shows an identical pair and the untranslated/echo guard loops forever.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.src = os.path.join(self.tmp, "in.epub")
        self.out = os.path.join(self.tmp, "out.epub")
        _make_epub(self.src, n_chapters=3)

    def _chapters(self, path):
        return list(iter_chapters(epub.read_epub(path)))

    def _content(self, item):
        return item.content.decode("utf-8")

    def _run(self, engine):
        return run_translation(
            config={},
            input=self.src,
            output=self.out,
            engine="openai",
            engine_obj=engine,
            from_chapter=1,
            to_chapter=3,
        )

    def _set_last_completed(self, chapter):
        cp = _default_checkpoint_file(self.out)
        data = _load_checkpoint(cp)
        data["last_completed_chapter"] = chapter
        data.pop("completed", None)
        with open(cp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)

    def test_restore_keeps_the_source_of_chapters_still_to_do(self):
        self._run(StubEngine())          # output now holds a translated copy
        book = epub.read_epub(self.src)
        before = {c.file_name: self._content(c) for c in iter_chapters(book)}
        self.assertIn("Hello", before["ch3.xhtml"])

        # chapter 3 is still to be translated → it must keep the source text
        _restore_existing_output_content(
            book, self.out, translate_from=3, translate_to=3
        )
        after = {c.file_name: self._content(c) for c in iter_chapters(book)}

        self.assertIn("Xin chào", after["ch1.xhtml"])   # finished → output copy
        self.assertIn("Xin chào", after["ch2.xhtml"])   # out of range → kept
        self.assertEqual(after["ch3.xhtml"], before["ch3.xhtml"])  # untouched
        self.assertNotIn("Xin chào", after["ch3.xhtml"])

    def test_restore_without_a_range_still_restores_everything(self):
        self._run(StubEngine())
        book = epub.read_epub(self.src)
        _restore_existing_output_content(book, self.out)
        # only the story chapters — the nav document is never translated
        restored = [
            c for c in iter_chapters(book) if c.file_name.startswith("ch")
        ]
        self.assertEqual(len(restored), 3)
        for chapter in restored:
            self.assertIn("Xin chào", self._content(chapter))

    def test_resumed_chapter_is_translated_from_the_input_epub(self):
        self._run(StubEngine())          # full run: all 3 chapters translated
        # Output is ahead of the checkpoint (the user reopened it to redo
        # chapter 3, whose copy in the output is bad) — the real situation.
        self._set_last_completed(2)

        engine = RecordingEngine()
        self._run(engine)

        # The engine saw the original text, never the stored translation.
        self.assertEqual(len(engine.seen), 1)
        self.assertIn("Hello world chapter 3", engine.seen[0])
        self.assertNotIn("Xin chào", engine.seen[0])

        # ...and the chapters kept out of this run were not reverted to the
        # source language by the save that follows.
        stored = {c.file_name: self._content(c) for c in self._chapters(self.out)}
        self.assertIn("Xin chào", stored["ch1.xhtml"])
        self.assertIn("Xin chào", stored["ch2.xhtml"])
        self.assertIn("Xin chào", stored["ch3.xhtml"])  # chapter 3 redone

        # The engine actually produced output for that one chapter.
        self.assertIn("chapter 3", engine.seen[0])

    def test_resumed_chapter_is_not_the_stored_translation(self):
        """Regression: before the fix the restored copy became the source."""
        self._run(StubEngine())
        self._set_last_completed(2)
        engine = RecordingEngine()
        self._run(engine)
        for text in engine.seen:
            self.assertNotIn("<p>Xin chào world chapter 3", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
