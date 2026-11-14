"""Integration tests: the real CLI against a real sayld process and a real local origin."""

import asyncio
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from cli.main import app, progress, size
from daemon.config import Paths
from daemon.db import make_engine
from daemon.models import Download, Status
from daemon.store import Store
from daemon.ytdlp_overlay import bundled_version
from tests.origin import SHA256, Origin
from tests.stack import free_port

runner = CliRunner()
WARNING = {
    "quality_degraded": {
        "items": [
            {
                "title": "Film",
                "requested": {"height": 1080, "vcodec": "avc1", "acodec": "", "has_audio": True},
                "delivered": {"height": 480, "vcodec": "avc1", "acodec": "", "has_audio": False},
                "reasons": ["height 480 is below 1080", "audio is missing"],
            }
        ],
        "solver_failures": ["n challenge solving failed"],
    }
}


@dataclass
class Sayld:
    home: Path
    out: Path
    origin: Origin
    port: int

    @property
    def paths(self) -> Paths:
        return Paths.from_env({"HOME": str(self.home)})


@pytest.fixture(scope="module")
def origin() -> Iterator[Origin]:
    """The CLI blocks, so the origin needs its own thread and event loop."""
    server = Origin()
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    asyncio.run_coroutine_threadsafe(server.start(), loop).result(5)
    yield server
    loop.call_soon_threadsafe(loop.stop)


@pytest.fixture(scope="module")
def sayld(tmp_path_factory: pytest.TempPathFactory, origin: Origin) -> Iterator[Sayld]:
    home = tmp_path_factory.mktemp("home")
    out, port = home / "out", free_port()
    config = home / ".config/sayl"
    config.mkdir(parents=True)
    (config / "config.toml").write_text(
        f'port = {port}\ncapture_port = {free_port()}\ndownload_dir = "{out}"\n'
        'ytdlp_index = "http://127.0.0.1:9"\n'  # Nothing listens: updates fail, offline.
    )
    env = {key: value for key, value in os.environ.items() if not key.startswith("XDG_")}
    process = subprocess.Popen(
        [sys.executable, "-m", "daemon.launch"],  # What the sayld entry point runs.
        env={**env, "HOME": str(home)},
    )
    target = Sayld(home, out, origin, port)
    try:
        for _ in range(600):
            if target.paths.token.exists():
                try:
                    httpx.get(f"http://127.0.0.1:{port}/docs", timeout=1)
                    break
                except httpx.TransportError:
                    pass
            time.sleep(0.05)
        yield target
    finally:
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=30)


@pytest.fixture(autouse=True)
def at_home(sayld: Sayld, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(sayld.home))
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COLUMNS", "200")


def sayl(*args: str, code: int = 0) -> str:
    result = runner.invoke(app, list(args))
    assert result.exit_code == code, result.output
    return result.output


def as_json(*args: str) -> Any:
    return json.loads(sayl(*args, "--json"))


def wait_for(download_id: str, *states: str) -> dict[str, Any]:
    for _ in range(1200):
        row: dict[str, Any] = as_json("info", download_id)
        if row["status"] in states:
            return row
        time.sleep(0.05)
    raise AssertionError(row["status"])


def add(sayld: Sayld, path: str, *args: str) -> str:
    row: dict[str, Any] = as_json("add", sayld.origin.url(path), *args)
    return str(row["id"])


def test_add_then_ls_and_info(sayld: Sayld) -> None:
    download_id = add(sayld, "/cli-a.bin")
    row = wait_for(download_id, "completed", "failed")
    assert row["status"] == "completed"
    data = (sayld.out / "cli-a.bin").read_bytes()
    assert hashlib.sha256(data).hexdigest() == SHA256
    assert download_id[:8] in sayl("ls")
    assert "cli-a.bin" in sayl("ls", "--status", "completed")
    detail = sayl("info", download_id[:8])
    assert "status: completed" in detail
    assert "4.0 MB / 4.0 MB (100%)" in detail


def test_add_prints_a_short_id(sayld: Sayld) -> None:
    output = sayl("add", sayld.origin.url("/slow/short.bin"), "--paused")
    short = output.split()[1]
    assert len(short) == 8
    sayl("rm", short)


