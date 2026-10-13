"""Offline unit tests for crawler (no network, no AI calls).

Covers:
  TC-C1: fetch_toc parses clist JSON + book title/author from HTML.
  TC-C2: fetch_chapter extracts clean paragraphs from article HTML.
  TC-C3: build_epub produces a readable EPUB with one file per chapter.
  TC-C4: shu69 TOC/chapter parsing (69shuba, Cloudflare source).
  TC-C5: xszj TOC/chapter parsing incl. multi-screen stitching.
  TC-C6: fanqie TOC/chapter parsing incl. PUA font decode.
"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ebooklib import epub as _epub

from crawler import crawl as crawl_cli
from crawler import fanqie, ixdzs, shu69, xszj
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


class TestCliTitleOverride(unittest.TestCase):
    """--title renames the book when the source itself renamed it."""

    def _run_rebuild(self, workdir, title=None):
        argv = ["crawl", "--source", "ixdzs", "--bid", "999", "--workdir", workdir, "--rebuild-only"]
        if title is not None:
            argv += ["--title", title]
        with patch.object(sys, "argv", argv):
            crawl_cli.main()

    def test_rebuild_uses_overridden_title(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "raw"))
            with open(os.path.join(d, "raw", "0001.txt"), "w", encoding="utf-8") as f:
                f.write("第1章 开始\n　　正文一段。\n")
            meta = {
                "title": "旧书名",
                "author": "作者",
                "chapters": [{"ordernum": "1", "title": "第1章 开始", "url": "u"}],
            }
            with open(os.path.join(d, "chapters.json"), "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False)

            self._run_rebuild(d, title="新书名")

            self.assertTrue(os.path.exists(os.path.join(d, "新书名.epub")))
            with open(os.path.join(d, "chapters.json"), encoding="utf-8") as f:
                saved = json.load(f)
            self.assertEqual(saved["title"], "新书名")
            # override must not lose chapter metadata
            self.assertEqual(saved["author"], "作者")
            self.assertEqual(saved["chapters"][0]["ordernum"], "1")

    def test_rebuild_without_title_keeps_existing_title(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "raw"))
            with open(os.path.join(d, "raw", "0001.txt"), "w", encoding="utf-8") as f:
                f.write("第1章 开始\n　　正文一段。\n")
            meta = {
                "title": "旧书名",
                "author": "作者",
                "chapters": [{"ordernum": "1", "title": "第1章 开始", "url": "u"}],
            }
            with open(os.path.join(d, "chapters.json"), "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False)

            self._run_rebuild(d)

            self.assertTrue(os.path.exists(os.path.join(d, "旧书名.epub")))


XSZJ_TOC_HTML = """
<html><head><meta charset="utf-8"></head><body>
<div id="list"><dl><div id="content_1">
  <a href="/b/336003/c/1772342" rel="chapter"><dd>第1章 第01章 校花当众送奶茶</dd></a>
  <a href="/b/336003/c/1772347" rel="chapter"><dd>第2章 第02章 高冷校花居然是未婚妻</dd></a>
  <a href="/b/336003/c/1772404" rel="chapter"><dd>第10章 校花高冷女神人设要崩了</dd></a>
</div></dl></div>
</body></html>
"""

XSZJ_META_HTML = """
<html><head><meta charset="utf-8"></head><body>
<div id="info">
  <h1>好烦啊，高冷校花天天倒追我</h1>
  <p>作者：<a href="/z/%E4%B8%80%E5%8F%AA%E5%B0%8F%E6%B0%B4%E4%BA%BA" title="x">一只小水人</a></p>
</div>
</body></html>
"""

XSZJ_CHAPTER_PAGE1 = """
<html><head><meta charset="utf-8">
<title>第01章 校花当众送奶茶-好烦啊，高冷校花天天倒追我第1章(1/3)-小说之家</title>
</head><body>
<div id="content"><div id="booktxt">
  <p></p><p>　　“大家快看，苏校花来我们新生晚会了！！”</p>
  <p>　　虽然这个未婚</p><p>　　：</p>
</div></div>
<div class="bottem2"><a rel="prev" href="/b/336003/c/1772342?page=2">下一页</a></div>
</body></html>
"""

XSZJ_CHAPTER_PAGE2 = """
<html><head><meta charset="utf-8">
<title>第01章 校花当众送奶茶-好烦啊，高冷校花天天倒追我第1章(2/3)-小说之家</title>
</head><body>
<div id="content"><div id="booktxt">
  <p>　　妻张成已经十年没见了，年底就是婚期了。</p>
  <p>　　众人都看呆了。</p><p>　　：</p>
