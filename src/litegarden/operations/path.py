"""connect_path: terrain-following pathfinding and paving (spec 8).

The Agent picks connection points and road style; the program solves the path
on the height/obstacle grid.

Routing keeps ``max_step = 1`` as a *geometric* constraint only. It is never
presented as "walkable without jumping": that question is answered by the
``walk_no_jump_v1`` profile in :mod:`litegarden.traversal`, which the compiler
runs on the finished candidate.

Paving builds a level road surface across the full road width:

- the road level of each cross-section is the highest cover-tolerant buildable
  ground in that cross-section, so no solid terrain is ever carved away;
- lower columns get a support fill down to their own ground, bounded by
  ``max_fill_depth``; deeper columns are skipped and reported instead of being
  bridged;
- known non-colliding cover (leaf litter, short grass) inside the walk volume
  is requested for clearance - only removable blocks are ever cleared;
- a one-block longitudinal step gets a slab transition so every rise is at most
  half a block, which is what makes the road walkable without jumping.

Blocks that cannot be touched (tree canopy, unknown modded blocks) are never
overwritten: the affected cell is skipped and reported with its coordinates.
"""
from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from ..blocks import COVER_BLOCKS
from ..scene import AIR, BlockChange, SceneSnapshot, Vec3

Vec2 = Tuple[int, int]

# Movement directions, used to keep the search state directional so a turn
# penalty yields the same optimum as the declared cost model.
_DIRS: Tuple[Vec2, ...] = ((1, 0), (-1, 0), (0, 1), (0, -1))


class PathError(ValueError):
    pass


def _neighbours(p: Vec2, sx: int, sz: int):
    x, z = p
    for dx, dz in _DIRS:
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
    *,
    build_height=None,
    width: int = 1,
    cut_cost: float = 0.0,
    fill_cost: float = 0.0,
    slope_cost: float = 2.0,
    turn_cost: float = 0.5,
    max_fill_depth: int = 3,
) -> List[Vec2]:
    """A* over the ground-height grid.

    Legal only between cells with verified ground that are neither water nor
    obstacle and whose height difference is ``<= max_step``. The cost is

        cost = length + slope_cost*|dy| + turn_cost*turn
             + cut_cost*estimated_cut + fill_cost*estimated_fill

    All weights are non-negative and come from read-only config. The search
    state includes the direction of entry, so a non-zero turn cost really is
    the optimum of the declared cost model rather than a collapsed
    position-only approximation. The cut/fill terms are *estimates* computed
    over the road width; the real de-duplicated cut/fill is reported after
    merging the net patch. Raises PathError when unsatisfiable.
    """
    sx, sz = ground_height.shape
    est_h = build_height if build_height is not None else ground_height

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

    half = width // 2

    def estimate(p: Vec2, prev: Optional[Vec2]) -> Tuple[float, float]:
        """(cut, fill) estimate for a road of `width` centred on this cell."""
        x, z = p
        cells = []
        for dx in range(-half, half + 1):
            for dz in range(-half, half + 1):
                cx, cz = x + dx, z + dz
                if not (0 <= cx < sx and 0 <= cz < sz):
                    return (float("inf"), float("inf"))
                cells.append((cx, cz))
        heights = [int(est_h[cx, cz]) for cx, cz in cells]
        buildable = [h for h in heights if h >= 0]
        if not buildable:
            return (float("inf"), float("inf"))
        level = max(buildable)
        fill = sum(level - h for h in buildable)
        # the road block replaces the surface ground block itself: no cut
        cut = 0
        if prev is not None:
            px, pz = prev
            prev_level = int(est_h[px, pz])
            if prev_level >= 0:
                diff = level - prev_level
                if diff > 0:
                    # a one-block rise needs a transition block, counted as fill
                    fill += diff
                elif diff < 0:
                    cut = abs(diff)
        if max(level - h for h in buildable) > max_fill_depth:
            return (float("inf"), float("inf"))
        return (float(cut), float(fill))

    def h(p: Vec2) -> float:
        return abs(p[0] - goal[0]) + abs(p[1] - goal[1])

    # state = (pos, direction index) with 4 = "no direction yet" at the start
    start_state = (start, 4)
    openq: List[Tuple[float, int, Tuple[Vec2, int]]] = [(h(start), 0, start_state)]
    came: Dict[Tuple[Vec2, int], Optional[Tuple[Vec2, int]]] = {start_state: None}
    g: Dict[Tuple[Vec2, int], float] = {start_state: 0.0}
    counter = 0
    best_goal: Optional[Tuple[Vec2, int]] = None

    while openq:
        _, _, state = heapq.heappop(openq)
        cur, dir_ix = state
        if cur == goal:
            best_goal = state
            break
        for nb in _neighbours(cur, sx, sz):
            if not walkable(nb):
                continue
            dy = abs(int(ground_height[nb]) - int(ground_height[cur]))
            if dy > max_step:
                continue
            step = 1.0 + slope_cost * dy
            nb_dir = _DIRS.index((nb[0] - cur[0], nb[1] - cur[1]))
            if dir_ix != 4 and nb_dir != dir_ix:
                step += turn_cost
            if cut_cost or fill_cost:
                ec, ef = estimate(nb, cur)
                if ec == float("inf"):
                    continue
                step += cut_cost * ec + fill_cost * ef
            ng = g[state] + step
            key = (nb, nb_dir)
            if ng < g.get(key, float("inf")):
                g[key] = ng
                came[key] = state
                counter += 1
                heapq.heappush(openq, (ng + h(nb), counter, key))

    if best_goal is None:
        raise PathError(f"no path from {start} to {goal} within max_step={max_step}")

    # rebuild by walking the state chain
    chain: List[Vec2] = []
    st: Optional[Tuple[Vec2, int]] = best_goal
    while st is not None:
        chain.append(st[0])
        st = came[st]
    chain.reverse()
    return chain


