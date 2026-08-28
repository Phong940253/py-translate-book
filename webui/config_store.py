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


def load_config(path: str = DEFAULT_CONFIG_PATH) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def mask_config(cfg: dict) -> dict:
    c = copy.deepcopy(cfg)
    for section in ("openai", "gemini", "webai", "groq"):
        if isinstance(c.get(section), dict) and c[section].get("api_key"):
            c[section]["api_key"] = MASKED
    return c


def save_config_text(text: str, path: str = DEFAULT_CONFIG_PATH) -> dict:
    new_cfg = yaml.safe_load(text)
    if not isinstance(new_cfg, dict):
        raise ValueError("Config must be a YAML mapping at the top level")

    existing = load_config(path)

    # Restore masked API keys from the existing config.
    for section in ("openai", "gemini", "webai", "groq"):
        new_sec = new_cfg.get(section)
        old_sec = existing.get(section) if isinstance(existing, dict) else None
        if isinstance(new_sec, dict) and isinstance(old_sec, dict):
            if new_sec.get("api_key") == MASKED and old_sec.get("api_key"):
                new_sec["api_key"] = old_sec["api_key"]

    if os.path.exists(path):
        shutil.copy2(path, path + ".bak")

    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(new_cfg, f, allow_unicode=True, sort_keys=False)

    return new_cfg
