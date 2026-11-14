"""Private display servers and a private session bus, so tests never touch the real desktop."""

import asyncio
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from jeepney import DBusAddress, MatchRule, message_bus, new_method_return, new_signal
from jeepney.io.asyncio import Proxy, open_dbus_router

NAME = "org.freedesktop.Notifications"
PATH = "/org/freedesktop/Notifications"

# No <servicedir>: nothing can be activated, so no real notification daemon starts.
BUS_CONFIG = """<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:dir={dir}</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""


def unavailable(reason: str) -> None:
    """Skip locally; CI sets SAYL_REQUIRE_TOOLS so a missing tool fails instead."""
    if os.environ.get("SAYL_REQUIRE_TOOLS"):
        pytest.fail(reason)
    pytest.skip(reason)


def need(*tools: str) -> None:
    missing = [tool for tool in tools if shutil.which(tool) is None]
    if missing:
        unavailable(f"needs {', '.join(missing)}")


@contextmanager
def xvfb() -> Iterator[str]:
    """A private X server; yields its DISPLAY."""
    need("Xvfb", "xclip")
    read, write = os.pipe()
    process = subprocess.Popen(  # noqa: S603
        ["Xvfb", "-displayfd", str(write), "-nolisten", "tcp", "-screen", "0", "320x240x24"],  # noqa: S607
        pass_fds=(write,),
        stderr=subprocess.DEVNULL,
    )
    os.close(write)
    with os.fdopen(read) as stream:
        number = stream.readline().strip()
    try:
        yield f":{number}"
    finally:
        process.terminate()
        process.wait(10)


@contextmanager
def headless_sway() -> Iterator[dict[str, str]]:
    """A headless wlroots compositor, which has data-control. Yields its environment."""
    need("sway", "wl-paste", "wl-copy")
    runtime = Path(tempfile.mkdtemp(prefix="sayl-wl-"))
    runtime.chmod(0o700)
    config = runtime / "config"
    config.write_text("")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(runtime),
        "XDG_RUNTIME_DIR": str(runtime),
        "WLR_BACKENDS": "headless",
        "WLR_LIBINPUT_NO_DEVICES": "1",
        "WLR_RENDERER": "pixman",
    }
    process = subprocess.Popen(  # noqa: S603
        ["sway", "-c", str(config)],  # noqa: S607
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            sockets = [p.name for p in runtime.glob("wayland-*") if not p.name.endswith(".lock")]
            if sockets:
                yield {**env, "WAYLAND_DISPLAY": sockets[0], "XDG_SESSION_TYPE": "wayland"}
                return
            if process.poll() is not None:
                unavailable("sway would not start headless here")
            time.sleep(0.1)
        unavailable("sway did not create a Wayland socket")
    finally:
        process.terminate()
        process.wait(10)
        shutil.rmtree(runtime, ignore_errors=True)


@contextmanager
def headless_kwin() -> Iterator[dict[str, str]]:
    """A virtual KWin (KDE's compositor), which has data-control. Yields its environment."""
    need("kwin_wayland", "wl-paste", "wl-copy")
    runtime = Path(tempfile.mkdtemp(prefix="sayl-kwin-"))
    runtime.chmod(0o700)
    with private_bus() as bus:
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(runtime),
            "XDG_RUNTIME_DIR": str(runtime),
            "DBUS_SESSION_BUS_ADDRESS": bus,
            "KWIN_COMPOSE": "Q",  # QPainter: no GPU on a CI runner.
            "QT_QPA_PLATFORM": "offscreen",
        }
        log = runtime / "kwin.log"
        with log.open("wb") as output:
            process = subprocess.Popen(
                ["kwin_wayland", "--virtual", "--no-lockscreen", "--socket", "wayland-sayl"],  # noqa: S607
                env=env,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if (runtime / "wayland-sayl").exists():
                    time.sleep(1)  # The socket appears before KWin serves globals.
                    yield {**env, "WAYLAND_DISPLAY": "wayland-sayl", "XDG_SESSION_TYPE": "wayland"}
                    return
                if process.poll() is not None:
                    unavailable(f"kwin_wayland would not start: {log.read_text()[-2000:]}")
                time.sleep(0.1)
            unavailable(f"kwin_wayland did not create a socket: {log.read_text()[-2000:]}")
        finally:
            process.terminate()
            process.wait(10)
            shutil.rmtree(runtime, ignore_errors=True)


@contextmanager
def private_bus() -> Iterator[str]:
    """A private session bus; yields its address."""
    need("dbus-daemon")
    directory = Path(tempfile.mkdtemp(prefix="sayl-bus-"))
    config = directory / "bus.conf"
    config.write_text(BUS_CONFIG.format(dir=directory))
    process = subprocess.Popen(  # noqa: S603
        ["dbus-daemon", f"--config-file={config}", "--nofork", "--print-address"],  # noqa: S607
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        yield process.stdout.readline().strip()
    finally:
        process.terminate()
        process.wait(10)
        shutil.rmtree(directory, ignore_errors=True)


@contextmanager
def copy_x11(display: str, text: str) -> Iterator[None]:
    """Own the X clipboard with this text until the block ends."""
    process = subprocess.Popen(
        ["xclip", "-quiet", "-i", "-selection", "clipboard"],  # noqa: S607
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "DISPLAY": display},
    )
    assert process.stdin is not None
    process.stdin.write(text.encode())
    process.stdin.close()
    try:
        yield
    finally:
        process.kill()
        process.wait()


@dataclass
class FakeNotifications:
    """Stands in for the desktop's notification server, on a private bus."""

    address: str
    calls: list[tuple[int, tuple[object, ...], float]] = field(default_factory=list)
    received: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None
    router: object = None

    async def start(self) -> None:
        ready = asyncio.Event()
        self.task = asyncio.create_task(self._serve(ready))
        await asyncio.wait_for(ready.wait(), 10)

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    async def _serve(self, ready: asyncio.Event) -> None:
        async with open_dbus_router(self.address) as router:
            self.router = router
            rule = MatchRule(type="method_call", interface=NAME, path=PATH)
            with router.filter(rule, bufsize=16) as calls:
                await Proxy(message_bus, router).RequestName(NAME, 0)
                ready.set()
                while True:
                    message = await calls.get()
                    number = len(self.calls) + 1
                    self.calls.append((number, message.body, time.monotonic()))
                    await router.send(new_method_return(message, "u", (number,)))
                    self.received.set()

    async def press(self, number: int, action: str) -> None:
        source = DBusAddress(PATH, interface=NAME)
        await self.router.send(new_signal(source, "ActionInvoked", "us", (number, action)))  # type: ignore[attr-defined]
