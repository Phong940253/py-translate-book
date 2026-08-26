"""Offline unit tests for translator.epub_utils (no AI calls).

Covers:
  TC-10 : save_epub injects new image items into the OPF manifest so readers
          can render illustrations that were added via EpubItem.
"""
import io
import os
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ebooklib import epub

from translator.epub_utils import (
    _infer_media_type,
    _inject_manifest_entries,
    save_epub,
)


class TestInferMediaType(unittest.TestCase):
    def test_jpg(self):
        self.assertEqual(_infer_media_type("images/ch-1.jpg"), "image/jpeg")
        self.assertEqual(_infer_media_type("img.PNG"), "image/png")
    def test_svg(self):
        self.assertEqual(_infer_media_type("foo.svg"), "image/svg+xml")
    def test_unknown_fallback(self):
        self.assertEqual(_infer_media_type("file.xyz"), "application/octet-stream")


class TestInjectManifestEntries(unittest.TestCase):
    OPF = (
        '<package><manifest>\n'
        '  <item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/>\n'
        '</manifest></package>'
    )

    def test_injects_image(self):
        additions = {"images/generated/ch1-x.jpg": b"\xff\xd8"}
        out = _inject_manifest_entries(self.OPF, additions)
        self.assertIn("images/generated/ch1-x.jpg", out)
        self.assertIn("image/jpeg", out)
        self.assertEqual(out.count("<item "), 2)

    def test_no_duplicates(self):
        additions = {"ch1.xhtml": b""}
        out = _inject_manifest_entries(self.OPF, additions)
        self.assertEqual(out.count("<item "), 1)  # existing, not re-added


class TestSaveEpubWritesManifest(unittest.TestCase):
    def test_added_image_in_opf(self):
        # Create a minimal EPUB in memory as source_path.
        source_book = epub.EpubBook()
        source_book.set_identifier("test-book-001")
        source_book.set_title("Test")
        source_book.set_language("vi")
        ch = epub.EpubHtml(title="Ch1", file_name="ch1.xhtml", lang="vi")
        ch.content = b"<html><body><p>Original text</p></body></html>"
        source_book.add_item(ch)
        source_book.toc = [ch]
        source_book.spine = ["nav", ch]
        source_book.add_item(epub.EpubNcx())
        source_book.add_item(epub.EpubNav())

        source_io = io.BytesIO()
        epub.write_epub(source_io, source_book, {})
        source_bytes = source_io.getvalue()

        with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as f:
            f.write(source_bytes)
            source_path = f.name

        try:
            # Now create a modified book with an image item added.
            mod_book = epub.read_epub(source_path, {"ignore_ncx": True})
            img_content = b"\xff\xd8\xff\xe0" + b"\x00" * 12
            img = epub.EpubItem(
                uid="img-01",
                file_name="images/generated/ch1-x.jpg",
                media_type="image/jpeg",
                content=img_content,
            )
            mod_book.add_item(img)

            out_path = source_path + "-out.epub"
            save_epub(mod_book, out_path, source_path=source_path)

            with zipfile.ZipFile(out_path, "r") as zf:
                opf_names = [n for n in zf.namelist() if n.lower().endswith(".opf")]
                self.assertEqual(len(opf_names), 1, zf.namelist())
                opf = zf.read(opf_names[0]).decode("utf-8")
                self.assertIn("images/generated/ch1-x.jpg", opf)
                self.assertIn("image/jpeg", opf)
                # image file is actually present
                self.assertIn("images/generated/ch1-x.jpg", zf.namelist())

            os.unlink(out_path)
        finally:
            os.unlink(source_path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
