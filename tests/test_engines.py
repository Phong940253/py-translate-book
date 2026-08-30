"""Offline tests for the multi-provider engine factory (translator.job).

Covers the OpenAI-compatible dynamic providers, model suggestions and the
``model`` override plumbing. No real AI calls are made.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translator import job as job_mod
from translator.job import (
    build_engine,
    list_supported_engines,
    list_engine_models,
)

COMPATIBLE = {
    "openai": {"api_key": "sk-test"},
    "gemini": {"api_key": "ai-test"},
    "webai": {"base_url": "http://localhost:6969"},
    "groq": {
        "type": "openai_compatible",
        "base_url": "https://api.groq.com/openai/v1",
        "model": "openai/gpt-oss-120b",
        "api_key": "gsk-test",
    },
    "deepseek": {
        "type": "openai_compatible",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-v4-flash",
        "models": ["deepseek-v4-pro", "deepseek-v4-pro", "deepseek-v4-flash"],
        "api_key": "",
    },
    "ollama": {
        "type": "openai_compatible",
        "base_url": "http://localhost:11434/v1",
        "model": "qwen3:14b",
        "api_key": "",
    },
}


class TestListSupportedEngines(unittest.TestCase):
    def test_builtins_always_present_in_order(self):
        self.assertEqual(list_supported_engines({}), ["openai", "gemini", "webai"])

    def test_compatible_providers_appended(self):
        engines = list_supported_engines(COMPATIBLE)
        self.assertEqual(
            engines,
            ["openai", "gemini", "webai", "groq", "deepseek", "ollama"],
        )

    def test_non_compatible_sections_ignored(self):
        cfg = {"openai": {}, "discord": {"webhook_url": "x"}, "groq": {"api_key": "y"}}
        self.assertEqual(list_supported_engines(cfg), ["openai", "gemini", "webai"])


class TestListEngineModels(unittest.TestCase):
    def test_uses_models_list_deduped(self):
        got = list_engine_models(COMPATIBLE, "deepseek")
        self.assertEqual(got, ["deepseek-v4-pro", "deepseek-v4-flash"])

    def test_falls_back_to_single_model(self):
        got = list_engine_models(COMPATIBLE, "groq")
        self.assertEqual(got, ["openai/gpt-oss-120b"])

    def test_openai_hardcoded_default(self):
        self.assertEqual(list_engine_models({}, "openai"), ["gpt-5-mini-2025-08-07"])

    def test_gemini_hardcoded_default(self):
        self.assertEqual(list_engine_models({}, "gemini"), ["gemini-flash-lite"])

    def test_unknown_engine_empty(self):
        self.assertEqual(list_engine_models({}, "nowhere"), [])


class TestBuildEngineCompatible(unittest.TestCase):
    def test_builds_compatible_engine(self):
        engine = build_engine("groq", COMPATIBLE)
        from translator.engines.compatible import OpenAICompatibleEngine

        self.assertIsInstance(engine, OpenAICompatibleEngine)
        self.assertEqual(engine.base_url, "https://api.groq.com/openai/v1")
        self.assertEqual(engine.model, "openai/gpt-oss-120b")
        self.assertEqual(engine.api_key, "gsk-test")

    def test_model_override_wins(self):
        engine = build_engine("groq", COMPATIBLE, model="openai/gpt-oss-20b")
        self.assertEqual(engine.model, "openai/gpt-oss-20b")

    def test_local_compatible_allows_empty_key(self):
        engine = build_engine("ollama", COMPATIBLE)
        self.assertEqual(engine.model, "qwen3:14b")
        self.assertEqual(engine.api_key, "")

    def test_missing_base_url_raises_friendly(self):
        cfg = {"foo": {"type": "openai_compatible", "api_key": "k"}}
        with self.assertRaises(ValueError) as ctx:
            build_engine("foo", cfg)
        self.assertIn("base_url", str(ctx.exception))

    def test_openai_compatible_requires_type(self):
        cfg = {"foo": {"api_key": "k"}}
        with self.assertRaises(ValueError):
            build_engine("foo", cfg)

    def test_unknown_engine_raises(self):
        with self.assertRaises(ValueError) as ctx:
            build_engine("nope", COMPATIBLE)
        self.assertIn("nope", str(ctx.exception))


class TestBuildEngineBuiltins(unittest.TestCase):
    def test_openai_without_key_raises_friendly(self):
        with self.assertRaises(ValueError) as ctx:
            build_engine("openai", {"openai": {}})
        self.assertIn("api_key", str(ctx.exception))

    def test_openai_model_override(self):
        engine = build_engine("openai", COMPATIBLE, model="gpt-5.5")
        self.assertEqual(engine.model, "gpt-5.5")

    def test_openai_model_from_config(self):
        engine = build_engine("openai", COMPATIBLE)
        # config has no model for openai -> built-in DEFAULT_MODEL
        from translator.engines.openai_engine import DEFAULT_MODEL

        self.assertEqual(engine.model, DEFAULT_MODEL)

    def test_gemini_model_override_and_analysis(self):
        cfg = {
            "gemini": {
                "api_key": "ai-test",
                "model": "gemini-3.5-flash-lite",
                "analysis_model": "gemini-3.5-flash",
            }
        }
        engine = build_engine("gemini", cfg)
        self.assertEqual(engine.model_name, "gemini-3.5-flash-lite")
        self.assertEqual(engine.analysis_model_name, "gemini-3.5-flash")
        override = build_engine("gemini", cfg, model="gemini-3.7-flash")
        self.assertEqual(override.model_name, "gemini-3.7-flash")


class TestRunTranslationModelPlumbing(unittest.TestCase):
    """``model`` must flow from run_translation into the engine factory."""

    def _make_epub(self, path):
        from ebooklib import epub

        book = epub.EpubBook()
        book.set_identifier("model-plumb")
        book.set_title("Model Plumb")
        book.set_language("en")
        ch = epub.EpubHtml(title="Ch1", file_name="ch1.xhtml", lang="en")
        ch.content = b"<html><body><p>Hello world sample text here</p></body></html>"
        book.add_item(ch)
        book.spine = ["nav", "ch1.xhtml"]
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        import tempfile

        epub.write_epub(path, book, {})

    def test_model_passed_to_build_engine(self):
        import shutil
        import tempfile
        from translator.engines.base import TranslationEngine

        class StubEngine(TranslationEngine):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)

            def supports_batch(self):
                return False

            def translate(self, text):
                return text.replace("Hello", "Xin chào")

        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        src = os.path.join(tmp, "in.epub")
        out = os.path.join(tmp, "out.epub")
        self._make_epub(src)

        # Verify: engine factory receives model AND it lands in the
        # checkpoint signature for safe resume (model change invalidates it).
        captured = {}

        def fake_build(*a, **k):
            captured["model"] = k.get("model")
            return StubEngine(from_lang="EN", to_lang="VI")

        orig = job_mod.build_engine
        job_mod.build_engine = fake_build
        self.addCleanup(setattr, job_mod, "build_engine", orig)

        stats = job_mod.run_translation(
            {"translation": {"fallback_max_chunk_size": 6000}},
            input=src,
            output=out,
            engine="openai",
            model="gpt-5.5",
            from_lang="EN",
            to_lang="VI",
        )
        self.assertEqual(captured["model"], "gpt-5.5")
        self.assertGreater(stats.get("chapters_processed", 0), 0)

        cp = job_mod._load_checkpoint(out + ".checkpoint.json")
        self.assertEqual(cp.get("model"), "gpt-5.5")
        self.assertIn("model", cp)


if __name__ == "__main__":
    unittest.main()