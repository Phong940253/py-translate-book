from openai import OpenAI
from .base import TranslationEngine
from ..logging_utils import log_text

DEFAULT_TEMPERATURE = 0.2


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
            {"role": "system", "content": self.system_prompt()},
            {"role": "user", "content": text},
        ]

        response = self.client.chat.completions.create(
            model="gpt-5-mini-2025-08-07",  # hoặc gpt-4o / gpt-5-mini khi bạn có quyền
            messages=messages,
        )
        
        output = response.choices[0].message.content
        log_text("OPENAI_OUTPUT", output)

        return output