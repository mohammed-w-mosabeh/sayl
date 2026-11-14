"""yt-dlp auto-update: fetch a newer wheel from PyPI, verify it, and stage it for the next start.

Nothing here changes the yt-dlp this process already imported. daemon/ytdlp_overlay.py picks
the staged copy up when sayld starts again.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import zipfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any

import httpx

from daemon import ytdlp_overlay
from daemon.ytdlp_overlay import STATE, newer, root

log = logging.getLogger(__name__)

INDEX = "https://pypi.org/pypi"
FIRST_CHECK_SECONDS = 600
INTERVAL_SECONDS = 24 * 3600
MAX_WHEEL_BYTES = 64 * 1024 * 1024
PIN = re.compile(
    r"^Requires-Dist:\s*yt-dlp-ejs\s*==\s*([\w.]+)\s*;.*extra\s*==\s*['\"]pin['\"]", re.M
)
VALIDATE = (
    "import sys; sys.path.insert(0, sys.argv[1]); import yt_dlp, yt_dlp.version; "
    "assert yt_dlp.__file__.startswith(sys.argv[1]), yt_dlp.__file__; "
    "print(yt_dlp.version.__version__)"
)


class UpdateError(Exception):
    pass


@dataclass(frozen=True)
class Wheel:
    name: str
    version: str
    url: str
    sha256: str


@dataclass
class Status:
    active: str
    bundled: str
    staged: str
    restart_pending: bool
    checked_at: str
    error: str


def pick_wheel(project: str, release: dict[str, Any]) -> Wheel:
    """The pure-Python wheel of a PyPI JSON release, or UpdateError."""
    version = str(release.get("info", {}).get("version", ""))
    for item in release.get("urls", []):
        filename = str(item.get("filename", ""))
        digest = str(item.get("digests", {}).get("sha256", ""))
        if (
            item.get("packagetype") == "bdist_wheel"
            and filename.endswith("-py3-none-any.whl")
            and re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            return Wheel(project, version, str(item["url"]), digest)
    raise UpdateError(f"{project} {version} has no py3-none-any wheel")


def pinned_ejs(wheel: bytes) -> str:
    """The yt-dlp-ejs version a yt-dlp wheel pins, or "" if it pins none."""
    with zipfile.ZipFile(BytesIO(wheel)) as archive:
        for name in archive.namelist():
            if name.endswith(".dist-info/METADATA"):
                found = PIN.search(archive.read(name).decode("utf-8", "replace"))
                return found.group(1) if found else ""
    raise UpdateError("the wheel has no METADATA")


def extract(wheel: bytes, into: Path) -> None:
    """Unpack a wheel, refusing members that would land outside `into`."""
    with zipfile.ZipFile(BytesIO(wheel)) as archive:
        for member in archive.infolist():
            name = member.filename
            parts = PurePosixPath(name).parts
            if not parts or name.startswith("/") or ".." in parts or "\\" in name:
                raise UpdateError(f"unsafe path in wheel: {member.filename!r}")
            if (member.external_attr >> 16) & 0o170000 == 0o120000:
                raise UpdateError(f"symlink in wheel: {member.filename!r}")
        archive.extractall(into)


def write_current(data: Path, version: str, ejs: str) -> None:
    folder = root(data)
    with tempfile.NamedTemporaryFile("w", dir=folder, delete=False, suffix=".tmp") as stream:
        json.dump({"version": version, "ejs": ejs}, stream)
    Path(stream.name).replace(folder / "current")


def prune(data: Path, keep: set[str]) -> None:
    """Remove staged versions other than `keep` and leftovers of an interrupted update."""
    folder = root(data)
    for entry in folder.iterdir():
        if entry.is_dir() and entry.name not in keep:
            shutil.rmtree(entry, ignore_errors=True)
        elif entry.suffix == ".tmp":
            entry.unlink(missing_ok=True)


class Updater:
    def __init__(
        self,
        data: Path,
        enabled: Callable[[], Awaitable[bool]],
        index: str = INDEX,
        python: str = sys.executable,
    ) -> None:
        self.data = data
        self.enabled = enabled
        self.index = index.rstrip("/")
        self.python = python
        self.proxy: str | None = None  # Set by OutboundProxy.
        self.checked_at = ""
        self.error = STATE.error
        self.lock = asyncio.Lock()
        self.task: asyncio.Task[None] | None = None

    def status(self) -> Status:
        staged = ytdlp_overlay.read_current(self.data).get("version", "")
        active = STATE.active or STATE.bundled or ytdlp_overlay.bundled_version()
        return Status(
            active=active,
            bundled=STATE.bundled or ytdlp_overlay.bundled_version(),
            staged=staged,
            restart_pending=bool(staged) and newer(staged, active),
            checked_at=self.checked_at,
            error=self.error,
        )

    async def start(self, first: float = FIRST_CHECK_SECONDS) -> None:
        self.task = asyncio.create_task(self._loop(first))

    async def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    async def _loop(self, first: float) -> None:
        await asyncio.sleep(first)
        while True:
            if await self.enabled():
                await self.check()
            await asyncio.sleep(INTERVAL_SECONDS)

    async def check(self) -> Status:
        """Stage a newer yt-dlp if PyPI has one. Errors are kept in the status, never raised."""
        async with self.lock:
            try:
                await self._check()
                self.error = ""
            except (UpdateError, httpx.HTTPError, OSError, ValueError, zipfile.BadZipFile) as error:
                self.error = f"yt-dlp update failed: {error}"
                log.warning("%s", self.error)
            self.checked_at = datetime.now(UTC).isoformat(timespec="seconds")
            return self.status()

    async def _check(self) -> None:
        async with httpx.AsyncClient(
            timeout=60, follow_redirects=True, proxy=self.proxy, headers={"User-Agent": "sayl"}
        ) as client:
            release = await self._json(client, f"{self.index}/yt-dlp/json")
            wheel = pick_wheel("yt-dlp", release)
            status = self.status()
            if not newer(wheel.version, status.staged or status.active):
                log.info("yt-dlp %s is current", status.staged or status.active)
                return
            payload = await self._download(client, wheel)
            ejs_version = await asyncio.to_thread(pinned_ejs, payload)
            wheels = [payload]
            if ejs_version:
                url = f"{self.index}/yt-dlp-ejs/{ejs_version}/json"
                ejs_release = await self._json(client, url)
                wheels.append(await self._download(client, pick_wheel("yt-dlp-ejs", ejs_release)))
        await self._stage(wheel.version, ejs_version, wheels)
        log.info("yt-dlp %s staged; it is used from the next start", wheel.version)

    async def _json(self, client: httpx.AsyncClient, url: str) -> dict[str, Any]:
        response = await client.get(url)
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            raise UpdateError(f"unexpected answer from {url}")
        return value

    async def _download(self, client: httpx.AsyncClient, wheel: Wheel) -> bytes:
        body = bytearray()
        async with client.stream("GET", wheel.url) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > MAX_WHEEL_BYTES:
                    raise UpdateError(f"{wheel.name} {wheel.version} is larger than expected")
        if hashlib.sha256(body).hexdigest() != wheel.sha256:
            raise UpdateError(f"{wheel.name} {wheel.version}: SHA-256 does not match the index")
        return bytes(body)

    async def _stage(self, version: str, ejs: str, wheels: list[bytes]) -> None:
        folder = root(self.data)
        await asyncio.to_thread(folder.mkdir, parents=True, exist_ok=True, mode=0o700)
        work = Path(await asyncio.to_thread(tempfile.mkdtemp, prefix=".stage-", dir=folder))
        try:
            for payload in wheels:
                await asyncio.to_thread(extract, payload, work)
            await self._validate(work, version)
            target = folder / version
            await asyncio.to_thread(shutil.rmtree, target, ignore_errors=True)
            await asyncio.to_thread(os.replace, work, target)
            await asyncio.to_thread(write_current, self.data, version, ejs)
        finally:
            await asyncio.to_thread(shutil.rmtree, work, ignore_errors=True)
        # The copy this process imported stays until it exits: lazy imports still read it.
        keep = {version} | ({Path(STATE.path).name} if STATE.path else set())
        await asyncio.to_thread(prune, self.data, keep)

    async def _validate(self, folder: Path, version: str) -> None:
        child = await asyncio.create_subprocess_exec(
            self.python,
            "-I",
            "-c",
            VALIDATE,
            str(folder),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(child.communicate(), timeout=120)
        except TimeoutError as error:
            child.kill()
            await child.wait()
            raise UpdateError("importing the new yt-dlp timed out") from error
        found = out.decode().strip()
        if child.returncode != 0 or found != version:
            last = (err.decode().strip().splitlines() or ["no output"])[-1]
            raise UpdateError(f"the new yt-dlp does not import ({found or last})")
