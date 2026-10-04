# -*- coding: utf-8 -*-
"""Name audit / maintenance for the translation rules and the output EPUB.

Usage
-----
    python names_tool.py                # report (read-only, default)
    python names_tool.py --seed         # build name_glossary from the old
                                        # migrate maps + the source-verified
                                        # canonical families (backs up rules)
    python names_tool.py --sanitize     # canonicalise rules.json in place
    python names_tool.py --remerge      # replay stored analyses (brings back
                                        # rules whose addressee was unmapped)
    python names_tool.py --rename "Trương Nhữ" "Trương Như"
                                        # rename a canonical spelling everywhere
    python names_tool.py --variant "Trương Như" "Trương Nhu"
                                        # record a reviewed spelling variant
    python names_tool.py --apply        # normalise the translated EPUB text
                                        # (backs up the EPUB first)

Options
-------
    --rules PATH   rules.json to work on (default: the *.rules.json in cwd)
    --epub PATH    translated EPUB (default: *.epub.translated.epub in cwd)
    --json         print the report as JSON instead of text

The glossary lives inside rules.json (``name_glossary``): every group is
``{"canonical": ..., "variants": [...], "cn": [...], "pinyin": [...]}`` and is
editable by hand — the job re-reads the file before it saves, so hand edits
win.
"""

import argparse
import ast
import glob
import json
import os
import re
import shutil
import sys
import time
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ebooklib import epub

from translator.epub_utils import iter_chapters, normalize_names_in_html
from translator.global_rules import (
    GlobalRulesManager,
    fold_name,
    has_cjk,
    looks_like_pinyin,
)

ROOT = os.path.dirname(os.path.abspath(__file__))

# Source-verified against the original .epub (Hán tự -> chosen spelling) and
# the variants that were live in the book when this tool was written.
CONFIRMED_GROUPS = [
    {
        "canonical": "Long Lân Đông",
        "cn": ["龙怜冬"],
        "variants": ["Long Lệ Đông", "Long Lệnh Đông"],
    },
    {
        "canonical": "Lục Đậu Tình",
        "cn": ["陆窦晴"],
        "variants": ["Lục Đấu Tình"],
    },
    {
        "canonical": "Đinh Uyển Quân",
        "cn": ["丁婉君"],
        "variants": ["Đinh Vấn Quân"],
    },
    {
        "canonical": "Tào Sảng",
        "cn": ["曹爽"],
        "variants": ["Cào Sảng"],
    },
    {
        "canonical": "Anh Thu",
        "pinyin": ["Anh Qiu"],
    },
]

# The one-off migration scripts that produced the old pinyin/Chinese ->
# Vietnamese maps.  Optional: missing file just means fewer seed entries.
LEGACY_MAPS = [
    os.path.join(
        os.environ.get("TEMP", ""),
        "opencode",
        "migrate_rules.py",
    ),
    os.path.join(ROOT, "migrate_rules.py"),
]

CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
SOURCE_TITLE_RE = re.compile(r"第\s*\d+\s*章")


def default_rules_path() -> str:
    found = sorted(glob.glob(os.path.join(ROOT, "*.rules.json")))
    found = [p for p in found if ".bak-" not in os.path.basename(p)]
    return found[0] if found else ""


def default_epub_path(rules_path: str = "") -> str:
    """The output EPUB that belongs to ``rules.json``:
    ``<book>.epub.translated.rules.json`` → ``<book>.epub.translated.epub``."""
    if rules_path:
        suffix = ".rules.json"
        if rules_path.endswith(suffix):
            candidate = rules_path[: -len(suffix)] + ".epub"
            if os.path.exists(candidate):
                return candidate
    found = sorted(glob.glob(os.path.join(ROOT, "*.epub.translated.epub")))
    return found[0] if found else ""


def backup_path(path: str, tag: str) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return f"{path}.bak-{tag}-{stamp}"


# ── seeding ────────────────────────────────────────────────────────


