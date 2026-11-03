"""Reading the X11 clipboard with xclip, shared by the X11 and XWayland watchers."""

from collections.abc import Mapping

from daemon.watchers.base import capture, text_type

SELECTION = ("-selection", "clipboard")


async def read_x11(env: Mapping[str, str]) -> str | None:
    targets = await capture(["xclip", "-o", *SELECTION, "-t", "TARGETS"], env)
    if targets is None:
        return None
    kind = text_type(targets.decode(errors="replace").split())
    if kind is None:
        return None
    content = await capture(["xclip", "-o", *SELECTION, "-t", kind], env)
    return None if content is None else content.decode(errors="replace")
