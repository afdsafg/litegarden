"""Coordinate transforms, SceneSnapshot and PatchSet.

Coordinate conventions (highest priority, see AGENTS.md):
- p_region: litemapy Region's own coordinates, may be negative.
- p_schematic = region.Position + p_region.
- p_local = p_schematic - enclosing_min (enclosing_min = min schematic coord over the scene).
- Agent/Plan only ever see p_local; export applies the inverse transform.
- region.block_positions() returns Region-own coordinates, NOT zero-based array indices.
- Never use min_schem + region_local in place of region.x/y/z + region_local:
  with negative Size regions that introduces a spurious offset.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterator, Optional, Tuple

Vec3 = Tuple[int, int, int]

AIR = "minecraft:air"


def _vadd(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _vsub(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


@dataclass(frozen=True)
class RegionInfo:
    """Metadata for the single supported region."""

    region_id: str
    position: Vec3  # schematic coords of region origin (Region.Position)
    size: Vec3  # signed size (Region.Size), may be negative per axis

    @property
    def min_schem(self) -> Vec3:
        """Minimum schematic coordinate actually covered by this region.

        Matches litemapy: positive size s spans position .. position+s-1;
        negative size s spans position+s+1 .. position.
        """
        return tuple(
            self.position[i] if self.size[i] >= 0 else self.position[i] + self.size[i] + 1
            for i in range(3)
        )  # type: ignore[return-value]

    @property
    def max_schem(self) -> Vec3:
        return tuple(
            self.position[i] + self.size[i] - 1 if self.size[i] >= 0 else self.position[i]
            for i in range(3)
        )  # type: ignore[return-value]

    @property
    def abs_size(self) -> Vec3:
        return tuple(abs(s) for s in self.size)  # type: ignore[return-value]
def _sign(v: int) -> int:
    return (v > 0) - (v < 0)


@dataclass
class CoordTransform:
    """Bidirectional p_local <-> p_region transform for the single region."""

    region: RegionInfo
    enclosing_min: Vec3  # min schematic coordinate of the whole scene

    def local_to_schematic(self, p_local: Vec3) -> Vec3:
        return _vadd(p_local, self.enclosing_min)

    def schematic_to_local(self, p_schem: Vec3) -> Vec3:
        return _vsub(p_schem, self.enclosing_min)

    def local_to_region(self, p_local: Vec3) -> Vec3:
        # p_schematic = region.Position + p_region  =>  p_region = p_schematic - Position
        return _vsub(self.local_to_schematic(p_local), self.region.position)

    def region_to_local(self, p_region: Vec3) -> Vec3:
        return self.schematic_to_local(_vadd(self.region.position, p_region))

    @property
    def local_size(self) -> Vec3:
        return self.region.abs_size

    def iter_local(self) -> Iterator[Vec3]:
        """Iterate all local coordinates covered by the region (0..abs_size-1 per axis)."""
        sx, sy, sz = self.region.abs_size
        for y in range(sy):
            for z in range(sz):
                for x in range(sx):
                    yield (x, y, z)

    def contains_local(self, p_local: Vec3) -> bool:
        sx, sy, sz = self.region.abs_size
        return 0 <= p_local[0] < sx and 0 <= p_local[1] < sy and 0 <= p_local[2] < sz


@dataclass
class BlockChange:
    """One net change against the original baseline.

    Absent coordinate = untouched; after == minecraft:air = explicit removal.
    ``action`` names what the write is doing ("place" / "path" / "support" /
    "clear" / "decorate" / "restore") so the write gate can decide whether the
    block and the action are inside the verified rules.
    """

    region_id: str
    pos_local: Vec3
    before: str  # block id at original baseline
    after: str  # block id after change (AIR means explicit removal)
    op_id: str
    action: str = "place"


@dataclass
class PatchSet:
    """Net per-coordinate changes against the original baseline."""

    changes: Dict[Vec3, BlockChange] = field(default_factory=dict)

    def set(self, change: BlockChange) -> None:
        self.changes[change.pos_local] = change

    def get(self, pos_local: Vec3) -> Optional[BlockChange]:
        return self.changes.get(pos_local)

    def __len__(self) -> int:
        return len(self.changes)

    def __iter__(self) -> Iterator[BlockChange]:
        return iter(self.changes.values())

    def non_air_after(self) -> Iterator[BlockChange]:
        """Changes whose after-state is not air (projection used by changes.litematic)."""
        return (c for c in self.changes.values() if c.after != AIR)


class SceneSnapshot:
    """Read-only baseline view of the scene.

    Wraps the litemapy region for block access plus the coordinate transform.
    Baseline block states are read through this object; the compiler keeps its
    own working view and never mutates the snapshot.
    """

    def __init__(self, region, transform: CoordTransform, data_version: int):
        self._region = region
        self.transform = transform
        self.data_version = data_version

    @property
    def region_id(self) -> str:
        return self.transform.region.region_id

    def block_at_local(self, p_local: Vec3) -> str:
        """Baseline block id at a local coordinate."""
        p_region = self.transform.local_to_region(p_local)
        return self._region[p_region].id

    def block_at_region(self, p_region: Vec3) -> str:
        return self._region[p_region].id

    def iter_local(self) -> Iterator[Vec3]:
        return self.transform.iter_local()

    def contains_local(self, p_local: Vec3) -> bool:
        return self.transform.contains_local(p_local)
