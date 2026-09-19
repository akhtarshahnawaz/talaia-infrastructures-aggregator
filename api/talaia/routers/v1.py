"""TALAIA v1 API.

Endpoints fall into three access classes:

* **Data** - ``/exposure``, ``/exposure/summary``, ``/assets``, ``/geocode``. Always
  require an API key when authentication is enabled.
* **Metadata** - ``/sources``, ``/taxonomy``, ``/stats``. Describe the service rather
  than returning exposure data, and the public documentation site renders them, so they
  are open unless ``TALAIA_PUBLIC_METADATA=false``.
* **Admin** - ``/admin/keys``. Guarded by a separate admin key and disabled entirely
  unless one is configured.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..auth import ApiKey, registry as key_registry, require_admin_key, require_api_key
from ..config import settings
from ..connectors import registry
from ..models import (ExposureReport, ExposureRequest, GeocodeRequest, SourceStatus)
from ..services.aggregator import build_report
from ..store import get_store
from ..taxonomy import as_dict as taxonomy_dict

log = logging.getLogger("talaia.api")


async def _optional_key(request: Request,
                        key: ApiKey | None = Depends(require_api_key)) -> ApiKey | None:
    return key


def _metadata_guard():
    """Open metadata endpoints unless the operator has closed them."""
    return [] if settings.public_metadata else [Depends(require_api_key)]


router = APIRouter(prefix="/v1", tags=["exposure"],
                   dependencies=[Depends(require_api_key)])
meta_router = APIRouter(prefix="/v1", tags=["metadata"],
                        dependencies=_metadata_guard())
admin_router = APIRouter(prefix="/v1/admin", tags=["admin"],
                         dependencies=[Depends(require_admin_key)])


# ---------------------------------------------------------------------------
# Data endpoints - always authenticated
# ---------------------------------------------------------------------------
@router.post("/exposure", response_model=ExposureReport,
             summary="Full values-at-risk report for a polygon")
async def exposure(req: ExposureRequest) -> ExposureReport:
    """Return everything of value inside the AOI.

    Accepts a GeoJSON Geometry, Feature or FeatureCollection. A FeatureCollection whose
    features carry a `band` property is treated as time-banded fire perimeters, and every
    asset is assigned to the earliest band that contains it.

    **Requires an API key** (`X-API-Key` or `Authorization: Bearer`).
    """
    try:
        return await build_report(req, get_store())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/exposure/summary", summary="Aggregates only - no asset array")
async def exposure_summary(req: ExposureRequest) -> dict[str, Any]:
    """The decision-relevant numbers without the long asset list.

    Intended for an agent loop that polls exposure as a fire evolves and only needs the
    asset detail once something crosses a threshold.
    """
    req = req.model_copy(update={"include_assets": False, "include_geometry": False})
    try:
        report = await build_report(req, get_store())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "request_id": report.request_id, "generated_at": report.generated_at,
        "aoi_bbox": report.aoi_bbox, "summary": report.summary,
        "bands": report.bands, "population": report.population,
        "networks": {"by_class": report.networks.by_class,
                     "total_length_km": report.networks.total_length_km}
        if report.networks else None,
        "warnings": report.warnings, "timing": report.timing,
    }


@router.post("/assets", summary="Assets as newline-delimited JSON (streaming)")
async def assets_stream(req: ExposureRequest) -> StreamingResponse:
    """Stream assets as NDJSON so a large AOI can be consumed incrementally."""
    try:
        report = await build_report(req, get_store())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    def generate():
        header = {"_type": "header", "request_id": report.request_id,
                  "count": len(report.assets), "summary": report.summary.model_dump(),
                  "warnings": report.warnings}
        yield json.dumps(header, default=str) + "\n"
        for asset in report.assets:
            yield json.dumps(asset.model_dump(), default=str) + "\n"

    return StreamingResponse(generate(), media_type="application/x-ndjson")


@router.post("/geocode", summary="Geocode a Spanish address", tags=["utilities"])
async def geocode_endpoint(req: GeocodeRequest) -> dict[str, Any]:
    """CartoCiudad passthrough, cached. Useful for turning a reported address into an
    AOI centre before calling /v1/exposure."""
    from ..connectors.es.cartociudad import geocode
    result = await geocode(req.query, get_store())
    if not result:
        raise HTTPException(status_code=404, detail="No match for that address")
    return result


# ---------------------------------------------------------------------------
# Metadata endpoints
# ---------------------------------------------------------------------------
@meta_router.get("/sources", response_model=list[SourceStatus],
                 summary="Live catalogue of data sources")
async def sources() -> list[SourceStatus]:
    """Every registered connector with its licence, coverage and load status.

    The documentation website renders this endpoint directly, so the published source
    list cannot drift from what the service actually runs.
    """
    stats = await get_store().source_stats()
    out = []
    for cls in registry.all_connectors():
        s = stats.get(cls.meta.id, {})
        out.append(SourceStatus(
            source=cls.meta, rows=s.get("rows", 0),
            last_run_at=s.get("last_run_at"),
            last_status=s.get("last_status", "never_run"),
            last_error=s.get("last_error")))
    return out


@meta_router.get("/taxonomy", summary="Category and subcategory definitions")
async def taxonomy() -> dict[str, Any]:
    """The closed vocabulary, with the vulnerability, criticality and valuation
    parameters attached to every subcategory."""
    return taxonomy_dict()


@meta_router.get("/stats", summary="Store contents")
async def stats() -> dict[str, Any]:
    store = get_store()
    data = await store.stats()
    data["core_impl"] = __import__("talaia.core_shim", fromlist=["impl"]).impl()
    data["auth_required"] = settings.require_auth
    return data


# ---------------------------------------------------------------------------
# Admin - key management
# ---------------------------------------------------------------------------
@admin_router.post("/keys", summary="Mint a new API key")
async def create_key(payload: dict[str, Any]) -> dict[str, Any]:
    """Create a key. **The secret is returned once and cannot be recovered afterwards**,
    because only its hash is stored."""
    label = str(payload.get("label") or "unnamed")[:64]
    limit = payload.get("rate_limit_per_min")
    raw, record = await key_registry.create(
        get_store(), label=label,
        rate_limit_per_min=int(limit) if limit else None)
    return {"api_key": raw, "prefix": record.prefix, "label": record.label,
            "rate_limit_per_min": record.rate_limit_per_min,
            "warning": "Store this now. It is not recoverable."}


@admin_router.get("/keys", summary="List keys (prefixes only, never secrets)")
async def list_keys() -> list[dict[str, Any]]:
    return await key_registry.list_keys(get_store())


@admin_router.delete("/keys/{prefix}", summary="Revoke a key by prefix")
async def revoke_key(prefix: str) -> dict[str, Any]:
    ok = await key_registry.revoke(get_store(), prefix)
    if not ok:
        raise HTTPException(status_code=404, detail="No active key with that prefix.")
    return {"revoked": prefix}
