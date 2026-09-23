"""Acceptance C: Agent protocol robustness.

The Agent only picks from existing references. Invalid JSON, unknown blocks,
fabricated sites, unsolvable routes and Agent/API interruption must all leave
the original input untouched and never export an invalid result.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from litegarden.compiler import CompileError, compile_plan
from litegarden.io import load_scene
from litegarden.schema import parse_plan
from litegarden.terrain import analyze, build_planning_index

from .test_operations import _flat_scene, _write_assets


def _scene(tmp_path):
    src = _flat_scene(tmp_path, size=(24, 8, 24))
    scene = load_scene(str(src))
    a = analyze(scene.snapshot)
    build_planning_index(a)
    return src, scene, a


def _hash(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# ---------- schema-level rejection ----------

def test_invalid_json_rejected():
    with pytest.raises(ValidationError):
        parse_plan("{not json")


def test_unknown_operation_rejected():
    with pytest.raises(ValidationError):
        parse_plan('{"schema_version":"0.1","scene_id":"t","operations":[{"id":"x","op":"nuke"}]}')


def test_extra_field_rejected():
    with pytest.raises(ValidationError):
        parse_plan('{"schema_version":"0.1","scene_id":"t","operations":[],"evil":1}')


def test_out_of_range_param_rejected():
    with pytest.raises(ValidationError):
        parse_plan('{"schema_version":"0.1","scene_id":"t","operations":['
                   '{"id":"p","op":"connect_path","from":"a","to":"b","width":99,"palette_id":"stone_path"}]}')


def test_non_integer_coordinate_rejected():
    # count must be an int, not a float
    with pytest.raises(ValidationError):
        parse_plan('{"schema_version":"0.1","scene_id":"t","operations":['
                   '{"id":"s","op":"scatter_assets","zone_id":"z","asset_id":"a","count":1.5}]}')


# ---------- compile-level rejection (fabricated references) ----------

def test_fabricated_site_rejected(tmp_path):
    _, scene, a = _scene(tmp_path)
    assets = _write_assets(tmp_path)
    plan = parse_plan('{"schema_version":"0.1","scene_id":"t","seed":1,"operations":['
                      '{"id":"p1","op":"place_asset","asset_id":"pavilion_small","site_id":"site_zzz"}]}')
    with pytest.raises(CompileError, match="unknown site"):
        compile_plan(scene.snapshot, plan, a, assets)


def test_fabricated_asset_rejected(tmp_path):
    _, scene, a = _scene(tmp_path)
    assets = _write_assets(tmp_path)
    plan = parse_plan('{"schema_version":"0.1","scene_id":"t","seed":1,"operations":['
                      '{"id":"p1","op":"place_asset","asset_id":"castle_huge","site_id":"site_00"}]}')
    with pytest.raises(CompileError, match="unknown asset"):
        compile_plan(scene.snapshot, plan, a, assets)


def test_fabricated_anchor_rejected(tmp_path):
    _, scene, a = _scene(tmp_path)
    assets = _write_assets(tmp_path)
    plan = parse_plan('{"schema_version":"0.1","scene_id":"t","seed":1,"operations":['
                      '{"id":"p1","op":"connect_path","from":"entry_zz","to":"entry_01","width":1,"palette_id":"stone_path"}]}')
    with pytest.raises(CompileError, match="unresolvable reference"):
        compile_plan(scene.snapshot, plan, a, assets)


def test_fabricated_palette_rejected(tmp_path):
    _, scene, a = _scene(tmp_path)
    assets = _write_assets(tmp_path)
    plan = parse_plan('{"schema_version":"0.1","scene_id":"t","seed":1,"operations":['
                      '{"id":"p1","op":"connect_path","from":"entry_00","to":"entry_01","width":1,"palette_id":"lava_path"}]}')
    with pytest.raises(CompileError, match="unknown palette"):
        compile_plan(scene.snapshot, plan, a, assets)


def test_fabricated_zone_rejected(tmp_path):
    _, scene, a = _scene(tmp_path)
    assets = _write_assets(tmp_path)
    plan = parse_plan('{"schema_version":"0.1","scene_id":"t","seed":1,"operations":['
                      '{"id":"s1","op":"scatter_assets","zone_id":"plant_zz","asset_id":"shrub_small","count":3}]}')
    with pytest.raises(CompileError, match="unknown zone"):
        compile_plan(scene.snapshot, plan, a, assets)


# ---------- unsolvable route ----------

def test_unsolvable_route_reports_op_id(tmp_path):
    _, scene, a = _scene(tmp_path)
    assets = _write_assets(tmp_path)
    # wall off the map: make a full-height barrier so no path exists
    a.ground_height[:, 12] = -1
    plan = parse_plan('{"schema_version":"0.1","scene_id":"t","seed":1,"operations":['
                      '{"id":"path_1","op":"connect_path","from":"entry_00","to":"entry_01","width":1,"palette_id":"stone_path"}]}')
    # force anchors onto opposite sides of the barrier
    a.anchors = {"entry_00": (0, 0), "entry_01": (23, 23)}
    with pytest.raises(CompileError) as ei:
        compile_plan(scene.snapshot, plan, a, assets)
    assert ei.value.op_id == "path_1"


# ---------- input preservation on failure ----------

def test_input_untouched_on_compile_failure(tmp_path):
    src, scene, a = _scene(tmp_path)
    assets = _write_assets(tmp_path)
    before = _hash(src)
    plan = parse_plan('{"schema_version":"0.1","scene_id":"t","seed":1,"operations":['
                      '{"id":"p1","op":"place_asset","asset_id":"pavilion_small","site_id":"site_zzz"}]}')
    with pytest.raises(CompileError):
        compile_plan(scene.snapshot, plan, a, assets)
    assert _hash(src) == before


def test_input_untouched_on_schema_failure(tmp_path):
    src, scene, a = _scene(tmp_path)
    before = _hash(src)
    with pytest.raises(ValidationError):
        parse_plan("{not json")
    assert _hash(src) == before
