"""Deterministic plan compiler: resolve references, compile ops in order,
enforce the write guard and resource budgets, and produce a net PatchSet
against the transaction baseline ``Br``.

Fixed plan + input + asset versions + seed => deterministic output.

Every modification goes through one write entry point
(:class:`litegarden.net_patch.WorkingWorld`, which calls
:class:`litegarden.constraints.WriteGuard`). Operations only *propose* changes;
they can neither write nor decide permissions themselves.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .blocks import COVER_BLOCKS
from .constraints import Box3, WriteGuard, build_policy, load_block_rules
from .net_patch import ConflictPolicy, NetPatchResult, WorkingWorld, WriteEvent
from .operations.path import PathError, RoadPlan, find_path, solve_road
from .operations.scatter import decorate_path, scatter_assets
from .operations.stamp import (
    Asset,
    AssetError,
    asset_base_y,
    load_catalog,
    load_prefab,
    place_asset,
)
from .scene import AIR, BlockChange, PatchSet, SceneSnapshot
from .schema import ConnectPath, DecoratePath, PlaceAsset, Plan, ScatterAssets
from .terrain import TerrainAnalysis

Vec2 = Tuple[int, int]
Vec3 = Tuple[int, int, int]

# The region's outermost layer is not a world floor: a voxel at the minimum y
# has no neighbour inside the input, so its support can be neither proved nor
# disproved from the file. Those support issues are reported as explicit
# "unverifiable" diagnostics instead of hard errors - they are never silently
# passed and never presented as verified support.
SLICE_BOTTOM_POLICY = (
    "voxels at the input region's minimum y are excluded from the support "
    "requirement because the supporting block lies outside the input file"
)


class CompileError(ValueError):
    """Structured compile failure carrying the op_id / code when known."""

    def __init__(
        self,
        message: str,
        op_id: Optional[str] = None,
        code: Optional[str] = None,
        issues: Optional[Sequence[dict]] = None,
    ):
        super().__init__(message)
        self.op_id = op_id
        self.code = code
        self.issues = list(issues or [])


@dataclass
class CompileResult:
    patch: PatchSet
    warnings: List[str] = field(default_factory=list)
    net: Optional[NetPatchResult] = None
    events: List[WriteEvent] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    guard: Optional[WriteGuard] = None
    roads: List[dict] = field(default_factory=list)
    road_plans: List[RoadPlan] = field(default_factory=list)
    entries: List[dict] = field(default_factory=list)
    issues: List[dict] = field(default_factory=list)
    diagnostics: List[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "changes": len(self.patch),
            "warnings": list(self.warnings),
            "stats": dict(self.stats),
            "roads": list(self.roads),
            "entries": list(self.entries),
            "issues": list(self.issues),
            "diagnostics": list(self.diagnostics),
        }


@dataclass
class _AssetPlacement:
    op_id: str
    asset: Asset
    origin: Vec2
    base_y: int


class _Ctx:
    def __init__(self, world, analysis, assets, palettes, seed):
        self.world = world
        self.analysis = analysis
        self.assets = assets
        self.palettes = palettes
        self.seed = seed
        self.placed: Dict[str, dict] = {}
        self.occupied: Set[Vec2] = set()
        self.asset_placements: List[_AssetPlacement] = []
        self.roads: List[RoadPlan] = []


def _site(ctx: _Ctx, site_id: str) -> dict:
    for s in ctx.analysis.site_candidates:
        if s["id"] == site_id:
            return s
    raise CompileError(f"unknown site '{site_id}'")


def _resolve_point(ctx: _Ctx, ref: str) -> Vec2:
    """Resolve 'entry_01' or 'pavilion_1.entry' to a local (x,z)."""
    if ref in ctx.analysis.anchors:
        return ctx.analysis.anchors[ref]
    if "." in ref:
        op_id, entry = ref.split(".", 1)
        if op_id in ctx.placed and "asset" in ctx.placed[op_id]:
            asset: Asset = ctx.placed[op_id]["asset"]
            origin = ctx.placed[op_id]["origin"]
            if entry in asset.entries:
                ex, ez = asset.entries[entry]
                return (origin[0] + ex, origin[1] + ez)
    raise CompileError(f"unresolvable reference '{ref}'")


def _asset(ctx: _Ctx, asset_id: str) -> Asset:
    if asset_id not in ctx.assets:
        raise CompileError(f"unknown asset '{asset_id}'")
    return ctx.assets[asset_id]


def _palette(ctx: _Ctx, palette_id: str):
    if palette_id not in ctx.palettes:
        raise CompileError(f"unknown palette '{palette_id}'")
    return ctx.palettes[palette_id]


def make_guard(
    snapshot: SceneSnapshot,
    config: Optional[dict],
    assets_dir: Path,
    entity_hosts: Iterable[Vec3] = (),
) -> WriteGuard:
    """Build the write guard for a compile from config + verified block rules."""
    rules = load_block_rules(Path(assets_dir) / "block_rules.json")
    sx, sy, sz = snapshot.transform.local_size
    bounds = Box3((0, 0, 0), (sx, sy, sz))
    policy = build_policy(
        config, rules, snapshot.data_version, bounds, entity_hosts=entity_hosts
    )
    return WriteGuard(bounds, policy, entity_hosts=entity_hosts)


# --------------------------------------------------------------------------
# entries
# --------------------------------------------------------------------------


def _entry_columns(pl: _AssetPlacement, ex: int, ez: int) -> Tuple[Vec2, Vec2]:
    """(door column, outward column) of one asset entry."""
    fx, fz = pl.asset.footprint
    centre_x = pl.origin[0] + (fx - 1) / 2.0
    centre_z = pl.origin[1] + (fz - 1) / 2.0
    door = (pl.origin[0] + ex, pl.origin[1] + ez)
    vx, vz = door[0] - centre_x, door[1] - centre_z
    if abs(vx) >= abs(vz):
        dirx, dirz = (1 if vx >= 0 else -1), 0
    else:
        dirx, dirz = 0, (1 if vz >= 0 else -1)
    return door, (door[0] + dirx, door[1] + dirz)


def _standing_voxel(world, x: int, z: int, y_start: int) -> Optional[Vec3]:
    """Topmost existing block at or below ``y_start`` in a column."""
    for y in range(y_start, -1, -1):
        state = world.block_at_local((x, y, z))
        if state not in (AIR, ""):
            return (x, y, z)
    return None


def _transition_block_for(ctx: _Ctx, outside: Vec2, config: dict) -> Optional[str]:
    """The slab used to make an entry threshold walkable."""
    from .operations.path import _palette_parts
    for op_id, info in ctx.placed.items():
        road: Optional[RoadPlan] = info.get("road")
        if road is None:
            continue
        for seg in road.segments:
            if any(v[0] == outside[0] and v[2] == outside[1] for v in seg):
                return _palette_parts(info.get("palette"))[3], op_id
    return config.get("entry_transition_block"), None


def _apply_entry_transitions(
    ctx: _Ctx, world, config: dict, warnings: List[str]
) -> int:
    """Make a one-block entry threshold walkable without a jump.

    A bottom slab is placed directly above the *lower* cell of the step, so the
    rise becomes two half-block steps. Only an air/known-cover voxel is used;
    anything else is reported instead of being overwritten.
    """
    from .traversal import classify_state

    placed = 0
    for pl in ctx.asset_placements:
        if not pl.asset.entries or pl.base_y < 1:
            continue
        for name, (ex, ez) in sorted(pl.asset.entries.items()):
            door, outside = _entry_columns(pl, ex, ez)
            floor = world.block_at_local((door[0], pl.base_y, door[1]))
            floor_shape = classify_state(floor)
            if not floor_shape.walkable:
                continue
            out_voxel = _standing_voxel(world, outside[0], outside[1], pl.base_y)
            if out_voxel is None:
                continue
            out_shape = classify_state(world.block_at_local(out_voxel))
            if not out_shape.walkable:
                continue
            rise = (pl.base_y + floor_shape.surface) - (out_voxel[1] + out_shape.surface)
            if abs(rise - 1.0) > 1e-6:
                continue
            slab_block, owner_op = _transition_block_for(ctx, outside, config)
            target = (outside[0], out_voxel[1] + 1, outside[1])
            if not world.contains_local(target):
                continue
            before = world.block_at_local(target)
            if slab_block is None:
                warnings.append(
                    f"{pl.op_id}.{name}: one-block entry threshold at {list(target)} "
                    f"has no configured transition slab"
                )
                continue
            if before != AIR and before not in COVER_BLOCKS:
                warnings.append(
                    f"{pl.op_id}.{name}: entry threshold transition skipped, "
                    f"{list(target)} holds {before}"
                )
                continue
            # The threshold sits on the road's last paved cell, so the write is
            # attributed to the road op that owns it - declaring it as a
            # separate writer would be an undeclared overwrite.
            writer = owner_op or pl.op_id
            world.write(
                target, slab_block, writer,
                expected_stage_before=before, action="path", target_id=writer,
            )
            placed += 1
    return placed


def _entry_contracts(ctx: _Ctx, world, min_headroom: int) -> List[dict]:
    """Rectangular entry contract for every placed asset that declares an entry.

    ``bounds`` is the doorway passage volume that must be air - the door column
    and the outward column share the same y span so the box is exactly the
    passage, never inflating to include the inside floor. ``inside``/``outside``
    are the two standing voxels and are checked for a walkable, supported
    surface by the traversal profile.
    """
    out: List[dict] = []
    for pl in ctx.asset_placements:
        if not pl.asset.entries or pl.base_y < 0:
            continue
        for name, (ex, ez) in sorted(pl.asset.entries.items()):
            door, outside = _entry_columns(pl, ex, ez)
            # Both landing columns must start at the same y, otherwise a
            # rectangular all-air passage volume cannot contain them (and would
            # either include the inside floor or miss the outside approach).
            out_voxel = _standing_voxel(world, outside[0], outside[1], pl.base_y)
            if out_voxel is None:
                y0 = pl.base_y + 1
            else:
                y0 = max(pl.base_y, out_voxel[1]) + 1
            y1 = y0 + min_headroom
            lo = [min(door[0], outside[0]), y0, min(door[1], outside[1])]
            hi = [max(door[0], outside[0]) + 1, y1, max(door[1], outside[1]) + 1]
            out.append({
                "id": f"{pl.op_id}.{name}",
                "op_id": pl.op_id,
                "outward": [outside[0] - door[0], outside[1] - door[1]],
                "inside": [door[0], pl.base_y, door[1]],
                "outside": list(out_voxel) if out_voxel else None,
                "bounds": {"min": lo, "max_exclusive": hi},
            })
    return out


# --------------------------------------------------------------------------
# compile
# --------------------------------------------------------------------------


def _road_cells(plan: RoadPlan) -> List[Vec3]:
    """Contiguous paved run of centreline road-surface voxels.

    The list stops where the pavement stops (a cell under a finished building is
    intentionally not paved), so the walkability check covers the road that was
    actually built rather than an idealised centreline.
    """
    cells: List[Vec3] = []
    for i, (cx, cz) in enumerate(plan.positions):
        seg = plan.segments[i] if i < len(plan.segments) else ()
        match = [v for v in seg if v[0] == cx and v[2] == cz]
        if not match:
            break
        cells.append(match[0])
    return cells


def compile_plan(
    snapshot: SceneSnapshot,
    plan: Plan,
    analysis: TerrainAnalysis,
    assets_dir: Path,
    max_blocks: Optional[int] = None,
    *,
    config: Optional[dict] = None,
    guard: Optional[WriteGuard] = None,
    entity_hosts: Iterable[Vec3] = (),
    conflict_policy: Optional[ConflictPolicy] = None,
    min_headroom: int = 2,
    max_fill_depth: int = 3,
    budgets: Optional[dict] = None,
    check_walkability: bool = True,
) -> CompileResult:
    """Compile a validated Plan into a net PatchSet against the baseline.

    ``config`` supplies read-only project rules (zones, budgets, weights). It
    is never read from the plan: an Agent cannot widen a mask or a budget.
    """
    assets_dir = Path(assets_dir)
    assets = load_catalog(assets_dir / "catalog.json")
    palettes = json.loads((assets_dir / "palettes.json").read_text(encoding="utf-8"))["palettes"]
    for aid, asset in assets.items():
        pf = assets_dir / "prefabs" / f"{aid}.json"
        if pf.exists():
            load_prefab(asset, pf)

    config = config or {}
    budgets = dict(budgets if budgets is not None else (config.get("budget") or {}))
    if max_blocks is None:
        max_blocks = budgets.get("max_blocks")
    if guard is None:
        guard = make_guard(snapshot, config, assets_dir, entity_hosts)
    world = WorkingWorld(snapshot, guard, conflict_policy)
    ctx = _Ctx(world, analysis, assets, palettes, plan.seed)

    weights = config.get("path_weights") or {}
    warnings: List[str] = []
    roads: List[dict] = []

    def absorb(changes: List[BlockChange], target_id: Optional[str] = None) -> None:
        for c in changes:
            world.write(
                c.pos_local,
                c.after,
                c.op_id,
                expected_stage_before=c.before,
                action=c.action,
                target_id=target_id,
            )

    for op in plan.operations:
        try:
            if isinstance(op, PlaceAsset):
                asset = _asset(ctx, op.asset_id)
                site = _site(ctx, op.site_id)
                origin = tuple(site["origin"])
                changes = place_asset(
                    world, analysis.ground_height, asset, origin, op.id, op.variant
                )
                absorb(changes, target_id=op.id)
                ctx.placed[op.id] = {"asset": asset, "origin": origin}
                try:
                    base_y = asset_base_y(analysis.ground_height, asset, origin)
                except AssetError:
                    base_y = -1
                ctx.asset_placements.append(_AssetPlacement(op.id, asset, origin, base_y))
                fx, fz = asset.footprint
                for dx in range(fx):
                    for dz in range(fz):
                        ctx.occupied.add((origin[0] + dx, origin[1] + dz))

            elif isinstance(op, ConnectPath):
                start = _resolve_point(ctx, op.from_anchor)
                goal = _resolve_point(ctx, op.to)
                palette = _palette(ctx, op.palette_id)
                path = find_path(
                    analysis.ground_height,
                    analysis.water_mask,
                    analysis.obstacle_mask,
                    start,
                    goal,
                    build_height=analysis.build_height,
                    width=op.width,
                    cut_cost=float(weights.get("cut", 0.0)),
                    fill_cost=float(weights.get("fill", 0.0)),
                    slope_cost=float(weights.get("slope", 2.0)),
                    turn_cost=float(weights.get("turn", 0.5)),
                    max_fill_depth=max_fill_depth,
                )
                road = solve_road(
                    world,
                    analysis.ground_height,
                    path,
                    op.width,
                    palette,
                    op.id,
                    build_height=analysis.build_height,
                    cover_above=analysis.cover_above,
                    min_headroom=min_headroom,
                    max_fill_depth=max_fill_depth,
                )
                absorb(road.changes, target_id=op.id)
                ctx.placed[op.id] = {"path": path, "road": road, "palette": palette}
                ctx.roads.append(road)
                ctx.occupied.update(path)
                # The paved footprint is wider than the centreline, so every
                # paved cell must be reserved too - otherwise a lamp or shrub
                # can be placed on the road (caught later as
                # PATH_HEADROOM_BLOCKED on the final candidate).
                for seg in road.segments:
                    for (px, _py, pz) in seg:
                        ctx.occupied.add((px, pz))
                warnings.extend(road.warnings)
                roads.append({
                    "op_id": op.id,
                    "width": op.width,
                    "length": len(path),
                    "paved": len(_road_cells(road)),
                    "transitions": len(road.transitions),
                    "skipped_cells": len(road.skipped),
                    "skipped": road.skipped,
                    "levels": road.levels,
                    "estimated": dict(road.estimated),
                })

            elif isinstance(op, DecoratePath):
                if op.path_id not in ctx.placed or "path" not in ctx.placed[op.path_id]:
                    raise CompileError(f"unknown path '{op.path_id}'")
                path = ctx.placed[op.path_id]["path"]
                asset = _asset(ctx, op.asset_id)
                changes = decorate_path(
                    world, analysis.ground_height, path, asset, op.spacing, op.id, ctx.occupied
                )
                absorb(changes, target_id=op.id)

            elif isinstance(op, ScatterAssets):
                if op.zone_id not in ctx.analysis.zones:
                    raise CompileError(f"unknown zone '{op.zone_id}'")
                bbox = ctx.analysis.zones[op.zone_id]["bbox"]
                zone = ((bbox[0], bbox[1]), (bbox[2], bbox[3]))
                asset = _asset(ctx, op.asset_id)
                changes = scatter_assets(
                    world, analysis.ground_height, zone, asset, op.count, ctx.seed,
                    op.id, ctx.occupied,
                )
                absorb(changes, target_id=op.id)
        except (CompileError, AssetError, PathError) as e:
            op_id = getattr(e, "op_id", None) or getattr(op, "id", None)
            raise CompileError(str(e), op_id=op_id, code=getattr(e, "code", None)) from e

    entry_transitions = _apply_entry_transitions(ctx, world, config, warnings)

    net = world.finalize()
    stats = net.stats
    # search estimates (over the full road width) next to the real, de-duplicated
    # net cut/fill so the two are never conflated
    stats["estimated_cut"] = sum(r.estimated.get("cut", 0) for r in ctx.roads)
    stats["estimated_fill"] = sum(r.estimated.get("fill", 0) for r in ctx.roads)
    warnings.extend(net.warnings)

    # ---- gate 2: re-check the merged net patch, including every ``before`` --
    guard.check_net_patch(net.net_changes, world.base_state_at)

    if max_blocks is not None and len(net.patch) > max_blocks:
        raise CompileError(
            f"budget exceeded: {len(net.patch)} net changes > {max_blocks} blocks",
            code="BUDGET_EXCEEDED",
        )
    for name, limit in (
        ("cut", budgets.get("max_cut")),
        ("fill", budgets.get("max_fill")),
        ("replace", budgets.get("max_replace")),
    ):
        if limit is not None and stats[name] > limit:
            raise CompileError(
                f"budget exceeded: {name} {stats[name]} > {limit}",
                code="BUDGET_EXCEEDED",
            )

    entries = _entry_contracts(ctx, world, min_headroom)
    issues: List[dict] = []
    diagnostics: List[dict] = []
    if check_walkability:
        issues, diagnostics = run_final_checks(
            world, ctx.roads, entries, min_headroom=min_headroom
        )
        diagnostics.append({"code": "SLICE_BOTTOM_POLICY", "detail": SLICE_BOTTOM_POLICY})

    result = CompileResult(
        patch=net.patch,
        warnings=warnings,
        net=net,
        events=list(net.events),
        stats=dict(stats, entry_transitions=entry_transitions),
        guard=guard,
        roads=roads,
        road_plans=list(ctx.roads),
        entries=entries,
        issues=issues,
        diagnostics=diagnostics,
    )
    if issues:
        raise CompileError(
            f"{len(issues)} walkability issue(s) on the final candidate: "
            + "; ".join(f"{i['code']}@{i['pos_local']}" for i in issues[:5]),
            op_id=issues[0].get("op_id"),
            code=issues[0]["code"],
            issues=issues,
        )
    return result


def _sampler(world):
    def sample(p) -> Optional[str]:
        p = tuple(p)
        if not world.contains_local(p):
            return None
        return world.block_at_local(p)

    return sample


def run_final_checks(
    world,
    roads: Sequence[RoadPlan],
    entries: Sequence[dict],
    *,
    min_headroom: int = 2,
    slice_bottom_y: int = 0,
) -> Tuple[List[dict], List[dict]]:
    """Walkability / entry checks on the *finished* staged candidate.

    Runs after every decoration, so a lamp post that blocks a doorway is caught
    on the final candidate rather than only on an intermediate stage.

    Returns ``(errors, diagnostics)``. A support issue for a voxel sitting on
    the input slice's bottom layer is a diagnostic, not an error: the block
    below is outside the file and cannot be evaluated either way.
    """
    from .traversal import (
        PROFILE_WALK_NO_JUMP_V1,
        check_connectivity,
        check_entry,
        check_road,
    )

    sample = _sampler(world)
    errors: List[dict] = []
    diagnostics: List[dict] = []

    def emit(issue_list, op_id: str) -> None:
        for it in issue_list:
            record = {
                "code": it.code,
                "pos_local": list(it.pos_local),
                "rule_id": it.rule_id,
                "expected": it.expected,
                "actual": it.actual,
                "detail": it.detail,
                "op_id": op_id,
            }
            if it.code == "SUPPORT_RULE_VIOLATION" and it.pos_local[1] <= slice_bottom_y:
                record["unverifiable"] = True
                record["detail"] = (
                    (it.detail + " ") if it.detail else ""
                ) + SLICE_BOTTOM_POLICY
                diagnostics.append(record)
            else:
                errors.append(record)

    for road in roads:
        cells = _road_cells(road)
        if not cells:
            continue
        # The road that was actually built is authoritative: a cross-section is
        # narrowed where terrain or an untouchable block prevents paving, and
        # those cells are reported as warnings rather than silently assumed
        # walkable. Checking an idealised rectangle would flag cells that were
        # deliberately never paved.
        emit(
            check_road(
                sample, cells, 1,
                profile=PROFILE_WALK_NO_JUMP_V1, min_headroom=min_headroom,
            ),
            "road",
        )
        centre = set(cells)
        for seg in road.segments:
            for voxel in seg:
                if voxel in centre:
                    continue
                # A single-cell list still enforces walkability, support and
                # headroom for that cell (no pair means no step rule).
                emit(
                    check_connectivity(
                        sample, [voxel],
                        profile=PROFILE_WALK_NO_JUMP_V1, min_headroom=min_headroom,
                    ),
                    "road",
                )
    for entry in entries:
        if entry["outside"] is None:
            errors.append({
                "code": "ENTRY_BLOCKED",
                "pos_local": entry["inside"],
                "rule_id": "walk_no_jump_v1/entry",
                "expected": "a supported cell outside the doorway",
                "actual": "no block found in the outward column",
                "detail": f"entry {entry['id']} has no outside landing",
                "op_id": entry["op_id"],
            })
            continue
        # When the approach cell sits on the input slice's bottom layer its
        # support lies outside the file, so entry issues that depend on it are
        # reported as unverifiable diagnostics rather than hard errors.
        unverifiable = entry["outside"][1] <= slice_bottom_y
        entry_issues = check_entry(
            sample,
            {
                "id": entry["id"],
                "bounds": entry["bounds"],
                "inside": entry["inside"],
                "outside": entry["outside"],
                "width": 1,
            },
            profile=PROFILE_WALK_NO_JUMP_V1,
            min_headroom=min_headroom,
        )
        if unverifiable:
            for it in entry_issues:
                diagnostics.append({
                    "code": it.code,
                    "pos_local": list(it.pos_local),
                    "rule_id": it.rule_id,
                    "expected": it.expected,
                    "actual": it.actual,
                    "detail": ((it.detail + " ") if it.detail else "") + SLICE_BOTTOM_POLICY,
                    "op_id": entry["op_id"],
                    "unverifiable": True,
                })
        else:
            emit(entry_issues, entry["op_id"])
    return (
        sorted(errors, key=lambda i: (i["pos_local"], i["code"])),
        sorted(diagnostics, key=lambda i: (i["pos_local"], i["code"])),
    )
