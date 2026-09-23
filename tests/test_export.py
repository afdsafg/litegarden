"""Export tests: changes projection and output files (spec 9)."""
from __future__ import annotations

import json
from pathlib import Path

from litegarden.io import (
    apply_patchset,
    build_changes_schematic,
    load_scene,
    save_scene,
)
from litegarden.scene import AIR, BlockChange, PatchSet

from .fixtures import GRASS, write_basic


def test_changes_projection_only_non_air(tmp_path: Path):
    src = tmp_path / "in.litematic"
    write_basic(src)
    scene = load_scene(str(src))
    snap = scene.snapshot

    ps = PatchSet()
    # one placement, one removal
    ps.set(
        BlockChange(snap.region_id, (2, 2, 2), snap.block_at_local((2, 2, 2)), GRASS, "op1")
    )
    ps.set(
        BlockChange(snap.region_id, (3, 3, 3), snap.block_at_local((3, 3, 3)), AIR, "op2")
    )
    apply_patchset(scene, ps)

    changes = build_changes_schematic(scene, ps)
    region = next(iter(changes.regions.values()))
    # Only the non-air change is present; the removal is not in the projection.
    ids = {
        region[(x, y, z)].id
        for x, y, z in region.block_positions()
    }
    assert GRASS in ids
    # bounding box spans both edits but the removed cell reads as air
    assert region[(0, 0, 0)].id in (GRASS, AIR)


def test_changes_projection_empty_raises(tmp_path: Path):
    src = tmp_path / "in.litematic"
    write_basic(src)
    scene = load_scene(str(src))
    try:
        build_changes_schematic(scene, PatchSet())
    except ValueError as e:
        assert "no non-air" in str(e)
    else:
        raise AssertionError("expected ValueError for empty patch")