</div></div>
<div class="bottem2"><a rel="prev" href="javascript:void(0);">没有了</a></div>
</body></html>
"""


class _FakeResp:
    def __init__(self, text="", payload=None):
        self.text = text
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeXszjSession:
    """GET keyed by url (page2 matched by its ?page= suffix)."""

    def __init__(self, responses):
        self.responses = responses  # list of (needle, html)
        self.calls = []

    def get(self, url, timeout=20):
        self.calls.append(url)
        for needle, html in self.responses:
            if needle in url:
                return _FakeResp(text=html)
        raise AssertionError(f"unexpected url {url}")


class TestXszjToc(unittest.TestCase):
    def test_parses_ordered_toc_and_strips_dup_seq(self):
        chapters = xszj.parse_toc_html(XSZJ_TOC_HTML, "336003")
        self.assertEqual(len(chapters), 3)
        self.assertEqual(chapters[0]["ordernum"], "1")
        self.assertEqual(chapters[0]["title"], "第01章 校花当众送奶茶")
        self.assertEqual(chapters[0]["url"], "https://xszj.org/b/336003/c/1772342")
        # no inner seq in the site title -> keep the full dd text
        self.assertEqual(chapters[2]["title"], "第10章 校花高冷女神人设要崩了")

    def test_parses_meta(self):
        title, author = xszj.parse_meta_html(XSZJ_META_HTML, "336003")
        self.assertEqual(title, "好烦啊，高冷校花天天倒追我")
        self.assertEqual(author, "一只小水人")


XSZJ_CHAPTER_WATERMARK = """
<html><head><meta charset="utf-8">
<title>第02章 高冷校花居然是未婚妻-好烦啊，高冷校花天天倒追我第2章(1/1)-小说之家</title>
</head><body>
<div id="content"><div id="booktxt">
  <p>　　苏校花找的人，居然是新生代表，家世也很好。M.Ι.</p>
  <p>　　这要是接了奶茶，就成了眼中钉肉中刺了。Ｅ</p>
  <p>　　真是人比人气死人！！.</p>
