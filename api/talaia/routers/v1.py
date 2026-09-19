"""TALAIA v1 API."""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..config import settings
from ..connectors import registry
from ..models import (ExposureReport, ExposureRequest, GeocodeRequest, SourceMeta,
                      SourceStatus)
from ..services.aggregator import build_report
from ..store import get_store
from ..taxonomy import as_dict as taxonomy_dict

log = logging.getLogger("talaia.api")
router = APIRouter(prefix="/v1", tags=["exposure"])


@router.post("/exposure", response_model=ExposureReport,
             summary="Full values-at-risk report for a polygon")
async def exposure(req: ExposureRequest) -> ExposureReport:
    """Return everything of value inside the AOI.

    Accepts a GeoJSON Geometry, Feature or FeatureCollection. A FeatureCollection whose
    features carry a `band` property is treated as time-banded fire perimeters, and every
    asset is assigned to the earliest band that contains it.
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


@router.get("/sources", response_model=list[SourceStatus],
            summary="Live catalogue of data sources", tags=["metadata"])
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


@router.get("/taxonomy", summary="Category and subcategory definitions", tags=["metadata"])
async def taxonomy() -> dict[str, Any]:
    """The closed vocabulary, with the vulnerability, criticality and valuation
    parameters attached to every subcategory."""
    return taxonomy_dict()


@router.get("/stats", summary="Store contents", tags=["metadata"])
async def stats() -> dict[str, Any]:
    store = get_store()
    data = await store.stats()
    data["core_impl"] = __import__("talaia.core_shim", fromlist=["impl"]).impl()
    return data


@router.post("/geocode", summary="Geocode a Spanish address", tags=["utilities"])
async def geocode_endpoint(req: GeocodeRequest) -> dict[str, Any]:
    """CartoCiudad passthrough, cached. Useful for turning a reported address into an
    AOI centre before calling /v1/exposure."""
    from ..connectors.es.cartociudad import geocode
    result = await geocode(req.query, get_store())
    if not result:
        raise HTTPException(status_code=404, detail="No match for that address")
    return result
