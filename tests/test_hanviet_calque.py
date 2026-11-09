"""Offline unit tests for the Hán-Việt calque guard (no AI calls).

HC-1 : the reported failure — every Chinese word rendered as its
       Sino-Vietnamese sound with Chinese word order kept ("Từ hoả xa trạm
       xuất lai hậu ...") is detected and rejected.
HC-2 : a natural Vietnamese translation of the same source passes.
HC-3 : legitimate compounds (đích thân, vấn đề, hòa bình, khởi hành,
       chi phí) never trip the guard.
HC-4 : non-Chinese sources and calque_check=False skip the check.
HC-5 : the calque retry prompt points at the markers and demands natural
       Vietnamese syntax; the loop retries a calque past max_tries and
       never accepts one into the output.
HC-6 : the detection lives in translator.calque and runs without a
       Translator instance, so offline verify scripts can apply the very
       same markers/thresholds the job rejects with.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bs4 import BeautifulSoup

from translator.translator import (
    Translator,
    _CalqueOutput,
    _TranslationStopped,
    _UntranslatedOutput,
)
from translator.calque import detect_hanviet_calque, find_calque_markers
from translator.engines.base import TranslationEngine

# The source the reported chapter was translated from.
CALQUE_SRC = (
    "<p>从火车站出来后，陈有义和宋语薇直接坐车来到北邮大学附近。"
    "网上的酒店就在这边，先到酒店把多余的东西放下。"
    "鼓鼓囊囊的背包瘪下来，只留了饭盒和水杯。"
    "陈有义这时才看到她背包里的东西，问：“带饭盒干什么？”"
    "宋语薇笑着解释：“我做了一些吃的，网上说景点东西都很贵，"
    "我就自己带一点。”</p>"
)

# The reported bad output: Sino readings in Chinese word order.
CALQUE_OUT = (
    "<p>Từ hoả xa trạm xuất lai hậu, Trần Hữu Hợi hòa Tống Ngữ Vi tựu "
    "trực tiếp tọa xa lai đáo Bắc Du đại học phụ cận. Vãng thượng đính "
    "đích tửu điên tựu tại giá biên, tiên đáo tửu điên bả đa dư đích "
    "đông phóng hạ. Cổ cổ nang nang đích bôi bao miết hạ lai, chỉ lưu "
    "phạn hộp hòa thủy bôi. Trần Hữu Hợi giá thời tài khán đáo cô bôi "
    "bao lý đích đông tây, vấn: đái phạn hộp càn thập ma? Tống Ngữ Vi "
    "tiếu trứ giải thích: ngã tố liễu nhất ta cật đích, vãng thượng "
    "thuyết cảnh điểm đông tây đô hận quý, ngã tựu tự kỷ đái nhất điểm."
    "</p>"
)

# The same passage written as real Vietnamese.
GOOD_OUT = (
    "<p>Sau khi ra khỏi nhà ga, Trần Hữu Hợi và Tống Ngữ Vi ngồi xe "
    "thẳng đến gần Đại học Bắc Du. Khách sạn đặt trên mạng ở ngay đây, "
    "hai người đến khách sạn trước để cất đồ đạc thừa. Chiếc ba lô căng "
    "phồng xẹp xuống, chỉ để lại hộp cơm và cốc nước. Trần Hữu Hợi lúc "
    "này mới nhìn thấy đồ trong ba lô của cô, hỏi: “Mang hộp cơm làm gì?” "
    "Tống Ngữ Vi cười giải thích: “Em làm vài món ăn, nghe nói đồ ở điểm "
    "du lịch đắt lắm nên em tự mang theo một ít.”</p>"
)


class TestCalqueDetection(unittest.TestCase):
    def setUp(self):
        self.t = Translator(_SilentEngine(), consistency_config={})

    def test_reported_calque_is_detected(self):
        hits = self.t._detect_hanviet_calque(CALQUE_SRC, CALQUE_OUT)
        self.assertTrue(hits, "the reported output must be rejected")
        self.assertIn("đích", hits)
        self.assertIn("tựu", hits)

    def test_natural_vietnamese_passes(self):
        self.assertEqual(self.t._detect_hanviet_calque(CALQUE_SRC, GOOD_OUT), [])

    def test_source_with_tags_still_detected(self):
        # The guard must look at visible text only, tags stripped.
        self.assertTrue(
            self.t._detect_hanviet_calque(CALQUE_SRC, CALQUE_OUT)
        )

    def test_legit_compounds_never_trip(self):
        out = (
            "<p>Hắn đích thân tới xử lý vấn đề hòa bình, khởi hành đúng "
            "chi phí; đái phạn hộp thì không, cô ấy mang theo thức ăn."
            "</p>"
        )
        # đích thân / vấn đề / hòa bình / khởi hành / chi phí are all
        # excluded by the lookaheads — only "bả"-free plain words remain.
        self.assertEqual(self.t._detect_hanviet_calque(CALQUE_SRC, out), [])

    def test_markers_below_threshold_accepted(self):
        # A single stray marker (a surname, a literary word) is not proof.
        out = "<p>Trần Hữu Hợi ngồi xuống, Liễu Y nói vài câu rồi đi.</p>"
        self.assertEqual(self.t._detect_hanviet_calque(CALQUE_SRC, out), [])

    def test_non_chinese_source_skips_the_check(self):
        src = "<p>The train station was crowded this morning, so they took a taxi.</p>"
        self.assertEqual(self.t._detect_hanviet_calque(src, CALQUE_OUT), [])

    def test_calque_check_disabled(self):
        t = Translator(
            _SilentEngine(),
            consistency_config={"calque_check": False},
        )
        self.assertEqual(t._detect_hanviet_calque(CALQUE_SRC, CALQUE_OUT), [])

    def test_empty_output_accepted(self):
        self.assertEqual(self.t._detect_hanviet_calque(CALQUE_SRC, ""), [])


class _SilentEngine(TranslationEngine):
    def __init__(self):
        super().__init__(from_lang="ZH", to_lang="VI")

    def translate(self, text):  # pragma: no cover - never called
        return text


class CalqueThenGoodEngine(TranslationEngine):
    """Returns the reported calque for the first `calque_times` calls, then a
    natural Vietnamese translation preserving the input's tags."""

    def __init__(self, calque_times=1):
        super().__init__(from_lang="ZH", to_lang="VI")
        self.calls = 0
        self.calque_times = calque_times
        self.last_inputs = []

    def supports_batch(self):
        return False

    def translate(self, text):
        self.calls += 1
        self.last_inputs.append(text)
        if self.calls <= self.calque_times:
            return CALQUE_OUT
        return GOOD_OUT


