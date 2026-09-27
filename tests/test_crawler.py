"""Offline unit tests for crawler (no network, no AI calls).

Covers:
  TC-C1: fetch_toc parses clist JSON + book title/author from HTML.
  TC-C2: fetch_chapter extracts clean paragraphs from article HTML.
  TC-C3: build_epub produces a readable EPUB with one file per chapter.
  TC-C4: shu69 TOC/chapter parsing (69shuba, Cloudflare source).
"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ebooklib import epub as _epub

from crawler import ixdzs, shu69
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

CHAPTER_HTML_DOUBLE_DUP = """
<html><body>
<h1 class="page-d-name">第261章 很自然带入角色的顾言</h1>
<article class="page-content"><h3>第261章 很自然带入角色的顾言</h3>
<section><p>app2;</p><p>第261章很自然带入角色的顾言</p><p>第261章很自然带入角色的顾言</p><p>君王？</p></section>
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

    def test_strips_double_dup_title(self):
        session = ixdzs.new_session()
        with patch.object(
            session, "get", return_value=_FakeResp(text=CHAPTER_HTML_DOUBLE_DUP)
        ):
            _, paras = ixdzs.fetch_chapter(
                session, "https://ixdzs8.com/read/546610/p261.html"
            )
        self.assertEqual(paras, ["君王？"])


SHU69_TOC_HTML = """
<html><head><meta charset="utf-8"></head><body>
<div class="catalog" id="catalog" data-eid="0">
 <h1 class="muluh1"><a href="https://www.69shuba.com/book/84642.htm">最新章节</a></h1>
 <ul>
  <li data-num="1"><a href="https://www.69shuba.com/txt/84642/39380099">第1章 重生校园时代，拯救校花白清夏【新书</a></li>
  <li data-num="2"><a href="/txt/84642/39380100">第2章 没藏东西？这不还有一层衣服吗！【新</a></li>
 </ul>
</div>
</body></html>
"""

SHU69_META_HTML = """
<html><head><meta charset="utf-8"></head><body>
<h1>重生：开局逮到高冷校花超市偷窃</h1>
作者： 陆远秋
</body></html>
"""

SHU69_CHAPTER_HTML = """
<html><head><meta charset="gbk"></head><body>
<div class="txtnav">
 <h1 class="hide720">第1章 重生校园时代，拯救校花白清夏【新书求收藏】</h1>
 <div class="txtinfo hide720"><span>2024-11-20</span><span>作者： 陆远秋</span></div>
 <div id="txtright"><script>loadAdv(2, 0);</script></div>
 第1章 重生校园时代，拯救校花白清夏【新书求收藏】<br/><br/>
 “你说什么？当年投票竞选体育委员？”<br/><br/>
 咖啡厅里，人影绰绰。<br/><br/>
 (本章完)
</div>
</body></html>
"""


class TestShu69Toc(unittest.TestCase):
    def test_parses_catalog_and_relative_urls(self):
        chapters = shu69.parse_toc_html(SHU69_TOC_HTML, "84642")
        self.assertEqual(len(chapters), 2)
        self.assertEqual(chapters[0]["ordernum"], "1")
        self.assertEqual(
            chapters[0]["url"], "https://www.69shuba.com/txt/84642/39380099"
        )
        self.assertEqual(chapters[1]["url"], "https://www.69shuba.com/txt/84642/39380100")

    def test_challenge_raises(self):
        with self.assertRaises(RuntimeError):
            shu69.parse_toc_html("<html>Just a moment...</html>", "84642")

    def test_parses_meta(self):
        title, author = shu69.parse_meta_html(SHU69_META_HTML)
        self.assertEqual(title, "重生：开局逮到高冷校花超市偷窃")
        self.assertEqual(author, "陆远秋")


class TestShu69Chapter(unittest.TestCase):
    def test_strips_h1_meta_ad_and_dup_title(self):
        title, paras = shu69.parse_chapter_html(SHU69_CHAPTER_HTML)
        self.assertEqual(title, "第1章 重生校园时代，拯救校花白清夏【新书求收藏】")
        self.assertEqual(
            paras,
            ["“你说什么？当年投票竞选体育委员？”", "咖啡厅里，人影绰绰。"],
        )

    def test_gbk_bytes_decode(self):
        raw = "<meta charset=\"gbk\">".encode("ascii") + "第1章".encode("gb18030")
        self.assertIn("第1章", shu69._decode(raw))

    def test_challenge_raises(self):
        with self.assertRaises(RuntimeError):
            shu69.parse_chapter_html("<html>Just a moment...</html>")

    def test_strips_sequence_prefix_and_heading_variant(self):
        # <h1> prefixed with the catalogue sequence number, body heading written
        # with a different decoration (em dash) than the <h1>.
        html = """
        <div class="txtnav">
          <h1>45.第44章 什么？你没上厕所？憋不住了？</h1>
          <div class="txtinfo"><span>2025-01-02</span></div>
          第44章 什么？你没上厕所？憋不住了？<br/><br/>
          她的脸涨得通红。<br/><br/>
          第45章 是下一章的标题吗<br/>
        </div>
        """
        title, paras = shu69.parse_chapter_html(html)
        self.assertEqual(title, "第44章 什么？你没上厕所？憋不住了？")
        self.assertEqual(paras[0], "她的脸涨得通红。")
        # a later paragraph that merely starts with a chapter number stays
        self.assertEqual(paras[1], "第45章 是下一章的标题吗")

    def test_strips_emdash_heading_duplicate(self):
        html = """
        <div class="txtnav">
          <h1>第50章 来，跟我学，啊【第二更】</h1>
          第50章 来，跟我学，啊——【第二更】<br/><br/>
          陆远秋清了清嗓子。<br/>
        </div>
        """
        title, paras = shu69.parse_chapter_html(html)
        self.assertEqual(title, "第50章 来，跟我学，啊【第二更】")
        self.assertEqual(paras, ["陆远秋清了清嗓子。"])


    def test_recovers_from_dead_browser(self):
        html = SHU69_CHAPTER_HTML.replace('charset="gbk"', 'charset="utf-8"').encode("utf-8")
        session = _StubSession([OSError("urlopen error"), OSError("gone"), html])
        with patch("crawler.shu69.time.sleep"):
            title, paras = shu69.fetch_chapter(
                session, "https://www.69shuba.com/txt/84642/39380099"
            )
        self.assertEqual(session.closed, 2)  # broken session dropped each time
        self.assertEqual(session.ensured, 3)
        self.assertEqual(title, "第1章 重生校园时代，拯救校花白清夏【新书求收藏】")
        self.assertEqual(paras[0], "“你说什么？当年投票竞选体育委员？”")

    def test_parse_errors_are_not_retried(self):
        session = _StubSession([b"<html>Just nothing</html>"])
        with self.assertRaises(RuntimeError):
            shu69.fetch_chapter(session, "https://www.69shuba.com/txt/84642/39380099")
        self.assertEqual(session.closed, 0)


class _StubSession:
    def __init__(self, results):
        self.results = list(results)
        self.closed = 0
        self.ensured = 0

    def ensure(self, url):
        self.ensured += 1

    def close(self):
        self.closed += 1

    def fetch_bytes(self, url, timeout=60):
        item = self.results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


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
