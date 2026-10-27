"""Offline tests for the WebUI v2 background-music library/import API."""

from __future__ import annotations

import os
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask

from webui import music


class FakeYoutubeDL:
    """Minimal yt-dlp stand-in: writes an mp3-like file and reports progress."""

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def extract_info(self, _url, download=True):
        assert download is True
        path = self.opts["outtmpl"].replace("%(id)s", "fakeid").replace("%(ext)s", "mp3")
        with open(path, "wb") as fh:
            fh.write(b"ID3" + b"x" * 2048)
        hook = self.opts["progress_hooks"][0]
        hook({"status": "downloading", "downloaded_bytes": 1024, "total_bytes": 2048, "speed": 2048, "eta": 1})
        hook({"status": "finished"})
        return {
            "id": "fakeid",
            "title": "Bài thử nghiệm",
            "artist": "Nghệ sĩ thử",
            "duration": 12.5,
        }


class SlowFakeYoutubeDL(FakeYoutubeDL):
    """Reports progress for long enough that the test can cancel it."""

    def extract_info(self, _url, download=True):
        hook = self.opts["progress_hooks"][0]
        for step in range(200):
            hook({
                "status": "downloading",
                "downloaded_bytes": step * 1024,
                "total_bytes": 200 * 1024,
                "speed": 1024,
                "eta": 200 - step,
            })
            time.sleep(0.01)
        return {"id": "slow", "title": "Slow"}


class MusicImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pytb-music-test-")
        self.addCleanup(self.tmp.cleanup)
        self.patches = [
            mock.patch.object(music, "MUSIC_DIR", self.tmp.name),
            mock.patch.object(music, "INDEX_FILE", os.path.join(self.tmp.name, "library.json")),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)
        music._import_jobs.clear()
        music._active_import = None
        self.addCleanup(music._import_jobs.clear)

    def test_url_allowlist(self):
        self.assertEqual(
            music.validate_youtube_url("https://www.youtube.com/watch?v=abc123"),
            "https://www.youtube.com/watch?v=abc123",
        )
        self.assertEqual(
            music.validate_youtube_url("https://youtu.be/abc123"),
            "https://youtu.be/abc123",
        )
        for bad in (
            "javascript:alert(1)",
            "https://youtube.com.evil.example/watch?v=x",
            "https://example.com/watch?v=x",
            "https://youtube.com/",
        ):
            with self.assertRaises(ValueError, msg=bad):
                music.validate_youtube_url(bad)

    def test_legacy_yt_filename_title(self):
        name = (
            "YTSave_YouTube_Media_V85iC-i9Id4_"
            "Moon-s-Voice-Ends-Heart-s-Light-Shines_009_128k.mp3"
        )
        self.assertEqual(music._display_title(name), "Moon-s-Voice-Ends-Heart-s-Light-Shines")

    def test_import_then_stream_and_delete(self):
        fake_yt_dlp = types.SimpleNamespace(YoutubeDL=FakeYoutubeDL)
        with mock.patch.dict(sys.modules, {"yt_dlp": fake_yt_dlp}):
            job = music.start_import("https://youtu.be/fakeid")
            deadline = time.time() + 5
            while time.time() < deadline:
                current = music.import_job(job["id"])
                if current and current["state"] in {"done", "error", "cancelled"}:
                    break
                time.sleep(0.02)
            else:
                self.fail("import job did not finish")

        self.assertEqual(current["state"], "done", current)
        self.assertEqual(current["track"]["title"], "Bài thử nghiệm")
        self.assertEqual(current["track"]["artist"], "Nghệ sĩ thử")
        self.assertNotIn("_cancel", current)

        tracks = music.list_tracks()
        self.assertEqual(len(tracks), 1)
        track = tracks[0]
        self.assertEqual(track["title"], "Bài thử nghiệm")
        self.assertTrue(os.path.isfile(os.path.join(self.tmp.name, track["id"] + ".mp3")))

        app = Flask(__name__)
        app.register_blueprint(music.music_bp)
        client = app.test_client()

        listing = client.get("/api/v2/music")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.json["tracks"][0]["id"], track["id"])

        stream = client.get(f"/api/v2/music/file/{track['id']}")
        self.assertEqual(stream.status_code, 200)
        self.assertTrue(stream.data.startswith(b"ID3"))
        stream.close()

        deleted = client.delete(f"/api/v2/music/{track['id']}")
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(music.list_tracks(), [])
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, track["id"] + ".mp3")))

    def test_rejects_invalid_url(self):
        app = Flask(__name__)
        app.register_blueprint(music.music_bp)
        client = app.test_client()
        response = client.post("/api/v2/music/import", json={"url": "not a url"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("URL", response.json["error"])

    def test_cancel_slow_import_leaves_no_track(self):
        fake_yt_dlp = types.SimpleNamespace(YoutubeDL=SlowFakeYoutubeDL)
        with mock.patch.dict(sys.modules, {"yt_dlp": fake_yt_dlp}):
            job = music.start_import("https://youtu.be/slow")
            deadline = time.time() + 3
            while time.time() < deadline:
                current = music.import_job(job["id"])
                if current and current["state"] == "downloading":
                    break
                time.sleep(0.02)
            else:
                self.fail("slow import never started downloading")
            music.cancel_import(job["id"])
            deadline = time.time() + 3
            while time.time() < deadline:
                current = music.import_job(job["id"])
                if current and current["state"] in {"cancelled", "error"}:
                    break
                time.sleep(0.02)
            else:
                self.fail("cancelled import did not stop")
        self.assertEqual(current["state"], "cancelled", current)
        self.assertEqual(music.list_tracks(), [])


if __name__ == "__main__":
    unittest.main()
