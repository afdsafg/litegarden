"""Generation-source registry and safe replacement of generated objects (spec 11.3/11.4).

A ``.litematic`` alone cannot tell an AI-placed foundation stone from the natural
hillside, so every generated asset, road or decoration is registered with its
provenance:

* what it claims (``occupied_voxels`` / ``required_empty`` / ``support`` / ``entry``
  / ``footprint`` / ``write_set``),
* what the terrain looked like **before that construction** (``substrate``) and what
  it looked like **after** (``owned``),
* which later objects read it (``read_dependencies``) and how it meets the rest of
  the design (``boundary_links``).

Two invariants drive every check in this module:

* a rollback restores the recorded **substrate**, never air and never the B0
  import - a foundation dug into a hill goes back to the dirt that was there;
* an object with an incomplete provenance record (``kind="imported"`` or
  ``complete=False``) can never be a replacement target, and neither can an object
  whose voxels have changed since it was recorded, whose claims reach outside the
  user-authorised selection, or that a later object depends on.

Pure data structure + validation logic: no file access, no litemapy, no scene
mutation. Coordinates are ``(x, y, z)`` integer triples in project-local space; on
the wire they are ``[x, y, z]`` lists and state maps use ``"x,y,z"`` string keys
(the prefab JSON convention). Every query returns a deterministic (x, y, z) order.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator, Mapping, Optional, Sequence, Tuple

from .blocks import AIR_BLOCKS
from .scene import AIR, Vec3

SCHEMA_VERSION = "0.2"

#: Object kinds the registry understands. ``imported`` has no verified provenance.
OBJECT_KINDS: tuple[str, ...] = ("asset", "path", "decoration", "imported")

Vec3Iterable = Iterable[Any]
StateReader = Callable[[Vec3], str]


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------


class ObjectError(RuntimeError):
    """Malformed record/registry input, or an illegal registry operation."""

    def to_dict(self) -> dict:
        return {"code": type(self).__name__, "message": str(self)}


class TargetOwnershipConflict(ObjectError):
    """The old object's owned state does not match its record, or writes would
    leave the authorised selection, or its provenance is not verifiable.

    Carries the ``object_id`` involved plus the coordinate detail (``positions``
    and a structured ``details`` mapping) so the caller can widen the selection
    or keep the object instead of guessing.
    """

    def __init__(
        self,
        message: str,
        *,
        object_id: Optional[str] = None,
        positions: Vec3Iterable = (),
        details: Optional[dict] = None,
    ) -> None:
        super().__init__(message)
        self.object_id = object_id
        self.positions: tuple[Vec3, ...] = tuple(tuple(p) for p in positions)  # type: ignore[misc]
        self.details: dict = dict(details or {})

    def to_dict(self) -> dict:
        d = super().to_dict()
        d.update(
            {
                "object_id": self.object_id,
                "positions": [list(p) for p in self.positions],
                "details": dict(self.details),
            }
        )
        return d


class TargetDependencyConflict(ObjectError):
    """A later object depends on a replacement target, so it cannot be withdrawn.

    ``dependents`` lists the object ids that read the target (spec 11.3: no
    last-writer-wins withdrawal of a shared foundation or road).
    """

    def __init__(
        self,
        message: str,
        *,
        object_id: Optional[str] = None,
        dependents: Iterable[dict] = (),
        positions: Vec3Iterable = (),
        details: Optional[dict] = None,
    ) -> None:
        super().__init__(message)
        self.object_id = object_id
        self.dependents: tuple[dict, ...] = tuple(dict(d) for d in dependents)
        self.positions: tuple[Vec3, ...] = tuple(tuple(p) for p in positions)  # type: ignore[misc]
        self.details: dict = dict(details or {})

    def to_dict(self) -> dict:
        d = super().to_dict()
        d.update(
            {
                "object_id": self.object_id,
                "dependents": [dict(x) for x in self.dependents],
                "positions": [list(p) for p in self.positions],
                "details": dict(self.details),
            }
        )
        return d


# --------------------------------------------------------------------------
# coordinate / state helpers
# --------------------------------------------------------------------------


def _require_int(value: Any, what: str) -> int:
    """Strict integer coercion: bools, floats and strings are refused."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ObjectError(f"{what} must be an integer, got {value!r}")
    return value


