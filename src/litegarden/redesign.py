"""Local redesign tasks: frozen authorisation, halo, boundary contracts.

A local task is the only thing that grants write permission for an area of the
scene. Everything that decides what a task may touch is derived here, on the
server side:

- the selection is the user's confirmed box and becomes ``task_authorized``;
- the context halo is a *read-only* neighbourhood (no Blend Band by default, so
  nothing outside the selection can ever be written);
- partial intersections with known generated objects may only be resolved by the
  user (expand the selection or keep the object) - never by silently expanding;
- boundary contracts record the existing outer connections (roads and anchors)
  that a task must preserve, and mark anything that cannot be determined from
  the input as ``pending`` instead of pretending to recognise it.

Bounds use half-open ``[min, max_exclusive)`` with an explicit ``schema_version``;
a legacy inclusive box is only accepted through :func:`migrate_selection`, which
converts it deliberately instead of silently changing its meaning.

The Agent never gets to widen any of this: the frozen :class:`TaskRequest` is
written by the server from user-confirmed inputs, and a plan cannot modify it.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .constraints import Box3, parse_box

Vec3 = Tuple[int, int, int]

SCHEMA_VERSION = "0.2"
DEFAULT_HALO_XZ = 12
TASK_DIR_PREFIX = "tasks"


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------


class RedesignError(RuntimeError):
    """A redesign request that cannot be honoured; ``code`` names the rule."""

    def __init__(self, code: str, message: str, detail: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.detail = detail or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": str(self), "detail": self.detail}


INVALID_SELECTION = "INVALID_SELECTION"
SELECTION_OUT_OF_BOUNDS = "SELECTION_OUT_OF_BOUNDS"
LEGACY_BOUNDS_NEEDS_MIGRATION = "LEGACY_BOUNDS_NEEDS_MIGRATION"
PARTIAL_OBJECT_IN_SELECTION = "PARTIAL_OBJECT_IN_SELECTION"
TARGET_OWNERSHIP_CONFLICT = "TARGET_OWNERSHIP_CONFLICT"
TARGET_DEPENDENCY_CONFLICT = "TARGET_DEPENDENCY_CONFLICT"
STALE_BASE_REVISION = "STALE_BASE_REVISION"
STALE_HEAD_GENERATION = "STALE_HEAD_GENERATION"
CONFIG_HASH_MISMATCH = "CONFIG_HASH_MISMATCH"
INVALID_TASK_STATE = "INVALID_TASK_STATE"
ATTEMPT_LIMIT_REACHED = "ATTEMPT_LIMIT_REACHED"
MISSING_REQUIRED_FIELD = "MISSING_REQUIRED_FIELD"


def _require_int(value, what: str) -> int:
    """Strict integer: bool, float and str are refused, never coerced."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise RedesignError(
            MISSING_REQUIRED_FIELD, f"{what} must be an integer, got {value!r}"
        )
    return value


def _require_str(value, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RedesignError(
            MISSING_REQUIRED_FIELD, f"{what} must be a non-empty string, got {value!r}"
        )
    return value


# --------------------------------------------------------------------------
# selection
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Selection:
    """The user-confirmed editable volume, half-open in project-local space."""

    min: Vec3
    max_exclusive: Vec3
    space: str = "project_local"
    schema_version: str = SCHEMA_VERSION

    @property
    def box(self) -> Box3:
        return Box3(self.min, self.max_exclusive)

    @property
    def size(self) -> Vec3:
        return self.box.size

    @property
    def volume(self) -> int:
        return self.box.volume()

    def contains(self, p: Vec3) -> bool:
        return self.box.contains(p)

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "space": self.space,
            "min": list(self.min),
            "max_exclusive": list(self.max_exclusive),
            "size": list(self.size),
            "volume": self.volume,
        }

    @staticmethod
    def from_dict(raw: Mapping, *, bounds: Optional[Box3] = None) -> "Selection":
        """Parse a half-open selection; a legacy box is refused, not guessed."""
        if not isinstance(raw, Mapping):
            raise RedesignError(INVALID_SELECTION, f"selection must be an object, got {raw!r}")
        if "min" not in raw or "max_exclusive" not in raw:
            raise RedesignError(
                LEGACY_BOUNDS_NEEDS_MIGRATION,
                "selection must use half-open min / max_exclusive; a legacy box "
                "must be converted explicitly with migrate_selection()",
            )
        try:
            box = parse_box(
                {"min": raw["min"], "max_exclusive": raw["max_exclusive"]}, "selection"
            )
        except ValueError as exc:
            raise RedesignError(INVALID_SELECTION, f"selection: {exc}") from None
        space = str(raw.get("space", "project_local"))
        if space != "project_local":
            raise RedesignError(
                INVALID_SELECTION, f"selection space must be 'project_local', got {space!r}"
            )
        selection = Selection(box.min, box.max_exclusive, space)
        if bounds is not None and not (
            bounds.contains(box.min)
            and all(box.max_exclusive[i] <= bounds.max_exclusive[i] for i in range(3))
        ):
            raise RedesignError(
                SELECTION_OUT_OF_BOUNDS,
                f"selection {selection.to_dict()} is not inside the scene bounds",
                {"selection": selection.to_dict(), "scene_bounds": bounds.to_dict()},
            )
        return selection


