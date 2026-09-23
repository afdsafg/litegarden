"""Analysis and preview rendering: top-down, height and change-mask images.

Images carry orientation / coordinate ticks and match the JSON coordinates
exactly. They are analysis diagrams, not Minecraft texture renders.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from .terrain import TerrainAnalysis

_SCALE = 5  # upscale factor so 1 block = 5px for readability


def _to_image(grid: np.ndarray, cmap) -> Image.Image:
    h, w = grid.shape
    img = Image.new("RGB", (w, h))
    px = img.load()
    for x in range(h):
        for z in range(w):
            px[z, x] = cmap(grid[x, z])
    return img


def _draw_axes(img: Image.Image, size_x: int, size_z: int) -> None:
    d = ImageDraw.Draw(img)
    for x in range(0, size_x, 8):
        d.line([(x, 0), (x, 2)], fill=(255, 255, 255))
    for z in range(0, size_z, 8):
        d.line([(0, z), (2, z)], fill=(255, 255, 255))


def _upscale(img: Image.Image) -> Image.Image:
    return img.resize((img.width * _SCALE, img.height * _SCALE), Image.NEAREST)


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
    _upscale(img).save(str(out))
    return out


def render_water(analysis: TerrainAnalysis, out: Path) -> Path:
    def cmap(v):
        return (30, 90, 220) if v else (18, 18, 24)
    img = _to_image(analysis.water_mask.astype(np.int32), cmap)
    _draw_axes(img, analysis.size_x, analysis.size_z)
    _upscale(img).save(str(out))
    return out


def render_obstacles(analysis: TerrainAnalysis, out: Path) -> Path:
    def cmap(v):
        return (220, 120, 30) if v else (18, 18, 24)
    img = _to_image(analysis.obstacle_mask.astype(np.int32), cmap)
    _draw_axes(img, analysis.size_x, analysis.size_z)
    _upscale(img).save(str(out))
    return out


def render_slope(analysis: TerrainAnalysis, out: Path) -> Path:
    s = analysis.slope_map
    def cmap(v):
        if not np.isfinite(v):
            return (40, 40, 40)
        t = min(1.0, float(v) / 3.0)
        return (int(255 * t), int(200 * (1 - t)), 60)
    img = _to_image(s, cmap)
    _draw_axes(img, analysis.size_x, analysis.size_z)
    _upscale(img).save(str(out))
    return out


def render_sites(analysis: TerrainAnalysis, out: Path) -> Path:
    """Ground map with site footprints, anchors and zones overlaid."""
    g = analysis.ground_height
    valid = g >= 0
    lo = int(g[valid].min()) if valid.any() else 0
    hi = int(g[valid].max()) if valid.any() else 1
    span = max(1, hi - lo)

    def base(v):
        if v < 0:
            return (40, 40, 40)
        t = (int(v) - lo) / span
        return (int(120 + 100 * t), int(120 + 100 * (1 - t)), 90)

    img = _to_image(g, base).convert("RGB")
    d = ImageDraw.Draw(img)
    # zones (green outline)
    for z in analysis.zones.values():
        x0, z0, x1, z1 = z["bbox"]
        d.rectangle([z0, x0, z1, x1], outline=(60, 200, 90))
    # sites (red filled footprint)
    for s in analysis.site_candidates:
        ox, oz = s["origin"]
        fx, fz = s["footprint"]
        d.rectangle([oz, ox, oz + fz - 1, ox + fx - 1], outline=(230, 60, 60), width=1)
    # anchors (blue dots)
    for ax, az in analysis.anchors.values():
        d.ellipse([az - 1, ax - 1, az + 1, ax + 1], fill=(60, 120, 255))
    _draw_axes(img, analysis.size_x, analysis.size_z)
    _upscale(img).save(str(out))
    return out


def render_all(analysis: TerrainAnalysis, out_dir: Path) -> dict:
    """Render the full analysis pack; returns {name: path}."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    return {
        "heightmap": render_heightmap(analysis, out_dir / "heightmap.png"),
        "water": render_water(analysis, out_dir / "water.png"),
        "obstacles": render_obstacles(analysis, out_dir / "obstacles.png"),
        "slope": render_slope(analysis, out_dir / "slope.png"),
        "sites": render_sites(analysis, out_dir / "sites.png"),
    }