# --------------------------------------------------------------------------
# road plan
# --------------------------------------------------------------------------


@dataclass
class RoadPlan:
    """Everything the compiler needs about a solved road."""

    positions: List[Vec2] = field(default_factory=list)
    levels: List[int] = field(default_factory=list)
    width: int = 1
    segments: List[Tuple[Vec3, ...]] = field(default_factory=list)
    # The actual road surface in path order: the surface voxel of each
    # centreline column, whichever cross-section happened to pave it. Needed
    # because neighbouring cross-sections overlap and a column is paved only
    # once.
    centerline: List[Vec3] = field(default_factory=list)
    changes: List[BlockChange] = field(default_factory=list)
    estimated: Dict[str, int] = field(default_factory=dict)
    transitions: List[Vec3] = field(default_factory=list)
    skipped: List[dict] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def _palette_parts(palette) -> tuple:
    """Accept a legacy flat list or a structured palette dict.

    Returns ``(surface_list, edge_block, support_block, transition_block)``.
    ``transition_block`` is the half-slab used to make a one-block step
    walkable; without it a stepped road cannot be built legally and is refused.
    """
    if isinstance(palette, dict):
        surface = list(palette.get("surface") or [])
        edge = palette.get("edge") or (surface[0] if surface else None)
        support = palette.get("support") or (surface[0] if surface else None)
        transition = palette.get("transition")
        return surface, edge, support, transition
    return list(palette), palette[0], palette[0], None


def _cross_section(cx: int, cz: int, half: int) -> List[Vec2]:
    out: List[Vec2] = []
    for dx in range(-half, half + 1):
        for dz in range(-half, half + 1):
            out.append((cx + dx, cz + dz))
    return out


