"""Browser cookie import: a download can carry the cookies a browser holds for its site.

`spec` is `browser` or `browser:profile` (a profile name or a path), the form yt-dlp takes.
yt-dlp's extractor reads Firefox and the Chromium family, Chrome's encrypted store included,
so it runs in a worker thread: unlocking a keyring can take a while. Cookie values are never
logged.
"""

import asyncio
import logging
import threading
import time
import urllib.request
from contextlib import AbstractContextManager, nullcontext
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any

from yt_dlp.cookies import SUPPORTED_BROWSERS, extract_cookies_from_browser

log = logging.getLogger(__name__)

CACHE_SECONDS = 60.0
_cache: dict[str, tuple[float, CookieJar]] = {}
_lock = threading.Lock()

# Where each browser keeps its profiles on Linux, deb and snap and Flatpak installs included.
_DIRS: dict[str, tuple[str, ...]] = {
    "firefox": (
        ".mozilla/firefox",
        "snap/firefox/common/.mozilla/firefox",
        ".var/app/org.mozilla.firefox/.mozilla/firefox",
    ),
    "chrome": (".config/google-chrome", ".var/app/com.google.Chrome/config/google-chrome"),
    "chromium": (
        ".config/chromium",
        "snap/chromium/common/chromium",
        ".var/app/org.chromium.Chromium/config/chromium",
    ),
    "brave": (
        ".config/BraveSoftware/Brave-Browser",
        ".var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser",
    ),
    "edge": (".config/microsoft-edge",),
    "opera": (".config/opera",),
    "vivaldi": (".config/vivaldi",),
}


class CookieError(Exception):
    """The browser's cookies could not be read. The message is safe to show."""


class _QuietLogger:
    """yt-dlp's logger interface, sent to our log at debug level. Messages hold no values."""

    def debug(self, message: str) -> None:
        log.debug("cookies: %s", message)

    def info(self, message: str) -> None:
        log.debug("cookies: %s", message)

    def warning(self, message: str, only_once: bool = False) -> None:
        log.info("cookies: %s", message)

    def error(self, message: str) -> None:
        log.warning("cookies: %s", message)

    def progress_bar(self) -> AbstractContextManager[Any]:
        return nullcontext(_NoBar())


class _NoBar:
    def print(self, message: str) -> None:
        pass


def browsers(home: Path) -> list[str]:
    """Supported browsers with a profile directory under this home, in a stable order."""
    return [
        name
        for name, dirs in _DIRS.items()
        if name in SUPPORTED_BROWSERS and any((home / d).is_dir() for d in dirs)
    ]


def parse(spec: str) -> tuple[str, str | None]:
    browser, _, profile = spec.strip().partition(":")
    browser = browser.lower()
    if browser not in SUPPORTED_BROWSERS:
        names = ", ".join(sorted(SUPPORTED_BROWSERS))
        raise CookieError(f"unknown browser {browser!r}; use one of {names}")
    return browser, profile or None


def _jar(spec: str) -> CookieJar:
    """Read once per minute at most, so a batch of a thousand links reads the store once."""
    now = time.monotonic()
    with _lock:
        cached = _cache.get(spec)
        if cached is not None and now - cached[0] < CACHE_SECONDS:
            return cached[1]
    browser, profile = parse(spec)
    try:
        jar: CookieJar = extract_cookies_from_browser(browser, profile, logger=_QuietLogger())
    except Exception as error:  # yt-dlp raises many kinds; all mean "could not read them".
        raise CookieError(f"could not read cookies from {browser}: {error}") from None
    with _lock:
        _cache[spec] = (now, jar)
    return jar


def cookie_header_sync(spec: str, url: str) -> str:
    """The Cookie header the browser would send to url, or "" when it holds none."""
    jar = _jar(spec)
    request = urllib.request.Request(url)  # noqa: S310 - only used to match cookies to the URL
    jar.add_cookie_header(request)
    return request.get_header("Cookie") or ""


async def cookie_header(spec: str, url: str) -> str:
    return await asyncio.to_thread(cookie_header_sync, spec, url)


def merge(existing: str, imported: str) -> str:
    """Cookies given explicitly win over imported ones with the same name."""
    if not imported:
        return existing
    if not existing:
        return imported
    named = {part.split("=", 1)[0].strip() for part in existing.split(";") if "=" in part}
    extra = [
        part.strip()
        for part in imported.split(";")
        if "=" in part and part.split("=", 1)[0].strip() not in named
    ]
    return "; ".join([existing.strip().rstrip(";"), *extra]) if extra else existing
