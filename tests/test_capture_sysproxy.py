import json
import os
import subprocess
import sys
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

ROOT = Path(__file__).resolve().parent.parent


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


PAC = "http://127.0.0.1:6971/capture/pac/secret.pac"
EVERY_KEY = (
    "org.gnome.system.proxy mode",
    "org.gnome.system.proxy autoconfig-url",
    "org.gnome.system.proxy ignore-hosts",
    "org.gnome.system.proxy.http host",
    "org.gnome.system.proxy.http port",
    "org.gnome.system.proxy.https host",
    "org.gnome.system.proxy.https port",
)


def gnome_or_skip(tmp_path: Path) -> dict[str, str]:
    need("gsettings")
    env = gnome_env(tmp_path)
    if "org.gnome.system.proxy" not in gsettings(env, "list-schemas").split():
        pytest.skip("org.gnome.system.proxy schema is not installed")
    return env


def snapshot(env: Mapping[str, str]) -> dict[str, str]:
    return {key: gsettings(env, "get", *key.split(" ")) for key in EVERY_KEY}


def test_gnome_uses_pac_mode_then_restores_exactly(tmp_path: Path) -> None:
    """Never manual mode: gnome-terminal exports manual mode as https_proxy to every shell."""
    env = gnome_or_skip(tmp_path)
    gsettings(env, "set", "org.gnome.system.proxy", "mode", "manual")
    gsettings(env, "set", "org.gnome.system.proxy", "ignore-hosts", "['intranet.test']")
    gsettings(env, "set", "org.gnome.system.proxy.http", "host", "corp-proxy.test")
    before = snapshot(env)

    proxy = SystemProxy(tmp_path / "state", env)
    assert proxy.enable(6972, PAC).applied
    assert proxy.enable(6972, PAC).applied  # Twice: the saved values must still be the originals.
    assert gsettings(env, "get", "org.gnome.system.proxy", "mode") == "'auto'"
    assert gsettings(env, "get", "org.gnome.system.proxy", "autoconfig-url") == f"'{PAC}'"
    assert proxy.gnome_mode() == ("auto", PAC)
    # The manual-mode proxy is not replaced by ours, so nothing can export 127.0.0.1:6972.
    assert gsettings(env, "get", "org.gnome.system.proxy.http", "host") == "'corp-proxy.test'"
    assert "6972" not in str(snapshot(env))
    assert proxy.active()
    assert proxy.saved_file.stat().st_mode & 0o777 == 0o600

    assert proxy.disable().applied
    assert snapshot(env) == before
    assert not proxy.active()
    assert proxy.disable().detail == "nothing to restore"


def test_legacy_manual_mode_state_restores(tmp_path: Path) -> None:
    """A file saved by Sayl 1.0 (no key names, manual mode) still puts everything back."""
    env = gnome_or_skip(tmp_path)
    before = snapshot(env)
    for schema in ("org.gnome.system.proxy.http", "org.gnome.system.proxy.https"):
        gsettings(env, "set", schema, "host", "127.0.0.1")
        gsettings(env, "set", schema, "port", "6972")
    gsettings(env, "set", "org.gnome.system.proxy", "mode", "manual")
    state = tmp_path / "state"
    state.mkdir()
    legacy = [gsettings(env, "get", *key.split(" ")) for key in (
        "org.gnome.system.proxy mode", "org.gnome.system.proxy ignore-hosts",
        "org.gnome.system.proxy use-same-proxy", "org.gnome.system.proxy.http host",
        "org.gnome.system.proxy.http port", "org.gnome.system.proxy.https host",
        "org.gnome.system.proxy.https port",
    )]  # fmt: skip
    legacy = [before["org.gnome.system.proxy mode"], *legacy[1:]]
    legacy[3:7] = ["''", "8080", "''", "0"]
    (state / "proxy-saved.json").write_text(json.dumps({"desktop": "gnome", "values": legacy}))

    assert SystemProxy(state, env).disable().applied
    assert snapshot(env) == before


def test_restore_command_puts_back_what_a_killed_sayld_left(tmp_path: Path) -> None:
    env = gnome_or_skip(tmp_path)
    before = snapshot(env)
    data = tmp_path / "data"
    assert SystemProxy(data / "sayl" / "capture", env).enable(6972, PAC).applied
    done = subprocess.run(
        [sys.executable, "-m", "daemon.launch", "--restore-proxy"],
        env={**env, "XDG_DATA_HOME": str(data), "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "restored"
    assert snapshot(env) == before


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
    assert proxy.enable(6972, PAC).applied
    assert kde.config["httpsProxy"] == "http://127.0.0.1 6972"
    assert kde.config["ProxyType"] == "1"
    assert json.loads(proxy.saved_file.read_text())["values"] == ["0", None, None, None]
    assert proxy.disable().applied
    assert kde.config == {"ProxyType": "0"}  # Keys that were absent are deleted again.
    assert any(call[0] == "dbus-send" for call in kde.calls)


def test_other_desktops_get_instructions(tmp_path: Path) -> None:
    result = SystemProxy(tmp_path, {"XDG_CURRENT_DESKTOP": "sway"}).enable(6972, PAC)
    assert not result.applied
    assert PAC in result.detail
    assert "https_proxy=" not in result.detail  # Never advise routing CLI tools through us.
