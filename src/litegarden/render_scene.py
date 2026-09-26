"""Read-only RenderScene delivery for the Viewer (spec 13.1 / 13.2).

This module turns a scene into the sparse display array the frontend consumes.
Three rules drive the implementation:

- **Built from a re-read file, not from compiler memory.**  A ``RenderScene`` is
  built from a :class:`~litegarden.io.LoadedScene` that came from
  :func:`~litegarden.io.load_scene` on the *serialized* candidate file.
  Serialization, coordinate and state-delivery faults are only visible on that
  re-read result, so the caller must hand in the re-read scene; nothing here
  reads any other file and no file byte hash is invented (``file_sha256`` is
  supplied by whoever wrote the candidate bytes).
- **Air is display index 0; the source palette is never touched.**  The display
  palette is a *view* for the renderer: the air family
  (``litegarden.blocks.AIR_BLOCKS``: ``air`` / ``cave_air`` / ``void_air``, all
  invisible in game) collapses onto index 0 and the sparse ``idx`` / ``state``
  arrays list only the non-air voxels.  The full state (id *and* properties) of
  every other voxel is preserved verbatim.  The NBT ``BlockStatePalette`` of the
  source (``scene.raw_nbt``) and the litemapy region are strictly read-only
  here: this module never assigns to either of them.
- **Semantic hash and file byte hash are different things.**
  ``scene_hash`` is the project's normalised voxel-semantics hash
  (:func:`litegarden.net_patch.scene_semantic_hash`) over the *whole* scene, so
  it does not change when only the crop changes, and it survives a different
  gzip wrapper or timestamp; it is the value a render receipt is bound to.
  ``file_sha256`` identifies the exact serialized bytes that were rendered and
  therefore comes from the caller.  When the caller has no file bytes the field
  is omitted (and flagged in :func:`summarize_for_client` diagnostics) rather
  than faked.  Neither hash licenses "ignore NBT differences": only the
  whitelisted save fields may differ, and that check lives in
  :mod:`litegarden.nbt_compare`.

Sparse layout (spec 13.1, matching the upstream viewer payload): for a crop of
size ``(size_x, size_y, size_z)`` a voxel at crop-local ``(x, y, z)`` has the
linear index ``i = x * size_y * size_z + y * size_z + z``.  ``idx`` lists the
non-air indices in ascending order, uniquely, and ``state[k]`` is the display
palette index of ``idx[k]``.  The index is relative to the crop, while
``crop_origin_local`` and ``full_scene_bounds`` are ``project_local``
coordinates.
"""
from __future__ import annotations

from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Sequence,
    Tuple,
)

from .blocks import is_air
from .net_patch import scene_semantic_hash
from .scene import AIR, Vec3

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .io import LoadedScene

RENDER_SCENE_SCHEMA_VERSION = "0.2"

#: Coordinate space of every coordinate in a RenderScene payload.
COORDINATE_SPACE = "project_local"

#: Display palette index that means "air" (spec 13.1).
DISPLAY_AIR_INDEX = 0

# ---- error codes (exact strings, spec 13.1) -------------------------------

INVALID_RENDER_PALETTE_INDEX = "INVALID_RENDER_PALETTE_INDEX"
ARRAY_LENGTH_MISMATCH = "ARRAY_LENGTH_MISMATCH"
DUPLICATE_VOXEL_INDEX = "DUPLICATE_VOXEL_INDEX"
INDEX_OUT_OF_RANGE = "INDEX_OUT_OF_RANGE"
UNKNOWN_RENDER_FIELD = "UNKNOWN_RENDER_FIELD"
RENDER_PAYLOAD_MISMATCH = "RENDER_PAYLOAD_MISMATCH"

