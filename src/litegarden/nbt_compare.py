"""Type-sensitive NBT comparison against an explicit allowed-path whitelist.

Saving a scene patches a deep copy of the original NBT tree, so an exported
file must re-read to a tree that is *identical* to the original everywhere
except at the paths this module whitelists (spec 7.1, acceptance C01/C02).

A plain ``==`` between two trees is not enough: nbtlib numeric tags compare
equal across types (``Short(5) == Int(5)`` is ``True``), so "the numbers are
the same" would silently accept an entity / tile-entity / extension field whose
NBT type was rewritten by a re-encode.  Therefore:

* tag *types* are compared before values (``type(tag).__name__`` semantics),
  so Byte/Short/Int/Long/Float/Double/String can never substitute for each other;
* Compound key *order* is not a semantic difference and keys are walked in
  sorted order, keeping the reported differences deterministic;
* Lists are compared by element tag type, length, order and value; arrays
  (ByteArray / IntArray / LongArray) by type, length and content;
* Float / Double are compared bitwise through :mod:`struct`, so ``NaN`` payloads
  and ``+0.0`` vs ``-0.0`` cannot be flattened away by an ordinary conversion;
* value renderings are bounded so a multi-megabyte array never lands in a report.

Whitelist semantics (``allowed_paths``):

* patterns are ``.``-separated segments; a list/array index is its own segment
  (``TileEntities[0]`` normalises to ``TileEntities`` + ``0``);
* ``*`` matches exactly one segment, ``**`` matches zero or more segments and is
  meant for subtrees only;
* a pattern that covers a subtree exempts *every* difference inside it, so the
  walker checks the whitelist before descending instead of returning thousands
  of uninteresting differences;
* nothing is exempt unless a pattern says so: a subtree exemption requires an
  explicit ``**`` and can never be inferred from a neighbouring path.
"""
from __future__ import annotations

import functools
import re
import struct
from dataclasses import dataclass
from typing import Any, Iterable, Sequence, Tuple, Union

from nbtlib.tag import Array, Compound, Double, End, Float, List, String

NBT_TYPE_CHANGED = "NBT_TYPE_CHANGED"
NBT_VALUE_CHANGED = "NBT_VALUE_CHANGED"
NBT_FIELD_MISSING = "NBT_FIELD_MISSING"
NBT_UNAUTHORIZED_FIELD_CHANGE = "NBT_UNAUTHORIZED_FIELD_CHANGE"

#: All difference codes this module can emit.
DIFFERENCE_CODES = frozenset(
    {
        NBT_TYPE_CHANGED,
        NBT_VALUE_CHANGED,
        NBT_FIELD_MISSING,
        NBT_UNAUTHORIZED_FIELD_CHANGE,
    }
)

#: Metadata fields a save may refresh (mirrors ``io._METADATA_WHITELIST``).
DEFAULT_METADATA_WHITELIST: Tuple[str, ...] = ("Description", "Author", "Name")

#: Maximum length of a ``NbtDifference`` value rendering.
MAX_VALUE_CHARS = 64

#: Placeholder used when a tag path has no segments (degenerate root diff).
ROOT_PATH = "<root>"

#: Placeholder for a list whose element type is no longer recoverable.
UNKNOWN_TYPE = "Unknown"

#: Syntactic segment used for "the element slot of this list".
_LIST_SLOT = "[]"

_MAX_ITEMS_IN_VALUE = 8

PathSegment = Union[str, int]
TagPath = Tuple[PathSegment, ...]


@dataclass(frozen=True)
class NbtDifference:
    """One type-sensitive difference between two NBT trees.

    ``code`` is one of :data:`DIFFERENCE_CODES`:

    * ``NBT_TYPE_CHANGED`` - the two tags are different NBT types (e.g. Short
      vs Int, Float vs Double) or, for a path ending in ``[]``, the two lists
      hold elements of different tag types;
    * ``NBT_VALUE_CHANGED`` - same type, different value; also used for list
      length/order differences, list element value differences and array
      length/content differences;
    * ``NBT_FIELD_MISSING`` - the field exists in the original tree only;
    * ``NBT_UNAUTHORIZED_FIELD_CHANGE`` - the field exists in the current tree
      only, i.e. something was added that the original never had.

    ``tag_path`` points at the differing tag (``Regions.main.TileEntities[0]
    .Items[2].count``); a path ending in ``[]`` points at the element slot of a
    list.  ``original_type`` / ``current_type`` are nbtlib class names and are
    ``None`` for the side where the tag does not exist at all.
    """

    code: str
    tag_path: str
    original_type: str | None
    current_type: str | None
    original_value: str
    current_value: str | None
    detail: str = ""


