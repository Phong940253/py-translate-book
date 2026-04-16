import argparse
import yaml
import logging
import platform
import subprocess
import os
import re
from datetime import datetime
from ebooklib import epub
from tqdm import tqdm

from translator.engines.openai_engine import OpenAIEngine
from translator.engines.gemini_engine import GeminiEngine
from translator.engines.webai_engine import WebAIEngine
from translator.translator import Translator
from translator.html_utils import detect_split_tag
from translator.epub_utils import iter_chapters, load_soup, save_epub
from translator.discord_notifier import DiscordNotifier

LOG_FILE = "translation.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    filename=LOG_FILE,
    filemode="a",
)



def read_config(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def sleep_pc():
    system = platform.system().lower()

    if system == "windows":
        subprocess.run(
            ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
            check=False,
        )
        return

    if system == "linux":
        subprocess.run(["systemctl", "suspend"], check=False)
        return

    if system == "darwin":
        subprocess.run(["pmset", "sleepnow"], check=False)
        return

    logging.warning(f"Sleep is not supported on this OS: {system}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", choices=["openai", "gemini", "webai"])
    parser.add_argument("--openai-batch", action="store_true")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", default=None)
    parser.add_argument("--config", default=None)
    parser.add_argument("--from-lang", default="EN")
    parser.add_argument("--to-lang", default="VI")
    parser.add_argument("--from-chapter", type=int, default=None)
    parser.add_argument("--to-chapter", type=int, default=None)
    parser.add_argument("--preview-chapter", type=int, default=None)
    parser.add_argument("--preview-chars", type=int, default=4000)
    parser.add_argument("--description", default=None)
    parser.add_argument("--sleep-pc-after-done", action="store_true")

    args = parser.parse_args()
    started_at = datetime.now()

    book = epub.read_epub(args.input)
    chapters = list(iter_chapters(book))

    total_chapters = len(chapters)

    if args.preview_chapter is not None:
        if args.from_chapter is not None or args.to_chapter is not None:
            raise ValueError("--preview-chapter cannot be used with --from-chapter/--to-chapter")
        if args.preview_chapter < 1:
            raise ValueError("--preview-chapter must be >= 1")
        if args.preview_chapter > total_chapters:
            raise ValueError(
                f"--preview-chapter exceeds total chapters ({total_chapters})"
            )

        preview_index = args.preview_chapter - 1
        preview_item = chapters[preview_index]
        preview_soup = load_soup(preview_item)

        preview_text = preview_soup.get_text("\n", strip=True)
        preview_text = re.sub(r"\n{2,}", "\n", preview_text)

        max_preview_chars = max(200, int(args.preview_chars))
        preview_title = (
            getattr(preview_item, "title", None)
            or getattr(preview_item, "id", None)
            or getattr(preview_item, "file_name", "(unknown)")
        )

        print(f"Preview chapter {args.preview_chapter}/{total_chapters}: {preview_title}")
        print("-" * 80)
        print(preview_text[:max_preview_chars])

        if len(preview_text) > max_preview_chars:
            print("\n...")
            print(
                f"(Preview truncated at {max_preview_chars} characters. "
                "Use --preview-chars to increase.)"
            )

        logging.info(
            "Previewed chapter %s/%s (%s chars shown)",
            args.preview_chapter,
            total_chapters,
            min(len(preview_text), max_preview_chars),
        )
        return

    if not args.output:
        raise ValueError("--output is required unless --preview-chapter is used")
    if not args.config:
        raise ValueError("--config is required unless --preview-chapter is used")
    if not args.engine:
        raise ValueError("--engine is required unless --preview-chapter is used")

    config = read_config(args.config)
    consistency_config = (
        config.get("translation", {}).get("consistency", {})
        if isinstance(config, dict)
        else {}
    )
    fallback_max_chunk_size = (
        config.get("translation", {}).get("fallback_max_chunk_size", 3500)
        if isinstance(config, dict)
        else 3500
    )
    common_custom_prompt = (
        config.get("translation", {}).get("custom_prompt")
        if isinstance(config, dict)
        else None
    )
    discord_config = config.get("discord", {}) if isinstance(config, dict) else {}

    if args.engine == "openai":
        engine_custom_prompt = config.get("openai", {}).get("custom_prompt")
        engine = OpenAIEngine(
            api_key=config["openai"]["api_key"],
            from_lang=args.from_lang,
            to_lang=args.to_lang,
            description=args.description,
            custom_prompt=engine_custom_prompt or common_custom_prompt,
        )
    elif args.engine == "gemini":
        engine_custom_prompt = config.get("gemini", {}).get("custom_prompt")
        engine = GeminiEngine(
            api_key=config["gemini"]["api_key"],
            from_lang=args.from_lang,
            to_lang=args.to_lang,
            description=args.description,
            custom_prompt=engine_custom_prompt or common_custom_prompt,
        )
    elif args.engine == "webai":
        webai_config = config.get("webai", {})
        engine_custom_prompt = webai_config.get("custom_prompt")
        engine = WebAIEngine(
            base_url=webai_config.get("base_url", "http://localhost:6969"),
            endpoint=webai_config.get("endpoint", "/v1/chat/completions"),
            model=webai_config.get("model", "gemini-2.5-flash"),
            api_key=webai_config.get("api_key"),
            timeout_seconds=webai_config.get("timeout_seconds", 120),
            chat_mode=webai_config.get("chat_mode", False),
            chat_start_endpoint=webai_config.get("chat_start_endpoint", "/gemini"),
            chat_continue_endpoint=webai_config.get("chat_continue_endpoint", "/gemini-chat"),
            chat_reset_every_chunks=webai_config.get("chat_reset_every_chunks", 30),
            from_lang=args.from_lang,
            to_lang=args.to_lang,
            description=args.description,
            custom_prompt=engine_custom_prompt or common_custom_prompt,
        )
    else:
        raise ValueError(f"Unsupported engine: {args.engine}")

    start = args.from_chapter if args.from_chapter is not None else 1
    end = args.to_chapter if args.to_chapter is not None else total_chapters

    if start < 1:
        raise ValueError("--from-chapter must be >= 1")
    if end < 1:
        raise ValueError("--to-chapter must be >= 1")
    if start > end:
        raise ValueError("--from-chapter must be <= --to-chapter")
    if end > total_chapters:
        raise ValueError(f"--to-chapter exceeds total chapters ({total_chapters})")

    selected_chapters = chapters[start - 1:end]
    logging.info(
        f"Translating chapters {start}-{end} of {total_chapters} total chapters"
    )

    preview_soup = load_soup(selected_chapters[0]) if selected_chapters else None
    split_tag = detect_split_tag(preview_soup) if preview_soup is not None else "<br>"
    logging.info(f"Auto-detected split_tag: {split_tag}")

    translator = Translator(
        engine,
        split_tag=split_tag,
        consistency_config=consistency_config,
        fallback_max_chunk_size=fallback_max_chunk_size,
    )

    try:
        if args.engine == "openai" and args.openai_batch:
            soups = [preview_soup] if preview_soup is not None else []
            soups.extend(load_soup(item) for item in selected_chapters[1:])
            translated_chapters = translator.translate_book_html_batch(soups)

            for item, translated in zip(selected_chapters, translated_chapters):
                item.content = translated.encode("utf-8")
        else:
            for item in tqdm(selected_chapters, desc="Translating chapters"):
                soup = load_soup(item)
                translated = translator.translate_html(soup)
                item.content = translated.encode("utf-8")
                save_epub(book, args.output, source_path=args.input)

        save_epub(book, args.output, source_path=args.input)
        logging.info(f"Saved translated EPUB to {args.output}")

        finished_at = datetime.now()
        elapsed = finished_at - started_at
        elapsed_seconds = int(elapsed.total_seconds())
        elapsed_label = (
            f"{elapsed_seconds // 3600:02d}:{(elapsed_seconds % 3600) // 60:02d}:{elapsed_seconds % 60:02d}"
        )

        translator_stats = translator.get_stats()
        input_size = os.path.getsize(args.input) if os.path.exists(args.input) else 0
        output_size = os.path.getsize(args.output) if os.path.exists(args.output) else 0

        chunk_stats = (
            f"total={translator_stats.get('chunks_total', 0)} | "
            f"translated={translator_stats.get('chunks_translated', 0)} | "
            f"cache_hits={translator_stats.get('cache_hits', 0)} | "
            f"failed={translator_stats.get('failed_chunks', 0)} | "
            f"fallback_splits={translator_stats.get('fallback_split_events', 0)}"
        )

        file_stats = (
            f"input={input_size / (1024 * 1024):.2f} MB | "
            f"output={output_size / (1024 * 1024):.2f} MB | "
            f"src_chars={translator_stats.get('source_chars', 0)} | "
            f"out_chars={translator_stats.get('translated_chars', 0)}"
        )

        if discord_config.get("enabled", True):
            DiscordNotifier.send_translation_completed(
                webhook_url=discord_config.get("webhook_url", ""),
                mention_user_id=str(discord_config.get("mention_user_id", "")).strip() or None,
                stats={
                    "summary": "Ban dich da xong. Co the tiep tuc voi chuong tiep theo.",
                    "input_name": os.path.basename(args.input),
                    "output_name": os.path.basename(args.output),
                    "engine": args.engine,
                    "chapters_label": f"{start}-{end} ({len(selected_chapters)} chapters)",
                    "elapsed_label": elapsed_label,
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "chunk_stats": chunk_stats,
                    "file_stats": file_stats,
                },
            )

        if args.sleep_pc_after_done:
            logging.info("Sleeping PC after translation as requested")
            sleep_pc()
    except KeyboardInterrupt:
        logging.warning("Interrupted by user (Ctrl+C)")
        print("Interrupted by user (Ctrl+C)")
        raise SystemExit(130)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(130)
