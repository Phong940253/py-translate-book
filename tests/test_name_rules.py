# -*- coding: utf-8 -*-
"""Offline unit tests for name canonicalisation (glossary / pinyin guard).

Covers:
  NR-1  : 'CN=VN' items split correctly (parsing, scoping, pairs).
  NR-2  : merge seeds the glossary and parks unmapped Chinese/pinyin names.
  NR-3  : canonical_name folds variants, honours the whitelist, passes plain
          names through, refuses unmapped Chinese/pinyin.
  NR-4  : looks_like_pinyin fires only on all-pinyin runs; the merge gate
          additionally catches a pinyin token no Vietnamese name uses.
  NR-5  : detect_pinyin_names flags 'Anh Qiu'/'Bai Qing Xia' but neither a
          proper Vietnamese name nor a whitelisted form.
  NR-6  : glossary_text lists only the Chinese forms the chapter uses and
          stays inside its budget.
  NR-7  : mapping() carries Vietnamese canonicals only (never pure Chinese);
          mixed-script garble ("Thẩm梨初") does map.
  NR-8  : glossary/whitelist survive save/load; hand edits on disk win the
          next save.
  NR-9  : sanitize parks Chinese keys, merges diacritic duplicates, keeps
          dangling addressees as bare keys, drops unmappable targets.
  NR-10 : apply_repair turns a repair reply into glossary + whitelist.
  NR-11 : the pinyin retry prompt carries the correction block.
  NR-12 : _render_chapter_rules opens with the glossary and stays in budget.
  NR-13 : _detect_pinyin_names only runs on Chinese-source chunks.
  NR-14 : normalize_names_in_html is longest-first and never rewrites
          Chinese (第N章 headings); mixed-script garble does get fixed;
          the book pass leaves binaries alone.
"""
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translator.epub_utils import normalize_names_in_book, normalize_names_in_html
from translator.engines.base import TranslationEngine
from translator.global_rules import (
    CharacterEntry,
    GlobalRulesManager,
    has_cjk,
    looks_like_pinyin,
    parse_repair_output,
    split_name_pair,
)
from translator.translator import (
    PINYIN_MAPPING_REFUSAL_AFTER,
    PINYIN_UNKNOWN_AUTO_WHITELIST_AFTER,
    Translator,
)

PAIR_ANALYSIS = (
    "1) Characters: 白清夏=Bạch Thanh Hạ, 龙怜冬=Long Lân Đông\n"
    "2) Address rules:\n"
    "白清夏 -> 龙怜冬: tôi - anh\n"
    "3) Narration\n"
    "Bạch Thanh Hạ: hắn\n"
    "4) Default fallback: tôi - người khác"
)

PLAIN_ANALYSIS = (
    "1) Characters: Bạch Thanh Hạ, Lục Viễn Thu\n"
    "2) Address rules:\n"
    "Bạch Thanh Hạ -> Lục Viễn Thu: tôi - anh\n"
    "3) Narration\n"
    "Bạch Thanh Hạ: hắn\n"
    "4) Default fallback: tôi - người khác"
)


def _manager(tmpdir, name="rules.json"):
    return GlobalRulesManager(path=os.path.join(tmpdir, name))


class AnalyzingEngine(TranslationEngine):
    def __init__(self):
        super().__init__(from_lang="ZH", to_lang="VI")
        self.analysis_calls = 0

    def translate(self, text):
        return text

    def analyze_chapter_consistency(self, excerpt):
        self.analysis_calls += 1
        return PLAIN_ANALYSIS


class TestNamePairParsing(unittest.TestCase):
    """NR-1"""

    def test_split_name_pair_shapes(self):
        self.assertEqual(
            split_name_pair("白清夏=Bạch Thanh Hạ"), ("白清夏", "Bạch Thanh Hạ")
        )
        self.assertEqual(
            split_name_pair("Bai Qing Xia=Bạch Thanh Hạ"),
            ("Bai Qing Xia", "Bạch Thanh Hạ"),
        )
        self.assertEqual(split_name_pair("Bạch Thanh Hạ"), (None, "Bạch Thanh Hạ"))
        self.assertEqual(split_name_pair("Bạch Thanh Hạ="), (None, "Bạch Thanh Hạ"))

    def test_scoping_takes_the_vietnamese_side(self):
        scoped = GlobalRulesManager.chars_in_analysis(
            "1) Characters: 白清夏=Bạch Thanh Hạ, Anh Qiu"
        )
        self.assertEqual(scoped, {"Bạch Thanh Hạ", "Anh Qiu"})

    def test_address_rules_still_yield_both_sides(self):
        self.assertEqual(
            GlobalRulesManager.chars_in_analysis(PAIR_ANALYSIS),
            {"Bạch Thanh Hạ", "Long Lân Đông"},
        )


