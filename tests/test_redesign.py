"""P3: frozen authorisation, halo, boundary contracts, task state machine."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from litegarden.constraints import Box3
from litegarden.redesign import (
    DEFAULT_HALO_XZ,
    INVALID_SELECTION,
    INVALID_TASK_STATE,
    LEGACY_BOUNDS_NEEDS_MIGRATION,
    PARTIAL_OBJECT_IN_SELECTION,
    SELECTION_OUT_OF_BOUNDS,
    STALE_BASE_REVISION,
    STALE_HEAD_GENERATION,
    CONFIG_HASH_MISMATCH,
    AttemptRecord,
    RedesignError,
    Selection,
    TaskRequest,
    TaskStore,
    analyze_selection,
    assert_transition,
    ensure_task_fresh,
    freeze_task,
    halo_for,
    migrate_selection,
)

BOUNDS = Box3((0, 0, 0), (64, 32, 64))


def _object(object_id, voxels, **extra):
    return {"object_id": object_id, "kind": "asset", "complete": True,
            "occupied_voxels": [list(v) for v in voxels], **extra}


# ---------------------------------------------------------------- selection


def test_selection_is_half_open_and_reports_size():
    sel = Selection.from_dict({"min": [10, 5, 20], "max_exclusive": [20, 15, 30]}, bounds=BOUNDS)
    assert sel.size == (10, 10, 10)
    assert sel.volume == 1000
    assert sel.contains((19, 14, 29))
    assert not sel.contains((20, 14, 29))
    assert sel.to_dict()["schema_version"] == "0.2"


def test_legacy_endpoint_form_is_refused_without_explicit_migration():
    with pytest.raises(RedesignError) as ei:
        Selection.from_dict({"min": [1, 1, 1], "max": [2, 2, 2]}, bounds=BOUNDS)
    assert ei.value.code == LEGACY_BOUNDS_NEEDS_MIGRATION


def test_migrate_selection_converts_inclusive_endpoints_deliberately():
    sel = migrate_selection({"min": [1, 1, 1], "max": [3, 3, 3]}, bounds=BOUNDS)
    assert sel.min == (1, 1, 1) and sel.max_exclusive == (4, 4, 4)
    assert sel.size == (3, 3, 3)
    sel2 = migrate_selection({"bbox": [1, 1, 3, 3], "min_y": 2, "max_y": 4}, bounds=BOUNDS)
    assert sel2.min == (1, 2, 1) and sel2.max_exclusive == (4, 5, 4)


def test_selection_outside_scene_bounds_is_refused():
    with pytest.raises(RedesignError) as ei:
        Selection.from_dict({"min": [60, 0, 0], "max_exclusive": [80, 8, 8]}, bounds=BOUNDS)
    assert ei.value.code == SELECTION_OUT_OF_BOUNDS


def test_selection_rejects_non_integers_and_empty_boxes():
    with pytest.raises(RedesignError):
        Selection.from_dict({"min": [0, 0, 0], "max_exclusive": [4.5, 8, 8]})
    with pytest.raises(RedesignError):
        Selection.from_dict({"min": [0, 0, 0], "max_exclusive": [4, True, 8]})


# ---------------------------------------------------------------- halo


def test_halo_is_read_only_and_expands_horizontally_only():
    sel = Selection.from_dict({"min": [20, 4, 20], "max_exclusive": [30, 12, 30]})
    halo = halo_for(sel, DEFAULT_HALO_XZ)
    assert halo.box.min == (8, 4, 8)
    assert halo.box.max_exclusive == (42, 12, 42)
    d = halo.to_dict()
    assert d["read_only"] is True and d["blend_band"] is None
    assert "no write outside the selection" in d["note"]


# ---------------------------------------------------------------- analysis


def test_partial_object_intersection_requires_a_user_decision():
    sel = Selection.from_dict({"min": [10, 0, 10], "max_exclusive": [20, 16, 20]})
    obj = _object("pavilion_003", [(12, 1, 12), (13, 1, 12), (25, 1, 25)])
    report = analyze_selection(sel, scene_bounds=BOUNDS, objects={"pavilion_003": obj})
    assert report.blocked
    assert report.partial_objects[0]["object_id"] == "pavilion_003"
    assert report.partial_objects[0]["inside_voxels"] == 2
    assert report.partial_objects[0]["resolution"] == "expand_selection_or_keep_object"


def test_fully_inside_object_is_not_a_partial_intersection():
    sel = Selection.from_dict({"min": [10, 0, 10], "max_exclusive": [20, 16, 20]})
    obj = _object("lamp_1", [(12, 1, 12), (13, 1, 12)])
    report = analyze_selection(sel, scene_bounds=BOUNDS, objects={"lamp_1": obj})
    assert report.partial_objects == ()
    assert not report.blocked


def test_protected_overlap_and_pending_anchors_are_reported():
    sel = Selection.from_dict({"min": [10, 0, 10], "max_exclusive": [20, 16, 20]})
    report = analyze_selection(
        sel, scene_bounds=BOUNDS,
        protected_boxes=[Box3((15, 0, 15), (18, 8, 18))],
        anchors={"entry_far": (50, 50)},
    )
    assert len(report.protected_overlap) == 1
    assert report.anchor_inside == ()
    assert "entry_far" in report.anchor_pending
    assert any("cannot be determined" in w for w in report.warnings)


def test_boundary_interfaces_are_carried_from_object_links():
    sel = Selection.from_dict({"min": [10, 0, 10], "max_exclusive": [20, 16, 20]})
    obj = _object("path_1", [(12, 1, 12)], boundary_links=[{
        "interface_id": "path_1@east", "cells": [[19, 1, 12], [19, 2, 12]],
        "direction": "east", "width": 1, "landing_y": 1, "status": "confirmed",
    }])
    report = analyze_selection(sel, scene_bounds=BOUNDS, objects={"path_1": obj})
    assert len(report.boundary_interfaces) == 1
    iface = report.boundary_interfaces[0].to_dict()
    assert iface["interface_id"] == "path_1@east" and iface["status"] == "confirmed"
    assert iface["cells"] == [[19, 1, 12], [19, 2, 12]]


# ---------------------------------------------------------------- freezing


def _freeze(**over):
    kwargs = dict(
        request_id="task_001", project_id="demo", base_revision_id="r003",
        base_scene_hash="abc123", head_generation=7, config_revision="cfg002",
        config_hash="cfg-hash", mode="revise_current", instruction="重新设计入口",
    )
    kwargs.update(over)
    return freeze_task(**kwargs)


def test_freeze_task_records_server_owned_authorisation():
    sel = Selection.from_dict({"min": [10, 5, 20], "max_exclusive": [42, 37, 52]})
    req = _freeze(selection=sel, seed=42)
    d = req.to_dict()
    assert d["schema_version"] == "0.2"
    assert d["selection"]["max_exclusive"] == [42, 37, 52]
    assert d["context_halo_xz"] == DEFAULT_HALO_XZ
    assert "frozen by the server" in d["authorisation_note"]
    assert TaskRequest.from_dict(d).selection == sel


def test_freeze_requires_instruction_and_targets_for_replacement():
    sel = Selection.from_dict({"min": [0, 0, 0], "max_exclusive": [4, 4, 4]})
    with pytest.raises(RedesignError):
        _freeze(selection=sel, instruction="   ")
    with pytest.raises(RedesignError):
        _freeze(selection=sel, mode="replace_generated")
    ok = _freeze(selection=sel, mode="replace_generated", target_ids=["pavilion_003"])
    assert ok.target_ids == ("pavilion_003",)


def test_freeze_refuses_while_a_known_object_is_only_partly_selected():
    sel = Selection.from_dict({"min": [10, 0, 10], "max_exclusive": [20, 16, 20]})
    report = analyze_selection(
        sel, scene_bounds=BOUNDS,
        objects={"pavilion_003": _object("pavilion_003", [(12, 1, 12), (25, 1, 25)])},
    )
    with pytest.raises(RedesignError) as ei:
        _freeze(selection=sel, report=report)
    assert ei.value.code == PARTIAL_OBJECT_IN_SELECTION


def test_bools_and_floats_are_never_coerced_to_integers():
    sel = Selection.from_dict({"min": [0, 0, 0], "max_exclusive": [4, 4, 4]})
    with pytest.raises(RedesignError):
        _freeze(selection=sel, head_generation=True)
    with pytest.raises(RedesignError):
        _freeze(selection=sel, seed=1.5)


def test_ensure_task_fresh_detects_stale_head_generation_and_config():
    sel = Selection.from_dict({"min": [0, 0, 0], "max_exclusive": [4, 4, 4]})
    req = _freeze(selection=sel)
    ensure_task_fresh(req, head_revision_id="r003", head_generation=7,
                      config_hash="cfg-hash", scene_hash="abc123")
    with pytest.raises(RedesignError) as e1:
        ensure_task_fresh(req, head_revision_id="r004", head_generation=7,
                          config_hash="cfg-hash", scene_hash="abc123")
    assert e1.value.code == STALE_BASE_REVISION
    with pytest.raises(RedesignError) as e2:
        ensure_task_fresh(req, head_revision_id="r003", head_generation=8,
                          config_hash="cfg-hash", scene_hash="abc123")
    assert e2.value.code == STALE_HEAD_GENERATION
    with pytest.raises(RedesignError) as e3:
        ensure_task_fresh(req, head_revision_id="r003", head_generation=7,
                          config_hash="other", scene_hash="abc123")
    assert e3.value.code == CONFIG_HASH_MISMATCH


def test_task_state_machine_refuses_illegal_transitions():
    assert_transition("CREATED", "CONTEXT_READY")
    assert_transition("COMPILING", "DATA_VALIDATING")
    assert_transition("READY_FOR_USER", "ACCEPTED")
    for bad in [("CREATED", "ACCEPTED"), ("ACCEPTED", "PLANNING"),
                ("READY_FOR_USER", "COMPILING"), ("CANCELED", "PLANNING")]:
        with pytest.raises(RedesignError) as ei:
            assert_transition(*bad)
        assert ei.value.code == INVALID_TASK_STATE


# ---------------------------------------------------------------- storage


def test_task_store_freezes_request_and_never_overwrites_attempts(tmp_path):
    sel = Selection.from_dict({"min": [10, 0, 10], "max_exclusive": [20, 16, 20]})
    store = TaskStore(tmp_path)
    req = _freeze(selection=sel)
    store.create_task(req, context={"summary": "flat area"})
    assert store.read_state("task_001")["state"] == "CREATED"
    assert store.read_request("task_001").selection == sel

    a1 = store.create_attempt("task_001")
    assert a1.name == "a001"
    store.write_attempt_json("task_001", "a001", "validation.json", {"ok": False})
    a2 = store.create_attempt("task_001")
    assert a2.name == "a002"
    assert json.loads((a1 / "validation.json").read_text(encoding="utf-8")) == {"ok": False}
    assert store.list_attempts("task_001") == ["a001", "a002"]

    with pytest.raises(RedesignError):
        store.create_attempt("task_001", attempt_id="a001")
    with pytest.raises(RedesignError):
        store.create_task(req)


def test_task_store_advances_state_and_records_attempt_metadata(tmp_path):
    sel = Selection.from_dict({"min": [0, 0, 0], "max_exclusive": [4, 4, 4]})
    store = TaskStore(tmp_path)
    store.create_task(_freeze(selection=sel))
    store.set_state("task_001", "CONTEXT_READY")
    store.set_state("task_001", "PLANNING")
    with pytest.raises(RedesignError):
        store.set_state("task_001", "ACCEPTED")
    attempt_dir = store.create_attempt("task_001")
    store.append_write_log("task_001", "a001", [{"pos_local": [1, 1, 1], "op_id": "op1"}])
    log = (attempt_dir / "write_log.jsonl").read_text(encoding="utf-8").strip()
    assert json.loads(log)["op_id"] == "op1"
    rec = store.read_attempt("task_001", "a001")
    assert isinstance(rec, AttemptRecord) and rec.state == "CREATED"
    updated = store.update_attempt("task_001", rec, state="FAILED",
                                   errors=[{"code": "BEFORE_MISMATCH"}])
    assert updated.state == "FAILED"
    assert store.read_attempt("task_001", "a001").errors[0]["code"] == "BEFORE_MISMATCH"
