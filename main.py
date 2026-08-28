import argparse
import logging
import os
import re
from datetime import datetime
from ebooklib import epub

from translator.epub_utils import iter_chapters, load_soup
from translator.discord_notifier import DiscordNotifier
from translator.job import read_config, run_translation

LOG_FILE = "translation.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    filename=LOG_FILE,
    filemode="a",
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", choices=["openai", "gemini", "webai"])
    parser.add_argument("--openai-batch", action="store_true")
    parser.add_argument("--input", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--config", default=None)
    parser.add_argument("--from-lang", default="EN")
    parser.add_argument("--to-lang", default="VI")
    parser.add_argument("--from-chapter", type=int, default=None)
    parser.add_argument("--to-chapter", type=int, default=None)
    parser.add_argument("--preview-chapter", type=int, default=None)
    parser.add_argument("--preview-chars", type=int, default=4000)
    parser.add_argument("--test-discord-webhook", action="store_true")
    parser.add_argument("--discord-webhook-url", default=None)
    parser.add_argument("--discord-mention-user-id", default=None)
    parser.add_argument("--description", default=None)
    parser.add_argument("--sleep-pc-after-done", action="store_true")
    parser.add_argument("--checkpoint-file", default=None)
    parser.add_argument("--disable-resume", action="store_true")
    parser.add_argument("--reset-checkpoint", action="store_true")

    args = parser.parse_args()
    started_at = datetime.now()

    if args.test_discord_webhook:
        if not args.config and not args.discord_webhook_url:
            raise ValueError(
                "--test-discord-webhook requires --config or --discord-webhook-url"
            )

        config = read_config(args.config) if args.config else {}
        discord_config = config.get("discord", {}) if isinstance(config, dict) else {}

        webhook_url = args.discord_webhook_url or discord_config.get("webhook_url", "")
        mention_user_id = (
            str(args.discord_mention_user_id).strip()
            if args.discord_mention_user_id is not None
            else str(discord_config.get("mention_user_id", "")).strip()
        ) or None

        success = DiscordNotifier.send_test_message(
            webhook_url=webhook_url,
            mention_user_id=mention_user_id,
            note="CLI webhook connectivity test",
        )

        if success:
            print("Discord webhook test: SUCCESS")
            return

        print("Discord webhook test: FAILED (see translation.log for details)")
        raise SystemExit(2)

    if not args.input:
        raise ValueError("--input is required unless --test-discord-webhook is used")

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

    run_translation(
        config,
        input=args.input,
        output=args.output,
        engine=args.engine,
        from_chapter=args.from_chapter,
        to_chapter=args.to_chapter,
        from_lang=args.from_lang,
        to_lang=args.to_lang,
        description=args.description,
        openai_batch=args.openai_batch,
        reset_checkpoint=args.reset_checkpoint,
        disable_resume=args.disable_resume,
        checkpoint_file=args.checkpoint_file,
        sleep_pc_after_done=args.sleep_pc_after_done,
    )

    elapsed = datetime.now() - started_at
    elapsed_seconds = int(elapsed.total_seconds())
    logging.info(
        "Total elapsed %02d:%02d:%02d",
        elapsed_seconds // 3600,
        (elapsed_seconds % 3600) // 60,
        elapsed_seconds % 60,
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(130)
