"""I/O: raw NBT snapshot, litemapy adaptation, save and re-read.

Preservation strategy (see AGENTS.md / spec 5.2):
- Keep two representations: the raw NBT tree (for saving) and the litemapy
  objects (for block manipulation).
- Save by deep-copying the raw NBT tree and, inside the edited Region,
  replacing only the re-encoded BlockStatePalette / BlockStates. Only
  whitelisted metadata fields are updated. Position, Size and
  MinecraftDataVersion are never changed.
- Unknown versions or unsupported NBT layouts -> stop, never guess.
- Never "create an empty Schematic and copy only non-air blocks" to stand in
  for preserving the original terrain.
"""
from __future__ import annotations

import copy
import os
import tempfile
from dataclasses import dataclass
from typing import Dict

import nbtlib
from litemapy import BlockState, Region, Schematic
from nbtlib.tag import Compound, Int


def block_state_from_string(state: str):
    """Build a litemapy ``BlockState`` from a normalised state string.

    ``"minecraft:oak_stairs[facing=north,half=bottom]"`` -> the block with those
    properties. A state without a ``[...]`` suffix is a plain block id.
    """
    if "[" not in state:
        return BlockState(state)
    block_id, _, rest = state.partition("[")
    rest = rest.rstrip("]")
    props = {}
    for part in rest.split(","):
        if not part.strip():
            continue
        key, _, value = part.partition("=")
        props[key.strip()] = value.strip()
    return BlockState(block_id, **props)

from .scene import AIR, CoordTransform, PatchSet, RegionInfo, SceneSnapshot, Vec3

# Metadata fields we are allowed to refresh on save (whitelist).
_METADATA_WHITELIST = frozenset({"Description", "Author", "Name"})


class UnsupportedFormatError(RuntimeError):
    """Raised when the input NBT layout or version is not supported."""


class MultiRegionError(UnsupportedFormatError):
    """Raised when the input contains more than one region."""


@dataclass
class LoadedScene:
    """A loaded litematic: raw NBT tree + litemapy view + snapshot."""

    path: str
    raw_nbt: Compound  # deep-copied original tree, used as the save base
    schematic: Schematic
    snapshot: SceneSnapshot
    data_version: int

    @property
    def region(self) -> Region:
        return self.schematic.regions[self.snapshot.region_id]


def load_scene(path: str) -> LoadedScene:
    """Load a .litematic into a LoadedScene with a read-only SceneSnapshot."""
    raw = nbtlib.load(path)
    if not isinstance(raw, Compound):
        raise UnsupportedFormatError(f"{path}: root tag is not a Compound")

    data_version = _require_int(raw, "MinecraftDataVersion", path)
    regions_tag = raw.get("Regions")
    if not isinstance(regions_tag, Compound) or len(regions_tag) == 0:
        raise UnsupportedFormatError(f"{path}: missing or empty 'Regions' compound")
    if len(regions_tag) > 1:
        raise MultiRegionError(
            f"{path}: {len(regions_tag)} regions found; MVP supports exactly one"
        )

    schematic = Schematic.fromnbt(raw)
    if len(schematic.regions) != 1:
        raise MultiRegionError(
            f"{path}: litemapy decoded {len(schematic.regions)} regions; expected 1"
        )

    region_id, region = next(iter(schematic.regions.items()))
    info = RegionInfo(
        region_id=region_id,
        position=(region.x, region.y, region.z),
        size=(region.width, region.height, region.length),
    )
    transform = CoordTransform(region=info, enclosing_min=info.min_schem)
    snapshot = SceneSnapshot(region=region, transform=transform, data_version=data_version)
    return LoadedScene(
        path=path,
        raw_nbt=copy.deepcopy(raw),
        schematic=schematic,
        snapshot=snapshot,
        data_version=data_version,
    )


def _require_int(root: Compound, key: str, path: str) -> int:
    v = root.get(key)
    if not isinstance(v, Int):
        raise UnsupportedFormatError(f"{path}: missing or non-int '{key}'")
    return int(v)


def _region_nbt(raw: Compound, region_id: str) -> Compound:
    regions = raw.get("Regions")
    assert isinstance(regions, Compound)
    r = regions.get(region_id)
    if not isinstance(r, Compound):
        raise UnsupportedFormatError(f"region '{region_id}' missing from raw NBT")
    return r


def apply_patchset(scene: LoadedScene, patch: PatchSet) -> None:
    """Apply a PatchSet to the litemapy region (working view only).

    Every change's `before` must match the original baseline; a mismatch means
    the patch was compiled against a different scene and we stop.
    """
    snap = scene.snapshot
    region = scene.region
    for change in patch:
        baseline = snap.block_at_local(change.pos_local)
        if baseline != change.before:
            raise ValueError(
                f"baseline mismatch at {change.pos_local}: patch expects "
                f"'{change.before}', scene has '{baseline}'"
            )
    for change in patch:
        p_region = snap.transform.local_to_region(change.pos_local)
        region[p_region] = block_state_from_string(change.after)