def _literal_dicts(path: str, wanted: set[str]) -> dict:
    """Read top-level dict literals from a Python file *without* running it."""
    try:
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
    except Exception as exc:                      # missing / unparsable
        return {}
    out = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in wanted:
                try:
                    out[target.id] = ast.literal_eval(node.value)
                except Exception:
                    continue
    return out


def _kind_of(alias: str) -> str:
    if has_cjk(alias):
        return "cn"
    if alias.isascii():
        return "pinyin"
    return "variant"


def collect_seed_groups() -> tuple[list[dict], list[str]]:
    """Groups to merge into ``name_glossary`` + notes about skipped sources."""
    groups: list[dict] = [dict(group) for group in CONFIRMED_GROUPS]
    notes: list[str] = []

    for path in LEGACY_MAPS:
        if not path or not os.path.exists(path):
            continue
        literals = _literal_dicts(path, {"KEY_MAP", "EXTRA"})
        if not literals:
            notes.append(f"{path}: no KEY_MAP/EXTRA found")
            continue
        added = 0
        key_map = literals.get("KEY_MAP") or {}
        for alias, value in key_map.items():
            if not isinstance(alias, str) or not isinstance(value, str):
                continue
            alias, value = alias.strip(), value.strip()
            if not alias or not value or has_cjk(value):
                continue
            groups.append({"canonical": value, _kind_of(alias): [alias]})
            added += 1
        extra = literals.get("EXTRA") or {}
        for alias, value in extra.items():
            if not isinstance(alias, str):
                continue
            alias = alias.strip()
            if not alias:
                continue
            if isinstance(value, dict):
                names = value.get("names") or []
                value = names[0] if names else ""
            if not isinstance(value, str):
                continue
            value = value.strip()
            if not value or has_cjk(value):
                continue
            groups.append({"canonical": value, _kind_of(alias): [alias]})
            added += 1
        notes.append(f"{path}: {added} legacy pairs")
    return groups, notes


def merge_group(glossary: list[dict], group: dict) -> str:
    canonical = (group.get("canonical") or "").strip()
    if not canonical or has_cjk(canonical):
        return "skip"
    folded = fold_name(canonical)
    entry = next(
        (e for e in glossary if fold_name(e["canonical"]) == folded), None
    )
    action = "add"
    if entry is None:
        entry = {"canonical": canonical, "variants": [], "cn": [], "pinyin": []}
        glossary.append(entry)
    else:
        action = "update"
        if entry["canonical"] != canonical:
            action = "merge"

    def _add(value, key):
        item = (value or "").strip()
        if item and item != entry["canonical"] and item not in entry[key]:
            entry[key].append(item)

    for key in ("variants", "cn", "pinyin"):
        for value in group.get(key) or []:
            _add(value, key)
    if action == "merge" and canonical != entry["canonical"]:
        _add(canonical, "variants")
    return action


def cmd_seed(rules_path: str) -> int:
    if not rules_path:
        print("no rules.json found (--rules PATH)")
        return 1
    with open(rules_path, encoding="utf-8") as handle:
        data = json.load(handle)
    glossary = data.get("name_glossary") or []
    if not isinstance(glossary, list):
        glossary = []

    groups, notes = collect_seed_groups()
    counts = {"add": 0, "update": 0, "merge": 0, "skip": 0}
    for group in groups:
        counts[merge_group(glossary, group)] += 1

    data["name_glossary"] = [
        entry
        for entry in glossary
        if entry.get("canonical") and "," not in entry["canonical"]
    ]
    backup = backup_path(rules_path, "seed")
    shutil.copy2(rules_path, backup)
    with open(rules_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)

    print(f"backup  -> {os.path.basename(backup)}")
    print(f"glossary: {len(glossary)} entries "
          f"(+{counts['add']} new, {counts['update']} extended, "
          f"{counts['merge']} merged, {counts['skip']} skipped)")
    for note in notes:
        print(f"  - {note}")
    return 0


# ── sanitize / apply ───────────────────────────────────────────────


