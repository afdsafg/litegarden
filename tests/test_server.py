"""P4: the local HTTP service layer (spec 17.2 / 19.2).

Covers the guarantees the workbench depends on: the service is loopback-only,
every mutating request needs the run token and a loopback Origin, static files
cannot escape their roots, unknown routes are 404, RenderScene payloads pass the
strict validator, a bad selection is a structured 4xx (never a 500), accepting a
candidate goes through the project store's CAS (409 on a stale HEAD) and
undo/redo move the linear history through the same API.

The fixtures build small synthetic projects with the helpers the other test
modules already use, so nothing here needs the real terrain file or the network.
"""
from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path

import pytest

from litegarden.project_store import ProjectStore
from litegarden.render_scene import validate_render_scene
from litegarden.server.app import ServerConfig, build_server

from .test_operations import _flat_scene, _write_assets
from .test_permissions import add_block_rules


REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = REPO_ROOT / "web"
WORK_DIR = REPO_ROOT / "work"
RUN_TOKEN = "test-run-token"
MISSING = object()

PROJECT_ID = "proj"
PLAN = {
    "schema_version": "0.1",
    "scene_id": "t",
    "seed": 7,
    "style_id": "rustic",
    "operations": [
        {"id": "pavilion_1", "op": "place_asset", "asset_id": "pavilion_small",
         "site_id": "site_00", "variant": "north"},
    ],
}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


class Client:
    """A tiny HTTP client that always sends an explicit Host and token header."""

    def __init__(self, port: int, token: str = RUN_TOKEN):
        self.host = "127.0.0.1"
        self.port = port
        self.token = token

    def request(self, method, path, body=MISSING, *, headers=None, token=MISSING):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=120)
        head = {"Host": f"{self.host}:{self.port}"}
        use = self.token if token is MISSING else token
        if use is not None:
            head["X-Litegarden-Token"] = use
        head.update(headers or {})
        payload = None
        if body is not MISSING:
            payload = json.dumps(body).encode("utf-8")
            head["Content-Type"] = "application/json"
        try:
            conn.request(method, path, body=payload, headers=head)
            response = conn.getresponse()
            raw = response.read()
            status = response.status
        finally:
            conn.close()
        text = raw.decode("utf-8", "replace")
        try:
            data = json.loads(text) if text else None
        except ValueError:
            data = text
        return status, data

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.request("POST", path, body if body is not None else {}, **kw)


class Server:
    """A running service plus the artefacts the assertions need."""

    def __init__(self, project_dir, store, assets, config, httpd):
        self.project_dir = project_dir
        self.store = store
        self.assets = assets
        self.config = config
        self.httpd = httpd
        self.client = Client(httpd.server_address[1], config.token)
        self.origin = f"http://127.0.0.1:{httpd.server_address[1]}"

    def head(self):
        return json.loads((self.project_dir / "HEAD.json").read_text(encoding="utf-8"))

    def revisions(self):
        return sorted(p.name for p in (self.project_dir / "revisions").iterdir()
                      if p.is_dir() and not p.name.startswith("."))


def _start(project_dir, store, assets, *, token=RUN_TOKEN, config=None):
    cfg = ServerConfig(
        project_dir=project_dir, web_dir=WEB_DIR, work_dir=WORK_DIR,
        assets_dir=assets, port=0, token=token,
    )
    if config:
        for key, value in config.items():
            setattr(cfg, key, value)
    httpd = build_server(cfg)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    server = Server(project_dir, store, assets, cfg, httpd)
    server.thread = thread
    return server


def _stop(server):
    server.httpd.shutdown()
    server.httpd.server_close()
    server.thread.join(timeout=15)


def _make_project(tmp_path, *, size=(24, 8, 24), config=None, name="proj"):
    src = _flat_scene(tmp_path, size=size)
    assets = _write_assets(tmp_path)
    add_block_rules(assets)
    store = ProjectStore.create(tmp_path / name, src, game_version="1.20.1",
                                config=config or {})
    return store, assets


def _full_selection(size=(24, 8, 24)):
    return {"min": [0, 0, 0], "max_exclusive": list(size)}


