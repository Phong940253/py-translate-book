import json
import logging
import socket
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

from .base import TranslationEngine
from ..logging_utils import log_text

DEFAULT_ENDPOINT = "/v1/chat/completions"
DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_TIMEOUT_SECONDS = 180
DEFAULT_TEMPERATURE = 0.1
DEFAULT_CHAT_START_ENDPOINT = "/gemini"
DEFAULT_CHAT_CONTINUE_ENDPOINT = "/gemini-chat"
DEFAULT_CHAT_RESET_EVERY_CHUNKS = 30
DEFAULT_ANALYSIS_TEMPERATURE = 0.2


class WebAIEngine(TranslationEngine):
    def __init__(
        self,
        base_url: str,
        endpoint: str = DEFAULT_ENDPOINT,
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        chat_mode: bool = False,
        chat_start_endpoint: str = DEFAULT_CHAT_START_ENDPOINT,
        chat_continue_endpoint: str = DEFAULT_CHAT_CONTINUE_ENDPOINT,
        chat_reset_every_chunks: int = DEFAULT_CHAT_RESET_EVERY_CHUNKS,
        **kwargs,
    ):
        super().__init__(**kwargs)

        if not base_url:
            raise ValueError("webai.base_url is required")

        normalized_endpoint = endpoint.strip() if endpoint else DEFAULT_ENDPOINT
        if not normalized_endpoint.startswith("/"):
            normalized_endpoint = f"/{normalized_endpoint}"

        self.base_url = base_url.rstrip("/")
        self.endpoint = normalized_endpoint
        self.model = model or DEFAULT_MODEL
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.chat_mode = chat_mode

        start_endpoint = chat_start_endpoint.strip() if chat_start_endpoint else DEFAULT_CHAT_START_ENDPOINT
        continue_endpoint = (
            chat_continue_endpoint.strip()
            if chat_continue_endpoint
            else DEFAULT_CHAT_CONTINUE_ENDPOINT
        )
        if not start_endpoint.startswith("/"):
            start_endpoint = f"/{start_endpoint}"
        if not continue_endpoint.startswith("/"):
            continue_endpoint = f"/{continue_endpoint}"

        self.chat_start_endpoint = start_endpoint
        self.chat_continue_endpoint = continue_endpoint
        self.chat_reset_every_chunks = max(1, int(chat_reset_every_chunks or DEFAULT_CHAT_RESET_EVERY_CHUNKS))
        self._chunks_in_current_chat_session = 0

        self._cookie_jar = CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._cookie_jar)
        )

    def translate(self, text: str) -> str:
        system_prompt = self.system_prompt()

        log_text("WEBAI_SYSTEM_PROMPT", system_prompt)
        log_text("WEBAI_INPUT", text)

        output = self._translate_with_system_prompt(system_prompt, text)
        return output

    def translate_with_context(
        self,
        text: str,
        chapter_rules: str,
        previous_translated: list[str],
        next_source: list[str],
        chunk_index: int | None,
        total_chunks: int | None,
    ) -> str:
        contextual_input = self.build_contextual_input(
            current_chunk=text,
            chapter_rules=chapter_rules,
            previous_translated=previous_translated,
            next_source=next_source,
            chunk_index=chunk_index,
            total_chunks=total_chunks,
        )
        return self.translate(contextual_input)

    def analyze_chapter_consistency(self, chapter_excerpt: str) -> str:
        analysis_prompt = self.chapter_consistency_prompt(chapter_excerpt)
        log_text("WEBAI_CONSISTENCY_ANALYSIS_INPUT", analysis_prompt)

        # Use a concise, deterministic system prompt for rule extraction.
        analysis_system_prompt = (
            "You are a Vietnamese literary translation consistency assistant. "
            "Return concise plain-text address-form rules only."
        )
        output = self._translate_with_system_prompt(
            analysis_system_prompt,
            analysis_prompt,
            temperature=DEFAULT_ANALYSIS_TEMPERATURE,
        )
        log_text("WEBAI_CONSISTENCY_ANALYSIS_OUTPUT", output)
        return output

    def _translate_with_system_prompt(
        self,
        system_prompt: str,
        text: str,
        temperature: float = DEFAULT_TEMPERATURE,
    ) -> str:
        response_json = self._call_endpoint(system_prompt, text, temperature=temperature)
        output = self._extract_output(response_json).strip()
        if not output:
            raise RuntimeError("Empty WebAI response")
        return output

    def _call_endpoint(self, system_prompt: str, text: str, temperature: float = DEFAULT_TEMPERATURE) -> dict:
        if self.chat_mode:
            # In chat mode, keep using the OpenAI-compatible endpoint to avoid
            # switching to legacy Gemini-specific routes.
            return self._post_openai_compatible(system_prompt, text, temperature=temperature)

        endpoint = self.endpoint
        if endpoint.startswith("/v1beta/models/"):
            return self._post_google_v1beta(system_prompt, text, temperature=temperature)
        if endpoint == "/v1/chat/completions":
            return self._post_openai_compatible(system_prompt, text, temperature=temperature)
        return self._post_gemini_routes(endpoint, system_prompt, text, temperature=temperature)

    def _choose_chat_endpoint(self) -> str:
        if self._chunks_in_current_chat_session == 0:
            return self.chat_start_endpoint

        if self._chunks_in_current_chat_session >= self.chat_reset_every_chunks:
            self._chunks_in_current_chat_session = 0
            return self.chat_start_endpoint

        return self.chat_continue_endpoint

    def _post_openai_compatible(self, system_prompt: str, text: str, temperature: float = DEFAULT_TEMPERATURE) -> dict:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text},
            ],
            "stream": False,
            "temperature": temperature,
        }
        return self._post_json(self.endpoint, payload)

    def _post_google_v1beta(self, system_prompt: str, text: str, temperature: float = DEFAULT_TEMPERATURE) -> dict:
        endpoint = self.endpoint
        if ":" not in endpoint.rsplit("/", maxsplit=1)[-1]:
            endpoint = f"{endpoint}:generateContent"

        payload = {
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"parts": [{"text": text}]}],
            "generationConfig": {"temperature": temperature},
        }
        return self._post_json(endpoint, payload)

    def _post_gemini_routes(
        self,
        endpoint: str,
        system_prompt: str,
        text: str,
        temperature: float = DEFAULT_TEMPERATURE,
    ) -> dict:
        composed_message = f"{system_prompt}\n\nInput:\n{text}".strip()
        payload = {
            "model": self.model,
            "message": composed_message,
            "files": None,
            "temperature": temperature,
        }
        response = self._post_json(endpoint, payload)

        if self.chat_mode and endpoint in (self.chat_start_endpoint, self.chat_continue_endpoint):
            self._chunks_in_current_chat_session += 1

        return response

    def _post_json(self, endpoint: str, payload: dict) -> dict:
        url = urllib.parse.urljoin(f"{self.base_url}/", endpoint.lstrip("/"))
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        request = urllib.request.Request(url, data=body, headers=headers, method="POST")

        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except (TimeoutError, socket.timeout) as error:
            self._reset_chat_session_state("timeout")
            raise RuntimeError(
                f"WebAI request timed out after {self.timeout_seconds}s"
            ) from error
        except urllib.error.HTTPError as error:
            error_body = error.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"WebAI request failed: HTTP {error.code} - {error_body}") from error
        except urllib.error.URLError as error:
            if isinstance(error.reason, TimeoutError):
                self._reset_chat_session_state("timeout")
                raise RuntimeError(
                    f"WebAI request timed out after {self.timeout_seconds}s"
                ) from error
            raise RuntimeError(f"WebAI request failed: {error.reason}") from error

        try:
            return json.loads(raw)
        except json.JSONDecodeError as error:
            raise RuntimeError(f"WebAI returned non-JSON response: {raw[:500]}") from error

    def _reset_chat_session_state(self, reason: str) -> None:
        if not self.chat_mode:
            return

        self._chunks_in_current_chat_session = 0
        self._cookie_jar = CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._cookie_jar)
        )
        logging.info(f"WebAI chat session reset due to {reason}")

    @staticmethod
    def _extract_output(payload: dict) -> str:
        choices = payload.get("choices") or []
        if choices:
            message = (choices[0].get("message") or {})
            content = message.get("content")
            if isinstance(content, str):
                return content

        candidates = payload.get("candidates") or []
        if candidates:
            candidate_content = (candidates[0].get("content") or {})
            parts = candidate_content.get("parts") or []
            if parts:
                texts = [part.get("text", "") for part in parts if isinstance(part, dict)]
                return "".join(texts)

        for key in ("response", "output", "text", "content", "translated_text", "answer"):
            value = payload.get(key)
            if isinstance(value, str):
                return value

        return ""