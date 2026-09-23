"""Type-sensitive NBT comparison tests (spec 7.1, acceptance C01/C02).

These tests are deliberately built from hand-assembled ``nbtlib.tag`` trees:
the point of ``litegarden.nbt_compare`` is that *numerically identical* tags of
different types (``Short(5)`` vs ``Int(5)``) are a failure, which a byte-for-byte
round trip through litemapy would not exercise on its own.
"""
from __future__ import annotations

import copy
import struct

import pytest
from nbtlib.tag import (
    Byte,
    ByteArray,
    Compound,
    Double,
    Float,
    Int,
    IntArray,
    List,
    Long,
    LongArray,
    Short,
    String,
)

from litegarden.nbt_compare import (
    DIFFERENCE_CODES,
    NBT_FIELD_MISSING,
    NBT_TYPE_CHANGED,
    NBT_UNAUTHORIZED_FIELD_CHANGE,
    NBT_VALUE_CHANGED,
    NbtDifference,
    NbtPreservationError,
    allowed_save_paths,
    compare_nbt,
    ensure_nbt_preserved,
    is_path_allowed,
)

MAIN = "main"

# The same quiet-NaN bit patterns used to prove the comparison is bit level.
NAN_BITS = 0x7FF8000000000000
NAN_BITS_OTHER_PAYLOAD = 0x7FF8000000000001


def _double_from_bits(bits: int) -> float:
    """Build a Python float whose IEEE-754 bits are exactly ``bits``."""
    return struct.unpack("<d", bits.to_bytes(8, "little"))[0]


def _tree() -> Compound:
    """A minimal litematic-shaped tree with one region, one entity, one chest.

    The first chest item stores its ``count`` as a ``Short``: that is the C01
    case ("values unchanged, Short rewritten as Int must fail").
    """
    return Compound(
        {
            "MinecraftDataVersion": Int(3465),
            "Version": Int(6),
            "Metadata": Compound(
                {
                    "Name": String("sample"),
                    "Author": String("tester"),
                    "Description": String("original"),
                    "TotalBlocks": Int(12),
                    "EnclosingSize": Compound({"x": Int(3), "y": Int(3), "z": Int(3)}),
                }
            ),
            "Regions": Compound(
                {
                    MAIN: Compound(
                        {
                            "Position": IntArray([0, 64, 0]),
                            "Size": IntArray([3, 3, 3]),
                            "BlockStatePalette": List(
                                [Compound({"Name": String("minecraft:stone")})]
                            ),
                            "BlockStates": LongArray([1, 1, 1, 1]),
                            "PendingBlockTicks": List[Compound]([]),
                            "Entities": List(
                                [
                                    Compound(
                                        {
                                            "id": String("minecraft:pig"),
                                            "Health": Float(10.0),
                                        }
                                    )
                                ]
                            ),
                            "TileEntities": List(
                                [
                                    Compound(
                                        {
                                            "id": String("minecraft:chest"),
                                            "Items": List(
                                                [
                                                    Compound(
                                                        {
                                                            "Slot": Byte(0),
                                                            "id": String(
                                                                "minecraft:diamond"
                                                            ),
                                                            "count": Short(5),
                                                        }
                                                    ),
                                                    Compound(
                                                        {
                                                            "Slot": Byte(1),
                                                            "id": String("minecraft:stick"),
                                                            "count": Int(64),
                                                        }
                                                    ),
                                                ]
                                            ),
                                        }
                                    )
                                ]
                            ),
                        }
                    )
                }
            ),
            # Original extension data that a save must carry through untouched.
            "Extension": Compound(
                {
                    "seed": Long(42),
                    "note": String("keep me"),
                    "ticks": List([Int(1), Int(2)]),
                    "mask": ByteArray([1, 2, 3]),
                    "weights": IntArray([1, 2, 3]),
                    "bias": Double(0.0),
                    "payload": Double(_double_from_bits(NAN_BITS)),
                }
            ),
        }
    )


def _extension(tree: Compound) -> Compound:
    return tree["Extension"]


def _region(tree: Compound) -> Compound:
    return tree["Regions"][MAIN]


def _chest(tree: Compound) -> Compound:
    return _region(tree)["TileEntities"][0]


def _item(tree: Compound, index: int) -> Compound:
    return _chest(tree)["Items"][index]


