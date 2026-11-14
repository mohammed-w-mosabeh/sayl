"""yt-dlp auto-update against a local fake index: verify, stage, activate, and fall back."""

import hashlib
import json
import subprocess
import sys
import threading
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path

import pytest

from daemon import ytdlp_overlay
from daemon.ytdlp_overlay import newer, read_current, version_key
from daemon.ytdlp_update import UpdateError, Updater, extract, pinned_ejs
from tests.stack import running_stack

NEW = "2099.1.1"
EJS = "9.9.9"


def wheel(files: dict[str, str]) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return buffer.getvalue()


def ytdlp_wheel(version: str, pin: str = EJS, broken: bool = False) -> bytes:
    metadata = f"Metadata-Version: 2.1\nName: yt-dlp\nVersion: {version}\n"
    if pin:
        metadata += f'Requires-Dist: yt-dlp-ejs=={pin}; extra == "pin"\n'
    return wheel(
        {
            "yt_dlp/__init__.py": "raise RuntimeError('broken')\n" if broken else "",
            "yt_dlp/version.py": f"__version__ = {version!r}\n",
            f"yt_dlp-{version}.dist-info/METADATA": metadata,
        }
    )


@dataclass
class Index:
    url: str = ""
    files: dict[str, bytes] = field(default_factory=dict)
    requests: list[str] = field(default_factory=list)

    def release(
        self, project: str, version: str, payload: bytes, digest: str = "", pinned: bool = False
    ) -> None:
        name = f"{project.replace('-', '_')}-{version}-py3-none-any.whl"
        self.files[f"/files/{name}"] = payload
        body = {
            "info": {"version": version},
            "urls": [
                {"packagetype": "sdist", "filename": f"{project}-{version}.tar.gz"},
                {
                    "packagetype": "bdist_wheel",
                    "filename": name,
                    "url": f"{self.url}/files/{name}",
                    "digests": {"sha256": digest or hashlib.sha256(payload).hexdigest()},
                },
            ],
        }
        path = f"/{project}/{version}/json" if pinned else f"/{project}/json"
        self.files[path] = json.dumps(body).encode()


