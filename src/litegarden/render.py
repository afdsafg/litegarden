"""Analysis and preview rendering: top-down, height and change-mask images.

Images carry orientation / coordinate ticks and match the JSON coordinates
exactly. They are analysis diagrams, not Minecraft texture renders.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from .terrain import TerrainAnalysis


def _to_image(grid: np.ndarray, cmap) -> Image.Image:
    h, w = grid.shape
    img = Image.new("RGB", (w, h))
    px = img.load()
    for x in range(h):
        for z in range(w):
            px[z, x] = cmap(grid[x, z])
    return img


def render_heightmap(analysis: TerrainAnalysis, out: Path) -> Path:
    """Top-down ground-height map with a coordinate axis."""
    g = analysis.ground_height
    valid = g >= 0
    lo = int(g[valid].min()) if valid.any() else 0
    hi = int(g[valid].max()) if valid.any() else 1
    span = max(1, hi - lo)

    def cmap(v):
        if v < 0:
            return (40, 40, 40)  # unknown
        t = (int(v) - lo) / span
        return (int(255 * t), int(255 * (1 - t)), 80)

    img = _to_image(g, cmap)
    _draw_axes(img, analysis.size_x, analysis.size_z)
    img.save(str(out))
    return out


def _draw_axes(img: Image.Image, size_x: int, size_z: int) -> None:
    d = ImageDraw.Draw(img)
    # origin marker + axis ticks every 8 cells
    for x in range(0, size_x, 8):
        d.line([(x, 0), (x, 2)], fill=(255, 255, 255))
    for z in range(0, size_z, 8):
        d.line([(0, z), (2, z)], fill=(255, 255, 255))
