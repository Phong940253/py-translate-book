"""Safe read/write of config.yaml.

- ``load_config`` returns the parsed dict.
- ``mask_config`` hides API keys for display (replaced with ********).
- ``save_config_text`` parses edited YAML, restores any API key the user left
  as ******** (so they don't accidentally wipe a real key), and writes a
  ``.bak`` backup before overwriting.
"""

import copy
import os
import shutil

import yaml

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.yaml")

MASKED = "********"


def _path_or_default(path: str | None) -> str:
    # Resolved at call time so tests (and callers) can point the store at a
    # different file by patching DEFAULT_CONFIG_PATH.
    return path or DEFAULT_CONFIG_PATH


def load_config(path: str | None = None) -> dict:
    path = _path_or_default(path)
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def mask_config(cfg: dict) -> dict:
    c = copy.deepcopy(cfg)
    for section, value in c.items():
        if isinstance(value, dict) and value.get("api_key"):
            value["api_key"] = MASKED
    return c


def save_config_text(text: str, path: str | None = None) -> dict:
    path = _path_or_default(path)
    new_cfg = yaml.safe_load(text)
    if not isinstance(new_cfg, dict):
        raise ValueError("Config must be a YAML mapping at the top level")

    existing = load_config(path)

    # Restore masked API keys from the existing config (any provider section).
    for section, new_sec in new_cfg.items():
        if not isinstance(new_sec, dict):
            continue
        old_sec = existing.get(section)
        if isinstance(old_sec, dict) and old_sec.get("api_key"):
            if new_sec.get("api_key") == MASKED:
                new_sec["api_key"] = old_sec["api_key"]

    if os.path.exists(path):
        shutil.copy2(path, path + ".bak")

    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(new_cfg, f, allow_unicode=True, sort_keys=False)

    return new_cfg


def save_config_dict(cfg: dict, path: str | None = None) -> dict:
    """Write a config dict directly (used by the API Keys form).

    Unlike ``save_config_text`` there is no MASKED placeholder to restore —
    the form keeps blank fields as "keep existing key", and the caller builds
    the final dict in memory. Backup + atomic write behave identically.
    """
    path = _path_or_default(path)
    if not isinstance(cfg, dict):
        raise ValueError("Config must be a mapping at the top level")

    if os.path.exists(path):
        shutil.copy2(path, path + ".bak")

    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)

    return cfg
