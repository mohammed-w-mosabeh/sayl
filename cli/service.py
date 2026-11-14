"""`sayl service`: run sayld as a systemd user unit that starts with the graphical session."""

import os
import socket
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

UNIT = "sayld.service"
MARKER = "# Written by `sayl service install`; `sayl service uninstall` removes it."
PACKAGED_UNIT = Path("/usr/lib/systemd/user") / UNIT
PACKAGED_SAYLD = "/usr/bin/sayld"
PACKAGED_RUNTIME = Path("/usr/lib/sayl/runtime")
# XAUTHORITY: X11 clients (xclip, python-xlib) need it under the unit as well.
SESSION_VARIABLES = (
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "XDG_SESSION_TYPE",
    "DBUS_SESSION_BUS_ADDRESS",
    "XAUTHORITY",
)

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def run_command(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - argument list
        list(command), capture_output=True, text=True, check=False, timeout=60
    )


def quote(argument: str) -> str:
    """One ExecStart word: systemd expands % and $, and splits on spaces unless quoted."""
    escaped = argument.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + escaped.replace("%", "%%").replace("$", "$$") + '"'


def unit_text(command: Sequence[str], marker: bool = True) -> str:
    head = f"{MARKER}\n" if marker else ""
    return f"""{head}[Unit]
Description=Sayl download daemon
# The session exports DISPLAY, WAYLAND_DISPLAY, and the bus before it reaches this target.
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=exec
ExecStart={" ".join(quote(word) for word in command)}
Restart=on-failure
RestartSec=5
# sayld stops aria2c and sayl-capture itself and restores the system proxy first.
KillMode=mixed
TimeoutStopSec=30

[Install]
WantedBy=graphical-session.target
"""


def packaged_unit_text() -> str:
    """The unit the .deb ships; packaging/systemd/sayld.service must match it."""
    return unit_text([PACKAGED_SAYLD], marker=False)


@dataclass(frozen=True)
class Target:
    command: list[str]
    packaged: bool  # The .deb's own unit already starts this sayld.


def target(env: Mapping[str, str], executable: str = sys.executable) -> Target:
    """The sayld that belongs to the sayl running this command."""
    appimage = env.get("APPIMAGE")
    if appimage:
        return Target([appimage, "sayld"], packaged=False)
    python = Path(executable)
    if python.is_relative_to(PACKAGED_RUNTIME) and PACKAGED_UNIT.exists():
        return Target([PACKAGED_SAYLD], packaged=True)
    script = python.parent / "sayld"
    if script.exists():
        return Target([str(script)], packaged=False)
    return Target([executable, "-m", "daemon.launch"], packaged=False)


def user_unit(env: Mapping[str, str]) -> Path:
    home = Path(env.get("HOME") or Path.home())
    return Path(env.get("XDG_CONFIG_HOME") or home / ".config") / "systemd" / "user" / UNIT


class ServiceError(Exception):
    pass


@dataclass
class Service:
    env: Mapping[str, str]
    runner: Runner = run_command
    executable: str = sys.executable
    # True when something already answers on sayld's port, such as a sayld started by hand.
    listening: Callable[[], bool] = lambda: False

    def _systemctl(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            done = self.runner(["systemctl", "--user", *arguments])
        except (OSError, subprocess.SubprocessError) as error:
            raise ServiceError(f"cannot run systemctl: {error}") from error
        if check and done.returncode != 0:
            message = (done.stderr or done.stdout).strip() or f"exit code {done.returncode}"
            raise ServiceError(f"systemctl --user {' '.join(arguments)}: {message}")
        return done

    def _ours(self, path: Path) -> bool:
        try:
            return path.read_text().startswith(MARKER)
        except OSError:
            return False

    def install(self, start: bool = True) -> list[str]:
        """Write (or repair) the unit, enable it, and start it. Returns what was done."""
        done: list[str] = []
        where = target(self.env, self.executable)
        path = user_unit(self.env)
        changed = False
        if where.packaged:
            if self._ours(path):  # Left by an AppImage or a checkout; the package's unit wins.
                path.unlink()
                done.append(f"removed {path}")
                changed = True
        else:
            if path.exists() and not self._ours(path):
                raise ServiceError(f"{path} was not written by Sayl; remove it first")
            text = unit_text(where.command)
            if not path.exists() or path.read_text() != text:
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix(".tmp")
                temporary.write_text(text)
                temporary.replace(path)
                done.append(f"wrote {path}")
                changed = True
        present = [name for name in SESSION_VARIABLES if self.env.get(name)]
        if present:
            self._systemctl("import-environment", *present)
        self._systemctl("daemon-reload")
        self._systemctl("enable", UNIT)
        done.append("enabled at login")
        if start and not self.active() and self.listening():
            done.append(
                "not started: a sayld outside systemd is running; the unit takes over at login"
            )
        elif start:
            verb = "restart" if changed and self.active() else "start"
            self._systemctl(verb, UNIT)
            done.append("started" if verb == "start" else "restarted")
        return done

    def uninstall(self) -> list[str]:
        done: list[str] = []
        self._systemctl("disable", "--now", UNIT, check=False)
        done.append("stopped and disabled")
        path = user_unit(self.env)
        if self._ours(path):
            path.unlink()
            done.append(f"removed {path}")
        self._systemctl("daemon-reload", check=False)
        return done

    def active(self) -> bool:
        return self._systemctl("is-active", "--quiet", UNIT, check=False).returncode == 0

    def enabled(self) -> bool:
        return self._systemctl("is-enabled", "--quiet", UNIT, check=False).returncode == 0

    def restart(self) -> None:
        self._systemctl("restart", UNIT)


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def default_service(port: int) -> Service:
    return Service(dict(os.environ), listening=lambda: port_open(port))
