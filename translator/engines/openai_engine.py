from openai import OpenAI
from .base import TranslationEngine
from ..logging_utils import log_text

DEFAULT_MODEL = "gpt-5-mini-2025-08-07"


class OpenAIEngine(TranslationEngine):
    def __init__(self, api_key: str, **kwargs):
        super().__init__(**kwargs)

        # ✅ DIRECT OPENAI
        self.client = OpenAI(api_key=api_key)

    def translate(self, text: str) -> str:
        system_prompt = self.system_prompt()
        log_text("OPENAI_SYSTEM_PROMPT", system_prompt)
        log_text("OPENAI_USER_INPUT", text)
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ]

        response = self.client.chat.completions.create(
            model=DEFAULT_MODEL,
            messages=messages,
        )
        
        output = (response.choices[0].message.content or "").strip()
        if not output:
            raise RuntimeError("Empty OpenAI response")

        log_text("OPENAI_OUTPUT", output)

        return output