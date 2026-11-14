"""Site grabber: crawl one site and list the files it links to, filtered by type and size.

A grab only finds files; the user picks which to download, and those go through /batch. The
crawl stays on the start URL's host, follows HTML pages breadth first up to `depth` links
away and `max_pages` pages, honours robots.txt, and keeps at most CONCURRENCY requests open.
Sizes come from HEAD requests, made only when a size filter is set.
"""

import asyncio
import contextlib
import logging
import posixpath
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from daemon.capture.detect import DOWNLOAD_EXTENSIONS
from daemon.models import new_id

log = logging.getLogger(__name__)

CONCURRENCY = 4
MAX_DEPTH = 5
MAX_PAGES = 2000
MAX_PAGE_BYTES = 5 * 1024 * 1024
KEEP_FINISHED = 20
USER_AGENT = "Sayl site grabber"
_LINK_ATTRIBUTES = {
    "a": "href",
    "area": "href",
    "img": "src",
    "source": "src",
    "video": "src",
    "audio": "src",
    "embed": "src",
    "iframe": "src",
    "frame": "src",
}
_PAGE_TYPES = ("text/html", "application/xhtml+xml")
# What a grab collects by default: what the capture proxy treats as a download, plus documents
# and media, which a browser would open instead. Images only when asked for.
DEFAULT_EXTENSIONS = frozenset(e.lstrip(".") for e in DOWNLOAD_EXTENSIONS) | {
    "pdf",
    "doc",
    "docx",
    "xls",
    "xlsx",
    "ppt",
    "pptx",
    "odt",
    "ods",
    "odp",
    "epub",
    "mp3",
    "flac",
    "ogg",
    "opus",
    "m4a",
    "wav",
    "mp4",
    "mkv",
    "webm",
    "avi",
    "mov",
}


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.base = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "base" and values.get("href"):
            self.base = values["href"] or ""
        wanted = _LINK_ATTRIBUTES.get(tag)
        if wanted and values.get(wanted):
            self.links.append(values[wanted] or "")


def links_in(html: str, page_url: str) -> list[str]:
    """Absolute http(s) links in a page, without fragments, in document order."""
    parser = _Links()
    with contextlib.suppress(AssertionError):  # html.parser can trip on broken markup.
        parser.feed(html)
    base = urljoin(page_url, parser.base) if parser.base else page_url
    found: list[str] = []
    for link in parser.links:
        absolute = urldefrag(urljoin(base, link.strip())).url
        if urlsplit(absolute).scheme in ("http", "https"):
            found.append(absolute)
    return list(dict.fromkeys(found))


def extension(url: str) -> str:
    return posixpath.splitext(urlsplit(url).path)[1].lower().lstrip(".")


@dataclass
class GrabOptions:
    url: str
    depth: int = 2
    extensions: tuple[str, ...] = ()  # Empty: every common download type.
    min_size: int = 0
    max_size: int = 0  # 0: no limit.
    max_pages: int = 200
    headers: dict[str, str] = field(default_factory=dict)

    def wanted(self, url: str) -> bool:
        kind = extension(url)
        if self.extensions:
            return kind in self.extensions
        return kind in DEFAULT_EXTENSIONS


@dataclass
class Found:
    url: str
    size: int | None  # None when the server did not say.
    page: str  # The page that linked to it.


@dataclass
class Grab:
    options: GrabOptions
    id: str = field(default_factory=new_id)
    status: str = "running"  # running, done, failed, canceled
    error: str = ""
    pages: int = 0
    files: list[Found] = field(default_factory=list)
    skipped_robots: int = 0
    started: float = field(default_factory=time.time)
    task: asyncio.Task[None] | None = None


