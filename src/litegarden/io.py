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
        region[p_region] = BlockState(change.after)


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