def migrate_selection(raw: Mapping, *, bounds: Optional[Box3] = None) -> Selection:
    """Convert a *legacy inclusive* box into a half-open selection.

    Old configs described endpoints as inclusive (``min`` .. ``max``) or as a 2D
    ``bbox`` with an inclusive corner. Converting is deliberate and recorded:
    the caller must ask for it, and the result is a half-open box, so existing
    tests keep their meaning instead of silently shifting by one voxel.
    """
    if not isinstance(raw, Mapping):
        raise RedesignError(INVALID_SELECTION, f"selection must be an object, got {raw!r}")
    if "min" in raw and "max_exclusive" in raw:
        return Selection.from_dict(raw, bounds=bounds)
    if "min" in raw and "max" in raw:
        lo = tuple(_require_int(v, f"selection.min[{i}]") for i, v in enumerate(raw["min"]))
        hi = tuple(_require_int(v, f"selection.max[{i}]") for i, v in enumerate(raw["max"]))
        if len(lo) != 3 or len(hi) != 3:
            raise RedesignError(
                INVALID_SELECTION, "legacy selection needs 3-coordinate min/max"
            )
        box = Box3.from_inclusive(lo, hi)
    elif "bbox" in raw:
        bb = raw["bbox"]
        if not isinstance(bb, (list, tuple)) or len(bb) != 4:
            raise RedesignError(INVALID_SELECTION, "legacy bbox must be [x0, z0, x1, z1]")
        bb = [_require_int(v, f"selection.bbox[{i}]") for i, v in enumerate(bb)]
        y0 = _require_int(raw["min_y"], "selection.min_y")
        y1 = _require_int(raw["max_y"], "selection.max_y")
        box = Box3((bb[0], y0, bb[1]), (bb[2] + 1, y1 + 1, bb[3] + 1))
    else:
        raise RedesignError(
            INVALID_SELECTION,
            "selection needs min/max_exclusive or a legacy min/max or bbox",
        )
    if any(box.size[i] <= 0 for i in range(3)):
        raise RedesignError(INVALID_SELECTION, f"selection is empty: {box.to_dict()}")
    return Selection.from_dict(
        {"min": list(box.min), "max_exclusive": list(box.max_exclusive)}, bounds=bounds
    )

@dataclass(frozen=True)
class ContextHalo:
    """Read-only neighbourhood of the selection (spec 11.1)."""

    selection: Selection
    halo_xz: int
    box: Box3

    def to_dict(self) -> dict:
        return {
            "halo_xz": self.halo_xz,
            "box": self.box.to_dict(),
            "read_only": True,
            "blend_band": None,
            "note": (
                "context halo is read-only; this round has no Blend Band, so no "
                "write outside the selection is ever authorised"
            ),
        }


