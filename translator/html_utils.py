import re
from bs4 import BeautifulSoup
from typing import Literal

SplitTag = Literal["<br>", "</p>"]

DEFAULT_SPLIT_TAG: SplitTag = "<br>"
MAX_CHUNK_SIZE = 4000


def extract_html_content(soup: BeautifulSoup, split_tag: SplitTag) -> str:
    container = soup.find("div") or soup.find("body")
    if not container:
        return ""

    content = "".join(str(x).strip() for x in container.contents)
    content = re.sub(r"\n+", "", content)

    if split_tag == "<br>":
        content = content.replace("<br/>", "<br>")

    return content


def split_html(
    content: str,
    split_tag: SplitTag = DEFAULT_SPLIT_TAG,
    max_size: int = MAX_CHUNK_SIZE,
) -> list[str]:
    parts = content.split(split_tag)
    chunks: list[str] = []
    buf = ""

    for part in parts:
        candidate = buf + split_tag + part if buf else part
        if len(candidate) > max_size:
            if buf:
                chunks.append(buf + split_tag)
            buf = part
        else:
            buf = candidate

    if buf:
        chunks.append(buf)

    if split_tag == "</p>":
        chunks = [
            c if c.endswith("</p>") else c + "</p>"
            for c in chunks
        ]
    else:
        chunks = [
            c if c.endswith("<br>") else c + "<br>"
            for c in chunks[:-1]
        ] + [chunks[-1]]

    return chunks


def assemble_html(chunks: list[str], split_tag: SplitTag) -> str:
    wrapper = "div" if split_tag == "<br>" else "body"

    return f"""<?xml version="1.0" encoding="utf-8"?>
<html>
<{wrapper}>
{''.join(chunks)}
</{wrapper}>
</html>"""
