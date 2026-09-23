"""Preservation tests: baseline matching and air semantics (spec 5.2/5.3)."""
from __future__ import annotations

from pathlib import Path

import pytest

from litegarden.io import apply_patchset, compare_to_expected, load_scene, save_scene
from litegarden.scene import AIR, BlockChange, PatchSet

from .fixtures import DIRT, GRASS, STONE, write_basic


def _patch_one(scene, pos_local, after, op_id="op1") -> PatchSet:
    before = scene.snapshot.block_at_local(pos_local)
    ps = PatchSet()
    ps.set(
        BlockChange(
            region_id=scene.snapshot.region_id,
            pos_local=pos_local,
            before=before,
            after=after,
            op_id=op_id,
        )
    )
    return ps


def test_apply_and_verify_change(tmp_path: Path):
    src = tmp_path / "in.litematic"
    write_basic(src)
    scene = load_scene(str(src))
    patch = _patch_one(scene, (2, 2, 2), GRASS)
    apply_patchset(scene, patch)
    out = tmp_path / "out.litematic"
    save_scene(scene, str(out))
    stats = compare_to_expected(str(out), scene, patch)
    assert stats["changed"] == 1


def test_baseline_mismatch_rejected(tmp_path: Path):
    src = tmp_path / "in.litematic"
    write_basic(src)
    scene = load_scene(str(src))
    ps = PatchSet()
    ps.set(
        BlockChange(
            region_id=scene.snapshot.region_id,
            pos_local=(0, 0, 0),
            before="minecraft:bedrock",  # wrong: baseline is dirt (min corner)
            after=GRASS,
            op_id="op1",
        )
    )
    with pytest.raises(ValueError, match="baseline mismatch"):
        apply_patchset(scene, ps)


def test_explicit_removal_is_air_after(tmp_path: Path):
    src = tmp_path / "in.litematic"
    write_basic(src)
    scene = load_scene(str(src))
    # Remove the min-corner dirt block: after == air is an explicit removal,
    # distinct from "coordinate absent = untouched".
    patch = _patch_one(scene, (0, 0, 0), AIR)
    assert patch.get((0, 0, 0)).after == AIR
    assert patch.get((1, 1, 1)) is None  # untouched
    apply_patchset(scene, patch)
    out = tmp_path / "out.litematic"
    save_scene(scene, str(out))
    reloaded = load_scene(str(out))
    assert reloaded.snapshot.block_at_local((0, 0, 0)) == AIR
    # Neighbour untouched.
    assert reloaded.snapshot.block_at_local((1, 0, 0)) == STONE