@dataclass(frozen=True)
class BoundaryInterface:
    """An existing outer connection the task must keep working (spec 11.5)."""

    interface_id: str
    object_id: Optional[str]
    cells: Tuple[Vec3, ...]
    direction: str
    width: int
    landing_y: int
    link_from: Optional[Vec3] = None
    link_to: Optional[Vec3] = None
    status: str = "confirmed"  # "confirmed" | "pending"
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "interface_id": self.interface_id,
            "object_id": self.object_id,
            "cells": [list(c) for c in self.cells],
            "direction": self.direction,
            "width": self.width,
            "landing_y": self.landing_y,
            "link_from": list(self.link_from) if self.link_from else None,
            "link_to": list(self.link_to) if self.link_to else None,
            "status": self.status,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class SelectionReport:
    """What the user is shown before a task is frozen (spec 9.2)."""

    selection: Selection
    halo: ContextHalo
    protected_overlap: Tuple[Box3, ...] = ()
    partial_objects: Tuple[dict, ...] = ()
    boundary_interfaces: Tuple[BoundaryInterface, ...] = ()
    anchor_inside: Tuple[str, ...] = ()
    anchor_pending: Tuple[str, ...] = ()
    warnings: Tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "selection": self.selection.to_dict(),
            "halo": self.halo.to_dict(),
            "protected_overlap": [b.to_dict() for b in self.protected_overlap],
            "partial_objects": list(self.partial_objects),
            "boundary_interfaces": [i.to_dict() for i in self.boundary_interfaces],
            "anchor_inside": list(self.anchor_inside),
            "anchor_pending": list(self.anchor_pending),
            "warnings": list(self.warnings),
            "requires_user_decision": bool(self.partial_objects),
        }

    @property
    def blocked(self) -> bool:
        """A partial object means the user must decide before we can freeze."""
        return bool(self.partial_objects)


def halo_for(selection: Selection, halo_xz: int = DEFAULT_HALO_XZ) -> ContextHalo:
    halo_xz = _require_int(halo_xz, "context_halo_xz")
    if halo_xz < 0:
        raise RedesignError(INVALID_SELECTION, "context_halo_xz must be >= 0")
    lo = (selection.min[0] - halo_xz, selection.min[1], selection.min[2] - halo_xz)
    hi = (
        selection.max_exclusive[0] + halo_xz,
        selection.max_exclusive[1],
        selection.max_exclusive[2] + halo_xz,
    )
    return ContextHalo(selection=selection, halo_xz=halo_xz, box=Box3(lo, hi))


def _box_of_positions(positions: Iterable[Vec3]) -> Optional[Box3]:
    pts = list(positions)
    if not pts:
        return None
    return Box3.around(pts)


def analyze_selection(
    selection: Selection,
    *,
    scene_bounds: Box3,
    protected_boxes: Sequence[Box3] = (),
    objects: Optional[Mapping[str, object]] = None,
    anchors: Optional[Mapping[str, Sequence[int]]] = None,
    halo_xz: int = DEFAULT_HALO_XZ,
) -> SelectionReport:
    """Describe a candidate selection before it is frozen.

    ``objects`` is the object registry (any mapping of object_id -> record with
    ``occupied_voxels``/``footprint`` attributes or dict keys). An object that is
    only *partly* inside the selection is reported as a partial intersection:
    the user may expand the selection or keep the object, but nothing expands
    itself behind their back.
    """
    protected_overlap = tuple(b for b in protected_boxes if b.intersects(selection.box))
    partial: List[dict] = []
    interfaces: List[BoundaryInterface] = []

    for object_id, record in (objects or {}).items():
        positions = _object_positions(record)
        if not positions:
            continue
        obj_box = _box_of_positions(positions)
        if obj_box is None or not obj_box.intersects(selection.box):
            continue
        inside = [p for p in positions if selection.contains(p)]
        if not inside:
            continue
        if len(inside) < len(positions):
            partial.append({
                "object_id": object_id,
                "kind": _object_field(record, "kind", "unknown"),
                "inside_voxels": len(inside),
                "total_voxels": len(positions),
                "complete": bool(_object_field(record, "complete", False)),
                "resolution": "expand_selection_or_keep_object",
            })
        for link in _object_links(record):
            cells = tuple(tuple(c) for c in link.get("cells", []) or [])
            if not cells:
                continue
            interfaces.append(BoundaryInterface(
                interface_id=str(link.get("interface_id", f"{object_id}:boundary")),
                object_id=object_id,
                cells=cells,
                direction=str(link.get("direction", "unknown")),
                width=_require_int(link.get("width", 1), "boundary width"),
                landing_y=_require_int(link.get("landing_y", cells[0][1]), "landing_y"),
                link_from=tuple(link["link_from"]) if link.get("link_from") else None,
                link_to=tuple(link["link_to"]) if link.get("link_to") else None,
                status=str(link.get("status", "confirmed")),
                detail=str(link.get("detail", "")),
            ))

    anchor_inside: List[str] = []
    anchor_pending: List[str] = []
    for name, pos in (anchors or {}).items():
        cell = (int(pos[0]), int(pos[2]) if len(pos) > 2 else 0, 0)
        xz_inside = (
            selection.min[0] <= int(pos[0]) < selection.max_exclusive[0]
            and selection.min[2] <= int(pos[1]) < selection.max_exclusive[2]
        )
        if xz_inside:
            anchor_inside.append(str(name))
    warnings: List[str] = []
    if not anchor_inside:
        warnings.append(
            "no user-declared anchor lies inside the selection: outer road "
            "connections cannot be determined from the input alone and must be "
            "declared or marked pending"
        )
        anchor_pending.extend(sorted((anchors or {}).keys()))

    return SelectionReport(
        selection=selection,
        halo=halo_for(selection, halo_xz),
        protected_overlap=protected_overlap,
        partial_objects=tuple(partial),
        boundary_interfaces=tuple(interfaces),
        anchor_inside=tuple(sorted(anchor_inside)),
        anchor_pending=tuple(anchor_pending),
        warnings=tuple(warnings),
    )