def _vec3(value: Any, what: str) -> Vec3:
    """A 3-integer coordinate from a list/tuple (or an ``"x,y,z"`` string)."""
    if isinstance(value, str):
        return _pos_from_key(value, what)
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ObjectError(f"{what} must be a 3-integer coordinate, got {value!r}")
    return (
        _require_int(value[0], f"{what}[0]"),
        _require_int(value[1], f"{what}[1]"),
        _require_int(value[2], f"{what}[2]"),
    )


def key_of(pos: Vec3) -> str:
    """Canonical ``"x,y,z"`` key of a coordinate (the prefab JSON convention)."""
    x, y, z = _vec3(pos, "pos")
    return f"{x},{y},{z}"


def _pos_from_key(key: Any, what: str) -> Vec3:
    if not isinstance(key, str):
        raise ObjectError(f"{what} must be an \"x,y,z\" string key, got {key!r}")
    parts = key.split(",")
    if len(parts) != 3:
        raise ObjectError(f"{what} must be an \"x,y,z\" string key, got {key!r}")
    out = []
    for i, part in enumerate(parts):
        text = part.strip()
        try:
            out.append(int(text))
        except ValueError as exc:
            raise ObjectError(f"{what}[{i}] is not an integer: {key!r}") from exc
    return (out[0], out[1], out[2])


def _sort_coords(values: Vec3Iterable, what: str) -> tuple[Vec3, ...]:
    """Deterministic (x, y, z) ordering; a repeated coordinate is one voxel."""
    return tuple(sorted({tuple(_vec3(v, f"{what}[]")) for v in values}))


def _state_map(raw: Any, what: str) -> dict[str, str]:
    """Normalise ``{"x,y,z": state}`` into sorted, canonical keys."""
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ObjectError(f"{what} must be a mapping of \"x,y,z\" -> block state")
    out: dict[str, str] = {}
    for key, state in raw.items():
        pos = _pos_from_key(key, f"{what} key")
        if not isinstance(state, str) or not state:
            raise ObjectError(f"{what}[{pos}] must be a non-empty block state string")
        out[key_of(pos)] = state
    return {k: out[k] for k in sorted(out, key=lambda k: _pos_from_key(k, what))}


def _state_of(states: Mapping[str, str], pos: Vec3) -> Optional[str]:
    return states.get(key_of(pos))


def _inside(pos: Vec3, box_min: Vec3, box_max_exclusive: Vec3) -> bool:
    """Half-open containment: the max corner is *not* part of the box."""
    return all(box_min[i] <= pos[i] < box_max_exclusive[i] for i in range(3))


def _box(box_min: Any, box_max_exclusive: Any) -> tuple[Vec3, Vec3]:
    lo = _vec3(box_min, "box_min")
    hi = _vec3(box_max_exclusive, "box_max_exclusive")
    if any(hi[i] <= lo[i] for i in range(3)):
        raise ObjectError(
            f"box is empty or inverted: min={list(lo)} max_exclusive={list(hi)}"
        )
    return lo, hi


def _bounds(points: Sequence[Vec3]) -> dict:
    lo = tuple(min(p[i] for p in points) for i in range(3))
    hi = tuple(max(p[i] for p in points) for i in range(3))
    return {
        "min": list(lo),
        "max_exclusive": [hi[i] + 1 for i in range(3)],
    }


def _target_ids(target_ids: Any) -> tuple[str, ...]:
    if isinstance(target_ids, str) or not isinstance(target_ids, (list, tuple, set, frozenset)):
        raise ObjectError("target_ids must be a sequence of object ids")
    ids = []
    for raw in target_ids:
        if not isinstance(raw, str) or not raw:
            raise ObjectError(f"target object id must be a non-empty string, got {raw!r}")
        ids.append(raw)
    return tuple(sorted(ids))


