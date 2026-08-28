import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold
from .base import TranslationEngine
from ..logging_utils import log_text

DEFAULT_MAX_OUTPUT_TOKENS = 8000
DEFAULT_TEMPERATURE = 0.1
DEFAULT_ANALYSIS_MAX_OUTPUT_TOKENS = 1200


class GeminiEngine(TranslationEngine):
    def __init__(self, api_key: str, **kwargs):
        super().__init__(**kwargs)

        genai.configure(api_key=api_key)

        self.model = genai.GenerativeModel(
            "gemini-flash-lite",
            system_instruction=self.system_prompt(),
        )
        self.analysis_model = genai.GenerativeModel("gemini-flash-lite")

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
        log_text("GEMINI_CONSISTENCY_ANALYSIS_INPUT", analysis_prompt)

        response = self.analysis_model.generate_content(
            analysis_prompt,
            generation_config={
                "temperature": 0.2,
                "max_output_tokens": DEFAULT_ANALYSIS_MAX_OUTPUT_TOKENS,
            },
            safety_settings=self.safety,
        )

        output = (response.text or "").strip()
        if not output:
            raise RuntimeError("Empty Gemini consistency analysis response")

        log_text("GEMINI_CONSISTENCY_ANALYSIS_OUTPUT", output)
        return output