def save_scene(scene: LoadedScene, out_path: str) -> None:
    """Save by patching the raw NBT tree with the re-encoded region blocks.

    Only BlockStatePalette / BlockStates inside the edited region are replaced;
    Position, Size, MinecraftDataVersion and all other fields are preserved.
    Uses a temp file + atomic replace; the source file is never overwritten.
    """
    src_abs = os.path.abspath(scene.path)
    out_abs = os.path.abspath(out_path)
    if src_abs == out_abs:
        raise ValueError("refusing to overwrite the source file")

    raw = copy.deepcopy(scene.raw_nbt)
    region_tag = _region_nbt(raw, scene.snapshot.region_id)
    encoded = scene.region.to_nbt()

    # Replace only the block data; keep everything else from the original tree.
    for key in ("BlockStatePalette", "BlockStates"):
        if key not in encoded:
            raise UnsupportedFormatError(f"re-encoded region missing '{key}'")
        region_tag[key] = encoded[key]

    # Sanity: Position / Size must be untouched.
    for key in ("Position", "Size"):
        if key in encoded:
            orig = scene.raw_nbt["Regions"][scene.snapshot.region_id].get(key)
            if orig is not None and encoded[key] != orig:
                raise AssertionError(f"region '{key}' changed during re-encode")

    root = nbtlib.File(raw)
    root.gzipped = True

    os.makedirs(os.path.dirname(out_abs) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(out_abs) or ".", suffix=".tmp")
    os.close(fd)
    try:
        root.save(tmp)
        os.replace(tmp, out_abs)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def build_changes_schematic(scene: LoadedScene, patch: PatchSet) -> Schematic:
    """Build the changes.litematic projection: only non-air after-states.

    This is explicitly a non-air projection, NOT a general patch format with
    deletion semantics. Untouched positions inside its bounding box read as
    air; it must not be pasted over the original world with All-replace mode.
    """
    non_air = list(patch.non_air_after())
    if not non_air:
        raise ValueError("no non-air changes; nothing to project")

    locals_ = [c.pos_local for c in non_air]
    min_l = tuple(min(p[i] for p in locals_) for i in range(3))
    max_l = tuple(max(p[i] for p in locals_) for i in range(3))
    size = tuple(max_l[i] - min_l[i] + 1 for i in range(3))

    # Place the projection at the same schematic coordinates as the edits so a
    # same-placement paste lines up with the original.
    origin_schem = scene.snapshot.transform.local_to_schematic(min_l)  # type: ignore[arg-type]
    schem = Schematic(name="changes")
    region = schem.regions.setdefault(
        scene.snapshot.region_id,
        Region(origin_schem[0], origin_schem[1], origin_schem[2], size[0], size[1], size[2]),
    )
    for c in non_air:
        rel = tuple(c.pos_local[i] - min_l[i] for i in range(3))
        region[rel] = BlockState(c.after)
    return schem


def compare_to_expected(path: str, scene: LoadedScene, patch: PatchSet) -> Dict[str, int]:
    """Re-read an exported file and compare every cell against the expected state.

    Returns counts; any mismatch raises AssertionError with details.
    """
    reloaded = load_scene(path)
    snap = scene.snapshot
    rsnap = reloaded.snapshot
    if rsnap.transform.region.position != snap.transform.region.position:
        raise AssertionError("reloaded region Position differs")
    if rsnap.transform.region.size != snap.transform.region.size:
        raise AssertionError("reloaded region Size differs")
    if rsnap.data_version != snap.data_version:
        raise AssertionError("reloaded MinecraftDataVersion differs")

    checked = changed = 0
    for p_local in snap.iter_local():
        expected_change = patch.get(p_local)
        expected = expected_change.after if expected_change else snap.block_at_local(p_local)
        got = rsnap.block_at_local(p_local)
        checked += 1
        if expected_change:
            changed += 1
        if got != expected:
            raise AssertionError(
                f"cell {p_local}: expected '{expected}', reloaded '{got}'"
            )
    return {"checked": checked, "changed": changed}


# --------------------------------------------------------------------------
# P0: NBT preservation and block-entity host dependencies (spec 7)
# --------------------------------------------------------------------------


class BlockEntityHostChanged(AssertionError):
    """A retained tile entity's host block changed, so its data is orphaned."""

    code = "BLOCK_ENTITY_HOST_CHANGED"

    def __init__(self, pos_local: Vec3, before: str, after: str, entity_id: str) -> None:
        super().__init__(
            f"{pos_local}: tile entity '{entity_id}' is retained but its host block "
            f"changed from '{before}' to '{after}'"
        )
        self.pos_local = tuple(pos_local)
        self.before = before
        self.after = after
        self.entity_id = entity_id

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "message": str(self),
            "pos_local": list(self.pos_local),
            "entity_id": self.entity_id,
            "expected": self.before,
            "actual": self.after,
            "rule_id": "nbt/block_entity_host",
        }


