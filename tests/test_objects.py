"""Acceptance: P3 provenance - ObjectRecord/ObjectRegistry and safe replacement.

Covers spec 11.3 / 11.4: the registry answers which object owns which voxels, that
an object may only be withdrawn when its record is complete, its owned state is
still on the ground, every claim is inside the authorised selection and no later
object depends on it - and the rollback writes the recorded ``substrate``, never
air and never B0.
"""
from __future__ import annotations

import json

import pytest

from litegarden.objects import (
    ObjectError,
    ObjectRecord,
    ObjectRegistry,
    TargetDependencyConflict,
    TargetOwnershipConflict,
    key_of,
    record_from_compile,
)

AIR = "minecraft:air"
GRASS = "minecraft:grass_block"
DIRT = "minecraft:dirt"
STONE = "minecraft:stone"
ANDESITE = "minecraft:andesite"
PLANKS = "minecraft:oak_planks"
COBBLE = "minecraft:cobblestone"

# a selection big enough for every fixture object below
SELECTION_MIN = (0, 0, 0)
SELECTION_MAX = (16, 8, 16)


def _pos(key: str) -> tuple:
    return tuple(int(part) for part in key.split(","))


def _reader(*records: ObjectRecord) -> dict:
    """A ``state_at`` reader whose scene holds exactly the recorded owned states."""
    scene: dict = {}
    for record in records:
        for key, state in record.owned.items():
            scene[_pos(key)] = state
    return scene


def _pavilion(**overrides) -> ObjectRecord:
    """A recorded pavilion: foundation dug into a hill, deck placed above it."""
    data = dict(
        object_id="pavilion_003",
        kind="asset",
        creation_revision="r003",
        operation_ids=("pavilion_1", "lights_1"),
        asset_version="pavilion_small@2026-09-23",
        occupied_voxels=[(5, 1, 5), (6, 1, 5)],
        required_empty=[(5, 1, 4)],
        support=[(5, 0, 5), (6, 0, 5)],
        entry=[(5, 1, 4)],
        footprint=[(5, 0, 5), (6, 0, 5), (5, 1, 5), (6, 1, 5)],
        write_set=[(5, 0, 5), (6, 0, 5), (5, 1, 5), (6, 1, 5), (5, 1, 4)],
        read_dependencies=[],
        boundary_links=[
            {"pos": [5, 1, 4], "direction": "north", "width": 1, "object_id": "path_1"}
        ],
        substrate={"5,0,5": DIRT, "6,0,5": DIRT, "5,1,5": AIR, "6,1,5": AIR, "5,1,4": GRASS},
        owned={"5,0,5": STONE, "6,0,5": STONE, "5,1,5": PLANKS, "6,1,5": PLANKS, "5,1,4": AIR},
        complete=True,
    )
    data.update(overrides)
    return ObjectRecord(**data)


def _lamp(**overrides) -> ObjectRecord:
    """A small recorded lamp, far from the pavilion."""
    data = dict(
        object_id="lamp_1",
        kind="decoration",
        creation_revision="r003",
        operation_ids=("lights_1",),
        asset_version="lamp_small@2026-09-23",
        occupied_voxels=[(9, 1, 9)],
        required_empty=[],
        support=[(9, 0, 9)],
        entry=[],
        footprint=[(9, 0, 9), (9, 1, 9)],
        write_set=[(9, 0, 9), (9, 1, 9)],
        read_dependencies=[],
        boundary_links=[],
        substrate={"9,0,9": DIRT, "9,1,9": AIR},
        owned={"9,0,9": ANDESITE, "9,1,9": "minecraft:lantern"},
        complete=True,
    )
    data.update(overrides)
    return ObjectRecord(**data)


