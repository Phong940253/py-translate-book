
import logging

def log_text(label: str, text: str, max_len: int = 1000):
    """
    Log text safely without flooding log file.
    """
    if not text:
        logging.info(f"{label}: <EMPTY>")
        return

    preview = text[:max_len]
    suffix = " ...[TRUNCATED]" if len(text) > max_len else ""
    logging.info(f"{label} ({len(text)} chars):\n{preview}{suffix}")