@pytest.fixture
def served(tmp_path):
    store, assets = _make_project(tmp_path)
    server = _start(tmp_path / "proj", store, assets)
    try:
        yield server
    finally:
        _stop(server)


@pytest.fixture
def served_with_object(tmp_path):
    """A project whose r001 registers an object that straddles a selection."""
    store, assets = _make_project(tmp_path)
    scene = store.load_head_scene()
    edited = tmp_path / "v1.litematic"
    from litemapy import BlockState

    pos_region = scene.snapshot.transform.local_to_region((1, 0, 1))
    scene.region[pos_region] = BlockState("minecraft:stone_bricks")
    from litegarden.io import save_scene

    save_scene(scene, str(edited))
    objects = {
        "schema_version": "0.2",
        "count": 1,
        "objects": [
            {
                "object_id": "pavilion_1@r001",
                "kind": "asset",
                "creation_revision": "r001",
                "occupied_voxels": [[1, 0, 1], [22, 0, 22]],
                "complete": True,
            }
        ],
    }
    store.commit_revision(
        scene_path=edited,
        patch=[{"region_id": "main", "pos_local": [1, 0, 1],
                "before": "minecraft:air", "after": "minecraft:stone_bricks",
                "op_id": "pavilion_1"}],
        objects=objects,
        expected_head="r000",
        expected_generation=0,
        task_id="task_0001",
    )
    server = _start(tmp_path / "proj", store, assets)
    try:
        yield server
    finally:
        _stop(server)


def _candidate_id(server) -> str:
    """Freeze a task and submit the shared plan; returns the candidate id."""
    status, report = server.client.post(
        f"/api/projects/{PROJECT_ID}/selections/validate",
        {"selection": _full_selection()},
    )
    assert status == 200, report
    assert report["blocked"] is False
    status, task = server.client.post(
        f"/api/projects/{PROJECT_ID}/tasks",
        {"selection": _full_selection(), "instruction": "place a pavilion",
         "mode": "revise_current"},
    )
    assert status == 201, task
    status, attempt = server.client.post(
        f"/api/tasks/{task['task_id']}/attempts", {"plan": PLAN},
    )
    assert status == 201, attempt
    return attempt["candidate_id"], task["task_id"]


# --------------------------------------------------------------------------
# loopback / Origin / token
# --------------------------------------------------------------------------


def test_non_loopback_host_is_refused(tmp_path):
    store, assets = _make_project(tmp_path)
    cfg = ServerConfig(project_dir=tmp_path / "proj", web_dir=WEB_DIR,
                       assets_dir=assets, host="0.0.0.0", port=0)
    with pytest.raises(ValueError, match="loopback-only"):
        build_server(cfg)


def test_service_binds_loopback_only(served):
    assert served.httpd.server_address[0] == "127.0.0.1"


def test_get_with_a_foreign_host_header_is_refused(served):
    status, payload = served.client.get("/api/session", headers={"Host": "evil.example"})
    assert status == 403
    assert payload["error"]["code"] == "BAD_HOST"


def test_get_with_a_cross_site_origin_is_refused(served):
    status, payload = served.client.get(
        "/api/session", headers={"Origin": "https://evil.example"})
    assert status == 403
    assert payload["error"]["code"] == "FORBIDDEN_ORIGIN"
    assert payload["error"]["message"] == (
        "Origin 'https://evil.example' is not the served loopback origin")


def test_mutating_request_needs_the_run_token(served):
    path = f"/api/projects/{PROJECT_ID}/undo"
    body = {"expected_head": "r000", "expected_generation": 0}
    status, payload = served.client.post(path, body, token=None)
    assert status == 403
    assert payload["error"]["code"] == "FORBIDDEN_TOKEN"
    status, payload = served.client.post(path, body, token="not-the-token")
    assert status == 403
    assert payload["error"]["code"] == "FORBIDDEN_TOKEN"
    # ... and the token does not override a cross-site Origin
    status, payload = served.client.post(
        path, body, headers={"Origin": "http://evil.example"})
    assert status == 403
    assert payload["error"]["code"] == "FORBIDDEN_ORIGIN"