</div></div>
</body></html>
"""


class TestXszjChapter(unittest.TestCase):
    def test_title_from_tag_and_cut_marker(self):
        title = xszj.chapter_title_from_html(XSZJ_CHAPTER_PAGE1)
        self.assertEqual(title, "第01章 校花当众送奶茶")
        paras, nxt, cut = xszj.parse_chapter_page(XSZJ_CHAPTER_PAGE1)
        self.assertEqual(paras, ["“大家快看，苏校花来我们新生晚会了！！”", "虽然这个未婚"])
        self.assertEqual(nxt, "/b/336003/c/1772342?page=2")
        self.assertTrue(cut)  # lone ： marks the screen cut

    def test_stitches_split_sentence_across_screens(self):
        session = _FakeXszjSession(
            [
                ("page=2", XSZJ_CHAPTER_PAGE2),
                ("1772342", XSZJ_CHAPTER_PAGE1),
            ]
        )
        with patch("crawler.xszj.time.sleep"):
            title, paras = xszj.fetch_chapter(
                session, "https://xszj.org/b/336003/c/1772342"
            )
        self.assertEqual(title, "第01章 校花当众送奶茶")
        self.assertEqual(
            paras,
            [
                "“大家快看，苏校花来我们新生晚会了！！”",
                # the two halves around the ： marker are rejoined
                "虽然这个未婚妻张成已经十年没见了，年底就是婚期了。",
                "众人都看呆了。",
            ],
        )

    def test_strips_anticopy_watermark(self):
        # site appends "M.Ι." / "Ｅ" / "." to random paragraphs
        session = _FakeXszjSession(
            [("1772347", XSZJ_CHAPTER_WATERMARK)]
        )
        _, paras = xszj.fetch_chapter(
            session, "https://xszj.org/b/336003/c/1772347"
        )
        self.assertEqual(
            paras,
            [
                "苏校花找的人，居然是新生代表，家世也很好。",
                "这要是接了奶茶，就成了眼中钉肉中刺了。",
                "真是人比人气死人！！",
            ],
        )

    def test_empty_body_raises(self):
        session = _FakeXszjSession(
            [("1772342", "<html><body><div id='booktxt'></div></body></html>")]
        )
        with self.assertRaises(RuntimeError):
            xszj.fetch_chapter(session, "https://xszj.org/b/336003/c/1772342")


FANQIE_TOC_HTML = """
<html><head><meta charset="utf-8"></head><body>
<script>
window.__INITIAL_STATE__={"page":{"bookName":"娇娇青梅一红眼，野戾太子爷轻哄",
"author":"七颗草莓","chapterListWithVolume":[
[{"itemId":"200","needPay":0,"title":"第2章“跟我回家好不好？”","realChapterOrder":"2"},
 {"itemId":"100","needPay":0,"title":"第1章重生","realChapterOrder":"1"}],
[{"itemId":"300","needPay":0,"title":"番外：执念生贪（3）","realChapterOrder":"3"}]]}};
</script></body></html>
"""

FANQIE_READER_HTML = (
    '<html><head><meta charset="utf-8"></head><body><script>\n'
    'window.__INITIAL_STATE__={"reader":{"chapterData":{'
    '"title":"第1章重生","needPay":0,"itemId":"100",'
    '"content":"<p>裴聿\\ue4de\\ue489深黑\\ue44c的\\ue515装，面色却惨\\ue473着。</p>'
    '<p>才回\\ue48a刚搬来这片\\ue3f8墅区。</p>"}}};\n'
    "</script></body></html>"
)


class _FakeFanqieSession:
    def __init__(self, html):
        self.html = html
        self.calls = []

    def get(self, url, timeout=20):
        self.calls.append(url)
        return _FakeResp(text=self.html)


class TestFanqieToc(unittest.TestCase):
    def test_parses_book_meta_and_flattens_volumes_in_order(self):
        title, author, chapters = fanqie.parse_toc_html(FANQIE_TOC_HTML)
        self.assertEqual(title, "娇娇青梅一红眼，野戾太子爷轻哄")
        self.assertEqual(author, "七颗草莓")
        self.assertEqual([c["ordernum"] for c in chapters], ["1", "2", "3"])
        # source title runs together: 第1章 + 重生 -> insert the missing space
        self.assertEqual(chapters[0]["title"], "第1章 重生")
        self.assertEqual(chapters[1]["title"], "第2章 “跟我回家好不好？”")
        # 番外 has no 第N章 prefix -> untouched
        self.assertEqual(chapters[2]["title"], "番外：执念生贪（3）")
        self.assertEqual(chapters[0]["url"], "https://fanqienovel.com/reader/100")

    def test_title_spacing_handles_titles_without_zhang(self):
        # 5 real fanqie titles omit 章 ("第38这是...") - still split number/title
        self.assertEqual(
            fanqie._norm_title("第38这是他亲手养大的女孩"), "第38 这是他亲手养大的女孩"
        )
        self.assertEqual(fanqie._norm_title("第1章重生"), "第1章 重生")
        # source already spaced after 章 - must not become "第77 章 ..."
        self.assertEqual(
            fanqie._norm_title("第77章 沈梨初是裴聿的眼珠子"), "第77章 沈梨初是裴聿的眼珠子"
        )
        self.assertEqual(fanqie._norm_title("第108章"), "第108章")
        self.assertEqual(fanqie._norm_title("番外：执念生贪（3）"), "番外：执念生贪（3）")
        self.assertEqual(
            fanqie._norm_title("if线：梨梨在Daddy身边长大（1）"),
            "if线：梨梨在Daddy身边长大（1）",
        )

    def test_fetch_toc_uses_page_endpoint(self):
        session = _FakeFanqieSession(FANQIE_TOC_HTML)
        title, author, chapters = fanqie.fetch_toc(session, "7478055015341378585")
        self.assertEqual(len(chapters), 3)
        self.assertEqual(
            session.calls[0], "https://fanqienovel.com/page/7478055015341378585"
        )


class TestFanqieChapter(unittest.TestCase):
    def setUp(self):
        fanqie._archive.clear()

    def test_decodes_pua_font_and_splits_paragraphs(self):
        title, paras = fanqie.parse_chapter_html(FANQIE_READER_HTML)
        self.assertEqual(title, "第1章 重生")
        self.assertEqual(
            paras,
            [
                "裴聿一身深黑色的西装，面色却惨白着。",
                "才回国刚搬来这片别墅区。",
            ],
        )
        # map really loaded: no PUA survived
        for p in paras:
            self.assertFalse(any("\ue000" <= ch <= "\uf8ff" for ch in p))

    def test_unknown_pua_is_kept_not_crashed(self):
        html = FANQIE_READER_HTML.replace("\\ue4de", "\\uf010")
        fanqie._unknown_pua.clear()
        _, paras = fanqie.parse_chapter_html(html)
        self.assertIn("\uf010", paras[0])

    def test_paid_chapter_raises(self):
        html = FANQIE_READER_HTML.replace('"needPay":0', '"needPay":1')
        with self.assertRaises(RuntimeError):
            fanqie.parse_chapter_html(html)

    def test_fetch_chapter_reads_reader_endpoint(self):
        session = _FakeFanqieSession(FANQIE_READER_HTML)
        title, paras = fanqie.fetch_chapter(
            session, "https://fanqienovel.com/reader/100"
        )
        self.assertEqual(title, "第1章 重生")
        self.assertEqual(len(paras), 2)
        self.assertEqual(session.calls, ["https://fanqienovel.com/reader/100"])


FANQIE_ARCHIVE_META = {"files": [{"name": "book-full.txt"}]}

FANQIE_ARCHIVE_TXT = (
    "别惹他的乖乖小甜梨\n原名：娇娇青梅一红眼，野戾太子爷轻哄\n作者：七颗草莓\n\n"
    "第1章重生\n裴聿一身深黑色的西装。\n面色却惨白着。\n\n"
    "第2章“跟我回家好不好？”\n她问他愿不愿意回家。\n"
)

FANQIE_ARCHIVE_TITLES = ["第1章 重生", "第2章 “跟我回家好不好？”", "第3章 缺失"]


class _FakeFanqieRoutes:
    """GET keyed by url substring: (needle, text, json_payload)."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, timeout=20):
        self.calls.append(url)
        for needle, text, payload in self.routes:
            if needle in url:
                return _FakeResp(text=text, payload=payload)
        raise AssertionError(f"unexpected url {url}")


