"""Installing the CA into trust stores, and taking it out again.

Stores: the system store (via pkexec, one prompt), the shared NSS database Chrome and Chromium
read (`~/.pki/nssdb`), and every Firefox profile. Snap and Flatpak builds keep their own copies,
which are found too. What was installed is recorded so uninstall can remove exactly that, and
uninstall also removes the CA by name from every store it can find.
"""

import json
import shutil
import subprocess
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path

from daemon.capture.ca import Authority

STATE_FILE = "installed.json"
NSS_TRUST = "C,,"  # Trusted to issue server certificates, nothing else.
TIMEOUT = 120  # pkexec waits for a person to type a password.

NSS_DATABASES = (
    ".pki/nssdb",
    "snap/chromium/current/.pki/nssdb",
    ".var/app/com.google.Chrome/.pki/nssdb",
    ".var/app/org.chromium.Chromium/.pki/nssdb",
)
FIREFOX_ROOTS = (
    ".mozilla/firefox",
    "snap/firefox/common/.mozilla/firefox",
    ".var/app/org.mozilla.firefox/.mozilla/firefox",
    ".librewolf",
    ".var/app/io.gitlab.librewolf-community/.librewolf",
)


@dataclass(frozen=True)
class SystemStore:
    """Where a distribution keeps extra roots, and the command that rebuilds its bundle."""

    anchor_dir: str
    update: str


SYSTEM_STORES = (
    SystemStore("/usr/local/share/ca-certificates", "update-ca-certificates"),  # Debian, Ubuntu
    SystemStore("/etc/pki/ca-trust/source/anchors", "update-ca-trust"),  # Fedora, RHEL
    SystemStore("/etc/ca-certificates/trust-source/anchors", "update-ca-trust"),  # Arch
)


class Outcome(StrEnum):
    INSTALLED = "installed"
    REMOVED = "removed"
    PRESENT = "present"
    ABSENT = "absent"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class Step:
    store: str
    target: str
    outcome: Outcome
    detail: str = ""


@dataclass
class Report:
    steps: list[Step] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(step.outcome is not Outcome.FAILED for step in self.steps)

    def add(self, store: str, target: str, outcome: Outcome, detail: str = "") -> None:
        self.steps.append(Step(store, target, outcome, detail))

    def as_dict(self) -> dict[str, object]:
        return {"ok": self.ok, "steps": [asdict(step) for step in self.steps]}


Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
Which = Callable[[str], str | None]


def run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - argument list, never a shell string
        list(argv), capture_output=True, text=True, timeout=TIMEOUT, check=False
    )


def nss_databases(home: Path) -> list[Path]:
    """Chrome-family databases that exist. The main one is created on install if missing."""
    found = [home / path for path in NSS_DATABASES if (home / path / "cert9.db").exists()]
    main = home / NSS_DATABASES[0]
    return found if main in found else [main, *found]


def firefox_profiles(home: Path) -> list[Path]:
    profiles: list[Path] = []
    for root in FIREFOX_ROOTS:
        profiles.extend(sorted(db.parent for db in (home / root).glob("*/cert9.db")))
    return profiles


def system_store(which: Which = shutil.which) -> SystemStore | None:
    for store in SYSTEM_STORES:
        if Path(store.anchor_dir).parent.is_dir() and which(store.update):
            return store
    return None


def _anchor_name(authority: Authority) -> str:
    return f"sayl-{authority.fingerprint[:16]}.crt"