class NbtPreservationError(AssertionError):
    """Raised when a non-whitelisted NBT difference is found.

    Carries ``differences`` (``list[NbtDifference]``), already sorted by tag
    path, so callers can list every offending path instead of just the first.
    """

    def __init__(self, message: str, differences: Sequence[NbtDifference]) -> None:
        super().__init__(message)
        self.differences: list[NbtDifference] = list(differences)


def allowed_save_paths(
    region_id: str,
    metadata_whitelist: Iterable[str] = DEFAULT_METADATA_WHITELIST,
) -> frozenset[str]:
    """Return the set of tag-path patterns a save is allowed to change.

    The returned set contains *exactly*:

    * ``Regions.<region_id>.BlockStatePalette``
    * ``Regions.<region_id>.BlockStates``
    * ``Metadata.<field>`` and ``<field>`` for every metadata whitelist field
      (defaults to ``Description``, ``Author``, ``Name``)

    Nothing else.  In particular ``Position``, ``Size`` and
    ``MinecraftDataVersion`` are **never** allowed to change, and no
    ``Regions.**`` / ``Metadata.**`` subtree wildcard is ever produced: a whole
    subtree is only exempt when the caller passes an explicit ``**`` pattern to
    :func:`compare_nbt` / :func:`ensure_nbt_preserved`.

    ``region_id`` and the whitelist fields are used as literal path segments,
    so wildcard characters (``*``, ``[``, ``]``) are rejected with
    ``ValueError`` rather than silently widening the whitelist.
    """
    _require_literal_segment(region_id, "region_id")
    patterns = {
        f"Regions.{region_id}.BlockStatePalette",
        f"Regions.{region_id}.BlockStates",
    }
    for field in metadata_whitelist:
        _require_literal_segment(field, "metadata whitelist field")
        patterns.add(f"Metadata.{field}")
        patterns.add(f"{field}")
    return frozenset(patterns)


def compare_nbt(
    original: Any,
    current: Any,
    allowed_paths: Iterable[str],
    *,
    max_diffs: int = 200,
) -> list[NbtDifference]:
    """Compare two NBT trees type-sensitively and return their differences.

    Arguments:
        original: root tag of the tree that must be preserved (usually the
            deep-copied original NBT).
        current: root tag of the tree to verify (usually re-read from the
            exported file).
        allowed_paths: whitelist patterns (see the module docstring).  Anything
            not covered by a pattern is a difference.
        max_diffs: upper bound on the number of returned differences; the walk
            stops as soon as the bound is reached, so the traversal never
            materialises a huge difference list.

    Returns:
        The differences, sorted by ``tag_path`` (code as a tie-breaker), each
        carrying the tag path, both type names and bounded value renderings.
        An empty list means the trees are equivalent under the whitelist.
    """
    return _Walker(allowed_paths, max_diffs).walk(original, current)


def ensure_nbt_preserved(
    original: Any,
    current: Any,
    allowed_paths: Iterable[str],
    *,
    max_diffs: int = 200,
) -> None:
    """Raise :class:`NbtPreservationError` on any non-whitelisted difference.

    Implemented on top of :func:`compare_nbt`, so "same numbers, different tag
    type" fails here exactly like a changed value does.
    """
    differences = compare_nbt(original, current, allowed_paths, max_diffs=max_diffs)
    if not differences:
        return

    listed = differences[:10]
    lines = [
        f"  {difference.code} at {difference.tag_path}: "
        f"{difference.original_value} -> {difference.current_value}"
        for difference in listed
    ]
    if len(differences) > len(listed):
        lines.append(f"  ... and {len(differences) - len(listed)} more")
    truncated = (
        " (list truncated at max_diffs)" if len(differences) >= max_diffs else ""
    )
    raise NbtPreservationError(
        f"{len(differences)} non-whitelisted NBT difference(s){truncated}:\n"
        + "\n".join(lines),
        differences,
    )


def is_path_allowed(tag_path: str, allowed_paths: Iterable[str]) -> bool:
    """Return ``True`` when ``tag_path`` is covered by an allowed-path pattern.

    ``tag_path`` uses the same spelling as :attr:`NbtDifference.tag_path`
    (``Regions.main.TileEntities[0].count``); indices become their own segment
    and the synthetic ``[]`` element slot of a list is transparent.
    """
    return _matches_patterns(_split_path(tag_path), _normalise_patterns(allowed_paths))


