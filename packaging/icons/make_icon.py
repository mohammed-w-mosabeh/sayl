"""Draws the Sayl icon (a down arrow over a wave) as a 1024 px PNG, with no image libraries.

Run: python3 packaging/icons/make_icon.py desktop/src-tauri/app-icon.png
Then: cd desktop && pnpm tauri icon src-tauri/app-icon.png
"""

import math
import struct
import sys
import zlib
from pathlib import Path

SIZE = 1024
SAMPLES = 4  # Supersampling per axis, for smooth edges.
BLUE_TOP = (37, 136, 214)
BLUE_BOTTOM = (20, 92, 168)
WHITE = (255, 255, 255)


def in_rounded_square(x: float, y: float) -> bool:
    margin, radius = 64, 200
    lo, hi = margin + radius, SIZE - margin - radius
    cx, cy = min(max(x, lo), hi), min(max(y, lo), hi)
    return (x - cx) ** 2 + (y - cy) ** 2 <= radius**2


def in_arrow(x: float, y: float) -> bool:
    shaft = 452 <= x <= 572 and 210 <= y <= 520
    head = 520 <= y <= 700 and abs(x - 512) <= (700 - y) * 1.15
    return shaft or head


def in_wave(x: float, y: float) -> bool:
    if not 180 <= x <= 844:
        return False
    centre = 790 + 34 * math.sin((x - 180) / 664 * 2 * math.pi)
    return abs(y - centre) <= 38


def pixel(px: int, py: int) -> tuple[int, int, int, int]:
    totals = [0.0, 0.0, 0.0, 0.0]
    for sy in range(SAMPLES):
        for sx in range(SAMPLES):
            x = px + (sx + 0.5) / SAMPLES
            y = py + (sy + 0.5) / SAMPLES
            if not in_rounded_square(x, y):
                continue
            if in_arrow(x, y) or in_wave(x, y):
                colour = WHITE
            else:
                t = y / SIZE
                colour = tuple(
                    round(a + (b - a) * t) for a, b in zip(BLUE_TOP, BLUE_BOTTOM, strict=True)
                )
            for i in range(3):
                totals[i] += colour[i]
            totals[3] += 255
    count = SAMPLES * SAMPLES
    alpha = totals[3] / count
    if alpha == 0:
        return (0, 0, 0, 0)
    covered = totals[3] / 255
    return (*(round(totals[i] / covered) for i in range(3)), round(alpha))  # type: ignore[return-value]


def png(path: str) -> None:
    rows = bytearray()
    for py in range(SIZE):
        rows.append(0)
        for px in range(SIZE):
            rows.extend(pixel(px, py))

    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = struct.pack(">I", zlib.crc32(kind + data))
        return struct.pack(">I", len(data)) + kind + data + crc

    header = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0)
    with Path(path).open("wb") as out:
        out.write(b"\x89PNG\r\n\x1a\n")
        out.write(chunk(b"IHDR", header))
        out.write(chunk(b"IDAT", zlib.compress(bytes(rows), 9)))
        out.write(chunk(b"IEND", b""))


if __name__ == "__main__":
    png(sys.argv[1])
