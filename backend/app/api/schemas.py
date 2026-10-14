"""Pydantic response contracts for the LithoCreep API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

HazardLevel = Literal["NONE", "WATCH_TIER1", "ALERT_TIER2", "CRITICAL_TIER3"]


class Facility(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    country: str
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    bbox_xmin: float = Field(ge=-180, le=180)
    bbox_ymin: float = Field(ge=-90, le=90)
    bbox_xmax: float = Field(ge=-180, le=180)
    bbox_ymax: float = Field(ge=-90, le=90)
    mine_type: str
    embankment_type: str
    crest_line_geom: Optional[dict[str, Any]] = Field(None, description="GeoJSON LineString; null until a crest line is sourced")
    status: str

    @model_validator(mode="after")
    def _bbox_ordered(self) -> "Facility":
        if not (self.bbox_xmin < self.bbox_xmax and self.bbox_ymin < self.bbox_ymax):
            raise ValueError("bbox must satisfy xmin < xmax and ymin < ymax")
        if not (self.bbox_xmin <= self.longitude <= self.bbox_xmax and self.bbox_ymin <= self.latitude <= self.bbox_ymax):
            raise ValueError("facility centre must lie inside its bbox")
        return self

    @field_validator("crest_line_geom")
    @classmethod
    def _line(cls, value: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
        if value is not None and value.get("type") not in {"LineString", "MultiLineString"}:
            raise ValueError("crest_line_geom must be a GeoJSON (Multi)LineString")
        return value


class Alert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    facility_id: str
    timestamp: datetime
    hazard_level: HazardLevel
    failure_date_est: Optional[datetime] = None
    failure_date_error_days: Optional[float] = Field(None, ge=0)
    diagnostic_evidence: dict[str, Any]


class DataSource(BaseModel):
    product: str
    file: str
    variable: str
    synthetic_or_mock: bool
    label: str


class LayerInfo(BaseModel):
    layer: str
    units: str
    categorical: bool
    time_dependent: bool
    available: bool
    source: Optional[DataSource] = None
    times: list[datetime] = []
    detail: Optional[str] = None


class Matrix(BaseModel):
    """A layer on a regular EPSG:4326 grid whose outer bounds equal the facility bbox."""

    facility_id: str
    layer: str
    units: str
    categorical: bool
    crs: Literal["EPSG:4326"] = "EPSG:4326"
    bounds: list[float] = Field(min_length=4, max_length=4, description="[xmin, ymin, xmax, ymax] = facility bbox")
    shape: list[int] = Field(min_length=2, max_length=2, description="[rows, cols]; row 0 is the northern edge")
    resolution_deg: list[float] = Field(min_length=2, max_length=2, description="[dx, dy] in degrees (~30 m)")
    time: Optional[datetime] = None
    requested_time: Optional[datetime] = None
    available_times: list[datetime] = []
    values: list[list[Optional[float]]]
    valid_fraction: float = Field(ge=0, le=1)
    flag_meanings: Optional[str] = None
    source: DataSource
    note: Optional[str] = None

    @model_validator(mode="after")
    def _consistent_shape(self) -> "Matrix":
        rows, cols = self.shape
        if len(self.values) != rows or any(len(r) != cols for r in self.values):
            raise ValueError("values do not match shape")
        return self


class PointMatch(BaseModel):
    method: Literal["exact", "bilinear", "nearest"]
    nearest_cell_distance_m: Optional[float] = Field(None, description="null when the source has no coordinates")
    note: Optional[str] = None


class KinematicSeries(BaseModel):
    source: DataSource
    match: PointMatch
    point_id: Optional[str] = None
    times: list[datetime]
    displacement_mm: list[Optional[float]]
    displacement_smooth_mm: Optional[list[Optional[float]]] = None
    displacement_smooth_lower_mm: Optional[list[Optional[float]]] = None
    displacement_smooth_upper_mm: Optional[list[Optional[float]]] = None
    velocity_mm_yr: Optional[list[Optional[float]]] = None
    velocity_lower_mm_yr: Optional[list[Optional[float]]] = None
    velocity_upper_mm_yr: Optional[list[Optional[float]]] = None
    acceleration_mm_yr2: Optional[list[Optional[float]]] = None
    acceleration_lower_mm_yr2: Optional[list[Optional[float]]] = None
    acceleration_upper_mm_yr2: Optional[list[Optional[float]]] = None
    inverse_velocity_yr_mm: Optional[list[Optional[float]]] = None
    temporal_coherence: Optional[float] = None
    status: Optional[str] = None
    instability_time: Optional[datetime] = None
    instability_time_lower: Optional[datetime] = None
    instability_time_upper: Optional[datetime] = None
    disclaimer: Optional[str] = None

    @model_validator(mode="after")
    def _lengths(self) -> "KinematicSeries":
        n = len(self.times)
        for name, value in self:
            if name != "times" and isinstance(value, list) and len(value) != n:
                raise ValueError(f"{name} has {len(value)} values for {n} epochs")
        return self


class SpectralPoint(BaseModel):
    source: DataSource
    match: PointMatch
    sai: Optional[float] = None
    rmse: Optional[float] = None
    ndvi: Optional[float] = None
    ndwi_swir: Optional[float] = None
    confidence: Optional[float] = None
    observability: Optional[str] = None
    abundance: dict[str, Optional[float]] = {}


class TimeValue(BaseModel):
    times: list[datetime]
    values: list[Optional[float]]


class HydrologyPoint(BaseModel):
    source: DataSource
    match: PointMatch
    elevation_m: Optional[float] = None
    slope_deg: Optional[float] = None
    aspect_deg: Optional[float] = None
    flow_direction_deg: Optional[float] = None
    flow_accumulation_cells: Optional[float] = None
    stream: Optional[bool] = None
    water_class: Optional[TimeValue] = None
    d_crest_m: Optional[TimeValue] = Field(None, description="facility-level setback series (not per pixel)")


class ClassificationPoint(BaseModel):
    source: DataSource
    match: PointMatch
    regime: int
    regime_meaning: str
    alert_tier: int
    flags: list[str]
    low_confidence: bool
    sai_interval: list[Optional[float]]
    velocity_interval_mm_yr: list[Optional[float]]
    failure_time_estimate: Optional[datetime] = None
    failure_time_lower: Optional[datetime] = None
    failure_time_upper: Optional[datetime] = None


class PixelResponse(BaseModel):
    facility_id: str
    lat: float
    lon: float
    utm_x: float
    utm_y: float
    utm_crs: str
    kinematics: Optional[KinematicSeries] = None
    spectral: Optional[SpectralPoint] = None
    hydrology: Optional[HydrologyPoint] = None
    classification: Optional[ClassificationPoint] = None
    unavailable: dict[str, str] = Field(default_factory=dict, description="product -> reason it is absent")


class Health(BaseModel):
    status: Literal["ok"]
    database: str
    facilities: int
    alerts: int


class AuditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: Optional[str] = Field(None, max_length=2000, description="Free-text request; defaults to a GISTM brief request")
    mode: Literal["auto", "llm", "deterministic"] = Field(
        "auto", description="llm: Claude with tools; deterministic: template; auto: llm when credentials exist"
    )


class AuditToolCall(BaseModel):
    tool: str
    input: dict[str, Any]
    ok: bool
    error: Optional[str] = None


class AuditResponse(BaseModel):
    facility_id: str
    generated_by: str
    brief: dict[str, Any]
    validation: dict[str, Any]
    tool_calls: list[AuditToolCall]
    turns: int
    usage: dict[str, int]

