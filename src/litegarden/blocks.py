"""Canonical block classification tables shared by analysis and constraints.

These sets are the single source of truth for:
- which blocks count as verified natural ground (terrain analysis),
- which blocks are water / construction obstacles,
- which blocks are non-colliding decorative *cover* that may be cleared when
  building a road across it (spec 8.2 "clearance request": only known blocks
  whose whole clearance volume is writable may be removed).

Extend only after in-game validation; an unknown block is never guessed.
"""
from __future__ import annotations

# Verified natural ground blocks.
GROUND_BLOCKS = frozenset({
    "minecraft:grass_block", "minecraft:dirt", "minecraft:stone",
    "minecraft:sand", "minecraft:gravel", "minecraft:podzol",
    "minecraft:mycelium", "minecraft:coarse_dirt", "minecraft:rooted_dirt",
    "minecraft:clay", "minecraft:moss_block",
})

# Solid but not ground (rock variants / ores). Treated as solid terrain for
# support purposes but never auto-selected as a buildable ground surface.
SOLID_ROCK_BLOCKS = frozenset({
    "minecraft:andesite", "minecraft:diorite", "minecraft:granite",
    "minecraft:sandstone", "minecraft:coal_ore", "minecraft:copper_ore",
    "minecraft:iron_ore", "minecraft:gold_ore", "minecraft:redstone_ore",
    "minecraft:lapis_ore", "minecraft:diamond_ore", "minecraft:emerald_ore",
    "minecraft:deepslate", "minecraft:cobblestone", "minecraft:stone_bricks",
})

WATER_BLOCKS = frozenset({"minecraft:water"})

# Blocks that obstruct construction routing but are not ground.
#
# NOTE: tree leaves are deliberately *not* listed here, to keep routing
# behaviour identical to the previously verified version. Leaves are instead
# protected at the write gate (they are not removable), so a road is never
# carved through a canopy; a canopy that is too low is caught by the road
# headroom check.
OBSTACLE_BLOCKS = frozenset({
    "minecraft:oak_log", "minecraft:birch_log", "minecraft:spruce_log",
    "minecraft:jungle_log", "minecraft:acacia_log", "minecraft:dark_oak_log",
    "minecraft:mangrove_log", "minecraft:cherry_log", "minecraft:pale_oak_log",
})

# Non-colliding decorative cover: a player walks straight through it, so it
# does not block a road, but it lies in the walk volume and should be cleared
# when the volume is known and fully writable.
COVER_BLOCKS = frozenset({
    "minecraft:leaf_litter", "minecraft:short_grass", "minecraft:tall_grass",
    "minecraft:fern", "minecraft:large_fern", "minecraft:dead_bush",
    "minecraft:bush", "minecraft:firefly_bush", "minecraft:sugar_cane",
    "minecraft:kelp", "minecraft:kelp_plant", "minecraft:seagrass",
    "minecraft:torch", "minecraft:redstone_torch", "minecraft:soul_torch",
    "minecraft:dandelion", "minecraft:poppy", "minecraft:blue_orchid",
    "minecraft:azure_bluet", "minecraft:oxeye_daisy", "minecraft:cornflower",
    "minecraft:lily_of_the_valley", "minecraft:pink_petals",
    "minecraft:wildflowers", "minecraft:cactus_flower", "minecraft:snow",
})

# Air-like blocks (never written as an explicit removal target by accident).
AIR_BLOCKS = frozenset({"minecraft:air", "minecraft:cave_air", "minecraft:void_air"})


def is_air(block_id: str) -> bool:
    return block_id in AIR_BLOCKS


def is_ground(block_id: str) -> bool:
    return block_id in GROUND_BLOCKS


def is_cover(block_id: str) -> bool:
    return block_id in COVER_BLOCKS
