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
from .compiler import CompileError, compile_plan
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
from .report import write_report
from .schema import parse_plan
from .terrain import analyze, build_planning_index
from .validate import validate_patch


def _assets_dir(args: argparse.Namespace) -> Path:
    return Path(getattr(args, "assets", "assets"))


def _analysis(scene, config_path=None):
    a = analyze(scene.snapshot)
    build_planning_index(a)
    # inject user-declared named anchors from config.json (read-only input)
    if config_path:
        import json, os
        if os.path.exists(config_path):
            cfg = json.loads(open(config_path, encoding="utf-8").read())
            for name, pos in cfg.get("anchors", {}).items():
                a.anchors[name] = tuple(pos)
    return a


def _cmd_inspect(args: argparse.Namespace) -> int:
    scene = load_scene(args.input)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    snap = scene.snapshot
    info = snap.transform.region
    a = _analysis(scene)
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
        "site_candidates": a.site_candidates,
        "anchors": {k: list(v) for k, v in a.anchors.items()},
        "zones": a.zones,
    }
    (out / "inspect.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("region_id", "local_size", "data_version")}, indent=2))
    print(f"sites={len(a.site_candidates)} anchors={len(a.anchors)} zones={len(a.zones)}")
    return 0


def _cmd_pack(args: argparse.Namespace) -> int:
    from .agent import build_agent_pack

    scene = load_scene(args.input)
    analysis = _analysis(scene, getattr(args, "config", None))
    config = None
    cfg_path = getattr(args, "config", None)
    if cfg_path:
        import os
        if os.path.exists(cfg_path):
            config = json.loads(open(cfg_path, encoding="utf-8").read())
    payload = build_agent_pack(scene, analysis, _assets_dir(args), Path(args.out), config)
    print(json.dumps({
        "out": str(args.out),
        "sites": len(payload["site_candidates"]),
        "anchors": len(payload["anchors"]),
        "zones": len(payload["zones"]),
        "images": payload["images"],
    }, indent=2))
    return 0


def _cmd_compile(args: argparse.Namespace) -> int:
    scene = load_scene(args.input)
    plan = parse_plan(Path(args.plan).read_text(encoding="utf-8"))
    analysis = _analysis(scene, getattr(args, "config", None))
    try:
        result = compile_plan(scene.snapshot, plan, analysis, _assets_dir(args))
    except CompileError as e:
        print(json.dumps({"error": str(e), "op_id": e.op_id}, indent=2), file=sys.stderr)
        return 1
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rep = validate_patch(scene.snapshot, result.patch)
    payload = {
        "input": str(args.input),
        "plan": str(args.plan),
        "dry_run": bool(args.dry_run),
        "ops": len(plan.operations),
        "changes": len(result.patch),
        "valid": rep.ok,
        "errors": rep.errors,
    }
    (out / "compile.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))
    return 0 if rep.ok else 1


def _cmd_export(args: argparse.Namespace) -> int:
    scene = load_scene(args.input)
    plan_path = Path(args.plan)
    plan = parse_plan(plan_path.read_text(encoding="utf-8"))
    analysis = _analysis(scene, getattr(args, "config", None))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    try:
        result = compile_plan(scene.snapshot, plan, analysis, _assets_dir(args))
    except CompileError as e:
        err = {"error": str(e), "op_id": e.op_id}
        (out / "error.json").write_text(json.dumps(err, indent=2), encoding="utf-8")
        print(json.dumps(err, indent=2), file=sys.stderr)
        return 1
    patch = result.patch

    rep = validate_patch(scene.snapshot, patch)
    if not rep.ok:
        err = {"error": "validation failed", "errors": rep.errors}
        (out / "error.json").write_text(json.dumps(err, indent=2), encoding="utf-8")
        print(json.dumps(err, indent=2), file=sys.stderr)
        return 1

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
    write_report(patch, out / "report.json", out / "report.md")
    report = {"verified": stats, "changes": len(patch), "warnings": result.warnings}
    print(json.dumps(report, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="litegarden", description=__doc__)
    p.add_argument("--version", action="version", version=f"litegarden {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_io(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("input", help="input terrain.litematic")
        sp.add_argument("--out", required=True, help="output directory")

    def add_assets(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--assets", default="assets", help="assets directory")
        sp.add_argument("--config", default=None, help="config.json with anchors/zones")

    sp = sub.add_parser("inspect", help="load and summarize a litematic")
    add_io(sp)
    sp.set_defaults(func=_cmd_inspect)

    sp = sub.add_parser("pack", help="write the Agent input pack (analysis.json + images + whitelist)")
    add_io(sp)
    add_assets(sp)
    sp.set_defaults(func=_cmd_pack)

    sp = sub.add_parser("compile", help="compile a plan into a PatchSet")
    add_io(sp)
    add_assets(sp)
    sp.add_argument("--plan", required=True, help="plan.json")
    sp.add_argument("--dry-run", action="store_true", help="validate only, no export")
    sp.set_defaults(func=_cmd_compile)

    sp = sub.add_parser("export", help="compile, validate and write outputs")
    add_io(sp)
    add_assets(sp)
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
