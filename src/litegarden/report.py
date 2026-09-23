"""Reporting: materials, changes, warnings, usage instructions."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from .scene import AIR, PatchSet


def build_report(patch: PatchSet) -> dict:
    placed = Counter()
    removed = Counter()
    for c in patch:
        if c.after == AIR:
            removed[c.before] += 1
        else:
            placed[c.after] += 1
    return {
        "changes": len(patch),
        "placed": dict(placed),
        "removed": dict(removed),
    }


def write_report(patch: PatchSet, out_json: Path, out_md: Path) -> None:
    rep = build_report(patch)
    out_json.write_text(json.dumps(rep, indent=2), encoding="utf-8")
    lines = ["# litegarden report", "", f"total changes: {rep['changes']}", "", "## placed"]
    lines += [f"- {b}: {n}" for b, n in sorted(rep["placed"].items())]
    lines += ["", "## removed"]
    lines += [f"- {b}: {n}" for b, n in sorted(rep["removed"].items())]
    out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
