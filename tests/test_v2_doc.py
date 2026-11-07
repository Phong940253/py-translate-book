"""WebUI v2 offline: the reader's chapter document is served inside /v2.

The reader iframe must navigate to a URL covered by the PWA service worker's
scope (/v2/) — only then does the worker control the document and every
/api/reader/res subresource it loads, which is what makes a downloaded book
readable offline. The route simply reuses the /api/reader/doc handler.
"""

import atexit
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Importing webui.app constructs the default registry; keep it away from the
# real webui/jobs/ directory (same guard as test_webui.py, which runs after
# this module alphabetically).
_TEST_JOB_DIR = tempfile.mkdtemp(prefix="pytb-test-jobs-")
atexit.register(shutil.rmtree, _TEST_JOB_DIR, ignore_errors=True)
os.environ["PYTB_JOB_DIR"] = _TEST_JOB_DIR

from ebooklib import epub

from webui.app import PROJECT_ROOT, app
import webui.app_v2  # noqa: F401 — registers the /v2/doc route on the app


def _make_epub(path):
    book = epub.EpubBook()
    book.set_identifier("v2-doc-test")
    book.set_title("V2 Doc Test")
    book.set_language("en")
    ch = epub.EpubHtml(title="Ch1", file_name="ch1.xhtml", lang="en")
    ch.content = b"<html><body><p>Hello world sample text here</p></body></html>"
    book.add_item(ch)
    book.spine = ["nav", "ch1.xhtml"]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    epub.write_epub(path, book, {})


class V2DocRouteTest(unittest.TestCase):
    def setUp(self):
        # The reader API only accepts paths inside the project root.
        self.src = os.path.join(PROJECT_ROOT, "_test_v2_doc.epub")
        _make_epub(self.src)
        self.addCleanup(lambda: os.path.exists(self.src) and os.remove(self.src))
        self.client = app.test_client()

    def test_v2_doc_serves_the_same_chapter_document(self):
        r = self.client.get("/v2/doc?path=" + self.src + "&n=1")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Hello world", r.data)
        self.assertIn(b"rd-content", r.data)  # sanitized reader wrapper
        api = self.client.get("/api/reader/doc?path=" + self.src + "&n=1")
        self.assertEqual(r.data, api.data, "byte-identical to the reader API")

    def test_v2_doc_reports_invalid_paths_like_the_api(self):
        r = self.client.get("/v2/doc?path=/definitely-missing.epub&n=1")
        self.assertEqual(r.status_code, 400)
        self.assertIn(b"invalid path", r.data)

    def test_v2_doc_is_not_the_spa_shell(self):
        # /v2/<path> would answer with index.html; the explicit route must win.
        r = self.client.get("/v2/doc?path=" + self.src + "&n=1")
        self.assertNotIn(b'<div id="root"', r.data)


if __name__ == "__main__":
    unittest.main()
