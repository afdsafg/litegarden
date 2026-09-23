"""Tests for the ``walk_no_jump_v1`` traversal profile (P1).

Every check is driven through a read-only dict sampler; an unknown voxel is
``None`` and must never be treated as air.
"""
from __future__ import annotations

import pytest

from litegarden.traversal import (
    PROFILE_WALK_NO_JUMP_V1,
    check_boundary_interface,
    check_connectivity,
    check_entry,
    check_headroom,
    check_road,
    classify_state,
    cut_fill_report,
    parse_state,
    surface_y_units,
)

AIR = "minecraft:air"
STONE = "minecraft:stone"
BRICKS = "minecraft:stone_bricks"
BOTTOM_SLAB = "minecraft:stone_brick_slab[type=bottom]"
TOP_SLAB = "minecraft:stone_brick_slab[type=top]"


def _sampler_from(grid, default=AIR):
    """Simplest possible read-only sampler: a dict plus a default state."""
    return lambda pos: grid.get(tuple(pos), default)


def _flat_road_grid(xs, zs=(-3, -2, -1, 0, 1, 2), y=5, surface=BRICKS, support=STONE):
    """Full-cube road surface at ``y`` with a supporting layer at ``y - 1``."""
    grid = {}
    for x in xs:
        for z in zs:
            grid[(x, y, z)] = surface
            grid[(x, y - 1, z)] = support
    return grid


def _step_scene(stair):
    """One-block rise from (1,5,0) to (2,6,0); ``stair`` fills the transition."""
    grid = _flat_road_grid(range(-1, 4))
    grid[(2, 6, 0)] = BRICKS
    grid[(2, 5, 0)] = STONE
    grid[(1, 6, 0)] = stair
    return _sampler_from(grid)


def _entry_scene():
    """1 voxel wide, 2 voxels high corridor along +z with the floor at y=5."""
    grid = {}
    for z in range(0, 5):
        grid[(-1, 5, z)] = BRICKS
        grid[(-1, 4, z)] = STONE
    return grid


def _entry_contract():
    return {
        "id": "pavilion_main",
        "bounds": {"min": [-1, 6, 0], "max_exclusive": [0, 8, 5]},
        "inside": [-1, 5, 0],
        "outside": [-1, 5, 4],
        "width": 1,
    }


# --------------------------------------------------------------------------- #
# state parsing / classification
# --------------------------------------------------------------------------- #
def test_parse_state_air_and_stairs():
    assert parse_state(AIR) == ("minecraft:air", {})
    block_id, props = parse_state(
        "minecraft:oak_stairs[facing=north,half=bottom,shape=straight]"
    )
    assert block_id == "minecraft:oak_stairs"
    assert props == {"facing": "north", "half": "bottom", "shape": "straight"}
    # anything that is not a canonical state string is an error, not a guess
    with pytest.raises(ValueError):
        parse_state("minecraft:oak_slab[type=bottom")


def test_attribute_order_insensitive():
    ordered = parse_state("minecraft:oak_slab[type=bottom,waterlogged=false]")
    reordered = parse_state("minecraft:oak_slab[waterlogged=false,type=bottom]")
    assert ordered == reordered
    assert ordered == (
        "minecraft:oak_slab",
        {"type": "bottom", "waterlogged": "false"},
    )
    shape_ordered = classify_state(
        "minecraft:oak_slab[type=bottom,waterlogged=false]"
    )
    shape_reordered = classify_state(
        "minecraft:oak_slab[waterlogged=false,type=bottom]"
    )
    assert shape_ordered == shape_reordered
    assert shape_ordered.surface == 0.5
    assert shape_ordered.walkable and shape_ordered.supports


def test_d01_none_is_unsupported_and_never_air():
    shape = classify_state(None)
    assert shape.kind == "unsupported"
    assert shape.detail == "unknown voxel"
    assert shape.walkable is False
    assert shape.supports is False
    assert shape.surface == 0.0
    assert shape.platforms == ()


