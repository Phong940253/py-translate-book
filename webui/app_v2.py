"""Server for the React/Tailwind WebUI v2.

Run it with:

    python -m webui.app_v2

It binds the official port 5000 by default (override with V2_PORT), reuses the
existing Flask APIs, and serves the built Vite app under /v2 — the classic v1
UI stays available at /.

The JSON endpoints below are the v2 equivalents of the v1 server-rendered
pages.  They deliberately reuse the same registry, config store and validation
helpers so the two UIs cannot drift apart.
"""

from __future__ import annotations

import copy
import io
import os
import re

from flask import jsonify, request, send_file, send_from_directory

import webui.config_schema as config_schema
import webui.core_runner as core_runner
import webui.i18n as i18n
import webui.reader as reader
from webui import books as book_store
from webui.app import (
    DEFAULT_CONFIG_PATH,
    PROJECT_ROOT,
    RESERVED_CONFIG_SECTIONS,
    _engine_info,
    _model_suggestions,
    api_reader_doc,
    app,
    fetch_models,
    list_supported_engines,
    load_config,
    mask_config,
    registry,
    save_config_dict,
    save_config_text,
    yaml_dump,
)
from webui.books import get_cover
from webui.music import music_bp

DIST = os.path.join(PROJECT_ROOT, "webui", "static", "v2")
ILLUSTRATION_DIR = os.path.join(PROJECT_ROOT, "images", "generated")
ILLUSTRATION_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".svg")

if "music_v2" not in app.blueprints:
    app.register_blueprint(music_bp)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() not in ("", "0", "false", "no", "off")


def _as_int(value, field: str):
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be an integer") from None


def _job_meta(job_id: str):
    meta = registry.load_meta(job_id)
    if meta:
        return meta
    job = registry.get(job_id)
    return job.meta() if job is not None else None


def _job_payload(meta: dict, *, full: bool = True) -> dict:
    """Safe JSON view of a persisted job record.

    ``params`` never contains API keys, but config_path/input/output are useful
    in the job detail page.  The list view drops the potentially large current
    chunk (source + translation) to keep the dashboard light.
    """
    progress = meta.get("progress") or {}
    if not full and isinstance(progress, dict):
        progress = {
            key: value
            for key, value in progress.items()
            if key not in ("current_chunk", "current_chunk_source")
        }
    return {
        "id": meta.get("id"),
        "created_at": meta.get("created_at"),
        "status": meta.get("status"),
        "params": meta.get("params") or {},
        "progress": progress,
        "result": meta.get("result") if full else None,
        "error": meta.get("error"),
        "stop_requested": bool(meta.get("stop_requested")),
    }


def _providers_payload() -> dict:
    cfg = load_config()
    engines = list_supported_engines(cfg)
    providers = []
    for engine in engines:
        info = _engine_info(cfg, engine)
        sec = cfg.get(engine) if isinstance(cfg, dict) else None
        sec = sec if isinstance(sec, dict) else {}
        raw_models = sec.get("models")
        info["models_text"] = (
            "\n".join(str(model) for model in raw_models)
            if isinstance(raw_models, list)
            else ""
        )
        info["has_models"] = isinstance(raw_models, list) and bool(raw_models)
        info["removable"] = info["type"] == "openai_compatible"
        providers.append(info)
    return {
        "providers": providers,
        "model_suggestions": _model_suggestions(cfg),
        "engine_missing": [p["name"] for p in providers if not p["ready"]],
        "config_path": DEFAULT_CONFIG_PATH,
    }


def _settings_payload(cfg: dict) -> dict:
    loc = i18n.resolve_locale(request)
    groups = config_schema.collect_groups(cfg)
    for group in groups:
        group["label_text"] = i18n.t(loc, group["label"])
        for field in group.get("fields", []):
            field["label_text"] = i18n.t(loc, field["label"])
    return {"groups": groups, "locale": loc}


def _validate_epub(path: str) -> str:
    if not path or not os.path.isfile(path) or not path.startswith(PROJECT_ROOT):
        raise ValueError("invalid path")
    if not path.lower().endswith(".epub"):
        raise ValueError("not an epub")
    return path


