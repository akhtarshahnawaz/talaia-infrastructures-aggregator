"""Pydantic contract for the TALAIA API.

These models ARE the public interface. Field names are stable; additive changes only.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

GeoJSON = dict[str, Any]


# ---------------------------------------------------------------------------
# Asset sub-objects
# ---------------------------------------------------------------------------
class Address(BaseModel):
    model_config = ConfigDict(extra="allow")
    street: str | None = None
    housenumber: str | None = None
    postcode: str | None = None
    municipality: str | None = None
    comarca: str | None = None
    province: str | None = None
    region: str | None = None
    country: str = "ES"
    full: str | None = None


class Contacts(BaseModel):
    phone: list[str] = Field(default_factory=list)
    email: list[str] = Field(default_factory=list)
    website: str | None = None
    operator: str | None = None
    emergency_contact: str | None = None


class Capacity(BaseModel):
    """Registered capacity. NEVER live occupancy - see ``basis`` and ``occupancy_note``."""
    people: float | None = Field(None, description="Best estimate of people present at capacity")
    beds: float | None = None
    students: float | None = None
    places: float | None = None
    animals: float | None = None
    livestock_units: float | None = Field(None, description="UB / LSU normalised livestock units")
    dwellings: float | None = None
    basis: str | None = Field(None, description="Which figure the estimate was derived from")
    confidence: float = 0.5


class Valuation(BaseModel):
    replacement_cost_eur: float = 0.0
    contents_eur: float = 0.0
    livestock_eur: float = 0.0
    total_eur: float = 0.0
    method: str = "none"
    confidence: float = 0.0
    assumptions: list[str] = Field(default_factory=list)
    currency: str = "EUR"


class Provenance(BaseModel):
    source_id: str
    source_ref: str | None = None
    retrieved_at: datetime | None = None
    fields: list[str] = Field(default_factory=list)
    url: str | None = None


class ExposureInfo(BaseModel):
    band: str | None = Field(None, description="Earliest arrival band containing this asset")
    band_index: int | None = None
    band_minutes: float | None = Field(None, description="Minutes until the band's front arrives")
    distance_to_front_m: float | None = None
    inside_aoi: bool = True
    priority_score: float = Field(0.0, description="0-100 composite triage score")


class Asset(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    category: str
    subcategory: str
    name: str | None = None
    geometry: GeoJSON | None = None
    geometry_kind: Literal["point", "footprint", "line", "area"] = "point"
    lon: float | None = None
    lat: float | None = None

    address: Address | None = None
    contacts: Contacts = Field(default_factory=Contacts)
    capacity: Capacity = Field(default_factory=Capacity)
    valuation: Valuation = Field(default_factory=Valuation)

    vulnerability: int = 50
    criticality: int = 50
    hazardous: bool = False
    response_asset: bool = Field(False, description="Helps fight the fire, not only at risk")
    human_bearing: bool = False

    exposure: ExposureInfo = Field(default_factory=ExposureInfo)
    occupancy_note: str | None = None
    provenance: list[Provenance] = Field(default_factory=list)
    confidence: float = 0.5
    possible_duplicate_of: list[str] = Field(default_factory=list)
    merged_count: int = 1
    attributes: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------
class SourceMeta(BaseModel):
    id: str
    name: str
    publisher: str
    tier: Literal["resident", "on_demand", "enrichment"]
    coverage: str
    country: str
    licence: str
    licence_url: str | None = None
    url: str | None = None
    categories: list[str] = Field(default_factory=list)
    update_cadence: str = "unknown"
    provides: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    commercial_use: bool = True


class SourceStatus(BaseModel):
    source: SourceMeta
    rows: int = 0
    last_run_at: datetime | None = None
    last_status: str = "never_run"
    last_error: str | None = None


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------
class ExposureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    aoi: GeoJSON = Field(
        ...,
        description=(
            "GeoJSON Geometry, Feature or FeatureCollection. A FeatureCollection whose "
            "features carry a band property is treated as time-banded fire perimeters."
        ),
    )
    layers: list[str] | None = Field(
        None, description="Category or subcategory keys to include. Omit or ['all'] for everything."
    )
    buffer_m: float = Field(0.0, ge=0, le=50_000, description="Outward buffer applied to the AOI")
    band_property: str = Field("band", description="Feature property holding the band label")
    minutes_property: str = Field(
        "minutes", description="Feature property holding minutes-to-arrival"
    )

    include_assets: bool = True
    include_networks: bool = True
    include_population: bool = True
    include_population_grid: bool = Field(
        False,
        description=("Return the individual census grid cells rather than only the AOI "
                     "total, so population can be mapped as a surface instead of a "
                     "single number. Cell polygons are included when include_geometry "
                     "is also set."))
    include_geometry: bool = Field(True, description="Return per-asset geometry")

    live_osm: bool | None = Field(None, description="Override the live OSM fetch flag")
    enrich: bool | None = Field(
        None,
        description=("Reserved. Tier C enrichment - geocoding an address-only registry "
                     "record - runs at ingest, not per query, so this flag changes "
                     "nothing today and timing.enrichment_ms is always 0. Kept in the "
                     "contract so the field does not have to be reintroduced later."))
    conflate: bool = True
    max_assets: int | None = Field(None, ge=1, le=50_000)
    sort_by: Literal["priority", "distance", "value", "category"] = "priority"


class SignupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(..., min_length=5, max_length=254)
    organisation: str | None = Field(None, max_length=120)
    use_case: str | None = Field(None, max_length=500)


class VerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(..., min_length=8, max_length=256,
                       description="The single-use token from the confirmation email")


class GeocodeRequest(BaseModel):
    query: str
    limit: int = Field(1, ge=1, le=10)


# ---------------------------------------------------------------------------
# Response
# ---------------------------------------------------------------------------
class BandSummary(BaseModel):
    band: str
    band_index: int
    minutes: float | None = None
    area_km2: float = 0.0
    asset_count: int = 0
    people_estimate: float = 0.0
    population_resident: float = 0.0
    total_value_eur: float = 0.0
    by_category: dict[str, int] = Field(default_factory=dict)
    value_by_category: dict[str, float] = Field(default_factory=dict)
    critical_assets: int = 0
    hazardous_assets: int = 0


class CategorySummary(BaseModel):
    category: str
    label: str
    count: int = 0
    people_estimate: float = 0.0
    total_value_eur: float = 0.0
    human_bearing: bool = False


class PopulationCell(BaseModel):
    """One census grid cell clipped to the AOI.

    ``population`` is the cell's full published count; ``population_in_aoi`` is that
    count scaled by how much of the cell the AOI actually covers. Density is computed
    from the whole cell, because a cell only half inside the AOI is not half as dense -
    it is the same neighbourhood, half observed.
    """
    cell_id: str
    lon: float = Field(..., description="Cell centroid longitude")
    lat: float = Field(..., description="Cell centroid latitude")
    population: float = Field(..., description="Residents in the whole cell")
    population_in_aoi: float = Field(..., description="Area-weighted share inside the AOI")
    overlap_fraction: float = Field(..., ge=0, le=1)
    density_per_km2: float = 0.0
    area_km2: float = 1.0
    band: str | None = Field(
        None,
        description=(
            "Earliest band whose perimeter covers this cell's CENTROID - a label for "
            "colouring a map, not an apportionment. A cell straddling a band edge gets "
            "one label, and a cell overlapping the AOI whose centroid sits outside every "
            "band gets none. Summing cells by this field will therefore not reproduce "
            "'by_band', which is area-weighted and exclusive. Quote 'by_band' for "
            "per-band population; use this for placement."))
    geometry: GeoJSON | None = Field(None, description="Cell polygon; only when include_geometry")


class PopulationResult(BaseModel):
    total: float = 0.0
    method: str = "ine_grid_area_weighted"
    cell_count: int = 0
    confidence: float = 0.6
    note: str = (
        "Census residents, area-weighted from 1 km2 grid cells. Not real-time occupancy: "
        "excludes tourists, daytime workers and anyone already evacuated. Per-band figures "
        "are exclusive - each band counts only the ground it adds over earlier bands."
    )
    by_band: dict[str, float] = Field(default_factory=dict)
    cells: list[PopulationCell] = Field(
        default_factory=list,
        description="Per-cell breakdown. Empty unless include_population_grid is set.")
    cells_truncated: bool = False
    peak_density_per_km2: float = Field(
        0.0, description="Densest cell touching the AOI - where evacuation load concentrates")


class NetworkClass(BaseModel):
    subcategory: str
    label: str
    length_km: float = 0.0
    feature_count: int = 0


class NetworkResult(BaseModel):
    by_class: list[NetworkClass] = Field(default_factory=list)
    total_length_km: float = 0.0
    access_routes: list[dict[str, Any]] = Field(
        default_factory=list, description="Roads crossing the AOI boundary - candidate access/egress"
    )
    geojson: GeoJSON | None = None


class ReportSummary(BaseModel):
    asset_count: int = 0
    people_estimate: float = Field(
        0.0, description="Sum of facility capacity estimates. Distinct from resident population."
    )
    people_from_registry: float = Field(
        0.0, description="Portion of people_estimate backed by a registry capacity figure"
    )
    people_from_defaults: float = Field(
        0.0, description="Portion inferred from class defaults - treat as a rough guess"
    )
    population_resident: float = 0.0
    total_value_eur: float = 0.0
    aoi_area_km2: float = 0.0
    critical_assets: int = 0
    hazardous_assets: int = 0
    response_assets: int = 0
    livestock_units: float = 0.0
    by_category: list[CategorySummary] = Field(default_factory=list)
    top_priority: list[dict[str, Any]] = Field(default_factory=list)
    coverage_regime: str = "unknown"


class Timing(BaseModel):
    total_ms: float = 0.0
    osm_fetch_ms: float = 0.0
    store_query_ms: float = 0.0
    conflation_ms: float = 0.0
    enrichment_ms: float = Field(
        0.0, description=("Always 0: enrichment happens at ingest, not per query. See "
                          "the `enrich` request field."))
    scoring_ms: float = 0.0
    tiles_total: int = 0
    tiles_fetched: int = 0
    tiles_cached: int = Field(
        0, description="Tiles held locally and still fresh. Excludes failed tiles.")
    tiles_failed: int = Field(
        0, description=("Tiles whose last fetch failed and are inside their retry "
                        "backoff. Data for these is MISSING, not absent - a warning "
                        "accompanies any non-zero value."))
    core_impl: str = "python"


class ExposureReport(BaseModel):
    request_id: str
    generated_at: datetime
    aoi_bbox: list[float] = Field(default_factory=list)
    bands: list[BandSummary] = Field(default_factory=list)
    summary: ReportSummary = Field(default_factory=ReportSummary)
    population: PopulationResult = Field(default_factory=PopulationResult)
    networks: NetworkResult | None = None
    assets: list[Asset] = Field(default_factory=list)
    assets_truncated: bool = False
    sources: list[SourceMeta] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    timing: Timing = Field(default_factory=Timing)


class ErrorEnvelope(BaseModel):
    error: str
    detail: str | None = None
    request_id: str | None = None
