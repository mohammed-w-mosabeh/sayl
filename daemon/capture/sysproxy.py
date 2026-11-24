"""Point the desktop's system proxy at sayl-capture, and put it back exactly as it was.

GNOME-family desktops: `gsettings org.gnome.system.proxy` in `auto` mode with sayld's PAC file.
Never `manual` mode: gnome-terminal turns manual mode into http_proxy/https_proxy variables for
every shell, which sends curl, git, package managers and API clients through a TLS-intercepting
proxy they do not trust. In auto mode terminals export nothing, browsers follow the PAC file,
and the PAC file falls back to DIRECT whenever sayl-capture is not answering.
KDE: `kioslaverc` via kwriteconfig (Konsole exports no proxy variables).
Anything else: how to point a browser at the PAC file, printed for the person to apply.
Previous values are saved as the tools print them, with their key names, and written back
unchanged. `python -m daemon.capture.sysproxy restore` does that from outside sayld.
"""

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

SAVED_FILE = "proxy-saved.json"

GNOME_KEYS = (
    ("org.gnome.system.proxy", "mode"),
    ("org.gnome.system.proxy", "autoconfig-url"),
)
# What Sayl 1.0 saved, in this order, before it stored key names: still restorable.
LEGACY_GNOME_KEYS = (
    ("org.gnome.system.proxy", "mode"),
    ("org.gnome.system.proxy", "ignore-hosts"),
    ("org.gnome.system.proxy", "use-same-proxy"),
    ("org.gnome.system.proxy.http", "host"),
    ("org.gnome.system.proxy.http", "port"),
    ("org.gnome.system.proxy.https", "host"),
    ("org.gnome.system.proxy.https", "port"),
)
KDE_GROUP = "Proxy Settings"
KDE_KEYS = ("ProxyType", "httpProxy", "httpsProxy", "NoProxyFor")
GNOME_DESKTOPS = frozenset({"gnome", "unity", "budgie", "cinnamon", "pantheon", "x-cinnamon"})

Runner = Callable[[Sequence[str], Mapping[str, str]], subprocess.CompletedProcess[str]]


class Desktop(StrEnum):
    GNOME = "gnome"
    KDE = "kde"
    OTHER = "other"