def test_none_voxels_block_a_road():
    # only the road surface is known: headroom above and support below are unknown
    grid = {(0, 5, 0): BRICKS, (1, 5, 0): BRICKS}
    sampler = _sampler_from(grid, default=None)
    issues = check_road(sampler, [(0, 5, 0), (1, 5, 0)], 1)
    assert {(i.code, i.pos_local) for i in issues} == {
        ("SUPPORT_RULE_VIOLATION", (0, 5, 0)),
        ("SUPPORT_RULE_VIOLATION", (1, 5, 0)),
        ("PATH_HEADROOM_BLOCKED", (0, 6, 0)),
        ("PATH_HEADROOM_BLOCKED", (1, 6, 0)),
    }
    # unknown is reported as unknown, not as air
    assert all("<unknown>" in i.actual for i in issues)


def test_water_is_unsupported_and_not_walkable():
    shape = classify_state("minecraft:water")
    assert shape.kind == "unsupported"
    assert shape.walkable is False
    assert shape.supports is False
    assert "block not in verified walk profile" in shape.detail
    grid = _flat_road_grid(range(-1, 3))
    grid[(1, 5, 1)] = "minecraft:water"
    issues = check_road(_sampler_from(grid), [(0, 5, 0), (1, 5, 0)], 3)
    assert [(i.code, i.pos_local) for i in issues] == [
        ("PATH_HEADROOM_BLOCKED", (1, 5, 1))
    ]


def test_unsupported_stairs_half_top_rejected():
    shape = classify_state(
        "minecraft:oak_stairs[facing=north,half=top,shape=straight]"
    )
    assert shape.kind == "unsupported"
    assert shape.walkable is False and shape.supports is False
    assert "half=top" in shape.detail
    # an unlisted stair block is not guessed either
    unlisted = classify_state(
        "minecraft:acacia_stairs[facing=north,half=bottom,shape=straight]"
    )
    assert unlisted.kind == "unsupported"
    # only straight stairs are verified
    inner = classify_state(
        "minecraft:oak_stairs[facing=north,half=bottom,shape=inner_left]"
    )
    assert inner.kind == "unsupported"
    assert "shape=inner_left" in inner.detail
    # as a road surface voxel it must fail the road check
    grid = _flat_road_grid(range(-1, 3))
    grid[(1, 5, 0)] = "minecraft:oak_stairs[facing=north,half=top,shape=straight]"
    issues = check_connectivity(_sampler_from(grid), [(0, 5, 0), (1, 5, 0)])
    assert len(issues) == 1
    assert issues[0].code == "PATH_HEADROOM_BLOCKED"
    assert issues[0].pos_local == (1, 5, 0)
    assert "unsupported" in issues[0].actual


def test_bottom_and_double_slab_and_defaults():
    assert classify_state("minecraft:stone_slab").surface == 0.5  # type defaults to bottom
    assert classify_state("minecraft:stone_slab[type=bottom]").surface == 0.5
    double = classify_state("minecraft:stone_slab[type=double]")
    assert double.surface == 1.0 and double.platforms == (1.0,)
    assert double.needs_support_below is True
    assert classify_state("minecraft:stone_slab[type=sideways]").kind == "unsupported"
    default_stair = classify_state(
        "minecraft:stone_brick_stairs[facing=east]"  # half/shape default to bottom/straight
    )
    assert default_stair.kind == "stairs"
    assert default_stair.platforms == (0.5, 1.0)
    assert default_stair.facing == "east"


def test_unknown_profile_is_rejected():
    with pytest.raises(ValueError):
        classify_state(STONE, profile="walk_jump_v2")
    with pytest.raises(ValueError):
        check_road(_sampler_from({}), [(0, 0, 0)], 1, profile="walk_jump_v2")
    assert PROFILE_WALK_NO_JUMP_V1 == "walk_no_jump_v1"


def test_surface_y_units_column_helper():
    assert surface_y_units([]) == 0.0
    assert surface_y_units([classify_state(AIR)]) == 0.0
    assert surface_y_units([classify_state(None)]) == 0.0
    assert surface_y_units([classify_state(STONE)]) == 1.0
    assert surface_y_units([classify_state(BOTTOM_SLAB)]) == 0.5
    assert surface_y_units([classify_state(AIR), classify_state(STONE)]) == 2.0
    assert surface_y_units([classify_state(STONE), classify_state(BOTTOM_SLAB)]) == 1.5


