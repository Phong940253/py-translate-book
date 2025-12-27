import time
import logging
from tqdm import tqdm

from .html_utils import extract_html_content, split_html, assemble_html
from .logging_utils import log_text

MAX_TRIES = 2
REQUEST_TIMEOUT = 4


class Translator:
    def __init__(self, engine, split_tag="<br>"):
        self.engine = engine
        self.split_tag = split_tag

    def translate_html(self, soup):
        content = extract_html_content(soup, self.split_tag)
        chunks = split_html(content, self.split_tag)

        translated: list[str] = []

        for chunk in tqdm(
            chunks,
            desc="Translating chunks",
            leave=False,
            total=len(chunks),
        ):
            translated.append(self._translate_chunk(chunk))

        return assemble_html(translated, self.split_tag)

    def _translate_chunk(self, text: str) -> str:
        log_text("INPUT_CHUNK", text)

        for attempt in range(1, MAX_TRIES + 1):
            try:
                result = self.engine.translate(text)

                log_text("AI_RESPONSE", result)
                return result

            except Exception as e:
                logging.warning(
                    f"Chunk failed (attempt {attempt}/{MAX_TRIES}): {e}"
                )
                time.sleep(REQUEST_TIMEOUT)

        logging.error("Giving up on chunk, returning original")
        return text