def _path(**overrides) -> ObjectRecord:
    """A recorded road segment that *reads* the pavilion entry without writing it."""
    data = dict(
        object_id="path_1",
        kind="path",
        creation_revision="r003",
        operation_ids=("path_1",),
        asset_version=None,
        occupied_voxels=[(5, 1, 3), (6, 1, 3)],
        required_empty=[],
        support=[(5, 0, 3), (6, 0, 3)],
        entry=[],
        footprint=[(5, 0, 3), (6, 0, 3), (5, 1, 3), (6, 1, 3)],
        write_set=[(5, 0, 3), (6, 0, 3), (5, 1, 3), (6, 1, 3)],
        read_dependencies=["pavilion_003"],
        boundary_links=[],
        substrate={"5,0,3": DIRT, "5,1,3": GRASS, "6,0,3": DIRT, "6,1,3": GRASS},
        owned={"5,0,3": DIRT, "5,1,3": COBBLE, "6,0,3": DIRT, "6,1,3": COBBLE},
        complete=True,
    )
    data.update(overrides)
    return ObjectRecord(**data)


# --------------------------------------------------------------------------
# serialisation
# --------------------------------------------------------------------------


def test_record_round_trip_uses_lists_and_dotted_keys():
    record = _pavilion()
    as_dict = record.to_dict()

    assert as_dict["object_id"] == "pavilion_003"
    assert as_dict["kind"] == "asset"
    assert as_dict["asset_version"] == "pavilion_small@2026-09-23"
    assert as_dict["operation_ids"] == ["pavilion_1", "lights_1"]
    # coordinates are [x, y, z] lists, sorted (x, y, z)
    assert as_dict["occupied_voxels"] == [[5, 1, 5], [6, 1, 5]]
    assert as_dict["write_set"] == [[5, 0, 5], [5, 1, 4], [5, 1, 5], [6, 0, 5], [6, 1, 5]]
    assert as_dict["read_dependencies"] == []
    assert as_dict["boundary_links"][0]["pos"] == [5, 1, 4]
    assert as_dict["boundary_links"][0]["direction"] == "north"
    # state maps use "x,y,z" string keys, in (x, y, z) order
    assert list(as_dict["substrate"]) == ["5,0,5", "5,1,4", "5,1,5", "6,0,5", "6,1,5"]
    assert as_dict["substrate"]["5,0,5"] == DIRT
    assert as_dict["owned"]["5,0,5"] == STONE
    assert as_dict["complete"] is True

    assert json.loads(json.dumps(as_dict)) == as_dict
    assert ObjectRecord.from_dict(as_dict) == record
    assert ObjectRecord.from_dict(json.loads(json.dumps(as_dict))) == record

    registry = ObjectRegistry({"pavilion_003": record})
    payload = registry.to_dict()
    assert payload["schema_version"] == "0.2"
    assert payload["count"] == 1
    assert payload["objects"] == [as_dict]
    rebuilt = ObjectRegistry.from_dict(json.loads(json.dumps(payload)))
    assert rebuilt.to_dict() == payload
    assert rebuilt.get("pavilion_003") == record
    assert len(rebuilt) == 1
    assert "pavilion_003" in rebuilt
    assert list(rebuilt) == ["pavilion_003"]
    assert rebuilt.ids == ("pavilion_003",)

    empty = ObjectRegistry()
    assert empty.to_dict() == {"schema_version": "0.2", "count": 0, "objects": []}
    assert len(ObjectRegistry.from_dict(empty.to_dict())) == 0
    assert empty.records() == ()