# ---------------------------------------------------------------------------
# Bootstrap / SPA
# ---------------------------------------------------------------------------


@app.get("/api/v2/bootstrap")
def api_v2_bootstrap():
    locale = i18n.resolve_locale(request)
    return jsonify({
        "version": "v2",
        "locale": locale,
        "i18n": i18n.table(locale),
        "config_path": DEFAULT_CONFIG_PATH,
        "legacy": {
            "library": "/books",
            "reader": "/read",
            "jobs": "/",
            "keys": "/config/keys",
            "settings": "/config/settings",
            "config": "/config",
            "illustrations": "/illustrations",
        },
    })


def _spa_index():
    path = os.path.join(DIST, "index.html")
    if not os.path.isfile(path):
        return jsonify({
            "error": "WebUI v2 chưa build.",
            "hint": "cd frontend && npm run build",
        }), 503
    response = send_from_directory(DIST, "index.html")
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/v2")
@app.get("/v2/")
def v2_spa_root():
    return _spa_index()


@app.get("/v2/<path:path>")
def v2_spa(path: str):
    target = os.path.realpath(os.path.join(DIST, path))
    if target.startswith(os.path.realpath(DIST) + os.sep) and os.path.isfile(target):
        return send_from_directory(DIST, path)
    # Client-side routes (/v2/music, /v2/library, …) fall back to index.html.
    return _spa_index()


@app.get("/v2/doc")
def v2_reader_doc():
    """Chapter document served inside the /v2 scope.

    The reader's iframe loads it here so the PWA service worker controls the
    navigation — and, through that controlled document, every /api/reader/res
    subresource — which is what makes downloaded books readable offline.
    Identical handler and payload to /api/reader/doc (the Cache API stores
    no-store responses too — Cache Control only governs the HTTP cache).
    """
    return api_reader_doc()


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


@app.get("/api/v2/jobs")
def api_v2_jobs():
    jobs = []
    for job in registry.all():
        meta = _job_meta(job.id) or job.meta()
        jobs.append(_job_payload(meta, full=False))
    return jsonify({"jobs": jobs})


@app.post("/api/v2/jobs")
def api_v2_job_create():
    payload = request.get_json(silent=True) or {}
    engine = str(payload.get("engine") or "").strip()
    input_path = str(payload.get("input") or "").strip()
    if not engine or not input_path:
        return jsonify({"error": "engine and input are required"}), 400

    cfg = load_config()
    if engine not in list_supported_engines(cfg):
        return jsonify({"error": f"unknown engine: {engine}"}), 400

    try:
        from_chapter = _as_int(payload.get("from_chapter"), "from_chapter")
        to_chapter = _as_int(payload.get("to_chapter"), "to_chapter")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    output = str(payload.get("output") or "").strip() or (input_path + ".translated.epub")
    params = {
        "engine": engine,
        "input": input_path,
        "output": output,
        "config_path": str(payload.get("config_path") or "").strip() or DEFAULT_CONFIG_PATH,
        "from_chapter": from_chapter,
        "to_chapter": to_chapter,
        "from_lang": str(payload.get("from_lang") or "EN"),
        "to_lang": str(payload.get("to_lang") or "VI"),
        "description": str(payload.get("description") or "").strip() or None,
        "model": str(payload.get("model") or "").strip() or None,
        "openai_batch": _as_bool(payload.get("openai_batch")),
        "reset_checkpoint": _as_bool(payload.get("reset_checkpoint")),
        "disable_resume": _as_bool(payload.get("disable_resume")),
        "tts_disk_cache": _as_bool(payload.get("tts_disk_cache")),
        "tts_voice": str(payload.get("tts_voice") or "").strip() or None,
        "global_rules_path": str(payload.get("global_rules_path") or "").strip() or None,
    }
    job = registry.create(params)
    core_runner.start_job(registry, job)
    meta = _job_meta(job.id) or job.meta()
    return jsonify({"job": _job_payload(meta)}), 201


