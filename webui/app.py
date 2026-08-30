"""Flask app for managing py-translate-book translations (local-only).

Run:  python -m webui.app   (or: python webui/app.py)
Open: http://127.0.0.1:5000
"""

import copy
import io
import json
import os
import re
import sys
import time

# Allow running as a bare script (python webui/app.py): ensure the project root
# is importable so `from webui.xxx import ...` resolves regardless of CWD.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import (
    Flask,
    abort,
    jsonify,
    render_template,
    request,
    redirect,
    url_for,
    Response,
    send_file,
)

import webui.core_runner as core_runner
import webui.config_schema as config_schema
from webui.jobs import JobRegistry
from webui.config_store import load_config, mask_config, save_config_text, save_config_dict
from webui.books import (
    build_library,
    list_epubs,
    preview_chapter,
    get_cover,
)
import webui.i18n as i18n

from translator.job import (
    list_supported_engines,
    list_engine_models,
)
from translator.model_lister import fetch_models

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.yaml")

# Sections users cannot overwrite with the "add provider" form (built-in
# engines + non-engine config groups).
RESERVED_CONFIG_SECTIONS = {
    "openai", "gemini", "webai",
    "translation", "illustration", "consistency", "discord",
}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024
registry = JobRegistry()


@app.after_request
def _no_cache(resp):
    """Never let the browser serve HTML pages or JSON API responses from cache.

    Without an explicit policy the browser may heuristic-cache a page and serve
    a stale copy on F5 (soft reload); navigating to another tab then back
    triggers a fresh navigation and re-reads the server, which is exactly the
    "stale on F5" symptom. Static assets are left alone (they may be cached).
    """
    if resp.mimetype in ("text/html", "application/json"):
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Expires"] = "0"
    return resp


@app.context_processor
def _inject_i18n():
    """Make ``t()``, the resolved ``locale`` and the locale translation table
    available to every template."""
    loc = i18n.resolve_locale(request)

    def t(key, **fmt):
        s = i18n.t(loc, key)
        for k, v in fmt.items():
            s = s.replace("{" + k + "}", str(v))
        return s

    return {"locale": loc, "t": t, "js_i18n": i18n.table(loc)}


# --------------------------------------------------------------------------
# Engine / key-status helpers (shared by job_new + API Keys pages)
# --------------------------------------------------------------------------


def _local_host(url: str) -> bool:
    """True when a base_url points at the local machine (webai proxy, Ollama)."""
    if not url:
        return False
    host = url.split("://")[-1].split("/")[0].split(":")[0].strip("[]").lower()
    return host in ("localhost", "127.0.0.1", "0.0.0.0", "::1")


def _engine_info(config: dict, engine_name: str) -> dict:
    sec = config.get(engine_name) if isinstance(config, dict) else None
    sec = sec if isinstance(sec, dict) else {}
    ptype = {
        "openai": "openai",
        "gemini": "gemini",
        "webai": "webai",
    }.get(engine_name, sec.get("type") or ("openai_compatible" if sec else "openai"))
    has_key = bool(sec.get("api_key"))
    # openai/gemini always need a key; cloud compatible providers need one,
    # but local endpoints (Ollama, mitmproxy webai) work without a key.
    needs_key = engine_name in ("openai", "gemini") or (
        ptype == "openai_compatible" and not _local_host(sec.get("base_url"))
    )
    return {
        "name": engine_name,
        "type": ptype,
        "has_key": has_key,
        "needs_key": needs_key,
        "ready": (not needs_key) or has_key,
        "base_url": sec.get("base_url", ""),
        "model": sec.get("model", ""),
        "show_base": ptype in ("webai", "openai_compatible"),
    }


def _model_suggestions(config: dict) -> dict:
    return {
        engine: list_engine_models(config, engine)
        for engine in list_supported_engines(config)
    }


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------