def test_record_from_compile_records_write_set_substrate_and_owned():
    base = {(4, 0, 4): GRASS, (4, 1, 4): AIR, (5, 0, 5): DIRT}
    final = {(4, 0, 4): STONE, (4, 1, 4): PLANKS, (5, 0, 5): DIRT}

    record = record_from_compile(
        object_id="hut_1",
        creation_revision="r004",
        base_states=base,
        final_states=final,
        op_id_kind={"hut_1": "asset", "lights_1": "decoration"},
    )

    # (5,0,5) is unchanged in the working view: not a write, hence not owned
    assert record.write_set == ((4, 0, 4), (4, 1, 4))
    assert record.substrate == {"4,0,4": GRASS, "4,1,4": AIR}
    assert record.owned == {"4,0,4": STONE, "4,1,4": PLANKS}
    assert record.occupied_voxels == ((4, 0, 4), (4, 1, 4))
    assert record.kind == "asset"  # two different tags -> the documented default
    assert record.operation_ids == ("hut_1", "lights_1")
    assert "4,1,4" in record.substrate and "5,0,5" not in record.substrate

    single = record_from_compile(
        object_id="hut_1",
        creation_revision="r004",
        base_states=base,
        final_states=final,
        op_id_kind={"hut_1": "asset"},
        support=[(4, 0, 4)],
    )
    assert single.kind == "asset"
    assert single.support == ((4, 0, 4),)
    assert single.claimed_voxels() == ((4, 0, 4), (4, 1, 4))

    # a clearance write is part of the write set but not an occupied voxel
    cleared = record_from_compile(
        object_id="door_1",
        kind="asset",
        creation_revision="r004",
        base_states={(4, 1, 4): STONE},
        final_states={(4, 1, 4): AIR},
    )
    assert cleared.write_set == ((4, 1, 4),)
    assert cleared.occupied_voxels == ()
    assert cleared.substrate == {"4,1,4": STONE}
    assert cleared.owned == {"4,1,4": AIR}

    # a written voxel without a recorded pre-construction state is refused, not
    # assumed to have been air
    with pytest.raises(ObjectError) as info:
        record_from_compile(
            object_id="hut_1",
            creation_revision="r004",
            base_states={(4, 0, 4): GRASS},
            final_states={(4, 0, 4): STONE, (4, 1, 4): PLANKS},
        )
    assert "assume air" in str(info.value)

    with pytest.raises(ObjectError):
        record_from_compile(
            object_id="hut_1",
            creation_revision="r004",
            base_states=base,
            final_states=final,
            extra={"unvalidated": True},
        )


def test_coordinate_sequences_are_sorted_and_deduplicated():
    record = ObjectRecord(
        object_id="road_1",
        kind="path",
        creation_revision="r001",
        write_set=[(9, 0, 0), (0, 0, 0), (0, 0, 0), (2, 1, 0)],
    )
    assert record.write_set == ((0, 0, 0), (2, 1, 0), (9, 0, 0))
    assert key_of(record.write_set[2]) == "9,0,0"


def test_malformed_records_and_registries_are_refused():
    with pytest.raises(ObjectError):
        ObjectRecord(object_id="", kind="asset", creation_revision="r001")
    with pytest.raises(ObjectError) as info:
        ObjectRecord(object_id="x", kind="building", creation_revision="r001")
    assert "kind" in str(info.value)
    with pytest.raises(ObjectError):
        ObjectRecord(object_id="x", kind="asset", creation_revision="r001", owned={"5,1": STONE})
    with pytest.raises(ObjectError):
        ObjectRecord(object_id="x", kind="asset", creation_revision="r001", substrate={"5,0,5": 3})
    with pytest.raises(ObjectError):
        ObjectRecord(object_id="x", kind="asset", creation_revision="r001", write_set=[(1, 2, True)])
    with pytest.raises(ObjectError):
        ObjectRecord(object_id="x", kind="asset", creation_revision="r001", complete="yes")

    with pytest.raises(ObjectError):
        ObjectRecord.from_dict({"kind": "asset"})
    with pytest.raises(ObjectError):
        ObjectRecord.from_dict({"object_id": "x", "kind": "asset", "substrate": {"nope": AIR}})
    with pytest.raises(ObjectError):
        ObjectRecord.from_dict({"object_id": "x", "kind": "asset", "complete": "yes"})
    with pytest.raises(ObjectError):
        ObjectRegistry.from_dict({"objects": "nope"})
    with pytest.raises(ObjectError):
        ObjectRegistry.from_dict({"count": 0})
    with pytest.raises(ObjectError):
        ObjectRegistry({"other_id": _pavilion()})

    registry = ObjectRegistry({"pavilion_003": _pavilion()})
    with pytest.raises(ObjectError) as info:
        registry.register(_pavilion())
    assert "already registered" in str(info.value)
    with pytest.raises(ObjectError):
        registry.get("missing")
    with pytest.raises(ObjectError):
        registry.overlapping((5, 0, 5), (5, 4, 4))


