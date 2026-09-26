"""Acceptance matrix A (permissions/masks) and B (net patch and provenance).

P0-B gates from the v0.2 engineering brief. Every failure case also checks that
the source file is untouched and that no candidate output is produced; an
illegal write is refused atomically rather than trimmed out of the patch.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from litegarden.compiler import CompileError, compile_plan, make_guard
from litegarden.constraints import (
    Box3,
    MaskSet,
    WriteRejected,
    build_policy,
    load_block_rules,
    parse_box,
)
from litegarden.io import load_scene
from litegarden.net_patch import ConflictPolicy, WorkingWorld
from litegarden.schema import parse_plan
from litegarden.terrain import analyze, build_planning_index

from .test_operations import _flat_scene, _write_assets

GRASS = "minecraft:grass_block"
STONE = "minecraft:stone"
MOSS = "minecraft:moss_block"
AIR = "minecraft:air"


def _sha(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def _scene(tmp_path, size=(16, 8, 16), y0=1):
    """Flat grass plateau with a dirt layer below, so support exists in-file."""
    src = _flat_scene(tmp_path, size=size, y0=y0)
    scene = load_scene(str(src))
    return src, scene


def _guard(scene, config=None, rules=None, hosts=()):
    assets = {}
    if rules is not None:
        assets = rules
    return make_guard(scene.snapshot, config or {}, _rules_dir(scene, assets), hosts)


def _rules_dir(scene, rules: dict) -> Path:
    """A temp assets dir carrying only the given block_rules (if any)."""
    import tempfile

    d = Path(tempfile.mkdtemp()) / "assets"
    d.mkdir(parents=True, exist_ok=True)
    if rules:
        (d / "block_rules.json").write_text(json.dumps(rules), encoding="utf-8")
    return d


def _world(scene, guard, policy=None):
    return WorkingWorld(scene.snapshot, guard, policy or ConflictPolicy(mode="permissive"))


def _permissive_guard(scene, config=None):
    """Guard with no block whitelist and no version gate (legacy permissive)."""
    sx, sy, sz = scene.snapshot.transform.local_size
    bounds = Box3((0, 0, 0), (sx, sy, sz))
    pol = build_policy(config, {}, scene.snapshot.data_version, bounds)
    from litegarden.constraints import WriteGuard

    return WriteGuard(bounds, pol)


def _path_plan(from_anchor: str, to: str, width: int = 1):
    return parse_plan(json.dumps({
        "schema_version": "0.1", "scene_id": "t", "seed": 1,
        "operations": [{
            "id": "path_1", "op": "connect_path", "from": from_anchor, "to": to,
            "width": width, "palette_id": "stone_path",
        }],
    }))


def _flat_with_anchors(tmp_path, size=(24, 8, 24)):
    src, scene = _scene(tmp_path, size=size)
    assets = _write_assets(tmp_path)
    add_block_rules(assets)
    a = analyze(scene.snapshot)
    build_planning_index(a)
    a.anchors = {"A": (2, 5), "B": (size[0] - 3, 5)}
    return src, scene, assets, a
_SYNTHETIC_RULES = {
    "version": "0.2",
    "editable_data_versions": [2975, 3953, 4671],
    "in_game_validated": False,
    "allowed_new_blocks": [
        "minecraft:stone_bricks", "minecraft:cobblestone", "minecraft:gravel",
        "minecraft:oak_planks", "minecraft:spruce_planks", "minecraft:oak_log",
        "minecraft:oak_leaves", "minecraft:lantern", "minecraft:oak_fence",
        "minecraft:stone_brick_slab",
    ],
}


def add_block_rules(assets) -> None:
    """Give a synthetic assets directory a verified block-rules file.

    The CLI refuses to run without one: a missing file would silently switch
    off both the block whitelist and the editable data version gate.
    """
    (Path(assets) / "block_rules.json").write_text(
        json.dumps(_SYNTHETIC_RULES), encoding="utf-8"
    )


def _flat_with_anchors(tmp_path, size=(24, 8, 24)):
    src, scene = _scene(tmp_path, size=size)
    assets = _write_assets(tmp_path)
    add_block_rules(assets)
    a = analyze(scene.snapshot)
    build_planning_index(a)
    a.anchors = {"A": (2, 5), "B": (size[0] - 3, 5)}
    return src, scene, assets, a


# ==========================================================================
# A. permissions and masks
# ==========================================================================


def test_a01_road_crossing_protected_zone_is_rejected(tmp_path):
    """A road that crosses a one-cell protected zone rejects the whole candidate."""
    src, scene, assets, a = _flat_with_anchors(tmp_path)
    before = _sha(src)
    config = {"protected_zones": [{"id": "keep", "min": [12, 0, 5], "max_exclusive": [13, 8, 6]}]}
    with pytest.raises(WriteRejected) as ei:
        compile_plan(scene.snapshot, _path_plan("A", "B"), a, assets, config=config)
    err = ei.value
    assert err.code == "WRITE_PROTECTED"
    assert err.pos_local == (12, 1, 5)  # the real write position, not a bbox
    assert err.rule_id and err.expected is not None and err.actual is not None
    assert _sha(src) == before
    assert not (tmp_path / "full.litematic").exists()


def test_a01_foundation_only_write_outside_zone_is_rejected(tmp_path):
    """An asset whose body fits but whose foundation write leaves the zone."""
    src, scene, assets, a = _flat_with_anchors(tmp_path)
    before = _sha(src)
    plan = parse_plan(json.dumps({
        "schema_version": "0.1", "scene_id": "t", "seed": 1,
        "operations": [{
            "id": "pav", "op": "place_asset", "asset_id": "pavilion_small",
            "site_id": "site_00", "variant": "north",
        }],
    }))
    # authorise only the pavilion footprint's upper rows: the base layer writes
    # below it, so the task selection is left.
    with pytest.raises(WriteRejected) as ei:
        compile_plan(
            scene.snapshot, plan, a, assets,
            config={"task_authorized": {"min": [9, 4, 9], "max_exclusive": [14, 8, 14]}},
        )
    assert ei.value.code == "WRITE_OUTSIDE_SELECTION"
    assert _sha(src) == before


def test_a02_clearance_above_the_authorised_ymax_is_rejected(tmp_path):
    """The task's Y range cannot be ignored: a clearance write above it fails."""
    src, scene = _scene(tmp_path, size=(16, 8, 16))
    assets = _write_assets(tmp_path)
    # a non-colliding cover voxel inside the road's walk volume, two above ground
    scene.snapshot._region[scene.snapshot.transform.local_to_region((8, 3, 5))] = \
        __import__("litemapy").BlockState("minecraft:leaf_litter")
    a = analyze(scene.snapshot)
    build_planning_index(a)
    a.anchors = {"A": (0, 5), "B": (15, 5)}
    before = _sha(src)
    cfg = {"task_authorized": {"min": [0, 0, 0], "max_exclusive": [16, 3, 16]}}
    with pytest.raises(WriteRejected) as ei:
        compile_plan(scene.snapshot, _path_plan("A", "B"), a, assets, config=cfg)
    err = ei.value
    assert err.code == "WRITE_OUTSIDE_SELECTION"
    assert err.rule_id == "selection/task_authorized"
    assert err.pos_local[1] == 3, err.pos_local  # exactly the voxel above ymax
    assert _sha(src) == before


