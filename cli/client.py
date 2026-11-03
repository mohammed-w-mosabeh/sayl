"""Talks to sayld. Like every client, the CLI holds no state of its own."""

import json
from collections.abc import Iterator
from typing import Any

import httpx
from websockets.exceptions import ConnectionClosed, WebSocketException
from websockets.sync.client import connect

from daemon.config import Config, Paths, load_config


class ClientError(Exception):
    """A message for the person at the terminal."""


class Client:
    def __init__(self, paths: Paths | None = None) -> None:
        self.paths = paths or Paths.from_env()
        self.config: Config = load_config(self.paths)
        try:
            self.token = self.paths.token.read_text().strip()
        except OSError as error:
            raise ClientError("sayld has never run here. Start it with: sayld") from error
        self.http = httpx.Client(
            base_url=f"http://{self.config.host}:{self.config.port}",
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=30,
        )

    def call(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self.http.request(method, path, **kwargs)
        except httpx.TransportError as error:
            raise ClientError("cannot reach sayld. Start it with: sayld") from error
        if response.status_code >= 400:
            raise ClientError(_detail(response))
        return response.json() if response.content else None

    def resolve(self, prefix: str) -> str:
        """Accept any unique start of an id, as `sayl ls` prints only the first 8 characters."""
        matches = [
            row["id"] for row in self.call("GET", "/downloads") if row["id"].startswith(prefix)
        ]
        if len(matches) == 1:
            return str(matches[0])
        if matches:
            raise ClientError(f"'{prefix}' matches {len(matches)} downloads; use more characters")
        raise ClientError(f"no download matches '{prefix}'")

    def events(self) -> Iterator[dict[str, Any]]:
        uri = f"ws://{self.config.host}:{self.config.port}/ws"
        subprotocol: Any = f"bearer.{self.token}"
        try:
            with connect(uri, subprotocols=[subprotocol]) as socket:
                for message in socket:
                    yield json.loads(message)
        except (OSError, WebSocketException) as error:
            if isinstance(error, ConnectionClosed):
                return
            raise ClientError("cannot reach sayld. Start it with: sayld") from error


def _detail(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail")
    except ValueError:
        detail = None
    return str(detail or f"sayld answered {response.status_code}")