def cmd_sanitize(rules_path: str) -> int:
    if not rules_path:
        print("no rules.json found (--rules PATH)")
        return 1
    backup = backup_path(rules_path, "sanitize")
    shutil.copy2(rules_path, backup)
    manager = GlobalRulesManager(rules_path)
    manager.load()
    before_chars = manager.character_count
    stats = manager.sanitize()
    manager.save()
    print(f"backup  -> {os.path.basename(backup)}")
    print(f"characters: {before_chars} -> {manager.character_count}")
    for key, value in stats.items():
        print(f"  {key}: {value}")
    pending = manager.pending
    if pending:
        print(f"pending names (still unmapped): {len(pending)}")
        for name, reason in sorted(pending.items())[:30]:
            print(f"  - {name!r} ({reason})")
    return 0


def cmd_remerge(rules_path: str) -> int:
    """Re-merge every stored raw analysis.

    Sanitising drops rules whose addressee we cannot map *yet*; once the
    glossary knows that name (seeded or repaired), replaying the analyses
    brings the rules back instead of waiting for a re-read of the book.
    Merges only ever add, so hand-written rules are never overwritten.
    """
    if not rules_path:
        print("no rules.json found (--rules PATH)")
        return 1
    manager = GlobalRulesManager(rules_path)
    manager.load()
    analyses = manager.get_all_analyses()
    before = manager.character_count
    for chapter in sorted(analyses):
        manager.merge_from_analysis(analyses[chapter], chapter_number=chapter)
    manager.save()
    print(f"re-merged {len(analyses)} stored analyses")
    print(f"characters: {before} -> {manager.character_count}")
    print(f"pending: {len(manager.pending)}")
    return 0


def _rename_key(state, old: str, new: str) -> bool:
    """Move a dictionary key onto ``new``, merging if that key exists too."""
    entry = state.characters.pop(old, None)
    if entry is None:
        return False
    existing = state.characters.get(new)
    if existing is None:
        state.characters[new] = entry
        return True
    for target, form in entry.calls.items():
        existing.calls.setdefault(target, form)
    for alias in entry.names:
        if alias not in existing.names:
            existing.names.append(alias)
    existing.pronouns.update(entry.pronouns)
    existing.last_seen = max(existing.last_seen, entry.last_seen)
    state.characters[new] = existing
    return True


def cmd_rename(rules_path: str, old: str, new: str) -> int:
    """Rename a canonical spelling everywhere it is recorded.

    Used when a spelling decision changes (``Trương Nhữ`` -> ``Trương Như``):
    the glossary keeps the old form as a variant, the dictionary key,
    narration defaults and every address rule follow it, so the EPUB pass and
    future chapters agree on one spelling."""
    if not rules_path:
        print("no rules.json found (--rules PATH)")
        return 1
    old, new = (old or "").strip(), (new or "").strip()
    if not old or not new or old == new:
        print("--rename needs two different names: --rename OLD NEW")
        return 1
    if has_cjk(new):
        print(f"refusing: {new!r} is Chinese — names must stay Vietnamese")
        return 1
    backup = backup_path(rules_path, "rename")
    shutil.copy2(rules_path, backup)
    manager = GlobalRulesManager(rules_path)
    manager.load()
    state = manager.state
    counts = {"glossary": 0, "keys": 0, "narration": 0, "targets": 0,
              "aliases": 0}

    for entry in state.name_glossary:
        if entry["canonical"] == old:
            entry["canonical"] = new
            variants = [v for v in entry["variants"] if v not in (new, old)]
            variants.append(old)
            entry["variants"] = variants
            counts["glossary"] += 1

    if _rename_key(state, old, new):
        counts["keys"] += 1

    if old in state.narration_defaults:
        value = state.narration_defaults.pop(old)
        state.narration_defaults.setdefault(new, value)
        counts["narration"] += 1

    for entry in state.characters.values():
        if old in entry.calls:
            form = entry.calls.pop(old)
            entry.calls.setdefault(new, form)
            counts["targets"] += 1
        if old in entry.names:
            entry.names[entry.names.index(old)] = new
            counts["aliases"] += 1

    manager.drop_pending(old)
    manager.save()
    print(f"backup  -> {os.path.basename(backup)}")
    print(f"renamed {old!r} -> {new!r}")
    for key, value in counts.items():
        print(f"  {key}: {value}")
    return 0


