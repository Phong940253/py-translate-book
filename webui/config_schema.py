"""Metadata-driven schema for the ``/config/settings`` GUI form.

The form edits the non-secret parts of config.yaml (``translation`` /
``illustration`` / ``consistency`` / ``discord``). API keys stay on the API
Keys page — this schema deliberately has no ``api_key`` fields, so a GUI save
can never touch or leak them.
"""

from __future__ import annotations

import copy

# Each field: ("dot.path", "kind", default, {"min":..,"max":..,"step":..,"rows":..})
#   kind: "int" | "float" | "bool" | "str" | "text"
FIELDS: list[tuple[str, str, object, dict]] = [
    # --- translation ---
    ("translation.fallback_max_chunk_size", "int", 6000, {"min": 500, "max": 100000}),
    ("translation.html_structure_similarity_threshold", "float", 0.7, {"min": 0.0, "max": 1.0, "step": 0.05}),
    ("translation.max_tries", "int", 0, {"min": 0}),
    ("translation.custom_prompt", "text", "", {}),
    # --- illustration ---
    ("translation.illustration.enabled", "bool", False, {}),
    ("translation.illustration.provider", "str", "webai", {}),
    ("translation.illustration.model", "str", "gemini-flash", {}),
    ("translation.illustration.webai_image_endpoint", "str", "/v1/chat/completions", {}),
    ("translation.illustration.every_n_chunks", "int", 3, {"min": 1}),
    ("translation.illustration.max_images_per_chapter", "int", 3, {"min": 1}),
    ("translation.illustration.min_chunk_chars", "int", 160, {"min": 0}),
    ("translation.illustration.prompt_max_chars", "int", 420, {"min": 100}),
    ("translation.illustration.width", "int", 768, {"min": 64, "max": 4096}),
    ("translation.illustration.height", "int", 1152, {"min": 64, "max": 4096}),
    ("translation.illustration.timeout_seconds", "int", 40, {"min": 1}),
    ("translation.illustration.output_dir", "str", "images/generated", {}),
    (
        "translation.illustration.style_prompt",
        "text",
        "Light novel illustration, cinematic composition, no text overlays, safe for work",
        {"rows": 3},
    ),
    ("translation.illustration.default_alt_text", "str", "Minh hoa noi dung", {}),
    # --- consistency ---
    ("translation.consistency.enabled", "bool", True, {}),
    ("translation.consistency.previous_translated_window", "int", 1, {"min": 0}),
    ("translation.consistency.next_source_window", "int", 1, {"min": 0}),
    ("translation.consistency.analysis_max_chars", "int", 30000, {"min": 1000}),
    ("translation.consistency.rules_max_chars", "int", 2000, {"min": 100}),
    ("translation.consistency.cache_chapter_rules", "bool", True, {}),
    # --- discord ---
    ("discord.enabled", "bool", True, {}),
    ("discord.webhook_url", "str", "", {}),
    ("discord.mention_user_id", "str", "", {}),
]

FIELD_LABELS: dict[str, str] = {
    "translation.fallback_max_chunk_size": "settings.fallback_max_chunk_size",
    "translation.html_structure_similarity_threshold": "settings.html_similarity",
    "translation.max_tries": "settings.max_tries",
    "translation.custom_prompt": "settings.custom_prompt",
    "translation.illustration.enabled": "settings.illustration_enabled",
    "translation.illustration.provider": "settings.illustration_provider",
    "translation.illustration.model": "settings.illustration_model",
    "translation.illustration.webai_image_endpoint": "settings.illustration_endpoint",
    "translation.illustration.every_n_chunks": "settings.illustration_every_n",
    "translation.illustration.max_images_per_chapter": "settings.illustration_max_images",
    "translation.illustration.min_chunk_chars": "settings.illustration_min_chunk_chars",
    "translation.illustration.prompt_max_chars": "settings.illustration_prompt_max",
    "translation.illustration.width": "settings.illustration_width",
    "translation.illustration.height": "settings.illustration_height",
    "translation.illustration.timeout_seconds": "settings.illustration_timeout",
    "translation.illustration.output_dir": "settings.illustration_output_dir",
    "translation.illustration.style_prompt": "settings.illustration_style_prompt",
    "translation.illustration.default_alt_text": "settings.illustration_alt_text",
    "translation.consistency.enabled": "settings.consistency_enabled",
    "translation.consistency.previous_translated_window": "settings.consistency_prev_window",
    "translation.consistency.next_source_window": "settings.consistency_next_window",
    "translation.consistency.analysis_max_chars": "settings.consistency_analysis_max",
    "translation.consistency.rules_max_chars": "settings.consistency_rules_max",
    "translation.consistency.cache_chapter_rules": "settings.consistency_cache",
    "discord.enabled": "settings.discord_enabled",
    "discord.webhook_url": "settings.discord_webhook_url",
    "discord.mention_user_id": "settings.discord_mention",
}