def test_same_origin_request_with_the_token_is_accepted(served):
    status, payload = served.client.post(
        f"/api/projects/{PROJECT_ID}/selections/validate",
        {"selection": _full_selection()},
        headers={"Origin": served.origin},
    )
    assert status == 200, payload
    assert payload["report"]["requires_user_decision"] is False


def test_token_is_published_by_the_session_route(served):
    status, payload = served.client.get("/api/session", token=None)
    assert status == 200
    assert payload["token"] == RUN_TOKEN
    assert payload["token_required"] is True
    assert payload["token_header"] == "X-Litegarden-Token"
    assert payload["server"]["loopback_only"] is True
    assert payload["capabilities"]["candidate_accept"] is True
    assert payload["capabilities"]["agent_runner"] is False


def test_token_can_be_disabled_explicitly(tmp_path):
    store, assets = _make_project(tmp_path)
    server = _start(tmp_path / "proj", store, assets, token="")
    try:
        status, payload = server.client.post(
            f"/api/projects/{PROJECT_ID}/selections/validate",
            {"selection": _full_selection()}, token=None,
        )
        assert status == 200, payload
        status, payload = server.client.get("/api/session", token=None)
        assert payload["token"] == "" and payload["token_required"] is False
    finally:
        _stop(server)


# --------------------------------------------------------------------------
# static files, traversal, unknown routes
# --------------------------------------------------------------------------


def test_index_and_static_files_are_served(served):
    status, body = served.client.get("/", token=None)
    assert status == 200
    assert isinstance(body, str) and "<html" in body.lower()
    status, body = served.client.get("/web/app.js", token=None)
    assert status == 200 and isinstance(body, str) and "fetchJson" in body
    # the same page also works when the workbench is opened at the bare root
    status, body = served.client.get("/app.js", token=None)
    assert status == 200 and isinstance(body, str) and "fetchJson" in body
    status, body = served.client.get("/styles.css", token=None)
    assert status == 200 and isinstance(body, str) and len(body) > 1000
    status, body = served.client.get("/AGENTS.md", token=None)
    assert status == 404  # only web_dir is aliased at the root
    status, payload = served.client.get("/work/sample_render_scene.json", token=None)
    assert status == 200 and payload["coordinate_space"] == "project_local"


def test_path_traversal_is_refused(served):
    attempts = [
        "/web/%2e%2e/pyproject.toml",
        "/web/..%2f..%2fpyproject.toml",
        "/web/..%5c..%5cpyproject.toml",
        "/work/%2e%2e/AGENTS.md",
        "/work/....//pyproject.toml",
    ]
    for path in attempts:
        status, payload = served.client.get(path, token=None)
        assert status in (403, 404), (path, status, payload)
        assert "tool.pytest" not in json.dumps(payload), path
    status, payload = served.client.get("/web/%2e%2e/pyproject.toml", token=None)
    assert status == 403
    assert payload["error"]["code"] == "PATH_ESCAPE"


def test_unknown_routes_are_404(served):
    for path in ("/api/nope", "/api/projects/proj/bogus",
                 "/api/candidates/x-a001/diff", "/web/nope.js",
                 "/api/projects/other"):
        status, payload = served.client.get(path, token=None)
        assert status == 404, (path, status)
        assert payload["error"]["code"] in (
            "UNKNOWN_ROUTE", "NOT_FOUND", "UNKNOWN_PROJECT",
            "INVALID_CANDIDATE_ID", "UNKNOWN_CANDIDATE",
        ), (path, payload)
    status, payload = served.client.post(
        f"/api/projects/{PROJECT_ID}/nope", {})
    assert status == 404 and payload["error"]["code"] == "UNKNOWN_ROUTE"


def test_write_methods_on_static_paths_are_refused(served):
    status, payload = served.client.post("/web/index.html", {})
    assert status == 405
    assert payload["error"]["code"] == "METHOD_NOT_ALLOWED"


# --------------------------------------------------------------------------
# project detail / render scene
# --------------------------------------------------------------------------