def _single_diff(differences: list[NbtDifference]) -> NbtDifference:
    """Assert there is exactly one difference and return it."""
    assert len(differences) == 1, [(d.code, d.tag_path) for d in differences]
    return differences[0]


# --------------------------------------------------------------------------- #
# Type-first comparison (C01)
# --------------------------------------------------------------------------- #
def test_short_to_int_same_value_is_type_change():
    original = _tree()
    current = copy.deepcopy(original)
    # Only the tag class changes: the numbers are identical.
    _item(current, 0)["count"] = Int(int(_item(original, 0)["count"]))
    assert int(_item(current, 0)["count"]) == int(_item(original, 0)["count"])
    assert Short(5) == Int(5)  # the trap this comparator exists for

    diff = _single_diff(compare_nbt(original, current, ()))

    assert diff.code == NBT_TYPE_CHANGED
    assert diff.tag_path == "Regions.main.TileEntities[0].Items[0].count"
    assert diff.original_type == "Short"
    assert diff.current_type == "Int"
    assert "5" in diff.original_value
    assert "5" in (diff.current_value or "")

    with pytest.raises(NbtPreservationError) as excinfo:
        ensure_nbt_preserved(original, current, allowed_save_paths(MAIN))
    assert [d.tag_path for d in excinfo.value.differences] == [diff.tag_path]


def test_int_to_long_same_value_is_type_change():
    original = _tree()
    current = copy.deepcopy(original)
    _item(current, 1)["count"] = Long(64)
    assert int(_item(current, 1)["count"]) == int(_item(original, 1)["count"])

    diff = _single_diff(compare_nbt(original, current, ()))

    assert diff.code == NBT_TYPE_CHANGED
    assert diff.tag_path == "Regions.main.TileEntities[0].Items[1].count"
    assert diff.original_type == "Int"
    assert diff.current_type == "Long"


def test_float_to_double_is_type_change():
    original = _tree()
    current = copy.deepcopy(original)
    _region(current)["Entities"][0]["Health"] = Double(10.0)
    assert float(_region(current)["Entities"][0]["Health"]) == float(
        _region(original)["Entities"][0]["Health"]
    )

    diff = _single_diff(compare_nbt(original, current, ()))

    assert diff.code == NBT_TYPE_CHANGED
    assert diff.tag_path == "Regions.main.Entities[0].Health"
    assert diff.original_type == "Float"
    assert diff.current_type == "Double"


def test_byte_to_int_same_value_is_type_change():
    original = _tree()
    current = copy.deepcopy(original)
    _item(current, 0)["Slot"] = Int(0)

    diff = _single_diff(compare_nbt(original, current, ()))

    assert diff.code == NBT_TYPE_CHANGED
    assert diff.tag_path == "Regions.main.TileEntities[0].Items[0].Slot"
    assert diff.original_type == "Byte"
    assert diff.current_type == "Int"


def test_compound_to_int_is_type_change():
    original = _tree()
    current = copy.deepcopy(original)
    _region(current)["EnclosingSize"] = Int(1)
    original = copy.deepcopy(original)
    original["Regions"][MAIN]["EnclosingSize"] = Compound({"x": Int(1)})

    diff = _single_diff(compare_nbt(original, current, ()))

    assert diff.code == NBT_TYPE_CHANGED
    assert diff.tag_path == "Regions.main.EnclosingSize"
    assert diff.original_type == "Compound"
    assert diff.current_type == "Int"


# --------------------------------------------------------------------------- #
# Compound keys (C02)
# --------------------------------------------------------------------------- #
def test_missing_field_reports_field_missing():
    original = _tree()
    current = copy.deepcopy(original)
    del _extension(current)["seed"]

    diff = _single_diff(compare_nbt(original, current, ()))

    assert diff.code == NBT_FIELD_MISSING
    assert diff.tag_path == "Extension.seed"
    assert diff.original_type == "Long"
    assert diff.current_type is None
    assert diff.current_value is None
    assert "42" in diff.original_value

    with pytest.raises(NbtPreservationError) as excinfo:
        ensure_nbt_preserved(original, current, allowed_save_paths(MAIN))
    assert [d.code for d in excinfo.value.differences] == [NBT_FIELD_MISSING]