def tile_entity_records(scene: LoadedScene) -> list:
    """Tile entities of the loaded region, each with its host block.

    In the litematic layout TileEntities[*].x/y/z are map-anchored (schematic)
    coordinates, not litemapy region indices; they are converted with the same
    transform the rest of the project uses. A coordinate outside the region is
    reported rather than silently dropped, and a malformed entry stops the run
    instead of being guessed at.
    """
    region_tag = _region_nbt(scene.raw_nbt, scene.snapshot.region_id)
    tag = region_tag.get("TileEntities")
    if tag is None:
        return []
    out = []
    for i, entry in enumerate(tag):
        if not isinstance(entry, Compound):
            raise UnsupportedFormatError(
                f"TileEntities[{i}] is not a Compound; refusing to guess"
            )
        missing = [k for k in ("x", "y", "z") if k not in entry]
        if missing:
            raise UnsupportedFormatError(
                f"TileEntities[{i}] is missing {missing}; refusing to guess a position"
            )
        p_schem = (int(entry["x"]), int(entry["y"]), int(entry["z"]))
        p_local = scene.snapshot.transform.schematic_to_local(p_schem)
        inside = scene.snapshot.contains_local(p_local)
        out.append({
            "index": i,
            "id": str(entry.get("id", "")),
            "pos_schematic": list(p_schem),
            "pos_local": list(p_local),
            "inside_region": inside,
            "host": scene.snapshot.block_at_local(p_local) if inside else None,
        })
    return out


def entity_host_positions(scene: LoadedScene) -> list:
    """Local positions whose host block must not change.

    Tile entities are treated conservatively: this round never migrates an
    entity, so the voxel hosting it is read-only. A tile entity outside the
    region is reported instead of being quietly ignored.
    """
    records = tile_entity_records(scene)
    outside = [r for r in records if not r["inside_region"]]
    if outside:
        raise UnsupportedFormatError(
            f"{len(outside)} tile entit(ies) fall outside the region bounds: "
            f"{[r['pos_schematic'] for r in outside][:4]}"
        )
    return sorted({tuple(r["pos_local"]) for r in records})  # type: ignore[misc]


def check_block_entity_hosts(original: LoadedScene, reloaded: LoadedScene) -> None:
    """Refuse a save that retained a tile entity but changed its host block."""
    for rec in tile_entity_records(original):
        if not rec["inside_region"]:
            continue
        p_local = tuple(rec["pos_local"])
        if not reloaded.snapshot.contains_local(p_local):
            raise UnsupportedFormatError(
                f"reloaded scene does not contain tile entity position {p_local}"
            )
        before = original.snapshot.block_at_local(p_local)
        after = reloaded.snapshot.block_at_local(p_local)
        if before != after:
            raise BlockEntityHostChanged(p_local, before, after, rec["id"])


def compare_nbt_preservation(
    original: LoadedScene,
    reloaded: LoadedScene,
    allowed_paths=None,
) -> list:
    """Type-sensitive NBT comparison between the source and a re-read export.

    Only the edited region's palette/BlockStates and the whitelisted metadata
    fields may differ. Returns the raw difference list; callers that need a
    hard failure use :func:`ensure_export_preserved`.
    """
    from .nbt_compare import allowed_save_paths, compare_nbt

    region_id = original.snapshot.region_id
    paths = allowed_paths if allowed_paths is not None else allowed_save_paths(region_id)
    return compare_nbt(original.raw_nbt, reloaded.raw_nbt, paths)


def ensure_export_preserved(original: LoadedScene, reloaded: LoadedScene) -> None:
    """Verify a re-read export preserves NBT types/values and host blocks.

    Raises NbtPreservationError for a non-whitelisted NBT change and
    BlockEntityHostChanged when retained data would be orphaned. A screenshot
    can never prove this; it is a data-layer check only.
    """
    from .nbt_compare import NbtPreservationError, allowed_save_paths, ensure_nbt_preserved

    paths = allowed_save_paths(original.snapshot.region_id)
    try:
        ensure_nbt_preserved(original.raw_nbt, reloaded.raw_nbt, paths)
    except NbtPreservationError as e:
        first = e.differences[0] if e.differences else None
        raise NbtPreservationError(
            f"{len(e.differences)} non-whitelisted NBT difference(s) between the source "
            f"and the re-read export"
            + (f"; first: {first.code} at {first.tag_path}" if first else ""),
            e.differences,
        ) from None
    check_block_entity_hosts(original, reloaded)