def test_pause_resume_and_rm(sayld: Sayld) -> None:
    download_id = add(sayld, "/slow/cli-b.bin", "--filename", "named.bin", "--limit", "100000")
    wait_for(download_id, "active")
    assert "paused" in sayl("pause", download_id[:8])
    assert as_json("info", download_id)["status"] == "paused"
    assert "active" in sayl("resume", download_id)
    assert as_json("info", download_id)["filename"].startswith("named")
    assert "removed" in sayl("rm", download_id)
    assert "no download matches" in sayl("info", download_id, code=1)


def test_watch_follows_one_download_to_its_end(sayld: Sayld) -> None:
    download_id = add(sayld, "/slow/cli-c.bin")
    output = sayl("watch", download_id[:8])
    assert f"{download_id[:8]} completed" in output
    assert "/s" in output  # Live progress lines came first.
    assert "completed" in sayl("watch", download_id)  # Already over: returns at once.


def test_refresh_needs_a_download_in_that_state(sayld: Sayld) -> None:
    download_id = add(sayld, "/cli-d.bin", "--paused")
    new = sayld.origin.url("/cli-d2.bin")
    assert "paused" in sayl("refresh", download_id, new)
    assert as_json("info", download_id)["url"] == new
    sayl("rm", download_id)


def test_config_sets_and_shows(sayld: Sayld) -> None:
    sayl("config", "speed_limit", "0")
    sayl("config", "theme", "dark")
    assert 'theme = "dark"' in sayl("config")
    assert sayl("config", "speed_limit").strip() == "speed_limit = 0"
    assert "no setting named" in sayl("config", "nothing", code=1)


def test_info_shows_a_quality_warning(sayld: Sayld) -> None:
    store = Store(make_engine(sayld.paths.database))
    row = Download(
        url="https://v",
        filename="f.mp4",
        status=Status.COMPLETED,
        warnings_json=json.dumps(WARNING),
    )
    store.add(row)
    detail = sayl("info", row.id)
    assert "quality_degraded" in detail
    assert "requested: 1080p, avc1, audio" in detail
    assert "delivered: 480p, avc1, no audio" in detail
    assert "height 480 is below 1080" in detail
    assert "solver: n challenge solving failed" in detail
    assert "completed (warning)" in sayl("ls")
    sayl("rm", row.id)


def test_a_prefix_must_be_unique(sayld: Sayld) -> None:
    first = add(sayld, "/p1.bin", "--paused")
    second = add(sayld, "/p2.bin", "--paused")
    shared = first[: next(i for i, (a, b) in enumerate(zip(first, second, strict=True)) if a != b)]
    assert "matches" in sayl("pause", shared, code=1)
    sayl("rm", first)
    sayl("rm", second)


def test_errors_are_readable_without_a_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert "has never run here" in sayl("ls", code=1)
    paths = Paths.from_env({"HOME": str(tmp_path)})
    paths.create()
    paths.token.write_text("x")
    (paths.config / "config.toml").write_text(f"port = {free_port()}\n")
    assert "cannot reach sayld" in sayl("ls", code=1)
    assert "cannot reach sayld" in sayl("watch", code=1)