def test_extra_field_reports_unauthorized():
    original = _tree()
    current = copy.deepcopy(original)
    _extension(current)["added"] = Byte(7)

    diff = _single_diff(compare_nbt(original, current, ()))

    assert diff.code == NBT_UNAUTHORIZED_FIELD_CHANGE
    assert diff.tag_path == "Extension.added"
    assert diff.original_type is None
    assert diff.current_type == "Byte"
    assert diff.original_value == ""

    with pytest.raises(NbtPreservationError) as excinfo:
        ensure_nbt_preserved(original, current, allowed_save_paths(MAIN))
    assert [d.code for d in excinfo.value.differences] == [
        NBT_UNAUTHORIZED_FIELD_CHANGE
    ]


def test_key_order_is_not_a_difference():
    original = _tree()

    reversed_extension = copy.deepcopy(original)
    extension = reversed_extension["Extension"]
    reversed_extension["Extension"] = Compound(
        {key: extension[key] for key in reversed(list(extension))}
    )
    assert [str(k) for k in reversed_extension["Extension"]] == [
        "payload",
        "bias",
        "weights",
        "mask",
        "ticks",
        "note",
        "seed",
    ]
    assert compare_nbt(original, reversed_extension, ()) == []

    reversed_root = Compound(
        {key: copy.deepcopy(value) for key, value in reversed(list(original.items()))}
    )
    assert compare_nbt(original, reversed_root, ()) == []
    assert compare_nbt(original, copy.deepcopy(original), ()) == []


# --------------------------------------------------------------------------- #
# Lists
# --------------------------------------------------------------------------- #
def test_list_element_type_change():
    original = _tree()
    current = copy.deepcopy(original)
    _extension(current)["ticks"] = List([Short(1), Short(2)])
    assert len(_extension(current)["ticks"]) == len(_extension(original)["ticks"])

    diff = _single_diff(compare_nbt(original, current, ()))

    assert diff.code == NBT_TYPE_CHANGED
    assert diff.tag_path == "Extension.ticks[]"
    assert diff.original_type == "Int"
    assert diff.current_type == "Short"
    assert "element type" in diff.detail


def test_list_reorder_is_a_difference():
    original = _tree()
    current = copy.deepcopy(original)
    _extension(current)["ticks"] = List([Int(2), Int(1)])

    differences = compare_nbt(original, current, ())

    assert [d.tag_path for d in differences] == [
        "Extension.ticks[0]",
        "Extension.ticks[1]",
    ]
    assert all(d.code == NBT_VALUE_CHANGED for d in differences)
    assert differences[0].original_value == "Int(1)"
    assert differences[0].current_value == "Int(2)"


def test_list_length_change():
    original = _tree()
    current = copy.deepcopy(original)
    _extension(current)["ticks"] = List([Int(1), Int(2), Int(3)])

    diff = _single_diff(compare_nbt(original, current, ()))

    assert diff.code == NBT_VALUE_CHANGED
    assert diff.tag_path == "Extension.ticks"
    assert diff.original_type == "List[Int]"
    assert diff.current_type == "List[Int]"
    assert "2 -> 3" in diff.detail


def test_delete_list_element_is_a_length_change():
    original = _tree()
    current = copy.deepcopy(original)
    del _chest(current)["Items"][1]

    diff = _single_diff(compare_nbt(original, current, ()))

    assert diff.code == NBT_VALUE_CHANGED
    assert diff.tag_path == "Regions.main.TileEntities[0].Items"
    assert "2 -> 1" in diff.detail


def test_empty_list_element_type_is_compared():
    original = _tree()
    current = copy.deepcopy(original)
    current["Regions"][MAIN]["PendingBlockTicks"] = List[Int]([])

    diff = _single_diff(compare_nbt(original, current, ()))

    assert diff.code == NBT_TYPE_CHANGED
    assert diff.tag_path == "Regions.main.PendingBlockTicks[]"
    assert diff.original_type == "Compound"
    assert diff.current_type == "Int"

    same_element_type = copy.deepcopy(original)
    same_element_type["Regions"][MAIN]["PendingBlockTicks"] = List[Compound]([])
    assert compare_nbt(original, same_element_type, ()) == []


def test_degenerate_empty_list_element_type_does_not_false_report():
    # A bare ``List([])`` loses the element type (nbtlib degrades it to End);
    # that must not be reported as a type change.
    original = _tree()
    original["Regions"][MAIN]["PendingBlockTicks"] = List([])
    current = copy.deepcopy(original)
    current["Regions"][MAIN]["PendingBlockTicks"] = List[Compound]([])
    assert compare_nbt(original, current, ()) == []


