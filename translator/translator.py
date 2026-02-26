import time
import logging
import re
from tqdm import tqdm

from .html_utils import extract_html_content, split_html, assemble_html
from .logging_utils import log_text

MAX_TRIES = 3
REQUEST_TIMEOUT = 2
BACKOFF_MULTIPLIER = 1.5
MIN_LENGTH_RATIO = 0.25


class Translator:
    def __init__(self, engine, split_tag="<br>"):
        self.engine = engine
        self.split_tag = split_tag
        self._translation_cache: dict[str, str] = {}

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
            if not chunk.strip():
                translated.append(chunk)
                continue

            cached_result = self._translation_cache.get(chunk)
            if cached_result is not None:
                translated.append(cached_result)
                continue

            translated_chunk = self._translate_chunk(chunk)
            self._translation_cache[chunk] = translated_chunk
            translated.append(translated_chunk)

        return assemble_html(translated, self.split_tag)

    def _translate_chunk(self, text: str) -> str:
        log_text("INPUT_CHUNK", text)
        expected_tags = self._extract_html_tags(text)

        for attempt in range(1, MAX_TRIES + 1):
            try:
                text_to_translate = (
                    text
                    if attempt == 1
                    else self._build_retry_input(text)
                )
                result = self.engine.translate(text_to_translate)

                if not self._is_valid_translation(text, result, expected_tags):
                    logging.warning(
                        f"Chunk validation failed (attempt {attempt}/{MAX_TRIES})"
                    )
                    raise ValueError("Invalid translation structure")

                log_text("AI_RESPONSE", result)
                return result

            except Exception as e:
                logging.warning(
                    f"Chunk failed (attempt {attempt}/{MAX_TRIES}): {e}"
                )
                sleep_seconds = REQUEST_TIMEOUT * (BACKOFF_MULTIPLIER ** (attempt - 1))
                time.sleep(sleep_seconds)

        logging.error("Giving up on chunk, returning original")
        return text

    @staticmethod
    def _extract_html_tags(text: str) -> list[str]:
        return re.findall(r"</?[^>]+?>", text)

    @staticmethod
    def _build_retry_input(text: str) -> str:
        retry_prefix = (
            "Retry translation strictly. Keep ALL HTML tags/attributes exactly unchanged, "
            "translate all visible text fully, and output only translated HTML/text.\n\n"
            "Input:\n"
        )
        return f"{retry_prefix}{text}"

    @staticmethod
    def _is_valid_translation(source: str, target: str, source_tags: list[str]) -> bool:
        if not target or not target.strip():
            return False

        target_tags = Translator._extract_html_tags(target)
        if source_tags != target_tags:
            return False

        source_plain = re.sub(r"<[^>]+>", "", source).strip()
        target_plain = re.sub(r"<[^>]+>", "", target).strip()

        if source_plain and len(target_plain) < max(10, int(len(source_plain) * MIN_LENGTH_RATIO)):
            return False

        return True