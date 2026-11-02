"""Single-use hint. It only skips acceleration, and never decides correctness."""

import math
from collections import Counter
from urllib.parse import parse_qsl, urlsplit

_SIGNED_PARAMS = frozenset(
    [
        "awsaccesskeyid",
        "expires",
        "googleaccessid",
        "se",
        "sig",
        "signature",
        "token",
        "x-amz-signature",
        "x-goog-signature",
    ]
)
_MIN_TOKEN_LENGTH = 20
_MIN_ENTROPY_BITS = 3.5


def _entropy(text: str) -> float:
    return -sum(n / len(text) * math.log2(n / len(text)) for n in Counter(text).values())


def _looks_like_token(segment: str) -> bool:
    if len(segment) < _MIN_TOKEN_LENGTH or "." in segment:
        return False
    has_digit = any(char.isdigit() for char in segment)
    has_alpha = any(char.isalpha() for char in segment)
    return has_digit and has_alpha and _entropy(segment) >= _MIN_ENTROPY_BITS


def looks_single_use(url: str) -> bool:
    parts = urlsplit(url)
    if any(key.lower() in _SIGNED_PARAMS for key, _ in parse_qsl(parts.query)):
        return True
    return any(_looks_like_token(segment) for segment in parts.path.split("/"))
