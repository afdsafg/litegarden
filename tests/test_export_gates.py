"""CLI export gates: the untested end of the P0 chain.

Covers gate 3 (re-validation before writing), the atomic promote, the
"never overwrite the source" guarantee, and the refusal to run without the
verified block rules or with a mistyped config path - all paths that had no
regression test before.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from litegarden.__main__ import main

from .test_operations import _flat_scene, _write_assets

STONE = "minecraft:stone"


def _sha(p) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def _assets_with_rules(tmp_path: Path) -> Path:
    """The synthetic assets plus a verified block-rules file."""
    d = _write_assets(tmp_path)
    (d / "block_rules.json").write_text(json.dumps({
        "version": "0.2",
        "editable_data_versions": [2975, 3953, 4671],
        "in_game_validated": False,
        "allowed_new_blocks": [
            "minecraft:stone_bricks", "minecraft:cobblestone", "minecraft:gravel",
            "minecraft:oak_planks", "minecraft:spruce_planks", "minecraft:oak_log",
            "minecraft:oak_leaves", "minecraft:lantern", "minecraft:oak_fence",
            "minecraft:stone_brick_slab",
        ],
    }), encoding="utf-8")
    # the real project palettes declare a half-slab transition; a palette
    # without one cannot express a level entrance at all
    (d / "palettes.json").write_text(json.dumps({"palettes": {
        "stone_path": {
            "surface": ["minecraft:stone_bricks", "minecraft:cobblestone"],
            "edge": "minecraft:cobblestone",
            "support": "minecraft:cobblestone",
            "transition": "minecraft:stone_brick_slab",
        },
    }}), encoding="utf-8")
    return d


def _write_plan(tmp_path: Path, anchors: dict, **extra) -> Path:
    cfg = {"anchors": anchors, **extra}
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    return p


def _plan_file(tmp_path: Path, name: str = "plan.json") -> Path:
    p = tmp_path / name
    p.write_text(json.dumps({
        "schema_version": "0.1", "scene_id": "t", "seed": 1,
        "operations": [{
            "id": "pav", "op": "place_asset", "asset_id": "pavilion_small",
            "site_id": "site_00", "variant": "north",
        }, {
            "id": "path_1", "op": "connect_path", "from": "A", "to": "pav.entry",
            "width": 1, "palette_id": "stone_path",
        }],
    }), encoding="utf-8")
    return p


def _fixture(tmp_path: Path):
    src = _flat_scene(tmp_path, size=(24, 8, 24), y0=1)
    assets = _assets_with_rules(tmp_path)
    return Path(src), assets


def test_export_succeeds_and_leaves_the_source_untouched(tmp_path):
    src, assets = _fixture(tmp_path)
    cfg = _write_plan(tmp_path, {"A": [2, 5], "B": [21, 5]})
    plan = _plan_file(tmp_path)
    out = tmp_path / "out"
    before = _sha(src)
    rc = main(["export", str(src), "--plan", str(plan), "--assets", str(assets),
               "--config", str(cfg), "--out", str(out)])
    assert rc == 0
    assert _sha(src) == before
    assert (out / "full.litematic").exists()
    assert not (out / "full.litematic.tmp").exists()
    assert (out / "write_log.jsonl").exists()
    assert (out / "nbt_preservation.json").exists()
    data = json.loads((out / "compile.json").read_text(encoding="utf-8"))
    assert data["changes"] > 0
    assert data["write_policy"]["version_gate"] == "active"
    assert data["write_policy"]["rules_source"] == "block_rules"
    assert data["verified"]["checked"] > 0


def test_gate3_rejects_a_write_into_a_protected_zone_on_export(tmp_path):
    """The CLI export path refuses, writes no candidate, and keeps the source."""
    src, assets = _fixture(tmp_path)
    cfg = _write_plan(
        tmp_path, {"A": [2, 5], "B": [21, 5]},
        protected_zones=[{"id": "keep", "min": [10, 0, 0], "max_exclusive": [11, 8, 24]}],
    )
    plan = _plan_file(tmp_path)
    out = tmp_path / "out"
    before = _sha(src)
    rc = main(["export", str(src), "--plan", str(plan), "--assets", str(assets),
               "--config", str(cfg), "--out", str(out)])
    assert rc == 1
    assert _sha(src) == before
    assert not (out / "full.litematic").exists()
    assert not (out / "full.litematic.tmp").exists()
    err = json.loads((out / "error.json").read_text(encoding="utf-8"))
    assert err["rejected"]["code"] == "WRITE_PROTECTED"
    assert err["rejected"]["pos_local"][0] == 10


def test_export_refuses_a_config_path_that_does_not_exist(tmp_path):
    src, assets = _fixture(tmp_path)
    plan = _plan_file(tmp_path)
    out = tmp_path / "out"
    before = _sha(src)
    rc = main(["export", str(src), "--plan", str(plan), "--assets", str(assets),
               "--config", str(tmp_path / "typo.json"), "--out", str(out)])
    assert rc == 1
    assert _sha(src) == before
    assert not (out / "full.litematic").exists()


def test_export_refuses_to_run_without_the_verified_block_rules(tmp_path):
    """A missing block_rules.json would silently disable the whitelist and the
    version gate, so the CLI must refuse instead of degrading quietly."""
    src = Path(_flat_scene(tmp_path, size=(24, 8, 24), y0=1))
    assets = _write_assets(tmp_path)  # no block_rules.json on purpose
    cfg = _write_plan(tmp_path, {"A": [2, 5], "B": [21, 5]})
    plan = _plan_file(tmp_path)
    out = tmp_path / "out"
    before = _sha(src)
    rc = main(["export", str(src), "--plan", str(plan), "--assets", str(assets),
               "--config", str(cfg), "--out", str(out)])
    assert rc == 1
    assert _sha(src) == before
    assert not (out / "full.litematic").exists()


def test_export_refuses_to_overwrite_its_own_input(tmp_path):
    """`--out` pointing at the input file's directory must not replace it."""
    src, assets = _fixture(tmp_path)
    target = tmp_path / "full.litematic"
    target.write_bytes(src.read_bytes())
    cfg = _write_plan(tmp_path, {"A": [2, 5], "B": [21, 5]})
    plan = _plan_file(tmp_path)
    before = _sha(target)
    rc = main(["export", str(target), "--plan", str(plan), "--assets", str(assets),
               "--config", str(cfg), "--out", str(tmp_path)])
    assert rc == 2
    assert _sha(target) == before  # the import is byte-identical afterwards
    err = json.loads((tmp_path / "error.json").read_text(encoding="utf-8"))
    assert "refusing to overwrite the input" in err["error"]


