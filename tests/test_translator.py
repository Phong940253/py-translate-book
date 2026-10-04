"""Offline unit tests for translator.translator (no AI calls).

Covers:
  TC-1   : retries are unlimited by default (max_tries=0 → never give up);
           an explicit positive max_tries still bounds structural failures.
  TC-2..5: _has_html_structure_mismatch tolerates paragraph merges but still
           rejects dropped structural tags and gross content loss.
  TC-9   : _split_oversized_chunk never splits an HTML tag in half.
  TC-10..: _has_untranslated_content rejects model echoes (verbatim or with
           trivial edits) while tolerating proper-noun residue and
           number-only chunks; untranslated failures retry past max_tries
           (stoppable via should_stop) with periodic manual-queue records.
"""
import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bs4 import BeautifulSoup
from translator.translator import (
    Translator,
    DEFAULT_MAX_TRIES,
    _repair_wrapper_balance,
    _TranslationStopped,
)
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
    def test_default_is_unlimited(self):
        # 0 = unlimited: a chunk is retried until it translates or the job is
        # stopped — the source text is never written just to make progress.
        self.assertEqual(DEFAULT_MAX_TRIES, 0)

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

    def test_zero_max_tries_means_unlimited(self):
        t = Translator(
            FakeEngine("good"),
            max_tries=0,
            consistency_config={},
        )
        self.assertEqual(t.max_tries, DEFAULT_MAX_TRIES)  # 0 = no cap
        self.assertFalse(t.max_tries)

    def test_default_never_falls_back_to_source_text(self):
        # Unlimited by default: an always-broken output must not cause the
        # original (Chinese) chunk to be written — the only way out is an
        # explicit stop of the job.
        t = Translator(
            FakeEngine("empty"),
            html_structure_min_similarity=0.7,
            consistency_config={},
        )
        with mock.patch("time.sleep"):
            with self.assertRaises(_TranslationStopped):
                t.translate_html(
                    BeautifulSoup(
                        "<html><body><p>Hello world test chunk here</p></body></html>",
                        "html.parser",
                    ),
                    chapter_number=1,
                    should_stop=lambda: t.engine.calls >= 5,
                )
        self.assertEqual(t.engine.calls, 5)
        self.assertEqual(t.stats["failed_chunks"], 0)


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


