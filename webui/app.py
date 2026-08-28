"""Flask app for managing py-translate-book translations (local-only).

Run:  python -m webui.app   (or: python webui/app.py)
Open: http://127.0.0.1:5000
"""

import io
import json
import os
import sys
import time

# Allow running as a bare script (python webui/app.py): ensure the project root
# is importable so `from webui.xxx import ...` resolves regardless of CWD.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import (
    Flask,
    abort,
    render_template,
    request,
    redirect,
    url_for,
    Response,
    send_file,
)

from webui.jobs import JobRegistry
import webui.core_runner as core_runner
from webui.config_store import load_config, mask_config, save_config_text
from webui.books import list_epubs, preview_chapter, chapter_count, get_cover

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.yaml")
ENGINES = ["openai", "gemini", "webai"]

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024
registry = JobRegistry()


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------


@app.route("/")
def index():
    jobs = registry.all()
    books = list_epubs(with_meta=True)
    return render_template("dashboard.html", jobs=jobs, books=books, engines=ENGINES)


@app.route("/jobs/new", methods=["GET", "POST"])
def job_new():
    if request.method == "POST":
        p = request.form
        params = {
            "engine": p.get("engine"),
            "input": p.get("input"),
            "output": p.get("output")
            or ((p.get("input") or "") + ".translated.epub"),
            "config_path": p.get("config_path") or DEFAULT_CONFIG_PATH,
            "from_chapter": int(p["from_chapter"]) if p.get("from_chapter") else None,
            "to_chapter": int(p["to_chapter"]) if p.get("to_chapter") else None,
            "from_lang": p.get("from_lang", "EN"),
            "to_lang": p.get("to_lang", "VI"),
            "description": p.get("description") or None,
            "openai_batch": bool(p.get("openai_batch")),
            "reset_checkpoint": bool(p.get("reset_checkpoint")),
            "disable_resume": bool(p.get("disable_resume")),
        }
        if not params["engine"] or not params["input"]:
            return "engine and input are required", 400
        job = registry.create(params)
        core_runner.start_job(registry, job)
        return redirect(url_for("job_view", job_id=job.id))

    books = list_epubs()
    config = load_config()
    return render_template(
        "job_new.html",
        engines=ENGINES,
        books=books,
        config=config,
        default_config=DEFAULT_CONFIG_PATH,
    )


@app.route("/jobs/<job_id>")
def job_view(job_id):
    job = registry.get(job_id)
    if not job:
        abort(404)
    return render_template("job_view.html", job=job)


@app.route("/jobs/<job_id>/stop", methods=["POST"])
def job_stop(job_id):
    job = registry.get(job_id)
    if not job:
        abort(404)
    # Cooperative: persist the stop flag (the worker finishes the current
    # chapter cleanly). Then hard-kill a subprocess worker for an instant stop.
    registry.request_stop(job)
    proc = core_runner.request_hard_stop(job_id)
    if proc is not None:
        # A real subprocess worker was terminated; its finally block never
        # runs, so finalize the status from the persisted JSON (authoritative
        # for process workers). Thread workers set their own status
        # cooperatively, so we must NOT override theirs.
        proc.join(timeout=2)
        core_runner._PROCS.pop(job_id, None)
        meta = registry.load_meta(job_id) or {}
        if meta.get("status") in ("running", "queued"):
            job = registry.get(job_id)
            if job is not None:
                job.status = "stopped"
                registry.save(job)
    return redirect(url_for("job_view", job_id=job_id))


@app.route("/jobs/<job_id>/resume", methods=["POST"])
def job_resume(job_id):
    job = registry.get(job_id)
    if not job:
        abort(404)
    # Only continue jobs that are not actively running and not freshly done.
    if job.status not in ("stopped", "interrupted", "error"):
        return redirect(url_for("job_view", job_id=job_id))
    # Reset per-run state so this run starts clean, but keep the same job id
    # (the user wants to "continue this job", not spawn a new one).
    job.stop_requested = False
    job.error = None
    job.result = None
    job.progress = {}
    job.events = []
    # Continue from the existing checkpoint instead of wiping it.
    params = dict(job.params)
    params["reset_checkpoint"] = False
    params["disable_resume"] = False
    job.params = params
    job.status = "running"
    registry.save(job)
    core_runner.start_job(registry, job)
    return redirect(url_for("job_view", job_id=job_id))


