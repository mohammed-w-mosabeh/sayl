"""Which clipboard watcher this session needs. See "Clipboard" in docs/SPEC.md."""

from collections.abc import Mapping
from enum import StrEnum


class Backend(StrEnum):
    WAYLAND = "wayland"  # wl-paste --watch, for compositors with data-control (wlroots, KDE).
    XWAYLAND_POLL = "xwayland_poll"  # GNOME on Wayland: no data-control, so poll XWayland.
    X11 = "x11"  # XFixes selection events.
    POLL = "poll"  # X11 without XFixes.
    NONE = "none"


def is_gnome(env: Mapping[str, str]) -> bool:
    desktops = env.get("XDG_CURRENT_DESKTOP", "") + ":" + env.get("XDG_SESSION_DESKTOP", "")
    return any(name.strip().lower() == "gnome" for name in desktops.replace(";", ":").split(":"))


def detect(env: Mapping[str, str]) -> Backend:
    session = env.get("XDG_SESSION_TYPE", "").lower()
    wayland = bool(env.get("WAYLAND_DISPLAY"))
    x11 = bool(env.get("DISPLAY"))
    if session == "wayland" or (session not in ("x11",) and wayland):
        if is_gnome(env):
            return Backend.XWAYLAND_POLL if x11 else Backend.NONE
        if wayland:
            return Backend.WAYLAND
        return Backend.POLL if x11 else Backend.NONE
    if x11:
        return Backend.X11
    return Backend.NONE
