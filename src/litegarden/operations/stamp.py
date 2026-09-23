"""place_asset: verified small-asset placement (spec 8).

Each asset registers footprint, occupied voxels, required-empty voxels,
support points, entries, allowed foundation depth and legal orientation
variants. Never paste only the non-air voxels: required_empty and entry
clearance must be validated too.
"""
from __future__ import annotations

# TODO(stage B): asset registry + placement validation.
