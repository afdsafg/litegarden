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

# Verified natural ground blocks (whitelist; extend only after validation).
_GROUND_BLOCKS = frozenset({
    "minecraft:grass_block", "minecraft:dirt", "minecraft:stone",
    "minecraft:sand", "minecraft:gravel", "minecraft:podzol",
    "minecraft:mycelium", "minecraft:coarse_dirt", "minecraft:rooted_dirt",
})
_WATER_BLOCKS = frozenset({"minecraft:water"})
# Blocks that are obstacles to construction but not ground.
_OBSTACLE_BLOCKS = frozenset({
    "minecraft:oak_log", "minecraft:birch_log", "minecraft:spruce_log",
    "minecraft:jungle_log", "minecraft:acacia_log", "minecraft:dark_oak_log",
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
    anchors: Dict[str, Vec3] = field(default_factory=dict)


def analyze(snapshot: SceneSnapshot) -> TerrainAnalysis:
    """Compute the terrain analysis grids from the baseline snapshot."""
    t = snapshot.transform
    sx, sy, sz = t.local_size
    surface = np.full((sx, sz), -1, dtype=np.int32)
    ground = np.full((sx, sz), -1, dtype=np.int32)
    water = np.zeros((sx, sz), dtype=bool)
    obstacle = np.zeros((sx, sz), dtype=bool)
    headroom = np.zeros((sx, sz), dtype=np.int32)

    # Cache the column block ids top-to-bottom per (x,z).
    for x in range(sx):
        for z in range(sz):
            col = [snapshot.block_at_local((x, y, z)) for y in range(sy)]
            # surface: top non-air
            top = -1
            for y in range(sy - 1, -1, -1):
                if col[y] != AIR:
                    top = y
                    break
            surface[x, z] = top
            # water / obstacle flags anywhere in the column
            water[x, z] = any(c in _WATER_BLOCKS for c in col)
            obstacle[x, z] = any(c in _OBSTACLE_BLOCKS for c in col)
            # ground: highest verified ground block with air above
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

    # slope: max abs height difference to 4-neighbours on ground height
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