# --------------------------------------------------------------------------- #
# Tag helpers
# --------------------------------------------------------------------------- #
def _type_name(tag: Any) -> str:
    """Return the nbtlib class name of ``tag``, e.g. ``"Short"`` / ``"List[Int]"``."""
    if isinstance(tag, List):
        return f"List[{_list_element_type_name(tag)}]"
    return type(tag).__name__


def _kind_name(tag: Any) -> str:
    """Return the coarse NBT kind used for the type-first comparison.

    ``nbtlib.File`` (what ``nbtlib.load`` returns) is a ``Compound`` subclass
    and carries NBT tag id 10, so it is treated as a Compound here while
    :func:`_type_name` still reports the concrete class name.
    """
    if isinstance(tag, Compound):
        return "Compound"
    if isinstance(tag, List):
        return "List"
    return type(tag).__name__


def _list_element_type_name(tag: List) -> str:
    """Return the element type name of a List, or ``"Unknown"`` if unavailable.

    NBT lists carry their element type even when empty and nbtlib keeps it in
    ``subtype``, so an empty ``List[Int]()`` still reports ``"Int"``.  A bare
    ``List()`` / ``List([])`` degrades to ``End`` and the information is truly
    gone; that is reported as ``"Unknown"`` and treated as compatible during
    comparison, so a degenerate empty list never produces a false type change.
    """
    subtype = getattr(tag, "subtype", None)
    if subtype is None or subtype is End:
        if len(tag) == 0:
            return UNKNOWN_TYPE
        subtype = type(tag[0])
    return subtype.__name__


def _float_bytes(tag: Any) -> bytes:
    """Return the IEEE-754 bits of a Float/Double tag in a fixed little-endian form."""
    fmt = "<f" if isinstance(tag, Float) else "<d"
    return struct.pack(fmt, float(tag))


def _float_bits(tag: Any) -> str:
    """Return the hex bit pattern of a Float/Double, for difference details."""
    width = 4 if isinstance(tag, Float) else 8
    return f"0x{int.from_bytes(_float_bytes(tag), 'little'):0{width * 2}X}"


def _array_equal(original: Array, current: Array) -> bool:
    """Compare two same-class, same-length arrays independently of dtype byte order.

    nbtlib parses arrays into a byte-order specific numpy dtype, while arrays
    built in Python use nbtlib's default dtype, so the raw bytes cannot be
    compared blindly; the value comparison is the authoritative fallback.
    """
    if original.tobytes() == current.tobytes():
        return True
    return original.tolist() == current.tolist()


def _bounded(text: str, limit: int = MAX_VALUE_CHARS) -> str:
    """Clamp a readable rendering to ``limit`` characters."""
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3] + "..."


def _describe_scalar(tag: Any) -> str:
    """Bounded ``repr`` of a scalar tag, without building huge intermediate strings."""
    if isinstance(tag, String):
        text = str(tag)
        if len(text) > MAX_VALUE_CHARS:
            return _bounded(repr(text[: MAX_VALUE_CHARS - 4]) + " ...")
        return _bounded(repr(text))
    return _bounded(repr(tag))


def _shallow_text(tag: Any) -> str:
    """One-line description of a container element (never dumps its content)."""
    if isinstance(tag, Compound):
        return f"Compound(len={len(tag)})"
    if isinstance(tag, List):
        return f"{_type_name(tag)}(len={len(tag)})"
    if isinstance(tag, Array):
        return f"{type(tag).__name__}(len={len(tag)})"
    return _describe_scalar(tag)


def _items_text(items: Sequence[Any]) -> str:
    """Bounded ``a, b, c`` rendering of a tag sequence."""
    shown = ", ".join(_shallow_text(item) for item in items[:_MAX_ITEMS_IN_VALUE])
    if len(items) > _MAX_ITEMS_IN_VALUE:
        return f"{shown}, ..."
    return shown


def _describe(tag: Any) -> str:
    """Bounded, readable rendering of a tag value (never a full array dump)."""
    if tag is None:
        return ""
    if isinstance(tag, Compound):
        keys = sorted(tag.keys(), key=str)
        shown = ", ".join(keys[:_MAX_ITEMS_IN_VALUE])
        if len(keys) > _MAX_ITEMS_IN_VALUE:
            shown += ", ..."
        return _bounded(f"Compound({{{shown}}})")
    if isinstance(tag, List):
        return _bounded(f"{_type_name(tag)}(len={len(tag)}, [{_items_text(tag)}])")
    if isinstance(tag, Array):
        values = ", ".join(str(int(value)) for value in tag[:_MAX_ITEMS_IN_VALUE])
        if len(tag) > _MAX_ITEMS_IN_VALUE:
            values += ", ..."
        return _bounded(f"{type(tag).__name__}(len={len(tag)}, [{values}])")
    return _describe_scalar(tag)


