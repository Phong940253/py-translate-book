"""CLI: crawl raw Chinese book -> raw txt per chapter + merged EPUB.

Sources (--source):
  ixdzs   ixdzs mirror, plain requests           default bid 546610
  shu69   69shuba behind Cloudflare, driven by   default bid 84642
          an undetected Chrome window

Examples:
  # test 2 chapters first
  python -m crawler.crawl --bid 546610 --limit 2
  # full book (~455 chapters)
  python -m crawler.crawl --bid 546610
  # another source
  python -m crawler.crawl --source shu69 --bid 84642 --limit 2
  # chapter range + custom output
  python -m crawler.crawl --bid 546610 --from 1 --to 50 --delay 1.0 -o out/book.epub

Resume: progress is checkpointed in <workdir>/progress.json + chapters.json,
re-running continues from the last unfinished chapter.
"""

import argparse
import importlib
import inspect
import json
import os
import re
import time

from tqdm import tqdm

from crawler.to_epub import build_epub

SOURCES = {
    "ixdzs": {"module": "crawler.ixdzs", "default_bid": "546610"},
    "shu69": {"module": "crawler.shu69", "default_bid": "84642"},
}


def _norm(s):
    return re.sub(r"\s+", "", s)


def _load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def _save_json(path, obj):
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="ixdzs", choices=sorted(SOURCES), help="where to crawl from")
    ap.add_argument("--bid", default=None, help="book id on that source (e.g. ixdzs 546610, shu69 84642)")
    ap.add_argument("--headless", action="store_true", help="shu69: run Chrome headless")
    ap.add_argument("--limit", type=int, default=None, help="crawl only first N chapters")
    ap.add_argument("--from", dest="from_ch", type=int, default=1, help="1-based start")
    ap.add_argument("--to", dest="to_ch", type=int, default=None, help="1-based end inclusive")
    ap.add_argument("--delay", type=float, default=1.2, help="seconds between requests")
    ap.add_argument("--workdir", default=None, help="raw/progress dir (default data/<bid>)")
    ap.add_argument("-o", "--output", default=None, help="output epub path")
    ap.add_argument("--no-epub", action="store_true", help="skip epub, keep raw only")
    ap.add_argument("--reset", action="store_true", help="ignore checkpoint, recrawl")
    ap.add_argument(
        "--rebuild-only",
        action="store_true",
        help="skip network; rebuild EPUB from workdir/raw/*.txt (for appended chapters)",
    )
    args = ap.parse_args()

    if args.bid is None:
        args.bid = SOURCES[args.source]["default_bid"]
    src = importlib.import_module(SOURCES[args.source]["module"])

    workdir = args.workdir or os.path.join("data", str(args.bid))
    os.makedirs(workdir, exist_ok=True)
    rawdir = os.path.join(workdir, "raw")
    os.makedirs(rawdir, exist_ok=True)

    session = None
    if not args.rebuild_only:
        kwargs = {}
        if args.headless and "headless" in inspect.signature(src.new_session).parameters:
            kwargs["headless"] = True
        session = src.new_session(**kwargs)
    try:
        if args.rebuild_only:
            meta = _load_json(os.path.join(workdir, "chapters.json"), {})
            book_title = meta.get("title", str(args.bid))
            author = meta.get("author", "")
            toc = meta.get("chapters", [])
            # pick up any raw/*.txt not listed in chapters.json (manually appended)
            known = {f"{int(c['ordernum']):04d}.txt" for c in toc}
            for fn in sorted(os.listdir(rawdir)):
                if fn.endswith(".txt") and fn not in known:
                    with open(os.path.join(rawdir, fn), encoding="utf-8") as f:
                        first = f.readline().strip()
                    toc.append(
                        {
                            "ordernum": str(int(fn.split(".")[0])),
                            "title": first,
                            "url": "",
                        }
                    )
            toc.sort(key=lambda c: int(c["ordernum"]))
        else:
            print(f"Fetching TOC ({args.source}) bid={args.bid} ...")
            book_title, author, toc = src.fetch_toc(session, args.bid)
            print(f"Book: {book_title} | Author: {author} | Chapters: {len(toc)}")
            # Preserve manually appended chapters (e.g. from another mirror)
            # that the fresh TOC does not list.
            old = _load_json(os.path.join(workdir, "chapters.json"), {}).get("chapters", [])
            known_orders = {c["ordernum"] for c in toc}
            for c in old:
                if c["ordernum"] not in known_orders:
                    toc.append(c)
            toc.sort(key=lambda c: int(c["ordernum"]))
            _save_json(
                os.path.join(workdir, "chapters.json"),
                {"title": book_title, "author": author, "chapters": toc},
            )

        if args.rebuild_only:
            selected = []
        else:
            selected = toc[args.from_ch - 1 : args.to_ch]
            if args.limit is not None:
                selected = selected[: args.limit]
        print(f"Selected {len(selected)} chapters (delay={args.delay}s)")

        progress_path = os.path.join(workdir, "progress.json")
        done = set() if args.reset else set(_load_json(progress_path, []))

        for ch in tqdm(selected, desc="Crawling"):
            key = ch["ordernum"]
            raw_path = os.path.join(rawdir, f"{int(key):04d}.txt")
            if key in done and os.path.exists(raw_path):
                continue
            for attempt in range(4):
                try:
                    page_title, paras = src.fetch_chapter(session, ch["url"])
                    title = page_title or ch["title"]
                    # safety net: drop leading dup of the final title as well
                    while paras and _norm(paras[0]) == _norm(title):
                        paras = paras[1:]
                    with open(raw_path, "w", encoding="utf-8") as f:
                        f.write(title + "\n" + "\n".join("　　" + p for p in paras) + "\n")
                    done.add(key)
                    _save_json(progress_path, sorted(done, key=int))
                    break
                except Exception as e:  # noqa: BLE001 - retry then fail loudly
                    wait = 2.0 * (attempt + 1)
                    print(f"\n[retry {attempt+1}/4] p{key} {e} (wait {wait}s)")
                    time.sleep(wait)
            else:
                raise SystemExit(f"FAILED chapter p{key} {ch['url']}")
            time.sleep(args.delay)

        print(f"Raw saved: {rawdir} ({len(done)} chapters)")
    finally:
        if session is not None:
            session.close()

    if args.no_epub:
        return

    epub_chapters = []
    for ch in toc:
        key = ch["ordernum"]
        raw_path = os.path.join(rawdir, f"{int(key):04d}.txt")
        if not os.path.exists(raw_path):
            continue  # not in selected range
        with open(raw_path, encoding="utf-8") as f:
            lines = f.read().splitlines()
        epub_chapters.append(
            {"title": lines[0] if lines else ch["title"], "paragraphs": [ln.strip() for ln in lines[1:] if ln.strip()]}
        )
    out = args.output or os.path.join(workdir, f"{book_title}.epub")
    build_epub(book_title, author, epub_chapters, out)
    print(f"EPUB: {out} ({len(epub_chapters)} chapters)")


if __name__ == "__main__":
    main()