#: Reserved.  An empty scene is a *legal* payload, not an error: it carries
#: ``empty: True``, empty ``idx`` / ``state`` and ``counts.non_air_voxels == 0``.
#: Nothing in this module raises this code; it exists so callers can name the
#: case explicitly instead of inventing a string.
RENDER_SCENE_EMPTY = "RENDER_SCENE_EMPTY"

#: The complete protocol field set.  Anything else is UNKNOWN_RENDER_FIELD.
RENDER_SCENE_FIELDS = frozenset({
    "schema_version",
    "scene_id",
    "scene_hash",
    "file_sha256",
    "coordinate_space",
    "crop_origin_local",
    "size",
    "full_scene_bounds",
    "minecraft_data_version",
    "palette",
    "idx",
    "state",
    "resource_hash",
    "counts",
    "empty",
})

#: ``file_sha256`` is optional (absent when the caller has no candidate bytes)
#: and so is ``empty`` (present only for a legal empty selection).
RENDER_SCENE_REQUIRED_FIELDS = RENDER_SCENE_FIELDS - {"file_sha256", "empty"}

_COUNTS_FIELDS = frozenset({"non_air_voxels", "voxels"})
_PALETTE_ENTRY_FIELDS = frozenset({"name", "props"})
_BOUNDS_FIELDS = frozenset({"min", "max_exclusive"})


class RenderSceneError(ValueError):
    """A RenderScene payload is malformed, unknown or inconsistent.

    ``code`` is one of the module-level error codes, ``detail`` describes the
    first violation found and ``field`` names the offending payload field when
    it is known.  Unknown fields and out-of-range data are never ignored.
    """

    def __init__(self, code: str, detail: str, *, field: Optional[str] = None) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.field = field

    def to_dict(self) -> dict:
        return {"code": self.code, "detail": self.detail, "field": self.field}


# --------------------------------------------------------------------------
# small type helpers
# --------------------------------------------------------------------------


def _is_int(value: Any) -> bool:
    """Strict integer test: bools and floats are not integers here."""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


def _int_triple(
    value: Any, field: str, code: str = RENDER_PAYLOAD_MISMATCH
) -> Tuple[int, int, int]:
    if not _is_sequence(value) or len(value) != 3 or not all(_is_int(v) for v in value):
        raise RenderSceneError(
            code, f"{field} must be three integers, got {value!r}", field=field
        )
    return (int(value[0]), int(value[1]), int(value[2]))


def _normalise_state(name: str, props: Optional[Mapping[str, str]]) -> str:
    """Normalise a block state to ``id`` or ``id[key=value,key2=value2]``.

    Property keys are sorted ascending so the same state always yields the same
    string (and two states that differ only in a property value never collide).
    """
    if not props:
        return name
    body = ",".join(f"{key}={props[key]}" for key in sorted(props))
    return f"{name}[{body}]"


class _State(NamedTuple):
    """One distinct block state: id, normalised string, sorted properties."""

    name: str
    state: str
    props: Dict[str, str]


class _SceneVoxelReader:
    """Read-only state sampler over a re-read scene.

    Block *properties* are not available through
    :meth:`~litegarden.scene.SceneSnapshot.block_at_local` (it returns the id
    only), so the full states are read from the litemapy region of the re-read
    file (``LoadedScene.region``, the same object the snapshot wraps and the
    same states the file's ``BlockStatePalette`` holds).  A scene that cannot
    report properties is refused instead of silently merging two states that
    differ only in a property value.

    Normalised strings are memoised per distinct ``BlockState``, so a crop of
    several hundred thousand voxels does not rebuild the same string per voxel.
    """

    def __init__(self, scene: Any) -> None:
        snapshot = getattr(scene, "snapshot", None)
        if snapshot is None:
            raise RenderSceneError(
                RENDER_PAYLOAD_MISMATCH,
                "scene has no 'snapshot'; pass a LoadedScene from load_scene()",
            )
        region = getattr(scene, "region", None)
        if region is None:
            raise RenderSceneError(
                RENDER_PAYLOAD_MISMATCH,
                "scene cannot report block properties (no 'region'); pass a "
                "LoadedScene from load_scene() so id and properties are both known",
            )
        self.snapshot = snapshot
        self.transform = snapshot.transform
        self._region = region
        self._cache: Dict[Any, _State] = {}

    def state_at_local(self, p_local: Vec3) -> _State:
        """Full state at a project-local coordinate; never writes anything."""
        block = self._region[self.transform.local_to_region(p_local)]
        cached = self._cache.get(block)
        if cached is None:
            name = block.id
            props = {k: v for k, v in sorted(block.properties())}
            cached = _State(name, _normalise_state(name, props), props)
            self._cache[block] = cached
        return cached


