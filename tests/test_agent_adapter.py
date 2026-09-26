"""P5: the Agent adapter - contract, bounded run, overreach refusal, read-only review.

Everything here is offline: the "Agent" is a local Python script started by the
real subprocess transport (a fake runner), or an injected provider call.  No
network, no model, no third-party dependency beyond what the project already
uses (the shared plan schema is exercised through its own import).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from litegarden.agent_adapter import (
    AGENT_BUDGET_EXCEEDED,
    AGENT_CANCELED,
    AGENT_EXIT_NONZERO,
    AGENT_OUTPUT_TOO_LARGE,
    AGENT_PLAN_INVALID,
    AGENT_PLAN_OUT_OF_SELECTION,
    AGENT_PLAN_UNVERIFIABLE,
    AGENT_PROTOCOL_INVALID,
    AGENT_REVIEW_INVALID,
    AGENT_REVIEW_NOT_READONLY,
    AGENT_STALE_EVIDENCE,
    AGENT_TIMEOUT,
    AGENT_UNAVAILABLE,
    AgentAdapter,
    AgentConfig,
    AgentConfigError,
    AgentContext,
    ProviderSpec,
    ReadonlyTool,
    ReadonlyToolRegistry,
    RunnerSpec,
    build_plan_payload,
    build_review_payload,
    run_agent_plan,
    run_agent_review,
    validate_plan,
)
from litegarden.redesign import Selection, TaskStore, freeze_task

TASK_ID = "task_001"
SELECTION = {"min": [0, 0, 0], "max_exclusive": [16, 8, 16]}

SITES = {
    "site_00": {"id": "site_00", "origin": [2, 2], "footprint": [4, 4], "mean_height": 1},
    "site_far": {"id": "site_far", "origin": [40, 40], "footprint": [4, 4], "mean_height": 1},
}
ZONES = {
    "plant_00": {"id": "plant_00", "bbox": [2, 2, 8, 8]},
    "plant_far": {"id": "plant_far", "bbox": [40, 40, 48, 48]},
}
ANCHORS = {"entry_00": (3, 3), "entry_edge": (1, 1), "entry_far": (60, 60)}


# --------------------------------------------------------------------------
# fixtures / helpers
# --------------------------------------------------------------------------


class _Analysis:
    """Duck-typed stand-in for litegarden.terrain.TerrainAnalysis."""

    def __init__(self, sites=None, zones=None, anchors=None):
        self.site_candidates = list((sites or SITES).values())
        self.zones = dict(zones or ZONES)
        self.anchors = dict(anchors or ANCHORS)


def _assets(tmp_path: Path) -> Path:
    d = tmp_path / "assets"
    (d / "prefabs").mkdir(parents=True, exist_ok=True)
    (d / "catalog.json").write_text(json.dumps({"assets": {
        "pavilion_small": {"footprint": [5, 5], "height": 4,
                           "entries": [{"id": "entry", "offset": [2, 0]}],
                           "variants": ["north"]},
        "lamp_small": {"footprint": [1, 1], "height": 3, "entries": [],
                       "variants": ["default"]},
        "shrub_small": {"footprint": [1, 1], "height": 1, "entries": [],
                        "variants": ["default"]},
    }}), encoding="utf-8")
    (d / "palettes.json").write_text(json.dumps({"palettes": {
        "stone_path": {"surface": ["minecraft:stone_bricks", "minecraft:cobblestone"],
                       "edge": "minecraft:cobblestone",
                       "support": "minecraft:cobblestone",
                       "transition": "minecraft:stone_brick_slab"},
        "lava_path": {"surface": ["minecraft:lava"]},
    }}), encoding="utf-8")
    (d / "block_rules.json").write_text(json.dumps({
        "allowed_new_blocks": [
            "minecraft:stone_bricks", "minecraft:cobblestone",
            "minecraft:stone_brick_slab", "minecraft:oak_planks",
            "minecraft:oak_fence", "minecraft:lantern", "minecraft:oak_leaves",
        ],
    }), encoding="utf-8")
    (d / "prefabs" / "pavilion_small.json").write_text(json.dumps({
        "blocks": {"0,0,0": "minecraft:oak_planks", "4,0,4": "minecraft:oak_planks"},
    }), encoding="utf-8")
    (d / "prefabs" / "lamp_small.json").write_text(json.dumps({
        "blocks": {"0,0,0": "minecraft:oak_fence", "0,1,0": "minecraft:lantern"},
    }), encoding="utf-8")
    (d / "prefabs" / "shrub_small.json").write_text(json.dumps({
        "blocks": {"0,0,0": "minecraft:oak_leaves"},
    }), encoding="utf-8")
    return d


def _task(tmp_path: Path, *, selection=None, task_id: str = TASK_ID, instruction="放一个亭子"):
    store = TaskStore(tmp_path / "project")
    request = freeze_task(
        request_id=task_id, project_id="demo", base_revision_id="r001",
        base_scene_hash="deadbeef", head_generation=1, config_revision="cfg001",
        config_hash="cfg-hash",
        selection=Selection.from_dict(selection or SELECTION),
        mode="revise_current", instruction=instruction,
    )
    store.create_task(request, context={"selection_report": {"warnings": []}})
    return store, request


def _context(request, assets_dir: Path, attempt_id: str = "a001") -> AgentContext:
    return AgentContext(
        request=request, attempt_id=attempt_id, sites=dict(SITES), zones=dict(ZONES),
        anchors=dict(ANCHORS), assets_dir=assets_dir,
    )


def _write_script(tmp_path: Path, body: str, name: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def _adapter(script: Path, tmp_path: Path, *, timeout=20.0, cap=2 * 1024 * 1024,
             env_allowlist=None, tools=None, max_evidence=8, args=()):
    spec = RunnerSpec(
        argv=(sys.executable, str(script), *[str(a) for a in args]),
        timeout_seconds=timeout, max_output_bytes=cap, working_dir=tmp_path,
        **({"env_allowlist": env_allowlist} if env_allowlist is not None else {}),
    )
    config = AgentConfig(mode="runner", runner=spec, max_evidence_requests=max_evidence)
    return AgentAdapter(config, tools=tools)


PLAN_FROM_FILE = '''
import json
import sys

request = json.loads(sys.stdin.buffer.read().decode("utf-8"))
plan = json.loads(open(sys.argv[1], encoding="utf-8").read())
envelope = {
    "schema_version": "0.2",
    "request_kind": "generate_plan",
    "task_id": request["task"]["task_id"],
    "attempt_id": request["attempt_id"],
    "plan": plan,
    "agent_evidence": {"summary": "loaded from file", "confidence": "medium"},
}
sys.stdout.buffer.write(json.dumps(envelope).encode("utf-8"))
sys.stdout.flush()
'''

REVIEW_FROM_FILE = '''
import json
import sys

request = json.loads(sys.stdin.buffer.read().decode("utf-8"))
envelope = json.loads(open(sys.argv[1], encoding="utf-8").read())
envelope["request_kind"] = "review_evidence"
envelope["task_id"] = request["task"]["task_id"]
envelope["attempt_id"] = request["attempt_id"]
sys.stdout.buffer.write(json.dumps(envelope).encode("utf-8"))
sys.stdout.flush()
'''

ENV_REPORTING = '''
import json
import os
import sys

request = json.loads(sys.stdin.buffer.read().decode("utf-8"))
info = {
    "has_request": isinstance(request.get("task"), dict),
    "token_visible": "LITEGARDEN_TOKEN" in os.environ,
    "key_visible": "OPENAI_API_KEY" in os.environ,
    "path_visible": bool(os.environ.get("PATH")),
    "cwd": os.getcwd(),
    "stdin_isatty": sys.stdin.isatty(),
}
envelope = {
    "schema_version": "0.2",
    "request_kind": "generate_plan",
    "task_id": request["task"]["task_id"],
    "attempt_id": request["attempt_id"],
    "plan": {"schema_version": "0.1", "scene_id": "demo", "seed": 0, "operations": []},
    "agent_evidence": {"summary": json.dumps(info)},
}
sys.stdout.buffer.write(json.dumps(envelope).encode("utf-8"))
sys.stdout.flush()
'''


def _plan(ops, **extra):
    return {"schema_version": "0.1", "scene_id": "demo", "seed": 7,
            "operations": list(ops), **extra}


LEGAL_OPS = [
    {"id": "pavilion_1", "op": "place_asset", "asset_id": "pavilion_small",
     "site_id": "site_00", "variant": "north"},
    {"id": "path_1", "op": "connect_path", "from": "entry_00", "to": "pavilion_1.entry",
     "width": 1, "palette_id": "stone_path"},
    {"id": "lights_1", "op": "decorate_path", "path_id": "path_1",
     "asset_id": "lamp_small", "spacing": 4},
    {"id": "shrubs_1", "op": "scatter_assets", "zone_id": "plant_00",
     "asset_id": "shrub_small", "count": 3},
]


def _plan_file(tmp_path: Path, plan, name: str = "plan.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(plan), encoding="utf-8")
    return path


def _envelope_file(tmp_path: Path, envelope, name: str = "review.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(envelope), encoding="utf-8")
    return path


def _review_envelope(**over):
    envelope = {
        "schema_version": "0.2",
        "candidate_id": "c004",
        "scene_hash": "scene-hash-1",
        "review_kind": "hard_error_only",
        "verdict": "suspected_issue",
        "findings": [{
            "id": "finding_01", "code": "ENTRY_BLOCKED", "op_id": "pavilion_1",
            "observation": "入口下部似乎有占位方块",
            "requested_check": "validate_entry_clearance",
        }],
        "evidence_requests": [],
    }
    envelope.update(over)
    return envelope


def _evidence(**over):
    evidence = {
        "candidate": {"candidate_id": "c004", "scene_hash": "scene-hash-1",
                      "file_sha256": "abc", "changes": 12},
        "hard_checks": {"ok": True, "checks": ["write_guard", "net_patch"]},
        "render": {"ok": True, "receipt": "frame-0001"},
        "manifest": {"images": ["after_top.png", "entry_pavilion_1.png"]},
        "coverage": {"checked": ["selection"], "unsupported": []},
    }
    evidence.update(over)
    return evidence


def _state(store: TaskStore, task_id: str = TASK_ID) -> str:
    return store.read_state(task_id)["state"]


# --------------------------------------------------------------------------
# 1. no provider configured: WAITING_AGENT, nothing fabricated
# --------------------------------------------------------------------------


def test_no_agent_configured_parks_the_task_in_waiting_agent(tmp_path):
    store, _ = _task(tmp_path)
    assets = _assets(tmp_path)
    adapter = AgentAdapter.from_config(None)
    assert adapter.mode == "none" and adapter.available is False

    outcome = run_agent_plan(adapter, store, TASK_ID, analysis=_Analysis(), assets_dir=assets)

    assert outcome.status == "WAITING_AGENT"
    assert outcome.ok is False
    assert outcome.plan is None and outcome.attempt_id is None
    assert outcome.error_code == AGENT_UNAVAILABLE
    assert store.list_attempts(TASK_ID) == []
    task_dir = store.task_dir(TASK_ID)
    assert list(task_dir.rglob("plan.json")) == []
    assert _state(store) == "WAITING_AGENT"
    parked = json.loads((task_dir / "agent_state.json").read_text(encoding="utf-8"))
    assert parked["reason"] == AGENT_UNAVAILABLE and parked["mode"] == "none"
    assert "fabricated" in " ".join(outcome.notes)


def test_no_agent_never_claims_a_review_was_executed(tmp_path):
    store, _ = _task(tmp_path)
    adapter = AgentAdapter.from_config(None)
    store.create_attempt(TASK_ID)

    outcome = run_agent_review(adapter, store, TASK_ID, "a001", evidence=_evidence())

    assert outcome.status == "WAITING_AGENT"
    assert outcome.review_executed is False and outcome.verdict is None
    assert outcome.error_code == AGENT_UNAVAILABLE
    record = store.read_attempt(TASK_ID, "a001")
    assert record.review["review_executed"] is False
    assert record.review["verdict"] is None
    assert "plan" not in record.review


# --------------------------------------------------------------------------
# 2. runner mode: the happy path is a real attempt
# --------------------------------------------------------------------------


def test_runner_plan_is_validated_and_recorded_as_an_attempt(tmp_path):
    store, request = _task(tmp_path)
    assets = _assets(tmp_path)
    script = _write_script(tmp_path, PLAN_FROM_FILE, "plan_runner.py")
    adapter = _adapter(script, tmp_path, args=[_plan_file(tmp_path, _plan(LEGAL_OPS))])

    outcome = run_agent_plan(adapter, store, TASK_ID, analysis=_Analysis(), assets_dir=assets)

    assert outcome.ok and outcome.status == "PLAN_READY"
    assert outcome.attempt_id == "a001"
    assert outcome.plan["operations"][0]["id"] == "pavilion_1"
    assert outcome.agent_evidence["summary"] == "loaded from file"
    assert [c["check"] for c in outcome.checks][:3] == [
        "plan_shape", "shared_plan_schema", "reference_allow_list"
    ]
    assert {c["check"] for c in outcome.checks} >= {
        "block_whitelist", "write_box_within_selection"
    }
    record = store.read_attempt(TASK_ID, "a001")
    assert record.state == "PLAN_READY"
    plan_file = json.loads(
        (store.task_dir(TASK_ID) / "attempts" / "a001" / "plan.json")
        .read_text(encoding="utf-8")
    )
    assert plan_file["operations"][2]["path_id"] == "path_1"
    # AttemptRecord.to_dict() only ever reports has_plan, so the accepted plan is
    # read back from the attempt's own plan.json (what the service layer writes too)
    assert record.validation["plan_file"] == "plan.json"
    assert record.validation["ok"] is True
    assert record.validation["agent_evidence_trust"] == "self_reported"
    assert _state(store) == "PLAN_READY"

    attempt_dir = store.task_dir(TASK_ID) / "attempts" / "a001"
    assert (attempt_dir / "agent_request.json").exists()
    assert (attempt_dir / "agent_trace.jsonl").exists()
    response = json.loads((attempt_dir / "agent_response.json").read_text(encoding="utf-8"))
    assert response["transport"]["transport"] == "runner"
    assert response["transport"]["exit_code"] == 0
    trace = [json.loads(line) for line in
             (attempt_dir / "agent_trace.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [e["kind"] for e in trace] == ["plan_request", "plan_response", "plan_accepted"]
    payload = json.loads((attempt_dir / "agent_request.json").read_text(encoding="utf-8"))
    assert payload["task"]["task_id"] == request.task_id
    assert payload["task"]["authorisation_note"].startswith("selection and target_ids")


def test_runner_working_dir_and_stdin_are_fixed_and_not_interactive(tmp_path):
    store, _ = _task(tmp_path)
    assets = _assets(tmp_path)
    script = _write_script(tmp_path, ENV_REPORTING, "env_runner.py")
    adapter = _adapter(script, tmp_path)

    outcome = run_agent_plan(adapter, store, TASK_ID, analysis=_Analysis(), assets_dir=assets)

    assert outcome.ok
    info = json.loads(outcome.agent_evidence["summary"])
    assert info["has_request"] is True            # the payload arrived on stdin
    assert info["stdin_isatty"] is False          # no interactive stdin
    assert os.path.normcase(os.path.realpath(info["cwd"])) == os.path.normcase(
        os.path.realpath(str(tmp_path))
    )


def test_the_runner_never_sees_tokens_even_when_the_env_has_them(tmp_path, monkeypatch):
    monkeypatch.setenv("LITEGARDEN_TOKEN", "s3cret-token")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-live-key")
    store, _ = _task(tmp_path)
    assets = _assets(tmp_path)
    script = _write_script(tmp_path, ENV_REPORTING, "env_runner.py")
    adapter = _adapter(script, tmp_path)

    outcome = run_agent_plan(adapter, store, TASK_ID, analysis=_Analysis(), assets_dir=assets)

    info = json.loads(outcome.agent_evidence["summary"])
    assert info["token_visible"] is False
    assert info["key_visible"] is False
    assert info["path_visible"] is True           # the allowlist still lets it run


def test_a_secret_name_may_not_be_allowlisted(tmp_path):
    script = _write_script(tmp_path, PLAN_FROM_FILE, "plan_runner.py")
    with pytest.raises(AgentConfigError, match="env_allowlist"):
        RunnerSpec.from_dict({
            "argv": [sys.executable, str(script)], "working_dir": str(tmp_path),
            "env_allowlist": ["PATH", "OPENAI_API_KEY"],
        })
    # the runtime check refuses a hand-built spec too, before anything is started
    store, request = _task(tmp_path)
    spec = RunnerSpec(argv=(sys.executable, str(script)),
                      env_allowlist=("PATH", "LITEGARDEN_TOKEN"),
                      working_dir=tmp_path)
    adapter = AgentAdapter(AgentConfig(mode="runner", runner=spec))
    with pytest.raises(AgentConfigError, match="env_allowlist"):
        adapter.generate_plan(_context(request, _assets(tmp_path)))


def test_runner_mode_requires_a_fixed_working_directory():
    with pytest.raises(AgentConfigError, match="working directory"):
        AgentAdapter(AgentConfig(mode="runner", runner=RunnerSpec(argv=(sys.executable,))))


# --------------------------------------------------------------------------
# 3. bounded run: timeout, malformed, oversized, non-zero exit, cancel
# --------------------------------------------------------------------------


def test_runner_timeout_is_structured_and_the_attempt_fails(tmp_path):
    store, _ = _task(tmp_path)
    assets = _assets(tmp_path)
    script = _write_script(tmp_path, "import time\ntime.sleep(30)\n", "sleep_runner.py")
    adapter = _adapter(script, tmp_path, timeout=0.5)

    outcome = run_agent_plan(adapter, store, TASK_ID, analysis=_Analysis(), assets_dir=assets)

    assert outcome.status == "FAILED" and outcome.plan is None
    assert outcome.error_code == AGENT_TIMEOUT
    assert "budget" in outcome.errors[0]["message"]
    record = store.read_attempt(TASK_ID, "a001")
    assert record.state == "FAILED" and record.plan is None
    assert record.errors[0]["code"] == AGENT_TIMEOUT
    assert _state(store) == "PLANNING"  # the project/task state is not advanced
    response = json.loads(
        (store.task_dir(TASK_ID) / "attempts" / "a001" / "agent_response.json")
        .read_text(encoding="utf-8")
    )
    assert response["status"] == "FAILED"


def test_runner_malformed_json_is_a_protocol_error(tmp_path):
    store, _ = _task(tmp_path)
    assets = _assets(tmp_path)
    script = _write_script(tmp_path, 'print("not json at all", flush=True)\n',
                           "broken_runner.py")
    adapter = _adapter(script, tmp_path)

    outcome = run_agent_plan(adapter, store, TASK_ID, analysis=_Analysis(), assets_dir=assets)

    assert outcome.error_code == AGENT_PROTOCOL_INVALID
    assert store.read_attempt(TASK_ID, "a001").state == "FAILED"
    detail = outcome.errors[0]["detail"]
    assert "strict JSON" in outcome.errors[0]["message"]
    assert "not json at all" in detail["body_summary"]


def test_runner_output_over_the_cap_is_refused(tmp_path):
    store, _ = _task(tmp_path)
    assets = _assets(tmp_path)
    script = _write_script(
        tmp_path, 'import sys\nsys.stdout.write("x" * (3 * 1024 * 1024))\n', "big_runner.py"
    )
    adapter = _adapter(script, tmp_path, cap=64 * 1024)

    outcome = run_agent_plan(adapter, store, TASK_ID, analysis=_Analysis(), assets_dir=assets)

    assert outcome.error_code == AGENT_OUTPUT_TOO_LARGE
    assert outcome.errors[0]["detail"]["max_output_bytes"] == 64 * 1024
    assert store.read_attempt(TASK_ID, "a001").state == "FAILED"


def test_runner_nonzero_exit_keeps_the_stderr_digest(tmp_path):
    store, _ = task_with_runner_stderr = _task(tmp_path)  # noqa: F841
    assets = _assets(tmp_path)
    script = _write_script(
        tmp_path,
        'import sys\nsys.stderr.write("boom: model unavailable")\nsys.exit(3)\n',
        "exit_runner.py",
    )
    adapter = _adapter(script, tmp_path)

    outcome = run_agent_plan(adapter, store, TASK_ID, analysis=_Analysis(), assets_dir=assets)

    assert outcome.error_code == AGENT_EXIT_NONZERO
    assert outcome.errors[0]["detail"]["exit_code"] == 3
    assert "boom: model unavailable" in outcome.errors[0]["detail"]["stderr_summary"]
    response = json.loads(
        (store.task_dir(TASK_ID) / "attempts" / "a001" / "agent_response.json")
        .read_text(encoding="utf-8")
    )
    assert "boom: model unavailable" in response["errors"][0]["detail"]["stderr_summary"]


def test_cancel_is_honoured_before_the_runner_answers(tmp_path):
    store, _ = _task(tmp_path)
    assets = _assets(tmp_path)
    script = _write_script(tmp_path, PLAN_FROM_FILE, "plan_runner.py")
    adapter = _adapter(script, tmp_path, args=[_plan_file(tmp_path, _plan(LEGAL_OPS))])

    outcome = run_agent_plan(adapter, store, TASK_ID, analysis=_Analysis(),
                             assets_dir=assets, cancel=lambda: True)

    assert outcome.error_code == AGENT_CANCELED
    assert outcome.plan is None
    assert store.read_attempt(TASK_ID, "a001").state == "FAILED"


# --------------------------------------------------------------------------
# 4. overreach: a plan outside the selection is rejected as a whole
# --------------------------------------------------------------------------


def test_plan_naming_a_reference_outside_the_selection_is_rejected_whole(tmp_path):
    store, _ = _task(tmp_path)
    assets = _assets(tmp_path)
    plan = _plan([
        LEGAL_OPS[0],
        {"id": "shrubs_1", "op": "scatter_assets", "zone_id": "plant_far",
         "asset_id": "shrub_small", "count": 3},
    ])
    script = _write_script(tmp_path, PLAN_FROM_FILE, "plan_runner.py")
    adapter = _adapter(script, tmp_path, args=[_plan_file(tmp_path, plan)])

    outcome = run_agent_plan(adapter, store, TASK_ID, analysis=_Analysis(), assets_dir=assets)

    assert outcome.status == "FAILED"
    assert outcome.error_code == AGENT_PLAN_OUT_OF_SELECTION
    assert outcome.plan is None
    refused = outcome.errors[0]["detail"]["out_of_selection_references"]
    assert [entry["id"] for entry in refused] == ["plant_far"]
    assert refused[0]["reason"] == "reference lies outside the frozen selection"
    # nothing was trimmed: the legal pavilion op must not be applied either
    record = store.read_attempt(TASK_ID, "a001")
    assert not (store.task_dir(TASK_ID) / "attempts" / "a001" / "plan.json").exists()
    assert record.state == "FAILED"
    assert record.errors[0]["code"] == AGENT_PLAN_OUT_OF_SELECTION


def test_write_box_check_refuses_an_operation_that_overflows_the_selection(tmp_path):
    store, request = _task(tmp_path)
    assets = _assets(tmp_path)
    plan = _plan([
        LEGAL_OPS[0],
        {"id": "path_1", "op": "connect_path", "from": "entry_edge",
         "to": "pavilion_1.entry", "width": 1, "palette_id": "stone_path"},
        {"id": "lights_1", "op": "decorate_path", "path_id": "path_1",
         "asset_id": "lamp_small", "spacing": 4},
    ])
    context = _context(request, assets)
    with pytest.raises(Exception) as ei:
        validate_plan(plan, context)
    error = ei.value
    assert error.code == AGENT_PLAN_OUT_OF_SELECTION
    violations = error.detail["violations"]
    assert [v["op_id"] for v in violations] == ["lights_1"]
    assert violations[0]["rule"] == "operation_write_box_outside_selection"
    assert error.detail["rejected_whole"] is True
    assert error.detail["authoritative_gate"].startswith("compiler task_authorized")


def test_unknown_reference_and_non_whitelisted_block_are_refused(tmp_path):
    _, request = _task(tmp_path)
    assets = _assets(tmp_path)
    context = _context(request, assets)

    with pytest.raises(Exception) as ei:
        validate_plan(_plan([{"id": "p1", "op": "place_asset", "asset_id": "pavilion_small",
                              "site_id": "site_zzz"}]), context)
    assert ei.value.code == AGENT_PLAN_INVALID
    assert ei.value.detail["unknown_references"][0]["id"] == "site_zzz"

    with pytest.raises(Exception) as ei2:
        validate_plan(_plan([{"id": "path_1", "op": "connect_path", "from": "entry_00",
                              "to": "pavilion_9.entry", "width": 1,
                              "palette_id": "stone_path"}]), context)
    assert ei2.value.code == AGENT_PLAN_INVALID

    with pytest.raises(Exception) as ei3:
        validate_plan(_plan([{"id": "path_1", "op": "connect_path", "from": "entry_00",
                              "to": "entry_edge", "width": 1,
                              "palette_id": "lava_path"}]), context)
    assert ei3.value.code == AGENT_PLAN_INVALID
    assert ei3.value.detail["blocks"][0]["block"] == "minecraft:lava"


def test_plan_shape_is_strict(tmp_path):
    _, request = _task(tmp_path)
    context = _context(request, _assets(tmp_path))

    with pytest.raises(Exception) as ei:
        validate_plan(_plan([{**LEGAL_OPS[0], "evil": 1}]), context)
    assert ei.value.code == AGENT_PLAN_INVALID and "unknown field" in str(ei.value)

    with pytest.raises(Exception) as ei2:
        validate_plan(_plan([{"id": "l1", "op": "decorate_path", "path_id": "nope",
                              "asset_id": "lamp_small", "spacing": 4}]), context)
    assert ei2.value.code == AGENT_PLAN_INVALID
    assert ei2.value.detail["unknown_references"][0]["kind"] == "path_id"

    with pytest.raises(Exception) as ei3:
        validate_plan(_plan([{"id": "s1", "op": "scatter_assets", "zone_id": "plant_00",
                              "asset_id": "shrub_small", "count": True}]), context)
    assert ei3.value.code == AGENT_PLAN_INVALID
    assert "booleans and floats are never coerced" in str(ei3.value)


def test_a_reference_without_a_usable_extent_is_unverifiable(tmp_path):
    """No extent means containment cannot be proven - refused, never assumed."""
    _, request = _task(tmp_path)
    assets = _assets(tmp_path)
    context = AgentContext(
        request=request, attempt_id="a001",
        sites={"site_blind": {"id": "site_blind"}},   # no origin/footprint
        zones={}, anchors={"entry_00": (3, 3)}, assets_dir=assets,
    )
    with pytest.raises(Exception) as ei:
        validate_plan(_plan([{"id": "p1", "op": "place_asset",
                              "asset_id": "pavilion_small", "site_id": "site_blind"}]),
                      context)
    assert ei.value.code == AGENT_PLAN_UNVERIFIABLE
    assert ei.value.detail["unverifiable_references"][0]["id"] == "site_blind"
    assert "no extent" in ei.value.detail["unverifiable_references"][0]["reason"]


def test_a_failed_attempt_writes_nothing_outside_the_task_directory(tmp_path):
    store, _ = _task(tmp_path)
    assets = _assets(tmp_path)
    script = _write_script(tmp_path, "import time\ntime.sleep(30)\n", "sleep_runner.py")
    adapter = _adapter(script, tmp_path, timeout=0.5)
    before = sorted(p.name for p in (tmp_path / "project").iterdir())

    run_agent_plan(adapter, store, TASK_ID, analysis=_Analysis(), assets_dir=assets)

    assert sorted(p.name for p in (tmp_path / "project").iterdir()) == before


# --------------------------------------------------------------------------
# 5. review_evidence: read-only, auditable, never a fake pass
# --------------------------------------------------------------------------


def _review_tools(calls):
    def voxels(params):
        calls.append(("inspect_voxels", params))
        return {"bounds": params["bounds"], "voxels": {"minecraft:stone": 12}}

    def counts(params):
        calls.append(("block_counts", params))
        return {"minecraft:stone": 12}

    return ReadonlyToolRegistry((
        ReadonlyTool("inspect_voxels", "voxels in a box", {"bounds": "box"}, voxels),
        ReadonlyTool("block_counts", "histogram in a box", {"bounds": "box"}, counts),
        ReadonlyTool("scene_summary", "summary", {}, lambda params: {"counts": {"voxels": 9}}),
    ))


def _review_run(tmp_path, envelope, *, calls=None, max_evidence=8, evidence=None,
                tools=None, case=None):
    base = tmp_path / case if case else tmp_path
    base.mkdir(parents=True, exist_ok=True)
    store, request = _task(base)
    store.create_attempt(TASK_ID)
    script = _write_script(base, REVIEW_FROM_FILE, "review_runner.py")
    adapter = _adapter(
        script, base, tools=tools or _review_tools(calls if calls is not None else []),
        max_evidence=max_evidence, args=[_envelope_file(base, envelope)],
    )
    outcome = run_agent_review(adapter, store, TASK_ID, "a001",
                               evidence=evidence or _evidence())
    return store, adapter, outcome


def test_review_answers_only_read_only_queries_and_records_everything(tmp_path):
    calls = []
    box = {"min": [2, 0, 2], "max_exclusive": [4, 2, 4]}
    envelope = _review_envelope(
        findings=[{
            "id": "finding_01", "code": "ENTRY_BLOCKED", "op_id": "pavilion_1",
            "bounds_local": box, "observation": "入口下部似乎有占位方块",
            "evidence_image_ids": ["entry_pavilion_1"], "requested_check": "check_entry",
        }],
        evidence_requests=[
            {"tool": "inspect_voxels", "params": {"bounds": box}},
            {"tool": "block_counts", "params": {"bounds": box}},
        ],
    )
    store, _, outcome = _review_run(tmp_path, envelope, calls=calls)

    assert outcome.ok and outcome.status == "REVIEWED"
    assert outcome.review_executed is True and outcome.verdict == "suspected_issue"
    assert outcome.evidence_ok is True
    assert [c[0] for c in calls] == ["inspect_voxels", "block_counts"]
    assert outcome.evidence_results[0]["result"]["voxels"]["minecraft:stone"] == 12
    assert outcome.evidence_results[0]["result_sha256"]
    assert outcome.findings[0]["bounds_local"] == box
    kinds = [e["kind"] for e in outcome.events]
    assert kinds[0] == "review_request" and kinds[-1] == "evidence_answered"
    assert "evidence_answered" in kinds

    record = store.read_attempt(TASK_ID, "a001")
    assert record.review["review_executed"] is True
    assert record.review["verdict"] == "suspected_issue"
    assert record.review["evidence_sha256"]
    assert [r["verdict"] for r in record.review["rounds"]] == ["suspected_issue"]
    assert record.review["findings"][0]["code"] == "ENTRY_BLOCKED"
    assert "confirmation" in record.review["note"]


def test_review_write_request_is_refused_before_anything_is_executed(tmp_path):
    calls = []
    box = {"min": [2, 0, 2], "max_exclusive": [4, 2, 4]}
    envelope = _review_envelope(evidence_requests=[
        {"tool": "inspect_voxels", "params": {"bounds": box}},
        {"tool": "apply_patch", "params": {"op": "place_asset"}},
    ])
    store, _, outcome = _review_run(tmp_path, envelope, calls=calls)

    assert outcome.status == "FAILED"
    assert outcome.error_code == AGENT_REVIEW_NOT_READONLY
    assert outcome.review_executed is False
    assert calls == [] and outcome.evidence_results == []
    record = store.read_attempt(TASK_ID, "a001")
    assert record.review["review_executed"] is False
    assert record.review["verdict"] is None
    assert "not a read-only query" in record.review["errors"][0]["message"]


def test_undeclared_tool_and_path_like_values_are_refused(tmp_path):
    box = {"min": [2, 0, 2], "max_exclusive": [4, 2, 4]}
    calls = []
    _, _, outcome = _review_run(
        tmp_path,
        _review_envelope(evidence_requests=[{"tool": "mystery_query", "params": {}}]),
        calls=calls,
    )
    assert outcome.error_code == AGENT_REVIEW_INVALID
    assert outcome.errors[0]["detail"]["declared"]

    _, _, outcome2 = _review_run(
        tmp_path,
        _review_envelope(evidence_requests=[
            {"tool": "inspect_voxels", "params": {"bounds": box, "path": "C:\\secrets.txt"}}
        ]),
        calls=calls, case="badpath",
    )
    assert outcome2.error_code == AGENT_REVIEW_NOT_READONLY
    assert calls == []

    _, _, outcome3 = _review_run(
        tmp_path,
        _review_envelope(evidence_requests=[
            {"tool": "scene_summary", "params": {}},
        ], findings=[{"id": "f1", "code": "ENTRY_BLOCKED",
                      "observation": "x", "object_id": "/etc/passwd"}]),
        calls=calls, case="legalcase",
    )
    # object_id is plain text, not a path parameter; the request itself is legal
    assert outcome3.ok


def test_aesthetic_finding_codes_are_refused(tmp_path):
    calls = []
    envelope = _review_envelope(findings=[{
        "id": "f1", "code": "NOT_PRETTY", "observation": "这个亭子不够宏伟",
    }])
    store, _, outcome = _review_run(tmp_path, envelope, calls=calls)

    assert outcome.error_code == AGENT_PROTOCOL_INVALID
    assert "ENTRY_BLOCKED" in outcome.errors[0]["detail"]["allowed_codes"]
    assert outcome.review_executed is False
    assert store.read_attempt(TASK_ID, "a001").review["review_executed"] is False


def test_review_rejects_unknown_verdict_a_carried_plan_and_a_stale_binding(tmp_path):
    _, _, outcome = _review_run(tmp_path, _review_envelope(verdict="approved_by_model"))
    assert outcome.error_code == AGENT_PROTOCOL_INVALID
    assert "approved_by_model" in outcome.errors[0]["message"]

    _, _, outcome2 = _review_run(
        tmp_path, {**_review_envelope(), "plan": {"operations": []}}, case="carried"
    )
    assert outcome2.error_code == AGENT_PROTOCOL_INVALID
    assert "plan" in outcome2.errors[0]["message"]

    _, _, outcome3 = _review_run(tmp_path, _review_envelope(candidate_id="c999"),
                                 case="stale")
    assert outcome3.error_code == AGENT_STALE_EVIDENCE
    assert outcome3.errors[0]["detail"]["expected"] == "c004"


def test_review_evidence_budget_is_bounded(tmp_path):
    calls = []
    box = {"min": [2, 0, 2], "max_exclusive": [4, 2, 4]}
    envelope = _review_envelope(evidence_requests=[
        {"tool": "inspect_voxels", "params": {"bounds": box}} for _ in range(9)
    ])
    _, _, outcome = _review_run(tmp_path, envelope, calls=calls, max_evidence=8)

    assert outcome.error_code == AGENT_BUDGET_EXCEEDED
    assert outcome.errors[0]["detail"]["requested"] == 9
    assert calls == []


def test_a_failing_read_only_tool_is_reported_and_not_a_pass(tmp_path):
    def broken(params):
        raise RuntimeError("scene cache is cold")

    tools = ReadonlyToolRegistry((
        ReadonlyTool("inspect_voxels", "voxels in a box", {"bounds": "box"}, broken),
    ))
    box = {"min": [2, 0, 2], "max_exclusive": [4, 2, 4]}
    envelope = _review_envelope(verdict="no_issue_observed", findings=[],
                                evidence_requests=[{"tool": "inspect_voxels",
                                                    "params": {"bounds": box}}])
    _, _, outcome = _review_run(tmp_path, envelope, tools=tools)

    assert outcome.ok and outcome.verdict == "no_issue_observed"
    assert outcome.evidence_ok is False
    assert outcome.evidence_results[0]["error"]["code"] == "AGENT_TOOL_FAILED"
    assert any("must not be treated as a passed review" in note for note in outcome.notes)


def test_a_tool_name_that_reads_like_a_write_cannot_even_be_declared():
    with pytest.raises(AgentConfigError, match="reads like a mutation"):
        ReadonlyToolRegistry((ReadonlyTool("export_candidate", "nope", {}, None),))


# --------------------------------------------------------------------------
# 6. provider mode shares the same contract; payload shape
# --------------------------------------------------------------------------


def test_provider_mode_posts_the_same_envelope_and_is_validated_the_same(tmp_path):
    store, request = _task(tmp_path)
    assets = _assets(tmp_path)
    captured = {}

    def fake_provider(spec, payload, *, cancel=None):
        from litegarden.agent_adapter import TransportResult
        captured["url"] = spec.url
        captured["payload"] = json.loads(payload.decode("utf-8"))
        envelope = {
            "schema_version": "0.2", "request_kind": "generate_plan",
            "task_id": TASK_ID, "attempt_id": "a001",
            "plan": _plan([LEGAL_OPS[0]]),
            "agent_evidence": {"summary": "provider plan"},
        }
        return TransportResult(transport="provider",
                               payload_text=json.dumps(envelope), endpoint=spec.url)

    config = AgentConfig(mode="provider",
                         provider=ProviderSpec(url="http://127.0.0.1:9/agent"))
    adapter = AgentAdapter(config, provider_call=fake_provider)
    outcome = run_agent_plan(adapter, store, TASK_ID, analysis=_Analysis(), assets_dir=assets)

    assert outcome.ok
    assert captured["url"] == "http://127.0.0.1:9/agent"
    assert captured["payload"]["request_kind"] == "generate_plan"
    assert captured["payload"]["selection"]["max_exclusive"] == [16, 8, 16]
    assert outcome.transport["transport"] == "provider"


def test_an_unreachable_provider_is_a_structured_transport_failure(tmp_path):
    store, _ = _task(tmp_path)
    assets = _assets(tmp_path)
    # loopback port 1 is closed: no external network is touched.  Windows may
    # report either "refused" or a connect timeout, and both are structured.
    config = AgentConfig(mode="provider", provider=ProviderSpec(
        url="http://127.0.0.1:1/agent", timeout_seconds=1.0))
    adapter = AgentAdapter(config)

    outcome = run_agent_plan(adapter, store, TASK_ID, analysis=_Analysis(), assets_dir=assets)

    assert outcome.error_code in (AGENT_UNAVAILABLE, AGENT_TIMEOUT)
    assert store.read_attempt(TASK_ID, "a001").state == "FAILED"


def test_payload_declares_the_coordinate_convention_tools_and_contract(tmp_path):
    _, request = _task(tmp_path)
    assets = _assets(tmp_path)
    adapter = AgentAdapter.from_config(None)
    payload = build_plan_payload(adapter, _context(request, assets))

    convention = payload["coordinate_convention"]
    assert convention["agent_uses"] == "p_local only"
    assert convention["bounds"] == "half-open [min, max_exclusive)"
    assert "context_halo" in convention["read_only"]
    assert payload["selection"]["min"] == [0, 0, 0]
    assert payload["context_halo"]["read_only"] is True
    assert payload["brief"]["instruction"] == "放一个亭子"
    assert payload["budget"]["timeout_seconds"] > 0
    assert payload["readonly_tools"] and all(
        t["writes"] is False for t in payload["readonly_tools"]
    )
    inventory = payload["reference_inventory"]
    assert inventory["usable"]["sites"] == ["site_00"]
    assert "site_far" in [entry["id"] for entry in inventory["refused"]]
    assert inventory["block_whitelist"] == "active"
    contract = payload["response_contract"]
    assert contract["review_evidence"]["verdicts"] == [
        "no_issue_observed", "suspected_issue", "insufficient_evidence", "render_issue"
    ]
    assert "ENTRY_BLOCKED" in contract["review_evidence"]["findings"]["codes"]
    assert contract["review_evidence"]["must_not_carry_a_plan"] is True
    assert "no widening of the selection" in " ".join(payload["agent_restrictions"])


def test_review_payload_binds_the_evidence_hash_and_hides_nothing(tmp_path):
    _, request = _task(tmp_path)
    assets = _assets(tmp_path)
    adapter = AgentAdapter.from_config(None)
    payload = build_review_payload(adapter, _context(request, assets), _evidence())

    assert payload["request_kind"] == "review_evidence"
    assert payload["evidence"]["candidate"]["candidate_id"] == "c004"
    assert len(payload["evidence_sha256"]) == 64
    assert "return a plan here" in " ".join(payload["review_instructions"]["must_not"])


# --------------------------------------------------------------------------
# 7. configuration loading
# --------------------------------------------------------------------------


def test_config_loading_is_explicit_and_refuses_half_configurations(tmp_path):
    assert AgentConfig.load(env={}).mode == "none"
    with pytest.raises(AgentConfigError, match="does not exist"):
        AgentConfig.load(tmp_path / "missing-agent.json")
    with pytest.raises(AgentConfigError, match="needs a runner object"):
        AgentConfig.from_dict({"mode": "runner"})
    with pytest.raises(AgentConfigError, match="remove runner/provider"):
        AgentConfig.from_dict({"mode": "none", "runner": {"argv": ["python"]}})
    with pytest.raises(AgentConfigError, match="http"):
        ProviderSpec.from_dict({"url": "file:///etc/passwd"})
    with pytest.raises(AgentConfigError, match="unknown field"):
        AgentConfig.from_dict({"mode": "none", "extra": 1})

    cfg = AgentConfig.from_dict({
        "mode": "runner",
        "runner": {"argv": [sys.executable, "-c", "pass"], "working_dir": str(tmp_path)},
        "max_evidence_requests": 4,
    })
    assert cfg.available and cfg.max_evidence_requests == 4
    assert "argv" in cfg.to_dict()["runner"]
    # header values are never echoed back in a descriptor
    provider = ProviderSpec(url="http://127.0.0.1:9/x", headers={"Authorization": "Bearer k"})
    assert provider.to_dict()["header_names"] == ["Authorization"]
    assert "Bearer" not in json.dumps(provider.to_dict())
