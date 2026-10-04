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
import unicodedata
import logging
from dataclasses import dataclass, field, asdict

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

# Standard Mandarin pinyin syllables (toneless).  A Latin word that is not in
# this set cannot be pinyin — which is what keeps `_looks_like_pinyin` from
# flagging Vietnamese names written without diacritics (Dinh/Van/Thanh are not
# pinyin syllables, Bai/Qing/Xia are).
_PINYIN_SYLLABLES = set(
    """
    a ai an ang ao ba bai ban bang bao bei ben beng bi bian biao bie bin bing bo bu
    ca cai can cang cao ce cen ceng cha chai chan chang chao che chen cheng chong chou
    chu chua chuai chuan chuang chui chun chuo ci cong cou cu cuan cui cun cuo
    da dai dan dang dao de dei den deng di dia dian diao die ding diu dong dou du
    duan dui dun duo e ei en eng er fa fan fang fei fen feng fo fou fu
    ga gai gan gang gao ge gei gen geng gong gou gu gua guai guan guang gui gun guo
    ha hai han hang hao he hei hen heng hong hou hu hua huai huan huang hui hun huo
    ji jia jian jiang jiao jie jin jing jiong jiu ju juan jue jun
    ka kai kan kang kao ke ken keng kong kou ku kua kuai kuan kuang kui kun kuo
    la lai lan lang lao le lei leng li lia lian liang liao lie lin ling liu long lou
    lu luan lun luo lv lve ma mai man mang mao me mei men meng mi mian miao mie min
    ming miu mo mou mu na nai nan nang nao ne nei nen neng ni nian niang niao nie
    nin ning niu nong nu nuan nuo nv nve o ou pa pai pan pang pao pei pen peng pi
    pian piao pie pin ping po pou pu qi qia qian qiang qiao qie qin qing qiong qiu
    qu quan que qun ran rang rao re ren reng ri rong rou ru rua ruan rui run ruo
    sa sai san sang sao se sen seng sha shai shan shang shao she shei shen sheng shou
    shu shua shuai shuan shuang shui shun shuo si song sou su suan sui sun suo
    ta tai tan tang tao te tei teng ti tian tiao tie ting tong tou tu tuan tui tun tuo
    wa wai wan wang wei wen weng wo wu xi xia xian xiang xiao xie xin xing xiong xiu
    xu xuan xue xun ya yan yang yao ye yi yin ying yo yong you yu yuan yue yun
    za zai zan zang zao ze zei zen zeng zha zhai zhan zhang zhao zhe zhei zhen zheng
    zhi zhong zhou zhu zhua zhuai zhuan zhuang zhui zhun zhuo zi zong zou zu zuan
    zui zun zuo
    """.split()
)

# Capitalized ASCII name-like run, 2-3 words: "Bai Qing Xia", "Anh Qiu".
# Diacritics are excluded on purpose — a Vietnamese name written properly
# (Bạch Thanh Hạ) never matches, a pinyin name always does.
_NAME_CANDIDATE_RE = re.compile(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2}\b")


def has_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text or ""))


