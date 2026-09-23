"""Assemble the Agent input pack (spec 6/7): analysis.json, analysis images,
asset/material whitelist and the operations doc. The Agent reads this pack and
writes plan.json; it never receives the raw voxel data.
"""
from __future__ import annotations

import json
from pathlib import Path

from .io import LoadedScene
from .render import render_all
from .terrain import TerrainAnalysis

OPERATIONS_DOC = """# litegarden plan.json operations

The Agent writes plan.json using ONLY the references present in analysis.json
(site_candidates, anchors, zones) and the asset/palette whitelist below.

## Operations
- place_asset {id, op, asset_id, site_id, variant?}
    Place a verified asset at a candidate site. variant must be one of the
    asset's legal orientation variants.
- connect_path {id, op, from, to, width, palette_id}
    Build a terrain-following path between an anchor (e.g. "entry_02") and an
    asset entry (e.g. "pavilion_1.entry"). Adjacent road cells differ by at
    most 1 block in height; the path never crosses water or unknown ground and
    never carves hills. width is 1..5.
- decorate_path {id, op, path_id, asset_id, spacing}
    Place a small asset (e.g. a lamp) alongside a solved path every `spacing`
    cells. path_id must reference an earlier connect_path op id.
- scatter_assets {id, op, zone_id, asset_id, count}
    Scatter up to `count` assets inside a planting zone with a fixed seed.

## Hard rules
- Every asset_id / site_id / anchor / zone_id / palette_id must exist in the
  whitelist or analysis.json. Unknown references are a compile error.
- No extra fields, no code, no arbitrary file paths, integer grid coords only.
- The Agent cannot widen protected zones or change the block budget.
- Compilation is deterministic for a fixed plan + input + assets + seed.
"""


def build_agent_pack(
    scene: LoadedScene,
    analysis: TerrainAnalysis,
    assets_dir: Path,
    out_dir: Path,
    config: dict | None = None,
) -> dict:
    """Write analysis.json, analysis images and copy the whitelist.

    Returns the analysis.json payload (also written to disk).
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    assets_dir = Path(assets_dir)

    images = render_all(analysis, out_dir)

    info = scene.snapshot.transform.region
    payload = {
        "scene_id": scene.snapshot.region_id,
        "data_version": scene.data_version,
        "coordinate_convention": {
            "p_local": "0-based local grid; Agent only uses this",
            "local_size": list(scene.snapshot.transform.local_size),
            "note": "y is up; (x,z) is the horizontal plane; origin at enclosing_min",
        },
        "region": {
            "region_id": info.region_id,
            "position": list(info.position),
            "size": list(info.size),
        },
        "grids": {
            "surface_height": analysis.surface_height.tolist(),
            "ground_height": analysis.ground_height.tolist(),
            "water_mask": analysis.water_mask.astype(int).tolist(),
            "obstacle_mask": analysis.obstacle_mask.astype(int).tolist(),
            "slope_map": [[(None if not (v == v or v != float("inf")) else float(v)) for v in row] for row in analysis.slope_map],
            "headroom": analysis.headroom.tolist(),
        },
        "site_candidates": analysis.site_candidates,
        "anchors": {k: list(v) for k, v in analysis.anchors.items()},
        "zones": analysis.zones,
        "images": {k: str(v.name) for k, v in images.items()},
    }

    (out_dir / "analysis.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (out_dir / "OPERATIONS.md").write_text(OPERATIONS_DOC, encoding="utf-8")

    # copy the whitelist so the Agent sees exactly what it may reference
    for name in ("catalog.json", "palettes.json", "block_rules.json"):
        src = assets_dir / name
        if src.exists():
            (out_dir / name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

    if config is not None:
        (out_dir / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")

    return payload