@app.get("/api/v2/jobs/<job_id>")
def api_v2_job(job_id: str):
    meta = _job_meta(job_id)
    if not meta:
        return jsonify({"error": "job not found"}), 404
    return jsonify({"job": _job_payload(meta)})


@app.get("/api/v2/jobs/<job_id>/state")
def api_v2_job_state(job_id: str):
    meta = _job_meta(job_id)
    if not meta:
        return jsonify({"error": "job not found"}), 404
    return jsonify({
        "progress": meta.get("progress") or {},
        "status": meta.get("status"),
        "result": meta.get("result"),
        "error": meta.get("error"),
    })


@app.patch("/api/v2/jobs/<job_id>")
def api_v2_job_update(job_id: str):
    job = registry.get(job_id)
    if not job:
        return jsonify({"error": "job not found"}), 404
    if job.status not in ("stopped", "interrupted", "error"):
        return jsonify({"error": f"cannot edit a {job.status} job"}), 409

    payload = request.get_json(silent=True) or {}
    allowed = {
        "engine", "model", "input", "output", "from_chapter", "to_chapter",
        "from_lang", "to_lang", "description", "openai_batch",
        "reset_checkpoint", "disable_resume", "tts_disk_cache",
        "tts_voice",
        "global_rules_path", "config_path",
    }
    unknown = sorted(set(payload) - allowed)
    if unknown:
        return jsonify({"error": f"unsupported job properties: {', '.join(unknown)}"}), 400

    params = dict(job.params)
    if "engine" in payload:
        engine = str(payload.get("engine") or "").strip()
        if engine not in list_supported_engines(load_config()):
            return jsonify({"error": f"unknown engine: {engine}"}), 400
        params["engine"] = engine
    for key in allowed - {"engine"}:
        if key not in payload:
            continue
        value = payload[key]
        if key in {"from_chapter", "to_chapter"}:
            try:
                value = _as_int(value, key)
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 400
        elif key in {
            "openai_batch", "reset_checkpoint", "disable_resume", "tts_disk_cache",
        }:
            value = _as_bool(value)
        elif key in {
            "model", "description", "global_rules_path", "config_path", "tts_voice",
        }:
            value = str(value or "").strip() or None
        elif key in {"input", "output", "from_lang", "to_lang"}:
            value = str(value or "").strip()
        params[key] = value

    if not params.get("input"):
        return jsonify({"error": "input is required"}), 400
    if not params.get("output"):
        return jsonify({"error": "output is required"}), 400
    job.params = params
    job.stop_requested = False
    registry.save(job)
    meta = _job_meta(job_id) or job.meta()
    return jsonify({"job": _job_payload(meta)})


@app.post("/api/v2/jobs/<job_id>/stop")
def api_v2_job_stop(job_id: str):
    job = registry.get(job_id)
    if not job:
        return jsonify({"error": "job not found"}), 404
    registry.request_stop(job)
    proc = core_runner.request_hard_stop(job_id)
    if proc is not None:
        proc.join(timeout=2)
        core_runner._PROCS.pop(job_id, None)
        meta = registry.load_meta(job_id) or {}
        if meta.get("status") in ("running", "queued"):
            job.status = "stopped"
            registry.save(job)
    meta = _job_meta(job_id) or job.meta()
    return jsonify({"job": _job_payload(meta)})


@app.post("/api/v2/jobs/<job_id>/resume")
def api_v2_job_resume(job_id: str):
    job = registry.get(job_id)
    if not job:
        return jsonify({"error": "job not found"}), 404
    if job.status not in ("stopped", "interrupted", "error"):
        return jsonify({"error": f"cannot resume a {job.status} job"}), 409
    job.stop_requested = False
    job.error = None
    job.result = None
    job.progress = {}
    job.events = []
    params = dict(job.params)
    params["reset_checkpoint"] = False
    params["disable_resume"] = False
    job.params = params
    job.status = "running"
    registry.save(job)
    core_runner.start_job(registry, job)
    meta = _job_meta(job_id) or job.meta()
    return jsonify({"job": _job_payload(meta)})


# ---------------------------------------------------------------------------
# API keys / providers
# ---------------------------------------------------------------------------


