import time
import logging
import re
import hashlib
import os
import json
from datetime import datetime
from difflib import SequenceMatcher
from typing import Callable
from tqdm import tqdm

from .html_utils import extract_html_content, split_html, split_html_with_metadata, assemble_html
from .logging_utils import log_text, log_consistency_event

DEFAULT_MAX_TRIES = 30
REQUEST_TIMEOUT = 2
BACKOFF_MULTIPLIER = 1.5
MAX_BACKOFF_SECONDS = 60
FALLBACK_MAX_CHUNK_SIZE = 3500
DEFAULT_HTML_STRUCTURE_MIN_SIMILARITY = 0.90
HTML_TAG_PATTERN = re.compile(r"</?[A-Za-z][^>]*>|<![^>]*>|<\?[^>]*\?>")
DEFAULT_MANUAL_REVIEW_FILE = "manual_translation_queue.jsonl"

# Tags whose boundaries are allowed to change between source and translated
# output (paragraphs and page-anchor markers). Their presence/count is not
# treated as a structural break.
_IGNORABLE_STRUCTURE_TAGS = {
    "<p>", "</p>", "<br>", "<br/>", "<br />", "<a>", "</a>",
}


class _TranslationStopped(Exception):
    """Raised inside the translation loop when a stop was requested.

    Lets callers that pass ``should_stop`` (e.g. the web UI) abort a running
    job promptly, without waiting for the current chapter/retry to finish.
    """
_TAG_NAME_RE = re.compile(r"^<(/?)([A-Za-z][A-Za-z0-9]*)")

# Decisive attributes for EPUB structure. A change in any of these means the
# translated chunk is NOT structurally equivalent (e.g. a koboSpan id keeps the
# <span> name but its id anchors the Kobo reading CSS/JS -- dropping/renaming it
# silently corrupts the kepub).
_ID_RE = re.compile(r'\bid="([^"]*)"', re.IGNORECASE)
_HREF_RE = re.compile(r'\bhref="([^"]*)"', re.IGNORECASE)
_SRC_RE = re.compile(r'\bsrc="([^"]*)"', re.IGNORECASE)
_CLASS_RE = re.compile(r'\bclass="([^"]*)"', re.IGNORECASE)


def _normalize_tag_name(tag: str) -> str:
    """Reduce a tag to its name (drop attributes) for structural comparison."""
    match = _TAG_NAME_RE.match(tag or "")
    if not match:
        return tag
    slash, name = match.group(1), match.group(2).lower()
    return f"<{slash}{name}>"


def _tag_signature(tag: str) -> str:
    """Attribute-aware structure token used by the mismatch guard.

    Decisive attributes are folded into the token so a rewrite that keeps the
    tag *name* but changes an anchor is still caught:

      ``<span class="koboSpan" id="k1">`` -> ``<span#k1.koboSpan>``
      ``<a href="u">``                    -> ``<a@u>``
      ``<img src="x.png">``               -> ``<img@x.png>``
      ``<span class="koboSpan">``         -> ``<span.koboSpan>``
      ``<div>``                           -> ``<div>``

    Only the ``koboSpan`` class is decisive (generic span classes are ignored to
    avoid false positives); ``id`` is decisive for every tag, ``href``/``src``
    only for their respective elements.
    """
    m = _TAG_NAME_RE.match(tag or "")
    if not m:
        return tag
    slash, name = m.group(1), m.group(2).lower()
    if slash:  # closing tags carry no attributes
        return f"</{name}>"
    cls = _CLASS_RE.search(tag)
    kobo = bool(cls and "koboSpan" in cls.group(1))
    id_m = _ID_RE.search(tag)
    if id_m:
        # id is decisive for every tag; keep the koboSpan marker so a class
        # rewrite (koboSpan -> plain) with the id preserved is still caught.
        return f"<{name}#{id_m.group(1)}" + (".koboSpan" if kobo else "") + ">"
    if name == "a":
        href = _HREF_RE.search(tag)
        if href:
            return f"<{name}@{href.group(1)}>"
    if name == "img":
        src = _SRC_RE.search(tag)
        if src:
            return f"<{name}@{src.group(1)}>"
    if kobo:
        return f"<{name}.koboSpan>"
    return f"<{name}>"


