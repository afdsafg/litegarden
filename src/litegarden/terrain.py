"""Terrain analysis: height, water, slope, obstacles, candidate sites.

Distinguishes surface_height (visible top) from ground_height (verified
natural ground). Tree canopies, roofs, water surfaces and ground are never
conflated; unrecognised columns are marked conservative.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

from .scene import AIR, SceneSnapshot, Vec3

Vec2 = Tuple[int, int]

# Verified natural ground blocks (whitelist; extend only after validation).
_GROUND_BLOCKS = frozenset({
    "minecraft:grass_block", "minecraft:dirt", "minecraft:stone",
    "minecraft:sand", "minecraft:gravel", "minecraft:podzol",
    "minecraft:mycelium", "minecraft:coarse_dirt", "minecraft:rooted_dirt",
    "minecraft:clay", "minecraft:moss_block",
})
_WATER_BLOCKS = frozenset({"minecraft:water"})
# Blocks that are obstacles to construction but not ground.
_OBSTACLE_BLOCKS = frozenset({
    "minecraft:oak_log", "minecraft:birch_log", "minecraft:spruce_log",
    "minecraft:jungle_log", "minecraft:acacia_log", "minecraft:dark_oak_log",
    "minecraft:mangrove_log", "minecraft:cherry_log", "minecraft:pale_oak_log",
})


@dataclass
class TerrainAnalysis:
    """Grid analysis over the local (x,z) plane."""

    size_x: int
    size_z: int
    surface_height: np.ndarray  # (x,z) int: top non-air y (local), -1 if all air
    ground_height: np.ndarray  # (x,z) int: verified ground y (local), -1 if unknown
    water_mask: np.ndarray  # (x,z) bool
    obstacle_mask: np.ndarray  # (x,z) bool
    slope_map: np.ndarray  # (x,z) float: max |dy| to 4-neighbours
    headroom: np.ndarray  # (x,z) int: clear air above ground
    site_candidates: List[dict] = field(default_factory=list)
    anchors: Dict[str, Vec2] = field(default_factory=dict)
    zones: Dict[str, dict] = field(default_factory=dict)


def analyze(snapshot: SceneSnapshot) -> TerrainAnalysis:
    """Compute the terrain analysis grids from the baseline snapshot."""
    t = snapshot.transform
    sx, sy, sz = t.local_size
    surface = np.full((sx, sz), -1, dtype=np.int32)
    ground = np.full((sx, sz), -1, dtype=np.int32)
    water = np.zeros((sx, sz), dtype=bool)
    obstacle = np.zeros((sx, sz), dtype=bool)
    headroom = np.zeros((sx, sz), dtype=np.int32)

    for x in range(sx):
        for z in range(sz):
            col = [snapshot.block_at_local((x, y, z)) for y in range(sy)]
            top = -1
            for y in range(sy - 1, -1, -1):
                if col[y] != AIR:
                    top = y
                    break
            surface[x, z] = top
            water[x, z] = any(c in _WATER_BLOCKS for c in col)
            obstacle[x, z] = any(c in _OBSTACLE_BLOCKS for c in col)
            g = -1
            for y in range(sy - 1, -1, -1):
                if col[y] in _GROUND_BLOCKS and (y + 1 >= sy or col[y + 1] == AIR):
                    g = y
                    break
            ground[x, z] = g
            if g >= 0:
                hr = 0
                for y in range(g + 1, sy):
                    if col[y] == AIR:
                        hr += 1
                    else:
                        break
                headroom[x, z] = hr

    slope = np.zeros((sx, sz), dtype=np.float32)
    for x in range(sx):
        for z in range(sz):
            g = ground[x, z]
            if g < 0:
                slope[x, z] = np.inf
                continue
            best = 0
            for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx, nz = x + dx, z + dz
                if 0 <= nx < sx and 0 <= nz < sz and ground[nx, nz] >= 0:
                    best = max(best, abs(int(ground[nx, nz]) - g))
            slope[x, z] = best

    return TerrainAnalysis(
        size_x=sx, size_z=sz,
        surface_height=surface, ground_height=ground,
        water_mask=water, obstacle_mask=obstacle,
        slope_map=slope, headroom=headroom,
    )


def _cell_ok(a: TerrainAnalysis, x: int, z: int, min_headroom: int) -> bool:
    if not (0 <= x < a.size_x and 0 <= z < a.size_z):
        return False
    return (
        a.ground_height[x, z] >= 0
        and not a.water_mask[x, z]
        and not a.obstacle_mask[x, z]
        and a.headroom[x, z] >= min_headroom
    )


def find_sites(
    analysis: TerrainAnalysis,
    footprint: Vec2 = (5, 5),
    count: int = 8,
    min_headroom: int = 4,
    max_slope: int = 1,
    margin: int = 1,
) -> List[dict]:
    """Deterministically pick non-overlapping candidate build sites.

    A site qualifies when every cell under the footprint has verified ground,
    no water/obstacle, enough headroom, height spread <= max_slope, and the
    site fits inside the map with a margin. Candidates are ranked by flatness
    then by centrality, and greedily thinned to avoid overlap.
    """
    fx, fz = footprint
    a = analysis
    ranked: List[Tuple[float, int, int, int, int]] = []
    for ox in range(margin, a.size_x - fx - margin + 1):
        for oz in range(margin, a.size_z - fz - margin + 1):
            hs = []
            ok = True
            for dx in range(fx):
                for dz in range(fz):
                    if not _cell_ok(a, ox + dx, oz + dz, min_headroom):
                        ok = False
                        break
                if not ok:
                    break
            if not ok:
                continue
            for dx in range(fx):
                for dz in range(fz):
                    hs.append(int(a.ground_height[ox + dx, oz + dz]))
            spread = max(hs) - min(hs)
            if spread > max_slope:
                continue
            cx = ox + fx / 2
            cz = oz + fz / 2
            centrality = abs(cx - a.size_x / 2) + abs(cz - a.size_z / 2)
            ranked.append((spread, centrality, ox, oz, int(sum(hs) / len(hs))))

    ranked.sort(key=lambda r: (r[0], r[1], r[2], r[3]))
    chosen: List[Tuple[int, int]] = []
    for spread, centrality, ox, oz, mean_h in ranked:
        if len(chosen) >= count:
            break
        clash = any(
            abs(ox - cx) < fx + 1 and abs(oz - cz) < fz + 1 for cx, cz in chosen
        )
        if clash:
            continue
        chosen.append((ox, oz))

    sites = []
    for i, (ox, oz) in enumerate(chosen):
        hs = [
            int(a.ground_height[ox + dx, oz + dz])
            for dx in range(fx) for dz in range(fz)
        ]
        sites.append({
            "id": f"site_{i:02d}",
            "origin": [ox, oz],
            "footprint": [fx, fz],
            "mean_height": int(sum(hs) / len(hs)),
        })
    return sites


def find_anchors(analysis: TerrainAnalysis, max_anchors: int = 6) -> Dict[str, Vec2]:
    """Pick spread-out walkable border-entry anchors named entry_NN."""
    a = analysis
    cand: List[Vec2] = []
    for x in range(a.size_x):
        for z in (0, a.size_z - 1):
            if _cell_ok(a, x, z, 1):
                cand.append((x, z))
    for z in range(a.size_z):
        for x in (0, a.size_x - 1):
            if _cell_ok(a, x, z, 1):
                cand.append((x, z))
    if not cand:
        return {}
    # thin out: keep greedily far-apart candidates
    chosen: List[Vec2] = []
    for c in cand:
        if all(abs(c[0] - o[0]) + abs(c[1] - o[1]) > max(8, a.size_x // 6) for o in chosen):
            chosen.append(c)
        if len(chosen) >= max_anchors:
            break
    if not chosen:
        chosen = cand[:1]
    return {f"entry_{i:02d}": c for i, c in enumerate(chosen)}


def build_planning_index(
    analysis: TerrainAnalysis,
    footprint: Vec2 = (5, 5),
    site_count: int = 8,
) -> None:
    """Populate site_candidates, anchors and zones on the analysis object."""
    a = analysis
    a.site_candidates = find_sites(a, footprint=footprint, count=site_count)
    a.anchors = find_anchors(a)

    zones: Dict[str, dict] = {}
    for i, site in enumerate(a.site_candidates):
        ox, oz = site["origin"]
        fx, fz = site["footprint"]
        x0 = max(0, ox - 7)
        z0 = max(0, oz - 7)
        x1 = min(a.size_x - 1, ox + fx + 6)
        z1 = min(a.size_z - 1, oz + fz + 6)
        zones[f"plant_{i:02d}"] = {
            "id": f"plant_{i:02d}",
            "bbox": [x0, z0, x1, z1],
        }
    a.zones = zones
