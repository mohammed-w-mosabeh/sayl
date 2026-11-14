import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from daemon.capture.sysproxy import (
    Desktop,
    SystemProxy,
    desktop,
    gvariant_list,
    parse_gvariant_list,
)
from tests.clipboard_env import need


def test_desktop_detection() -> None:
    def every(name: str) -> str:
        return f"/usr/bin/{name}"

    assert desktop({"XDG_CURRENT_DESKTOP": "ubuntu:GNOME"}, every) is Desktop.GNOME
    assert desktop({"XDG_CURRENT_DESKTOP": "X-Cinnamon"}, every) is Desktop.GNOME
    assert desktop({"XDG_CURRENT_DESKTOP": "KDE"}, every) is Desktop.KDE
    assert desktop({"XDG_CURRENT_DESKTOP": "sway"}, every) is Desktop.OTHER
    assert desktop({"XDG_CURRENT_DESKTOP": "GNOME"}, lambda _: None) is Desktop.OTHER


def test_gvariant_lists_round_trip() -> None:
    items = ["localhost", "it's", "a\\b"]
    assert parse_gvariant_list(gvariant_list(items)) == items
    assert parse_gvariant_list("@as []") == []
    assert parse_gvariant_list("['a', 'b']") == ["a", "b"]


def gnome_env(tmp_path: Path) -> dict[str, str]:
    """Real gsettings, writing to a private keyfile instead of the person's settings."""
    return {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_DIRS": os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share"),
        "GSETTINGS_BACKEND": "keyfile",
        "XDG_CURRENT_DESKTOP": "GNOME",
    }


def gsettings(env: Mapping[str, str], *args: str) -> str:
    return subprocess.run(  # noqa: S603
        ["gsettings", *args],  # noqa: S607
        env=dict(env),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_gnome_enable_then_restore_exactly(tmp_path: Path) -> None:
    need("gsettings")
    env = gnome_env(tmp_path)
    schemas = gsettings(env, "list-schemas").split()
    if "org.gnome.system.proxy" not in schemas:
        pytest.skip("org.gnome.system.proxy schema is not installed")
    gsettings(env, "set", "org.gnome.system.proxy", "mode", "auto")
    gsettings(env, "set", "org.gnome.system.proxy", "ignore-hosts", "['intranet.test']")
    gsettings(env, "set", "org.gnome.system.proxy.http", "host", "corp-proxy.test")
    before = {key: gsettings(env, "get", *key.split(" ")) for key in (
        "org.gnome.system.proxy mode",
        "org.gnome.system.proxy ignore-hosts",
        "org.gnome.system.proxy.http host",
        "org.gnome.system.proxy.http port",
        "org.gnome.system.proxy.https port",
    )}  # fmt: skip

    proxy = SystemProxy(tmp_path / "state", env)
    assert proxy.enable(6972).applied
    assert proxy.enable(6972).applied  # Twice: the saved values must still be the originals.
    assert gsettings(env, "get", "org.gnome.system.proxy", "mode") == "'manual'"
    assert gsettings(env, "get", "org.gnome.system.proxy.https", "port") == "6972"
    ignore = parse_gvariant_list(gsettings(env, "get", "org.gnome.system.proxy", "ignore-hosts"))
    assert ignore == ["intranet.test", "localhost", "127.0.0.0/8", "::1"]
    assert proxy.active()

    assert proxy.disable().applied
    after = {key: gsettings(env, "get", *key.split(" ")) for key in before}
    assert after == before
    assert not proxy.active()
    assert proxy.disable().detail == "nothing to restore"


class FakeKde:
    def __init__(self) -> None:
        self.config: dict[str, str] = {"ProxyType": "0"}
        self.calls: list[list[str]] = []

    def __call__(
        self, argv: Sequence[str], env: Mapping[str, str]
    ) -> subprocess.CompletedProcess[str]:
        args = list(argv)
        self.calls.append(args)
        if args[0].startswith("kwriteconfig"):
            key = args[args.index("--key") + 1]
            if args[-1] == "--delete":
                self.config.pop(key, None)
            else:
                self.config[key] = args[-1]
        if args[0].startswith("kreadconfig"):
            key = args[args.index("--key") + 1]
            return subprocess.CompletedProcess(args, 0, self.config.get(key, "") + "\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")


def test_kde_enable_then_restore(tmp_path: Path) -> None:
    kde = FakeKde()
    proxy = SystemProxy(
        tmp_path, {"XDG_CURRENT_DESKTOP": "KDE"}, kde, lambda name: f"/usr/bin/{name}"
    )
    assert proxy.enable(6972).applied
    assert kde.config["httpsProxy"] == "http://127.0.0.1 6972"
    assert kde.config["ProxyType"] == "1"
    assert json.loads(proxy.saved_file.read_text())["values"] == ["0", None, None, None]
    assert proxy.disable().applied
    assert kde.config == {"ProxyType": "0"}  # Keys that were absent are deleted again.
    assert any(call[0] == "dbus-send" for call in kde.calls)


def test_other_desktops_get_instructions(tmp_path: Path) -> None:
    result = SystemProxy(tmp_path, {"XDG_CURRENT_DESKTOP": "sway"}).enable(6972)
    assert not result.applied
    assert "https_proxy=http://127.0.0.1:6972" in result.detail
