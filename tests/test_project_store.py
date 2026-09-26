"""Acceptance: P3 persistence - immutable revisions, HEAD, atomic commit, undo/redo.

Covers spec 12.1 / 12.4 / 12.5: the import never touches the user's file, HEAD
never points at a revision that is not complete on disk, commit/undo/redo are
compare-and-swap guarded, a crash leaves no half-written revision, and the
single-writer project lock refuses a second writer.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest
from litemapy import BlockState, Region, Schematic

from litegarden import project_store as ps
from litegarden.io import MultiRegionError, load_scene, save_scene
from litegarden.project_store import (
    HeadState,
    IncompleteRevisionError,
    ProjectLock,
    ProjectStore,
    ProjectStoreError,
    StaleHeadError,
    canonical_hash,
    sha256_file,
)

AIR = "minecraft:air"
GRASS = "minecraft:grass_block"
DIRT = "minecraft:dirt"
STONE = "minecraft:stone"
BRICKS = "minecraft:stone_bricks"


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


def _write_scene(path: Path, *, size=(6, 4, 6)) -> Path:
    """A tiny single-region litematic: grass floor, dirt layer, stone marker."""
    region = Region(0, 0, 0, *size)
    for pos in region.block_positions():
        x, y, z = pos
        if y == 0:
            region[pos] = BlockState(GRASS)
        elif y == 1:
            region[pos] = BlockState(DIRT)
        else:
            region[pos] = BlockState(AIR)
    region[(2, 1, 2)] = BlockState(STONE)
    schem = Schematic(name="fixture", author="litegarden-test")
    schem.regions["main"] = region
    schem.save(str(path))
    return path


def _edit_scene(source: Path, out: Path, edits: dict) -> Path:
    """Save a copy of ``source`` with a few local coordinates changed."""
    scene = load_scene(str(source))
    for pos, state in edits.items():
        p_region = scene.snapshot.transform.local_to_region(pos)
        scene.region[p_region] = BlockState(state)
    save_scene(scene, str(out))
    return out


def _next_scene(store: ProjectStore, out: Path, edits: dict) -> Path:
    """A new scene derived from the current HEAD scene."""
    scene = store.load_head_scene()
    for pos, state in edits.items():
        p_region = scene.snapshot.transform.local_to_region(pos)
        scene.region[p_region] = BlockState(state)
    save_scene(scene, str(out))
    return out


def _change(pos, before, after, op_id="op_1") -> dict:
    return {
        "region_id": "main",
        "pos_local": list(pos),
        "before": before,
        "after": after,
        "op_id": op_id,
    }


def _objects(objects=None) -> dict:
    objects = objects if objects is not None else []
    return {"schema_version": "0.2", "count": len(objects), "objects": objects}


def _tree_state(root: Path) -> dict:
    """Every file under ``root`` with its content hash, for exact comparisons."""
    out = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            rel = str(path.relative_to(root)).replace("\\", "/")
            out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def _project(tmp_path: Path, *, config=None, name="proj"):
    src = _write_scene(tmp_path / "user input.litematic")
    store = ProjectStore.create(tmp_path / name, src, game_version="1.20.1", config=config)
    return src, store


# --------------------------------------------------------------------------
# create / import
# --------------------------------------------------------------------------


def test_create_imports_the_source_without_touching_it(tmp_path):
    src = _write_scene(tmp_path / "user input.litematic")
    sha_before = sha256_file(src)
    size_before = src.stat().st_size

    store = ProjectStore.create(
        tmp_path / "proj", src, game_version="1.20.1", config={"editable_zone": None}
    )

    # the user's file is only ever read
    assert sha256_file(src) == sha_before
    assert src.stat().st_size == size_before

    for rel in (
        "project.json",
        "source/original.litematic",
        "source/source_manifest.json",
        "config/rules.cfg001.json",
        "HEAD.json",
        "revisions/r000/manifest.json",
        "revisions/r000/patch.json",
        "revisions/r000/objects.json",
        "revisions/r000/scene.litematic",
    ):
        assert (store.dir / rel).is_file(), rel
    for rel in ("source", "config", "revisions", "tasks", "exports", "logs"):
        assert (store.dir / rel).is_dir(), rel

    manifest = store.source_manifest()
    assert manifest["sha256"] == sha_before
    assert manifest["size_bytes"] == size_before
    assert manifest["imported_at"].startswith("20") and datetime.fromisoformat(
        manifest["imported_at"]
    ).tzinfo is not None
    assert manifest["region"]["region_id"] == "main"
    assert manifest["region"]["position"] == [0, 0, 0]
    assert manifest["region"]["size"] == [6, 4, 6]
    assert manifest["minecraft_data_version"] == load_scene(str(src)).data_version
    # B0 is a byte-identical copy
    assert sha256_file(store.source_scene_path) == sha_before

    project = store.project_json()
    assert project["schema_version"] == ps.SCHEMA_VERSION
    assert project["project_id"] == "proj"
    assert store.project_id == "proj"
    assert project["game_version"] == "1.20.1"

    assert store.head == HeadState("r000", 0, "cfg001", canonical_hash({"editable_zone": None}))
    assert store.load_config("cfg001") == {"editable_zone": None}
    assert store.load_patch("r000") == []
    assert store.load_objects("r000") == {"schema_version": "0.2", "count": 0, "objects": []}
    assert store.parent_of("r000") is None
    assert store.list_revisions() == ["r000"]
    assert store.next_revision_id() == "r001"

    scene = store.load_head_scene()
    assert scene.snapshot.transform.region.size == (6, 4, 6)
    assert scene.snapshot.transform.region.position == (0, 0, 0)
    assert scene.snapshot.block_at_local((2, 1, 2)) == STONE
    assert scene.snapshot.block_at_local((0, 0, 0)) == GRASS

    head_raw = json.loads((store.dir / "HEAD.json").read_text(encoding="utf-8"))
    assert head_raw["revision_id"] == "r000"
    assert head_raw["generation"] == 0
    assert head_raw["redo"] == []


def test_create_refuses_bad_input_without_writing_anything(tmp_path):
    src = _write_scene(tmp_path / "in.litematic")

    occupied = tmp_path / "proj"
    occupied.mkdir()
    (occupied / "keep.txt").write_text("existing", encoding="utf-8")
    with pytest.raises(ProjectStoreError) as info:
        ProjectStore.create(occupied, src)
    assert "not empty" in str(info.value)
    assert sorted(p.name for p in occupied.iterdir()) == ["keep.txt"]

    multi = tmp_path / "multi.litematic"
    schem = Schematic(name="multi")
    for region_id, origin in (("a", (0, 0, 0)), ("b", (8, 0, 8))):
        region = Region(*origin, 2, 2, 2)
        for pos in region.block_positions():
            region[pos] = BlockState(STONE)
        schem.regions[region_id] = region
    schem.save(str(multi))
    with pytest.raises(MultiRegionError):
        ProjectStore.create(tmp_path / "proj2", multi)
    assert not (tmp_path / "proj2").exists()

    with pytest.raises(ProjectStoreError):
        ProjectStore.create(tmp_path / "proj3", tmp_path / "missing.litematic")
    assert not (tmp_path / "proj3").exists()


# --------------------------------------------------------------------------
# commit
# --------------------------------------------------------------------------


def test_commit_advances_head_and_head_scene_is_re_read_from_disk(tmp_path):
    src, store = _project(tmp_path)
    edited = _edit_scene(src, tmp_path / "v1.litematic", {(1, 1, 1): BRICKS, (2, 1, 2): AIR})
    patch = [
        _change((1, 1, 1), DIRT, BRICKS),
        _change((2, 1, 2), STONE, AIR),
    ]
    objects = _objects(
        [
            {
                "object_id": "bricks_1",
                "kind": "asset",
                "creation_revision": "r001",
                "substrate": {"1,1,1": DIRT, "2,1,2": STONE},
                "owned": {"1,1,1": BRICKS, "2,1,2": AIR},
                "complete": True,
            }
        ]
    )

    head = store.commit_revision(
        scene_path=edited,
        patch=patch,
        objects=objects,
        expected_head="r000",
        expected_generation=0,
        task_id="task_0001",
    )

    assert head == HeadState("r001", 1, "cfg001", canonical_hash({}))
    assert store.head == head
    assert store.head_scene_path() == store.dir / "revisions" / "r001" / "scene.litematic"
    assert sha256_file(store.head_scene_path()) == sha256_file(edited)

    reloaded = store.load_head_scene()
    assert reloaded.snapshot.block_at_local((1, 1, 1)) == BRICKS
    assert reloaded.snapshot.block_at_local((2, 1, 2)) == AIR
    assert reloaded.snapshot.block_at_local((0, 0, 0)) == GRASS
    assert reloaded.snapshot.transform.region.size == (6, 4, 6)
    # nothing is cached: every call re-reads the artefact on disk
    assert store.load_head_scene() is not store.load_head_scene()

    assert store.load_patch("r001") == patch
    assert store.load_objects("r001") == objects

    manifest = store.load_manifest("r001")
    for key in ps.MANIFEST_KEYS:
        assert key in manifest, key
    assert manifest["revision_id"] == "r001"
    assert manifest["parent_revision"] == "r000"
    assert manifest["created"].startswith("20")
    assert manifest["scene_sha256"] == sha256_file(store.head_scene_path())
    assert manifest["patch_count"] == 2
    assert manifest["objects_count"] == 1
    assert manifest["config_revision"] == "cfg001"
    assert manifest["config_hash"] == canonical_hash({})
    assert manifest["task_id"] == "task_0001"
    assert manifest["source_sha256"] == store.source_manifest()["sha256"]

    assert store.parent_of("r001") == "r000"
    assert store.list_revisions() == ["r000", "r001"]
    assert store.next_revision_id() == "r002"
    assert store.redo_stack == []

    # the import and its copy are untouched by a commit
    assert sha256_file(src) == store.source_manifest()["sha256"]
    assert sha256_file(store.source_scene_path) == store.source_manifest()["sha256"]
    head_raw = json.loads((store.dir / "HEAD.json").read_text(encoding="utf-8"))
    assert head_raw["revision_id"] == "r001" and head_raw["generation"] == 1


def test_commit_reports_progress_in_the_log(tmp_path):
    src, store = _project(tmp_path)
    edited = _edit_scene(src, tmp_path / "v1.litematic", {(1, 1, 1): BRICKS})
    store.commit_revision(
        scene_path=edited,
        patch=[_change((1, 1, 1), DIRT, BRICKS)],
        objects=_objects(),
        expected_head="r000",
        expected_generation=0,
    )
    log_path = store.dir / "logs" / "commits.jsonl"
    assert log_path.is_file()
    lines = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert [line["revision_id"] for line in lines] == ["r001"]
    assert lines[0]["logged_at"].startswith("20")
    assert store.log("commits", {"revision_id": "extra"}).name == "commits.jsonl"


# --------------------------------------------------------------------------
# compare-and-swap
# --------------------------------------------------------------------------


def test_stale_expected_head_or_generation_writes_nothing(tmp_path):
    src, store = _project(tmp_path)
    first = store.commit_revision(
        scene_path=_edit_scene(src, tmp_path / "v1.litematic", {(1, 1, 1): BRICKS}),
        patch=[_change((1, 1, 1), DIRT, BRICKS)],
        objects=_objects(),
        expected_head="r000",
        expected_generation=0,
    )
    assert first.revision_id == "r001"

    head_bytes = (store.dir / "HEAD.json").read_bytes()
    tree = _tree_state(store.dir)
    another = _edit_scene(src, tmp_path / "v2.litematic", {(3, 1, 3): BRICKS})

    for expected_head, expected_generation in (("r000", 1), ("r001", 0), ("r000", 0)):
        with pytest.raises(StaleHeadError) as info:
            store.commit_revision(
                scene_path=another,
                patch=[_change((3, 1, 3), DIRT, BRICKS)],
                objects=_objects(),
                expected_head=expected_head,
                expected_generation=expected_generation,
            )
        exc = info.value
        assert isinstance(exc, ProjectStoreError)
        assert exc.expected_head == expected_head
        assert exc.actual_head == "r001"
        assert exc.expected_generation == expected_generation
        assert exc.actual_generation == 1

    # undo/redo are guarded by the same contract
    with pytest.raises(StaleHeadError):
        store.undo(expected_head="r001", expected_generation=0)
    with pytest.raises(StaleHeadError):
        store.redo(expected_head="r000", expected_generation=1)

    assert store.head == first
    assert (store.dir / "HEAD.json").read_bytes() == head_bytes
    assert _tree_state(store.dir) == tree


def test_an_existing_revision_id_is_never_reused(tmp_path, monkeypatch):
    src, store = _project(tmp_path)
    tree = _tree_state(store.dir)
    head_bytes = (store.dir / "HEAD.json").read_bytes()

    # force the id collider the store must refuse instead of overwriting r000
    monkeypatch.setattr(store, "next_revision_id", lambda: "r000")
    with pytest.raises(ProjectStoreError) as info:
        store.commit_revision(
            scene_path=_edit_scene(src, tmp_path / "v1.litematic", {(1, 1, 1): BRICKS}),
            patch=[_change((1, 1, 1), DIRT, BRICKS)],
            objects=_objects(),
            expected_head="r000",
            expected_generation=0,
        )
    assert "r000" in str(info.value)
    assert "never overwritten" in str(info.value) or "immutable" in str(info.value)
    assert _tree_state(store.dir) == tree
    assert (store.dir / "HEAD.json").read_bytes() == head_bytes
    assert store.head == HeadState("r000", 0, "cfg001", canonical_hash({}))


def test_a_pre_existing_revision_directory_is_not_overwritten(tmp_path):
    """A stray/hand-placed revision directory is neither reused nor rewritten."""
    src, store = _project(tmp_path)
    sentinel = store.dir / "revisions" / "r001"
    sentinel.mkdir()
    (sentinel / "keep.txt").write_text("do not touch", encoding="utf-8")
    keep = sha256_file(sentinel / "keep.txt")

    head = store.commit_revision(
        scene_path=_edit_scene(src, tmp_path / "v1.litematic", {(1, 1, 1): BRICKS}),
        patch=[_change((1, 1, 1), DIRT, BRICKS)],
        objects=_objects(),
        expected_head="r000",
        expected_generation=0,
    )
    # the occupied id is skipped instead of being overwritten
    assert head.revision_id == "r002"
    assert sorted(p.name for p in sentinel.iterdir()) == ["keep.txt"]
    assert sha256_file(sentinel / "keep.txt") == keep
    assert store.list_revisions() == ["r000", "r001", "r002"]
    assert store.next_revision_id() == "r003"


def test_a_failed_artifact_write_leaves_no_partial_revision(tmp_path, monkeypatch):
    src, store = _project(tmp_path)
    head_before = (store.dir / "HEAD.json").read_bytes()
    tree_before = _tree_state(store.dir)
    real_dump = json.dump

    def failing_dump(obj, fp, *args, **kwargs):
        # the manifest write of the staged revision fails; HEAD.json is untouched
        name = str(getattr(fp, "name", ""))
        if "revisions" in name and name.endswith("manifest.json"):
            raise RuntimeError("simulated disk failure")
        return real_dump(obj, fp, *args, **kwargs)

    monkeypatch.setattr(json, "dump", failing_dump)
    with pytest.raises(ProjectStoreError) as info:
        store.commit_revision(
            scene_path=_edit_scene(src, tmp_path / "v1.litematic", {(1, 1, 1): BRICKS}),
            patch=[_change((1, 1, 1), DIRT, BRICKS)],
            objects=_objects(),
            expected_head="r000",
            expected_generation=0,
        )
    assert "r001" in str(info.value)

    assert (store.dir / "HEAD.json").read_bytes() == head_before
    # no promoted revision and no leftover temp directory
    assert _tree_state(store.dir) == tree_before
    assert store.list_revisions() == ["r000"]
    assert store.head == HeadState("r000", 0, "cfg001", canonical_hash({}))


# --------------------------------------------------------------------------
# crash recovery
# --------------------------------------------------------------------------


def test_head_never_points_at_an_incomplete_revision(tmp_path):
    src, store = _project(tmp_path)
    head_raw = json.loads((store.dir / "HEAD.json").read_text(encoding="utf-8"))
    head_bytes = (store.dir / "HEAD.json").read_bytes()

    # (a) a directory without a manifest
    bogus = store.dir / "revisions" / "r999"
    bogus.mkdir()
    (bogus / "scene.litematic").write_bytes(store.head_scene_path().read_bytes())
    broken = dict(head_raw, revision_id="r999", generation=4)
    (store.dir / "HEAD.json").write_text(json.dumps(broken), encoding="utf-8")
    with pytest.raises(IncompleteRevisionError) as info:
        _ = store.head
    assert info.value.revision_id == "r999"
    assert "r999" in str(info.value) and "manifest.json" in str(info.value)
    with pytest.raises(IncompleteRevisionError):
        store.head_scene_path()
    with pytest.raises(IncompleteRevisionError):
        store.load_head_scene()

    # (b) a complete but unreferenced revision stays an orphan
    (store.dir / "HEAD.json").write_bytes(head_bytes)
    orphan = store.dir / "revisions" / "r042"
    orphan.mkdir()
    for name in ps.REVISION_ARTIFACTS:
        (orphan / name).write_bytes((store.dir / "revisions" / "r000" / name).read_bytes())
    orphan_manifest = json.loads((orphan / "manifest.json").read_text(encoding="utf-8"))
    orphan_manifest["revision_id"] = "r042"
    orphan_manifest["parent_revision"] = "r000"
    (orphan / "manifest.json").write_text(json.dumps(orphan_manifest), encoding="utf-8")

    assert store.head == HeadState("r000", 0, "cfg001", canonical_hash({}))
    assert store.list_revisions() == ["r000", "r042", "r999"]
    assert store.parent_of("r042") == "r000"
    assert store.verify_revision_complete("r042")["revision_id"] == "r042"
    assert store.load_head_scene().snapshot.block_at_local((2, 1, 2)) == STONE

    # (c) a tampered HEAD scene is detected instead of being used
    scene_path = store.dir / "revisions" / "r000" / "scene.litematic"
    scene_path.write_bytes(scene_path.read_bytes() + b"tampered")
    with pytest.raises(IncompleteRevisionError) as info:
        _ = store.head
    assert "sha256" in str(info.value)


# --------------------------------------------------------------------------
# undo / redo
# --------------------------------------------------------------------------


def test_undo_redo_are_linear_and_persist_across_reopen(tmp_path):
    src, store = _project(tmp_path)
    h1 = store.commit_revision(
        scene_path=_edit_scene(src, tmp_path / "v1.litematic", {(1, 1, 1): BRICKS}),
        patch=[_change((1, 1, 1), DIRT, BRICKS)],
        objects=_objects(),
        expected_head="r000",
        expected_generation=0,
    )
    assert (h1.revision_id, h1.generation) == ("r001", 1)

    h2 = store.commit_revision(
        scene_path=_next_scene(store, tmp_path / "v2.litematic", {(3, 1, 3): BRICKS}),
        patch=[_change((3, 1, 3), DIRT, BRICKS)],
        objects=_objects(),
        expected_head="r001",
        expected_generation=1,
    )
    assert (h2.revision_id, h2.generation) == ("r002", 2)

    undone = store.undo(expected_head="r002", expected_generation=2)
    assert undone == HeadState("r001", 3, "cfg001", canonical_hash({}))
    assert store.redo_stack == ["r002"]
    scene = store.load_head_scene()
    assert scene.snapshot.block_at_local((1, 1, 1)) == BRICKS
    assert scene.snapshot.block_at_local((3, 1, 3)) == DIRT

    # the redo chain lives in HEAD.json, so a reopened store still has it
    reopened = ProjectStore(store.dir)
    assert reopened.redo_stack == ["r002"]
    redone = reopened.redo(expected_head="r001", expected_generation=3)
    assert redone == HeadState("r002", 4, "cfg001", canonical_hash({}))
    assert reopened.redo_stack == []
    assert reopened.load_head_scene().snapshot.block_at_local((3, 1, 3)) == BRICKS

    # replaying the same transition is stale, not a silent second application
    with pytest.raises(StaleHeadError):
        reopened.redo(expected_head="r001", expected_generation=3)
    with pytest.raises(ProjectStoreError) as info:
        reopened.redo(expected_head="r002", expected_generation=4)
    assert "nothing to redo" in str(info.value)

    step = reopened.undo(expected_head="r002", expected_generation=4)
    assert (step.revision_id, step.generation) == ("r001", 5)
    step = reopened.undo(expected_head="r001", expected_generation=5)
    assert (step.revision_id, step.generation) == ("r000", 6)
    # the chain is a stack: the next redo target comes last
    assert reopened.redo_stack == ["r002", "r001"]

    before = _tree_state(reopened.dir)
    with pytest.raises(ProjectStoreError) as info:
        reopened.undo(expected_head="r000", expected_generation=6)
    assert "no parent" in str(info.value)
    assert _tree_state(reopened.dir) == before
    assert reopened.head == HeadState("r000", 6, "cfg001", canonical_hash({}))

    # a new commit from here makes the old redo chain read-only history
    h3 = reopened.commit_revision(
        scene_path=_next_scene(reopened, tmp_path / "v3.litematic", {(4, 1, 4): BRICKS}),
        patch=[_change((4, 1, 4), DIRT, BRICKS)],
        objects=_objects(),
        expected_head="r000",
        expected_generation=6,
    )
    assert (h3.revision_id, h3.generation) == ("r003", 7)
    assert reopened.redo_stack == []
    head_raw = json.loads((reopened.dir / "HEAD.json").read_text(encoding="utf-8"))
    # the abandoned chain is kept verbatim as read-only history (spec 12.5)
    assert head_raw["redo_history"] == ["r002", "r001"]
    assert reopened.list_revisions() == ["r000", "r001", "r002", "r003"]


# --------------------------------------------------------------------------
# config revisions
# --------------------------------------------------------------------------


def test_config_revisions_are_versioned_and_immutable(tmp_path):
    src, store = _project(tmp_path, config={"budget": 10})
    assert store.head.config_revision == "cfg001"
    assert store.head.config_hash == canonical_hash({"budget": 10})
    assert store.load_config("cfg001") == {"budget": 10}

    h1 = store.commit_revision(
        scene_path=_edit_scene(src, tmp_path / "v1.litematic", {(1, 1, 1): BRICKS}),
        patch=[_change((1, 1, 1), DIRT, BRICKS)],
        objects=_objects(),
        expected_head="r000",
        expected_generation=0,
    )
    assert h1.config_revision == "cfg001"
    assert h1.config_hash == canonical_hash({"budget": 10})

    h2 = store.commit_revision(
        scene_path=_next_scene(store, tmp_path / "v2.litematic", {(3, 1, 3): BRICKS}),
        patch=[_change((3, 1, 3), DIRT, BRICKS)],
        objects=_objects(),
        expected_head="r001",
        expected_generation=1,
        config={"budget": 20},
    )
    assert h2.config_revision == "cfg002"
    assert h2.config_hash == canonical_hash({"budget": 20})
    assert store.load_config("cfg002") == {"budget": 20}
    assert (store.dir / "config" / "rules.cfg002.json").is_file()
    assert store.load_manifest("r002")["config_revision"] == "cfg002"

    # undo restores the config revision that the parent actually used
    undone = store.undo(expected_head="r002", expected_generation=2)
    assert (undone.revision_id, undone.config_revision) == ("r001", "cfg001")
    assert undone.config_hash == canonical_hash({"budget": 10})

    # a frozen config revision is never rewritten with different content
    before = _tree_state(store.dir)
    with pytest.raises(ProjectStoreError) as info:
        store.commit_revision(
            scene_path=_edit_scene(src, tmp_path / "v3.litematic", {(4, 1, 4): BRICKS}),
            patch=[_change((4, 1, 4), DIRT, BRICKS)],
            objects=_objects(),
            expected_head="r001",
            expected_generation=3,
            config_revision="cfg001",
            config={"budget": 999},
        )
    assert "immutable" in str(info.value)
    assert _tree_state(store.dir) == before

    # an unknown config revision cannot be referenced either
    with pytest.raises(ProjectStoreError):
        store.commit_revision(
            scene_path=_edit_scene(src, tmp_path / "v4.litematic", {(4, 1, 4): BRICKS}),
            patch=[_change((4, 1, 4), DIRT, BRICKS)],
            objects=_objects(),
            expected_head="r001",
            expected_generation=3,
            config_revision="cfg099",
        )
    assert _tree_state(store.dir) == before
    assert store.head == HeadState("r001", 3, "cfg001", canonical_hash({"budget": 10}))


def test_working_directories_and_log_paths_are_confined(tmp_path):
    _, store = _project(tmp_path)
    assert store.create_task_dir("task_0004").parent == store.tasks_dir
    attempt = store.create_attempt_dir("task_0004", "a001")
    assert attempt.parent.parent == store.create_task_dir("task_0004")
    assert store.export_dir("export_1").parent == store.exports_dir
    with pytest.raises(ProjectStoreError):
        store.create_task_dir("../escape")
    with pytest.raises(ProjectStoreError):
        store.export_dir("nested/escape")
    with pytest.raises(ProjectStoreError):
        store.log("../../escape", {"x": 1})
    assert not (tmp_path / "escape").exists()


# --------------------------------------------------------------------------
# lock
# --------------------------------------------------------------------------


def test_project_lock_refuses_a_second_writer(tmp_path):
    _, store = _project(tmp_path)
    lock_path = ProjectLock(store.dir).path

    with ProjectLock(store.dir, timeout=0.1, poll_interval=0.01):
        assert lock_path.is_file()
        owner = json.loads(lock_path.read_text(encoding="utf-8"))
        assert owner["pid"] == os.getpid()
        assert owner["acquired_at"].startswith("20")
        with pytest.raises(ProjectStoreError) as info:
            with ProjectLock(store.dir, timeout=0.05, poll_interval=0.01):
                pytest.fail("a second writer must never be allowed in")
        assert "locked" in str(info.value)
    # released: the same writer can take it again
    assert not lock_path.is_file()
    with ProjectLock(store.dir, timeout=0.05, poll_interval=0.01):
        assert lock_path.is_file()
    assert not lock_path.is_file()


def test_project_lock_reclaims_a_stale_lock_and_never_creates_the_project(tmp_path):
    _, store = _project(tmp_path)
    lock_path = ProjectLock(store.dir).path

    # a lock whose owner process is gone
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    assert dead.returncode == 0
    assert ps._pid_alive(dead.pid) is False
    lock_path.write_text(
        json.dumps({"pid": dead.pid, "acquired_at": "2000-01-01T00:00:00+00:00"}),
        encoding="utf-8",
    )
    with ProjectLock(store.dir, timeout=0.5, poll_interval=0.01):
        assert json.loads(lock_path.read_text(encoding="utf-8"))["pid"] == os.getpid()
    assert not lock_path.is_file()

    # a fresh but unreadable lock is not stolen
    lock_path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ProjectStoreError):
        with ProjectLock(store.dir, timeout=0.05, poll_interval=0.01):
            pytest.fail("a corrupt lock must not be silently reclaimed")
    lock_path.unlink()

    # the lock never creates (or escapes) a project directory
    missing = tmp_path / "not-a-project"
    with pytest.raises(ProjectStoreError):
        with ProjectLock(missing):
            pytest.fail("the lock must not create a project directory")
    assert not missing.exists()