def _object_positions(record) -> List[Vec3]:
    raw = _object_field(record, "occupied_voxels", None)
    if raw is None:
        raw = _object_field(record, "write_set", []) or []
    out: List[Vec3] = []
    for p in raw:
        if isinstance(p, (list, tuple)) and len(p) == 3:
            out.append((int(p[0]), int(p[1]), int(p[2])))
        elif isinstance(p, str):
            parts = p.split(",")
            if len(parts) == 3:
                out.append((int(parts[0]), int(parts[1]), int(parts[2])))
    return out


def _object_field(record, name: str, default):
    if isinstance(record, Mapping):
        return record.get(name, default)
    return getattr(record, name, default)


def _object_links(record) -> List[dict]:
    links = _object_field(record, "boundary_links", []) or []
    return [l for l in links if isinstance(l, Mapping)]


# --------------------------------------------------------------------------
# task state machine
# --------------------------------------------------------------------------


TASK_STATES = (
    "CREATED", "CONTEXT_READY", "WAITING_AGENT", "PLANNING", "PLAN_READY",
    "COMPILING", "DATA_VALIDATING", "CANDIDATE_SERIALIZED", "READBACK_VALIDATING",
    "RENDERING", "AGENT_REVIEW", "VERIFYING_FINDINGS", "READY_FOR_USER",
    "ACCEPTED", "REJECTED",
)
SIDE_STATES = ("FAILED", "CANCELED", "STALE", "NEEDS_MANUAL_REVIEW")
TERMINAL_STATES = ("ACCEPTED", "REJECTED", "CANCELED")

_ALLOWED: Dict[str, Tuple[str, ...]] = {
    "CREATED": ("CONTEXT_READY", "FAILED", "CANCELED"),
    "CONTEXT_READY": ("WAITING_AGENT", "PLANNING", "FAILED", "CANCELED"),
    "WAITING_AGENT": ("PLANNING", "PLAN_READY", "FAILED", "CANCELED", "STALE"),
    "PLANNING": ("PLAN_READY", "FAILED", "CANCELED", "STALE"),
    "PLAN_READY": ("COMPILING", "FAILED", "CANCELED", "STALE"),
    "COMPILING": ("DATA_VALIDATING", "FAILED", "CANCELED", "STALE", "NEEDS_MANUAL_REVIEW"),
    "DATA_VALIDATING": ("CANDIDATE_SERIALIZED", "FAILED", "NEEDS_MANUAL_REVIEW", "CANCELED"),
    "CANDIDATE_SERIALIZED": ("READBACK_VALIDATING", "FAILED", "NEEDS_MANUAL_REVIEW"),
    "READBACK_VALIDATING": ("RENDERING", "FAILED", "NEEDS_MANUAL_REVIEW"),
    "RENDERING": ("AGENT_REVIEW", "FAILED", "NEEDS_MANUAL_REVIEW", "CANCELED"),
    "AGENT_REVIEW": ("VERIFYING_FINDINGS", "FAILED", "NEEDS_MANUAL_REVIEW", "CANCELED"),
    "VERIFYING_FINDINGS": ("READY_FOR_USER", "FAILED", "NEEDS_MANUAL_REVIEW", "CANCELED"),
    "READY_FOR_USER": ("ACCEPTED", "REJECTED", "FAILED", "STALE", "CANCELED",
                       "NEEDS_MANUAL_REVIEW"),
    "ACCEPTED": (),
    "REJECTED": (),
    "FAILED": ("PLANNING", "CANCELED"),
    "CANCELED": (),
    "STALE": ("PLANNING", "CANCELED"),
    "NEEDS_MANUAL_REVIEW": ("AGENT_REVIEW", "READY_FOR_USER", "REJECTED", "CANCELED"),
}


