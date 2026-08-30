import json
import logging
import os
import tempfile
import time

from openai import OpenAI
from .base import TranslationEngine
from ..logging_utils import log_text

DEFAULT_MODEL = "gpt-5-mini-2025-08-07"
DEFAULT_BATCH_COMPLETION_WINDOW = "24h"
DEFAULT_BATCH_POLL_INTERVAL_SECONDS = 10
DEFAULT_MAX_RETRIES = 1
BATCH_ENDPOINT = "/v1/chat/completions"


class OpenAIEngine(TranslationEngine):
    def __init__(self, api_key: str, model: str | None = None, **kwargs):
        super().__init__(**kwargs)
        self.model = model or DEFAULT_MODEL

        # ✅ DIRECT OPENAI
        self.client = OpenAI(
            api_key=api_key,
            max_retries=DEFAULT_MAX_RETRIES,
        )

    def translate(self, text: str) -> str:
        system_prompt = self.system_prompt()
        log_text("OPENAI_SYSTEM_PROMPT", system_prompt)
        log_text("OPENAI_USER_INPUT", text)

        messages = self._build_messages(system_prompt, text)

        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
        )

        output = (response.choices[0].message.content or "").strip()
        if not output:
            raise RuntimeError("Empty OpenAI response")

        log_text("OPENAI_OUTPUT", output)

        return output

    def supports_batch(self) -> bool:
        return True

    def translate_batch(
        self,
        texts: list[str],
        completion_window: str = DEFAULT_BATCH_COMPLETION_WINDOW,
        poll_interval_seconds: int = DEFAULT_BATCH_POLL_INTERVAL_SECONDS,
    ) -> list[str]:
        if not texts:
            return []

        system_prompt = self.system_prompt()
        requests = [
            {
                "custom_id": f"chunk-{index}",
                "method": "POST",
                "url": BATCH_ENDPOINT,
                "body": {
                    "model": self.model,
                    "messages": self._build_messages(system_prompt, text),
                },
            }
            for index, text in enumerate(texts)
        ]

        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".jsonl",
            delete=False,
        ) as temp_file:
            temp_path = temp_file.name
            for request in requests:
                temp_file.write(json.dumps(request, ensure_ascii=False) + "\n")

        try:
            with open(temp_path, "rb") as file_handle:
                input_file = self.client.files.create(file=file_handle, purpose="batch")

            batch = self.client.batches.create(
                input_file_id=input_file.id,
                endpoint=BATCH_ENDPOINT,
                completion_window=completion_window,
            )
            logging.info(f"OpenAI batch created: {batch.id}")

            try:
                completed_batch = self._wait_for_batch(batch.id, poll_interval_seconds)
            except KeyboardInterrupt:
                logging.warning(f"KeyboardInterrupt received. Attempting to cancel batch {batch.id}...")
                try:
                    self.client.batches.cancel(batch.id)
                    logging.warning(f"Batch {batch.id} cancel request sent")
                except Exception as cancel_error:
                    logging.warning(f"Failed to cancel batch {batch.id}: {cancel_error}")
                raise

            if completed_batch.status != "completed":
                raise RuntimeError(f"OpenAI batch did not complete successfully: {completed_batch.status}")

            if not completed_batch.output_file_id:
                raise RuntimeError("OpenAI batch completed without an output file")

            output_content = self.client.files.content(completed_batch.output_file_id)
            output_text = self._file_content_to_text(output_content)

            return self._parse_batch_output(output_text, len(texts))
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    @staticmethod
    def _build_messages(system_prompt: str, text: str) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ]

    def _wait_for_batch(self, batch_id: str, poll_interval_seconds: int):
        terminal_statuses = {"completed", "failed", "expired", "cancelled"}

        while True:
            batch = self.client.batches.retrieve(batch_id)
            if batch.status in terminal_statuses:
                logging.info(f"OpenAI batch {batch_id} finished with status: {batch.status}")
                return batch

            logging.info(f"OpenAI batch {batch_id} status: {batch.status}")
            time.sleep(poll_interval_seconds)

    @staticmethod
    def _file_content_to_text(file_content) -> str:
        text_attr = getattr(file_content, "text", None)
        if isinstance(text_attr, str):
            return text_attr
        if callable(text_attr):
            text_value = text_attr()
            if isinstance(text_value, str):
                return text_value

        read_attr = getattr(file_content, "read", None)
        if callable(read_attr):
            raw = read_attr()
            if isinstance(raw, (bytes, bytearray)):
                return raw.decode("utf-8")
            if isinstance(raw, str):
                return raw

        if isinstance(file_content, (bytes, bytearray)):
            return file_content.decode("utf-8")

        return str(file_content)

    @staticmethod
    def _parse_batch_output(output_text: str, expected_count: int) -> list[str]:
        translated: list[str | None] = [None] * expected_count

        for line in output_text.splitlines():
            if not line.strip():
                continue

            payload = json.loads(line)
            custom_id = payload.get("custom_id", "")
            error = payload.get("error")

            if error is not None:
                raise RuntimeError(f"Batch request failed for {custom_id}: {error}")

            response = payload.get("response") or {}
            status_code = response.get("status_code")
            if status_code != 200:
                raise RuntimeError(f"Batch request returned status {status_code} for {custom_id}")

            body = response.get("body") or {}
            choices = body.get("choices") or []
            message = (choices[0].get("message") if choices else {}) or {}
            content = (message.get("content") or "").strip()

            if not content:
                raise RuntimeError(f"Empty batch output for {custom_id}")

            if not custom_id.startswith("chunk-"):
                raise RuntimeError(f"Unexpected custom_id format: {custom_id}")

            index = int(custom_id.split("-", maxsplit=1)[1])
            if index < 0 or index >= expected_count:
                raise RuntimeError(f"custom_id index out of range: {custom_id}")

            translated[index] = content

        missing_indexes = [str(index) for index, value in enumerate(translated) if value is None]
        if missing_indexes:
            raise RuntimeError(f"Missing batch outputs for indexes: {', '.join(missing_indexes)}")

        return [value for value in translated if value is not None]