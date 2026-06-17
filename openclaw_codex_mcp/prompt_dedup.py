from __future__ import annotations

import difflib
import hashlib
import re
import unicodedata


DEFAULT_PROMPT_SIMILARITY_THRESHOLD = 0.90
MIN_FUZZY_PROMPT_CHARS = 40


def normalize_prompt(value: str) -> str:
    text = unicodedata.normalize("NFKC", value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\s+", " ", text, flags=re.UNICODE)
    return text.strip().casefold()


def prompt_hash(normalized_prompt: str) -> str:
    return hashlib.sha256(normalized_prompt.encode("utf-8")).hexdigest()


def prompt_similarity(left: str, right: str) -> float:
    if left == right:
        return 1.0
    if min(len(left), len(right)) < MIN_FUZZY_PROMPT_CHARS:
        return 0.0
    sequence_ratio = difflib.SequenceMatcher(None, left, right, autojunk=False).ratio()
    token_ratio = difflib.SequenceMatcher(
        None,
        _token_sort(left),
        _token_sort(right),
        autojunk=False,
    ).ratio()
    return max(sequence_ratio, token_ratio)


def _token_sort(value: str) -> str:
    return " ".join(sorted(re.findall(r"\w+", value, flags=re.UNICODE)))