def _scene_data_version(scene: Any, snapshot: Any) -> Any:
    for source in (scene, snapshot):
        value = getattr(source, "data_version", None)
        if value is not None:
            return value
    return None


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------


def build_render_scene(
    scene: "LoadedScene",
    *,
    scene_id: str,
    file_sha256: Optional[str] = None,
    crop_origin_local: Vec3 = (0, 0, 0),
    crop_size: Optional[Vec3] = None,
    resource_hash: Optional[str] = None,
    minecraft_data_version: Optional[int] = None,
) -> dict:
    """Build the sparse RenderScene payload for a re-read scene (spec 13.1).

    :param scene: a :class:`~litegarden.io.LoadedScene` loaded from the
        *serialized* candidate file (``load_scene(candidate_path)``).  Read-only.
    :param scene_id: immutable id of the rendered revision (e.g. the candidate
        id); required and non-empty.
    :param file_sha256: sha256 of the serialized candidate bytes, computed by
        the caller.  ``None`` omits the field (the summariser flags it) instead
        of writing a fake value.
    :param crop_origin_local: ``project_local`` origin of the selection.
    :param crop_size: selection size; ``None`` means the whole scene.  The crop
        must lie inside the scene bounds.
    :param resource_hash: hash of the pinned viewer assets, or ``None``.
    :param minecraft_data_version: defaults to the scene's data version.

    Returns the payload described in spec 13.1 with the keys in that order.
    ``palette[0]`` is always air with empty properties; the states actually seen
    inside the crop follow, sorted by their normalised string, so an id with two
    different property sets yields two palette entries.  Air-like ids
    (:data:`litegarden.blocks.AIR_BLOCKS`) are not listed in ``idx``: they are
    display index 0.  An empty crop is legal: ``idx`` / ``state`` are empty,
    ``counts.non_air_voxels`` is 0 and ``empty: True`` is added so the frontend
    can tell "empty selection" from "load failed".

    ``scene_hash`` is the semantic hash of the *whole* scene (unaffected by the
    crop); ``file_sha256`` is the byte hash of the serialized file.  Raises
    :class:`RenderSceneError` (``INDEX_OUT_OF_RANGE``) for a crop that is not a
    positive-volume box inside the scene.
    """
    reader = _SceneVoxelReader(scene)
    transform = reader.transform
    snapshot = reader.snapshot

    if not isinstance(scene_id, str) or not scene_id:
        raise RenderSceneError(
            RENDER_PAYLOAD_MISMATCH, "scene_id must be a non-empty string", field="scene_id"
        )
    if file_sha256 is not None and (not isinstance(file_sha256, str) or not file_sha256):
        raise RenderSceneError(
            RENDER_PAYLOAD_MISMATCH,
            "file_sha256 must be a non-empty string or None",
            field="file_sha256",
        )

    data_version = (
        minecraft_data_version
        if minecraft_data_version is not None
        else _scene_data_version(scene, snapshot)
    )
    if not _is_int(data_version):
        raise RenderSceneError(
            RENDER_PAYLOAD_MISMATCH,
            f"minecraft_data_version must be an int, got {data_version!r}",
            field="minecraft_data_version",
        )

    full_size = _int_triple(transform.local_size, "scene size")
    origin = _int_triple(crop_origin_local, "crop_origin_local", code=INDEX_OUT_OF_RANGE)
    size = full_size if crop_size is None else _int_triple(
        crop_size, "crop_size", code=INDEX_OUT_OF_RANGE
    )
    if min(size) <= 0:
        raise RenderSceneError(
            INDEX_OUT_OF_RANGE,
            f"crop_size must be positive on every axis, got {list(size)}",
            field="crop_size",
        )
    for axis in range(3):
        if origin[axis] < 0 or origin[axis] + size[axis] > full_size[axis]:
            raise RenderSceneError(
                INDEX_OUT_OF_RANGE,
                f"crop {list(origin)} + {list(size)} leaves the scene bounds "
                f"{list(full_size)} on axis {axis}",
                field="crop_origin_local",
            )

    origin_x, origin_y, origin_z = origin
    size_x, size_y, size_z = size
    volume = size_x * size_y * size_z

    idx: List[int] = []
    state_keys: List[str] = []
    distinct: Dict[str, _State] = {}

    # Iterating x, then y, then z emits the linear index in ascending order,
    # so no post-sort is needed and the layout is exactly i = x*sy*sz + y*sz + z.
    for x in range(origin_x, origin_x + size_x):
        for y in range(origin_y, origin_y + size_y):
            row = ((x - origin_x) * size_y + (y - origin_y)) * size_z
            for z in range(origin_z, origin_z + size_z):
                found = reader.state_at_local((x, y, z))
                if is_air(found.name):
                    continue  # display index 0, never listed in a sparse payload
                key = found.state
                if key not in distinct:
                    distinct[key] = found
                idx.append(row + (z - origin_z))
                state_keys.append(key)

    palette: List[dict] = [{"name": AIR, "props": {}}]
    palette_index: Dict[str, int] = {AIR: DISPLAY_AIR_INDEX}
    for key in sorted(distinct):
        palette_index[key] = len(palette)
        entry = distinct[key]
        palette.append({"name": entry.name, "props": dict(entry.props)})
    state: List[int] = [palette_index[key] for key in state_keys]
    del state_keys

    payload: dict = {
        "schema_version": RENDER_SCENE_SCHEMA_VERSION,
        "scene_id": scene_id,
        "scene_hash": scene_semantic_hash(
            snapshot, region_id=snapshot.region_id, data_version=int(data_version)
        ),
    }
    if file_sha256 is not None:
        payload["file_sha256"] = file_sha256
    payload.update({
        "coordinate_space": COORDINATE_SPACE,
        "crop_origin_local": list(origin),
        "size": list(size),
        "full_scene_bounds": {"min": [0, 0, 0], "max_exclusive": list(full_size)},
        "minecraft_data_version": int(data_version),
        "palette": palette,
        "idx": idx,
        "state": state,
        "resource_hash": resource_hash,
        "counts": {"non_air_voxels": len(idx), "voxels": volume},
    })
    if not idx:
        payload["empty"] = True
    return payload