class TestMergeSeedsAndParks(unittest.TestCase):
    """NR-2"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.mgr = _manager(self.tmp)

    def test_cn_vn_pairs_seed_the_glossary_and_the_keys(self):
        self.mgr.merge_from_analysis(PAIR_ANALYSIS, chapter_number=10)

        glossary = {e["canonical"]: e for e in self.mgr.glossary}
        self.assertIn("Bạch Thanh Hạ", glossary)
        self.assertIn("白清夏", glossary["Bạch Thanh Hạ"]["cn"])
        self.assertIn("Long Lân Đông", glossary)
        self.assertIn("龙怜冬", glossary["Long Lân Đông"]["cn"])

        # the Vietnamese side becomes the key, the Chinese side never does
        self.assertIn("Bạch Thanh Hạ", self.mgr.state.characters)
        self.assertIn("Long Lân Đông", self.mgr.state.characters)
        self.assertNotIn("白清夏", self.mgr.state.characters)
        self.assertNotIn("龙怜冬", self.mgr.state.characters)

        # the Chinese speaker/target of the address rule resolved too
        entry = self.mgr.state.characters["Bạch Thanh Hạ"]
        self.assertIn("Long Lân Đông", entry.calls)
        self.assertEqual(self.mgr.pending, {})

    def test_unmapped_chinese_and_pinyin_stay_pending(self):
        self.mgr.merge_from_analysis(
            "1) Characters: 陆远秋\n2) Address rules:\n"
            "陆远秋 -> Bai Qing Xia: tôi - anh",
            chapter_number=11,
        )
        self.assertNotIn("陆远秋", self.mgr.state.characters)
        self.assertNotIn("Bai Qing Xia", self.mgr.state.characters)
        self.assertEqual(self.mgr.pending.get("陆远秋"), "unmapped_chinese")
        # 'Bai Qing Xia' is all-pinyin, 'Anh Qiu' carries an unknown pinyin token
        self.mgr.merge_from_analysis("1) Characters: Anh Qiu", chapter_number=12)
        self.assertNotIn("Anh Qiu", self.mgr.state.characters)
        self.assertEqual(self.mgr.pending.get("Anh Qiu"), "pinyin")
        # no rules were taught for those names either
        self.assertEqual(
            [e for e in self.mgr.state.characters.values() if e.calls], []
        )

    def test_plain_new_names_still_become_keys(self):
        self.mgr.merge_from_analysis(
            "1) Characters: Trần Lệ, Bai Qing Xia", chapter_number=13
        )
        self.assertIn("Trần Lệ", self.mgr.state.characters)
        self.assertNotIn("Bai Qing Xia", self.mgr.state.characters)


class TestCanonicalName(unittest.TestCase):
    """NR-3"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.mgr = _manager(self.tmp)
        self.mgr.add_glossary_entry("Long Lân Đông", cn="龙怜冬", variant="Long Lệ Đông")
        self.mgr.add_glossary_entry("Bạch Thanh Hạ", cn="白清夏")

    def test_variants_and_folds_resolve_to_the_canonical(self):
        self.assertEqual(self.mgr.canonical_name("Long Lệ Đông"), "Long Lân Đông")
        self.assertEqual(self.mgr.canonical_name("Long Lân Đông"), "Long Lân Đông")
        # diacritic-less spelling of a known name folds onto it
        self.assertEqual(self.mgr.canonical_name("Bach Thanh Ha"), "Bạch Thanh Hạ")
        # Chinese side resolves too
        self.assertEqual(self.mgr.canonical_name("龙怜冬"), "Long Lân Đông")

    def test_whitelist_plain_names_and_refusals(self):
        self.mgr.add_to_whitelist("Bai Songzhe")
        self.assertEqual(self.mgr.canonical_name("Bai Songzhe"), "Bai Songzhe")
        self.assertEqual(self.mgr.canonical_name("Trần Lệ"), "Trần Lệ")
        self.assertIsNone(self.mgr.canonical_name("陆远秋"))     # unmapped Chinese
        self.assertIsNone(self.mgr.canonical_name("Bai Qing Xia"))  # pinyin
        # pure reads must not park anything
        self.assertEqual(self.mgr.pending, {})


