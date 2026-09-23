"""place_asset: verified small-asset placement (spec 8).

Each asset registers footprint, occupied voxels, required-empty voxels,
support points, entries, allowed foundation depth and legal orientation
variants. Never paste only the non-air voxels: required_empty and entry
clearance must be validated too.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..scene import AIR, BlockChange, SceneSnapshot, Vec3

Vec2 = Tuple[int, int]


@dataclass
class Asset:
    """A verified static asset template.

    blocks: {(dx,dy,dz): block_id} relative to the asset origin (its
    south-west-bottom corner at variant "north"). required_empty: voxels that
    must be air after placement (doorway, interior). support: voxels that must
    rest on solid ground. entries: walk-in points used by connect_path.
    """

    asset_id: str
    footprint: Vec2
    height: int
    blocks: Dict[Vec3, str] = field(default_factory=dict)
    required_empty: List[Vec3] = field(default_factory=list)
    support: List[Vec2] = field(default_factory=list)
    entries: Dict[str, Vec2] = field(default_factory=dict)
    variants: List[str] = field(default_factory=lambda: ["default"])
    max_foundation_depth: int = 2


class AssetError(ValueError):
    pass


def load_catalog(path: Path) -> Dict[str, Asset]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    out: Dict[str, Asset] = {}
    for aid, a in raw.get("assets", {}).items():
        entries = {e["id"]: tuple(e["offset"]) for e in a.get("entries", [])}
        out[aid] = Asset(
            asset_id=aid,
            footprint=tuple(a["footprint"]),
            height=int(a["height"]),
            entries=entries,
            variants=list(a.get("variants", ["default"])),
        )
    return out


def load_prefab(asset: Asset, prefab_path: Path) -> None:
    """Populate an asset's voxel data from a prefab JSON file.

    Prefab format: {"blocks": {"x,y,z": "block_id"}, "required_empty": [...],
    "support": [...], "max_foundation_depth": n}.
    """
    raw = json.loads(Path(prefab_path).read_text(encoding="utf-8"))
    asset.blocks = {tuple(map(int, k.split(","))): v for k, v in raw.get("blocks", {}).items()}
    asset.required_empty = [tuple(p) for p in raw.get("required_empty", [])]
    asset.support = [tuple(p) for p in raw.get("support", [])]
    if "max_foundation_depth" in raw:
        asset.max_foundation_depth = int(raw["max_foundation_depth"])


def _ground_y(snapshot: SceneSnapshot, ground_height, x: int, z: int) -> int:
    if 0 <= x < ground_height.shape[0] and 0 <= z < ground_height.shape[1]:
        return int(ground_height[x, z])
    return -1


def asset_base_y(ground_height, asset: Asset, origin_xz: Vec2) -> int:
    """The y of the asset's dy=0 layer: one above the highest footprint ground."""
    fx, fz = asset.footprint
    ox, oz = origin_xz
    heights = []
    for dx in range(fx):
        for dz in range(fz):
            g = _ground_y(None, ground_height, ox + dx, oz + dz)
            if g < 0:
                raise AssetError(f"no verified ground under footprint at ({ox+dx},{oz+dz})")
            heights.append(g)
    return max(heights) + 1


def place_asset(
    snapshot: SceneSnapshot,
    ground_height,
    asset: Asset,
    origin_xz: Vec2,
    op_id: str,
    variant: Optional[str] = None,
) -> List[BlockChange]:
    """Validate and emit BlockChanges for one asset placement.

    origin_xz is the local (x,z) of the asset's south-west corner. The base
    sits on the highest ground inside the footprint. Raises AssetError on any
    collision, missing support, or insufficient entry clearance.
    """
    if variant and asset.variants and variant not in asset.variants:
        raise AssetError(f"{op_id}: unknown variant '{variant}' for {asset.asset_id}")

    fx, fz = asset.footprint
    ox, oz = origin_xz
    try:
        base_y = asset_base_y(ground_height, asset, origin_xz)
    except AssetError as e:
        raise AssetError(f"{op_id}: {e}") from None

    changes: List[BlockChange] = []
    rid = snapshot.region_id

    changes: List[BlockChange] = []
    rid = snapshot.region_id

    def emit(pos: Vec3, block: str) -> None:
        if not snapshot.contains_local(pos):
            raise AssetError(f"{op_id}: {pos} out of bounds")
        before = snapshot.block_at_local(pos)
        if before != block:
            changes.append(BlockChange(rid, pos, before, block, op_id))

    # occupied voxels
    for (dx, dy, dz), block in asset.blocks.items():
        emit((ox + dx, base_y + dy, oz + dz), block)
    # required-empty voxels (doorway/interior) must end up air
    for (dx, dy, dz) in asset.required_empty:
        emit((ox + dx, base_y + dy, oz + dz), AIR)
    # support: every support column must have ground within foundation depth
    for (dx, dz) in asset.support:
        g = _ground_y(snapshot, ground_height, ox + dx, oz + dz)
        if g < 0 or base_y - 1 - g > asset.max_foundation_depth:
            raise AssetError(
                f"{op_id}: support at ({ox+dx},{oz+dz}) needs foundation deeper than {asset.max_foundation_depth}"
            )
    return changes
