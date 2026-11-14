"""Checksum verification of finished files.

A checksum is written `algo:hex` (md5, sha1, sha224, sha256, sha384, sha512; `sha-256` and
upper case are accepted too), or as bare hex, whose length picks the algorithm. Hashing runs
in a worker thread, so a large file never blocks the event loop.
"""

import asyncio
import hashlib
import re
from pathlib import Path

ALGORITHMS = ("md5", "sha1", "sha224", "sha256", "sha384", "sha512")
_BY_LENGTH = {32: "md5", 40: "sha1", 56: "sha224", 64: "sha256", 96: "sha384", 128: "sha512"}
_HEX = re.compile(r"[0-9a-f]+")
BLOCK = 1024 * 1024


def parse(checksum: str) -> tuple[str, str]:
    """Return (algorithm, lowercase hex). Raises ValueError for anything unusable."""
    text = checksum.strip()
    algo, digest = "", text
    for separator in (":", "="):  # sha256:ab… or aria2's sha-256=ab…
        if separator in text:
            algo, _, digest = text.partition(separator)
            break
    digest = digest.strip().lower()
    algo = algo.strip().lower().replace("-", "")
    if not _HEX.fullmatch(digest):
        raise ValueError("a checksum is hexadecimal, optionally after 'algo:'")
    if not algo:
        algo = _BY_LENGTH.get(len(digest), "")
        if not algo:
            raise ValueError("cannot tell the algorithm from the length; write it as algo:hex")
    if algo not in ALGORITHMS:
        raise ValueError(f"unsupported algorithm {algo!r}; use one of {', '.join(ALGORITHMS)}")
    if len(digest) != hashlib.new(algo).digest_size * 2:
        raise ValueError(f"a {algo} checksum has {hashlib.new(algo).digest_size * 2} hex digits")
    return algo, digest


def digest_file(path: Path, algo: str) -> str:
    hasher = hashlib.new(algo)
    with path.open("rb") as file:
        while block := file.read(BLOCK):
            hasher.update(block)
    return hasher.hexdigest()


async def digest(path: Path, algo: str) -> str:
    return await asyncio.to_thread(digest_file, path, algo)
