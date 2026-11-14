"""Batch download patterns: `file[001-100].jpg`, `img_[a-f].png`, `s[1-3]e[01-10].mkv`.

A bracket holding a number range or a single-letter range expands; the width of the first
number keeps zero padding. Anything else in brackets stays literal, so IPv6 hosts such as
`http://[::1]/` are left alone. Several ranges multiply out, left to right.
"""

import itertools
import re

MAX_ITEMS = 10_000
_RANGE = re.compile(r"\[(?:(\d+)-(\d+)|([a-zA-Z])-([a-zA-Z]))(?::(\d+))?\]")


def _values(match: re.Match[str]) -> list[str]:
    first, last, low, high, step_text = match.groups()
    step = int(step_text) if step_text else 1
    if step < 1:
        raise ValueError("a range step must be at least 1")
    if first is not None:
        start, end = int(first), int(last)
        width = len(first) if first.startswith("0") else 0
        values = range(start, end + 1, step) if start <= end else range(start, end - 1, -step)
        return [str(value).zfill(width) for value in values]
    if low.islower() != high.islower():
        raise ValueError("a letter range keeps one case, such as [a-z] or [A-Z]")
    start, end = ord(low), ord(high)
    codes = range(start, end + 1, step) if start <= end else range(start, end - 1, -step)
    return [chr(code) for code in codes]


def pattern_ranges(text: str) -> bool:
    """Whether text holds a range that expand would expand."""
    return _RANGE.search(text) is not None


def expand(pattern: str) -> list[str]:
    """Every URL the pattern names, in order. Raises ValueError past MAX_ITEMS."""
    matches = list(_RANGE.finditer(pattern))
    if not matches:
        return [pattern]
    parts: list[str] = []
    choices: list[list[str]] = []
    position = 0
    total = 1
    for match in matches:
        parts.append(pattern[position : match.start()])
        values = _values(match)
        choices.append(values)
        total *= len(values)
        if total > MAX_ITEMS:
            raise ValueError(f"the pattern names more than {MAX_ITEMS} links")
        position = match.end()
    tail = pattern[position:]
    return [
        "".join(itertools.chain.from_iterable(zip(parts, combo, strict=True))) + tail
        for combo in itertools.product(*choices)
    ]
