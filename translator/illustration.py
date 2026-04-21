import hashlib
import json
import logging
import posixpath
import re
import base64
import urllib.error
import urllib.parse
import urllib.request

from ebooklib import epub


HTML_TAG_PATTERN = re.compile(r"<[^>]+>")
SPACE_PATTERN = re.compile(r"\s+")


class IllustrationManager:
    def __init__(self, book, config: dict | None = None):
        self.book = book
        cfg = config or {}

        self.enabled = bool(cfg.get("enabled", False))
        self.provider = str(cfg.get("provider", "pollinations")).strip().lower()
        self.model = str(cfg.get("model", "flux")).strip()
        self.every_n_chunks = max(1, int(cfg.get("every_n_chunks", 3)))
        self.max_images_per_chapter = max(0, int(cfg.get("max_images_per_chapter", 3)))
        self.min_chunk_chars = max(20, int(cfg.get("min_chunk_chars", 140)))
        self.prompt_max_chars = max(120, int(cfg.get("prompt_max_chars", 420)))
        self.width = max(256, int(cfg.get("width", 768)))
        self.height = max(256, int(cfg.get("height", 1152)))
        self.timeout_seconds = max(5, int(cfg.get("timeout_seconds", 30)))
        self.output_dir = str(cfg.get("output_dir", "images/generated")).strip("/") or "images/generated"
        self.style_prompt = (
            str(
                cfg.get(
                    "style_prompt",
                    "Cinematic light-novel illustration, vivid details, no text overlays, safe for work",
                )
            )
            .strip()
        )
        self.default_alt_text = str(cfg.get("default_alt_text", "Minh hoa noi dung")).strip() or "Minh hoa noi dung"
        self.webai_base_url = str(cfg.get("webai_base_url", "")).strip().rstrip("/")
        self.webai_api_key = str(cfg.get("webai_api_key", "")).strip()
        self.webai_image_endpoint = str(
            cfg.get("webai_image_endpoint", "/v1/images/generations")
        ).strip()
        if self.webai_image_endpoint and not self.webai_image_endpoint.startswith("/"):
            self.webai_image_endpoint = f"/{self.webai_image_endpoint}"
        self.webai_response_format = str(
            cfg.get("webai_response_format", "b64_json")
        ).strip()

        # prompt hash -> EPUB file path
        self._prompt_cache: dict[str, str] = {}

    def is_enabled(self) -> bool:
        return self.enabled and self.max_images_per_chapter > 0

    def inject_illustrations(
        self,
        translated_chunks: list[str],
        source_chunks: list[str],
        split_tag: str,
        chapter_number: int | None = None,
        chapter_file_name: str | None = None,
        chapter_title: str | None = None,
    ) -> list[str]:
        if not self.is_enabled():
            return translated_chunks

        if len(translated_chunks) != len(source_chunks):
            logging.warning("Illustration injection skipped: source/translated chunk length mismatch")
            return translated_chunks

        output: list[str] = []
        inserted = 0

        for idx, (translated_chunk, source_chunk) in enumerate(zip(translated_chunks, source_chunks), start=1):
            output.append(translated_chunk)

            if inserted >= self.max_images_per_chapter:
                continue
            if idx % self.every_n_chunks != 0:
                continue
            if not self._is_chunk_eligible(source_chunk):
                continue

            prompt = self._build_prompt(
                source_chunk=source_chunk,
                chapter_number=chapter_number,
                chapter_title=chapter_title,
                chunk_index=idx,
            )
            image_src = self._ensure_epub_image(
                prompt=prompt,
                chapter_file_name=chapter_file_name,
                chapter_number=chapter_number,
                chunk_index=idx,
            )
            if not image_src:
                continue

            output.append(self._build_image_block(image_src, split_tag=split_tag, chunk_index=idx))
            inserted += 1

        return output

    def _is_chunk_eligible(self, chunk: str) -> bool:
        plain = self._strip_tags(chunk)
        return len(plain) >= self.min_chunk_chars

    def _build_prompt(
        self,
        source_chunk: str,
        chapter_number: int | None,
        chapter_title: str | None,
        chunk_index: int,
    ) -> str:
        scene = self._strip_tags(source_chunk)
        if len(scene) > self.prompt_max_chars:
            scene = scene[: self.prompt_max_chars].rstrip()

        chapter_context = ""
        if chapter_title:
            chapter_context = f"Chapter: {chapter_title}. "
        elif chapter_number is not None:
            chapter_context = f"Chapter {chapter_number}. "

        return (
            f"{self.style_prompt}. "
            f"{chapter_context}"
            f"Chunk {chunk_index}. "
            f"Scene summary: {scene}"
        ).strip()

    def _ensure_epub_image(
        self,
        prompt: str,
        chapter_file_name: str | None,
        chapter_number: int | None,
        chunk_index: int,
    ) -> str | None:
        prompt_key = hashlib.sha1(prompt.encode("utf-8", errors="ignore")).hexdigest()
        cached_file_name = self._prompt_cache.get(prompt_key)
        if cached_file_name is not None:
            return self._to_chapter_relative_path(cached_file_name, chapter_file_name)

        image_bytes = self._generate_image_bytes(prompt)
        if not image_bytes:
            return None

        chapter_label = f"ch-{chapter_number}" if chapter_number is not None else "chapter"
        file_name = posixpath.join(
            self.output_dir,
            f"{chapter_label}-chunk-{chunk_index}-{prompt_key[:12]}.jpg",
        )

        image_item = epub.EpubItem(
            uid=f"img-{prompt_key[:16]}",
            file_name=file_name,
            media_type="image/jpeg",
            content=image_bytes,
        )
        self.book.add_item(image_item)
        self._prompt_cache[prompt_key] = file_name
        logging.info("Illustration added: %s", file_name)
        return self._to_chapter_relative_path(file_name, chapter_file_name)

    def _generate_image_bytes(self, prompt: str) -> bytes | None:
        if self.provider == "pollinations":
            return self._generate_via_pollinations(prompt)

        if self.provider == "webai":
            return self._generate_via_webai(prompt)

        logging.warning("Unsupported illustration provider: %s", self.provider)
        return None

    def _generate_via_pollinations(self, prompt: str) -> bytes | None:

        encoded_prompt = urllib.parse.quote(prompt, safe="")
        query = urllib.parse.urlencode(
            {
                "width": self.width,
                "height": self.height,
                "model": self.model,
                "nologo": "true",
            }
        )
        url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?{query}"

        try:
            with urllib.request.urlopen(url, timeout=self.timeout_seconds) as response:
                return response.read()
        except Exception as exc:
            logging.warning("Failed to generate illustration via Pollinations: %s", exc)
            return None

    def _generate_via_webai(self, prompt: str) -> bytes | None:
        if not self.webai_base_url:
            logging.warning(
                "Illustration provider 'webai' requires webai_base_url in translation.illustration config"
            )
            return None

        if self.webai_image_endpoint.endswith("/chat/completions"):
            return self._generate_via_webai_chat_completions(prompt)

        return self._generate_via_webai_images_endpoint(prompt)

    def _generate_via_webai_images_endpoint(self, prompt: str) -> bytes | None:
        url = urllib.parse.urljoin(
            f"{self.webai_base_url}/",
            self.webai_image_endpoint.lstrip("/"),
        )
        request_payload = {
            "model": self.model,
            "prompt": prompt,
            "size": f"{self.width}x{self.height}",
            "width": self.width,
            "height": self.height,
            "n": 1,
            "response_format": self.webai_response_format,
        }

        response_payload = self._post_json(url, request_payload)
        return self._extract_image_bytes_from_payload(response_payload)

    def _generate_via_webai_chat_completions(self, prompt: str) -> bytes | None:
        url = urllib.parse.urljoin(
            f"{self.webai_base_url}/",
            self.webai_image_endpoint.lstrip("/"),
        )

        size = f"{self.width}x{self.height}"
        payload_candidates = [
            {
                "model": self.model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    f"Generate one illustration only. {prompt}. "
                                    "Return image output, not explanation text."
                                ),
                            }
                        ],
                    }
                ],
                "modalities": ["text", "image"],
                "image": {
                    "size": size,
                },
                "response_format": self.webai_response_format,
                "stream": False,
                "temperature": 0.2,
            },
            {
                "model": self.model,
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            f"Generate one image for this prompt: {prompt}. "
                            f"Target size {size}. Return image data only."
                        ),
                    }
                ],
                "stream": False,
                "temperature": 0.2,
            },
            {
                "model": self.model,
                "messages": [
                    {
                        "role": "system",
                        "content": "You are an image generator.",
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": prompt,
                            }
                        ],
                    },
                ],
                "stream": False,
            },
        ]

        for idx, request_payload in enumerate(payload_candidates, start=1):
            response_payload = self._post_json(url, request_payload, suppress_warning=True)
            if response_payload is None:
                continue

            image_bytes = self._extract_image_bytes_from_payload(response_payload)
            if image_bytes:
                if idx > 1:
                    logging.info("WebAI chat image generation succeeded with payload variant %s", idx)
                return image_bytes

        logging.warning("WebAI chat completion response did not contain image data")
        return None

    def _post_json(self, url: str, request_payload: dict, suppress_warning: bool = False) -> dict | None:
        body = json.dumps(request_payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.webai_api_key:
            headers["Authorization"] = f"Bearer {self.webai_api_key}"

        request = urllib.request.Request(url, data=body, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            details = ""
            try:
                error_body = exc.read().decode("utf-8", errors="ignore")
                if error_body:
                    details = f" | body={error_body[:300]}"
            except Exception:
                details = ""

            if not suppress_warning:
                logging.warning("Failed to generate illustration via WebAI: HTTP %s%s", exc.code, details)
            return None
        except Exception as exc:
            if not suppress_warning:
                logging.warning("Failed to generate illustration via WebAI: %s", exc)
            return None

        try:
            return json.loads(raw.decode("utf-8", errors="ignore"))
        except Exception as exc:
            if not suppress_warning:
                logging.warning("WebAI image endpoint returned non-JSON response: %s", exc)
            return None

    def _extract_image_bytes_from_payload(self, payload: dict | None) -> bytes | None:
        if not isinstance(payload, dict):
            return None

        data_items = payload.get("data") or []
        if not data_items:
            choices = payload.get("choices") or []
            if choices:
                message = (choices[0].get("message") if isinstance(choices[0], dict) else {}) or {}

                direct_image = message.get("image") if isinstance(message, dict) else None
                image_bytes = self._image_bytes_from_object(direct_image)
                if image_bytes:
                    return image_bytes

                content = message.get("content") if isinstance(message, dict) else None
                if isinstance(content, list):
                    for part in content:
                        image_bytes = self._image_bytes_from_object(part)
                        if image_bytes:
                            return image_bytes
                elif isinstance(content, str):
                    image_bytes = self._image_bytes_from_text(content)
                    if image_bytes:
                        return image_bytes

            return None

        first = data_items[0] if isinstance(data_items[0], dict) else {}
        return self._image_bytes_from_object(first)

    def _image_bytes_from_object(self, obj) -> bytes | None:
        if not isinstance(obj, dict):
            return None

        b64_image = (
            obj.get("b64_json")
            or obj.get("image_base64")
            or obj.get("base64")
            or obj.get("image")
        )
        if isinstance(b64_image, str) and b64_image:
            image_bytes = self._decode_base64_image(b64_image)
            if image_bytes:
                return image_bytes

        image_url = obj.get("url")
        if isinstance(image_url, str) and image_url:
            if image_url.startswith("data:image/"):
                return self._image_bytes_from_text(image_url)
            try:
                with urllib.request.urlopen(image_url, timeout=self.timeout_seconds) as response:
                    return response.read()
            except Exception as exc:
                logging.warning("Failed to download WebAI image URL: %s", exc)
                return None

        nested_image_url = obj.get("image_url")
        if isinstance(nested_image_url, dict):
            url = nested_image_url.get("url")
            if isinstance(url, str) and url:
                if url.startswith("data:image/"):
                    return self._image_bytes_from_text(url)
                try:
                    with urllib.request.urlopen(url, timeout=self.timeout_seconds) as response:
                        return response.read()
                except Exception as exc:
                    logging.warning("Failed to download WebAI nested image URL: %s", exc)
                    return None

        return None

    def _image_bytes_from_text(self, text: str) -> bytes | None:
        if not isinstance(text, str) or not text:
            return None

        data_uri_match = re.search(r"data:image/[^;]+;base64,([A-Za-z0-9+/=\r\n]+)", text)
        if data_uri_match:
            return self._decode_base64_image(data_uri_match.group(1))

        trimmed = text.strip()
        if trimmed.startswith("{"):
            try:
                parsed = json.loads(trimmed)
                if isinstance(parsed, dict):
                    return self._image_bytes_from_object(parsed)
            except Exception:
                return None

        return None

    @staticmethod
    def _decode_base64_image(value: str) -> bytes | None:
        cleaned = value.strip()
        if not cleaned:
            return None

        if cleaned.startswith("data:image/"):
            parts = cleaned.split(",", 1)
            cleaned = parts[1] if len(parts) == 2 else cleaned

        try:
            return base64.b64decode(cleaned)
        except Exception:
            return None

    def _build_image_block(self, image_src: str, split_tag: str, chunk_index: int) -> str:
        alt_text = f"{self.default_alt_text} - chunk {chunk_index}"
        figure = (
            '<div class="chunk-illustration" style="text-align:center;margin:1em 0;">'
            f'<img src="{image_src}" alt="{alt_text}" '
            'style="max-width:100%;height:auto;"/>'
            "</div>"
        )

        if split_tag == "<br>":
            return f"{figure}<br>"
        return figure

    @staticmethod
    def _strip_tags(text: str) -> str:
        without_tags = HTML_TAG_PATTERN.sub(" ", text or "")
        return SPACE_PATTERN.sub(" ", without_tags).strip()

    @staticmethod
    def _to_chapter_relative_path(asset_file_name: str, chapter_file_name: str | None) -> str:
        chapter_dir = posixpath.dirname(chapter_file_name or "")
        if not chapter_dir:
            return asset_file_name
        return posixpath.relpath(asset_file_name, start=chapter_dir)