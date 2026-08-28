"""Offline smoke tests for the Flask web UI.

We stub ``build_engine`` (so no real AI call) and ``DiscordNotifier`` (no
webhook spam), then exercise the full web flow: create a job via the form,
let the background runner finish, and assert the output EPUB is produced.
"""

import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ebooklib import epub

from translator.engines.base import TranslationEngine


class StubEngine(TranslationEngine):
    def __init__(self):
        super().__init__(from_lang="EN", to_lang="VI")

    def supports_batch(self):
        return False

    def translate(self, text):
        return text.replace("Hello", "Xin chào").replace("world", "thế giới")


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

    def tearDown(self):
        import translator.job as job_mod
        import translator.discord_notifier as dn_mod

        job_mod.build_engine = self._orig_build
        dn_mod.DiscordNotifier.send_translation_completed = self._orig_discord

    def test_pages_load(self):
        for path in ("/", "/jobs/new", "/config", "/books", "/illustrations"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, path)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