class TestWrapperRepair(unittest.TestCase):
    """Cach B: model may append/complete outer wrapper tags (</section></div>);
    these must be repaired (not treated as a structural break) so chunks
    reassemble into valid HTML, while genuine content loss is still rejected.
    """

    def setUp(self):
        self.t = Translator(FakeEngine(), html_structure_min_similarity=0.7,
                            consistency_config={})

    def test_repair_strips_surplus_wrapper_closes(self):
        # Source chunk opens <div><section> but has NO closing wrapper inside it.
        src = ('<div class="galley-rw"><section class="body-rw Chapter-rw" id="c5">'
               '<p><span class="koboSpan" id="k1">Hello world</span></p>')
        # Model "completes" the document by appending closing wrappers.
        out = ('<div class="galley-rw"><section class="body-rw Chapter-rw" id="c5">'
               '<p><span class="koboSpan" id="k1">Xin chào thế giới</span></p>'
               '</section></div>')
        repaired = _repair_wrapper_balance(src, out)
        self.assertNotIn("</section>", repaired)
        self.assertNotIn("</div>", repaired)
        # After repair the tag balance matches the source -> no mismatch.
        self.assertFalse(self.t._has_html_structure_mismatch(src, repaired))

    def test_repair_handles_uppercase_wrapper_close(self):
        src = '<div class="galley-rw"><section id="c5"><p><span>x</span></p>'
        out = ('<div class="galley-rw"><section id="c5"><p><span>y</span></p>'
               '</SECTION></div> ')
        repaired = _repair_wrapper_balance(src, out)
        self.assertNotIn("</section>", repaired.lower())
        self.assertFalse(self.t._has_html_structure_mismatch(src, repaired))

    def test_repair_strips_surplus_wrapper_opens(self):
        src = '<p><span>x</span></p>'
        out = '<div><section><p><span>y</span></p>'
        repaired = _repair_wrapper_balance(src, out)
        self.assertNotIn("<div>", repaired)
        self.assertNotIn("<section>", repaired)

    # --- surplus closes on tags the structural guard ignores (chapter 5 case)

    def test_repair_strips_surplus_paragraph_close(self):
        # Real chapter-5 output: one extra trailing </p>. <p> is excluded from
        # the structural guard, so without this repair it ships straight into
        # the epub as ill-formed XHTML and the chapter stops rendering.
        src = '<h2>第5章 脱了给我康康</h2><p>甲。</p><p>乙。</p>'
        out = '<h2>Chương 5</h2><p>Một.</p><p>Hai.</p></p>'
        repaired = _repair_wrapper_balance(src, out)
        self.assertEqual(repaired, '<h2>Chương 5</h2><p>Một.</p><p>Hai.</p>')
        self.assertFalse(self.t._has_html_structure_mismatch(src, repaired))

    def test_repair_keeps_legitimate_extra_paragraph_pair(self):
        # Splitting one source paragraph into two changes the counts but stays
        # balanced — that is a legal rewrite, not a surplus close.
        src = '<p>a</p><p>b</p>'
        out = '<p>a</p><p>b1</p><p>b2</p>'
        self.assertEqual(_repair_wrapper_balance(src, out), out)

    def test_repair_keeps_close_inherited_from_previous_chunk(self):
        # This chunk opens nothing and inherits its </p> from the chunk before
        # it: the source's own count (here: one close) is the target.
        src = 'tiếp tục</p>'
        out = 'tiếp tục</p>'
        self.assertEqual(_repair_wrapper_balance(src, out), out)
        # ...and an extra one beyond the source's is still stripped.
        out2 = 'tiếp tục</p></p>'
        self.assertEqual(_repair_wrapper_balance(src, out2), 'tiếp tục</p>')

    def test_repair_does_not_confuse_p_with_lookalike_tags(self):
        # </pre> starts with "</p" — the match must be exact.
        src = '<p>a</p>'
        out = '<p>a</p><pre>q</pre></p>'
        self.assertEqual(
            _repair_wrapper_balance(src, out), '<p>a</p><pre>q</pre>'
        )

    def test_translate_chunk_repairs_surplus_paragraph_close(self):
        # End-to-end: the extra </p> must neither trigger a retry nor reach
        # the returned translation.
        src = '<p>Hello world test chunk</p><p>Second paragraph</p>'

        class SurplusPCloseEngine(FakeEngine):
            def translate(self, text):
                self.calls += 1
                return (text.replace("Hello world test chunk", "Xin chào thế giới")
                            .replace("Second paragraph", "Đoạn thứ hai") + "</p>")

        t = Translator(SurplusPCloseEngine(), html_structure_min_similarity=0.7,
                       consistency_config={})
        out = t._translate_chunk(src, chapter_number=5)
        self.assertEqual(t.engine.calls, 1)  # no retry
        self.assertTrue(out.endswith("</p>"))
        self.assertFalse(out.endswith("</p></p>"))

    def test_real_leaf_tag_drop_still_rejected(self):
        # Dropping a real <em> (leaf structural tag) is genuine content loss.
        src = '<p><span>a</span><em><span>b</span></em><span>c</span></p>'
        out = '<p><span>x</span><span>y</span><span>z</span></p>'
        self.assertTrue(self.t._has_html_structure_mismatch(src, out))

    def test_translate_chunk_accepts_surplus_wrapper_close(self):
        # End-to-end through _translate_chunk: a surplus wrapper close must NOT
        # cause a retry, and must be stripped from the returned translation.
        src = ('<div class="galley-rw"><section id="c5">'
               '<p><span class="k">Hello world test chunk</span></p>')

        class SurplusCloseEngine(FakeEngine):
            def translate(self, text):
                self.calls += 1
                return (text.replace("Hello world test chunk", "Xin chào thế giới đoạn kiểm tra")
                            + "</section></div>")

        t = Translator(SurplusCloseEngine(), html_structure_min_similarity=0.7,
                       consistency_config={})
        out = t._translate_chunk(src, chapter_number=5)
        self.assertEqual(t.engine.calls, 1)  # no retry
        self.assertNotIn("</section>", out)
        self.assertNotIn("</div>", out)
        self.assertIn("Xin chào thế giới đoạn kiểm tra", out)

    def test_translate_chunk_stops_on_should_stop(self):
        # A chunk that would retry (structure mismatch) must abort via
        # _TranslationStopped instead of looping, when should_stop fires.
        class BadEngine(FakeEngine):
            def translate(self, text):
                self.calls += 1
                # drop spans -> structure mismatch -> would otherwise retry
                return text.replace('<span class="x">', '').replace('</span>', '')

        state = {"n": 0}

        def should_stop():
            state["n"] += 1
            return state["n"] >= 2  # allow first attempt, then stop

        t = Translator(BadEngine(), html_structure_min_similarity=0.7,
                       consistency_config={})
        with self.assertRaises(_TranslationStopped):
            t._translate_chunk(SRC, should_stop=should_stop)
        # Only the first attempt ran: no backoff sleep, no second attempt.
        self.assertEqual(t.engine.calls, 1)


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


