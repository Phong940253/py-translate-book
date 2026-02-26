import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold
from .base import TranslationEngine
from ..logging_utils import log_text

DEFAULT_MAX_OUTPUT_TOKENS = 8000
DEFAULT_TEMPERATURE = 0.1


class GeminiEngine(TranslationEngine):
    def __init__(self, api_key: str, **kwargs):
        super().__init__(**kwargs)

        genai.configure(api_key=api_key)

        self.model = genai.GenerativeModel(
            "gemini-2.5-flash-lite",
            system_instruction=self.system_prompt(),
        )

        self.safety = {
            HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
        }

    def translate(self, text: str) -> str:
        log_text("GEMINI_SYSTEM_PROMPT", self.system_prompt())
        log_text("GEMINI_INPUT", text)

        response = self.model.generate_content(
            text,
            generation_config={
                "temperature": DEFAULT_TEMPERATURE,
                "max_output_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
            },
            safety_settings=self.safety,
        )

        output = (response.text or "").strip()
        if not output:
            raise RuntimeError("Empty Gemini response")

        log_text("GEMINI_OUTPUT", output)
        return output
