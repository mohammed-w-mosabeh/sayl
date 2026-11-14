"""Aria2Engine: a managed aria2c child process driven by JSON-RPC over WebSocket."""

import asyncio
import base64
import contextlib
import json
import logging
import os
import secrets
import shutil
import socket
import subprocess
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx
import websockets
from websockets.asyncio.client import ClientConnection

from daemon.engines.base import Engine, EngineError, Job, ProbeResult, Progress
from daemon.filenames import sanitize
from daemon.models import EngineName, Status

log = logging.getLogger(__name__)

CONNECT_ATTEMPTS = 100
_STATES = {
    "active": Status.ACTIVE,
    "waiting": Status.QUEUED,
    "paused": Status.PAUSED,
    "complete": Status.COMPLETED,
    "error": Status.FAILED,
    "removed": Status.CANCELED,
}


def file_allocation(directory: Path) -> str:
    """falloc, or none on filesystems that do not support it."""
    try:
        with tempfile.NamedTemporaryFile(dir=directory) as probe:
            os.posix_fallocate(probe.fileno(), 0, 4096)
    except OSError:
        return "none"
    return "falloc"


def local_torrent(url: str) -> Path | None:
    """A .torrent on this machine, as a file:// URL or an absolute path, if url is one."""
    if url.startswith("file://"):
        path = Path(unquote(urlsplit(url).path))
    elif url.startswith("/"):
        path = Path(url)
    else:
        return None
    return path if path.suffix.lower() == ".torrent" else None


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


@dataclass(frozen=True)
class Seeding:
    """How long a finished torrent keeps seeding: until either limit is reached."""

    ratio: float = 1.0
    minutes: int = 60


DEFAULT_SEEDING = Seeding()


def command_line(
    binary: str,
    port: int,
    secret: str,
    data: Path,
    allocation: str,
    seeding: Seeding = DEFAULT_SEEDING,
) -> list[str]:
    session = data / "aria2.session"
    arguments = [
        binary,
        "--enable-rpc",
        "--rpc-listen-all=false",
        f"--rpc-listen-port={port}",
        f"--rpc-secret={secret}",
        "--continue=true",
        f"--save-session={session}",
        "--save-session-interval=30",
        "--max-connection-per-server=16",
        "--split=16",
        "--min-split-size=1M",
        f"--file-allocation={allocation}",
        f"--stop-with-process={os.getpid()}",  # Never outlive a killed daemon.
        "--quiet=true",
        f"--seed-ratio={seeding.ratio}",
        f"--seed-time={seeding.minutes}",
        "--bt-detach-seed-only=true",  # A seeding torrent does not hold a download slot.
        "--bt-save-metadata=true",  # A magnet's metadata survives a restart.
        f"--dht-file-path={data / 'dht.dat'}",
        f"--dht-file-path6={data / 'dht6.dat'}",
    ]
    if session.exists():
        arguments.append(f"--input-file={session}")
    return arguments


async def probe_url(client: httpx.AsyncClient, url: str, headers: dict[str, str]) -> ProbeResult:
    """HEAD, falling back to a ranged GET for servers that refuse it."""
    response = await client.head(url, headers=headers, follow_redirects=True)
    if response.status_code >= 400:
        ranged = {**headers, "Range": "bytes=0-0"}
        async with client.stream("GET", url, headers=ranged, follow_redirects=True) as stream:
            response = stream
    resumable = response.status_code == 206 or (
        response.headers.get("accept-ranges", "").lower() == "bytes"
    )
    content_range = response.headers.get("content-range", "")
    size = content_range.rpartition("/")[2] or response.headers.get("content-length", "")
    final = str(response.url)
    return ProbeResult(
        final,
        sanitize(final.split("?")[0].rsplit("/", 1)[-1]),
        int(size) if size.isdigit() else None,
        resumable,
    )