GROUPS: list[tuple[str, list[str]]] = [
    (
        "settings.group.translation",
        [
            "translation.fallback_max_chunk_size",
            "translation.html_structure_similarity_threshold",
            "translation.max_tries",
            "translation.custom_prompt",
        ],
    ),
    (
        "settings.group.illustration",
        [
            "translation.illustration.enabled",
            "translation.illustration.provider",
            "translation.illustration.model",
            "translation.illustration.webai_image_endpoint",
            "translation.illustration.every_n_chunks",
            "translation.illustration.max_images_per_chapter",
            "translation.illustration.min_chunk_chars",
            "translation.illustration.prompt_max_chars",
            "translation.illustration.width",
            "translation.illustration.height",
            "translation.illustration.timeout_seconds",
            "translation.illustration.output_dir",
            "translation.illustration.style_prompt",
            "translation.illustration.default_alt_text",
        ],
    ),
    (
        "settings.group.consistency",
        [
            "translation.consistency.enabled",
            "translation.consistency.previous_translated_window",
            "translation.consistency.next_source_window",
            "translation.consistency.analysis_max_chars",
            "translation.consistency.rules_max_chars",
            "translation.consistency.cache_chapter_rules",
        ],
    ),
    (
        "settings.group.discord",
        [
            "discord.enabled",
            "discord.webhook_url",
            "discord.mention_user_id",
        ],
    ),
]


def _get_path(cfg: dict, path: str, default=None):
    node = cfg
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def _set_path(cfg: dict, path: str, value) -> None:
    parts = path.split(".")
    node = cfg
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


def _field_meta(path: str) -> tuple[str, object, dict]:
    for p, kind, default, extra in FIELDS:
        if p == path:
            return kind, default, extra
    raise KeyError(f"no such settings field: {path}")


def collect_groups(cfg: dict) -> list[dict]:
    """Build the render payload: group -> list of field dicts with values."""
    groups = []
    for group_key, paths in GROUPS:
        fields = []
        for path in paths:
            kind, default, extra = _field_meta(path)
            value = _get_path(cfg, path)
            if value is None:
                value = default
            fields.append(
                {
                    "path": path,
                    "kind": kind,
                    "value": value,
                    "extra": extra,
                    "label": FIELD_LABELS[path],
                }
            )
        groups.append({"label": group_key, "fields": fields})
    return groups


def _fmt_invalid(loc: str, path: str, raw) -> str:
    import webui.i18n as i18n

    msg = i18n.t(loc, "settings.invalid_number")
    return msg.replace("{path}", path).replace("{value}", str(raw))


def apply_form(cfg: dict, form, loc: str = "vi") -> tuple[dict, list[str]]:
    """Build a new config dict from the submitted form.

    Returns ``(new_cfg, errors)``. With any error the caller must re-render
    without saving (``new_cfg`` still carries the partial values so the form
    can show what was typed). Untouched top-level sections are preserved.
    """
    import webui.i18n as i18n

    new_cfg = copy.deepcopy(cfg)
    errors: list[str] = []
    for path, kind, default, extra in FIELDS:
        raw = form.get(path)
        if kind == "bool":
            _set_path(
                new_cfg, path, bool(raw and str(raw) not in ("", "0", "false"))
            )
        elif kind in ("int", "float"):
            if raw is None or not str(raw).strip():
                continue  # blank = keep the existing value
            try:
                value = int(raw) if kind == "int" else float(raw)
            except (TypeError, ValueError):
                errors.append(_fmt_invalid(loc, path, raw))
                continue
            lo, hi = extra.get("min"), extra.get("max")
            if lo is not None and value < lo:
                errors.append(_fmt_invalid(loc, path, raw))
                continue
            if hi is not None and value > hi:
                errors.append(_fmt_invalid(loc, path, raw))
                continue
            _set_path(new_cfg, path, value)
        elif kind == "text":
            _set_path(new_cfg, path, "" if raw is None else str(raw))
        else:  # "str"
            _set_path(new_cfg, path, str(raw).strip() if raw is not None else "")
    return new_cfg, errors