class TestPinyinGates(unittest.TestCase):
    """NR-4 / NR-5"""

    def test_looks_like_pinyin_needs_every_token(self):
        self.assertTrue(looks_like_pinyin("Bai Qing Xia"))
        self.assertTrue(looks_like_pinyin("Cao Shuang"))
        self.assertFalse(looks_like_pinyin("Dinh Van Quan"))    # Vietnamese
        self.assertFalse(looks_like_pinyin("Bach Thanh Ha"))    # 1 pinyin token
        self.assertFalse(looks_like_pinyin("Anh Qiu"))          # 1 pinyin token
        self.assertFalse(looks_like_pinyin("白清夏"))

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.mgr = _manager(self.tmp)
        self.mgr.merge_from_analysis(PLAIN_ANALYSIS, chapter_number=1)
        self.mgr.add_glossary_entry("Bạch Thanh Hạ", cn="白清夏")
        self.mgr.add_to_whitelist("Hải An")

    def test_detect_flags_leaks_only(self):
        text = "Hắn nhìn Anh Qiu rồi gọi Bai Qing Xia, còn Bạch Thanh Hạ thì cười."
        self.assertEqual(
            self.mgr.detect_pinyin_names(text), ["Anh Qiu", "Bai Qing Xia"]
        )

    def test_sanctioned_and_whitelisted_names_are_never_flagged(self):
        text = "Bạch Thanh Hạ gặp Hải An ở Lục Viễn Thu."
        self.assertEqual(self.mgr.detect_pinyin_names(text), [])

    def test_single_word_and_short_runs_are_ignored(self):
        # 'Bai' alone is too weak a signal to reject a chapter over
        self.assertEqual(self.mgr.detect_pinyin_names("Bai cười."), [])

    def test_hanviet_readings_are_not_flagged_as_pinyin(self):
        # 'Gia Sa' = Hán-Việt of 王家沙 (a Shanghai shop) and 'Ca Sinh' of
        # 蔡生: only 'sa'/'ca' look pinyin, and both are ordinary Vietnamese
        # syllables too — flagging them fed the retry a correction that
        # rewrote the shop into a monk's robe (cà sa).
        text = "Cậu thấy tiệm lâu đời Vương Gia Sa ở Nam Kinh Tây."
        self.assertEqual(self.mgr.detect_pinyin_names(text), [])
        self.assertEqual(self.mgr.detect_pinyin_names("Nhân vật Ca Sinh đi."), [])

    def test_real_leaks_still_flagged_beside_hanviet_runs(self):
        # an exotic syllable (Qiu) or an all-pinyin run still raises it
        text = "Gặp Anh Qiu ở tiệm Vương Gia Sa, rồi gọi thêm Bai Qing Xia."
        self.assertEqual(
            self.mgr.detect_pinyin_names(text), ["Anh Qiu", "Bai Qing Xia"]
        )


