"""What the capture proxy intercepts. Pure functions: no mitmproxy, easy to test.

See "Addon logic" in docs/SPEC.md. Decisions happen on response headers, never on bodies.
"""

import fnmatch
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

DEFAULT_THRESHOLD = 2 * 1024 * 1024

DOWNLOAD_EXTENSIONS = frozenset(
    {
        ".7z", ".aab", ".apk", ".appimage", ".bin", ".bz2", ".cab", ".deb", ".dmg", ".epub",
        ".exe", ".flatpak", ".gz", ".img", ".iso", ".jar", ".lz", ".lzma", ".msi", ".msix",
        ".pkg", ".rar", ".rpm", ".run", ".snap", ".tar", ".tbz2", ".tgz", ".torrent", ".txz",
        ".vhd", ".vmdk", ".xz", ".zip", ".zst",
    }
)  # fmt: skip
_NOT_BINARY = (
    "text/",
    "font/",
    "application/json",
    "application/javascript",
    "application/xml",
    "application/xhtml+xml",
    "application/x-javascript",
    "application/font",
    "application/x-font",
    "application/vnd.ms-fontobject",
)
_MANIFEST_EXTENSIONS = (".m3u8", ".mpd")
_MANIFEST_TYPES = frozenset(
    {"application/dash+xml", "application/vnd.apple.mpegurl", "application/x-mpegurl"}
)

# Never intercepted: mitmproxy reads the SNI from the ClientHello and splices the raw TCP stream
# to the real server, so the client sees the real certificate. Hosts include their subdomains.
# Deliberately tiny: every host here is a host Sayl cannot capture downloads from. More hosts are
# opt-in, through `capture_passthrough` in config.toml or the `capture_bypass` setting, or added
# automatically when a browser rejects Sayl's CA for one host.
DEFAULT_BYPASS = (
    "anthropic.com",  # Also *.anthropic.com: API clients pin to the public roots.
    "claude.ai",  # Also *.claude.ai.
)  # Loopback needs no entry: the PAC file sends it DIRECT and never reaches the proxy.


@dataclass(frozen=True)
class Request:
    method: str
    url: str
    fetch_mode: str | None
    fetch_dest: str | None = None
    accept: str = ""


def mime_of(headers: Mapping[str, str]) -> str:
    return headers.get("content-type", "").split(";")[0].strip().lower()


def is_interceptable(request: Request) -> bool:
    """Top-level navigations, or requests with no Sec-Fetch headers that ask for a page.

    Browsers omit Sec-Fetch headers on plain http://, where a navigation still asks for HTML.
    Anything else without them is browser-internal or non-browser traffic and is left alone.
    """
    if request.fetch_mode is None:
        return urlsplit(request.url).scheme == "http" and "text/html" in request.accept
    return request.fetch_mode == "navigate"


def content_length(headers: Mapping[str, str]) -> int | None:
    value = headers.get("content-length", "")
    return int(value) if value.isdigit() else None


def is_download(
    request: Request, headers: Mapping[str, str], threshold: int = DEFAULT_THRESHOLD
) -> bool:
    """Attachment, a known download extension, or a large binary body."""
    if headers.get("content-disposition", "").strip().lower().startswith("attachment"):
        return True
    suffix = PurePosixPath(unquote(urlsplit(request.url).path)).suffix.lower()
    if suffix in DOWNLOAD_EXTENSIONS:
        return True
    mime = mime_of(headers)
    if not mime or mime.startswith(_NOT_BINARY) or mime in _MANIFEST_TYPES:
        return False
    length = content_length(headers)
    return length is not None and length > threshold


def is_media_manifest(url: str, headers: Mapping[str, str]) -> bool:
    path = urlsplit(url).path.lower()
    return path.endswith(_MANIFEST_EXTENSIONS) or mime_of(headers) in _MANIFEST_TYPES


def is_media(headers: Mapping[str, str]) -> bool:
    mime = mime_of(headers)
    return mime.startswith(("video/", "audio/")) and mime != "video/mp2t"  # Not HLS segments.


def bypass_pattern(entry: str) -> str | None:
    """A mitmproxy `ignore_hosts` regex for one bypass entry, matched against `host:port`.

    Path-qualified entries (github.com/login) cannot be bypassed at the TLS layer, where only
    the host is known; they are checked per request by `bypassed` instead.
    """
    value = entry.strip().lower()
    if not value or "/" in value:
        return None
    if any(char in value for char in "*?["):
        body = fnmatch.translate(value).removeprefix("(?s:").removesuffix(r")\Z")
        return rf"^{body}(:\d+)?$"
    return rf"^(.+\.)?{re.escape(value)}(:\d+)?$"


def ignore_hosts(entries: Iterable[str]) -> list[str]:
    return [pattern for entry in entries if (pattern := bypass_pattern(entry))]


def bypassed(url: str, entries: Iterable[str]) -> bool:
    """For a request that was already decrypted: does any entry, including paths, cover it?"""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    target = f"{host}{parts.path}".lower()
    for entry in entries:
        value = entry.strip().lower()
        if not value:
            continue
        if "/" in value:
            prefix = value.rstrip("/")
            if target == prefix or target.startswith(prefix + "/"):
                return True
        elif (pattern := bypass_pattern(value)) and re.match(pattern, host):
            return True
    return False