def assert_transition(current: str, target: str) -> None:
    """Refuse an illegal state change; only the frozen machine may advance."""
    if current not in _ALLOWED:
        raise RedesignError(INVALID_TASK_STATE, f"unknown state {current!r}")
    if target not in TASK_STATES + SIDE_STATES:
        raise RedesignError(INVALID_TASK_STATE, f"unknown state {target!r}")
    if target not in _ALLOWED[current]:
        raise RedesignError(
            INVALID_TASK_STATE,
            f"illegal task transition {current} -> {target}",
            {"current": current, "target": target, "allowed": list(_ALLOWED[current])},
        )


# --------------------------------------------------------------------------
# frozen task request
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskRequest:
    """The frozen, server-owned description of one redesign task (spec 12.2)."""

    task_id: str
    project_id: str
    base_revision_id: str
    base_scene_hash: str
    head_generation: int
    config_revision: str
    config_hash: str
    selection: Selection
    mode: str
    instruction: str
    context_halo_xz: int = DEFAULT_HALO_XZ
    target_ids: Tuple[str, ...] = ()
    seed: int = 0
    created: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "project_id": self.project_id,
            "base_revision_id": self.base_revision_id,
            "base_scene_hash": self.base_scene_hash,
            "head_generation": self.head_generation,
            "config_revision": self.config_revision,
            "config_hash": self.config_hash,
            "selection": self.selection.to_dict(),
            "context_halo_xz": self.context_halo_xz,
            "mode": self.mode,
            "target_ids": list(self.target_ids),
            "instruction": self.instruction,
            "seed": self.seed,
            "created": self.created,
            "authorisation_note": (
                "selection and target_ids are frozen by the server from "
                "user-confirmed input; a plan cannot widen them"
            ),
        }

    @staticmethod
    def from_dict(raw: Mapping) -> "TaskRequest":
        selection = Selection.from_dict(raw["selection"])
        mode = _require_str(raw.get("mode", "revise_current"), "mode")
        if mode not in ("revise_current", "replace_generated"):
            raise RedesignError(INVALID_SELECTION, f"unknown mode {mode!r}")
        return TaskRequest(
            task_id=_require_str(raw.get("task_id"), "task_id"),
            project_id=_require_str(raw.get("project_id"), "project_id"),
            base_revision_id=_require_str(raw.get("base_revision_id"), "base_revision_id"),
            base_scene_hash=_require_str(raw.get("base_scene_hash"), "base_scene_hash"),
            head_generation=_require_int(raw.get("head_generation"), "head_generation"),
            config_revision=_require_str(raw.get("config_revision"), "config_revision"),
            config_hash=_require_str(raw.get("config_hash"), "config_hash"),
            selection=selection,
            mode=mode,
            instruction=str(raw.get("instruction", "")),
            context_halo_xz=_require_int(raw.get("context_halo_xz", DEFAULT_HALO_XZ),
                                         "context_halo_xz"),
            target_ids=tuple(str(t) for t in (raw.get("target_ids") or ())),
            seed=_require_int(raw.get("seed", 0), "seed"),
            created=str(raw.get("created", datetime.now(timezone.utc).isoformat())),
        )