def test_project_detail_reports_head_config_and_panels(served):
    status, payload = served.client.get(f"/api/projects/{PROJECT_ID}", token=None)
    assert status == 200
    assert payload["project_id"] == PROJECT_ID
    assert payload["head_revision"] == "r000" and payload["head_generation"] == 0
    assert payload["revision_ids"] == ["r000"]
    assert payload["can_undo"] is False and payload["can_redo"] is False
    assert payload["config"] == {}
    # the four keys web/app.js reads for ?panels=
    assert payload["protected_areas"] == []
    assert payload["assets"] == []
    assert [row["revision_id"] for row in payload["history"]] == ["r000"]
    assert payload["issues"] == []
    assert payload["diagnostics"]["errors"] == 0
    status, alias = served.client.get("/api/project", token=None)
    assert status == 200 and alias["head_revision"] == "r000"


def test_project_detail_lists_protected_zones_and_objects(served_with_object):
    server = served_with_object
    status, payload = server.client.get(f"/api/projects/{PROJECT_ID}", token=None)
    assert status == 200
    assert payload["head_revision"] == "r001"
    assert [row["revision_id"] for row in payload["revisions"]] == ["r000", "r001"]
    assets = payload["assets"]
    assert [row["object_id"] for row in assets] == ["pavilion_1@r001"]
    assert assets[0]["bounds"] == {"min": [1, 0, 1], "max_exclusive": [23, 1, 23]}
    assert assets[0]["status"] == "registered"
    assert payload["objects"]["count"] == 1


def test_render_scene_payload_is_valid_and_croppable(served):
    status, payload = served.client.get(
        f"/api/projects/{PROJECT_ID}/revisions/r000/render-scene", token=None)
    assert status == 200
    validate_render_scene(payload)  # the strict protocol validator
    assert payload["scene_id"] == "r000"
    assert payload["coordinate_space"] == "project_local"
    assert payload["size"] == [24, 8, 24]
    assert payload["crop_origin_local"] == [0, 0, 0]
    assert payload["full_scene_bounds"] == {"min": [0, 0, 0], "max_exclusive": [24, 8, 24]}
    assert len(payload["file_sha256"]) == 64
    status, crop = served.client.get(
        f"/api/projects/{PROJECT_ID}/revisions/r000/render-scene"
        "?crop_origin=1,0,1&crop_size=4,4,4", token=None)
    assert status == 200
    validate_render_scene(crop)
    assert crop["crop_origin_local"] == [1, 0, 1] and crop["size"] == [4, 4, 4]
    status, summary = served.client.get(
        f"/api/projects/{PROJECT_ID}/revisions/r000/render-scene?summary=1", token=None)
    assert status == 200 and "idx" not in summary and summary["scene_id"] == "r000"


def test_render_scene_refuses_a_crop_outside_the_scene(served):
    status, payload = served.client.get(
        f"/api/projects/{PROJECT_ID}/revisions/r000/render-scene?crop_size=99,99,99",
        token=None)
    assert status == 400
    assert payload["error"]["code"] == "INDEX_OUT_OF_RANGE"
    status, payload = served.client.get(
        f"/api/projects/{PROJECT_ID}/revisions/r000/render-scene?crop_size=nope",
        token=None)
    assert status == 400 and payload["error"]["code"] == "INVALID_QUERY"


def test_unknown_revision_is_404_and_incomplete_revision_is_500(served, tmp_path):
    status, payload = served.client.get(
        f"/api/projects/{PROJECT_ID}/revisions/r999/render-scene", token=None)
    assert status == 404 and payload["error"]["code"] == "UNKNOWN_REVISION"

    # A revision whose artefacts are gone is a broken store, not a client error.
    (served.project_dir / "revisions" / "r000" / "patch.json").unlink()
    status, payload = served.client.get(
        f"/api/projects/{PROJECT_ID}/revisions/r000/render-scene", token=None)
    assert status == 500
    assert payload["error"]["code"] == "INCOMPLETE_REVISION"
    assert payload["error"]["detail"]["revision_id"] == "r000"

