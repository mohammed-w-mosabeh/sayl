"""`sayl service`: the generated unit, idempotent install, and uninstall."""

import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from cli.service import (
    MARKER,
    Service,
    ServiceError,
    packaged_unit_text,
    quote,
    target,
    unit_text,
    user_unit,
)

ROOT = Path(__file__).parent.parent


class FakeSystemctl:
    def __init__(self, active: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.is_active = active

    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        words = list(command)
        self.calls.append(words[2:])
        code = 0
        if words[2] == "is-active":
            code = 0 if self.is_active else 3
        if words[2] in ("start", "restart"):
            self.is_active = True
        return subprocess.CompletedProcess(words, code, "", "")

    def verbs(self) -> list[str]:
        return [call[0] for call in self.calls if call[0] not in ("is-active", "is-enabled")]


def make(tmp_path: Path, runner: FakeSystemctl, **env: str) -> Service:
    environment = {"HOME": str(tmp_path), **env}
    return Service(environment, runner, executable=str(tmp_path / "venv" / "bin" / "python"))


def test_quote_escapes_systemd_specials() -> None:
    assert quote("/opt/My Apps/Sayl.AppImage") == '"/opt/My Apps/Sayl.AppImage"'
    assert quote('a"b\\c%d$e') == '"a\\"b\\\\c%%d$$e"'


def test_packaged_unit_file_matches_generator() -> None:
    assert (ROOT / "packaging/systemd/sayld.service").read_text() == packaged_unit_text()


def test_targets(tmp_path: Path) -> None:
    assert target({"APPIMAGE": "/x/Sayl.AppImage"}).command == ["/x/Sayl.AppImage", "sayld"]
    venv = tmp_path / "bin"
    venv.mkdir()
    (venv / "sayld").touch()
    assert target({}, str(venv / "python")).command == [str(venv / "sayld")]
    assert target({}, str(tmp_path / "python")).command == [
        str(tmp_path / "python"),
        "-m",
        "daemon.launch",
    ]


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="needs systemd-analyze")
def test_generated_unit_verifies(tmp_path: Path) -> None:
    unit = tmp_path / "sayld.service"
    unit.write_text(unit_text([sys.executable, "-m", "daemon.launch"]))
    done = subprocess.run(  # noqa: S603 - argument list
        ["systemd-analyze", "--user", "verify", str(unit)],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    problems = [line for line in done.stderr.splitlines() if "sayld.service" in line]
    assert done.returncode == 0, done.stderr
    assert problems == []


def test_install_twice_writes_one_unit_and_imports_the_session(tmp_path: Path) -> None:
    systemctl = FakeSystemctl()
    service = make(tmp_path, systemctl, APPIMAGE="/apps/Sayl.AppImage", DISPLAY=":0")
    first = service.install()
    path = user_unit(service.env)
    assert path == tmp_path / ".config/systemd/user/sayld.service"
    assert f"wrote {path}" in first
    text = path.read_text()
    assert text.startswith(MARKER)
    assert 'ExecStart="/apps/Sayl.AppImage" "sayld"' in text
    assert ["import-environment", "DISPLAY"] in systemctl.calls
    assert systemctl.verbs() == ["import-environment", "daemon-reload", "enable", "start"]

    systemctl.calls.clear()
    second = service.install()
    assert not any(step.startswith("wrote") for step in second)
    assert systemctl.verbs() == ["import-environment", "daemon-reload", "enable", "start"]
    assert list(path.parent.iterdir()) == [path]


def test_moved_appimage_rewrites_and_restarts(tmp_path: Path) -> None:
    systemctl = FakeSystemctl()
    make(tmp_path, systemctl, APPIMAGE="/old/Sayl.AppImage").install()
    systemctl.calls.clear()
    make(tmp_path, systemctl, APPIMAGE="/new/Sayl.AppImage").install()
    assert "/new/Sayl.AppImage" in (tmp_path / ".config/systemd/user/sayld.service").read_text()
    assert systemctl.verbs()[-1] == "restart"


def test_a_running_manual_sayld_is_not_fought(tmp_path: Path) -> None:
    systemctl = FakeSystemctl()
    service = make(tmp_path, systemctl, APPIMAGE="/a/Sayl.AppImage")
    service.listening = lambda: True
    steps = service.install()
    assert "start" not in systemctl.verbs()
    assert any("takes over at login" in step for step in steps)


def test_foreign_unit_is_left_alone(tmp_path: Path) -> None:
    path = tmp_path / ".config/systemd/user/sayld.service"
    path.parent.mkdir(parents=True)
    path.write_text("[Service]\nExecStart=/my/own\n")
    with pytest.raises(ServiceError, match="not written by Sayl"):
        make(tmp_path, FakeSystemctl(), APPIMAGE="/a").install()
    make(tmp_path, FakeSystemctl()).uninstall()
    assert path.exists()


def test_uninstall_removes_only_our_unit(tmp_path: Path) -> None:
    systemctl = FakeSystemctl()
    service = make(tmp_path, systemctl, APPIMAGE="/a/Sayl.AppImage")
    service.install()
    steps = service.uninstall()
    assert not user_unit(service.env).exists()
    assert ["disable", "--now", "sayld.service"] in systemctl.calls
    assert "stopped and disabled" in steps


def test_failed_systemctl_is_reported(tmp_path: Path) -> None:
    def broken(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(list(command), 1, "", "Failed to connect to bus")

    with pytest.raises(ServiceError, match="Failed to connect to bus"):
        Service({"HOME": str(tmp_path), "APPIMAGE": "/a"}, broken).install()