def _structural_tags(tags) -> list:
    return [t for t in tags if t not in _IGNORABLE_STRUCTURE_TAGS]


# Wrapper tags a model may "complete" by adding extra open/close pairs at the
# boundaries of a chunk (e.g. appending </section></div> to close the chapter
# wrapper it saw opened at the top of the chunk). These are repaired, not treated
# as a structural break, because the source chunk is the authoritative balance.
_WRAPPER_REPAIR_TAGS = ("div", "section", "body")


def _count_tag_kind(text: str, tag: str):
    """Return (open_count, close_count) for a given tag name in ``text``."""
    opens = closes = 0
    for raw in HTML_TAG_PATTERN.findall(text or ""):
        norm = _normalize_tag_name(raw)
        if norm == f"<{tag}>":
            opens += 1
        elif norm == f"</{tag}>":
            closes += 1
    return opens, closes


def _repair_wrapper_balance(source: str, out: str) -> str:
    """Strip wrapper tags (div/section/body) the model added beyond the source
    chunk's own open/close balance, so chunks reassemble into valid HTML.

    Only *surplus* wrapper tags are removed. Tags the model genuinely dropped are
    intentionally left in place so ``_has_html_structure_mismatch`` still rejects
    the chunk (and the caller retries).
    """
    if not out:
        return out
    result = out
    for tag in _WRAPPER_REPAIR_TAGS:
        src_open, src_close = _count_tag_kind(source, tag)
        _, out_close = _count_tag_kind(result, tag)

        # Models often "complete" the document by appending closing wrappers
        # (e.g. </section></div>) at the end of a chunk. Strip the surplus.
        for _ in range(out_close - src_close):
            idx = result.lower().rfind(f"</{tag}")
            if idx == -1:
                break
            end = result.find(">", idx)
            if end == -1:
                break
            result = result[:idx] + result[end + 1:]

        # Mirror handling for surplus opening wrappers added at the start.
        out_open, _ = _count_tag_kind(result, tag)
        for _ in range(out_open - src_open):
            idx = result.lower().find(f"<{tag}")
            if idx == -1:
                break
            end = result.find(">", idx)
            if end == -1:
                break
            result = result[:idx] + result[end + 1:]
    return result


def _similarity_tags(tags) -> list:
    # Keep everything except paragraph AND line-break boundaries for sequence
    # similarity (models legitimately merge/split paragraphs and move <br>).
    return [t for t in tags if t not in ("<p>", "</p>", "<br>")]