def test_nested_list_element_paths_use_index_segments():
    original = _tree()
    current = copy.deepcopy(original)
    _item(current, 1)["count"] = Long(64)

    diff = _single_diff(compare_nbt(original, current, ()))

    assert diff.tag_path == "Regions.main.TileEntities[0].Items[1].count"
    assert diff.code == NBT_TYPE_CHANGED


# --------------------------------------------------------------------------- #
# Arrays
# --------------------------------------------------------------------------- #
def test_byte_array_content_change():
    original = _tree()
    current = copy.deepcopy(original)
    _extension(current)["mask"] = ByteArray([1, 2, 4])

    diff = _single_diff(compare_nbt(original, current, ()))

    assert diff.code == NBT_VALUE_CHANGED
    assert diff.tag_path == "Extension.mask"
    assert diff.original_type == "ByteArray"
    assert diff.current_type == "ByteArray"
    assert "1, 2, 3" in diff.original_value
    assert "1, 2, 4" in (diff.current_value or "")


def test_int_array_length_change():
    original = _tree()
    current = copy.deepcopy(original)
    _extension(current)["weights"] = IntArray([1, 2, 3, 4])

    diff = _single_diff(compare_nbt(original, current, ()))

    assert diff.code == NBT_VALUE_CHANGED
    assert diff.tag_path == "Extension.weights"
    assert diff.original_type == "IntArray"
    assert "3 -> 4" in diff.detail


def test_long_array_content_change():
    original = _tree()
    current = copy.deepcopy(original)
    region = _region(current)
    region["BlockStates"] = LongArray([1, 1, 1, 9])

    diff = _single_diff(compare_nbt(original, current, ()))

    assert diff.code == NBT_VALUE_CHANGED
    assert diff.tag_path == "Regions.main.BlockStates"
    assert diff.original_type == "LongArray"
    assert "length 4 unchanged" in diff.detail


# --------------------------------------------------------------------------- #
# Floats
# --------------------------------------------------------------------------- #
def test_nan_and_negative_zero_bitwise():
    original = _tree()

    # +0.0 vs -0.0: numerically equal, bitwise different -> must be reported.
    negative_zero = copy.deepcopy(original)
    negative_zero["Extension"]["bias"] = Double(-0.0)
    assert float(original["Extension"]["bias"]) == float(negative_zero["Extension"]["bias"])
    diff = _single_diff(compare_nbt(original, negative_zero, ()))
    assert diff.code == NBT_VALUE_CHANGED
    assert diff.tag_path == "Extension.bias"

    # Two NaNs with identical bits are equal, even though NaN != NaN.
    same_nan = copy.deepcopy(original)
    same_nan["Extension"]["payload"] = Double(_double_from_bits(NAN_BITS))
    payload = float(same_nan["Extension"]["payload"])
    assert payload != payload
    original_payload = float(original["Extension"]["payload"])
    assert struct.pack("<d", payload) == struct.pack("<d", original_payload)
    assert compare_nbt(original, same_nan, ()) == []

    # A different NaN payload is still a bit-level difference.
    other_nan = copy.deepcopy(original)
    other_nan["Extension"]["payload"] = Double(_double_from_bits(NAN_BITS_OTHER_PAYLOAD))
    diff = _single_diff(compare_nbt(original, other_nan, ()))
    assert diff.code == NBT_VALUE_CHANGED
    assert diff.tag_path == "Extension.payload"
    assert "0x7FF8000000000000" in diff.original_value or "0x7FF8000000000000" in diff.detail


def test_float_bits_are_compared_for_float_tags():
    original = Compound({"temperature": Float(_double_from_bits(NAN_BITS))})
    same = Compound({"temperature": Float(_double_from_bits(NAN_BITS))})
    other = Compound({"temperature": Float(0.0)})

    assert compare_nbt(original, same, ()) == []
    diff = _single_diff(compare_nbt(original, other, ()))
    assert diff.code == NBT_VALUE_CHANGED
    assert diff.tag_path == "temperature"
    assert diff.original_type == "Float"
    assert diff.current_type == "Float"


