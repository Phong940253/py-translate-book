"""Shared translation orchestration used by both the CLI (main.py) and the
web UI (webui/). It owns the per-chapter loop, checkpoint/resume handling,
illustration injection, Discord notification and statistics collection.

Keeping this logic in one place means the CLI and the web interface behave
identically (same resume semantics, same output) and we avoid duplicating the
tricky checkpoint/restore code.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Callable, Optional

from ebooklib import epub

from translator.translator import Translator
from translator.html_utils import detect_split_tag
from translator.epub_utils import iter_chapters, load_soup, save_epub
from translator.discord_notifier import DiscordNotifier
from translator.illustration import IllustrationManager

# ---------------------------------------------------------------------------
# Config / checkpoint helpers
# ---------------------------------------------------------------------------


def read_config(path: str) -> dict:
    import yaml

    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _default_checkpoint_file(output_path: str) -> str:
    return f"{output_path}.checkpoint.json"


def _load_checkpoint(checkpoint_path: str) -> dict:
    if not os.path.exists(checkpoint_path):
        return {}

    try:
        with open(checkpoint_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001
        logging.warning("Failed to read checkpoint file %s: %s", checkpoint_path, exc)
        return {}


def _save_checkpoint(checkpoint_path: str, checkpoint_data: dict) -> None:
    temp_path = f"{checkpoint_path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(checkpoint_data, f, ensure_ascii=True, indent=2)
    os.replace(temp_path, checkpoint_path)


def _restore_existing_output_content(input_book, output_path: str) -> None:
    if not os.path.exists(output_path):
        return

    try:
        output_book = epub.read_epub(output_path)
    except Exception as exc:  # noqa: BLE001
        logging.warning(
            "Cannot read existing output EPUB for resume (%s): %s", output_path, exc
        )
        return

    input_chapters = list(iter_chapters(input_book))
    output_chapters = list(iter_chapters(output_book))

    restored = 0
    for input_item, output_item in zip(input_chapters, output_chapters):
        input_item.content = output_item.content
        restored += 1

    logging.info("Restored %s chapter documents from existing output EPUB", restored)


# ---------------------------------------------------------------------------
# Engine factory
# ---------------------------------------------------------------------------


def build_engine(
    engine_name: str,
    config: dict,
    *,
    from_lang: str = "EN",
    to_lang: str = "VI",
    description: Optional[str] = None,
    custom_prompt: Optional[str] = None,
):
    """Construct a TranslationEngine from config (mirrors the CLI factory)."""
    from translator.engines.openai_engine import OpenAIEngine
    from translator.engines.gemini_engine import GeminiEngine
    from translator.engines.webai_engine import WebAIEngine

    translation_cfg = config.get("translation", {}) if isinstance(config, dict) else {}
    common_custom_prompt = translation_cfg.get("custom_prompt")
    engine_key_cfg = config.get(engine_name, {}) if isinstance(config, dict) else {}
    engine_custom_prompt = engine_key_cfg.get("custom_prompt") if isinstance(engine_key_cfg, dict) else None
    effective_prompt = custom_prompt or engine_custom_prompt or common_custom_prompt

    if engine_name == "openai":
        return OpenAIEngine(
            api_key=engine_key_cfg["api_key"],
            from_lang=from_lang,
            to_lang=to_lang,
            description=description,
            custom_prompt=effective_prompt,
        )
    if engine_name == "gemini":
        return GeminiEngine(
            api_key=engine_key_cfg["api_key"],
            from_lang=from_lang,
            to_lang=to_lang,
            description=description,
            custom_prompt=effective_prompt,
        )
    if engine_name == "webai":
        return WebAIEngine(
            base_url=engine_key_cfg.get("base_url", "http://localhost:6969"),
            endpoint=engine_key_cfg.get("endpoint", "/v1/chat/completions"),
            model=engine_key_cfg.get("model", "gemini-2.5-flash"),
            api_key=engine_key_cfg.get("api_key"),
            timeout_seconds=engine_key_cfg.get("timeout_seconds", 120),
            chat_mode=engine_key_cfg.get("chat_mode", False),
            chat_start_endpoint=engine_key_cfg.get("chat_start_endpoint", "/gemini"),
            chat_continue_endpoint=engine_key_cfg.get("chat_continue_endpoint", "/gemini-chat"),
            chat_reset_every_chunks=engine_key_cfg.get("chat_reset_every_chunks", 30),
            from_lang=from_lang,
            to_lang=to_lang,
            description=description,
            custom_prompt=effective_prompt,
        )
    raise ValueError(f"Unsupported engine: {engine_name}")


# ---------------------------------------------------------------------------
# Progress callback contract
# ---------------------------------------------------------------------------
#
# progress_cb(event_type: str, data: dict) -> None
#   event types: "job_started", "chapter_start", "chapter_done",
#                "job_done", "error", "log"
# Default: None (no callbacks).


def _noop_cb(event_type: str, data: dict) -> None:  # pragma: no cover - placeholder
    return None


# ---------------------------------------------------------------------------
# Core run
# ---------------------------------------------------------------------------


def run_translation(
    config: dict,
    *,
    input: str,
    output: str,
    engine: str,
    engine_obj: Optional[object] = None,
    from_chapter: Optional[int] = None,
    to_chapter: Optional[int] = None,
    from_lang: str = "EN",
    to_lang: str = "VI",
    description: Optional[str] = None,
    custom_prompt: Optional[str] = None,
    openai_batch: bool = False,
    reset_checkpoint: bool = False,
    disable_resume: bool = False,
    checkpoint_file: Optional[str] = None,
    progress_cb: Optional[Callable[[str, dict], None]] = None,
    sleep_pc_after_done: bool = False,
) -> dict:
    """Run a full translation job. Returns the Translator stats dict.

    Behavior mirrors the original CLI loop (checkpoint/resume, illustration,
    Discord). ``progress_cb`` receives live events for UIs.
    """
    import platform
    import subprocess

    cb = progress_cb or _noop_cb
    started_at = datetime.now()

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
    html_structure_similarity_threshold = (
        config.get("translation", {}).get("html_structure_similarity_threshold", 0.99)
        if isinstance(config, dict)
        else 0.99
    )
    max_tries = (
        config.get("translation", {}).get("max_tries", 0)
        if isinstance(config, dict)
        else 0
    )
    illustration_config = (
        config.get("translation", {}).get("illustration", {})
        if isinstance(config, dict)
        else {}
    )
    if isinstance(config, dict) and isinstance(illustration_config, dict):
        webai_cfg = config.get("webai", {}) or {}
        if "webai_base_url" not in illustration_config:
            illustration_config["webai_base_url"] = webai_cfg.get("base_url", "")
        if "webai_api_key" not in illustration_config:
            illustration_config["webai_api_key"] = webai_cfg.get("api_key", "")
        if "webai_image_endpoint" not in illustration_config:
            illustration_config["webai_image_endpoint"] = webai_cfg.get(
                "image_endpoint", "/v1/images/generations"
            )
    discord_config = config.get("discord", {}) if isinstance(config, dict) else {}

    book = epub.read_epub(input)
    chapters = list(iter_chapters(book))
    total_chapters = len(chapters)

    start = from_chapter if from_chapter is not None else 1
    end = to_chapter if to_chapter is not None else total_chapters

    if start < 1:
        raise ValueError("--from-chapter must be >= 1")
    if end < 1:
        raise ValueError("--to-chapter must be >= 1")
    if start > end:
        raise ValueError("--from-chapter must be <= --to-chapter")
    if end > total_chapters:
        raise ValueError(f"--to-chapter exceeds total chapters ({total_chapters})")

    selected_chapters = chapters[start - 1:end]
    logging.info("Translating chapters %s-%s of %s total chapters", start, end, total_chapters)

    _checkpoint_file = checkpoint_file or _default_checkpoint_file(output)
    resume_enabled = not disable_resume

    if reset_checkpoint and os.path.exists(_checkpoint_file):
        os.remove(_checkpoint_file)
        logging.info("Deleted checkpoint file: %s", _checkpoint_file)

    checkpoint_data = _load_checkpoint(_checkpoint_file) if resume_enabled else {}
    checkpoint_signature = {
        "input": os.path.abspath(input),
        "output": os.path.abspath(output),
        "engine": engine,
        "from_lang": from_lang,
        "to_lang": to_lang,
    }

    if checkpoint_data:
        signature_mismatch = any(
            checkpoint_data.get(key) != value
            for key, value in checkpoint_signature.items()
        )
        if signature_mismatch:
            logging.warning(
                "Checkpoint signature mismatch. Ignoring previous checkpoint at %s",
                _checkpoint_file,
            )
            checkpoint_data = {}

    last_completed_chapter = int(checkpoint_data.get("last_completed_chapter", 0) or 0)

    if resume_enabled and last_completed_chapter >= start and os.path.exists(output):
        _restore_existing_output_content(book, output)

    effective_start = (
        max(start, last_completed_chapter + 1) if resume_enabled else start
    )
    selected_chapters = chapters[effective_start - 1:end]

    if resume_enabled and effective_start > start:
        logging.info(
            "Resuming from chapter %s (checkpoint file: %s)",
            effective_start,
            _checkpoint_file,
        )
        print(f"Resume enabled: skip to chapter {effective_start}/{total_chapters}")

    preview_soup = load_soup(selected_chapters[0]) if selected_chapters else None
    split_tag = detect_split_tag(preview_soup) if preview_soup is not None else "<br>"
    logging.info("Auto-detected split_tag: %s", split_tag)

    engine_obj = engine_obj or build_engine(
        engine,
        config,
        from_lang=from_lang,
        to_lang=to_lang,
        description=description,
        custom_prompt=custom_prompt,
    )

    translator = Translator(
        engine_obj,
        split_tag=split_tag,
        illustration_manager=IllustrationManager(book=book, config=illustration_config),
        consistency_config=consistency_config,
        fallback_max_chunk_size=fallback_max_chunk_size,
        max_tries=max_tries,
        html_structure_min_similarity=html_structure_similarity_threshold,
    )

    cb(
        "job_started",
        {
            "total_chapters": total_chapters,
            "start": start,
            "end": end,
            "effective_start": effective_start,
            "engine": engine,
            "split_tag": split_tag,
        },
    )

    try:
        if not selected_chapters:
            logging.info("No chapters to translate in selected range after resume filtering")
            save_epub(book, output, source_path=input)
        elif engine == "openai" and openai_batch:
            soups = [preview_soup] if preview_soup is not None else []
            soups.extend(load_soup(item) for item in selected_chapters[1:])

            batch_chapter_numbers = list(
                range(effective_start, effective_start + len(selected_chapters))
            )
            batch_file_names = [
                getattr(item, "file_name", None) for item in selected_chapters
            ]
            batch_titles = [
                (getattr(item, "title", None) or getattr(item, "id", None))
                for item in selected_chapters
            ]
            translated_chapters = translator.translate_book_html_batch(
                soups,
                chapter_numbers=batch_chapter_numbers,
                chapter_file_names=batch_file_names,
                chapter_titles=batch_titles,
            )

            for offset, (item, translated) in enumerate(
                zip(selected_chapters, translated_chapters), start=0
            ):
                chapter_number = effective_start + offset
                item.content = translated.encode("utf-8")
                save_epub(book, output, source_path=input)

                if resume_enabled:
                    _save_checkpoint(
                        _checkpoint_file,
                        {
                            **checkpoint_signature,
                            "from_chapter": start,
                            "to_chapter": end,
                            "last_completed_chapter": chapter_number,
                            "updated_at": datetime.now().isoformat(timespec="seconds"),
                        },
                    )
                cb("chapter_done", {"chapter_number": chapter_number})
        else:
            total = len(selected_chapters)
            for offset, item in enumerate(selected_chapters, start=0):
                chapter_number = effective_start + offset
                title = getattr(item, "title", None) or getattr(item, "id", None)
                file_name = getattr(item, "file_name", None)
                cb(
                    "chapter_start",
                    {
                        "chapter_number": chapter_number,
                        "title": title,
                        "file_name": file_name,
                        "index": offset,
                        "total": total,
                    },
                )
                soup = load_soup(item)
                translated = translator.translate_html(
                    soup,
                    chapter_number=chapter_number,
                    chapter_file_name=file_name,
                    chapter_title=title,
                )
                item.content = translated.encode("utf-8")
                save_epub(book, output, source_path=input)

                if resume_enabled:
                    _save_checkpoint(
                        _checkpoint_file,
                        {
                            **checkpoint_signature,
                            "from_chapter": start,
                            "to_chapter": end,
                            "last_completed_chapter": chapter_number,
                            "updated_at": datetime.now().isoformat(timespec="seconds"),
                        },
                    )
                cb("chapter_done", {"chapter_number": chapter_number})

        save_epub(book, output, source_path=input)
        logging.info("Saved translated EPUB to %s", output)

        if resume_enabled:
            _save_checkpoint(
                _checkpoint_file,
                {
                    **checkpoint_signature,
                    "from_chapter": start,
                    "to_chapter": end,
                    "last_completed_chapter": end,
                    "completed": True,
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                },
            )

        finished_at = datetime.now()
        elapsed = finished_at - started_at
        elapsed_seconds = int(elapsed.total_seconds())
        elapsed_label = (
            f"{elapsed_seconds // 3600:02d}:{(elapsed_seconds % 3600) // 60:02d}"
            f":{elapsed_seconds % 60:02d}"
        )

        translator_stats = translator.get_stats()
        input_size = os.path.getsize(input) if os.path.exists(input) else 0
        output_size = os.path.getsize(output) if os.path.exists(output) else 0

        chunk_stats = (
            f"total={translator_stats.get('chunks_total', 0)} | "
            f"translated={translator_stats.get('chunks_translated', 0)} | "
            f"cache_hits={translator_stats.get('cache_hits', 0)} | "
            f"failed={translator_stats.get('failed_chunks', 0)} | "
            f"manual_skips={translator_stats.get('manual_review_skips', 0)} | "
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
                    "summary": "Bản dịch đã xong. Có thể tiếp tục với chương tiếp theo.",
                    "input_name": os.path.basename(input),
                    "output_name": os.path.basename(output),
                    "engine": engine,
                    "chapters_label": f"{start}-{end} ({len(selected_chapters)} chapters)",
                    "elapsed_label": elapsed_label,
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "chunk_stats": chunk_stats,
                    "file_stats": file_stats,
                },
            )

        if sleep_pc_after_done:
            system = platform.system().lower()
            if system == "windows":
                subprocess.run(
                    ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"], check=False
                )
            elif system == "linux":
                subprocess.run(["systemctl", "suspend"], check=False)
            elif system == "darwin":
                subprocess.run(["pmset", "sleepnow"], check=False)
            else:
                logging.warning("Sleep is not supported on this OS: %s", system)

        cb(
            "job_done",
            {
                "stats": translator_stats,
                "elapsed_label": elapsed_label,
                "output": output,
            },
        )
        return translator_stats
    except Exception as exc:  # noqa: BLE001
        cb("error", {"message": str(exc)})
        raise