# --------------------------------------------------------------------------- #
# D01: full road width / headroom
# --------------------------------------------------------------------------- #
def test_flat_full_cube_road_has_no_issues():
    sampler = _sampler_from(_flat_road_grid(range(-1, 4)))
    cells = [(0, 5, 0), (1, 5, 0), (2, 5, 0)]
    assert check_road(sampler, cells, 3) == []
    assert check_connectivity(sampler, cells) == []
    assert check_headroom(sampler, (1, 5, 0)) == []
    # JSON-ish list input is accepted too
    assert check_road(sampler, [[0, 5, 0], [1, 5, 0]], 1) == []


def test_d01_side_collision_blocks_road():
    grid = _flat_road_grid(range(-1, 4))
    grid[(2, 6, 1)] = STONE  # wall one voxel above the side lane of the road
    issues = check_road(_sampler_from(grid), [(0, 5, 0), (1, 5, 0), (2, 5, 0)], 3)
    assert len(issues) == 1
    assert issues[0].code == "PATH_HEADROOM_BLOCKED"
    assert issues[0].rule_id == "walk_no_jump_v1/headroom"
    assert issues[0].pos_local == (2, 6, 1)  # the concrete side voxel
    assert STONE in issues[0].actual


def test_d01_headroom_blocked_above():
    grid = _flat_road_grid(range(-1, 4))
    grid[(1, 6, 0)] = STONE
    issues = check_road(_sampler_from(grid), [(0, 5, 0), (1, 5, 0), (2, 5, 0)], 3)
    assert len(issues) == 1
    assert issues[0].code == "PATH_HEADROOM_BLOCKED"
    assert issues[0].pos_local == (1, 6, 0)
    assert "headroom" in issues[0].detail


def test_road_width_offsets_match_pave_path_rounding():
    grid = _flat_road_grid(range(-1, 4))
    grid[(1, 6, 1)] = STONE
    issues = check_road(_sampler_from(grid), [(0, 5, 0), (1, 5, 0), (2, 5, 0)], 2)
    # width=2 -> half = 1 -> offsets -1..1, same as operations.path.pave_path
    assert [(i.code, i.pos_local) for i in issues] == [
        ("PATH_HEADROOM_BLOCKED", (1, 6, 1))
    ]


def test_road_along_z_uses_x_lateral_axis():
    grid = {}
    for z in range(0, 4):
        for x in (-1, 0, 1):
            grid[(x, 5, z)] = BRICKS
            grid[(x, 4, z)] = STONE
    sampler = _sampler_from(grid)
    cells = [(0, 5, 0), (0, 5, 1), (0, 5, 2)]
    assert check_road(sampler, cells, 3) == []
    grid[(1, 6, 0)] = STONE
    issues = check_road(sampler, cells, 3)
    assert [(i.code, i.pos_local) for i in issues] == [
        ("PATH_HEADROOM_BLOCKED", (1, 6, 0))
    ]


def test_road_hole_in_surface_is_not_walkable():
    grid = _flat_road_grid(range(-1, 4))
    grid[(1, 5, -1)] = AIR
    issues = check_road(_sampler_from(grid), [(0, 5, 0), (1, 5, 0), (2, 5, 0)], 3)
    assert [(i.code, i.pos_local) for i in issues] == [
        ("PATH_STEP_INVALID", (1, 5, -1))
    ]


# --------------------------------------------------------------------------- #
# D02: steps and transitions
# --------------------------------------------------------------------------- #
def test_d02_one_block_step_without_transition_rejected():
    grid = _flat_road_grid(range(-1, 4))
    grid[(2, 6, 0)] = BRICKS
    grid[(2, 5, 0)] = STONE
    issues = check_connectivity(_sampler_from(grid), [(1, 5, 0), (2, 6, 0)])
    assert len(issues) == 1
    issue = issues[0]
    assert issue.code == "PATH_STEP_INVALID"
    assert issue.rule_id == "walk_no_jump_v1/step"
    assert issue.pos_local == (1, 5, 0)  # the lower unit
    assert issue.expected == "step <= 0.5 without jump"
    assert issue.actual == "step 1.0"
    assert "transition" in issue.detail