# --------------------------------------------------------------------------- #
# Path handling
# --------------------------------------------------------------------------- #
def _render_path(path: TagPath) -> str:
    """Render a tag path, appending index / element-slot segments to their parent."""
    text = ""
    for segment in path:
        if isinstance(segment, int):
            text = f"{text}[{segment}]"
        elif segment == _LIST_SLOT:
            text = f"{text}[]"
        else:
            text = f"{text}.{segment}" if text else str(segment)
    return text or ROOT_PATH


def _split_path(text: str) -> Tuple[str, ...]:
    """Split a dotted/indexed path or pattern into raw segments.

    ``Regions.main.TileEntities[0].Items`` -> ``('Regions', 'main',
    'TileEntities', '0', 'Items')``; an empty ``[]`` yields no segment.
    """
    segments = []
    for chunk in re.split(r"[.\[\]]+", text):
        if chunk:
            segments.append(chunk)
    return tuple(segments)



def _segments_of(path: TagPath) -> Tuple[str, ...]:
    """Return match segments for a tag path: indices become their own segment."""
    return tuple(str(segment) for segment in path if segment != _LIST_SLOT)


def _normalise_patterns(allowed_paths: Iterable[str]) -> Tuple[Tuple[str, ...], ...]:
    """Parse whitelist patterns once, dropping empty ones."""
    patterns = []
    for pattern in allowed_paths:
        segments = _split_path(pattern)
        if segments:
            patterns.append(segments)
    return tuple(patterns)


@functools.lru_cache(maxsize=4096)
def _match_segments(segments: Tuple[str, ...], pattern: Tuple[str, ...]) -> bool:
    """Match path segments against one pattern (``*`` one segment, ``**`` many)."""
    if not pattern:
        return not segments
    head, rest = pattern[0], pattern[1:]
    if head == "**":
        # Zero or more segments: `**` is only meant to exempt subtrees, so it is
        # checked before descending into them.
        for skipped in range(len(segments) + 1):
            if _match_segments(segments[skipped:], rest):
                return True
        return False
    if not segments:
        return False
    if head == "*" or head == segments[0]:
        return _match_segments(segments[1:], rest)
    return False


def _matches_patterns(
    segments: Tuple[str, ...], patterns: Tuple[Tuple[str, ...], ...]
) -> bool:
    """Return ``True`` if any pattern covers the given match segments."""
    return any(_match_segments(segments, pattern) for pattern in patterns)