def test_render_scene_declares_the_resource_set_it_was_built_with(served):
    path = f"/api/projects/{PROJECT_ID}/revisions/r000/render-scene"
    status, payload = served.client.get(path, token=None)
    assert status == 200
    if not (WEB_DIR / "vendor" / "litematica-viewer").is_dir():
        assert payload["resource_hash"] is None
        return
    digest = payload["resource_hash"]
    assert isinstance(digest, str) and len(digest) == 64
    status, again = served.client.get(path, token=None)
    assert again["resource_hash"] == digest  # deterministic, not per-request
    status, crop = served.client.get(path + "?crop_size=2,2,2", token=None)
    assert crop["resource_hash"] == digest


# --------------------------------------------------------------------------
# selection validation
# --------------------------------------------------------------------------


def test_selection_validation_rejects_bad_input_with_structured_errors(served):
    path = f"/api/projects/{PROJECT_ID}/selections/validate"
    status, payload = served.client.post(path, {})
    assert status == 400 and payload["error"]["code"] == "MISSING_REQUIRED_FIELD"

    status, payload = served.client.post(
        path, {"selection": {"min": [0, 0, 0], "max": [4, 4, 4]}})
    assert status == 400
    assert payload["error"]["code"] == "LEGACY_BOUNDS_NEEDS_MIGRATION"

    status, payload = served.client.post(
        path, {"selection": {"min": [20, 0, 20], "max_exclusive": [40, 8, 40]}})
    assert status == 400 and payload["error"]["code"] == "SELECTION_OUT_OF_BOUNDS"

    status, payload = served.client.post(
        path, {"selection": {"min": [0, 0, 0], "max_exclusive": [4.5, 4, 4]}})
    assert status == 400 and payload["error"]["code"] == "INVALID_SELECTION"
    assert "detail" in payload["error"]

    # A legacy box is only converted when the caller asks for it explicitly.
    status, payload = served.client.post(path, {
        "selection": {"min": [0, 0, 0], "max": [3, 3, 3]},
        "allow_legacy_migration": True,
    })
    assert status == 200, payload
    assert payload["report"]["selection"]["max_exclusive"] == [4, 4, 4]


def test_selection_validation_reports_the_halo_and_protected_overlap(tmp_path):
    store, assets = _make_project(tmp_path, config={
        "protected_zones": [
            {"id": "tree", "min": [5, 0, 5], "max_exclusive": [8, 6, 8]}
        ],
        "anchors": {"entry": [22, 22]},
    })
    server = _start(tmp_path / "proj", store, assets)
    try:
        status, payload = server.client.post(
            f"/api/projects/{PROJECT_ID}/selections/validate",
            {"selection": {"min": [4, 0, 4], "max_exclusive": [12, 8, 12]},
             "context_halo_xz": 3},
        )
        assert status == 200, payload
        report = payload["report"]
        assert report["halo"]["halo_xz"] == 3
        assert report["halo"]["box"] == {"min": [1, 0, 1], "max_exclusive": [15, 8, 15]}
        assert report["halo"]["read_only"] is True
        assert [box["min"] for box in report["protected_overlap"]] == [[5, 0, 5]]
        assert report["anchor_inside"] == [] and report["anchor_pending"] == ["entry"]
    finally:
        _stop(server)


def test_a_partial_object_blocks_the_freeze_with_a_structured_error(served_with_object):
    server = served_with_object
    partial = {"min": [0, 0, 0], "max_exclusive": [10, 8, 10]}
    status, payload = server.client.post(
        f"/api/projects/{PROJECT_ID}/selections/validate", {"selection": partial})
    assert status == 200, payload
    report = payload["report"]
    assert payload["requires_user_decision"] is True
    assert report["partial_objects"][0]["object_id"] == "pavilion_1@r001"
    assert report["partial_objects"][0]["inside_voxels"] == 1
    assert report["partial_objects"][0]["total_voxels"] == 2

    status, payload = server.client.post(
        f"/api/projects/{PROJECT_ID}/tasks",
        {"selection": partial, "instruction": "rebuild the pavilion"},
    )
    assert status == 409
    assert payload["error"]["code"] == "PARTIAL_OBJECT_IN_SELECTION"
    assert payload["error"]["detail"]["partial_objects"][0]["object_id"] == "pavilion_1@r001"
    assert list((server.project_dir / "tasks").iterdir()) == []


