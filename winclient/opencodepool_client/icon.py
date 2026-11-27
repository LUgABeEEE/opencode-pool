"""用纯标准库生成 .ico（托盘与窗口图标）。

为什么自己画：客户端要求"双击就能跑"，不该因为少一个 Pillow 就起不来；
而 ICO 格式本身足够简单 —— 多帧目录 + 每帧一张 PNG，PNG 用 zlib 手工封。

图案：深色圆角底 + 一个缺口在右上的进度环（三档额度的隐喻）+ 环心一个点。
"""
from __future__ import annotations

import os
import struct
import zlib

SIZES = (16, 22, 24, 32, 48, 64, 128, 256)
SUPERSAMPLE = 3

BG_TOP = (13, 17, 23)
BG_BOTTOM = (22, 30, 41)
RING = (53, 208, 216)
RING_DIM = (43, 111, 176)
DOT = (228, 240, 255)
EDGE = (74, 168, 255)


def _lerp(a, b, t):
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))


def _rounded_box(x, y, w, h, radius) -> bool:
    """点 (x,y) 是否落在 (0,0,w,h) 的圆角矩形里。"""
    if x < 0 or y < 0 or x > w or y > h:
        return False
    cx = min(max(x, radius), w - radius)
    cy = min(max(y, radius), h - radius)
    dx, dy = x - cx, y - cy
    return dx * dx + dy * dy <= radius * radius


def _ring_hit(x, y, cx, cy, radius, width) -> bool:
    dx, dy = x - cx, y - cy
    dist2 = dx * dx + dy * dy
    outer = (radius + width / 2) ** 2
    inner = (radius - width / 2) ** 2
    return inner <= dist2 <= outer


def _arc_angle(x, y, cx, cy) -> float:
    """返回以 12 点为 0°、顺时针增长的百分比位置（0–1）。"""
    import math

    angle = math.degrees(math.atan2(y - cy, x - cx))      # -180..180，0=东
    clockwise = (angle + 90.0) % 360.0                    # 0 = 北，顺时针递增
    return clockwise / 360.0


def _shade(size: int) -> list[list[tuple[int, int, int, int]]]:
    """渲染一帧：返回 size×size 的 RGBA 像素表（含抗锯齿）。"""
    n = size
    ss = SUPERSAMPLE
    coverage = [[0.0] * n for _ in range(n)]
    color_acc = [[[0.0, 0.0, 0.0, 0.0] for _ in range(n)] for _ in range(n)]

    radius = n * 0.24
    cx = cy = n / 2.0
    ring_radius = n * 0.30
    ring_width = max(1.0, n * 0.115)
    dot_radius = max(0.9, n * 0.075)

    for py in range(n):
        for px in range(n):
            acc = [0.0, 0.0, 0.0, 0.0]
            hits = 0
            for sy in range(ss):
                for sx in range(ss):
                    x = px + (sx + 0.5) / ss
                    y = py + (sy + 0.5) / ss
                    if not _rounded_box(x, y, n, n, radius):
                        continue
                    t = y / n
                    # 底：上深下浅的竖渐变 + 右下角一点高光
                    base = _lerp(BG_TOP, BG_BOTTOM, t)
                    if _ring_hit(x, y, cx, cy, ring_radius, ring_width):
                        pos = _arc_angle(x, y, cx, cy)
                        if pos <= 0.78:                       # 缺口留在右上
                            base = RING if pos <= 0.60 else _lerp(RING, RING_DIM, 0.55)
                        else:
                            base = _lerp(base, RING_DIM, 0.25)
                    dx, dy = x - cx, y - cy
                    if dx * dx + dy * dy <= dot_radius * dot_radius:
                        base = DOT
                    # 外框一圈细边，小尺寸下更像"有边框的图标"
                    if not _rounded_box(x, y, n - 1, n - 1, radius - 0.5):
                        base = _lerp(base, EDGE, 0.55)
                    acc[0] += base[0]
                    acc[1] += base[1]
                    acc[2] += base[2]
                    acc[3] += 255.0
                    hits += 1
            total = ss * ss
            if hits:
                coverage[py][px] = hits / total
                color_acc[py][px] = [acc[i] / hits for i in range(4)]
    pixels = []
    for py in range(n):
        row = []
        for px in range(n):
            alpha = coverage[py][px]
            if alpha <= 0.004:
                row.append((0, 0, 0, 0))
                continue
            r, g, b, _ = color_acc[py][px]
            row.append((int(r), int(g), int(b), int(round(alpha * 255))))
        pixels.append(row)
    return pixels


def _png(size: int, pixels) -> bytes:
    raw = bytearray()
    for row in pixels:
        raw.append(0)  # filter type 0
        for r, g, b, a in row:
            raw += bytes((r, g, b, a))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


def build_ico(path: str) -> str:
    """把多帧 PNG 封进一个 .ico；返回实际路径。失败时返回空串（界面不崩）。"""
    try:
        frames = [(size, _png(size, _shade(size))) for size in SIZES]
        header = struct.pack("<HHH", 0, 1, len(frames))
        offset = 6 + 16 * len(frames)
        entries = bytearray()
        blob = bytearray()
        for size, data in frames:
            dim = 0 if size >= 256 else size
            entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
            offset += len(data)
            blob += data
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "wb") as f:
            f.write(header + bytes(entries) + bytes(blob))
        return path
    except (OSError, ValueError):
        return ""


def ensure_icon(path: str) -> str:
    """已存在且非空就直接用（画一次约 30–80ms，不值得每次启动都花）。"""
    try:
        if os.path.isfile(path) and os.path.getsize(path) > 1024:
            return path
    except OSError:
        pass
    return build_ico(path)


if __name__ == "__main__":  # 手工生成一张看看
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else "app.ico"
    print(build_ico(target) or "failed")
