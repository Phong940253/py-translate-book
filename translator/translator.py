import time
import logging
import re
from tqdm import tqdm

from .html_utils import extract_html_content, split_html, assemble_html
from .logging_utils import log_text

MAX_TRIES = 3
REQUEST_TIMEOUT = 2
BACKOFF_MULTIPLIER = 1.5
FALLBACK_MAX_CHUNK_SIZE = 3500


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

    def translate_book_html_batch(self, soups: list) -> list[str]:
        if not self.engine.supports_batch():
            raise RuntimeError("Current engine does not support batch translation")

        chapter_chunks: list[list[str]] = []
        queued_chunks: list[str] = []
        queued_set: set[str] = set()

        for soup in soups:
            content = extract_html_content(soup, self.split_tag)
            chunks = split_html(content, self.split_tag)
            chapter_chunks.append(chunks)

            for chunk in chunks:
                if not chunk.strip():
                    continue
                if chunk in self._translation_cache or chunk in queued_set:
                    continue

                queued_set.add(chunk)
                queued_chunks.append(chunk)

        if queued_chunks:
            logging.info(f"Submitting {len(queued_chunks)} unique chunks to batch API")
            batch_results = self.engine.translate_batch(queued_chunks)

            if len(batch_results) != len(queued_chunks):
                raise RuntimeError("Batch result size does not match request size")

            for source, translated in zip(queued_chunks, batch_results):
                self._translation_cache[source] = translated

        translated_chapters: list[str] = []
        for chunks in chapter_chunks:
            translated: list[str] = []
            for chunk in chunks:
                if not chunk.strip():
                    translated.append(chunk)
                    continue

                cached_result = self._translation_cache.get(chunk)
                if cached_result is None:
                    cached_result = self._translate_chunk(chunk)
                    self._translation_cache[chunk] = cached_result

                translated.append(cached_result)

            translated_chapters.append(assemble_html(translated, self.split_tag))

        return translated_chapters

    def _translate_chunk(self, text: str) -> str:
        log_text("INPUT_CHUNK", text)

        # Some chapters (e.g., TOC/nav blocks) may not contain split tags and can become
        # very large single chunks that frequently time out on upstream APIs.
        if len(text) > FALLBACK_MAX_CHUNK_SIZE:
            fallback_parts = self._split_oversized_chunk(text)
            if len(fallback_parts) > 1:
                logging.info(
                    f"Oversized chunk ({len(text)} chars) split into {len(fallback_parts)} fallback parts"
                )
                translated_parts = [self._translate_chunk(part) for part in fallback_parts]
                merged = "".join(translated_parts)
                log_text("AI_RESPONSE", merged)
                return merged

        for attempt in range(1, MAX_TRIES + 1):
            try:
                text_to_translate = (
                    text
                    if attempt == 1
                    else self._build_retry_input(text)
                )
                result = self.engine.translate(text_to_translate)
                result = self._normalize_model_output(result, text)

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
    def _build_retry_input(text: str) -> str:
        retry_prefix = (
            "Retry translation strictly. Translate ONLY visible text nodes. "
            "Keep all HTML tags, attributes, attribute values, entities, URLs, and code-like tokens unchanged. "
            "Do not add/remove/reorder tags. Output only translated content.\n\n"
            "Input:\n"
        )
        return f"{retry_prefix}{text}"

    @staticmethod
    def _normalize_model_output(output: str, source_text: str) -> str:
        if not output:
            return output

        normalized = output.strip()

        # Some models wrap output in markdown fences even when instructed not to.
        if normalized.startswith("```"):
            normalized = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", normalized)
            normalized = re.sub(r"\s*```$", "", normalized)

        # WebAI responses can contain markdown-escaped HTML (e.g. \<tag\>, \_).
        # Unescape only characters that commonly corrupt HTML/XML fragments.
        if "<" in source_text and any(token in normalized for token in ("\\<", "\\>", "\\_", "\\#", "\\!")):
            normalized = re.sub(r"\\([<>_#!])", r"\1", normalized)

        return normalized

    @staticmethod
    def _split_oversized_chunk(text: str, max_size: int = FALLBACK_MAX_CHUNK_SIZE) -> list[str]:
        if len(text) <= max_size:
            return [text]

        # Prefer semantic HTML boundaries first to keep tags intact.
        boundary_tokens = [
            "</li>",
            "</p>",
            "<br>",
            "<br/>",
            "<br />",
            ">",
            " ",
        ]

        chunks: list[str] = []
        cursor = 0
        text_len = len(text)

        while cursor < text_len:
            if text_len - cursor <= max_size:
                chunks.append(text[cursor:])
                break

            window_end = cursor + max_size
            split_idx = -1

            for token in boundary_tokens:
                idx = text.rfind(token, cursor, window_end)
                if idx != -1:
                    candidate = idx + len(token)
                    if candidate > cursor:
                        split_idx = max(split_idx, candidate)

            if split_idx == -1:
                split_idx = window_end

            chunks.append(text[cursor:split_idx])
            cursor = split_idx

        return [c for c in chunks if c]
