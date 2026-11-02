"""A real sayld on a real socket, in this event loop. Only the origin is a test double."""

import asyncio
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
import uvicorn

from daemon.api import create_app
from daemon.config import Config, Paths
from daemon.main import Daemon, build


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


@dataclass
class Stack:
    daemon: Daemon
    client: httpx.AsyncClient
    port: int
    downloads: Path


@asynccontextmanager
async def running_stack(root: Path, **overrides: int) -> AsyncIterator[Stack]:
    paths = Paths.from_env({"HOME": str(root)})
    values = {"connections": 4, "accelerate_min_bytes": 1024, "download_dir": str(root / "out")}
    daemon = await build(paths, Config(**{**values, **overrides}))  # type: ignore[arg-type]
    await daemon.start()
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(daemon.services),
            host="127.0.0.1",
            port=port,
            log_config=None,
            lifespan="off",
        )
    )
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    headers = {"Authorization": f"Bearer {daemon.services.token}"}
    client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", headers=headers, timeout=30)
    try:
        yield Stack(daemon, client, port, root / "out")
    finally:
        await client.aclose()
        server.should_exit = True
        await serving
        await daemon.stop()
