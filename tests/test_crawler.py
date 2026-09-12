"""Offline unit tests for crawler (no network, no AI calls).

Covers:
  TC-C1: fetch_toc parses clist JSON + book title/author from HTML.
  TC-C2: fetch_chapter extracts clean paragraphs from article HTML.
  TC-C3: build_epub produces a readable EPUB with one file per chapter.
"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ebooklib import epub as _epub

from crawler import ixdzs
from crawler.to_epub import build_epub


class _FakeResp:
    def __init__(self, text="", payload=None):
        self.text = text
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


TOC_HTML = """
<html><body>
<h1>反派：迎娶盲人未婚妻</h1>
<a class="bauthor" href="/author/x">牛头人战士</a>
</body></html>
"""

CHAPTER_HTML = """
<html><body>
<h1 class="page-d-name">第1章 标题</h1>
<article class="page-content"><h3>第1章 标题</h3>
<section><p>江海市。</p><p>  </p><p>天湖洞一号别墅内。</p></section>
</article>
</body></html>
"""

CHAPTER_HTML_JUNK = """
<html><body>
<h1 class="page-d-name">第11章 你这个大少爷还会做饭？</h1>
<article class="page-content"><h3>第11章 你这个大少爷还会做饭？</h3>
<section><p>app2;</p><p>第11章你这个大少爷还会做饭？</p><p>黄昏的下午。</p><p>app2;</p><p>chaptererror;</p></section>
</article>
</body></html>
"""


class TestFetchToc(unittest.TestCase):
    def test_parses_clist_and_meta(self):
        payload = {
            "rs": 200,
            "data": [
                {"ctype": "1", "ordernum": "0", "title": "第一卷"},
                {"ctype": "0", "ordernum": "1", "title": "第1章 标题"},
                {"ctype": "0", "ordernum": "2", "title": "第2章 标题2"},
            ],
        }
        session = ixdzs.new_session()
        with patch.object(
            session, "get", return_value=_FakeResp(text=TOC_HTML)
        ), patch.object(
            session, "post", return_value=_FakeResp(text="{}", payload=payload)
        ):
            title, author, chapters = ixdzs.fetch_toc(session, "546610")
        self.assertEqual(title, "反派：迎娶盲人未婚妻")
        self.assertEqual(author, "牛头人战士")
        self.assertEqual(len(chapters), 2)  # volume header skipped
        self.assertEqual(
            chapters[0]["url"], "https://ixdzs8.com/read/546610/p1.html"
        )

    def test_clist_error_raises(self):
        session = ixdzs.new_session()
        with patch.object(
            session, "get", return_value=_FakeResp(text=TOC_HTML)
        ), patch.object(
            session,
            "post",
            return_value=_FakeResp(text="{}", payload={"rs": 500}),
        ):
            with self.assertRaises(RuntimeError):
                ixdzs.fetch_toc(session, "546610")


class TestFetchChapter(unittest.TestCase):
    def test_extracts_paragraphs(self):
        session = ixdzs.new_session()
        with patch.object(
            session, "get", return_value=_FakeResp(text=CHAPTER_HTML)
        ):
            title, paras = ixdzs.fetch_chapter(
                session, "https://ixdzs8.com/read/546610/p1.html"
            )
        self.assertEqual(title, "第1章 标题")
        self.assertEqual(paras, ["江海市。", "天湖洞一号别墅内。"])

    def test_missing_article_raises(self):
        session = ixdzs.new_session()
        with patch.object(session, "get", return_value=_FakeResp(text="<html></html>")):
            with self.assertRaises(RuntimeError):
                ixdzs.fetch_chapter(session, "https://ixdzs8.com/read/1/p1.html")

    def test_strips_template_junk_and_dup_title(self):
        session = ixdzs.new_session()
        with patch.object(
            session, "get", return_value=_FakeResp(text=CHAPTER_HTML_JUNK)
        ):
            title, paras = ixdzs.fetch_chapter(
                session, "https://ixdzs8.com/read/546610/p11.html"
            )
        self.assertEqual(title, "第11章 你这个大少爷还会做饭？")
        self.assertEqual(paras, ["黄昏的下午。"])


class TestBuildEpub(unittest.TestCase):
    def test_epub_roundtrip(self):
        chapters = [
            {"title": "第1章 标题", "paragraphs": ["江海市。", "天湖洞一号别墅内。"]},
            {"title": "第2章 标题2", "paragraphs": ["第二章正文。"]},
        ]
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "book.epub")
            build_epub("书名", "作者", chapters, out)
            book = _epub.read_epub(out)
            self.assertEqual(book.get_metadata("DC", "title")[0][0], "书名")
            docs = [
                i
                for i in book.get_items()
                if i.file_name.startswith("chap_")
            ]
            self.assertEqual(len(docs), 2)
            self.assertIn("江海市", docs[0].content.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
