"""Pure download detection and handoff selection. No mitmproxy imports, so it is easy to test."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath
from urllib.parse import parse_qsl, unquote, urlsplit

DEFAULT_THRESHOLD = 2 * 1024 * 1024

DOWNLOAD_EXTENSIONS = frozenset(
    [
        ".7z",
        ".apk",
        ".appimage",
        ".bin",
        ".bz2",
        ".deb",
        ".dmg",
        ".exe",
        ".flatpak",
        ".gz",
        ".img",
        ".iso",
        ".jar",
        ".msi",
        ".pkg",
        ".rar",
        ".rpm",
        ".tar",
        ".tgz",
        ".torrent",
        ".xz",
        ".zip",
        ".zst",
    ]
)
_NOT_BINARY = ("text/", "font/", "application/json", "application/javascript", "application/xml")
_INLINE_MEDIA = ("audio/", "video/")
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
_MANIFEST_EXTENSIONS = (".m3u8", ".mpd")
_MANIFEST_TYPES = (
    "application/dash+xml",
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
)


class Strategy(Enum):
    RE_REQUEST = "re-request"
    STREAM_THROUGH = "stream-through"


@dataclass(frozen=True)
class Request:
    method: str
    url: str
    fetch_mode: str | None
    accept: str = ""


def is_interceptable(request: Request) -> bool:
    """Top-level navigations only.

    Browsers omit Sec-Fetch headers on insecure origins, so a plain http:// request without them
    can still be a navigation, and then it asks for HTML. Anything else without them is
    browser-internal or non-browser traffic, such as Chrome's http://clients2.google.com time
    check, and is left alone.
    """
    if request.fetch_mode is None:
        return urlsplit(request.url).scheme == "http" and "text/html" in request.accept
    return request.fetch_mode == "navigate"


def _content_length(headers: Mapping[str, str]) -> int:
    try:
        return int(headers.get("content-length", ""))
    except ValueError:
        return 0


def is_download(
    request: Request, headers: Mapping[str, str], threshold: int = DEFAULT_THRESHOLD
) -> bool:
    """Apply the three SPEC rules: attachment, known extension, or a large binary body."""
    if headers.get("content-disposition", "").lower().startswith("attachment"):
        return True
    suffix = PurePosixPath(unquote(urlsplit(request.url).path)).suffix.lower()
    if suffix in DOWNLOAD_EXTENSIONS:
        return True
    mime = headers.get("content-type", "").split(";")[0].strip().lower()
    if not mime or mime.startswith(_NOT_BINARY) or mime.startswith(_INLINE_MEDIA):
        return False
    if mime.startswith("image/"):
        return False
    return _content_length(headers) > threshold


def looks_single_use(url: str) -> bool:
    return any(key.lower() in _SIGNED_PARAMS for key, _ in parse_qsl(urlsplit(url).query))


def choose_strategy(request: Request, headers: Mapping[str, str]) -> Strategy:
    ranged = headers.get("accept-ranges", "").lower() == "bytes"
    if request.method != "GET" or not ranged or looks_single_use(request.url):
        return Strategy.STREAM_THROUGH
    return Strategy.RE_REQUEST


def is_media_manifest(url: str, headers: Mapping[str, str]) -> bool:
    path = urlsplit(url).path.lower()
    mime = headers.get("content-type", "").split(";")[0].strip().lower()
    return path.endswith(_MANIFEST_EXTENSIONS) or mime in _MANIFEST_TYPES


def filename_for(url: str, headers: Mapping[str, str]) -> str:
    """Pick a safe file name: never a path, never hidden, never empty."""
    disposition = headers.get("content-disposition", "")
    name = ""
    for part in disposition.split(";"):
        key, _, value = part.strip().partition("=")
        if key.lower() == "filename*":
            name = unquote(value.split("''")[-1].strip('"'))
            break
        if key.lower() == "filename":
            name = value.strip('"')
    if not name:
        name = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(char for char in name if char.isprintable()).lstrip(".")
    return name.encode()[:255].decode(errors="ignore") or "download.bin"