def test_d02_one_block_step_with_slab_transition_allowed():
    grid = _flat_road_grid(range(-1, 4))
    grid[(2, 6, 0)] = BRICKS
    grid[(2, 5, 0)] = STONE
    grid[(1, 6, 0)] = BOTTOM_SLAB  # half step above the lower unit
    assert check_connectivity(_sampler_from(grid), [(1, 5, 0), (2, 6, 0)]) == []


def test_d02_stair_transition_direction_matters():
    ramp_east = "minecraft:stone_brick_stairs[facing=east,half=bottom,shape=straight]"
    assert check_connectivity(_step_scene(ramp_east), [(1, 5, 0), (2, 6, 0)]) == []
    for stair in (
        "minecraft:stone_brick_stairs[facing=west,half=bottom,shape=straight]",
        "minecraft:stone_brick_stairs[facing=north,half=bottom,shape=straight]",
        "minecraft:stone_brick_stairs[half=bottom,shape=straight]",  # no facing
    ):
        issues = check_connectivity(_step_scene(stair), [(1, 5, 0), (2, 6, 0)])
        assert [i.code for i in issues] == ["PATH_STEP_INVALID"], stair
        assert issues[0].pos_local == (1, 5, 0)


def test_d02_top_slab_is_different_from_bottom_slab():
    bottom = classify_state(BOTTOM_SLAB)
    top = classify_state(TOP_SLAB)
    assert bottom.surface == 0.5
    assert top.surface == 1.0
    assert bottom != top
    assert bottom.platforms == (0.5,)
    assert top.platforms == (1.0,)
    assert bottom.needs_support_below is True
    assert top.needs_support_below is False  # the lower half of the voxel is empty
    assert surface_y_units([bottom]) == 0.5
    assert surface_y_units([top]) == 1.0
    # the top slab cannot split a 1.0 step into two 0.5 hops
    issues = check_connectivity(_step_scene(TOP_SLAB), [(1, 5, 0), (2, 6, 0)])
    assert {(i.code, i.pos_local) for i in issues} == {
        ("PATH_STEP_INVALID", (1, 5, 0)),
        ("PATH_HEADROOM_BLOCKED", (1, 6, 0)),
    }


def test_top_slab_hangs_without_support_below():
    grid = {(1, 5, 0): TOP_SLAB, (0, 5, 0): BRICKS, (0, 4, 0): STONE}
    assert check_connectivity(_sampler_from(grid), [(1, 5, 0)]) == []


def test_support_below_air_reports_support_violation():
    grid = _flat_road_grid(range(-1, 3))
    del grid[(1, 4, 0)]  # the supporting voxel under the road surface is gone
    issues = check_connectivity(_sampler_from(grid), [(1, 5, 0)])
    assert len(issues) == 1
    issue = issues[0]
    assert issue.code == "SUPPORT_RULE_VIOLATION"
    assert issue.rule_id == "walk_no_jump_v1/support"
    assert issue.pos_local == (1, 5, 0)
    assert "(1, 4, 0)" in issue.actual
    assert AIR in issue.actual


def test_d02_side_lane_step_without_transition_rejected():
    grid = _flat_road_grid(range(-1, 4))
    for z in (-1, 0, 1):
        grid[(2, 6, z)] = BRICKS
        grid[(2, 5, z)] = STONE
    grid[(1, 6, 0)] = BOTTOM_SLAB  # only the centre lane gets its transition
    issues = check_road(
        _sampler_from(grid), [(0, 5, 0), (1, 5, 0), (2, 6, 0)], 3
    )
    assert {i.code for i in issues} == {"PATH_STEP_INVALID"}
    assert [i.pos_local for i in issues] == [(1, 5, -1), (1, 5, 1)]


def test_road_full_width_step_with_transitions_is_clean():
    grid = _flat_road_grid(range(-1, 4))
    for z in (-1, 0, 1):
        grid[(2, 6, z)] = BRICKS
        grid[(2, 5, z)] = STONE
        grid[(1, 6, z)] = BOTTOM_SLAB
    assert (
        check_road(_sampler_from(grid), [(0, 5, 0), (1, 5, 0), (2, 6, 0)], 3) == []
    )


