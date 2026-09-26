"""Acceptance matrix C (NBT and input gates) and D (traversal, entries, cut/fill).

The unit-level cases for the walk profile live in ``tests/test_traversal.py``;
this file covers the gates that only exist at the pipeline level: the raw-NBT
input gate, type-sensitive preservation of a re-read export, block-entity host
dependencies, negative-size preservation, and the final walkability re-check
that runs after every decoration.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import nbtlib
import numpy as np
import pytest
from litemapy import BlockState, Region, Schematic

from litegarden.blocks import COVER_BLOCKS
from litegarden.compiler import CompileError, compile_plan, run_final_checks
from litegarden.constraints import WriteRejected
from litegarden.io import (
    BlockEntityHostChanged,
    MultiRegionError,
    UnsupportedFormatError,
    apply_patchset,
    check_block_entity_hosts,
    compare_nbt_preservation,
    entity_host_positions,
    load_scene,
    save_scene,
    tile_entity_records,
)
from litegarden.nbt_compare import NbtPreservationError, ensure_nbt_preserved, allowed_save_paths
from litegarden.operations.path import PathError, find_path, solve_road
from litegarden.schema import parse_plan
from litegarden.terrain import analyze, build_planning_index

from .test_operations import _flat_scene, _write_assets

from .test_permissions import (
    _permissive_guard,
    _scene,
    _world,
    add_block_rules,
)

GRASS = "minecraft:grass_block"
STONE = "minecraft:stone"


def _sha(p) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def _scene_with_tile_entity(tmp_path, size=(8, 6, 8)):
    """Flat scene plus one tile entity, whose host block must stay intact."""
    src = _flat_scene(tmp_path, size=size, y0=1)
    raw = nbtlib.load(str(src))
    rid = list(raw["Regions"].keys())[0]
    raw["Regions"][rid]["TileEntities"] = nbtlib.List[nbtlib.Compound]([
        nbtlib.Compound({
            "id": nbtlib.String("minecraft:chest"),
            "x": nbtlib.Int(3), "y": nbtlib.Int(1), "z": nbtlib.Int(3),
        })
    ])
    f = nbtlib.File(raw)
    f.gzipped = True
    f.save(str(src))
    return src


# ==========================================================================
# C. NBT and input gates
# ==========================================================================

def test_c01_short_to_int_is_caught_on_a_reexport(tmp_path):
    """C01: same value, different tag type, detected on the re-read export."""
    src, scene = _scene(tmp_path)
    out = tmp_path / "full.litematic"
    save_scene(scene, str(out))
    reloaded = load_scene(str(out))
    # simulate a writer that silently narrows an Int metadata field to Short
    before = int(reloaded.raw_nbt["Metadata"]["RegionCount"])
    reloaded.raw_nbt["Metadata"]["RegionCount"] = nbtlib.Short(before)
    diffs = compare_nbt_preservation(scene, reloaded)
    assert any(d.code == "NBT_TYPE_CHANGED" for d in diffs), diffs
    mismatch = [d for d in diffs if d.code == "NBT_TYPE_CHANGED"][0]
    assert mismatch.tag_path == "Metadata.RegionCount", mismatch
    assert "Int" in (mismatch.original_type or "") + (mismatch.current_type or "")
    with pytest.raises(NbtPreservationError):
        ensure_nbt_preserved(
            scene.raw_nbt, reloaded.raw_nbt, allowed_save_paths(scene.snapshot.region_id)
        )


def test_c02_non_whitelisted_field_changes_are_caught(tmp_path):
    """C02: deleted extension field, changed array, extra field."""
    src, scene = _scene(tmp_path)
    out = tmp_path / "full.litematic"
    save_scene(scene, str(out))
    reloaded = load_scene(str(out))
    rid = scene.snapshot.region_id

    # extra (unauthorised) field
    reloaded.raw_nbt["Regions"][rid]["AuthorNotes"] = nbtlib.String("hello")
    diffs = compare_nbt_preservation(scene, reloaded)
    assert any(d.code == "NBT_UNAUTHORIZED_FIELD_CHANGE" for d in diffs), diffs

    # deleted non-whitelisted field
    reloaded2 = load_scene(str(out))
    del reloaded2.raw_nbt["Metadata"]["Software"]
    diffs2 = compare_nbt_preservation(scene, reloaded2)
    assert any(d.code == "NBT_FIELD_MISSING" for d in diffs2), diffs2

    # changed array content on a non-whitelisted array
    reloaded3 = load_scene(str(out))
    from nbtlib.tag import IntArray

    reloaded3.raw_nbt["Metadata"]["PreviewImageData"] = IntArray([1, 2, 3])
    diffs3 = compare_nbt_preservation(scene, reloaded3)
    assert any(d.code == "NBT_VALUE_CHANGED" for d in diffs3), diffs3

    # changing the authorised BlockStates array is allowed - that is exactly
    # what a real save does, and it must not be reported as a violation
    reloaded4 = load_scene(str(out))
    reloaded4.raw_nbt["Regions"][rid]["BlockStates"] = \
        type(reloaded4.raw_nbt["Regions"][rid]["BlockStates"])([1, 2, 3])
    assert compare_nbt_preservation(scene, reloaded4) == []


def test_c03_tile_entity_host_change_is_caught(tmp_path):
    """C03: retained block-entity data with a replaced host block fails."""
    src = _scene_with_tile_entity(tmp_path)
    scene = load_scene(str(src))
    recs = tile_entity_records(scene)
    assert len(recs) == 1 and recs[0]["id"] == "minecraft:chest"
    host = tuple(recs[0]["pos_local"])
    assert entity_host_positions(scene) == [host]
    before = scene.snapshot.block_at_local(host)
    # (a) the write gate refuses to change the host voxel at all
    from litegarden.compiler import make_guard

    guarded = make_guard(scene.snapshot, {}, Path(tmp_path / "no_rules"), [host])
    world = _world(scene, guarded)
    with pytest.raises(WriteRejected) as ei:
        world.write(host, STONE, "op1", expected_stage_before=before)
    assert ei.value.code == "ENTITY_DEPENDENCY_UNSAFE"
    assert ei.value.rule_id == "entity_dependency"

    # (b) and if a host change ever reached the file, the re-read check fails
    out = tmp_path / "host_changed.litematic"
    # (b) and if a host change ever reached the file, the re-read check fails
    out = tmp_path / "host_changed.litematic"
    pristine = load_scene(str(src))
    changed = load_scene(str(src))
    changed.snapshot._region[changed.snapshot.transform.local_to_region(host)] = BlockState(STONE)
    save_scene(changed, str(out))
    reloaded = load_scene(str(out))
    with pytest.raises(BlockEntityHostChanged) as ei2:
        check_block_entity_hosts(pristine, reloaded)
    assert ei2.value.code == "BLOCK_ENTITY_HOST_CHANGED"
    assert ei2.value.pos_local == host


def test_c04_negative_size_and_properties_survive_a_round_trip(tmp_path):
    """C04: negative Size, non-zero Position and block properties are preserved."""
    region = Region(100, 20, -5, -8, 6, 7)
    for pos in region.block_positions():
        region[pos] = BlockState(GRASS) if pos[1] == 0 else BlockState("minecraft:air")
    region[(-3, 0, 2)] = BlockState("minecraft:oak_stairs", facing="north", half="bottom")
    region[(-1, 0, 2)] = BlockState("minecraft:oak_slab", type="top")
    schem = Schematic(name="neg")
    schem.regions["main"] = region
    src = tmp_path / "neg.litematic"
    schem.save(str(src))

    scene = load_scene(str(src))
    info = scene.snapshot.transform.region
    assert info.position == (100, 20, -5)
    assert info.size == (-8, 6, 7)
    assert info.min_schem == (93, 20, -5)
    assert info.max_schem == (100, 25, 1)
    assert scene.snapshot.transform.enclosing_min == (93, 20, -5)

    palette = scene.raw_nbt["Regions"]["main"]["BlockStatePalette"]
    names = {str(e["Name"]): dict(e.get("Properties", {}) or {}) for e in palette}
    assert names["minecraft:oak_stairs"] == {"facing": "north", "half": "bottom"}
    assert names["minecraft:oak_slab"] == {"type": "top"}

    out = tmp_path / "neg_out.litematic"
    save_scene(scene, str(out))
    reloaded = load_scene(str(out))
    assert reloaded.snapshot.transform.region.position == (100, 20, -5)
    assert reloaded.snapshot.transform.region.size == (-8, 6, 7)
    rpalette = reloaded.raw_nbt["Regions"]["main"]["BlockStatePalette"]
    rnames = {str(e["Name"]): dict(e.get("Properties", {}) or {}) for e in rpalette}
    assert rnames["minecraft:oak_stairs"] == {"facing": "north", "half": "bottom"}
    # a zero-change round trip preserves every non-whitelisted field
    assert compare_nbt_preservation(scene, reloaded) == []


def test_c05_multi_region_and_broken_regions_are_rejected_before_decoding(tmp_path):
    """C05: more than one region, a wrong Regions type, or none at all."""
    a = Region(0, 0, 0, 3, 3, 3)
    b = Region(10, 0, 0, 3, 3, 3)
    schem = Schematic(name="two")
    schem.regions["one"] = a
    schem.regions["two"] = b
    two = tmp_path / "two.litematic"
    schem.save(str(two))
    with pytest.raises(MultiRegionError):
        load_scene(str(two))

    one = _flat_scene(tmp_path, size=(4, 4, 4), y0=1)
    raw = nbtlib.load(str(one))
    rid = list(raw["Regions"].keys())[0]
    raw["Regions"] = nbtlib.Int(7)  # wrong type: a scanner must stop, not guess
    wrong = tmp_path / "wrong.litematic"
    f = nbtlib.File(raw)
    f.gzipped = True
    f.save(str(wrong))
    with pytest.raises(UnsupportedFormatError):
        load_scene(str(wrong))

    raw2 = nbtlib.load(str(one))
    del raw2["Regions"]
    missing = tmp_path / "missing.litematic"
    f2 = nbtlib.File(raw2)
    f2.gzipped = True
    f2.save(str(missing))
    with pytest.raises(UnsupportedFormatError):
        load_scene(str(missing))


def test_c06_cli_refuses_an_unlisted_data_version(tmp_path):
    """C06: a scene that reads and renders fine is still not editable."""
    from litegarden.__main__ import main

    src, _scene_obj, assets, _a = _flat_with(tmp_path)
    src = Path(src)
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({
        "anchors": {"A": [0, 5], "B": [23, 5]},
        "editable_data_versions": [1],  # deliberately not the file's version
    }), encoding="utf-8")
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({
        "schema_version": "0.1", "scene_id": "t", "seed": 1,
        "operations": [{"id": "p", "op": "connect_path", "from": "A", "to": "B",
                        "width": 1, "palette_id": "stone_path"}],
    }), encoding="utf-8")
    out = tmp_path / "out"
    before = _sha(src)
    rc = main(["compile", str(src), "--plan", str(plan), "--assets", str(assets),
               "--config", str(cfg), "--out", str(out)])
    assert rc == 1
    assert _sha(src) == before
    err = json.loads((out / "error.json").read_text(encoding="utf-8"))
    assert err["rejected"]["code"] == "EDIT_VERSION_UNSUPPORTED"


def _flat_with(tmp_path, size=(24, 8, 24)):
    src, scene = _scene(tmp_path, size=size)
    assets = _write_assets(tmp_path)
    add_block_rules(assets)
    a = analyze(scene.snapshot)
    build_planning_index(a)
    a.anchors = {"A": (2, 5), "B": (size[0] - 3, 5)}
    return src, scene, assets, a


# ==========================================================================
# D. traversal, entries and cut/fill at the pipeline level
# ==========================================================================


def test_d01_a_blocked_side_cell_is_reported_with_its_exact_voxel(tmp_path):
    """D01: the full road width matters, and the report names the voxel."""
    src, scene = _scene(tmp_path, size=(16, 8, 16))
    assets = _write_assets(tmp_path)
    region = scene.snapshot._region
    to_region = scene.snapshot.transform.local_to_region
    # a solid block one cell to the side and one above the road surface
    region[to_region((8, 2, 6))] = BlockState("minecraft:oak_planks")
    a = analyze(scene.snapshot)
    build_planning_index(a)
    a.anchors = {"A": (2, 5), "B": (13, 5)}
    result = compile_plan(scene.snapshot, _path("A", "B", width=3), a, assets)
    # the blocked side cell is skipped, reported by coordinate, and never carved
    skipped = result.roads[0]["skipped"]
    assert any(s["pos"] == [8, 6] for s in skipped), skipped
    entry = next(s for s in skipped if s["pos"] == [8, 6])
    assert "no buildable ground" in entry["reason"], entry
    assert len(result.patch) > 0
    # and nothing was written into the blocked column
    assert not any(c.pos_local[:1] == (8,) and c.pos_local[1] == 2 for c in result.patch)


def test_d02_a_one_block_step_without_the_transition_block_is_refused(tmp_path):
    """D02: a 1-block step needs a legal transition; a flat palette cannot do it."""
    src, scene = _scene(tmp_path, size=(16, 8, 16))
    assets = _write_assets(tmp_path)  # legacy flat palette: no 'transition' entry
    a = analyze(scene.snapshot)
    build_planning_index(a)
    a.anchors = {"A": (0, 5), "B": (15, 5)}
    a.build_height = a.build_height.copy()
    a.build_height[8:, :] += 1  # a 1-block step across the road
    with pytest.raises(CompileError, match="transition"):
        compile_plan(scene.snapshot, _path("A", "B", width=1), a, assets)


def test_d03_a_decoration_blocking_the_doorway_fails_the_final_check(tmp_path):
    """D03: interior empty but the door blocked => ENTRY_BLOCKED, not accepted."""
    src, scene, assets, a = _flat_with(tmp_path, size=(24, 8, 24))
    before = _sha(src)
    plan = parse_plan(json.dumps({
        "schema_version": "0.1", "scene_id": "t", "seed": 1,
        "operations": [
            {"id": "pav", "op": "place_asset", "asset_id": "pavilion_small",
             "site_id": "site_00", "variant": "north"},
            {"id": "path_1", "op": "connect_path", "from": "A", "to": "pav.entry",
             "width": 1, "palette_id": "stone_path"},
            {"id": "lights", "op": "decorate_path", "path_id": "path_1",
             "asset_id": "lamp_small", "spacing": 4},
        ],
    }))
    with pytest.raises(CompileError) as ei:
        compile_plan(scene.snapshot, plan, a, assets)
    codes = {i["code"] for i in ei.value.issues}
    assert "ENTRY_BLOCKED" in codes, ei.value.issues
    entry_issue = next(i for i in ei.value.issues if i["code"] == "ENTRY_BLOCKED")
    assert entry_issue["op_id"] == "pav" and entry_issue["pos_local"]
    assert _sha(src) == before


def test_d04_a_late_decoration_on_the_road_fails_only_on_the_final_candidate(tmp_path):
    """D04: paving passes, decoration blocks it, the final re-check catches it."""
    src, scene, assets, a = _flat_with(tmp_path, size=(24, 8, 24))
    plan = parse_plan(json.dumps({
        "schema_version": "0.1", "scene_id": "t", "seed": 1,
        "operations": [{"id": "path_1", "op": "connect_path", "from": "A", "to": "B",
                        "width": 3, "palette_id": "stone_path"}],
    }))
    result = compile_plan(scene.snapshot, plan, a, assets, check_walkability=False)
    world = result.net.world

    # the freshly paved road is clean
    clean_errors, _diag = run_final_checks(world, result.road_plans, [], min_headroom=2)
    assert clean_errors == [], clean_errors

    # now a decoration lands on a paved road cell
    paved = sorted(c.pos_local for c in result.patch if c.after.startswith("minecraft:"))
    road_voxel = paved[len(paved) // 2]
    above = (road_voxel[0], road_voxel[1] + 1, road_voxel[2])
    world.write(above, "minecraft:oak_fence", "decor",
                expected_stage_before="minecraft:air", action="decorate")

    # the same check, run again on the finished candidate, now fails
    errors, _diag = run_final_checks(world, result.road_plans, [], min_headroom=2)
    assert any(i["code"] == "PATH_HEADROOM_BLOCKED" for i in errors), errors
    headroom = [i for i in errors if i["code"] == "PATH_HEADROOM_BLOCKED"][0]
    assert headroom["pos_local"] == list(above)
    assert headroom["actual"].find("oak_fence") >= 0

def test_d06_search_estimate_and_real_cut_fill_are_reported_separately(tmp_path):
    """D06: the A* estimate is not the hard budget, and both are reported."""
    src, scene, assets, a = _flat_with(tmp_path)
    plan = _path("A", "B", width=3)
    result = compile_plan(scene.snapshot, plan, a, assets)
    assert "estimated_fill" in result.stats and "estimated_cut" in result.stats
    assert "fill" in result.stats and "cut" in result.stats
    assert result.roads and "estimated" in result.roads[0]
    # the real numbers are de-duplicated net voxel counts
    assert result.stats["fill"] + result.stats["cut"] + result.stats["replace"] == \
        result.stats["net_changes"]


def test_d06_a_hard_cut_fill_budget_rejects_a_cheap_route(tmp_path):
    """D06: a low cost estimate never overrides the hard budget."""
    src, scene, assets, a = _flat_with(tmp_path)
    plan = _path("A", "B", width=3)
    with pytest.raises(CompileError) as ei:
        compile_plan(scene.snapshot, plan, a, assets, budgets={"max_replace": 0})
    assert ei.value.code == "BUDGET_EXCEEDED"
    with pytest.raises(CompileError) as ei2:
        compile_plan(scene.snapshot, plan, a, assets, budgets={"max_blocks": 5})
    assert ei2.value.code == "BUDGET_EXCEEDED"


def test_d06_fill_cost_changes_the_chosen_route():
    """A non-negative fill weight really shapes the route, not just the report."""
    size = 9
    ground = np.zeros((size, size), dtype=np.int32)
    ground[4, 2:7] = 1  # a one-block ridge across the middle of the map
    water = np.zeros((size, size), dtype=bool)
    obstacle = np.zeros((size, size), dtype=bool)

    direct = find_path(ground, water, obstacle, (0, 4), (8, 4), fill_cost=0.0)
    avoid = find_path(ground, water, obstacle, (0, 4), (8, 4), fill_cost=50.0)

    def crosses_ridge(path):
        return any(p == (4, z) for p in path for z in range(2, 7))

    # with no fill weight the route crosses the ridge; with a real weight it
    # goes around it even though that path is longer
    assert crosses_ridge(direct), direct
    assert not crosses_ridge(avoid), avoid
    assert len(avoid) > len(direct)
    assert (0, 4) in direct and (0, 4) in avoid


def _path(from_anchor: str, to: str, width: int = 1):
    return parse_plan(json.dumps({
        "schema_version": "0.1", "scene_id": "t", "seed": 1,
        "operations": [{
            "id": "path_1", "op": "connect_path", "from": from_anchor, "to": to,
            "width": width, "palette_id": "stone_path",
        }],
    }))