def fold_name(name: str) -> str:
    """Diacritic-insensitive comparison key: 'Bạch Thanh Hạ' → 'bach thanh ha'."""
    value = unicodedata.normalize("NFD", name or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.replace("đ", "d").replace("Đ", "D")
    return re.sub(r"\s+", " ", value).strip().lower()


def looks_like_pinyin(name: str) -> bool:
    """True for a 2-3 word Latin string built only from pinyin syllables."""
    value = (name or "").strip()
    if not value or has_cjk(value):
        return False
    tokens = value.split()
    if not 2 <= len(tokens) <= 3:
        return False
    if not all(tok.isascii() and tok.isalpha() for tok in tokens):
        return False
    if not all(tok.lower() in _PINYIN_SYLLABLES for tok in tokens):
        return False
    return all(tok[0].isupper() for tok in tokens)


def _safe_to_rewrite(value: str) -> bool:
    """Is this form shape-safe to replace literally inside a written EPUB?

    Only proper-name-shaped strings qualify: they start with a capital.
    Role labels (``đạo trưởng``, ``học sinh``) and mid-sentence phrases are
    ordinary words — swapping them in place would corrupt prose, so both the
    surface and its canonical must be name-shaped."""
    text = (value or "").strip()
    return bool(text) and text[:1].isupper()


def split_name_pair(part: str) -> tuple[str | None, str]:
    """Split one ``Characters:`` item that carries its source form.

    ``'白清夏=Bạch Thanh Hạ'`` → ``('白清夏', 'Bạch Thanh Hạ')`` (Hán tự)
    ``'Bai Qing Xia=Bạch Thanh Hạ'`` → ``('Bai Qing Xia', ...)`` (pinyin)
    ``'Bạch Thanh Hạ'`` → ``(None, 'Bạch Thanh Hạ')``  (no pair — unchanged)
    """
    value = (part or "").strip()
    if "=" not in value:
        return None, value
    left, right = value.split("=", 1)
    left, right = left.strip(), right.strip()
    if not right:
        return None, left
    if has_cjk(left) and not has_cjk(right):
        return (left or None), right
    if has_cjk(right) and not has_cjk(left):
        return (right or None), left
    # Neither side is Chinese (pinyin=Vietnamese) or both are: the prompt asks
    # for "source=Vietnamese", so keep the right-hand side as the name.
    return (left or None), right


def parse_repair_output(text: str) -> tuple[list[tuple[str, str | None, str]], list[str]]:
    """Parse a name-repair reply.

    ``'Bai Qing Xia -> 白清夏 = Bạch Thanh Hạ'`` → ``('Bai Qing Xia', '白清夏', 'Bạch Thanh Hạ')``
    ``'KEEP: Hải An'`` → kept verbatim.

    Returns ``(mappings, keep)``.
    """
    mappings: list[tuple[str, str | None, str]] = []
    keep: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip().lstrip("-•* ").strip()
        if not line:
            continue
        low = line.lower()
        if low.startswith("keep"):
            value = line.split(":", 1)[-1].strip() if ":" in line else ""
            if value:
                keep.append(value)
            continue
        if "=" not in line:
            continue
        if "->" in line:
            left, right = line.split("->", 1)
            form = left.strip()
            source, vietnamese = (right.split("=", 1) + [""])[:2]
            source, vietnamese = source.strip(), vietnamese.strip()
            # the model sometimes swaps the sides — Chinese belongs on the left
            if has_cjk(vietnamese) and not has_cjk(source):
                source, vietnamese = vietnamese, source
            if not form or not vietnamese or has_cjk(vietnamese):
                continue
            mappings.append((form, source or None, vietnamese))
        else:
            # 'Bai Qing Xia = Bạch Thanh Hạ' — no Chinese recovered
            form, vietnamese = (line.split("=", 1) + [""])[:2]
            form, vietnamese = form.strip(), vietnamese.strip()
            if not form or not vietnamese or has_cjk(vietnamese):
                continue
            mappings.append((form, None, vietnamese))
    return mappings, keep


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


def _norm_glossary(raw) -> list[dict]:
    """Keep glossary groups well-formed: one canonical + string lists."""
    out: list[dict] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        canonical = str(item.get("canonical") or "").strip()
        if not canonical:
            continue
        out.append(
            {
                "canonical": canonical,
                "variants": [str(v).strip() for v in item.get("variants", []) if str(v).strip()],
                "cn": [str(v).strip() for v in item.get("cn", []) if str(v).strip()],
                "pinyin": [str(v).strip() for v in item.get("pinyin", []) if str(v).strip()],
            }
        )
    return out


@dataclass
class GlobalRulesState:
    """Full state of the global rules for a translation job."""
    characters: dict[str, CharacterEntry] = field(default_factory=dict)
    narration_defaults: dict[str, str] = field(default_factory=dict)
    fallback: str = ""
    version: int = 1
    updated_at: str = ""
    # Canonical-name glossary (user-editable, job preserves manual edits):
    # [{"canonical": "Bạch Thanh Hạ", "variants": [...], "cn": [...],
    #   "pinyin": [...]}] — the single source of truth for how a person is
    # spelled, used to canonicalise analyses and to block pinyin in output.
    name_glossary: list[dict] = field(default_factory=list)
    # Forms the user marked "this is not pinyin / keep as-is" — never flagged.
    name_whitelist: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "characters": {
                name: asdict(entry) for name, entry in self.characters.items()
            },
            "narration_defaults": self.narration_defaults,
            "fallback": self.fallback,
            "version": self.version,
            "updated_at": self.updated_at,
            "name_glossary": self.name_glossary,
            "name_whitelist": self.name_whitelist,
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
            name_glossary=_norm_glossary(d.get("name_glossary")),
            name_whitelist=[
                str(v).strip() for v in d.get("name_whitelist", []) if str(v).strip()
            ],
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
        # Names the analysis produced that we refuse to make dictionary keys
        # (pinyin/Chinese with no known mapping) — kept for the repair pass
        # and the names report instead of teaching the model a wrong spelling.
        self._pending_names: dict[str, str] = {}     # name -> reason
        self._disk_mtime: float = 0.0                # of our last load/save

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
                self._pending_names = {
                    str(k): str(v)
                    for k, v in (data.get("_pending_names") or {}).items()
                }
                self._disk_mtime = os.path.getmtime(self._path)
                logging.info(
                    "Loaded global rules: %d characters, %d glossary entries",
                    len(self._state.characters),
                    len(self._state.name_glossary),
                )
        except Exception as exc:
            logging.warning("Failed to load global rules: %s", exc)
            self._state = GlobalRulesState()

    def _refresh_user_fields_from_disk(self) -> None:
        """Let hand-edits win: re-read ``name_glossary``/``name_whitelist``
        from disk when the file changed under us, so saving the (machine
        owned) analyses never reverts an edit the user made in their editor.
        Lock held."""
        try:
            mtime = os.path.getmtime(self._path)
        except OSError:
            return
        if not self._disk_mtime or mtime == self._disk_mtime:
            return
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            logging.warning("Could not re-read user name fields: %s", exc)
            return
        if "name_glossary" in data:
            disk_glossary = _norm_glossary(data.get("name_glossary"))
            if disk_glossary != self._state.name_glossary:
                logging.info("Adopting hand-edited name_glossary from disk (%d entries)",
                             len(disk_glossary))
                self._state.name_glossary = disk_glossary
        if "name_whitelist" in data:
            disk_whitelist = [
                str(v).strip() for v in data.get("name_whitelist", []) if str(v).strip()
            ]
            if disk_whitelist != self._state.name_whitelist:
                logging.info("Adopting hand-edited name_whitelist from disk (%d entries)",
                             len(disk_whitelist))
                self._state.name_whitelist = disk_whitelist

    def save(self) -> None:
        """Persist state to disk."""
        try:
            with self._lock:
                self._refresh_user_fields_from_disk()
                self._state.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
                data = self._state.to_dict()
                data["_chapter_analyses"] = self._chapter_analyses
                data["_analysis_hashes"] = self._analysis_hashes
                data["_pending_names"] = self._pending_names
                os.makedirs(os.path.dirname(self._path), exist_ok=True)
                with open(self._path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                self._disk_mtime = os.path.getmtime(self._path)
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

            1) Characters: 白清夏=Bạch Thanh Hạ, 陆远秋=Lục Viễn Thu
            2) Address rules:
               白清夏 -> 陆远秋: xưng "ngươi", gọi "Bạch Thanh Hạ"
               陆远秋 -> 白清夏: xưng "ta", gọi "Lục Viễn Thu"
            3) Narration: Bạch Thanh Hạ → "hắn"
            4) Default fallback: xưng "ta"

        Every name goes through :meth:`canonical_name` first: a spelling the
        glossary already knows folds onto the canonical one, Chinese/pinyin
        names with no known mapping never become dictionary keys (they are
        parked as *pending* for the repair pass instead), and
        ``source=Vietnamese`` pairs seed the glossary automatically — a new
        book therefore builds its own Chinese → Vietnamese name map as the
        chapters go by, with no manual migration step.
        """
        if not llm_output or not llm_output.strip():
            return

        with self._lock:
            if chapter_number is not None:
                self._chapter_analyses[chapter_number] = llm_output
                if content_hash:
                    self._analysis_hashes[chapter_number] = content_hash

            # Seed the glossary from "source=Vietnamese" pairs.
            for alias, vietnamese in self._glossary_pairs(llm_output):
                if not vietnamese:
                    continue
                self._add_glossary_entry_locked(
                    canonical=vietnamese,
                    cn=alias if has_cjk(alias) else None,
                    pinyin=alias if alias and not has_cjk(alias) else None,
                )

            characters = self._parse_characters(llm_output)
            address_rules = self._parse_address_rules(llm_output)
            narration = self._parse_narration(llm_output)
            fallback = self._parse_fallback(llm_output)

            # Merge characters
            for char_name, aliases in characters.items():
                resolved = self._resolve_locked(char_name)
                if resolved is None:
                    continue
                if resolved not in self._state.characters:
                    self._state.characters[resolved] = CharacterEntry()
                entry = self._state.characters[resolved]
                self._touch(entry, chapter_number)
                for alias in aliases:
                    if has_cjk(alias) or self._pinyin_suspicious_locked(alias):
                        resolved_alias = self._resolve_locked(alias)
                        if resolved_alias is None:
                            continue
                        alias = resolved_alias
                    if alias != resolved and alias not in entry.names:
                        entry.names.append(alias)

            # Merge address rules: only ADD new rules, never overwrite existing
            for speaker, targets in address_rules.items():
                speaker_key = self._resolve_locked(speaker)
                if speaker_key is None:
                    continue
                if speaker_key not in self._state.characters:
                    self._state.characters[speaker_key] = CharacterEntry()
                entry = self._state.characters[speaker_key]
                self._touch(entry, chapter_number)
                for target, form in targets.items():
                    target_key = self._resolve_target_locked(target)
                    if target_key is None:
                        continue
                    if target_key not in entry.calls:
                        entry.calls[target_key] = form
                    # If existing rule is different, log a conflict but keep old
                    elif entry.calls[target_key] != form:
                        logging.info(
                            "Rule conflict %s→%s: keeping '%s', new '%s' ignored",
                            speaker_key, target_key, entry.calls[target_key], form,
                        )

            # Merge narration defaults (only fill missing)
            for ctx, pronoun in narration.items():
                ctx_key = self._resolve_locked(ctx)
                if ctx_key is None or ctx_key in self._state.narration_defaults:
                    continue
                self._state.narration_defaults[ctx_key] = pronoun

            # Fallback
            if fallback and not self._state.fallback:
                self._state.fallback = fallback

    # ── name canonicalisation (glossary) ───────────────────────────

    @property
    def glossary(self) -> list[dict]:
        with self._lock:
            return [dict(entry) for entry in self._state.name_glossary]

    @property
    def whitelist(self) -> list[str]:
        with self._lock:
            return list(self._state.name_whitelist)

    @property
    def pending(self) -> dict[str, str]:
        """Names the analysis produced that we refused to memorise
        (pinyin/Chinese with no mapping yet) — input for the repair pass."""
        with self._lock:
            return dict(self._pending_names)

    def add_to_whitelist(self, name: str) -> None:
        value = (name or "").strip()
        if not value:
            return
        with self._lock:
            if value not in self._state.name_whitelist:
                self._state.name_whitelist.append(value)
            self._pending_names.pop(value, None)

    def add_glossary_entry(
        self,
        canonical: str,
        cn: str | None = None,
        pinyin: str | None = None,
        variant: str | None = None,
    ) -> None:
        with self._lock:
            self._add_glossary_entry_locked(canonical, cn=cn, pinyin=pinyin, variant=variant)

    def _add_glossary_entry_locked(
        self,
        canonical: str,
        cn: str | None = None,
        pinyin: str | None = None,
        variant: str | None = None,
    ) -> None:
        """Upsert one glossary group.  Chinese is never stored as the
        canonical spelling — it belongs on the ``cn`` side of the pair."""
        canonical = (canonical or "").strip()
        # a comma list is a description, not a name (legacy maps carried a few)
        if not canonical or has_cjk(canonical) or "," in canonical:
            return
        folded = fold_name(canonical)
        entry = None
        for candidate in self._state.name_glossary:
            if fold_name(candidate["canonical"]) == folded:
                entry = candidate
                break
        if entry is None:
            entry = {"canonical": canonical, "variants": [], "cn": [], "pinyin": []}
            self._state.name_glossary.append(entry)

        def _add(value: str | None, key: str) -> None:
            item = (value or "").strip()
            if not item or item == entry["canonical"] or item in entry[key]:
                return
            entry[key].append(item)

        _add(cn, "cn")
        _add(pinyin, "pinyin")
        _add(variant, "variants")
        # A differently-spelled canonical (diacritics!) becomes a variant of
        # the spelling we already committed to.
        if entry["canonical"] != canonical:
            _add(canonical, "variants")

    def canonical_name(self, name: str) -> str | None:
        """Canonical spelling of ``name``, or ``None`` when it is Chinese/
        pinyin with no known mapping (the caller should park it as pending).

        Pure — records nothing; merge/sanitize pass ``park=True`` internally.
        """
        with self._lock:
            return self._resolve_locked(name, park=False)

    def _resolve_locked(self, name: str, park: bool = True) -> str | None:
        value = (name or "").strip()
        if not value:
            return None
        if value in self._state.name_whitelist:
            return value

        folded = fold_name(value)
        for entry in self._state.name_glossary:
            surfaces = (
                [entry["canonical"]]
                + entry["variants"]
                + entry["cn"]
                + entry["pinyin"]
            )
            for surface in surfaces:
                if surface == value or (surface and fold_name(surface) == folded):
                    return entry["canonical"]

        # Chinese/pinyin with no glossary hit never becomes (or stays) a key —
        # a legacy dictionary full of such keys is exactly what sanitize fixes.
        if has_cjk(value):
            if park:
                self._pending_names.setdefault(value, "unmapped_chinese")
            return None
        if self._pinyin_suspicious_locked(value):
            if park:
                self._pending_names.setdefault(value, "pinyin")
            return None

        if value in self._state.characters:
            return value
        for key in self._state.characters:
            if fold_name(key) == folded:
                return key
        return value

    def _known_vn_tokens_locked(self) -> tuple[set[str], set[str]]:
        """Tokens and folded forms of every *sanctioned* Vietnamese name.

        Wrong spellings (variants) deliberately don't count: their tokens are
        exactly the ones that should still be flagged."""
        tokens: set[str] = set()
        folded: set[str] = {fold_name(key) for key in self._state.characters}
        for key in self._state.characters:
            tokens.update(fold_name(key).split())
        for entry in self._state.name_glossary:
            folded.add(fold_name(entry["canonical"]))
            tokens.update(fold_name(entry["canonical"]).split())
        for white in self._state.name_whitelist:
            folded.add(fold_name(white))
            folded.add((white or "").lower())
            tokens.update(fold_name(white).split())
        return tokens, folded

    def _pinyin_suspicious_locked(self, value: str) -> bool:
        """Is this Latin name probably pinyin/romanised Chinese?

        True when *every* token is a pinyin syllable (``Bai Qing Xia``) — the
        conservative test — or when it carries a pinyin syllable that no
        sanctioned Vietnamese name uses (``Anh Qiu``: ``Qiu`` never occurs in
        a Vietnamese spelling, ``Hai``/``Quan`` do, so ``Hai An`` passes).
        """
        text = (value or "").strip()
        if not text or has_cjk(text):
            return False
        tokens = text.split()
        if not tokens or len(tokens) > 4:
            return False
        if not all(tok.isascii() and tok.isalpha() for tok in tokens):
            return False                      # mixed script → Chinese path
        pinyin_hits = [
            tok for tok in tokens if tok.lower() in _PINYIN_SYLLABLES
        ]
        if not pinyin_hits:
            return False
        if len(pinyin_hits) == len(tokens):
            return True
        known_tokens, _ = self._known_vn_tokens_locked()
        return any(tok.lower() not in known_tokens for tok in pinyin_hits)

    def _resolve_target_locked(self, target: str) -> str | None:
        """Resolve one addressee; ``"A, B"`` composites resolve part by part."""
        value = (target or "").strip()
        if not value:
            return None
        if "," in value:
            parts = [part.strip() for part in value.split(",") if part.strip()]
            mapped = [self._resolve_locked(part) for part in parts]
            if not parts or any(item is None for item in mapped):
                return None
            return ", ".join(mapped)
        return self._resolve_locked(value)

    def add_pending(self, name: str, reason: str = "pinyin") -> None:
        value = (name or "").strip()
        if not value or value in self._state.name_whitelist:
            return
        with self._lock:
            self._pending_names.setdefault(value, reason)

    def drop_pending(self, name: str) -> None:
        """Forget a name from the repair queue — it is mapped now."""
        value = (name or "").strip()
        if not value:
            return
        with self._lock:
            self._pending_names.pop(value, None)

    def apply_repair(self, output: str) -> dict[str, str]:
        """Consume a name-repair reply: seed glossary groups, whitelist the
        forms the model said to keep, clear them from pending.

        Returns the applicable corrections ``{bad_form: canonical}`` so the
        retry prompt can show them inline."""
        mappings, keep = parse_repair_output(output)
        fixes: dict[str, str] = {}
        with self._lock:
            for form, chinese, vietnamese in mappings:
                if not vietnamese:
                    continue
                self._add_glossary_entry_locked(
                    vietnamese,
                    cn=chinese if chinese and has_cjk(chinese) else None,
                    pinyin=form if not has_cjk(form) else None,
                    variant=form if has_cjk(form) else None,
                )
                self._pending_names.pop(form, None)
                if form != vietnamese:
                    fixes[form] = vietnamese
            for form in keep:
                if form and form not in self._state.name_whitelist:
                    self._state.name_whitelist.append(form)
                self._pending_names.pop(form, None)
        if fixes or keep:
            logging.info(
                "Name repair applied: %d corrections, %d kept as-is",
                len(fixes), len(keep),
            )
        return fixes

    @staticmethod
    def _glossary_pairs(text: str) -> list[tuple[str, str]]:
        """``1) Characters: 白清夏=Bạch Thanh Hạ, …`` → ``[(source, vietnamese)]``."""
        pairs: list[tuple[str, str]] = []
        for line in text.splitlines():
            stripped = line.strip()
            low = stripped.lower()
            if not (low.startswith("1)") or low.startswith("characters")):
                continue
            part = stripped.split(":", 1)[-1] if ":" in stripped else stripped
            part = re.sub(r"^[\d\)]+\s*", "", part).strip()
            for item in re.split(r"[,;]", part):
                item = item.strip()
                if "=" not in item:
                    continue
                source, vietnamese = split_name_pair(item)
                if source and vietnamese and source != vietnamese:
                    pairs.append((source, vietnamese))
        return pairs

    def glossary_text(self, source_text: str, budget: int | None = None) -> str:
        """``NAME GLOSSARY`` block listing only the Chinese forms that occur
        in this chapter's source — a substring scan, no extra LLM call."""
        if not source_text:
            return ""
        with self._lock:
            pairs: list[tuple[str, str]] = []
            for entry in self._state.name_glossary:
                canonical = entry["canonical"]
                # Role labels the legacy maps carried over (学生 = học sinh,
                # 我 = tôi) are translation hints, not names: emitting them
                # would fill the block with common words and push real names
                # past the budget.  A name — even "cô Trương" — always has a
                # capitalised token.
                if not any(tok[:1].isupper() for tok in canonical.split()):
                    continue
                hit = next(
                    (cn for cn in entry["cn"] if cn and cn in source_text), None
                )
                if hit:
                    pairs.append((hit, canonical))
            if not pairs:
                return ""
            head = (
                "NAME GLOSSARY (Chinese source -> write the Vietnamese form exactly, "
                "never pinyin):"
            )
            if budget is not None and len(head) + 2 > budget:
                return ""                       # no room for a useful block
            lines = [head]
            used = len(head) + 1
            for source, vietnamese in pairs:
                piece = f"  {source} = {vietnamese}"
                size = len(piece) + 1
                if budget is not None and used + size > budget:
                    break
                lines.append(piece)
                used += size
        return "\n".join(lines)

    def pinyin_fixes(self, names: list[str]) -> dict[str, str]:
        """Known correction for each pinyin/variant form (``{bad: canonical}``)."""
        fixes: dict[str, str] = {}
        if not names:
            return fixes
        with self._lock:
            for name in names:
                value = (name or "").strip()
                if not value:
                    continue
                for entry in self._state.name_glossary:
                    surfaces = entry["pinyin"] + entry["variants"]
                    if value in surfaces:
                        fixes[value] = entry["canonical"]
                        break
        return fixes

    def detect_pinyin_names(self, text: str) -> list[str]:
        """Latin name-like runs in a translation that look like pinyin.

        A run is suspicious when it is 2-3 capitalized ASCII words, is not a
        known Vietnamese name (dictionary, canonical, whitelist), and carries
        at least one pinyin syllable that no known name uses — which is what
        separates 'Anh Qiu' (leak) from 'Dinh Van Quan' (Vietnamese without
        diacritics)."""
        if not text:
            return []
        with self._lock:
            _, known_folded = self._known_vn_tokens_locked()
            found: list[str] = []
            seen: set[str] = set()
            for match in _NAME_CANDIDATE_RE.finditer(text):
                candidate = match.group(0)
                if candidate in seen:
                    continue
                seen.add(candidate)
                if fold_name(candidate) in known_folded or candidate in known_folded:
                    continue                    # a sanctioned Vietnamese name
                tokens = candidate.split()
                if not 2 <= len(tokens) <= 3:
                    continue
                if self._pinyin_suspicious_locked(candidate):
                    found.append(candidate)
        return found

    def mapping(self) -> dict[str, str]:
        """Surface form → canonical spelling, for normalising a written EPUB.
        Chinese surfaces are excluded: the source title/text is never touched."""
        with self._lock:
            out: dict[str, str] = {}
            for entry in self._state.name_glossary:
                canonical = entry["canonical"]
                if not _safe_to_rewrite(canonical):
                    continue
                for surface in entry["variants"] + entry["pinyin"]:
                    if not surface or surface == canonical or has_cjk(surface):
                        continue
                    # a lowercase-initial surface ("anh Thu") is a common
                    # phrase as much as a name, and a case-only difference
                    # ("Đạo trưởng" -> "đạo trưởng") would lowercase text
                    # that is simply starting a sentence — rewriting either
                    # would corrupt ordinary prose.
                    if not _safe_to_rewrite(surface):
                        continue
                    if surface.lower() == canonical.lower():
                        continue
                    out[surface] = canonical
            canonical_by_fold = {
                fold_name(entry["canonical"]): entry["canonical"]
                for entry in self._state.name_glossary
            }
            for key in self._state.characters:
                canonical = canonical_by_fold.get(fold_name(key))
                if not canonical or canonical == key or has_cjk(key):
                    continue
                if not _safe_to_rewrite(canonical) or not _safe_to_rewrite(key):
                    continue
                if key.lower() == canonical.lower():
                    continue
                out[key] = canonical
        return out

    # ── one-time dictionary cleanup ────────────────────────────────

    def sanitize(self) -> dict:
        """Canonicalise every stored name in place: fold variants onto their
        glossary canonical (recording the rename as a variant so the EPUB
        normaliser applies the same fix), park unmappable Chinese/pinyin
        names instead of keeping them as keys, and make sure every addressee
        is a dictionary key — a dangling target gets scoped out of the prompt
        and its rule silently disappears."""
        stats = {
            "renamed": 0,
            "pending": 0,
            "targets_fixed": 0,
            "targets_dropped": 0,
            "aliases_cleaned": 0,
            "narration_fixed": 0,
        }
        with self._lock:
            # 1) keys → canonical spelling (or park).  A key that only differs
            #    by diacritics from another key is left for step 2 so the
            #    rename still records a variant the EPUB pass can apply.
            sibling_folds: dict[str, int] = {}
            for key in self._state.characters:
                sibling_folds[fold_name(key)] = sibling_folds.get(fold_name(key), 0) + 1
            for key in list(self._state.characters):
                resolved = self._resolve_locked(key, park=False)
                if resolved is None:
                    if sibling_folds.get(fold_name(key), 0) > 1:
                        continue                # dedupe in step 2 below
                    reason = (
                        "pinyin"
                        if self._pinyin_suspicious_locked(key)
                        else "unmapped_chinese"
                    )
                    self._pending_names.setdefault(key, reason)
                    del self._state.characters[key]
                    stats["pending"] += 1
                    continue
                if resolved == key:
                    continue
                self._add_glossary_entry_locked(resolved, variant=key)
                self._merge_entry_locked(key, resolved)
                stats["renamed"] += 1

            # 2) keys that only differ by diacritics/spacing but are not yet
            #    covered by the glossary
            groups: dict[str, list[str]] = {}
            for key in self._state.characters:
                groups.setdefault(fold_name(key), []).append(key)
            for folded, keys in groups.items():
                if len(keys) < 2:
                    continue
                gloss = next(
                    (
                        g for g in self._state.name_glossary
                        if fold_name(g["canonical"]) == folded
                    ),
                    None,
                )
                canonical = (
                    gloss["canonical"]
                    if gloss is not None
                    else max(
                        keys, key=lambda k: self._state.characters[k].last_seen
                    )
                )
                for key in keys:
                    if key == canonical:
                        continue
                    self._add_glossary_entry_locked(canonical, variant=key)
                    self._merge_entry_locked(key, canonical)
                    stats["renamed"] += 1

            # 3) aliases that are still Chinese/pinyin
            for key, entry in list(self._state.characters.items()):
                cleaned: list[str] = []
                for alias in entry.names:
                    if has_cjk(alias) or self._pinyin_suspicious_locked(alias):
                        resolved = self._resolve_locked(alias, park=False)
                        if resolved is None:
                            self._pending_names.setdefault(alias, "unmapped_alias")
                            stats["aliases_cleaned"] += 1
                            continue
                        alias = resolved
                    if alias != key and alias not in cleaned:
                        cleaned.append(alias)
                    elif alias == key:
                        stats["aliases_cleaned"] += 1
                entry.names = cleaned

            # 4) addressee targets: rewrite what we know, park what we don't,
            #    and materialise bare keys so scoped prompts keep the rule.
            for key, entry in list(self._state.characters.items()):
                for target in list(entry.calls):
                    resolved = self._resolve_target_locked(target)
                    if resolved is None:
                        # unmappable addressee (Chinese/pinyin with no
                        # mapping) — it is parked by the resolver itself
                        del entry.calls[target]
                        stats["targets_dropped"] += 1
                        continue
                    if resolved != target:
                        if resolved not in entry.calls:
                            entry.calls[resolved] = entry.calls.pop(target)
                        else:
                            del entry.calls[target]
                        stats["targets_fixed"] += 1
                    if "," not in resolved and resolved not in self._state.characters:
                        self._state.characters[resolved] = CharacterEntry()
                        stats["targets_fixed"] += 1

            # 5) narration keys follow the same rules
            for ctx in list(self._state.narration_defaults):
                resolved = self._resolve_locked(ctx, park=False)
                if resolved is None:
                    del self._state.narration_defaults[ctx]
                    stats["narration_fixed"] += 1
                elif resolved != ctx:
                    self._state.narration_defaults[resolved] = (
                        self._state.narration_defaults.pop(ctx)
                    )
                    stats["narration_fixed"] += 1

        return stats

    # Lock held.
    def _merge_entry_locked(self, source: str, target: str) -> None:
        """Fold ``source``'s entry (and every reference to it) into ``target``."""
        if source == target:
            return
        src = self._state.characters.pop(source, None)
        dst = self._state.characters.setdefault(target, CharacterEntry())
        if src is not None:
            for alias in src.names:
                if alias != target and alias not in dst.names:
                    dst.names.append(alias)
            for other, form in src.calls.items():
                dst.calls.setdefault(other, form)
            for context, pronoun in src.pronouns.items():
                dst.pronouns.setdefault(context, pronoun)
            if src.notes and not dst.notes:
                dst.notes = src.notes
            if src.last_seen > dst.last_seen:
                dst.last_seen = src.last_seen
        for entry in self._state.characters.values():
            if source in entry.calls:
                form = entry.calls.pop(source)
                entry.calls.setdefault(target, form)
        if source in self._state.narration_defaults:
            self._state.narration_defaults.setdefault(
                target, self._state.narration_defaults.pop(source)
            )

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
                        name = split_name_pair(name)[1] or name
                        # a Chinese name can never be a dictionary key, so
                        # scoping on it would only add dead weight
                        if name and not has_cjk(name):
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
                    # "白清夏=Bạch Thanh Hạ" scopes on the Vietnamese side —
                    # the Chinese side is never a dictionary key.
                    name = split_name_pair(name)[1] or name
                    if name and not has_cjk(name):
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
        """Extract '1) Characters: 白清夏=Bạch Thanh Hạ, 陆远秋' section.

        For a ``source=Vietnamese`` pair the Vietnamese side becomes the key
        (that is the canonical spelling); the source form seeds the glossary
        separately via :meth:`_glossary_pairs`.  Plain lists behave exactly
        as before."""
        result = {}
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith("1)") or stripped.lower().startswith("characters"):
                # everything after the colon
                part = stripped.split(":", 1)[-1] if ":" in stripped else stripped
                # remove numbering prefix if any
                part = re.sub(r"^[\d\)]+\s*", "", part).strip()
                if part:
                    items = [n.strip() for n in re.split(r"[,;]", part) if n.strip()]
                    for item in items:
                        # one entry per character: "白清夏=Bạch Thanh Hạ" is a
                        # person of its own, not an alias of its neighbour
                        vietnamese = split_name_pair(item)[1]
                        if vietnamese:
                            result[vietnamese] = [vietnamese]
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