def test_replace_generated_needs_verifiable_targets(served):
    status, payload = served.client.post(
        f"/api/projects/{PROJECT_ID}/tasks",
        {"selection": _full_selection(), "instruction": "replace it",
         "mode": "replace_generated", "target_ids": ["ghost@r001"]},
    )
    assert status == 409
    assert payload["error"]["code"] == "TARGET_OWNERSHIP_CONFLICT"


def test_task_creation_freezes_the_request_and_reports_a_stale_base(served):
    status, payload = served.client.post(
        f"/api/projects/{PROJECT_ID}/tasks",
        {"selection": _full_selection(), "instruction": "place a pavilion"},
    )
    assert status == 201, payload
    task_id = payload["task_id"]
    assert task_id == "task_001" and payload["state"] == "CREATED"
    request = payload["request"]
    assert request["base_revision_id"] == "r000" and request["head_generation"] == 0
    assert request["selection"]["max_exclusive"] == [24, 8, 24]
    assert "frozen by the server" in request["authorisation_note"]

    status, detail = served.client.get(f"/api/tasks/{task_id}", token=None)
    assert status == 200
    assert detail["state"] == "CREATED" and detail["attempts"] == []
    assert detail["fresh"] is True and detail["stale"] is None
    assert detail["context"]["selection_report"]["requires_user_decision"] is False

    status, payload = served.client.post(
        f"/api/projects/{PROJECT_ID}/redesign-tasks",
        {"selection": _full_selection(), "instruction": "again",
         "base_revision": "r099"},
    )
    assert status == 409 and payload["error"]["code"] == "STALE_BASE_REVISION"

    status, payload = served.client.post(
        f"/api/projects/{PROJECT_ID}/tasks",
        {"selection": _full_selection(), "instruction": "   "},
    )
    assert status == 409 and payload["error"]["code"] == "MISSING_REQUIRED_FIELD"


# --------------------------------------------------------------------------
# attempts, diff, accept, undo/redo
# --------------------------------------------------------------------------


def test_attempt_compiles_a_candidate_with_a_net_diff(served):
    candidate_id, task_id = _candidate_id(served)
    assert candidate_id == "task_001-a001"

    status, detail = served.client.get(f"/api/tasks/{task_id}", token=None)
    assert status == 200
    assert detail["state"] == "READY_FOR_USER"
    attempt = detail["attempts"][0]
    assert attempt["attempt_id"] == "a001" and attempt["state"] == "READY_FOR_USER"
    assert attempt["candidate_id"] == candidate_id
    assert attempt["candidate"]["accepted"] is False
    assert attempt["candidate"]["status"] == "READY_FOR_USER"
    assert attempt["candidate"]["verified"]["changed"] == attempt["candidate"]["changes"]
    assert attempt["urls"]["diff"] == f"/api/candidates/{candidate_id}/diff"
    assert attempt["errors"] == []

    status, attempt_detail = served.client.get(
        f"/api/tasks/{task_id}/attempts/a001", token=None)
    assert status == 200 and attempt_detail["attempt"]["attempt_id"] == "a001"

    status, diff = served.client.get(f"/api/candidates/{candidate_id}/diff", token=None)
    assert status == 200, diff
    assert diff["base_revision"] == "r000"
    assert diff["counts"] == {"added": 5, "removed": 0, "replaced": 0, "state_only": 0}
    assert diff["net_change_total"] == 5 == len(diff["net_changes"])
    assert diff["write_events"] == 5
    seen = set()
    for change in diff["net_changes"]:
        assert len(change["pos_local"]) == 3
        assert change["category"] in ("added", "removed", "replaced", "state_only")
        assert change["before"]["name"] == "minecraft:air"
        assert isinstance(change["after"]["props"], dict)
        key = tuple(change["pos_local"])
        assert key not in seen, "a net change must not repeat a coordinate"
        seen.add(key)

    status, candidate_scene = served.client.get(
        f"/api/candidates/{candidate_id}/render-scene", token=None)
    assert status == 200
    validate_render_scene(candidate_scene)
    assert candidate_scene["scene_id"] == candidate_id
    assert candidate_scene["scene_hash"] == diff["scene_hash"]

    status, record = served.client.get(f"/api/candidates/{candidate_id}", token=None)
    assert status == 200 and record["file_present"] is True and record["accepted"] is False
    # nothing was committed by compiling
    assert served.revisions() == ["r000"]
    assert served.head()["revision_id"] == "r000"