@app.get("/api/v2/config/keys")
def api_v2_keys():
    return jsonify(_providers_payload())


@app.put("/api/v2/config/keys")
def api_v2_keys_save():
    payload = request.get_json(silent=True) or {}
    providers = payload.get("providers") or []
    if isinstance(providers, dict):
        providers = [
            {"name": name, **(value if isinstance(value, dict) else {})}
            for name, value in providers.items()
        ]
    if not isinstance(providers, list):
        return jsonify({"error": "providers must be a list"}), 400

    cfg = load_config()
    supported = set(list_supported_engines(cfg))
    new_cfg = copy.deepcopy(cfg)
    for provider in providers:
        if not isinstance(provider, dict):
            continue
        name = str(provider.get("name") or "").strip()
        if name not in supported:
            return jsonify({"error": f"unknown provider: {name}"}), 400
        sec = new_cfg.get(name)
        if not isinstance(sec, dict):
            sec = {}
        key_value = str(provider.get("api_key") or "").strip()
        if key_value and key_value != "********":
            sec["api_key"] = key_value
        if "base_url" in provider and provider.get("base_url") is not None:
            sec["base_url"] = str(provider.get("base_url") or "").strip()
        if "model" in provider and provider.get("model") is not None:
            sec["model"] = str(provider.get("model") or "").strip()
        if provider.get("clear_models"):
            sec["models"] = []
        elif "models" in provider and provider.get("models") is not None:
            raw_models = provider.get("models")
            if isinstance(raw_models, str):
                raw_models = [
                    line.strip() for line in raw_models.splitlines() if line.strip()
                ]
            if isinstance(raw_models, list):
                sec["models"] = [str(item).strip() for item in raw_models if str(item).strip()]
        new_cfg[name] = sec
    save_config_dict(new_cfg)
    return jsonify(_providers_payload())


@app.post("/api/v2/config/keys/providers")
def api_v2_provider_add():
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name") or "").strip()
    base_url = str(payload.get("base_url") or "").strip()
    api_key = str(payload.get("api_key") or "").strip()
    model = str(payload.get("model") or "").strip()
    cfg = load_config()

    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        return jsonify({"error": "provider name may only contain A-Z, a-z, 0-9, _ and -"}), 400
    if name in RESERVED_CONFIG_SECTIONS:
        return jsonify({"error": f"provider name is reserved: {name}"}), 400
    if name in cfg:
        return jsonify({"error": f"provider already exists: {name}"}), 409
    if not base_url:
        return jsonify({"error": "base_url is required"}), 400

    sec = {"type": "openai_compatible", "base_url": base_url, "timeout_seconds": 240}
    if api_key:
        sec["api_key"] = api_key
    if model:
        sec["model"] = model
    cfg[name] = sec
    save_config_dict(cfg)
    return jsonify(_providers_payload()), 201


@app.delete("/api/v2/config/keys/providers/<name>")
def api_v2_provider_remove(name: str):
    cfg = load_config()
    sec = cfg.get(name)
    if name in RESERVED_CONFIG_SECTIONS or not (
        isinstance(sec, dict) and sec.get("type") == "openai_compatible"
    ):
        return jsonify({"error": "cannot remove built-in or unknown provider"}), 400
    cfg.pop(name, None)
    save_config_dict(cfg)
    return jsonify(_providers_payload())


@app.get("/api/v2/config/models")
def api_v2_models():
    engine = request.args.get("engine", "")
    cfg = load_config()
    if engine not in list_supported_engines(cfg):
        return jsonify({"error": "unknown engine"}), 400
    return jsonify(fetch_models(cfg, engine))


# ---------------------------------------------------------------------------
# GUI settings / advanced YAML / Discord
# ---------------------------------------------------------------------------


@app.get("/api/v2/config/settings")
def api_v2_settings():
    return jsonify(_settings_payload(load_config()))


