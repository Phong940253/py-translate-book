"""Offline unit tests for translator.global_rules + chapter-scoped prompts.

Covers:
  GR-1  : to_prompt_text keeps the documented shape (names / rules / tail).
  GR-2  : budget caps the prompt and the narration/fallback block survives it.
  GR-3  : scope (`only`) renders just those characters and only their rules —
          pairs whose addressee is outside the scope are noise for the chapter.
  GR-4  : an empty/unknown scope falls back to the budgeted full dictionary
          rather than an empty <CHAPTER_RULES> block.
  GR-5  : chars_in_analysis understands all three shapes the LLM emits.
  GR-6  : analyses are stored with a content hash and reused across restarts;
          old rules.json files (string keys, no hash, no last_seen) still load.
  GR-7  : Translator injects scoped rules within rules_prompt_max_chars and
          skips the analysis LLM call when the stored hash matches.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translator.engines.base import TranslationEngine
from translator.global_rules import (
    CharacterEntry,
    GlobalRulesManager,
)
from translator.translator import Translator


CHAPTER_ANALYSIS = (
    "1) Characters: Bạch Thanh Hạ, Lục Viễn Thu\n"
    "2) Address rules:\n"
    "Bạch Thanh Hạ -> Lục Viễn Thu: tôi - anh\n"
    "Lục Viễn Thu -> Bạch Thanh Hạ: em - tôi\n"
    "3) Narration\n"
    "Bạch Thanh Hạ: hắn\n"
    "4) Default fallback: tôi - người khác"
)


def _manager(tmpdir, name="rules.json"):
    return GlobalRulesManager(path=os.path.join(tmpdir, name))


def _fill(mgr, count, calls_per_char=8):
    """A dictionary big enough that the budget has to bite."""
    for i in range(count):
        mgr.state.characters[f"Nhân vật {i:03d}"] = CharacterEntry(
            names=[f"Nhân vật {i:03d}"],
            calls={
                f"Đối phương {j}": f"tao - mày {'x' * 30} {j}" for j in range(calls_per_char)
            },
            last_seen=i,
        )
    mgr.state.narration_defaults = {"Nhân vật 000": "hắn"}
    mgr.state.fallback = "tôi - người khác"


class TestPromptRendering(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.mgr = _manager(self.tmp)
        self.mgr.merge_from_analysis(CHAPTER_ANALYSIS, chapter_number=10)

    def test_full_render_keeps_the_documented_shape(self):
        text = self.mgr.to_prompt_text()
        self.assertIn("Characters: Bạch Thanh Hạ, Lục Viễn Thu", text)
        self.assertIn("Bạch Thanh Hạ -> Lục Viễn Thu: tôi - anh", text)
        self.assertIn("Lục Viễn Thu -> Bạch Thanh Hạ: em - tôi", text)
        self.assertIn("Narration defaults:", text)
        self.assertIn("Default fallback: tôi - người khác", text)

    def test_scope_renders_only_those_characters_and_their_pairs(self):
        both = self.mgr.to_prompt_text(only={"Bạch Thanh Hạ", "Lục Viễn Thu"})
        self.assertIn("Bạch Thanh Hạ -> Lục Viễn Thu", both)
        self.assertIn("Lục Viễn Thu -> Bạch Thanh Hạ", both)

        one = self.mgr.to_prompt_text(only={"Bạch Thanh Hạ"})
        self.assertIn("Characters: Bạch Thanh Hạ", one)
        self.assertNotIn("Lục Viễn Thu ->", one)          # speaker out of scope
        self.assertNotIn("-> Lục Viễn Thu", one)          # addressee out of scope
        # the small tail never gets scoped away
        self.assertIn("Default fallback: tôi - người khác", one)

    def test_scope_accepts_a_composite_addressee(self):
        _fill(self.mgr, 2)
        self.mgr.state.characters["Nhân vật 000"].calls = {
            "Nhân vật 001, Lục Viễn Thu": "tao - anh em"
        }
        text = self.mgr.to_prompt_text(only={"Nhân vật 000", "Nhân vật 001"})
        self.assertIn("Nhân vật 001, Lục Viễn Thu: tao - anh em", text)

    def test_unknown_or_empty_scope_falls_back_to_the_full_dictionary(self):
        full = self.mgr.to_prompt_text()
        self.assertEqual(self.mgr.to_prompt_text(only=set()), full)
        self.assertEqual(self.mgr.to_prompt_text(only={"Không tồn tại"}), full)

    def test_budget_caps_the_prompt_and_keeps_the_tail(self):
        _fill(self.mgr, count=120)
        for budget in (4000, 12000, 20000):
            text = self.mgr.to_prompt_text(budget=budget)
            self.assertLessEqual(len(text), budget, f"budget {budget}")
            self.assertTrue(text.rstrip().endswith("tôi - người khác"), text[-80:])

    def test_budget_keeps_the_most_recent_characters_first(self):
        _fill(self.mgr, count=60)
        text = self.mgr.to_prompt_text(budget=400)
        self.assertLessEqual(len(text), 400)
        header = text.split("Address rules", 1)[0]
        self.assertIn("Nhân vật 059", header)
        self.assertNotIn("Nhân vật 000", header)

    def test_budget_smaller_than_the_tail_still_returns_the_tail(self):
        text = self.mgr.to_prompt_text(budget=10)
        self.assertIn("Default fallback: tôi - người khác", text)

    def test_render_is_deterministic(self):
        _fill(self.mgr, count=40)
        first = self.mgr.to_prompt_text(only={"Nhân vật 001", "Nhân vật 039"}, budget=3000)
        second = self.mgr.to_prompt_text(only={"Nhân vật 039", "Nhân vật 001"}, budget=3000)
        self.assertEqual(first, second)


class TestCharsInAnalysis(unittest.TestCase):
    def test_section_one_with_and_without_the_characters_label(self):
        labelled = "1) Characters: A, B, C"
        bare = "1) A, B, C"
        self.assertEqual(
            GlobalRulesManager.chars_in_analysis(labelled), {"A", "B", "C"}
        )
        self.assertEqual(GlobalRulesManager.chars_in_analysis(bare), {"A", "B", "C"})

    def test_bare_list_first_line(self):
        text = "Lục Viễn Thu, Bạch Thanh Hạ, Cậu bé\nLục Viễn Thu -> Cậu bé: tao - mày"
        self.assertEqual(
            GlobalRulesManager.chars_in_analysis(text),
            {"Lục Viễn Thu", "Bạch Thanh Hạ", "Cậu bé"},
        )

    def test_address_rules_yield_both_sides_and_drop_other_sections(self):
        self.assertEqual(
            GlobalRulesManager.chars_in_analysis(CHAPTER_ANALYSIS),
            {"Bạch Thanh Hạ", "Lục Viễn Thu"},
        )

    def test_known_intersection_and_empty_input(self):
        self.assertEqual(
            GlobalRulesManager.chars_in_analysis(CHAPTER_ANALYSIS, known={"Bạch Thanh Hạ"}),
            {"Bạch Thanh Hạ"},
        )
        self.assertEqual(GlobalRulesManager.chars_in_analysis(""), set())


class TestAnalysisPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_hash_gates_reuse_and_round_trips(self):
        mgr = _manager(self.tmp)
        mgr.merge_from_analysis(CHAPTER_ANALYSIS, chapter_number=5, content_hash="h1")
        mgr.save()
        self.assertTrue(mgr.has_analysis(5, "h1"))
        self.assertFalse(mgr.has_analysis(5, "h2"))     # chapter text changed
        self.assertFalse(mgr.has_analysis(6, "h1"))     # another chapter
        self.assertFalse(mgr.has_analysis(5, None))

        reloaded = _manager(self.tmp)
        reloaded.load()
        # JSON stringifies keys — lookups must still work after a restart
        self.assertEqual(reloaded.get_analysis(5), CHAPTER_ANALYSIS)
        self.assertTrue(reloaded.has_analysis(5, "h1"))

    def test_legacy_rules_json_still_loads(self):
        path = os.path.join(self.tmp, "rules.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "characters": {
                        "Bạch Thanh Hạ": {
                            "names": ["Bạch Thanh Hạ"],
                            "calls": {"Lục Viễn Thu": "tôi - anh"},
                            "pronouns": {},
                            "notes": "",
                        }
                    },
                    "narration_defaults": {},
                    "fallback": "tôi - người khác",
                    "version": 1,
                    "updated_at": "",
                    "_chapter_analyses": {"5": CHAPTER_ANALYSIS},
                },
                fh,
                ensure_ascii=False,
            )
        mgr = _manager(self.tmp)
        mgr.load()
        self.assertEqual(mgr.character_count, 1)
        self.assertEqual(mgr.get_analysis(5), CHAPTER_ANALYSIS)
        self.assertFalse(mgr.has_analysis(5, "anything"))  # no hash recorded yet
        self.assertIn("Bạch Thanh Hạ", mgr.to_prompt_text())

    def test_last_seen_tracks_the_chapter_and_orders_by_recency(self):
        mgr = _manager(self.tmp)
        mgr.merge_from_analysis(CHAPTER_ANALYSIS, chapter_number=3)
        self.assertEqual(mgr.state.characters["Bạch Thanh Hạ"].last_seen, 3)
        mgr.merge_from_analysis("1) Characters: Bạch Thanh Hạ", chapter_number=9)
        self.assertEqual(mgr.state.characters["Bạch Thanh Hạ"].last_seen, 9)
        # an older chapter must not move the clock backwards
        mgr.merge_from_analysis("1) Characters: Bạch Thanh Hạ", chapter_number=2)
        self.assertEqual(mgr.state.characters["Bạch Thanh Hạ"].last_seen, 9)


class AnalyzingEngine(TranslationEngine):
    """Engine that can analyse — records how often it was asked to."""

    def __init__(self):
        super().__init__(from_lang="ZH", to_lang="VI")
        self.analysis_calls = 0

    def translate(self, text):
        return text

    def analyze_chapter_consistency(self, excerpt):
        self.analysis_calls += 1
        return CHAPTER_ANALYSIS


class PlainEngine(TranslationEngine):
    """No consistency support at all."""

    def __init__(self):
        super().__init__(from_lang="ZH", to_lang="VI")

    def translate(self, text):
        return text


class TestChapterRulesPrompt(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = os.path.join(self.tmp, "book.rules.json")
        self.content = "<p>龙怜冬看着白清夏。</p>" * 40

    def _translator(self, engine, **overrides):
        config = {
            "enabled": True,
            "rules_prompt_max_chars": 4000,
            "rules_filter_by_chapter": True,
        }
        config.update(overrides)
        return Translator(
            engine, consistency_config=config, global_rules_path=self.path
        )

    def test_injects_scoped_rules_within_the_budget(self):
        engine = AnalyzingEngine()
        rules = self._translator(engine)._analyze_chapter_consistency(
            self.content, chapter_number=7
        )
        self.assertEqual(engine.analysis_calls, 1)
        self.assertLessEqual(len(rules), 4000)
        self.assertIn("Bạch Thanh Hạ", rules)
        self.assertIn("Default fallback: tôi - người khác", rules)

    def test_second_run_reuses_the_stored_analysis(self):
        first = AnalyzingEngine()
        text_first = self._translator(first)._analyze_chapter_consistency(
            self.content, chapter_number=7
        )

        # a fresh Translator = a restarted job: same rules file, same chapter
        second = AnalyzingEngine()
        text_second = self._translator(second)._analyze_chapter_consistency(
            self.content, chapter_number=7
        )
        self.assertEqual(second.analysis_calls, 0, "stored analysis must be reused")
        self.assertEqual(text_first, text_second)

    def test_changed_chapter_text_triggers_a_new_analysis(self):
        self._translator(AnalyzingEngine())._analyze_chapter_consistency(
            self.content, chapter_number=7
        )
        engine = AnalyzingEngine()
        self._translator(engine)._analyze_chapter_consistency(
            self.content + "<p>khác</p>", chapter_number=7
        )
        self.assertEqual(engine.analysis_calls, 1)

    def test_engine_without_analysis_still_gets_the_rules(self):
        self._translator(AnalyzingEngine())._analyze_chapter_consistency(
            self.content, chapter_number=7
        )
        rules = self._translator(PlainEngine())._analyze_chapter_consistency(
            self.content, chapter_number=7
        )
        self.assertIn("Bạch Thanh Hạ", rules)   # used to return ""

    def test_filter_can_be_turned_off(self):
        engine = AnalyzingEngine()
        rules = self._translator(engine, rules_filter_by_chapter=False)._analyze_chapter_consistency(
            self.content, chapter_number=7
        )
        self.assertIn("Characters:", rules)
        self.assertLessEqual(len(rules), 4000)

    def test_disabled_consistency_returns_nothing(self):
        engine = AnalyzingEngine()
        translator = Translator(
            engine,
            consistency_config={"enabled": False},
            global_rules_path=self.path,
        )
        self.assertEqual(translator._analyze_chapter_consistency(self.content, 7), "")
        self.assertEqual(engine.analysis_calls, 0)


if __name__ == "__main__":
    unittest.main()
