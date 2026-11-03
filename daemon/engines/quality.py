"""Detects silent quality loss: yt-dlp can exit 0 and still deliver less than was asked for."""

import json
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from daemon.engines.base import Job

_SOLVER_FAILURE = re.compile(
    r"(signature|n challenge) solving failed|challenge solver script|"
    r"no supported javascript runtime|some formats may be missing",
    re.IGNORECASE,
)
_CAP = re.compile(r"height(<=|=)(\d+)")
_CODECS = (
    ("avc", "avc1"),
    ("h264", "avc1"),
    ("hev", "hevc"),
    ("hvc", "hevc"),
    ("av01", "av1"),
    ("av1", "av1"),
    ("vp09", "vp9"),
    ("vp9", "vp9"),
    ("mp4a", "aac"),
    ("aac", "aac"),
    ("opus", "opus"),
)


@dataclass(frozen=True)
class Media:
    height: int | None = None
    vcodec: str = ""
    acodec: str = ""
    has_audio: bool | None = None


def codec_family(name: str | None) -> str:
    text = (name or "").lower()
    if text in ("", "none"):
        return ""
    return next((family for prefix, family in _CODECS if text.startswith(prefix)), text)


def is_solver_failure(message: str) -> bool:
    return _SOLVER_FAILURE.search(message) is not None


def _has_audio(entry: dict[str, Any]) -> bool:
    return codec_family(entry.get("acodec")) != ""


def _has_video(entry: dict[str, Any]) -> bool:
    return codec_family(entry.get("vcodec")) != ""


def _explicit_formats(job: Job, available: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Formats named by id, as in 399+140. A selector expression names none of them."""
    known = {str(entry.get("format_id")): entry for entry in available}
    tokens = job.format_id.split("+") if job.format_id else []
    return [known[token] for token in tokens] if all(t in known for t in tokens) else []


def requested_media(job: Job, info: dict[str, Any]) -> Media:
    available: list[dict[str, Any]] = info.get("formats") or []
    if job.audio_only:
        return Media(has_audio=True)
    named = _explicit_formats(job, available)
    if named:
        empty: dict[str, Any] = {}
        video = next((entry for entry in named if _has_video(entry)), empty)
        audio = next((entry for entry in named if _has_audio(entry)), empty)
        return Media(
            video.get("height"),
            codec_family(video.get("vcodec")),
            codec_family(audio.get("acodec")),
            bool(audio),
        )
    selector = job.format_id
    if selector.startswith(("w", "bv*[", "bv[")) and "+" not in selector:
        return Media()  # The smallest format, or video only: there is no quality to lose.
    heights = [entry["height"] for entry in available if entry.get("height")]
    best = max(heights, default=None)
    cap = _CAP.search(selector)
    if cap is None:
        height = None if selector.startswith("w") else best
    elif cap.group(1) == "=" or best is None:
        height = int(cap.group(2))
    else:
        height = min(int(cap.group(2)), best)  # A cap is not a loss when the source is smaller.
    return Media(height, has_audio=any(_has_audio(entry) for entry in available) or None)


def probe_file(path: Path) -> Media | None:
    """Read the real file. yt-dlp's metadata says what it meant to fetch, not what arrived."""
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None or not path.exists():
        return None
    try:
        found = subprocess.run(  # noqa: S603 - argument list, path from shutil.which
            [ffprobe, "-v", "error", "-show_streams", "-of", "json", str(path)],
            capture_output=True,
            check=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    streams = json.loads(found.stdout).get("streams", [])
    empty: dict[str, Any] = {}
    video = next((s for s in streams if s.get("codec_type") == "video"), empty)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), empty)
    return Media(
        video.get("height"),
        codec_family(video.get("codec_name")),
        codec_family(audio.get("codec_name")),
        bool(audio),
    )


def _from_metadata(info: dict[str, Any]) -> Media:
    return Media(
        info.get("height"),
        codec_family(info.get("vcodec")),
        codec_family(info.get("acodec")),
        _has_audio(info),
    )


def delivered_media(info: dict[str, Any]) -> Media:
    downloads = info.get("requested_downloads") or [{}]
    path = downloads[-1].get("filepath") or info.get("filepath")
    probed = probe_file(Path(path)) if path else None
    return probed or _from_metadata(info)


def compare(requested: Media, delivered: Media) -> list[str]:
    reasons = []
    if requested.height and (delivered.height or 0) < requested.height:
        reasons.append(f"height {delivered.height} is below {requested.height}")
    if requested.vcodec and delivered.vcodec != requested.vcodec:
        reasons.append(f"video codec {delivered.vcodec or 'none'} is not {requested.vcodec}")
    if requested.acodec and delivered.acodec != requested.acodec:
        reasons.append(f"audio codec {delivered.acodec or 'none'} is not {requested.acodec}")
    if requested.has_audio and not delivered.has_audio:
        reasons.append("audio is missing")
    return reasons


def assess(job: Job, info: dict[str, Any] | None, solver_failures: list[str]) -> dict[str, Any]:
    """Return the quality_degraded warning, or {} when the delivery matches the request."""
    source = info or {}
    entries = [entry for entry in source.get("entries") or [source] if entry]
    items = []
    for entry in entries:
        requested, delivered = requested_media(job, entry), delivered_media(entry)
        reasons = compare(requested, delivered)
        if reasons:
            items.append(
                {
                    "title": str(entry.get("title", "")),
                    "requested": asdict(requested),
                    "delivered": asdict(delivered),
                    "reasons": reasons,
                }
            )
    if not items and not solver_failures:
        return {}
    return {"items": items, "solver_failures": sorted(set(solver_failures))}
