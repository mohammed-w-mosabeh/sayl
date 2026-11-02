"""Environment checks behind `sayl doctor`."""

import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

Which = Callable[[str], str | None]
VersionOf = Callable[[str], str]

_VERSION_RE = re.compile(r"\d+(?:\.\d+)+")

# Runtimes yt-dlp can use for YouTube, in order of preference. Deno works out of
# the box; the others must be enabled through yt-dlp's js_runtimes option.
_JS_RUNTIMES = ("deno", "node", "bun", "qjs")

_PACKAGES: dict[str, dict[str, str]] = {
    "aria2c": {"apt": "aria2", "dnf": "aria2", "pacman": "aria2"},
    "ffmpeg": {"apt": "ffmpeg", "dnf": "ffmpeg", "pacman": "ffmpeg"},
    "certutil": {"apt": "libnss3-tools", "dnf": "nss-tools", "pacman": "nss"},
    "wl-paste": {"apt": "wl-clipboard", "dnf": "wl-clipboard", "pacman": "wl-clipboard"},
    "xclip": {"apt": "xclip", "dnf": "xclip", "pacman": "xclip"},
}
_INSTALL = {
    "apt": "sudo apt install {}",
    "dnf": "sudo dnf install {}",
    "pacman": "sudo pacman -S {}",
}
_DENO_INSTALL = "curl -fsSL https://deno.land/install.sh | sh"


class Status(Enum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str
    fix: str = ""


def package_manager(os_release: str) -> str | None:
    """Map the contents of /etc/os-release to a package manager family."""
    ids: set[str] = set()
    for line in os_release.splitlines():
        key, _, value = line.partition("=")
        if key in ("ID", "ID_LIKE"):
            ids.update(value.strip().strip('"').lower().split())
    if ids & {"debian", "ubuntu"}:
        return "apt"
    if ids & {"fedora", "rhel", "centos"}:
        return "dnf"
    if ids & {"arch"}:
        return "pacman"
    return None


def install_hint(tool: str, manager: str | None) -> str:
    """Return the exact install command for a tool, or a generic instruction."""
    if manager is None:
        return f"install {tool} with your package manager"
    return _INSTALL[manager].format(_PACKAGES[tool][manager])


def tool_version(path: str) -> str:
    """Return the first version-looking token of `<path> --version`, or ''."""
    try:
        result = subprocess.run(  # noqa: S603 - path comes from shutil.which
            [path, "--version"], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    match = _VERSION_RE.search(result.stdout + result.stderr)
    return match.group(0) if match else ""


def _check_tool(
    tool: str,
    *,
    missing: Status,
    purpose: str,
    which: Which,
    version_of: VersionOf,
    manager: str | None,
) -> Check:
    path = which(tool)
    if path is None:
        return Check(tool, missing, f"missing ({purpose})", install_hint(tool, manager))
    return Check(tool, Status.OK, version_of(path) or path)


def _check_js_runtime(which: Which, version_of: VersionOf) -> Check:
    for name in _JS_RUNTIMES:
        path = which(name)
        if path is not None:
            detail = f"{name} {version_of(path)}".strip()
            if name != "deno":
                detail += " (needs yt-dlp js_runtimes option)"
            return Check("js runtime", Status.OK, detail)
    return Check("js runtime", Status.WARN, "missing (needed for YouTube)", _DENO_INSTALL)


def _check_session(env: Mapping[str, str]) -> Check:
    session = env.get("XDG_SESSION_TYPE", "")
    if session in ("x11", "wayland"):
        return Check("session", Status.OK, session)
    return Check("session", Status.WARN, session or "unknown", "clipboard watcher may not work")


def _clipboard_tool(env: Mapping[str, str]) -> tuple[str, str]:
    session = env.get("XDG_SESSION_TYPE", "")
    desktop = env.get("XDG_CURRENT_DESKTOP", "").lower()
    if session == "wayland" and "gnome" not in desktop:
        return "wl-paste", "Wayland clipboard watcher"
    return "xclip", "X11/XWayland clipboard watcher"


def run_checks(
    *,
    which: Which = shutil.which,
    version_of: VersionOf = tool_version,
    env: Mapping[str, str] = os.environ,
    os_release_path: Path = Path("/etc/os-release"),
) -> list[Check]:
    """Run every environment check. Pure apart from the injected collaborators."""
    os_release = os_release_path.read_text() if os_release_path.exists() else ""
    manager = package_manager(os_release)
    clipboard, clipboard_purpose = _clipboard_tool(env)
    return [
        _check_tool(
            "aria2c",
            missing=Status.FAIL,
            purpose="download engine",
            which=which,
            version_of=version_of,
            manager=manager,
        ),
        _check_tool(
            "ffmpeg",
            missing=Status.FAIL,
            purpose="video merging",
            which=which,
            version_of=version_of,
            manager=manager,
        ),
        _check_js_runtime(which, version_of),
        _check_tool(
            "certutil",
            missing=Status.WARN,
            purpose="installing the capture CA",
            which=which,
            version_of=lambda _: "present",
            manager=manager,
        ),
        _check_tool(
            clipboard,
            missing=Status.WARN,
            purpose=clipboard_purpose,
            which=which,
            version_of=version_of,
            manager=manager,
        ),
        _check_session(env),
    ]


def has_failures(checks: list[Check]) -> bool:
    return any(check.status is Status.FAIL for check in checks)
