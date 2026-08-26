"""Offline unit tests for translator.html_utils (no AI calls).

Covers:
  TC-6/TC-7 : split_html must NOT append a spurious </p> to a trailing
              non-<p> fragment (e.g. </section></div>) but still closes
              normal </p> content.
  TC-8      : extract_html_content always normalizes <br/> and <br /> to <br>.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translator.html_utils import split_html, extract_html_content
from bs4 import BeautifulSoup


class TestSplitHtmlTrailingFragment(unittest.TestCase):
    def test_no_spurious_p_on_trailing_div(self):
        content = '<p>A</p><p>B</p></section></div>'
        chunks = split_html(content, "</p>")
        self.assertEqual(chunks[-1], '<p>A</p><p>B</p></section></div>')
        self.assertFalse(chunks[-1].endswith("</p>"))

    def test_normal_p_content_still_closes(self):
        content = '<p>A</p><p>B</p>'
        chunks = split_html(content, "</p>")
        self.assertEqual(chunks[-1], '<p>A</p><p>B</p>')

    def test_br_trailing_fragment_untouched(self):
        # For <br> split, the last chunk is left as-is (no spurious <br> appended).
        content = '<p>A</p><br><p>B</p>'
        chunks = split_html(content, "<br>")
        self.assertEqual(chunks, ['<p>A</p><br><p>B</p>'])
        self.assertFalse(chunks[-1].endswith("<br>"))

    def test_br_split_does_not_append_to_last_chunk(self):
        # Force a split so the boundary matters: each chunk (except last) that
        # lost its <br> gets it back, but the final chunk never gets one.
        content = "<p>" + "x" * 5000 + "</p><br>" + "<p>" + "y" * 5000 + "</p>"
        chunks = split_html(content, "<br>")
        self.assertTrue(len(chunks) >= 2)
        self.assertFalse(chunks[-1].endswith("<br>"))
        self.assertTrue(all(c.endswith("<br>") for c in chunks[:-1]))


class TestExtractHtmlContentBrNormalization(unittest.TestCase):
    def _extract(self, html, split_tag):
        soup = BeautifulSoup(html, "html.parser")
        return extract_html_content(soup, split_tag)

    def test_br_normalized_for_br_split(self):
        html = "<html><body><p>x<br/>y<br />z</p></body></html>"
        out = self._extract(html, "<br>")
        self.assertIn("<br>", out)
        self.assertNotIn("<br/>", out)
        self.assertNotIn("<br />", out)

    def test_br_normalized_even_for_p_split(self):
        # When the global split tag is </p>, <br/> variants must still be
        # normalized so a per-chapter re-evaluation to <br> still matches.
        html = "<html><body><p>x<br/>y<br />z</p></body></html>"
        out = self._extract(html, "</p>")
        self.assertIn("<br>", out)
        self.assertNotIn("<br/>", out)
        self.assertNotIn("<br />", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
