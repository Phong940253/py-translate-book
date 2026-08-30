"""Offline smoke tests for the Flask web UI.

We stub ``build_engine`` (so no real AI call) and ``DiscordNotifier`` (no
webhook spam), then exercise the full web flow: create a job via the form,
let the background runner finish, and assert the output EPUB is produced.
"""

import os
import base64
import re
import shutil
import sys
import tempfile
import time
import unittest

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
        for path in ("/", "/jobs/new", "/config", "/books", "/illustrations"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, path)

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

        bad = self.client.get("/books/cover?path=/etc/passwd")
        self.assertEqual(bad.status_code, 400, "path outside PROJECT_ROOT rejected")

        missing = self.client.get("/books/cover?path=" + nocov_path)
        self.assertEqual(missing.status_code, 404, "no cover -> 404")

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

    def test_job_new_shows_model_datalist_and_missing_key_hint(self):
        """job_new page embeds model suggestions per engine and flags engines
        that are missing a required key."""
        r = self.client.get("/jobs/new")
        self.assertEqual(r.status_code, 200)
        page = r.get_data(as_text=True)
        # datalist id + the Model field exist
        self.assertIn('id="model-suggestions"', page)
        self.assertIn('name="model"', page)
        # Missing-key engines are listed (deepseek/openrouter have no key in the
        # real config; groq/openai do).
        self.assertIn("deepseek", page)


if __name__ == "__main__":
    unittest.main(verbosity=2)
