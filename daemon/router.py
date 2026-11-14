"""Picks the engine for a download that starts from the CLI, clipboard, or UI.

Captured downloads never pass through here: they are attached straight to HybridEngine.
"""

from collections.abc import Callable
from urllib.parse import urlsplit

from daemon.engines.ytdlp import supported_extractor
from daemon.models import EngineName


def route(url: str, extractor_for: Callable[[str], str] = supported_extractor) -> EngineName:
    if url.startswith("magnet:") or urlsplit(url).path.lower().endswith(".torrent"):
        return EngineName.ARIA2
    if urlsplit(url).path.lower().endswith((".m3u8", ".mpd")):
        return EngineName.YTDLP  # A stream manifest, from the video grabber: yt-dlp assembles it.
    return EngineName.YTDLP if extractor_for(url) else EngineName.ARIA2
