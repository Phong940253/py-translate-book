import argparse
import yaml
import logging
from ebooklib import epub
from tqdm import tqdm

from translator.engines.openai_engine import OpenAIEngine
from translator.engines.gemini_engine import GeminiEngine
from translator.translator import Translator
from translator.epub_utils import iter_chapters, load_soup, save_epub

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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", required=True, choices=["openai", "gemini"])
    parser.add_argument("--openai-batch", action="store_true")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--from-lang", default="EN")
    parser.add_argument("--to-lang", default="VI")
    parser.add_argument("--from-chapter", type=int, default=None)
    parser.add_argument("--to-chapter", type=int, default=None)
    parser.add_argument("--description", default=None)

    args = parser.parse_args()
    config = read_config(args.config)
    common_custom_prompt = (
        config.get("translation", {}).get("custom_prompt")
        if isinstance(config, dict)
        else None
    )

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
    else:
        raise ValueError(f"Unsupported engine: {args.engine}")

    translator = Translator(engine, split_tag="</p>")

    book = epub.read_epub(args.input)
    chapters = list(iter_chapters(book))

    total_chapters = len(chapters)
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

    try:
        if args.engine == "openai" and args.openai_batch:
            soups = [load_soup(item) for item in selected_chapters]
            translated_chapters = translator.translate_book_html_batch(soups)

            for item, translated in zip(selected_chapters, translated_chapters):
                item.content = translated.encode("utf-8")
        else:
            for item in tqdm(selected_chapters, desc="Translating chapters"):
                soup = load_soup(item)
                translated = translator.translate_html(soup)
                item.content = translated.encode("utf-8")

        save_epub(book, args.output)
        logging.info(f"Saved translated EPUB to {args.output}")
    except KeyboardInterrupt:
        logging.warning("Interrupted by user (Ctrl+C)")
        print("Interrupted by user (Ctrl+C)")
        raise SystemExit(130)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(130)