class TestGlossaryText(unittest.TestCase):
    """NR-6 / NR-7"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.mgr = _manager(self.tmp)
        self.mgr.add_glossary_entry("Bạch Thanh Hạ", cn="白清夏")
        self.mgr.add_glossary_entry("Lục Đậu Tình", cn="陆窦晴")
        self.mgr.add_glossary_entry("Long Lân Đông", cn="龙怜冬",
                                    variant="Long Lệ Đông")

    def test_only_names_present_in_this_chapter(self):
        text = self.mgr.glossary_text("白清夏笑了笑。他想。")
        self.assertIn("白清夏 = Bạch Thanh Hạ", text)
        self.assertNotIn("陆窦晴", text)
        self.assertNotIn("龙怜冬", text)
        self.assertTrue(text.startswith("NAME GLOSSARY"))

    def test_absent_source_or_tiny_budget_returns_nothing(self):
        self.assertEqual(self.mgr.glossary_text(""), "")
        self.assertEqual(self.mgr.glossary_text("完全无关的内容"), "")
        self.assertEqual(self.mgr.glossary_text("白清夏", budget=20), "")

    def test_role_labels_are_not_names_and_stay_out_of_the_block(self):
        # legacy maps also carried translation hints (学生 = học sinh) — they
        # must not crowd real names out of the budget
        self.mgr.add_glossary_entry("học sinh", cn="学生")
        self.mgr.add_glossary_entry("tôi", cn="我")
        text = self.mgr.glossary_text("学生 我 白清夏")
        self.assertIn("白清夏 = Bạch Thanh Hạ", text)
        self.assertNotIn("học sinh", text)
        self.assertNotIn("= tôi", text)
        # a name behind a lowercase title still counts as a name
        self.mgr.add_glossary_entry("cô Trương", cn="张姨")
        self.assertIn("cô Trương", self.mgr.glossary_text("白清夏 张姨"))

    def test_mapping_never_rewrites_case_or_role_labels(self):
        self.mgr.add_glossary_entry("học sinh", cn="学生", variant="Học sinh")
        self.mgr.add_glossary_entry("cô Trương", cn="张姨", variant="Cô Trương")
        self.mgr.add_glossary_entry("Anh Thu", cn="秋哥", variant="anh Thu")
        self.mgr.add_glossary_entry("Trương Nhữ", variant="Trương Như")
        mapping = self.mgr.mapping()
        # sentence-position case swaps would corrupt ordinary prose
        self.assertNotIn("Học sinh", mapping)
        self.assertNotIn("Cô Trương", mapping)
        self.assertNotIn("anh Thu", mapping)
        # real spelling corrections still go through
        self.assertEqual(mapping.get("Long Lệ Đông"), "Long Lân Đông")
        self.assertEqual(mapping.get("Trương Như"), "Trương Nhữ")

    def test_mapping_never_rewrites_common_word_surfaces(self):
        # the glossary entry for the character 海 carried the pinyin surface
        # "Hai", which is also the ordinary word "hai" (hai người = two
        # people) — sentence-initial "Hai người" became "Hải người".  Short
        # or all-common-word surfaces must stay out of the rewrite map.
        self.mgr.add_glossary_entry("Hải", cn="海", pinyin="Hai")
        self.mgr.add_glossary_entry("Cố Quân", cn="顾君", variant="Cố")
        self.mgr.add_glossary_entry("Lạc Thanh Hàn", cn="洛清寒", variant="Lạc")
        self.mgr.add_glossary_entry("Thẩm U", cn="沈悠", variant="Hai Người")
        mapping = self.mgr.mapping()
        self.assertNotIn("Hai", mapping)
        self.assertNotIn("Cố", mapping)
        self.assertNotIn("Lạc", mapping)
        self.assertNotIn("Hai Người", mapping)
        # distinctive multi-word surfaces still go through
        self.assertEqual(mapping.get("Long Lệ Đông"), "Long Lân Đông")

    def test_mapping_never_rewrites_a_surface_inside_a_known_name(self):
        # a surface that sits inside a canonical rewrites the name itself
        # into a duplicated one: the glossary of 病娇 carried the pinyin
        # surface "Giang" for "Giang Du", so every "Giang Tích" became
        # "Giang Du Tích" and, on the next normalise pass, "Giang Du Du
        # Tích" — hundreds of times across the book.
        self.mgr.add_glossary_entry("Giang Du", cn="江俞", pinyin="Giang")
        self.mgr.add_glossary_entry("Giang Du", cn="江俞", pinyin="Jiang Yu")
        self.mgr.add_glossary_entry("Thẩm U", cn="沈悠", variant="Thẩm")
        self.mgr.add_glossary_entry(
            "Nguyễn Tiểu Khê", cn="阮小溪", variant="Nguyễn Tiểu"
        )
        mapping = self.mgr.mapping()
        # the name-internal surfaces are out of the rewrite map …
        self.assertNotIn("Giang", mapping)
        self.assertNotIn("Thẩm", mapping)
        self.assertNotIn("Nguyễn Tiểu", mapping)
        # … while the real pinyin of the same name still goes through
        self.assertEqual(mapping.get("Jiang Yu"), "Giang Du")

    def test_budget_caps_the_block(self):
        source = "白清夏 陆窦晴 龙怜冬"
        for budget in (200, 500, 2500):
            text = self.mgr.glossary_text(source, budget=budget)
            if text:
                self.assertLessEqual(len(text), budget)

    def test_mapping_is_vietnamese_only(self):
        mapping = self.mgr.mapping()
        self.assertEqual(mapping.get("Long Lệ Đông"), "Long Lân Đông")
        self.assertFalse(any(has_cjk(key) for key in mapping))
        self.assertFalse(any(has_cjk(value) for value in mapping.values()))
        self.assertNotIn("龙怜冬", mapping)

    def test_mapping_rewrites_mixed_garble_not_pure_chinese(self):
        self.mgr.add_glossary_entry("Thẩm Lê Sơ", cn="沈梨初", variant="Thẩm梨初")
        self.mgr.add_glossary_entry("Bùi Duật", cn="裴聿", variant="裴 Duật")
        mapping = self.mgr.mapping()
        # mixed-script garble the engine left in translated text
        self.assertEqual(mapping.get("Thẩm梨初"), "Thẩm Lê Sơ")
        self.assertEqual(mapping.get("裴 Duật"), "Bùi Duật")
        # pure Chinese stays excluded: kept source titles are never touched
        self.assertNotIn("沈梨初", mapping)
        self.assertFalse(any(has_cjk(value) for value in mapping.values()))


class TestPersistence(unittest.TestCase):
    """NR-8"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = os.path.join(self.tmp, "rules.json")

    def _read(self):
        with open(self.path, encoding="utf-8") as fh:
            return json.load(fh)

    def _write(self, data, bump=3.0):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        stamp = time.time() + bump
        os.utime(self.path, (stamp, stamp))

    def test_glossary_and_pending_round_trip(self):
        mgr = _manager(self.tmp)
        mgr.merge_from_analysis(PAIR_ANALYSIS, chapter_number=5, content_hash="h1")
        mgr.merge_from_analysis("1) Characters: Anh Qiu", chapter_number=6)
        mgr.add_to_whitelist("Hải An")
        mgr.save()

        reloaded = _manager(self.tmp)
        reloaded.load()
        self.assertEqual(
            {e["canonical"] for e in reloaded.glossary},
            {"Bạch Thanh Hạ", "Long Lân Đông"},
        )
        self.assertEqual(reloaded.pending.get("Anh Qiu"), "pinyin")
        self.assertIn("Hải An", reloaded.whitelist)

    def test_plain_save_keeps_what_the_job_learned(self):
        mgr = _manager(self.tmp)
        mgr.add_glossary_entry("Bạch Thanh Hạ", cn="白清夏")
        mgr.save()
        mgr.add_glossary_entry("Long Lân Đông", cn="龙怜冬")
        mgr.save()
        data = self._read()
        self.assertEqual(
            {e["canonical"] for e in data["name_glossary"]},
            {"Bạch Thanh Hạ", "Long Lân Đông"},
        )

    def test_hand_edits_on_disk_win_the_next_save(self):
        mgr = _manager(self.tmp)
        mgr.add_glossary_entry("Bạch Thanh Hạ", cn="白清夏")
        mgr.merge_from_analysis(PLAIN_ANALYSIS, chapter_number=5, content_hash="h5")
        mgr.save()

        # user edits the file in their editor while the job runs
        data = self._read()
        data["name_whitelist"] = ["KEEP ME"]
        data["name_glossary"].append(
            {"canonical": "Lục Viễn Thu", "variants": [], "cn": ["陆远秋"],
             "pinyin": []}
        )
        self._write(data)

        # ...job-side save (a new chapter merged) must not revert that edit
        mgr.merge_from_analysis(
            "1) Characters: Long Lân Đông", chapter_number=6, content_hash="h6"
        )
        mgr.add_glossary_entry("Long Lân Đông", cn="龙怜冬")
        mgr.save()

        on_disk = self._read()
        self.assertEqual(on_disk["name_whitelist"], ["KEEP ME"])
        self.assertIn(
            "Lục Viễn Thu", [e["canonical"] for e in on_disk["name_glossary"]]
        )
        # machine-owned data is written too
        self.assertIn("6", on_disk["_chapter_analyses"])
        self.assertNotIn(
            "Long Lân Đông", [e["canonical"] for e in on_disk["name_glossary"]]
        )


