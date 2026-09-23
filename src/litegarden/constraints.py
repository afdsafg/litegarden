"""Write-permission masks and the single non-bypassable WriteGuard (P0).

Spec section 5: every real modification - paving, clearance, foundation,
decoration, rollback of a previously generated object, repair - must pass
through one write entry point. Individual operations may *propose* changes but
must never be responsible for checking permissions themselves.

Three distinct queries are kept apart (spec 5.1):

- ``known_voxel(p)``       the voxel is inside the input range and its block
                           state was read successfully. Known *air* is known.
- ``ground_valid(x, z)``   the column exposes a usable ground surface. This is
                           a terrain-analysis question, not a "does the block
                           exist" question.
- ``edit_supported(p)``    the block and the action are inside the range the
                           current verified rules can handle safely.

"Unknown ground", "unknown block rule" and "outside the file" are three
different refusals and must never be collapsed into one.

Protection wins: when a protected zone and an editable zone overlap, the
protected zone always decides. Any condition that cannot be evaluated is a
refusal, never a pass.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, FrozenSet, Iterable, Optional, Sequence, Tuple

from .blocks import AIR_BLOCKS, COVER_BLOCKS, GROUND_BLOCKS, SOLID_ROCK_BLOCKS

Vec3 = Tuple[int, int, int]

# Actions a write can claim. Kept explicit so a new write path has to name
# what it is doing instead of inheriting a permissive default.
ACTIONS: FrozenSet[str] = frozenset({
    "place",     # place an asset voxel
    "path",      # pave a road surface
    "support",   # foundation / support fill below a road or asset
    "clear",     # remove a known removable block (clearance request)
    "decorate",  # decorative placement alongside a road
    "restore",   # roll a previously generated object back to its substrate
})

# Blocks that may be *removed or replaced* by construction. Anything outside
# this set (trees, block-entity hosts, unknown modded blocks) is refused.
REMOVABLE_BLOCKS: FrozenSet[str] = frozenset(
    AIR_BLOCKS | GROUND_BLOCKS | SOLID_ROCK_BLOCKS | COVER_BLOCKS
)


def state_id(state: str) -> str:
    """Block id of a block state string, dropping any ``[props]`` suffix."""
    return state.split("[", 1)[0]


# --------------------------------------------------------------------------
# geometry
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Box3:
    """Half-open integer box ``[min, max_exclusive)`` in project-local coords."""

    min: Vec3
    max_exclusive: Vec3

    @classmethod
    def from_inclusive(cls, lo: Vec3, hi: Vec3) -> "Box3":
        return cls(tuple(lo), tuple(hi[i] + 1 for i in range(3)))  # type: ignore[arg-type]

    @classmethod
    def around(cls, points: Iterable[Vec3]) -> "Box3":
        pts = list(points)
        if not pts:
            raise ValueError("cannot build a box from no points")
        lo = tuple(min(p[i] for p in pts) for i in range(3))
        hi = tuple(max(p[i] for p in pts) for i in range(3))
        return cls.from_inclusive(lo, hi)  # type: ignore[arg-type]

    @property
    def size(self) -> Vec3:
        return tuple(self.max_exclusive[i] - self.min[i] for i in range(3))  # type: ignore[return-value]

    def contains(self, p: Vec3) -> bool:
        return all(self.min[i] <= p[i] < self.max_exclusive[i] for i in range(3))

    def intersects(self, other: "Box3") -> bool:
        return all(
            self.min[i] < other.max_exclusive[i] and other.min[i] < self.max_exclusive[i]
            for i in range(3)
        )

    def volume(self) -> int:
        s = self.size
        return max(0, s[0]) * max(0, s[1]) * max(0, s[2])

    def to_dict(self) -> dict:
        return {"min": list(self.min), "max_exclusive": list(self.max_exclusive)}


class MaskSet:
    """Union of half-open boxes with an explicit 3D membership query.

    Sparse by construction: the semantics are identical to a full-size boolean
    array, without allocating one.
    """

    def __init__(self, boxes: Sequence[Box3] = ()) -> None:
        self._boxes: list[Box3] = []
        for b in boxes:
            self.add(b)

    def add(self, box: Box3) -> None:
        if box.volume() > 0:
            self._boxes.append(box)

    @property
    def boxes(self) -> Tuple[Box3, ...]:
        return tuple(self._boxes)

    def __len__(self) -> int:
        return len(self._boxes)

    def __bool__(self) -> bool:
        return bool(self._boxes)

    def contains(self, p: Vec3) -> bool:
        return any(b.contains(p) for b in self._boxes)

    def intersects(self, box: Box3) -> bool:
        return any(b.intersects(box) for b in self._boxes)

    def to_list(self) -> list:
        return [b.to_dict() for b in self._boxes]


# --------------------------------------------------------------------------
# refusals
# --------------------------------------------------------------------------


class WriteRejected(ValueError):
    """A refused write, carrying the structured fields the spec requires.

    ``code / op_id / pos_local / rule_id / expected / actual`` are always
    present; ``detail`` carries free-form context. The whole candidate is
    rejected atomically - illegal writes are never trimmed out of the patch.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        op_id: Optional[str] = None,
        pos_local: Optional[Vec3] = None,
        rule_id: Optional[str] = None,
        expected: Optional[str] = None,
        actual: Optional[str] = None,
        detail: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.op_id = op_id
        self.pos_local = tuple(pos_local) if pos_local is not None else None
        self.rule_id = rule_id
        self.expected = expected
        self.actual = actual
        self.detail = detail

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "message": str(self),
            "op_id": self.op_id,
            "pos_local": list(self.pos_local) if self.pos_local else None,
            "rule_id": self.rule_id,
            "expected": self.expected,
            "actual": self.actual,
            "detail": self.detail,
        }


