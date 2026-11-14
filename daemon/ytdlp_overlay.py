"""Put a staged yt-dlp in front of the bundled one, before anything imports `yt_dlp`.

The updater (daemon/ytdlp_update.py) stages verified wheels in `<data>/yt-dlp/<version>/` and
names the one to use in `<data>/yt-dlp/current`. This module only reads that; it must not
import yt-dlp, httpx, or anything that does, because it runs first in sayld.
"""

import importlib
import importlib.metadata
import json
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

MODULES = ("yt_dlp", "yt_dlp_ejs")


@dataclass
class Overlay:
    bundled: str = ""
    active: str = ""
    staged: str = ""  # The version `current` names, active or waiting for a restart.
    path: str = ""  # The staged directory in use, "" when the bundled copy is active.
    error: str = ""  # Why a staged copy was dropped.


STATE = Overlay()


def version_key(version: str) -> tuple[int, ...] | None:
    """yt-dlp versions are dates (2026.10.26, nightlies add .HHMMSS). None if not numeric."""
    if not re.fullmatch(r"\d+(\.\d+)*", version):
        return None
    return tuple(int(part) for part in version.split("."))


def newer(candidate: str, than: str) -> bool:
    new, old = version_key(candidate), version_key(than)
    if new is None:
        return False
    return old is None or new > old


def root(data: Path) -> Path:
    return data / "yt-dlp"


def read_current(data: Path) -> dict[str, str]:
    """The staged entry, or {} when there is none or the file is not one we wrote."""
    try:
        value = json.loads((root(data) / "current").read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(value, dict) or not isinstance(value.get("version"), str):
        return {}
    return {key: str(item) for key, item in value.items()}


def bundled_version() -> str:
    try:
        return importlib.metadata.version("yt-dlp")
    except importlib.metadata.PackageNotFoundError:
        return ""


def _purge() -> None:
    for name in list(sys.modules):
        if name.split(".")[0] in MODULES:
            del sys.modules[name]
    importlib.invalidate_caches()


def activate(data: Path) -> Overlay:
    """Use the staged yt-dlp if it is newer and imports; otherwise the bundled one."""
    state = Overlay(bundled=bundled_version())
    current = read_current(data)
    state.staged = current.get("version", "")
    folder = root(data) / state.staged if state.staged else None
    if folder is not None and folder.is_dir() and newer(state.staged, state.bundled):
        sys.path.insert(0, str(folder))
        try:
            module = importlib.import_module("yt_dlp.version")
            found = str(getattr(module, "__version__", ""))
            if found != state.staged:
                raise ImportError(f"it reports version {found or 'none'}")
        except Exception as error:
            sys.path.remove(str(folder))
            _purge()
            state.error = f"staged yt-dlp {state.staged} ignored: {error}"
            log.warning("%s", state.error)
        else:
            state.path = str(folder)
    state.active = state.staged if state.path else state.bundled
    STATE.__dict__.update(state.__dict__)
    return state
