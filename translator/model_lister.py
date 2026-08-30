"""Live model listing for the web UI (job form dropdown + API Keys page).

Best-effort network calls with a short timeout. On ANY failure the caller
falls back to the static ``models:``/``model:`` hints from config so the UI
never blocks or shows an error to the user.

Supported endpoints:
- OpenAI / OpenAI-compatible providers (Groq, DeepSeek, OpenRouter, Ollama…):
  ``GET {base}/models`` → ``{"data":[{"id": ...}]}`` (Ollama also exposes
  ``GET /api/tags`` → ``{"models":[{"name": ...}]}`` on older versions).
- Gemini: ``GET https://generativelanguage.googleapis.com/v1beta/models``,
  filtered to ``generateContent``-capable models (embeddings are excluded).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from translator.job import list_engine_models, list_supported_engines

DEFAULT_TIMEOUT_SECONDS = 6

OPENAI_BASE_URL = "https://api.openai.com/v1"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
OLLAMA_HOSTS = ("localhost:11434", "127.0.0.1:11434", "[::1]:11434")


def _http_get_json(url: str, api_key: str | None = None, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> dict:
    """GET a JSON endpoint (Best-effort: raises on any HTTP/network error)."""
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _ids_from_openai_payload(payload: dict) -> list[str]:
    """Extract ``data[].id`` from an OpenAI-style ``/models`` response."""
    data = payload.get("data") or []
    ids = [str(m.get("id")) for m in data if isinstance(m, dict) and m.get("id")]
    return sorted(set(ids))


def _is_ollama(base_url: str) -> bool:
    host = urllib.parse.urlparse(base_url).netloc.lower()
    return host in OLLAMA_HOSTS


def _root_of(base_url: str) -> str:
    """Strip a trailing ``/v1`` so the legacy Ollama ``/api/tags`` works."""
    base = base_url.rstrip("/")
    return base[: -len("/v1")] if base.endswith("/v1") else base


def _fetch_ollama_tags(base_url: str, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> list[str]:
    payload = _http_get_json(f"{_root_of(base_url)}/api/tags", timeout=timeout)
    names = []
    for m in payload.get("models") or []:
        if isinstance(m, dict) and m.get("name"):
            names.append(str(m["name"]))
    return sorted(set(names))


def fetch_compatible_models(
    base_url: str,
    api_key: str | None = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> list[str]:
    """GET ``{base}/models`` for any OpenAI-compatible provider.

    Tries in order: ``{base}/models`` then (when the base does not end in
    ``/v1``) ``{base}/v1/models``. For a local Ollama host it additionally
    falls back to the legacy ``/api/tags`` endpoint on a 404.
    """
    base = base_url.rstrip("/")
    candidates = [f"{base}/models"]
    if not base.endswith("/v1"):
        candidates.append(f"{base}/v1/models")

    last_exc: Exception | None = None
    for url in candidates:
        try:
            ids = _ids_from_openai_payload(_http_get_json(url, api_key, timeout))
            if ids:
                return ids
        except Exception as exc:  # noqa: BLE001 - try the next candidate
            last_exc = exc
            if isinstance(exc, urllib.error.HTTPError) and _is_ollama(base):
                try:
                    return _fetch_ollama_tags(base, timeout)
                except Exception:  # noqa: BLE001 - keep the original error
                    pass

    if last_exc is not None:
        raise last_exc
    return []


def fetch_openai(config: dict, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> list[str]:
    sec = config.get("openai") if isinstance(config, dict) else None
    sec = sec if isinstance(sec, dict) else {}
    api_key = sec.get("api_key")
    if not api_key:
        raise ValueError("openai.api_key is required")
    return fetch_compatible_models(OPENAI_BASE_URL, api_key, timeout)


def fetch_gemini(config: dict, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> list[str]:
    sec = config.get("gemini") if isinstance(config, dict) else None
    sec = sec if isinstance(sec, dict) else {}
    api_key = sec.get("api_key")
    if not api_key:
        raise ValueError("gemini.api_key is required")
    url = f"{GEMINI_BASE_URL}/models?key={urllib.parse.quote(api_key)}"
    payload = _http_get_json(url, timeout=timeout)
    out = []
    for m in payload.get("models") or []:
        if not isinstance(m, dict):
            continue
        methods = m.get("supportedGenerationMethods") or []
        name = str(m.get("name") or "")
        if "generateContent" in methods and name.startswith("models/"):
            out.append(name[len("models/"):])
    return sorted(set(out), key=str.lower)


def fetch_webai(config: dict, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> list[str]:
    sec = config.get("webai") if isinstance(config, dict) else None
    sec = sec if isinstance(sec, dict) else {}
    base_url = sec.get("base_url") or "http://localhost:6969"
    return fetch_compatible_models(base_url, sec.get("api_key"), timeout)


def fetch_models(config: dict, engine: str, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> dict:
    """Best-effort live model list for one provider.

    Returns ``{"engine", "models", "source", "error"}``. ``source`` is
    ``"live"`` when the provider answered, otherwise ``"config"`` with the
    static hints from config. Raises ``ValueError`` for unknown engines.
    """
    if engine not in list_supported_engines(config):
        raise ValueError(f"unknown engine: {engine}")

    live: list[str] = []
    error: str | None = None
    try:
        if engine == "openai":
            live = fetch_openai(config, timeout)
        elif engine == "gemini":
            live = fetch_gemini(config, timeout)
        elif engine == "webai":
            live = fetch_webai(config, timeout)
        else:
            sec = config.get(engine) if isinstance(config, dict) else None
            sec = sec if isinstance(sec, dict) else {}
            base_url = sec.get("base_url") or ""
            if not base_url:
                raise ValueError(f"{engine}.base_url is required")
            live = fetch_compatible_models(base_url, sec.get("api_key"), timeout)
    except Exception as exc:  # noqa: BLE001 - the UI must never break on this
        live = []
        error = str(exc)

    if live:
        return {"engine": engine, "models": live, "source": "live", "error": error}

    fallback = list_engine_models(config, engine)
    return {"engine": engine, "models": fallback, "source": "config", "error": error}