# --------------------------------------------------------------------------
# policy
# --------------------------------------------------------------------------


def _require_int(value, what: str) -> int:
    """Strict integer coercion: bools, floats and strings are refused."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{what} must be an integer, got {value!r}")
    return value


def _require_int_triple(value, what: str) -> Vec3:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError(f"{what} must be a list of 3 integers, got {value!r}")
    return tuple(_require_int(v, f"{what}[{i}]") for i, v in enumerate(value))  # type: ignore[return-value]


def parse_box(raw, what: str) -> Box3:
    """Parse a half-open box.

    Accepts ``{"min": [x,y,z], "max_exclusive": [x,y,z]}`` (schema 0.2) or the
    legacy 2D form ``{"bbox": [x0, z0, x1, z1], "min_y": .., "max_y_exclusive": ..}``.
    Bounds are half-open; a 2D box still needs an explicit Y range, because
    silently dropping the Y range is exactly the A02 failure.
    """
    if not isinstance(raw, dict):
        raise ValueError(f"{what} must be an object, got {raw!r}")
    if "min" in raw or "max_exclusive" in raw:
        lo = _require_int_triple(raw.get("min"), f"{what}.min")
        hi = _require_int_triple(raw.get("max_exclusive"), f"{what}.max_exclusive")
    elif "bbox" in raw:
        bb = raw["bbox"]
        if not isinstance(bb, (list, tuple)) or len(bb) != 4:
            raise ValueError(f"{what}.bbox must be [x0, z0, x1, z1]")
        bb = [_require_int(v, f"{what}.bbox[{i}]") for i, v in enumerate(bb)]
        lo = (bb[0], _require_int(raw.get("min_y", 0), f"{what}.min_y"), bb[1])
        hi = (
            bb[2] + 1,
            _require_int(raw.get("max_y_exclusive", 1 << 30), f"{what}.max_y_exclusive"),
            bb[3] + 1,
        )
    else:
        raise ValueError(f"{what} needs 'min'/'max_exclusive' (or a legacy 'bbox')")
    box = Box3(lo, hi)
    if any(box.size[i] <= 0 for i in range(3)):
        raise ValueError(f"{what} is empty or inverted: {box.to_dict()}")
    return box


@dataclass(frozen=True)
class WritePolicy:
    """Read-only inputs that decide whether a write is permitted."""

    data_version: int
    # None means "the project did not declare an editable-version whitelist",
    # which is reported as an inactive gate (never presented as verified).
    editable_versions: Optional[FrozenSet[int]]
    # None means "no block whitelist supplied" (legacy permissive compile).
    allowed_new_blocks: Optional[FrozenSet[str]]
    removable_blocks: FrozenSet[str]
    protected: MaskSet
    editable: Optional[MaskSet]
    task_authorized: Optional[MaskSet]
    unknown: MaskSet
    # Local positions that host data we cannot migrate; their host block must
    # not change (spec 7.2).
    entity_hosts: FrozenSet[Vec3]
    rules_source: str
    policy_hash: str

    def describe(self) -> dict:
        """Serialisable diagnostics; safe to expose to the Agent or the UI."""
        return {
            "schema_version": "0.2",
            "data_version": self.data_version,
            "editable_versions": (
                sorted(self.editable_versions) if self.editable_versions is not None else None
            ),
            "version_gate": "active" if self.editable_versions is not None else "inactive",
            "allowed_new_blocks": (
                sorted(self.allowed_new_blocks) if self.allowed_new_blocks is not None else None
            ),
            "removable_blocks": sorted(self.removable_blocks),
            "protected_zones": self.protected.to_list(),
            "editable_zone": self.editable.to_list() if self.editable is not None else None,
            "task_authorized": (
                self.task_authorized.to_list() if self.task_authorized is not None else None
            ),
            "unknown_zones": self.unknown.to_list(),
            "entity_host_count": len(self.entity_hosts),
            "rules_source": self.rules_source,
            "policy_hash": self.policy_hash,
        }


def _policy_digest(payload: dict) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def load_block_rules(path: Optional[Path]) -> dict:
    """Load ``assets/block_rules.json`` if present; an absent file is not an error."""
    if path is None:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    raw = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{p}: block rules must be a JSON object")
    return raw


def build_policy(
    config: Optional[dict],
    block_rules: dict,
    data_version: int,
    scene_bounds: Box3,
    entity_hosts: Iterable[Vec3] = (),
) -> WritePolicy:
    """Build the immutable write policy from project config + block rules.

    ``config`` (project, may be None for the legacy CLI path) supplies the
    zones and budgets. ``block_rules`` supplies the verified block whitelist
    and the editable-version whitelist. Neither is ever bypassable from a
    plan: the Agent cannot widen a mask, raise a budget or change a rule.
    """
    config = config or {}

    protected = MaskSet()
    for i, z in enumerate(config.get("protected_zones") or []):
        zid = z.get("id", f"protected_{i}") if isinstance(z, dict) else f"protected_{i}"
        protected.add(parse_box(z, f"protected_zones[{zid}]"))

    editable: Optional[MaskSet] = None
    if config.get("editable_zone"):
        editable = MaskSet([parse_box(config["editable_zone"], "editable_zone")])

    task_authorized: Optional[MaskSet] = None
    if config.get("task_authorized"):
        task_authorized = MaskSet([parse_box(config["task_authorized"], "task_authorized")])

    unknown = MaskSet()
    for i, z in enumerate(config.get("unknown_zones") or []):
        unknown.add(parse_box(z, f"unknown_zones[{i}]"))

    # Version gate: declared by the project config, else by the block rules.
    # An undeclared gate stays inactive and is reported as such.
    versions = config.get("editable_data_versions")
    source = "config"
    if versions is None:
        versions = block_rules.get("editable_data_versions")
        source = "block_rules" if versions is not None else "none"
    if versions is not None:
        if not isinstance(versions, (list, tuple)):
            raise ValueError("editable_data_versions must be a list of integers")
        editable_versions: Optional[FrozenSet[int]] = frozenset(
            _require_int(v, "editable_data_versions[]") for v in versions
        )
    else:
        editable_versions = None

    new_blocks = block_rules.get("allowed_new_blocks")
    allowed_new: Optional[FrozenSet[str]]
    if new_blocks is None:
        allowed_new = None
        if source == "none":
            source = "none"
    else:
        if not isinstance(new_blocks, (list, tuple)) or not all(
            isinstance(b, str) for b in new_blocks
        ):
            raise ValueError("allowed_new_blocks must be a list of block id strings")
        allowed_new = frozenset(new_blocks)

    hosts = frozenset(tuple(h) for h in entity_hosts)  # type: ignore[misc]

    digest_payload = {
        "data_version": data_version,
        "editable_versions": sorted(editable_versions) if editable_versions else None,
        "allowed_new_blocks": sorted(allowed_new) if allowed_new else None,
        "removable_blocks": sorted(REMOVABLE_BLOCKS),
        "protected": protected.to_list(),
        "editable": editable.to_list() if editable else None,
        "task_authorized": task_authorized.to_list() if task_authorized else None,
        "unknown": unknown.to_list(),
        "entity_hosts": sorted(list(hosts)),
    }
    return WritePolicy(
        data_version=data_version,
        editable_versions=editable_versions,
        allowed_new_blocks=allowed_new,
        removable_blocks=REMOVABLE_BLOCKS,
        protected=protected,
        editable=editable,
        task_authorized=task_authorized,
        unknown=unknown,
        entity_hosts=hosts,
        rules_source=source,
        policy_hash=_policy_digest(digest_payload),
    )


# --------------------------------------------------------------------------
# guard
# --------------------------------------------------------------------------


@dataclass
class GuardStats:
    checked: int = 0
    rejected: int = 0
    by_code: Dict[str, int] = field(default_factory=dict)

    def record(self, code: Optional[str] = None) -> None:
        self.checked += 1
        if code:
            self.rejected += 1
            self.by_code[code] = self.by_code.get(code, 0) + 1

    def to_dict(self) -> dict:
        return {"checked": self.checked, "rejected": self.rejected, "by_code": dict(self.by_code)}


class WriteGuard:
    """The only place that decides whether a write is permitted.

    ``bounds`` is the input file's local box; every write must be inside it.
    """

    def __init__(
        self,
        bounds: Box3,
        policy: WritePolicy,
        entity_hosts: Iterable[Vec3] = (),
    ) -> None:
        self.bounds = bounds
        self.policy = policy
        self.entity_hosts = frozenset(tuple(h) for h in entity_hosts)  # type: ignore[misc]
        self.stats = GuardStats()

    # -- individual predicates -------------------------------------------

    def in_input_bounds(self, p: Vec3) -> bool:
        return self.bounds.contains(p)

    def known_voxel(self, p: Vec3) -> bool:
        """In range and readable; an explicit unknown zone is *not* known."""
        return self.in_input_bounds(p) and not self.policy.unknown.contains(p)

    def global_editable(self, p: Vec3) -> bool:
        if self.policy.editable is None:
            return True
        return self.policy.editable.contains(p)

    def task_authorized(self, p: Vec3) -> bool:
        if self.policy.task_authorized is None:
            return True
        return self.policy.task_authorized.contains(p)

    def protected(self, p: Vec3) -> bool:
        return self.policy.protected.contains(p)

    def edit_supported(self, p: Vec3, action: str, current: str, after: str) -> bool:
        """Whether the block states involved are inside the verified rules."""
        if action not in ACTIONS:
            return False
        after_id = state_id(after)
        current_id = state_id(current)
        # The block being written must be air (explicit removal) or verified.
        if after_id not in AIR_BLOCKS:
            if self.policy.allowed_new_blocks is None:
                pass  # legacy permissive mode: no block whitelist supplied
            elif after_id not in self.policy.allowed_new_blocks:
                return False
        # The block being replaced must be something we may safely remove.
        if current_id not in AIR_BLOCKS and current_id not in self.policy.removable_blocks:
            if self.policy.allowed_new_blocks is None:
                pass  # legacy permissive mode: no block whitelist supplied
            else:
                return False
        return True

    def entity_dependency_safe(self, p: Vec3, action: str) -> bool:
        """Writes over a voxel that hosts unmigratable data are refused."""
        return p not in self.entity_hosts

    # -- the gate ---------------------------------------------------------

    def check_write(
        self,
        pos: Vec3,
        current: str,
        after: str,
        op_id: Optional[str] = None,
        action: str = "place",
    ) -> None:
        """Gate 1/3: refuse an illegal write *before* it is applied."""
        pos = tuple(pos)  # type: ignore[assignment]

        def reject(code: str, message: str, rule_id: str, expected: str, actual: str) -> None:
            self.stats.record(code)
            raise WriteRejected(
                code, message, op_id=op_id, pos_local=pos, rule_id=rule_id,
                expected=expected, actual=actual,
            )

        if self.policy.editable_versions is not None:
            if self.policy.data_version not in self.policy.editable_versions:
                reject(
                    "EDIT_VERSION_UNSUPPORTED",
                    f"{pos}: MinecraftDataVersion {self.policy.data_version} is not in the "
                    f"declared editable set {sorted(self.policy.editable_versions)}",
                    "rules/editable_data_versions",
                    f"data version in {sorted(self.policy.editable_versions)}",
                    str(self.policy.data_version),
                )

        if not self.in_input_bounds(pos):
            reject(
                "WRITE_OUT_OF_BOUNDS",
                f"{pos}: outside the input region bounds {self.bounds.to_dict()}",
                "bounds/input",
                self.bounds.to_dict().__str__(),
                str(pos),
            )

        # Protection wins over every other permission.
        if self.protected(pos):
            reject(
                "WRITE_PROTECTED",
                f"{pos}: inside a protected zone",
                "protected_zone",
                "no write",
                f"{current} -> {after}",
            )

        if not self.known_voxel(pos):
            reject(
                "UNKNOWN_VOXEL_RULE",
                f"{pos}: voxel state is not known/readable",
                "masks/known_voxel",
                "known voxel",
                "unknown",
            )

        if not self.task_authorized(pos):
            reject(
                "WRITE_OUTSIDE_SELECTION",
                f"{pos}: outside the frozen task selection",
                "selection/task_authorized",
                "inside task selection",
                str(pos),
            )

        if not self.global_editable(pos):
            reject(
                "WRITE_OUTSIDE_SELECTION",
                f"{pos}: outside the editable zone",
                "selection/global_editable",
                "inside editable zone",
                str(pos),
            )

        if not self.entity_dependency_safe(pos, action):
            reject(
                "ENTITY_DEPENDENCY_UNSAFE",
                f"{pos}: voxel hosts data that cannot be migrated",
                "entity_dependency",
                "host block unchanged",
                f"{current} -> {after}",
            )

        if not self.edit_supported(pos, action, current, after):
            after_id = state_id(after)
            current_id = state_id(current)
            allowed = self.policy.allowed_new_blocks
            if after_id not in AIR_BLOCKS and allowed is not None and after_id not in allowed:
                reject(
                    "UNKNOWN_BLOCK_RULE",
                    f"{pos}: block '{after_id}' is not in the verified placeable set",
                    "rules/allowed_new_blocks",
                    "block in allowed_new_blocks",
                    after_id,
                )
            reject(
                "UNKNOWN_BLOCK_RULE",
                f"{pos}: baseline block '{current_id}' is not verified removable",
                "rules/removable_blocks",
                "block in removable set",
                current_id,
            )

        self.stats.record(None)

    def check_net_patch(self, changes: Iterable, base_state_at) -> None:
        """Gate 2: re-check the final net patch, including ``before``.

        ``changes`` yields objects with ``pos_local``/``before``/``after``/
        ``op_id`` (the compiler's ``BlockChange`` or ``net_patch.NetChange``).
        """
        for c in changes:
            pos = tuple(c.pos_local)  # type: ignore[assignment]
            expected_before = base_state_at(pos)
            if c.before != expected_before:
                self.stats.record("BEFORE_MISMATCH")
                raise WriteRejected(
                    "BEFORE_MISMATCH",
                    f"{pos}: patch before '{c.before}' != transaction baseline "
                    f"'{expected_before}'",
                    op_id=getattr(c, "op_id", None),
                    pos_local=pos,
                    rule_id="net_patch/baseline",
                    expected=expected_before,
                    actual=c.before,
                )
            self.check_write(
                pos,
                current=expected_before,
                after=c.after,
                op_id=getattr(c, "op_id", None),
                action="place",
            )

    def diagnostics(self) -> dict:
        d = self.policy.describe()
        d["guard_stats"] = self.stats.to_dict()
        return d