# --------------------------------------------------------------------------- #
# Values, paths and bounds
# --------------------------------------------------------------------------- #
def test_value_representations_are_bounded():
    original = _tree()
    original["Extension"]["note"] = String("y" * 200)
    original["Regions"][MAIN]["BlockStates"] = LongArray(list(range(1000)))
    current = copy.deepcopy(original)
    current["Extension"]["note"] = String("z" * 200)
    current["Regions"][MAIN]["BlockStates"] = LongArray(list(range(999)) + [7])

    differences = compare_nbt(original, current, ())

    assert [d.tag_path for d in differences] == ["Extension.note", "Regions.main.BlockStates"]
    for diff in differences:
        assert len(diff.original_value) <= 64
        assert diff.current_value is not None
        assert len(diff.current_value) <= 64
    assert differences[0].original_value.endswith("...")
    assert differences[1].original_value.startswith("LongArray(len=1000")


def test_differences_carry_tag_path_and_are_sorted():
    original = _tree()
    current = copy.deepcopy(original)
    current["Metadata"]["TotalBlocks"] = Int(99)
    _extension(current)["note"] = String("changed")
    del _extension(current)["seed"]
    _extension(current)["extra"] = Byte(7)

    differences = compare_nbt(original, current, ())

    paths = [d.tag_path for d in differences]
    assert paths == [
        "Extension.extra",
        "Extension.note",
        "Extension.seed",
        "Metadata.TotalBlocks",
    ]
    assert paths == sorted(paths)
    assert [d.code for d in differences] == [
        NBT_UNAUTHORIZED_FIELD_CHANGE,
        NBT_VALUE_CHANGED,
        NBT_FIELD_MISSING,
        NBT_VALUE_CHANGED,
    ]
    assert all(d.code in DIFFERENCE_CODES for d in differences)
    assert all(d.tag_path for d in differences)


def test_max_diffs_bounds_output():
    original = Compound({"Values": Compound({f"v{i:03d}": Int(i) for i in range(50)})})
    current = Compound({"Values": Compound({f"v{i:03d}": Int(i + 1) for i in range(50)})})

    assert len(compare_nbt(original, current, ())) == 50
    for bound in (1, 7, 10, 49):
        differences = compare_nbt(original, current, (), max_diffs=bound)
        assert len(differences) == bound
        paths = [d.tag_path for d in differences]
        assert paths == sorted(paths)
    assert compare_nbt(original, current, (), max_diffs=0) == []


# --------------------------------------------------------------------------- #
# Whitelist semantics
# --------------------------------------------------------------------------- #
def test_whitelisted_palette_change_allowed():
    original = _tree()
    current = copy.deepcopy(original)
    current["Regions"][MAIN]["BlockStatePalette"] = List(
        [Compound({"Name": String("minecraft:dirt")})]
    )
    current["Regions"][MAIN]["BlockStates"] = LongArray([2, 2, 2, 2])

    allowed = allowed_save_paths(MAIN)

    assert compare_nbt(original, current, allowed) == []
    ensure_nbt_preserved(original, current, allowed)  # must not raise


def test_position_size_dataversion_never_allowed():
    allowed = allowed_save_paths(MAIN)

    assert allowed == frozenset(
        {
            "Regions.main.BlockStatePalette",
            "Regions.main.BlockStates",
            "Metadata.Description",
            "Metadata.Author",
            "Metadata.Name",
            "Description",
            "Author",
            "Name",
        }
    )
    forbidden_paths = (
        "Regions.main.Position",
        "Regions.main.Size",
        "Regions.main.Position[1]",
        "Regions.main",
        "MinecraftDataVersion",
        "Regions",
        "Metadata",
        "Metadata.TotalBlocks",
        "Regions.main.Position.**",
    )
    for path in forbidden_paths:
        assert not is_path_allowed(path, allowed), path
    assert not any("**" in pattern for pattern in allowed)

    original = _tree()
    for forbidden in ("Position", "Size"):
        current = copy.deepcopy(original)
        region = _region(current)
        region[forbidden] = IntArray([9, 9, 9])
        with pytest.raises(NbtPreservationError) as excinfo:
            ensure_nbt_preserved(original, current, allowed)
        assert [d.tag_path for d in excinfo.value.differences] == [
            f"Regions.main.{forbidden}"
        ]
        assert excinfo.value.differences[0].code == NBT_VALUE_CHANGED
        assert isinstance(excinfo.value, AssertionError)

    changed_version = copy.deepcopy(original)
    changed_version["MinecraftDataVersion"] = Int(3953)
    with pytest.raises(NbtPreservationError) as excinfo:
        ensure_nbt_preserved(original, changed_version, allowed)
    assert [d.tag_path for d in excinfo.value.differences] == ["MinecraftDataVersion"]
    assert excinfo.value.differences[0].code == NBT_VALUE_CHANGED


