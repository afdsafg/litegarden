"""connect_path: terrain-following pathfinding and paving (spec 8).

The Agent picks connection points and road style; the program solves the path
on the height/obstacle grid. First version allows at most 1 block height
difference between adjacent road cells, no water, no unknown areas, no
auto-bridges. Unsatisfiable constraints return no-solution; never carve hills.
"""
from __future__ import annotations

import heapq
from typing import Dict, List, Optional, Tuple

from ..scene import AIR, BlockChange, SceneSnapshot, Vec3

Vec2 = Tuple[int, int]


class PathError(ValueError):
    pass


def _neighbours(p: Vec2, sx: int, sz: int):
    x, z = p
    for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        nx, nz = x + dx, z + dz
        if 0 <= nx < sx and 0 <= nz < sz:
            yield (nx, nz)


def find_path(
    ground_height,
    water_mask,
    obstacle_mask,
    start: Vec2,
    goal: Vec2,
    max_step: int = 1,
) -> List[Vec2]:
    """A* over the ground-height grid.

    Cost = distance + slope penalty + turn penalty. A move is legal only if
    both cells have verified ground, are neither water nor obstacle, and the
    height difference is <= max_step. Raises PathError when unsatisfiable.
    """
    sx, sz = ground_height.shape

    def walkable(p: Vec2) -> bool:
        x, z = p
        return (
            ground_height[x, z] >= 0
            and not water_mask[x, z]
            and not obstacle_mask[x, z]
        )

    if not walkable(start):
        raise PathError(f"start {start} not walkable")
    if not walkable(goal):
        raise PathError(f"goal {goal} not walkable")

    def h(p: Vec2) -> int:
        return abs(p[0] - goal[0]) + abs(p[1] - goal[1])

    openq: List[Tuple[float, int, Vec2]] = [(h(start), 0, start)]
    came: Dict[Vec2, Optional[Vec2]] = {start: None}
    g: Dict[Vec2, float] = {start: 0.0}
    counter = 0

    while openq:
        _, _, cur = heapq.heappop(openq)
        if cur == goal:
            break
        for nb in _neighbours(cur, sx, sz):
            if not walkable(nb):
                continue
            dy = abs(int(ground_height[nb]) - int(ground_height[cur]))
            if dy > max_step:
                continue
            # cost: base 1 + slope penalty + turn penalty
            step = 1.0 + 2.0 * dy
            if came[cur] is not None:
                prev = came[cur]
                if (nb[0] - cur[0], nb[1] - cur[1]) != (cur[0] - prev[0], cur[1] - prev[1]):
                    step += 0.5  # turn penalty
            ng = g[cur] + step
            if ng < g.get(nb, float("inf")):
                g[nb] = ng
                came[nb] = cur
                counter += 1
                heapq.heappush(openq, (ng + h(nb), counter, nb))

    if goal not in came:
        raise PathError(f"no path from {start} to {goal} within max_step={max_step}")

    path = [goal]
    while came[path[-1]] is not None:
        path.append(came[path[-1]])
    path.reverse()
    return path



def _palette_parts(palette) -> tuple:
    """Accept either a legacy flat list or a structured {surface, edge, support}.

    Returns (surface_list, edge_block, support_block).
    """
    if isinstance(palette, dict):
        surface = list(palette.get("surface") or [])
        edge = palette.get("edge") or (surface[0] if surface else None)
        support = palette.get("support") or (surface[0] if surface else None)
        return surface, edge, support
    # legacy flat list
    return list(palette), palette[0], palette[0]


def pave_path(
    snapshot: SceneSnapshot,
    ground_height,
    path: List[Vec2],
    width: int,
    palette,
    op_id: str,
) -> List[BlockChange]:
    """Pave a solved path at the given width using a palette.

    The full road width is levelled to the centre cell's ground height and
    paved; edge cells use the palette's edge block for a defined border, and
    the cell directly beneath each road block is filled with the support block
    when it is air. Raises PathError if any road cell is out of bounds.
    """
    surface_list, edge_block, support_block = _palette_parts(palette)
    if not surface_list:
        raise PathError(f"{op_id}: empty palette")
    half = width // 2
    changes: List[BlockChange] = []
    rid = snapshot.region_id

    def emit(pos: Vec3, block: str) -> None:
        if not snapshot.contains_local(pos):
            raise PathError(f"{op_id}: {pos} out of bounds")
        before = snapshot.block_at_local(pos)
        if before != block:
            changes.append(BlockChange(rid, pos, before, block, op_id))

    for i, (cx, cz) in enumerate(path):
        gy = int(ground_height[cx, cz])
        surface = surface_list[i % len(surface_list)]
        # road direction at this cell (for edge orientation)
        if i + 1 < len(path):
            dirx, dirz = path[i + 1][0] - cx, path[i + 1][1] - cz
        elif i > 0:
            dirx, dirz = cx - path[i - 1][0], cz - path[i - 1][1]
        else:
            dirx, dirz = 1, 0
        for dx in range(-half, half + 1):
            for dz in range(-half, half + 1):
                x, z = cx + dx, cz + dz
                if not (0 <= x < ground_height.shape[0] and 0 <= z < ground_height.shape[1]):
                    raise PathError(f"{op_id}: road cell ({x},{z}) out of bounds")
                # edge = cells offset perpendicular to the road direction
                if dirx != 0:  # road runs along x -> edges are at dz extremes
                    is_edge = abs(dz) == half
                elif dirz != 0:  # road runs along z -> edges are at dx extremes
                    is_edge = abs(dx) == half
                else:
                    is_edge = abs(dx) == half or abs(dz) == half
                emit((x, gy, z), edge_block if (is_edge and width > 1) else surface)
                # light support: fill the cell below if it is air
                if gy - 1 >= 0 and snapshot.block_at_local((x, gy - 1, z)) == AIR:
                    emit((x, gy - 1, z), support_block)
    return changes