# --------------------------------------------------------------------------
# validate
# --------------------------------------------------------------------------


def validate_render_scene(payload: dict, *, expected_scene_hash: Optional[str] = None) -> None:
    """Strictly validate an already serialized RenderScene payload.

    Raises :class:`RenderSceneError` with the exact code for the first problem:

    - ``UNKNOWN_RENDER_FIELD``: a field outside the protocol set (also inside
      ``full_scene_bounds``, ``counts`` and each palette entry),
    - ``RENDER_PAYLOAD_MISMATCH``: missing field, wrong type/shape, wrong
      ``schema_version`` / ``coordinate_space``, a self-inconsistent ``counts``
      or ``empty`` flag, a crop outside ``full_scene_bounds``, or a
      ``scene_hash`` that does not match ``expected_scene_hash``,
    - ``ARRAY_LENGTH_MISMATCH``: ``idx`` and ``state`` differ in length,
    - ``INVALID_RENDER_PALETTE_INDEX``: missing/empty palette, a palette entry
      that is not ``{name, props}``, a ``state`` value outside the palette, or a
      ``palette[0]`` that is not air,
    - ``INDEX_OUT_OF_RANGE``: an ``idx`` outside ``[0, size_x*size_y*size_z)``,
    - ``DUPLICATE_VOXEL_INDEX``: an ``idx`` listed twice.

    An empty scene is valid and must pass.  Uniqueness and range are enforced;
    ascending order is what the builder emits but is not required back.  A
    ``state`` value may legally point at ``palette[0]`` (the builder only emits
    non-air states, but a listed voxel naming air is not an inconsistency).
    """
    if not isinstance(payload, dict):
        raise RenderSceneError(
            RENDER_PAYLOAD_MISMATCH, f"payload must be a dict, got {type(payload).__name__}"
        )

    unknown = sorted(set(payload) - RENDER_SCENE_FIELDS)
    if unknown:
        raise RenderSceneError(
            UNKNOWN_RENDER_FIELD, f"unknown field(s): {unknown}", field=unknown[0]
        )
    missing = sorted(RENDER_SCENE_REQUIRED_FIELDS - set(payload))
    if missing:
        raise RenderSceneError(
            RENDER_PAYLOAD_MISMATCH, f"missing required field(s): {missing}", field=missing[0]
        )

    if payload["schema_version"] != RENDER_SCENE_SCHEMA_VERSION:
        raise RenderSceneError(
            RENDER_PAYLOAD_MISMATCH,
            f"schema_version {payload['schema_version']!r} != "
            f"{RENDER_SCENE_SCHEMA_VERSION!r}",
            field="schema_version",
        )
    if not isinstance(payload["scene_id"], str) or not payload["scene_id"]:
        raise RenderSceneError(
            RENDER_PAYLOAD_MISMATCH, "scene_id must be a non-empty string", field="scene_id"
        )
    scene_hash = payload["scene_hash"]
    if not isinstance(scene_hash, str) or not scene_hash:
        raise RenderSceneError(
            RENDER_PAYLOAD_MISMATCH, "scene_hash must be a non-empty string", field="scene_hash"
        )
    if payload["coordinate_space"] != COORDINATE_SPACE:
        raise RenderSceneError(
            RENDER_PAYLOAD_MISMATCH,
            f"coordinate_space {payload['coordinate_space']!r} != {COORDINATE_SPACE!r}",
            field="coordinate_space",
        )
    if "file_sha256" in payload and payload["file_sha256"] is not None:
        if not isinstance(payload["file_sha256"], str) or not payload["file_sha256"]:
            raise RenderSceneError(
                RENDER_PAYLOAD_MISMATCH,
                "file_sha256 must be a non-empty string or None",
                field="file_sha256",
            )
    if payload["resource_hash"] is not None and not isinstance(payload["resource_hash"], str):
        raise RenderSceneError(
            RENDER_PAYLOAD_MISMATCH,
            f"resource_hash must be a string or None, got {payload['resource_hash']!r}",
            field="resource_hash",
        )
    if not _is_int(payload["minecraft_data_version"]):
        raise RenderSceneError(
            RENDER_PAYLOAD_MISMATCH,
            f"minecraft_data_version must be an int, got "
            f"{payload['minecraft_data_version']!r}",
            field="minecraft_data_version",
        )

    size = _int_triple(payload["size"], "size")
    if min(size) <= 0:
        raise RenderSceneError(
            RENDER_PAYLOAD_MISMATCH, f"size must be positive, got {list(size)}", field="size"
        )
    origin = _int_triple(payload["crop_origin_local"], "crop_origin_local")

    bounds = payload["full_scene_bounds"]
    if not isinstance(bounds, Mapping):
        raise RenderSceneError(
            RENDER_PAYLOAD_MISMATCH,
            f"full_scene_bounds must be an object, got {bounds!r}",
            field="full_scene_bounds",
        )
    bounds_unknown = sorted(set(bounds) - _BOUNDS_FIELDS)
    if bounds_unknown:
        raise RenderSceneError(
            UNKNOWN_RENDER_FIELD,
            f"unknown full_scene_bounds field(s): {bounds_unknown}",
            field="full_scene_bounds",
        )
    bounds_missing = sorted(_BOUNDS_FIELDS - set(bounds))
    if bounds_missing:
        raise RenderSceneError(
            RENDER_PAYLOAD_MISMATCH,
            f"full_scene_bounds is missing {bounds_missing}",
            field="full_scene_bounds",
        )
    bounds_min = _int_triple(bounds["min"], "full_scene_bounds.min")
    bounds_max = _int_triple(bounds["max_exclusive"], "full_scene_bounds.max_exclusive")
    if any(bounds_max[i] < bounds_min[i] for i in range(3)):
        raise RenderSceneError(
            RENDER_PAYLOAD_MISMATCH,
            f"full_scene_bounds max_exclusive {list(bounds_max)} is below min "
            f"{list(bounds_min)}",
            field="full_scene_bounds",
        )
    for axis in range(3):
        if origin[axis] < bounds_min[axis] or origin[axis] + size[axis] > bounds_max[axis]:
            raise RenderSceneError(
                RENDER_PAYLOAD_MISMATCH,
                f"crop {list(origin)} + {list(size)} is outside full_scene_bounds "
                f"{{'min': {list(bounds_min)}, 'max_exclusive': {list(bounds_max)}}} "
                f"on axis {axis}",
                field="crop_origin_local",
            )

    palette = payload["palette"]
    if not _is_sequence(palette):
        raise RenderSceneError(
            RENDER_PAYLOAD_MISMATCH,
            f"palette must be an array, got {type(palette).__name__}",
            field="palette",
        )
    for i, entry in enumerate(palette):
        field = f"palette[{i}]"
        if not isinstance(entry, Mapping):
            raise RenderSceneError(
                RENDER_PAYLOAD_MISMATCH, f"{field} must be an object", field=field
            )
        entry_unknown = sorted(set(entry) - _PALETTE_ENTRY_FIELDS)
        if entry_unknown:
            raise RenderSceneError(
                UNKNOWN_RENDER_FIELD,
                f"{field} has unknown field(s): {entry_unknown}",
                field=field,
            )
        if sorted(entry) != sorted(_PALETTE_ENTRY_FIELDS):
            raise RenderSceneError(
                RENDER_PAYLOAD_MISMATCH,
                f"{field} must have exactly {'name', 'props'}",
                field=field,
            )
        if not isinstance(entry["name"], str) or not entry["name"]:
            raise RenderSceneError(
                RENDER_PAYLOAD_MISMATCH, f"{field}.name must be a non-empty string", field=field
            )
        props = entry["props"]
        if not isinstance(props, Mapping):
            raise RenderSceneError(
                RENDER_PAYLOAD_MISMATCH, f"{field}.props must be an object", field=field
            )
        for key, value in props.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise RenderSceneError(
                    RENDER_PAYLOAD_MISMATCH,
                    f"{field}.props must map strings to strings, got {key!r}: {value!r}",
                    field=field,
                )
    if not palette:
        raise RenderSceneError(
            INVALID_RENDER_PALETTE_INDEX,
            "palette is empty; palette[0] must be air",
            field="palette",
        )
    first = palette[0]
    if first["name"] != AIR or dict(first["props"]):
        raise RenderSceneError(
            INVALID_RENDER_PALETTE_INDEX,
            f"palette[0] must be {AIR!r} with empty props, got {dict(first)!r}",
            field="palette",
        )

    idx = payload["idx"]
    state = payload["state"]
    if not _is_sequence(idx):
        raise RenderSceneError(
            RENDER_PAYLOAD_MISMATCH, f"idx must be an array, got {type(idx).__name__}", field="idx"
        )
    if not _is_sequence(state):
        raise RenderSceneError(
            RENDER_PAYLOAD_MISMATCH,
            f"state must be an array, got {type(state).__name__}",
            field="state",
        )
    if len(idx) != len(state):
        raise RenderSceneError(
            ARRAY_LENGTH_MISMATCH,
            f"idx has {len(idx)} entries but state has {len(state)}",
            field="state",
        )

    volume = size[0] * size[1] * size[2]
    seen = set()
    for position, value in enumerate(idx):
        field = f"idx[{position}]"
        if not _is_int(value):
            raise RenderSceneError(
                RENDER_PAYLOAD_MISMATCH, f"{field} must be an integer, got {value!r}", field=field
            )
        if value < 0 or value >= volume:
            raise RenderSceneError(
                INDEX_OUT_OF_RANGE,
                f"{field} = {value} is outside [0, {volume}) for size {list(size)}",
                field=field,
            )
        if value in seen:
            raise RenderSceneError(
                DUPLICATE_VOXEL_INDEX, f"{field} = {value} is listed more than once", field=field
            )
        seen.add(value)

    palette_size = len(palette)
    for position, value in enumerate(state):
        field = f"state[{position}]"
        if not _is_int(value):
            raise RenderSceneError(
                RENDER_PAYLOAD_MISMATCH, f"{field} must be an integer, got {value!r}", field=field
            )
        if value < 0 or value >= palette_size:
            raise RenderSceneError(
                INVALID_RENDER_PALETTE_INDEX,
                f"{field} = {value} is outside the palette of {palette_size} state(s)",
                field=field,
            )

    counts = payload["counts"]
    if not isinstance(counts, Mapping):
        raise RenderSceneError(
            RENDER_PAYLOAD_MISMATCH,
            f"counts must be an object, got {counts!r}",
            field="counts",
        )
    counts_unknown = sorted(set(counts) - _COUNTS_FIELDS)
    if counts_unknown:
        raise RenderSceneError(
            UNKNOWN_RENDER_FIELD,
            f"unknown counts field(s): {counts_unknown}",
            field="counts",
        )
    if "non_air_voxels" in counts:
        if not _is_int(counts["non_air_voxels"]) or counts["non_air_voxels"] != len(idx):
            raise RenderSceneError(
                RENDER_PAYLOAD_MISMATCH,
                f"counts.non_air_voxels = {counts['non_air_voxels']!r} does not match "
                f"the {len(idx)} listed voxel(s)",
                field="counts",
            )
    if "voxels" in counts:
        if not _is_int(counts["voxels"]) or counts["voxels"] != volume:
            raise RenderSceneError(
                RENDER_PAYLOAD_MISMATCH,
                f"counts.voxels = {counts['voxels']!r} does not match the crop volume {volume}",
                field="counts",
            )
    if "empty" in payload:
        if not isinstance(payload["empty"], bool) or payload["empty"] != (len(idx) == 0):
            raise RenderSceneError(
                RENDER_PAYLOAD_MISMATCH,
                f"empty = {payload['empty']!r} does not match {len(idx)} listed voxel(s)",
                field="empty",
            )

    if expected_scene_hash is not None and scene_hash != expected_scene_hash:
        raise RenderSceneError(
            RENDER_PAYLOAD_MISMATCH,
            f"scene_hash {scene_hash!r} does not match the expected "
            f"{expected_scene_hash!r}",
            field="scene_hash",
        )


