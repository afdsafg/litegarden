"""connect_path: terrain-following pathfinding and paving (spec 8).

The Agent picks connection points and road style; the program solves the path
on the height/obstacle grid. First version allows at most 1 block height
difference between adjacent road cells, no water, no unknown areas, no
auto-bridges. Unsatisfiable constraints return no-solution; never carve hills.
"""
from __future__ import annotations

# TODO(stage B): A* over the ground-height grid with cost = length + slope +
# turns + cut/fill, checking the full road width (not just the centre line).
