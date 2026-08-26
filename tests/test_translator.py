"""Offline unit tests for translator.translator (no AI calls).

Covers:
  TC-1   : retry is bounded by max_tries / DEFAULT_MAX_TRIES (no infinite hang).
  TC-2..5: _has_html_structure_mismatch tolerates paragraph merges but still
           rejects dropped structural tags and gross content loss.
  TC-9   : _split_oversized_chunk never splits an HTML tag in half.
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bs4 import BeautifulSoup
from translator.translator import Translator, DEFAULT_MAX_TRIES
from translator.engines.base import TranslationEngine


class FakeEngine(TranslationEngine):
    """Duck-typed stub engine; never calls a real API."""

    def __init__(self, behave="empty"):
        super().__init__(from_lang="EN", to_lang="VI")
        self.calls = 0
        self.behave = behave

    def supports_batch(self):
        return False

    def translate(self, text):
        self.calls += 1
        if self.behave == "empty":
            return ""
        if self.behave == "nodiv":
            # drop <div> but keep <span>
            return text.replace("<div>", "").replace("</div>", "")
        if self.behave == "good":
            # keep structure, only translate text nodes crudely
            import re
            return re.sub(r"(?<=>)[^<>]+(?=<)", lambda m: "DICH_" + m.group(0)[:6], text)
        return text


class TestRetryCap(unittest.TestCase):
    def test_default_cap_is_30(self):
        self.assertEqual(DEFAULT_MAX_TRIES, 30)

    def test_retry_bounded(self):
        soup = BeautifulSoup(
            "<html><body><p>Hello world test chunk here</p></body></html>",
            "html.parser",
        )
        t = Translator(
            FakeEngine("empty"),
            max_tries=3,
            html_structure_min_similarity=0.7,
            consistency_config={},
        )
        out = t.translate_html(soup, chapter_number=1)
        self.assertEqual(t.engine.calls, 3)
        # giving up keeps the original text
        self.assertIn("Hello world test chunk here", out)

    def test_zero_max_tries_falls_back_to_default(self):
        t = Translator(
            FakeEngine("good"),
            max_tries=0,
            consistency_config={},
        )
        self.assertEqual(t.max_tries, DEFAULT_MAX_TRIES)


SRC = (
    '<div><p><span class="x">Hello world this is a test</span></p>'
    '<p><span class="x">Second paragraph here</span></p></div>'
)


class TestStructureMismatch(unittest.TestCase):
    def setUp(self):
        self.t = Translator(FakeEngine(), html_structure_min_similarity=0.7,
                            consistency_config={})

    def test_paragraph_merge_accepted(self):
        out = ('<div><span class="x">Xin chào thế giới đây là bài kiểm tra</span>'
               '<span class="x">Đoạn thứ hai ở đây</span></div>')
        self.assertFalse(self.t._has_html_structure_mismatch(SRC, out))

    def test_dropped_div_rejected(self):
        out = '<span class="x">Xin chào thế giới</span><span class="x">Đoạn hai</span>'
        self.assertTrue(self.t._has_html_structure_mismatch(SRC, out))

    def test_empty_output_rejected(self):
        out = '<a id="page-195"></a>'
        self.assertTrue(self.t._has_html_structure_mismatch(SRC, out))

    def test_faithful_translation_accepted(self):
        out = ('<div><p><span class="x">Xin chào thế giới đây là bài kiểm tra</span></p>'
               '<p><span class="x">Đoạn thứ hai ở đây</span></p></div>')
        self.assertFalse(self.t._has_html_structure_mismatch(SRC, out))

    def test_p_merge_with_spans_accepted(self):
        src = '<p><span class="C">A</span></p><p><span class="C">B</span></p><p><span class="C">C</span></p>'
        out = '<span class="C">A</span><span class="C">B</span><span class="C">C</span>'
        self.assertFalse(self.t._has_html_structure_mismatch(src, out))


class TestOversizeSplit(unittest.TestCase):
    def test_does_not_split_inside_tag(self):
        t = Translator(FakeEngine(), fallback_max_chunk_size=80,
                       consistency_config={})
        # one long run of a single opening tag + text, no boundary tokens
        text = "<p><span>" + ("x" * 400) + "</span></p>"
        parts = t._split_oversized_chunk(text, max_size=80)
        joined = "".join(parts)
        self.assertEqual(joined, text)
        # no chunk ends with a partial tag like "<sp"
        for p in parts:
            self.assertNotRegex(p, r"<[a-zA-Z]*$")


class FakeBatchEngine(TranslationEngine):
    """Stub engine with batch support (never calls a real API)."""

    def __init__(self):
        super().__init__(from_lang="EN", to_lang="VI")
        self.batch_calls = 0

    def supports_batch(self):
        return True

    def _translate_text(self, text):
        # Faithful "translation": prefix visible text nodes, preserve all tags.
        return re.sub(r"(?<=>)[^<>]+(?=<)", lambda m: "DICH_" + m.group(0), text)

    def translate(self, text):
        return self._translate_text(text)

    def translate_batch(self, texts):
        self.batch_calls += 1
        return [self._translate_text(t) for t in texts]


class TestBatchPath(unittest.TestCase):
    def _soup(self, body):
        return BeautifulSoup(f"<html><body>{body}</body></html>", "html.parser")

    def test_batch_translates_with_p_split(self):
        t = Translator(FakeBatchEngine(), consistency_config={})
        soup = self._soup("<p>Alpha one two</p><p>Beta three four</p>")
        out = t.translate_book_html_batch([soup], chapter_numbers=[1])
        self.assertEqual(len(out), 1)
        self.assertIn("DICH_", out[0])
        self.assertIn("</p>", out[0])

    def test_batch_dedup_and_p_br_split(self):
        t = Translator(FakeBatchEngine(), consistency_config={})
        # one chapter uses <p>, another uses <br> -> per-chapter re-eval
        soup_p = self._soup("<p>Alpha</p><p>Beta</p>")
        soup_br = self._soup("<p>X<br>Y<br>Z</p>")
        out = t.translate_book_html_batch([soup_p, soup_br])
        self.assertEqual(len(out), 2)
        self.assertIn("</p>", out[0])
        self.assertIn("<br>", out[1])
        # engine.batch_calls == 1 (both chapters queued together)
        self.assertEqual(t.engine.batch_calls, 1)

    def test_batch_consistency_context_does_not_crash(self):
        t = Translator(FakeBatchEngine(), consistency_config={"enabled": True})
        soup = self._soup("<p>One two three</p><p>Four five six</p>")
        out = t.translate_book_html_batch([soup], chapter_numbers=[1])
        self.assertEqual(len(out), 1)
        self.assertIn("DICH_", out[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