def _boundary_link(raw: Any, what: str) -> dict:
    """Copy a boundary interface, validating the fields the protocol fixes."""
    if not isinstance(raw, Mapping):
        raise ObjectError(f"{what} must be a mapping")
    link = dict(raw)
    if "pos" in link:
        link["pos"] = list(_vec3(link["pos"], f"{what}.pos"))
    if "direction" in link and not isinstance(link["direction"], str):
        raise ObjectError(f"{what}.direction must be a string")
    if "width" in link:
        link["width"] = _require_int(link["width"], f"{what}.width")
    for key in ("entry", "position"):
        if key in link and not isinstance(link[key], str):
            raise ObjectError(f"{what}.{key} must be a string")
    return link


# --------------------------------------------------------------------------
# records
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ObjectRecord:
    """One generated object and its verifiable provenance.

    ``substrate`` records the block state of every written voxel **before this
    object was constructed**; ``owned`` records the state after. A rollback
    restores ``substrate``, which is neither air nor B0. Both use ``"x,y,z"``
    string keys, matching the prefab JSON convention; every coordinate tuple is
    serialised as an ``[x, y, z]`` list.
    """

    object_id: str
    kind: str
    creation_revision: str
    operation_ids: tuple[str, ...] = ()
    asset_version: Optional[str] = None
    occupied_voxels: tuple[Vec3, ...] = ()
    required_empty: tuple[Vec3, ...] = ()
    support: tuple[Vec3, ...] = ()
    entry: tuple[Vec3, ...] = ()
    footprint: tuple[Vec3, ...] = ()
    write_set: tuple[Vec3, ...] = ()
    read_dependencies: tuple[str, ...] = ()
    boundary_links: tuple[dict, ...] = ()
    substrate: dict = field(default_factory=dict)
    owned: dict = field(default_factory=dict)
    complete: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.object_id, str) or not self.object_id:
            raise ObjectError(f"object_id must be a non-empty string, got {self.object_id!r}")
        if self.kind not in OBJECT_KINDS:
            raise ObjectError(
                f"object '{self.object_id}': unknown kind {self.kind!r}; "
                f"expected one of {list(OBJECT_KINDS)}"
            )
        if not isinstance(self.creation_revision, str):
            raise ObjectError(
                f"object '{self.object_id}': creation_revision must be a string"
            )
        if self.asset_version is not None and not isinstance(self.asset_version, str):
            raise ObjectError(f"object '{self.object_id}': asset_version must be a string or null")
        if not isinstance(self.complete, bool):
            raise ObjectError(f"object '{self.object_id}': complete must be a bool")

        object.__setattr__(
            self, "operation_ids", tuple(str(op) for op in self.operation_ids)
        )
        object.__setattr__(
            self, "read_dependencies", tuple(sorted({str(d) for d in self.read_dependencies}))
        )
        for name in (
            "occupied_voxels",
            "required_empty",
            "support",
            "entry",
            "footprint",
            "write_set",
        ):
            object.__setattr__(
                self, name, _sort_coords(getattr(self, name) or (), f"{self.object_id}.{name}")
            )
        object.__setattr__(
            self, "boundary_links", tuple(
                _boundary_link(link, f"{self.object_id}.boundary_links[]")
                for link in (self.boundary_links or ())
            )
        )
        object.__setattr__(
            self, "substrate", _state_map(self.substrate, f"{self.object_id}.substrate")
        )
        object.__setattr__(self, "owned", _state_map(self.owned, f"{self.object_id}.owned"))

    # -- queries ---------------------------------------------------------

    def claimed_voxels(self) -> tuple[Vec3, ...]:
        """Union of every voxel the record claims, in deterministic (x, y, z) order.

        Occupied voxels, construction writes, supports, entries, footprint and
        required-empty voxels are all part of the object's physical footprint: an
        object counts as "fully inside" a selection only when this whole union is.
        """
        return _sort_coords(
            (
                *self.occupied_voxels,
                *self.write_set,
                *self.support,
                *self.entry,
                *self.footprint,
                *self.required_empty,
            ),
            f"{self.object_id}.claimed_voxels",
        )

    def substrate_of(self, pos: Vec3) -> Optional[str]:
        """Recorded pre-construction state of a voxel (``None`` when not recorded)."""
        return _state_of(self.substrate, pos)

    def owned_of(self, pos: Vec3) -> Optional[str]:
        return _state_of(self.owned, pos)

    def is_replaceable(self) -> bool:
        """Whether the provenance is complete enough to allow a replacement."""
        return self.complete and self.kind != "imported"

    # -- serialisation ---------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "object_id": self.object_id,
            "kind": self.kind,
            "creation_revision": self.creation_revision,
            "operation_ids": list(self.operation_ids),
            "asset_version": self.asset_version,
            "occupied_voxels": [list(p) for p in self.occupied_voxels],
            "required_empty": [list(p) for p in self.required_empty],
            "support": [list(p) for p in self.support],
            "entry": [list(p) for p in self.entry],
            "footprint": [list(p) for p in self.footprint],
            "write_set": [list(p) for p in self.write_set],
            "read_dependencies": list(self.read_dependencies),
            "boundary_links": [dict(link) for link in self.boundary_links],
            "substrate": {k: self.substrate[k] for k in self.substrate},
            "owned": {k: self.owned[k] for k in self.owned},
            "complete": self.complete,
        }

    @staticmethod
    def from_dict(raw: dict) -> "ObjectRecord":
        """Inverse of :meth:`to_dict`; unknown kinds and bad coordinates are refused."""
        if not isinstance(raw, Mapping):
            raise ObjectError(f"ObjectRecord.from_dict expects a mapping, got {raw!r}")
        object_id = raw.get("object_id")
        if not isinstance(object_id, str) or not object_id:
            raise ObjectError(f"object record is missing a string 'object_id': {raw!r}")
        complete = raw.get("complete", True)
        if not isinstance(complete, bool):
            raise ObjectError(f"object '{object_id}': 'complete' must be a bool")
        return ObjectRecord(
            object_id=object_id,
            kind=raw.get("kind"),
            creation_revision=str(raw.get("creation_revision") or ""),
            operation_ids=tuple(raw.get("operation_ids") or ()),
            asset_version=raw.get("asset_version"),
            occupied_voxels=tuple(raw.get("occupied_voxels") or ()),
            required_empty=tuple(raw.get("required_empty") or ()),
            support=tuple(raw.get("support") or ()),
            entry=tuple(raw.get("entry") or ()),
            footprint=tuple(raw.get("footprint") or ()),
            write_set=tuple(raw.get("write_set") or ()),
            read_dependencies=tuple(raw.get("read_dependencies") or ()),
            boundary_links=tuple(raw.get("boundary_links") or ()),
            substrate=raw.get("substrate") or {},
            owned=raw.get("owned") or {},
            complete=complete,
        )


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------


