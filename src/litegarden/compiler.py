"""Deterministic plan compiler: resolve references, compile ops in order,
enforce resource budgets, and produce a PatchSet against the original baseline.

Fixed plan + input + asset versions + seed => deterministic output.
"""
from __future__ import annotations

from dataclasses import dataclass

from .scene import PatchSet, SceneSnapshot
from .schema import Plan


@dataclass
class CompileResult:
    patch: PatchSet
    warnings: list


def compile_plan(snapshot: SceneSnapshot, plan: Plan) -> CompileResult:
    """Compile a validated Plan into a PatchSet.

    The operations layer (path/stamp/scatter) is implemented next; for now a
    plan with operations raises NotImplementedError so failures are explicit
    rather than silently producing an empty patch.
    """
    if plan.operations:
        raise NotImplementedError(
            "operations layer not yet implemented (path/stamp/scatter)"
        )
    return CompileResult(patch=PatchSet(), warnings=[])
