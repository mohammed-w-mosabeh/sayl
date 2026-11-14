"""Applies the outbound_proxy setting to every engine and HTTP client the daemon owns."""

import logging

import httpx

from daemon.engines.aria2 import Aria2Engine
from daemon.engines.hybrid import HybridEngine
from daemon.engines.ytdlp import YtDlpEngine
from daemon.grabber import USER_AGENT, Grabber
from daemon.proxy import ProxyUrl, Socks5Bridge, parse
from daemon.ytdlp_update import Updater

log = logging.getLogger(__name__)


class OutboundProxy:
    def __init__(
        self,
        aria2: Aria2Engine,
        ytdlp: YtDlpEngine,
        hybrid: HybridEngine,
        grabber: Grabber | None,
    ) -> None:
        self.aria2 = aria2
        self.ytdlp = ytdlp
        self.hybrid = hybrid
        self.grabber = grabber
        self.current: ProxyUrl | None = None
        self.bridge: Socks5Bridge | None = None
        self.updater: Updater | None = None
        # Replaced clients may still carry a running download; they close when sayld stops.
        self.retired: list[httpx.AsyncClient] = []

    async def apply(self, text: str) -> None:
        """Switch every connection to this proxy, or to direct for "". ProxyError if invalid."""
        proxy = parse(text)
        if proxy == self.current:
            return
        old_bridge = self.bridge
        self.bridge = None
        aria2_url = ""
        if proxy is not None and proxy.socks:
            self.bridge = Socks5Bridge(proxy)
            await self.bridge.start()
            aria2_url = self.bridge.url
        elif proxy is not None:
            aria2_url = proxy.url()
        await self.aria2.set_proxy(aria2_url)
        if old_bridge is not None:
            await old_bridge.stop()
        url = proxy.url() if proxy is not None else None
        self.ytdlp.proxy = url or ""
        if self.updater is not None:
            self.updater.proxy = url
        self._replace_clients(url)
        self.current = proxy
        log.info("outbound proxy: %s", proxy or "direct")

    def _replace_clients(self, url: str | None) -> None:
        self.retired += [self.hybrid.client, self.aria2.client]
        self.hybrid.client = httpx.AsyncClient(follow_redirects=False, timeout=30, proxy=url)
        self.aria2.client = httpx.AsyncClient(timeout=30, proxy=url)
        if self.grabber is not None:
            self.retired.append(self.grabber.client)
            self.grabber.client = httpx.AsyncClient(
                follow_redirects=True, timeout=20, headers={"User-Agent": USER_AGENT}, proxy=url
            )
            self.grabber.proxy = url

    async def stop(self) -> None:
        if self.bridge is not None:
            await self.bridge.stop()
        for client in self.retired:
            await client.aclose()
        self.retired.clear()
