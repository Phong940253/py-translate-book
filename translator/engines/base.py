from abc import ABC, abstractmethod


class TranslationEngine(ABC):
    def __init__(
        self,
        from_lang: str,
        to_lang: str,
        description: str | None = None,
        custom_prompt: str | None = None,
    ):
        self.from_lang = from_lang
        self.to_lang = to_lang
        self.description = description
        self.custom_prompt = custom_prompt

    @abstractmethod
    def translate(self, text: str) -> str:
        pass

    def supports_batch(self) -> bool:
        return False

    def translate_batch(self, texts: list[str]) -> list[str]:
        raise NotImplementedError("Batch translation is not supported by this engine")

    def system_prompt(self) -> str:
        if self.custom_prompt:
            prompt = self.custom_prompt.strip()
            prompt = prompt.replace("{from_lang}", self.from_lang)
            prompt = prompt.replace("{to_lang}", self.to_lang)
            if self.description and "{description}" in prompt:
                prompt = prompt.replace("{description}", self.description)
            return prompt

        prompt = f"""
You are an expert literary translator and post-editor.
Translate from {self.from_lang} to {self.to_lang}.

Output constraints (MUST follow):
1) Keep ALL HTML tags/attributes/entities exactly unchanged.
2) Translate all visible source text fully into natural Vietnamese.
3) Final output must not contain Chinese characters.
4) Do not add explanation, notes, markdown, or code fences.
5) Keep paragraph/sentence order exactly as input.

Cleaning rules:
- Remove watermark/noise fragments such as: bqgooヽcc, bqg00, bqg., wap, .com-like tail noise.
- If a short garbage token appears at line end, delete it instead of translating.

Terminology and style:
- Use consistent Hán-Việt for proper nouns, sects, realms, techniques, and item names.
- Keep cultivation terms coherent across the same chunk.
- Tone: webnovel xianxia, fluent and readable Vietnamese.

Disambiguation for slang/idioms:
- "大比兜" = "một bạt tai thật lực" (slap), NOT tournament/combat event.
- If source is humorous/ironic, preserve humor naturally.

Quality self-check before finalizing each chunk:
- Ensure no untranslated Chinese remains.
- Ensure no source sentence is dropped.
- Ensure no obvious mistranslation of idioms/slang.
"""
        prompt += "Dịch thuần Việt, không pha tiếng Trung.\n"
        prompt += "Nếu gặp tên riêng chưa rõ, ưu tiên âm Hán Việt nhất quán.\n"