@pytest.fixture
def index() -> Iterator[Index]:
    state = Index()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            state.requests.append(self.path)
            body = state.files.get(self.path)
            self.send_response(200 if body is not None else 404)
            self.send_header("Content-Length", str(len(body or b"")))
            self.end_headers()
            self.wfile.write(body or b"")

        def log_message(self, *_: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield state
    server.shutdown()
    server.server_close()


def publish(index: Index, version: str = NEW, **options: bool | str) -> None:
    index.release("yt-dlp", version, ytdlp_wheel(version, broken=bool(options.get("broken"))))
    index.release("yt-dlp-ejs", EJS, wheel({"yt_dlp_ejs/__init__.py": ""}), pinned=True)


async def enabled() -> bool:
    return True


def activate_in_child(data: Path) -> dict[str, str]:
    """What a fresh sayld would import, without touching this test process's yt-dlp."""
    code = (
        "import json, sys; from pathlib import Path; from daemon import ytdlp_overlay; "
        "state = ytdlp_overlay.activate(Path(sys.argv[1])); import yt_dlp; "
        "print(json.dumps({**state.__dict__, 'file': yt_dlp.__file__}))"
    )
    done = subprocess.run(  # noqa: S603 - argument list
        [sys.executable, "-c", code, str(data)], capture_output=True, text=True, check=True
    )
    result: dict[str, str] = json.loads(done.stdout)
    return result


@pytest.mark.parametrize(
    ("candidate", "than", "expected"),
    [
        ("2026.11.1", "2026.10.26", True),
        ("2026.10.26.123456", "2026.10.26", True),
        ("2026.10.26", "2026.10.26", False),
        ("2026.9.30", "2026.10.26", False),
        ("2026.11.1", "", True),
        ("garbage", "2026.10.26", False),
    ],
)
def test_versions(candidate: str, than: str, expected: bool) -> None:
    assert newer(candidate, than) is expected
    assert version_key("1.2.x") is None


def test_pinned_ejs() -> None:
    assert pinned_ejs(ytdlp_wheel(NEW)) == EJS
    assert pinned_ejs(ytdlp_wheel(NEW, pin="")) == ""


@pytest.mark.parametrize("name", ["../evil.py", "/abs.py", "a/../../b.py", "a\\..\\b.py"])
def test_unsafe_wheel_paths_are_refused(tmp_path: Path, name: str) -> None:
    with pytest.raises(UpdateError):
        extract(wheel({name: "x"}), tmp_path)
    assert list(tmp_path.iterdir()) == []


async def test_newer_version_is_verified_staged_and_used_after_restart(
    tmp_path: Path, index: Index
) -> None:
    publish(index)
    updater = Updater(tmp_path, enabled, index=index.url)
    status = await updater.check()
    assert status.error == ""
    assert status.staged == NEW
    assert status.restart_pending
    assert status.checked_at
    assert read_current(tmp_path) == {"version": NEW, "ejs": EJS}
    assert (tmp_path / "yt-dlp" / NEW / "yt_dlp_ejs" / "__init__.py").exists()
    assert {p.name for p in (tmp_path / "yt-dlp").iterdir()} == {"current", NEW}

    child = activate_in_child(tmp_path)
    assert child["active"] == NEW
    assert child["file"].startswith(str(tmp_path / "yt-dlp" / NEW))

    # A second check finds nothing newer than what is staged and downloads no wheel.
    index.requests.clear()
    await updater.check()
    assert index.requests == ["/yt-dlp/json"]


async def test_wrong_hash_stages_nothing(tmp_path: Path, index: Index) -> None:
    publish(index)
    index.release("yt-dlp", NEW, ytdlp_wheel(NEW), digest="0" * 64)
    status = await Updater(tmp_path, enabled, index=index.url).check()
    assert "SHA-256" in status.error
    assert read_current(tmp_path) == {}
    assert not status.restart_pending


async def test_older_version_is_ignored(tmp_path: Path, index: Index) -> None:
    publish(index, version="2001.1.1")
    status = await Updater(tmp_path, enabled, index=index.url).check()
    assert status.error == ""
    assert status.staged == ""
    assert not any(path.endswith(".whl") for path in index.requests)


async def test_wheel_that_does_not_import_is_not_staged(tmp_path: Path, index: Index) -> None:
    publish(index, broken=True)
    status = await Updater(tmp_path, enabled, index=index.url).check()
    assert "does not import" in status.error
    assert read_current(tmp_path) == {}
    assert [p.name for p in (tmp_path / "yt-dlp").iterdir()] == []


async def test_unreachable_index_is_reported(tmp_path: Path) -> None:
    status = await Updater(tmp_path, enabled, index="http://127.0.0.1:9").check()
    assert status.error.startswith("yt-dlp update failed")


def test_staged_copy_that_fails_to_import_falls_back(tmp_path: Path) -> None:
    folder = tmp_path / "yt-dlp" / NEW
    extract(ytdlp_wheel(NEW, broken=True), folder)
    (tmp_path / "yt-dlp" / "current").write_text(json.dumps({"version": NEW}))
    child = activate_in_child(tmp_path)
    assert child["active"] == child["bundled"] == ytdlp_overlay.bundled_version()
    assert "ignored" in child["error"]
    assert not child["file"].startswith(str(tmp_path))


def test_bundled_copy_newer_than_staged_wins(tmp_path: Path) -> None:
    extract(ytdlp_wheel("2001.1.1"), tmp_path / "yt-dlp" / "2001.1.1")
    (tmp_path / "yt-dlp" / "current").write_text(json.dumps({"version": "2001.1.1"}))
    child = activate_in_child(tmp_path)
    assert child["active"] == ytdlp_overlay.bundled_version()
    assert child["error"] == ""


async def test_api_checks_now_and_info_reports(tmp_path: Path, index: Index) -> None:
    publish(index)
    async with running_stack(tmp_path, ytdlp_index=index.url) as stack:
        before = (await stack.client.get("/info")).json()["ytdlp"]
        assert before["bundled"] == ytdlp_overlay.bundled_version()
        assert before["restart_pending"] is False
        checked = (await stack.client.post("/ytdlp/update")).json()
        assert checked["staged"] == NEW
        assert checked["restart_pending"] is True
        after = (await stack.client.get("/info")).json()["ytdlp"]
        assert after["staged"] == NEW
