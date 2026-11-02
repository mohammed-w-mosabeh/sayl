import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cli import doctor
from cli.doctor import Status, has_failures, install_hint, package_manager, run_checks, tool_version
from cli.main import app

ALL = {"aria2c", "ffmpeg", "deno", "certutil", "wl-paste", "xclip"}


def which_from(available: set[str]) -> doctor.Which:
    return lambda name: f"/usr/bin/{name}" if name in available else None


def checks(available: set[str], env: dict[str, str], tmp_path: Path) -> dict[str, doctor.Check]:
    release = tmp_path / "os-release"
    release.write_text('ID=debian\nVERSION_ID="13"\n')
    result = run_checks(
        which=which_from(available),
        version_of=lambda _: "1.2.3",
        env=env,
        os_release_path=release,
    )
    return {check.name: check for check in result}


@pytest.mark.parametrize(
    ("release", "expected"),
    [
        ("ID=debian\n", "apt"),
        ('ID=linuxmint\nID_LIKE="ubuntu debian"\n', "apt"),
        ("ID=fedora\n", "dnf"),
        ('ID=rocky\nID_LIKE="rhel centos fedora"\n', "dnf"),
        ("ID=manjaro\nID_LIKE=arch\n", "pacman"),
        ("ID=void\n", None),
        ("", None),
    ],
)
def test_package_manager(release: str, expected: str | None) -> None:
    assert package_manager(release) == expected


def test_install_hint() -> None:
    assert install_hint("certutil", "apt") == "sudo apt install libnss3-tools"
    assert install_hint("certutil", "dnf") == "sudo dnf install nss-tools"
    assert "aria2c" in install_hint("aria2c", None)


def test_all_present_on_x11(tmp_path: Path) -> None:
    found = checks(ALL, {"XDG_SESSION_TYPE": "x11"}, tmp_path)
    assert {c.status for c in found.values()} == {Status.OK}
    assert "xclip" in found
    assert found["aria2c"].detail == "1.2.3"


def test_missing_required_tool_fails(tmp_path: Path) -> None:
    found = checks(ALL - {"aria2c"}, {"XDG_SESSION_TYPE": "x11"}, tmp_path)
    assert found["aria2c"].status is Status.FAIL
    assert found["aria2c"].fix == "sudo apt install aria2"
    assert has_failures(list(found.values()))


def test_missing_optional_tool_only_warns(tmp_path: Path) -> None:
    found = checks(ALL - {"certutil", "deno"}, {"XDG_SESSION_TYPE": "x11"}, tmp_path)
    assert found["certutil"].status is Status.WARN
    assert found["js runtime"].status is Status.WARN
    assert not has_failures(list(found.values()))


def test_node_runtime_notes_option(tmp_path: Path) -> None:
    found = checks((ALL - {"deno"}) | {"node"}, {"XDG_SESSION_TYPE": "x11"}, tmp_path)
    assert found["js runtime"].status is Status.OK
    assert "js_runtimes" in found["js runtime"].detail


def test_gnome_wayland_uses_xclip(tmp_path: Path) -> None:
    env = {"XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "ubuntu:GNOME"}
    assert "xclip" in checks(ALL, env, tmp_path)


def test_kde_wayland_uses_wl_paste(tmp_path: Path) -> None:
    env = {"XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "KDE"}
    assert "wl-paste" in checks(ALL, env, tmp_path)


def test_unknown_session_warns(tmp_path: Path) -> None:
    assert checks(ALL, {}, tmp_path)["session"].status is Status.WARN


def test_tool_version_parses_output() -> None:
    assert tool_version(sys.executable).startswith("3.")


def test_tool_version_handles_missing_binary() -> None:
    assert tool_version("/nonexistent/tool") == ""


def test_cli_exit_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    ok = doctor.Check("aria2c", Status.OK, "1.2.3")
    monkeypatch.setattr("cli.main.run_checks", lambda: [ok])
    assert runner.invoke(app, ["doctor"]).exit_code == 0
    bad = doctor.Check("aria2c", Status.FAIL, "missing", "sudo apt install aria2")
    monkeypatch.setattr("cli.main.run_checks", lambda: [bad])
    assert runner.invoke(app, ["doctor"]).exit_code == 1
