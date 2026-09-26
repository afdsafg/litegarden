"""CLI entry point: inspect / compile / export / pack.

Contract (spec section 10):
    python -m litegarden inspect terrain.litematic --out work/demo
    python -m litegarden compile terrain.litematic --plan plan.json --dry-run --out work/demo
    python -m litegarden export terrain.litematic --plan plan.json --out output/demo

export re-compiles and re-validates; it never trusts a previous dry-run state.
A failed validation produces no full.litematic.

Both commands go through the same WriteGuard and net-patch merge as the library
API: there is no separate compile path for the CLI.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from .compiler import CompileError, compile_plan, make_guard
from .constraints import WriteRejected
from .io import (
    BlockEntityHostChanged,
    LoadedScene,
    MultiRegionError,
    UnsupportedFormatError,
    apply_patchset,
    build_changes_schematic,
    compare_to_expected,
    entity_host_positions,
    ensure_export_preserved,
    load_scene,
    save_scene,
    tile_entity_records,
)
from .nbt_compare import NbtPreservationError
from .report import write_report
from .schema import parse_plan
from .terrain import analyze, build_planning_index
from .validate import validate_patch


def _assets_dir(args: argparse.Namespace) -> Path:
    return Path(getattr(args, "assets", "assets"))


def _load_config(path) -> dict:
    """Load the read-only project config; a named-but-missing file is an error.

    Silently treating a mistyped ``--config`` as "no rules" would switch off
    protection, the editable zone and the version gate at once, which is
    exactly the "unknown condition must refuse" rule this project promises.
    """
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        raise ValueError(f"--config {path} does not exist")
    raw = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{p}: config must be a JSON object")
    return raw


def _require_block_rules(assets_dir: Path) -> None:
    """Refuse the CLI path when the verified block rules are unavailable."""
    rules_path = Path(assets_dir) / "block_rules.json"
    if not rules_path.exists():
        raise ValueError(
            f"{rules_path} not found: the block whitelist and the editable data "
            "version gate would both be switched off, so nothing can be verified. "
            "Point --assets at the project's assets directory."
        )


def _analysis(scene, config: dict):
    a = analyze(scene.snapshot)
    build_planning_index(a)
    # user-declared named anchors from the read-only config
    for name, pos in (config.get("anchors") or {}).items():
        a.anchors[name] = tuple(pos)
    return a


def _guard_for(scene: LoadedScene, args, config: dict):
    try:
        hosts = entity_host_positions(scene)
    except UnsupportedFormatError as e:
        print(f"error: {e}", file=sys.stderr)
        raise
    return make_guard(scene.snapshot, config, _assets_dir(args), hosts), hosts


def _cmd_inspect(args: argparse.Namespace) -> int:
    scene = load_scene(args.input)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    snap = scene.snapshot
    info = snap.transform.region
    config = _load_config(getattr(args, "config", None))
    a = _analysis(scene, config)
    try:
        hosts = entity_host_positions(scene)
    except UnsupportedFormatError as e:
        hosts = []
        print(f"warning: {e}", file=sys.stderr)
    guard, _ = _guard_for(scene, args, config)
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
        "tile_entities": tile_entity_records(scene),
        "entity_host_voxels": [list(h) for h in hosts],
        "write_policy": guard.policy.describe(),
    }
    (out / "inspect.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("region_id", "local_size", "data_version")}, indent=2))
    print(
        f"sites={len(a.site_candidates)} anchors={len(a.anchors)} zones={len(a.zones)} "
        f"tile_entities={len(summary['tile_entities'])}"
    )
    print(
        f"version_gate={guard.policy.describe()['version_gate']} "
        f"rules_source={guard.policy.rules_source}"
    )
    return 0


def _cmd_pack(args: argparse.Namespace) -> int:
    from .agent import build_agent_pack

    scene = load_scene(args.input)
    config = _load_config(getattr(args, "config", None))
    analysis = _analysis(scene, config)
    payload = build_agent_pack(scene, analysis, _assets_dir(args), Path(args.out), config or None)
    print(json.dumps({
        "out": str(args.out),
        "sites": len(payload["site_candidates"]),
        "anchors": len(payload["anchors"]),
        "zones": len(payload["zones"]),
        "images": payload["images"],
    }, indent=2))
    return 0


def _compile(args, scene, config):
    """Shared compile step for `compile` and `export` (one implementation).

    Both commands refuse to run without the verified block rules: a missing
    ``block_rules.json`` would switch off the block whitelist and the editable
    data version gate at the same time, so nothing could be verified.
    """
    _require_block_rules(_assets_dir(args))
    plan = parse_plan(Path(args.plan).read_text(encoding="utf-8"))
    analysis = _analysis(scene, config)
    guard, hosts = _guard_for(scene, args, config)
    return compile_plan(
        scene.snapshot,
        plan,
        analysis,
        _assets_dir(args),
        config=config,
        guard=guard,
        entity_hosts=hosts,
    )


def _report_payload(args, result, extra=None) -> dict:
    payload = {
        "input": str(args.input),
        "plan": str(args.plan),
        "ops": None,
        "changes": len(result.patch),
        "valid": True,
        "errors": [],
        "warnings": result.warnings,
        "stats": result.stats,
        "roads": result.roads,
        "entries": result.entries,
        "issues": result.issues,
        "write_policy": result.guard.policy.describe(),
        "write_audit": {
            "guard": result.guard.stats.to_dict(),
        },
    }
    if extra:
        payload.update(extra)
    return payload


def _cmd_compile(args: argparse.Namespace) -> int:
    scene = load_scene(args.input)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    try:
        config = _load_config(getattr(args, "config", None))
        result = _compile(args, scene, config)
    except WriteRejected as e:
        err = {"error": "write refused", "rejected": e.to_dict()}
        (out / "error.json").write_text(json.dumps(err, indent=2), encoding="utf-8")
        print(json.dumps(err, indent=2), file=sys.stderr)
        return 1
    except ValueError as e:
        # an early refusal (missing config, missing verified rules, bad plan)
        # still leaves a structured error behind
        err = {"error": str(e), "code": "POLICY_INPUT_INVALID"}
        (out / "error.json").write_text(json.dumps(err, indent=2), encoding="utf-8")
        print(json.dumps(err, indent=2), file=sys.stderr)
        return 1
        return 1
    except CompileError as e:
        err = {"error": str(e), "op_id": e.op_id, "code": e.code, "issues": e.issues}
        (out / "error.json").write_text(json.dumps(err, indent=2), encoding="utf-8")
        print(json.dumps(err, indent=2), file=sys.stderr)
        return 1
    rep = validate_patch(scene.snapshot, result.patch)
    payload = _report_payload(args, result, {
        "dry_run": bool(args.dry_run),
        "valid": rep.ok,
        "errors": rep.errors,
    })
    (out / "compile.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (out / "write_log.jsonl").write_text(
        "\n".join(json.dumps(e.to_dict(), sort_keys=True) for e in result.events) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({k: payload[k] for k in ("changes", "valid", "stats")}, indent=2))
    return 0 if rep.ok else 1


def _cmd_export(args: argparse.Namespace) -> int:
    scene = load_scene(args.input)
    config = _load_config(getattr(args, "config", None))
    plan_path = Path(args.plan)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    try:
        result = _compile(args, scene, config)
    except WriteRejected as e:
        err = {"error": "write refused", "rejected": e.to_dict()}
        (out / "error.json").write_text(json.dumps(err, indent=2), encoding="utf-8")
        print(json.dumps(err, indent=2), file=sys.stderr)
        return 1
    except CompileError as e:
        err = {"error": str(e), "op_id": e.op_id, "code": e.code, "issues": e.issues}
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

    # Gate 3: re-validate against the frozen rule set before writing anything.
    try:
        result.guard.check_net_patch(patch, scene.snapshot.block_at_local)
    except WriteRejected as e:
        err = {"error": "final permission re-check failed", "rejected": e.to_dict()}
        (out / "error.json").write_text(json.dumps(err, indent=2), encoding="utf-8")
        print(json.dumps(err, indent=2), file=sys.stderr)
        return 1

    # Every artefact that can fail is produced *before* the candidate is
    # promoted, so a failed export never leaves a fresh full.litematic behind.
    changes_schem = build_changes_schematic(scene, patch) if len(patch) else None
    # The host-block check must compare against the untouched import, so the
    # pristine scene is loaded before the working copy is patched (patching
    # mutates the in-memory region in place).
    pristine = load_scene(str(args.input))
    final_path = Path(out) / "full.litematic"
    tmp_path = Path(out) / "full.litematic.tmp"
    input_abs = os.path.abspath(str(args.input))
    if os.path.abspath(str(final_path)) == input_abs:
        err = {
            "error": "refusing to overwrite the input file",
            "detail": f"{final_path} is the input; choose a different --out directory",
        }
        (Path(out) / "error.json").write_text(json.dumps(err, indent=2), encoding="utf-8")
        print(json.dumps(err, indent=2), file=sys.stderr)
        return 2
    apply_patchset(scene, patch)
    try:
        save_scene(scene, str(tmp_path))
        reloaded = load_scene(str(tmp_path))
        stats = compare_to_expected(str(tmp_path), scene, patch)
        ensure_export_preserved(pristine, reloaded)
    except (NbtPreservationError, BlockEntityHostChanged, AssertionError,
            UnsupportedFormatError, ValueError) as e:
        err = {"error": "re-read verification failed", "detail": str(e)}
        if isinstance(e, NbtPreservationError):
            err["differences"] = [d.__dict__ for d in e.differences]
        if isinstance(e, BlockEntityHostChanged):
            err.update(e.to_dict())
        if tmp_path.exists():
            os.remove(tmp_path)
        (Path(out) / "error.json").write_text(json.dumps(err, indent=2), encoding="utf-8")
        print(json.dumps(err, indent=2), file=sys.stderr)
        return 1
    os.replace(tmp_path, final_path)
    if changes_schem is not None and os.path.abspath(
        str(Path(out) / "changes.litematic")
    ) != input_abs:
        changes_schem.save(str(Path(out) / "changes.litematic"))
    changes_json = {
        "schema_version": "0.2",
        "base_scene": str(args.input),
        "changes": [c.__dict__ for c in patch],
    }
    (out / "changes.json").write_text(json.dumps(changes_json, indent=2), encoding="utf-8")
    (out / "plan.json").write_text(plan_path.read_text(encoding="utf-8"), encoding="utf-8")
    write_report(patch, out / "report.json", out / "report.md")

    payload = _report_payload(args, result, {"verified": stats})
    (out / "compile.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (out / "write_log.jsonl").write_text(
        "\n".join(json.dumps(e.to_dict(), sort_keys=True) for e in result.events) + "\n",
        encoding="utf-8",
    )
    (out / "nbt_preservation.json").write_text(
        json.dumps({
            "preserved": True,
            "allowed_paths": sorted(
                __import__("litegarden.nbt_compare", fromlist=["x"]).allowed_save_paths(
                    scene.snapshot.region_id
                )
            ),
            "note": (
                "type-sensitive comparison of every non-whitelisted NBT field; "
                "block-entity host blocks unchanged"
            ),
        }, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(
        {k: payload[k] for k in ("changes", "verified", "stats")}, indent=2
    ))
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
        sp.add_argument("--config", default=None, help="config.json with zones/anchors/budgets")

    sp = sub.add_parser("inspect", help="load and summarize a litematic")
    add_io(sp)
    add_assets(sp)
    sp.set_defaults(func=_cmd_inspect)

    sp = sub.add_parser("pack", help="write the Agent input pack (analysis.json + images + whitelist)")
    add_io(sp)
    add_assets(sp)
    sp.set_defaults(func=_cmd_pack)

    sp = sub.add_parser("compile", help="compile a plan into a net PatchSet")
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
    except WriteRejected as e:
        print(json.dumps({"error": "write refused", "rejected": e.to_dict()}, indent=2),
              file=sys.stderr)
        return 1
    except (ValueError, AssertionError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
