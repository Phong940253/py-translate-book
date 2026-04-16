import time
import logging
import re
import hashlib
from tqdm import tqdm

from .html_utils import extract_html_content, split_html, split_html_with_metadata, assemble_html
from .logging_utils import log_text, log_consistency_event

MAX_TRIES = 3
REQUEST_TIMEOUT = 2
BACKOFF_MULTIPLIER = 1.5
FALLBACK_MAX_CHUNK_SIZE = 3500
HTML_TAG_PATTERN = re.compile(r"<[^>]+>")


class Translator:
    def __init__(
        self,
        engine,
        split_tag="<br>",
        consistency_config: dict | None = None,
        fallback_max_chunk_size: int = FALLBACK_MAX_CHUNK_SIZE,
    ):
        self.engine = engine
        self.split_tag = split_tag
        self.fallback_max_chunk_size = max(500, int(fallback_max_chunk_size))
        self._translation_cache: dict[str, str] = {}
        self._chapter_rules_cache: dict[str, str] = {}

        consistency_config = consistency_config or {}
        self.consistency_enabled = bool(consistency_config.get("enabled", False))
        self.previous_translated_window = max(
            0, int(consistency_config.get("previous_translated_window", 0))
        )
        self.next_source_window = max(
            0, int(consistency_config.get("next_source_window", 0))
        )
        self.analysis_max_chars = max(
            1000, int(consistency_config.get("analysis_max_chars", 18000))
        )
        self.rules_max_chars = max(
            200, int(consistency_config.get("rules_max_chars", 1200))
        )
        self.cache_chapter_rules = bool(consistency_config.get("cache_chapter_rules", True))
        self.stats = {
            "chapters_processed": 0,
            "chunks_total": 0,
            "chunks_translated": 0,
            "cache_hits": 0,
            "fallback_split_events": 0,
            "fallback_generated_parts": 0,
            "batch_submitted": 0,
            "batch_results": 0,
            "failed_chunks": 0,
            "source_chars": 0,
            "translated_chars": 0,
        }

    def translate_html(self, soup):
        content = extract_html_content(soup, self.split_tag)
        chunk_records = split_html_with_metadata(content, self.split_tag)
        chunks = [record["text"] for record in chunk_records]
        logging.info(f"Chapter split into {len(chunks)} chunks (before fallback split)")
        self.stats["chapters_processed"] += 1
        self.stats["chunks_total"] += len(chunks)
        chapter_rules = self._analyze_chapter_consistency(content) if self.consistency_enabled else ""
        if chapter_rules:
            log_consistency_event("CHAPTER_RULES_READY", f"{len(chapter_rules)} chars")

        translated: list[str] = []

        for index, chunk in tqdm(
            enumerate(chunks),
            desc="Translating chunks",
            leave=False,
            total=len(chunks),
        ):
            if not chunk.strip():
                translated.append(chunk)
                continue

            previous_context = self._collect_previous_translated(translated, index)
            next_source_context = self._collect_next_source(chunks, index)
            cache_key = self._build_cache_key(
                chunk=chunk,
                chapter_rules=chapter_rules,
                previous_translated=previous_context,
                next_source=next_source_context,
            )

            cached_result = self._translation_cache.get(cache_key)
            if cached_result is not None:
                self.stats["cache_hits"] += 1
                translated.append(cached_result)
                continue

            translated_chunk = self._translate_chunk(
                chunk,
                chapter_rules=chapter_rules,
                previous_translated=previous_context,
                next_source=next_source_context,
                chunk_index=index + 1,
                total_chunks=len(chunks),
            )
            self._translation_cache[cache_key] = translated_chunk
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
            self.stats["chapters_processed"] += 1
            self.stats["chunks_total"] += len(chunks)

            for chunk in chunks:
                if not chunk.strip():
                    continue
                if chunk in self._translation_cache or chunk in queued_set:
                    continue

                queued_set.add(chunk)
                queued_chunks.append(chunk)

        if queued_chunks:
            logging.info(f"Submitting {len(queued_chunks)} unique chunks to batch API")
            self.stats["batch_submitted"] += len(queued_chunks)
            batch_results = self.engine.translate_batch(queued_chunks)

            if len(batch_results) != len(queued_chunks):
                raise RuntimeError("Batch result size does not match request size")

            for source, translated in zip(queued_chunks, batch_results):
                self._translation_cache[source] = translated
                self.stats["batch_results"] += 1
                self.stats["chunks_translated"] += 1
                self.stats["source_chars"] += len(source)
                self.stats["translated_chars"] += len(translated or "")

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
                else:
                    self.stats["cache_hits"] += 1

                translated.append(cached_result)

            translated_chapters.append(assemble_html(translated, self.split_tag))

        return translated_chapters

    def _translate_chunk(
        self,
        text: str,
        chapter_rules: str = "",
        previous_translated: list[str] | None = None,
        next_source: list[str] | None = None,
        chunk_index: int | None = None,
        total_chunks: int | None = None,
    ) -> str:
        prepared_input = self.engine.build_contextual_input(
            current_chunk=text,
            chapter_rules=chapter_rules,
            previous_translated=previous_translated,
            next_source=next_source,
            chunk_index=chunk_index,
            total_chunks=total_chunks,
        )

        log_text("INPUT_CHUNK", text)
        if prepared_input != text:
            log_text("INPUT_CHUNK_WITH_CONTEXT", prepared_input)

        # Some chapters (e.g., TOC/nav blocks) may not contain split tags and can become
        # very large single chunks that frequently time out on upstream APIs.
        if len(text) > self.fallback_max_chunk_size:
            fallback_parts = self._split_oversized_chunk(
                text,
                max_size=self.fallback_max_chunk_size,
            )
            if len(fallback_parts) > 1:
                self.stats["fallback_split_events"] += 1
                self.stats["fallback_generated_parts"] += len(fallback_parts)
                logging.info(
                    f"Oversized chunk ({len(text)} chars) split into {len(fallback_parts)} fallback parts"
                )
                translated_parts = []
                for part_index, part in enumerate(fallback_parts, start=1):
                    # Collect previously translated fallback parts for consistency
                    fallback_previous = [p for p in translated_parts if p.strip()]
                    # Collect next source fallback parts
                    fallback_next = [
                        fb for fb in fallback_parts[part_index:part_index + self.next_source_window]
                        if fb.strip()
                    ]
                    translated_part = self._translate_chunk(
                        part,
                        chapter_rules=chapter_rules,
                        previous_translated=fallback_previous,
                        next_source=fallback_next,
                        chunk_index=part_index,
                        total_chunks=len(fallback_parts),
                    )
                    translated_parts.append(translated_part)
                merged = "".join(translated_parts)
                log_text("AI_RESPONSE", merged)
                self.stats["chunks_translated"] += 1
                self.stats["source_chars"] += len(text)
                self.stats["translated_chars"] += len(merged or "")
                return merged

        for attempt in range(1, MAX_TRIES + 1):
            try:
                text_to_translate = (
                    prepared_input
                    if attempt == 1
                    else self._build_retry_input(prepared_input)
                )
                if (
                    attempt == 1
                    and self.consistency_enabled
                    and hasattr(self.engine, "translate_with_context")
                ):
                    result = self.engine.translate_with_context(
                        text=text,
                        chapter_rules=chapter_rules,
                        previous_translated=previous_translated or [],
                        next_source=next_source or [],
                        chunk_index=chunk_index,
                        total_chunks=total_chunks,
                    )
                else:
                    result = self.engine.translate(text_to_translate)
                result = self._normalize_model_output(result, text)

                if self._is_html_tag_missing(text, result):
                    raise ValueError("Model output lost HTML tags")
                if self._has_html_structure_mismatch(text, result):
                    raise ValueError("Model output changed HTML structure")

                log_text("AI_RESPONSE", result)
                self.stats["chunks_translated"] += 1
                self.stats["source_chars"] += len(text)
                self.stats["translated_chars"] += len(result or "")
                return result

            except Exception as e:
                logging.warning(
                    f"Chunk failed (attempt {attempt}/{MAX_TRIES}): {e}"
                )
                sleep_seconds = REQUEST_TIMEOUT * (BACKOFF_MULTIPLIER ** (attempt - 1))
                time.sleep(sleep_seconds)

        logging.error("Giving up on chunk, returning original")
        self.stats["failed_chunks"] += 1
        return text

    def get_stats(self) -> dict:
        return dict(self.stats)

    def _analyze_chapter_consistency(self, content: str) -> str:
        if not self.consistency_enabled:
            return ""

        if not hasattr(self.engine, "analyze_chapter_consistency"):
            log_consistency_event("SKIP_ANALYSIS", "Engine does not support chapter analysis")
            return ""

        chapter_hash = hashlib.sha1(content.encode("utf-8", errors="ignore")).hexdigest()
        if self.cache_chapter_rules and chapter_hash in self._chapter_rules_cache:
            log_consistency_event("RULES_CACHE_HIT", chapter_hash[:12])
            return self._chapter_rules_cache[chapter_hash]

        excerpt = content[: self.analysis_max_chars]
        try:
            rules = self.engine.analyze_chapter_consistency(excerpt) or ""
            rules = rules.strip()
            if len(rules) > self.rules_max_chars:
                rules = rules[: self.rules_max_chars].rstrip()
            if rules:
                log_text("CHAPTER_RULES", rules)
                if self.cache_chapter_rules:
                    self._chapter_rules_cache[chapter_hash] = rules
            return rules
        except Exception as error:
            logging.warning(f"Chapter analysis failed, fallback to normal translation: {error}")
            log_consistency_event("ANALYSIS_FAILED", str(error))
            return ""

    def _collect_previous_translated(self, translated: list[str], index: int) -> list[str]:
        if not self.consistency_enabled or self.previous_translated_window <= 0:
            return []

        start = max(0, index - self.previous_translated_window)
        return [chunk for chunk in translated[start:index] if chunk.strip()]

    def _collect_next_source(self, chunks: list[str], index: int) -> list[str]:
        if not self.consistency_enabled or self.next_source_window <= 0:
            return []

        end = min(len(chunks), index + 1 + self.next_source_window)
        return [chunk for chunk in chunks[index + 1:end] if chunk.strip()]

    def _build_cache_key(
        self,
        chunk: str,
        chapter_rules: str,
        previous_translated: list[str],
        next_source: list[str],
    ) -> str:
        if not self.consistency_enabled:
            return chunk

        payload = "\n\n".join(
            [
                chunk,
                chapter_rules,
                "\n".join(previous_translated),
                "\n".join(next_source),
            ]
        )
        digest = hashlib.sha1(payload.encode("utf-8", errors="ignore")).hexdigest()
        return f"ctx::{digest}"

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
    def _is_html_tag_missing(source_text: str, output_text: str) -> bool:
        source_has_tag = bool(HTML_TAG_PATTERN.search(source_text or ""))
        output_has_tag = bool(HTML_TAG_PATTERN.search(output_text or ""))
        return source_has_tag and not output_has_tag

    @staticmethod
    def _has_html_structure_mismatch(source_text: str, output_text: str) -> bool:
        source_tags = HTML_TAG_PATTERN.findall(source_text or "")
        if not source_tags:
            return False

        output_tags = HTML_TAG_PATTERN.findall(output_text or "")
        if not output_tags:
            return True

        # Translation must preserve exact tag order and attributes.
        return source_tags != output_tags

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