class ObjectRegistry:
    """Deterministically ordered provenance registry keyed by ``object_id``."""

    def __init__(self, records: Optional[Mapping[str, ObjectRecord]] = None) -> None:
        self._records: dict[str, ObjectRecord] = {}
        for key, record in (records or {}).items():
            if not isinstance(record, ObjectRecord):
                raise ObjectError(f"registry entry {key!r} is not an ObjectRecord")
            if str(key) != record.object_id:
                raise ObjectError(
                    f"registry key {key!r} does not match record object_id "
                    f"{record.object_id!r}"
                )
            self.register(record)

    # -- container protocol ----------------------------------------------

    def __len__(self) -> int:
        return len(self._records)

    def __contains__(self, object_id: object) -> bool:
        return object_id in self._records

    def __iter__(self) -> Iterator[str]:
        """Iterate object ids in deterministic order."""
        return iter(sorted(self._records))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ObjectRegistry({sorted(self._records)!r})"

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._records))

    def register(self, record: ObjectRecord) -> None:
        """Add a record; a duplicate id is refused (never silently overwritten)."""
        if not isinstance(record, ObjectRecord):
            raise ObjectError(f"register expects an ObjectRecord, got {record!r}")
        if record.object_id in self._records:
            raise ObjectError(
                f"object '{record.object_id}' is already registered; a provenance record "
                f"is never silently replaced"
            )
        self._records[record.object_id] = record

    def get(self, object_id: str) -> ObjectRecord:
        try:
            return self._records[object_id]
        except KeyError:
            raise ObjectError(f"object '{object_id}' is not in the generation-source registry") from None

    def records(self) -> tuple[ObjectRecord, ...]:
        """All records in deterministic object-id order."""
        return tuple(self._records[oid] for oid in sorted(self._records))

    # -- geometry queries ------------------------------------------------

    def overlapping(self, box_min: Any, box_max_exclusive: Any) -> list[ObjectRecord]:
        """Records with at least one claimed voxel inside the half-open box."""
        lo, hi = _box(box_min, box_max_exclusive)
        return [
            self._records[oid]
            for oid in sorted(self._records)
            if any(_inside(p, lo, hi) for p in self._records[oid].claimed_voxels())
        ]

    def partial_intersections(self, box_min: Any, box_max_exclusive: Any) -> list[dict]:
        """Objects the selection cuts in half (spec 11.4).

        Only objects with voxels both inside and outside the box are reported: the
        caller may only widen the selection to cover them or keep them as they
        are; a generated object is never silently sawn in two.
        """
        lo, hi = _box(box_min, box_max_exclusive)
        out: list[dict] = []
        for oid in sorted(self._records):
            record = self._records[oid]
            claimed = record.claimed_voxels()
            inside = tuple(p for p in claimed if _inside(p, lo, hi))
            if not inside:
                continue
            outside = tuple(p for p in claimed if not _inside(p, lo, hi))
            if not outside:
                continue  # fully inside the selection: not a partial intersection
            out.append(
                {
                    "object_id": oid,
                    "kind": record.kind,
                    "inside_count": len(inside),
                    "outside_count": len(outside),
                    "inside_positions": [list(p) for p in inside[:8]],
                    "outside_positions": [list(p) for p in outside[:8]],
                    "claimed_box": _bounds(claimed),
                }
            )
        return out

    # -- replacement preconditions ---------------------------------------

    def validate_targets(
        self,
        target_ids: Iterable[str],
        *,
        selection_min: Any,
        selection_max_exclusive: Any,
        state_at: StateReader,
    ) -> None:
        """Check that a ``replace_generated`` request may withdraw these objects.

        The four checks of spec 11.3, in this order, and none of them is skipped:

        1. every target exists and has a complete provenance record
           (:class:`TargetOwnershipConflict` otherwise; ``kind="imported"`` or
           ``complete=False`` is never replaceable),
        2. every voxel the object claims (occupied, written, support, entry,
           footprint, required-empty) lies inside the authorised selection,
        3. every ``owned`` voxel still holds the state the record claims,
        4. no other object's ``read_dependencies`` names a target
           (:class:`TargetDependencyConflict`).

        Raises nothing on success; ``state_at`` is a read-only block-state reader
        (``SceneSnapshot.block_at_local``/``WorkingWorld.block_at_local``).
        """
        lo, hi = _box(selection_min, selection_max_exclusive)
        if not callable(state_at):
            raise ObjectError("state_at must be a callable p_local -> block state")
        targets = _target_ids(target_ids)

        # (1) existence + verifiable provenance
        for oid in targets:
            record = self._records.get(oid)
            if record is None:
                raise TargetOwnershipConflict(
                    f"target object '{oid}' is not in the generation-source registry; "
                    f"its provenance cannot be verified",
                    object_id=oid,
                    details={"reason": "unknown_object"},
                )
            if not record.is_replaceable():
                raise TargetOwnershipConflict(
                    f"target object '{oid}' has no verifiable generation source "
                    f"(kind={record.kind!r}, complete={record.complete}); it must be kept "
                    f"or explicitly handled by the user instead of being replaced",
                    object_id=oid,
                    details={
                        "reason": "incomplete_source",
                        "kind": record.kind,
                        "complete": record.complete,
                    },
                )

        # (2) every claimed voxel inside the authorised selection
        for oid in targets:
            record = self._records[oid]
            outside = [p for p in record.claimed_voxels() if not _inside(p, lo, hi)]
            if outside:
                raise TargetOwnershipConflict(
                    f"target object '{oid}': {len(outside)} claimed voxel(s) lie outside the "
                    f"authorised selection, first {[list(p) for p in outside[:4]]}; widen the "
                    f"selection or keep the object",
                    object_id=oid,
                    positions=outside,
                    details={
                        "reason": "outside_selection",
                        "outside_count": len(outside),
                        "selection_min": list(lo),
                        "selection_max_exclusive": list(hi),
                    },
                )

        # (3) the recorded owned state is still what is on the ground
        for oid in targets:
            record = self._records[oid]
            mismatches: list[tuple[Vec3, str, str]] = []
            for key in sorted(record.owned, key=lambda k: _pos_from_key(k, "owned key")):
                pos = _pos_from_key(key, "owned key")
                expected = record.owned[key]
                actual = state_at(pos)
                if actual != expected:
                    mismatches.append((pos, expected, str(actual)))
            if mismatches:
                raise TargetOwnershipConflict(
                    f"target object '{oid}': {len(mismatches)} owned voxel(s) no longer hold "
                    f"the recorded state (e.g. {[list(p) for p, _, _ in mismatches[:3]]}); "
                    f"the object was overwritten and is not withdrawn blindly",
                    object_id=oid,
                    positions=[p for p, _, _ in mismatches],
                    details={
                        "reason": "owned_state_changed",
                        "mismatches": [
                            {"pos": list(p), "expected": e, "actual": a}
                            for p, e, a in mismatches
                        ],
                    },
                )

        # (4) no later object reads a target
        target_set = set(targets)
        dependents: list[dict] = []
        for oid in sorted(self._records):
            if oid in target_set:
                continue
            deps = sorted(d for d in self._records[oid].read_dependencies if d in target_set)
            if deps:
                dependents.append({"object_id": oid, "depends_on": deps})
        if dependents:
            names = [d["object_id"] for d in dependents]
            raise TargetDependencyConflict(
                f"{len(dependents)} object(s) depend on the replacement target(s) "
                f"{sorted(target_set)}: {names}; they must be kept or added to the "
                f"authorised target set",
                object_id=names[0],
                dependents=dependents,
                details={"reason": "later_dependency", "targets": sorted(target_set)},
            )

    def plan_restore(
        self, target_ids: Iterable[str], *, state_at: Optional[StateReader] = None
    ) -> list[tuple[Vec3, str]]:
        """Writes that put the targets (and their foundations) back to substrate.

        The restore target of a voxel is the state recorded in ``substrate`` - the
        state before that construction - which is neither ``minecraft:air`` nor the
        B0 import. A voxel the object wrote but never recorded is refused instead of
        being guessed (:class:`TargetOwnershipConflict`), two targets that disagree
        about a shared foundation are refused, and when ``state_at`` is given the
        recorded ``owned`` state must still be on the ground.

        The result is deduplicated and ordered by ``(x, y, z)``, so the same input
        always yields exactly the same plan. Each entry is ``(p_local, state)`` for
        the caller to feed through the same :class:`~litegarden.constraints.WriteGuard`
        as any other write.
        """
        if state_at is not None and not callable(state_at):
            raise ObjectError("state_at must be a callable p_local -> block state or None")
        targets = _target_ids(target_ids)

        plan: dict[Vec3, str] = {}
        for oid in targets:
            record = self._records.get(oid)
            if record is None:
                raise TargetOwnershipConflict(
                    f"target object '{oid}' is not in the generation-source registry; "
                    f"its substrate is unknown",
                    object_id=oid,
                    details={"reason": "unknown_object"},
                )
            if not record.is_replaceable():
                raise TargetOwnershipConflict(
                    f"target object '{oid}' has no verifiable generation source "
                    f"(kind={record.kind!r}, complete={record.complete}); its substrate "
                    f"cannot be restored",
                    object_id=oid,
                    details={
                        "reason": "incomplete_source",
                        "kind": record.kind,
                        "complete": record.complete,
                    },
                )

            written = _sort_coords(
                (*record.write_set, *record.occupied_voxels), f"{oid}.written"
            )
            missing = [p for p in written if _state_of(record.substrate, p) is None]
            if missing:
                raise TargetOwnershipConflict(
                    f"target object '{oid}': {len(missing)} written voxel(s) have no recorded "
                    f"pre-construction state (substrate), first {[list(p) for p in missing[:4]]}; "
                    f"refusing to assume air or B0",
                    object_id=oid,
                    positions=missing,
                    details={"reason": "substrate_incomplete", "missing_count": len(missing)},
                )

            if state_at is not None:
                mismatches = [
                    (pos, state, str(state_at(pos)))
                    for pos, state in (
                        (_pos_from_key(k, "owned key"), record.owned[k]) for k in record.owned
                    )
                    if str(state_at(pos)) != state
                ]
                if mismatches:
                    raise TargetOwnershipConflict(
                        f"target object '{oid}': {len(mismatches)} owned voxel(s) no longer hold "
                        f"the recorded state (e.g. "
                        f"{[list(p) for p, _, _ in sorted(mismatches)[:3]]}); restoring the "
                        f"substrate over a changed scene is refused",
                        object_id=oid,
                        positions=[p for p, _, _ in mismatches],
                        details={
                            "reason": "owned_state_changed",
                            "mismatches": [
                                {"pos": list(p), "expected": e, "actual": a}
                                for p, e, a in sorted(mismatches)
                            ],
                        },
                    )

            for key in sorted(record.substrate, key=lambda k: _pos_from_key(k, "substrate key")):
                pos = _pos_from_key(key, "substrate key")
                state = record.substrate[key]
                previous = plan.get(pos)
                if previous is not None and previous != state:
                    raise TargetOwnershipConflict(
                        f"targets {sorted(targets)} disagree about the pre-construction state of "
                        f"shared voxel {list(pos)}: {previous!r} vs {state!r}; the shared "
                        f"foundation must be resolved by the user",
                        object_id=oid,
                        positions=[pos],
                        details={
                            "reason": "substrate_conflict",
                            "other_state": previous,
                            "states": sorted({previous, state}),
                        },
                    )
                plan[pos] = state

        return [(pos, plan[pos]) for pos in sorted(plan)]

    # -- serialisation ---------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "count": len(self._records),
            "objects": [self._records[oid].to_dict() for oid in sorted(self._records)],
        }

    @staticmethod
    def from_dict(raw: Any) -> "ObjectRegistry":
        """Rebuild a registry from ``to_dict`` output (list or id->record mapping)."""
        if not isinstance(raw, Mapping):
            raise ObjectError(f"ObjectRegistry.from_dict expects a mapping, got {raw!r}")
        if "objects" not in raw:
            raise ObjectError(
                "registry payload must contain an 'objects' list/mapping of records"
            )
        entries = raw["objects"]
        records: dict[str, ObjectRecord] = {}
        if isinstance(entries, Mapping):
            for key, value in entries.items():
                record = ObjectRecord.from_dict(value)
                if str(key) != record.object_id:
                    raise ObjectError(
                        f"registry key {key!r} does not match object_id {record.object_id!r}"
                    )
                records[record.object_id] = record
        elif isinstance(entries, (list, tuple)):
            for value in entries:
                record = ObjectRecord.from_dict(value)
                records[record.object_id] = record
        else:
            raise ObjectError("registry 'objects' must be a list or a mapping")
        return ObjectRegistry(records)


