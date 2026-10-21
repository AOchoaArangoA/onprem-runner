"""Generate the app icons (PNG, ICO, tray) with the standard library only.

Run:  python desktop/assets/make_icons.py
The artwork is the logo of the sidebar: three nodes joined by smooth connectors on a rounded tile.
"""

from __future__ import annotations

import math
import struct
import zlib
from itertools import pairwise
from pathlib import Path

HERE = Path(__file__).parent
ACCENT_TOP = (107, 140, 255)
ACCENT_BOTTOM = (66, 99, 235)
WHITE = (255, 255, 255)
SAMPLES = 3  # SAMPLES x SAMPLES supersampling for smooth edges


def bezier(p0, p1, p2, p3, steps=40):
    points = []
    for i in range(steps + 1):
        t = i / steps
        u = 1 - t
        points.append(
            (
                u**3 * p0[0] + 3 * u**2 * t * p1[0] + 3 * u * t**2 * p2[0] + t**3 * p3[0],
                u**3 * p0[1] + 3 * u**2 * t * p1[1] + 3 * u * t**2 * p2[1] + t**3 * p3[1],
            )
        )
    return points


LEFT = (0.27, 0.5)
UPPER = (0.73, 0.29)
LOWER = (0.73, 0.71)
CURVES = [
    bezier(LEFT, (0.5, 0.5), (0.5, 0.29), UPPER),
    bezier(LEFT, (0.5, 0.5), (0.5, 0.71), LOWER),
]
NODES = [(LEFT, 0.105), (UPPER, 0.085), (LOWER, 0.085)]
STROKE = 0.028  # half width of the connectors
RADIUS = 0.22  # corner radius of the tile


def seg_distance(px, py, a, b):
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def in_tile(x, y):
    """Inside the rounded square covering the unit square."""
    cx = min(max(x, RADIUS), 1 - RADIUS)
    cy = min(max(y, RADIUS), 1 - RADIUS)
    return math.hypot(x - cx, y - cy) <= RADIUS


def is_white(x, y):
    for (nx, ny), r in NODES:
        if math.hypot(x - nx, y - ny) <= r:
            return True
    for curve in CURVES:
        for a, b in pairwise(curve):
            if seg_distance(x, y, a, b) <= STROKE:
                return True
    return False


def render(size: int, transparent_tile: bool = False) -> bytes:
    rows = []
    for j in range(size):
        row = bytearray()
        for i in range(size):
            r = g = b = a = 0
            for sj in range(SAMPLES):
                for si in range(SAMPLES):
                    x = (i + (si + 0.5) / SAMPLES) / size
                    y = (j + (sj + 0.5) / SAMPLES) / size
                    if not in_tile(x, y):
                        continue
                    if is_white(x, y):
                        color = WHITE
                    else:
                        mix = y
                        color = tuple(
                            round(ACCENT_TOP[k] * (1 - mix) + ACCENT_BOTTOM[k] * mix)
                            for k in range(3)
                        )
                    r += color[0]
                    g += color[1]
                    b += color[2]
                    a += 255
            covered = a // 255
            if covered:
                row += bytes((r // covered, g // covered, b // covered, a // (SAMPLES * SAMPLES)))
            else:
                row += bytes((0, 0, 0, 0))
        rows.append(bytes(row))
    return png(size, rows)


def png(size: int, rows: list[bytes]) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return (
            struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + row for row in rows)
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def ico(images: dict[int, bytes]) -> bytes:
    entries = b""
    blobs = b""
    offset = 6 + 16 * len(images)
    for size, data in sorted(images.items()):
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset)
        blobs += data
        offset += len(data)
    return struct.pack("<HHH", 0, 1, len(images)) + entries + blobs


def main() -> None:
    sizes = [16, 24, 32, 48, 64, 128, 256]
    images = {size: render(size) for size in sizes}
    (HERE / "icon.ico").write_bytes(ico(images))
    (HERE / "icon.png").write_bytes(render(512))
    (HERE / "tray.png").write_bytes(images[32])
    print("wrote icon.ico, icon.png, tray.png")


if __name__ == "__main__":
    main()