def test_a03_write_then_restore_in_protected_zone_still_rejected(tmp_path):
    """A zero net change never licenses an unauthorised write."""
    _, scene = _scene(tmp_path)
    guard = _permissive_guard(
        scene, {"protected_zones": [{"id": "p", "min": [5, 1, 5], "max_exclusive": [6, 2, 6]}]}
    )
    world = _world(scene, guard)
    with pytest.raises(WriteRejected) as ei:
        world.write((5, 1, 5), STONE, "op1", expected_stage_before=GRASS)
    assert ei.value.code == "WRITE_PROTECTED"
    assert ei.value.rule_id == "protected_zone"
    assert world.events == []

    # the same pair of writes outside the zone really does cancel out, which is
    # what makes the protected case meaningful
    world2 = _world(scene, _permissive_guard(scene))
    world2.write((7, 1, 7), STONE, "op1", expected_stage_before=GRASS)
    world2.write((7, 1, 7), GRASS, "op2", expected_stage_before=STONE)
    assert world2.finalize().net_changes == []


def test_a04_known_air_column_is_known_but_has_no_ground(tmp_path):
    """Known air is known; an unknown block id is readable but not editable."""
    src, scene = _scene(tmp_path)
    region = scene.snapshot._region
    to_region = scene.snapshot.transform.local_to_region
    for y in range(scene.snapshot.transform.local_size[1]):
        region[to_region((3, y, 3))] = __import__("litemapy").BlockState(AIR)
    region[to_region((4, 1, 4))] = __import__("litemapy").BlockState("example:unknown_block")
    a = analyze(scene.snapshot)

    # (1) a column of all air has no usable ground, yet every voxel is known
    assert a.ground_height[3, 3] == -1
    assert a.build_height[3, 3] == -1
    guard = _permissive_guard(scene)
    assert guard.known_voxel((3, 1, 3)) is True
    assert guard.in_input_bounds((3, 1, 3)) is True

    # (2) an unknown block id reads fine but is not treated as safely editable
    assert scene.snapshot.block_at_local((4, 1, 4)) == "example:unknown_block"
    strict = make_guard(
        scene.snapshot, {},
        _rules_dir(scene, {"allowed_new_blocks": [STONE]}),
    )
    with pytest.raises(WriteRejected) as ei:
        strict.check_write((4, 1, 4), "example:unknown_block", STONE, op_id="op1")
    assert ei.value.code == "UNKNOWN_BLOCK_RULE"
    assert ei.value.rule_id == "rules/removable_blocks"