def _require_literal_segment(value: str, label: str) -> None:
    """Reject path segments that would silently widen a whitelist pattern."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string, got {value!r}")
    forbidden = sorted(ch for ch in value if ch in "*[]")
    if forbidden:
        raise ValueError(
            f"{label} {value!r} contains wildcard/bracket characters {forbidden}; "
            "paths are literal and whitelist patterns must stay exact"
        )


# --------------------------------------------------------------------------- #
# Walker
# --------------------------------------------------------------------------- #
class _DiffBudgetExhausted(Exception):
    """Internal signal: ``max_diffs`` differences were collected, stop walking."""


class _Walker:
    """Whitelist-aware recursive walker collecting type-sensitive differences."""

    def __init__(self, allowed_paths: Iterable[str], max_diffs: int) -> None:
        self._patterns = _normalise_patterns(allowed_paths)
        self._max_diffs = max(0, int(max_diffs))
        self._differences: list[NbtDifference] = []

    def walk(self, original: Any, current: Any) -> list[NbtDifference]:
        if self._max_diffs == 0:
            return []
        try:
            self._compare(original, current, ())
        except _DiffBudgetExhausted:
            pass
        self._differences.sort(key=lambda difference: (difference.tag_path, difference.code))
        return self._differences

    # -- recording ---------------------------------------------------------
    def _exempt(self, path: TagPath) -> bool:
        """Return ``True`` when the whitelist covers this path (whole subtree)."""
        return _matches_patterns(_segments_of(path), self._patterns)

    def _add(
        self,
        code: str,
        path: TagPath,
        original: Any,
        current: Any,
        original_type: str | None,
        current_type: str | None,
        detail: str = "",
    ) -> None:
        if len(self._differences) >= self._max_diffs:
            raise _DiffBudgetExhausted
        self._differences.append(
            NbtDifference(
                code=code,
                tag_path=_render_path(path),
                original_type=original_type,
                current_type=current_type,
                original_value=_describe(original),
                current_value=None if current is None else _describe(current),
                detail=detail,
            )
        )
        if len(self._differences) >= self._max_diffs:
            raise _DiffBudgetExhausted

    # -- comparison --------------------------------------------------------
    def _compare(self, original: Any, current: Any, path: TagPath) -> None:
        if self._exempt(path):
            # An allowed subtree/list is skipped before descending: every
            # difference inside it is exempt by definition.
            return

        original_kind = _kind_name(original)
        current_kind = _kind_name(current)
        if original_kind != current_kind:
            self._add(
                NBT_TYPE_CHANGED,
                path,
                original,
                current,
                _type_name(original),
                _type_name(current),
                detail=f"tag type changed: {_type_name(original)} -> {_type_name(current)}",
            )
            return

        if original_kind == "Compound":
            self._compare_compound(original, current, path)
        elif original_kind == "List":
            self._compare_list(original, current, path)
        elif isinstance(original, Array):
            self._compare_array(original, current, path)
        elif isinstance(original, (Float, Double)):
            self._compare_float(original, current, path)
        elif original != current:
            # Byte / Short / Int / Long / String: types already match, so a
            # plain value comparison is faithful here.
            self._add(
                NBT_VALUE_CHANGED,
                path,
                original,
                current,
                _type_name(original),
                _type_name(current),
                detail=f"value changed: {_describe(original)} -> {_describe(current)}",
            )

    def _compare_compound(self, original: Compound, current: Compound, path: TagPath) -> None:
        keys = sorted(set(original.keys()) | set(current.keys()), key=str)
        for key in keys:
            child_path = path + (key,)
            if self._exempt(child_path):
                # A whitelisted field may also be refreshed away entirely.
                continue
            if key not in original:
                self._add(
                    NBT_UNAUTHORIZED_FIELD_CHANGE,
                    child_path,
                    None,
                    current[key],
                    None,
                    _type_name(current[key]),
                    detail="field present in the current tree but not in the original",
                )
            elif key not in current:
                self._add(
                    NBT_FIELD_MISSING,
                    child_path,
                    original[key],
                    None,
                    _type_name(original[key]),
                    None,
                    detail="field present in the original tree but not in the current tree",
                )
            else:
                self._compare(original[key], current[key], child_path)

    def _compare_list(self, original: List, current: List, path: TagPath) -> None:
        original_element = _list_element_type_name(original)
        current_element = _list_element_type_name(current)
        if (
            original_element != current_element
            and UNKNOWN_TYPE not in (original_element, current_element)
        ):
            self._add(
                NBT_TYPE_CHANGED,
                path + (_LIST_SLOT,),
                original,
                current,
                original_element,
                current_element,
                detail=(
                    f"list element type changed: {original_element} -> {current_element} "
                    f"(lengths {len(original)} -> {len(current)})"
                ),
            )
            return

        if len(original) != len(current):
            self._add(
                NBT_VALUE_CHANGED,
                path,
                original,
                current,
                _type_name(original),
                _type_name(current),
                detail=f"list length changed: {len(original)} -> {len(current)}",
            )
            return

        for index, (original_item, current_item) in enumerate(zip(original, current)):
            self._compare(original_item, current_item, path + (index,))

    def _compare_array(self, original: Array, current: Array, path: TagPath) -> None:
        name = type(original).__name__
        if len(original) != len(current):
            self._add(
                NBT_VALUE_CHANGED,
                path,
                original,
                current,
                name,
                name,
                detail=f"{name} length changed: {len(original)} -> {len(current)}",
            )
            return
        if not _array_equal(original, current):
            self._add(
                NBT_VALUE_CHANGED,
                path,
                original,
                current,
                name,
                name,
                detail=f"{name} contents differ (length {len(original)} unchanged)",
            )

    def _compare_float(self, original: Any, current: Any, path: TagPath) -> None:
        original_bits = _float_bytes(original)
        current_bits = _float_bytes(current)
        if original_bits == current_bits:
            # Bitwise equality: two NaNs with identical bits are equal, while
            # +0.0 and -0.0 (numerically equal) are not.
            return
        self._add(
            NBT_VALUE_CHANGED,
            path,
            original,
            current,
            _type_name(original),
            _type_name(current),
            detail=f"float bits differ: {_float_bits(original)} -> {_float_bits(current)}",
        )