def test_attempt_rejects_a_bad_plan_and_a_plan_outside_the_selection(served):
    status, task = served.client.post(
        f"/api/projects/{PROJECT_ID}/tasks",
        {"selection": {"min": [0, 0, 0], "max_exclusive": [8, 8, 8]},
         "instruction": "small area only"},
    )
    assert status == 201
    task_id = task["task_id"]

    status, payload = served.client.post(
        f"/api/tasks/{task_id}/attempts", {"plan": {"nope": True}})
    assert status == 400 and payload["error"]["code"] == "INVALID_PLAN"
    assert list((served.project_dir / "tasks" / task_id / "attempts").iterdir()) == []

    status, payload = served.client.post(
        f"/api/tasks/{task_id}/attempts", {"plan": PLAN})
    assert status == 409, payload
    assert payload["error"]["code"].startswith("WRITE_")
    assert payload["error"]["detail"]["pos_local"] is not None

    status, detail = served.client.get(f"/api/tasks/{task_id}", token=None)
    attempt = detail["attempts"][0]
    assert attempt["state"] == "FAILED" and attempt["candidate_id"] is None
    assert attempt["errors"][0]["code"] == payload["error"]["code"]
    assert (served.project_dir / "tasks" / task_id / "attempts" / "a001"
            / "candidate.litematic").exists() is False


def test_accept_is_cas_guarded_and_undo_redo_move_the_history(served):
    candidate_id, task_id = _candidate_id(served)
    head = served.head()
    assert head["revision_id"] == "r000" and head["generation"] == 0

    # 1. a stale expected_head conflicts and writes nothing
    status, payload = served.client.post(
        f"/api/candidates/{candidate_id}/accept",
        {"expected_head": "r999", "expected_generation": 0})
    assert status == 409
    assert payload["error"]["code"] == "STALE_HEAD"
    assert payload["error"]["detail"]["expected_head"] == "r999"
    assert payload["error"]["detail"]["actual_head"] == "r000"
    assert served.revisions() == ["r000"]

    status, payload = served.client.post(
        f"/api/candidates/{candidate_id}/accept",
        {"expected_head": "r000", "expected_generation": 7})
    assert status == 409
    assert payload["error"]["detail"]["expected_generation"] == 7
    assert payload["error"]["detail"]["actual_generation"] == 0

    status, payload = served.client.post(
        f"/api/candidates/{candidate_id}/accept", {"expected_head": "r000"})
    assert status == 400 and payload["error"]["code"] == "MISSING_REQUIRED_FIELD"

    # 2. the real accept commits r001 through the project store
    status, accepted = served.client.post(
        f"/api/candidates/{candidate_id}/accept",
        {"expected_head": "r000", "expected_generation": 0,
         "expected_config_hash": served.head()["config_hash"]})
    assert status == 200, accepted
    assert accepted["accepted"] is True
    assert accepted["revision_id"] == "r001"
    assert accepted["head"] == {"revision_id": "r001", "generation": 1,
                               "config_revision": "cfg001",
                               "config_hash": accepted["head"]["config_hash"]}
    assert accepted["patch_count"] == 5
    assert accepted["task_state"] == "ACCEPTED"
    assert served.head()["revision_id"] == "r001"
    manifest = json.loads(
        (served.project_dir / "revisions" / "r001" / "manifest.json").read_text("utf-8"))
    assert manifest["candidate_id"] == candidate_id and manifest["task_id"] == task_id
    assert manifest["patch_count"] == 5 and manifest["objects_count"] == 1
    objects = json.loads(
        (served.project_dir / "revisions" / "r001" / "objects.json").read_text("utf-8"))
    assert [o["object_id"] for o in objects["objects"]] == ["pavilion_1@r001"]
    assert objects["objects"][0]["creation_revision"] == "r001"
    assert len(objects["objects"][0]["owned"]) == 5

    status, payload = served.client.post(
        f"/api/candidates/{candidate_id}/accept",
        {"expected_head": "r001", "expected_generation": 1})
    assert status == 409 and payload["error"]["code"] == "ALREADY_ACCEPTED"

    # 3. undo returns to r000 and redo replays r001 (both CAS-guarded)
    status, undone = served.client.post(
        f"/api/projects/{PROJECT_ID}/undo",
        {"expected_head": "r001", "expected_generation": 1})
    assert status == 200, undone
    assert undone["head"]["revision_id"] == "r000" and undone["head"]["generation"] == 2
    assert undone["previous_head"]["revision_id"] == "r001"
    assert undone["redo_stack"] == ["r001"]
    assert undone["accepted"] is False

    status, payload = served.client.post(
        f"/api/projects/{PROJECT_ID}/undo",
        {"expected_head": "r001", "expected_generation": 1})
    assert status == 409 and payload["error"]["code"] == "STALE_HEAD"

    status, payload = served.client.post(
        f"/api/projects/{PROJECT_ID}/undo",
        {"expected_head": "r000", "expected_generation": 2})
    assert status == 409 and payload["error"]["code"] == "NOTHING_TO_UNDO"

    status, redone = served.client.post(
        f"/api/projects/{PROJECT_ID}/redo",
        {"expected_head": "r000", "expected_generation": 2})
    assert status == 200, redone
    assert redone["head"]["revision_id"] == "r001" and redone["head"]["generation"] == 3
    assert redone["redo_stack"] == []
    assert served.head()["revision_id"] == "r001"
    # the revisions themselves are immutable: r000 and r001 are both still there
    assert served.revisions() == ["r000", "r001"]

    status, payload = served.client.post(
        f"/api/projects/{PROJECT_ID}/redo",
        {"expected_head": "r001", "expected_generation": 3})
    assert status == 409 and payload["error"]["code"] == "NOTHING_TO_REDO"