def cmd_variant(rules_path: str, canonical: str, surface: str) -> int:
    """Record ``surface`` as another spelling of ``canonical``.

    The report prints live misspellings with their counts; this is how a
    reviewed one is declared so ``--apply`` can rewrite it."""
    if not rules_path:
        print("no rules.json found (--rules PATH)")
        return 1
    canonical = (canonical or "").strip()
    surface = (surface or "").strip()
    if not canonical or not surface:
        print("--variant needs: --variant CANONICAL SURFACE")
        return 1
    if surface == canonical or has_cjk(surface):
        print(f"refusing: {surface!r} is not a usable surface form")
        return 1
    backup = backup_path(rules_path, "variant")
    shutil.copy2(rules_path, backup)
    manager = GlobalRulesManager(rules_path)
    manager.load()
    entry = next(
        (e for e in manager.state.name_glossary if e["canonical"] == canonical),
        None,
    )
    if entry is None:
        print(f"no glossary entry with canonical {canonical!r}")
        return 1
    if surface not in entry["variants"]:
        entry["variants"].append(surface)
    manager.drop_pending(surface)
    manager.save()
    print(f"backup  -> {os.path.basename(backup)}")
    print(f"{canonical!r} now also covers {surface!r}")
    return 0


APPLY_SUFFIXES = (".xhtml", ".html", ".htm", ".ncx")


def _apply_to_zip(epub_path: str, mapping: dict[str, str]) -> int:
    """Rewrite the EPUB at the archive level; returns the replacements.

    An ebooklib round-trip would rebuild every ``<head>`` from
    ``item.title`` — which the reader leaves empty — so the kept ``第N章``
    source titles and ``lang="zh"`` would be dropped (and its NCX writer
    crashes on the TOC links it re-reads with ``uid=None``).  Copying the
    entries and touching only the text of the documents preserves every
    other byte, exactly like the job's own ``save_epub`` merge path."""
    part_path = epub_path + ".part"
    total = 0
    try:
        with zipfile.ZipFile(epub_path, "r") as source_zip:
            with zipfile.ZipFile(part_path, "w") as output_zip:
                for info in source_zip.infolist():
                    data = source_zip.read(info.filename)
                    if info.filename.lower().endswith(APPLY_SUFFIXES):
                        try:
                            text = data.decode("utf-8")
                        except UnicodeDecodeError:
                            text = None      # keep the entry untouched
                        if text is not None:
                            new_text, count = normalize_names_in_html(text, mapping)
                            if count:
                                data = new_text.encode("utf-8")
                                total += count
                    output_zip.writestr(info, data)
        os.replace(part_path, epub_path)
    finally:
        if os.path.exists(part_path):
            try:
                os.remove(part_path)
            except OSError:
                pass
    return total


def cmd_apply(epub_path: str, rules_path: str) -> int:
    if not epub_path:
        print("no translated epub found (--epub PATH)")
        return 1
    if not rules_path:
        print("no rules.json found (--rules PATH)")
        return 1
    manager = GlobalRulesManager(rules_path)
    manager.load()
    mapping = manager.mapping()
    if not mapping:
        print("glossary has nothing to map yet — run --seed first")
        return 1

    backup = backup_path(epub_path, "namefix")
    shutil.copy2(epub_path, backup)
    total = _apply_to_zip(epub_path, mapping)
    print(f"backup  -> {os.path.basename(backup)}")
    print(f"mapping: {len(mapping)} surfaces; replaced {total} occurrences")
    return 0


# ── report ─────────────────────────────────────────────────────────


