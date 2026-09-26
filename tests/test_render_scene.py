"""Acceptance C/D: the sparse RenderScene payload (spec 13.1).

Fixtures follow tests/test_operations.py: a small .litematic is built with
litemapy, saved and then **re-read** with ``load_scene``, because a RenderScene
must be built from the serialized candidate file and not from compiler memory.
The linear index contract is hand-checked voxel by voxel:

    i = x * size_y * size_z + y * size_z + z   # x, y, z are crop-local
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from litemapy import BlockState, Region, Schematic

from litegarden.io import LoadedScene, block_state_from_string, load_scene
from litegarden.net_patch import scene_semantic_hash
from litegarden.render_scene import (
    ARRAY_LENGTH_MISMATCH,
    DUPLICATE_VOXEL_INDEX,
    INDEX_OUT_OF_RANGE,
    INVALID_RENDER_PALETTE_INDEX,
    RENDER_PAYLOAD_MISMATCH,
    RENDER_SCENE_SCHEMA_VERSION,
    UNKNOWN_RENDER_FIELD,
    RenderSceneError,
    air_display_index,
    build_render_scene,
    summarize_for_client,
    validate_render_scene,
)

AIR = "minecraft:air"
STONE = "minecraft:stone"
DIRT = "minecraft:dirt"
GRASS = "minecraft:grass_block"
BEDROCK = "minecraft:bedrock"
GOLD = "minecraft:gold_block"
STAIRS_NORTH = "minecraft:oak_stairs[facing=north,half=bottom]"
STAIRS_EAST = "minecraft:oak_stairs[facing=east,half=bottom]"

BASIC_SIZE = (4, 2, 3)  # size_y * size_z = 6, so i = x*6 + y*3 + z
BASIC_FILLS = {
    (0, 0, 0): STONE,
    (1, 0, 2): GRASS,
    (2, 0, 0): DIRT,
    (2, 1, 0): STAIRS_NORTH,
    (3, 1, 1): STAIRS_EAST,
}


def _write_scene(path: Path, position, size, fills) -> LoadedScene:
    """Save a small region and re-read it: the RenderScene input."""
    region = Region(position[0], position[1], position[2], size[0], size[1], size[2])
    air = BlockState(AIR)
    for pos in region.block_positions():
        region[pos] = air
    for pos, state in fills.items():
        region[pos] = block_state_from_string(state)
    schem = Schematic(name="render", author="litegarden-test", description="render fixture")
    schem.regions["main"] = region
    schem.save(str(path))
    return load_scene(str(path))


def _basic(tmp_path: Path) -> LoadedScene:
    return _write_scene(tmp_path / "basic.litematic", (0, 0, 0), BASIC_SIZE, BASIC_FILLS)


def _decode(index: int, size) -> tuple[int, int, int]:
    """Invert the linear index: crop-local (x, y, z)."""
    _, size_y, size_z = size
    x, rest = divmod(index, size_y * size_z)
    y, z = divmod(rest, size_z)
    return (x, y, z)


# ---------- basic construction ----------


def test_build_full_scene_layout_and_hand_computed_indices(tmp_path):
    scene = _basic(tmp_path)
    assert scene.snapshot.region_id == "main"
    assert scene.snapshot.transform.local_size == (4, 2, 3)

    payload = build_render_scene(scene, scene_id="c001", file_sha256="a" * 64)

    assert payload["schema_version"] == RENDER_SCENE_SCHEMA_VERSION
    assert payload["scene_id"] == "c001"
    assert payload["coordinate_space"] == "project_local"
    assert payload["crop_origin_local"] == [0, 0, 0]
    assert payload["size"] == [4, 2, 3]
    assert payload["full_scene_bounds"] == {"min": [0, 0, 0], "max_exclusive": [4, 2, 3]}
    assert payload["minecraft_data_version"] == scene.data_version
    assert payload["file_sha256"] == "a" * 64
    assert payload["resource_hash"] is None
    assert payload["counts"] == {"non_air_voxels": 5, "voxels": 24}
    assert "empty" not in payload

    # air is display index 0 and only the states seen in the crop are listed,
    # sorted by the normalised state string.
    assert payload["palette"][0] == {"name": AIR, "props": {}}
    assert [(e["name"], e["props"]) for e in payload["palette"][1:]] == [
        (DIRT, {}),
        (GRASS, {}),
        ("minecraft:oak_stairs", {"facing": "east", "half": "bottom"}),
        ("minecraft:oak_stairs", {"facing": "north", "half": "bottom"}),
        (STONE, {}),
    ]

    # i = x*size_y*size_z + y*size_z + z = x*6 + y*3 + z, hand computed:
    #   (0,0,0) stone       -> 0*6 + 0*3 + 0 = 0
    #   (1,0,2) grass       -> 1*6 + 0*3 + 2 = 8
    #   (2,0,0) dirt        -> 2*6 + 0*3 + 0 = 12
    #   (2,1,0) stairs N    -> 2*6 + 1*3 + 0 = 15
    #   (3,1,1) stairs E    -> 3*6 + 1*3 + 1 = 22
    assert payload["idx"] == [0, 8, 12, 15, 22]
    assert payload["state"] == [5, 2, 1, 4, 3]

    hand_computed = {(0, 0, 0): 0, (1, 0, 2): 8, (2, 0, 0): 12, (2, 1, 0): 15, (3, 1, 1): 22}
    assert {_decode(i, payload["size"]): i for i in payload["idx"]} == hand_computed
    assert payload["idx"] == sorted(set(payload["idx"]))  # ascending and unique
    assert len(payload["idx"]) == len(payload["state"]) == payload["counts"]["non_air_voxels"]

    # every listed voxel really carries the state its index points at
    for index, state_index in zip(payload["idx"], payload["state"]):
        local = _decode(index, payload["size"])
        assert scene.snapshot.block_at_local(local) == payload["palette"][state_index]["name"]
        assert local in BASIC_FILLS  # only the non-air voxels are listed

    # semantics of the whole scene, not of the crop
    assert payload["scene_hash"] == scene_semantic_hash(
        scene.snapshot, region_id="main", data_version=scene.data_version
    )

    validate_render_scene(payload, expected_scene_hash=payload["scene_hash"])
    assert air_display_index(payload) == 0


def test_palette_separates_block_properties(tmp_path):
    fills = {(0, 0, 0): STAIRS_NORTH, (1, 0, 0): STAIRS_EAST}
    scene = _write_scene(tmp_path / "props.litematic", (0, 0, 0), BASIC_SIZE, fills)
    payload = build_render_scene(scene, scene_id="c002")

    assert payload["idx"] == [0, 6]  # (0,0,0) -> 0, (1,0,0) -> 1*6 = 6
    assert [e["name"] for e in payload["palette"]] == [
        AIR, "minecraft:oak_stairs", "minecraft:oak_stairs",
    ]
    # facing=east sorts before facing=north -> distinct palette entries
    assert payload["palette"][1]["props"] == {"facing": "east", "half": "bottom"}
    assert payload["palette"][2]["props"] == {"facing": "north", "half": "bottom"}
    assert payload["state"] == [2, 1]

    by_index = dict(zip(payload["idx"], payload["state"]))
    assert by_index[0] != by_index[6]
    assert payload["palette"][by_index[0]] == {
        "name": "minecraft:oak_stairs", "props": {"facing": "north", "half": "bottom"},
    }
    assert payload["palette"][by_index[6]] == {
        "name": "minecraft:oak_stairs", "props": {"facing": "east", "half": "bottom"},
    }
    # property keys are written in ascending order for a stable payload
    assert list(payload["palette"][1]["props"]) == ["facing", "half"]
    assert list(payload["palette"][2]["props"]) == ["facing", "half"]


def test_nonzero_position_negative_size_use_project_local(tmp_path):
    fills = {(0, 0, 0): STONE, (3, 1, 0): DIRT, (1, 0, -2): GRASS}
    scene = _write_scene(tmp_path / "neg.litematic", (10, 64, -20), (4, 2, -3), fills)
    transform = scene.snapshot.transform
    assert transform.region.position == (10, 64, -20)
    assert transform.local_size == (4, 2, 3)
    assert transform.enclosing_min == (10, 64, -22)
    # p_local is relative to enclosing_min, not to the region array
    assert transform.local_to_region((0, 0, 2)) == (0, 0, 0)
    assert transform.local_to_region((1, 0, 0)) == (1, 0, -2)
    assert transform.local_to_region((3, 1, 2)) == (3, 1, 0)

    payload = build_render_scene(scene, scene_id="c008")
    assert payload["full_scene_bounds"] == {"min": [0, 0, 0], "max_exclusive": [4, 2, 3]}
    # (0,0,0)   -> local (0,0,2) -> 0*6 + 0*3 + 2 = 2
    # (1,0,-2)  -> local (1,0,0) -> 1*6 + 0*3 + 0 = 6
    # (3,1,0)   -> local (3,1,2) -> 3*6 + 1*3 + 2 = 23
    assert payload["idx"] == [2, 6, 23]
    assert payload["state"] == [3, 2, 1]  # dirt, grass_block, stone
    by_index = dict(zip(payload["idx"], payload["state"]))
    for index, fill in ((2, STONE), (6, GRASS), (23, DIRT)):
        local = _decode(index, payload["size"])
        assert scene.snapshot.block_at_local(local) == fill
        assert payload["palette"][by_index[index]]["name"] == fill
    validate_render_scene(payload, expected_scene_hash=payload["scene_hash"])


# ---------- air semantics ----------


def test_all_air_scene_is_a_legal_empty_selection(tmp_path):
    scene = _write_scene(tmp_path / "empty.litematic", (0, 0, 0), (2, 2, 2), {})
    payload = build_render_scene(scene, scene_id="c003")

    assert payload["empty"] is True
    assert payload["idx"] == []
    assert payload["state"] == []
    assert payload["counts"] == {"non_air_voxels": 0, "voxels": 8}
    assert payload["palette"] == [{"name": AIR, "props": {}}]
    validate_render_scene(payload, expected_scene_hash=payload["scene_hash"])
    assert air_display_index(payload) == 0
    summary = summarize_for_client(payload)
    assert summary["empty"] is True
    assert summary["counts"]["non_air_voxels"] == 0

    # an all-air crop of a non-empty scene behaves the same way
    filled = _basic(tmp_path)
    crop = build_render_scene(
        filled, scene_id="c003b", crop_origin_local=(0, 1, 0), crop_size=(1, 1, 1)
    )
    assert crop["empty"] is True
    assert crop["idx"] == [] and crop["state"] == []
    assert crop["counts"] == {"non_air_voxels": 0, "voxels": 1}
    validate_render_scene(crop)


# ---------- crop ----------


def test_crop_restricts_voxels_but_keeps_full_scene_hash(tmp_path):
    fills = {
        (0, 0, 0): BEDROCK,  # outside the crop
        (3, 1, 2): DIRT,  # outside the crop
        (1, 1, 1): GRASS,  # crop-local (0,1,0) -> 2
        (2, 0, 2): STONE,  # crop-local (1,0,1) -> 5
        (2, 1, 2): GOLD,  # crop-local (1,1,1) -> 7
    }
    scene = _write_scene(tmp_path / "crop.litematic", (0, 0, 0), BASIC_SIZE, fills)
    full = build_render_scene(scene, scene_id="c004")
    cropped = build_render_scene(
        scene, scene_id="c004", crop_origin_local=(1, 0, 1), crop_size=(2, 2, 2)
    )

    assert cropped["crop_origin_local"] == [1, 0, 1]
    assert cropped["size"] == [2, 2, 2]
    assert cropped["full_scene_bounds"] == full["full_scene_bounds"] == {
        "min": [0, 0, 0], "max_exclusive": [4, 2, 3],
    }
    # the index stays crop-relative: i = lx*2*2 + ly*2 + lz
    # the index stays crop-relative: i = lx*2*2 + ly*2 + lz; the palette is
    # sorted by normalised state string, and "gold_block" < "grass_block"
    assert cropped["idx"] == [2, 5, 7]
    assert cropped["state"] == [2, 3, 1]
    assert [e["name"] for e in cropped["palette"]] == [AIR, GOLD, GRASS, STONE]
    assert cropped["counts"] == {"non_air_voxels": 3, "voxels": 8}
    # states that only exist outside the crop are not smuggled into the palette
    assert BEDROCK not in [e["name"] for e in cropped["palette"]]
    assert DIRT not in [e["name"] for e in cropped["palette"]]
    listed = {_decode(i, cropped["size"]) for i in cropped["idx"]}
    assert listed == {(0, 1, 0), (1, 0, 1), (1, 1, 1)}
    for index, state_index in zip(cropped["idx"], cropped["state"]):
        lx, ly, lz = _decode(index, cropped["size"])
        assert scene.snapshot.block_at_local((1 + lx, ly, 1 + lz)) == cropped["palette"][state_index]["name"]

    # the semantic hash covers the whole scene, so the crop cannot change it
    assert cropped["scene_hash"] == full["scene_hash"]
    assert cropped["scene_hash"] == scene_semantic_hash(
        scene.snapshot, region_id="main", data_version=scene.data_version
    )
    validate_render_scene(cropped, expected_scene_hash=full["scene_hash"])


def test_build_rejects_a_crop_outside_the_scene(tmp_path):
    scene = _basic(tmp_path)
    for origin, size in (
        ((3, 0, 0), (2, 2, 2)),  # x exceeds the scene width
        ((0, 0, 0), (4, 3, 3)),  # y exceeds the scene height
        ((-1, 0, 0), (1, 1, 1)),  # negative origin
        ((0, 0, 0), (0, 2, 2)),  # empty crop
    ):
        with pytest.raises(RenderSceneError) as exc:
            build_render_scene(
                scene, scene_id="c009", crop_origin_local=origin, crop_size=size
            )
        assert exc.value.code == INDEX_OUT_OF_RANGE
        assert exc.value.to_dict()["code"] == INDEX_OUT_OF_RANGE


def test_build_refuses_a_scene_that_cannot_report_properties(tmp_path):
    # A read view that only knows block ids would merge two states that differ
    # by a property alone, so it is refused instead of guessed at.
    scene = _basic(tmp_path)

    class _NoRegion:
        snapshot = scene.snapshot

    with pytest.raises(RenderSceneError) as exc:
        build_render_scene(_NoRegion(), scene_id="c013")
    assert exc.value.code == RENDER_PAYLOAD_MISMATCH
    assert "properties" in exc.value.detail


# ---------- validate: positive cases ----------


def test_validate_accepts_built_payloads(tmp_path):
    scene = _basic(tmp_path)
    full = build_render_scene(scene, scene_id="c010", file_sha256="b" * 64)
    cropped = build_render_scene(
        scene, scene_id="c010", crop_origin_local=(1, 0, 1), crop_size=(2, 2, 2)
    )
    empty = build_render_scene(
        scene, scene_id="c010", crop_origin_local=(0, 1, 0), crop_size=(1, 1, 1)
    )
    for payload in (full, cropped, empty):
        validate_render_scene(payload, expected_scene_hash=payload["scene_hash"])
        assert air_display_index(payload) == 0

    # the wire form is JSON, so a round trip must validate too
    wire = json.loads(json.dumps(full))
    assert wire == full
    validate_render_scene(wire, expected_scene_hash=full["scene_hash"])

    # a payload without the file byte hash is still a valid payload
    validate_render_scene(build_render_scene(scene, scene_id="c010"))

    with pytest.raises(RenderSceneError) as exc:
        validate_render_scene(full, expected_scene_hash="0" * 64)
    assert exc.value.code == RENDER_PAYLOAD_MISMATCH


# ---------- validate: negative cases ----------


def _state_out_of_palette(payload):
    payload["state"][0] = len(payload["palette"])


def _state_negative(payload):
    payload["state"][0] = -1


def _state_length_mismatch(payload):
    payload["state"].pop()


def _idx_above_volume(payload):
    payload["idx"][0] = payload["counts"]["voxels"]


def _idx_negative(payload):
    payload["idx"][0] = -1


def _idx_duplicate(payload):
    payload["idx"][1] = payload["idx"][0]


def _idx_not_an_integer(payload):
    payload["idx"][0] = 0.0


def _unknown_field(payload):
    payload["renderer_build_hash"] = "0" * 64


def _unknown_bounds_field(payload):
    payload["full_scene_bounds"]["padding"] = 1


def _unknown_counts_field(payload):
    payload["counts"]["accepted_voxel_count"] = 5


def _unknown_palette_field(payload):
    payload["palette"].append({"name": STONE, "props": {}, "tint": 1})


def _palette_empty(payload):
    payload["palette"] = []


def _palette_display_index_not_air(payload):
    payload["palette"][0] = {"name": STONE, "props": {}}


def _palette_entry_missing_props(payload):
    payload["palette"][1] = {"name": DIRT}


def _counts_inconsistent(payload):
    payload["counts"]["non_air_voxels"] = 99


def _counts_missing(payload):
    del payload["counts"]


def _schema_version_old(payload):
    payload["schema_version"] = "0.1"


def _coordinate_space_wrong(payload):
    payload["coordinate_space"] = "region"


def _crop_outside_full_bounds(payload):
    payload["crop_origin_local"] = [3, 0, 0]


def _empty_flag_lies(payload):
    payload["empty"] = True


def _scene_hash_not_a_string(payload):
    payload["scene_hash"] = 5


@pytest.mark.parametrize(
    "corruptor, code",
    [
        pytest.param(_state_out_of_palette, INVALID_RENDER_PALETTE_INDEX, id="state-index"),
        pytest.param(_state_negative, INVALID_RENDER_PALETTE_INDEX, id="state-negative"),
        pytest.param(_state_length_mismatch, ARRAY_LENGTH_MISMATCH, id="state-length"),
        pytest.param(_idx_above_volume, INDEX_OUT_OF_RANGE, id="idx-above-volume"),
        pytest.param(_idx_negative, INDEX_OUT_OF_RANGE, id="idx-negative"),
        pytest.param(_idx_duplicate, DUPLICATE_VOXEL_INDEX, id="idx-duplicate"),
        pytest.param(_idx_not_an_integer, RENDER_PAYLOAD_MISMATCH, id="idx-not-int"),
        pytest.param(_unknown_field, UNKNOWN_RENDER_FIELD, id="unknown-field"),
        pytest.param(_unknown_bounds_field, UNKNOWN_RENDER_FIELD, id="unknown-bounds-field"),
        pytest.param(_unknown_counts_field, UNKNOWN_RENDER_FIELD, id="unknown-counts-field"),
        pytest.param(_unknown_palette_field, UNKNOWN_RENDER_FIELD, id="unknown-palette-field"),
        pytest.param(_palette_empty, INVALID_RENDER_PALETTE_INDEX, id="palette-empty"),
        pytest.param(
            _palette_display_index_not_air, INVALID_RENDER_PALETTE_INDEX, id="palette-0-not-air"
        ),
        pytest.param(
            _palette_entry_missing_props, RENDER_PAYLOAD_MISMATCH, id="palette-entry-shape"
        ),
        pytest.param(_counts_inconsistent, RENDER_PAYLOAD_MISMATCH, id="counts-inconsistent"),
        pytest.param(_counts_missing, RENDER_PAYLOAD_MISMATCH, id="counts-missing"),
        pytest.param(_schema_version_old, RENDER_PAYLOAD_MISMATCH, id="schema-version"),
        pytest.param(_coordinate_space_wrong, RENDER_PAYLOAD_MISMATCH, id="coordinate-space"),
        pytest.param(_crop_outside_full_bounds, RENDER_PAYLOAD_MISMATCH, id="crop-outside"),
        pytest.param(_empty_flag_lies, RENDER_PAYLOAD_MISMATCH, id="empty-flag"),
        pytest.param(_scene_hash_not_a_string, RENDER_PAYLOAD_MISMATCH, id="scene-hash-type"),
    ],
)
def test_validate_rejects_corrupted_payloads(tmp_path, corruptor, code):
    scene = _basic(tmp_path)
    payload = build_render_scene(scene, scene_id="c011", file_sha256="c" * 64)
    validate_render_scene(payload, expected_scene_hash=payload["scene_hash"])

    bad = copy.deepcopy(payload)
    corruptor(bad)
    with pytest.raises(RenderSceneError) as exc:
        validate_render_scene(bad)
    assert exc.value.code == code
    assert exc.value.detail, "an error code without a detail is not diagnosable"
    assert exc.value.to_dict()["code"] == code
    assert str(exc.value).startswith(code)

    # the untouched payload is still fine: the corruption is what was rejected
    validate_render_scene(payload, expected_scene_hash=payload["scene_hash"])


def test_validate_rejects_unknown_field_before_anything_else():
    # unknown fields must never be silently tolerated, not even with a wrong hash
    payload = {"schema_version": RENDER_SCENE_SCHEMA_VERSION, "surprise": 1}
    with pytest.raises(RenderSceneError) as exc:
        validate_render_scene(payload, expected_scene_hash="x")
    assert exc.value.code == UNKNOWN_RENDER_FIELD
    assert exc.value.field == "surprise"


def test_air_display_index_requires_air_at_zero(tmp_path):
    scene = _basic(tmp_path)
    payload = build_render_scene(scene, scene_id="c012")
    payload["palette"][0] = {"name": STONE, "props": {}}
    with pytest.raises(RenderSceneError) as exc:
        air_display_index(payload)
    assert exc.value.code == INVALID_RENDER_PALETTE_INDEX

    with pytest.raises(RenderSceneError) as exc2:
        air_display_index({"palette": []})
    assert exc2.value.code == INVALID_RENDER_PALETTE_INDEX

    with pytest.raises(RenderSceneError) as exc3:
        air_display_index({"palette": [{"name": AIR, "props": {"waterlogged": "false"}}]})
    assert exc3.value.code == INVALID_RENDER_PALETTE_INDEX


# ---------- determinism, read-only, summary ----------


def test_build_is_deterministic(tmp_path):
    scene = _basic(tmp_path)
    first = build_render_scene(
        scene, scene_id="c005", file_sha256="d" * 64, resource_hash="assets-v1"
    )
    second = build_render_scene(
        scene, scene_id="c005", file_sha256="d" * 64, resource_hash="assets-v1"
    )
    assert first == second
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    # key order is part of the delivery contract, so compare without sorting too
    assert json.dumps(first) == json.dumps(second)

    # an independent re-read of the same file produces the same payload
    reloaded = load_scene(str(tmp_path / "basic.litematic"))
    third = build_render_scene(
        reloaded, scene_id="c005", file_sha256="d" * 64, resource_hash="assets-v1"
    )
    assert json.dumps(third, sort_keys=True) == json.dumps(first, sort_keys=True)
    assert third["scene_hash"] == first["scene_hash"]


def test_build_never_modifies_the_source(tmp_path):
    scene = _basic(tmp_path)
    path = tmp_path / "basic.litematic"

    before_nbt = copy.deepcopy(scene.raw_nbt)
    before_states = {p: scene.snapshot.block_at_local(p) for p in scene.snapshot.iter_local()}
    before_bytes = path.read_bytes()

    def source_palette():
        entries = scene.raw_nbt["Regions"]["main"]["BlockStatePalette"]
        return [
            (
                str(entry["Name"]),
                sorted((str(k), str(v)) for k, v in entry.get("Properties", {}).items()),
            )
            for entry in entries
        ]

    before_palette = source_palette()
    assert sum(1 for name, _ in before_palette if name == "minecraft:oak_stairs") == 2

    payload = build_render_scene(
        scene, scene_id="c006", crop_origin_local=(1, 0, 1), crop_size=(2, 2, 2)
    )
    validate_render_scene(payload)

    assert scene.raw_nbt == before_nbt
    assert source_palette() == before_palette
    assert {p: scene.snapshot.block_at_local(p) for p in scene.snapshot.iter_local()} == before_states
    assert path.read_bytes() == before_bytes


def test_summary_drops_arrays_and_flags_a_missing_file_hash(tmp_path):
    scene = _basic(tmp_path)
    with_hash = build_render_scene(
        scene, scene_id="c007", file_sha256="e" * 64, resource_hash="assets-v1"
    )
    without_hash = build_render_scene(scene, scene_id="c007")
    assert "file_sha256" not in without_hash
    validate_render_scene(without_hash)

    summary = summarize_for_client(with_hash)
    assert "idx" not in summary
    assert "state" not in summary
    assert "idx" not in json.dumps(summary)
    assert "state" not in json.dumps(summary)
    assert summary["scene_id"] == "c007"
    assert summary["scene_hash"] == with_hash["scene_hash"] == without_hash["scene_hash"]
    assert summary["size"] == [4, 2, 3]
    assert summary["crop_origin_local"] == [0, 0, 0]
    assert summary["full_scene_bounds"] == {"min": [0, 0, 0], "max_exclusive": [4, 2, 3]}
    assert summary["palette_size"] == len(with_hash["palette"]) == 6
    assert summary["counts"] == {"non_air_voxels": 5, "voxels": 24}
    assert summary["file_sha256"] == "e" * 64
    assert summary["file_sha256_missing"] is False
    assert summary["diagnostics"]["file_sha256_missing"] is False
    assert summary["diagnostics"]["crop_is_full_scene"] is True
    assert summary["diagnostics"]["unknown_fields"] == []

    bare = summarize_for_client(without_hash)
    assert "file_sha256" not in bare
    assert bare["file_sha256_missing"] is True
    assert bare["diagnostics"]["file_sha256_missing"] is True
    assert "file_sha256_missing" in json.dumps(bare)

    cropped = summarize_for_client(
        build_render_scene(scene, scene_id="c007", crop_origin_local=(1, 0, 1), crop_size=(2, 2, 2))
    )
    assert cropped["crop_origin_local"] == [1, 0, 1]
    assert cropped["size"] == [2, 2, 2]
    assert cropped["diagnostics"]["crop_is_full_scene"] is False
