"""decorate_path / scatter_assets: constrained greenery and road decor (spec 8).

Uses the same asset placer. Roads and main structures are placed first, then
decorations, with road/entry avoidance, ground adaptation, support and count
checks. Trees are static assets, not growth-dependent saplings.
"""
from __future__ import annotations

# TODO(stage B): constrained scatter with avoidance and support checks.