def test_path_gap_reports_disconnected():
    sampler = _sampler_from(_flat_road_grid(range(-1, 5)))
    issues = check_connectivity(sampler, [(0, 5, 0), (2, 5, 0)])
    assert [i.code for i in issues] == ["PATH_DISCONNECTED"]
    assert issues[0].rule_id == "walk_no_jump_v1/connectivity"
    assert issues[0].pos_local == (0, 5, 0)
    diagonal = check_connectivity(sampler, [(0, 5, 0), (1, 5, 1)])
    assert [i.code for i in diagonal] == ["PATH_DISCONNECTED"]


# --------------------------------------------------------------------------- #
# D05: selection boundary interface
# --------------------------------------------------------------------------- #
def test_d05_boundary_interface_state_change_detected():
    base = {(-1, 5, 0): BRICKS, (0, 5, 0): BRICKS, (1, 5, 0): BRICKS}
    candidate = {(-1, 5, 0): BRICKS, (0, 5, 0): "minecraft:cobblestone", (1, 5, 0): BRICKS}
    interface = {
        "id": "b1",
        "cells": [[0, 5, 0]],
        "link_from": [-1, 5, 0],
        "link_to": [1, 5, 0],
    }
    issues = check_boundary_interface(
        _sampler_from(base), _sampler_from(candidate), interface
    )
    assert len(issues) == 1
    issue = issues[0]
    assert issue.code == "BOUNDARY_ANCHOR_BROKEN"
    assert issue.rule_id == "walk_no_jump_v1/boundary"
    assert issue.pos_local == (0, 5, 0)
    assert BRICKS in issue.actual and "minecraft:cobblestone" in issue.actual


def test_d05_boundary_interface_state_compare_is_order_insensitive():
    interface = {
        "id": "b1",
        "cells": [[0, 5, 0]],
        "link_from": [-1, 5, 0],
        "link_to": [1, 5, 0],
    }
    base = _sampler_from(
        {
            (-1, 5, 0): BRICKS,
            (1, 5, 0): BRICKS,
            (0, 5, 0): "minecraft:oak_slab[type=bottom,waterlogged=false]",
        }
    )
    candidate = _sampler_from(
        {
            (-1, 5, 0): BRICKS,
            (1, 5, 0): BRICKS,
            (0, 5, 0): "minecraft:oak_slab[waterlogged=false,type=bottom]",
        }
    )
    assert check_boundary_interface(base, candidate, interface) == []


def test_d05_boundary_interface_height_mismatch():
    scene = {
        (-1, 6, 0): BRICKS,
        (0, 6, 0): BRICKS,
        (1, 8, 0): BRICKS,
    }
    sampler = _sampler_from(scene)
    interface = {
        "id": "b2",
        "cells": [[0, 6, 0]],
        "link_from": [-1, 6, 0],
        "link_to": [1, 8, 0],
    }
    issues = check_boundary_interface(sampler, sampler, interface)
    assert len(issues) == 1
    issue = issues[0]
    assert issue.code == "BOUNDARY_ANCHOR_BROKEN"
    assert issue.pos_local == (0, 6, 0)
    assert "step 2.0" in issue.actual
    assert "7.0" in issue.actual and "9.0" in issue.actual


def test_d05_boundary_interface_lateral_mismatch():
    scene = {(-1, 5, 0): BRICKS, (0, 5, 0): BRICKS, (2, 5, 2): BRICKS}
    sampler = _sampler_from(scene)
    interface = {
        "id": "b3",
        "cells": [[0, 5, 0]],
        "link_from": [-1, 5, 0],
        "link_to": [2, 5, 2],
    }
    issues = check_boundary_interface(sampler, sampler, interface)
    assert len(issues) == 1
    issue = issues[0]
    assert issue.code == "BOUNDARY_ANCHOR_BROKEN"
    assert issue.pos_local == (-1, 5, 0)
    assert "lateral" in issue.expected and "width mismatch" in issue.detail


