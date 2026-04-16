
import logging


LABEL_MAX_LEN_OVERRIDES = {
    "INPUT_CHUNK": 500,
    "AI_RESPONSE": 500,
}


def log_text(label: str, text: str, max_len: int = 1000):
    """
    Log text safely without flooding log file.
    """
    if not text:
        logging.info(f"{label}: <EMPTY>")
        return

    max_len = LABEL_MAX_LEN_OVERRIDES.get(label, max_len)

    if len(text) <= max_len:
        preview = text
    else:
        marker = " ...[TRUNCATED MIDDLE]... "
        available = max_len - len(marker)

        # If max_len is too small, keep a compact marker-only preview.
        if available <= 0:
            preview = marker.strip()
        else:
            head_len = max(1, available // 2)
            tail_len = max(1, available - head_len)
            preview = f"{text[:head_len]}{marker}{text[-tail_len:]}"

    logging.info(f"{label} ({len(text)} chars):\n{preview}")


def log_consistency_event(event: str, details: str | None = None):
    if details:
        logging.info(f"CONSISTENCY::{event}: {details}")
        return

    logging.info(f"CONSISTENCY::{event}")