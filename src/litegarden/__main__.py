"""CLI entry point: inspect / compile / export.

Contract (spec section 10):
    python -m litegarden inspect terrain.litematic --out work/demo
    python -m litegarden compile terrain.litematic --plan plan.json --dry-run --out work/demo
    python -m litegarden export terrain.litematic --plan plan.json --out output/demo

export re-compiles and re-validates; it never trusts a previous dry-run state.
A failed validation produces no full.litematic.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .io import (
    LoadedScene,
    MultiRegionError,
    UnsupportedFormatError,
    apply_patchset,
    build_changes_schematic,
    compare_to_expected,
    load_scene,
    save_scene,
)
from .scene import PatchSet


def _cmd_inspect(args: argparse.Namespace) -> int:
    scene = load_scene(args.input)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    snap = scene.snapshot
    info = snap.transform.region
    summary = {
        "input": str(args.input),
        "data_version": scene.data_version,
        "region_id": info.region_id,
        "position": list(info.position),
        "size": list(info.size),
        "abs_size": list(info.abs_size),
        "min_schem": list(info.min_schem),
        "max_schem": list(info.max_schem),
        "local_size": list(snap.transform.local_size),
    }
    (out / "inspect.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


def _cmd_compile(args: argparse.Namespace) -> int:
    # Full plan compilation arrives with the operations layer; for now we load
    # the scene, validate the plan file parses, and report a no-op patch.
    scene = load_scene(args.input)
    plan_path = Path(args.plan)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    patch = PatchSet()  # TODO: compile plan -> PatchSet via operations
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    result = {
        "input": str(args.input),
        "plan": str(plan_path),
        "dry_run": bool(args.dry_run),
        "ops": len(plan.get("operations", [])),
        "changes": len(patch),
    }
    (out / "compile.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    scene = load_scene(args.input)
    plan_path = Path(args.plan)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    patch = PatchSet()  # TODO: compile plan -> PatchSet via operations

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    apply_patchset(scene, patch)
    full_path = out / "full.litematic"
    save_scene(scene, str(full_path))
    stats = compare_to_expected(str(full_path), scene, patch)

    if len(patch):
        changes = build_changes_schematic(scene, patch)
        changes.save(str(out / "changes.litematic"))

    changes_json = [
        {
            "region_id": c.region_id,
            "pos_local": list(c.pos_local),
            "before": c.before,
            "after": c.after,
            "op_id": c.op_id,
        }
        for c in patch
    ]
    (out / "changes.json").write_text(json.dumps(changes_json, indent=2), encoding="utf-8")
    (out / "plan.json").write_text(plan_path.read_text(encoding="utf-8"), encoding="utf-8")
    report = {"verified": stats, "changes": len(patch)}
    (out / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="litegarden", description=__doc__)
    p.add_argument("--version", action="version", version=f"litegarden {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_io(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("input", help="input terrain.litematic")
        sp.add_argument("--out", required=True, help="output directory")

    sp = sub.add_parser("inspect", help="load and summarize a litematic")
    add_io(sp)
    sp.set_defaults(func=_cmd_inspect)

    sp = sub.add_parser("compile", help="compile a plan into a PatchSet")
    add_io(sp)
    sp.add_argument("--plan", required=True, help="plan.json")
    sp.add_argument("--dry-run", action="store_true", help="validate only, no export")
    sp.set_defaults(func=_cmd_compile)

    sp = sub.add_parser("export", help="compile, validate and write outputs")
    add_io(sp)
    sp.add_argument("--plan", required=True, help="plan.json")
    sp.set_defaults(func=_cmd_export)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (UnsupportedFormatError, MultiRegionError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except (ValueError, AssertionError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
