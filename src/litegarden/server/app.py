"""Local HTTP service: same-origin JSON API + workbench static files.

Why the standard library
------------------------
The engineering brief (§17.1/§19.2) asks for a local service module, not a new
runtime dependency: this project's rules forbid adding third-party packages, the
service only ever serves one loopback user, and the route table is small. So the
transport is ``http.server.ThreadingHTTPServer`` + ``json``, and the frontend in
``web/`` is served from the same origin as the API.

Security posture (§19.2)
------------------------
* loopback only: the host must be ``127.0.0.1``/``localhost``/``::1`` unless the
  caller opts in explicitly with ``allow_remote``;
* every request is checked against the peer address and the ``Host`` header, and
  any request carrying a non-loopback ``Origin``/``Referer`` is refused: being
  reachable on 127.0.0.1 does not make a browser request trustworthy;
* every mutating request (POST/PUT/DELETE/PATCH) must additionally present
  ``X-Litegarden-Token`` equal to the per-run token handed out by
  ``GET /api/session`` (or a fixed token given to ``--token``);
* static files are resolved inside one of the two allowed roots and refused if
  the resolved path leaves it (``%2e%2e`` and backslashes included);
* errors are structured JSON ``{"error": {"code", "message", "detail"}}``; a
  traceback, an absolute private path or a token is never sent to the browser.

Write semantics
---------------
Every mutation goes through :class:`~litegarden.project_store.ProjectStore` under
:class:`~litegarden.project_store.ProjectLock` with its compare-and-swap rules;
a stale ``expected_head``/``expected_generation`` is a ``StaleHeadError`` and
becomes HTTP 409. Accepting a candidate is an explicit user action
(``POST /api/candidates/<c>/accept``) and is never implied by submitting,
compiling or reviewing one.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import threading
import urllib.parse
from contextlib import AbstractContextManager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from ..blocks import AIR_BLOCKS
from ..compiler import CompileError, compile_plan, make_guard
from ..constraints import Box3, WriteRejected, parse_box
from ..io import (
    MultiRegionError,
    UnsupportedFormatError,
    apply_patchset,
    compare_to_expected,
    ensure_export_preserved,
    entity_host_positions,
    load_scene,
    save_scene,
)
from ..net_patch import scene_semantic_hash
from ..objects import (
    ObjectError,
    ObjectRegistry,
    TargetDependencyConflict,
    TargetOwnershipConflict,
    record_from_compile,
)
from ..project_store import (
    HeadState,
    IncompleteRevisionError,
    ProjectLock,
    ProjectStore,
    ProjectStoreError,
    StaleHeadError,
    sha256_file,
)
from ..redesign import (
    RedesignError,
    Selection,
    SelectionReport,
    TaskRequest,
    TaskStore,
    analyze_selection,
    assert_transition,
    ensure_task_fresh,
    freeze_task,
    migrate_selection,
)
from ..render_scene import (
    RenderSceneError,
    build_render_scene,
    summarize_for_client,
    validate_render_scene,
)
from ..schema import parse_plan
from ..terrain import analyze, build_planning_index

SERVER_SCHEMA_VERSION = "0.2"
API_PREFIX = "api"
MAX_BODY_BYTES = 8 * 1024 * 1024
LOCK_TIMEOUT_SECONDS = 30.0

LOOPBACK_HOSTS = frozenset(
    {"127.0.0.1", "localhost", "::1", "0:0:0:0:0:0:0:1", "[::1]"}
)

TASK_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
ATTEMPT_ID_RE = re.compile(r"^a\d{3,}$")
_NEXT_TASK_RE = re.compile(r"^task[_-]?(\d+)$")

#: ``plan.json`` op type -> the provenance ``kind`` stored in ``objects.json``.
OP_KINDS: Dict[str, str] = {
    "place_asset": "asset",
    "connect_path": "path",
    "decorate_path": "decoration",
    "scatter_assets": "asset",
}

_STATE_RE = re.compile(r"^(?P<name>[^\[\]]+?)(?:\[(?P<props>.*)\])?$")
_WINDOWS_PATH_RE = re.compile(r"[A-Za-z]:\\[^\"']*")

#: What this service really implements; ``GET /api/session`` publishes it so a
#: client never has to guess whether undo/accept/review exist yet.
CAPABILITIES: Dict[str, bool] = {
    "session": True,
    "project_import": False,  # `project init` is still a CLI action
    "revisions": True,
    "render_scene": True,
    "selection_validate": True,
    "redesign_tasks": True,
    "attempt_compile": True,
    "candidate_diff": True,
    "candidate_accept": True,
    "candidate_reject": False,
    "undo_redo": True,
    "exports": False,
    "review_jobs": False,
    "agent_runner": False,
}


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------


class ApiError(Exception):
    """A structured API failure; it never carries a traceback to the client."""

    def __init__(self, status: int, code: str, message: str, detail: Optional[dict] = None):
        super().__init__(message)
        self.status = int(status)
        self.code = str(code)
        self.detail = detail or {}

    def to_dict(self) -> dict:
        return {
            "error": {
                "code": self.code,
                "message": str(self),
                "detail": _sanitise(self.detail),
                "status": self.status,
            }
        }


#: ``objects.py`` failures carry no code attribute; map them to the API codes.
_OBJECT_ERROR_CODES: Dict[str, str] = {
    "TargetOwnershipConflict": "TARGET_OWNERSHIP_CONFLICT",
    "TargetDependencyConflict": "TARGET_DEPENDENCY_CONFLICT",
}


def object_error_code(exc: BaseException) -> str:
    """Stable machine code for an ``objects.py`` failure (spec 17.2 codes)."""
    return _OBJECT_ERROR_CODES.get(type(exc).__name__, "OBJECT_ERROR")

def viewer_resource_hash(web_dir: Path) -> Optional[str]:
    """A deterministic hash of the viewer bundle this service actually serves.

    ``RenderScene.resource_hash`` must describe the resource set a payload was
    rendered with, so it is computed from the served bytes (name, size and
    content hash per file) instead of copied from a lock file that may not match
    the vendored copy on this machine. ``None`` when no bundle is present: the
    field is then omitted and the client reports the missing claim.
    """
    root = Path(web_dir) / "vendor" / "litematica-viewer"
    if not root.is_dir():
        return None
    files = sorted(p for p in root.rglob("*") if p.is_file())
    if not files:
        return None
    digest = hashlib.sha256()
    for path in files:
        digest.update(str(path.relative_to(root)).replace("\\", "/").encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(path.stat().st_size).encode("ascii"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()



def _sanitise(value: Any) -> Any:
    """Drop absolute local paths from a payload before it reaches a browser."""
    if isinstance(value, dict):
        return {k: _sanitise(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitise(v) for v in value]
    if isinstance(value, str):
        return _WINDOWS_PATH_RE.sub(lambda m: os.path.basename(m.group(0)), value)
    return value


def _require(body: Mapping, key: str, what: Optional[str] = None) -> Any:
    if key not in body or body[key] is None:
        raise ApiError(400, "MISSING_REQUIRED_FIELD",
                       f"request body needs '{what or key}'")
    return body[key]


def _require_int(value: Any, what: str) -> int:
    """Strict integer: a bool, a float or a string is never coerced."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ApiError(400, "INVALID_FIELD", f"{what} must be an integer, got {value!r}")
    return int(value)


def _ints(values: Optional[Sequence[str]]) -> Optional[List[int]]:
    """``?crop_size=10,4,10`` -> ``[10, 4, 10]``."""
    if not values:
        return None
    try:
        out = [int(v) for v in str(values[0]).split(",")]
    except (ValueError, AttributeError, IndexError):
        raise ApiError(400, "INVALID_QUERY",
                       "expected a comma separated integer triple") from None
    if len(out) != 3:
        raise ApiError(400, "INVALID_QUERY", "expected exactly 3 integers")
    return out


def parse_state(text: Any) -> dict:
    """``"minecraft:oak_planks[facing=north]"`` -> ``{"name", "props"}``."""
    raw = str(text if text is not None else "").strip()
    match = _STATE_RE.match(raw)
    if not match:
        return {"name": raw, "props": {}}
    props: Dict[str, str] = {}
    for item in (match.group("props") or "").split(","):
        if "=" in item:
            key, value = item.split("=", 1)
            props[key.strip()] = value.strip()
    return {"name": match.group("name").strip(), "props": props}


def _is_air(state: Any) -> bool:
    return parse_state(state)["name"] in AIR_BLOCKS


def classify_change(before: Any, after: Any) -> Optional[str]:
    """The four net-change categories, using the frontend's rules verbatim."""
    b_air, a_air = _is_air(before), _is_air(after)
    if b_air and a_air:
        return None
    if b_air:
        return "added"
    if a_air:
        return "removed"
    b, a = parse_state(before), parse_state(after)
    if b["name"] != a["name"]:
        return "replaced"
    if b["props"] != a["props"]:
        return "state_only"
    return None


def _analysis(scene, config: Mapping) -> Any:
    """Terrain analysis with the config's declared anchors (same as the CLI)."""
    analysis = analyze(scene.snapshot)
    build_planning_index(analysis)
    for name, pos in (config.get("anchors") or {}).items():
        try:
            analysis.anchors[str(name)] = tuple(int(v) for v in pos)
        except (TypeError, ValueError):
            continue
    return analysis