def freeze_task(
    *,
    request_id: str,
    project_id: str,
    base_revision_id: str,
    base_scene_hash: str,
    head_generation: int,
    config_revision: str,
    config_hash: str,
    selection: Selection,
    mode: str,
    instruction: str,
    report: Optional[SelectionReport] = None,
    context_halo_xz: int = DEFAULT_HALO_XZ,
    target_ids: Sequence[str] = (),
    seed: int = 0,
) -> TaskRequest:
    """Freeze a task exactly as the user confirmed it.

    Refuses to freeze while a known object is only partly selected: the user has
    to decide (expand or keep) instead of the backend guessing, and nothing is
    written before that decision exists.
    """
    if mode not in ("revise_current", "replace_generated"):
        raise RedesignError(INVALID_SELECTION, f"unknown mode {mode!r}")
    if mode == "replace_generated" and not target_ids:
        raise RedesignError(
            INVALID_SELECTION,
            "replace_generated needs an explicit target_ids list of known objects",
        )
    if report is not None and report.partial_objects:
        raise RedesignError(
            PARTIAL_OBJECT_IN_SELECTION,
            "the selection cuts through known objects; expand the selection or keep "
            "the objects before freezing the task",
            {"partial_objects": list(report.partial_objects)},
        )
    if not isinstance(instruction, str) or not instruction.strip():
        raise RedesignError(MISSING_REQUIRED_FIELD, "instruction must not be empty")
    return TaskRequest(
        task_id=_require_str(request_id, "task_id"),
        project_id=_require_str(project_id, "project_id"),
        base_revision_id=_require_str(base_revision_id, "base_revision_id"),
        base_scene_hash=_require_str(base_scene_hash, "base_scene_hash"),
        head_generation=_require_int(head_generation, "head_generation"),
        config_revision=_require_str(config_revision, "config_revision"),
        config_hash=_require_str(config_hash, "config_hash"),
        selection=selection,
        mode=mode,
        instruction=instruction,
        context_halo_xz=_require_int(context_halo_xz, "context_halo_xz"),
        target_ids=tuple(str(t) for t in target_ids),
        seed=_require_int(seed, "seed"),
    )


def ensure_task_fresh(
    request: TaskRequest,
    *,
    head_revision_id: str,
    head_generation: int,
    config_hash: str,
    scene_hash: str,
) -> None:
    """Refuse a stale task: HEAD, generation, config or scene must still match."""
    if request.base_revision_id != head_revision_id:
        raise RedesignError(
            STALE_BASE_REVISION,
            f"task base {request.base_revision_id} != current HEAD {head_revision_id}",
        )
    if request.head_generation != head_generation:
        raise RedesignError(
            STALE_HEAD_GENERATION,
            f"task generation {request.head_generation} != HEAD generation {head_generation}",
        )
    if request.config_hash != config_hash:
        raise RedesignError(
            CONFIG_HASH_MISMATCH, "the frozen config hash no longer matches the project config"
        )
    if request.base_scene_hash != scene_hash:
        raise RedesignError(
            STALE_BASE_REVISION, "the frozen base scene hash no longer matches the scene"
        )


# --------------------------------------------------------------------------
# task storage
# --------------------------------------------------------------------------


_ATTEMPT_RE = re.compile(r"^a(\d{3,})$")


@dataclass
class AttemptRecord:
    attempt_id: str
    state: str = "CREATED"
    plan: Optional[dict] = None
    candidate_path: Optional[str] = None
    candidate: Optional[dict] = None
    validation: Optional[dict] = None
    review: Optional[dict] = None
    errors: List[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "attempt_id": self.attempt_id,
            "state": self.state,
            "has_plan": self.plan is not None,
            "candidate_path": self.candidate_path,
            "candidate": self.candidate,
            "validation": self.validation,
            "review": self.review,
            "errors": list(self.errors),
        }