#         prompt += "A mysterious medieval setting in another world. "
#         prompt += "Translate the story in the writing style of an 18+ light novel. "
#         prompt += 'The main characters are Shin (Shinei Nouzen) and Lena (Vladilena Milizé). To make it more romantic, in their conversations, you can use "anh" and "em" when referring to each other.'
#         prompt += "Here is some information about the characters' dates of birth, which can be used to determine seniority and address in conversation. People of the same age and gender prefer to call each other cậu-tớ. "
#         prompt += """Vladilena Milizé (Lena, Handler One, Bloody Reina, The Bloodstained Queen, Regina ☆ Lena), gender: Female, dob: July 12th 2131.
# Henrietta von Penrose (Annette, Rita, Minerva, Owlette ☆ Annette), gender: Female, dob: November 12th 2131.
# Dustin Jaeger, gender: Male, dob: March 19th 2132.
# Lev Aldrecht, gender: Male, dob: January 13th.
# Jérôme Karlstahl, gender: Male, dob: November 27.
# Vaclav Milizé, gender: Male, dob: March 15th 2103
# The Reverend, gender: Male, dob: October 10.
# The Old Nan (The Old Nan, Alba Hag, Old Alba Woman, Raiden's Nana), gender: Female, dob: May 1st.
# Shinei Nouzen (Shin, Undertaker, Reaper, Báleygr), gender: Male, dob: May 19th 2132.
# Raiden Shuga, gender: Male, dob: August 25th 2132.
# Anju Emma, gender: Female, dob: October 2nd 2132.
# Kurena Kukumila, gender: Female, dob: May 6th 2133.
# Theoto Rikka (Theo, Laughing Fox), gender: Male, dob: April 20th 2132.
# Haruto Keats, gender: Male, dob: July 4th.
# Daiya Irma, gender: Male, dob: September 16th 2131.
# Kaie Taniya, gender: Female, dob: April 7th 2130.
# Kujo Nico, gender: Male, dob: March 17th 2130.
# Shana Aya, gender: Female, dob: November 9th.
# Reki Michihi, gender: Female, dob: March 4th 2133.
# Rito Oriya, gender: Male, dob: January 5th 2135.
# Siri Shion, gender: Male, dob: April 23rd.
# Canaan Nyuud, gender: Male, dob: August 18th.
# Suiu Tohkanya, gender: Female, dob: December 13th.
# Claude Knot, gender: Male, dob: January 29th.
# Tohru Ranshi, gender: Male, dob: February 14th.
# Yuuto Crow, gender: Male, dob: January 27.
# Chitori Myora (Chitori Oki, Citri (Not Canon)), gender: Female, dob: October 5th.
# Shourei Nouzen (Rei, Dullahan, Headless Knight), gender: Male, dob: October 18th 2122.
# Alice Araish, gender: Female, dob: April 18th 2126.
# Eijyu Nunat, gender: Male, dob: October 13th.
# Guren Akino, gender: Male, dob: November 22nd 2119.
# Touka Keisha, gender: Female, dob: August 31st 2122.
# Isuka, gender: Male, dob: February 4th 2125.
# Saiki Tateha, gender: Male, dob: September 4th 2130.
# Reisha Nouzen, gender: Male, dob: June 14.
# Yuuna Nouzen, gender: Female, dob: January 2.
# Louie Kino, gender: Male, dob: September 1st.
# Mina Shiroka, gender: Female, dob: December 25th.
# Grethe Wensel, gender: Female, dob: August 11th 2122.
# Erwin Marcel, gender: Male, dob: September 9th 2132.
# Augusta Frederica Adel-Adler (Frederica Rosenfort, Goddess of Victory, All-seeing Witch), gender: Female, dob: February 7th 2139.
# Brent Bernholdt, gender: Male, dob: November 16th.
# Eugene Rantz, gender: Male, dob: May 20th 2132.
# Nina Rantz, gender: Female, dob: March 8th 2143.
# Teresa, gender: Female, dob: May 30th.
# Ernst Zimmerman, gender: Male, dob: April 30th.
# Richard Altner, gender: Male, dob: December 12th 2112.
# Willem Ehrenfried, gender: Male, dob: February 16th 2122.
# Kiriya Nouzen (Kiri, Pale Rider), gender: Male, dob: July 12th 2129.
# Zelene Birkenbaum (Mistress, Merciless Queen), gender: Female, dob: December 9th.
# Yatrai Nouzen, gender: Male, dob: October 8th.
# Gilwiese Günter (Gil, Mock Turtle), gender: Male, dob: December 1.
# Svenja Brantolote, gender: Female, dob: March 27th 2140.
# Isabella Perschmann, gender: Female, dob: March 16th.
# Joschka Maika, gender: Male, dob: February 24th 2122.
# Noele Rohi, gender: Female, dob: April 27th.
# Ninha Rekaf, gender: Female, dob: June 12th.
# Mele, gender: Male, dob: January 12th.
# Niam Mialona, gender: Female, dob: March 31st.
# Viktor Idinarohk (Vika, Necrophile, The King of Corpses, The Serpent of Shackles and Decay, Gadyuka, Hveðrungr), gender: Male, dob: December 22nd 2131.
# Lerche (Seven-year-old, Lerchenlied, Chaika), gender: None (Feminine), dob: September 3rd 2131.
# Zafar Idinarohk, gender: Male, dob: April 29th 2122.
# Unknown, starts with "Ya" (Zashya, Roshya, Królik), gender: Female, dob: April 2nd.
# Olivia Aegis (Anna Maria, Olivier (Not Canon)), gender: Male, dob: March 8th.
# Ishmael Ahab, gender: Male, dob: August 12th.
# Esther, gender: Female, dob: July 6.
# Himmelnåde Réze, gender: Female, dob: June 27th 2135."""

        if self.description:
            prompt += f"\nContext:\n{self.description}\n"

        return prompt.strip()