def test_exported_candidate_still_passes_the_readback_checks(tmp_path):
    """The written file really is the compiled candidate (re-read + NBT check)."""
    from litegarden.io import block_state_from_string, load_scene

    src, assets = _fixture(tmp_path)
    cfg = _write_plan(tmp_path, {"A": [2, 5], "B": [21, 5]})
    plan = _plan_file(tmp_path)
    out = tmp_path / "out"
    assert main(["export", str(src), "--plan", str(plan), "--assets", str(assets),
                 "--config", str(cfg), "--out", str(out)]) == 0
    original = load_scene(str(src))
    exported = load_scene(str(out / "full.litematic"))
    changes = json.loads((out / "changes.json").read_text(encoding="utf-8"))["changes"]
    assert changes
    for change in changes:
        pos = tuple(change["pos_local"])
        assert exported.snapshot.block_at_local(pos) == change["after"]
    # a new block really landed in the file
    assert any(
        exported.snapshot.block_at_local(tuple(c["pos_local"])) == c["after"]
        for c in changes
    )
    # and the untouched cells are still identical
    differing = 0
    for pos in original.snapshot.iter_local():
        if original.snapshot.block_at_local(pos) != exported.snapshot.block_at_local(pos):
            differing += 1
    assert differing == len(changes)
    assert block_state_from_string("minecraft:oak_slab[type=bottom]").id.endswith("oak_slab")