def _object_inputs_from_patch(
    changes: Sequence[Mapping], op_kinds: Mapping[str, str]
) -> List[dict]:
    """Derive per-operation provenance inputs from a net patch.

    ``substrate`` is the state before the operation and ``owned`` the state
    after it - exactly what the patch records, so nothing is invented. The
    coordinates are ``"x,y,z"`` keys, the same convention ``objects.json`` and
    the prefab files use.
    """
    grouped: Dict[str, List[Mapping]] = {}
    for change in changes:
        if not isinstance(change, Mapping):
            continue
        grouped.setdefault(str(change.get("op_id") or "unattributed"), []).append(change)
    out: List[dict] = []
    for op_id in sorted(grouped):
        rows = grouped[op_id]
        substrate: Dict[str, str] = {}
        owned: Dict[str, str] = {}
        occupied: List[List[int]] = []
        for row in rows:
            pos = row.get("pos_local")
            if not isinstance(pos, (list, tuple)) or len(pos) != 3:
                continue
            key = f"{int(pos[0])},{int(pos[1])},{int(pos[2])}"
            substrate[key] = str(row.get("before"))
            owned[key] = str(row.get("after"))
            if not _is_air(row.get("after")):
                occupied.append([int(pos[0]), int(pos[1]), int(pos[2])])
        out.append({
            "op_id": op_id,
            "kind": op_kinds.get(op_id, "asset"),
            "operation_ids": [op_id],
            "substrate": substrate,
            "owned": owned,
            "occupied_voxels": occupied,
            "write_set": sorted(substrate),
        })
    return out


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------


def default_assets_dir() -> Path:
    """``<repo>/assets`` next to the installed package."""
    return Path(__file__).resolve().parents[3] / "assets"


def default_work_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "work"


@dataclass
class ServerConfig:
    """Everything the service needs; the project is never taken from a request."""

    project_dir: Path
    web_dir: Path
    work_dir: Optional[Path] = None
    assets_dir: Optional[Path] = None
    host: str = "127.0.0.1"
    port: int = 8765
    token: Optional[str] = None
    allow_remote: bool = False

    def __post_init__(self) -> None:
        self.project_dir = Path(self.project_dir)
        self.web_dir = Path(self.web_dir)
        if self.work_dir is None:
            self.work_dir = default_work_dir()
        else:
            self.work_dir = Path(self.work_dir)
        if self.assets_dir is None:
            self.assets_dir = default_assets_dir()
        else:
            self.assets_dir = Path(self.assets_dir)
        self.host = str(self.host)
        self.port = int(self.port)
        # ``None`` -> a fresh random token for this run; ``""`` -> explicitly no
        # token (a deliberate, documented decision, never a silent default).
        self.token = secrets.token_urlsafe(24) if self.token is None else str(self.token)

    @property
    def require_token(self) -> bool:
        return bool(self.token)

    def base_url(self, port: Optional[int] = None) -> str:
        return f"http://{self.host}:{int(port if port is not None else self.port)}"


# --------------------------------------------------------------------------
# service (all read/write logic; the HTTP layer only translates)
# --------------------------------------------------------------------------