def run(argv: Sequence[str], env: Mapping[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - argument list
        list(argv), capture_output=True, text=True, timeout=15, check=False, env=dict(env)
    )


def desktop(env: Mapping[str, str], which: Callable[[str], str | None] = shutil.which) -> Desktop:
    names = {
        name.strip().lower()
        for name in env.get("XDG_CURRENT_DESKTOP", "").replace(";", ":").split(":")
    }
    if "kde" in names and (which("kwriteconfig6") or which("kwriteconfig5")):
        return Desktop.KDE
    if names & GNOME_DESKTOPS and which("gsettings"):
        return Desktop.GNOME
    return Desktop.OTHER


def gvariant_list(items: Sequence[str]) -> str:
    """A GVariant string array: ['a', 'b']. Values are hosts, quoted and escaped."""
    quoted = ("'" + item.replace("\\", "\\\\").replace("'", "\\'") + "'" for item in items)
    return "[" + ", ".join(quoted) + "]"


def parse_gvariant_list(text: str) -> list[str]:
    body = text.strip().removeprefix("@as").strip()
    if not body.startswith("[") or body == "[]":
        return []
    items = []
    for part in body[1:-1].split(","):
        value = part.strip()
        if len(value) >= 2 and value[0] == value[-1] == "'":
            items.append(value[1:-1].replace("\\'", "'").replace("\\\\", "\\"))
    return items


@dataclass
class Result:
    desktop: Desktop
    applied: bool
    detail: str = ""


class SystemProxy:
    def __init__(
        self,
        state_dir: Path,
        env: Mapping[str, str],
        runner: Runner = run,
        which: Callable[[str], str | None] = shutil.which,
    ) -> None:
        self.state_dir = state_dir
        self.env = dict(env)
        self.runner = runner
        self.which = which

    @property
    def saved_file(self) -> Path:
        return self.state_dir / SAVED_FILE

    def _call(self, *argv: str) -> subprocess.CompletedProcess[str]:
        return self.runner(argv, self.env)

    def _kde_tools(self) -> tuple[str, str]:
        version = "6" if self.which("kwriteconfig6") else "5"
        return f"kwriteconfig{version}", f"kreadconfig{version}"

    def enable(self, port: int, pac_url: str) -> Result:
        """Save the current settings once, then point browsers at the proxy."""
        kind = desktop(self.env, self.which)
        if kind is Desktop.OTHER:
            return Result(
                kind,
                False,
                f"Set your browser's automatic proxy configuration URL to {pac_url} "
                f"(or its proxy to 127.0.0.1:{port}). Do not export https_proxy in your session: "
                "command-line tools do not trust Sayl's CA.",
            )
        if not self.saved_file.exists():  # A second enable must not save our own values.
            self._save(kind)
        try:
            if kind is Desktop.GNOME:
                self._gnome_enable(pac_url)
            else:
                self._kde_enable(port)
        except OSError as error:
            return Result(kind, False, str(error))
        return Result(kind, True)

    def disable(self) -> Result:
        """Write back exactly what was there before enable. A no-op if nothing was saved."""
        kind = desktop(self.env, self.which)
        if not self.saved_file.exists():
            return Result(kind, False, "nothing to restore")
        saved = json.loads(self.saved_file.read_text())
        try:
            if saved["desktop"] == Desktop.GNOME:
                keys = [tuple(pair) for pair in saved.get("keys", LEGACY_GNOME_KEYS)]
                for (schema, key), value in zip(keys, saved["values"], strict=True):
                    self._check(self._call("gsettings", "set", schema, key, value))
            else:
                write, _ = self._kde_tools()
                for key, value in zip(KDE_KEYS, saved["values"], strict=True):
                    args = ["--file", "kioslaverc", "--group", KDE_GROUP, "--key", key]
                    if value is None:
                        self._check(self._call(write, *args, "--delete"))
                    else:
                        self._check(self._call(write, *args, value))
                self._kde_reload()
        except OSError as error:
            return Result(kind, False, str(error))
        self.saved_file.unlink()
        return Result(Desktop(saved["desktop"]), True)

    def active(self) -> bool:
        return self.saved_file.exists()

    def _check(self, result: subprocess.CompletedProcess[str]) -> str:
        if result.returncode != 0:
            raise OSError(result.stderr.strip() or f"exit {result.returncode}")
        return result.stdout.strip()

    def _save(self, kind: Desktop) -> None:
        if kind is Desktop.GNOME:
            values: list[str | None] = [
                self._check(self._call("gsettings", "get", schema, key))
                for schema, key in GNOME_KEYS
            ]
        else:
            _, read = self._kde_tools()
            values = []
            for key in KDE_KEYS:
                args = ["--file", "kioslaverc", "--group", KDE_GROUP, "--key", key]
                result = self._call(read, *args)
                values.append(result.stdout.strip() if result.stdout.strip() else None)
        keys = GNOME_KEYS if kind is Desktop.GNOME else KDE_KEYS
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        content = json.dumps({"desktop": kind, "keys": keys, "values": values}).encode()
        descriptor = os.open(self.saved_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)

    def _gnome_enable(self, pac_url: str) -> None:
        """Auto mode with our PAC file. The manual-mode keys stay as they were."""
        schema = "org.gnome.system.proxy"
        self._check(self._call("gsettings", "set", schema, "autoconfig-url", pac_url))
        self._check(self._call("gsettings", "set", schema, "mode", "auto"))

    def gnome_mode(self) -> tuple[str, str] | None:
        """The live GNOME proxy mode and PAC URL, for `sayl doctor`. None off GNOME."""
        if desktop(self.env, self.which) is not Desktop.GNOME:
            return None
        try:
            mode = self._check(self._call("gsettings", "get", "org.gnome.system.proxy", "mode"))
            url = self._check(
                self._call("gsettings", "get", "org.gnome.system.proxy", "autoconfig-url")
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return mode.strip("'"), url.strip("'")

    def _kde_enable(self, port: int) -> None:
        write, _ = self._kde_tools()
        values = {
            "ProxyType": "1",
            "httpProxy": f"http://127.0.0.1 {port}",
            "httpsProxy": f"http://127.0.0.1 {port}",
            "NoProxyFor": "localhost,127.0.0.0/8,::1",
        }
        for key, value in values.items():
            args = ["--file", "kioslaverc", "--group", KDE_GROUP, "--key", key, value]
            self._check(self._call(write, *args))
        self._kde_reload()

    def _kde_reload(self) -> None:
        """Ask running KDE apps to reread kioslaverc. Best effort: new apps read it anyway."""
        if self.which("dbus-send"):
            self._call(
                "dbus-send", "--type=signal", "/KIO/Scheduler",
                "org.kde.KIO.Scheduler.reparseSlaveConfiguration", "string:",
            )  # fmt: skip


def main(argv: Sequence[str] = sys.argv[1:]) -> int:
    """`restore`: put back what a crashed sayld left. systemd runs it after sayld stops."""
    from daemon.config import Paths

    if list(argv) != ["restore"]:
        sys.stderr.write("usage: python -m daemon.capture.sysproxy restore\n")
        return 2
    result = SystemProxy(Paths.from_env().data / "capture", os.environ).disable()
    sys.stdout.write(("restored" if result.applied else result.detail) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
