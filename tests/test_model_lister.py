"""Offline tests for translator/model_lister.py (no network is hit — the
``_http_get_json`` helper is stubbed in every test)."""

import os
import sys
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import translator.model_lister as ml
from translator.job import list_engine_models


def _config(extra=None):
    cfg = {
        "openai": {"api_key": "sk-test"},
        "gemini": {"api_key": "ai-test"},
        "webai": {"base_url": "http://localhost:6969", "api_key": ""},
        "groq": {
            "type": "openai_compatible",
            "base_url": "https://api.groq.com/openai/v1",
            "api_key": "gsk-test",
        },
    }
    if extra:
        cfg.update(extra)
    return cfg


class IdsParsingTests(unittest.TestCase):
    def test_ids_are_deduped_and_sorted(self):
        payload = {"data": [{"id": "b"}, {"id": "a"}, {"id": "b"}]}
        self.assertEqual(ml._ids_from_openai_payload(payload), ["a", "b"])

    def test_ids_ignore_missing_entries(self):
        payload = {"data": [{}, {"id": 5}, {"id": "x"}]}
        self.assertEqual(ml._ids_from_openai_payload(payload), ["5", "x"])


class OpenAITests(unittest.TestCase):
    def test_live_parse_and_auth_header(self):
        calls = {}

        def fake_get(url, api_key=None, timeout=ml.DEFAULT_TIMEOUT_SECONDS):
            calls["url"] = url
            calls["key"] = api_key
            return {"data": [{"id": "gpt-5.5"}, {"id": "gpt-5.4-mini"}]}

        with mock.patch.object(ml, "_http_get_json", side_effect=fake_get):
            result = ml.fetch_models(_config(), "openai")
        self.assertEqual(result["source"], "live")
        self.assertEqual(result["models"], ["gpt-5.4-mini", "gpt-5.5"])
        self.assertEqual(calls["key"], "sk-test")
        self.assertIn("api.openai.com", calls["url"])
        self.assertTrue(calls["url"].endswith("/models"))

    def test_missing_key_falls_back_without_network(self):
        with mock.patch.object(ml, "_http_get_json") as fake:
            result = ml.fetch_models(_config({"openai": {}}), "openai")
        fake.assert_not_called()
        self.assertEqual(result["source"], "config")
        self.assertEqual(result["models"], list_engine_models({"openai": {}}, "openai"))
        self.assertIsNotNone(result["error"])


class GeminiTests(unittest.TestCase):
    def test_only_generatecontent_models_keep_name_stripped(self):
        payload = {
            "models": [
                {
                    "name": "models/gemini-3.5-flash",
                    "supportedGenerationMethods": ["generateContent", "embedContent"],
                },
                {
                    "name": "models/text-embedding-004",
                    "supportedGenerationMethods": ["embedContent"],
                },
                {
                    "name": "models/gemini-3.1-pro-preview",
                    "supportedGenerationMethods": ["generateContent"],
                },
            ]
        }

        def fake_get(url, api_key=None, timeout=ml.DEFAULT_TIMEOUT_SECONDS):
            self.assertIn("key=", url)  # key travels in the query string
            return payload

        with mock.patch.object(ml, "_http_get_json", side_effect=fake_get):
            result = ml.fetch_models(_config(), "gemini")
        self.assertEqual(result["source"], "live")
        self.assertEqual(result["models"], ["gemini-3.1-pro-preview", "gemini-3.5-flash"])

    def test_missing_key_falls_back(self):
        with mock.patch.object(ml, "_http_get_json") as fake:
            result = ml.fetch_models(_config({"gemini": {}}), "gemini")
        fake.assert_not_called()
        self.assertEqual(result["source"], "config")


class CompatibleProviderTests(unittest.TestCase):
    def test_groq_live(self):
        calls = {}

        def fake_get(url, api_key=None, timeout=ml.DEFAULT_TIMEOUT_SECONDS):
            calls["url"] = url
            calls["key"] = api_key
            return {"data": [{"id": "openai/gpt-oss-120b"}, {"id": "qwen/qwen3.6-27b"}]}

        with mock.patch.object(ml, "_http_get_json", side_effect=fake_get):
            result = ml.fetch_models(_config(), "groq")
        self.assertEqual(result["source"], "live")
        self.assertEqual(result["models"], ["openai/gpt-oss-120b", "qwen/qwen3.6-27b"])
        self.assertEqual(calls["key"], "gsk-test")
        self.assertTrue(calls["url"].endswith("/v1/models"))

    def test_missing_base_url_error(self):
        with mock.patch.object(ml, "_http_get_json") as fake:
            result = ml.fetch_models(_config({"foo": {"type": "openai_compatible"}}), "foo")
        fake.assert_not_called()
        self.assertEqual(result["source"], "config")
        self.assertIn("base_url", result["error"] or "")

    def test_network_error_falls_back(self):
        def fake_get(*args, **kwargs):
            raise urllib.error.URLError("boom")

        with mock.patch.object(ml, "_http_get_json", side_effect=fake_get):
            result = ml.fetch_models(_config(), "groq")
        self.assertEqual(result["source"], "config")
        self.assertEqual(result["models"], list_engine_models(_config(), "groq"))
        self.assertIn("boom", result["error"] or "")

    def test_empty_live_payload_falls_back(self):
        def fake_get(url, api_key=None, timeout=ml.DEFAULT_TIMEOUT_SECONDS):
            return {"data": []}

        with mock.patch.object(ml, "_http_get_json", side_effect=fake_get):
            result = ml.fetch_models(_config(), "groq")
        self.assertEqual(result["source"], "config")


class TriesExtraCandidateTests(unittest.TestCase):
    def test_base_without_v1_tries_second_candidate(self):
        urls = []

        def fake_get(url, api_key=None, timeout=ml.DEFAULT_TIMEOUT_SECONDS):
            urls.append(url)
            if url == "http://localhost:6969/models":
                raise urllib.error.URLError("first candidate down")
            return {"data": [{"id": "gemini-flash"}]}

        with mock.patch.object(ml, "_http_get_json", side_effect=fake_get):
            result = ml.fetch_models(_config(), "webai")
        self.assertEqual(result["source"], "live")
        self.assertEqual(result["models"], ["gemini-flash"])
        self.assertEqual(len(urls), 2)
        self.assertTrue(urls[0].endswith("/models"))
        self.assertTrue(urls[1].endswith("/v1/models"))


class OllamaTagsFallbackTests(unittest.TestCase):
    def test_404_on_v1_models_falls_back_to_api_tags(self):
        def fake_get(url, api_key=None, timeout=ml.DEFAULT_TIMEOUT_SECONDS):
            if url.endswith("/v1/models"):
                raise urllib.error.HTTPError(url, 404, "Not Found", None, None)
            if url.endswith("/api/tags"):
                return {
                    "models": [
                        {"name": "qwen3:8b"},
                        {"name": "qwen3:14b"},
                        {"name": "qwen3:8b"},  # duplicate is filtered
                    ]
                }
            raise AssertionError(f"unexpected url: {url}")

        config = _config(
            {"ollama": {"type": "openai_compatible", "base_url": "http://localhost:11434/v1"}}
        )
        with mock.patch.object(ml, "_http_get_json", side_effect=fake_get):
            result = ml.fetch_models(config, "ollama")
        self.assertEqual(result["source"], "live")
        self.assertEqual(result["models"], ["qwen3:14b", "qwen3:8b"])


class DispatchTests(unittest.TestCase):
    def test_unknown_engine_raises(self):
        with self.assertRaises(ValueError):
            ml.fetch_models(_config(), "nope")


if __name__ == "__main__":
    unittest.main()