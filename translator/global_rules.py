"""Global cross-chapter rules manager for translation consistency.

Maintains a cumulative character dictionary + addressing rules that evolve
as the translator processes each chapter.  Rules are persisted to disk so
they survive server restarts and can be inspected/edited by the user.

The dictionary grows without bound (78k+ characters on a long book), so the
prompt text is *scoped* to the characters of the chapter being translated
(``only=``) and *capped* (``budget=``) — narration defaults and the fallback
line are always reserved a place.
"""

import json
import os
import re
import threading
import time
import logging
from dataclasses import dataclass, field, asdict


def _norm_chapter_map(raw) -> dict:
    """JSON turns integer object keys into strings — map them back."""
    out = {}
    for key, value in (raw or {}).items():
        try:
            out[int(key)] = value
        except (TypeError, ValueError):
            continue
    return out


def _strip_numbering(part: str) -> str:
    """``' 1) Bạch Thanh Hạ '`` → ``'Bạch Thanh Hạ'``."""
    name = re.sub(r"^\s*\d\s*\)\s*", "", part).strip().rstrip(":").strip()
    return name.strip("\"'“”")


def _target_in_scope(target: str, scope: set[str]) -> bool:
    """An addressee may be written as ``"A, B"`` — any part counts."""
    if target in scope:
        return True
    return any(part.strip() in scope for part in target.split(","))