def test_a05_plan_cannot_declare_its_own_authorisation():
    """A plan may not smuggle in constraint fields."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        parse_plan(json.dumps({
            "schema_version": "0.1", "scene_id": "t",
            "operations": [],
            "authorized_bounds": {"min": [0, 0, 0], "max_exclusive": [99, 99, 99]},
        }))
    with pytest.raises(ValidationError):
        parse_plan(json.dumps({
            "schema_version": "0.1", "scene_id": "t", "operations": [],
            "task_authorized": {"min": [0, 0, 0], "max_exclusive": [1, 1, 1]},
        }))


def test_a05_cli_bypass_is_rejected_like_the_ui(tmp_path):
    """The CLI goes through the same gate; it cannot bypass a protected zone."""
    from litegarden.__main__ import main

    src, scene, _, _ = _flat_with_anchors(tmp_path)
    src = Path(src)
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({
        "anchors": {"A": [0, 5], "B": [23, 5]},
        "protected_zones": [{"id": "keep", "min": [12, 0, 5], "max_exclusive": [13, 8, 6]}],
    }), encoding="utf-8")
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({
        "schema_version": "0.1", "scene_id": "t", "seed": 1,
        "operations": [{
            "id": "path_1", "op": "connect_path", "from": "A", "to": "B",
            "width": 1, "palette_id": "stone_path",
        }],
    }), encoding="utf-8")
    out = tmp_path / "cliout"
    before = _sha(src)
    rc = main([
        "compile", str(src), "--plan", str(plan), "--assets", str(tmp_path / "assets"),
        "--config", str(cfg), "--out", str(out),
    ])
    assert rc == 1
    assert _sha(src) == before
    assert not (out / "full.litematic").exists()
    err = json.loads((out / "error.json").read_text(encoding="utf-8"))
    assert err["rejected"]["code"] == "WRITE_PROTECTED"
    assert err["rejected"]["pos_local"] == [12, 1, 5]


def test_a06_protection_wins_over_an_overlapping_editable_zone(tmp_path):
    _, scene = _scene(tmp_path)
    cfg = {
        "editable_zone": {"min": [0, 0, 0], "max_exclusive": [16, 8, 16]},
        "protected_zones": [{"id": "p", "min": [5, 1, 5], "max_exclusive": [7, 3, 7]}],
    }
    guard = _permissive_guard(scene, cfg)
    assert guard.global_editable((5, 1, 5)) is True
    assert guard.protected((5, 1, 5)) is True
    with pytest.raises(WriteRejected) as ei:
        guard.check_write((5, 1, 5), GRASS, STONE, op_id="op1")
    assert ei.value.code == "WRITE_PROTECTED"


def test_a06_changing_the_protection_config_invalidates_a_frozen_candidate(tmp_path):
    _, scene = _scene(tmp_path)
    sx, sy, sz = scene.snapshot.transform.local_size
    bounds = Box3((0, 0, 0), (sx, sy, sz))
    base = build_policy({}, {}, scene.snapshot.data_version, bounds)
    frozen = build_policy(
        {"protected_zones": [{"id": "a", "min": [1, 0, 1], "max_exclusive": [2, 8, 2]}]},
        {}, scene.snapshot.data_version, bounds,
    )
    changed = build_policy(
        {"protected_zones": [{"id": "a", "min": [1, 0, 1], "max_exclusive": [3, 8, 3]}]},
        {}, scene.snapshot.data_version, bounds,
    )
    assert len({base.policy_hash, frozen.policy_hash, changed.policy_hash}) == 3
    # a candidate built against `frozen` cannot be accepted under `changed`
    assert frozen.policy_hash != changed.policy_hash


# ==========================================================================
# B. net patch and provenance
# ==========================================================================


def test_b01_two_ops_merge_into_one_net_change(tmp_path):
    _, scene = _scene(tmp_path)
    world = _world(scene, _permissive_guard(scene))
    pos = (7, 1, 7)
    world.write(pos, STONE, "op1", expected_stage_before=GRASS)
    world.write(pos, MOSS, "op2", expected_stage_before=STONE)
    res = world.finalize()
    assert len(res.net_changes) == 1
    nc = res.net_changes[0]
    assert (nc.before, nc.after) == (GRASS, MOSS)
    assert nc.contributors == ("op1", "op2")
    assert nc.op_id == "op2"
    assert len(res.events) == 2
    assert res.stats["write_events"] == 2
    assert res.stats["net_changes"] == 1


def test_b02_cancelled_writes_leave_no_net_change_but_are_logged(tmp_path):
    _, scene = _scene(tmp_path)
    world = _world(scene, _permissive_guard(scene))
    ground = (7, 1, 7)
    sky = (7, 3, 7)

    world.write(ground, STONE, "op1", expected_stage_before=GRASS)
    world.write(ground, GRASS, "op2", expected_stage_before=STONE)
    world.write(sky, STONE, "op1", expected_stage_before=AIR)
    world.write(sky, AIR, "op2", expected_stage_before=STONE)

    res = world.finalize()
    assert res.net_changes == []
    assert len(res.patch) == 0
    assert len(res.events) == 4  # the internal write log keeps every write
    assert res.stats["write_events"] == 4
    assert res.stats["touched"] == 2
    # ... and the permission gate really ran for each of them
    assert world.guard.stats.checked >= 4


def test_b03_wrong_stage_before_is_rejected_immediately(tmp_path):
    _, scene = _scene(tmp_path)
    world = _world(scene, _permissive_guard(scene))
    pos = (7, 1, 7)
    world.write(pos, STONE, "op1", expected_stage_before=GRASS)
    with pytest.raises(WriteRejected) as ei:
        # op2 believes the cell is still grass; the final overwrite must not hide it
        world.write(pos, MOSS, "op2", expected_stage_before=GRASS)
    err = ei.value
    assert err.code == "BEFORE_MISMATCH"
    assert err.rule_id == "net_patch/stage_before"
    assert err.expected == GRASS and err.actual == STONE
    assert [e.op_id for e in world.events] == ["op1"]


def test_b04_property_only_difference_is_a_real_change(tmp_path):
    _, scene = _scene(tmp_path)
    world = _world(scene, _permissive_guard(scene))
    pos = (7, 2, 7)
    north = "minecraft:oak_stairs[facing=north,half=bottom,shape=straight]"
    east = "minecraft:oak_stairs[facing=east,half=bottom,shape=straight]"
    world.write(pos, north, "op1", expected_stage_before=AIR)
    world.write(pos, east, "op2", expected_stage_before=north)
    res = world.finalize()
    assert len(res.net_changes) == 1
    assert res.net_changes[0].before == AIR
    assert res.net_changes[0].after == east
    # same block id, different properties => a real state change, not a no-op
    assert len(res.events) == 2

    wet = "minecraft:oak_slab[type=bottom,waterlogged=true]"
    dry = "minecraft:oak_slab[type=bottom,waterlogged=false]"
    world.write(pos, dry, "op3", expected_stage_before=east)
    world.write(pos, wet, "op4", expected_stage_before=dry)
    assert len(world.finalize().net_changes) == 1


def test_b05_second_revision_before_comes_from_br_not_b0(tmp_path):
    """A second local revision merges against its own Br, not the import."""
    src, _b0 = _scene(tmp_path)
    # revision 1 changed this cell, so Br (the baseline of revision 2) differs
    # from B0 there; the import itself is always read from disk.
    pos = (7, 1, 7)
    br = load_scene(str(src))
    br.snapshot._region[br.snapshot.transform.local_to_region(pos)] = \
        __import__("litemapy").BlockState(STONE)

    world = _world(br, _permissive_guard(br))
    world.write(pos, MOSS, "op1", expected_stage_before=STONE)
    res = world.finalize()
    assert res.net_changes[0].before == STONE  # Br, not the imported GRASS
    assert res.base_scene_hash() is not None
    assert res.final_scene_hash() is not None
    assert res.base_scene_hash() != res.final_scene_hash()

    # a hash of B0 is different, which is exactly why the base is recorded
    b0 = load_scene(str(src))
    world0 = _world(b0, _permissive_guard(b0))
    assert world0.baseline_scene_hash() != res.base_scene_hash()


def test_b06_repeat_run_is_deterministic(tmp_path):
    """Same input/config/assets/plan/seed twice => identical normalised result."""
    src = _flat_scene(tmp_path, size=(24, 8, 24))
    assets = _write_assets(tmp_path)
    from .test_operations import _plan_obj

    scene1 = load_scene(str(src))
    a1 = analyze(scene1.snapshot)
    build_planning_index(a1)
    r1 = compile_plan(scene1.snapshot, _plan_obj(), a1, assets)

    scene2 = load_scene(str(src))
    a2 = analyze(scene2.snapshot)
    build_planning_index(a2)
    r2 = compile_plan(scene2.snapshot, _plan_obj(), a2, assets)

    def norm(result):
        return sorted((c.pos_local, c.before, c.after) for c in result.patch)

    assert norm(r1) == norm(r2)
    assert len(r1.patch) > 0
    # semantic hashes match; compressed bytes and timestamps need not
    assert r1.net.final_scene_hash() == r2.net.final_scene_hash()
    assert r1.net.base_scene_hash() == r2.net.base_scene_hash()
    assert r1.guard.policy.policy_hash == r2.guard.policy.policy_hash
    assert r1.stats == r2.stats


def test_parse_box_is_half_open_and_strict_about_types():
    box = parse_box({"min": [10, 5, 20], "max_exclusive": [20, 15, 30]}, "z")
    assert box.size == (10, 10, 10)
    assert box.contains((19, 14, 29))
    assert not box.contains((20, 14, 29))
    with pytest.raises(ValueError):
        parse_box({"min": [10, 5, 20], "max_exclusive": [20, True, 30]}, "z")
    with pytest.raises(ValueError):
        parse_box({"min": [10, 5, 20], "max_exclusive": [20, 15.5, 30]}, "z")
    with pytest.raises(ValueError):
        parse_box({"min": [10, 5], "max_exclusive": [20, 15, 30]}, "z")
    with pytest.raises(ValueError):
        parse_box({"min": [20, 5, 20], "max_exclusive": [10, 15, 30]}, "z")


def test_maskset_is_sparse_but_equivalent_to_an_explicit_array():
    masks = MaskSet([Box3((1, 1, 1), (3, 3, 3)), Box3((5, 0, 0), (6, 8, 8))])
    for x in range(8):
        for y in range(8):
            for z in range(8):
                expected = (1 <= x < 3 and 1 <= y < 3 and 1 <= z < 3) or (
                    5 <= x < 6 and 0 <= y < 8 and 0 <= z < 8
                )
                assert masks.contains((x, y, z)) is expected


def test_version_gate_blocks_an_unlisted_data_version(tmp_path):
    """C06: a readable scene is not automatically editable."""
    _, scene = _scene(tmp_path)
    sx, sy, sz = scene.snapshot.transform.local_size
    bounds = Box3((0, 0, 0), (sx, sy, sz))
    pol = build_policy(
        {"editable_data_versions": [scene.snapshot.data_version + 1]},
        {}, scene.snapshot.data_version, bounds,
    )
    from litegarden.constraints import WriteGuard

    guard = WriteGuard(bounds, pol)
    with pytest.raises(WriteRejected) as ei:
        guard.check_write((1, 1, 1), GRASS, STONE, op_id="op1")
    assert ei.value.code == "EDIT_VERSION_UNSUPPORTED"
    assert pol.describe()["version_gate"] == "active"


def test_block_rules_declare_the_editable_versions():
    rules = load_block_rules(Path("assets") / "block_rules.json")
    assert rules.get("editable_data_versions"), rules
    assert rules.get("in_game_validated") is False
    assert "minecraft:stone_brick_slab" in rules["allowed_new_blocks"]
