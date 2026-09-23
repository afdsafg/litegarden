"""Real traversal validation for the ``walk_no_jump_v1`` profile (P1).

PROFILE
-------
``walk_no_jump_v1`` models a player that walks **without jumping**:

* every walk surface is a block shape that is explicitly listed in the
  whitelists below - nothing else is assumed,
* two adjacent walk surfaces may differ by at most ``0.5`` in world height
  (any descent is accepted),
* a difference in ``(0.5, 1.0]`` is only accepted when the voxel directly above
  the **lower** unit provides a verified half-step transition (a ``type=bottom``
  slab or a ``half=bottom`` / ``shape=straight`` stair with the correct facing),
  which splits the step into two ``0.5`` hops,
* a difference above ``1.0`` is always rejected,
* a walk surface needs a supporting voxel below (``supports=True``); ``type=top``
  slabs and stairs carry their own support and are exempt,
* ``min_headroom`` consecutive ``minecraft:air`` voxels are required above the
  walk surface.

The world height of a walk voxel is ``voxel.y + shape.surface``.

SUPPORTED SHAPES (whitelist driven)
-----------------------------------
* air: ``minecraft:air`` / ``minecraft:cave_air`` / ``minecraft:void_air``,
* full cubes: :data:`FULL_CUBE_BLOCKS` (stone, stone bricks, dirt family, sand,
  sandstone, clay, moss, planks, logs, oak leaves),
* stairs: :data:`STAIR_BLOCKS` with ``half=bottom`` (the default) and
  ``shape=straight`` (the default or missing),
* slabs: :data:`SLAB_BLOCKS` with ``type`` ``bottom`` (default) / ``top`` /
  ``double``.

Everything else - fences, lanterns, decoration blocks, ``minecraft:water``,
modded blocks, a ``half=top`` stair, an unlisted ``_stairs`` block, an air state
that carries unknown properties - is ``unsupported``. **Unsupported shapes are
never guessed as full cubes**: they block road, headroom, support, entry and
boundary validation instead of passing it. An unknown voxel (``None`` from the
sampler) is *not* air either: it is ``unsupported`` and therefore blocking.

COORDINATES
-----------
Every position handled here is a ``p_local`` voxel index; samplers, ``cells``,
``pos_local`` and the caller's ``entry`` / ``interface`` mappings use that same
local frame.

ISSUE CODES AND RULE IDS
------------------------
=================================  ================================
code                               rule_id
=================================  ================================
``ENTRY_BLOCKED``                  ``walk_no_jump_v1/entry``
``PATH_HEADROOM_BLOCKED``          ``walk_no_jump_v1/headroom``
``PATH_STEP_INVALID``              ``walk_no_jump_v1/step``
``PATH_DISCONNECTED``              ``walk_no_jump_v1/connectivity``
``SUPPORT_RULE_VIOLATION``         ``walk_no_jump_v1/support``
``BOUNDARY_ANCHOR_BROKEN``         ``walk_no_jump_v1/boundary``
=================================  ================================

Every public check returns a de-duplicated list of :class:`TraversalIssue`
sorted by ``pos_local``, then ``code`` (and the remaining fields as final
tie breakers), so repeated runs and reordered inputs produce identical lists.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "PROFILE_WALK_NO_JUMP_V1",
    "Sampler",
    "Voxel",
    "BlockShape",
    "TraversalIssue",
    "AIR_BLOCKS",
    "FULL_CUBE_BLOCKS",
    "STAIR_BLOCKS",
    "SLAB_BLOCKS",
    "KIND_AIR",
    "KIND_FULL_CUBE",
    "KIND_STAIRS",
    "KIND_SLAB",
    "KIND_NON_COLLIDING",
    "KIND_UNSUPPORTED",
    "FACING_VECTORS",
    "parse_state",
    "classify_state",
    "surface_y_units",
    "check_headroom",
    "check_road",
    "check_connectivity",
    "check_entry",
    "check_boundary_interface",
    "cut_fill_report",
]

Voxel = Tuple[int, int, int]
Sampler = Callable[[Voxel], Optional[str]]

PROFILE_WALK_NO_JUMP_V1 = "walk_no_jump_v1"

#: Maximum height difference (in voxel units) that is walkable without any
#: transition.
STEP_MAX_UNITS = 0.5
#: Maximum height difference that a verified transition can bridge.
MAX_STEP_UNITS = 1.0
#: Step tolerance of the P1 8.3 entrance passage search.
ENTRY_MAX_STEP_UNITS = 1.0
_EPS = 1e-9

#: Values of the ``kind`` field of :class:`BlockShape`.
KIND_AIR = "air"
KIND_FULL_CUBE = "full_cube"
KIND_STAIRS = "stairs"
KIND_SLAB = "slab"
#: Reserved for other profiles; ``walk_no_jump_v1`` never emits it (a block that
#: is not on a whitelist is ``unsupported``, not silently "non colliding").
KIND_NON_COLLIDING = "non_colliding"
KIND_UNSUPPORTED = "unsupported"

AIR_BLOCKS = frozenset(
    {
        "minecraft:air",
        "minecraft:cave_air",
        "minecraft:void_air",
    }
)

FULL_CUBE_BLOCKS = frozenset(
    {
        "minecraft:stone",
        "minecraft:stone_bricks",
        "minecraft:cobblestone",
        "minecraft:gravel",
        "minecraft:dirt",
        "minecraft:grass_block",
        "minecraft:coarse_dirt",
        "minecraft:rooted_dirt",
        "minecraft:podzol",
        "minecraft:mycelium",
        "minecraft:sand",
        "minecraft:sandstone",
        "minecraft:clay",
        "minecraft:moss_block",
        "minecraft:oak_planks",
        "minecraft:spruce_planks",
        "minecraft:birch_planks",
        "minecraft:oak_log",
        "minecraft:spruce_log",
        "minecraft:birch_log",
        "minecraft:oak_leaves",
    }
)

STAIR_BLOCKS = frozenset(
    {
        "minecraft:stone_brick_stairs",
        "minecraft:cobblestone_stairs",
        "minecraft:oak_stairs",
        "minecraft:spruce_stairs",
        "minecraft:stone_stairs",
        "minecraft:sandstone_stairs",
    }
)

SLAB_BLOCKS = frozenset(
    {
        "minecraft:stone_brick_slab",
        "minecraft:cobblestone_slab",
        "minecraft:oak_slab",
        "minecraft:spruce_slab",
        "minecraft:stone_slab",
        "minecraft:sandstone_slab",
    }
)

#: ``facing`` value -> unit vector. For stairs ``facing`` is read as the
#: **ascent direction**: ``facing=east`` puts the full-height half of the voxel
#: in the ``+x`` half and the half step in the ``-x`` half (Minecraft sets
#: ``StairBlock.FACING`` from the placer's horizontal look direction).
FACING_VECTORS: Dict[str, Voxel] = {
    "north": (0, 0, -1),
    "south": (0, 0, 1),
    "east": (1, 0, 0),
    "west": (-1, 0, 0),
}

_NEIGHBOURS_6: Tuple[Voxel, ...] = (
    (1, 0, 0),
    (-1, 0, 0),
    (0, 1, 0),
    (0, -1, 0),
    (0, 0, 1),
    (0, 0, -1),
)


@dataclass(frozen=True)
class BlockShape:
    """Verified geometry of one voxel for one traversal profile.

    ``surface`` is the offset in voxel units from the bottom of the voxel to the
    profile's *verified walkable top surface*:

    * ``air`` and unsupported shapes: ``0.0`` (there is no verified surface),
    * full cube / ``type=double`` slab / ``type=top`` slab: ``1.0``,
    * ``type=bottom`` slab: ``0.5``,
    * supported ``half=bottom`` / ``shape=straight`` stair: ``1.0``, the top
      step of the stair.

    A stair is **not** a single-surface shape: it also provides a verified
    ``0.5`` half step, exposed through ``platforms == (0.5, 1.0)``. That half
    step is what :func:`check_road` / :func:`check_connectivity` use to accept a
    ``1.0`` step: lower unit -> ``0.5`` half step -> upper unit are then two
    ``0.5`` hops. The stair's ``facing`` is the ascent direction, so the half
    step lies on the side opposite ``facing`` and the tall half lies on the
    ``facing`` side (the tall half must adjoin the upper unit).

    Extension fields, all derived from the same whitelists:

    * ``platforms``: every verified walkable platform offset inside the voxel,
      ascending. Empty for air and unsupported shapes.
    * ``facing``: stair ascent direction (``north``/``south``/``east``/``west``),
      ``""`` when the state carries no valid facing.
    * ``needs_support_below``: whether a ``supports=True`` voxel is required
      directly below when this shape is used as a walk surface. ``False`` for
      ``type=top`` slabs, which hang from above and leave the lower half of the
      voxel empty.
    """

    block_id: str
    kind: str
    surface: float
    walkable: bool
    supports: bool
    detail: str = ""
    platforms: Tuple[float, ...] = ()
    facing: str = ""
    needs_support_below: bool = True


@dataclass(frozen=True)
class TraversalIssue:
    """One traversal violation, anchored at a concrete ``p_local`` voxel."""

    code: str
    pos_local: Voxel
    rule_id: str
    expected: str
    actual: str
    detail: str = ""


def _require_profile(profile: str) -> None:
    if profile != PROFILE_WALK_NO_JUMP_V1:
        raise ValueError(
            f"unknown traversal profile {profile!r}; "
            f"only {PROFILE_WALK_NO_JUMP_V1!r} is implemented"
        )


def _require_min_headroom(min_headroom: int) -> None:
    if int(min_headroom) < 0:
        raise ValueError(f"min_headroom must be >= 0, got {min_headroom!r}")


def _as_voxel(pos) -> Voxel:
    """Normalise ``(x, y, z)`` / ``[x, y, z]`` into an int tuple."""
    try:
        x, y, z = pos
    except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
        raise ValueError(f"expected a (x, y, z) voxel, got {pos!r}") from exc
    return (int(x), int(y), int(z))


def _unsupported(block_id: str, detail: str) -> BlockShape:
    return BlockShape(
        block_id=block_id,
        kind=KIND_UNSUPPORTED,
        surface=0.0,
        walkable=False,
        supports=False,
        detail=detail,
    )


def _full_cube(block_id: str) -> BlockShape:
    return BlockShape(
        block_id=block_id,
        kind=KIND_FULL_CUBE,
        surface=1.0,
        walkable=True,
        supports=True,
        platforms=(1.0,),
    )


def parse_state(state: str) -> Tuple[str, Dict[str, str]]:
    """Split a canonical block state string into ``(block_id, properties)``.

    Both ``"minecraft:air"`` and
    ``"minecraft:oak_stairs[facing=north,half=bottom,shape=straight]"`` are
    accepted. The property order in the input is irrelevant (the result is a
    mapping keyed by property name), whitespace around brackets, commas and
    ``=`` is tolerated, and the block id plus property keys are lower-cased.

    Raises ``ValueError`` for anything that is not a canonical state string;
    callers (see :func:`classify_state`) turn that into ``unsupported`` instead
    of guessing a shape.
    """
    if not isinstance(state, str):
        raise ValueError(f"block state must be a string, got {type(state).__name__}")
    text = state.strip()
    if not text:
        raise ValueError("empty block state")
    if "[" not in text:
        if "]" in text or "=" in text:
            raise ValueError(f"malformed block state: {state!r}")
        return text.lower(), {}
    head, bracket, tail = text.partition("[")
    if not bracket or not tail.endswith("]"):
        raise ValueError(f"malformed block state: {state!r}")
    block_id = head.strip().lower()
    if not block_id:
        raise ValueError(f"malformed block state: {state!r}")
    props: Dict[str, str] = {}
    body = tail[:-1].strip()
    if body:
        for item in body.split(","):
            key, separator, value = item.partition("=")
            key = key.strip().lower()
            value = value.strip()
            if not separator or not key or not value:
                raise ValueError(f"malformed block state property: {item!r}")
            props[key] = value
    return block_id, props


def _classify_stair(block_id: str, props: Dict[str, str]) -> BlockShape:
    unknown = sorted(set(props) - {"facing", "half", "shape", "waterlogged"})
    if unknown:
        return _unsupported(
            block_id,
            f"stair properties {unknown} are not verified by walk_no_jump_v1",
        )
    waterlogged = props.get("waterlogged", "false")
    if waterlogged not in ("true", "false"):
        return _unsupported(
            block_id, f"stair waterlogged={waterlogged!r} is not a verified value"
        )
    if waterlogged == "true":
        return _unsupported(
            block_id,
            "stair waterlogged=true is not a verified walk surface "
            "(water in the voxel is not modelled by walk_no_jump_v1)",
        )
    half = props.get("half", "bottom")
    if half != "bottom":
        return _unsupported(
            block_id,
            f"stair half={half} is not supported by walk_no_jump_v1 "
            "(only half=bottom, shape=straight)",
        )
    shape = props.get("shape", "straight")
    if shape != "straight":
        return _unsupported(
            block_id,
            f"stair shape={shape} is not supported by walk_no_jump_v1 "
            "(only half=bottom, shape=straight)",
        )
    facing = props.get("facing", "")
    if facing and facing not in FACING_VECTORS:
        return _unsupported(
            block_id, f"stair facing={facing!r} is not a horizontal direction"
        )
    return BlockShape(
        block_id=block_id,
        kind=KIND_STAIRS,
        surface=1.0,
        walkable=True,
        supports=True,
        platforms=(0.5, 1.0),
        facing=facing,
    )


def _classify_slab(block_id: str, props: Dict[str, str]) -> BlockShape:
    unknown = sorted(set(props) - {"type", "waterlogged"})
    if unknown:
        return _unsupported(
            block_id,
            f"slab properties {unknown} are not verified by walk_no_jump_v1",
        )
    waterlogged = props.get("waterlogged", "false")
    if waterlogged not in ("true", "false"):
        return _unsupported(
            block_id, f"slab waterlogged={waterlogged!r} is not a verified value"
        )
    if waterlogged == "true":
        return _unsupported(
            block_id,
            "slab waterlogged=true is not a verified walk surface "
            "(water in the voxel is not modelled by walk_no_jump_v1)",
        )
    slab_type = props.get("type", "bottom")
    if slab_type == "double":
        # a double slab occupies the whole voxel: full cube geometry
        return BlockShape(
            block_id=block_id,
            kind=KIND_SLAB,
            surface=1.0,
            walkable=True,
            supports=True,
            platforms=(1.0,),
        )
    if slab_type == "bottom":
        return BlockShape(
            block_id=block_id,
            kind=KIND_SLAB,
            surface=0.5,
            walkable=True,
            supports=True,
            platforms=(0.5,),
        )
    if slab_type == "top":
        # the lower half of the voxel stays empty and the slab hangs from above
        return BlockShape(
            block_id=block_id,
            kind=KIND_SLAB,
            surface=1.0,
            walkable=True,
            supports=True,
            platforms=(1.0,),
            needs_support_below=False,
        )
    return _unsupported(
        block_id,
        f"slab type={slab_type} is not supported by walk_no_jump_v1 "
        "(only bottom, top, double)",
    )


def classify_state(
    state: Optional[str], profile: str = PROFILE_WALK_NO_JUMP_V1
) -> BlockShape:
    """Classify one voxel state for ``walk_no_jump_v1``.

    ``None`` means "unknown voxel / outside the input range" and returns
    ``kind="unsupported"`` with ``detail="unknown voxel"`` - it is never treated
    as air. States that are not on a whitelist return ``kind="unsupported"``
    too, so no unverified collision shape is ever assumed.
    """
    _require_profile(profile)
    if state is None:
        return _unsupported("", "unknown voxel")
    try:
        block_id, props = parse_state(state)
    except ValueError as exc:
        return _unsupported("", f"unparseable block state {state!r}: {exc}")
    if block_id in AIR_BLOCKS:
        if props:
            return _unsupported(
                block_id, f"air state {state!r} carries unknown properties"
            )
        return BlockShape(
            block_id=block_id,
            kind=KIND_AIR,
            surface=0.0,
            walkable=False,
            supports=False,
        )
    if block_id in FULL_CUBE_BLOCKS:
        unknown = sorted(set(props) - {"snowy"})
        if unknown or props.get("snowy", "false") not in ("true", "false"):
            return _unsupported(
                block_id,
                f"full cube state {state!r} carries properties that are not "
                "verified by walk_no_jump_v1",
            )
        return _full_cube(block_id)
    if block_id.endswith("_stairs") and block_id in STAIR_BLOCKS:
        return _classify_stair(block_id, props)
    if block_id in SLAB_BLOCKS:
        return _classify_slab(block_id, props)
    return _unsupported(block_id, f"block not in verified walk profile: {block_id}")


def surface_y_units(shapes: Sequence[BlockShape]) -> float:
    """Top-most verified walk surface of a bottom-to-top column of shapes.

    ``shapes`` is ordered bottom first. The returned value is the offset in
    voxel units from the bottom of ``shapes[0]``: the highest
    ``index + shape.surface`` over the shapes that are walkable and carry a
    verified platform. ``0.0`` means the column has no verified walk surface
    (empty column, air only, unsupported shapes only).
    """
    top = 0.0
    for index, shape in enumerate(shapes):
        if shape.walkable and shape.platforms:
            top = max(top, index + shape.surface)
    return top


def _state_key(state: Optional[str]) -> Tuple[object, ...]:
    """Canonical comparison key so that attribute order never matters."""
    if state is None:
        return ("\u0000unknown",)
    try:
        block_id, props = parse_state(state)
    except ValueError:
        return ("\u0000raw", state)
    return (block_id, tuple(sorted(props.items())))


def _state_text(state: Optional[str]) -> str:
    return "<unknown>" if state is None else str(state)


def _lookup(
    sampler: Sampler, profile: str
) -> Callable[[Voxel], Tuple[Optional[str], BlockShape]]:
    def _get(pos: Voxel) -> Tuple[Optional[str], BlockShape]:
        voxel = (int(pos[0]), int(pos[1]), int(pos[2]))
        state = sampler(voxel)
        return state, classify_state(state, profile)

    return _get


def _issue(
    code: str, pos: Voxel, rule_id: str, expected: str, actual: str, detail: str = ""
) -> TraversalIssue:
    return TraversalIssue(
        code=code,
        pos_local=(int(pos[0]), int(pos[1]), int(pos[2])),
        rule_id=rule_id,
        expected=expected,
        actual=actual,
        detail=detail,
    )


def _finish(issues: Iterable[TraversalIssue]) -> List[TraversalIssue]:
    unique = {
        (i.code, i.pos_local, i.rule_id, i.expected, i.actual, i.detail): i
        for i in issues
    }
    return sorted(
        unique.values(),
        key=lambda i: (
            i.pos_local,
            i.code,
            i.rule_id,
            i.expected,
            i.actual,
            i.detail,
        ),
    )


def _world_y(voxel: Voxel, shape: BlockShape) -> float:
    return voxel[1] + shape.surface


def _horizontal_delta(a: Voxel, b: Voxel) -> Voxel:
    return (b[0] - a[0], 0, b[2] - a[2])


def _dominant_axis(delta: Voxel) -> Voxel:
    """Signed unit vector of the dominant horizontal axis of ``delta``."""
    if abs(delta[0]) == 0 and abs(delta[2]) == 0:
        return (1, 0, 0)
    if abs(delta[0]) >= abs(delta[2]):
        return (1, 0, 0) if delta[0] >= 0 else (-1, 0, 0)
    return (0, 0, 1) if delta[2] >= 0 else (0, 0, -1)


def _lateral_axis(direction: Voxel) -> Voxel:
    """Horizontal axis perpendicular to a road direction."""
    return (0, 0, 1) if direction[0] != 0 else (1, 0, 0)


def _facing_name(direction: Voxel) -> str:
    for name, vector in FACING_VECTORS.items():
        if vector == direction:
            return name
    return f"{direction}"


def _surface_issues(
    voxel: Voxel, state: Optional[str], shape: BlockShape
) -> List[TraversalIssue]:
    """Problem when a road-surface voxel carries no verified walk surface."""
    if shape.walkable:
        return []
    text = _state_text(state)
    if shape.kind == KIND_AIR:
        return [
            _issue(
                "PATH_STEP_INVALID",
                voxel,
                "walk_no_jump_v1/step",
                "a walk surface verified by walk_no_jump_v1",
                f"{voxel} is {text} (air)",
                f"road voxel {voxel} is air: there is no walkable surface here",
            )
        ]
    return [
        _issue(
            "PATH_HEADROOM_BLOCKED",
            voxel,
            "walk_no_jump_v1/headroom",
            "a walk surface verified by walk_no_jump_v1",
            f"{voxel} is {text} ({shape.kind})",
            f"{voxel} is occupied by {text}; walk_no_jump_v1 does not guess the "
            "collision shape of unverified blocks",
        )
    ]


def _support_issues(
    get: Callable[[Voxel], Tuple[Optional[str], BlockShape]],
    voxel: Voxel,
    shape: BlockShape,
) -> List[TraversalIssue]:
    """Problem when a walk surface has no verified support below it."""
    if not shape.walkable or not shape.needs_support_below:
        return []
    below = (voxel[0], voxel[1] - 1, voxel[2])
    below_state, below_shape = get(below)
    if below_shape.supports:
        return []
    return [
        _issue(
            "SUPPORT_RULE_VIOLATION",
            voxel,
            "walk_no_jump_v1/support",
            "the voxel below a walk surface is a supporting shape (supports=True)",
            f"{below} is {_state_text(below_state)} ({below_shape.kind})",
            f"walk surface {voxel} ({shape.block_id or shape.kind}) has no "
            f"verified support below at {below}",
        )
    ]


def _headroom_issues(
    get: Callable[[Voxel], Tuple[Optional[str], BlockShape]],
    voxel: Voxel,
    shape: BlockShape,
    *,
    min_headroom: int,
    code: str = "PATH_HEADROOM_BLOCKED",
    rule_id: str = "walk_no_jump_v1/headroom",
) -> List[TraversalIssue]:
    """Problem when ``min_headroom`` air voxels are missing above a walk surface.

    A verified sub-block platform (``0.5`` slab / stair half step) directly above
    the walk voxel is not an obstruction: the player simply stands on it, so the
    required headroom starts one voxel higher.
    """
    if not shape.walkable or min_headroom <= 0:
        return []
    level = voxel[1] + 1
    above_state, above_shape = get((voxel[0], level, voxel[2]))
    if (
        above_shape.walkable
        and above_shape.platforms
        and min(above_shape.platforms) <= STEP_MAX_UNITS + _EPS
    ):
        level += 1
    for y in range(level, level + int(min_headroom)):
        probe = (voxel[0], y, voxel[2])
        probe_state, probe_shape = get(probe)
        if probe_shape.kind != KIND_AIR:
            return [
                _issue(
                    code,
                    probe,
                    rule_id,
                    f"{int(min_headroom)} consecutive minecraft:air voxels above "
                    "the walk surface",
                    f"{probe} is {_state_text(probe_state)} ({probe_shape.kind})",
                    f"headroom of walk surface {voxel} is blocked at {probe}",
                )
            ]
    return []


def _step_transition(
    get: Callable[[Voxel], Tuple[Optional[str], BlockShape]],
    lower: Voxel,
    upper: Voxel,
) -> Tuple[bool, str]:
    """Is the ``lower -> upper`` step walkable under ``walk_no_jump_v1``?

    Returns ``(True, "")`` or ``(False, reason)``. A step of at most ``0.5``
    (including every descent) is always walkable. A step in ``(0.5, 1.0]`` is
    walkable only when the voxel directly above ``lower`` provides a verified
    ``0.5`` platform (``type=bottom`` slab or the half step of a
    ``half=bottom``/``shape=straight`` stair) and, for stairs, when the stair
    faces the ascent direction, i.e. towards ``upper``.
    """
    lower_shape = get(lower)[1]
    upper_shape = get(upper)[1]
    dh = _world_y(upper, upper_shape) - _world_y(lower, lower_shape)
    if dh <= STEP_MAX_UNITS + _EPS:
        return True, ""
    if dh > MAX_STEP_UNITS + _EPS:
        return (
            False,
            f"step {dh} is above the maximum walkable step "
            f"{MAX_STEP_UNITS} even for lower units that provide a transition",
        )
    direction = _horizontal_delta(lower, upper)
    if direction not in FACING_VECTORS.values() or lower[1] != upper[1] - 1:
        return (
            False,
            f"{lower} -> {upper} is not a horizontal step of one block; only "
            "horizontal neighbours one level apart can use a transition above "
            "the lower unit",
        )
    transition_pos = (lower[0], lower[1] + 1, lower[2])
    transition_state, transition = get(transition_pos)
    if not transition.walkable or not transition.platforms:
        return (
            False,
            f"no verified transition above the lower unit: {transition_pos} is "
            f"{_state_text(transition_state)} ({transition.kind})",
        )
    if transition.kind == KIND_STAIRS:
        if not transition.facing or FACING_VECTORS.get(transition.facing) != direction:
            return (
                False,
                f"stair at {transition_pos} faces "
                f"{transition.facing or '<missing>'} but the step rises towards "
                f"{_facing_name(direction)}; a verified stair transition needs "
                "facing == ascent direction",
            )
    for platform in transition.platforms:
        if platform > STEP_MAX_UNITS + _EPS:
            continue
        transition_y = transition_pos[1] + platform
        if (
            transition_y - _world_y(lower, lower_shape) <= STEP_MAX_UNITS + _EPS
            and _world_y(upper, upper_shape) - transition_y <= STEP_MAX_UNITS + _EPS
        ):
            return True, ""
    return (
        False,
        f"no verified intermediate 0.5 platform in {transition_pos} splits the "
        f"{dh} step into two walkable 0.5 hops",
    )


def _pair_step_issues(
    get: Callable[[Voxel], Tuple[Optional[str], BlockShape]],
    lower: Voxel,
    upper: Voxel,
) -> List[TraversalIssue]:
    """Step issues for one ordered pair of road voxels (order defines walking)."""
    lower_state, lower_shape = get(lower)
    upper_state, upper_shape = get(upper)
    issues: List[TraversalIssue] = []
    for voxel, state, shape in (
        (lower, lower_state, lower_shape),
        (upper, upper_state, upper_shape),
    ):
        if not shape.walkable:
            issues.extend(_surface_issues(voxel, state, shape))
    if issues:
        return issues
    ok, reason = _step_transition(get, lower, upper)
    if ok:
        return []
    dh = _world_y(upper, upper_shape) - _world_y(lower, lower_shape)
    return [
        _issue(
            "PATH_STEP_INVALID",
            lower,
            "walk_no_jump_v1/step",
            "step <= 0.5 without jump",
            f"step {dh}",
            f"{lower} ({_world_y(lower, lower_shape)}) -> {upper} "
            f"({_world_y(upper, upper_shape)}): {reason}",
        )
    ]


def check_headroom(
    sampler: Sampler,
    cell: Voxel,
    *,
    profile: str = PROFILE_WALK_NO_JUMP_V1,
    min_headroom: int = 2,
) -> List[TraversalIssue]:
    """Headroom above a single road cell.

    ``cell`` must be a walkable voxel under ``walk_no_jump_v1`` and the
    ``min_headroom`` voxels above its verified walk surface must all be
    ``minecraft:air``. The returned list points at the offending voxel: the cell
    itself when it carries no verified walk surface, otherwise the first
    non-air voxel above it.
    """
    _require_profile(profile)
    _require_min_headroom(min_headroom)
    get = _lookup(sampler, profile)
    voxel = _as_voxel(cell)
    state, shape = get(voxel)
    if not shape.walkable:
        return _finish(_surface_issues(voxel, state, shape))
    return _finish(
        _headroom_issues(get, voxel, shape, min_headroom=int(min_headroom))
    )


def check_connectivity(
    sampler: Sampler,
    cells: Sequence[Voxel],
    *,
    profile: str = PROFILE_WALK_NO_JUMP_V1,
    min_headroom: int = 2,
) -> List[TraversalIssue]:
    """Verify a sequence of road cells as a continuous no-jump walk.

    Consecutive cells are adjacent when they are horizontal 4-neighbours
    (``|dx| + |dz| == 1``); the ``y`` difference is the step that is being
    checked, which is what makes a one-block step checkable at all. Cells that
    are not horizontal neighbours (a gap, a diagonal, duplicated cells) are
    reported as ``PATH_DISCONNECTED``.

    For every cell the verified walk surface, its support and the headroom are
    checked, and for every pair the step rule of the profile is applied
    (:func:`_step_transition`).
    """
    _require_profile(profile)
    _require_min_headroom(min_headroom)
    get = _lookup(sampler, profile)
    positions = [_as_voxel(c) for c in cells]
    issues: List[TraversalIssue] = []
    for voxel in positions:
        state, shape = get(voxel)
        if not shape.walkable:
            issues.extend(_surface_issues(voxel, state, shape))
            continue
        issues.extend(_support_issues(get, voxel, shape))
        issues.extend(
            _headroom_issues(get, voxel, shape, min_headroom=int(min_headroom))
        )
    for lower, upper in zip(positions, positions[1:]):
        horizontal = abs(lower[0] - upper[0]) + abs(lower[2] - upper[2])
        if horizontal != 1:
            issues.append(
                _issue(
                    "PATH_DISCONNECTED",
                    lower,
                    "walk_no_jump_v1/connectivity",
                    "consecutive road cells are horizontal 4-neighbours",
                    f"{lower} -> {upper} is not a horizontal neighbour "
                    f"(|dx| + |dz| = {horizontal})",
                    "the road surface is not continuous between these two cells",
                )
            )
            continue
        issues.extend(_pair_step_issues(get, lower, upper))
    return _finish(issues)


def _road_direction(cells: Sequence[Voxel], index: int) -> Voxel:
    """Road direction at ``cells[index]`` from the neighbouring difference.

    The following difference is used when it exists, otherwise the preceding
    one; a single-cell road falls back to ``+x``.
    """
    if index + 1 < len(cells):
        delta = _horizontal_delta(cells[index], cells[index + 1])
    elif index > 0:
        delta = _horizontal_delta(cells[index - 1], cells[index])
    else:
        delta = (1, 0, 0)
    return _dominant_axis(delta)


def check_road(
    sampler: Sampler,
    cells: Sequence[Voxel],
    width: int,
    *,
    profile: str = PROFILE_WALK_NO_JUMP_V1,
    min_headroom: int = 2,
) -> List[TraversalIssue]:
    """Verify a road by its centre line and full width.

    ``cells`` are centre-line voxels (``y`` is the road surface voxel height) and
    ``width`` is the integer road width. The cross section of a cell spans the
    offsets ``-width // 2 .. width // 2`` along the axis perpendicular to the
    road direction (identical to ``operations.path.pave_path``, so even widths
    round up to the next odd number of voxels). The direction comes from the
    difference to the next cell, or from the previous cell for the last one.

    Checked for every voxel of the full width: a verified walk surface
    (``walkable``), support below and ``min_headroom`` air voxels above.
    Consecutive cross sections of the full width must also step by at most
    ``0.5``, or by at most ``1.0`` with a verified transition above the lower
    voxel; across a turn the two cross sections are not parallel, so only the
    centre-line rule applies there. Finally :func:`check_connectivity` runs on
    the centre line (adjacency, step rule, support, headroom).

    A verified ``0.5`` platform directly above a road voxel is a legal half step
    (the player stands on it) and is not treated as a headroom obstruction; the
    required headroom then starts one voxel higher.
    """
    _require_profile(profile)
    _require_min_headroom(min_headroom)
    if int(width) < 1:
        raise ValueError(f"road width must be a positive integer, got {width!r}")
    get = _lookup(sampler, profile)
    centre = [_as_voxel(c) for c in cells]
    if not centre:
        return []
    half = int(width) // 2
    sections: List[Tuple[Voxel, List[Voxel]]] = []
    for index, cell in enumerate(centre):
        direction = _road_direction(centre, index)
        lateral = _lateral_axis(direction)
        section = [
            (cell[0] + lateral[0] * offset, cell[1], cell[2] + lateral[2] * offset)
            for offset in range(-half, half + 1)
        ]
        sections.append((direction, section))

    issues: List[TraversalIssue] = []
    # 1. every voxel of the full road width
    for _direction, section in sections:
        for voxel in section:
            state, shape = get(voxel)
            if not shape.walkable:
                issues.extend(_surface_issues(voxel, state, shape))
                continue
            issues.extend(_support_issues(get, voxel, shape))
            issues.extend(
                _headroom_issues(get, voxel, shape, min_headroom=int(min_headroom))
            )
    # 2. full-width step consistency between adjacent cross sections
    for index in range(len(sections) - 1):
        direction_a, section_a = sections[index]
        direction_b, section_b = sections[index + 1]
        if direction_a != direction_b:
            # a turn: the cross sections are perpendicular to each other
            continue
        if (
            abs(centre[index][0] - centre[index + 1][0])
            + abs(centre[index][2] - centre[index + 1][2])
            != 1
        ):
            # reported by check_connectivity as PATH_DISCONNECTED
            continue
        for lower, upper in zip(section_a, section_b):
            issues.extend(_pair_step_issues(get, lower, upper))
    # 3. centre line connectivity
    issues.extend(
        check_connectivity(
            sampler, centre, profile=profile, min_headroom=int(min_headroom)
        )
    )
    return _finish(issues)


def _feet_height(
    get: Callable[[Voxel], Tuple[Optional[str], BlockShape]], voxel: Voxel
) -> Optional[float]:
    """Height of the verified surface a player stands on at ``voxel``.

    A walkable voxel stands for its own top surface (the anchor/floor case); an
    air voxel stands for the top of the supporting voxel directly below it.
    Returns ``None`` when there is no verified standable surface.
    """
    _state, shape = get(voxel)
    if shape.walkable and shape.platforms:
        return voxel[1] + shape.surface
    if shape.kind != KIND_AIR:
        return None
    below = (voxel[0], voxel[1] - 1, voxel[2])
    _below_state, below_shape = get(below)
    if below_shape.walkable and below_shape.supports:
        return below[1] + below_shape.surface
    return None


def check_entry(
    sampler: Sampler,
    entry: dict,
    *,
    profile: str = PROFILE_WALK_NO_JUMP_V1,
    min_headroom: int = 2,
) -> List[TraversalIssue]:
    """Verify one asset entrance contract.

    ``entry`` is a mapping with ``id``, ``bounds`` (``min`` /
    ``max_exclusive``, a half-open passage volume that must be entirely
    ``minecraft:air``), ``inside`` (the landing voxel inside the asset),
    ``outside`` (the link voxel outside it) and ``width``.

    Checked: the passage volume must be all air (``ENTRY_BLOCKED`` per blocked
    voxel); ``inside``/``outside`` must be walkable with support
    (``SUPPORT_RULE_VIOLATION``) and enough headroom; the declared ``width``
    must fit into the passage volume perpendicular to the entrance direction;
    and there must be an air-only path from ``outside`` to ``inside`` through
    the passage volume. The path walks voxel by voxel (6-neighbourhood), every
    step must be at most ``1`` in world height and every landing must have a
    supporting floor (P1 8.3 tolerance). Without such a path the result is
    ``ENTRY_BLOCKED``, positioned at the first blocked voxel of the volume when
    there is one, otherwise at ``inside``.

    The passage volume must include the landing column of both anchors (the
    voxels directly above ``inside`` and ``outside``); an anchor without an
    adjacent passage voxel can never be reached by the search. A malformed
    contract (missing ``bounds``, empty volume) raises ``ValueError``.
    """
    _require_profile(profile)
    _require_min_headroom(min_headroom)
    get = _lookup(sampler, profile)
    bounds = entry.get("bounds") or {}
    if "min" not in bounds or "max_exclusive" not in bounds:
        raise ValueError(f"entry {entry.get('id')!r} has no usable bounds volume")
    low = _as_voxel(bounds["min"])
    high = _as_voxel(bounds["max_exclusive"])
    if any(high[axis] <= low[axis] for axis in range(3)):
        raise ValueError(
            f"entry {entry.get('id')!r} has an empty bounds volume: "
            f"{low} .. {high}"
        )
    inside = _as_voxel(entry["inside"])
    outside = _as_voxel(entry["outside"])
    entry_id = str(entry.get("id", ""))
    width = int(entry.get("width", 1) or 1)

    issues: List[TraversalIssue] = []
    # (a) the passage volume must be all air
    blocking: List[Tuple[Voxel, Optional[str], BlockShape]] = []
    for x in range(low[0], high[0]):
        for y in range(low[1], high[1]):
            for z in range(low[2], high[2]):
                voxel = (x, y, z)
                state, shape = get(voxel)
                if shape.kind == KIND_AIR:
                    continue
                blocking.append((voxel, state, shape))
                issues.append(
                    _issue(
                        "ENTRY_BLOCKED",
                        voxel,
                        "walk_no_jump_v1/entry",
                        "every voxel of the entrance passage volume is "
                        "minecraft:air",
                        f"{voxel} is {_state_text(state)} ({shape.kind})",
                        f"entry {entry_id}: passage volume is blocked at {voxel}",
                    )
                )
    # (b) the landings must be walkable, supported and have headroom
    for label, anchor in (("inside", inside), ("outside", outside)):
        state, shape = get(anchor)
        if not shape.walkable:
            issues.append(
                _issue(
                    "ENTRY_BLOCKED",
                    anchor,
                    "walk_no_jump_v1/entry",
                    f"the {label} landing is a walk surface verified by "
                    "walk_no_jump_v1",
                    f"{anchor} is {_state_text(state)} ({shape.kind})",
                    f"entry {entry_id}: the {label} landing is not a verified "
                    "walk surface",
                )
            )
            continue
        issues.extend(_support_issues(get, anchor, shape))
        issues.extend(
            _headroom_issues(
                get,
                anchor,
                shape,
                min_headroom=int(min_headroom),
                code="ENTRY_BLOCKED",
                rule_id="walk_no_jump_v1/entry",
            )
        )
    # (b2) the declared width must fit into the passage volume
    direction = _dominant_axis(_horizontal_delta(outside, inside))
    perpendicular = 2 if direction[0] != 0 else 0
    span = high[perpendicular] - low[perpendicular]
    if width >= 1 and span < width:
        issues.append(
            _issue(
                "ENTRY_BLOCKED",
                outside,
                "walk_no_jump_v1/entry",
                f"the passage volume spans at least the declared entrance width "
                f"{width} perpendicular to the entrance direction",
                f"the passage volume spans {span} voxel(s) along the "
                f"perpendicular axis (entry runs towards {_facing_name(direction)})",
                f"entry {entry_id}: declared width {width} does not fit into the "
                "passage volume",
            )
        )
    # (c) an air-only path from outside to inside
    domain: Dict[Voxel, Optional[float]] = {}
    for x in range(low[0], high[0]):
        for y in range(low[1], high[1]):
            for z in range(low[2], high[2]):
                voxel = (x, y, z)
                _state, shape = get(voxel)
                if shape.kind != KIND_AIR:
                    continue
                domain[voxel] = _feet_height(get, voxel)
    for anchor in (inside, outside):
        domain[anchor] = _feet_height(get, anchor)
    reachable = False
    if domain.get(outside) is not None and domain.get(inside) is not None:
        seen = {outside}
        queue = deque([outside])
        while queue:
            current = queue.popleft()
            if current == inside:
                reachable = True
                break
            current_height = domain[current]
            if current_height is None:
                continue
            for step in _NEIGHBOURS_6:
                nxt = (
                    current[0] + step[0],
                    current[1] + step[1],
                    current[2] + step[2],
                )
                if nxt in seen or nxt not in domain:
                    continue
                next_height = domain[nxt]
                if next_height is None:
                    continue
                if abs(next_height - current_height) > ENTRY_MAX_STEP_UNITS + _EPS:
                    continue
                seen.add(nxt)
                queue.append(nxt)
    if not reachable:
        pos = blocking[0][0] if blocking else inside
        issues.append(
            _issue(
                "ENTRY_BLOCKED",
                pos,
                "walk_no_jump_v1/entry",
                f"an air-only path from outside {outside} to inside {inside} "
                f"with |step| <= {ENTRY_MAX_STEP_UNITS:g} and a supported landing",
                f"no such path inside the passage volume {low} .. {high}"
                + (f" (first blockage {pos})" if blocking else ""),
                f"entry {entry_id}: the entrance passage does not connect the "
                "outside link to the inside landing",
            )
        )
    return _finish(issues)


def check_boundary_interface(
    base_sampler: Sampler,
    candidate_sampler: Sampler,
    interface: dict,
    *,
    profile: str = PROFILE_WALK_NO_JUMP_V1,
) -> List[TraversalIssue]:
    """Verify a selection-boundary road interface against the base scene.

    ``interface`` is a mapping with ``id``, ``cells`` (the boundary road cross
    section), ``link_from`` (the existing external road unit) and ``link_to``
    (the selection-edge unit).

    Checked: (a) every interface voxel must have the **same state** in the base
    and the candidate scene (attribute order is normalised), otherwise
    ``BOUNDARY_ANCHOR_BROKEN``; (b) every interface voxel must still be walkable
    in the candidate with ``2`` air voxels above it; (c) ``link_from`` and
    ``link_to`` must sit on one straight corridor line through the interface
    (same lateral offset, consecutive horizontal neighbours) and every hop
    ``link_from -> interface -> link_to`` must obey the no-jump step rule of the
    profile, otherwise ``BOUNDARY_ANCHOR_BROKEN`` with the mismatching heights
    or the lateral mismatch in ``expected``/``actual``.
    """
    _require_profile(profile)
    get_base = _lookup(base_sampler, profile)
    get_candidate = _lookup(candidate_sampler, profile)
    interface_id = str(interface.get("id", ""))
    cells = [_as_voxel(c) for c in interface.get("cells", [])]
    link_from = _as_voxel(interface["link_from"])
    link_to = _as_voxel(interface["link_to"])
    issues: List[TraversalIssue] = []

    # (a) the interface itself must not be modified
    for voxel in cells:
        base_state = get_base(voxel)[0]
        candidate_state = get_candidate(voxel)[0]
        if _state_key(base_state) != _state_key(candidate_state):
            issues.append(
                _issue(
                    "BOUNDARY_ANCHOR_BROKEN",
                    voxel,
                    "walk_no_jump_v1/boundary",
                    "base and candidate state are identical at the boundary "
                    "interface",
                    f"base {_state_text(base_state)} -> candidate "
                    f"{_state_text(candidate_state)}",
                    f"interface {interface_id}: the candidate modifies the "
                    f"boundary anchor at {voxel}",
                )
            )
    # (b) the interface must stay walkable with headroom in the candidate
    for voxel in cells:
        candidate_state, candidate_shape = get_candidate(voxel)
        if not candidate_shape.walkable:
            issues.append(
                _issue(
                    "BOUNDARY_ANCHOR_BROKEN",
                    voxel,
                    "walk_no_jump_v1/boundary",
                    "boundary interface voxels stay walkable in the candidate "
                    "scene",
                    f"{voxel} is {_state_text(candidate_state)} "
                    f"({candidate_shape.kind})",
                    f"interface {interface_id}: the boundary road cross section "
                    "is no longer walkable",
                )
            )
            continue
        issues.extend(
            _headroom_issues(get_candidate, voxel, candidate_shape, min_headroom=2)
        )
    # (c) link_from -> interface -> link_to must be walkable and level
    for label, link in (("link_from", link_from), ("link_to", link_to)):
        state, shape = get_candidate(link)
        if not shape.walkable:
            issues.append(
                _issue(
                    "BOUNDARY_ANCHOR_BROKEN",
                    link,
                    "walk_no_jump_v1/boundary",
                    f"{label} is a walk surface verified by walk_no_jump_v1",
                    f"{link} is {_state_text(state)} ({shape.kind})",
                    f"interface {interface_id}: {label} is not a walkable road "
                    "unit",
                )
            )
    direction = _dominant_axis(_horizontal_delta(link_from, link_to))
    channel = 0 if direction[0] != 0 else 2
    axis_perpendicular = 2 if direction[0] != 0 else 0
    if link_from[axis_perpendicular] != link_to[axis_perpendicular]:
        issues.append(
            _issue(
                "BOUNDARY_ANCHOR_BROKEN",
                link_from,
                "walk_no_jump_v1/boundary",
                "link_from and link_to share one lateral offset, so the "
                "interface connects one road lane",
                f"lateral offset {'z' if axis_perpendicular == 2 else 'x'}: "
                f"{link_from[axis_perpendicular]} (link_from) vs "
                f"{link_to[axis_perpendicular]} (link_to)",
                f"interface {interface_id}: the external link and the selection "
                "edge do not line up laterally (width mismatch)",
            )
        )
        return _finish(issues)
    step = 1 if link_to[channel] > link_from[channel] else -1
    between: List[Tuple[int, Voxel]] = []
    for voxel in cells:
        if voxel[axis_perpendicular] != link_from[axis_perpendicular]:
            continue
        if voxel == link_from or voxel == link_to:
            continue
        before = (voxel[channel] - link_from[channel]) * step
        after = (link_to[channel] - voxel[channel]) * step
        if before > 0 and after > 0:
            between.append((before, voxel))
    between.sort()
    chain = [link_from] + [voxel for _offset, voxel in between] + [link_to]
    if not between:
        issues.append(
            _issue(
                "BOUNDARY_ANCHOR_BROKEN",
                link_to,
                "walk_no_jump_v1/boundary",
                "the interface contains a road voxel that lies between "
                "link_from and link_to",
                f"no interface voxel of {interface_id} lies between {link_from} "
                f"and {link_to}",
                f"interface {interface_id}: the declared interface does not "
                "bridge the external link and the selection edge",
            )
        )
        return _finish(issues)
    for lower, upper in zip(chain, chain[1:]):
        horizontal = abs(lower[0] - upper[0]) + abs(lower[2] - upper[2])
        if horizontal != 1:
            issues.append(
                _issue(
                    "BOUNDARY_ANCHOR_BROKEN",
                    lower,
                    "walk_no_jump_v1/boundary",
                    "link_from, the interface voxel and link_to are consecutive "
                    "horizontal neighbours",
                    f"{lower} -> {upper} is not a horizontal neighbour "
                    f"(|dx| + |dz| = {horizontal})",
                    f"interface {interface_id}: the interface does not line up "
                    "with the linked road units",
                )
            )
            continue
        lower_shape = get_candidate(lower)[1]
        upper_shape = get_candidate(upper)[1]
        if not lower_shape.walkable or not upper_shape.walkable:
            continue  # already reported above
        ok, reason = _step_transition(get_candidate, lower, upper)
        if ok:
            continue
        issues.append(
            _issue(
                "BOUNDARY_ANCHOR_BROKEN",
                lower,
                "walk_no_jump_v1/boundary",
                "the interface bridges link_from and link_to with steps <= 0.5 "
                "(or a verified transition above the lower unit)",
                f"step {_world_y(upper, upper_shape) - _world_y(lower, lower_shape)}"
                f" from {lower} ({_world_y(lower, lower_shape)}) to {upper} "
                f"({_world_y(upper, upper_shape)})",
                f"interface {interface_id}: {reason}",
            )
        )
    return _finish(issues)


def cut_fill_report(
    base_sampler: Sampler, candidate_sampler: Sampler, positions: Iterable
) -> dict:
    """Exact cut / fill / replace statistics for a set of voxels.

    ``positions`` is any iterable of ``(x, y, z)`` voxels; duplicates are
    removed, so every coordinate is counted once. States are compared through
    their canonical form (attribute order never matters) and sorted by
    ``(x, y, z)``.

    Definitions - ``cut`` and ``fill`` only ever count voxels that **really**
    become empty or **really** become a block:

    * ``cut``: the base state is air and the candidate state is not,
    * ``fill``: the base state is not air and the candidate state is air,
    * ``replace``: both sides are known and non-empty but differ - counted
      **only** here, never as a cut or a fill,
    * voxels where either side is ``None`` (unknown voxel) are ``replace`` too,
      because the change cannot be verified as a genuine cut or fill,
    * both sides empty (``air`` / ``cave_air``) and identical states are not
      counted at all.

    ``touched`` is the number of deduplicated voxels with a net change, i.e.
    ``cut + fill + replace``.
    """
    voxels = sorted({_as_voxel(pos) for pos in positions})
    cut_voxels: List[Voxel] = []
    fill_voxels: List[Voxel] = []
    replace_voxels: List[Voxel] = []
    for voxel in voxels:
        base_state = base_sampler(voxel)
        candidate_state = candidate_sampler(voxel)
        if _state_key(base_state) == _state_key(candidate_state):
            continue
        base_air = classify_state(base_state).kind == KIND_AIR
        candidate_air = classify_state(candidate_state).kind == KIND_AIR
        if base_state is None or candidate_state is None:
            replace_voxels.append(voxel)
        elif base_air and candidate_air:
            continue
        elif base_air:
            fill_voxels.append(voxel)
        elif candidate_air:
            cut_voxels.append(voxel)
        else:
            replace_voxels.append(voxel)
    return {
        "cut": len(cut_voxels),
        "fill": len(fill_voxels),
        "replace": len(replace_voxels),
        "cut_voxels": cut_voxels,
        "fill_voxels": fill_voxels,
        "replace_voxels": replace_voxels,
        "touched": len(cut_voxels) + len(fill_voxels) + len(replace_voxels),
    }
