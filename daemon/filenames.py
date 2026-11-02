"""Safe file names. A server chooses them, so treat them as hostile."""

from collections.abc import Mapping
from pathlib import Path
from urllib.parse import unquote, urlsplit

MAX_BYTES = 255
FALLBACK = "download.bin"
_RESERVED = frozenset({".", ".."})


def sanitize(name: str) -> str:
    """Strip path separators and control characters, never hidden, capped at 255 bytes."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    clean = "".join(char for char in base if char.isprintable()).strip().lstrip(".")
    if clean in _RESERVED or not clean:
        return FALLBACK
    return clean.encode()[:MAX_BYTES].decode(errors="ignore") or FALLBACK


def unique_path(directory: Path, name: str) -> Path:
    """Never overwrite an existing file: report.pdf becomes report (1).pdf."""
    candidate = directory / name
    stem, suffix = candidate.stem, candidate.suffix
    number = 1
    while candidate.exists():
        room = MAX_BYTES - len(f" ({number}){suffix}".encode())
        short = stem.encode()[:room].decode(errors="ignore")
        candidate = directory / f"{short} ({number}){suffix}"
        number += 1
    return candidate


def filename_from_response(url: str, headers: Mapping[str, str]) -> str:
    """Content-Disposition first (filename* wins), then the URL path."""
    name = ""
    for part in headers.get("content-disposition", "").split(";"):
        key, _, value = part.strip().partition("=")
        if key.lower() == "filename*":
            name = unquote(value.split("''")[-1].strip('"'))
            break
        if key.lower() == "filename":
            name = value.strip('"')
    return sanitize(name or unquote(urlsplit(url).path.rsplit("/", 1)[-1]))