@app.route("/")
def index():
    jobs = registry.all()
    return render_template("dashboard.html", jobs=jobs)


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
            "model": (p.get("model") or "").strip() or None,
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
    engines = list_supported_engines(config)
    model_suggestions = _model_suggestions(config)
    engine_status = {e: _engine_info(config, e)["ready"] for e in engines}
    missing = [e for e in engines if not engine_status[e]]
    return render_template(
        "job_new.html",
        engines=engines,
        books=books,
        config=config,
        default_config=DEFAULT_CONFIG_PATH,
        model_suggestions=model_suggestions,
        engine_status=engine_status,
        engine_missing=missing,
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

_STATUS_T = {
    "done": "status.done",
    "partial": "status.partial",
    "assumed": "status.assumed",
    "untranslated": "status.untranslated",
}
_RANK_T = {
    "done": "rank.done",
    "partial": "rank.partial",
    "assumed": "rank.assumed",
    "untranslated": "rank.untranslated",
}


def _entry_payload(e: dict, loc: str, with_meta: bool) -> dict:
    out = {
        "name": e["name"],
        "path": e["path"],
        "size_mb": e.get("size_mb"),
        "kind": e["kind"],
        "status": e["status"],
        "label": i18n.t(loc, _STATUS_T[e["status"]]),
        "progress": e.get("progress"),
        "chapters": e.get("chapters"),
    }
    if with_meta:
        out["meta"] = e.get("meta") or {}
    return out


def _slim_library(library: dict) -> dict:
    """Strip internal fields (``abs``, raw ``translations`` paths, …) and
    localize the status/rank labels for the current request locale."""
    loc = i18n.resolve_locale(request)
    groups = []
    for g in library["groups"]:
        groups.append(
            {
                "base_name": g["base_name"],
                "title": g["title"],
                "rank": g["rank"],
                "rank_label": i18n.t(loc, _RANK_T[g["rank"]]),
                "source": _entry_payload(g["source"], loc, True) if g["source"] else None,
                "translations": [_entry_payload(e, loc, False) for e in g["translations"]],
                "entries": [_entry_payload(e, loc, True) for e in g["entries"]],
            }
        )
    return {"groups": groups, "stats": library["stats"], "locale": loc}


@app.route("/api/library")
def api_library():
    refresh = request.args.get("refresh") == "1"
    library = build_library(
        with_chapters=request.args.get("chapters") == "1",
        use_cache=not refresh,
        refresh=refresh,
    )
    return jsonify(_slim_library(library))


@app.route("/set-lang/<code>")
def set_lang(code):
    if code not in i18n.SUPPORTED:
        abort(400)
    resp = redirect(request.args.get("next") or url_for("index"))
    resp.set_cookie("lang", code, max_age=60 * 60 * 24 * 365)
    return resp


@app.route("/books")
def books():
    valid = {"untranslated", "partial", "done", "assumed"}
    active = request.args.get("filter")
    if active not in valid:
        active = None
    return render_template("books.html", active_filter=active)


@app.route("/books/preview")
def book_preview():
    path = request.args.get("path", "")
    try:
        chapter = int(request.args.get("chapter", "1"))
        max_chars = int(request.args.get("max_chars", "4000"))
    except (TypeError, ValueError):
        return jsonify({"error": "chapter/max_chars must be integers"}), 400
    if not path or not os.path.isfile(path) or not path.startswith(PROJECT_ROOT):
        return jsonify({"error": "invalid path"}), 400
    try:
        text, total = preview_chapter(path, chapter, max_chars)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(
        {
            "path": path,
            "chapter": chapter,
            "total": total,
            "shown": len(text),
            "text": text,
        }
    )


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
                "config.html", config_text=text, error=str(e)
            )
        return redirect(url_for("config_view"))
    cfg = load_config()
    return render_template(
        "config.html", config_text=yaml_dump(mask_config(cfg)), error=None
    )