@app.route("/jobs/<job_id>/stream")
def job_stream(job_id):
    job = registry.get(job_id)
    if not job:
        abort(404)

    log_path = registry._log_path(job_id)

    def gen():
        last = 0
        while True:
            # Read state from disk so it works for both the thread worker (which
            # shares memory but also persists) and a subprocess worker (which
            # only shares the JSON / .log files).
            meta = registry.load_meta(job_id) or {}
            status = meta.get("status")
            prog = meta.get("progress") or {}
            result = meta.get("result")
            error = meta.get("error")

            lines = []
            if os.path.exists(log_path):
                try:
                    with open(log_path, encoding="utf-8") as f:
                        cur = f.read().splitlines()
                except Exception:  # noqa: BLE001
                    cur = []
                lines = cur[last:]
                last = len(cur)

            for line in lines:
                yield "data: " + json.dumps(
                    {"type": "log", "line": line}, ensure_ascii=False
                ) + "\n\n"
            payload = {
                "type": "progress",
                "progress": prog,
                "status": status,
                "result": result,
                "error": error,
            }
            yield "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
            if status in ("done", "error", "stopped", "interrupted"):
                yield "data: " + json.dumps(
                    {"type": "end", "status": status}, ensure_ascii=False
                ) + "\n\n"
                break
            time.sleep(0.7)

    return Response(gen(), mimetype="text/event-stream")


# --------------------------------------------------------------------------
# Books library
# --------------------------------------------------------------------------


@app.route("/books")
def books():
    items = list_epubs(with_meta=True)
    for it in items:
        it["chapters"] = chapter_count(it["path"])
    return render_template("books.html", books=items)


@app.route("/books/preview")
def book_preview():
    path = request.args.get("path", "")
    chapter = int(request.args.get("chapter", "1"))
    max_chars = int(request.args.get("max_chars", "4000"))
    if not os.path.isfile(path) or not path.startswith(PROJECT_ROOT):
        abort(400)
    try:
        text, total = preview_chapter(path, chapter, max_chars)
    except ValueError as e:
        return str(e), 400
    return {
        "path": path,
        "chapter": chapter,
        "total": total,
        "shown": len(text),
        "text": text,
    }


@app.route("/books/cover")
def book_cover():
    path = request.args.get("path", "")
    if not path.startswith(PROJECT_ROOT) or not os.path.isfile(path):
        abort(400)
    cover = get_cover(path)
    if not cover:
        abort(404)
    data, mime = cover
    return send_file(io.BytesIO(data), mimetype=mime)


# --------------------------------------------------------------------------
# Config editor
# --------------------------------------------------------------------------


@app.route("/config", methods=["GET", "POST"])
def config_view():
    if request.method == "POST":
        text = request.form.get("config_text", "")
        try:
            save_config_text(text)
        except Exception as e:  # noqa: BLE001
            return render_template(
                "config.html", config_text=text, error=str(e), engines=ENGINES
            )
        return redirect(url_for("config_view"))
    cfg = load_config()
    return render_template(
        "config.html", config_text=yaml_dump(mask_config(cfg)), error=None, engines=ENGINES
    )


def yaml_dump(d):
    import yaml

    return yaml.safe_dump(d, allow_unicode=True, sort_keys=False)


@app.route("/config/test-discord", methods=["POST"])
def config_test_discord():
    from translator.discord_notifier import DiscordNotifier

    cfg = load_config()
    discord_cfg = cfg.get("discord", {}) if isinstance(cfg, dict) else {}
    url = discord_cfg.get("webhook_url", "")
    mention = str(discord_cfg.get("mention_user_id", "")).strip() or None
    ok = DiscordNotifier.send_test_message(webhook_url=url, mention_user_id=mention)
    return {"success": bool(ok)}


# --------------------------------------------------------------------------
# Illustrations gallery
# --------------------------------------------------------------------------


@app.route("/illustrations")
def illustrations():
    img_dir = os.path.join(PROJECT_ROOT, "images", "generated")
    images = []
    if os.path.isdir(img_dir):
        for fn in sorted(os.listdir(img_dir)):
            if fn.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".svg")):
                images.append(fn)
    return render_template("illustrations.html", images=images, img_dir=img_dir)


@app.route("/images/<path:filename>")
def serve_image(filename):
    img_dir = os.path.join(PROJECT_ROOT, "images", "generated")
    path = os.path.join(img_dir, filename)
    if not os.path.isfile(path) or not path.startswith(img_dir):
        abort(404)
    return send_file(path)


# --------------------------------------------------------------------------
# Output download
# --------------------------------------------------------------------------


@app.route("/download")
def download():
    path = request.args.get("path", "")
    if not path.startswith(PROJECT_ROOT) or not os.path.isfile(path):
        abort(400)
    return send_file(path, as_attachment=True)


@app.route("/health")
def health():
    return {"status": "ok", "jobs": len(registry.jobs)}


if __name__ == "__main__":
    print("Web UI running at http://127.0.0.1:5000  (local only)")
    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)
