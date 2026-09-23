"""Deterministic plan compiler: resolve references, compile ops in order,
enforce resource budgets, and produce a PatchSet against the original baseline.

Fixed plan + input + asset versions + seed => deterministic output.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .operations.path import PathError, find_path, pave_path
from .operations.scatter import decorate_path, scatter_assets
from .operations.stamp import Asset, AssetError, load_catalog, load_prefab, place_asset
from .scene import BlockChange, PatchSet, SceneSnapshot
from .schema import ConnectPath, DecoratePath, PlaceAsset, Plan, ScatterAssets
from .terrain import TerrainAnalysis

Vec2 = Tuple[int, int]


class CompileError(ValueError):
    """Structured compile failure carrying the op_id when known."""

    def __init__(self, message: str, op_id: Optional[str] = None):
        super().__init__(message)
        self.op_id = op_id


@dataclass
class CompileResult:
    patch: PatchSet
    warnings: List[str] = field(default_factory=list)


class _Ctx:
    def __init__(self, snapshot, analysis, assets, palettes, seed):
        self.snapshot = snapshot
        self.analysis = analysis
        self.assets = assets
        self.palettes = palettes
        self.seed = seed
        self.placed: Dict[str, dict] = {}  # op_id -> info (asset entry / path)
        self.occupied: Set[Vec2] = set()


def _site(ctx: _Ctx, site_id: str) -> dict:
    for s in ctx.analysis.site_candidates:
        if s["id"] == site_id:
            return s
    raise CompileError(f"unknown site '{site_id}'")


def _anchor(ctx: _Ctx, name: str) -> Vec2:
    if name in ctx.analysis.anchors:
        return ctx.analysis.anchors[name]
    raise CompileError(f"unknown anchor '{name}'")


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


def _palette(ctx: _Ctx, palette_id: str) -> List[str]:
    if palette_id not in ctx.palettes:
        raise CompileError(f"unknown palette '{palette_id}'")
    return ctx.palettes[palette_id]


def compile_plan(
    snapshot: SceneSnapshot,
    plan: Plan,
    analysis: TerrainAnalysis,
    assets_dir: Path,
    max_blocks: Optional[int] = None,
) -> CompileResult:
    """Compile a validated Plan into a PatchSet against the original baseline."""
    assets = load_catalog(assets_dir / "catalog.json")
    palettes = json.loads((assets_dir / "palettes.json").read_text(encoding="utf-8"))["palettes"]
    # load any prefab voxel data that exists
    for aid, asset in assets.items():
        pf = assets_dir / "prefabs" / f"{aid}.json"
        if pf.exists():
            load_prefab(asset, pf)

    ctx = _Ctx(snapshot, analysis, assets, palettes, plan.seed)
    patch = PatchSet()
    warnings: List[str] = []
    total = 0

    def absorb(changes: List[BlockChange]) -> None:
        nonlocal total
        for c in changes:
            # last writer wins on the working view; net change recorded once
            patch.set(c)
        total = len(patch)
        if max_blocks is not None and total > max_blocks:
            raise CompileError(f"budget exceeded: {total} > {max_blocks} blocks")

    for op in plan.operations:
        try:
            if isinstance(op, PlaceAsset):
                asset = _asset(ctx, op.asset_id)
                site = _site(ctx, op.site_id)
                origin = tuple(site["origin"])
                changes = place_asset(snapshot, analysis.ground_height, asset, origin, op.id, op.variant)
                absorb(changes)
                ctx.placed[op.id] = {"asset": asset, "origin": origin}
                fx, fz = asset.footprint
                for dx in range(fx):
                    for dz in range(fz):
                        ctx.occupied.add((origin[0] + dx, origin[1] + dz))

            elif isinstance(op, ConnectPath):
                start = _resolve_point(ctx, op.from_anchor)
                goal = _resolve_point(ctx, op.to)
                palette = _palette(ctx, op.palette_id)
                path = find_path(analysis.ground_height, analysis.water_mask, analysis.obstacle_mask, start, goal)
                changes = pave_path(snapshot, analysis.ground_height, path, op.width, palette, op.id)
                absorb(changes)
                ctx.placed[op.id] = {"path": path}
                ctx.occupied.update(path)

            elif isinstance(op, DecoratePath):
                if op.path_id not in ctx.placed or "path" not in ctx.placed[op.path_id]:
                    raise CompileError(f"unknown path '{op.path_id}'")
                path = ctx.placed[op.path_id]["path"]
                asset = _asset(ctx, op.asset_id)
                changes = decorate_path(snapshot, analysis.ground_height, path, asset, op.spacing, op.id, ctx.occupied)
                absorb(changes)

            elif isinstance(op, ScatterAssets):
                if op.zone_id not in ctx.analysis.zones:
                    raise CompileError(f"unknown zone '{op.zone_id}'")
                bbox = ctx.analysis.zones[op.zone_id]["bbox"]
                zone = ((bbox[0], bbox[1]), (bbox[2], bbox[3]))
                asset = _asset(ctx, op.asset_id)
                changes = scatter_assets(snapshot, analysis.ground_height, zone, asset, op.count, ctx.seed, op.id, ctx.occupied)
                absorb(changes)
        except (CompileError, AssetError, PathError) as e:
            op_id = getattr(e, "op_id", None) or getattr(op, "id", None)
            raise CompileError(str(e), op_id=op_id) from e

    return CompileResult(patch=patch, warnings=warnings)