@dataclass
class CharacterEntry:
    """A single character's addressing profile."""
    names: list[str] = field(default_factory=list)       # aliases / names used in text
    calls: dict[str, str] = field(default_factory=dict)  # {target_name: "xung hô"}
    pronouns: dict[str, str] = field(default_factory=dict)  # {context: "pronoun"}
    notes: str = ""
    last_seen: int = 0   # chapter that last mentioned this character (recency)


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
                last_seen=int(entry_d.get("last_seen", 0) or 0),
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

        # After LLM analysis of a chapter.  content_hash fingerprints the
        # chapter text, so a restarted job reuses the stored analysis:
        mgr.merge_from_analysis(text, chapter_number=5, content_hash=h)
        mgr.save()                          # persist to disk

        # Inject into the translation prompt — only the characters this
        # chapter has, and never more than `budget` characters:
        rules_text = mgr.to_prompt_text(only={"Bạch Thanh Hạ"}, budget=12000)
    """

    def __init__(self, path: str):
        self._path = path
        self._state = GlobalRulesState()
        self._lock = threading.Lock()
        self._chapter_analyses: dict[int, str] = {}  # chapter_number -> raw LLM output
        self._analysis_hashes: dict[int, str] = {}   # chapter_number -> sha1(content)

    # ── persistence ────────────────────────────────────────────────

    def load(self) -> None:
        """Load state from disk.  Missing file → start empty."""
        try:
            if os.path.exists(self._path):
                with open(self._path, encoding="utf-8") as f:
                    data = json.load(f)
                self._state = GlobalRulesState.from_dict(data)
                # JSON object keys are strings — normalise back to chapter numbers
                # so a restarted job can still find (and reuse) past analyses.
                self._chapter_analyses = _norm_chapter_map(data.get("_chapter_analyses"))
                self._analysis_hashes = _norm_chapter_map(data.get("_analysis_hashes"))
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
            data["_analysis_hashes"] = self._analysis_hashes
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            logging.warning("Failed to save global rules: %s", exc)

    # ── merge logic ────────────────────────────────────────────────

    def merge_from_analysis(
        self,
        llm_output: str,
        chapter_number: int | None = None,
        content_hash: str | None = None,
    ) -> None:
        """Parse LLM consistency output and merge into global state.

        ``content_hash`` fingerprints the chapter text the analysis was made
        from, so the next run can reuse the stored analysis instead of paying
        for another LLM call (see :meth:`has_analysis`).

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
                if content_hash:
                    self._analysis_hashes[chapter_number] = content_hash

            characters = self._parse_characters(llm_output)
            address_rules = self._parse_address_rules(llm_output)
            narration = self._parse_narration(llm_output)
            fallback = self._parse_fallback(llm_output)

            # Merge characters
            for char_name, aliases in characters.items():
                if char_name not in self._state.characters:
                    self._state.characters[char_name] = CharacterEntry()
                entry = self._state.characters[char_name]
                self._touch(entry, chapter_number)
                for alias in aliases:
                    if alias not in entry.names:
                        entry.names.append(alias)

            # Merge address rules: only ADD new rules, never overwrite existing
            for speaker, targets in address_rules.items():
                if speaker not in self._state.characters:
                    self._state.characters[speaker] = CharacterEntry()
                entry = self._state.characters[speaker]
                self._touch(entry, chapter_number)
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

    def to_prompt_text(
        self,
        only: set[str] | None = None,
        budget: int | None = None,
    ) -> str:
        """Render the global rules as plain text for injection into the
        translation prompt (inside <CHAPTER_RULES> tags).

        only   — render only these characters (keeps the prompt scoped to the
                 characters actually present in the chapter being translated);
                 ``None`` renders the whole dictionary.  An empty/unknown set
                 falls back to the full dictionary rather than an empty block.
        budget — hard cap in characters.  The small narration/fallback block
                 is reserved first, then the name header, then as many address
                 rules as fit (most recently active characters first), so the
                 prompt never grows past the cap no matter how many characters
                 accumulate.
        """
        with self._lock:
            scope = None
            if only:
                scope = {str(n).strip() for n in only if str(n).strip()} or None
            names = self._ordered_names(scope)
            if scope is not None and not names:
                # The scope named nothing we know — render the budgeted full
                # dictionary instead of an empty <CHAPTER_RULES> block.
                scope = None
                names = self._ordered_names(None)
            tail = self._tail_text()

            reserve = (len(tail) + 1) if tail else 0
            limit = None if budget is None else max(0, int(budget) - reserve)

            lines = self._header_lines(names, limit)
            used = sum(len(line) + 1 for line in lines) + reserve
            for line in self._rule_lines(scope):
                size = len(line) + 1
                if limit is not None and used + size > limit:
                    break
                lines.append(line)
                used += size

            if tail:
                if lines:
                    lines.append("")
                lines.append(tail)
            return "\n".join(lines)

    # Characters in "most recently active first" order (ties keep the
    # insertion order), narrowed to ``scope`` when one is given. Lock held.
    def _ordered_names(self, scope: set[str] | None) -> list[str]:
        seen_order = {name: i for i, name in enumerate(self._state.characters)}
        names = [
            name for name in self._state.characters
            if scope is None or name in scope
        ]
        names.sort(key=lambda n: (-self._state.characters[n].last_seen, seen_order[n]))
        return names

    # Address-rule lines for one scope. A scoped render only keeps rules whose
    # addressee is in scope too — the pair plays out in this very chapter, so
    # rules about everyone else are noise. Lock held.
    def _rule_lines(self, scope: set[str] | None):
        for name in self._ordered_names(scope):
            items = list(self._state.characters[name].calls.items())
            if scope is not None:
                items = [kv for kv in items if _target_in_scope(kv[0], scope)]
            for target, form in items:
                yield f"  {name} -> {target}: {form}"

    # "Characters: …" + section header; the name list shrinks to fit ``limit``.
    def _header_lines(self, names: list[str], limit: int | None) -> list[str]:
        if not names:
            return []
        rule_head = "Address rules (global — must follow consistently):"
        if limit is None:
            keep = names
        else:
            # fixed cost: "Characters: " + blank line + section header
            used = len("Characters: ") + 1 + 1 + len(rule_head) + 1
            keep = []
            for name in names:
                add = len(name) + (2 if keep else 0)   # ", " separator
                if used + add > limit:
                    break
                used += add
                keep.append(name)
            if not keep:
                return []
        return ["Characters: " + ", ".join(keep), "", rule_head]

    # Small block that must survive any budget. Lock held.
    def _tail_text(self) -> str:
        lines = []
        if self._state.narration_defaults:
            lines.append("Narration defaults:")
            for ctx, pronoun in self._state.narration_defaults.items():
                lines.append(f"  {ctx}: {pronoun}")
            lines.append("")
        if self._state.fallback:
            lines.append(f"Default fallback: {self._state.fallback}")
        return "\n".join(lines)

    @staticmethod
    def _touch(entry: CharacterEntry, chapter_number: int | None) -> None:
        """Mark a character as active in ``chapter_number`` (recency sort)."""
        if chapter_number is None:
            return
        chapter = int(chapter_number)
        if chapter > entry.last_seen:
            entry.last_seen = chapter

    # ── chapter-scoped filtering ───────────────────────────────────

    @staticmethod
    def chars_in_analysis(text: str, known: set[str] | None = None) -> set[str]:
        """Character names mentioned in one chapter's raw analysis output.

        The LLM emits three different shapes — ``1) Characters: A, B``,
        ``1) A, B`` and a bare comma list — so the section-1 line(s) and every
        ``A -> B:`` rule line are all scanned.  Names that are not in
        ``known`` (when given) are dropped; otherwise the caller intersects.
        """
        found: set[str] = set()
        if not text:
            return found
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            if "->" in line:                       # speaker -> addressee: form
                left, right = line.split("->", 1)
                right = right.split(":", 1)[0]
                for part in re.split(r"[,;]", left.split(":", 1)[0]) + re.split(r"[,;]", right):
                    name = _strip_numbering(part)
                    if name:
                        found.add(name)
                continue
            low = line.lower()
            if low.startswith(("2)", "3)", "4)", "address", "narration",
                               "default", "fallback")):
                continue                            # other sections, not names
            if low.startswith("1)") or low.startswith("characters"):
                body = line.split(":", 1)[1] if ":" in line else line
            elif ":" in line:
                body = line.split(":", 1)[0]        # "3) Narration…: …" / "A: pronoun"
            else:
                body = line                         # bare comma list
            for part in re.split(r"[,;]", body):
                name = _strip_numbering(part)
                if name:
                    found.add(name)
        if known is not None:
            found &= known
        return found

    # ── per-chapter analysis reuse ─────────────────────────────────

    def get_analysis(self, chapter_number: int | None) -> str | None:
        """Raw LLM output stored for a chapter (if any)."""
        if chapter_number is None:
            return None
        with self._lock:
            return self._chapter_analyses.get(int(chapter_number))

    def has_analysis(self, chapter_number: int | None, content_hash: str | None) -> bool:
        """True when a stored analysis matches this chapter text — the caller
        can skip the analysis LLM call and reuse it verbatim."""
        if chapter_number is None or not content_hash:
            return False
        with self._lock:
            chapter = int(chapter_number)
            return (
                chapter in self._chapter_analyses
                and self._analysis_hashes.get(chapter) == content_hash
            )

    def get_raw_analysis(self, chapter_number: int) -> str | None:
        """Return the raw LLM output for a specific chapter (for UI display)."""
        try:
            chapter = int(chapter_number)
        except (TypeError, ValueError):
            return None
        with self._lock:
            return self._chapter_analyses.get(chapter)

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
