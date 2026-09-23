"""Plan schema and operation whitelist (spec 7).

The Agent outputs plan.json; this module validates it strictly. Unknown
operations, extra fields, non-integer grid coordinates, out-of-range
parameters, dangerous code or arbitrary path inputs are all rejected.
"""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "0.1"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlaceAsset(_Strict):
    id: str
    op: Literal["place_asset"]
    asset_id: str
    site_id: str
    variant: Optional[str] = None


class ConnectPath(_Strict):
    id: str
    op: Literal["connect_path"]
    from_anchor: str = Field(alias="from")
    to: str
    width: int = Field(ge=1, le=5)
    palette_id: str


class DecoratePath(_Strict):
    id: str
    op: Literal["decorate_path"]
    path_id: str
    asset_id: str
    spacing: int = Field(ge=2, le=64)


class ScatterAssets(_Strict):
    id: str
    op: Literal["scatter_assets"]
    zone_id: str
    asset_id: str
    count: int = Field(ge=1, le=1000)


Operation = PlaceAsset | ConnectPath | DecoratePath | ScatterAssets


class Plan(_Strict):
    schema_version: str
    scene_id: str
    seed: int = 0
    style_id: str = "default"
    operations: List[Operation]


def parse_plan(text: str) -> Plan:
    """Parse and validate plan.json text; raises pydantic.ValidationError."""
    return Plan.model_validate_json(text)