def solve_road(
    snapshot,
    ground_height,
    path: List[Vec2],
    width: int,
    palette,
    op_id: str,
    *,
    build_height=None,
    cover_above: Optional[Dict[Vec2, Tuple[int, ...]]] = None,
    min_headroom: int = 2,
    max_fill_depth: int = 3,
) -> RoadPlan:
    """Solve the road surface, support, clearance and step transitions.

    ``snapshot`` is any read view exposing ``block_at_local`` /
    ``contains_local`` / ``region_id``; the compiler passes its staged working
    view so every ``before`` reflects the current stage, not the baseline.
    """
    surface_list, edge_block, support_block, transition_block = _palette_parts(palette)
    if not surface_list:
        raise PathError(f"{op_id}: empty palette")
    est_h = build_height if build_height is not None else ground_height
    cover_above = cover_above or {}
    sx, sz = ground_height.shape
    half = width // 2
    plan = RoadPlan()

    # Overlapping cross-sections and the clearance/transition passes can target
    # the same voxel more than once. Proposals are all written later, so the
    # local overlay keeps every ``before`` in step with the staged state that
    # the write gate will actually see.
    pending: Dict[Vec3, str] = {}

    def block(p) -> str:
        p = tuple(p)
        if p in pending:
            return pending[p]
        return snapshot.block_at_local(p)

    def removable_at(p) -> bool:
        b = block(p)
        return b == AIR or b in COVER_BLOCKS

    # ---- pass 1: road level per position, validity of every cross-section ----
    levels: List[Optional[int]] = []
    cell_info: List[Dict[Vec2, int]] = []
    for i, (cx, cz) in enumerate(path):
        cells = _cross_section(cx, cz, half)
        info: Dict[Vec2, int] = {}
        for (x, z) in cells:
            if not (0 <= x < sx and 0 <= z < sz):
                raise PathError(f"{op_id}: road cell ({x},{z}) out of bounds")
            info[(x, z)] = int(est_h[x, z])
        heights = [h for h in info.values() if h >= 0]
        if not heights:
            raise PathError(
                f"{op_id}: no buildable ground in the {width}-wide road cross-section at {(cx, cz)}"
            )
        level = max(heights)
        if max(level - h for h in heights) > max_fill_depth:
            raise PathError(
                f"{op_id}: cross-section at {(cx, cz)} needs a fill deeper than "
                f"{max_fill_depth} blocks"
            )
        levels.append(level)
        cell_info.append(info)

    plan.positions = list(path)
    plan.levels = [int(v) for v in levels]
    plan.width = width

    # Search estimate, kept separate from the real de-duplicated cut/fill that
    # the net merge reports later. It covers the whole road width, which is
    # what the A* cost model used.
    est_cut = est_fill = 0
    for i, info in enumerate(cell_info):
        level_i = plan.levels[i]
        est_fill += sum(level_i - h for h in info.values() if h >= 0)
        if i + 1 < len(plan.levels):
            d = plan.levels[i + 1] - level_i
            if d > 0:
                est_fill += d
            elif d < 0:
                est_cut += -d
    plan.estimated = {"cut": est_cut, "fill": est_fill}

    # ---- longitudinal step guard ------------------------------------------
    lv = plan.levels
    for i in range(len(lv) - 1):
        if abs(lv[i + 1] - lv[i]) > 1:
            raise PathError(
                f"{op_id}: road level jumps from {lv[i]} at {path[i]} to "
                f"{lv[i + 1]} at {path[i + 1]}; a legal transition is impossible"
            )
    for i in range(len(lv) - 2):
        d1, d2 = lv[i + 1] - lv[i], lv[i + 2] - lv[i + 1]
        if d1 == d2 and d1 != 0:
            raise PathError(
                f"{op_id}: two consecutive {d1:+d}-block steps at {path[i]}..{path[i+2]} "
                f"need stair transitions, which walk_no_jump_v1 does not support yet; "
                f"route unsupported"
            )

    # ---- pass 2: pave, support, clear cover --------------------------------
    def emit(pos: Vec3, blk: str, action: str) -> None:
        if not snapshot.contains_local(pos):
            raise PathError(f"{op_id}: {pos} out of bounds")
        before = block(pos)
        if before != blk:
            plan.changes.append(BlockChange(snapshot.region_id, pos, before, blk, op_id, action))
            pending[pos] = blk

    # A centreline cell's cross-section overlaps its neighbour's, so a column
    # can be covered by several positions at different levels. Each column is
    # therefore paved exactly once - by the first position that covers it - and
    # the step transitions then run along the leading band of each position.
    claimed: Set[Vec2] = set()
    paved_columns: Dict[Vec2, Vec3] = {}
    for i, (cx, cz) in enumerate(path):
        level = plan.levels[i]
        cells = cell_info[i]
        paved: List[Vec3] = []
        for (x, z), h in sorted(cells.items()):
            if (x, z) in claimed:
                continue
            claimed.add((x, z))
            if h < 0:
                plan.skipped.append({"pos": [x, z], "reason": "no buildable ground"})
                continue
            # every voxel between the column's own ground and the road level,
            # and the walk volume above it, must be air or removable cover
            blocked = None
            for y in range(h + 1, level):
                b = block((x, y, z))
                if not removable_at((x, y, z)):
                    blocked = ((x, y, z), b)
                    break
            if blocked is None:
                for y in range(level + 1, level + 1 + min_headroom):
                    if y >= 0 and not removable_at((x, y, z)):
                        blocked = ((x, y, z), block((x, y, z)))
                        break
            # Support chain: the cell's own ground block may itself be floating
            # (natural overhangs exist in real terrain). Fill the void below it
            # down to solid rock, bounded by max_fill_depth; a void deeper than
            # the bound means the cell cannot be supported and is skipped.
            support_below: List[int] = []
            if blocked is None:
                y = h
                depth = 0
                while y - 1 >= 0:
                    if not removable_at((x, y - 1, z)):
                        break
                    if depth >= max_fill_depth:
                        blocked = ((x, y - 1, z), "void deeper than max_fill_depth")
                        break
                    support_below.append(y - 1)
                    y -= 1
                    depth += 1
            if blocked is not None:
                plan.skipped.append(
                    {"pos": [x, z], "reason": f"blocked by {blocked[1]} at {list(blocked[0])}"}
                )
                plan.warnings.append(
                    f"{op_id}: road cell ({x},{z}) skipped, blocked by "
                    f"{blocked[1]} at {list(blocked[0])}"
                )
                continue

            surface = surface_list[i % len(surface_list)]
            if i + 1 < len(path):
                dirx, dirz = path[i + 1][0] - cx, path[i + 1][1] - cz
            elif i > 0:
                dirx, dirz = cx - path[i - 1][0], cz - path[i - 1][1]
            else:
                dirx, dirz = 1, 0
            dx, dz = x - cx, z - cz
            if dirx != 0:
                is_edge = abs(dz) == half
            elif dirz != 0:
                is_edge = abs(dx) == half
            else:
                is_edge = abs(dx) == half or abs(dz) == half
            blk = edge_block if (is_edge and width > 1) else surface

            # clearance request: only known removable cover is cleared
            for y in cover_above.get((x, z), ()):
                if level + 1 <= y <= level + min_headroom:
                    emit((x, y, z), AIR, "clear")
            for y in range(h + 1, level):
                emit((x, y, z), support_block, "support")
            for y in support_below:
                emit((x, y, z), support_block, "support")
            emit((x, level, z), blk, "path")
            paved.append((x, level, z))
            paved_columns[(x, z)] = (x, level, z)
        plan.segments.append(tuple(paved))

    # ---- pass 3: step transitions ------------------------------------------
    for i in range(len(lv) - 1):
        delta = lv[i + 1] - lv[i]
        if delta == 0:
            continue
        lower = i if delta == 1 else i + 1
        if transition_block is None:
            raise PathError(
                f"{op_id}: road has a {abs(delta)}-block step between {path[i]} and "
                f"{path[i + 1]} but the palette declares no 'transition' slab"
            )
        for (x, level, z) in plan.segments[lower]:
            y = level + 1
            if not snapshot.contains_local((x, y, z)) or not removable_at((x, y, z)):
                plan.warnings.append(
                    f"{op_id}: step transition at ({x},{z}) skipped, "
                    f"voxel {list((x, y, z))} is not clearable"
                )
                continue
            emit((x, y, z), transition_block, "path")
            plan.transitions.append((x, y, z))

    from ..traversal import classify_state

    def walk_surface(x: int, z: int, level: int):
        """The voxel a walker stands on in this column, at or below ``level``."""
        for y in range(level, -1, -1):
            if classify_state(block((x, y, z))).walkable:
                return (x, y, z)
        return None

    levels_by_column = {p: plan.levels[i] for i, p in enumerate(path)}
    for (cx, cz) in path:
        voxel = paved_columns.get((cx, cz))
        if voxel is None:
            # The column was deliberately not paved (an untouchable block or no
            # clearance). The walk surface there is whatever the existing
            # terrain offers, so the final check can still measure continuity
            # instead of silently stopping at the gap.
            voxel = walk_surface(cx, cz, levels_by_column[(cx, cz)])
        if voxel is None:
            break
        plan.centerline.append(voxel)

    # ---- pass 4: transitions between pavement and existing terrain ---------
    if transition_block is not None:
        for i in range(len(plan.centerline) - 1):
            a_vox = plan.centerline[i]
            b_vox = plan.centerline[i + 1]
            a_shape = classify_state(block(a_vox))
            b_shape = classify_state(block(b_vox))
            if not (a_shape.walkable and b_shape.walkable):
                continue
            rise = (b_vox[1] + b_shape.surface) - (a_vox[1] + a_shape.surface)
            if abs(abs(rise) - 1.0) > 1e-6:
                continue
            lower = a_vox if rise > 0 else b_vox
            target = (lower[0], lower[1] + 1, lower[2])
            if not snapshot.contains_local(target) or not removable_at(target):
                continue
            if target in pending:
                continue
            emit(target, transition_block, "path")
            plan.transitions.append(target)

    return plan


def pave_path(
    snapshot: SceneSnapshot,
    ground_height,
    path: List[Vec2],
    width: int,
    palette,
    op_id: str,
    **kwargs,
) -> List[BlockChange]:
    """Backwards-compatible wrapper returning only the proposed changes."""
    return solve_road(
        snapshot, ground_height, path, width, palette, op_id, **kwargs
    ).changes
