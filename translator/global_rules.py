"""Global cross-chapter rules manager for translation consistency.

Maintains a cumulative character dictionary + addressing rules that evolve
as the translator processes each chapter.  Rules are persisted to disk so
they survive server restarts and can be inspected/edited by the user.
"""

import json
import os
import re
import threading
import time
import logging
from dataclasses import dataclass, field, asdict


@dataclass
class CharacterEntry:
    """A single character's addressing profile."""
    names: list[str] = field(default_factory=list)       # aliases / names used in text
    calls: dict[str, str] = field(default_factory=dict)  # {target_name: "xung hô"}
    pronouns: dict[str, str] = field(default_factory=dict)  # {context: "pronoun"}
    notes: str = ""


@dataclass
class GlobalRulesState:
    """Full state of the global rules for a translation job."""
    characters: dict[str, CharacterEntry] = field(default_factory=dict)
    narration_defaults: dict[str, str] = field(default_factory=dict)
    fallback: str = ""
    version: int = 1
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "characters": {
                name: asdict(entry) for name, entry in self.characters.items()
            },
            "narration_defaults": self.narration_defaults,
            "fallback": self.fallback,
            "version": self.version,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "GlobalRulesState":
        chars = {}
        for name, entry_d in d.get("characters", {}).items():
            chars[name] = CharacterEntry(
                names=entry_d.get("names", []),
                calls=entry_d.get("calls", {}),
                pronouns=entry_d.get("pronouns", {}),
                notes=entry_d.get("notes", ""),
            )
        return cls(
            characters=chars,
            narration_defaults=d.get("narration_defaults", {}),
            fallback=d.get("fallback", ""),
            version=d.get("version", 1),
            updated_at=d.get("updated_at", ""),
        )