class TaskStore:
    """`tasks/<task_id>/{request.json,context/,attempts/aNNN/...}` on disk.

    Attempts are immutable: a new id is always allocated, and a failed attempt's
    products are never overwritten by the next one.
    """

    def __init__(self, project_dir: Path) -> None:
        self.project_dir = Path(project_dir)
        self.tasks_dir = self.project_dir / TASK_DIR_PREFIX

    # -- tasks -----------------------------------------------------------

    def create_task(self, request: TaskRequest, context: Optional[dict] = None) -> Path:
        task_dir = self.tasks_dir / request.task_id
        if task_dir.exists():
            raise RedesignError(
                INVALID_TASK_STATE, f"task {request.task_id} already exists"
            )
        (task_dir / "attempts").mkdir(parents=True)
        self._write_json(task_dir / "request.json", request.to_dict())
        self._write_json(task_dir / "state.json", {"state": "CREATED", "attempts": []})
        if context is not None:
            (task_dir / "context").mkdir(exist_ok=True)
            self._write_json(task_dir / "context" / "context.json", context)
        return task_dir

    def task_dir(self, task_id: str) -> Path:
        d = self.tasks_dir / task_id
        if not d.exists():
            raise RedesignError(INVALID_TASK_STATE, f"unknown task {task_id}")
        return d

    def read_request(self, task_id: str) -> TaskRequest:
        path = self.task_dir(task_id) / "request.json"
        return TaskRequest.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def read_state(self, task_id: str) -> dict:
        path = self.task_dir(task_id) / "state.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def set_state(self, task_id: str, state: str) -> dict:
        current = self.read_state(task_id)
        assert_transition(current["state"], state)
        current["state"] = state
        current["updated"] = datetime.now(timezone.utc).isoformat()
        self._write_json(self.task_dir(task_id) / "state.json", current)
        return current

    # -- attempts --------------------------------------------------------

    def next_attempt_id(self, task_id: str) -> str:
        existing = self.list_attempts(task_id)
        n = 1
        used = {a for a in existing}
        while f"a{n:03d}" in used:
            n += 1
        return f"a{n:03d}"

    def create_attempt(self, task_id: str, *, attempt_id: Optional[str] = None) -> Path:
        aid = attempt_id or self.next_attempt_id(task_id)
        if not _ATTEMPT_RE.match(aid):
            raise RedesignError(INVALID_TASK_STATE, f"bad attempt id {aid!r}")
        d = self.task_dir(task_id) / "attempts" / aid
        if d.exists():
            raise RedesignError(INVALID_TASK_STATE, f"attempt {aid} already exists")
        d.mkdir(parents=True)
        self._write_json(d / "attempt.json", AttemptRecord(attempt_id=aid).to_dict())
        state = self.read_state(task_id)
        state.setdefault("attempts", []).append(aid)
        self._write_json(self.task_dir(task_id) / "state.json", state)
        return d

    def list_attempts(self, task_id: str) -> List[str]:
        d = self.task_dir(task_id) / "attempts"
        return sorted(p.name for p in d.iterdir() if p.is_dir()) if d.exists() else []

    def read_attempt(self, task_id: str, attempt_id: str) -> AttemptRecord:
        raw = json.loads(
            (self.task_dir(task_id) / "attempts" / attempt_id / "attempt.json")
            .read_text(encoding="utf-8")
        )
        return AttemptRecord(
            attempt_id=raw["attempt_id"], state=raw.get("state", "CREATED"),
            plan=raw.get("plan"), candidate_path=raw.get("candidate_path"),
            candidate=raw.get("candidate"), validation=raw.get("validation"),
            review=raw.get("review"), errors=list(raw.get("errors") or []),
        )

    def update_attempt(self, task_id: str, record: AttemptRecord, **changes) -> AttemptRecord:
        updated = replace(record, **changes)
        path = self.task_dir(task_id) / "attempts" / record.attempt_id / "attempt.json"
        self._write_json(path, updated.to_dict())
        return updated

    def write_attempt_json(self, task_id: str, attempt_id: str, name: str,
                           payload: dict) -> Path:
        path = self.task_dir(task_id) / "attempts" / attempt_id / name
        self._write_json(path, payload)
        return path

    def append_write_log(self, task_id: str, attempt_id: str, events: Iterable[dict]) -> Path:
        path = self.task_dir(task_id) / "attempts" / attempt_id / "write_log.jsonl"
        with open(path, "a", encoding="utf-8") as fh:
            for event in events:
                fh.write(json.dumps(event, sort_keys=True) + "\n")
        return path

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _write_json(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