def test_registry_iteration_is_deterministic():
    registry = ObjectRegistry({"lamp_1": _lamp(), "path_1": _path(), "pavilion_003": _pavilion()})
    assert list(registry) == ["lamp_1", "path_1", "pavilion_003"]
    assert [r.object_id for r in registry.records()] == ["lamp_1", "path_1", "pavilion_003"]


# --------------------------------------------------------------------------
# hanging geometry queries
# --------------------------------------------------------------------------


def test_overlapping_and_partial_intersections_use_half_open_boxes():
    registry = ObjectRegistry({"pavilion_003": _pavilion()})
    record = registry.get("pavilion_003")
    assert record.claimed_voxels() == (
        (5, 0, 5),
        (5, 1, 4),
        (5, 1, 5),
        (6, 0, 5),
        (6, 1, 5),
    )

    # a box touching the object at all overlaps it
    assert [r.object_id for r in registry.overlapping((5, 1, 5), (6, 2, 6))] == ["pavilion_003"]
    assert [r.object_id for r in registry.overlapping((0, 0, 0), (7, 8, 6))] == ["pavilion_003"]

    # half-open: max_exclusive is not part of the box
    assert [r.object_id for r in registry.overlapping((0, 0, 0), (6, 8, 5))] == ["pavilion_003"]
    assert registry.overlapping((0, 0, 0), (5, 8, 5)) == []
    assert registry.overlapping((0, 0, 0), (5, 8, 6)) == []
    assert registry.overlapping((7, 0, 0), (9, 8, 4)) == []
    assert registry.overlapping((5, 2, 0), (7, 9, 9)) == []
    assert registry.overlapping((11, 0, 0), (12, 1, 1)) == []

    # partial: the object has voxels both inside and outside the box
    partial = registry.partial_intersections((0, 0, 0), (6, 2, 5))
    assert [entry["object_id"] for entry in partial] == ["pavilion_003"]
    assert partial[0]["inside_positions"] == [[5, 1, 4]]
    assert partial[0]["outside_positions"] == [[5, 0, 5], [5, 1, 5], [6, 0, 5], [6, 1, 5]]
    assert partial[0]["inside_count"] == 1
    assert partial[0]["outside_count"] == 4
    assert partial[0]["claimed_box"] == {"min": [5, 0, 4], "max_exclusive": [7, 2, 6]}
    assert partial[0]["kind"] == "asset"

    # fully inside is not a partial intersection, and neither is "no overlap"
    assert registry.partial_intersections((0, 0, 0), (16, 8, 16)) == []
    assert registry.partial_intersections((20, 20, 20), (24, 24, 24)) == []
    two = ObjectRegistry({"pavilion_003": _pavilion(), "lamp_1": _lamp()})
    # the lamp is far outside while the pavilion is cut (its z=5 voxels fall out)
    assert [e["object_id"] for e in two.partial_intersections((0, 0, 0), (7, 8, 5))] == [
        "pavilion_003"
    ]


# --------------------------------------------------------------------------
# validate_targets
# --------------------------------------------------------------------------


def test_validate_targets_accepts_a_fully_authorised_target():
    record = _pavilion()
    registry = ObjectRegistry({"pavilion_003": record})
    result = registry.validate_targets(
        ["pavilion_003"],
        selection_min=SELECTION_MIN,
        selection_max_exclusive=SELECTION_MAX,
        state_at=_reader(record).get,
    )
    assert result is None


def test_validate_targets_rejects_an_unknown_target():
    registry = ObjectRegistry({"pavilion_003": _pavilion()})
    with pytest.raises(TargetOwnershipConflict) as info:
        registry.validate_targets(
            ["pavilion_999"],
            selection_min=SELECTION_MIN,
            selection_max_exclusive=SELECTION_MAX,
            state_at=lambda pos: AIR,
        )
    exc = info.value
    assert exc.object_id == "pavilion_999"
    assert exc.details["reason"] == "unknown_object"
    assert "not in the generation-source registry" in str(exc)