class TestSanitize(unittest.TestCase):
    """NR-9"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.mgr = _manager(self.tmp)
        self.mgr.add_glossary_entry("Bạch Thanh Hạ", cn="白清夏")
        self.mgr.state.characters["陆远秋"] = CharacterEntry(
            calls={"Bạch Thanh Hạ": "tôi - anh"}, last_seen=1
        )
        self.mgr.state.characters["Bach Thanh Hạ"] = CharacterEntry(last_seen=2)
        self.mgr.state.characters["Bạch Thanh Hạ"] = CharacterEntry(
            calls={"Chu Kiệt Luân": "tao - mày", "脏辫女生": "tao - nó"},
            last_seen=9,
        )
        self.mgr.state.characters["Long Lân Đông"] = CharacterEntry(last_seen=5)
        self.mgr.state.narration_defaults = {"陆远秋": "hắn"}

    def test_cleanup_pass(self):
        stats = self.mgr.sanitize()

        # Chinese keys are parked, not taught
        self.assertNotIn("陆远秋", self.mgr.state.characters)
        self.assertEqual(self.mgr.pending.get("陆远秋"), "unmapped_chinese")

        # diacritic duplicate folds onto its canonical, recorded as a variant
        self.assertNotIn("Bach Thanh Hạ", self.mgr.state.characters)
        glossary = {e["canonical"]: e for e in self.mgr.glossary}
        self.assertIn("Bach Thanh Hạ", glossary["Bạch Thanh Hạ"]["variants"])
        self.assertEqual(
            self.mgr.mapping().get("Bach Thanh Hạ"), "Bạch Thanh Hạ"
        )

        # a resolvable dangling addressee becomes a bare key (rules survive
        # chapter scoping) ...
        entry = self.mgr.state.characters["Bạch Thanh Hạ"]
        self.assertIn("Chu Kiệt Luân", entry.calls)
        self.assertIn("Chu Kiệt Luân", self.mgr.state.characters)
        # ... an unmappable one is dropped from the rules and parked
        self.assertNotIn("脏辫女生", entry.calls)
        self.assertEqual(self.mgr.pending.get("脏辫女生"), "unmapped_chinese")

        # Chinese narration key is gone
        self.assertNotIn("陆远秋", self.mgr.state.narration_defaults)
        self.assertEqual(stats["pending"], 1)
        self.assertEqual(stats["targets_dropped"], 1)

        # running it again is a no-op
        self.assertEqual(self.mgr.sanitize()["renamed"], 0)


class TestRepairParsing(unittest.TestCase):
    """NR-10"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.mgr = _manager(self.tmp)

    def test_parse_mapping_and_keep_lines(self):
        mappings, keep = parse_repair_output(
            "Bai Qing Xia -> 白清夏 = Bạch Thanh Hạ\n"
            "- Anh Qiu -> 安秋 = Anh Thu\n"
            "KEEP: Hải An\n"
            "markdown ignored"
        )
        self.assertEqual(mappings[0], ("Bai Qing Xia", "白清夏", "Bạch Thanh Hạ"))
        self.assertEqual(mappings[1], ("Anh Qiu", "安秋", "Anh Thu"))
        self.assertEqual(keep, ["Hải An"])

    def test_swapped_sides_are_corrected(self):
        # the model put the Chinese on the right of the '='
        mappings, _ = parse_repair_output("Bai Qing Xia -> Bạch Thanh Hạ = 白清夏")
        self.assertEqual(mappings, [("Bai Qing Xia", "白清夏", "Bạch Thanh Hạ")])

    def test_apply_repair_seeds_whitelist_and_returns_fixes(self):
        self.mgr.add_pending("Bai Qing Xia")
        self.mgr.add_pending("Hải An")
        fixes = self.mgr.apply_repair(
            "Bai Qing Xia -> 白清夏 = Bạch Thanh Hạ\nKEEP: Hải An"
        )
        self.assertEqual(fixes, {"Bai Qing Xia": "Bạch Thanh Hạ"})
        glossary = {e["canonical"]: e for e in self.mgr.glossary}
        self.assertIn("白清夏", glossary["Bạch Thanh Hạ"]["cn"])
        self.assertIn("Bai Qing Xia", glossary["Bạch Thanh Hạ"]["pinyin"])
        self.assertIn("Hải An", self.mgr.whitelist)
        self.assertNotIn("Bai Qing Xia", self.mgr.pending)
        self.assertNotIn("Hải An", self.mgr.pending)
        # the correction is now available to the retry prompt
        self.assertEqual(
            self.mgr.pinyin_fixes(["Bai Qing Xia", "nope"]),
            {"Bai Qing Xia": "Bạch Thanh Hạ"},
        )

    def test_apply_repair_learns_mixed_script_garble(self):
        # The chapter-146 garble: "江 biệt" (CJK + romanized reading). The fix
        # must reach the retry prompt (pinyin_fixes) and the EPUB normaliser
        # (mapping) alike, and the name must leave the pending queue.
        self.mgr.add_pending("江 biệt")
        fixes = self.mgr.apply_repair("江 biệt -> 江别 = Giang Biệt")
        self.assertEqual(fixes, {"江 biệt": "Giang Biệt"})
        self.assertEqual(
            self.mgr.pinyin_fixes(["江 biệt"]), {"江 biệt": "Giang Biệt"}
        )
        self.assertEqual(self.mgr.mapping().get("江 biệt"), "Giang Biệt")
        self.assertNotIn("江 biệt", self.mgr.pending)


