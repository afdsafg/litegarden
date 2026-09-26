"""Transactional baseline, staged working view and net-change merging (P0).

Spec section 6. Two baselines are kept strictly apart:

- ``B0`` the originally imported scene, used to restore a project and to export
  "what changed in total". It is *not* the before-baseline of a local task.
- ``Br`` the currently accepted revision when a local redesign starts. It is
  the before-baseline of the transaction: every generation or repair attempt
  starts from this same Br and never stacks onto a previous failed candidate.

Two kinds of record are kept apart as well:

- ``WriteEvent`` records what the *staged working view* did, in order, per op.
- ``NetChange`` records, per coordinate, ``before = Br[p]`` and
  ``after = final working view[p]`` plus the ordered list of writers.

The net merge fixes ``before`` correctness. It does **not** prove that every
overwrite was intentional: an overlap between two different ops is reported as
a conflict unless an explicit dependency or an explicit overwrite rule allows
it. A zero net change never licenses an unauthorised write - every single write
goes through the same guard, so "write then restore" is still refused at the
first real write.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Tuple
import hashlib
from .blocks import AIR_BLOCKS
from .constraints import WriteGuard, WriteRejected
from .scene import AIR, BlockChange, PatchSet, SceneSnapshot, Vec3


@dataclass(frozen=True)
class WriteEvent:
    """One staged write, kept even when it is later cancelled out."""

    pos_local: Vec3
    stage_before: str
    stage_after: str
    op_id: str
    target_id: Optional[str]
    sequence: int
    action: str

    def to_dict(self) -> dict:
        return {
            "pos_local": list(self.pos_local),
            "stage_before": self.stage_before,
            "stage_after": self.stage_after,
            "op_id": self.op_id,
            "target_id": self.target_id,
            "sequence": self.sequence,
            "action": self.action,
        }


@dataclass(frozen=True)
class NetChange:
    """One final per-coordinate change against ``Br``."""

    pos_local: Vec3
    before: str  # Br[p]
    after: str  # final working view[p]
    contributors: Tuple[str, ...]  # ordered writers, first writer first
    region_id: str

    @property
    def op_id(self) -> str:
        """The last writer - the op that produced the final state."""
        return self.contributors[-1] if self.contributors else ""

    def to_dict(self) -> dict:
        return {
            "region_id": self.region_id,
            "pos_local": list(self.pos_local),
            "before": self.before,
            "after": self.after,
            "contributors": list(self.contributors),
            "op_id": self.op_id,
        }


@dataclass(frozen=True)
class ConflictPolicy:
    """How overlapping writes from *different* ops are treated.

    ``strict`` refuses an undeclared overlap. A declared overlap needs either
    an explicit op pair in ``allowed_pairs`` or the new write passing
    ``depends_on`` naming the previous writer.
    """

    mode: str = "strict"  # "strict" | "permissive"
    allowed_pairs: FrozenSet[Tuple[str, str]] = frozenset()

    def permits(
        self,
        previous: WriteEvent,
        op_id: str,
        target_id: Optional[str],
        depends_on: Sequence[str],
    ) -> bool:
        if self.mode == "permissive":
            return True
        if previous.op_id == op_id:
            return True
        if target_id is not None and previous.target_id == target_id:
            return True
        if previous.op_id in depends_on:
            return True
        return (previous.op_id, op_id) in self.allowed_pairs


@dataclass
class NetPatchResult:
    """The merged net patch plus the evidence needed to audit it."""

    patch: PatchSet
    net_changes: List[NetChange]
    events: List[WriteEvent] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    world: Optional["WorkingWorld"] = None

    @property
    def stats(self) -> dict:
        cut = fill = replace = 0
        for c in self.net_changes:
            before_air = c.before in AIR_BLOCKS or c.before == AIR
            after_air = c.after in AIR_BLOCKS or c.after == AIR
            if before_air and not after_air:
                fill += 1
            elif not before_air and after_air:
                cut += 1
            elif not before_air and not after_air:
                replace += 1
        return {
            "net_changes": len(self.net_changes),
            "write_events": len(self.events),
            "touched": len({e.pos_local for e in self.events}),
            "cut": cut,
            "fill": fill,
            "replace": replace,
        }

    def contributors_at(self, pos_local: Vec3) -> Tuple[str, ...]:
        for c in self.net_changes:
            if c.pos_local == pos_local:
                return c.contributors
        return ()

    def base_scene_hash(self) -> Optional[str]:
        """Semantic hash of the transaction baseline ``Br`` (B05/B06)."""
        return self.world.baseline_scene_hash() if self.world else None

    def final_scene_hash(self) -> Optional[str]:
        """Semantic hash of the merged final working view (B06)."""
        return self.world.staged_scene_hash() if self.world else None


class WorkingWorld:
    """The staged working view: the only mutable state during a compile.

    Reads mirror :class:`~litegarden.scene.SceneSnapshot` so operations can be
    handed this object instead of the baseline snapshot without changing their
    signatures. Writes only happen through :meth:`write`, which is the single
    entry point every modification must pass.
    """

    def __init__(
        self,
        snapshot: SceneSnapshot,
        guard: WriteGuard,
        conflict_policy: Optional[ConflictPolicy] = None,
    ) -> None:
        self.snapshot = snapshot
        self.guard = guard
        self.conflicts = conflict_policy or ConflictPolicy()
        self.transform = snapshot.transform
        self.data_version = snapshot.data_version
        self.region_id = snapshot.region_id
        self._writes: Dict[Vec3, str] = {}
        self._writers: Dict[Vec3, List[WriteEvent]] = {}
        self.events: List[WriteEvent] = []
        self._base_hash: Optional[str] = None
        self._staged_hash: Optional[str] = None
        self.warnings: List[str] = []

    # -- read side (mirrors SceneSnapshot) --------------------------------

    def base_state_at(self, p: Vec3) -> str:
        """``Br[p]`` - the frozen transaction baseline."""
        return self.snapshot.block_at_local(p)

    def block_at_local(self, p: Vec3) -> str:
        """Current staged state at ``p`` (baseline overlaid with staged writes)."""
        if p in self._writes:
            return self._writes[p]
        return self.snapshot.block_at_local(p)

    def contains_local(self, p: Vec3) -> bool:
        return self.snapshot.contains_local(p)

    def iter_local(self):
        return self.snapshot.iter_local()

    def is_staged(self, p: Vec3) -> bool:
        return p in self._writes

    # -- write side -------------------------------------------------------

    def write(
        self,
        pos: Vec3,
        after: str,
        op_id: str,
        expected_stage_before: Optional[str] = None,
        action: str = "place",
        target_id: Optional[str] = None,
        depends_on: Sequence[str] = (),
    ) -> Optional[WriteEvent]:
        """The single write entry point.

        Order of operations is deliberate: the stage-before contract is checked
        first, then the permission gate, and only then is a conflict considered.
        Nothing is applied before the gate passes, so an illegal write is
        refused atomically instead of being written and rolled back.
        """
        pos = tuple(pos)  # type: ignore[assignment]
        if not self.contains_local(pos):
            raise WriteRejected(
                "WRITE_OUT_OF_BOUNDS",
                f"{pos}: outside the scene",
                op_id=op_id, pos_local=pos, rule_id="bounds/scene",
                expected="inside scene", actual=str(pos),
            )
        current = self.block_at_local(pos)

        if expected_stage_before is not None and current != expected_stage_before:
            raise WriteRejected(
                "BEFORE_MISMATCH",
                f"{pos}: staged state '{current}' does not match the op's expected "
                f"stage_before '{expected_stage_before}' (op {op_id})",
                op_id=op_id, pos_local=pos, rule_id="net_patch/stage_before",
                expected=expected_stage_before, actual=current,
            )

        # Gate 1: every real write is checked before it is applied.
        self.guard.check_write(pos, current, after, op_id=op_id, action=action)

        if current == after:
            return None  # no-op: nothing to record

        mine = target_id if target_id is not None else op_id
        for previous in self._writers.get(pos, []):
            if previous.op_id == op_id:
                continue
            if not self.conflicts.permits(previous, op_id, mine, depends_on):
                raise WriteRejected(
                    "WRITE_CONFLICT",
                    f"{pos}: op '{op_id}' would overwrite '{previous.stage_after}' "
                    f"written by op '{previous.op_id}' without a declared dependency",
                    op_id=op_id, pos_local=pos, rule_id="net_patch/conflict",
                    expected=f"no undeclared overwrite of {previous.op_id}",
                    actual=f"{current} -> {after}",
                )
            self.warnings.append(
                f"{pos}: op '{op_id}' overwrites op '{previous.op_id}' "
                f"(declared via target/dependency)"
            )

        event = WriteEvent(
            pos_local=pos,
            stage_before=current,
            stage_after=after,
            op_id=op_id,
            target_id=mine,
            sequence=len(self.events),
            action=action,
        )
        self.events.append(event)
        self._writes[pos] = after
        self._writers.setdefault(pos, []).append(event)
        self._staged_hash = None  # the cached semantic hash is now stale
        return event

    # -- merge ------------------------------------------------------------

    def finalize(self) -> NetPatchResult:
        """Merge the working view into the net patch against ``Br``."""
        result: List[NetChange] = []
        for pos in sorted(self._writes):
            original = self.base_state_at(pos)
            final = self._writes[pos]
            if original == final:
                continue
            writers = tuple(e.op_id for e in self._writers.get(pos, []))
            result.append(
                NetChange(
                    pos_local=pos,
                    before=original,
                    after=final,
                    contributors=writers,
                    region_id=self.region_id,
                )
            )

        patch = PatchSet()
        for nc in result:
            patch.set(
                BlockChange(
                    region_id=nc.region_id,
                    pos_local=nc.pos_local,
                    before=nc.before,
                    after=nc.after,
                    op_id=nc.op_id,
                )
            )
        return NetPatchResult(
            patch=patch, net_changes=result, events=list(self.events),
            warnings=list(self.warnings), world=self,
        )

    # -- semantic hashes (B05/B06) ---------------------------------------

    def baseline_scene_hash(self) -> str:
        """Semantic hash of ``Br``, the frozen transaction baseline."""
        if self._base_hash is None:
            self._base_hash = scene_semantic_hash(
                self.snapshot, region_id=self.region_id, data_version=self.data_version
            )
        return self._base_hash

    def staged_scene_hash(self) -> str:
        """Semantic hash of the staged working view."""
        if self._staged_hash is None:
            self._staged_hash = scene_semantic_hash(
                self, region_id=self.region_id, data_version=self.data_version
            )
        return self._staged_hash


def overlay_sampler(
    base: Callable[[Vec3], str], writes: Mapping[Vec3, str]
) -> Callable[[Vec3], str]:
    """Read-only sampler over a baseline plus an overlay; used by checks."""

    def sample(p: Vec3) -> str:
        p = tuple(p)  # type: ignore[assignment]
        if p in writes:
            return writes[p]
        return base(p)

    return sample


def scene_semantic_hash(reader, *, region_id: str, data_version: int) -> str:
    """Semantic hash of every voxel state in a read view.

    Deliberately independent of file bytes, compression and timestamps: two
    runs of the same input, config, assets and plan must produce the same
    semantic hash, while a compressed file may still differ byte-for-byte.
    """
    h = hashlib.sha256()
    h.update(f"{region_id}|{data_version}".encode("utf-8"))
    for pos in reader.iter_local():
        h.update(f"{pos[0]},{pos[1]},{pos[2]}={reader.block_at_local(pos)};".encode("utf-8"))
    return h.hexdigest()