class ProjectService:
    """One instance owns one project directory.

    Reads re-read the artefacts on disk (no cache), and mutations hold both an
    in-process lock and the on-disk :class:`ProjectLock`, so two API calls can
    never interleave a read-modify-write.
    """

    def __init__(self, config: ServerConfig):
        self.config = config
        self.project_dir = Path(config.project_dir).resolve()
        self.store = ProjectStore(self.project_dir)
        self.tasks = TaskStore(self.project_dir)
        self._lock = threading.RLock()
        self._viewer_hash: Optional[str] = None
        self._viewer_hash_ready = False

    @property
    def resource_hash(self) -> Optional[str]:
        """Cached hash of the served viewer bundle (see :func:`viewer_resource_hash`)."""
        if not self._viewer_hash_ready:
            self._viewer_hash = viewer_resource_hash(Path(self.config.web_dir))
            self._viewer_hash_ready = True
        return self._viewer_hash

    # -- helpers ---------------------------------------------------------

    def _mutation(self, what: str) -> AbstractContextManager:
        @contextlib.contextmanager
        def ctx() -> Iterator[None]:
            with self._lock:
                lock = ProjectLock(self.project_dir, timeout=LOCK_TIMEOUT_SECONDS)
                try:
                    lock.__enter__()
                except ProjectStoreError as exc:
                    raise ApiError(
                        503, "PROJECT_LOCK_UNAVAILABLE",
                        f"could not lock the project to {what}: {exc}",
                    ) from None
                try:
                    yield
                finally:
                    lock.__exit__(None, None, None)

        return ctx()

    @property
    def project_id(self) -> str:
        try:
            return self.store.project_id
        except ProjectStoreError as exc:
            raise ApiError(500, "PROJECT_UNREADABLE", str(exc)) from None

    def _head(self) -> HeadState:
        try:
            return self.store.head
        except IncompleteRevisionError as exc:
            raise ApiError(500, "INCOMPLETE_REVISION", str(exc), exc.to_dict()) from None
        except StaleHeadError as exc:  # pragma: no cover - defensive
            raise ApiError(409, "STALE_HEAD", str(exc), exc.to_dict()) from None
        except ProjectStoreError as exc:
            raise ApiError(500, "PROJECT_STATE_UNREADABLE", str(exc)) from None

    def _config(self) -> dict:
        head = self._head()
        try:
            return self.store.load_config(head.config_revision)
        except ProjectStoreError as exc:
            raise ApiError(500, "CONFIG_UNREADABLE", str(exc)) from None

    def _revision_scene_path(self, revision_id: str) -> Path:
        return self.store.revision_dir(revision_id) / "scene.litematic"

    def load_revision_scene(self, revision_id: str):
        """Load a revision's scene, refusing anything that is not complete."""
        if revision_id not in self.store.list_revisions():
            raise ApiError(404, "UNKNOWN_REVISION", f"unknown revision {revision_id!r}")
        try:
            self.store.verify_revision_complete(revision_id)
        except IncompleteRevisionError as exc:
            raise ApiError(500, "INCOMPLETE_REVISION", str(exc), exc.to_dict()) from None
        except ProjectStoreError as exc:
            raise ApiError(500, "INCOMPLETE_REVISION", str(exc)) from None
        path = self._revision_scene_path(revision_id)
        try:
            return load_scene(str(path))
        except (MultiRegionError, UnsupportedFormatError, ValueError, OSError) as exc:
            raise ApiError(500, "SCENE_UNREADABLE",
                           f"revision {revision_id!r} could not be re-read: {exc}") from None

    def scene_bounds(self, scene) -> Box3:
        size = scene.snapshot.transform.local_size
        return Box3((0, 0, 0), (int(size[0]), int(size[1]), int(size[2])))

    def scene_hash(self, scene) -> str:
        return scene_semantic_hash(
            scene.snapshot,
            region_id=scene.snapshot.region_id,
            data_version=scene.data_version,
        )

    def _objects_payload(self, revision_id: str) -> dict:
        try:
            return self.store.load_objects(revision_id)
        except (ProjectStoreError, IncompleteRevisionError):
            return {"schema_version": SERVER_SCHEMA_VERSION, "count": 0, "objects": []}

    def _registry(self, revision_id: str) -> ObjectRegistry:
        payload = self._objects_payload(revision_id)
        try:
            return ObjectRegistry.from_dict(payload)
        except ObjectError as exc:
            raise ApiError(500, "OBJECTS_UNREADABLE",
                           f"revision {revision_id!r} objects.json is not a registry: {exc}") from None

    def _check_project_id(self, project_id: str) -> None:
        if project_id not in (self.project_id, "@current", "-"):
            raise ApiError(404, "UNKNOWN_PROJECT", f"unknown project {project_id!r}")

    def _task_ids(self) -> List[str]:
        root = self.tasks.tasks_dir
        if not root.is_dir():
            return []
        return sorted(p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))

    def _next_task_id(self) -> str:
        highest = 0
        for name in self._task_ids():
            match = _NEXT_TASK_RE.match(name)
            if match:
                highest = max(highest, int(match.group(1)))
        return f"task_{highest + 1:03d}"

    def _task_state(self, task_id: str) -> dict:
        try:
            return self.tasks.read_state(task_id)
        except FileNotFoundError:
            raise ApiError(404, "UNKNOWN_TASK", f"unknown task {task_id!r}") from None
        except RedesignError as exc:
            raise ApiError(404, "UNKNOWN_TASK", str(exc), exc.to_dict()) from None

    def _task_request(self, task_id: str) -> TaskRequest:
        try:
            return self.tasks.read_request(task_id)
        except FileNotFoundError:
            raise ApiError(404, "UNKNOWN_TASK", f"unknown task {task_id!r}") from None
        except RedesignError as exc:
            raise ApiError(500, "TASK_UNREADABLE", str(exc), exc.to_dict()) from None

    def _read_attempt(self, task_id: str, attempt_id: str):
        try:
            return self.tasks.read_attempt(task_id, attempt_id)
        except FileNotFoundError:
            raise ApiError(404, "UNKNOWN_ATTEMPT",
                           f"unknown attempt {attempt_id!r} of task {task_id!r}") from None
        except RedesignError as exc:
            raise ApiError(404, "UNKNOWN_ATTEMPT", str(exc), exc.to_dict()) from None

    def _attempt_dir(self, task_id: str, attempt_id: str) -> Path:
        """Path lookup only: an unknown task is a 404, not a state-machine error."""
        return self.tasks.tasks_dir / task_id / "attempts" / attempt_id

    def _split_candidate(self, candidate_id: str) -> Tuple[str, str]:
        if not isinstance(candidate_id, str) or "-" not in candidate_id:
            raise ApiError(400, "INVALID_CANDIDATE_ID",
                           "a candidate id looks like <task_id>-<attempt_id>")
        task_id, attempt_id = candidate_id.rsplit("-", 1)
        if not TASK_ID_RE.match(task_id) or not ATTEMPT_ID_RE.match(attempt_id):
            raise ApiError(400, "INVALID_CANDIDATE_ID", f"malformed candidate id {candidate_id!r}")
        return task_id, attempt_id

    def candidate_paths(self, candidate_id: str) -> dict:
        task_id, attempt_id = self._split_candidate(candidate_id)
        directory = self._attempt_dir(task_id, attempt_id)
        if not directory.is_dir():
            raise ApiError(404, "UNKNOWN_CANDIDATE", f"unknown candidate {candidate_id!r}")
        return {
            "task_id": task_id,
            "attempt_id": attempt_id,
            "dir": directory,
            "scene": directory / "candidate.litematic",
            "record": directory / "candidate.json",
            "patch": directory / "patch.json",
            "objects": directory / "object_inputs.json",
            "write_log": directory / "write_log.jsonl",
        }

    def _assert_task_fresh(self, request: TaskRequest) -> None:
        """Refuse a stale task before anything is written (409, never 500)."""
        head = self._head()
        base_scene = self.load_revision_scene(request.base_revision_id)
        try:
            ensure_task_fresh(
                request,
                head_revision_id=head.revision_id,
                head_generation=head.generation,
                config_hash=head.config_hash,
                scene_hash=self.scene_hash(base_scene),
            )
        except RedesignError as exc:
            raise ApiError(409, exc.code, str(exc), exc.detail) from None

    # -- render scene ----------------------------------------------------

    def _render_scene(
        self,
        scene,
        *,
        scene_id: str,
        scene_path: Path,
        crop_origin: Optional[Sequence[int]] = None,
        crop_size: Optional[Sequence[int]] = None,
    ) -> dict:
        try:
            payload = build_render_scene(
                scene,
                scene_id=scene_id,
                file_sha256=sha256_file(scene_path) if Path(scene_path).is_file() else None,
                crop_origin_local=tuple(crop_origin) if crop_origin else (0, 0, 0),
                crop_size=tuple(crop_size) if crop_size else None,
                resource_hash=self.resource_hash,
                minecraft_data_version=scene.data_version,
            )
        except RenderSceneError as exc:
            code = getattr(exc, "code", "RENDER_SCENE_INVALID")
            # A bad crop is client input; anything else is a broken artefact.
            status = 400 if code == "INDEX_OUT_OF_RANGE" else 500
            raise ApiError(status, code, str(exc), {"scene_id": scene_id}) from None
        try:
            validate_render_scene(payload)
        except RenderSceneError as exc:
            raise ApiError(500, getattr(exc, "code", "RENDER_SCENE_INVALID"), str(exc),
                           {"scene_id": scene_id}) from None
        return payload

    def render_scene_for_revision(self, revision_id: str, crop_origin, crop_size) -> dict:
        scene = self.load_revision_scene(revision_id)
        return self._render_scene(
            scene, scene_id=revision_id,
            scene_path=self._revision_scene_path(revision_id),
            crop_origin=crop_origin, crop_size=crop_size,
        )

    def candidate_render_scene(self, candidate_id: str, crop_origin, crop_size) -> dict:
        paths = self.candidate_paths(candidate_id)
        if not paths["scene"].is_file():
            raise ApiError(404, "CANDIDATE_NOT_SERIALIZED",
                           f"candidate {candidate_id!r} has no serialized scene")
        try:
            scene = load_scene(str(paths["scene"]))
        except (MultiRegionError, UnsupportedFormatError, ValueError, OSError) as exc:
            raise ApiError(500, "SCENE_UNREADABLE",
                           f"candidate {candidate_id!r} could not be re-read: {exc}") from None
        return self._render_scene(
            scene, scene_id=candidate_id, scene_path=paths["scene"],
            crop_origin=crop_origin, crop_size=crop_size,
        )

    # -- session / project ----------------------------------------------

    def session_info(self) -> dict:
        head = self._head()
        project_id = self.project_id
        return {
            "schema_version": SERVER_SCHEMA_VERSION,
            "server": {
                "name": "litegarden-local",
                "schema_version": SERVER_SCHEMA_VERSION,
                "pid": os.getpid(),
                "loopback_only": True,
            },
            "bind": {"host": self.config.host, "port": self.config.port},
            "project_id": project_id,
            "head": head.to_dict(),
            # The run token is handed out here (and printed on start): the
            # workbench is same-origin, so the page can read it without a second
            # channel, and a cross-site page cannot read this response at all.
            "token": self.config.token,
            "token_required": self.config.require_token,
            "token_header": "X-Litegarden-Token",
            "capabilities": dict(CAPABILITIES),
            "routes": [
                "GET  /api/session",
                "GET  /api/project",
                "GET  /api/projects/<project_id>",
                "GET  /api/projects/<project_id>/revisions/<revision>/render-scene",
                "POST /api/projects/<project_id>/selections/validate",
                "POST /api/projects/<project_id>/tasks",
                "POST /api/projects/<project_id>/redesign-tasks",
                "GET  /api/tasks/<task_id>",
                "GET  /api/tasks/<task_id>/attempts/<attempt_id>",
                "POST /api/tasks/<task_id>/attempts",
                "GET  /api/candidates/<candidate_id>",
                "GET  /api/candidates/<candidate_id>/diff",
                "GET  /api/candidates/<candidate_id>/render-scene",
                "POST /api/candidates/<candidate_id>/accept",
                "POST /api/projects/<project_id>/undo",
                "POST /api/projects/<project_id>/redo",
            ],
            "static": {
                "index": "/web/index.html",
                "web_root": "/web/*",
                "work_root": "/work/*",
            },
        }

    def _protected_rows(self, config: Mapping) -> Tuple[List[dict], List[dict]]:
        rows: List[dict] = []
        issues: List[dict] = []
        for index, zone in enumerate(config.get("protected_zones") or []):
            zone_id = zone.get("id") if isinstance(zone, dict) else None
            label = zone.get("label") if isinstance(zone, dict) else None
            try:
                box = parse_box(zone, f"protected_zones[{zone_id or index}]")
            except ValueError as exc:
                issues.append({
                    "code": "PROTECTED_ZONE_INVALID",
                    "message": f"protected zone {zone_id or index!r} cannot be parsed: {exc}",
                    "severity": "error",
                    "kind": "config",
                })
                continue
            row = {
                "id": str(zone_id or f"protected_{index}"),
                "label": str(label or zone_id or f"保护区 {index}"),
                "bounds": box.to_dict(),
                "read_only": True,
                "source": "project config",
            }
            if isinstance(zone, dict) and zone.get("source"):
                row["source"] = str(zone["source"])
            rows.append(row)
        return rows, issues

    def _asset_rows(self, objects_payload: Mapping) -> Tuple[List[dict], List[dict]]:
        rows: List[dict] = []
        issues: List[dict] = []
        entries = objects_payload.get("objects")
        if isinstance(entries, Mapping):
            entries = list(entries.values())
        if not isinstance(entries, list):
            entries = []
        for record in entries:
            if not isinstance(record, Mapping):
                continue
            voxels = record.get("occupied_voxels") or record.get("write_set") or []
            points = [
                (int(p[0]), int(p[1]), int(p[2]))
                for p in voxels
                if isinstance(p, (list, tuple)) and len(p) == 3
            ]
            bounds = Box3.around(points).to_dict() if points else None
            complete = bool(record.get("complete", True))
            rows.append({
                "object_id": record.get("object_id"),
                "kind": record.get("kind"),
                "asset_version": record.get("asset_version"),
                "creation_revision": record.get("creation_revision"),
                "bounds": bounds,
                "status": "registered" if complete else "incomplete_source",
                "operation_ids": list(record.get("operation_ids") or []),
                "owned_voxels": len(record.get("owned") or {}),
            })
            if not complete:
                issues.append({
                    "code": "OBJECT_PROVENANCE_INCOMPLETE",
                    "message": (
                        f"object {record.get('object_id')!r} has no complete "
                        f"provenance and is never treated as system-generated"
                    ),
                    "severity": "warning",
                    "kind": "objects",
                })
        return rows, issues

    def _revision_rows(self) -> Tuple[List[dict], List[dict]]:
        rows: List[dict] = []
        issues: List[dict] = []
        project_id = self.project_id
        for revision_id in self.store.list_revisions():
            try:
                manifest = self.store.load_manifest(revision_id)
            except ProjectStoreError as exc:
                issues.append({
                    "code": "REVISION_INCOMPLETE",
                    "message": f"revision {revision_id} cannot be read: {exc}",
                    "severity": "error",
                    "kind": "revision",
                    "revision_id": revision_id,
                })
                continue
            rows.append({
                "revision_id": revision_id,
                "kind": "accepted",
                "label": (
                    f"revision {revision_id}: {manifest.get('patch_count')} net change(s), "
                    f"config {manifest.get('config_revision')}"
                ),
                "scene_url": f"/api/projects/{project_id}/revisions/{revision_id}/render-scene",
                "parent_revision": manifest.get("parent_revision"),
                "task_id": manifest.get("task_id"),
                "created": manifest.get("created"),
                "patch_count": manifest.get("patch_count"),
                "objects_count": manifest.get("objects_count"),
            })
        return rows, issues

    def _task_rows(self) -> Tuple[List[dict], List[dict], List[dict]]:
        """``(history_rows, issue_rows, task_summaries)`` for the panels payload."""
        history: List[dict] = []
        issue_rows: List[dict] = []
        summaries: List[dict] = []
        summaries: List[dict] = []
        for task_id in self._task_ids():
            try:
                state = self.tasks.read_state(task_id)
                request = self.tasks.read_request(task_id)
            except (RedesignError, ProjectStoreError, FileNotFoundError, ValueError) as exc:
                issue_rows.append({
                    "code": "TASK_UNREADABLE",
                    "message": f"task {task_id} cannot be read: {exc}",
                    "severity": "error",
                    "kind": "task",
                    "task_id": task_id,
                })
                continue
            attempts = []
            for attempt_id in self.tasks.list_attempts(task_id):
                record = self.tasks.read_attempt(task_id, attempt_id)
                candidate = record.candidate or None
                candidate_id = None
                if candidate is not None:
                    candidate_id = str(candidate.get("candidate_id") or f"{task_id}-{attempt_id}")
                attempts.append({
                    "attempt_id": attempt_id,
                    "state": record.state,
                    "has_plan": record.plan is not None,
                    "candidate_id": candidate_id,
                    "candidate": candidate,
                    "errors": list(record.errors),
                })
                for error in record.errors or []:
                    if not isinstance(error, Mapping):
                        continue
                    issue_rows.append({
                        "code": error.get("code") or "ATTEMPT_FAILED",
                        "message": str(error.get("message") or error),
                        "severity": "error",
                        "kind": "attempt",
                        "task_id": task_id,
                        "attempt_id": attempt_id,
                        "pos_local": error.get("pos_local"),
                        "rule_id": error.get("rule_id"),
                        "expected": error.get("expected"),
                        "actual": error.get("actual"),
                    })
                if candidate_id:
                    history.append({
                        "revision_id": candidate_id,
                        "kind": "candidate",
                        "label": (
                            f"candidate {candidate_id}: {record.state}"
                            + (f", {candidate.get('changes')} net change(s)" if isinstance(candidate, Mapping) else "")
                        ),
                        "scene_url": f"/api/candidates/{candidate_id}/render-scene",
                        "task_id": task_id,
                        "attempt_id": attempt_id,
                        "accepted": False,
                    })
            summaries.append({
                "task_id": task_id,
                "state": state.get("state"),
                "attempts": attempts,
                "instruction": request.instruction,
                "mode": request.mode,
                "base_revision_id": request.base_revision_id,
                "selection": request.selection.to_dict(),
            })
        return history, issue_rows, summaries

    def project_detail(self) -> dict:
        head = self._head()
        project_id = self.project_id
        config = self._config()
        objects_payload = self._objects_payload(head.revision_id)
        protected_rows, protected_issues = self._protected_rows(config)
        asset_rows, asset_issues = self._asset_rows(objects_payload)
        revision_rows, revision_issues = self._revision_rows()
        task_history, task_issues, task_summaries = self._task_rows()
        issues = protected_issues + revision_issues + asset_issues + task_issues
        redo_stack = []
        try:
            redo_stack = self.store.redo_stack
        except ProjectStoreError:
            redo_stack = []
        try:
            project_json = self.store.project_json()
        except ProjectStoreError:
            project_json = {}
        counts: Dict[str, int] = {}
        for issue in issues:
            code = str(issue.get("code"))
            counts[code] = counts.get(code, 0) + 1
        return {
            "schema_version": SERVER_SCHEMA_VERSION,
            "project_id": project_id,
            "project": project_json,
            "head": head.to_dict(),
            "head_revision": head.revision_id,
            "head_generation": head.generation,
            "redo_stack": redo_stack,
            "can_undo": bool(self.store.parent_of(head.revision_id)) if head.revision_id.startswith("r") else False,
            "can_redo": bool(redo_stack),
            "revisions": revision_rows,
            "revision_ids": self.store.list_revisions(),
            "config": config,
            "config_revision": head.config_revision,
            "config_hash": head.config_hash,
            "objects": {
                "count": len(asset_rows),
                "ids": [row["object_id"] for row in asset_rows],
                "objects": asset_rows,
            },
            "tasks": task_summaries,
            "diagnostics": {
                "errors": sum(1 for i in issues if i.get("severity") == "error"),
                "warnings": sum(1 for i in issues if i.get("severity") != "error"),
                "counts_by_code": counts,
                "note": (
                    "counts come from real artefacts (revision manifests, attempt "
                    "errors, object provenance); no progress percentage is invented"
                ),
            },
            # The four keys below are the shape web/app.js reads for ?panels=,
            # so GET /api/projects/<p> can be handed to the workbench as-is.
            "protected_areas": protected_rows,
            "assets": asset_rows,
            "history": revision_rows + task_history,
            "issues": issues,
        }

    # -- selection / tasks ----------------------------------------------

    def _selection_from_body(self, body: Mapping, bounds: Box3) -> Selection:
        raw = body.get("selection")
        if not isinstance(raw, Mapping):
            raise ApiError(400, "MISSING_REQUIRED_FIELD", "request body needs a 'selection' object")
        try:
            if body.get("allow_legacy_migration"):
                return migrate_selection(raw, bounds=bounds)
            return Selection.from_dict(raw, bounds=bounds)
        except RedesignError as exc:
            raise ApiError(400, exc.code, str(exc), exc.detail) from None

    def _analyze(self, selection: Selection, bounds: Box3, *, halo_xz: int,
                 revision_id: str, config: Mapping) -> SelectionReport:
        protected = []
        for index, zone in enumerate(config.get("protected_zones") or []):
            try:
                protected.append(parse_box(zone, f"protected_zones[{index}]"))
            except ValueError:
                continue
        registry = self._registry(revision_id)
        return analyze_selection(
            selection,
            scene_bounds=bounds,
            protected_boxes=protected,
            objects={oid: registry.get(oid) for oid in registry.ids},
            anchors=config.get("anchors") or {},
            halo_xz=halo_xz,
        )

    def validate_selection(self, body: Mapping) -> dict:
        head = self._head()
        base_revision = str(body.get("base_revision") or head.revision_id)
        scene = self.load_revision_scene(base_revision)
        bounds = self.scene_bounds(scene)
        selection = self._selection_from_body(body, bounds)
        halo_xz = _require_int(body.get("context_halo_xz", 12), "context_halo_xz")
        report = self._analyze(selection, bounds, halo_xz=halo_xz,
                              revision_id=base_revision, config=self._config())
        return {
            "schema_version": SERVER_SCHEMA_VERSION,
            "base_revision": base_revision,
            "scene_bounds": bounds.to_dict(),
            "report": report.to_dict(),
            "blocked": report.blocked,
            "requires_user_decision": bool(report.partial_objects),
        }

    def create_task(self, body: Mapping) -> dict:
        head = self._head()
        base_revision = str(body.get("base_revision") or head.revision_id)
        if base_revision != head.revision_id:
            raise ApiError(
                409, "STALE_BASE_REVISION",
                f"task base {base_revision!r} is not the current HEAD {head.revision_id!r}",
            )
        scene = self.load_revision_scene(base_revision)
        bounds = self.scene_bounds(scene)
        selection = self._selection_from_body(body, bounds)
        halo_xz = _require_int(body.get("context_halo_xz", 12), "context_halo_xz")
        config = self._config()
        report = self._analyze(selection, bounds, halo_xz=halo_xz,
                              revision_id=base_revision, config=config)
        mode = str(body.get("mode", "revise_current"))
        target_ids = [str(t) for t in (body.get("target_ids") or ())]
        if mode == "replace_generated":
            # The user's replacement request is checked against the registry
            # before it is frozen (ownership, scope, dependencies).
            self._validate_targets(base_revision, selection, target_ids)
        task_id = str(body.get("task_id") or self._next_task_id())
        if not TASK_ID_RE.match(task_id):
            raise ApiError(400, "INVALID_FIELD", f"bad task id {task_id!r}")
        try:
            request = freeze_task(
                request_id=task_id,
                project_id=self.project_id,
                base_revision_id=base_revision,
                base_scene_hash=self.scene_hash(scene),
                head_generation=head.generation,
                config_revision=head.config_revision,
                config_hash=head.config_hash,
                selection=selection,
                mode=mode,
                instruction=str(body.get("instruction", "")),
                report=report,
                context_halo_xz=halo_xz,
                target_ids=target_ids,
                seed=_require_int(body.get("seed", 0), "seed"),
            )
        except RedesignError as exc:
            raise ApiError(409, exc.code, str(exc), exc.detail) from None
        with self._mutation("create the task"):
            if task_id in self._task_ids():
                raise ApiError(409, "TASK_EXISTS", f"task {task_id!r} already exists")
            try:
                self.tasks.create_task(request, context={
                    "selection_report": report.to_dict(),
                    "scene_hash": request.base_scene_hash,
                    "scene_bounds": bounds.to_dict(),
                    "config": config,
                })
            except RedesignError as exc:
                raise ApiError(409, exc.code, str(exc), exc.detail) from None
        self.store.log("tasks", {"action": "created", "task_id": task_id,
                                "base_revision": base_revision, "mode": mode})
        state = self._task_state(task_id)
        return {
            "schema_version": SERVER_SCHEMA_VERSION,
            "task_id": task_id,
            "state": state.get("state"),
            "request": request.to_dict(),
            "report": report.to_dict(),
        }

    def _validate_targets(self, revision_id: str, selection: Selection,
                          target_ids: Sequence[str]) -> None:
        registry = self._registry(revision_id)
        scene = self.load_revision_scene(revision_id)
        try:
            registry.validate_targets(
                target_ids,
                selection_min=selection.min,
                selection_max_exclusive=selection.max_exclusive,
                state_at=scene.snapshot.block_at_local,
            )
        except ObjectError as exc:
            raise ApiError(409, object_error_code(exc), str(exc), exc.to_dict()) from None

    def task_detail(self, task_id: str) -> dict:
        state = self._task_state(task_id)
        request = self._task_request(task_id)
        attempts = []
        for attempt_id in self.tasks.list_attempts(task_id):
            record = self._read_attempt(task_id, attempt_id)
            candidate = record.candidate or None
            candidate_id = None
            if candidate is not None:
                candidate_id = str(candidate.get("candidate_id") or f"{task_id}-{attempt_id}")
            attempts.append({
                "attempt_id": attempt_id,
                "state": record.state,
                "has_plan": record.plan is not None,
                "candidate_id": candidate_id,
                "candidate": candidate,
                "errors": list(record.errors),
                "urls": {
                    "diff": f"/api/candidates/{candidate_id}/diff",
                    "render_scene": f"/api/candidates/{candidate_id}/render-scene",
                    "accept": f"/api/candidates/{candidate_id}/accept",
                } if candidate_id else None,
            })
        context_path = self.tasks.task_dir(task_id) / "context" / "context.json"
        context = {}
        if context_path.is_file():
            try:
                context = json.loads(context_path.read_text(encoding="utf-8"))
            except ValueError:
                context = {}
        stale: Optional[dict] = None
        try:
            self._assert_task_fresh(request)
        except ApiError as exc:
            stale = {"code": exc.code, "message": str(exc), "detail": _sanitise(exc.detail)}
        return {
            "schema_version": SERVER_SCHEMA_VERSION,
            "task_id": task_id,
            "state": state.get("state"),
            "state_updated": state.get("updated"),
            "request": request.to_dict(),
            "context": context,
            "attempts": attempts,
            "attempt_ids": [a["attempt_id"] for a in attempts],
            "fresh": stale is None,
            "stale": stale,
        }

    # -- attempts --------------------------------------------------------

    def _advance(self, task_id: str, target: str, *, via: Sequence[str] = ()) -> str:
        """Walk an explicit path through the frozen task state machine.

        Every step is validated by ``assert_transition``; a state that is
        already where the path starts is skipped, so a path may be replayed
        idempotently. An illegal step is a 409, never a silent write.
        """
        current = self._task_state(task_id).get("state")
        for step in tuple(via) + (target,):
            if current == step:
                continue
            try:
                assert_transition(current, step)
            except RedesignError as exc:
                raise ApiError(409, exc.code, str(exc), exc.detail) from None
            current = self.tasks.set_state(task_id, step).get("state")
        return current

    #: How each state reaches ``COMPILING``: the only paths the machine allows.
    _COMPILE_PATH: Dict[str, Tuple[str, ...]] = {
        "CREATED": ("CONTEXT_READY", "PLANNING", "PLAN_READY", "COMPILING"),
        "CONTEXT_READY": ("PLANNING", "PLAN_READY", "COMPILING"),
        "WAITING_AGENT": ("PLANNING", "PLAN_READY", "COMPILING"),
        "PLANNING": ("PLAN_READY", "COMPILING"),
        "PLAN_READY": ("COMPILING",),
        "FAILED": ("PLANNING", "PLAN_READY", "COMPILING"),
        "STALE": ("PLANNING", "PLAN_READY", "COMPILING"),
        "COMPILING": (),
    }

    def _start_compiling(self, task_id: str) -> None:
        state = self._task_state(task_id).get("state")
        path = self._COMPILE_PATH.get(state)
        if path is None:
            raise ApiError(
                409, "INVALID_TASK_STATE",
                f"task {task_id!r} is {state!r}: it has left the planning phase, so a "
                f"new attempt must be a new task (a failed attempt may be retried)",
            )
        self._advance(task_id, path[-1], via=path[:-1])

    def submit_attempt(self, task_id: str, body: Mapping) -> dict:
        """Compile an Agent plan inside the frozen task and serialize a candidate.

        The frozen selection becomes ``task_authorized``, so a plan that writes
        outside it is refused by the same gate that guards the CLI. A compile
        failure is a structured error (409 + code), never a 500 and never a
        half-written candidate.
        """
        raw_plan = body.get("plan")
        if raw_plan is None:
            raise ApiError(400, "MISSING_REQUIRED_FIELD", "request body needs a 'plan'")
        plan_text = raw_plan if isinstance(raw_plan, str) else json.dumps(raw_plan)
        try:
            plan = parse_plan(plan_text)
        except ValueError as exc:
            raise ApiError(400, "INVALID_PLAN", f"plan is not valid: {exc}") from None

        request = self._task_request(task_id)
        self._assert_task_fresh(request)
        config = self._config()
        base_scene = self.load_revision_scene(request.base_revision_id)
        assets_dir = Path(self.config.assets_dir)
        op_kinds = {op.id: OP_KINDS.get(op.op, "asset") for op in plan.operations}
        attempt_id = ""
        with self._mutation("compile the attempt"):
            # Re-checked inside the lock: a commit that landed while the plan was
            # being parsed must not be compiled against a stale base.
            self._assert_task_fresh(request)
            attempt_dir = self.tasks.create_attempt(task_id)
            attempt_id = attempt_dir.name
            record = self.tasks.read_attempt(task_id, attempt_id)
            self.tasks.write_attempt_json(task_id, attempt_id, "plan.json", {
                "schema_version": SERVER_SCHEMA_VERSION,
                "task_id": task_id,
                "attempt_id": attempt_id,
                "plan": json.loads(plan_text),
            })
            record = self.tasks.update_attempt(task_id, record,
                                               plan=json.loads(plan_text))
            self._start_compiling(task_id)
            analysis = _analysis(base_scene, config)
            guard_config = dict(config)
            guard_config["task_authorized"] = request.selection.to_dict()
            try:
                hosts = entity_host_positions(base_scene)
            except UnsupportedFormatError:
                hosts = []
            guard = make_guard(base_scene.snapshot, guard_config, assets_dir, hosts)
            self._advance(task_id, "COMPILING")
            try:
                result = compile_plan(
                    base_scene.snapshot, plan, analysis, assets_dir,
                    config=guard_config, guard=guard, entity_hosts=hosts,
                )
            except WriteRejected as exc:
                self._fail_attempt(task_id, attempt_id, exc.to_dict())
                raise ApiError(409, exc.code, str(exc), exc.to_dict()) from None
            except CompileError as exc:
                detail = {
                    "op_id": exc.op_id,
                    "code": exc.code,
                    "issues": _sanitise(list(exc.issues or [])),
                }
                self._fail_attempt(task_id, attempt_id, {
                    "code": exc.code or "COMPILE_FAILED",
                    "message": str(exc),
                    "op_id": exc.op_id,
                    "issues": detail["issues"],
                })
                raise ApiError(409, exc.code or "COMPILE_FAILED", str(exc), detail) from None
            except (UnsupportedFormatError, ValueError) as exc:
                self._fail_attempt(task_id, attempt_id, {
                    "code": getattr(exc, "code", None) or "COMPILE_FAILED",
                    "message": str(exc),
                })
                raise ApiError(
                    409, getattr(exc, "code", None) or "COMPILE_FAILED", str(exc)
                ) from None

            self._advance(task_id, "DATA_VALIDATING")
            candidate_path = attempt_dir / "candidate.litematic"
            try:
                # Gate 3 runs before the working copy is patched: the patch's
                # ``before`` values must be compared against the untouched
                # baseline, not against the states this compile just produced.
                guard.check_net_patch(result.patch, base_scene.snapshot.block_at_local)
            except WriteRejected as exc:
                self._fail_attempt(task_id, attempt_id, exc.to_dict())
                raise ApiError(409, exc.code, str(exc), exc.to_dict()) from None
            try:
                apply_patchset(base_scene, result.patch)
                save_scene(base_scene, str(candidate_path))
                reloaded = load_scene(str(candidate_path))
            except (UnsupportedFormatError, ValueError, OSError) as exc:
                self._fail_attempt(task_id, attempt_id, {
                    "code": "CANDIDATE_SERIALIZATION_FAILED", "message": str(exc)
                })
                raise ApiError(500, "CANDIDATE_SERIALIZATION_FAILED",
                               f"candidate could not be serialized: {exc}") from None
            self._advance(task_id, "CANDIDATE_SERIALIZED")
            self._advance(task_id, "READBACK_VALIDATING")
            try:
                stats = compare_to_expected(str(candidate_path), base_scene, result.patch)
                ensure_export_preserved(
                    self.load_revision_scene(request.base_revision_id), reloaded
                )
            except (AssertionError, UnsupportedFormatError, ValueError) as exc:
                self._fail_attempt(task_id, attempt_id, {
                    "code": "READBACK_VALIDATION_FAILED", "message": str(exc)
                })
                raise ApiError(409, "READBACK_VALIDATION_FAILED",
                               f"re-read verification failed: {exc}") from None

            changes = [c.__dict__ for c in result.patch]
            candidate_id = f"{task_id}-{attempt_id}"
            candidate = {
                "schema_version": SERVER_SCHEMA_VERSION,
                "candidate_id": candidate_id,
                "task_id": task_id,
                "attempt_id": attempt_id,
                "base_revision": request.base_revision_id,
                "file_sha256": sha256_file(candidate_path),
                "scene_hash": self.scene_hash(reloaded),
                "changes": len(result.patch),
                "stats": dict(result.stats),
                "verified": stats,
                "issues": list(result.issues),
                "diagnostics": list(result.diagnostics),
                "warnings": list(result.warnings),
                "status": "READY_FOR_USER",
                "accepted": False,
                # No review runner exists in this build (see CAPABILITIES), so the
                # record says so instead of implying an automated review happened.
                "review": None,
                "review_note": (
                    "no automated review ran: the candidate is handed to the user "
                    "for review; accept stays an explicit user action"
                ),
            }
            self.tasks.write_attempt_json(task_id, attempt_id, "candidate.json", candidate)
            self.tasks.write_attempt_json(task_id, attempt_id, "patch.json",
                                          {"changes": changes})
            self.tasks.write_attempt_json(task_id, attempt_id, "object_inputs.json", {
                "schema_version": SERVER_SCHEMA_VERSION,
                "inputs": _object_inputs_from_patch(changes, op_kinds),
            })
            self.tasks.append_write_log(task_id, attempt_id,
                                        [e.to_dict() for e in result.events])
            self.tasks.update_attempt(task_id, record, state="READY_FOR_USER",
                                      candidate=candidate,
                                      candidate_path=str(candidate_path),
                                      errors=[])
            # The state machine requires READY_FOR_USER via the manual-review
            # path, which is exactly right here: the user is the reviewer.
            self._advance(task_id, "NEEDS_MANUAL_REVIEW")
            self._advance(task_id, "READY_FOR_USER")
        self.store.log("attempts", {
            "action": "compiled", "task_id": task_id, "attempt_id": attempt_id,
            "candidate_id": candidate_id, "changes": candidate["changes"],
        })
        return {
            "schema_version": SERVER_SCHEMA_VERSION,
            "task_id": task_id,
            "attempt_id": attempt_id,
            "candidate_id": candidate_id,
            "state": self._task_state(task_id).get("state"),
            "candidate": candidate,
            "counts": candidate["stats"],
            "write_log_lines": len(result.events),
        }

    def _fail_attempt(self, task_id: str, attempt_id: str, error: dict) -> None:
        """Record a failed attempt; the attempt products stay untouched."""
        record = self._read_attempt(task_id, attempt_id)
        self.tasks.write_attempt_json(task_id, attempt_id, "error.json", error)
        self.tasks.update_attempt(task_id, record, state="FAILED", errors=[error])
        state = self._task_state(task_id).get("state")
        for step in ("FAILED",):
            try:
                assert_transition(state, step)
            except RedesignError:
                continue
            state = self.tasks.set_state(task_id, step).get("state")
        self.store.log("attempts", {
            "action": "failed", "task_id": task_id, "attempt_id": attempt_id,
            "code": error.get("code"), "message": error.get("message"),
        })

    # -- candidates ------------------------------------------------------

    def candidate_detail(self, candidate_id: str) -> dict:
        paths = self.candidate_paths(candidate_id)
        if not paths["record"].is_file():
            raise ApiError(404, "UNKNOWN_CANDIDATE", f"candidate {candidate_id!r} has no record")
        try:
            payload = json.loads(paths["record"].read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ApiError(500, "CANDIDATE_RECORD_UNREADABLE", str(exc)) from None
        payload = dict(payload)
        payload["urls"] = {
            "diff": f"/api/candidates/{candidate_id}/diff",
            "render_scene": f"/api/candidates/{candidate_id}/render-scene",
            "accept": f"/api/candidates/{candidate_id}/accept",
        }
        payload["file_present"] = paths["scene"].is_file()
        return payload

    def candidate_diff(self, candidate_id: str) -> dict:
        """The net change of the candidate against its frozen base revision."""
        paths = self.candidate_paths(candidate_id)
        if not paths["patch"].is_file():
            raise ApiError(404, "CANDIDATE_NOT_COMPILED",
                           f"candidate {candidate_id!r} has no net patch")
        try:
            payload = json.loads(paths["patch"].read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ApiError(500, "CANDIDATE_PATCH_UNREADABLE", str(exc)) from None
        changes = payload.get("changes") if isinstance(payload, Mapping) else payload
        if not isinstance(changes, list):
            raise ApiError(500, "CANDIDATE_PATCH_UNREADABLE",
                           "patch.json does not hold a list of changes")
        record: dict = {}
        if paths["record"].is_file():
            try:
                record = json.loads(paths["record"].read_text(encoding="utf-8"))
            except ValueError:
                record = {}
        counts = {"added": 0, "removed": 0, "replaced": 0, "state_only": 0}
        net_changes: List[dict] = []
        for change in changes:
            if not isinstance(change, Mapping):
                continue
            pos = change.get("pos_local")
            if not isinstance(pos, (list, tuple)) or len(pos) != 3:
                continue
            category = classify_change(change.get("before"), change.get("after"))
            if category is None:
                continue
            before = parse_state(change.get("before"))
            after = parse_state(change.get("after"))
            counts[category] += 1
            net_changes.append({
                "pos_local": [int(pos[0]), int(pos[1]), int(pos[2])],
                "before": before,
                "after": after,
                "category": category,
                "op_id": change.get("op_id"),
                "region_id": change.get("region_id"),
                "action": change.get("action"),
            })
        write_log_lines = 0
        if paths["write_log"].is_file():
            with open(paths["write_log"], "r", encoding="utf-8") as handle:
                write_log_lines = sum(1 for _ in handle)
        return {
            "schema_version": SERVER_SCHEMA_VERSION,
            "candidate_id": candidate_id,
            "task_id": paths["task_id"],
            "attempt_id": paths["attempt_id"],
            "base_revision": record.get("base_revision"),
            "scene_hash": record.get("scene_hash"),
            "file_sha256": record.get("file_sha256"),
            "counts": counts,
            "net_changes": net_changes,
            "net_change_total": len(net_changes),
            "write_events": write_log_lines,
            "stats": record.get("stats"),
            "note": (
                "net per-coordinate change against the task's frozen base revision; "
                "same coordinate never appears twice"
            ),
        }

    def accept_candidate(self, candidate_id: str, body: Mapping) -> dict:
        """The explicit user action: CAS-commit the candidate as a new revision."""
        paths = self.candidate_paths(candidate_id)
        task_id, attempt_id = paths["task_id"], paths["attempt_id"]
        if not paths["scene"].is_file():
            raise ApiError(409, "CANDIDATE_NOT_READY",
                           f"candidate {candidate_id!r} has no serialized scene to accept")
        record = self._read_attempt(task_id, attempt_id)
        candidate = record.candidate or {}
        expected_head = _require(body, "expected_head")
        expected_generation = _require_int(_require(body, "expected_generation"),
                                          "expected_generation")
        expected_config_hash = body.get("expected_config_hash")
        request = self._task_request(task_id)

        with self._mutation("accept the candidate"):
            head = self._head()
            state = self._task_state(task_id).get("state")
            if state == "ACCEPTED":
                raise ApiError(409, "ALREADY_ACCEPTED",
                               f"task {task_id!r} was already accepted")
            if expected_config_hash is not None and str(expected_config_hash) != head.config_hash:
                raise ApiError(409, "CONFIG_HASH_MISMATCH",
                               "the project config changed after the candidate was frozen",
                               {"expected_config_hash": str(expected_config_hash),
                                "actual_config_hash": head.config_hash})
            self._assert_task_fresh(request)
            expected_sha = candidate.get("file_sha256")
            actual_sha = sha256_file(paths["scene"])
            if expected_sha and actual_sha != expected_sha:
                raise ApiError(409, "CANDIDATE_FILE_CHANGED",
                               "candidate.litematic is not the file that was compiled",
                               {"expected_sha256": expected_sha, "actual_sha256": actual_sha})
            if not paths["patch"].is_file():
                raise ApiError(409, "CANDIDATE_NOT_READY",
                               f"candidate {candidate_id!r} has no net patch to commit")
            try:
                patch = json.loads(paths["patch"].read_text(encoding="utf-8"))["changes"]
            except (ValueError, KeyError, TypeError) as exc:
                raise ApiError(500, "CANDIDATE_PATCH_UNREADABLE", str(exc)) from None

            # Replacement preconditions are re-checked against the *current*
            # registry before anything is withdrawn.
            if request.mode == "replace_generated":
                self._validate_targets(head.revision_id, request.selection, request.target_ids)

            # The state machine is consulted before the commit so an illegal
            # transition cannot leave a committed revision with no task record.
            bridge: Tuple[str, ...] = ()
            if state == "READY_FOR_USER":
                bridge = ()
            elif state in ("AGENT_REVIEW", "VERIFYING_FINDINGS", "NEEDS_MANUAL_REVIEW"):
                bridge = ("READY_FOR_USER",)
            else:
                raise ApiError(409, "INVALID_TASK_STATE",
                               f"task {task_id!r} is {state!r}; accept is only possible "
                               f"for a candidate that is ready for the user")

            new_revision = self.store.next_revision_id()
            registry = self._registry(head.revision_id)
            objects_payload = self._objects_for_accept(
                registry, paths, request, new_revision, task_id, attempt_id
            )
            try:
                new_head = self.store.commit_revision(
                    scene_path=paths["scene"],
                    patch=patch,
                    objects=objects_payload,
                    expected_head=expected_head,
                    expected_generation=expected_generation,
                    task_id=task_id,
                    manifest_extra={"candidate_id": candidate_id, "attempt_id": attempt_id},
                )
            except StaleHeadError as exc:
                raise ApiError(409, "STALE_HEAD", str(exc), exc.to_dict()) from None
            except ProjectStoreError as exc:
                raise ApiError(409, "COMMIT_REJECTED", str(exc), exc.to_dict()) from None

            self.tasks.update_attempt(task_id, record, state="ACCEPTED")
            self._advance(task_id, "ACCEPTED", via=bridge)
            manifest = self.store.load_manifest(new_head.revision_id)
        self.store.log("accepts", {
            "action": "accepted", "candidate_id": candidate_id, "task_id": task_id,
            "attempt_id": attempt_id, "revision_id": new_head.revision_id,
            "generation": new_head.generation, "patch_count": len(patch),
        })
        return {
            "schema_version": SERVER_SCHEMA_VERSION,
            "candidate_id": candidate_id,
            "task_id": task_id,
            "attempt_id": attempt_id,
            "accepted": True,
            "revision_id": new_head.revision_id,
            "head": new_head.to_dict(),
            "previous_head": head.to_dict(),
            "manifest": manifest,
            "patch_count": len(patch),
            "objects_count": manifest.get("objects_count"),
            "task_state": self._task_state(task_id).get("state"),
        }

    def _objects_for_accept(self, registry: ObjectRegistry, paths: Mapping,
                            request: TaskRequest, new_revision: str,
                            task_id: str, attempt_id: str) -> dict:
        """Base registry + the objects this attempt created (provenance kept)."""
        records = {oid: registry.get(oid) for oid in registry.ids}
        inputs_payload: dict = {}
        if Path(paths["objects"]).is_file():
            try:
                inputs_payload = json.loads(Path(paths["objects"]).read_text(encoding="utf-8"))
            except ValueError:
                inputs_payload = {}
        for entry in inputs_payload.get("inputs") or []:
            if not isinstance(entry, Mapping):
                continue
            op_id = str(entry.get("op_id") or "unattributed")
            object_id = f"{op_id}@{new_revision}"
            if object_id in records:
                continue
            try:
                records[object_id] = record_from_compile(
                    object_id=object_id,
                    kind=entry.get("kind") or "asset",
                    creation_revision=new_revision,
                    substrate={str(k): str(v) for k, v in (entry.get("substrate") or {}).items()},
                    owned={str(k): str(v) for k, v in (entry.get("owned") or {}).items()},
                    occupied_voxels=entry.get("occupied_voxels") or (),
                    operation_ids=entry.get("operation_ids") or (),
                )
            except ObjectError as exc:
                raise ApiError(500, "OBJECT_RECORD_INVALID",
                               f"attempt {task_id}-{attempt_id}: {exc}") from None
        return ObjectRegistry(records).to_dict()

    # -- undo / redo -----------------------------------------------------

    def _history_transition(self, action: str, body: Mapping) -> dict:
        expected_head = _require(body, "expected_head")
        expected_generation = _require_int(_require(body, "expected_generation"),
                                          "expected_generation")
        with self._mutation(f"move the project history ({action})"):
            previous = self._head()
            try:
                if action == "undo":
                    new_head = self.store.undo(expected_head=expected_head,
                                               expected_generation=expected_generation)
                else:
                    new_head = self.store.redo(expected_head=expected_head,
                                               expected_generation=expected_generation)
            except StaleHeadError as exc:
                raise ApiError(409, "STALE_HEAD", str(exc), exc.to_dict()) from None
            except ProjectStoreError as exc:
                code = "NOTHING_TO_REDO" if action == "redo" else "NOTHING_TO_UNDO"
                raise ApiError(409, code, str(exc), exc.to_dict()) from None
            redo_stack: List[str] = []
            try:
                redo_stack = self.store.redo_stack
            except ProjectStoreError:
                redo_stack = []
        self.store.log("revisions", {
            "action": action, "from_revision": previous.revision_id,
            "to_revision": new_head.revision_id, "generation": new_head.generation,
        })
        return {
            "schema_version": SERVER_SCHEMA_VERSION,
            "action": action,
            "accepted": False,  # a history move is not an accept
            "previous_head": previous.to_dict(),
            "head": new_head.to_dict(),
            "redo_stack": redo_stack,
            "render_scene_url": (
                f"/api/projects/{self.project_id}/revisions/"
                f"{new_head.revision_id}/render-scene"
            ),
        }

    def undo(self, body: Mapping) -> dict:
        return self._history_transition("undo", body)

    def redo(self, body: Mapping) -> dict:
        return self._history_transition("redo", body)

    def scene_summary(self, revision_id: str, crop_origin, crop_size) -> dict:
        """A lightweight summary of a RenderScene (no voxel arrays)."""
        payload = self.render_scene_for_revision(revision_id, crop_origin, crop_size)
        return summarize_for_client(payload)


# --------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    server_version = "litegarden/0.2"
    service: ProjectService  # injected by build_server

    # -- logging ---------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:
        if os.environ.get("LITEGARDEN_HTTP_DEBUG"):
            super().log_message(fmt, *args)

    # -- plumbing --------------------------------------------------------

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".mjs": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".map": "application/json; charset=utf-8",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".svg": "image/svg+xml",
            ".ico": "image/x-icon",
            ".wasm": "application/wasm",
            ".woff2": "font/woff2",
            ".txt": "text/plain; charset=utf-8",
            ".md": "text/markdown; charset=utf-8",
        }.get(path.suffix.lower(), "application/octet-stream")
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            self.close_connection = True
            raise ApiError(413, "BODY_TOO_LARGE",
                           f"request body larger than {MAX_BODY_BYTES} bytes")
        raw = self.rfile.read(length) if length else b""
        text = raw.decode("utf-8", errors="strict") if raw else ""
        if not text.strip():
            return {}
        try:
            data = json.loads(text)
        except ValueError:
            raise ApiError(400, "INVALID_JSON", "request body is not valid JSON") from None
        if not isinstance(data, dict):
            raise ApiError(400, "INVALID_JSON", "request body must be a JSON object")
        return data

    # -- guards ----------------------------------------------------------

    def _check_client(self) -> None:
        peer = self.client_address[0] if self.client_address else ""
        if peer not in LOOPBACK_HOSTS and peer not in ("::ffff:127.0.0.1",):
            raise ApiError(403, "FORBIDDEN_CLIENT",
                           "this service is loopback-only")
        host = (self.headers.get("Host") or "").strip()
        if not host:
            raise ApiError(403, "BAD_HOST", "a Host header is required")
        hostname = host
        if hostname.startswith("["):  # [::1]:8765
            hostname = hostname.split("]", 1)[0] + "]"
        elif ":" in hostname:
            hostname = hostname.rsplit(":", 1)[0]
        if hostname.lower() not in LOOPBACK_HOSTS:
            raise ApiError(403, "BAD_HOST",
                           f"Host {host!r} is not a loopback host")

    def _check_origin(self) -> None:
        """A cross-site page may reach 127.0.0.1; it must not reach this API."""
        for header in ("Origin", "Referer"):
            value = self.headers.get(header)
            if not value:
                continue
            parsed = urllib.parse.urlparse(value)
            hostname = (parsed.hostname or "").lower()
            if hostname in ("", "null") and header == "Origin" and value.strip() == "null":
                raise ApiError(403, "FORBIDDEN_ORIGIN",
                               "an opaque Origin is not the served origin")
            if hostname and hostname not in LOOPBACK_HOSTS:
                raise ApiError(403, "FORBIDDEN_ORIGIN",
                               f"{header} {value!r} is not the served loopback origin")

    def _check_token(self) -> None:
        config = self.service.config
        if not config.require_token:
            return
        provided = self.headers.get("X-Litegarden-Token") or ""
        if not provided or not secrets.compare_digest(provided, config.token):
            raise ApiError(
                403, "FORBIDDEN_TOKEN",
                "mutating requests must carry X-Litegarden-Token with the run token "
                "from GET /api/session",
            )

    def _safe_static_path(self, root: Path, relative: str) -> Path:
        """Resolve ``relative`` under ``root`` or refuse it (no traversal)."""
        raw = urllib.parse.unquote(relative).replace("\\", "/")
        if "\x00" in raw:
            raise ApiError(403, "PATH_ESCAPE", "path contains a NUL byte")
        parts = [p for p in raw.split("/") if p not in ("", ".")]
        if any(p == ".." for p in parts):
            raise ApiError(403, "PATH_ESCAPE", "path traversal is refused")
        base = Path(root).resolve()
        target = base.joinpath(*parts).resolve() if parts else base
        if target != base and not target.is_relative_to(base):
            raise ApiError(403, "PATH_ESCAPE", "resolved path leaves the served root")
        return target

    def _serve_static(self, path: str) -> None:
        config = self.service.config
        mounts = {"web": Path(config.web_dir)}
        if config.work_dir is not None:
            mounts["work"] = Path(config.work_dir)
        relative = path.strip("/")
        if relative in ("", "index.html"):
            index = Path(config.web_dir) / "index.html"
            if index.is_file():
                self._send_file(index)
                return
            raise ApiError(404, "NOT_FOUND", "web/index.html was not found")
        segments = relative.split("/")
        mount = segments[0]
        if mount in mounts:
            target = self._safe_static_path(mounts[mount], "/".join(segments[1:]))
            if target.is_file():
                self._send_file(target)
                return
            raise ApiError(404, "NOT_FOUND", f"{path} was not found")
        # Anything else is served from web_dir, so the workbench also works when
        # it is opened at "/" (its assets resolve relative to the page) - the
        # lookup stays inside web_dir, so no other repository file is reachable.
        target = self._safe_static_path(Path(config.web_dir), relative)
        if target.is_file():
            self._send_file(target)
            return
        raise ApiError(404, "NOT_FOUND", f"{path} was not found")

    # -- dispatch --------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        try:
            parsed = urllib.parse.urlparse(self.path)
            path = urllib.parse.unquote(parsed.path)
            query = urllib.parse.parse_qs(parsed.query)
            self._check_client()
            self._check_origin()
            if method not in ("GET", "HEAD"):
                self._check_token()
            if path == "/" or not path.startswith(f"/{API_PREFIX}/"):
                if method != "GET":
                    raise ApiError(405, "METHOD_NOT_ALLOWED",
                                   f"{method} is not allowed on {path}")
                self._serve_static(path)
                return
            segments = [s for s in path.strip("/").split("/") if s]
            self._route(method, segments, query)
        except ApiError as exc:
            self._send_json(exc.to_dict(), exc.status)
        except RedesignError as exc:
            self._send_json({"error": {"code": exc.code, "message": str(exc),
                                       "detail": _sanitise(exc.detail), "status": 409}}, 409)
        except StaleHeadError as exc:
            self._send_json({"error": {"code": "STALE_HEAD", "message": str(exc),
                                       "detail": _sanitise(exc.to_dict()), "status": 409}}, 409)
        except IncompleteRevisionError as exc:
            self._send_json({"error": {"code": "INCOMPLETE_REVISION", "message": str(exc),
                                       "detail": _sanitise(exc.to_dict()), "status": 500}}, 500)
        except ProjectStoreError as exc:
            self._send_json({"error": {"code": "PROJECT_STORE_ERROR", "message": str(exc),
                                       "detail": _sanitise(exc.to_dict()), "status": 409}}, 409)
        except (TargetOwnershipConflict, TargetDependencyConflict, ObjectError) as exc:
            self._send_json({"error": {"code": object_error_code(exc),
                                       "message": str(exc),
                                       "detail": _sanitise(exc.to_dict()), "status": 409}}, 409)
        except WriteRejected as exc:
            self._send_json({"error": {"code": exc.code, "message": str(exc),
                                       "detail": _sanitise(exc.to_dict()), "status": 409}}, 409)
        except RenderSceneError as exc:
            self._send_json({"error": {"code": getattr(exc, "code", "RENDER_SCENE_INVALID"),
                                       "message": str(exc),
                                       "detail": _sanitise(exc.to_dict()), "status": 500}}, 500)
        except Exception as exc:  # noqa: BLE001 - never leak a traceback
            self._send_json({"error": {"code": "INTERNAL_ERROR",
                                       "message": f"{type(exc).__name__}: {exc}",
                                       "detail": {}, "status": 500}}, 500)

    # -- routes ----------------------------------------------------------

    def _route(self, method: str, segments: List[str], query: Mapping) -> None:
        # segments[0] == "api"
        rest = segments[1:]
        if not rest:
            raise ApiError(404, "UNKNOWN_ROUTE", "no such API route")
        service = self.service
        crop_origin = _ints(query.get("crop_origin"))
        crop_size = _ints(query.get("crop_size"))

        if method == "GET":
            if rest == ["session"]:
                self._send_json(service.session_info())
                return
            if rest == ["project"]:
                self._send_json(service.project_detail())
                return
            if len(rest) >= 2 and rest[0] == "projects":
                project_id = rest[1]
                service._check_project_id(project_id)
                tail = rest[2:]
                if not tail:
                    self._send_json(service.project_detail())
                    return
                if (len(tail) == 3 and tail[0] == "revisions"
                        and tail[2] == "render-scene"):
                    # ``?summary=1`` returns the lightweight view (no voxel
                    # arrays) so a panel or log can read it cheaply.
                    if "summary" in query:
                        self._send_json(service.scene_summary(
                            tail[1], crop_origin, crop_size))
                    else:
                        self._send_json(service.render_scene_for_revision(
                            tail[1], crop_origin, crop_size))
                    return
                raise ApiError(404, "UNKNOWN_ROUTE", "no such project route")
            if len(rest) >= 2 and rest[0] == "tasks":
                task_id = rest[1]
                tail = rest[2:]
                if not tail:
                    self._send_json(service.task_detail(task_id))
                    return
                if len(tail) == 2 and tail[0] == "attempts":
                    detail = service.task_detail(task_id)
                    for attempt in detail["attempts"]:
                        if attempt["attempt_id"] == tail[1]:
                            self._send_json({
                                "schema_version": SERVER_SCHEMA_VERSION,
                                "task_id": task_id,
                                "state": detail["state"],
                                "attempt": attempt,
                            })
                            return
                    raise ApiError(404, "UNKNOWN_ATTEMPT",
                                   f"unknown attempt {tail[1]!r} of task {task_id!r}")
                raise ApiError(404, "UNKNOWN_ROUTE", "no such task route")
            if len(rest) >= 2 and rest[0] == "candidates":
                candidate_id = rest[1]
                tail = rest[2:]
                if not tail:
                    self._send_json(service.candidate_detail(candidate_id))
                    return
                if tail == ["diff"]:
                    self._send_json(service.candidate_diff(candidate_id))
                    return
                if tail == ["render-scene"]:
                    self._send_json(service.candidate_render_scene(
                        candidate_id, crop_origin, crop_size))
                    return
                raise ApiError(404, "UNKNOWN_ROUTE", "no such candidate route")
            raise ApiError(404, "UNKNOWN_ROUTE", "no such API route")

        if method != "POST":
            raise ApiError(405, "METHOD_NOT_ALLOWED",
                           f"{method} is not allowed on this API route")

        body = self._body()
        if rest == ["selections", "validate"]:
            self._send_json(service.validate_selection(body))
            return
        if len(rest) >= 2 and rest[0] == "projects":
            project_id = rest[1]
            service._check_project_id(project_id)
            tail = rest[2:]
            if tail == ["selections", "validate"]:
                self._send_json(service.validate_selection(body))
                return
            if tail in (["tasks"], ["redesign-tasks"]):
                self._send_json(service.create_task(body), 201)
                return
            if tail == ["undo"]:
                self._send_json(service.undo(body))
                return
            if tail == ["redo"]:
                self._send_json(service.redo(body))
                return
            raise ApiError(404, "UNKNOWN_ROUTE", "no such project route")
        if len(rest) == 3 and rest[0] == "tasks" and rest[2] == "attempts":
            self._send_json(service.submit_attempt(rest[1], body), 201)
            return
        if len(rest) == 3 and rest[0] == "candidates" and rest[2] == "accept":
            self._send_json(service.accept_candidate(rest[1], body))
            return
        raise ApiError(404, "UNKNOWN_ROUTE", "no such API route")


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def build_server(config: ServerConfig) -> _Server:
    """Bind the local service; refuse a non-loopback host unless opted in."""
    if not config.allow_remote and config.host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError(
            f"refusing to bind {config.host!r}: the workbench service is loopback-only "
            "unless allow_remote is set explicitly"
        )
    assets_dir = Path(config.assets_dir)
    for required in ("catalog.json", "palettes.json"):
        if not (assets_dir / required).is_file():
            raise ValueError(
                f"{assets_dir / required} is missing: the compiler cannot be run "
                "without the verified asset catalog, so nothing would be checked. "
                "Point --assets at the project's assets directory."
            )
    service = ProjectService(config)
    handler = type("BoundHandler", (_Handler,), {"service": service})
    server = _Server((config.host, config.port), handler)
    server.service = service  # type: ignore[attr-defined]
    return server


def serve_forever(config: ServerConfig) -> None:
    """Run until interrupted, printing the workbench URL and the run token."""
    server = build_server(config)
    host, port = server.server_address[:2]
    config.port = int(port)
    # ``flush=True``: the URL and the token must be visible immediately even when
    # stdout is redirected to a file or a pipe.
    print(f"litegarden workbench: http://{host}:{port}/web/index.html", flush=True)
    print(f"  scenes:  http://{host}:{port}/work/sample_render_scene.json", flush=True)
    print(f"  session: http://{host}:{port}/api/session  (run token lives here)",
          flush=True)
    print(f"  project: {config.project_dir}", flush=True)
    print(f"  token:   {config.token if config.require_token else '(disabled)'}",
          flush=True)
    print("  accept is an explicit user action: POST /api/candidates/<c>/accept",
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        server.server_close()


__all__ = [
    "API_PREFIX",
    "CAPABILITIES",
    "SERVER_SCHEMA_VERSION",
    "ApiError",
    "ProjectService",
    "ServerConfig",
    "build_server",
    "classify_change",
    "default_assets_dir",
    "parse_state",
    "serve_forever",
]