# --------------------------------------------------------------------------
# client-facing helpers
# --------------------------------------------------------------------------


def _copy_triple(value: Any) -> Optional[List[int]]:
    if not _is_sequence(value):
        return None
    return [int(v) if _is_int(v) else v for v in value]


def summarize_for_client(payload: dict) -> dict:
    """Lightweight summary for the client and diagnostics.

    Deliberately drops ``idx`` / ``state`` (they may hold ~400k entries) and
    keeps the hashes, size, crop, counts, palette length and diagnostics.
    ``file_sha256_missing`` is True when the payload carries no file byte hash
    (the builder omits it when the caller has none); it is reported both at the
    top level and inside ``diagnostics`` so a log line can be abbreviated.

    This is a *view*, not a validator: it does not enforce the protocol.
    """
    if not isinstance(payload, dict):
        raise RenderSceneError(
            RENDER_PAYLOAD_MISMATCH, f"payload must be a dict, got {type(payload).__name__}"
        )

    palette = payload.get("palette")
    palette_size = len(palette) if _is_sequence(palette) else 0
    counts = payload.get("counts")
    counts = dict(counts) if isinstance(counts, Mapping) else {}
    file_sha256 = payload.get("file_sha256")
    file_sha256_missing = not (isinstance(file_sha256, str) and file_sha256)
    empty = bool(payload.get("empty", counts.get("non_air_voxels") == 0))
    bounds = payload.get("full_scene_bounds")
    bounds = bounds if isinstance(bounds, Mapping) else {}
    origin = payload.get("crop_origin_local")
    size = payload.get("size")
    crop_is_full_scene = (
        _is_sequence(origin)
        and _is_sequence(size)
        and all(_is_int(v) and v == 0 for v in origin)
        and _is_sequence(bounds.get("max_exclusive"))
        and list(size) == list(bounds["max_exclusive"])
    )

    summary: dict = {
        "schema_version": payload.get("schema_version"),
        "scene_id": payload.get("scene_id"),
        "scene_hash": payload.get("scene_hash"),
    }
    if not file_sha256_missing:
        summary["file_sha256"] = file_sha256
    summary.update({
        "coordinate_space": payload.get("coordinate_space"),
        "crop_origin_local": _copy_triple(origin),
        "size": _copy_triple(size),
        "full_scene_bounds": (
            {"min": _copy_triple(bounds.get("min")),
             "max_exclusive": _copy_triple(bounds.get("max_exclusive"))}
            if bounds else None
        ),
        "minecraft_data_version": payload.get("minecraft_data_version"),
        "resource_hash": payload.get("resource_hash"),
        "palette_size": palette_size,
        "counts": counts,
        "empty": empty,
        "file_sha256_missing": file_sha256_missing,
        "diagnostics": {
            "file_sha256_missing": file_sha256_missing,
            "empty": empty,
            "crop_is_full_scene": crop_is_full_scene,
            "unknown_fields": sorted(set(payload) - RENDER_SCENE_FIELDS),
        },
    })
    return summary


def air_display_index(payload: dict) -> int:
    """The display palette index that means air (always 0, spec 13.1).

    Raises :class:`RenderSceneError` with ``INVALID_RENDER_PALETTE_INDEX`` when
    the payload has no palette or ``palette[0]`` is not ``minecraft:air`` with
    empty properties - a renderer that silently picked another index would
    display the wrong mask.
    """
    palette = payload.get("palette") if isinstance(payload, Mapping) else None
    if not _is_sequence(palette) or not palette:
        raise RenderSceneError(
            INVALID_RENDER_PALETTE_INDEX,
            "payload has no palette; display index 0 must be air",
            field="palette",
        )
    first = palette[0]
    if (
        not isinstance(first, Mapping)
        or first.get("name") != AIR
        or dict(first.get("props") or {})
    ):
        raise RenderSceneError(
            INVALID_RENDER_PALETTE_INDEX,
            f"palette[0] must be {AIR!r} with empty props, got {first!r}",
            field="palette",
        )
    return DISPLAY_AIR_INDEX
