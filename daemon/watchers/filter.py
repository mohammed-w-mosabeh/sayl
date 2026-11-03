"""Decides whether copied text is a link worth offering. See "Clipboard" in docs/SPEC.md."""

import fnmatch
import time
from collections.abc import Callable, Iterable
from urllib.parse import urlsplit

SCHEMES = frozenset({"http", "https", "ftp", "magnet"})
DEBOUNCE_SECONDS = 10.0
MAX_LENGTH = 4096  # Longer text is a document, not a link someone means to download.


def as_link(text: str) -> str | None:
    """The link, if the text is exactly one supported link and nothing else."""
    value = text.strip()
    if not value or len(value) > MAX_LENGTH or any(char.isspace() for char in value):
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in SCHEMES:
        return None
    if scheme == "magnet":
        return value if "xt=" in parts.query else None
    return value if parts.hostname else None


def host_of(link: str) -> str:
    try:
        return (urlsplit(link).hostname or "").lower()
    except ValueError:
        return ""


def ignored(link: str, patterns: Iterable[str]) -> bool:
    """A pattern is a host, which also covers its subdomains, or a glob over the whole link."""
    host = host_of(link)
    for raw in patterns:
        pattern = raw.strip().lower()
        if not pattern:
            continue
        if any(char in pattern for char in "*?["):
            if fnmatch.fnmatchcase(link.lower(), pattern):
                return True
        elif host == pattern or host.endswith(f".{pattern}"):
            return True
    return False


class ClipFilter:
    """Turns clipboard changes into links, once each.

    `prime` records what was on the clipboard before watching began, so starting the daemon
    never offers an old link. A link seen in the last ten seconds is not offered again, even if
    something else was copied in between.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.last_text: str | None = None
        self.seen: dict[str, float] = {}
        self.patterns: tuple[str, ...] = ()

    def prime(self, text: str | None) -> None:
        self.last_text = text

    def accept(self, text: str) -> str | None:
        if text == self.last_text:
            return None  # Not a change: the same content read again.
        self.last_text = text
        link = as_link(text)
        if link is None or ignored(link, self.patterns):
            return None
        now = self.clock()
        self.seen = {key: at for key, at in self.seen.items() if now - at < DEBOUNCE_SECONDS}
        if link in self.seen:
            return None
        self.seen[link] = now
        return link