def test_d05_boundary_interface_without_bridging_voxel():
    scene = {
        (-1, 5, 0): BRICKS,
        (0, 5, 0): BRICKS,
        (1, 5, 0): BRICKS,
        (2, 5, 0): BRICKS,
        (0, 5, 1): BRICKS,
    }
    sampler = _sampler_from(scene)
    interface = {
        "id": "b4",
        "cells": [[0, 5, 1]],
        "link_from": [-1, 5, 0],
        "link_to": [2, 5, 0],
    }
    issues = check_boundary_interface(sampler, sampler, interface)
    assert [i.code for i in issues] == ["BOUNDARY_ANCHOR_BROKEN"]
    assert "does not" in issues[0].detail


def test_d05_boundary_interface_broken_candidate_surface():
    base = {(-1, 5, 0): BRICKS, (0, 5, 0): BRICKS, (1, 5, 0): BRICKS}
    candidate = dict(base)
    candidate[(0, 5, 0)] = "minecraft:water"  # candidate punctures the interface
    interface = {
        "id": "b5",
        "cells": [[0, 5, 0]],
        "link_from": [-1, 5, 0],
        "link_to": [1, 5, 0],
    }
    issues = check_boundary_interface(
        _sampler_from(base), _sampler_from(candidate), interface
    )
    assert {i.code for i in issues} == {"BOUNDARY_ANCHOR_BROKEN"}
    assert (0, 5, 0) in {i.pos_local for i in issues}


# --------------------------------------------------------------------------- #
# entrance validation
# --------------------------------------------------------------------------- #
def test_entry_ok_when_open():
    sampler = _sampler_from(_entry_scene())
    assert check_entry(sampler, _entry_contract()) == []


def test_entry_blocked_by_obstruction():
    grid = _entry_scene()
    grid[(-1, 6, 2)] = STONE  # a solid block inside the passage volume
    issues = check_entry(_sampler_from(grid), _entry_contract())
    assert issues
    assert {i.code for i in issues} == {"ENTRY_BLOCKED"}
    assert {i.pos_local for i in issues} == {(-1, 6, 2)}
    assert any("blocked" in i.detail for i in issues)


def test_entry_disconnected_inside_outside():
    # the passage volume itself is all air, but the floor of the corridor is
    # missing one cell in front of the inside landing: crossing it needs a jump
    grid = _entry_scene()
    del grid[(-1, 5, 2)]
    issues = check_entry(_sampler_from(grid), _entry_contract())
    assert issues
    assert {i.code for i in issues} == {"ENTRY_BLOCKED"}
    assert {i.pos_local for i in issues} == {(-1, 5, 0)}  # the unreachable landing
    assert any("does not connect" in i.detail for i in issues)


def test_entry_walled_inside_rejected():
    grid = _entry_scene()
    for z in (1, 2, 3):  # a wall at the standing layer seals off the inside
        grid[(-1, 6, z)] = STONE
        grid[(-1, 5, z)] = STONE
    entry = {
        "id": "pavilion_walled",
        "bounds": {"min": [-1, 6, 0], "max_exclusive": [0, 8, 1]},
        "inside": [-1, 5, 0],
        "outside": [-1, 5, 4],
        "width": 1,
    }
    issues = check_entry(_sampler_from(grid), entry)
    assert issues
    assert {i.code for i in issues} == {"ENTRY_BLOCKED"}
    assert {i.pos_local for i in issues} == {(-1, 5, 0)}


def test_entry_width_must_fit_the_passage_volume():
    entry = _entry_contract()
    entry["width"] = 3
    issues = check_entry(_sampler_from(_entry_scene()), entry)
    assert {i.code for i in issues} == {"ENTRY_BLOCKED"}
    assert any("width" in i.detail for i in issues)


def test_entry_landing_without_support_is_reported():
    grid = _entry_scene()
    del grid[(-1, 4, 4)]  # the outside link hangs in the air
    issues = check_entry(_sampler_from(grid), _entry_contract())
    assert {(i.code, i.pos_local) for i in issues} == {
        ("SUPPORT_RULE_VIOLATION", (-1, 5, 4))
    }


def test_entry_landing_headroom_is_reported():
    grid = _entry_scene()
    grid[(-1, 7, 4)] = STONE  # blocks the headroom above the outside link
    issues = check_entry(_sampler_from(grid), _entry_contract())
    assert ("ENTRY_BLOCKED", (-1, 7, 4)) in {(i.code, i.pos_local) for i in issues}


