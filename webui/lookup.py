"""AI explain/lookup for the reader: one short chat completion.

Answers "nghĩa của từ này là gì?" for a selected word/phrase with a single
``POST /chat/completions`` call against any provider in config.yaml:

- ``gemini``/``openai`` keep no ``base_url`` in config but both speak the
  OpenAI protocol on their official endpoints, so the mapping lives here;
- dynamic providers (``type: openai_compatible``) bring their own ``base_url``;
- ``ollama``/``webai`` are local and need no API key.

No ``max_tokens``/``temperature`` are sent on purpose: several models (OpenAI
reasoning tiers, some OpenRouter routes) reject non-default values, and the
system prompt already caps the answer at a few lines.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

SYSTEM_PROMPT = (
    "Bạn là từ điển ngắn kiêm trợ lý giải thích khi người dùng đang đọc một "
    "tiểu thuyết tiếng Việt (dịch từ tiếng Trung). Giải thích từ hoặc cụm "
    "được chọn bằng tiếng Việt, tối đa 4 dòng, súc tích: không markdown, "
    "không tiêu đề, không gạch đầu dòng, không chào hỏi.\n"
    "- nghĩa thông dụng và từ loại;\n"
    "- nếu là tên riêng: đây là ai/vật gì (dựa trên ngữ cảnh nếu có);\n"
    "- nếu là thành ngữ/ảnh dụ: nghĩa figurative."
)

# Built-ins that speak OpenAI's protocol although config stores no base_url.
_OPENAI_COMPAT_BASE = {
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "openai": "https://api.openai.com/v1",
}
_BUILTIN = ("gemini", "openai", "webai", "ollama")
_KEYLESS = ("webai", "ollama")          # localhost services: Bearer key optional
_ORDER = ("gemini", "openai", "groq", "deepseek", "openrouter", "webai", "ollama")

MAX_TEXT = 300      # selection is a word/phrase, not a paragraph
MAX_CTX = 400       # surrounding sentence(s) for context
DEFAULT_TIMEOUT = 25


class LookupError(RuntimeError):
    """Provider missing or the upstream call failed — mapped to HTTP 502."""


def _candidates(config: dict):
    """(name, section, lookup-section) in preference order."""
    lk = config.get("lookup") if isinstance(config.get("lookup"), dict) else {}
    ordered: list[str] = []
    pref = lk.get("engine")
    if isinstance(pref, str) and pref.strip():
        ordered.append(pref.strip())
    for name in _ORDER:
        if name not in ordered:
            ordered.append(name)
    for name, sec in config.items():        # any extra openai_compatible provider
        if name in ordered or not isinstance(sec, dict):
            continue
        if sec.get("type") == "openai_compatible":
            ordered.append(name)
    for name in ordered:
        sec = config.get(name)
        if not isinstance(sec, dict):
            continue
        if name not in _BUILTIN and sec.get("type") != "openai_compatible":
            continue
        yield name, sec, lk


def _resolve_all(config: dict) -> list[dict]:
    """Every usable provider in preference order (fallback chain)."""
    out: list[dict] = []
    for name, sec, lk in _candidates(config):
        key = str(sec.get("api_key") or "").strip()
        if not key and name not in _KEYLESS:
            continue
        base = str(
            sec.get("base_url") or _OPENAI_COMPAT_BASE.get(name) or ""
        ).rstrip("/")
        if not base:
            continue
        endpoint = str(sec.get("endpoint") or "/chat/completions")
        if not endpoint.startswith("/"):
            endpoint = "/" + endpoint
        model = lk.get("model") or sec.get("model")
        if not isinstance(model, str) or not model.strip():
            continue
        cfg_timeout = sec.get("timeout_seconds")
        try:
            timeout = min(int(cfg_timeout), 60) if cfg_timeout else DEFAULT_TIMEOUT
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT
        out.append({
            "engine": name,
            "url": base + endpoint,
            "api_key": key,
            "model": model.strip(),
            "timeout": timeout,
        })
    return out


def _call(resolved: dict, text: str, ctx: str) -> str:
    """One chat completion against `resolved`; returns the reply text."""
    user = f"Từ/cụm: {text}"
    ctx = (ctx or "").strip()
    if ctx:
        user = f"Ngữ cảnh: …{ctx}…\n{user}"
    payload = {
        "model": resolved["model"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
    }
    headers = {"Content-Type": "application/json"}
    if resolved["api_key"]:
        headers["Authorization"] = f"Bearer {resolved['api_key']}"
    req = urllib.request.Request(
        resolved["url"],
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=resolved["timeout"]) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001 — body is best-effort context only
            pass
        raise LookupError(f"{resolved['engine']}: HTTP {e.code} {detail}") from e
    except urllib.error.URLError as e:
        raise LookupError(f"{resolved['engine']}: {e.reason}") from e
    except (ValueError, OSError) as e:
        raise LookupError(f"{resolved['engine']}: {e}") from e

    try:
        reply = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, ValueError) as e:
        raise LookupError(f"{resolved['engine']}: lỗi cấu trúc phản hồi") from e
    if not isinstance(reply, str):
        reply = str(reply)
    reply = reply.strip()
    if not reply:
        raise LookupError(f"{resolved['engine']}: phản hồi rỗng")
    return reply


def explain(config: dict, text: str, ctx: str = "") -> dict:
    """Vietnamese explanation of `text` — walks the provider fallback chain
    so one provider being overloaded (Gemini 503 spikes) doesn't fail the
    lookup while other configured keys are idle."""
    chain = _resolve_all(config)
    if not chain:
        raise LookupError(
            "không có AI provider nào dùng được — thêm API key tại "
            "web UI → API Keys"
        )
    errors: list[str] = []
    for resolved in chain:
        try:
            return {"engine": resolved["engine"], "result": _call(resolved, text, ctx)}
        except LookupError as e:
            errors.append(str(e))
    raise LookupError(" | ".join(errors[:4]))