@app.route("/config/keys", methods=["GET", "POST"])
def keys_view():
    cfg = load_config()
    engines = list_supported_engines(cfg)

    if request.method == "POST":
        p = request.form
        new_cfg = copy.deepcopy(cfg)
        for name in engines:
            sec = new_cfg.setdefault(name, {})
            if not isinstance(sec, dict):
                sec = {}
            key_val = (p.get(f"key_{name}") or "").strip()
            if key_val:
                sec["api_key"] = key_val
            base_val = (p.get(f"base_{name}") or "").strip()
            if base_val:
                sec["base_url"] = base_val
            model_val = (p.get(f"model_{name}") or "").strip()
            if model_val:
                sec["model"] = model_val
            # Model suggestions list (1 line / model). A blank textarea keeps
            # the existing list; ticking "clear" wipes it explicitly.
            models_raw = p.get(f"models_{name}")
            if p.get(f"clear_models_{name}"):
                sec["models"] = []
            elif models_raw is not None and models_raw.strip():
                sec["models"] = [
                    ln.strip() for ln in models_raw.splitlines() if ln.strip()
                ]
            new_cfg[name] = sec
        save_config_dict(new_cfg)
        return redirect(url_for("keys_view", saved=1))

    providers = []
    for e in engines:
        info = _engine_info(cfg, e)
        sec = cfg.get(e) if isinstance(cfg, dict) else None
        sec = sec if isinstance(sec, dict) else {}
        raw_models = sec.get("models")
        info["models_text"] = (
            "\n".join(str(m) for m in raw_models)
            if isinstance(raw_models, list)
            else ""
        )
        info["has_models"] = isinstance(raw_models, list) and bool(raw_models)
        info["removable"] = info["type"] == "openai_compatible"
        providers.append(info)
    return render_template(
        "keys.html",
        providers=providers,
        model_suggestions=_model_suggestions(cfg),
        saved=bool(request.args.get("saved")),
        add_error=request.args.get("add_error"),
    )


@app.route("/config/keys/add-provider", methods=["POST"])
def add_provider():
    """Create a new ``type: openai_compatible`` provider section."""
    loc = i18n.resolve_locale(request)
    name = (request.form.get("name") or "").strip()
    base_url = (request.form.get("base_url") or "").strip()
    api_key = (request.form.get("api_key") or "").strip()
    model = (request.form.get("model") or "").strip()
    cfg = load_config()

    def local(key: str, **fmt) -> str:
        msg = i18n.t(loc, key)
        for k, v in fmt.items():
            msg = msg.replace("{" + k + "}", str(v))
        return msg

    error = None
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        error = local("keys.add_error_name")
    elif name in RESERVED_CONFIG_SECTIONS:
        error = local("keys.add_error_reserved", name=name)
    elif name in cfg:
        error = local("keys.add_error_exists", name=name)
    elif not base_url:
        error = local("keys.add_error_url")
    if error:
        return redirect(url_for("keys_view", add_error=error))

    new_sec = {
        "type": "openai_compatible",
        "base_url": base_url,
        "timeout_seconds": 240,
    }
    if api_key:
        new_sec["api_key"] = api_key
    if model:
        new_sec["model"] = model
    cfg[name] = new_sec
    save_config_dict(cfg)
    return redirect(url_for("keys_view", saved=1))


@app.route("/config/keys/remove-provider", methods=["POST"])
def remove_provider():
    """Remove a user-created OpenAI-compatible provider (never built-ins)."""
    name = (request.form.get("name") or "").strip()
    cfg = load_config()
    sec = cfg.get(name)
    if name in RESERVED_CONFIG_SECTIONS or not (
        isinstance(sec, dict) and sec.get("type") == "openai_compatible"
    ):
        return "cannot remove built-in or unknown provider", 400
    cfg.pop(name, None)
    save_config_dict(cfg)
    return redirect(url_for("keys_view", saved=1))


@app.route("/api/provider-models")
def api_provider_models():
    """Live model list for one provider (used by the job form dropdown and
    the API Keys page fetch button). Never raises: unknown engine -> 400."""
    engine = request.args.get("engine", "")
    config = load_config()
    if engine not in list_supported_engines(config):
        return jsonify({"error": "unknown engine"}), 400
    return jsonify(fetch_models(config, engine))


@app.route("/config/settings", methods=["GET", "POST"])
def settings_view():
    """Form-based (GUI) config editor for the non-secret settings. API keys
    stay on the API Keys page — the schema has no api_key fields."""
    loc = i18n.resolve_locale(request)
    if request.method == "POST":
        cfg = load_config()
        new_cfg, errors = config_schema.apply_form(
            copy.deepcopy(cfg), request.form, loc
        )
        if errors:
            return render_template(
                "settings.html",
                groups=config_schema.collect_groups(new_cfg),
                errors=errors,
                saved=False,
            )
        save_config_dict(new_cfg)
        return redirect(url_for("settings_view", saved=1))
    cfg = load_config()
    return render_template(
        "settings.html",
        groups=config_schema.collect_groups(cfg),
        errors=None,
        saved=bool(request.args.get("saved")),
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
