"""Offline tests for webui.diff_utils (word-level chunk diff)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from webui.diff_utils import (
    tokenize_words,
    split_html_lines,
    diff_chunk,
    structure_signature,
    chunk_structure,
)


class TestDiffUtils(unittest.TestCase):
    def test_tokenize_keeps_tags(self):
        toks = tokenize_words("<p>Hello world</p>")
        self.assertIn("<p>", toks)
        self.assertIn("</p>", toks)
        self.assertIn("Hello", toks)
        self.assertIn("world", toks)

    def test_split_html_lines(self):
        lines = split_html_lines("<p>Hello</p><p>World</p>")
        self.assertEqual(lines, ["<p>Hello</p>", "<p>World</p>"])

    def test_word_diff_marks_change(self):
        d = diff_chunk("<p>Hello world</p>", "<p>Xin chào thế giới</p>")
        self.assertIn("lines", d)
        change = next((ln for ln in d["lines"] if ln["kind"] == "change"), None)
        self.assertIsNotNone(change)
        ops = [p["op"] for p in change["parts"]]
        self.assertIn("ins", ops)
        self.assertIn("del", ops)
        self.assertGreater(d["added_words"], 0)
        self.assertGreater(d["removed_words"], 0)

    def test_merged_paragraph_counts_words(self):
        src = "<p>First sentence here.</p><p>Second sentence here.</p>"
        tgt = "<p>First sentence and second sentence here.</p>"
        d = diff_chunk(src, tgt)
        self.assertGreater(d["removed_words"], 0)
        self.assertGreater(d["added_words"], 0)
        kinds = [ln["kind"] for ln in d["lines"]]
        self.assertIn("del", kinds)

    def test_structure_signature_keeps_ids(self):
        sig = structure_signature(
            '<p>Hi</p><span class="koboSpan" id="kobo.146.2">x</span>'
        )
        self.assertIn("<p>", sig)
        self.assertIn("</p>", sig)
        self.assertIn("<span#kobo.146.2>", sig)

    def test_chunk_structure_missing_span(self):
        src = (
            '<p><span class="koboSpan" id="kobo.146.2">A</span>'
            '<span class="koboSpan" id="kobo.147.1">B</span></p>'
        )
        # translation drops kobo.146.2
        tgt = '<p><span class="koboSpan" id="kobo.147.1">A dich</span></p>'
        st = chunk_structure(src, tgt)
        self.assertFalse(st["same"])
        self.assertEqual(st["coverage"]["missing"], ["kobo.146.2"])
        self.assertEqual(st["coverage"]["total_source"], 2)
        self.assertEqual(st["coverage"]["total_translated"], 1)

    def test_chunk_structure_equal(self):
        html = '<p><span class="koboSpan" id="kobo.146.2">A</span></p>'
        st = chunk_structure(html, html)
        self.assertTrue(st["same"])
        self.assertEqual(st["tag_diff"], [])
        self.assertEqual(st["coverage"]["missing"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
