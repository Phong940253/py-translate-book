"""Battery of tag-mismatch test cases for the HTML structure guard.

These pin down the behavior of ``translator.Translator._has_html_structure_mismatch``
and the static ``_is_html_tag_missing`` -- the guard that decides whether a model
output is rejected and retried (or given up) during translation.

Two groups:

  A) CURRENT behavior that must stay stable (regression guards). These pin down
     the tag-name-level contract: dropping/duplicating leaf tags, surplus wrappers,
     content loss and paragraph merges are handled as before.

  B) Attribute-level behavior (now FIXED). The guard compares attribute-aware
     signatures (``id`` / ``href`` / ``src`` / ``koboSpan``-class), so it rejects an
     output that keeps every tag name but rewrites a decisive attribute -- e.g. a
     ``koboSpan`` whose ``id`` was dropped or changed (the exact kobo.874.2 failure
     mode). The guard and the live monitor's ``chunk_structure`` now agree.

Run with: python -m unittest tests.test_html_mismatch -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translator.translator import Translator
from translator.engines.base import TranslationEngine


class _NullEngine(TranslationEngine):
    """Engine that returns its input untouched (we test the guard, not the model)."""

    def __init__(self):
        super().__init__(from_lang="EN", to_lang="VI")

    def supports_batch(self):
        return False

    def translate(self, text):
        return text


def _guard(min_sim=0.7):
    """A Translator whose only job here is to expose the structure guard."""
    return Translator(_NullEngine(), html_structure_min_similarity=min_sim,
                      consistency_config={})


# ---------------------------------------------------------------------------
# A) Current behavior -- must stay stable (regression guards)
# ---------------------------------------------------------------------------
class TestMismatchCurrentBehavior(unittest.TestCase):
    def setUp(self):
        self.g = _guard()

    # -- dropped structural tags (any tag name) are rejected --------------
    def test_dropped_leaf_span_rejected(self):
        self.assertTrue(self.g._has_html_structure_mismatch(
            "<p><span>a</span></p>", "<p>a</p>"))

    def test_dropped_em_rejected(self):
        self.assertTrue(self.g._has_html_structure_mismatch(
            "<p><em>x</em></p>", "<p>x</p>"))

    def test_dropped_strong_rejected(self):
        self.assertTrue(self.g._has_html_structure_mismatch(
            "<p><strong>x</strong></p>", "<p>x</p>"))

    def test_dropped_img_rejected(self):
        self.assertTrue(self.g._has_html_structure_mismatch(
            '<p><img src="a.png">x</p>', "<p>x</p>"))

    def test_dropped_wrapper_div_rejected(self):
        self.assertTrue(self.g._has_html_structure_mismatch(
            "<div><p>x</p></div>", "<p>x</p>"))

    def test_dropped_wrapper_section_rejected(self):
        self.assertTrue(self.g._has_html_structure_mismatch(
            "<section><p>x</p></section>", "<p>x</p>"))

    # -- spurious / extra tags are rejected --------------------------------
    def test_added_wrapper_div_rejected(self):
        self.assertTrue(self.g._has_html_structure_mismatch(
            "<p>x</p>", "<p>x</p><div>y</div>"))

    def test_duplicated_tag_rejected(self):
        self.assertTrue(self.g._has_html_structure_mismatch(
            "<span>a</span>", "<span>a</span><span>b</span>"))

    # -- reordered distinct tag sequence is rejected ----------------------
    def test_reordered_distinct_tags_rejected(self):
        # Tag *names* are identical but their sequence differs -> low similarity.
        self.assertTrue(self.g._has_html_structure_mismatch(
            "<b>x</b><i>y</i>", "<i>y</i><b>x</b>"))

    # -- gross content loss is rejected ------------------------------------
    def test_content_loss_rejected(self):
        src = ("<p>" + "word " * 30 + "</p>")
        self.assertTrue(self.g._has_html_structure_mismatch(src, "<p>x</p>"))

    # -- legitimate changes are ACCEPTED (no false positive) ---------------
    def test_paragraph_merge_accepted(self):
        self.assertFalse(self.g._has_html_structure_mismatch(
            "<p><span>a</span></p><p><span>b</span></p>",
            "<span>a</span><span>b</span>"))

    def test_translation_only_accepted(self):
        self.assertFalse(self.g._has_html_structure_mismatch(
            '<p><span class="koboSpan" id="k1">Hello world</span></p>',
            '<p><span class="koboSpan" id="k1">Xin chào thế giới</span></p>'))

    def test_self_closing_img_preserved_accepted(self):
        # img tag kept, only text translated, same src -> accepted.
        self.assertFalse(self.g._has_html_structure_mismatch(
            '<p><img src="a.png"/>Hello</p>',
            '<p><img src="a.png"/>Xin chào</p>'))

    def test_tagless_both_accepted(self):
        self.assertFalse(self.g._has_html_structure_mismatch(
            "Hello world plain text", "Xin chào thế giới"))

    def test_uppercase_tag_names_normalized(self):
        # <P>/<SPAN> must normalize to <p>/<span>; still accepted as same.
        self.assertFalse(self.g._has_html_structure_mismatch(
            "<P><SPAN>x</SPAN></P>", "<p><span>y</span></p>"))

    def test_entities_not_mistaken_for_tags(self):
        # &lt;p&gt; is an entity, not a tag -> both sides tagless -> accepted.
        self.assertFalse(self.g._has_html_structure_mismatch(
            "&lt;p&gt;Hello&lt;/p&gt; sample", "&lt;p&gt;Xin chào&lt;/p&gt; mẫu"))

    def test_empty_output_not_mismatched_here(self):
        # Empty output is handled by _is_html_tag_missing; the structural guard
        # returns False for missing output so the two checks stay orthogonal.
        self.assertFalse(self.g._has_html_structure_mismatch("<p>x</p>", ""))


# ---------------------------------------------------------------------------
# _is_html_tag_missing (static) -- any-tag-present check
# ---------------------------------------------------------------------------
class TestHtmlTagMissing(unittest.TestCase):
    def test_source_tag_output_none(self):
        self.assertTrue(Translator._is_html_tag_missing("<p>x</p>", "x"))

    def test_both_have_tags(self):
        self.assertFalse(Translator._is_html_tag_missing("<p>x</p>", "<div>y</div>"))

    def test_both_tagless(self):
        self.assertFalse(Translator._is_html_tag_missing("x", "y"))

    def test_output_has_more_tags_ok(self):
        # Only checks that the *source* tag is not lost; extra tags are fine here.
        self.assertFalse(Translator._is_html_tag_missing("<p>x</p>",
                                                          "<p>x</p><div>y</div>"))


# ---------------------------------------------------------------------------
# B) Attribute-level behavior (now FIXED): the guard compares tag signatures that
#    fold in decisive attributes (id / href / src / koboSpan-class), so it rejects
#    an output that rewrites those while keeping the tag name. chunk_structure (used
#    by the live monitor) agrees with the guard -- no more silent gap.
# ---------------------------------------------------------------------------
class TestMismatchAttributeAware(unittest.TestCase):
    def setUp(self):
        self.g = _guard()

    def test_guard_rejects_kobospon_id_change(self):
        self.assertTrue(self.g._has_html_structure_mismatch(
            '<p><span class="koboSpan" id="kobo.874.2">The old man</span> looked.</p>',
            '<p><span class="koboSpan" id="kobo.874.3">Ông lão</span> nhìn.</p>'))

    def test_guard_rejects_kobospon_id_dropped(self):
        self.assertTrue(self.g._has_html_structure_mismatch(
            '<p><span class="koboSpan" id="k1">Hello</span></p>',
            '<p><span class="koboSpan">Xin chào</span></p>'))

    def test_guard_rejects_kobospon_class_dropped(self):
        # id kept but koboSpan class rewritten to plain -> still a drift.
        self.assertTrue(self.g._has_html_structure_mismatch(
            '<p><span class="koboSpan" id="k1">Hello</span></p>',
            '<p><span class="plain" id="k1">Xin chào</span></p>'))

    def test_guard_rejects_anchor_id_changed(self):
        self.assertTrue(self.g._has_html_structure_mismatch(
            '<a id="page-1">x</a>', '<a id="page-2">x</a>'))

    def test_guard_rejects_href_changed(self):
        self.assertTrue(self.g._has_html_structure_mismatch(
            '<a href="url-a">x</a>', '<a href="url-b">x</a>'))

    def test_guard_rejects_duplicate_kobospon_id(self):
        src = ('<span class="koboSpan" id="k1">a</span>'
               '<span class="koboSpan" id="k2">b</span>')
        out = ('<span class="koboSpan" id="k1">a</span>'
               '<span class="koboSpan" id="k1">b</span>')
        self.assertTrue(self.g._has_html_structure_mismatch(src, out))

    def test_guard_rejects_img_src_changed(self):
        self.assertTrue(self.g._has_html_structure_mismatch(
            '<p><img src="a.png"/>Hello</p>',
            '<p><img src="b.png"/>Xin chào</p>'))

    def test_guard_accepts_preserved_kobospon_id(self):
        # Same id (and koboSpan class) kept -> accepted, only text translated.
        self.assertFalse(self.g._has_html_structure_mismatch(
            '<p><span class="koboSpan" id="k1">Hello world</span></p>',
            '<p><span class="koboSpan" id="k1">Xin chào thế giới</span></p>'))

    def test_guard_ignores_generic_span_class_change(self):
        # class on a non-koboSpan span is NOT decisive -> accepted.
        self.assertFalse(self.g._has_html_structure_mismatch(
            '<p><span class="calibre1">Hello</span></p>',
            '<p><span class="calibre2">Xin chào</span></p>'))

    def test_structure_agrees_with_guard_on_id_change(self):
        # Guard and monitor's chunk_structure must agree (no silent gap).
        from webui.diff_utils import chunk_structure

        src = '<p><span class="koboSpan" id="k1">Hello</span></p>'
        out = '<p><span class="koboSpan" id="k2">Xin chào</span></p>'
        self.assertTrue(self.g._has_html_structure_mismatch(src, out))
        st = chunk_structure(src, out)
        self.assertFalse(st["same"])
        self.assertEqual(st["coverage"]["missing"], ["k1"])
        self.assertEqual(st["coverage"]["extra"], ["k2"])


# ---------------------------------------------------------------------------
# Unit tests for the attribute-aware signature token itself.
# ---------------------------------------------------------------------------
class TestTagSignature(unittest.TestCase):
    def _sig(self, tag):
        from translator.translator import _tag_signature
        return _tag_signature(tag)

    def test_kobospon_id(self):
        self.assertEqual(
            self._sig('<span class="koboSpan" id="k1">'),
            "<span#k1.koboSpan>")

    def test_kobospon_id_class_rewrite_kept(self):
        # id preserved but class dropped -> marker still recorded so it differs.
        self.assertEqual(self._sig('<span class="plain" id="k1">'), "<span#k1>")

    def test_anchor_id(self):
        self.assertEqual(self._sig('<a id="page-1">'), "<a#page-1>")

    def test_href(self):
        self.assertEqual(self._sig('<a href="url-a">'), "<a@url-a>")

    def test_img_src(self):
        self.assertEqual(self._sig('<img src="x.png"/>'), "<img@x.png>")

    def test_kobospon_no_id(self):
        self.assertEqual(self._sig('<span class="koboSpan">'), "<span.koboSpan>")

    def test_generic_span(self):
        self.assertEqual(self._sig('<span class="calibre1">'), "<span>")

    def test_closing_tag(self):
        self.assertEqual(self._sig('</span>'), "</span>")

    def test_uppercase_name_normalized(self):
        self.assertEqual(self._sig('<SPAN CLASS="koboSpan" ID="K1">'), "<span#K1.koboSpan>")


if __name__ == "__main__":
    unittest.main(verbosity=2)