class TestCalqueRetryLoop(unittest.TestCase):
    ZH_SOUP = "<html><body>" + CALQUE_SRC + "</body></html>"

    def test_calque_rejected_then_good_accepted(self):
        eng = CalqueThenGoodEngine(calque_times=1)
        t = Translator(eng, max_tries=3, consistency_config={})
        with mock.patch("time.sleep"):
            out = t.translate_html(
                BeautifulSoup(self.ZH_SOUP, "html.parser"), chapter_number=1
            )
        self.assertEqual(eng.calls, 2, "the calque attempt must be retried")
        self.assertIn("Sau khi ra khỏi nhà ga", out)
        self.assertNotIn("đích tửu điên", out)
        # the retry prompt points at the failure
        self.assertIn("REJECTED", eng.last_inputs[1])
        self.assertIn("Hán-Việt", eng.last_inputs[1])

    def test_calque_bypasses_max_tries_but_stoppable(self):
        eng = CalqueThenGoodEngine(calque_times=10 ** 9)  # never translates
        t = Translator(eng, max_tries=3, consistency_config={})
        with mock.patch("time.sleep"):
            with self.assertRaises(_TranslationStopped):
                t.translate_html(
                    BeautifulSoup(self.ZH_SOUP, "html.parser"),
                    chapter_number=1,
                    should_stop=lambda: eng.calls >= 5,
                )
        # 5 attempts despite max_tries=3: a calque can never be accepted
        self.assertEqual(eng.calls, 5)

    def test_calque_error_is_an_untranslated_subclass(self):
        # Inherits the never-give-up policy (infinite retry, manual queue,
        # chunk splitting) from _UntranslatedOutput.
        self.assertTrue(issubclass(_CalqueOutput, _UntranslatedOutput))

    def test_retry_prompt_names_the_markers(self):
        prompt = Translator._build_retry_input(
            "chunk", reason="calque", to_lang="VI", hits=["đích", "tựu"]
        )
        self.assertIn("REJECTED", prompt)
        self.assertIn("đích, tựu", prompt)
        self.assertIn("VI", prompt)
        self.assertIn("chunk", prompt)
        self.assertIn("Hán-Việt", prompt)


class TestSharedCalqueModule(unittest.TestCase):
    """HC-6: translator.calque is the single source of truth.

    Verify scripts outside a job import the same functions, so a chapter
    review can never drift from what the job itself rejects.
    """

    def test_detect_works_without_a_translator(self):
        hits = detect_hanviet_calque(CALQUE_SRC, CALQUE_OUT)
        self.assertTrue(hits, "the reported output must be rejected")
        self.assertIn("đích", hits)
        self.assertIn("tựu", hits)

    def test_good_translation_passes(self):
        self.assertEqual(detect_hanviet_calque(CALQUE_SRC, GOOD_OUT), [])

    def test_non_chinese_source_skips(self):
        src = "<p>The train station was crowded this morning.</p>"
        self.assertEqual(detect_hanviet_calque(src, CALQUE_OUT), [])

    def test_empty_arguments_pass(self):
        self.assertEqual(detect_hanviet_calque("", CALQUE_OUT), [])
        self.assertEqual(detect_hanviet_calque(CALQUE_SRC, ""), [])

    def test_find_markers_reports_sub_threshold_text(self):
        # Chapter-level reviews want the raw occurrences even below the
        # rejection thresholds (the "bả vai"/"càn quét" review case).
        text = (
            "<p>Quay người bước qua bả vai, né đợt càn quét, "
            "trả lời vấn đề hòa bình.</p>"
        )
        self.assertEqual(find_calque_markers(text), ["bả", "càn"])

    def test_guard_and_module_agree(self):
        t = Translator(_SilentEngine(), consistency_config={})
        self.assertEqual(
            t._detect_hanviet_calque(CALQUE_SRC, CALQUE_OUT),
            detect_hanviet_calque(CALQUE_SRC, CALQUE_OUT),
        )


if __name__ == "__main__":
    unittest.main()