def test_validate_targets_rejects_imported_or_incomplete_sources():
    imported = _pavilion(object_id="old_house", kind="imported", complete=False)
    registry = ObjectRegistry({"old_house": imported})
    with pytest.raises(TargetOwnershipConflict) as info:
        registry.validate_targets(
            ["old_house"],
            selection_min=SELECTION_MIN,
            selection_max_exclusive=SELECTION_MAX,
            state_at=_reader(imported).get,
        )
    exc = info.value
    assert exc.object_id == "old_house"
    assert exc.details == {"reason": "incomplete_source", "kind": "imported", "complete": False}

    half_known = _pavilion(object_id="half_known", complete=False)
    registry = ObjectRegistry({"half_known": half_known})
    with pytest.raises(TargetOwnershipConflict) as info:
        registry.validate_targets(
            ["half_known"],
            selection_min=SELECTION_MIN,
            selection_max_exclusive=SELECTION_MAX,
            state_at=_reader(half_known).get,
        )
    assert info.value.object_id == "half_known"
    assert info.value.details["complete"] is False


def test_validate_targets_rejects_claims_outside_the_selection():
    record = _pavilion()
    registry = ObjectRegistry({"pavilion_003": record})
    with pytest.raises(TargetOwnershipConflict) as info:
        registry.validate_targets(
            ["pavilion_003"],
            selection_min=(5, 0, 5),
            selection_max_exclusive=(7, 2, 6),
            state_at=_reader(record).get,
        )
    exc = info.value
    assert exc.object_id == "pavilion_003"
    assert exc.details["reason"] == "outside_selection"
    assert (5, 1, 4) in exc.positions  # the doorway reaches outside
    assert (5, 1, 5) not in exc.positions
    assert exc.details["outside_count"] == 1
    assert exc.details["selection_min"] == [5, 0, 5]


def test_validate_targets_rejects_a_changed_owned_state():
    record = _pavilion()
    registry = ObjectRegistry({"pavilion_003": record})
    scene = _reader(record)
    scene[(6, 1, 5)] = COBBLE  # a later edit overwrote the deck
    with pytest.raises(TargetOwnershipConflict) as info:
        registry.validate_targets(
            ["pavilion_003"],
            selection_min=SELECTION_MIN,
            selection_max_exclusive=SELECTION_MAX,
            state_at=scene.get,
        )
    exc = info.value
    assert exc.object_id == "pavilion_003"
    assert exc.details["reason"] == "owned_state_changed"
    assert exc.positions == ((6, 1, 5),)
    assert exc.details["mismatches"] == [
        {"pos": [6, 1, 5], "expected": PLANKS, "actual": COBBLE}
    ]


def test_validate_targets_reports_later_dependencies():
    pavilion = _pavilion()
    path = _path()
    registry = ObjectRegistry({"pavilion_003": pavilion, "path_1": path})
    with pytest.raises(TargetDependencyConflict) as info:
        registry.validate_targets(
            ["pavilion_003"],
            selection_min=SELECTION_MIN,
            selection_max_exclusive=SELECTION_MAX,
            state_at=_reader(pavilion, path).get,
        )
    exc = info.value
    assert exc.dependents == ({"object_id": "path_1", "depends_on": ["pavilion_003"]},)
    assert exc.object_id == "path_1"
    assert exc.details["targets"] == ["pavilion_003"]
    assert "path_1" in str(exc)

    # widening the authorised target set to cover the dependent is the remedy
    assert (
        registry.validate_targets(
            ["pavilion_003", "path_1"],
            selection_min=SELECTION_MIN,
            selection_max_exclusive=SELECTION_MAX,
            state_at=_reader(pavilion, path).get,
        )
        is None
    )


# --------------------------------------------------------------------------
# plan_restore
# --------------------------------------------------------------------------


