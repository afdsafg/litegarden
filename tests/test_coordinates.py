"""Coordinate transform tests (spec 5.1, highest priority)."""
from __future__ import annotations

from litegarden.scene import CoordTransform, RegionInfo


def make(position, size) -> CoordTransform:
    info = RegionInfo(region_id="main", position=position, size=size)
    return CoordTransform(region=info, enclosing_min=info.min_schem)


def test_positive_size_roundtrip():
    t = make((10, 64, -20), (8, 6, 8))
    # enclosing_min = position for positive size
    assert t.enclosing_min == (10, 64, -20)
    for p_local in [(0, 0, 0), (7, 5, 7), (3, 2, 1)]:
        p_region = t.local_to_region(p_local)
        assert t.region_to_local(p_region) == p_local
    # local (0,0,0) maps to region (0,0,0) since enclosing_min == position
    assert t.local_to_region((0, 0, 0)) == (0, 0, 0)
    assert t.local_to_region((7, 5, 7)) == (7, 5, 7)


def test_negative_size_min_schem():
    # Region at (10,64,-20) with size (-8,6,-8) covers schematic
    # x in [3,10], y in [64,69], z in [-27,-20].
    t = make((10, 64, -20), (-8, 6, -8))
    assert t.region.min_schem == (3, 64, -27)
    assert t.region.max_schem == (10, 69, -20)
    assert t.enclosing_min == (3, 64, -27)
    assert t.local_size == (8, 6, 8)


def test_negative_size_local_maps_to_min_corner():
    # p_local (0,0,0) is the schematic min corner (3,64,-27), whose region
    # coordinate is min_schem - position = (-7, 0, -7).
    t = make((10, 64, -20), (-8, 6, -8))
    assert t.local_to_schematic((0, 0, 0)) == (3, 64, -27)
    assert t.local_to_region((0, 0, 0)) == (-7, 0, -7)
    # max local corner maps to region (0,5,0)
    assert t.local_to_region((7, 5, 7)) == (0, 5, 0)


def test_no_spurious_offset_with_negative_size():
    # The forbidden formula min_schem + region_local would give, for the
    # region origin corner (region coord (0,0,0)), (3,64,-27) — wrong.
    # Correct: position + region_local = (10,64,-20).
    t = make((10, 64, -20), (-8, 6, -8))
    p_region = (0, 0, 0)
    p_schem = t.local_to_schematic(t.region_to_local(p_region))
    assert p_schem == (10, 64, -20)
    wrong = tuple(t.enclosing_min[i] + p_region[i] for i in range(3))
    assert wrong == (3, 64, -27)
    assert p_schem != wrong


def test_roundtrip_all_cells_negative_size():
    t = make((10, 64, -20), (-8, 6, -8))
    for p_local in t.iter_local():
        assert t.region_to_local(t.local_to_region(p_local)) == p_local
        assert t.contains_local(p_local)


def test_contains_local_bounds():
    t = make((0, 0, 0), (4, 4, 4))
    assert t.contains_local((0, 0, 0))
    assert t.contains_local((3, 3, 3))
    assert not t.contains_local((4, 0, 0))
    assert not t.contains_local((-1, 0, 0))