class Crawler:
    def __init__(self, grab: Grab, client: httpx.AsyncClient) -> None:
        self.grab = grab
        self.options = grab.options
        self.client = client
        self.host = (urlsplit(self.options.url).hostname or "").lower()
        self.robots: RobotFileParser | None = None
        self.seen: set[str] = set()
        self.slots = asyncio.Semaphore(CONCURRENCY)

    def _same_site(self, url: str) -> bool:
        return (urlsplit(url).hostname or "").lower() == self.host

    async def _load_robots(self) -> None:
        parts = urlsplit(self.options.url)
        parser = RobotFileParser()
        try:
            response = await self.client.get(f"{parts.scheme}://{parts.netloc}/robots.txt")
        except httpx.HTTPError:
            return  # Unreachable robots.txt: nothing is disallowed.
        if response.status_code >= 400:
            return
        parser.parse(response.text.splitlines())
        self.robots = parser

    def _allowed(self, url: str) -> bool:
        if self.robots is None or self.robots.can_fetch(USER_AGENT, url):
            return True
        self.grab.skipped_robots += 1
        return False

    async def _page(self, url: str) -> list[str]:
        """The links on a page, or [] when it is not HTML or cannot be read."""
        async with self.slots, self.client.stream("GET", url) as response:
            kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
            if response.status_code >= 400 or kind not in _PAGE_TYPES:
                return []
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > MAX_PAGE_BYTES:
                    break
            final = str(response.url)
            encoding = response.encoding or "utf-8"
        self.grab.pages += 1
        return links_in(body.decode(encoding, errors="replace"), final)

    async def _size(self, url: str) -> int | None:
        async with self.slots:
            try:
                response = await self.client.head(url)
            except httpx.HTTPError:
                return None
        length = response.headers.get("content-length", "")
        return int(length) if response.status_code < 400 and length.isdigit() else None

    def _fits(self, size: int | None) -> bool:
        if size is None:
            return self.options.min_size == 0  # Unknown sizes pass unless a minimum is set.
        if size < self.options.min_size:
            return False
        return not (self.options.max_size and size > self.options.max_size)

    async def run(self) -> None:
        await self._load_robots()
        frontier = [self.options.url]
        self.seen.add(self.options.url)
        candidates: dict[str, str] = {}  # file URL -> the page that linked it
        for level in range(self.options.depth + 1):
            budget = self.options.max_pages - self.grab.pages
            pages = [url for url in frontier if self._allowed(url)][: max(0, budget)]
            if not pages:
                break
            results = await asyncio.gather(
                *(self._page(url) for url in pages), return_exceptions=True
            )
            frontier = []
            for page, links in zip(pages, results, strict=True):
                if isinstance(links, BaseException):
                    if isinstance(links, httpx.HTTPError):
                        continue
                    raise links
                for link in links:
                    if link in self.seen or not self._same_site(link):
                        continue
                    self.seen.add(link)
                    if self.options.wanted(link):
                        candidates.setdefault(link, page)
                    elif level < self.options.depth:
                        frontier.append(link)
        await self._measure(candidates)

    async def _measure(self, candidates: dict[str, str]) -> None:
        if not (self.options.min_size or self.options.max_size):
            self.grab.files = [Found(url, None, page) for url, page in candidates.items()]
            return
        urls = list(candidates)
        sizes = await asyncio.gather(*(self._size(url) for url in urls))
        self.grab.files = [
            Found(url, size, candidates[url])
            for url, size in zip(urls, sizes, strict=True)
            if self._fits(size)
        ]


class Grabber:
    """Grabs in progress and the last few finished ones, in memory."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self.client = client or httpx.AsyncClient(
            follow_redirects=True, timeout=20, headers={"User-Agent": USER_AGENT}
        )
        self.grabs: dict[str, Grab] = {}
        self.proxy: str | None = None  # Set with the outbound proxy.

    def start(self, options: GrabOptions) -> Grab:
        options.depth = max(0, min(options.depth, MAX_DEPTH))
        options.max_pages = max(1, min(options.max_pages, MAX_PAGES))
        grab = Grab(options)
        self.grabs[grab.id] = grab
        grab.task = asyncio.create_task(self._run(grab))
        self._forget_old()
        return grab

    async def _run(self, grab: Grab) -> None:
        crawler = Crawler(grab, self.client)
        try:
            await self._with_headers(crawler, grab.options.headers)
            grab.status = "done"
        except asyncio.CancelledError:
            grab.status = "canceled"
            raise
        except (httpx.HTTPError, OSError, ValueError) as error:
            grab.status, grab.error = "failed", str(error)
            log.info("site grab failed: %s", error)

    async def _with_headers(self, crawler: Crawler, headers: dict[str, str]) -> None:
        if not headers:
            await crawler.run()
            return
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=20,
            headers={"User-Agent": USER_AGENT, **headers},
            proxy=self.proxy,
        ) as client:
            crawler.client = client
            await crawler.run()

    def _forget_old(self) -> None:
        finished = [g for g in self.grabs.values() if g.status != "running"]
        for grab in sorted(finished, key=lambda g: g.started)[:-KEEP_FINISHED]:
            self.grabs.pop(grab.id, None)

    async def cancel(self, grab_id: str) -> bool:
        grab = self.grabs.get(grab_id)
        if grab is None:
            return False
        if grab.task is not None and not grab.task.done():
            grab.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await grab.task
        return True

    async def stop(self) -> None:
        for grab_id in list(self.grabs):
            await self.cancel(grab_id)
        await self.client.aclose()