def test_a_wrong_token_is_reported(
    sayld: Sayld, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = Paths.from_env({"HOME": str(tmp_path)})
    paths.create()
    paths.token.write_text("wrong")
    (paths.config / "config.toml").write_text(f"port = {sayld.port}\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert "invalid token" in sayl("ls", code=1)


@pytest.mark.parametrize(
    ("value", "text"), [(10, "10 B"), (2048, "2.0 KB"), (5 << 30, "5.0 GB"), (5 << 40, "5120.0 GB")]
)
def test_size(value: int, text: str) -> None:
    assert size(value) == text


def test_progress_without_a_known_size() -> None:
    assert progress({"total_bytes": 0, "done_bytes": 2048}) == "2.0 KB"


def test_capture_enable_status_disable_and_uninstall(sayld: Sayld) -> None:
    assert "not created yet" in sayl("capture", "status")
    enabled = sayl("capture", "enable", "--no-system-proxy")
    assert "running" in enabled
    assert "No browser trusts the Sayl CA yet" in enabled
    status = as_json("capture", "status")
    assert (status["state"], status["system_proxy"]) == ("running", False)
    assert status["ca"]["name"].startswith("Sayl local CA")
    assert "off" in sayl("capture", "disable")
    removed = sayl("capture", "uninstall")
    assert "system" in removed
    assert as_json("capture", "status")["ca"] is None


def test_capture_install_ca_reports_each_store(sayld: Sayld) -> None:
    has_certutil = shutil.which("certutil") is not None
    output = sayl("capture", "install-ca", "--no-system", code=0 if has_certutil else 1)
    assert "chrome" in output if has_certutil else "libnss3-tools" in output
    sayl("capture", "uninstall")


def test_add_with_a_checksum_then_verify(sayld: Sayld) -> None:
    download_id = add(sayld, "/cli-sum.bin", "--checksum", f"sha256:{SHA256}")
    assert wait_for(download_id, "completed", "failed")["status"] == "completed"
    assert f"ok sha256 {SHA256}" in sayl("verify", download_id[:8])
    output = sayl("verify", download_id[:8], "0" * 64, code=1)
    assert "mismatch" in output and f"actual   {SHA256}" in output


def test_queue_stop_start_and_schedules(sayld: Sayld) -> None:
    assert "stopped" in sayl("queue", "stop")
    try:
        download_id = add(sayld, "/cli-held.bin")
        time.sleep(1.5)
        assert as_json("info", download_id)["status"] == "queued"
    finally:
        assert "running" in sayl("queue", "start")
    assert wait_for(download_id, "completed", "failed")["status"] == "completed"

    added = sayl("schedule", "add", "night", "0 2 * * *", "speed_limit", "--limit", "5000")
    short = added.split()[1].rstrip(",")
    listing = sayl("schedule", "ls")
    assert short in listing and "speed_limit 4.9 KB/s" in listing
    assert f"ran {short}" in sayl("schedule", "run", short)
    assert "speed_limit = 5000" in sayl("config", "speed_limit")
    sayl("config", "speed_limit", "0")
    assert f"removed {short}" in sayl("schedule", "rm", short)
    assert "0 schedules start with" in sayl("schedule", "run", short, code=1)
    bad = sayl("schedule", "add", "x", "0 2 * * *", "stop_queue", "--after", "shutdown", code=1)
    assert "only a start_queue" in bad


def test_batch_reads_links_and_patterns(sayld: Sayld, tmp_path: Path) -> None:
    listing = tmp_path / "links.txt"
    listing.write_text(f"# a comment\n{sayld.origin.url('/cli-list.bin')}\n\n")
    output = sayl("batch", sayld.origin.url("/cli-p[1-2].bin"), "--file", str(listing), "--paused")
    assert "added 3 downloads" in output


def test_grab_lists_then_queues_a_sites_files(sayld: Sayld) -> None:
    output = sayl("grab", sayld.origin.url("/site/"), "--queue", "--dest", str(sayld.out / "grab"))
    assert "2 files on 1 pages (done)" in output
    assert "added 2 downloads" in output


def test_creds_take_the_password_from_stdin_and_never_print_it(sayld: Sayld) -> None:
    result = runner.invoke(
        app, ["creds", "set", "https://Files.Example/x", "me", "--password-stdin"], input="pw1\n"
    )
    assert result.exit_code == 0, result.output
    assert "files.example" in result.output
    listed = sayl("creds", "ls")
    assert "files.example" in listed and "me" in listed and "pw1" not in listed
    prompted = runner.invoke(app, ["creds", "set", "b.example", "you"], input="pw2\n")
    assert prompted.exit_code == 0 and "pw2" not in prompted.output
    assert {row["host"] for row in as_json("creds", "ls")} == {"files.example", "b.example"}
    sayl("creds", "rm", "files.example")
    sayl("creds", "rm", "files.example", code=1)
    sayl("creds", "rm", "b.example")


def test_history_export_import_and_stats(sayld: Sayld, tmp_path: Path) -> None:
    add(sayld, "/normal.bin")
    exported = tmp_path / "history.json"
    assert "exported" in sayl("history", "export", str(exported))
    document = json.loads(exported.read_text())
    assert document["format"] == "sayl-history" and document["downloads"]
    assert "skipped" in sayl("history", "import", str(exported))  # Same ids: all skipped.
    assert json.loads(sayl("history", "export"))["format"] == "sayl-history"
    broken = tmp_path / "broken.json"
    broken.write_text("{")
    assert "cannot read" in sayl("history", "import", str(broken), code=1)
    assert "last 30 days" in sayl("stats")
    assert len(as_json("stats")["per_day"]) == 30


def test_ytdlp_status_and_a_failed_update(sayld: Sayld) -> None:
    status = sayl("ytdlp", "status")
    assert f"active   {bundled_version()}" in status
    assert "update failed" in sayl("ytdlp", "update")
