"""Background-music library and YouTube import jobs for WebUI v2.

The legacy reader only exposes the files that happen to live in ``music/``.
This module keeps that folder but adds a small metadata index, stable track
ids, safe streaming URLs, and a cancellable yt-dlp import job.  It is mounted
by ``webui.app_v2`` under ``/api/v2/music`` so the running v1 server on port
5000 is never touched.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from flask import Blueprint, abort, jsonify, request, send_file

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MUSIC_DIR = os.path.join(PROJECT_ROOT, "music")
INDEX_FILE = os.path.join(MUSIC_DIR, "library.json")
MUSIC_EXTS = (".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".wav", ".flac")
ALLOWED_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtube-nocookie.com",
}
MAX_DURATION = 2 * 60 * 60       # 2 hours
MAX_BYTES = 250 * 1024 * 1024    # 250 MB

_lock = threading.RLock()
_active_import: str | None = None
_import_jobs: dict[str, dict] = {}

music_bp = Blueprint("music_v2", __name__, url_prefix="/api/v2/music")


# ---------------------------------------------------------------------------
# library index
# ---------------------------------------------------------------------------


def _ensure_dir() -> None:
    os.makedirs(MUSIC_DIR, exist_ok=True)


def _now() -> int:
    return int(time.time())


def _clean_text(value: object, fallback: str = "") -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text or fallback


def _display_title(filename: str) -> str:
    """Best-effort title for files copied into music/ by hand."""
    stem = Path(filename).stem
    # YTSave_YouTube_Media_<id>_<title>_... → keep the readable tail when we
    # can, otherwise the UI still truncates it safely.
    if stem.lower().startswith("ytsave"):
        stem = re.sub(r"^YTSave(?:\.to)?_YouTube_Media_[A-Za-z0-9_-]{11}_", "", stem)
    stem = re.sub(r"_(?:\d{3})_(?:128k|vbr)$", "", stem, flags=re.IGNORECASE)
    stem = stem.replace("_", " ").replace(".", " ")
    return _clean_text(stem, Path(filename).name)


def _probe_duration(path: str) -> float | None:
    exe = shutil.which("ffprobe")
    if not exe:
        return None
    try:
        proc = subprocess.run(
            [exe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=12, check=False,
        )
        value = float((proc.stdout or "").strip())
        return round(value, 2) if value > 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _track_id(filename: str) -> str:
    return hashlib.sha1(filename.encode("utf-8")).hexdigest()[:16]


def _read_index() -> dict:
    try:
        with open(INDEX_FILE, encoding="utf-8") as fh:
            payload = json.load(fh)
        if isinstance(payload, dict) and isinstance(payload.get("tracks"), list):
            return payload
    except (OSError, ValueError, TypeError):
        pass
    return {"version": 1, "tracks": []}


def _save_index(payload: dict) -> None:
    _ensure_dir()
    tmp = INDEX_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, INDEX_FILE)


def _scan_files() -> list[str]:
    _ensure_dir()
    out = []
    for name in os.listdir(MUSIC_DIR):
        path = os.path.join(MUSIC_DIR, name)
        if os.path.isfile(path) and name.lower().endswith(MUSIC_EXTS):
            out.append(name)
    return sorted(out)


def _sync_index() -> dict:
    """Merge files dropped into music/ with the persisted metadata index."""
    with _lock:
        payload = _read_index()
        by_file = {
            str(t.get("file")): t
            for t in payload.get("tracks", [])
            if isinstance(t, dict) and t.get("file")
        }
        changed = False
        files = _scan_files()
        present = set(files)
        for name in files:
            if name in by_file:
                continue
            path = os.path.join(MUSIC_DIR, name)
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            by_file[name] = {
                "id": _track_id(name),
                "file": name,
                "title": _display_title(name),
                "artist": "",
                "duration": _probe_duration(path),
                "source_url": None,
                "added_at": _now(),
                "size": size,
            }
            changed = True
        for name in list(by_file):
            if name not in present:
                del by_file[name]
                changed = True
        tracks = sorted(
            by_file.values(),
            key=lambda t: (-int(t.get("added_at") or 0), str(t.get("title") or "").lower()),
        )
        if changed or not os.path.exists(INDEX_FILE):
            payload = {"version": 1, "tracks": tracks}
            _save_index(payload)
        else:
            payload["tracks"] = tracks
        return payload


def list_tracks() -> list[dict]:
    return [_public_track(t) for t in _sync_index()["tracks"]]


def _public_track(track: dict) -> dict:
    tid = str(track.get("id") or "")
    return {
        "id": tid,
        "title": _clean_text(track.get("title"), "Không tên"),
        "artist": _clean_text(track.get("artist")),
        "duration": track.get("duration"),
        "source_url": track.get("source_url"),
        "added_at": track.get("added_at"),
        "size": track.get("size"),
        "url": f"/api/v2/music/file/{tid}",
    }


def _find_track(track_id: str) -> tuple[dict, str] | None:
    for track in _sync_index()["tracks"]:
        if track.get("id") == track_id:
            path = os.path.join(MUSIC_DIR, str(track.get("file") or ""))
            if os.path.isfile(path):
                return track, path
    return None


def delete_track(track_id: str) -> bool:
    with _lock:
        payload = _sync_index()
        keep = []
        removed = None
        for track in payload["tracks"]:
            if track.get("id") == track_id:
                removed = track
            else:
                keep.append(track)
        if removed is None:
            return False
        path = os.path.join(MUSIC_DIR, str(removed.get("file") or ""))
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError as exc:
            raise RuntimeError(f"Không xoá được file: {exc}") from exc
        payload["tracks"] = keep
        _save_index(payload)
        return True


# ---------------------------------------------------------------------------
# yt-dlp import
# ---------------------------------------------------------------------------


class ImportCancelled(RuntimeError):
    pass


def validate_youtube_url(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        raise ValueError("Dán URL YouTube trước đã.")
    if len(raw) > 2048:
        raise ValueError("URL quá dài.")
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Chỉ chấp nhận URL http/https.")
    if parsed.username or parsed.password:
        raise ValueError("URL không được chứa thông tin đăng nhập.")
    host = (parsed.hostname or "").lower().rstrip(".")
    if host not in ALLOWED_HOSTS:
        raise ValueError("Chỉ hỗ trợ link YouTube / YouTube Music / youtu.be.")
    if parsed.port not in (None, 80, 443):
        raise ValueError("Port trong URL không được hỗ trợ.")
    if not parsed.path or parsed.path == "/":
        raise ValueError("URL không trỏ tới video nào.")
    return raw


def _job_public(job: dict) -> dict:
    return {k: v for k, v in job.items() if k not in {"_cancel"}}

def _set_job(job_id: str, **updates) -> None:
    with _lock:
        job = _import_jobs.get(job_id)
        if not job:
            return
        job.update(updates)
        job["updated_at"] = _now()


def start_import(url: str) -> dict:
    global _active_import
    clean = validate_youtube_url(url)
    with _lock:
        if _active_import:
            active = _import_jobs.get(_active_import)
            if active and active.get("state") in ("queued", "fetching", "downloading", "processing", "cancelling"):
                raise RuntimeError("Đang có một file đang tải. Chờ xong hoặc huỷ trước.")
        job_id = uuid.uuid4().hex[:12]
        job = {
            "id": job_id,
            "url": clean,
            "state": "queued",
            "progress": 0.0,
            "downloaded_bytes": 0,
            "total_bytes": 0,
            "speed": None,
            "eta": None,
            "title": "",
            "error": None,
            "track": None,
            "created_at": _now(),
            "updated_at": _now(),
            "_cancel": threading.Event(),
        }
        _import_jobs[job_id] = job
        _active_import = job_id
    threading.Thread(target=_run_import, args=(job_id, clean), daemon=True).start()
    return _job_public(job)


def _run_import(job_id: str, url: str) -> None:
    global _active_import
    tmpdir = None
    try:
        try:
            import yt_dlp  # type: ignore
        except ImportError as exc:
            raise RuntimeError("yt-dlp chưa được cài. Chạy: pip install yt-dlp") from exc

        _set_job(job_id, state="fetching", progress=0.0)
        _ensure_dir()
        tmpdir = tempfile.mkdtemp(prefix=".import-", dir=MUSIC_DIR)
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("Chưa có ffmpeg trên PATH; cần ffmpeg để chuyển audio sang MP3.")

        def cancelled() -> bool:
            with _lock:
                job = _import_jobs.get(job_id)
                return bool(job and job["_cancel"].is_set())

        def hook(data: dict) -> None:
            if cancelled():
                raise ImportCancelled("Đã huỷ.")
            status = data.get("status")
            if status == "downloading":
                total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
                done = data.get("downloaded_bytes") or 0
                pct = min(99.0, (done * 100.0 / total) if total else 0.0)
                _set_job(
                    job_id,
                    state="downloading",
                    progress=round(pct, 1),
                    downloaded_bytes=int(done),
                    total_bytes=int(total),
                    speed=data.get("speed"),
                    eta=data.get("eta"),
                )
            elif status == "finished":
                _set_job(job_id, state="processing", progress=100.0, eta=0)

        def match_filter(info: dict, *, incomplete: bool = False):
            duration = info.get("duration")
            if duration and float(duration) > MAX_DURATION:
                return "Video dài hơn 2 giờ."
            filesize = info.get("filesize") or info.get("filesize_approx")
            if filesize and float(filesize) > MAX_BYTES:
                return "File lớn hơn 250 MB."
            return None

        outtmpl = os.path.join(tmpdir, "%(id)s.%(ext)s")
        opts = {
            "format": "bestaudio/best",
            "outtmpl": outtmpl,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "progress_hooks": [hook],
            "match_filter": match_filter,
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
        }
        opts["ffmpeg_location"] = ffmpeg

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)

        if cancelled():
            raise ImportCancelled("Đã huỷ.")
        if not isinstance(info, dict):
            raise RuntimeError("Không đọc được thông tin video.")

        files = []
        for name in os.listdir(tmpdir):
            path = os.path.join(tmpdir, name)
            if os.path.isfile(path) and name.lower().endswith(MUSIC_EXTS):
                files.append(path)
        if not files:
            raise RuntimeError("Không tìm thấy file audio sau khi tải.")
        source = max(files, key=os.path.getsize)
        duration = info.get("duration")
        if duration and float(duration) > MAX_DURATION:
            raise RuntimeError("Video dài hơn 2 giờ, không tải.")
        if os.path.getsize(source) > MAX_BYTES:
            raise RuntimeError("File audio lớn hơn 250 MB, không tải.")
        ext = os.path.splitext(source)[1].lower() or ".mp3"

        track_id = hashlib.sha1(
            (url + "|" + str(info.get("id") or "") + "|" + str(time.time())).encode("utf-8")
        ).hexdigest()[:16]
        filename = f"{track_id}{ext}"
        dest = os.path.join(MUSIC_DIR, filename)
        os.replace(source, dest)

        track = {
            "id": track_id,
            "file": filename,
            "title": _clean_text(info.get("title"), _display_title(filename)),
            "artist": _clean_text(info.get("artist") or info.get("uploader") or info.get("channel")),
            "duration": round(float(duration), 2) if duration else None,
            "source_url": url,
            "added_at": _now(),
            "size": os.path.getsize(dest),
        }
        with _lock:
            payload = _read_index()
            payload.setdefault("tracks", []).append(track)
            _save_index(payload)
        _set_job(job_id, state="done", progress=100.0, eta=0, track=_public_track(track))
    except ImportCancelled as exc:
        _set_job(job_id, state="cancelled", error=str(exc))
    except Exception as exc:  # noqa: BLE001 — surface a useful message to the UI
        _set_job(job_id, state="error", error=str(exc) or type(exc).__name__)
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
        with _lock:
            if _active_import == job_id:
                _active_import = None


def cancel_import(job_id: str) -> dict | None:
    with _lock:
        job = _import_jobs.get(job_id)
        if not job:
            return None
        if job.get("state") in ("done", "error", "cancelled"):
            return _job_public(job)
        job["_cancel"].set()
        job["state"] = "cancelling"
        job["updated_at"] = _now()
        return _job_public(job)


def import_job(job_id: str) -> dict | None:
    with _lock:
        job = _import_jobs.get(job_id)
        return _job_public(job) if job else None


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------


@music_bp.get("")
def api_music_list_v2():
    with _lock:
        active = _job_public(_import_jobs[_active_import]) if _active_import else None
    return jsonify({"tracks": list_tracks(), "import": active})


@music_bp.post("/import")
def api_music_import():
    payload = request.get_json(silent=True) or {}
    try:
        job = start_import(str(payload.get("url") or ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 409
    return jsonify({"job": job}), 202


@music_bp.get("/import/<job_id>")
def api_music_import_status(job_id: str):
    job = import_job(job_id)
    if not job:
        return jsonify({"error": "Không tìm thấy job."}), 404
    return jsonify({"job": job})


@music_bp.delete("/import/<job_id>")
def api_music_import_cancel(job_id: str):
    job = cancel_import(job_id)
    if not job:
        return jsonify({"error": "Không tìm thấy job."}), 404
    return jsonify({"job": job}), 202


@music_bp.get("/file/<track_id>")
def api_music_file_v2(track_id: str):
    found = _find_track(track_id)
    if not found:
        abort(404)
    _track, path = found
    return send_file(path, conditional=True)


@music_bp.delete("/<track_id>")
def api_music_delete(track_id: str):
    try:
        removed = delete_track(track_id)
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 409
    if not removed:
        return jsonify({"error": "Không tìm thấy track."}), 404
    return jsonify({"ok": True})
