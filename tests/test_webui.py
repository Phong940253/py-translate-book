"""Offline smoke tests for the Flask web UI.

We stub ``build_engine`` (so no real AI call) and ``DiscordNotifier`` (no
webhook spam), then exercise the full web flow: create a job via the form,
let the background runner finish, and assert the output EPUB is produced.
"""

import os
import base64
import json
import re
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ebooklib import epub

from translator.engines.base import TranslationEngine
from webui import app as webui_app

PROJECT_ROOT = webui_app.PROJECT_ROOT


class StubEngine(TranslationEngine):
    def __init__(self):
        super().__init__(from_lang="EN", to_lang="VI")

    def supports_batch(self):
        return False

    def translate(self, text):
        return text.replace("Hello", "Xin chào").replace("world", "thế giới")


class BadStructureEngine(TranslationEngine):
    """Returns output with every HTML tag stripped -> HTML structure mismatch.

    Strips *all* tags (not just ``<p>``) so the mismatch survives retries: the
    retry prompt itself contains ``<br>``, which would otherwise let a
    ``<p>``-only stripper pass the structural check on attempt 2.
    """

    def __init__(self):
        super().__init__(from_lang="EN", to_lang="VI")

    def supports_batch(self):
        return False

    def translate(self, text):
        return re.sub(r"<[^>]+>", "", text)


class _SubprocSlowEngine(TranslationEngine):
    """Top-level (picklable) slow engine for the subprocess worker test.

    Defined at module level (not nested) so a spawned child can unpickle the
    instance it receives as a ``run_translation`` engine argument.
    """

    def supports_batch(self):
        return False

    def __init__(self):
        super().__init__(from_lang="EN", to_lang="VI")

    def translate(self, text):
        time.sleep(0.6)
        return text.replace("Hello", "Xin chào")


class _FakeBookDoc:
    """Minimal BookDoc stand-in for the TTS cache job tests."""

    def __init__(self, texts):
        # texts = {chapter_number: [paragraph, ...]}
        self._texts = texts
        self.path = "fake-book.epub"
        self.chapters = [
            {"chars": sum(len(t) for t in texts[n])} for n in sorted(texts)
        ]

    def chapter(self, n):
        if n not in self._texts:
            raise ValueError(f"Chapter {n} out of range")
        return {"items": [{"type": "p", "text": t} for t in self._texts[n]]}


def _make_epub(path):
    book = epub.EpubBook()
    book.set_identifier("webui-test")
    book.set_title("WebUI Test")
    book.set_language("en")
    ch = epub.EpubHtml(title="Ch1", file_name="ch1.xhtml", lang="en")
    ch.content = b"<html><body><p>Hello world sample text here</p></body></html>"
    book.add_item(ch)
    book.spine = ["nav", "ch1.xhtml"]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    epub.write_epub(path, book, {})


_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
)


def _make_epub_with_cover(path, with_cover=True):
    book = epub.EpubBook()
    book.set_identifier("webui-cover-test")
    book.set_title("Cover Test")
    book.set_language("en")
    if with_cover:
        img = epub.EpubImage()
        img.id = "cover"
        img.file_name = "cover.png"
        img.media_type = "image/png"
        img.content = _PNG
        book.add_item(img)
    ch = epub.EpubHtml(title="Ch1", file_name="ch1.xhtml", lang="en")
    ch.content = b"<html><body><p>Hello</p></body></html>"
    book.add_item(ch)
    book.spine = ["nav", "ch1.xhtml"]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    epub.write_epub(path, book, {})