def test_project_detail_reflects_an_accepted_revision(served):
    candidate_id, task_id = _candidate_id(served)
    served.client.post(
        f"/api/candidates/{candidate_id}/accept",
        {"expected_head": "r000", "expected_generation": 0})
    status, payload = served.client.get(f"/api/projects/{PROJECT_ID}", token=None)
    assert status == 200
    assert payload["head_revision"] == "r001" and payload["can_undo"] is True
    assert [row["revision_id"] for row in payload["revisions"]] == ["r000", "r001"]
    history = {row["revision_id"]: row for row in payload["history"]}
    assert history["r001"]["kind"] == "accepted"
    assert history[candidate_id]["kind"] == "candidate"
    assert history[candidate_id]["accepted"] is False
    assert history["r001"]["scene_url"] == (
        f"/api/projects/{PROJECT_ID}/revisions/r001/render-scene")
    assert payload["tasks"][0]["task_id"] == task_id
    assert payload["tasks"][0]["state"] == "ACCEPTED"


def test_a_task_invalidated_by_a_new_revision_reports_staleness(served):
    candidate_id, task_id = _candidate_id(served)
    served.client.post(
        f"/api/candidates/{candidate_id}/accept",
        {"expected_head": "r000", "expected_generation": 0})
    status, detail = served.client.get(f"/api/tasks/{task_id}", token=None)
    assert status == 200
    assert detail["fresh"] is False
    assert detail["stale"]["code"] == "STALE_BASE_REVISION"
    # a second candidate for the stale task is refused before anything is written
    status, payload = served.client.post(
        f"/api/tasks/{task_id}/attempts", {"plan": PLAN})
    assert status == 409
    assert payload["error"]["code"] in ("STALE_BASE_REVISION", "STALE_HEAD_GENERATION")
    # no new attempt was allocated for the stale task
    attempts = sorted(p.name for p in
                      (served.project_dir / "tasks" / task_id / "attempts").iterdir())
    assert attempts == ["a001"]
    assert (served.project_dir / "tasks" / task_id / "attempts" / "a001"
            / "candidate.json").is_file()
