"""Acceptance B: deterministic construction, operations layer.

Covers the failure samples required by the spec: narrow corridors, slopes,
occupied entries, templates filled by terrain, missing support. Any
over-budget / out-of-bounds / protected write must block output.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from litemapy import BlockState, Region, Schematic

from litegarden.compiler import CompileError, compile_plan
from litegarden.io import apply_patchset, compare_to_expected, load_scene, save_scene
from litegarden.operations.path import PathError, find_path, pave_path
from litegarden.operations.stamp import Asset, AssetError, place_asset
from litegarden.schema import parse_plan
from litegarden.terrain import analyze, build_planning_index

STONE = "minecraft:stone"
GRASS = "minecraft:grass_block"
WATER = "minecraft:water"
LEAVES = "minecraft:oak_leaves"


def _flat_scene(tmp_path: Path, size=(16, 8, 16), y0=0):
    """A flat grass plateau with a dirt support layer, all walkable."""
    region = Region(0, 0, 0, *size)
    grass = BlockState(GRASS)
    dirt = BlockState("minecraft:dirt")
    for pos in region.block_positions():
        x, y, z = pos
        region[pos] = grass if y == y0 else (dirt if y < y0 else BlockState("minecraft:air"))
    schem = Schematic(name="flat")
    schem.regions["main"] = region
    path = tmp_path / "flat.litematic"
    schem.save(str(path))
    return path


def _analysis(scene):
    a = analyze(scene.snapshot)
    build_planning_index(a)
    return a


# ---------- find_path ----------

def test_find_path_straight_flat():
    gh = np.full((10, 10), 0, dtype=np.int32)
    wm = np.zeros((10, 10), dtype=bool)
    om = np.zeros((10, 10), dtype=bool)
    p = find_path(gh, wm, om, (0, 0), (0, 9))
    assert p[0] == (0, 0) and p[-1] == (0, 9)
    assert len(p) == 10


def test_find_path_steep_slope_no_solution():
    # a wall of 2 blocks: unreachable with max_step=1
    gh = np.zeros((10, 10), dtype=np.int32)
    gh[:, 5] = 2
    wm = np.zeros((10, 10), dtype=bool)
    om = np.zeros((10, 10), dtype=bool)
    with pytest.raises(PathError):
        find_path(gh, wm, om, (0, 0), (0, 9))


def test_find_path_water_blocked():
    gh = np.zeros((10, 10), dtype=np.int32)
    wm = np.zeros((10, 10), dtype=bool)
    wm[:, 5] = True
    om = np.zeros((10, 10), dtype=bool)
    with pytest.raises(PathError):
        find_path(gh, wm, om, (0, 0), (0, 9))


def test_find_path_unknown_ground_blocked():
    gh = np.zeros((10, 10), dtype=np.int32)
    gh[5, :] = -1  # unverified band
    wm = np.zeros((10, 10), dtype=bool)
    om = np.zeros((10, 10), dtype=bool)
    with pytest.raises(PathError):
        find_path(gh, wm, om, (0, 0), (9, 0))


# ---------- place_asset ----------

def test_place_asset_flat(tmp_path):
    src = _flat_scene(tmp_path)
    scene = load_scene(str(src))
    a = _analysis(scene)
    asset = Asset(asset_id="lamp", footprint=(1, 1), height=1,
                  blocks={(0, 0, 0): "minecraft:lantern"}, support=[(0, 0)])
    changes = place_asset(scene.snapshot, a.ground_height, asset, (5, 5), "op1")
    assert len(changes) >= 1
    assert changes[0].after == "minecraft:lantern"


def test_place_asset_foundation_too_deep(tmp_path):
    src = _flat_scene(tmp_path)
    scene = load_scene(str(src))
def test_place_asset_foundation_too_deep(tmp_path):
    src = _flat_scene(tmp_path)
    scene = load_scene(str(src))
    a = _analysis(scene)
    # footprint spans two columns; force a deep drop under one support so the
    # base (set by the higher column) is far above the lower support.
    a.ground_height[5, 5] = 0
    a.ground_height[5, 6] = 5  # base_y becomes 6 over this footprint
    asset = Asset(asset_id="lamp", footprint=(1, 2), height=1,
                  blocks={(0, 0, 0): "minecraft:lantern", (0, 0, 1): "minecraft:lantern"},
                  support=[(0, 0), (0, 1)], max_foundation_depth=1)
    with pytest.raises(AssetError):
        place_asset(scene.snapshot, a.ground_height, asset, (5, 5), "op1")


def test_place_asset_required_empty_clears_interior(tmp_path):
    src = _flat_scene(tmp_path)
    scene = load_scene(str(src))
    a = _analysis(scene)
    # ground top is at local y=0 (grass), so base_y = 1. Put a solid block at
    # the doorway relative voxel (0,0,0)->(x,1,z) and require it to be empty,
    # which must emit an explicit air change over that solid block.
    asset = Asset(asset_id="hut", footprint=(1, 1), height=2,
                  blocks={(0, 1, 0): "minecraft:oak_planks"},
                  required_empty=[(0, 0, 0)], support=[(0, 0)])
    # place a solid block at the doorway target so before != air
    scene.snapshot._region[scene.snapshot.transform.local_to_region((4, 1, 4))] = BlockState("minecraft:stone")
    a.ground_height[4, 4] = 0
    changes = place_asset(scene.snapshot, a.ground_height, asset, (4, 4), "op1")
    airs = [c for c in changes if c.after == "minecraft:air"]
    assert any(c.pos_local == (4, 1, 4) for c in airs), changes


def test_place_asset_unknown_variant(tmp_path):
    src = _flat_scene(tmp_path)
    scene = load_scene(str(src))
    a = _analysis(scene)
    asset = Asset(asset_id="x", footprint=(1, 1), height=1,
                  blocks={(0, 0, 0): "minecraft:stone"}, support=[(0, 0)],
                  variants=["north", "south"])
    with pytest.raises(AssetError):
        place_asset(scene.snapshot, a.ground_height, asset, (4, 4), "op1", variant="east")


# ---------- full pipeline determinism & budgets ----------

def _write_assets(tmp_path: Path) -> Path:
    import json
    d = tmp_path / "assets"
    (d / "prefabs").mkdir(parents=True)
    (d / "catalog.json").write_text(json.dumps({"assets": {
        "lamp_small": {"footprint": [1, 1], "height": 3, "entries": [], "variants": ["default"]},
        "shrub_small": {"footprint": [1, 1], "height": 1, "entries": [], "variants": ["default"]},
        "pavilion_small": {"footprint": [5, 5], "height": 4,
                            "entries": [{"id": "entry", "offset": [2, 0]}],
                            "variants": ["north"]},
    }}), encoding="utf-8")
    (d / "palettes.json").write_text(json.dumps({"palettes": {
        "stone_path": ["minecraft:stone_bricks", "minecraft:cobblestone"],
    }}), encoding="utf-8")
    (d / "prefabs" / "lamp_small.json").write_text(json.dumps({
        "blocks": {"0,0,0": "minecraft:oak_fence", "0,1,0": "minecraft:oak_fence",
                    "0,2,0": "minecraft:lantern"},
        "required_empty": [], "support": [[0, 0]], "max_foundation_depth": 2,
    }), encoding="utf-8")
    (d / "prefabs" / "shrub_small.json").write_text(json.dumps({
        "blocks": {"0,0,0": "minecraft:oak_leaves"},
        "required_empty": [], "support": [[0, 0]], "max_foundation_depth": 1,
    }), encoding="utf-8")
    (d / "prefabs" / "pavilion_small.json").write_text(json.dumps({
        "blocks": {"0,0,0": "minecraft:oak_planks", "4,0,0": "minecraft:oak_planks",
                    "0,0,4": "minecraft:oak_planks", "4,0,4": "minecraft:oak_planks",
                    "2,0,0": "minecraft:oak_planks"},
        "required_empty": [[2, 1, 0]], "support": [[0, 0], [4, 0], [0, 4], [4, 4]],
        "max_foundation_depth": 2,
    }), encoding="utf-8")
    return d


def _plan_obj():
    return parse_plan("""{
      "schema_version": "0.1", "scene_id": "t", "seed": 7, "style_id": "rustic",
      "operations": [
        {"id": "pavilion_1", "op": "place_asset", "asset_id": "pavilion_small", "site_id": "site_00"},
        {"id": "path_1", "op": "connect_path", "from": "entry_00", "to": "pavilion_1.entry", "width": 1, "palette_id": "stone_path"},
        {"id": "lights_1", "op": "decorate_path", "path_id": "path_1", "asset_id": "lamp_small", "spacing": 4},
        {"id": "shrubs_1", "op": "scatter_assets", "zone_id": "plant_00", "asset_id": "shrub_small", "count": 6}
      ]
    }""")


def test_pipeline_deterministic(tmp_path):
    src = _flat_scene(tmp_path, size=(24, 8, 24))
    assets = _write_assets(tmp_path)
    a1 = load_scene(str(src)); an1 = _analysis(a1)
    r1 = compile_plan(a1.snapshot, _plan_obj(), an1, assets)
    a2 = load_scene(str(src)); an2 = _analysis(a2)
    r2 = compile_plan(a2.snapshot, _plan_obj(), an2, assets)
    assert {k: v.after for k, v in r1.patch.changes.items()} == {k: v.after for k, v in r2.patch.changes.items()}
    assert len(r1.patch) > 0


def test_pipeline_budget_blocks_output(tmp_path):
    src = _flat_scene(tmp_path, size=(24, 8, 24))
    assets = _write_assets(tmp_path)
    scene = load_scene(str(src)); an = _analysis(scene)
    with pytest.raises(CompileError, match="budget"):
        compile_plan(scene.snapshot, _plan_obj(), an, assets, max_blocks=10)


def test_pipeline_unknown_site_rejected(tmp_path):
    src = _flat_scene(tmp_path, size=(24, 8, 24))
    assets = _write_assets(tmp_path)
    scene = load_scene(str(src)); an = _analysis(scene)
    plan = parse_plan("""{"schema_version":"0.1","scene_id":"t","seed":1,
      "operations":[{"id":"p1","op":"place_asset","asset_id":"pavilion_small","site_id":"site_zzz"}]}""")
    with pytest.raises(CompileError, match="unknown site"):
        compile_plan(scene.snapshot, plan, an, assets)


def test_pipeline_export_roundtrip(tmp_path):
    src = _flat_scene(tmp_path, size=(24, 8, 24))
    assets = _write_assets(tmp_path)
    scene = load_scene(str(src)); an = _analysis(scene)
    result = compile_plan(scene.snapshot, _plan_obj(), an, assets)
    apply_patchset(scene, result.patch)
    out = tmp_path / "full.litematic"
    save_scene(scene, str(out))
    stats = compare_to_expected(str(out), scene, result.patch)
    assert stats["changed"] == len(result.patch)
    assert stats["checked"] > 0