class TestWebUI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.src = os.path.join(self.tmp, "in.epub")
        self.out = os.path.join(self.tmp, "out.epub")
        _make_epub(self.src)

        import translator.job as job_mod
        import translator.discord_notifier as dn_mod

        self._orig_build = job_mod.build_engine
        self._orig_discord = dn_mod.DiscordNotifier.send_translation_completed
        job_mod.build_engine = lambda *a, **k: StubEngine()
        dn_mod.DiscordNotifier.send_translation_completed = staticmethod(lambda **k: None)

        from webui import app as webui_app
        from webui.jobs import JobRegistry

        # Isolate jobs in a temp registry so test jobs don't persist in the real
        # webui/jobs/ directory (the "Jobs gần đây" list). Restored in cleanup.
        self._orig_registry = webui_app.registry
        webui_app.registry = JobRegistry(self.tmp)
        self.addCleanup(setattr, webui_app, "registry", self._orig_registry)

        self.app = webui_app.app
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.registry = webui_app.registry

        # Force the in-process thread worker for these tests: the parent patches
        # build_engine (which does not cross into a spawned child), and the
        # thread shares the in-memory Job object the assertions read.
        import webui.core_runner as cr_mod

        self._orig_use_process = cr_mod._USE_PROCESS
        cr_mod._USE_PROCESS = False
        self.addCleanup(setattr, cr_mod, "_USE_PROCESS", self._orig_use_process)

    def tearDown(self):
        import translator.job as job_mod
        import translator.discord_notifier as dn_mod

        job_mod.build_engine = self._orig_build
        dn_mod.DiscordNotifier.send_translation_completed = self._orig_discord

    def test_pages_load(self):
        for path in ("/", "/jobs/new", "/config", "/config/settings", "/books", "/illustrations"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, path)

    def test_html_and_api_pages_are_never_cached(self):
        """F5 (soft reload) must always re-read data from the server, so every
        HTML page and JSON API response carries Cache-Control: no-store."""
        for path in ("/config/settings", "/jobs/new", "/config/keys"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, path)
            self.assertEqual(
                r.headers.get("Cache-Control"),
                "no-store",
                f"{path} must be marked no-store",
            )
        with mock.patch.object(
            webui_app,
            "fetch_models",
            return_value={"engine": "openai", "models": [], "source": "config", "error": "offline test"},
        ):
            r = self.client.get("/api/provider-models?engine=openai")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("Cache-Control"), "no-store")

    def test_book_cover_route(self):
        # Cover bytes are served for an EPUB under PROJECT_ROOT; a path outside
        # it is rejected (400) and an EPUB with no cover 404s.
        cover_path = os.path.join(PROJECT_ROOT, "_test_cover.epub")
        nocov_path = os.path.join(PROJECT_ROOT, "_test_nocover.epub")
        _make_epub_with_cover(cover_path, with_cover=True)
        _make_epub_with_cover(nocov_path, with_cover=False)
        self.addCleanup(lambda: os.path.exists(cover_path) and os.remove(cover_path))
        self.addCleanup(lambda: os.path.exists(nocov_path) and os.remove(nocov_path))

        r = self.client.get("/books/cover?path=" + cover_path)
        self.assertEqual(r.status_code, 200, "cover should be served")
        self.assertTrue(r.content_type.startswith("image/"))
        self.assertEqual(r.data, _PNG)
        # the lock-screen artwork is refetched on every metadata assignment —
        # cache it client-side instead of hitting the server per paragraph
        self.assertIn("max-age", r.headers.get("Cache-Control", ""))

        bad = self.client.get("/books/cover?path=/etc/passwd")
        self.assertEqual(bad.status_code, 400, "path outside PROJECT_ROOT rejected")

        missing = self.client.get("/books/cover?path=" + nocov_path)
        self.assertEqual(missing.status_code, 404, "no cover -> 404")
        self.assertIn("max-age", missing.headers.get("Cache-Control", ""),
                      "negative answers must be cacheable too")

    def test_books_page_renders_shell_fast(self):
        # /books no longer blocks on a full library scan: it renders the toolbar
        # and an empty #lib container that client JS fills via /api/library.
        r = self.client.get("/books")
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertIn('id="lib"', body)
        self.assertIn('id="lib-search"', body)
        self.assertIn('id="lib-refresh"', body)
        self.assertIn("filter-chip", body)
        # window.APP config the client needs is embedded.
        self.assertIn("libUrl", body)
        self.assertIn("coverUrl", body)
        # Preview bug regression: paths must never be embedded into JS literals.
        self.assertNotIn("onclick=\"preview('", body)

    @staticmethod
    def _fake_library():
        """Two groups — one fully translated, one untranslated."""
        src_path = os.path.join(PROJECT_ROOT, "_test_cover.epub")

        def entry(base, title, kind, status, label, name):
            return {
                "name": name, "path": src_path, "size_mb": 0.0,
                "has_checkpoint": False, "base_name": base, "kind": kind,
                "status": status, "label": label, "progress": None,
                "source_path": src_path, "translations": [], "chapters": 1,
                "meta": {"title": title, "creator": "Jane", "language": "en"},
            }

        src = entry("Done", "Done Book", "source", "untranslated", "Nguồn",
                    "Done.epub")
        tr = entry("Done", "Done Book", "translated", "done", "Đã dịch",
                   "Done-dich.epub")
        src["translations"] = [os.path.normcase(os.path.abspath(src_path))]
        done_group = {"base_name": "Done", "title": "Done Book", "source": src,
                      "entries": [src, tr], "translations": [tr],
                      "rank": "done", "rank_label": "Đã dịch"}
        untr = entry("Todo", "Todo Book", "source", "untranslated", "Nguồn",
                     "Todo.epub")
        todo_group = {"base_name": "Todo", "title": "Todo Book", "source": untr,
                      "entries": [untr], "translations": [],
                      "rank": "untranslated", "rank_label": "Chưa dịch"}
        return {"groups": [todo_group, done_group], "stats": {"total": 2, "files": 3,
                "untranslated": 1, "partial": 0, "assumed": 0, "done": 1}}

    def test_books_filter_initializes_client_side(self):
        # /books?filter=done hands the active filter to the client JS. The
        # actual rank filtering happens in the browser; the server only renders
        # the correct initial state.
        r = self.client.get("/books?filter=done")
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertIn('window.APP.initialFilter = "done";', body)
        self.assertIn('class="btn btn-primary filter-chip" data-filter="done"', body)
        self.assertIn('class="btn btn-ghost filter-chip" data-filter=""', body)

        r2 = self.client.get("/books?filter=nonsense")
        self.assertEqual(r2.status_code, 200)
        self.assertIn('window.APP.initialFilter = "";', r2.get_data(as_text=True))

    def test_dashboard_renders_jobs_and_lib_placeholder(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertIn("Jobs gần đây", body)
        self.assertIn('id="lib-mini"', body)
        # Library cards are client-rendered -> not present in the SSR HTML.
        self.assertNotIn("book-card", body)

    def test_books_and_dashboard_skip_library_build(self):
        # Shell rendering must not trigger the expensive per-request build.
        real = webui_app.build_library

        def fake(*a, **k):
            raise AssertionError("build_library must not run for shell render")

        webui_app.build_library = fake
        self.addCleanup(setattr, webui_app, "build_library", real)
        for path in ("/", "/books"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, path)

    def test_api_library_json(self):
        real = webui_app.build_library

        def fake(dirs=None, with_meta=True, with_chapters=False,
                 use_cache=True, refresh=False):
            return self._fake_library()

        webui_app.build_library = fake
        self.addCleanup(setattr, webui_app, "build_library", real)

        r = self.client.get("/api/library")
        self.assertEqual(r.status_code, 200)
        payload = r.get_json()
        self.assertEqual(payload["locale"], "vi")
        self.assertEqual(payload["stats"]["total"], 2)
        self.assertEqual([g["rank"] for g in payload["groups"]],
                         ["untranslated", "done"])
        done = payload["groups"][1]
        self.assertEqual(done["rank_label"], "Đã dịch")
        tr = [e for e in done["entries"] if e["kind"] == "translated"][0]
        self.assertEqual(tr["label"], "Đã dịch")
        self.assertEqual(done["source"]["meta"]["title"], "Done Book")
        # Internal keys must not leak to the client.
        for g in payload["groups"]:
            for e in g["entries"]:
                self.assertNotIn("abs", e)

    def test_api_library_passes_params(self):
        real = webui_app.build_library
        seen = {}

        def fake(dirs=None, with_meta=True, with_chapters=False,
                 use_cache=True, refresh=False):
            seen["with_chapters"] = with_chapters
            seen["use_cache"] = use_cache
            seen["refresh"] = refresh
            return self._fake_library()

        webui_app.build_library = fake
        self.addCleanup(setattr, webui_app, "build_library", real)

        self.client.get("/api/library?chapters=1")
        self.assertIs(seen["with_chapters"], True)
        self.assertIs(seen["use_cache"], True)
        self.assertIs(seen["refresh"], False)

        self.client.get("/api/library?refresh=1")
        self.assertIs(seen["refresh"], True)
        self.assertIs(seen["use_cache"], False)

    def test_api_library_json_locale(self):
        real = webui_app.build_library

        def fake(dirs=None, with_meta=True, with_chapters=False,
                 use_cache=True, refresh=False):
            return self._fake_library()

        webui_app.build_library = fake
        self.addCleanup(setattr, webui_app, "build_library", real)

        payload = self.client.get("/api/library?lang=en").get_json()
        self.assertEqual(payload["locale"], "en")
        done = [g for g in payload["groups"] if g["rank"] == "done"][0]
        self.assertEqual(done["rank_label"], "Done")
        tr = [e for e in done["entries"] if e["kind"] == "translated"][0]
        self.assertEqual(tr["label"], "Done")

    def test_preview_rejects_bad_path_with_json(self):
        r = self.client.get("/books/preview?path=/etc/passwd&chapter=1")
        self.assertEqual(r.status_code, 400)
        self.assertTrue(r.is_json)
        self.assertIn("error", r.get_json())

    def test_preview_rejects_invalid_chapter_with_json(self):
        path = os.path.join(PROJECT_ROOT, "_test_preview_bad.epub")
        _make_epub(path)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        r = self.client.get("/books/preview?path=" + path + "&chapter=abc")
        self.assertEqual(r.status_code, 400)
        self.assertTrue(r.is_json)
        self.assertIn("error", r.get_json())

    def test_preview_ok(self):
        path = os.path.join(PROJECT_ROOT, "_test_preview_ok.epub")
        _make_epub(path)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        r = self.client.get("/books/preview?path=" + path + "&chapter=1")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.is_json)
        payload = r.get_json()
        self.assertIn("Hello world", payload["text"])
        self.assertGreater(payload["total"], 0)

    def test_preview_windows_path_with_backslashes(self):
        # Regression for the reported bug: the Windows path travelled inside a
        # JS string literal and the backslashes were eaten as escapes. The
        # server must accept the fully-serialized path.
        name = "86—EIGHTY-SIX - LN 10-dich.epub"
        path = os.path.join(PROJECT_ROOT, name)
        _make_epub(path)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        r = self.client.get("/books/preview?path=" + path + "&chapter=1")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Hello world", r.get_json()["text"])

    # ------------------------------------------------------------------ /read
    def _reader_epub(self, name="_test_reader.epub"):
        path = os.path.join(PROJECT_ROOT, name)
        _make_epub(path)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return path

    def _warm_noop(self):
        """Never let a test boot the real TTS model (downloads + ~15s load)."""
        from webui import reader
        return mock.patch.object(reader.ENGINE, "ensure_warm")

    def _reader_multi_epub(self, name="_test_reader_multi.epub"):
        """3 chapters with known words — exercises cross-chapter search."""
        path = os.path.join(PROJECT_ROOT, name)
        book = epub.EpubBook()
        book.set_identifier("webui-search")
        book.set_title("Search Test")
        book.set_language("en")
        texts = [
            "The apple is red. An apple a day.",
            "banana split and more banana",
            "cherry  pie with APPLE sauce",
        ]
        names = []
        for i, text in enumerate(texts, start=1):
            ch = epub.EpubHtml(title=f"Ch{i}", file_name=f"c{i}.xhtml", lang="en")
            ch.content = f"<html><body><p>{text}</p></body></html>".encode()
            book.add_item(ch)
            names.append(ch.file_name)
        book.spine = ["nav"] + names
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        epub.write_epub(path, book, {})
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return path

    def test_read_page_renders_with_reader_bootstrap(self):
        path = self._reader_epub()
        with self._warm_noop():
            r = self.client.get("/read?path=" + path)
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertIn("window.APP.reader", body)
        self.assertIn("reader.js", body)
        self.assertIn("reader.css", body)
        self.assertIn("rd-panel-tts", body)
        # P1: iframe document endpoints + sandboxed frame + nested TOC tree
        self.assertIn("tocUrl", body)
        self.assertIn("docUrl", body)
        self.assertIn("stateUrl", body)
        self.assertIn('id="rd-frame"', body)
        self.assertIn('sandbox="allow-same-origin"', body)
        self.assertIn("rd-toc-tree", body)
        self.assertIn("rd-set-margin", body)   # appearance controls
        # P2: search + annotations bootstrap
        self.assertIn("searchUrl", body)
        self.assertIn("annUrl", body)
        self.assertIn("rd-panel-search", body)
        self.assertIn("rd-panel-ann", body)
        self.assertIn("rd-selpop", body)

    def test_read_page_bootstraps_background_music(self):
        """The reader gets a music endpoint + its own controls in the TTS panel."""
        path = self._reader_epub()
        with self._warm_noop():
            r = self.client.get("/read?path=" + path)
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertIn("musicUrl", body)
        self.assertIn('id="rd-music-file"', body)
        self.assertIn('id="rd-music-vol"', body)
        self.assertIn('id="rd-music-play"', body)
        self.assertIn('id="rd-music-note"', body)

    def test_music_list_creates_folder_and_filters_extensions(self):
        music_dir = os.path.join(self.tmp, "music")
        with mock.patch.object(webui_app, "MUSIC_DIR", music_dir):
            r = self.client.get("/api/music")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.get_json(), {"files": []})
            self.assertTrue(os.path.isdir(music_dir), "empty folder is auto-created")

            with open(os.path.join(music_dir, "BGM One.MP3"), "wb") as fh:
                fh.write(b"\xff\xfb" + b"m" * 32)
            with open(os.path.join(music_dir, "readme.txt"), "w", encoding="utf-8") as fh:
                fh.write("not audio")
            r = self.client.get("/api/music")
            files = r.get_json()["files"]
            self.assertEqual([f["name"] for f in files], ["BGM One.MP3"])
            self.assertEqual(files[0]["url"], "/api/music/BGM%20One.MP3")

    def test_music_file_serves_audio_and_refuses_traversal(self):
        music_dir = os.path.join(self.tmp, "music")
        os.makedirs(music_dir, exist_ok=True)
        payload = b"\xff\xfb" + b"music-bytes" * 8
        with open(os.path.join(music_dir, "bgm.mp3"), "wb") as fh:
            fh.write(payload)
        with open(os.path.join(music_dir, "notes.txt"), "w", encoding="utf-8") as fh:
            fh.write("nope")
        with open(os.path.join(self.tmp, "outside.mp3"), "wb") as fh:
            fh.write(b"secret")

        with mock.patch.object(webui_app, "MUSIC_DIR", music_dir):
            r = self.client.get("/api/music/bgm.mp3")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.data, payload)

            # wrong extension → 404
            self.assertEqual(self.client.get("/api/music/notes.txt").status_code, 404)
            # escaping the folder (backslash and slash forms) → 404, never a leak
            self.assertEqual(self.client.get("/api/music/..%5Coutside.mp3").status_code, 404)
            self.assertEqual(self.client.get("/api/music/sub/..%2Foutside.mp3").status_code, 404)

    def test_reader_search_scans_all_chapters(self):
        path = self._reader_multi_epub("_test_reader_search.epub")

        r = self.client.get("/api/reader/search?path=" + path + "&q=apple")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["total"], 3)  # 2 in ch1 + 1 in ch3 (APPLE)
        self.assertFalse(data["truncated"])
        rows = {row["n"]: row for row in data["chapters"]}
        self.assertEqual(set(rows), {1, 3})
        self.assertEqual(rows[1]["count"], 2)
        self.assertEqual(rows[3]["count"], 1)
        self.assertIn("apple", rows[1]["hits"][0]["snip"])

        # case-insensitive
        r = self.client.get("/api/reader/search?path=" + path + "&q=BANANA")
        self.assertEqual(r.get_json()["chapters"][0]["n"], 2)

        # max cap truncates the scan at the first hits
        r = self.client.get("/api/reader/search?path=" + path + "&q=apple&max=2")
        data = r.get_json()
        self.assertEqual(data["total"], 2)
        self.assertTrue(data["truncated"])
        self.assertEqual([row["n"] for row in data["chapters"]], [1])

        # no hits
        r = self.client.get("/api/reader/search?path=" + path + "&q=zzzz")
        self.assertEqual(r.get_json()["total"], 0)
        self.assertEqual(r.get_json()["chapters"], [])

        # validation: empty/oversized query, bad max, foreign path
        r = self.client.get("/api/reader/search?path=" + path + "&q=")
        self.assertEqual(r.status_code, 400)
        r = self.client.get("/api/reader/search?path=" + path + "&q=" + "x" * 201)
        self.assertEqual(r.status_code, 400)
        r = self.client.get("/api/reader/search?path=" + path + "&q=apple&max=nope")
        self.assertEqual(r.status_code, 400)
        r = self.client.get("/api/reader/search?path=/etc/x.epub&q=apple")
        self.assertEqual(r.status_code, 400)

    def test_reader_annotations_crud(self):
        path = self._reader_epub("_test_reader_ann.epub")
        sidecar = os.path.splitext(path)[0] + ".ann.json"
        self.addCleanup(lambda: os.path.exists(sidecar) and os.remove(sidecar))

        # empty before anything was saved
        r = self.client.get("/api/reader/annotations?path=" + path)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), {"anns": []})

        # create
        r = self.client.post(
            "/api/reader/annotations",
            json={
                "path": path,
                "ann": {
                    "ch": 1, "item": 0, "quote": "Hello world",
                    "pre": "", "post": "", "color": "blue", "note": "first",
                },
            },
        )
        self.assertEqual(r.status_code, 201)
        ann = r.get_json()
        self.assertTrue(ann["id"])
        self.assertIn("created", ann)
        self.assertIn("updated", ann)
        self.assertEqual(ann["color"], "blue")
        self.assertTrue(os.path.exists(sidecar))

        # list
        r = self.client.get("/api/reader/annotations?path=" + path)
        self.assertEqual(len(r.get_json()["anns"]), 1)

        # update keeps created
        ann["note"] = "edited"
        ann["color"] = "pink"
        r = self.client.put("/api/reader/annotations", json={"path": path, "ann": ann})
        self.assertEqual(r.status_code, 200)
        upd = r.get_json()
        self.assertEqual(upd["note"], "edited")
        self.assertEqual(upd["color"], "pink")
        self.assertEqual(upd["created"], ann["created"])

        # unknown id on PUT → 404
        r = self.client.put(
            "/api/reader/annotations",
            json={"path": path, "ann": dict(ann, id="nope")},
        )
        self.assertEqual(r.status_code, 404)

        # delete → 200, second delete → 404
        url = "/api/reader/annotations?path=" + path + "&id=" + ann["id"]
        self.assertEqual(self.client.delete(url).status_code, 200)
        self.assertEqual(self.client.delete(url).status_code, 404)
        r = self.client.get("/api/reader/annotations?path=" + path)
        self.assertEqual(r.get_json()["anns"], [])

        # validation: bad payloads → 400
        for bad in (
            {"ch": 1, "quote": ""},                   # empty quote
            {"ch": 0, "quote": "x"},                  # chapter out of range
            {"ch": 1, "quote": "x", "color": "red"},  # unknown color
            {"ch": 1, "quote": "x" * 501},            # quote too long
            None,                                     # not an object
        ):
            r = self.client.post(
                "/api/reader/annotations", json={"path": path, "ann": bad}
            )
            self.assertEqual(r.status_code, 400, bad)

        # foreign path refused
        r = self.client.post(
            "/api/reader/annotations",
            json={"path": "/etc/x.epub", "ann": {"ch": 1, "quote": "x"}},
        )
        self.assertEqual(r.status_code, 400)

    def test_i18n_locales_have_identical_keys(self):
        from webui import i18n as i18n_mod

        vi = set(i18n_mod.TRANSLATIONS["vi"])
        en = set(i18n_mod.TRANSLATIONS["en"])
        self.assertEqual(vi, en)
        # the keys the reader template/js rely on must exist
        for key in ("read.mode_scroll", "read.mode_page", "read.fullscreen",
                    "read.footnote", "read.page_of", "read.eta",
                    "read.time_hm", "read.landmarks", "read.override_font"):
            self.assertIn(key, vi)

    def test_read_page_rejects_bad_path(self):
        r = self.client.get("/read?path=/etc/passwd")
        self.assertEqual(r.status_code, 400)

    def test_reader_meta_excludes_nav_document(self):
        # The EPUB nav (table of contents) document is also ITEM_DOCUMENT — it
        # must not show up as a readable chapter.
        path = self._reader_epub("_test_reader_meta.epub")
        r = self.client.get("/api/reader/meta?path=" + path)
        self.assertEqual(r.status_code, 200)
        meta = r.get_json()
        self.assertEqual(meta["total"], 1)
        self.assertEqual(meta["chapters"][0]["n"], 1)
        self.assertEqual(meta["title"], "WebUI Test")

    def test_reader_chapter_payload(self):
        path = self._reader_epub("_test_reader_ch.epub")
        r = self.client.get("/api/reader/chapter?path=" + path + "&n=1")
        self.assertEqual(r.status_code, 200)
        ch = r.get_json()
        self.assertEqual(ch["n"], 1)
        self.assertIsNone(ch["prev"])
        self.assertIsNone(ch["next"])
        self.assertEqual(ch["total"], 1)
        self.assertTrue(ch["items"])
        self.assertEqual(ch["items"][0]["type"], "p")
        self.assertIn("Hello world", ch["items"][0]["text"])

    def test_reader_chapter_errors_are_json(self):
        path = self._reader_epub("_test_reader_bad.epub")
        r = self.client.get("/api/reader/chapter?path=" + path + "&n=42")
        self.assertEqual(r.status_code, 400)
        self.assertTrue(r.is_json)
        self.assertIn("error", r.get_json())

        r = self.client.get("/api/reader/chapter?path=" + path + "&n=abc")
        self.assertEqual(r.status_code, 400)
        self.assertTrue(r.is_json)

        r = self.client.get("/api/reader/meta?path=" + os.path.join("C:\\", "nope.epub"))
        self.assertEqual(r.status_code, 400)
        self.assertTrue(r.is_json)

    # ------------------------------------------------- reader iframe documents
    def _reader_rich_epub(self, name="_test_reader_rich.epub", lang="vi"):
        """2 chapters + image + internal/external/footnote links + CSS."""
        from ebooklib import epub as _epub

        path = os.path.join(PROJECT_ROOT, name)
        book = _epub.EpubBook()
        book.set_identifier("webui-rich")
        book.set_title("Rich Reader")
        book.set_language(lang)
        book.add_item(_epub.EpubItem(
            uid="css", file_name="Styles/main.css", media_type="text/css",
            content=b'body{color:#222}.x{background:url(../Images/p.png)}'))
        img = _epub.EpubImage()
        img.id, img.file_name, img.media_type, img.content = (
            "p", "Images/p.png", "image/png", _PNG)
        book.add_item(img)

        ch1 = _epub.EpubHtml(title="Ch1", file_name="ch1.xhtml", lang="vi")
        ch1.content = (
            '<html><head><title>Ch1 head</title></head><body>'
            '<h2>Chuong 1</h2>'
            '<p onclick="evil()" style="color:red">Doan '
            '<a href="ch2.xhtml#sec">link noi</a> '
            '<a href="https://vi.wikipedia.org/wiki/Truyen">ngoai</a> '
            '<a href="javascript:evil()">js</a>.</p>'
            '<p><img src="../Images/p.png" alt="anh"/></p>'
            '<p>Foot: <a href="#foot1">chú thich</a>.</p>'
            '<aside epub:type="footnote" id="foot1"><p>Noi dung foot.</p></aside>'
            '<table><tr><td>o1</td></tr></table>'
            '<script>bad()</script><iframe src="http://evil"></iframe>'
            '</body></html>'
        )
        ch2 = _epub.EpubHtml(title="Ch2", file_name="ch2.xhtml", lang="vi")
        ch2.content = ('<html><body><h2>Chuong 2</h2>'
                       '<p id="sec">Muc 2 voi <img src="../Images/p.png"/>.</p>'
                       '</body></html>')
        book.add_item(ch1)
        book.add_item(ch2)
        book.spine = ["nav", ch1, ch2]
        book.toc = [_epub.Link("ch1.xhtml", "Chuong 1", "c1"),
                    _epub.Link("ch2.xhtml", "Chuong 2", "c2")]
        book.add_item(_epub.EpubNcx())
        book.add_item(_epub.EpubNav())
        _epub.write_epub(path, book, {})
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return path

    def test_reader_toc_endpoint_maps_nav_to_chapters(self):
        path = self._reader_rich_epub("_test_reader_toc.epub")
        r = self.client.get("/api/reader/toc?path=" + path)
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertIn("toc", data)
        self.assertIn("landmarks", data)
        self.assertIn("pages", data)
        # nav doc itself is not a chapter; its links map to chapter numbers
        self.assertEqual([t["n"] for t in data["toc"]], [1, 2])
        self.assertEqual(data["toc"][0]["children"], [])

    def test_reader_doc_serves_sanitized_chapter_html(self):
        path = self._reader_rich_epub("_test_reader_doc.epub")
        r = self.client.get("/api/reader/doc?path=" + path + "&n=1")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "text/html")
        self.assertIn("default-src 'none'", r.headers.get("Content-Security-Policy", ""))
        body = r.get_data(as_text=True)

        self.assertIn('class="rd-content"', body)          # layout wrapper
        self.assertIn("Content-Security-Policy", body)      # CSP meta too
        self.assertNotIn("<script", body.lower())
        self.assertNotIn("onclick", body.lower())
        self.assertNotIn("<iframe", body.lower())
        self.assertNotIn("javascript:", body.lower())
        # internal link → chapter document URL, external kept, table normalized
        flat = body.replace("&amp;", "&")
        self.assertIn("/api/reader/doc?path=", flat)
        self.assertIn("n=2#sec", flat)
        self.assertIn("https://vi.wikipedia.org/wiki/Truyen", body)
        self.assertIn("<tbody>", body)
        self.assertIn('<aside epub:type="footnote"', body)
        # image + book CSS point at the resource endpoint
        self.assertIn("/api/reader/res?path=", flat)
        self.assertIn("ref=Images/p.png", flat)

        # the chapter's own <head><title> is replaced, never shown as a paragraph
        self.assertNotIn("Ch1 head", body)

        # bad chapter → HTML error page, not JSON (it renders in the iframe)
        bad = self.client.get("/api/reader/doc?path=" + path + "&n=42")
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(bad.mimetype, "text/html")

    def test_reader_doc_declares_the_content_language(self):
        # A translation output keeps dc:language=zh in the OPF, but the page
        # shows the TARGET text: declaring zh on Vietnamese makes the browser
        # apply CJK font fallback per glyph and the diacritics render with
        # gaps after every accented letter (the "nặ m / chiế n" bug). The
        # source book keeps its own language; the OPF metadata stays as-is.
        src = self._reader_rich_epub("_test_reader_src.epub", lang="zh")
        body = self.client.get("/api/reader/doc?path=" + src + "&n=1").get_data(as_text=True)
        self.assertIn('<html lang="zh">', body,
                      "source book keeps its own language on the root")

        out = self._reader_rich_epub("_test_reader_out.translated.epub", lang="zh")
        body = self.client.get("/api/reader/doc?path=" + out + "&n=1").get_data(as_text=True)
        self.assertIn('<html lang="vi">', body,
                      "translated content must be declared as Vietnamese")
        self.assertNotIn('<html lang="zh">', body)
        meta = self.client.get("/api/reader/meta?path=" + out).get_json()
        self.assertEqual(meta["language"], "zh", "OPF metadata must stay untouched")

    def test_reader_toolbar_layout_and_font_guard(self):
        def read(*parts):
            with open(os.path.join(PROJECT_ROOT, "webui", *parts), encoding="utf-8") as fh:
                return fh.read()

        css = read("static", "reader.css")
        # phone: the action row stays ONE line (sideways scroll) so no button
        # is stranded alone on a second row, and the bottom bar retracts with
        # the rest on scroll — except while the read-aloud transport shows
        self.assertIn("flex-wrap: nowrap", css)
        self.assertIn("body.chrome-away .rd-jump", css)
        self.assertIn(":has(.rd-tts-jump:not([hidden]))", css)
        # the paragraph counter moves into the progress row on the phone
        self.assertIn("#rd-p-pos-m", css)

        js = read("static", "reader.js")
        # Vietnamese coverage: Noto (full diacritics) in the reading stacks,
        # and an uploaded font is probed pixel-by-pixel before it can break
        # the text (fonts.check() only reports loading state, not glyphs)
        self.assertIn("'Noto Serif'", js)
        self.assertIn("function probeCustomFont(", js)
        self.assertIn("function lacksVietnamese(", js)
        self.assertIn("read.font_no_vi", js)

        html = read("templates", "reader.html")
        self.assertIn('id="rd-font-warn"', html)
        self.assertIn('id="rd-p-pos-m"', html)
        # assets carry their mtime as ?v= — the service worker answers
        # static requests cache-first, so without a fresh URL an edit would
        # stay invisible until the SW's own version changed (the cover
        # refetch fix never reached the browser that way)
        self.assertIn("asset_v('reader.js')", html)

        page = self.client.get(
            "/read?path=" + self._reader_rich_epub("_test_reader_v.epub")
        ).get_data(as_text=True)
        self.assertIn("/static/reader.js?v=", page,
                      "the rendered page must carry the asset version")

        from webui import i18n as wui_i18n

        for lang in ("vi", "en"):
            self.assertIn("read.font_no_vi", wui_i18n.TRANSLATIONS[lang])

        # reader assets changed → bump so installed PWAs refetch them
        self.assertIn("'pytb-v16'", read("static", "sw.js"))

    def test_reader_res_serves_assets_and_refuses_documents(self):
        path = self._reader_rich_epub("_test_reader_res.epub")

        r = self.client.get("/api/reader/res?path=" + path + "&ref=Images/p.png")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "image/png")

        r = self.client.get("/api/reader/res?path=" + path + "&ref=Styles/main.css")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "text/css")
        css = r.get_data(as_text=True)
        self.assertIn("/api/reader/res?ref=EPUB/Images/p.png", css)  # url() rewritten

        # chapters, traversal and unknown files never come out raw
        for ref in ("ch1.xhtml", "../secrets.png", "nope.png", "ch1.xhtml%00.png"):
            r = self.client.get("/api/reader/res?path=" + path + "&ref=" + ref)
            self.assertEqual(r.status_code, 404, ref)

        r = self.client.get("/api/reader/res?path=/etc/x.epub&ref=Images/p.png")
        self.assertEqual(r.status_code, 400)

    def test_reader_state_roundtrip(self):
        from webui import reader as _reader

        path = self._reader_epub("_test_reader_state.epub")
        side = _reader.state_path_for(path)
        self.addCleanup(lambda: os.path.exists(side) and os.remove(side))

        state = {"pos": {"ch": 1, "path": "/4/1", "ratio": 0.25},
                 "settings": {"size": 21, "mode": "page"}}
        r = self.client.put("/api/reader/state", json={"path": path, "state": state})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])

        r = self.client.get("/api/reader/state?path=" + path)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), state)

        self.assertEqual(
            self.client.put("/api/reader/state", json={"path": path}).status_code, 400)
        self.assertEqual(
            self.client.put("/api/reader/state",
                            json={"path": "/etc/x.epub", "state": {}}).status_code, 400)
        # broken/absent sidecar reads as an empty object
        os.remove(side)
        r = self.client.get("/api/reader/state?path=" + path)
        self.assertEqual(r.get_json(), {})

    def test_tts_voices_reports_engine_state(self):
        from webui import reader
        fake = {
            "state": "ready", "error": None, "load_seconds": 12.3,
            "voices": [{"id": "v1", "label": "Test voice"}], "default": "v1",
        }
        with mock.patch.object(reader.ENGINE, "ensure_warm"), \
                mock.patch.object(reader.ENGINE, "status", return_value=fake):
            r = self.client.get("/api/tts/voices")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["state"], "ready")
        self.assertEqual(data["voices"][0]["label"], "Test voice")

    def test_tts_rejects_bad_payload(self):
        from webui import reader
        with mock.patch.object(reader.ENGINE, "ensure_warm"):
            r = self.client.post("/api/tts", json={"text": ""})
            self.assertEqual(r.status_code, 400)
            self.assertIn("error", r.get_json())

            r = self.client.post("/api/tts", json={"text": "x" * (reader.MAX_TEXT + 1)})
            self.assertEqual(r.status_code, 400)

            r = self.client.post("/api/tts", json={"text": "ok", "voice": 42})
            self.assertEqual(r.status_code, 400)

    def test_tts_returns_wav_when_engine_ready(self):
        from webui import reader
        wav = b"RIFF0000WAVEfake"
        segs = [[0, 9, 0.0], [10, 18, 1.25]]
        with mock.patch.object(reader.ENGINE, "ensure_warm"), \
                mock.patch.object(reader.ENGINE, "synthesize", return_value=(wav, segs)) as syn:
            r = self.client.post("/api/tts", json={"text": "Xin chào", "voice": "v1"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "audio/wav")
        self.assertEqual(r.get_data(), wav)
        syn.assert_called_once_with("Xin chào", "v1")
        # sentence timing rides along for the client's sentence highlight
        self.assertEqual(json.loads(r.headers["X-TTS-Segs"]), segs)

    def test_tts_503_when_engine_not_ready(self):
        from webui import reader
        with mock.patch.object(reader.ENGINE, "ensure_warm"), \
                mock.patch.object(reader.ENGINE, "synthesize",
                                  side_effect=RuntimeError("tts not ready (state=idle)")):
            r = self.client.post("/api/tts", json={"text": "Xin chào"})
        self.assertEqual(r.status_code, 503)
        self.assertIn("not ready", r.get_json()["error"])

    def test_tts_stream_frames_and_errors(self):
        import struct
        from webui import reader

        def frames(data):
            """uint32be hdrLen | JSON hdr | uint32be wavLen | wav — per piece."""
            out, off = [], 0
            while off < len(data):
                hl = struct.unpack(">I", data[off:off + 4])[0]
                off += 4
                hdr = json.loads(data[off:off + hl])
                off += hl
                wl = struct.unpack(">I", data[off:off + 4])[0]
                off += 4
                out.append((hdr, data[off:off + wl]))
                off += wl
            return out

        with mock.patch.object(reader.ENGINE, "ensure_warm"):
            # validation mirrors /api/tts — bad payloads never stream
            self.assertEqual(
                self.client.post("/api/tts/stream", json={"text": ""}).status_code, 400)
            self.assertEqual(
                self.client.post("/api/tts/stream",
                                 json={"text": "x" * (reader.MAX_TEXT + 1)}).status_code, 400)
            self.assertEqual(
                self.client.post("/api/tts/stream", json={"text": "ok", "voice": 42}).status_code, 400)

            # engine not ready → 503 with the readable error, before any frame
            with mock.patch.object(reader.ENGINE, "synthesize_stream",
                                   side_effect=RuntimeError("tts not ready (state=idle)")):
                r = self.client.post("/api/tts/stream", json={"text": "Xin chào"})
                self.assertEqual(r.status_code, 503)
                self.assertIn("not ready", r.get_json()["error"])

            # ready → one framed piece after another, `final` on the last one
            def fake_stream(text, voice=None):
                yield {"i": 0, "s": 0, "e": 5, "d": 0.5, "sr": 48000,
                       "final": False}, b"RIFFaaaa"
                yield {"i": 1, "s": 6, "e": 11, "d": 0.4, "sr": 48000,
                       "final": True}, b"RIFFbbbb"

            with mock.patch.object(reader.ENGINE, "synthesize_stream",
                                   side_effect=fake_stream) as syn:
                r = self.client.post("/api/tts/stream",
                                     json={"text": "Xin chào bạn", "voice": "v1"})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.mimetype, "application/octet-stream")
            got = frames(r.get_data())
            self.assertEqual([h["i"] for h, _ in got], [0, 1])
            self.assertEqual([w for _, w in got], [b"RIFFaaaa", b"RIFFbbbb"])
            self.assertFalse(got[0][0]["final"])
            self.assertTrue(got[1][0]["final"])
            syn.assert_called_once_with("Xin chào bạn", "v1")

            # a mid-stream crash reaches the client as an error frame,
            # not as a dead connection with half a paragraph
            def boom(text, voice=None):
                yield {"i": 0, "s": 0, "e": 3, "d": 0.2, "sr": 48000,
                       "final": False}, b"RIFFaaaa"
                raise RuntimeError("engine died")

            with mock.patch.object(reader.ENGINE, "synthesize_stream",
                                   side_effect=boom):
                r = self.client.post("/api/tts/stream", json={"text": "Xin chào"})
            self.assertEqual(r.status_code, 200)   # headers already sent
            got = frames(r.get_data())
            self.assertEqual(len(got), 2)
            self.assertIn("engine died", got[1][0]["error"])
            self.assertEqual(got[1][1], b"")

    def test_split_for_tts_respects_the_char_limit(self):
        from webui import reader
        long_text = ("Một câu dài. " * 60).strip()
        parts = reader.split_for_tts(long_text)
        self.assertTrue(parts)
        self.assertTrue(all(len(p) <= reader._CHUNK_CHARS for p in parts))
        self.assertEqual(" ".join(parts).replace(" .", "."), long_text)
        self.assertEqual(reader.split_for_tts("   "), [])

    def test_split_for_tts_cuts_at_sentences_and_clauses(self):
        from webui import reader
        # the pre-c47d63c cut, restored after an A/B of rendered WAVs picked it
        # as the original voice: every clause becomes its own infer() call, so
        # the model resets its intonation the way the listener remembers
        text = "Nghe thấy câu này, Tào Sảng cười, chối phắt: “Làm sao có thể chứ?”"
        self.assertEqual(reader.split_for_tts(text),
                         ["Nghe thấy câu này,", "Tào Sảng cười,",
                          "chối phắt:", "“Làm sao có thể chứ?”"])
        self.assertEqual(reader.split_for_tts("Ừ. Đúng vậy. Không nói gì."),
                         ["Ừ.", "Đúng vậy.", "Không nói gì."])
        # a lone sentence has nothing to cut — it stays as-is
        self.assertEqual(reader.split_for_tts("Ừ."), ["Ừ."])

    def test_split_for_tts_long_clause_runs_cut_at_spaces(self):
        from webui import reader
        clause = "một hai ba bốn năm sáu bảy tám chín mười mười một"
        text = ", ".join([clause] * 5) + "."
        self.assertGreater(len(text), reader._CHUNK_CHARS)
        parts = reader.split_for_tts(text)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p) <= reader._CHUNK_CHARS for p in parts))
        # clause pieces keep their trailing comma, joined they rebuild the text
        self.assertTrue(parts[0].endswith(","))
        self.assertEqual(" ".join(parts), reader._collapse(text))

    def test_split_for_tts_seg_offsets_slice_the_collapsed_text(self):
        from webui import reader
        cases = [
            "Một câu ngắn. Câu thứ hai! Câu thứ ba?",
            ("Một câu dài. " * 60).strip(),
            "  khoảng   trắng   giữa  các   từ  ",
            "x" * (reader._CHUNK_CHARS * 2 + 37),   # forced mid-piece cuts
            "clause one, clause two; clause three.",
        ]
        for text in cases:
            collapsed = reader._collapse(text)
            segs = reader.split_for_tts_seg(text)
            pieces = reader.split_for_tts(text)
            self.assertEqual([p for p, _s, _e in segs], pieces, text[:40])
            last_end = 0
            for piece, s, e in segs:
                self.assertEqual(collapsed[s:e], piece, (text[:40], s, e))
                self.assertGreaterEqual(s, last_end)
                self.assertGreater(e, s)
                last_end = e
        self.assertEqual(reader.split_for_tts_seg("   "), [])

    def test_tts_guard_flags_only_overrun_audio(self):
        from webui import reader

        # the real chap_0323 case: 31-char piece, normal ≈1.7–1.9 s,
        # a free-running third copy lands at ≈2.4–2.7 s
        shout = '"Giáo quan tới! Giáo quan tới!"'
        self.assertEqual(len(shout), 31)
        self.assertTrue(reader._suspects_extra_copy(shout, 2.64))
        self.assertTrue(reader._suspects_extra_copy(shout, 2.40))
        self.assertFalse(reader._suspects_extra_copy(shout, 1.84))
        self.assertFalse(reader._suspects_extra_copy(shout, 2.08))

        # 13-char phrase: normal ≈0.93 s stays quiet, a doubled read trips
        self.assertFalse(reader._suspects_extra_copy("Giáo quan tới!", 0.96))
        self.assertTrue(reader._suspects_extra_copy("Giáo quan tới!", 1.92))

        # long pieces: normal ~0.05 s/char never trips; +1.5 s overrun does
        normal64 = 'Giáo quan nhíu mày: "Cậu xem mấy ký túc xá khác còn trống không?'
        self.assertEqual(len(normal64), 64)
        self.assertFalse(reader._suspects_extra_copy(normal64, 3.36))
        self.assertTrue(reader._suspects_extra_copy(normal64, 5.10))

        # floor protects tiny pieces; a doubled short stub still trips
        self.assertFalse(reader._suspects_extra_copy("Ừ.", 0.6))
        self.assertTrue(reader._suspects_extra_copy("Ừ.", 1.2))
        self.assertFalse(reader._suspects_extra_copy("", 0.0))

    # ---------------------------------------------------------- AI lookup

    def _patch_lookup_urlopen(self, seen, reply="nghĩa là …"):
        """Route webui.lookup's HTTP calls into a fake OpenAI response."""
        from webui import lookup as lk_mod

        def fake(req, timeout=None, *a, **k):
            seen["url"] = req.full_url
            seen["auth"] = req.get_header("Authorization")
            seen["body"] = json.loads(req.data.decode("utf-8"))
            seen["timeout"] = timeout

            class _Resp:
                def read(self_inner):
                    return json.dumps(
                        {"choices": [{"message": {"content": "  " + reply + "  "}}]}
                    ).encode("utf-8")

                def __enter__(self_inner):
                    return self_inner

                def __exit__(self_inner, *exc):
                    return False

            return _Resp()

        orig = lk_mod.urllib.request.urlopen
        lk_mod.urllib.request.urlopen = fake
        self.addCleanup(setattr, lk_mod.urllib.request, "urlopen", orig)
        return lk_mod

    def test_lookup_explain_picks_first_usable_provider(self):
        seen = {}
        lk = self._patch_lookup_urlopen(seen)
        cfg = {
            "openai": {"api_key": ""},                 # builtin without key → skip
            "gemini": {"api_key": "", "model": "g"},   # ditto
            "groq": {
                "type": "openai_compatible",
                "base_url": "https://api.groq.com/openai/v1",
                "api_key": "k1",
                "model": "m1",
            },
        }
        out = lk.explain(cfg, "hạ hạ", "ngữ cảnh")
        self.assertEqual(out["engine"], "groq")
        self.assertEqual(out["result"], "nghĩa là …")   # whitespace stripped
        self.assertTrue(seen["url"].endswith("/chat/completions"))
        self.assertEqual(seen["auth"], "Bearer k1")
        self.assertEqual(seen["body"]["model"], "m1")
        # no length/temperature knobs: several models reject non-defaults
        self.assertNotIn("max_tokens", seen["body"])
        self.assertNotIn("temperature", seen["body"])
        user = seen["body"]["messages"][1]["content"]
        self.assertIn("hạ hạ", user)
        self.assertIn("ngữ cảnh", user)

    def test_lookup_maps_builtin_gemini_to_openai_compat(self):
        seen = {}
        lk = self._patch_lookup_urlopen(seen)
        out = lk.explain(
            {"gemini": {"api_key": "AIza", "model": "flash-lite"}}, "truong"
        )
        self.assertEqual(out["engine"], "gemini")
        self.assertIn(
            "generativelanguage.googleapis.com/v1beta/openai/chat/completions",
            seen["url"],
        )

    def test_lookup_engine_preference_and_keyless_provider(self):
        seen = {}
        lk = self._patch_lookup_urlopen(seen)
        cfg = {
            "lookup": {"engine": "ollama"},
            "gemini": {"api_key": "AIza", "model": "g"},
            "ollama": {
                "type": "openai_compatible",
                "base_url": "http://localhost:11434/v1",
                "model": "qwen3:14b",
            },
        }
        out = lk.explain(cfg, "từ")
        self.assertEqual(out["engine"], "ollama")
        self.assertIn("localhost:11434", seen["url"])
        self.assertIsNone(seen["auth"])                 # local box: no Bearer

    def test_lookup_no_usable_provider_raises(self):
        from webui import lookup as lk_mod

        with self.assertRaises(lk_mod.LookupError):
            lk_mod.explain({"openai": {"api_key": ""}}, "từ")

    def test_lookup_http_error_becomes_lookup_error(self):
        import io
        import urllib.error

        seen = {}
        lk = self._patch_lookup_urlopen(seen)

        def boom(req, timeout=None, *a, **k):
            raise urllib.error.HTTPError(
                req.full_url, 429, "Too Many Requests", {},
                io.BytesIO(b'{"error": {"message": "rate limit"}}'),
            )

        orig = lk.urllib.request.urlopen
        lk.urllib.request.urlopen = boom
        self.addCleanup(setattr, lk.urllib.request, "urlopen", orig)
        with self.assertRaises(lk.LookupError) as ctx:
            lk.explain({"gemini": {"api_key": "AIza", "model": "g"}}, "từ")
        self.assertIn("429", str(ctx.exception))

    def test_lookup_falls_back_to_next_provider_on_error(self):
        import io
        import urllib.error

        seen = {}
        lk = self._patch_lookup_urlopen(seen)
        attempts = []

        def fake(req, timeout=None, *a, **k):
            attempts.append(req.full_url)
            if "generativelanguage" in req.full_url:      # first choice: 503 spike
                raise urllib.error.HTTPError(
                    req.full_url, 503, "UNAVAILABLE", {}, io.BytesIO(b"{}")
                )

            class _Resp:
                def read(self_inner):
                    return json.dumps(
                        {"choices": [{"message": {"content": "chữ"}}]}
                    ).encode("utf-8")

                def __enter__(self_inner):
                    return self_inner

                def __exit__(self_inner, *exc):
                    return False

            return _Resp()

        orig = lk.urllib.request.urlopen
        lk.urllib.request.urlopen = fake
        self.addCleanup(setattr, lk.urllib.request, "urlopen", orig)
        cfg = {
            "gemini": {"api_key": "AIza", "model": "g"},
            "groq": {
                "type": "openai_compatible",
                "base_url": "https://api.groq.com/openai/v1",
                "api_key": "k",
                "model": "m",
            },
        }
        out = lk.explain(cfg, "từ")
        self.assertEqual(out["engine"], "groq")
        self.assertEqual(out["result"], "chữ")
        self.assertEqual(len(attempts), 2)                 # gemini → groq, then stop

    def test_lookup_endpoint_validates_text(self):
        self.assertEqual(
            self.client.post("/api/reader/lookup", json={"text": ""}).status_code, 400
        )
        self.assertEqual(
            self.client.post("/api/reader/lookup", json={}).status_code, 400
        )

    def test_lookup_endpoint_success_truncates_input(self):
        with mock.patch(
            "webui.lookup.explain",
            return_value={"engine": "gemini", "result": "nghĩa"},
        ) as ex:
            r = self.client.post(
                "/api/reader/lookup",
                json={"text": "x" * 500, "ctx": "c" * 900},
            )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["result"], "nghĩa")
        from webui import lookup as lk_mod

        args = ex.call_args[0]
        self.assertEqual(len(args[1]), lk_mod.MAX_TEXT)
        self.assertEqual(len(args[2]), lk_mod.MAX_CTX)

    def test_lookup_endpoint_maps_lookup_error_to_502(self):
        from webui import lookup as lk_mod

        with mock.patch(
            "webui.lookup.explain",
            side_effect=lk_mod.LookupError("no provider"),
        ):
            r = self.client.post("/api/reader/lookup", json={"text": "từ"})
        self.assertEqual(r.status_code, 502)
        self.assertIn("no provider", r.get_json()["error"])

    def test_pwa_manifest_and_service_worker(self):
        m = self.client.get("/manifest.webmanifest")
        self.assertEqual(m.status_code, 200)
        self.assertIn("manifest+json", m.headers.get("Content-Type", ""))
        data = json.loads(m.data)
        self.assertEqual(data["scope"], "/")
        self.assertTrue(data["icons"])
        for icon in data["icons"]:
            r = self.client.get(icon["src"])
            self.assertEqual(r.status_code, 200, icon["src"])
        sw = self.client.get("/sw.js")
        self.assertEqual(sw.status_code, 200)
        self.assertEqual(sw.headers.get("Service-Worker-Allowed"), "/")
        self.assertIn("self.addEventListener", sw.data.decode("utf-8"))

    def test_pages_link_the_pwa_shell(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'rel="manifest"', r.data)
        self.assertIn(b"/sw.js", r.data)

    def test_tts_engine_synthesizes_with_a_stub_model(self):
        from webui import reader

        class _Stub:
            sample_rate = 48000

            def __init__(self):
                self.calls = []

            def infer(self, text, voice=None):
                self.calls.append((text, voice))
                return [0.0] * 480

        eng = reader.TTSEngine()
        stub = _Stub()
        eng._tts = stub
        eng.state = "ready"
        eng.default_voice = "Hải Đăng"
        eng.voices = [{"id": "Hải Đăng", "label": "Hải Đăng"}]

        wav, segs = eng.synthesize("Một hai ba.", voice="khong-ton-tai")
        self.assertTrue(wav.startswith(b"RIFF"))
        self.assertEqual(stub.calls[-1][1], "Hải Đăng")  # unknown voice → default
        # segs: [start, end, t] rows over the collapsed text, time ascending
        self.assertEqual(len(segs), 1)
        s, e, t = segs[0]
        self.assertEqual(("Một hai ba."[s:e]), "Một hai ba.")
        self.assertEqual(t, 0.0)

        wav2, _ = eng.synthesize("Một.", voice="Hải Đăng")
        self.assertEqual(stub.calls[-1][1], "Hải Đăng")

        # synthesis LRU: an identical repeat must not hit the model again
        calls_before = len(stub.calls)
        wav3, segs3 = eng.synthesize("Một hai ba.", voice="khong-ton-tai")
        self.assertEqual(len(stub.calls), calls_before)
        self.assertEqual(wav3, wav)
        self.assertEqual(segs3, segs)

        # Defensive clamp: an over-long request must not reach the model whole.
        eng.synthesize("x" * (reader.MAX_TEXT + 500), voice="Hải Đăng")
        self.assertLessEqual(max(len(t) for t, _ in stub.calls), reader.MAX_TEXT)

        with self.assertRaises(RuntimeError):
            reader.TTSEngine().synthesize("chưa sẵn sàng")

    def test_tts_engine_resynthesizes_a_piece_that_overruns(self):
        from webui import reader

        class _Stub:
            sample_rate = 48000

            def __init__(self):
                self.calls = []

            def infer(self, text, voice=None):
                self.calls.append(text)
                # first render free-runs (≈3 s for an 11-char piece); a fresh
                # sample comes back clean (≈0.4 s)
                secs = 3.0 if len(self.calls) == 1 else 0.4
                return [0.0] * int(48000 * secs)

        eng = reader.TTSEngine()
        stub = _Stub()
        eng._tts = stub
        eng.state = "ready"
        eng.default_voice = "Hải Đăng"
        eng.voices = [{"id": "Hải Đăng", "label": "Hải Đăng"}]

        wav, _segs = eng.synthesize("Một hai ba.", voice="Hải Đăng")
        self.assertEqual(len(stub.calls), 2)   # suspect overrun → one retry
        expected = reader._to_wav([[0.0] * int(48000 * 0.4)], 48000)
        self.assertEqual(wav, expected)        # the clean sample wins

        # a clean render is never re-synthesized (2 calls → 3, no 4th)
        eng.synthesize("Một câu khác hẳn.", voice="Hải Đăng")
        self.assertEqual(len(stub.calls), 3)

    def test_tts_engine_silences_text_it_cannot_read(self):
        from webui import reader

        class _Stub:
            sample_rate = 48000

            def __init__(self):
                self.calls = []

            def infer(self, text, voice=None):
                self.calls.append(text)
                # the phonemizer yields no phonemes for Chinese or for
                # punctuation-only text like a lone "？"
                if (any("㐀" <= c <= "鿿" for c in text)
                        or not any(c.isalpha() for c in text)):
                    return []
                return [0.0] * 4800

        eng = reader.TTSEngine()
        stub = _Stub()
        eng._tts = stub
        eng.state = "ready"
        eng.default_voice = "Hải Đăng"
        eng.voices = [{"id": "Hải Đăng", "label": "Hải Đăng"}]

        import io
        import wave

        def dur(wav_bytes):
            with wave.open(io.BytesIO(wav_bytes)) as wf:
                return wf.getnframes() / wf.getframerate()

        # a lone full-width "？": nothing to pronounce → never reaches the
        # model, goes straight to the silence floor
        wav, segs = eng.synthesize("？", voice="Hải Đăng")
        self.assertEqual(len(stub.calls), 0)
        self.assertGreaterEqual(dur(wav), reader._SILENCE_MIN)
        self.assertTrue(wav.startswith(b"RIFF"))
        self.assertEqual(len(segs), 1)

        # "……" phonemizes to a lone "." token that the model would turn
        # into junk speech (heard as Chinese-sounding gibberish) → pause
        wav_e, segs_e = eng.synthesize("……", voice="Hải Đăng")
        self.assertEqual(len(stub.calls), 0)
        self.assertGreaterEqual(dur(wav_e), reader._SILENCE_MIN)
        self.assertEqual(len(segs_e), 1)

        # an untranslated paragraph: the model IS asked, comes back empty,
        # and the piece becomes silence at reading pace with segs intact
        text = "全班掌声热烈。"
        wav2, segs2 = eng.synthesize(text, voice="Hải Đăng")
        self.assertEqual(len(stub.calls), 1)
        expected = max(len(text) * reader._SILENCE_SEC_PER_CHAR,
                       reader._SILENCE_MIN)
        self.assertAlmostEqual(dur(wav2), expected, places=3)
        s, e, t = segs2[0]
        self.assertEqual(text[s:e], text)
        self.assertEqual(t, 0.0)

        # the empty-output path must not trigger an extra-copy retry loop
        # (no second infer call for unpronounceable text)
        self.assertEqual(len(stub.calls), 1)

    def test_tts_engine_streams_pieces_as_they_render(self):
        from webui import reader

        class _Stub:
            sample_rate = 48000

            def __init__(self):
                self.calls = []

            def infer(self, text, voice=None):
                self.calls.append(text)
                if any("㐀" <= c <= "鿿" for c in text):
                    return []
                return [0.0] * int(48000 * 0.3)

        eng = reader.TTSEngine()
        stub = _Stub()
        eng._tts = stub
        eng.state = "ready"
        eng.default_voice = "v1"
        eng.voices = [{"id": "v1", "label": "v1"}]

        text = ("Câu đầu tiên đọc đủ rõ ràng để làm một mảnh riêng. "
                "Câu thứ hai cũng dài đủ để đứng riêng một mảnh. Hết.")
        pieces = reader.split_for_tts_seg(text)
        self.assertGreater(len(pieces), 1)   # long sentences stay separate

        import io
        import wave

        def dur(wav_bytes):
            with wave.open(io.BytesIO(wav_bytes)) as wf:
                return wf.getnframes() / wf.getframerate()

        out = list(eng.synthesize_stream(text, voice="v1"))
        self.assertEqual(len(out), len(pieces))
        n = len(pieces)
        for idx, ((part, s, e), (hdr, wav)) in enumerate(zip(pieces, out)):
            self.assertEqual(hdr["i"], idx)
            self.assertEqual((hdr["s"], hdr["e"]), (s, e))
            self.assertEqual(text[s:e], part)
            self.assertEqual(hdr["final"], idx == n - 1)
            self.assertEqual(hdr["sr"], 48000)
            self.assertTrue(wav.startswith(b"RIFF"))
            self.assertAlmostEqual(hdr["d"], 0.3, places=2)
            self.assertAlmostEqual(dur(wav), 0.3, places=2)
        self.assertFalse(out[0][0]["final"])
        self.assertTrue(out[-1][0]["final"])

        # a piece the model cannot read arrives as proportional silence
        # instead of a 0-frame WAV the browser could not decode
        out_q = list(eng.synthesize_stream("？", voice="v1"))
        self.assertEqual(len(out_q), 1)
        self.assertGreaterEqual(dur(out_q[0][1]), reader._SILENCE_MIN)

        # piece cache: the repeat above already covered it — no new renders
        calls_before = len(stub.calls)
        out2 = list(eng.synthesize_stream(text, voice="v1"))
        self.assertEqual(len(out2), n)
        self.assertEqual(len(stub.calls), calls_before)

        # the classic endpoint keeps its (wav, segs) contract
        wav2, segs = eng.synthesize(text, voice="v1")
        self.assertTrue(wav2.startswith(b"RIFF"))
        self.assertEqual(len(segs), n)
        self.assertEqual(len(stub.calls), calls_before)  # pieces shared

    # -------------------------------------------------- TTS disk chapter cache
    def _disk_engine(self, tmp=None, stamp=0.1):
        """TTSEngine over a tmp cache dir + a counting stub model."""
        from webui import reader

        if tmp is None:
            tmp = tempfile.mkdtemp(prefix="tts_chapter_cache_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)

        class _Stub:
            sample_rate = 48000

            def __init__(self):
                self.calls = []

            def infer(self, text, voice=None):
                self.calls.append((text, voice))
                return [0.05] * int(48000 * stamp)

        eng = reader.TTSEngine(cache_dir=tmp)
        stub = _Stub()
        eng._tts = stub
        eng.state = "ready"
        eng.default_voice = "v1"
        eng.voices = [{"id": "v1", "label": "v1"}]
        return eng, stub, tmp

    def _wait_job(self, eng, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = eng.cache_status()["job"]
            if st and st["state"] != "running":
                return st
            time.sleep(0.01)
        self.fail("cache job did not finish in time")

    def test_tts_disk_cache_read_through_and_inventory(self):
        from webui import reader

        text = "Xin chào thế giới. Một câu thứ hai ở đây."
        eng, stub, tmp = self._disk_engine()
        eng.note_chapter("book.epub", 1)   # what the reader page tells the engine
        wav1, segs1 = eng.synthesize(text, voice="v1")
        self.assertEqual(len(stub.calls), len(reader.split_for_tts(text)))
        self.assertTrue(wav1.startswith(b"RIFF"))
        self.assertGreater(eng.disk.stats()["bytes"], 0)

        # a fresh engine over the same dir never touches the model
        eng2, stub2, _ = self._disk_engine(tmp=tmp)
        wav2, segs2 = eng2.synthesize(text, voice="v1")
        self.assertEqual(stub2.calls, [])                 # served from disk
        self.assertEqual(len(wav2), len(wav1))
        self.assertEqual(segs2, segs1)

        # streaming playback serves the same bundle without the model
        frames = list(eng2.synthesize_stream(text, voice="v1"))
        pieces = reader.split_for_tts_seg(text)
        self.assertEqual(stub2.calls, [])
        self.assertEqual(len(frames), len(pieces))
        self.assertFalse(frames[-1][0]["final"] is False)
        for idx, (header, payload) in enumerate(frames):
            self.assertEqual(header["i"], idx)
            self.assertEqual((header["s"], header["e"]),
                             (pieces[idx][1], pieces[idx][2]))
            self.assertGreater(header["d"], 0)
            self.assertTrue(payload.startswith(b"RIFF"))

        # inventory: planning matches playback; all pieces counted cached
        doc = _FakeBookDoc({1: [text]})
        inv = eng.inspect_cache(doc, 1, "v1")
        self.assertEqual(inv["pieces"], len(reader.split_for_tts(text)))
        self.assertEqual(inv["cached"], inv["pieces"])
        self.assertEqual(inv["chars"], inv["cached_chars"])
        # unknown voice falls back to the default (same file set)
        inv2 = eng.inspect_cache(doc, 1, "khong-co-giong-nay")
        self.assertEqual(inv2["voice"], "v1")
        self.assertEqual(inv2["cached"], inv2["pieces"])
        # a different voice is a different key space
        eng.voices.append({"id": "v2", "label": "v2"})
        self.assertEqual(eng.inspect_cache(doc, 1, "v2")["cached"], 0)

        # clear wipes the store
        eng.clear_cache()
        disk = eng.disk.stats()
        self.assertEqual((disk["files"], disk["bytes"]), (0, 0))

    def test_tts_disk_cache_off_without_a_dir(self):
        from webui import reader

        eng = reader.TTSEngine()
        self.assertIsNone(eng.disk)

        class _Stub:
            sample_rate = 48000

            def infer(self, text, voice=None):
                return [0.05] * 4800

        eng._tts = _Stub()
        eng.state = "ready"
        eng.default_voice = "v1"
        eng.voices = [{"id": "v1", "label": "v1"}]
        wav, _ = eng.synthesize("Xin chào.")
        self.assertTrue(wav.startswith(b"RIFF"))

    def test_tts_disk_cache_bundles_one_pair_per_chapter(self):
        """A whole chapter costs ONE audio file + ONE index, appended as
        paragraphs are rendered — not one file per paragraph or piece."""
        from webui import reader

        def listing():
            return sorted(os.path.join(d, n)
                          for d, _s, ns in os.walk(tmp) for n in ns)

        eng, stub, tmp = self._disk_engine()
        eng.note_chapter("book.epub", 1)
        texts = ["Đoạn một đủ dài để tách thành mảnh riêng.",
                 "Đoạn hai cũng thế, dài vừa đủ cho một mảnh."]
        for t in texts:
            eng.synthesize(t, voice="v1")

        files = listing()
        self.assertEqual(len(files), 2)                   # one pair, not four
        mp3 = [f for f in files if f.endswith(".mp3")]
        self.assertEqual(len(mp3), 1)
        self.assertEqual(len([f for f in files if f.endswith(".json")]), 1)
        self.assertGreater(os.path.getsize(mp3[0]), 0)

        # a second chapter gets its own pair
        eng.note_chapter("book.epub", 2)
        eng.synthesize("Chương hai một đoạn ngắn.", voice="v1")
        self.assertEqual(len(listing()), 4)

        # both paragraphs stream back from disk on a fresh engine
        eng2, stub2, _ = self._disk_engine(tmp=tmp)
        for t in texts:
            frames = list(eng2.synthesize_stream(t, voice="v1"))
            self.assertEqual(len(frames), len(reader.split_for_tts(t)))
        self.assertEqual(stub2.calls, [])

        # a new paragraph for the same chapter is appended to the existing
        # pair (still four files) and served on the next read
        eng2.note_chapter("book.epub", 1)
        extra = "Đoạn mới thêm vào chương một."
        eng2.synthesize(extra, voice="v1")
        self.assertTrue(stub2.calls)                      # rendered once, live
        self.assertEqual(len(listing()), 4)
        eng3, stub3, _ = self._disk_engine(tmp=tmp)
        eng3.synthesize(extra, voice="v1")
        self.assertEqual(stub3.calls, [])

    def test_tts_disk_cache_skips_silence_and_heals_corruption(self):
        from webui import reader

        eng, stub, tmp = self._disk_engine()
        eng.note_chapter("book.epub", 1)

        # a lone "？" never reaches the model → silence, nothing on disk
        wav, _ = eng.synthesize("？", voice="v1")
        self.assertEqual(stub.calls, [])
        self.assertEqual(eng.disk.stats()["files"], 0)

        # model comes back empty (unreadable paragraph) → silence too
        class _EmptyStub:
            sample_rate = 48000

            def __init__(self):
                self.calls = []

            def infer(self, text, voice=None):
                self.calls.append(text)
                return []

        eng._tts = _EmptyStub()
        wav2, _ = eng.synthesize("全班掌声热烈。", voice="v1")
        self.assertTrue(wav2.startswith(b"RIFF"))
        self.assertEqual(eng.disk.stats()["files"], 0)

        # a torn audio file reads as a miss and is replaced by the fresh render
        text = "Chỉ một câu ngắn."
        eng3, stub3, _ = self._disk_engine(tmp=tmp)
        eng3.note_chapter("book.epub", 1)
        eng3.synthesize(text, voice="v1")
        path = eng3.disk._base("v1", "book.epub", 1) + ".mp3"
        with open(path, "wb") as f:
            f.write(b"RIFFgarbage")
        eng4, stub4, _ = self._disk_engine(tmp=tmp)
        eng4.note_chapter("book.epub", 1)
        wav4, _ = eng4.synthesize(text, voice="v1")
        self.assertEqual(len(stub4.calls), 1)             # corrupt → re-rendered
        self.assertTrue(wav4.startswith(b"RIFF"))
        self.assertIsNotNone(eng4.disk.get_item(
            "v1", reader._collapse(text), len(reader.split_for_tts(text))))

    def test_tts_disk_cache_stores_compressed_audio(self):
        import soundfile as sf

        from webui import reader

        eng, _stub, _tmp = self._disk_engine(stamp=0.5)
        eng.note_chapter("book.epub", 1)
        text = "Một câu đủ dài để kiểm tra nén."
        eng.synthesize(text, voice="v1")
        path = eng.disk._base("v1", "book.epub", 1) + ".mp3"
        self.assertTrue(os.path.isfile(path))
        # 0.5 s of float32 samples is ~96 KB in RAM; the on-disk copy is a
        # small fraction of that (MP3, not PCM/WAV).
        raw = 48000 * 0.5 * 4 * len(reader.split_for_tts(text))
        self.assertLess(os.path.getsize(path), raw * 0.5)
        samples, sr = sf.read(path, dtype="float32")
        self.assertEqual(sr, 48000)
        self.assertAlmostEqual(len(samples) / 48000, 0.5, delta=0.1)
        got = eng.disk.get_item("v1", reader._collapse(text),
                                len(reader.split_for_tts(text)))
        self.assertIsNotNone(got)                          # decodes for playback
        self.assertTrue(all(len(chunk) > 0 for chunk in got))

    def test_tts_disk_cache_prunes_legacy_wav_pieces(self):
        eng, _stub, tmp = self._disk_engine()
        legacy = os.path.join(tmp, "ab", "deadbeef.wav")
        os.makedirs(os.path.dirname(legacy), exist_ok=True)
        with open(legacy, "wb") as f:
            f.write(b"RIFF-old-format")
        st = eng.disk.stats()
        self.assertEqual((st["files"], st["bytes"]), (0, 0))
        self.assertFalse(os.path.exists(legacy))

    def test_tts_cache_job_renders_chapters_and_skips_cached(self):
        from webui import reader

        eng, stub, _tmp = self._disk_engine()
        doc = _FakeBookDoc({
            1: ["Câu đầu tiên đủ dài để tách thành mảnh. Câu hai cũng đủ dài vậy."],
            2: ["Chương hai chỉ có một đoạn ngắn."],
        })
        eng.start_cache(doc, "v1", 1, 2, book="book.epub")
        st = self._wait_job(eng)
        self.assertEqual(st["state"], "done", st)
        self.assertEqual(st["book"], "book.epub")
        self.assertEqual(st["ch_done"], 2)
        self.assertEqual(st["chars_done"], st["chars_total"])
        plan_len = sum(len(reader.TTSEngine.piece_plan(it["text"]))
                       for n in (1, 2) for it in doc.chapter(n)["items"])
        self.assertEqual(len(stub.calls), plan_len)
        for n in (1, 2):
            inv = eng.inspect_cache(doc, n, "v1")
            self.assertGreater(inv["pieces"], 0)
            self.assertEqual(inv["cached"], inv["pieces"])
        self.assertGreater(eng.disk.stats()["bytes"], 0)

        # re-run: everything comes off disk, the model is not called again
        calls = len(stub.calls)
        eng.start_cache(doc, "v1", 1, 2)
        st2 = self._wait_job(eng)
        self.assertEqual(st2["state"], "done", st2)
        self.assertEqual(len(stub.calls), calls)

    def test_tts_cache_job_stop_halts_between_pieces(self):
        import threading

        from webui import reader

        gate = threading.Event()
        tmp = tempfile.mkdtemp(prefix="tts_cache_stop_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)

        class _GatedStub:
            sample_rate = 48000

            def __init__(self):
                self.calls = []

            def infer(self, text, voice=None):
                self.calls.append(text)
                gate.wait(5)          # hold the first piece
                return [0.05] * 4800

        eng = reader.TTSEngine(cache_dir=tmp)
        stub = _GatedStub()
        eng._tts = stub
        eng.state = "ready"
        eng.default_voice = "v1"
        eng.voices = [{"id": "v1", "label": "v1"}]

        doc = _FakeBookDoc({1: ["Đoạn một đủ dài để thành một mảnh riêng.",
                                "Đoạn hai cũng thế, dài vừa đủ cho một mảnh."]})
        eng.start_cache(doc, "v1", 1, 1)
        deadline = time.time() + 5
        while not stub.calls and time.time() < deadline:
            time.sleep(0.01)
        self.assertTrue(stub.calls, "job never reached the model")
        eng.stop_cache()
        gate.set()
        st = self._wait_job(eng)
        self.assertEqual(st["state"], "stopped")
        self.assertEqual(len(stub.calls), 1)   # held piece done, then stopped

    def test_read_page_bootstraps_tts_disk_cache(self):
        path = self._reader_epub()
        with self._warm_noop():
            r = self.client.get("/read?path=" + path)
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertIn("cacheStartUrl", body)
        self.assertIn("cacheInspectUrl", body)
        self.assertIn("rd-cache-start", body)
        self.assertIn("rd-cache-bar", body)
        self.assertIn("rd-cache-clear", body)
        # the cache is deliberately uncapped: no budget inputs / endpoint
        self.assertNotIn("cacheLimitUrl", body)
        self.assertNotIn("rd-cache-max-mb", body)
        self.assertNotIn("rd-cache-max-files", body)

    def test_tts_cache_endpoints(self):
        from webui import reader

        path = self._reader_epub()
        with self._warm_noop():
            r = self.client.get("/api/tts/cache/inspect?path=%s&ch=1&voice=v1" % path)
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["ch"], 1)
        self.assertEqual(data["voice"], "v1")   # engine not loaded → kept as-is
        self.assertGreaterEqual(data["pieces"], 1)
        self.assertEqual(data["cached"], 0)
        self.assertIn("disk", data)
        self.assertIn("job", data)

        with self._warm_noop():
            r = self.client.get("/api/tts/cache/inspect?path=%s&ch=99" % path)
        self.assertEqual(r.status_code, 400)

        # start: model not ready → 503 with state, no job created
        with self._warm_noop():
            r = self.client.post("/api/tts/cache/start",
                                 json={"path": path, "ch": 1, "chapters": 1})
        self.assertEqual(r.status_code, 503)

        # bad payloads
        r = self.client.post("/api/tts/cache/start", json={"path": path, "ch": "x"})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/tts/cache/start",
                             json={"path": path, "ch": 1, "voice": 42})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/tts/cache/start", json={"path": "nope.epub"})
        self.assertEqual(r.status_code, 400)

        job = {"state": "running", "ch_now": 1, "chapters": 2,
               "pieces_done": 3, "pieces_total": 9, "chars_done": 30,
               "chars_total": 90, "eta": 5, "book": os.path.basename(path)}
        with self._warm_noop(), \
                mock.patch.object(reader.ENGINE, "start_cache",
                                  return_value=job) as start:
            r = self.client.post("/api/tts/cache/start",
                                 json={"path": path, "ch": 1,
                                       "chapters": 999, "voice": "v1"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["job"]["state"], "running")
        # chapters clamped to the end of the book; voice + book forwarded
        self.assertEqual(start.call_args.args[1:4], ("v1", 1, 1))
        self.assertEqual(start.call_args.kwargs["book"], os.path.basename(path))

        # busy → 409
        with self._warm_noop(), \
                mock.patch.object(reader.ENGINE, "start_cache",
                                  side_effect=ValueError("busy")):
            r = self.client.post("/api/tts/cache/start", json={"path": path, "ch": 1})
        self.assertEqual(r.status_code, 409)

        # status + stop + clear (mocked: never touch the real cache dir)
        with mock.patch.object(reader.ENGINE, "cache_status",
                               return_value={"job": job,
                                             "disk": {"files": 4, "bytes": 2048}}):
            r = self.client.get("/api/tts/cache/status")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["disk"]["files"], 4)

        stopped = dict(job, state="stopped")
        with mock.patch.object(reader.ENGINE, "stop_cache",
                               return_value={"job": stopped,
                                             "disk": {"files": 4, "bytes": 2048}}):
            r = self.client.post("/api/tts/cache/stop")
        self.assertEqual(r.get_json()["job"]["state"], "stopped")

        with mock.patch.object(reader.ENGINE, "clear_cache",
                               return_value={"job": None,
                                             "disk": {"files": 0, "bytes": 0}}):
            r = self.client.post("/api/tts/cache/clear")
        self.assertEqual(r.get_json()["disk"]["files"], 0)
        with mock.patch.object(reader.ENGINE, "clear_cache",
                               side_effect=ValueError("busy")):
            r = self.client.post("/api/tts/cache/clear")
        self.assertEqual(r.status_code, 409)

        # the old budget endpoint is gone — the cache is uncapped on purpose
        r = self.client.post("/api/tts/cache/limit",
                             json={"max_bytes": 1024})
        self.assertEqual(r.status_code, 404)

    def test_tts_stream_write_through_follows_open_chapter(self):
        """Loading a chapter sets the write-through target: streaming its
        paragraphs bundles one audio+index pair per chapter, and a second
        listen replays them with zero model calls."""
        import struct

        from webui import reader

        tmp = tempfile.mkdtemp(prefix="tts_writethrough_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)

        class _Stub:
            sample_rate = 48000

            def __init__(self):
                self.calls = []

            def infer(self, text, voice=None):
                self.calls.append(text)
                return [0.05] * 4800

        def frame_count(data):
            n, off = 0, 0
            while off < len(data):
                hl = struct.unpack(">I", data[off:off + 4])[0]
                off += 4 + hl
                wl = struct.unpack(">I", data[off:off + 4])[0]
                off += 4 + wl
                n += 1
            return n

        path = self._reader_epub(name="_test_writethrough.epub")
        stub = _Stub()
        eng = reader.ENGINE
        self.addCleanup(setattr, eng, "_book_ctx", None)
        with self._warm_noop(), \
                mock.patch.object(eng, "disk", reader.TTSChapterCache(tmp)), \
                mock.patch.object(eng, "_tts", stub), \
                mock.patch.object(eng, "state", "ready"), \
                mock.patch.object(eng, "default_voice", "v1"), \
                mock.patch.object(eng, "voices", [{"id": "v1", "label": "v1"}]):
            r = self.client.get("/api/reader/chapter?path=%s&n=1" % path)
            self.assertEqual(r.status_code, 200)
            text = r.get_json()["items"][0]["text"]

            r1 = self.client.post("/api/tts/stream",
                                  json={"text": text, "voice": "v1"})
            self.assertEqual(r1.status_code, 200)
            self.assertGreater(frame_count(r1.get_data()), 0)
            calls = len(stub.calls)
            self.assertGreater(calls, 0)

            files = [os.path.join(d, n)
                     for d, _s, ns in os.walk(tmp) for n in ns]
            self.assertEqual(len(files), 2)          # one pair for chapter 1

            # replay: same frames from the bundle, model untouched
            r2 = self.client.post("/api/tts/stream",
                                  json={"text": text, "voice": "v1"})
            self.assertEqual(len(stub.calls), calls)
            self.assertEqual(frame_count(r2.get_data()),
                             frame_count(r1.get_data()))

    def test_set_lang_sets_cookie(self):
        r = self.client.get("/set-lang/en?next=/books")
        self.assertEqual(r.status_code, 302)
        self.assertIn("lang=en", r.headers.get("Set-Cookie", ""))
        # Unsupported language is rejected.
        r2 = self.client.get("/set-lang/xx")
        self.assertEqual(r2.status_code, 400)

    def test_pages_localized_by_locale(self):
        r = self.client.get("/books")
        body = r.get_data(as_text=True)
        self.assertIn("Thư viện</a>", body)
        self.assertIn("Thư viện EPUB", body)

        self.client.set_cookie("lang", "en")
        r2 = self.client.get("/books")
        body2 = r2.get_data(as_text=True)
        self.assertIn("Library</a>", body2)
        self.assertIn("EPUB Library", body2)
        self.assertNotIn("Thư viện EPUB", body2)

    def test_theme_toggle_present(self):
        r = self.client.get("/")
        body = r.get_data(as_text=True)
        self.assertIn('id="theme-toggle"', body)
        self.assertIn("toggleTheme", body)
        self.assertIn('data-theme="dark"', body)

    def test_global_theme_menu_lists_every_theme(self):
        # The topbar menu is the single place to pick a theme; the reader's
        # page theme follows it (see test_theme_is_one_source_of_truth).
        r = self.client.get("/")
        body = r.get_data(as_text=True)
        self.assertIn('id="theme-pop"', body)
        self.assertIn("toggleThemeMenu", body)
        for name in ("light", "sepia", "dark", "black", "system", "custom"):
            self.assertIn(f'data-gtheme="{name}"', body)
        # custom pair lives with the global theme, not per book
        self.assertIn('id="theme-custom-row"', body)
        self.assertIn('id="theme-bg"', body)
        self.assertIn('id="theme-fg"', body)
        # labels resolve for both locales
        self.assertIn("Giấy cổ", body)
        self.client.set_cookie("lang", "en")
        body_en = self.client.get("/").get_data(as_text=True)
        self.assertIn("Sepia", body_en)

    def test_theme_is_one_source_of_truth(self):
        def read(*parts):
            with open(os.path.join(PROJECT_ROOT, "webui", *parts), encoding="utf-8") as fh:
                return fh.read()

        style = read("static", "style.css")
        # every global theme remaps the app tokens (sepia/black/custom added);
        # dark is the :root default, the rest are explicit overrides
        self.assertRegex(style, r":root \{\s*\n\s*color-scheme: dark;")
        for name in ("light", "sepia", "black", "custom"):
            self.assertIn(f'html[data-theme="{name}"]', style)
        self.assertIn("color-scheme", style)
        self.assertIn("--custom-bg", style)

        # the reading stage paints with those same tokens
        reader_css = read("static", "reader.css")
        stage = reader_css.split(".rd-stage {", 1)[1].split("}", 1)[0]
        self.assertIn("var(--bg", stage)
        for hexval in ("#ffffff", "#f7ecd7", "#14171c", "#000000"):
            self.assertNotIn(hexval, reader_css)

        app_js = read("static", "app.js")
        for needle in ("function setTheme(", "function themePref(",
                       "function applyTheme(", "themechange"):
            self.assertIn(needle, app_js)

        reader_js = read("static", "reader.js")
        # the reader subscribes to the global theme instead of keeping its own
        self.assertIn("'themechange'", reader_js)
        self.assertIn("globalThemePref()", reader_js)
        self.assertNotIn("settings.theme", reader_js)

    def test_media_session_background_audio(self):
        # Screen-off playback: both sound sources leave through hidden <audio>
        # elements fed by MediaStream destinations (a playing media element is
        # what Android keeps alive in the background — a bare AudioContext is
        # suspended and the prefetch queue dies with it), and a Media Session
        # drives the lock-screen / headset controls.
        def read(*parts):
            with open(os.path.join(PROJECT_ROOT, "webui", *parts), encoding="utf-8") as fh:
                return fh.read()

        js = read("static", "reader.js")
        for needle in ("createMediaStreamDestination", "srcObject", "ttsOut(true)",
                       "bgmOut(true)", "'mediaSession'", "setActionHandler",
                       "playbackState", "setPositionState", "MediaMetadata",
                       "queueDepth"):
            self.assertIn(needle, js)
        # refused play() falls back to the raw destination: never silent
        self.assertIn("player.gain.connect(player.ctx.destination)", js)
        self.assertIn("bgm.gain.connect(bgm.ctx.destination)", js)
        # metadata is assigned only when it CHANGES and the cover is probed
        # once — otherwise Chrome refetches the artwork per paragraph and a
        # book without a cover fills the log with 404s while read-aloud runs
        self.assertIn("media.metaKey", js)
        self.assertIn("media.art = false", js)

        html = read("templates", "reader.html")
        self.assertIn('id="rd-tts-bg-hint"', html)

        from webui import i18n as wui_i18n

        for lang in ("vi", "en"):
            self.assertIn("read.tts_bg_hint", wui_i18n.TRANSLATIONS[lang])

    def test_read_aloud_speed_keeps_the_voice(self):
        # Reported: x1.00 read with the right voice, any other speed changed
        # it. wsola() is the ONLY thing that runs at rate != 1 (stretched()
        # hands x1.00 the untouched buffer), so drive the real function —
        # extracted from reader.js and executed under node — through a voiced
        # 120 Hz signal and pin what "keeps the voice" means:
        #   - exact duration (len / rate),
        #   - pitch preserved: a resampler moves f0 by 12*log2(rate) semitones
        #     (+25% in Hz at x1.25, +50% at x1.5) — the chipmunk bug,
        #   - envelope tracks the time map (no dropped or echoed chunks).
        # Plus the wiring needles: stretch before scheduling, and the
        # test-voice button must not go through <audio>.playbackRate, which
        # shifts the pitch on every browser without preservesPitch.
        import re
        import shutil
        import subprocess
        import tempfile
        import wave

        import numpy as np

        with open(os.path.join(PROJECT_ROOT, "webui", "static", "reader.js"),
                  encoding="utf-8") as fh:
            js = fh.read()

        self.assertIn("stretched(p.buf, player.rate)", js)   # stretch -> schedule
        self.assertIn("Math.abs(rate - 1) < 0.005", js)      # x1.00: zero DSP
        self.assertNotIn("audio.playbackRate = ttsCfg.rate", js)

        with open(os.path.join(PROJECT_ROOT, "webui", "templates", "reader.html"),
                  encoding="utf-8") as fh:
            html = fh.read()
        # the slider must keep reaching x2 (requested range 0.75..2)
        self.assertIn('id="rd-tts-rate" type="range" min="0.75" max="2"', html)

        found = re.search(r"function wsola\(x, rate, sr\) \{.*?\n  \}", js, re.S)
        self.assertIsNotNone(found, "wsola() must stay in reader.js")

        node = shutil.which("node")
        if not node:
            self.skipTest("node not on PATH")

        harness = found.group(0) + r"""
const fs = require('fs'), path = require('path');
const sr = 48000, n = sr * 2, x = new Float32Array(n);
for (let i = 0; i < n; i++) {
  const t = i / sr;
  const saw = 2 * ((t * 120) % 1) - 1;                  // 120 Hz voiced pulse
  const env = 0.35 + 0.65 * Math.abs(Math.sin(2 * Math.PI * t));  // slow swell
  x[i] = 0.8 * env * saw;
}
function writeWav(p, f32) {
  const buf = Buffer.alloc(44 + f32.length * 2);
  buf.write('RIFF', 0); buf.writeUInt32LE(36 + f32.length * 2, 4); buf.write('WAVE', 8);
  buf.write('fmt ', 12); buf.writeUInt32LE(16, 16); buf.writeUInt16LE(1, 20);
  buf.writeUInt16LE(1, 22); buf.writeUInt32LE(sr, 24); buf.writeUInt32LE(sr * 2, 28);
  buf.writeUInt16LE(2, 32); buf.writeUInt16LE(16, 34);
  buf.write('data', 36); buf.writeUInt32LE(f32.length * 2, 40);
  for (let i = 0; i < f32.length; i++) {
    const v = Math.max(-32768, Math.min(32767, Math.round(f32[i] * 32767)));
    buf.writeInt16LE(v, 44 + i * 2);
  }
  fs.writeFileSync(p, buf);
}
const out = process.argv[2];
writeWav(path.join(out, 'in.wav'), x);
[0.75, 1.25, 1.5, 1.75, 2.0].forEach(function (r) {
  writeWav(path.join(out, 'r' + r.toFixed(2) + '.wav'), wsola(x, r, sr));
});
"""

        def read16(path):
            with wave.open(path) as w:
                return np.frombuffer(w.readframes(w.getnframes()),
                                     dtype="<i2").astype(np.float64) / 32768.0

        def f0_of(a):
            # autocorrelation peak over the middle half of the signal
            seg = a[len(a) // 4: 3 * len(a) // 4]
            seg = seg - seg.mean()
            nfft = 1 << (2 * len(seg)).bit_length()
            spec = np.fft.rfft(seg, nfft)
            ac = np.fft.irfft(spec * np.conj(spec))[:len(seg)]
            ac = ac / (ac[0] + 1e-12)
            lo, hi = int(48000 / 300), int(48000 / 70)   # 70..300 Hz search
            return 48000 / (lo + int(np.argmax(ac[lo:hi])))

        def env_db(a):
            h = 480                                     # 10 ms frames
            m = (len(a) // h) * h
            return 20 * np.log10(np.sqrt((a[:m].reshape(-1, h) ** 2).mean(1)) + 1e-12)

        with tempfile.TemporaryDirectory() as td:
            hp = os.path.join(td, "h.js")
            with open(hp, "w", encoding="utf-8") as fh:
                fh.write(harness)
            subprocess.run([node, hp, td], check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            xin = read16(os.path.join(td, "in.wav"))
            f0_in = f0_of(xin)
            e_in = env_db(xin)
            for rate in (0.75, 1.25, 1.5, 1.75, 2.0):
                y = read16(os.path.join(td, f"r{rate:.2f}.wav"))
                self.assertEqual(len(y), round(len(xin) / rate),
                                 f"x{rate}: stretched length must be len/rate")
                err = abs(f0_of(y) / f0_in - 1)
                self.assertLess(
                    err, 0.02,
                    f"x{rate}: pitch moved {err * 100:.1f}% — speeding up "
                    f"must not change the voice (a resampler would read "
                    f"{rate * 100:.0f}% of the original Hz)")
                e = env_db(y)
                idx = np.arange(len(e)) * rate
                keep = idx < len(e_in) - 1
                dev = e[keep] - np.interp(idx[keep], np.arange(len(e_in)), e_in)
                self.assertLess(
                    float(dev.std()), 2.5,
                    f"x{rate}: envelope lost track of the time map "
                    f"({dev.std():.1f} dB) — audio dropped or echoed")

    def test_public_clients_are_403_and_tls_probes_stay_quiet(self):
        # The log showed an AWS scanner (18.116.101.220) reaching the port: a
        # TLS handshake answered with 400 "Bad request version" plus a line of
        # raw bytes. The UI holds provider keys (config.yaml), so (a) non-LAN
        # clients never get an answer at all, and (b) non-HTTP request lines
        # never reach the console log.
        r = self.client.get("/health",
                            environ_base={"REMOTE_ADDR": "18.116.101.220"})
        self.assertEqual(r.status_code, 403)
        r = self.client.get("/health", environ_base={"REMOTE_ADDR": "192.168.1.2"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get("/health").status_code, 200)  # loopback

        # opt-out for a tunnel/VPN of the user's own
        os.environ["WEBUI_PUBLIC"] = "1"
        try:
            r = self.client.get("/health",
                                environ_base={"REMOTE_ADDR": "18.116.101.220"})
            self.assertEqual(r.status_code, 200)
        finally:
            os.environ.pop("WEBUI_PUBLIC", None)

        from webui.app import _ProbeQuietHandler

        h = object.__new__(_ProbeQuietHandler)
        h.client_address = ("18.116.101.220", 44321)
        h.address_string = lambda: "18.116.101.220"
        # a TLS ClientHello is not an HTTP request line -> logged nowhere
        h.requestline = "\x16\x03\x01\x00\xee\x01\x00\x00\xea\x03\x03ab"
        with self.assertNoLogs("werkzeug", level="INFO"):
            h.log("info", '"%s" %s %s', h.requestline, 400, "-")
            h.log("error", "code %s, message %s", 400,
                  "Bad request version ('À\\x14À')")
        # real requests keep their access log
        h.requestline = "GET /health HTTP/1.1"
        with self.assertLogs("werkzeug", level="INFO") as lm:
            h.log("info", '"%s" %s %s', "GET /health HTTP/1.1", 200, "15")
        self.assertIn("GET /health", lm.output[0])

    def test_read_aloud_keeps_idle_hidden_bars_hidden(self):
        # After 10s idle the bars retract; a read-aloud auto-advance into the
        # next chapter is NOT a deliberate act and must not reveal them again
        # (loadChapter only calls setChromeAway(false) for non-quiet loads).
        with open(os.path.join(PROJECT_ROOT, "webui", "static", "reader.js"),
                  encoding="utf-8") as fh:
            js = fh.read()
        self.assertIn("if (!opts.quiet) setChromeAway(false)", js)
        self.assertIn("loadChapter(next, { item: 0, push: true, quiet: true })", js)

    def test_job_view_page_renders(self):
        # The job view HTML must expose every element job_view.js wires up, and
        # keep the no-inline-handler / no-emoji contract (component of the
        # WebUI refactor: monitor panes moved into templates, JS reads DOM ids).
        job = self.registry.create(
            {
                "engine": "openai",
                "input": self.src,
                "output": self.out,
                "from_chapter": 1,
                "to_chapter": None,
            }
        )
        job.status = "interrupted"
        self.registry.save(job)

        r = self.client.get(f"/jobs/{job.id}")
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)

        # window.APP config + monitor wiring ids job_view.js depends on.
        for needle in (
            "window.APP.jobData",
            "streamUrl",
            'id="btnStruct"',
            'id="btnDiff"',
            'id="btnSide"',
            'id="structView"',
            'id="diffView"',
            'id="diffWrap"',
            'id="diffSource"',
            'id="diffTrans"',
            'id="rulesChapter"',
            'id="rulesFilter"',
            'id="rulesAll"',
            'id="rulesMeta"',
            'id="log"',
            'id="progress"',
            'id="bar"',
        ):
            self.assertIn(needle, body)

        # No model paths baked into JS string literals; no emoji labels.
        self.assertNotIn("onclick=\"preview('", body)
        for emoji in ("✅", "⏳", "📖", "❓", "▶"):
            self.assertNotIn(emoji, body)

        # An interrupted job offers resume, not stop.
        self.assertIn(f'action="/jobs/{job.id}/resume"', body)
        self.assertNotIn(f'action="/jobs/{job.id}/stop"', body)

    def test_job_rules_endpoint_scopes_to_a_chapter(self):
        """?chapter=N returns only the characters that chapter's analysis
        named (the full dictionary + every analysis is ~1MB of JSON)."""
        job = self.registry.create(
            {"engine": "openai", "input": self.src, "output": self.out}
        )
        rules_path = os.path.splitext(self.out)[0] + ".rules.json"
        with open(rules_path, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "characters": {
                        "Bạch Thanh Hạ": {
                            "names": ["Bạch Thanh Hạ"],
                            "calls": {"Lục Viễn Thu": "tôi - anh"},
                            "pronouns": {},
                            "notes": "",
                        },
                        "Lục Viễn Thu": {
                            "names": ["Lục Viễn Thu"],
                            "calls": {"Bạch Thanh Hạ": "em - tôi"},
                            "pronouns": {},
                            "notes": "",
                        },
                        "Người lạ": {
                            "names": ["Người lạ"],
                            "calls": {},
                            "pronouns": {},
                            "notes": "",
                        },
                    },
                    "narration_defaults": {"Bạch Thanh Hạ": "hắn"},
                    "fallback": "tôi - người khác",
                    "_chapter_analyses": {
                        "5": (
                            "1) Characters: Bạch Thanh Hạ, Lục Viễn Thu\n"
                            "Bạch Thanh Hạ -> Lục Viễn Thu: tôi - anh"
                        ),
                    },
                },
                fh,
                ensure_ascii=False,
            )

        # Full payload (no chapter) — unchanged behaviour.
        data = self.client.get(f"/api/jobs/{job.id}/rules").get_json()
        self.assertEqual(
            set(data["characters"]),
            {"Bạch Thanh Hạ", "Lục Viễn Thu", "Người lạ"},
        )
        self.assertIsNone(data["meta"]["chapter"])
        self.assertEqual(data["meta"]["total_characters"], 3)
        self.assertIn("5", data["analyses"])

        # Scoped payload: only chapter 5's characters + only its analysis.
        data = self.client.get(f"/api/jobs/{job.id}/rules?chapter=5").get_json()
        self.assertEqual(set(data["characters"]), {"Bạch Thanh Hạ", "Lục Viễn Thu"})
        self.assertNotIn("Người lạ", data["characters"])
        self.assertEqual(data["meta"]["chapter"], 5)
        self.assertEqual(data["meta"]["returned_characters"], 2)
        self.assertTrue(data["meta"]["has_analysis"])
        self.assertEqual(set(data["analyses"]), {"5"})
        self.assertEqual(data["fallback"], "tôi - người khác")

        # A chapter never analysed → empty subset, but still a well-formed 200.
        data = self.client.get(f"/api/jobs/{job.id}/rules?chapter=999").get_json()
        self.assertEqual(data["characters"], {})
        self.assertEqual(data["analyses"], {})
        self.assertFalse(data["meta"]["has_analysis"])
        self.assertEqual(data["meta"]["total_characters"], 3)

        # Bad chapter parameter is rejected, not silently ignored.
        r = self.client.get(f"/api/jobs/{job.id}/rules?chapter=abc")
        self.assertEqual(r.status_code, 400)

        # Unknown job stays a 404.
        self.assertEqual(self.client.get("/api/jobs/nope/rules").status_code, 404)

    def test_job_view_page_localized(self):
        job = self.registry.create(
            {"engine": "openai", "input": self.src, "output": self.out}
        )
        job.status = "interrupted"
        self.registry.save(job)

        r = self.client.get(f"/jobs/{job.id}")
        body = r.get_data(as_text=True)
        self.assertIn("Thông số", body)
        self.assertIn("Tiến trình", body)
        self.assertIn("Giám sát", body)
        self.assertIn("Bị gián đoạn", body)
        self.assertIn("Tiếp tục (resume từ checkpoint)", body)

        self.client.set_cookie("lang", "en")
        r2 = self.client.get(f"/jobs/{job.id}")
        body2 = r2.get_data(as_text=True)
        self.assertIn("Parameters", body2)
        self.assertIn("Progress", body2)
        self.assertIn("Monitor", body2)
        self.assertIn("Interrupted", body2)
        self.assertIn("Continue (resume from checkpoint)", body2)
        self.assertNotIn("Tiến trình", body2)

    def test_additional_pages_localized_by_locale(self):
        r = self.client.get("/jobs/new")
        body = r.get_data(as_text=True)
        self.assertIn("Tạo job dịch mới", body)
        self.assertIn("Bắt đầu dịch", body)
        self.assertIn("Tắt resume", body)
        self.assertIn("(mặc định: input.translated.epub)", body)

        r = self.client.get("/config")
        body = r.get_data(as_text=True)
        self.assertIn("Cấu hình (config.yaml)", body)
        self.assertIn("Lưu cấu hình", body)

        r = self.client.get("/illustrations")
        body = r.get_data(as_text=True)
        self.assertIn("Ảnh minh họa", body)

        self.client.set_cookie("lang", "en")
        r = self.client.get("/jobs/new")
        body = r.get_data(as_text=True)
        self.assertIn("Create a new translation job", body)
        self.assertIn("Start translation", body)
        self.assertIn("Disable resume", body)

        r = self.client.get("/config")
        body = r.get_data(as_text=True)
        self.assertIn("Configuration (config.yaml)", body)
        self.assertIn("Save configuration", body)

        r = self.client.get("/illustrations")
        body = r.get_data(as_text=True)
        self.assertIn("Illustrations", body)
        self.assertNotIn("Ảnh minh họa", body)
        self.assertNotIn("Cấu hình", body)

    def test_job_status_localized_on_dashboard(self):
        # Status tokens are rendered localized (vi default) on the dashboard.
        job = self.registry.create(
            {"engine": "openai", "input": self.src, "output": self.out}
        )
        job.status = "error"
        self.registry.save(job)

        r = self.client.get("/")
        body = r.get_data(as_text=True)
        self.assertIn('badge error', body)
        self.assertIn("Lỗi</span>", body)

        self.client.set_cookie("lang", "en")
        r2 = self.client.get("/")
        body2 = r2.get_data(as_text=True)
        self.assertIn("Error</span>", body2)

    def test_create_and_run_job(self):
        r = self.client.post(
            "/jobs/new",
            data={
                "engine": "openai",
                "input": self.src,
                "output": self.out,
                "from_chapter": "1",
                "to_chapter": "1",
                "from_lang": "EN",
                "to_lang": "VI",
            },
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 302)
        job_id = r.headers["Location"].rstrip("/").split("/")[-1]

        # Wait for the background runner to finish.
        job = None
        for _ in range(100):
            job = self.registry.get(job_id)
            if job and job.status in ("done", "error", "stopped"):
                break
            time.sleep(0.1)

        self.assertIsNotNone(job)
        self.assertEqual(job.status, "done", job.error)
        self.assertTrue(os.path.exists(self.out))

        # Output contains the stubbed translation.
        out_book = epub.read_epub(self.out)
        found = False
        for item in out_book.get_items():
            content = getattr(item, "content", b"")
            text = content.decode("utf-8", "ignore") if isinstance(content, bytes) else content
            if "Xin chào" in text:
                found = True
        self.assertTrue(found)

        # Job view page renders.
        rv = self.client.get(f"/jobs/{job_id}")
        self.assertEqual(rv.status_code, 200)

    def test_stop_aborts_job_early(self):
        # An EPUB with several paragraphs -> several chunks. A slow engine gives
        # the test time to click Stop between chunks; the job must abort early
        # (not translate every chunk) instead of running to completion.
        import time as _time

        book = epub.EpubBook()
        book.set_identifier("webui-stop")
        book.set_title("Stop Test")
        book.set_language("en")
        paras = "".join(
            f"<p>Hello world sample text number {i}</p>" for i in range(5)
        )
        ch = epub.EpubHtml(title="Ch1", file_name="ch1.xhtml", lang="en")
        ch.content = ("<html><body>" + paras + "</body></html>").encode("utf-8")
        book.add_item(ch)
        book.spine = ["nav", "ch1.xhtml"]
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        epub_path = os.path.join(self.tmp, "stop.epub")
        out_path = os.path.join(self.tmp, "stop.out.epub")
        epub.write_epub(epub_path, book, {})

        captured = {}

        class SlowEngine(TranslationEngine):
            def supports_batch(self):
                return False

            def __init__(self):
                super().__init__(from_lang="EN", to_lang="VI")
                self.calls = 0

            def translate(self, text):
                self.calls += 1
                _time.sleep(0.6)
                return text.replace("Hello", "Xin chào")

        def make_engine(*a, **k):
            eng = SlowEngine()
            captured["eng"] = eng
            return eng

        import translator.job as job_mod

        self._orig_build = job_mod.build_engine
        job_mod.build_engine = make_engine

        try:
            r = self.client.post(
                "/jobs/new",
                data={
                    "engine": "openai",
                    "input": epub_path,
                    "output": out_path,
                    "from_chapter": "1",
                    "to_chapter": "1",
                    "from_lang": "EN",
                    "to_lang": "VI",
                },
                follow_redirects=False,
            )
            self.assertEqual(r.status_code, 302)
            job_id = r.headers["Location"].rstrip("/").split("/")[-1]

            # Click Stop almost immediately, while the first chunk is translating.
            self.client.post(f"/jobs/{job_id}/stop")

            job = None
            for _ in range(100):
                job = self.registry.get(job_id)
                if job and job.status in ("done", "error", "stopped"):
                    break
                _time.sleep(0.1)

            self.assertIsNotNone(job)
            self.assertEqual(job.status, "stopped")
            # Stop fired before the later chunks ran (<= 1 chunk translated).
            self.assertLessEqual(captured["eng"].calls, 1)
        finally:
            job_mod.build_engine = self._orig_build

    def test_job_monitoring_events(self):
        # A finished job must have emitted chunk_progress events and recorded
        # live monitoring data (API timing + current chunk diff) into progress.
        r = self.client.post(
            "/jobs/new",
            data={
                "engine": "openai",
                "input": self.src,
                "output": self.out,
                "from_chapter": "1",
                "to_chapter": "1",
                "from_lang": "EN",
                "to_lang": "VI",
            },
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 302)
        job_id = r.headers["Location"].rstrip("/").split("/")[-1]

        job = None
        for _ in range(100):
            job = self.registry.get(job_id)
            if job and job.status in ("done", "error", "stopped"):
                break
            time.sleep(0.1)
        self.assertEqual(job.status, "done", job.error)

        events = [e for e in job.events if e[0] == "chunk_progress"]
        self.assertTrue(events, "expected at least one chunk_progress event")
        self.assertEqual(events[-1][1]["index"], events[-1][1]["total"])

        api = job.progress.get("api")
        self.assertIsNotNone(api)
        self.assertGreater(api["calls"], 0)
        self.assertGreaterEqual(api["total_ms"], 0.0)

        cc = job.progress.get("current_chunk")
        self.assertIsNotNone(cc)
        self.assertIn("Hello", cc["source"])
        self.assertIn("Xin chào", cc["translated"])

        # word-level diff must be attached to the current chunk
        diff = cc.get("diff")
        self.assertIsNotNone(diff)
        self.assertIn("lines", diff)
        self.assertIn("added_words", diff)
        self.assertIn("removed_words", diff)
        self.assertGreater(diff["added_words"], 0)

        # structure + koboSpan coverage must be attached too
        struct = cc.get("structure")
        self.assertIsNotNone(struct)
        self.assertIn("same", struct)
        self.assertIn("tag_diff", struct)
        self.assertIn("coverage", struct)
        self.assertIn("missing", struct["coverage"])
        self.assertIn("total_source", struct["coverage"])
        self.assertGreater(struct["source_tags"], 0)
        self.assertIsInstance(struct["same"], bool)

    def test_job_monitoring_failed_chunk(self):
        # A chunk whose model output drops HTML tags must surface on the live
        # monitor with status="failed" (so a dropped koboSpan would be visible),
        # while the job still finishes (give-up returns the original chunk).
        import translator.job as job_mod
        import translator.translator as translator_mod

        orig_build = job_mod.build_engine
        job_mod.build_engine = lambda *a, **k: BadStructureEngine()
        self.addCleanup(lambda: setattr(job_mod, "build_engine", orig_build))

        # Keep the retry loop short so the (always-failing) chunk gives up fast.
        orig_default = translator_mod.DEFAULT_MAX_TRIES
        translator_mod.DEFAULT_MAX_TRIES = 2
        self.addCleanup(
            lambda: setattr(translator_mod, "DEFAULT_MAX_TRIES", orig_default)
        )

        r = self.client.post(
            "/jobs/new",
            data={
                "engine": "openai",
                "input": self.src,
                "output": self.out,
                "from_chapter": "1",
                "to_chapter": "1",
                "from_lang": "EN",
                "to_lang": "VI",
            },
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 302)
        job_id = r.headers["Location"].rstrip("/").split("/")[-1]

        job = None
        for _ in range(100):
            job = self.registry.get(job_id)
            if job and job.status in ("done", "error", "stopped"):
                break
            time.sleep(0.1)
        self.assertEqual(job.status, "done", job.error)

        # at least one chunk_progress event reported the failed attempt
        failed_events = [
            e
            for e in job.events
            if e[0] == "chunk_progress"
            and (e[1].get("stats") or {}).get("current_chunk", {}).get("status") == "failed"
        ]
        self.assertTrue(failed_events, "expected a chunk_progress with status=failed")
        cc = failed_events[-1][1]["stats"]["current_chunk"]
        self.assertEqual(cc["status"], "failed")
        self.assertIsNotNone(cc.get("error"))
        # the persisted current chunk also reflects the failure
        self.assertEqual(
            (job.progress.get("current_chunk") or {}).get("status"), "failed"
        )

    def test_stream_sends_tail_only_and_keeps_disk_file(self):
        # A huge on-disk log must not flood the monitor: a fresh stream sends
        # only the newest LOG_TAIL_LINES, while the disk file stays complete.
        import json

        from webui.jobs import LOG_TAIL_LINES

        job = self.registry.create({"engine": "openai"})
        log_path = self.registry._log_path(job.id)
        total = LOG_TAIL_LINES + 200
        with open(log_path, "w", encoding="utf-8") as f:
            for i in range(total):
                f.write(f"line-{i:05d}\n")
        job.status = "done"
        self.registry.save(job)

        r = self.client.get(f"/jobs/{job.id}/stream")
        self.assertEqual(r.status_code, 200)
        received = []
        for raw in r.get_data(as_text=True).splitlines():
            if not raw.startswith("data: "):
                continue
            payload = json.loads(raw[len("data: "):])
            # The tail may arrive as single `line` events or batched
            # `log_batch` events (SSE batching keeps the monitor fast).
            if "line" in payload:
                received.append(payload["line"])
            elif payload.get("type") == "log_batch":
                received.extend(payload.get("lines") or [])
        self.assertEqual(len(received), LOG_TAIL_LINES)
        self.assertEqual(received[0], f"line-{total - LOG_TAIL_LINES:05d}")
        self.assertEqual(received[-1], f"line-{total - 1:05d}")

        # disk file untouched (full history kept)
        with open(log_path, encoding="utf-8") as f:
            self.assertEqual(len(f.read().splitlines()), total)

    def test_stale_running_job_marked_interrupted(self):
        # A job persisted as "running" (or "queued") belongs to a previous
        # process that is now dead; on load it must become "interrupted" so the
        # UI reflects reality instead of showing a stuck "running".
        import json as _json
        from webui.jobs import JobRegistry

        tmp = tempfile.mkdtemp()
        running_meta = {
            "id": "abc12345",
            "created_at": time.time(),
            "status": "running",
            "params": {"engine": "openai", "input": "x.epub", "output": "y.epub"},
            "progress": {},
            "result": None,
            "error": None,
            "stop_requested": False,
        }
        with open(os.path.join(tmp, "abc12345.json"), "w", encoding="utf-8") as f:
            _json.dump(running_meta, f)

        reg = JobRegistry(tmp)
        self.assertEqual(reg.get("abc12345").status, "interrupted")

        # A completed job must be left untouched.
        done_meta = dict(running_meta)
        done_meta["id"] = "def67890"
        done_meta["status"] = "done"
        with open(os.path.join(tmp, "def67890.json"), "w", encoding="utf-8") as f:
            _json.dump(done_meta, f)

        reg2 = JobRegistry(tmp)
        self.assertEqual(reg2.get("def67890").status, "done")

    def test_resume_stopped_job(self):
        # Stop a job almost immediately, then press Resume: the resumed run must
        # pick the same job back up and finish it (continuing from the checkpoint)
        # rather than being stuck. Per-chapter checkpoint-skip is covered by
        # test_job_core.TestRunTranslationCore.test_checkpoint_written_and_resume.
        import time as _time
        import translator.job as job_mod

        book = epub.EpubBook()
        book.set_identifier("webui-resume")
        book.set_title("Resume Test")
        book.set_language("en")
        ch1 = epub.EpubHtml(title="Ch1", file_name="ch1.xhtml", lang="en")
        ch1.content = b"<html><body><p>Hello world chapter one text</p></body></html>"
        ch2_paras = "".join(
            f"<p>Hello world chapter two number {i}</p>" for i in range(4)
        )
        ch2 = epub.EpubHtml(title="Ch2", file_name="ch2.xhtml", lang="en")
        ch2.content = ("<html><body>" + ch2_paras + "</body></html>").encode("utf-8")
        book.add_item(ch1)
        book.add_item(ch2)
        book.spine = ["nav", "ch1.xhtml", "ch2.xhtml"]
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        epub_path = os.path.join(self.tmp, "resume.epub")
        out_path = os.path.join(self.tmp, "resume.out.epub")
        epub.write_epub(epub_path, book, {})

        captured = {}

        class FastEngine(TranslationEngine):
            def supports_batch(self):
                return False

            def __init__(self):
                super().__init__(from_lang="EN", to_lang="VI")
                self.calls = 0

            def translate(self, text):
                self.calls += 1
                return text.replace("Hello", "Xin chào")

        def make_engine(*a, **k):
            eng = FastEngine()
            captured["eng"] = eng
            return eng

        self._orig_build = job_mod.build_engine
        try:
            job_mod.build_engine = make_engine
            r = self.client.post(
                "/jobs/new",
                data={
                    "engine": "openai",
                    "input": epub_path,
                    "output": out_path,
                    "from_chapter": "1",
                    "to_chapter": "2",
                    "from_lang": "EN",
                    "to_lang": "VI",
                },
                follow_redirects=False,
            )
            self.assertEqual(r.status_code, 302)
            job_id = r.headers["Location"].rstrip("/").split("/")[-1]

            # Click Stop immediately (before the first chunk is translated).
            self.client.post(f"/jobs/{job_id}/stop")
            for _ in range(100):
                job = self.registry.get(job_id)
                if job and job.status in ("done", "error", "stopped"):
                    break
                _time.sleep(0.1)
            self.assertEqual(job.status, "stopped")
            self.assertEqual(captured["eng"].calls, 0)  # aborted before translating

            # Resume: the same job must continue and finish the translation.
            r = self.client.post(f"/jobs/{job_id}/resume", follow_redirects=False)
            self.assertEqual(r.status_code, 302)
            for _ in range(100):
                job = self.registry.get(job_id)
                if job and job.status in ("done", "error", "stopped"):
                    break
                _time.sleep(0.1)
            self.assertEqual(job.status, "done")
            self.assertTrue(os.path.exists(out_path))
            # The resumed run actually translated the remaining chapters.
            self.assertGreater(captured["eng"].calls, 0)
            import zipfile

            with zipfile.ZipFile(out_path) as zf:
                names = [n for n in zf.namelist() if n.endswith("ch1.xhtml")]
                ch1_content = zf.read(names[0]).decode("utf-8") if names else ""
            self.assertIn("Xin chào", ch1_content)
        finally:
            job_mod.build_engine = self._orig_build

    def test_subprocess_stop(self):
        # The worker runs in a real multiprocessing.Process; pressing Stop must
        # terminate() it instantly and mark the job "stopped". The child's
        # finally block does not run on terminate(), so the app finalizes the
        # status from the persisted JSON.
        import time as _time
        import webui.core_runner as cr_mod

        book = epub.EpubBook()
        book.set_identifier("webui-subproc")
        book.set_title("Subproc Stop Test")
        book.set_language("en")
        paras = "".join(
            f"<p>Hello world sample text number {i}</p>" for i in range(5)
        )
        ch = epub.EpubHtml(title="Ch1", file_name="ch1.xhtml", lang="en")
        ch.content = ("<html><body>" + paras + "</body></html>").encode("utf-8")
        book.add_item(ch)
        book.spine = ["nav", "ch1.xhtml"]
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        epub_path = os.path.join(self.tmp, "subproc.epub")
        out_path = os.path.join(self.tmp, "subproc.out.epub")
        epub.write_epub(epub_path, book, {})

        saved = cr_mod._USE_PROCESS
        cr_mod._USE_PROCESS = True
        try:
            job = self.registry.create(
                {
                    "engine": "openai",
                    "input": epub_path,
                    "output": out_path,
                    "config_path": None,
                    "from_chapter": 1,
                    "to_chapter": 1,
                    "from_lang": "EN",
                    "to_lang": "VI",
                }
            )
            # Inject the engine directly so the spawned child does not need to
            # build a real (networked) engine.
            cr_mod.start_job(self.registry, job, engine=_SubprocSlowEngine())

            # Wait until the subprocess worker is actually alive.
            for _ in range(100):
                proc = cr_mod._PROCS.get(job.id)
                if proc is not None and proc.is_alive():
                    break
                _time.sleep(0.05)

            # Keep a reference: job_stop pops it from _PROCS after terminating.
            proc = cr_mod._PROCS.get(job.id)
            rv = self.client.post(f"/jobs/{job.id}/stop")
            self.assertEqual(rv.status_code, 302)

            if proc is not None:
                proc.join(timeout=5)
                self.assertFalse(proc.is_alive(), "subprocess worker was not terminated")

            # Status must end as "stopped", not stuck "running".
            for _ in range(100):
                meta = self.registry.load_meta(job.id)
                if meta and meta.get("status") in ("done", "error", "stopped"):
                    break
                _time.sleep(0.1)
            self.assertEqual(self.registry.load_meta(job.id)["status"], "stopped")
        finally:
            cr_mod._USE_PROCESS = saved


    def test_keys_page_renders_providers(self):
        """GET /config/keys shows every configured provider with key status and
        never leaks a raw key value into the HTML."""
        r = self.client.get("/config/keys")
        self.assertEqual(r.status_code, 200)
        for name in ("openai", "gemini", "webai", "groq", "deepseek", "openrouter", "ollama"):
            self.assertIn(name, r.get_data(as_text=True), f"provider {name} missing")
        # Raw real keys must not appear (only masked status labels).
        page = r.get_data(as_text=True)
        self.assertNotIn("sk-proj", page)
        self.assertNotIn("gsk_", page)
        # Compatible/openai providers expose a base_url field.
        self.assertIn("base_openrouter", page)

    def test_keys_page_save_keeps_existing_and_sets_new(self):
        """POST /config/keys on a temp config: a filled key is saved, a blank
        field keeps the existing key, and MASKED is never written."""
        import yaml

        from webui import config_store as cs

        tmp_cfg = os.path.join(self.tmp, "config.yaml")
        with open(tmp_cfg, "w", encoding="utf-8") as f:
            yaml.safe_dump(
                {
                    "openai": {"api_key": "sk-old", "model": "gpt-5.4-mini"},
                    "gemini": {"api_key": "ai-old"},
                    "webai": {"base_url": "http://localhost:6969", "api_key": ""},
                    "groq": {
                        "type": "openai_compatible",
                        "base_url": "https://api.groq.com/openai/v1",
                        "model": "openai/gpt-oss-120b",
                        "api_key": "gsk-old",
                    },
                    "openrouter": {
                        "type": "openai_compatible",
                        "base_url": "https://openrouter.ai/api/v1",
                        "api_key": "",
                    },
                    "ollama": {
                        "type": "openai_compatible",
                        "base_url": "http://localhost:11434/v1",
                        "model": "qwen3:14b",
                        "api_key": "",
                    },
                },
                f,
                allow_unicode=True,
            )

        orig_path = cs.DEFAULT_CONFIG_PATH
        cs.DEFAULT_CONFIG_PATH = tmp_cfg
        self.addCleanup(setattr, cs, "DEFAULT_CONFIG_PATH", orig_path)

        rv = self.client.post(
            "/config/keys",
            data={
                "key_openrouter": "sk-or-new",
                "key_groq": "",  # blank -> keep gsk-old
                "key_gemini": "",  # blank -> keep ai-old
                "model_groq": "openai/gpt-oss-20b",
            },
        )
        self.assertEqual(rv.status_code, 302)

        cfg = cs.load_config(tmp_cfg)
        self.assertEqual(cfg["openrouter"]["api_key"], "sk-or-new")
        self.assertEqual(cfg["groq"]["api_key"], "gsk-old", "blank must keep key")
        self.assertEqual(cfg["gemini"]["api_key"], "ai-old", "blank must keep key")
        self.assertEqual(cfg["groq"]["model"], "openai/gpt-oss-20b")
        self.assertNotIn("********", open(tmp_cfg, encoding="utf-8").read())

    def test_keys_page_supports_new_compatible_provider(self):
        """A brand-new provider section (type: openai_compatible) appears on the
        keys page and is saved through the form."""
        import yaml

        from webui import config_store as cs

        tmp_cfg = os.path.join(self.tmp, "config.yaml")
        with open(tmp_cfg, "w", encoding="utf-8") as f:
            yaml.safe_dump(
                {
                    "openai": {"api_key": "sk-old"},
                    "gemini": {"api_key": "ai-old"},
                    "webai": {"base_url": "http://localhost:6969"},
                    "groq": {
                        "type": "openai_compatible",
                        "base_url": "https://api.groq.com/openai/v1",
                        "model": "openai/gpt-oss-120b",
                        "api_key": "gsk-old",
                    },
                    "mistral": {
                        "type": "openai_compatible",
                        "base_url": "https://api.mistral.ai/v1",
                        "model": "mistral-large",
                        "api_key": "",
                    },
                },
                f,
                allow_unicode=True,
            )

        orig_path = cs.DEFAULT_CONFIG_PATH
        cs.DEFAULT_CONFIG_PATH = tmp_cfg
        self.addCleanup(setattr, cs, "DEFAULT_CONFIG_PATH", orig_path)

        r = self.client.get("/config/keys")
        page = r.get_data(as_text=True)
        self.assertIn("mistral", page)
        self.assertIn("base_mistral", page)

        rv = self.client.post("/config/keys", data={"key_mistral": "mi-new-key"})
        self.assertEqual(rv.status_code, 302)
        cfg = cs.load_config(tmp_cfg)
        self.assertEqual(cfg["mistral"]["api_key"], "mi-new-key")

    def test_job_new_shows_model_dropdown_and_missing_key_hint(self):
        """job_new page renders a live model <select> (with config fallback
        embedded for the JS), a custom-model input and flags engines that are
        missing a required key."""
        r = self.client.get("/jobs/new")
        self.assertEqual(r.status_code, 200)
        page = r.get_data(as_text=True)
        # Real dropdown + custom model input + refresh button + fetch endpoint.
        self.assertIn('<select id="inp-model"', page)
        self.assertIn('name="model"', page)
        self.assertIn("data-engine-url", page)
        self.assertIn("/api/provider-models", page)
        self.assertIn('id="inp-model-custom"', page)
        self.assertIn('id="btn-model-refresh"', page)
        # Static fallback per engine is embedded for the JS.
        self.assertIn("MODEL_SUGGESTIONS", page)
        # Missing-key engines are listed (deepseek/openrouter have no key in the
        # real config; groq/openai do).
        self.assertIn("deepseek", page)

    def test_provider_models_api(self):
        """GET /api/provider-models?engine=... proxies fetch_models; unknown
        engines get a 400."""
        with mock.patch.object(
            webui_app,
            "fetch_models",
            return_value={"engine": "groq", "models": ["a", "b"], "source": "live", "error": None},
        ) as fake:
            r = self.client.get("/api/provider-models?engine=groq")
            self.assertEqual(fake.call_count, 1)
        self.assertEqual(r.status_code, 200)
        payload = r.get_json()
        self.assertEqual(payload["models"], ["a", "b"])
        self.assertEqual(payload["source"], "live")

        r2 = self.client.get("/api/provider-models?engine=nope")
        self.assertEqual(r2.status_code, 400)
        self.assertIn("error", r2.get_json())

    def test_keys_models_textarea_flow(self):
        """API Keys: the models textarea saves into config `models:`, a blank
        textarea keeps the existing list, and the clear checkbox wipes it."""
        import yaml

        from webui import config_store as cs

        tmp_cfg = os.path.join(self.tmp, "config.yaml")
        with open(tmp_cfg, "w", encoding="utf-8") as f:
            yaml.safe_dump(
                {
                    "openai": {"api_key": "sk-old", "models": ["gpt-5.4-mini"]},
                    "gemini": {"api_key": "ai-old"},
                    "webai": {"base_url": "http://localhost:6969"},
                },
                f,
                allow_unicode=True,
            )
        orig_path = cs.DEFAULT_CONFIG_PATH
        cs.DEFAULT_CONFIG_PATH = tmp_cfg
        self.addCleanup(setattr, cs, "DEFAULT_CONFIG_PATH", orig_path)

        # The page renders a textarea per provider with the current models.
        page = self.client.get("/config/keys").get_data(as_text=True)
        self.assertIn("name=\"models_openai\"", page)
        self.assertIn("gpt-5.4-mini", page)
        self.assertIn("data-fetch-models=\"openai\"", page)

        # Non-blank textarea -> replaces the list.
        rv = self.client.post(
            "/config/keys", data={"models_openai": "gpt-5.5\ngpt-5.4-mini"}
        )
        self.assertEqual(rv.status_code, 302)
        cfg = cs.load_config(tmp_cfg)
        self.assertEqual(cfg["openai"]["models"], ["gpt-5.5", "gpt-5.4-mini"])

        # Blank + no clear -> keeps existing.
        self.client.post("/config/keys", data={"models_openai": ""})
        cfg = cs.load_config(tmp_cfg)
        self.assertEqual(cfg["openai"]["models"], ["gpt-5.5", "gpt-5.4-mini"])

        # Clear checkbox -> wipes the list.
        self.client.post("/config/keys", data={"models_openai": "", "clear_models_openai": "1"})
        cfg = cs.load_config(tmp_cfg)
        self.assertEqual(cfg["openai"]["models"], [])

    def test_add_provider_route(self):
        """POST /config/keys/add-provider creates an openai_compatible section
        and rejects reserved / duplicate / malformed names."""
        import yaml

        from webui import config_store as cs

        tmp_cfg = os.path.join(self.tmp, "config.yaml")
        with open(tmp_cfg, "w", encoding="utf-8") as f:
            yaml.safe_dump(
                {
                    "openai": {"api_key": "sk-old"},
                    "gemini": {"api_key": "ai-old"},
                    "webai": {"base_url": "http://localhost:6969"},
                },
                f,
                allow_unicode=True,
            )
        orig_path = cs.DEFAULT_CONFIG_PATH
        cs.DEFAULT_CONFIG_PATH = tmp_cfg
        self.addCleanup(setattr, cs, "DEFAULT_CONFIG_PATH", orig_path)

        rv = self.client.post(
            "/config/keys/add-provider",
            data={
                "name": "mistral",
                "base_url": "https://api.mistral.ai/v1",
                "api_key": "mi-1",
                "model": "mistral-large",
            },
        )
        self.assertEqual(rv.status_code, 302)
        cfg = cs.load_config(tmp_cfg)
        self.assertEqual(cfg["mistral"]["type"], "openai_compatible")
        self.assertEqual(cfg["mistral"]["base_url"], "https://api.mistral.ai/v1")
        self.assertEqual(cfg["mistral"]["api_key"], "mi-1")
        self.assertEqual(cfg["mistral"]["model"], "mistral-large")

        # Reserved section name rejected (openai stays untouched).
        rv = self.client.post(
            "/config/keys/add-provider", data={"name": "openai", "base_url": "http://x"}
        )
        self.assertEqual(rv.status_code, 302)
        self.assertIn("add_error", rv.headers["Location"])
        cfg = cs.load_config(tmp_cfg)
        self.assertEqual(cfg["openai"]["api_key"], "sk-old")

        # Duplicate name rejected (config unchanged).
        rv = self.client.post(
            "/config/keys/add-provider",
            data={"name": "mistral", "base_url": "http://other.example/v1"},
        )
        self.assertEqual(rv.status_code, 302)
        cfg = cs.load_config(tmp_cfg)
        self.assertEqual(cfg["mistral"]["base_url"], "https://api.mistral.ai/v1")

        # Invalid name and missing base_url rejected.
        rv = self.client.post(
            "/config/keys/add-provider", data={"name": "bad name!", "base_url": "http://x"}
        )
        self.assertEqual(rv.status_code, 302)
        rv = self.client.post(
            "/config/keys/add-provider", data={"name": "nourl", "base_url": ""}
        )
        self.assertEqual(rv.status_code, 302)
        self.assertNotIn("nourl", cs.load_config(tmp_cfg))

    def test_remove_provider_route(self):
        """POST /config/keys/remove-provider removes only user-created
        openai_compatible sections."""
        import yaml

        from webui import config_store as cs

        tmp_cfg = os.path.join(self.tmp, "config.yaml")
        with open(tmp_cfg, "w", encoding="utf-8") as f:
            yaml.safe_dump(
                {
                    "openai": {"api_key": "sk-old"},
                    "gemini": {"api_key": "ai-old"},
                    "webai": {"base_url": "http://localhost:6969"},
                    "mistral": {
                        "type": "openai_compatible",
                        "base_url": "https://api.mistral.ai/v1",
                        "api_key": "",
                    },
                },
                f,
                allow_unicode=True,
            )
        orig_path = cs.DEFAULT_CONFIG_PATH
        cs.DEFAULT_CONFIG_PATH = tmp_cfg
        self.addCleanup(setattr, cs, "DEFAULT_CONFIG_PATH", orig_path)

        rv = self.client.post("/config/keys/remove-provider", data={"name": "mistral"})
        self.assertEqual(rv.status_code, 302)
        cfg = cs.load_config(tmp_cfg)
        self.assertNotIn("mistral", cfg)
        self.assertIn("openai", cfg)

        # Built-ins and unknown names are protected.
        rv = self.client.post("/config/keys/remove-provider", data={"name": "openai"})
        self.assertEqual(rv.status_code, 400)
        rv = self.client.post("/config/keys/remove-provider", data={"name": "ghost"})
        self.assertEqual(rv.status_code, 400)
        cfg = cs.load_config(tmp_cfg)
        self.assertIn("openai", cfg)

    def test_settings_page_get(self):
        """GET /config/settings renders the GUI form grouped by section and
        never exposes api_key inputs."""
        r = self.client.get("/config/settings")
        self.assertEqual(r.status_code, 200)
        page = r.get_data(as_text=True)
        self.assertIn('name="translation.fallback_max_chunk_size"', page)
        self.assertIn('name="discord.enabled"', page)
        self.assertIn('name="translation.illustration.style_prompt"', page)
        # API keys are deliberately not editable from this page.
        self.assertNotIn('name="openai.api_key"', page)
        self.assertNotIn('name="gemini.api_key"', page)

    def test_settings_page_post(self):
        """POST /config/settings writes typed values, applies checkbox
        semantics, and preserves unrelated sections as-is."""
        import yaml

        from webui import config_store as cs

        tmp_cfg = os.path.join(self.tmp, "config.yaml")
        with open(tmp_cfg, "w", encoding="utf-8") as f:
            yaml.safe_dump(
                {
                    "openai": {"api_key": "sk-old"},
                    "translation": {"fallback_max_chunk_size": 6000},
                },
                f,
                allow_unicode=True,
            )
        orig_path = cs.DEFAULT_CONFIG_PATH
        cs.DEFAULT_CONFIG_PATH = tmp_cfg
        self.addCleanup(setattr, cs, "DEFAULT_CONFIG_PATH", orig_path)

        rv = self.client.post(
            "/config/settings",
            data={
                "translation.fallback_max_chunk_size": "8000",
                "translation.html_structure_similarity_threshold": "0.8",
                "translation.custom_prompt": "Dịch chuẩn",
                "translation.illustration.enabled": "1",
                "translation.illustration.width": "1024",
                "translation.consistency.enabled": "",  # unchecked -> False
                "discord.enabled": "1",
                "discord.webhook_url": "https://discord.com/api/webhooks/1/x",
                "discord.mention_user_id": "123",
            },
        )
        self.assertEqual(rv.status_code, 302)
        cfg = cs.load_config(tmp_cfg)
        self.assertEqual(cfg["translation"]["fallback_max_chunk_size"], 8000)
        self.assertAlmostEqual(cfg["translation"]["html_structure_similarity_threshold"], 0.8)
        self.assertEqual(cfg["translation"]["custom_prompt"], "Dịch chuẩn")
        self.assertIs(cfg["translation"]["illustration"]["enabled"], True)
        self.assertEqual(cfg["translation"]["illustration"]["width"], 1024)
        self.assertIs(cfg["translation"]["consistency"]["enabled"], False)
        self.assertIs(cfg["discord"]["enabled"], True)
        self.assertEqual(cfg["discord"]["webhook_url"], "https://discord.com/api/webhooks/1/x")
        self.assertEqual(cfg["discord"]["mention_user_id"], "123")
        # Untouched sections/keys are preserved.
        self.assertEqual(cfg["openai"]["api_key"], "sk-old")

    def test_settings_page_post_invalid_number(self):
        """A non-numeric value re-renders the page with an error and does not
        save anything."""
        import yaml

        from webui import config_store as cs

        tmp_cfg = os.path.join(self.tmp, "config.yaml")
        with open(tmp_cfg, "w", encoding="utf-8") as f:
            yaml.safe_dump(
                {"translation": {"max_tries": 1}},
                f,
                allow_unicode=True,
            )
        orig_path = cs.DEFAULT_CONFIG_PATH
        cs.DEFAULT_CONFIG_PATH = tmp_cfg
        self.addCleanup(setattr, cs, "DEFAULT_CONFIG_PATH", orig_path)

        rv = self.client.post(
            "/config/settings",
            data={"translation.fallback_max_chunk_size": "abc"},
        )
        self.assertEqual(rv.status_code, 200)
        page = rv.get_data(as_text=True)
        self.assertIn("settings.invalid_body", page)  # localized body rendered
        self.assertIn("fallback_max_chunk_size", page)
        # Nothing was written (max_tries untouched, no new key).
        cfg = cs.load_config(tmp_cfg)
        self.assertEqual(cfg["translation"]["max_tries"], 1)
        self.assertNotIn("fallback_max_chunk_size", cfg["translation"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
