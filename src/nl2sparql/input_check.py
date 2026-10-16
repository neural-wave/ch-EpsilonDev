"""Input check: normalize the request text, reject only empty or oversized input.

Clinical questions contain characters like ≥, µ, ° and accented names, so we never reject a
request because of the characters it uses. Emoji and control characters are removed.
"""
from __future__ import annotations

import re
import unicodedata

EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF️‍]",
)
WHITESPACE_RE = re.compile(r"\s+")


class InputError(ValueError):
    """The request text cannot be processed. The API answers HTTP 400."""


def normalize_text(text: object, max_chars: int = 2000) -> str:
    if not isinstance(text, str):
        raise InputError("'text' must be a string.")
    normalized = unicodedata.normalize("NFC", text)
    normalized = EMOJI_RE.sub("", normalized)
    normalized = "".join(
        ch if unicodedata.category(ch)[0] != "C" else " " for ch in normalized
    )
    normalized = WHITESPACE_RE.sub(" ", normalized).strip()
    if not normalized:
        raise InputError("'text' must not be empty.")
    if len(normalized) > max_chars:
        raise InputError(f"'text' must be at most {max_chars} characters.")
    return normalized