class TestFanqieArchive(unittest.TestCase):
    def setUp(self):
        self._old_min = fanqie.ARCHIVE_MIN_BYTES
        fanqie.ARCHIVE_MIN_BYTES = 10  # tiny fixtures

    def tearDown(self):
        fanqie.ARCHIVE_MIN_BYTES = self._old_min

    def test_split_matches_headers_across_title_spacing(self):
        secs = fanqie.split_archive_text(FANQIE_ARCHIVE_TXT, FANQIE_ARCHIVE_TITLES)
        self.assertEqual(secs["第1章 重生"], ["裴聿一身深黑色的西装。", "面色却惨白着。"])
        self.assertEqual(secs["第2章 “跟我回家好不好？”"], ["她问他愿不愿意回家。"])
        self.assertNotIn("第3章 缺失", secs)  # preamble must not leak into a body

    def test_fetch_prefers_archive_and_skips_reader(self):
        session = _FakeFanqieRoutes(
            [
                ("/page/", FANQIE_TOC_HTML, None),
                ("archive.org/metadata", "", FANQIE_ARCHIVE_META),
                ("archive.org/download", FANQIE_ARCHIVE_TXT, None),
            ]
        )
        _, _, chapters = fanqie.fetch_toc(session, "7478055015341378585")
        title, paras = fanqie.fetch_chapter(session, chapters[0]["url"])
        self.assertIsNone(title)  # crawl.py falls back to the TOC title
        self.assertEqual(paras, ["裴聿一身深黑色的西装。", "面色却惨白着。"])
        # archive hit -> the reader page was never requested
        self.assertFalse(any("/reader/" in u for u in session.calls))

    def test_reader_page_fallback_when_archive_misses(self):
        session = _FakeFanqieRoutes(
            [
                ("/page/", FANQIE_TOC_HTML, None),
                ("archive.org/metadata", "", FANQIE_ARCHIVE_META),
                ("archive.org/download", FANQIE_ARCHIVE_TXT, None),
                ("/reader/", FANQIE_READER_HTML, None),
            ]
        )
        fanqie.fetch_toc(session, "7478055015341378585")
        # ch3 has no archive body -> reader page path (PUA decode)
        title, paras = fanqie.fetch_chapter(session, "https://fanqienovel.com/reader/300")
        self.assertEqual(title, "第1章 重生")
        self.assertEqual(paras[0], "裴聿一身深黑色的西装，面色却惨白着。")

    def test_reader_truncation_warns_but_returns(self):
        html = FANQIE_READER_HTML.replace(
            '"content"', '"chapterWordNumber":9000,"content"'
        )
        title, paras = fanqie.parse_chapter_html(html)
        self.assertEqual(len(paras), 2)  # kept; a warning is printed


if __name__ == "__main__":
    unittest.main()