def test_plan_restore_returns_substrate_not_air_and_not_b0():
    record = _pavilion()
    registry = ObjectRegistry({"pavilion_003": record})
    scene = _reader(record)
    # the import (B0) had grass under the pavilion; the construction dug through it
    b0 = {(5, 0, 5): GRASS, (6, 0, 5): GRASS, (5, 1, 4): GRASS}

    plan = registry.plan_restore(["pavilion_003"], state_at=scene.get)
    restored = dict(plan)

    assert restored[(5, 0, 5)] == DIRT
    assert restored[(5, 0, 5)] != AIR
    assert restored[(5, 0, 5)] != b0[(5, 0, 5)]
    assert restored[(6, 0, 5)] == DIRT
    # this deck voxel really was air before the construction, and the record says so
    assert restored[(5, 1, 5)] == AIR
    # the cleared doorway goes back to the grass that was there
    assert restored[(5, 1, 4)] == GRASS
    # deterministic (x, y, z) order, one entry per recorded voxel
    assert [pos for pos, _ in plan] == sorted(restored)
    assert len(plan) == len(record.substrate) == 5

    # the plan is a pure function of the records
    assert registry.plan_restore(["pavilion_003"], state_at=scene.get) == plan
    assert registry.plan_restore(["pavilion_003"], state_at=None) == plan

    other = ObjectRegistry({"pavilion_003": record, "lamp_1": _lamp()})
    both = _reader(record, _lamp())
    forward = other.plan_restore(["pavilion_003", "lamp_1"], state_at=both.get)
    backward = other.plan_restore(["lamp_1", "pavilion_003"], state_at=both.get)
    assert forward == backward
    assert [pos for pos, _ in forward] == sorted(dict(forward))
    assert len(forward) == 7


def test_plan_restore_refuses_a_write_without_a_recorded_substrate():
    record = _pavilion(substrate={"5,0,5": DIRT})  # the rest of the write set is unrecorded
    registry = ObjectRegistry({"pavilion_003": record})
    with pytest.raises(TargetOwnershipConflict) as info:
        registry.plan_restore(["pavilion_003"], state_at=_reader(record).get)
    exc = info.value
    assert exc.object_id == "pavilion_003"
    assert exc.details["reason"] == "substrate_incomplete"
    assert (5, 0, 5) not in exc.positions
    assert (5, 1, 5) in exc.positions
    assert exc.details["missing_count"] == len(exc.positions) == 4
    assert "refusing to assume air or B0" in str(exc)


def test_plan_restore_refuses_a_scene_that_no_longer_matches_the_record():
    record = _pavilion()
    registry = ObjectRegistry({"pavilion_003": record})
    scene = _reader(record)
    scene[(5, 1, 5)] = STONE  # somebody rebuilt the deck differently
    with pytest.raises(TargetOwnershipConflict) as info:
        registry.plan_restore(["pavilion_003"], state_at=scene.get)
    exc = info.value
    assert exc.object_id == "pavilion_003"
    assert exc.details["reason"] == "owned_state_changed"
    assert exc.positions == ((5, 1, 5),)


def test_plan_restore_refuses_conflicting_shared_foundations():
    shared = ObjectRegistry(
        {
            "pavilion_003": _pavilion(),
            "lamp_1": _lamp(
                substrate={"5,0,5": ANDESITE, "9,0,9": DIRT, "9,1,9": AIR},
                write_set=[(5, 0, 5), (9, 0, 9), (9, 1, 9)],
            ),
        }
    )
    with pytest.raises(TargetOwnershipConflict) as info:
        shared.plan_restore(["pavilion_003", "lamp_1"], state_at=None)
    exc = info.value
    assert exc.details["reason"] == "substrate_conflict"
    assert exc.positions == ((5, 0, 5),)
    assert exc.details["states"] == sorted([DIRT, ANDESITE])


def test_plan_restore_refuses_imported_or_unknown_targets():
    imported = _pavilion(object_id="old_house", kind="imported", complete=False)
    registry = ObjectRegistry({"old_house": imported})
    with pytest.raises(TargetOwnershipConflict) as info:
        registry.plan_restore(["old_house"], state_at=_reader(imported).get)
    assert info.value.details["reason"] == "incomplete_source"

    with pytest.raises(TargetOwnershipConflict) as info:
        registry.plan_restore(["ghost"], state_at=None)
    assert info.value.object_id == "ghost"
    assert info.value.details["reason"] == "unknown_object"

    with pytest.raises(ObjectError):
        registry.plan_restore(["old_house"], state_at=42)