class NameAwareEngine(AnalyzingEngine):
    def __init__(self):
        super().__init__()
        self.repair_calls = []

    def repair_names(self, pending_names, excerpt):
        self.repair_calls.append(list(pending_names))
        return ""


class TestTranslatorNameFlow(unittest.TestCase):
    """NR-11 / NR-12 / NR-13"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = os.path.join(self.tmp, "rules.json")
        seed = _manager(self.tmp)
        seed.add_glossary_entry("Bạch Thanh Hạ", cn="白清夏")
        seed.merge_from_analysis(PLAIN_ANALYSIS, chapter_number=1, content_hash="h1")
        seed.save()
        self.content = "<p>白清夏看着 Long Lệ Đông。</p>" * 40

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

    def test_retry_prompt_carries_the_corrections(self):
        prompt = Translator._build_retry_input(
            "<p>x</p>",
            reason="pinyin",
            to_lang="Vietnamese",
            corrections={"Bai Qing Xia": "Bạch Thanh Hạ"},
        )
        self.assertIn("pinyin", prompt)
        self.assertIn("Bai Qing Xia -> Bạch Thanh Hạ", prompt)
        self.assertIn("<p>x</p>", prompt)

    def test_rules_open_with_the_glossary_for_this_chapter(self):
        rules = self._translator(AnalyzingEngine())._render_chapter_rules(
            7, "<p>白清夏笑了。</p>"
        )
        self.assertTrue(rules.startswith("NAME GLOSSARY"), rules[:120])
        self.assertIn("白清夏 = Bạch Thanh Hạ", rules)
        self.assertIn("Default fallback: tôi - người khác", rules)

    def test_glossary_is_omitted_when_the_chapter_never_mentions_it(self):
        rules = self._translator(AnalyzingEngine())._render_chapter_rules(
            7, "<p>完全无关的章节</p>"
        )
        self.assertNotIn("NAME GLOSSARY", rules)
        self.assertNotIn("白清夏", rules)

    def test_glossary_stays_inside_the_rules_budget(self):
        mgr = _manager(self.tmp)
        mgr.load()                              # keep the seeded glossary
        for i in range(60):
            mgr.state.characters[f"Nhân vật {i:03d}"] = CharacterEntry(
                names=[f"Nhân vật {i:03d}"],
                calls={"Đối phương": "tao - mày " + "x" * 40},
                last_seen=i,
            )
        mgr.save()
        translator = self._translator(
            AnalyzingEngine(), rules_prompt_max_chars=600
        )
        rules = translator._render_chapter_rules(7, "<p>白清夏笑了。</p>")
        tail = len(translator.global_rules.to_prompt_text(budget=1))
        self.assertTrue(rules.startswith("NAME GLOSSARY"))
        self.assertLessEqual(len(rules), 600 + tail)

    def test_pinyin_hit_is_parked_only_for_chinese_sources(self):
        translator = self._translator(NameAwareEngine())
        hits = translator._detect_pinyin_names(
            "<p>白清夏看着 Anh Qiu。</p>",
            "<p>Hắn nhìn Anh Qiu rồi quay đi.</p>",
        )
        self.assertEqual(hits, ["Anh Qiu"])
        self.assertEqual(translator.global_rules.pending.get("Anh Qiu"), "pinyin")
        # not a Chinese source → pinyin cannot be a leak
        self.assertEqual(
            translator._detect_pinyin_names(
                "<p>Anh Qiu smiled.</p>", "<p>Hắn cười.</p>"
            ),
            [],
        )

    def test_mixed_script_garble_is_detected_and_parked(self):
        # "江 biệt" / "Thẩm梨初": the engine half-translated a name, leaving
        # CJK glued to (or ahead of) the romanized reading. It used to ship
        # silently; the detector must flag it so the repair pass learns the
        # canonical form and later chapters get it from the glossary.
        translator = self._translator(NameAwareEngine())
        hits = translator._detect_pinyin_names(
            "<p>江别看着沈青提。</p>",
            "<p>trái tim 江 biệt đập nhanh, Thẩm梨初 mỉm cười.</p>",
        )
        self.assertEqual(hits, ["江 biệt", "Thẩm梨初"])
        self.assertEqual(translator.global_rules.pending.get("江 biệt"), "pinyin")
        # kept 第N章 numbering next to translated text is not garble
        self.assertEqual(
            translator._detect_pinyin_names(
                "<p>江别看着沈青提。</p>", "<p>第146章 Giang Biệt bước vào.</p>"
            ),
            [],
        )

    def test_pinyin_detection_offers_names_to_the_repair_pass_once(self):
        engine = NameAwareEngine()
        translator = self._translator(engine)
        translator._detect_pinyin_names(
            "<p>白清夏看着 Anh Qiu。</p>",
            "<p>Hắn nhìn Anh Qiu rồi quay đi.</p>",
        )
        translator._detect_pinyin_names(
            "<p>白清夏 lại thấy Anh Qiu。</p>",
            "<p>Cô lại thấy Anh Qiu.</p>",
        )
        self.assertEqual(engine.repair_calls, [["Anh Qiu"]])

    def test_analysis_merges_pairs_and_renders_scoped(self):
        translator = self._translator(AnalyzingEngine())
        rules = translator._analyze_chapter_consistency(self.content, chapter_number=7)
        self.assertIn("Bạch Thanh Hạ", rules)
        self.assertLessEqual(len(rules), 4000)
        # the analysis seeded the pinyin-free spellings dictionary
        self.assertIn(
            "Bạch Thanh Hạ", translator.global_rules.state.characters
        )


class PinyinEngine(TranslationEngine):
    """Writes a pinyin name for the first ``bad_times`` calls, then a clean
    Vietnamese sentence with the tags intact."""

    def __init__(self, bad_times=1, good_name="Bạch Thanh Hạ"):
        super().__init__(from_lang="ZH", to_lang="VI")
        self.calls = 0
        self.bad_times = bad_times
        self.good_name = good_name
        self.last_inputs = []

    def translate(self, text):
        self.calls += 1
        self.last_inputs.append(text)
        chunk = text.split("Input:\n", 1)[1] if "Input:\n" in text else text
        name = "Bai Qing Xia" if self.calls <= self.bad_times else self.good_name
        return re.sub(
            r"(?<=>)[^<>]+(?=<)", f"Hắn nhìn {name} rồi quay đi.", chunk
        )


class TestPinyinRetryLoop(unittest.TestCase):
    """NR-11 + the never-hang safety valve."""

    ZH_SOUP = (
        "<html><body><p>白清夏和陆远秋破天荒地提早回了家看着满桌子的菜"
        "什么山珍海味鲍鱼龙虾应有尽有顾言嘴角一抽地向自己爸妈问道</p>"
        "</body></html>"
    )

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = os.path.join(self.tmp, "rules.json")

    def _translator(self, engine):
        return Translator(
            engine, max_tries=5, consistency_config={}, global_rules_path=self.path
        )

    def test_known_pinyin_is_rejected_then_corrected(self):
        seed = _manager(self.tmp)
        seed.add_glossary_entry(
            "Bạch Thanh Hạ", cn="白清夏", pinyin="Bai Qing Xia"
        )
        seed.save()

        engine = PinyinEngine(bad_times=1)
        translator = self._translator(engine)
        with mock.patch("time.sleep"):
            out = translator._translate_chunk(self.ZH_SOUP, chapter_number=1)

        self.assertEqual(engine.calls, 2)
        self.assertIn("Bạch Thanh Hạ", out)
        self.assertNotIn("Bai Qing Xia", out)
        retry = engine.last_inputs[1]
        self.assertIn("NAME CORRECTIONS", retry)
        self.assertIn("Bai Qing Xia -> Bạch Thanh Hạ", retry)
        self.assertIn("pinyin", retry)

    def test_uncorrectable_pinyin_whitelists_instead_of_hanging(self):
        engine = PinyinEngine(bad_times=10 ** 9)      # never corrects itself
        translator = self._translator(engine)
        with mock.patch("time.sleep"):
            out = translator._translate_chunk(self.ZH_SOUP, chapter_number=1)

        # 3 rejections, then the name is accepted as-is — no infinite loop
        self.assertEqual(engine.calls, PINYIN_UNKNOWN_AUTO_WHITELIST_AFTER + 1)
        self.assertIn("Bai Qing Xia", out)
        self.assertIn("Bai Qing Xia", translator.global_rules.whitelist)
        self.assertNotIn("Bai Qing Xia", translator.global_rules.pending)

    def test_refused_correction_drops_the_mapping_instead_of_hanging(self):
        # The real "Dao Dao" loop: the glossary CAN correct the flagged form
        # (it maps it onto another character who reads alike), the model
        # keeps writing its own form and rejects the correction — 42 attempts
        # in a row before the job was stopped. Three refusals of the SAME
        # known correction must drop the mapping and sanction the form.
        seed = _manager(self.tmp)
        seed.add_glossary_entry(
            "Trì Bí Bí", cn="迟笨笨", pinyin="Bai Qing Xia"
        )
        seed.save()

        engine = PinyinEngine(bad_times=10 ** 9)      # never changes its mind
        translator = self._translator(engine)
        with mock.patch("time.sleep"):
            out = translator._translate_chunk(self.ZH_SOUP, chapter_number=1)

        self.assertEqual(engine.calls, PINYIN_MAPPING_REFUSAL_AFTER + 1)
        self.assertIn("Bai Qing Xia", out)          # the model's form lands
        self.assertIn("Bai Qing Xia", translator.global_rules.whitelist)
        # the wrong mapping is gone and no repair pass will resurrect it
        self.assertNotIn(
            "Bai Qing Xia",
            translator.global_rules.pinyin_fixes(["Bai Qing Xia"]),
        )
        # the final prompt was the plain retry — nothing left to argue with
        self.assertNotIn("NAME CORRECTIONS", engine.last_inputs[-1])
        self.assertNotIn("pinyin", engine.last_inputs[-1].split("Input:\n", 1)[0])

    def test_pinyin_check_skips_non_chinese_sources(self):
        engine = PinyinEngine(bad_times=10 ** 9)
        translator = self._translator(engine)
        english = "<html><body><p>Anh Qiu smiled at him and then left.</p></body></html>"
        out = translator._translate_chunk(english, chapter_number=2)
        self.assertEqual(engine.calls, 1)             # never rejected
        self.assertIn("Bai Qing Xia", out)


class TestEpubNameNormalization(unittest.TestCase):
    """NR-14"""

    def test_longest_surface_wins_and_counts(self):
        mapping = {
            "Long Lệ Đông": "Long Lân Đông",
            "Lệ Đông": "Lân Đông",
            "Bach Thanh Ha": "Bạch Thanh Hạ",
        }
        html = "<p>Long Lệ Đông cười, Lệ Đông chạy, Bach Thanh Ha đứng.</p>"
        text, count = normalize_names_in_html(html, mapping)
        self.assertEqual(
            text,
            "<p>Long Lân Đông cười, Lân Đông chạy, Bạch Thanh Hạ đứng.</p>",
        )
        self.assertEqual(count, 3)

    def test_latin_surface_never_lands_inside_a_word(self):
        mapping = {"An": "Án", "Du": "Đỗ"}
        html = "<p>Anh đợi ở An, Giang Du ngồi, GiangDu đứng.</p>"
        text, count = normalize_names_in_html(html, mapping)
        self.assertEqual(
            text,
            "<p>Anh đợi ở Án, Giang Đỗ ngồi, GiangDu đứng.</p>",
        )
        self.assertEqual(count, 2)

    def test_chinese_surfaces_and_headings_are_never_touched(self):
        mapping = {"白清夏": "Bạch Thanh Hạ", "Long Lệ Đông": "Long Lân Đông"}
        html = "<h3>第130章 白清夏</h3><p>Long Lệ Đông đi.</p>"
        text, count = normalize_names_in_html(html, mapping)
        self.assertIn("第130章 白清夏", text)
        self.assertIn("Long Lân Đông", text)
        self.assertEqual(count, 1)

    def test_mixed_script_surface_is_rewritten_but_pure_chinese_is_not(self):
        mapping = {"Thẩm梨初": "Thẩm Lê Sơ", "沈梨初": "Thẩm Lê Sơ"}
        html = "<title>第3章 沈梨初</title><p>Thẩm梨初 cười.</p>"
        text, count = normalize_names_in_html(html, mapping)
        self.assertIn("第3章 沈梨初", text)      # source title untouched
        self.assertIn("Thẩm Lê Sơ cười", text)   # engine garble fixed
        self.assertEqual(count, 1)

    def test_book_pass_skips_binary_items(self):
        class Item:
            def __init__(self, media_type, content):
                self.media_type = media_type
                self.content = content

        class Book:
            def __init__(self, items):
                self._items = items

            def get_items(self):
                return self._items

        image_bytes = b"\x89PNG\x00\x00" + "Long Lệ Đông".encode("utf-8")
        image = Item("image/png", image_bytes)
        chapter = Item(
            "application/xhtml+xml",
            "<p>Long Lệ Đông vui.</p>".encode("utf-8"),
        )
        book = Book([image, chapter])
        total = normalize_names_in_book(
            book, {"Long Lệ Đông": "Long Lân Đông"}
        )
        self.assertEqual(total, 1)
        self.assertEqual(image.content, image_bytes)     # untouched
        self.assertEqual(
            chapter.content.decode("utf-8"),
            "<p>Long Lân Đông vui.</p>",
        )
        # an empty mapping is a no-op
        self.assertEqual(normalize_names_in_book(book, {}), 0)


if __name__ == "__main__":
    unittest.main()
