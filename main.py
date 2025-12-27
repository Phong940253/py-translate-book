import argparse
import yaml
import logging
from ebooklib import epub
from tqdm import tqdm

from translator.engines.openai_engine import OpenAIEngine
from translator.engines.gemini_engine import GeminiEngine
from translator.translator import Translator
from translator.epub_utils import iter_chapters, load_soup, save_epub
import logging

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
    parser.add_argument("--engine", required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--from-lang", default="EN")
    parser.add_argument("--to-lang", default="VI")
    parser.add_argument("--description", default=None)

    args = parser.parse_args()
    config = read_config(args.config)

    if args.engine == "openai":
        engine = OpenAIEngine(
            api_key=config["openai"]["api_key"],
            from_lang=args.from_lang,
            to_lang=args.to_lang,
            description=args.description,
        )
    else:
        engine = GeminiEngine(
            api_key=config["gemini"]["api_key"],
            from_lang=args.from_lang,
            to_lang=args.to_lang,
            description=args.description,
        )

    translator = Translator(engine)

    book = epub.read_epub(args.input)
    chapters = list(iter_chapters(book))

    for item in tqdm(chapters, desc="Translating chapters"):
        soup = load_soup(item)
        translated = translator.translate_html(soup)
        item.content = translated.encode("utf-8")
        save_epub(book, args.output)


if __name__ == "__main__":
    main()