# --------------------------------------------------------------------------- #
# cut / fill / replace accounting
# --------------------------------------------------------------------------- #
def test_cut_fill_report_counts_and_dedup():
    base = {
        (0, 0, 0): "minecraft:dirt",   # -> air     : cut
        (1, 0, 0): AIR,                # -> stone   : fill
        (2, 0, 0): "minecraft:dirt",   # -> moss    : replace only
        (3, 0, 0): "minecraft:dirt",   # unchanged
        (4, 0, 0): AIR,                # -> cave_air: both empty, not counted
        (5, 0, 0): STONE,              # -> air     : cut
        (7, 0, 0): STONE,              # -> unknown : replace (unverifiable)
    }
    candidate = {
        (0, 0, 0): AIR,
        (1, 0, 0): STONE,
        (2, 0, 0): "minecraft:moss_block",
        (3, 0, 0): "minecraft:dirt",
        (4, 0, 0): "minecraft:cave_air",
        (5, 0, 0): AIR,
        (6, 0, 0): STONE,              # unknown -> stone : replace (unverifiable)
    }
    positions = [
        (6, 0, 0),
        (6, 0, 0),        # duplicate stays one voxel
        (0, 0, 0),
        [0, 0, 0],        # list form of the same voxel
        (1, 0, 0),
        (1, 0, 0),
        (2, 0, 0),
        (3, 0, 0),
        (4, 0, 0),
        (5, 0, 0),
        (7, 0, 0),
    ]
    report = cut_fill_report(
        _sampler_from(base, None), _sampler_from(candidate, None), positions
    )
    assert report["cut"] == 2
    assert report["fill"] == 1
    assert report["replace"] == 3
    assert report["touched"] == 6
    assert report["cut_voxels"] == [(0, 0, 0), (5, 0, 0)]
    assert report["fill_voxels"] == [(1, 0, 0)]
    assert report["replace_voxels"] == [(2, 0, 0), (6, 0, 0), (7, 0, 0)]
    # a replacement is never also counted as a cut or a fill
    assert not set(report["cut_voxels"]) & set(report["replace_voxels"])
    assert not set(report["fill_voxels"]) & set(report["replace_voxels"])
    for key in ("cut_voxels", "fill_voxels", "replace_voxels"):
        assert report[key] == sorted(report[key])


def test_cut_fill_report_is_order_independent():
    base = {(0, 0, 0): "minecraft:dirt", (1, 0, 0): AIR}
    candidate = {(0, 0, 0): AIR, (1, 0, 0): STONE}
    forward = cut_fill_report(_sampler_from(base), _sampler_from(candidate), [(0, 0, 0), (1, 0, 0)])
    backward = cut_fill_report(_sampler_from(base), _sampler_from(candidate), [(1, 0, 0), (0, 0, 0)])
    assert forward == backward
    assert forward["cut"] == 1 and forward["fill"] == 1 and forward["replace"] == 0


# --------------------------------------------------------------------------- #
# determinism
# --------------------------------------------------------------------------- #
def test_issues_are_deterministically_sorted():
    grid = _flat_road_grid(range(-1, 4))
    grid[(1, 5, -1)] = AIR   # hole in the full-width road surface
    grid[(2, 6, 1)] = STONE  # obstruction beside the road
    del grid[(2, 4, 0)]      # road surface without support
    sampler = _sampler_from(grid)
    cells = [(0, 5, 0), (1, 5, 0), (2, 5, 0)]
    issues = check_road(sampler, cells, 3)
    assert {i.code for i in issues} == {
        "PATH_STEP_INVALID",
        "PATH_HEADROOM_BLOCKED",
        "SUPPORT_RULE_VIOLATION",
    }
    positions = [i.pos_local for i in issues]
    assert positions == sorted(positions)
    assert issues == sorted(issues, key=lambda i: (i.pos_local, i.code))
    # re-running the same input yields the identical list
    assert check_road(sampler, cells, 3) == issues
    # dedup: no identical issue twice
    assert len(set(issues)) == len(issues)
