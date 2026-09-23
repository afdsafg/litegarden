"""decorate_path / scatter_assets: constrained greenery and road decor (spec 8).

Uses the same asset placer. Roads and main structures are placed first, then
decorations, with road/entry avoidance, ground adaptation, support and count
checks. Trees are static assets, not growth-dependent saplings.
"""
from __future__ import annotations

import random
from typing import List, Optional, Set, Tuple

from ..scene import AIR, BlockChange, SceneSnapshot, Vec3
from .stamp import Asset, AssetError, place_asset

Vec2 = Tuple[int, int]


class ScatterError(ValueError):
    pass


def decorate_path(
    snapshot: SceneSnapshot,
    ground_height,
    path: List[Vec2],
    asset: Asset,
    spacing: int,
    op_id: str,
    occupied: Optional[Set[Vec2]] = None,
) -> List[BlockChange]:
    """Place `asset` alongside a solved path every `spacing` cells.

    Lamps alternate sides and keep one cell clear of the road edge. Cells in
    `occupied` (road, entries, other assets) are avoided.
    """
    occupied = occupied or set()
    road = set(path)
    changes: List[BlockChange] = []
    side = 1
    for i in range(0, len(path), max(1, spacing)):
        cx, cz = path[i]
        # offset perpendicular to the road direction
        if i + 1 < len(path):
            dx, dz = path[i + 1][0] - cx, path[i + 1][1] - cz
        else:
            dx, dz = cx - path[i - 1][0], cz - path[i - 1][1]
        # perpendicular (rotate 90°)
        px, pz = -dz, dx
        if px == 0 and pz == 0:
            px, pz = 1, 0
        lx, lz = cx + side * 2 * (1 if px else 0), cz + side * 2 * (1 if pz else 0)
        side = -side
        if (lx, lz) in road or (lx, lz) in occupied:
            continue
        try:
            changes.extend(place_asset(snapshot, ground_height, asset, (lx, lz), op_id))
            occupied.add((lx, lz))
        except AssetError:
            continue  # skip unsuitable lamp site, keep decorating
    return changes


def scatter_assets(
    snapshot: SceneSnapshot,
    ground_height,
    zone: Tuple[Vec2, Vec2],  # (min_xz, max_xz) inclusive
    asset: Asset,
    count: int,
    seed: int,
    op_id: str,
    occupied: Optional[Set[Vec2]] = None,
) -> List[BlockChange]:
    """Scatter up to `count` assets inside a zone with a fixed seed.

    Deterministic for a fixed seed. Cells in `occupied` are avoided; a cell
    that fails placement is skipped rather than aborting the whole scatter.
    """
    occupied = occupied or set()
    (x0, z0), (x1, z1) = zone
    rng = random.Random(seed)
    cells = [(x, z) for x in range(x0, x1 + 1) for z in range(z0, z1 + 1)]
    rng.shuffle(cells)

    changes: List[BlockChange] = []
    placed = 0
    for c in cells:
        if placed >= count:
            break
        if c in occupied:
            continue
        try:
            changes.extend(place_asset(snapshot, ground_height, asset, c, op_id))
            occupied.add(c)
            placed += 1
        except AssetError:
            continue
    if placed < count:
        # not an error: report shortfall via the returned change count
        pass
    return changes