class Translator:
    def __init__(
        self,
        engine,
        split_tag="<br>",
        illustration_manager=None,
        consistency_config: dict | None = None,
        fallback_max_chunk_size: int = FALLBACK_MAX_CHUNK_SIZE,
        max_tries: int | None = DEFAULT_MAX_TRIES,
        html_structure_min_similarity: float = DEFAULT_HTML_STRUCTURE_MIN_SIMILARITY,
    ):
        self.engine = engine
        self.split_tag = split_tag
        self.illustration_manager = illustration_manager
        self.fallback_max_chunk_size = max(500, int(fallback_max_chunk_size))
        if max_tries is None:
            self.max_tries = DEFAULT_MAX_TRIES
        else:
            parsed_max_tries = int(max_tries)
            self.max_tries = parsed_max_tries if parsed_max_tries > 0 else DEFAULT_MAX_TRIES
        self.html_structure_min_similarity = min(
            1.0,
            max(0.0, float(html_structure_min_similarity)),
        )
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
        self.previous_context_max_chars = max(
            0, int(consistency_config.get("previous_context_max_chars", 2400))
        )
        self.next_source_max_chars = max(
            0, int(consistency_config.get("next_source_max_chars", 2400))
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
            "manual_review_skips": 0,
            "source_chars": 0,
            "translated_chars": 0,
            "api_calls": 0,
            "api_time_total_ms": 0.0,
            "api_time_last_ms": 0.0,
            "current_chunk": None,
        }

    def translate_html(
        self,
        soup,
        chapter_number: int | None = None,
        chapter_file_name: str | None = None,
        chapter_title: str | None = None,
        progress_callback: Callable[[int, int, int | None], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ):
        content = extract_html_content(soup, self.split_tag)
        effective_split_tag = self.split_tag
        br_count = content.count("<br>")
        p_close_count = content.count("</p>")

        # Re-evaluate split tag per chapter. A global split tag chosen from an
        # earlier chapter can create tiny + massive chunk pairs on chapters
        # with different markup density.
        if p_close_count > 0 and br_count > 0:
            effective_split_tag = "</p>" if p_close_count >= br_count else "<br>"

        if effective_split_tag not in content:
            alternate_split_tag = "</p>" if effective_split_tag == "<br>" else "<br>"
            if alternate_split_tag in content:
                logging.info(
                    "Adjusted split_tag for chapter from %s to %s",
                    effective_split_tag,
                    alternate_split_tag,
                )
                effective_split_tag = alternate_split_tag

        chunk_records = split_html_with_metadata(content, effective_split_tag)
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
            if should_stop and should_stop():
                raise _TranslationStopped
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
                self.stats["current_chunk"] = {
                    "chapter": chapter_number,
                    "index": index + 1,
                    "total": len(chunks),
                    "source": chunk,
                    "translated": cached_result,
                    "api_ms": None,
                }
                if progress_callback is not None:
                    progress_callback(index + 1, len(chunks), chapter_number)
                continue

            translated_chunk = self._translate_chunk(
                chunk,
                chapter_rules=chapter_rules,
                previous_translated=previous_context,
                next_source=next_source_context,
                chunk_index=index + 1,
                total_chunks=len(chunks),
                chunk_path=str(index + 1),
                chapter_number=chapter_number,
                chapter_file_name=chapter_file_name,
                chapter_title=chapter_title,
                should_stop=should_stop,
                progress_callback=progress_callback,
            )
            self._translation_cache[cache_key] = translated_chunk
            translated.append(translated_chunk)
            if progress_callback is not None:
                progress_callback(index + 1, len(chunks), chapter_number)

        if self.illustration_manager and self.illustration_manager.is_enabled():
            translated = self.illustration_manager.inject_illustrations(
                translated_chunks=translated,
                source_chunks=chunks,
                split_tag=effective_split_tag,
                chapter_number=chapter_number,
                chapter_file_name=chapter_file_name,
                chapter_title=chapter_title,
            )

        return assemble_html(translated, effective_split_tag)

    def translate_book_html_batch(
        self,
        soups: list,
        chapter_numbers: list | None = None,
        chapter_file_names: list | None = None,
        chapter_titles: list | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> list[str]:
        if not self.engine.supports_batch():
            raise RuntimeError("Current engine does not support batch translation")

        n = len(soups)
        chapter_numbers = chapter_numbers or [None] * n
        chapter_file_names = chapter_file_names or [None] * n
        chapter_titles = chapter_titles or [None] * n

        # Per-chapter state: list of (translatable, chunk_text, split_tag, cache_key)
        chapter_entries: list[list[tuple[bool, str, str, str | None]]] = []
        queued_texts: list[str] = []
        queued_text_set: set[str] = set()
        # cache_key -> chunk text (needed to map batch results back to keys)
        chunk_text_for_key: dict[str, str] = {}

        for soup, chapter_number, chapter_file_name, chapter_title in zip(
            soups, chapter_numbers, chapter_file_names, chapter_titles
        ):
            if should_stop and should_stop():
                raise _TranslationStopped
            content = extract_html_content(soup, self.split_tag)

            # Per-chapter split-tag re-evaluation (same logic as translate_html).
            effective_split_tag = self.split_tag
            br_count = content.count("<br>")
            p_close_count = content.count("</p>")
            if p_close_count > 0 and br_count > 0:
                effective_split_tag = "</p>" if p_close_count >= br_count else "<br>"
            if effective_split_tag not in content:
                alternate_split_tag = "</p>" if effective_split_tag == "<br>" else "<br>"
                if alternate_split_tag in content:
                    effective_split_tag = alternate_split_tag

            chunks = split_html(content, effective_split_tag)
            chapter_entries.append([])
            self.stats["chapters_processed"] += 1
            self.stats["chunks_total"] += len(chunks)

            chapter_rules = (
                self._analyze_chapter_consistency(content)
                if self.consistency_enabled
                else ""
            )
            if chapter_rules:
                log_consistency_event("CHAPTER_RULES_READY", f"{len(chapter_rules)} chars")

            for chunk_index, chunk in enumerate(chunks):
                if not chunk.strip() or self._is_tag_only_chunk(chunk):
                    # keep original text, not translatable
                    chapter_entries[-1].append((False, chunk, effective_split_tag, None))
                    continue

                next_source = (
                    self._collect_next_source(chunks, chunk_index)
                    if self.consistency_enabled
                    else []
                )
                cache_key = self._build_cache_key(chunk, chapter_rules, [], next_source)
                if cache_key in self._translation_cache:
                    chapter_entries[-1].append(
                        (True, chunk, effective_split_tag, cache_key)
                    )
                    continue
                if cache_key not in chunk_text_for_key:
                    chunk_text_for_key[cache_key] = chunk
                    if chunk not in queued_text_set:
                        queued_text_set.add(chunk)
                        queued_texts.append(chunk)
                chapter_entries[-1].append((True, chunk, effective_split_tag, cache_key))

        if queued_texts:
            logging.info(f"Submitting {len(queued_texts)} unique chunks to batch API")
            self.stats["batch_submitted"] += len(queued_texts)
            batch_results = self.engine.translate_batch(queued_texts)

            if len(batch_results) != len(queued_texts):
                raise RuntimeError("Batch result size does not match request size")

            for source, translated in zip(queued_texts, batch_results):
                self.stats["batch_results"] += 1
                self.stats["chunks_translated"] += 1
                self.stats["source_chars"] += len(source)
                self.stats["translated_chars"] += len(translated or "")
                # Store the result under every cache key that maps to this text.
                for key, text in chunk_text_for_key.items():
                    if text == source:
                        self._translation_cache[key] = translated

        translated_chapters: list[str] = []
        for chapter_offset, entries in enumerate(chapter_entries, start=1):
            translated: list[str] = []
            split_tag = self.split_tag
            for translatable, chunk, effective_split_tag, cache_key in entries:
                split_tag = effective_split_tag
                if not translatable:
                    translated.append(chunk)
                    continue

                cached_result = self._translation_cache.get(cache_key)
                if cached_result is None:
                    cached_result = self._translate_chunk(
                        chunk,
                        chapter_rules="",
                        chapter_number=chapter_offset,
                        chapter_file_name=chapter_file_names[chapter_offset - 1],
                        chapter_title=chapter_titles[chapter_offset - 1],
                    )
                    self._translation_cache[cache_key] = cached_result
                else:
                    self.stats["cache_hits"] += 1

                translated.append(cached_result)

            if self.illustration_manager and self.illustration_manager.is_enabled():
                translated = self.illustration_manager.inject_illustrations(
                    translated_chunks=translated,
                    source_chunks=[c for _, c, _, _ in entries],
                    split_tag=split_tag,
                    chapter_number=chapter_offset,
                    chapter_file_name=chapter_file_names[chapter_offset - 1],
                    chapter_title=chapter_titles[chapter_offset - 1],
                )

            translated_chapters.append(assemble_html(translated, split_tag))

        return translated_chapters

    def _translate_chunk(
        self,
        text: str,
        chapter_rules: str = "",
        previous_translated: list[str] | None = None,
        next_source: list[str] | None = None,
        chunk_index: int | None = None,
        total_chunks: int | None = None,
        chunk_path: str | None = None,
        chapter_number: int | None = None,
        chapter_file_name: str | None = None,
        chapter_title: str | None = None,
        should_stop: Callable[[], bool] | None = None,
        progress_callback: Callable[[int, int, int | None], None] | None = None,
    ) -> str:
        if self._is_tag_only_chunk(text):
            logging.info("Skipping translation for tag-only chunk")
            log_text("AI_RESPONSE", text)
            self.stats["chunks_translated"] += 1
            self.stats["source_chars"] += len(text)
            self.stats["translated_chars"] += len(text)
            return text

        # Split oversized chunks before adding contextual wrappers so each request
        # stays bounded and context windows remain predictable.
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
                    if self.consistency_enabled and self.previous_translated_window > 0:
                        start = max(0, len(translated_parts) - self.previous_translated_window)
                        fallback_previous = [
                            p for p in translated_parts[start:] if p.strip()
                        ]
                    else:
                        fallback_previous = []

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
                        chunk_path=(
                            f"{chunk_path}.{part_index}"
                            if chunk_path
                            else str(part_index)
                        ),
                        chapter_number=chapter_number,
                        chapter_file_name=chapter_file_name,
                        chapter_title=chapter_title,
                        progress_callback=progress_callback,
                    )
                    translated_parts.append(translated_part)
                merged = "".join(translated_parts)
                log_text("AI_RESPONSE", merged)
                self.stats["chunks_translated"] += 1
                self.stats["source_chars"] += len(text)
                self.stats["translated_chars"] += len(merged or "")
                return merged

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

        attempt = 1
        specific_http500_abort_count = 0
        result = None
        dt_ms = None
        last_error = None
        while True:
            if should_stop and should_stop():
                raise _TranslationStopped
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
                    api_call = lambda: self.engine.translate_with_context(
                        text=text,
                        chapter_rules=chapter_rules,
                        previous_translated=previous_translated or [],
                        next_source=next_source or [],
                        chunk_index=chunk_index,
                        total_chunks=total_chunks,
                    )
                else:
                    api_call = lambda: self.engine.translate(text_to_translate)
                t0 = time.perf_counter()
                result = api_call()
                dt_ms = (time.perf_counter() - t0) * 1000.0
                self.stats["api_calls"] += 1
                self.stats["api_time_total_ms"] += dt_ms
                self.stats["api_time_last_ms"] = dt_ms
                result = self._normalize_model_output(result, text)
                result = _repair_wrapper_balance(text, result)

                if self._is_html_tag_missing(text, result):
                    raise ValueError("Model output lost HTML tags")
                if self._has_html_structure_mismatch(text, result):
                    raise ValueError("Model output changed HTML structure")

                log_text("AI_RESPONSE", result)
                self.stats["chunks_translated"] += 1
                self.stats["source_chars"] += len(text)
                self.stats["translated_chars"] += len(result or "")
                self.stats["current_chunk"] = {
                    "chapter": chapter_number,
                    "index": chunk_index,
                    "total": total_chunks,
                    "source": text,
                    "translated": result,
                    "api_ms": dt_ms,
                }
                return result

            except Exception as e:
                max_tries_label = "inf" if self.max_tries is None else str(self.max_tries)
                logging.warning(
                    f"Chunk failed (attempt {attempt}/{max_tries_label}): {e}"
                )
                last_error = str(e)
                # Surface the failed attempt on the live monitor (so a structural
                # mismatch like a dropped koboSpan id is visible before give-up).
                self.stats["current_chunk"] = {
                    "chapter": chapter_number,
                    "index": chunk_index,
                    "total": total_chunks,
                    "source": text,
                    "translated": result if result is not None else "",
                    "api_ms": dt_ms,
                    "status": "retry",
                    "attempt": attempt,
                    "error": last_error,
                }
                if progress_callback is not None:
                    progress_callback(chunk_index, total_chunks, chapter_number)

                if self._is_google_silent_abort_http500_error(e):
                    specific_http500_abort_count += 1
                    if specific_http500_abort_count >= 3:
                        self.stats["manual_review_skips"] += 1
                        self._record_manual_translation_task(
                            source_text=text,
                            prepared_prompt=prepared_input,
                            last_attempt_prompt=text_to_translate,
                            error_message=str(e),
                            attempt=attempt,
                            chapter_number=chapter_number,
                            chapter_file_name=chapter_file_name,
                            chapter_title=chapter_title,
                            chunk_index=chunk_index,
                            total_chunks=total_chunks,
                            chunk_path=chunk_path,
                        )
                        logging.error(
                            "Skipping chunk after 3 repeated WebAI HTTP 500 silent-abort errors"
                        )
                        break

                if self.max_tries is not None and attempt >= self.max_tries:
                    break

                if should_stop and should_stop():
                    raise _TranslationStopped

                sleep_seconds = min(
                    REQUEST_TIMEOUT * (BACKOFF_MULTIPLIER ** (attempt - 1)),
                    MAX_BACKOFF_SECONDS,
                )
                time.sleep(sleep_seconds)
                attempt += 1

        logging.error("Giving up on chunk, returning original")
        self.stats["failed_chunks"] += 1
        self.stats["current_chunk"] = {
            "chapter": chapter_number,
            "index": chunk_index,
            "total": total_chunks,
            "source": text,
            "translated": result if result is not None else "",
            "api_ms": None,
            "status": "failed",
            "attempt": attempt,
            "error": last_error or "exceeded max tries",
        }
        if progress_callback is not None:
            progress_callback(chunk_index, total_chunks, chapter_number)
        return text

    @staticmethod
    def _is_google_silent_abort_http500_error(error: Exception) -> bool:
        message = str(error).lower()
        if "http 500" not in message:
            return False
        return "silently aborted by google" in message

    def _record_manual_translation_task(
        self,
        source_text: str,
        prepared_prompt: str,
        last_attempt_prompt: str,
        error_message: str,
        attempt: int,
        chapter_number: int | None,
        chapter_file_name: str | None,
        chapter_title: str | None,
        chunk_index: int | None,
        total_chunks: int | None,
        chunk_path: str | None,
    ) -> None:
        payload = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "error": error_message,
            "attempt": attempt,
            "source_text": source_text,
            "prompt": prepared_prompt,
            "last_attempt_prompt": last_attempt_prompt,
            "ebook_position": {
                "chapter_number": chapter_number,
                "chapter_file_name": chapter_file_name,
                "chapter_title": chapter_title,
                "chunk_index": chunk_index,
                "total_chunks": total_chunks,
                "chunk_path": chunk_path,
            },
        }

        review_file = os.path.abspath(DEFAULT_MANUAL_REVIEW_FILE)
        with open(review_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False))
            f.write("\n")

        logging.warning("Manual review task appended to %s", review_file)

    def get_stats(self) -> dict:
        s = dict(self.stats)
        calls = s.get("api_calls", 0)
        s["api_time_avg_ms"] = (s.get("api_time_total_ms", 0.0) / calls) if calls else 0.0
        return s

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
        selected = [chunk for chunk in translated[start:index] if chunk.strip()]
        return self._limit_context_chunks(
            selected,
            max_chars=self.previous_context_max_chars,
            keep_recent_tail=True,
        )

    def _collect_next_source(self, chunks: list[str], index: int) -> list[str]:
        if not self.consistency_enabled or self.next_source_window <= 0:
            return []

        end = min(len(chunks), index + 1 + self.next_source_window)
        selected = [chunk for chunk in chunks[index + 1:end] if chunk.strip()]
        return self._limit_context_chunks(
            selected,
            max_chars=self.next_source_max_chars,
            keep_recent_tail=False,
        )

    @staticmethod
    def _limit_context_chunks(
        chunks: list[str],
        max_chars: int,
        keep_recent_tail: bool,
    ) -> list[str]:
        if max_chars <= 0 or not chunks:
            return []

        ordered = list(reversed(chunks)) if keep_recent_tail else list(chunks)
        kept: list[str] = []
        consumed = 0

        for chunk in ordered:
            text = (chunk or "").strip()
            if not text:
                continue

            remaining = max_chars - consumed
            if remaining <= 0:
                break

            if len(text) <= remaining:
                kept.append(text)
                consumed += len(text)
                continue

            # Keep a useful tail for previous context and a useful head for next
            # context so the model still sees nearby dialogue cues.
            if remaining >= 200:
                clipped = text[-remaining:] if keep_recent_tail else text[:remaining]
                kept.append(clipped)
            break

        if keep_recent_tail:
            kept.reverse()
        return kept

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
            "Do not add/remove/reorder tags. Preserve exact tag sequence, line breaks, and <br> separators. "
            "If any HTML tag would be lost, keep that fragment unchanged instead of rewriting it. "
            "Output translated content only with original HTML preserved.\n\n"
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

        # Some models rewrite attribute URLs into markdown links, e.g.
        # href="[http://a.com](http://a.com)" which must be restored.
        if "<" in source_text and "[http" in normalized:
            normalized = re.sub(
                r'([\w:-]+)="\[(https?://[^\]\s]+)\]\((https?://[^)\s]+)\)"',
                lambda m: f'{m.group(1)}="{m.group(2)}"' if m.group(2) == m.group(3) else m.group(0),
                normalized,
            )

        # If source uses entity-escaped double-angle markers (&lt;&lt;...&gt;&gt;),
        # keep that style in output to avoid malformed HTML/text parsing later.
        source_uses_entity_brackets = "&lt;&lt;" in source_text and "&gt;&gt;" in source_text
        if source_uses_entity_brackets and ("<<" in normalized or ">>" in normalized):
            normalized = normalized.replace("<<", "&lt;&lt;").replace(">>", "&gt;&gt;")

        return normalized

    @staticmethod
    def _is_html_tag_missing(source_text: str, output_text: str) -> bool:
        source_has_tag = bool(HTML_TAG_PATTERN.search(source_text or ""))
        output_has_tag = bool(HTML_TAG_PATTERN.search(output_text or ""))
        val = source_has_tag and not output_has_tag
        if val:
            logging.warning("HTML tag missing in output")
            logging.warning("Source text with tags:\n%s", source_text)
            logging.warning("Output text:\n%s", output_text)
        return val
    

    def _has_html_structure_mismatch(self, source_text: str, output_text: str) -> bool:
        if not source_text or not output_text:
            return False

        source_raw = HTML_TAG_PATTERN.findall(source_text or "")
        output_raw = HTML_TAG_PATTERN.findall(output_text or "")
        if not source_raw:
            return False
        if not output_raw:
            return True

        source_tags = [_tag_signature(t) for t in source_raw]
        output_tags = [_tag_signature(t) for t in output_raw]

        # 1) Structural-tag integrity: div/section/span/strong/em/img must be
        #    preserved. Paragraph/line-break boundaries are allowed to change
        #    (models legitimately merge or split paragraphs).
        if sorted(_structural_tags(source_tags)) != sorted(_structural_tags(output_tags)):
            logging.warning(
                "HTML structural tags changed | source=%s | output=%s",
                sorted(_structural_tags(source_tags)),
                sorted(_structural_tags(output_tags)),
            )
            logging.warning("Source text with tags:\n%s", source_text)
            logging.warning("Output text with tags:\n%s", output_text)
            return True

        # 2) Gross content loss: model returned almost nothing.
        source_text_len = len(HTML_TAG_PATTERN.sub("", source_text).strip())
        output_text_len = len(HTML_TAG_PATTERN.sub("", output_text).strip())
        if source_text_len > 0 and output_text_len < 0.3 * source_text_len:
            logging.warning(
                "HTML output lost too much text | source_chars=%s | output_chars=%s",
                source_text_len,
                output_text_len,
            )
            logging.warning("Source text with tags:\n%s", source_text)
            logging.warning("Output text with tags:\n%s", output_text)
            return True

        # 3) Tag-sequence similarity ignoring paragraph boundaries.
        similarity_src = _similarity_tags(source_tags)
        similarity_out = _similarity_tags(output_tags)
        if not similarity_src:
            return False

        similarity = SequenceMatcher(None, similarity_src, similarity_out).ratio()
        mismatch = similarity < self.html_structure_min_similarity
        if mismatch:
            logging.warning(
                "HTML tag similarity too low | similarity=%.4f | threshold=%.4f",
                similarity,
                self.html_structure_min_similarity,
            )
            logging.warning("Source text with tags:\n%s", source_text)
            logging.warning("Output text with tags:\n%s", output_text)
        else:
            logging.debug(
                "HTML tag similarity accepted | similarity=%.4f | threshold=%.4f",
                similarity,
                self.html_structure_min_similarity,
            )
        return mismatch

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
                # No semantic boundary found in the window. Cut at the nearest
                # tag boundary (just after a '>') so we never split an HTML tag
                # in half, which would corrupt the markup.
                tag_end = text.rfind(">", cursor, window_end)
                split_idx = tag_end + 1 if tag_end != -1 else window_end

            chunks.append(text[cursor:split_idx])
            cursor = split_idx

        return [c for c in chunks if c]

    @staticmethod
    def _is_tag_only_chunk(text: str) -> bool:
        stripped = (text or "").strip()
        if not stripped:
            return True

        if not HTML_TAG_PATTERN.search(stripped):
            return False

        visible_text = HTML_TAG_PATTERN.sub("", stripped)
        return not visible_text.strip()

    @staticmethod
    def _canonicalize_html_tag(tag: str) -> str:
        if not tag:
            return tag

        normalized = tag

        if "[http" in normalized:
            normalized = re.sub(
                r'([\w:-]+)="\[(https?://[^\]\s]+)\]\((https?://[^)\s]+)\)"',
                lambda m: f'{m.group(1)}="{m.group(2)}"' if m.group(2) == m.group(3) else m.group(0),
                normalized,
            )

        return normalized
