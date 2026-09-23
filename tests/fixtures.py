"""Test fixtures: build small synthetic .litematic files.

Covers the acceptance-A requirements: corner markers, non-zero Position,
negative Size, multiple block states, and a multi-region file for rejection.
"""
from __future__ import annotations

from pathlib import Path

import nbtlib
from litemapy import BlockState, Region, Schematic
from nbtlib.tag import Int, String

STONE = "minecraft:stone"
DIRT = "minecraft:dirt"
GRASS = "minecraft:grass_block"
AIR = "minecraft:air"


def _region_with_markers(x: int, y: int, z: int, sx: int, sy: int, sz: int) -> Region:
    """A region filled with stone, with distinct corner marker blocks.

    Markers are placed at the min/max corners in *schematic* space so tests can
    verify the coordinate transform handles non-zero Position and negative Size.
    """
    region = Region(x, y, z, sx, sy, sz)
    stone = BlockState(STONE)
    for pos in region.block_positions():
        region[pos] = stone
    # min corner (schematic) and max corner (schematic), in region coords
    lo = tuple(min(0, s - 1) if s > 0 else min(0, s + 1) for s in (sx, sy, sz))
    hi = tuple(max(0, s - 1) if s > 0 else max(0, s + 1) for s in (sx, sy, sz))
    region[lo] = BlockState(DIRT)
    region[hi] = BlockState(GRASS)
    return region


def write_basic(path: Path) -> dict:
    """Non-zero positive Position, positive Size."""
    schem = Schematic(name="basic", author="litegarden-test", description="fixture")
    schem.regions["main"] = _region_with_markers(10, 64, -20, 8, 6, 8)
    schem.save(str(path))
    return {"position": (10, 64, -20), "size": (8, 6, 8)}


def write_negative_size(path: Path) -> dict:
    """Non-zero Position with a negative Size on x and z."""
    schem = Schematic(name="negsize", author="litegarden-test", description="fixture")
    schem.regions["main"] = _region_with_markers(10, 64, -20, -8, 6, -8)
    schem.save(str(path))
    return {"position": (10, 64, -20), "size": (-8, 6, -8)}


def write_multi_region(path: Path) -> None:
    """Two regions: must be rejected, never silently dropped or merged."""
    schem = Schematic(name="multi", author="litegarden-test", description="fixture")
    schem.regions["a"] = _region_with_markers(0, 64, 0, 4, 4, 4)
    schem.regions["b"] = _region_with_markers(10, 64, 10, 4, 4, 4)
    schem.save(str(path))


def write_with_entities(path: Path) -> dict:
    """A region carrying an Entities list and extra metadata, to verify that
    non-whitelisted fields survive a zero-change round-trip in the NBT tree
    (semantic/type comparison, not raw bytes)."""
    meta = write_basic(path)
    raw = nbtlib.load(str(path))
    region_tag = raw["Regions"]["main"]
    region_tag["Entities"] = nbtlib.tag.List[nbtlib.tag.Compound]([])
    region_tag["PendingBlockTicks"] = nbtlib.tag.List[nbtlib.tag.Compound]([])
    raw["Metadata"]["CustomNote"] = String("preserve-me")
    raw["Metadata"]["CustomCount"] = Int(7)
    root = nbtlib.File(raw)
    root.gzipped = True
    root.save(str(path))
    return meta
