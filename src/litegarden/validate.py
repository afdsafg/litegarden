"""Validation: collision, protected regions, support, access, state checks.

Any over-budget / out-of-bounds / protected-region write blocks output.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .scene import PatchSet, SceneSnapshot


@dataclass
class ValidationReport:
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_patch(snapshot: SceneSnapshot, patch: PatchSet) -> ValidationReport:
    """Check every change is in-bounds; protected/editable masks come next."""
    rep = ValidationReport()
    for c in patch:
        if not snapshot.contains_local(c.pos_local):
            rep.errors.append(f"{c.op_id}: {c.pos_local} out of bounds")
    return rep