class GlobalRulesManager:
    """Thread-safe manager for cross-chapter consistency rules.

    Usage::

        mgr = GlobalRulesManager(path="jobs/abc123/rules.json")
        mgr.load()                          # load from disk (or start empty)

        # After LLM analysis of a chapter:
        mgr.merge_from_analysis(llm_output_text, chapter_number=5)
        rules_text = mgr.to_prompt_text()   # inject into translation prompt
        mgr.save()                          # persist to disk
    """

    def __init__(self, path: str):
        self._path = path
        self._state = GlobalRulesState()
        self._lock = threading.Lock()
        self._chapter_analyses: dict[int, str] = {}  # chapter_number -> raw LLM output

    # ── persistence ────────────────────────────────────────────────

    def load(self) -> None:
        """Load state from disk.  Missing file → start empty."""
        try:
            if os.path.exists(self._path):
                with open(self._path, encoding="utf-8") as f:
                    data = json.load(f)
                self._state = GlobalRulesState.from_dict(data)
                self._chapter_analyses = data.get("_chapter_analyses", {})
                logging.info("Loaded global rules: %d characters", len(self._state.characters))
        except Exception as exc:
            logging.warning("Failed to load global rules: %s", exc)
            self._state = GlobalRulesState()

    def save(self) -> None:
        """Persist state to disk."""
        try:
            self._state.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
            data = self._state.to_dict()
            data["_chapter_analyses"] = self._chapter_analyses
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            logging.warning("Failed to save global rules: %s", exc)

    # ── merge logic ────────────────────────────────────────────────

    def merge_from_analysis(self, llm_output: str, chapter_number: int | None = None) -> None:
        """Parse LLM consistency output and merge into global state.

        Expected LLM output format::

            1) Characters: 顾言, 小冷, 夜夜
            2) Address rules:
               顾言 -> 小冷: xưng "ngươi", gọi "小冷"
               小冷 -> 顾言: xưng "ta", gọi "顾言"
            3) Narration:顾言 → "hắn", 小冷 → "nàng"
            4) Fallback: xưng "ta"
        """
        if not llm_output or not llm_output.strip():
            return

        with self._lock:
            if chapter_number is not None:
                self._chapter_analyses[chapter_number] = llm_output

            characters = self._parse_characters(llm_output)
            address_rules = self._parse_address_rules(llm_output)
            narration = self._parse_narration(llm_output)
            fallback = self._parse_fallback(llm_output)

            # Merge characters
            for char_name, aliases in characters.items():
                if char_name not in self._state.characters:
                    self._state.characters[char_name] = CharacterEntry()
                entry = self._state.characters[char_name]
                for alias in aliases:
                    if alias not in entry.names:
                        entry.names.append(alias)

            # Merge address rules: only ADD new rules, never overwrite existing
            for speaker, targets in address_rules.items():
                if speaker not in self._state.characters:
                    self._state.characters[speaker] = CharacterEntry()
                entry = self._state.characters[speaker]
                for target, form in targets.items():
                    if target not in entry.calls:
                        entry.calls[target] = form
                    # If existing rule is different, log a conflict but keep old
                    elif entry.calls[target] != form:
                        logging.info(
                            "Rule conflict %s→%s: keeping '%s', new '%s' ignored",
                            speaker, target, entry.calls[target], form,
                        )

            # Merge narration defaults (only fill missing)
            for ctx, pronoun in narration.items():
                if ctx not in self._state.narration_defaults:
                    self._state.narration_defaults[ctx] = pronoun

            # Fallback
            if fallback and not self._state.fallback:
                self._state.fallback = fallback

    # ── prompt text generation ─────────────────────────────────────

    def to_prompt_text(self) -> str:
        """Render the global rules as plain text for injection into the
        translation prompt (inside <CHAPTER_RULES> tags)."""
        with self._lock:
            lines = []
            if self._state.characters:
                names = ", ".join(self._state.characters.keys())
                lines.append(f"Characters: {names}")
                lines.append("")
                lines.append("Address rules (global — must follow consistently):")
                for char, entry in self._state.characters.items():
                    for target, form in entry.calls.items():
                        lines.append(f"  {char} -> {target}: {form}")
                lines.append("")

            if self._state.narration_defaults:
                lines.append("Narration defaults:")
                for ctx, pronoun in self._state.narration_defaults.items():
                    lines.append(f"  {ctx}: {pronoun}")
                lines.append("")

            if self._state.fallback:
                lines.append(f"Default fallback: {self._state.fallback}")

            return "\n".join(lines)

    def get_raw_analysis(self, chapter_number: int) -> str | None:
        """Return the raw LLM output for a specific chapter (for UI display)."""
        with self._lock:
            return self._chapter_analyses.get(chapter_number)

    def get_all_analyses(self) -> dict[int, str]:
        """Return all raw chapter analyses."""
        with self._lock:
            return dict(self._chapter_analyses)

    @property
    def character_count(self) -> int:
        with self._lock:
            return len(self._state.characters)

    @property
    def state(self) -> GlobalRulesState:
        with self._lock:
            return self._state

    # ── LLM output parsers ─────────────────────────────────────────

    @staticmethod
    def _parse_characters(text: str) -> dict[str, list[str]]:
        """Extract '1) Characters: A, B, C' section."""
        result = {}
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith("1)") or stripped.lower().startswith("characters"):
                # everything after the colon
                part = stripped.split(":", 1)[-1] if ":" in stripped else stripped
                # remove numbering prefix if any
                part = re.sub(r"^[\d\)]+\s*", "", part).strip()
                if part:
                    names = [n.strip() for n in re.split(r"[,;]", part) if n.strip()]
                    if names:
                        result[names[0]] = names  # first name is primary
        return result

    @staticmethod
    def _parse_address_rules(text: str) -> dict[str, dict[str, str]]:
        """Extract '2) Address rules:' section."""
        result: dict[str, dict[str, str]] = {}
        in_section = False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith("2)") or stripped.lower().startswith("address"):
                in_section = True
                continue
            if in_section:
                # stop at next section
                if re.match(r"^\d+\)", stripped) or stripped.lower().startswith("narration"):
                    break
                # parse "A -> B: xung ho"
                match = re.match(r"(.+?)\s*->\s*(.+?):\s*(.+)", stripped)
                if match:
                    speaker = match.group(1).strip()
                    target = match.group(2).strip()
                    form = match.group(3).strip()
                    if speaker not in result:
                        result[speaker] = {}
                    result[speaker][target] = form
        return result

    @staticmethod
    def _parse_narration(text: str) -> dict[str, str]:
        """Extract '3) Narration' section."""
        result = {}
        in_section = False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith("3)") or stripped.lower().startswith("narration"):
                in_section = True
                continue
            if in_section:
                if re.match(r"^\d+\)", stripped) or stripped.lower().startswith("default") or stripped.lower().startswith("fallback"):
                    break
                # parse "char: pronoun" or "char → pronoun"
                match = re.match(r"(.+?):\s*(.+)", stripped)
                if not match:
                    match = re.match(r"(.+?)\s*→\s*(.+)", stripped)
                if match:
                    key = match.group(1).strip()
                    val = match.group(2).strip()
                    result[key] = val
        return result

    @staticmethod
    def _parse_fallback(text: str) -> str:
        """Extract '4) Default fallback' line."""
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith("4)") or stripped.lower().startswith("default fallback"):
                part = stripped.split(":", 1)[-1] if ":" in stripped else stripped
                part = re.sub(r"^[\d\)]+\s*", "", part).strip()
                part = re.sub(r"^default fallback:?\s*", "", part, flags=re.IGNORECASE).strip()
                return part
        return ""
