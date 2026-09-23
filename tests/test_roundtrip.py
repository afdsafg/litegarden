"""Acceptance A: zero-change round-trip.

Zero-change output must match the input cell-for-cell, the input file hash
must be unchanged, and version/coordinates/raw-NBT non-whitelisted fields
must be preserved. Multi-region input must be rejected.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import nbtlib
import pytest

from litegarden.io import (
    MultiRegionError,
    apply_patchset,
    compare_to_expected,
    load_scene,
    save_scene,
)
from litegarden.scene import PatchSet

from .fixtures import (
    write_basic,
    write_multi_region,
    write_negative_size,
    write_with_entities,
)


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.mark.parametrize("writer", [write_basic, write_negative_size])
def test_zero_change_roundtrip(tmp_path: Path, writer):
    src = tmp_path / "in.litematic"
    writer(src)
    before_hash = _sha256(src)

    scene = load_scene(str(src))
    patch = PatchSet()  # zero changes
    apply_patchset(scene, patch)

    out = tmp_path / "out.litematic"
    save_scene(scene, str(out))

    # Input file untouched.
    assert _sha256(src) == before_hash
    # Cell-for-cell identical, Position/Size/DataVersion preserved.
    stats = compare_to_expected(str(out), scene, patch)
    assert stats["changed"] == 0
    assert stats["checked"] > 0


def test_multi_region_rejected(tmp_path: Path):
    src = tmp_path / "multi.litematic"
    write_multi_region(src)
    with pytest.raises(MultiRegionError):
        load_scene(str(src))


def test_refuses_to_overwrite_source(tmp_path: Path):
    src = tmp_path / "in.litematic"
    write_basic(src)
    scene = load_scene(str(src))
    with pytest.raises(ValueError, match="overwrite"):
        save_scene(scene, str(src))


def test_non_whitelisted_nbt_preserved(tmp_path: Path):
    src = tmp_path / "in.litematic"
    write_with_entities(src)
    scene = load_scene(str(src))
    out = tmp_path / "out.litematic"
    save_scene(scene, str(out))

    orig = nbtlib.load(str(src))
    new = nbtlib.load(str(out))

    # Metadata: custom fields preserved, version unchanged.
    assert new["Metadata"]["CustomNote"] == orig["Metadata"]["CustomNote"]
    assert new["Metadata"]["CustomCount"] == orig["Metadata"]["CustomCount"]
    assert new["MinecraftDataVersion"] == orig["MinecraftDataVersion"]

    # Region: Position/Size/Entities/PendingBlockTicks preserved.
    o, n = orig["Regions"]["main"], new["Regions"]["main"]
    assert n["Position"] == o["Position"]
    assert n["Size"] == o["Size"]
    assert "Entities" in n and "PendingBlockTicks" in n
    assert type(n["Entities"]) is type(o["Entities"])