# --------------------------------------------------------------------------
# helper for the compiler
# --------------------------------------------------------------------------


def record_from_compile(
    *,
    object_id: str,
    kind: Optional[str] = None,
    creation_revision: str = "",
    base_states: Optional[Mapping[Any, str]] = None,
    final_states: Optional[Mapping[Any, str]] = None,
    op_id_kind: Optional[Mapping[str, str]] = None,
    operation_ids: Sequence[str] = (),
    asset_version: Optional[str] = None,
    occupied_voxels: Optional[Iterable[Any]] = None,
    required_empty: Iterable[Any] = (),
    support: Iterable[Any] = (),
    entry: Iterable[Any] = (),
    footprint: Iterable[Any] = (),
    read_dependencies: Iterable[str] = (),
    boundary_links: Iterable[Any] = (),
    substrate: Optional[Mapping[Any, str]] = None,
    owned: Optional[Mapping[Any, str]] = None,
    complete: bool = True,
    extra: Optional[Mapping[str, Any]] = None,
) -> ObjectRecord:
    """Build an :class:`ObjectRecord` from one compile, without guessing anything.

    Every field may be passed as a keyword; the defaults are deliberately narrow.

    * ``base_states`` / ``final_states``: the transaction baseline Br and the final
      staged working view, keyed by ``(x, y, z)`` or by ``"x,y,z"``. Their union is
      the ``write_set`` (any voxel where the two differ, plus every voxel that only
      the final view has).
    * ``substrate``: the pre-construction state of every written voxel. Derived from
      ``base_states`` when not given; a written voxel that has no entry there is
      **refused** (:class:`ObjectError`) rather than assumed to have been air.
    * ``owned``: the post-construction state, from ``final_states`` when not given.
    * ``occupied_voxels``: defaults to the written voxels whose final state is not
      air (a clearance write is part of the write set but not an occupied voxel).
    * ``operation_ids``: the compile's operation ids; ``op_id_kind`` (op id ->
      kind tag) contributes its keys when ``operation_ids`` is empty and is also
      what ``kind`` is normally derived from - pass ``kind`` explicitly when the
      object mixes operations.
    * ``creation_revision``: the revision this object is first committed in; it is
      recorded verbatim, never invented (pass ``""`` for "unknown").
    * ``extra``: additional manifest-level fields are not part of the record and
      cause an :class:`ObjectError`, so callers cannot smuggle unvalidated state in.
    """
    if extra:
        raise ObjectError(
            f"record_from_compile does not accept extra fields: {sorted(extra)}; "
            f"put them in the revision manifest instead"
        )
    if not isinstance(base_states, Mapping) and base_states is not None:
        raise ObjectError("base_states must be a mapping of position -> block state")
    if not isinstance(final_states, Mapping) and final_states is not None:
        raise ObjectError("final_states must be a mapping of position -> block state")

    base = {key_of(p): str(s) for p, s in (base_states or {}).items()}
    final = {key_of(p): str(s) for p, s in (final_states or {}).items()}
    write_keys = sorted(
        {k for k in set(base) | set(final) if base.get(k) != final.get(k)},
        key=lambda k: _pos_from_key(k, "write set"),
    )
    derived_substrate = {k: base[k] for k in write_keys if k in base}
    derived_owned = {k: final[k] for k in write_keys if k in final}

    if substrate is not None:
        substrate_map = {key_of(p): str(s) for p, s in substrate.items()}
    else:
        substrate_map = derived_substrate
    if owned is not None:
        owned_map = {key_of(p): str(s) for p, s in owned.items()}
    else:
        owned_map = derived_owned

    if substrate is None:
        missing = [k for k in write_keys if k not in substrate_map]
        if missing:
            raise ObjectError(
                f"object '{object_id}': base_states is missing the pre-construction state of "
                f"{len(missing)} written voxel(s) (e.g. {missing[:3]}); refusing to assume air"
            )

    if occupied_voxels is None:
        occupied = tuple(
            _pos_from_key(k, "write set") for k in write_keys if final.get(k) not in AIR_BLOCKS
        )
    else:
        occupied = tuple(occupied_voxels)

    ops = tuple(operation_ids) or tuple((op_id_kind or {}).keys())
    if kind is None:
        tags = sorted(set((op_id_kind or {}).values()))
        # A single operation kind names the object; a mixed compile is an "asset"
        # only because the caller did not say otherwise - pass ``kind`` explicitly.
        resolved_kind = tags[0] if len(tags) == 1 and tags[0] in OBJECT_KINDS else "asset"
    else:
        resolved_kind = kind

    return ObjectRecord(
        object_id=object_id,
        kind=resolved_kind,
        creation_revision=creation_revision,
        operation_ids=ops,
        asset_version=asset_version,
        occupied_voxels=occupied,
        required_empty=tuple(required_empty),
        support=tuple(support),
        entry=tuple(entry),
        footprint=tuple(footprint),
        write_set=tuple(_pos_from_key(k, "write set") for k in write_keys),
        read_dependencies=tuple(read_dependencies),
        boundary_links=tuple(boundary_links),
        substrate=substrate_map,
        owned=owned_map,
        complete=complete,
    )


__all__ = [
    "AIR",
    "OBJECT_KINDS",
    "ObjectError",
    "ObjectRecord",
    "ObjectRegistry",
    "SCHEMA_VERSION",
    "TargetDependencyConflict",
    "TargetOwnershipConflict",
    "key_of",
    "record_from_compile",
]