@app.put("/api/v2/config/settings")
def api_v2_settings_save():
    payload = request.get_json(silent=True) or {}
    values = payload.get("values", payload)
    if not isinstance(values, dict):
        return jsonify({"error": "values must be an object"}), 400
    cfg = load_config()
    loc = i18n.resolve_locale(request)
    new_cfg, errors = config_schema.apply_form(copy.deepcopy(cfg), values, loc)
    if errors:
        return jsonify({"error": "invalid settings", "errors": errors, "groups": _settings_payload(new_cfg)["groups"]}), 400
    save_config_dict(new_cfg)
    return jsonify({"ok": True, **_settings_payload(new_cfg)})


@app.get("/api/v2/config")
def api_v2_config():
    return jsonify({
        "text": yaml_dump(mask_config(load_config())),
        "config_path": DEFAULT_CONFIG_PATH,
    })


@app.put("/api/v2/config")
def api_v2_config_save():
    payload = request.get_json(silent=True) or {}
    text = payload.get("text")
    if not isinstance(text, str):
        return jsonify({"error": "text is required"}), 400
    try:
        save_config_text(text)
    except Exception as exc:  # noqa: BLE001 - YAML errors are user-facing
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True, "text": yaml_dump(mask_config(load_config()))})


@app.post("/api/v2/config/test-discord")
def api_v2_test_discord():
    from translator.discord_notifier import DiscordNotifier

    cfg = load_config()
    discord_cfg = cfg.get("discord", {}) if isinstance(cfg, dict) else {}
    url = discord_cfg.get("webhook_url", "")
    mention = str(discord_cfg.get("mention_user_id", "")).strip() or None
    ok = DiscordNotifier.send_test_message(webhook_url=url, mention_user_id=mention)
    return jsonify({"success": bool(ok)})


# ---------------------------------------------------------------------------
# Illustrations / preview / misc
# ---------------------------------------------------------------------------


@app.get("/api/v2/illustrations")
def api_v2_illustrations():
    images = []
    if os.path.isdir(ILLUSTRATION_DIR):
        for name in sorted(os.listdir(ILLUSTRATION_DIR)):
            if name.lower().endswith(ILLUSTRATION_EXTS):
                images.append({"name": name, "url": f"/images/{name}"})
    return jsonify({"images": images, "dir": ILLUSTRATION_DIR})


@app.get("/api/v2/books/cover")
def api_v2_book_cover():
    """Cover art with a real placeholder for books without a cover.

    The v1 endpoint answers 404 for covers that do not exist, which is fine
    for the server-rendered library but produces a burst of browser console
    errors when a grid of 60 cards mounts.  Returning the placeholder with a
    200 keeps the v2 library quiet while preserving the same caching policy.
    """
    path = request.args.get("path", "")
    try:
        _validate_epub(path)
    except ValueError:
        return send_from_directory(DIST, "cover-placeholder.svg", max_age=3600)
    cover = get_cover(path)
    if not cover:
        return send_from_directory(DIST, "cover-placeholder.svg", max_age=3600)
    data, mime = cover
    return send_file(io.BytesIO(data), mimetype=mime, max_age=3600)


@app.get("/api/v2/books/preview")
def api_v2_book_preview():
    path = request.args.get("path", "")
    try:
        _validate_epub(path)
        chapter = int(request.args.get("chapter", "1"))
        max_chars = int(request.args.get("max_chars", "4000"))
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    try:
        text, total = book_store.preview_chapter(path, chapter, max_chars)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({
        "path": path,
        "chapter": chapter,
        "total": total,
        "shown": len(text),
        "text": text,
    })


@app.get("/api/v2/reader/chapter")
def api_v2_reader_chapter():
    """Chapter items for v2 TTS/search clients (same backend as v1)."""
    path = request.args.get("path", "")
    try:
        _validate_epub(path)
        n = int(request.args.get("n", "1"))
        doc = reader.load_book(path)
        chapter = doc.chapter(n)
    except (ValueError, TypeError) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 422
    reader.ENGINE.note_chapter(doc.path, n)
    return jsonify(chapter)


# ---------------------------------------------------------------------------
# Dev server
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("V2_PORT", "5000"))
    host = os.environ.get("WEBUI_HOST", "0.0.0.0")
    print(f"WebUI running at http://127.0.0.1:{port}/ (v2 at /v2/)")
    app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)