def _strip_tags(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def _epub_chapters(epub_path: str) -> list[str]:
    """Raw HTML per chapter, in book order."""
    if not epub_path:
        return []
    book = epub.read_epub(epub_path)
    chapters = []
    for item in iter_chapters(book):
        raw = item.content
        chapters.append(
            raw.decode("utf-8", errors="ignore")
            if isinstance(raw, (bytes, bytearray))
            else str(raw or "")
        )
    return chapters


def _epub_texts(epub_path: str) -> list[str]:
    return [_strip_tags(html) for html in _epub_chapters(epub_path)]


TITLE_LINE_RE = re.compile(r"第\s*\d+\s*章[^<]{0,40}")


def _cjk_breakdown(chapters: list[str], start: int
                   ) -> tuple[int, list[tuple[str, str, str]]]:
    """Split the Chinese of the translated chapters into the kept source
    headings (``第N章 …`` — left in both languages on purpose) and everything
    else, which is a genuine leftover the reader will trip over."""
    title_runs = 0
    elsewhere: list[tuple[str, str, str]] = []
    for offset, html in enumerate(chapters):
        chapter_no = offset + 1
        if chapter_no < start:
            continue                     # chapters before --from stay Chinese
        titles = TITLE_LINE_RE.findall(html)
        title_runs += len(CJK_RUN_RE.findall(_strip_tags(" ".join(titles))))
        number = SOURCE_TITLE_RE.search(html)
        label = number.group(0).replace(" ", "") if number else f"#{chapter_no}"
        plain = _strip_tags(TITLE_LINE_RE.sub(" ", html))
        for match in CJK_RUN_RE.finditer(plain):
            context = plain[max(0, match.start() - 50):match.end() + 50].strip()
            elsewhere.append((label, match.group(0), context))
    return title_runs, elsewhere


def build_report(rules_path: str, epub_path: str) -> dict:
    manager = GlobalRulesManager(rules_path)
    manager.load()
    state = manager.state
    texts = _epub_texts(epub_path)
    # chapters translated by this job (the ones before --from stay Chinese)
    start = 130
    translated = " ".join(texts[start - 1:]) if len(texts) >= start else " ".join(texts)

    mapping = manager.mapping()
    variant_counts = {}
    for surface, canonical in mapping.items():
        count = translated.count(surface)
        if count:
            variant_counts.setdefault(canonical, []).append((surface, count))

    title_runs, elsewhere = _cjk_breakdown(_epub_chapters(epub_path), start)

    keys = list(state.characters)
    suspicious_keys = [k for k in keys if has_cjk(k) or looks_like_pinyin(k)]
    dangling = {}
    for key, entry in state.characters.items():
        for target in entry.calls:
            for part in [p.strip() for p in target.split(",")]:
                if part and part not in state.characters:
                    dangling.setdefault(part, 0)
                    dangling[part] += 1

    pinyin_in_epub = manager.detect_pinyin_names(translated) if translated else []

    return {
        "rules": os.path.basename(rules_path) if rules_path else None,
        "epub": os.path.basename(epub_path) if epub_path else None,
        "chapters": len(texts),
        "characters": len(keys),
        "analyses": len(manager.get_all_analyses()),
        "glossary": len(state.name_glossary),
        "whitelist": len(state.name_whitelist),
        "pending": dict(sorted(manager.pending.items())),
        "suspicious_keys": sorted(suspicious_keys),
        "dangling_targets": dict(
            sorted(dangling.items(), key=lambda kv: -kv[1])
        ),
        "live_variants": {
            canonical: sorted(pairs, key=lambda kv: -kv[1])
            for canonical, pairs in sorted(variant_counts.items())
        },
        "cjk_runs": title_runs + len(elsewhere),
        "cjk_in_source_titles": title_runs,
        "cjk_elsewhere": len(elsewhere),
        "cjk_elsewhere_samples": elsewhere[:12],
        "pinyin_in_epub": pinyin_in_epub,
    }


def print_report(report: dict) -> None:
    print("=== NAMES REPORT ===")
    print(f"rules: {report['rules']}   epub: {report['epub']} "
          f"({report['chapters']} chapters)")
    print(f"characters: {report['characters']}   analyses: {report['analyses']}   "
          f"glossary: {report['glossary']}   whitelist: {report['whitelist']}   "
          f"pending: {len(report['pending'])}")

    if report["suspicious_keys"]:
        print(f"\n-- dictionary keys that are Chinese/pinyin "
              f"({len(report['suspicious_keys'])}): run --sanitize")
        for key in report["suspicious_keys"][:25]:
            print(f"   {key!r}")
    else:
        print("\n-- dictionary keys: clean")

    if report["dangling_targets"]:
        print(f"\n-- addressees with no dictionary key "
              f"({len(report['dangling_targets'])}): run --sanitize")
        for target, count in list(report["dangling_targets"].items())[:15]:
            print(f"   {count:>4}  {target!r}")

    if report["live_variants"]:
        print("\n-- variant spellings still present in the EPUB: run --apply")
        for canonical, pairs in report["live_variants"].items():
            for surface, count in pairs:
                print(f"   {count:>5}  {surface!r}  ->  {canonical!r}")
    else:
        print("\n-- EPUB variants: none found")

    print(f"\n-- Chinese in translated chapters: {report['cjk_runs']} runs "
          f"({report['cjk_in_source_titles']} inside kept 第N章 source titles, "
          f"{report['cjk_elsewhere']} elsewhere)")
    for label, run, context in report.get("cjk_elsewhere_samples", []):
        print(f"   [{label}] {run!r}: ...{context[:90]}...")

    if report["pinyin_in_epub"]:
        print(f"\n-- pinyin-looking names in the EPUB "
              f"({len(report['pinyin_in_epub'])}):")
        for name in report["pinyin_in_epub"][:25]:
            print(f"   {name!r}")

    if report["pending"]:
        print(f"\n-- pending names (analysis produced them, no mapping yet) "
              f"({len(report['pending'])}):")
        for name, reason in list(report["pending"].items())[:30]:
            print(f"   {name!r} ({reason})")
    else:
        print("\n-- pending names: none")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rules", default="", help="path to rules.json")
    parser.add_argument("--epub", default="", help="path to translated epub")
    parser.add_argument("--seed", action="store_true",
                        help="build name_glossary from the legacy maps")
    parser.add_argument("--sanitize", action="store_true",
                        help="canonicalise rules.json in place (backup)")
    parser.add_argument("--remerge", action="store_true",
                        help="replay every stored analysis through the merge")
    parser.add_argument("--rename", nargs=2, metavar=("OLD", "NEW"),
                        help="rename a canonical spelling everywhere in rules")
    parser.add_argument("--variant", nargs=2, metavar=("CANONICAL", "SURFACE"),
                        help="record a reviewed spelling as a variant")
    parser.add_argument("--apply", action="store_true",
                        help="normalise the translated epub (backup)")
    parser.add_argument("--json", action="store_true",
                        help="print the report as JSON")
    args = parser.parse_args(argv)

    rules_path = args.rules or default_rules_path()
    epub_path = args.epub or default_epub_path(rules_path)

    ran = args.seed or args.sanitize or args.remerge or args.apply \
        or bool(args.rename) or bool(args.variant)
    status = 0
    if args.seed:
        status = status or cmd_seed(rules_path)
    if args.sanitize:
        status = status or cmd_sanitize(rules_path)
    if args.remerge:
        status = status or cmd_remerge(rules_path)
    if args.rename:
        status = status or cmd_rename(rules_path, args.rename[0],
                                      args.rename[1])
    if args.variant:
        status = status or cmd_variant(rules_path, args.variant[0],
                                       args.variant[1])
    if args.apply:
        status = status or cmd_apply(epub_path, rules_path)
    if status:
        return status

    if not ran:
        report = build_report(rules_path, epub_path)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