class _TimedFakeEngine(FakeEngine):
    """FakeEngine that actually waits a moment so API timing is measurable."""

    def translate(self, text):
        import time as _t

        _t.sleep(0.001)
        return super().translate(text)


class TestMonitoring(unittest.TestCase):
    def test_stats_track_api_time_and_current_chunk(self):
        soup = BeautifulSoup(
            "<html><body><p>Hello world this is a test</p>"
            "<p>Second paragraph here friend</p></body></html>",
            "html.parser",
        )
        eng = _TimedFakeEngine("good")
        t = Translator(eng, consistency_config={}, html_structure_min_similarity=0.7)
        out = t.translate_html(soup, chapter_number=3)
        self.assertIn("DICH_", out)

        stats = t.get_stats()
        # API timing
        self.assertEqual(stats["api_calls"], eng.calls)
        self.assertGreater(stats["api_calls"], 0)
        self.assertGreater(stats["api_time_total_ms"], 0)
        self.assertGreaterEqual(stats["api_time_avg_ms"], 0)
        # current chunk diff snapshot (last chunk processed)
        cc = stats["current_chunk"]
        self.assertIsNotNone(cc)
        self.assertEqual(cc["chapter"], 3)
        # index must equal total: current_chunk always reflects the final chunk
        self.assertEqual(cc["index"], cc["total"])
        self.assertGreaterEqual(cc["index"], 1)
        self.assertTrue(cc["source"])
        self.assertIn("DICH_", cc["translated"])


class TestUntranslatedGuard(unittest.TestCase):
    """Model echoes (verbatim or with trivial edits) must be rejected and
    retried; tolerated residue (proper nouns) and number-only chunks must not
    false-positive."""

    ZH_SRC = (
        "<p>顾天明和苏慕容破天荒地提早回了家看着满桌子的菜什么山珍海味"
        "鲍鱼龙虾应有尽有顾言嘴角一抽地向自己爸妈问道今天是什么日子</p>"
    )

    def setUp(self):
        self.t = Translator(FakeEngine(), consistency_config={})

    def test_verbatim_echo_rejected(self):
        self.assertTrue(self.t._has_untranslated_content(self.ZH_SRC, self.ZH_SRC))

    def test_trivial_edit_echo_rejected(self):
        # The reported case: echo with a couple of words changed.
        out = self.ZH_SRC.replace("顾言嘴角一抽", "顾言笑了笑").replace(
            "今天是什么日子", "今天是什么好日子啊")
        self.assertTrue(self.t._has_untranslated_content(self.ZH_SRC, out))

    def test_faithful_translation_accepted(self):
        out = ("<p>Cố Thiên Minh và Tô Mộ Dung hiếm hoi về nhà sớm, nhìn mâm cơm "
               "đầy sơn hào hải vị, Cố Ngôn giật khóe miệng hỏi cha mẹ hôm nay là ngày gì</p>")
        self.assertFalse(self.t._has_untranslated_content(self.ZH_SRC, out))

    def test_small_proper_noun_residue_tolerated(self):
        src = "<p>Chào buổi sáng mọi người hôm nay chúng ta họp ở đây nhé bạn</p>"
        out = "<p>Chào buổi sáng 顾言 hôm nay chúng ta họp ở đây nhé bạn</p>"
        self.assertFalse(self.t._has_untranslated_content(src, out))

    def test_numbers_only_echo_accepted(self):
        src = "<p>123 !!! 456</p>"
        self.assertFalse(self.t._has_untranslated_content(src, src))

    def test_english_echo_rejected_language_agnostic(self):
        src = ("<p>Hello world, this is a long test sentence for echo detection, "
               "yes indeed my friend</p>")
        self.assertTrue(self.t._has_untranslated_content(src, src))
        out = ("<p>Xin chào thế giới, đây là câu kiểm tra dài để phát hiện echo, "
               "đúng vậy bạn tôi</p>")
        self.assertFalse(self.t._has_untranslated_content(src, out))

    def test_retry_prompt_mentions_rejection_and_target_lang(self):
        prompt = Translator._build_retry_input("chunk", reason="untranslated", to_lang="VI")
        self.assertIn("REJECTED", prompt)
        self.assertIn("VI", prompt)
        self.assertIn("chunk", prompt)
        # default reason keeps the legacy structure-focused prefix
        legacy = Translator._build_retry_input("chunk")
        self.assertNotIn("REJECTED", legacy)
        self.assertIn("chunk", legacy)


