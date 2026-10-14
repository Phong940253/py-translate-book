"""Flask app for managing py-translate-book translations (local-only).

Run:  python -m webui.app   (or: python webui/app.py)
Open: http://127.0.0.1:5000
"""

import copy
import io
import ipaddress
import json
import os
import re
import struct
import sys
import time
import uuid

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
    stream_with_context,
)
from werkzeug.serving import WSGIRequestHandler

import webui.core_runner as core_runner
import webui.config_schema as config_schema
from webui.jobs import JobRegistry, LOG_TAIL_LINES
from webui.config_store import load_config, mask_config, save_config_text, save_config_dict
from webui.books import (
    build_library,
    list_epubs,
    preview_chapter,
    get_cover,
)
import webui.reader as reader
import webui.lookup as lookup
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


def _is_lan(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


@app.before_request
def _lan_only():
    """The UI can read and rewrite provider keys (config.yaml), so never
    answer the public internet even when port 5000 ends up forwarded —
    internet scanners already knock on it (their TLS handshakes log as 400
    "Bad request version"). LAN/loopback clients pass; set WEBUI_PUBLIC=1
    if you front the app with a tunnel/VPN of your own."""
    if os.environ.get("WEBUI_PUBLIC") == "1":
        return
    if not _is_lan(request.remote_addr or ""):
        abort(403)
registry = JobRegistry()


@app.after_request
def _no_cache(resp):
    """Never let the browser serve HTML pages or JSON API responses from cache.

    Without an explicit policy the browser may heuristic-cache a page and serve
    a stale copy on F5 (soft reload); navigating to another tab then back
    triggers a fresh navigation and re-reads the server, which is exactly the
    "stale on F5" symptom. Static assets are left alone (they may be cached),
    and so is any response that states its own max-age (e.g. the cover route —
    its artwork is refetched on every lock-screen metadata update).
    """
    cc = resp.headers.get("Cache-Control", "")
    if resp.mimetype in ("text/html", "application/json") and "max-age" not in cc:
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
            "global_rules_path": p.get("global_rules_path") or None,
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


@app.route("/api/jobs/<job_id>/rules")
def api_job_rules(job_id):
    """Return the global cross-chapter rules for a job (JSON)."""
    job = registry.get(job_id)
    if not job:
        abort(404)
    # Determine rules path from job params (same logic as run_translation)
    output = (job.params or {}).get("output", "")
    if not output:
        return jsonify({"error": "no output path"}), 404
    rules_path = os.path.splitext(output)[0] + ".rules.json"
    if not os.path.exists(rules_path):
        return jsonify({
            "characters": {},
            "narration_defaults": {},
            "fallback": "",
            "meta": {
                "chapter": None,
                "total_characters": 0,
                "returned_characters": 0,
                "has_analysis": False,
            },
        })
    chapter_arg = request.args.get("chapter")
    chapter: int | None = None
    if chapter_arg not in (None, ""):
        try:
            chapter = int(chapter_arg)
        except (TypeError, ValueError):
            return jsonify({"error": "bad chapter"}), 400

    try:
        from translator.global_rules import GlobalRulesManager
        mgr = GlobalRulesManager(rules_path)
        mgr.load()
        state = mgr.state
        only = None
        analysis = None
        if chapter is not None:
            # Scope to the characters this chapter's own analysis named — a
            # long book accumulates thousands of rules and 1MB of analyses.
            analysis = mgr.get_raw_analysis(chapter)
            only = (
                mgr.chars_in_analysis(analysis, known=set(state.characters))
                if analysis
                else set()
            )
        chars = {
            name: {
                "names": entry.names,
                "calls": entry.calls,
                "pronouns": entry.pronouns,
                "notes": entry.notes,
            }
            for name, entry in state.characters.items()
            if only is None or name in only
        }
        return jsonify({
            "characters": chars,
            "narration_defaults": state.narration_defaults,
            "fallback": state.fallback,
            "analyses": (
                ({str(chapter): analysis} if analysis else {})
                if chapter is not None
                else mgr.get_all_analyses()
            ),
            "meta": {
                "chapter": chapter,
                "total_characters": len(state.characters),
                "returned_characters": len(chars),
                "has_analysis": analysis is not None,
            },
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


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

    LOG_BATCH_SIZE = 100  # send log lines in batches to avoid O(n²) DOM updates

    def gen():
        # Send only the tail on connect: the on-disk log is never truncated,
        # so resending it whole would flood the client on every page load.
        # Afterwards follow the file by byte position (O(new bytes) per tick
        # instead of re-reading the whole file).
        pos = 0
        if os.path.exists(log_path):
            try:
                with open(log_path, encoding="utf-8") as f:
                    tail = f.read().splitlines()[-LOG_TAIL_LINES:]
                # Send log tail in batches to avoid overwhelming the client
                for i in range(0, len(tail), LOG_BATCH_SIZE):
                    batch = tail[i : i + LOG_BATCH_SIZE]
                    yield "data: " + json.dumps(
                        {"type": "log_batch", "lines": batch}, ensure_ascii=False
                    ) + "\n\n"
                pos = os.path.getsize(log_path)
            except Exception:  # noqa: BLE001
                pos = 0
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
                    size = os.path.getsize(log_path)
                    if size < pos:
                        pos = 0  # file shrank; start over
                    if size > pos:
                        with open(log_path, encoding="utf-8") as f:
                            f.seek(pos)
                            lines = f.read().splitlines()
                        pos = size
                except Exception:  # noqa: BLE001
                    lines = []

            if lines:
                # Send incremental log lines in batches
                for i in range(0, len(lines), LOG_BATCH_SIZE):
                    batch = lines[i : i + LOG_BATCH_SIZE]
                    yield "data: " + json.dumps(
                        {"type": "log_batch", "lines": batch}, ensure_ascii=False
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
        # Negatively cached: the reader probes the cover once and Chrome
        # fetches the lock-screen artwork on every MediaMetadata set — books
        # without a cover must stop coming back as 404 log lines.
        return Response(b"", status=404,
                        headers={"Cache-Control": "private, max-age=3600"})
    data, mime = cover
    return send_file(io.BytesIO(data), mimetype=mime, max_age=3600)


# --------------------------------------------------------------------------
# EPUB reader (/read) + local TTS
# --------------------------------------------------------------------------


def _validate_epub_path(path: str) -> str:
    """Absolute EPUB path inside the project root (400 on anything else)."""
    if not path or not path.startswith(PROJECT_ROOT) or not os.path.isfile(path):
        abort(400, description="invalid path")
    if not path.lower().endswith(".epub"):
        abort(400, description="not an epub")
    return path


def _reader_path() -> str:
    """Validated absolute EPUB path from ``?path=`` (400 on anything else)."""
    return _validate_epub_path(request.args.get("path", ""))


def _reader_doc():
    """Parsed book for an API request, or a JSON error response to return early.

    Usage: ``doc, err = _reader_doc(); if err: return err``.
    """
    return _reader_doc_for(request.args.get("path", ""))


def _reader_doc_for(path: str):
    """Same as ``_reader_doc`` but for a path already read from args/body."""
    if not path or not path.startswith(PROJECT_ROOT) or not os.path.isfile(path):
        return None, (jsonify({"error": "invalid path"}), 400)
    try:
        return reader.load_book(path), None
    except ValueError as e:
        return None, (jsonify({"error": str(e)}), 400)
    except Exception as e:  # noqa: BLE001 — broken EPUB → clean JSON error
        return None, (jsonify({"error": f"{type(e).__name__}: {e}"}), 422)


@app.route("/read")
def reader_view():
    path = _reader_path()
    reader.ENGINE.ensure_warm()
    return render_template("reader.html", path=path, book_title=os.path.basename(path))


@app.route("/api/reader/meta")
def api_reader_meta():
    doc, err = _reader_doc()
    if err:
        return err
    return jsonify(doc.meta())


@app.route("/api/reader/chapter")
def api_reader_chapter():
    doc, err = _reader_doc()
    if err:
        return err
    try:
        n = int(request.args.get("n", "1"))
    except (TypeError, ValueError):
        return jsonify({"error": "n must be an integer"}), 400
    try:
        return jsonify(doc.chapter(n))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/reader/image")
def api_reader_image():
    doc, err = _reader_doc()
    if err:
        return err
    src = request.args.get("src", "")
    data, mime = reader.read_image(doc.path, src)
    if not data:
        abort(404)
    return send_file(io.BytesIO(data), mimetype=mime, max_age=86400)


def _doc_error_page(message: str, status: int):
    """HTML error body — /api/reader/doc is loaded inside the reader iframe."""
    from markupsafe import escape

    return Response(
        f"<!DOCTYPE html><html><body><p>{escape(message)}</p></body></html>",
        status=status,
        mimetype="text/html",
        headers={"Content-Security-Policy": reader.CHAPTER_CSP},
    )


@app.route("/api/reader/toc")
def api_reader_toc():
    """Nested TOC (EPUB3 nav / EPUB2 ncx) + landmarks + page-list."""
    doc, err = _reader_doc()
    if err:
        return err
    return jsonify(doc.toc_payload())


@app.route("/api/reader/doc")
def api_reader_doc():
    """Sanitized chapter document for the reader's sandboxed iframe."""
    doc, err = _reader_doc()
    if err:
        resp, code = err
        data = resp.get_json() or {}
        return _doc_error_page(data.get("error", "error"), code)
    try:
        n = int(request.args.get("n", "1"))
    except (TypeError, ValueError):
        return _doc_error_page("n must be an integer", 400)
    try:
        page = doc.doc_html(n)
    except ValueError as e:
        return _doc_error_page(str(e), 400)
    except Exception as e:  # noqa: BLE001 — broken chapter → readable page
        return _doc_error_page(f"{type(e).__name__}: {e}", 422)
    return Response(
        page,
        mimetype="text/html",
        headers={
            "Content-Security-Policy": reader.CHAPTER_CSP,
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


@app.route("/api/reader/res")
def api_reader_res():
    """CSS / image / font straight out of the EPUB zip (chapters are refused)."""
    path = _reader_path()
    ref = request.args.get("ref", "")
    try:
        data, mime = reader.read_resource(path, ref)
    except ValueError:
        data, mime = None, None
    if not data:
        abort(404)
    headers = {"X-Content-Type-Options": "nosniff"}
    if mime == "image/svg+xml":
        # SVG opened as a document could run scripts; force a no-op CSP.
        headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'"
    resp = send_file(io.BytesIO(data), mimetype=mime, max_age=86400)
    resp.headers.update(headers)
    return resp


@app.route("/api/reader/state", methods=["GET", "PUT"])
def api_reader_state():
    """Per-book reading state (position + settings) sidecar <book>.reader.json."""
    if request.method == "GET":
        path = _reader_path()
        return jsonify(reader.load_state(path))
    payload = request.get_json(silent=True) or {}
    state = payload.get("state")
    if not isinstance(state, dict):
        return jsonify({"error": "state object is required"}), 400
    path = _validate_epub_path(str(payload.get("path", "")))
    try:
        reader.save_state(path, state)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except OSError as e:
        return jsonify({"error": f"cannot write state: {e}"}), 500
    return jsonify({"ok": True})


@app.route("/api/reader/search")
def api_reader_search():
    """Full-book text search (plain scan over the spine, cached per book)."""
    doc, err = _reader_doc()
    if err:
        return err
    q = request.args.get("q", "")
    try:
        max_hits = int(request.args.get("max", "200"))
    except (TypeError, ValueError):
        return jsonify({"error": "max must be an integer"}), 400
    try:
        return jsonify(reader.search_book(doc, q, max_hits=max_hits))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/reader/annotations", methods=["GET", "POST", "PUT", "DELETE"])
def api_reader_annotations():
    """Highlights/notes CRUD on the sidecar ``<book>.ann.json``."""
    if request.method == "GET":
        path = _reader_path()
        return jsonify({"anns": reader.load_anns(path)})

    if request.method == "DELETE":
        path = _validate_epub_path(request.args.get("path", ""))
        ann_id = request.args.get("id", "")
        with reader._ANN_LOCK:
            anns = reader.load_anns(path)
            kept = [a for a in anns if a.get("id") != ann_id]
            if len(kept) == len(anns):
                return jsonify({"error": "unknown id"}), 404
            try:
                reader.save_anns(path, kept)
            except ValueError as e:
                return jsonify({"error": str(e)}), 400
            except OSError as e:
                return jsonify({"error": f"cannot write annotations: {e}"}), 500
        return jsonify({"ok": True})

    payload = request.get_json(silent=True) or {}
    path = _validate_epub_path(str(payload.get("path", "")))
    doc, err = _reader_doc_for(path)
    if err:
        return err
    try:
        ann = reader.ann_validate(payload.get("ann"), total=len(doc.chapters))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    now = int(time.time())
    with reader._ANN_LOCK:
        anns = reader.load_anns(path)
        if request.method == "POST":
            ann["id"] = uuid.uuid4().hex[:12]
            ann["created"] = ann["updated"] = now
            anns.append(ann)
            status = 201
        else:  # PUT — replace by id, keep the original creation time
            idx = next(
                (i for i, a in enumerate(anns) if a.get("id") == ann.get("id")), -1
            )
            if idx < 0:
                return jsonify({"error": "unknown id"}), 404
            ann["created"] = anns[idx].get("created", now)
            ann["updated"] = now
            anns[idx] = ann
            status = 200
        try:
            reader.save_anns(path, anns)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except OSError as e:
            return jsonify({"error": f"cannot write annotations: {e}"}), 500
    return jsonify(ann), status


# --------------------------------------------------------------------------
# PWA: manifest + service worker (scope must be the site root)
# --------------------------------------------------------------------------


@app.route("/manifest.webmanifest")
def pwa_manifest():
    path = os.path.join(PROJECT_ROOT, "webui", "static", "manifest.webmanifest")
    return send_file(path, mimetype="application/manifest+json", max_age=3600)


@app.route("/sw.js")
def pwa_service_worker():
    path = os.path.join(PROJECT_ROOT, "webui", "static", "sw.js")
    resp = send_file(path, mimetype="application/javascript", max_age=0)
    resp.headers["Service-Worker-Allowed"] = "/"
    return resp


@app.route("/api/reader/lookup", methods=["POST"])
def api_reader_lookup():
    payload = request.get_json(silent=True) or {}
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        return jsonify({"error": "text is required"}), 400
    ctx = payload.get("ctx")
    try:
        return jsonify(lookup.explain(
            load_config(),
            text.strip()[: lookup.MAX_TEXT],
            ctx.strip()[: lookup.MAX_CTX] if isinstance(ctx, str) else "",
        ))
    except lookup.LookupError as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/tts/voices")
def api_tts_voices():
    reader.ENGINE.ensure_warm()
    return jsonify(reader.ENGINE.status())


@app.route("/api/tts", methods=["POST"])
def api_tts():
    payload = request.get_json(silent=True) or {}
    text = payload.get("text")
    voice = payload.get("voice")
    if not isinstance(text, str) or not text.strip():
        return jsonify({"error": "text is required"}), 400
    if len(text) > reader.MAX_TEXT:
        return jsonify({"error": f"text too long (max {reader.MAX_TEXT} chars)"}), 400
    if voice is not None and not isinstance(voice, str):
        return jsonify({"error": "voice must be a string"}), 400
    reader.ENGINE.ensure_warm()
    try:
        wav, segs = reader.ENGINE.synthesize(text, voice)
    except RuntimeError as e:
        return jsonify({"error": str(e), "state": reader.ENGINE.state}), 503
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:  # noqa: BLE001 — synthesis failure → readable JSON
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500
    resp = send_file(io.BytesIO(wav), mimetype="audio/wav")
    try:  # sentence timing for the reader's sentence-level highlight
        resp.headers["X-TTS-Segs"] = json.dumps(segs)
    except (TypeError, ValueError):  # pragma: no cover — never fail the audio
        pass
    return resp


def _tts_frame(header: dict, wav: bytes) -> bytes:
    """One stream frame: uint32be len(JSON header) | header |
    uint32be len(wav) | wav bytes (0 bytes for an error frame)."""
    hj = json.dumps(header, separators=(",", ":")).encode("utf-8")
    return struct.pack(">I", len(hj)) + hj + struct.pack(">I", len(wav)) + wav


@app.route("/api/tts/stream", methods=["POST"])
def api_tts_stream():
    """Chunked piece stream for read-aloud playback: one frame per split
    piece as soon as it is synthesized, so the reader can start speaking
    without waiting for the whole paragraph."""
    payload = request.get_json(silent=True) or {}
    text = payload.get("text")
    voice = payload.get("voice")
    if not isinstance(text, str) or not text.strip():
        return jsonify({"error": "text is required"}), 400
    if len(text) > reader.MAX_TEXT:
        return jsonify({"error": f"text too long (max {reader.MAX_TEXT} chars)"}), 400
    if voice is not None and not isinstance(voice, str):
        return jsonify({"error": "voice must be a string"}), 400
    reader.ENGINE.ensure_warm()
    try:
        gen = reader.ENGINE.synthesize_stream(text, voice)
        first = next(gen)  # surface engine errors BEFORE the 200 headers
    except StopIteration:
        return jsonify({"error": "empty text"}), 400
    except RuntimeError as e:
        return jsonify({"error": str(e), "state": reader.ENGINE.state}), 503
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:  # noqa: BLE001 — synthesis failure → readable JSON
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500

    def body():
        try:
            header, wav = first
            yield _tts_frame(header, wav)
            for header, wav in gen:
                yield _tts_frame(header, wav)
        except Exception as e:  # noqa: BLE001 — mid-stream failure → error frame
            yield _tts_frame({"error": f"{type(e).__name__}: {e}"}, b"")

    resp = Response(stream_with_context(body()),
                    mimetype="application/octet-stream")
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Accel-Buffering"] = "no"  # proxies: flush per frame
    return resp


# --------------------------------------------------------------------------
# Background music: plain audio files dropped into <project>/music/
# --------------------------------------------------------------------------

MUSIC_DIR = os.path.join(PROJECT_ROOT, "music")
MUSIC_EXTS = (".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".wav", ".flac")


@app.route("/api/music")
def api_music_list():
    """Audio files the reader can loop as background music while reading."""
    try:
        os.makedirs(MUSIC_DIR, exist_ok=True)   # give the user a folder to fill
    except OSError:
        pass
    files = []
    if os.path.isdir(MUSIC_DIR):
        for fn in sorted(os.listdir(MUSIC_DIR)):
            if fn.lower().endswith(MUSIC_EXTS) and os.path.isfile(os.path.join(MUSIC_DIR, fn)):
                files.append({"name": fn, "url": url_for("api_music_file", name=fn)})
    return jsonify({"files": files})


@app.route("/api/music/<path:name>")
def api_music_file(name):
    base = os.path.realpath(MUSIC_DIR)
    path = os.path.realpath(os.path.join(base, name))
    if not path.startswith(base + os.sep) or not os.path.isfile(path):
        abort(404)
    if not path.lower().endswith(MUSIC_EXTS):
        abort(404)
    return send_file(path, conditional=True)   # Range support for streaming


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


class _ProbeQuietHandler(WSGIRequestHandler):
    """Silence non-HTTP probes in the access log. A TLS handshake (or any
    scanner garbage) reaches the parser as a binary "request line", and
    werkzeug then prints a 400 ``Bad request version`` plus a line of raw
    bytes to the console. Every request log path funnels through log() —
    drop only the ones whose request line does not start with an HTTP
    method; real requests, their errors and the startup banner keep
    printing."""

    _METHODS = ("GET ", "POST ", "PUT ", "HEAD ", "DELETE ",
                "OPTIONS ", "PATCH ", "TRACE ", "CONNECT ")

    def log(self, type, message, *args):
        line = getattr(self, "requestline", "") or ""
        if line and not line.startswith(self._METHODS):
            return
        super().log(type, message, *args)


if __name__ == "__main__":
    # LAN access: bind all interfaces by default so phones/tablets on the same
    # network can open http://<lan-ip>:5000. Set WEBUI_HOST=127.0.0.1 to lock
    # the UI to this machine only.
    host = os.environ.get("WEBUI_HOST", "0.0.0.0")
    scope = "local only" if host in ("127.0.0.1", "localhost") else "LAN reachable"
    print(f"Web UI running at http://127.0.0.1:5000  ({scope})")
    app.run(host=host, port=5000, debug=False, use_reloader=False, threaded=True,
            request_handler=_ProbeQuietHandler)