class Trust:
    def __init__(self, home: Path, runner: Runner = run, which: Which = shutil.which) -> None:
        self.home = home
        self.runner = runner
        self.which = which

    # NSS (Chrome family and Firefox)

    def _certutil(self, *args: str) -> subprocess.CompletedProcess[str]:
        return self.runner(["certutil", *args])

    def _has(self, database: Path, nickname: str) -> bool:
        return self._certutil("-L", "-d", f"sql:{database}", "-n", nickname).returncode == 0

    def _nss_install(
        self, report: Report, store: str, database: Path, authority: Authority
    ) -> None:
        if not (database / "cert9.db").exists():
            database.mkdir(parents=True, exist_ok=True, mode=0o700)
            created = self._certutil("-N", "-d", f"sql:{database}", "--empty-password")
            if created.returncode != 0:
                report.add(store, str(database), Outcome.FAILED, created.stderr.strip())
                return
        if self._has(database, authority.name):
            report.add(store, str(database), Outcome.PRESENT)
            return
        added = self._certutil(
            "-A", "-d", f"sql:{database}", "-n", authority.name, "-t", NSS_TRUST,
            "-i", str(authority.cert_file),
        )  # fmt: skip
        outcome = Outcome.INSTALLED if added.returncode == 0 else Outcome.FAILED
        report.add(store, str(database), outcome, added.stderr.strip())

    def _nss_remove(self, report: Report, store: str, database: Path, nickname: str) -> None:
        if not (database / "cert9.db").exists() or not self._has(database, nickname):
            report.add(store, str(database), Outcome.ABSENT)
            return
        # A name can be present more than once if it was added twice; remove every copy.
        removed = self._certutil("-D", "-d", f"sql:{database}", "-n", nickname)
        while removed.returncode == 0 and self._has(database, nickname):
            removed = self._certutil("-D", "-d", f"sql:{database}", "-n", nickname)
        outcome = Outcome.REMOVED if removed.returncode == 0 else Outcome.FAILED
        report.add(store, str(database), outcome, removed.stderr.strip())

    # System store

    def _system(self, report: Report, authority: Authority, install: bool) -> None:
        store = system_store(self.which)
        if store is None:
            report.add("system", "", Outcome.SKIPPED, "no supported system trust store")
            return
        anchor = Path(store.anchor_dir) / _anchor_name(authority)
        present = anchor.exists()
        if install == present:
            report.add("system", str(anchor), Outcome.PRESENT if present else Outcome.ABSENT)
            return
        if self.which("pkexec") is None:
            report.add("system", str(anchor), Outcome.FAILED, "pkexec is not installed")
            return
        # One prompt for both steps. Paths are positional arguments, never part of the script.
        script = 'install -D -m 0644 "$1" "$2" && "$3"' if install else 'rm -f "$2" && "$3"'
        result = self.runner(
            ["pkexec", "/bin/sh", "-c", script, "sayl", str(authority.cert_file), str(anchor),
             store.update]
        )  # fmt: skip
        if result.returncode == 0:
            report.add("system", str(anchor), Outcome.INSTALLED if install else Outcome.REMOVED)
        else:
            detail = result.stderr.strip() or f"exit {result.returncode} (cancelled?)"
            report.add("system", str(anchor), Outcome.FAILED, detail)

    # Public

    def install(self, authority: Authority, *, system: bool = True) -> Report:
        report = Report()
        if self.which("certutil") is None:
            report.add("nss", "", Outcome.FAILED, "certutil is missing: install libnss3-tools")
        else:
            for database in nss_databases(self.home):
                self._nss_install(report, "chrome", database, authority)
            for profile in firefox_profiles(self.home):
                self._nss_install(report, "firefox", profile, authority)
        if system:
            self._system(report, authority, install=True)
        self._record(authority, report)
        return report

    def uninstall(self, authority: Authority, names: Iterable[str] = ()) -> Report:
        """Remove this CA, and any older Sayl CA named in `names`, from every store found."""
        report = Report()
        nicknames = list(dict.fromkeys([authority.name, *names]))
        if self.which("certutil") is not None:
            databases = [("chrome", db) for db in nss_databases(self.home)]
            databases += [("firefox", profile) for profile in firefox_profiles(self.home)]
            for store, database in databases:
                for nickname in nicknames:
                    self._nss_remove(report, store, database, nickname)
        else:
            report.add("nss", "", Outcome.SKIPPED, "certutil is missing")
        self._system(report, authority, install=False)
        state = authority.directory / STATE_FILE
        if report.ok:
            state.unlink(missing_ok=True)
        return report

    def status(self, authority: Authority) -> dict[str, object]:
        state = authority.directory / STATE_FILE
        recorded = json.loads(state.read_text()) if state.exists() else {}
        store = system_store(self.which)
        anchor = Path(store.anchor_dir) / _anchor_name(authority) if store else None
        return {
            "name": authority.name,
            "fingerprint": authority.fingerprint,
            "system": bool(anchor and anchor.exists()),
            "stores": recorded.get("stores", []),
        }

    def _record(self, authority: Authority, report: Report) -> None:
        stores = [
            {"store": step.store, "target": step.target}
            for step in report.steps
            if step.outcome in (Outcome.INSTALLED, Outcome.PRESENT)
        ]
        state = authority.directory / STATE_FILE
        state.write_text(json.dumps({"name": authority.name, "stores": stores}, indent=1))
