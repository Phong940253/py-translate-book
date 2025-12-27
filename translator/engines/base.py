from abc import ABC, abstractmethod


class TranslationEngine(ABC):
    def __init__(self, from_lang: str, to_lang: str, description: str | None = None):
        self.from_lang = from_lang
        self.to_lang = to_lang
        self.description = description

    @abstractmethod
    def translate(self, text: str) -> str:
        pass

    def system_prompt(self) -> str:
        prompt = f"""
Translate the following text from {self.from_lang} to {self.to_lang}.

Rules:
- Translate ALL content fully.
- Preserve all HTML tags exactly.
- Preserve special characters.
- Do NOT add markdown or code fences.
"""
        if self.description:
            prompt += f"\nContext:\n{self.description}\n"

        return prompt.strip()