class Aria2Engine(Engine):
    name = EngineName.ARIA2

    def __init__(
        self,
        data: Path,
        *,
        binary: str | None = None,
        client: httpx.AsyncClient | None = None,
        on_restart: Callable[[], Awaitable[None]] | None = None,
        seeding: Seeding = DEFAULT_SEEDING,
    ) -> None:
        self.data = data
        self.seeding = seeding
        self.binary = binary or shutil.which("aria2c")
        self.client = client or httpx.AsyncClient(timeout=30)
        self.on_restart = on_restart
        self.process: subprocess.Popen[bytes] | None = None
        self.socket: ClientConnection | None = None
        self.secret = ""
        self.pending: dict[str, asyncio.Future[Any]] = {}
        self.reader: asyncio.Task[None] | None = None
        self.watchdog: asyncio.Task[None] | None = None
        self.stopping = False
        self.global_limit = 0
        self.own_limits: dict[str, int] = {}
        self.proxy = ""  # An HTTP proxy URL, or "" for direct; kept across aria2 restarts.

    def _effective(self, own: int) -> int:
        """aria2's overall limit is a moving average, so it lets a burst through after a slow
        period. A per-download cap has no such window, so the global limit is applied as both."""
        return min((rate for rate in (own, self.global_limit) if rate), default=0)

    async def start(self) -> None:
        await self._spawn()
        self.watchdog = asyncio.create_task(self._watch())

    async def _spawn(self) -> None:
        if self.binary is None:
            raise EngineError("aria2c is not installed; run 'sayl doctor'")
        port = _free_port()
        self.secret = secrets.token_hex(16)
        allocation = await asyncio.to_thread(file_allocation, self.data)
        arguments = command_line(
            self.binary, port, self.secret, self.data, allocation, self.seeding
        )
        self.process = subprocess.Popen(arguments)  # noqa: S603, ASYNC220 - argument list
        for _ in range(CONNECT_ATTEMPTS):
            try:
                self.socket = await websockets.connect(f"ws://127.0.0.1:{port}/jsonrpc")
                break
            except OSError:
                await asyncio.sleep(0.05)
        else:
            raise EngineError("aria2c did not start")
        self.reader = asyncio.create_task(self._read(self.socket))
        if self.proxy:
            await self.set_proxy(self.proxy)

    async def _read(self, connection: ClientConnection) -> None:
        with contextlib.suppress(websockets.ConnectionClosed):
            async for message in connection:
                reply = json.loads(message)
                future = self.pending.pop(reply.get("id", ""), None)
                if future is None or future.done():
                    continue  # A notification. The queue polls status instead.
                if "error" in reply:
                    future.set_exception(EngineError(reply["error"]["message"]))
                else:
                    future.set_result(reply["result"])
        for future in self.pending.values():
            if not future.done():
                future.set_exception(EngineError("aria2c connection lost"))
        self.pending.clear()

    async def _watch(self) -> None:
        """Restart aria2c if it dies. Its session file brings back unfinished downloads."""
        while not self.stopping:
            await asyncio.sleep(0.5)
            if self.process is not None and self.process.poll() is not None:
                log.warning("aria2c exited, restarting")
                await self._spawn()
                if self.on_restart is not None:
                    await self.on_restart()

    async def stop(self) -> None:
        self.stopping = True
        if self.watchdog is not None:
            self.watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.watchdog
        with contextlib.suppress(EngineError, OSError):
            await asyncio.wait_for(self.rpc("aria2.saveSession"), 5)
        if self.socket is not None:
            await self.socket.close()
        if self.reader is not None:
            await self.reader
        if self.process is not None:
            self.process.terminate()
            await asyncio.to_thread(self.process.wait, 10)
        await self.client.aclose()

    async def rpc(self, method: str, *params: Any) -> Any:
        if self.socket is None:
            raise EngineError("aria2c is not running")
        call = secrets.token_hex(8)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self.pending[call] = future
        body = {
            "jsonrpc": "2.0",
            "id": call,
            "method": method,
            "params": [f"token:{self.secret}", *params],
        }
        try:
            await self.socket.send(json.dumps(body))
        except websockets.ConnectionClosed as error:
            self.pending.pop(call, None)
            raise EngineError("aria2c connection lost") from error
        return await future

    async def probe(self, url: str, headers: dict[str, str]) -> ProbeResult:
        return await probe_url(self.client, url, headers)

    async def add(self, job: Job) -> str:
        options: dict[str, Any] = {
            "dir": job.dest_dir,
            "header": [f"{name}: {value}" for name, value in job.headers.items()],
        }
        limit = self._effective(job.speed_limit)
        if limit:
            options["max-download-limit"] = str(limit)
        torrent = local_torrent(job.url)
        if torrent is not None:
            metainfo = await asyncio.to_thread(torrent.read_bytes)
            encoded = base64.b64encode(metainfo).decode()
            gid = str(await self.rpc("aria2.addTorrent", encoded, [], options))
            self.own_limits[gid] = job.speed_limit
            return gid
        if job.filename:
            options["out"] = sanitize(job.filename)
        gid = str(await self.rpc("aria2.addUri", [job.url], options))
        self.own_limits[gid] = job.speed_limit
        return gid

    async def pause(self, engine_id: str) -> None:
        await self.rpc("aria2.forcePause", engine_id)

    async def resume(self, engine_id: str) -> None:
        await self.rpc("aria2.unpause", engine_id)

    async def cancel(self, engine_id: str) -> None:
        await self.rpc("aria2.forceRemove", engine_id)

    async def set_speed_limit(self, engine_id: str, limit: int) -> None:
        self.own_limits[engine_id] = limit
        effective = str(self._effective(limit))
        await self.rpc("aria2.changeOption", engine_id, {"max-download-limit": effective})

    async def set_proxy(self, url: str) -> None:
        """Route HTTP and FTP through this HTTP proxy, with CONNECT even for plain HTTP."""
        self.proxy = url
        options = {"all-proxy": url, "proxy-method": "tunnel" if url else "get"}
        await self.rpc("aria2.changeGlobalOption", options)

    async def set_global_speed_limit(self, limit: int) -> None:
        self.global_limit = limit
        await self.rpc("aria2.changeGlobalOption", {"max-overall-download-limit": str(limit)})
        for gid, own in list(self.own_limits.items()):
            with contextlib.suppress(EngineError):  # It may have finished already.
                await self.set_speed_limit(gid, own)

    async def refresh_url(self, engine_id: str, new_url: str) -> None:
        details = await self.rpc("aria2.tellStatus", engine_id, ["files"])
        old = sorted({entry["uri"] for entry in details["files"][0]["uris"]})
        await self.rpc("aria2.changeUri", engine_id, 1, old, [new_url])

    async def status(self, engine_id: str) -> Progress:
        details = await self.rpc("aria2.tellStatus", engine_id)
        followed = details.get("followedBy") or []
        if followed:
            # A magnet's metadata, or a .torrent fetched over HTTP: the files follow under a
            # new id, which takes over the per-download limit.
            if engine_id in self.own_limits and followed[0] not in self.own_limits:
                with contextlib.suppress(EngineError):
                    await self.set_speed_limit(followed[0], self.own_limits[engine_id])
            progress = await self.status(followed[0])
            return replace(progress, engine_ref=progress.engine_ref or followed[0])
        files = details.get("files") or [{}]
        total, done = int(details["totalLength"]), int(details["completedLength"])
        state = _STATES[details["status"]]
        torrent = details.get("bittorrent")
        name = Path(files[0].get("path", "")).name
        if torrent is not None and not torrent.get("info"):
            # A magnet still fetching its metadata: the files have not started.
            if state in (Status.ACTIVE, Status.COMPLETED):
                state, total, done = Status.ACTIVE, 0, 0
            name = ""
        elif torrent is not None:
            name = torrent["info"].get("name") or name
            if state is Status.ACTIVE and total and done >= total:
                state = Status.COMPLETED  # Seeding in the background: the files are complete.
        return Progress(
            state,
            total,
            done,
            int(details["downloadSpeed"]),
            int(details.get("connections", 0)),
            name,
            details.get("errorMessage", ""),
        )