class EchoThenGoodEngine(TranslationEngine):
    """Echoes the input chunk unchanged for the first `echo_times` calls, then
    returns a CJK-free Vietnamese replacement preserving tags."""

    def __init__(self, echo_times=1):
        super().__init__(from_lang="ZH", to_lang="VI")
        self.calls = 0
        self.echo_times = echo_times
        self.last_inputs = []

    def supports_batch(self):
        return False

    def translate(self, text):
        self.calls += 1
        self.last_inputs.append(text)
        chunk = text.split("Input:\n", 1)[1] if "Input:\n" in text else text
        if self.calls <= self.echo_times:
            return chunk
        return re.sub(r"(?<=>)[^<>]+(?=<)", "Bản dịch tiếng Việt ở đây", chunk)


class TestUntranslatedRetryLoop(unittest.TestCase):
    ZH_SOUP = (
        "<html><body><p>顾天明和苏慕容破天荒地提早回了家看着满桌子的菜"
        "什么山珍海味鲍鱼龙虾应有尽有顾言嘴角一抽地向自己爸妈问道</p>"
        "</body></html>"
    )

    def test_echo_retried_then_accepted(self):
        eng = EchoThenGoodEngine(echo_times=1)
        t = Translator(eng, max_tries=3, consistency_config={})
        with mock.patch("time.sleep"):
            out = t.translate_html(BeautifulSoup(self.ZH_SOUP, "html.parser"),
                                   chapter_number=1)
        self.assertEqual(eng.calls, 2)
        self.assertIn("Bản dịch tiếng Việt", out)
        self.assertNotRegex(out, r"[\u4e00-\u9fff]")
        # the retry used the untranslated-specific prompt
        self.assertIn("REJECTED", eng.last_inputs[1])

    def test_untranslated_bypasses_max_tries_but_stoppable(self):
        eng = EchoThenGoodEngine(echo_times=10 ** 9)  # never translates
        t = Translator(eng, max_tries=3, consistency_config={})
        with mock.patch("time.sleep"):
            with self.assertRaises(_TranslationStopped):
                t.translate_html(
                    BeautifulSoup(self.ZH_SOUP, "html.parser"),
                    chapter_number=1,
                    should_stop=lambda: eng.calls >= 5,
                )
        # 5 attempts despite max_tries=3: untranslated never gives up on its own
        self.assertEqual(eng.calls, 5)

    def test_manual_queue_recorded_periodically_while_stuck(self):
        import translator.translator as tr_mod

        eng = EchoThenGoodEngine(echo_times=10 ** 9)
        t = Translator(eng, max_tries=3, consistency_config={})
        with mock.patch("time.sleep"), \
                mock.patch.object(t, "_record_manual_translation_task") as rec, \
                mock.patch.object(tr_mod, "UNTRANSLATED_MANUAL_QUEUE_EVERY", 3):
            with self.assertRaises(_TranslationStopped):
                t.translate_html(
                    BeautifulSoup(self.ZH_SOUP, "html.parser"),
                    chapter_number=1,
                    should_stop=lambda: eng.calls >= 7,
                )
        # streaks 3 and 6 hit the periodic record; the loop keeps retrying
        self.assertEqual(rec.call_count, 2)
        self.assertEqual(eng.calls, 7)


if __name__ == "__main__":
    unittest.main(verbosity=2)
