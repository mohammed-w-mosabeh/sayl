import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from daemon.capture import ca
from daemon.capture.trust import Outcome, Trust, firefox_profiles, nss_databases
from tests.clipboard_env import need


def done(code: int = 0, out: str = "", err: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, out, err)


class FakeCertutil:
    """certutil and pkexec in memory: databases are sets of nicknames."""

    def __init__(self, pkexec_code: int = 0) -> None:
        self.databases: dict[str, list[str]] = {}
        self.calls: list[list[str]] = []
        self.pkexec_code = pkexec_code

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        args = list(argv)
        self.calls.append(args)
        if args[0] == "pkexec":
            return done(self.pkexec_code, err="" if self.pkexec_code == 0 else "Not authorized")
        flag, database = args[1], args[3].removeprefix("sql:")
        names = self.databases.setdefault(database, [])
        if flag == "-N":
            Path(database, "cert9.db").touch()
            return done()
        nickname = args[args.index("-n") + 1]
        if flag == "-L":
            return done(0 if nickname in names else 255)
        if flag == "-A":
            names.append(nickname)
            return done()
        if flag == "-D" and nickname in names:
            names.remove(nickname)
            return done()
        return done(255, err="not found")


def which_all(name: str) -> str | None:
    return f"/usr/bin/{name}"


def profile(home: Path, root: str, name: str) -> Path:
    directory = home / root / name
    directory.mkdir(parents=True)
    (directory / "cert9.db").touch()
    return directory


def test_finds_every_browser_store(tmp_path: Path) -> None:
    profile(tmp_path, ".mozilla/firefox", "a.default")
    profile(tmp_path, "snap/firefox/common/.mozilla/firefox", "b.default")
    (tmp_path / ".mozilla/firefox/no-db").mkdir()
    profile(tmp_path, "snap/chromium/current/.pki", "nssdb")
    assert [p.name for p in firefox_profiles(tmp_path)] == ["a.default", "b.default"]
    # The main Chrome database comes first even before Chrome created it.
    assert nss_databases(tmp_path) == [
        tmp_path / ".pki/nssdb",
        tmp_path / "snap/chromium/current/.pki/nssdb",
    ]


def test_install_then_uninstall_with_a_fake_certutil(tmp_path: Path) -> None:
    authority = ca.create(tmp_path / "ca")
    home = tmp_path / "home"
    firefox = profile(home, ".mozilla/firefox", "x.default")
    certutil = FakeCertutil()
    trust = Trust(home, certutil, which_all)

    report = trust.install(authority, system=False)
    assert report.ok
    assert [(s.store, s.outcome) for s in report.steps] == [
        ("chrome", Outcome.INSTALLED),
        ("firefox", Outcome.INSTALLED),
    ]
    assert certutil.databases[str(firefox)] == [authority.name]
    added = next(call for call in certutil.calls if call[1] == "-A")
    assert added[added.index("-t") + 1] == "C,,"  # Server certificates only.
    assert trust.install(authority, system=False).steps[0].outcome is Outcome.PRESENT
    assert len(trust.status(authority)["stores"]) == 2  # type: ignore[arg-type]

    certutil.databases[str(firefox)].append(authority.name)  # Somehow added twice.
    report = trust.uninstall(authority, names=["Sayl local CA old"])
    assert report.ok
    assert all(not names for names in certutil.databases.values())
    assert not (authority.directory / "installed.json").exists()


def test_system_store_uses_one_pkexec_prompt(tmp_path: Path) -> None:
    authority = ca.create(tmp_path / "ca")
    certutil = FakeCertutil()
    trust = Trust(tmp_path / "home", certutil, which_all)
    step = trust.install(authority).steps[-1]
    assert (step.store, step.outcome) == ("system", Outcome.INSTALLED)
    pkexec = [call for call in certutil.calls if call[0] == "pkexec"]
    assert len(pkexec) == 1
    argv = pkexec[0]
    assert argv[:3] == ["pkexec", "/bin/sh", "-c"]
    assert str(authority.cert_file) not in argv[3]  # Paths are arguments, not script text.
    assert argv[5] == str(authority.cert_file)
    assert argv[6].endswith(f"sayl-{authority.fingerprint[:16]}.crt")


def test_cancelled_prompt_and_missing_tools_are_reported(tmp_path: Path) -> None:
    authority = ca.create(tmp_path / "ca")
    cancelled = Trust(tmp_path / "home", FakeCertutil(pkexec_code=126), which_all)
    report = cancelled.install(authority)
    assert not report.ok
    assert report.steps[-1].detail == "Not authorized"

    nothing = Trust(tmp_path / "home", FakeCertutil(), lambda name: None)
    steps = nothing.install(authority).steps
    assert steps[0].detail == "certutil is missing: install libnss3-tools"
    assert steps[-1].outcome is Outcome.SKIPPED  # No system store tool either.


def test_real_certutil_round_trip(tmp_path: Path) -> None:
    need("certutil")
    authority = ca.create(tmp_path / "ca")
    home = tmp_path / "home"
    firefox = home / ".mozilla/firefox/real.default"
    firefox.mkdir(parents=True)
    create = ["certutil", "-N", "-d", f"sql:{firefox}", "--empty-password"]
    subprocess.run(create, check=True)  # noqa: S603
    trust = Trust(home, which=lambda name: None if name == "pkexec" else f"/usr/bin/{name}")

    assert trust.install(authority, system=False).ok
    for database in (home / ".pki/nssdb", firefox):
        listed = subprocess.run(  # noqa: S603
            ["certutil", "-L", "-d", f"sql:{database}"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        line = next(line for line in listed.splitlines() if line.startswith(authority.name))
        assert line.split()[-1] == "C,,"

    report = trust.uninstall(authority)
    assert [s.outcome for s in report.steps if s.store != "system"] == [Outcome.REMOVED] * 2
    assert (
        authority.name
        not in subprocess.run(  # noqa: S603
            ["certutil", "-L", "-d", f"sql:{firefox}"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )


@pytest.fixture(autouse=True)
def _no_real_system_anchor(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point every system store at a temporary directory: tests never see /usr or /etc."""
    from daemon.capture import trust

    stores = tuple(
        trust.SystemStore(str(tmp_path / "anchors" / str(n)), store.update)
        for n, store in enumerate(trust.SYSTEM_STORES)
    )
    (tmp_path / "anchors").mkdir()
    monkeypatch.setattr(trust, "SYSTEM_STORES", stores)