def test_metadata_whitelist_field_allowed():
    original = _tree()
    current = copy.deepcopy(original)
    current["Metadata"]["Description"] = String("refreshed by litegarden")
    current["Metadata"]["Name"] = String("renamed")

    allowed = allowed_save_paths(MAIN)
    ensure_nbt_preserved(original, current, allowed)  # must not raise

    current["Metadata"]["TotalBlocks"] = Int(13)
    with pytest.raises(NbtPreservationError) as excinfo:
        ensure_nbt_preserved(original, current, allowed)
    assert [d.tag_path for d in excinfo.value.differences] == ["Metadata.TotalBlocks"]
    assert [d.code for d in excinfo.value.differences] == [NBT_VALUE_CHANGED]

    # A removed whitelisted metadata field is a refresh of that field, and the
    # whole ``Metadata`` subtree is still not exempt.
    without_description = copy.deepcopy(original)
    del without_description["Metadata"]["Description"]
    ensure_nbt_preserved(original, without_description, allowed)  # must not raise
    assert not is_path_allowed("Metadata.Description.anything", allowed)


def test_root_level_whitelisted_field_allowed():
    original = _tree()
    current = copy.deepcopy(original)
    current["Description"] = String("root level description")

    allowed = allowed_save_paths(MAIN)
    ensure_nbt_preserved(original, current, allowed)  # must not raise


def test_other_region_id_is_not_exempted():
    original = _tree()
    current = copy.deepcopy(original)
    current["Regions"][MAIN]["BlockStates"] = LongArray([5, 5, 5, 5])

    with pytest.raises(NbtPreservationError):
        ensure_nbt_preserved(original, current, allowed_save_paths("other"))


def test_allowed_subtree_exempts_all_differences():
    original = _tree()
    current = copy.deepcopy(original)
    current["Regions"][MAIN]["Entities"] = List(
        [
            Compound(
                {
                    "id": String("minecraft:cow"),
                    "Health": Double(1.0),
                    "added": Int(1),
                }
            ),
            Compound({"id": String("minecraft:sheep")}),
        ]
    )

    assert compare_nbt(original, current, ()) != []
    assert compare_nbt(original, current, ("Regions.main.Entities.**",)) == []
    assert compare_nbt(original, current, ("Regions.*.Entities.**",)) == []
    # ``**`` is required for a subtree exemption; a sibling pattern is not enough.
    assert compare_nbt(original, current, ("Regions.other.**",)) != []
    assert compare_nbt(original, current, ("Regions.main.Entities[0].id",)) != []


def test_index_segments_and_star_patterns():
    original = _tree()
    current = copy.deepcopy(original)
    _item(current, 0)["count"] = Int(5)

    diff_path = "Regions.main.TileEntities[0].Items[0].count"
    assert [d.tag_path for d in compare_nbt(original, current, ())] == [diff_path]

    assert compare_nbt(original, current, (diff_path,)) == []
    assert compare_nbt(original, current, ("Regions.main.TileEntities.*.Items.*.count",)) == []
    assert compare_nbt(original, current, ("Regions.main.TileEntities[0].Items[0].**",)) == []
    assert compare_nbt(original, current, ("Regions.main.TileEntities[1].**",)) != []
    assert compare_nbt(original, current, ("Regions.main.Items[0].count",)) != []

    assert is_path_allowed(diff_path, (diff_path,))
    assert is_path_allowed(
        "Regions.main.TileEntities[1].Items[3].count",
        ("Regions.main.TileEntities.*.Items.*.count",),
    )
    assert not is_path_allowed(diff_path, allowed_save_paths(MAIN))
    assert not is_path_allowed("Regions.main.Position[1]", allowed_save_paths(MAIN))


def test_identity_tree_has_no_differences():
    original = _tree()
    assert compare_nbt(original, copy.deepcopy(original), ()) == []
    assert compare_nbt(original, copy.deepcopy(original), allowed_save_paths(MAIN)) == []
