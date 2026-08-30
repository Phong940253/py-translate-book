"""Generic OpenAI-compatible provider engine (Groq, DeepSeek, OpenRouter,
Ollama, ...).

Any provider that speaks the OpenAI ``POST /v1/chat/completions`` protocol
works here — it reuses the proven HTTP client from WebAIEngine with a fixed
``chat_mode=False`` and the standard chat-completions endpoint.
"""

from .webai_engine import WebAIEngine

DEFAULT_ENDPOINT = "/v1/chat/completions"


class OpenAICompatibleEngine(WebAIEngine):
    def __init__(
        self,
        base_url: str,
        model: str | None = None,
        api_key: str | None = None,
        timeout_seconds: int = 180,
        **kwargs,
    ):
        super().__init__(
            base_url=base_url,
            endpoint=DEFAULT_ENDPOINT,
            model=model,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            chat_mode=False,
            **kwargs,
        )

        # Do NOT fall back to WebAIEngine's "gemini-flash" placeholder on a
        # generic OpenAI-compatible backend: send an empty model so the remote
        # API answers with a clear "model is required" error instead of a
        # confusing 404/400 on a wrong model name.
        if not model:
            self.model = ""