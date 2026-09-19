"""Model Context Protocol server for TALAIA.

Mounted on the same FastAPI app at ``/mcp``, which is the whole point: the MCP endpoint
carries the same ``require_api_key`` dependency as ``/v1/exposure``, and every tool runs
``_enforce_key_limits`` before ``build_report``. Those are the same two calls the REST
handler makes, in the same order, in the same process. An agent therefore cannot reach
past a tier's area cap, rate limit or daily quota by coming in through MCP - not because
the limits were reimplemented here and kept in step, but because there is only one
implementation and this is it.

A second, in-process copy of the limit logic would be the obvious way to build this and
the wrong one: it would drift the first time a quota changed, and the failure would be
silent and in the direction of letting people through.

Transport is stateless Streamable HTTP - one JSON-RPC request in, one JSON response out,
no session, no SSE stream. TALAIA's tools are request/response with no server-initiated
messages, so a session would add reconnection and resumability machinery to carry
nothing. ``mcp_stdio.py`` bridges this over stdio for clients that want to launch a
local process.

Tool results are deliberately not the raw report. A full exposure response runs to
megabytes of JSON, which is both useless in a model's context and expensive; each tool
returns a readable summary as text alongside the structured payload, and the asset list
is trimmed to what a triage decision actually needs.
"""
from __future__ import annotations

import logging
import math
from typing import Any, Callable, Awaitable

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from .auth import ApiKey, registry as key_registry, require_api_key
from .config import settings
from .models import ExposureRequest
from .store import get_store
from .taxonomy import as_dict as taxonomy_dict

log = logging.getLogger("talaia.mcp")

SERVER_NAME = "talaia"
SERVER_VERSION = "0.1.0"
# Newest first. An unknown version from the client is answered with our newest, which is
# what the specification asks for; the client then decides whether it can proceed.
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")

# JSON-RPC 2.0 error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


# ---------------------------------------------------------------------------
# AOI helpers
# ---------------------------------------------------------------------------
def _circle(lon: float, lat: float, radius_km: float, segments: int = 48) -> dict:
    """A GeoJSON polygon approximating a circle.

    Agents reason in "5 km around this point" far more naturally than in polygon rings,
    and asking a model to emit correct GeoJSON by hand is a reliable source of malformed
    geometry. Longitude is scaled by cos(latitude) so the result is round on the ground
    rather than round in degrees.
    """
    k = max(math.cos(math.radians(lat)), 1e-6)
    deg_lat = radius_km / 111.32
    deg_lon = deg_lat / k
    ring = [[lon + deg_lon * math.cos(2 * math.pi * i / segments),
             lat + deg_lat * math.sin(2 * math.pi * i / segments)]
            for i in range(segments)]
    ring.append(ring[0])
    return {"type": "Polygon", "coordinates": [ring]}


def _aoi_from(args: dict) -> dict:
    """Accept either a GeoJSON object or centre-plus-radius."""
    aoi = args.get("aoi")
    if isinstance(aoi, dict) and aoi.get("type"):
        return aoi
    lon, lat = args.get("lon"), args.get("lat")
    if lon is None or lat is None:
        raise ValueError(
            "Give either 'aoi' as a GeoJSON geometry/Feature/FeatureCollection, "
            "or 'lon' and 'lat' with 'radius_km'.")
    # `or 5.0` would quietly turn an explicit radius_km of 0 into a 5 km circle, which
    # is worse than an error: the caller asked for nothing and would get a real answer.
    raw = args.get("radius_km")
    radius = 5.0 if raw is None else float(raw)
    if radius <= 0 or radius > 200:
        raise ValueError("radius_km must be greater than 0 and at most 200.")
    return _circle(float(lon), float(lat), radius)


def _eur(value: float) -> str:
    for unit, scale in (("bn", 1e9), ("m", 1e6), ("k", 1e3)):
        if abs(value) >= scale:
            return f"EUR {value / scale:,.1f}{unit}"
    return f"EUR {value:,.0f}"


def _request_from(args: dict, *, include_assets: bool, max_assets: int | None = None
                  ) -> ExposureRequest:
    return ExposureRequest(
        aoi=_aoi_from(args),
        layers=args.get("layers"),
        buffer_m=float(args.get("buffer_m") or 0.0),
        include_assets=include_assets,
        include_networks=bool(args.get("include_networks", True)),
        include_population=True,
        include_geometry=bool(args.get("include_geometry", False)),
        live_osm=args.get("live_osm"),
        max_assets=max_assets,
        sort_by=args.get("sort_by") or "priority",
    )


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------
async def _tool_exposure_summary(args: dict, key: ApiKey | None) -> dict:
    from .routers.v1 import _enforce_key_limits
    from .services.aggregator import build_report

    req = _enforce_key_limits(_request_from(args, include_assets=False), key)
    report = await build_report(req, get_store())
    s = report.summary
    lines = [
        f"{s.asset_count:,} assets across {s.aoi_area_km2:,.1f} km2.",
        f"People at facilities (capacity, not live occupancy): {s.people_estimate:,.0f}"
        f" - {s.people_from_registry:,.0f} from registry figures,"
        f" {s.people_from_defaults:,.0f} inferred from class defaults.",
        f"Resident population (census, area-weighted): {s.population_resident:,.0f}"
        + (f", peaking at {report.population.peak_density_per_km2:,.0f} people/km2 "
           f"(talaia_population_grid shows where)."
           if report.population.peak_density_per_km2 else "."),
        f"Replacement value at risk: {_eur(s.total_value_eur)}.",
        f"Critical: {s.critical_assets:,}   Hazardous: {s.hazardous_assets:,}   "
        f"Response assets: {s.response_assets:,}   "
        f"Livestock units: {s.livestock_units:,.0f}.",
    ]
    if s.by_category:
        lines.append("\nBy category:")
        for c in sorted(s.by_category, key=lambda c: -c.total_value_eur)[:12]:
            lines.append(f"  {c.label:<24} {c.count:>6,}  {_eur(c.total_value_eur):>14}"
                         + (f"  {c.people_estimate:,.0f} people" if c.human_bearing else ""))
    if report.bands:
        lines.append("\nBy arrival band (population figures are exclusive per band):")
        for b in report.bands:
            when = f"t+{b.minutes:,.0f} min" if b.minutes is not None else b.band
            lines.append(f"  {b.band:<14} {when:<12} {b.asset_count:>6,} assets  "
                         f"{b.people_estimate:>8,.0f} people  {_eur(b.total_value_eur):>14}")
    if s.top_priority:
        lines.append("\nHighest triage scores:")
        for t in s.top_priority[:10]:
            lines.append(f"  {t.get('priority_score', 0):>5.1f}  "
                         f"{(t.get('name') or 'unnamed')[:44]:<44} "
                         f"{t.get('subcategory', '')}")
    if report.warnings:
        lines.append("\nWarnings:")
        lines.extend(f"  - {w}" for w in report.warnings)

    return {
        "text": "\n".join(lines),
        "structured": {
            "request_id": report.request_id,
            "aoi_bbox": report.aoi_bbox,
            "summary": report.summary.model_dump(),
            "population": report.population.model_dump(),
            "bands": [b.model_dump() for b in report.bands],
            "networks": {"by_class": [c.model_dump() for c in report.networks.by_class],
                         "total_length_km": report.networks.total_length_km}
            if report.networks else None,
            "warnings": report.warnings,
        },
    }


# An agent reading a tool result pays for every token, so the asset list is capped well
# below the API's own ceiling. Anything larger belongs in the REST API, whose response
# goes to a program rather than into a context window.
_MCP_ASSET_CAP = 200
_MCP_ASSET_DEFAULT = 50


async def _tool_list_assets(args: dict, key: ApiKey | None) -> dict:
    from .routers.v1 import _enforce_key_limits
    from .services.aggregator import build_report

    raw_limit = args.get("limit")
    want = _MCP_ASSET_DEFAULT if raw_limit is None else int(raw_limit)
    if want < 1:
        raise ValueError("limit must be at least 1.")
    want = min(want, _MCP_ASSET_CAP)
    req = _enforce_key_limits(
        _request_from(args, include_assets=True, max_assets=want), key)
    report = await build_report(req, get_store())

    rows = []
    for a in report.assets[:want]:
        contact = a.contacts.phone[0] if a.contacts.phone else (
            a.contacts.email[0] if a.contacts.email else None)
        rows.append({
            "id": a.id, "name": a.name, "category": a.category,
            "subcategory": a.subcategory, "lon": a.lon, "lat": a.lat,
            "priority_score": round(a.exposure.priority_score, 1),
            "band": a.exposure.band,
            "band_minutes": a.exposure.band_minutes,
            "people": a.capacity.people, "beds": a.capacity.beds,
            "animals": a.capacity.animals,
            "value_eur": round(a.valuation.total_eur),
            "hazardous": a.hazardous, "response_asset": a.response_asset,
            "vulnerability": a.vulnerability, "criticality": a.criticality,
            "contact": contact, "operator": a.contacts.operator,
            "address": a.address.full if a.address else None,
            "municipality": a.address.municipality if a.address else None,
            "sources": [p.source_id for p in a.provenance],
            "occupancy_note": a.occupancy_note,
        })

    header = (f"{len(rows)} of {report.summary.asset_count:,} assets, "
              f"sorted by {req.sort_by}.")
    if report.assets_truncated or report.summary.asset_count > len(rows):
        header += (" Truncated - narrow the area, pass 'layers' to filter by category, "
                   "or raise 'limit'.")
    lines = [header, ""]
    for r in rows:
        bits = [f"{r['priority_score']:>5.1f}", f"{(r['name'] or 'unnamed')[:40]:<40}",
                f"{r['subcategory']:<22}"]
        if r["people"]:
            bits.append(f"{r['people']:,.0f} people")
        if r["animals"]:
            bits.append(f"{r['animals']:,.0f} animals")
        if r["value_eur"]:
            bits.append(_eur(r["value_eur"]))
        if r["band"]:
            bits.append(f"band {r['band']}")
        if r["hazardous"]:
            bits.append("HAZARDOUS")
        if r["response_asset"]:
            bits.append("RESPONSE")
        if r["contact"]:
            bits.append(str(r["contact"]))
        lines.append("  " + "  ".join(bits))
    if report.warnings:
        lines.append("")
        lines.extend(f"warning: {w}" for w in report.warnings)

    return {"text": "\n".join(lines),
            "structured": {"request_id": report.request_id,
                           "count": len(rows),
                           "total_matched": report.summary.asset_count,
                           "truncated": report.summary.asset_count > len(rows),
                           "assets": rows}}


async def _tool_population_grid(args: dict, key: ApiKey | None) -> dict:
    from .routers.v1 import _enforce_key_limits
    from .services.aggregator import build_report

    raw_top = args.get("top")
    top = 20 if raw_top is None else int(raw_top)
    if top < 1:
        raise ValueError("top must be at least 1.")
    top = min(top, 100)

    req = _request_from(args, include_assets=False)
    req = req.model_copy(update={"include_population_grid": True,
                                 "include_networks": False,
                                 "include_geometry": False})
    req = _enforce_key_limits(req, key)
    report = await build_report(req, get_store())
    pop = report.population
    if not pop.cells:
        return {"text": (f"No census grid covers this area ({pop.method}). "
                         f"Resident population could not be mapped."),
                "structured": pop.model_dump(), "is_error": False}

    cells = pop.cells[:top]
    lines = [
        f"{pop.total:,.0f} residents across {pop.cell_count:,} census cells of "
        f"~1 km2. Peak density {pop.peak_density_per_km2:,.0f} people/km2.",
        "",
        f"Densest {len(cells)} cell(s) - this is where evacuation load concentrates:",
        f"  {'lon':>8} {'lat':>8} {'per km2':>9} {'in AOI':>9}  {'cover':>6}  band",
    ]
    for c in cells:
        lines.append(
            f"  {c.lon:>8.4f} {c.lat:>8.4f} {c.density_per_km2:>9,.0f} "
            f"{c.population_in_aoi:>9,.0f}  {c.overlap_fraction:>5.0%}  "
            f"{c.band or '-'}")
    if pop.by_band:
        lines.append("\nBy arrival band (exclusive - each band counts only the ground "
                     "it adds):")
        for band, value in pop.by_band.items():
            lines.append(f"  {band:<14} {value:>10,.0f} residents")
    lines.append("\n" + pop.note)
    return {"text": "\n".join(lines), "structured": pop.model_dump()}


async def _tool_geocode(args: dict, key: ApiKey | None) -> dict:
    from .connectors.es.cartociudad import geocode

    query = str(args.get("query") or "").strip()
    if not query:
        raise ValueError("'query' is required.")
    result = await geocode(query, get_store())
    if not result:
        return {"text": f"No match for {query!r}.", "structured": {"match": None},
                "is_error": True}
    return {"text": f"{result.get('address') or query} -> "
                    f"{result.get('lat')}, {result.get('lon')}",
            "structured": {"match": result}}


async def _tool_taxonomy(args: dict, key: ApiKey | None) -> dict:
    data = taxonomy_dict()
    lines = ["Category and subcategory keys accepted in 'layers'. A category key selects "
             "all of its subcategories.", ""]
    for cat in data["categories"]:
        lines.append(f"{cat['key']:<16} {cat['label']}")
        lines.append(f"{'':<16} {', '.join(s['key'] for s in cat['subcategories'])}")
    return {"text": "\n".join(lines), "structured": data}


async def _tool_my_limits(args: dict, key: ApiKey | None) -> dict:
    if key is None:
        return {"text": "Authentication is disabled on this deployment; no limits apply.",
                "structured": {"authenticated": False}}
    used = key_registry.used_today(key)
    limits = key.limits()
    area = limits["max_aoi_km2"]
    lines = [
        f"Tier: {key.tier}",
        f"Max area per call: {area if area == 'unlimited' else f'{area:,.0f} km2'}",
        f"Rate limit: {limits['rate_limit_per_min']} per minute",
        f"Daily quota: {limits['daily_quota']}   used today: {used:,}",
        f"Max assets per call: {limits['max_assets']:,} "
        f"(MCP tools trim further, to at most {_MCP_ASSET_CAP})",
    ]
    if area != "unlimited":
        lines.append(
            f"\nA request above {area:,.0f} km2 is refused. Split a larger perimeter into "
            f"tiles, or query the highest-risk band on its own.")
    return {"text": "\n".join(lines),
            "structured": {"authenticated": True, "tier": key.tier,
                           "limits": limits, "usage_today": used}}


async def _tool_coverage(args: dict, key: ApiKey | None) -> dict:
    from .routers.v1 import coverage as coverage_endpoint

    data = await coverage_endpoint()
    warm = [r for r in data["regions"] if r["percent"] > 0][:20]
    lines = [f"{data['fresh_tiles_total']:,} tiles cached "
             f"(tile size {data['tile_deg']} deg, TTL {data['ttl_hours']} h).", ""]
    if warm:
        lines.append("Regions with cached coverage:")
        lines += [f"  {r['key']:<22} {r['percent']:>5.1f}%  "
                  f"{r['tiles_cached']:,}/{r['tiles_total']:,} tiles" for r in warm]
        lines.append("\nA cached area answers from local storage. An uncached one pays "
                     "an OpenStreetMap round-trip on the first request.")
    else:
        lines.append("No region is warm yet; the first request for an area will be slow.")
    return {"text": "\n".join(lines), "structured": data}


# ---------------------------------------------------------------------------
# Tool catalogue
# ---------------------------------------------------------------------------
_AOI_PROPERTIES = {
    "aoi": {"type": "object",
            "description": "GeoJSON Geometry, Feature or FeatureCollection. A "
                           "FeatureCollection whose features carry a 'band' property "
                           "is read as time-banded fire perimeters and each asset is "
                           "assigned to the earliest band containing it."},
    "lon": {"type": "number", "description": "Centre longitude, as an alternative to aoi."},
    "lat": {"type": "number", "description": "Centre latitude, as an alternative to aoi."},
    "radius_km": {"type": "number", "default": 5,
                  "description": "Radius around lon/lat. Ignored when aoi is given."},
    "buffer_m": {"type": "number", "default": 0,
                 "description": "Outward buffer on the area, in metres. Counts towards "
                                "your tier's area cap."},
    "layers": {"type": "array", "items": {"type": "string"},
               "description": "Category or subcategory keys to include, e.g. "
                              "['healthcare','education']. Omit for everything. "
                              "Call talaia_taxonomy for the vocabulary."},
}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "talaia_exposure_summary",
        "title": "Summarise what is at risk in an area",
        "description":
            "Aggregate values at risk inside a polygon: asset counts by category, people "
            "at facilities, resident population, replacement value, livestock, and the "
            "highest-scoring assets for triage. Returns no per-asset list, so it is the "
            "cheap call to make repeatedly as a fire perimeter evolves. Figures are "
            "registered capacity and parametric replacement cost, never live occupancy "
            "or market value.",
        "inputSchema": {"type": "object", "properties": {
            **_AOI_PROPERTIES,
            "include_networks": {"type": "boolean", "default": True,
                                 "description": "Include road, rail and power length cut."},
            "live_osm": {"type": "boolean",
                         "description": "Force or skip the OpenStreetMap fetch. Skipping "
                                        "is faster but returns registry data only."},
        }},
        "handler": _tool_exposure_summary,
    },
    {
        "name": "talaia_list_assets",
        "title": "List individual assets at risk",
        "description":
            "The individual schools, hospitals, care homes, farms, factories, campsites "
            "and other assets inside an area, ranked by a life-safety-weighted triage "
            "score, with contact details, capacity, valuation and provenance. Use after "
            "talaia_exposure_summary when you need to act on specific sites. Ask for a "
            "small limit and filter with 'layers' - this returns a list for a person or "
            "an agent to read, not a bulk export.",
        "inputSchema": {"type": "object", "properties": {
            **_AOI_PROPERTIES,
            "limit": {"type": "integer", "default": _MCP_ASSET_DEFAULT,
                      "maximum": _MCP_ASSET_CAP,
                      "description": f"Assets to return, at most {_MCP_ASSET_CAP}."},
            "sort_by": {"type": "string",
                        "enum": ["priority", "distance", "value", "category"],
                        "default": "priority"},
            "include_geometry": {"type": "boolean", "default": False,
                                 "description": "Include each asset's full geometry. "
                                                "Large; usually not wanted."},
        }},
        "handler": _tool_list_assets,
    },
    {
        "name": "talaia_population_grid",
        "title": "Where the people are inside an area",
        "description":
            "Resident population as a surface rather than a single number: the census "
            "grid cells covering the area, each ~1 km2, ranked by density, with how much "
            "of each cell falls inside the area and which arrival band reaches it. Use "
            "this to decide where evacuation load concentrates, rather than only how "
            "many people are exposed in total. Census residents at home - not tourists, "
            "daytime workers, or anyone already evacuated.",
        "inputSchema": {"type": "object", "properties": {
            **_AOI_PROPERTIES,
            "top": {"type": "integer", "default": 20, "maximum": 100,
                    "description": "How many of the densest cells to list."},
        }},
        "handler": _tool_population_grid,
    },
    {
        "name": "talaia_geocode",
        "title": "Geocode a Spanish address",
        "description":
            "Turn a Spanish street address or place name into coordinates via "
            "CartoCiudad, so a reported location can become the centre of an area query.",
        "inputSchema": {"type": "object",
                        "properties": {"query": {"type": "string",
                                                 "description": "Street and municipality, "
                                                                "e.g. 'Carrer de Mallorca "
                                                                "401, Barcelona'."}},
                        "required": ["query"]},
        "handler": _tool_geocode,
    },
    {
        "name": "talaia_taxonomy",
        "title": "List the categories and subcategories",
        "description":
            "The closed classification vocabulary, with the vulnerability, criticality "
            "and valuation parameters behind every subcategory. Call this to find the "
            "right keys for the 'layers' filter.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": _tool_taxonomy,
    },
    {
        "name": "talaia_my_limits",
        "title": "Check this key's limits and usage",
        "description":
            "The tier, area cap, rate limit and daily quota in force for the key this "
            "session is using, plus today's usage. Worth checking before a large job: "
            "the area cap is the one that will refuse a request outright.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": _tool_my_limits,
    },
    {
        "name": "talaia_coverage",
        "title": "Check which areas are cached",
        "description":
            "How much of each named region is already in the local OpenStreetMap tile "
            "cache. A cached area answers in milliseconds; an uncached one pays an "
            "upstream round-trip on the first request.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": _tool_coverage,
    },
]

HANDLERS: dict[str, Callable[[dict, ApiKey | None], Awaitable[dict]]] = {
    t["name"]: t["handler"] for t in TOOLS}
TOOL_SPECS = [{k: v for k, v in t.items() if k != "handler"} for t in TOOLS]


# ---------------------------------------------------------------------------
# JSON-RPC plumbing
# ---------------------------------------------------------------------------
def _result(rpc_id: Any, payload: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": payload}


def _error(rpc_id: Any, code: int, message: str, data: Any = None) -> dict:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": rpc_id, "error": err}


async def dispatch(message: dict, key: ApiKey | None) -> dict | None:
    """Handle one JSON-RPC message. Returns None for notifications.

    Shared by the HTTP mount and the stdio bridge so both speak byte-identical protocol.
    """
    if message.get("jsonrpc") != "2.0":
        return _error(message.get("id"), INVALID_REQUEST,
                      "Only JSON-RPC 2.0 is supported.")
    method = message.get("method")
    rpc_id = message.get("id")
    params = message.get("params") or {}
    is_notification = "id" not in message

    if method == "initialize":
        asked = (params.get("protocolVersion") or "").strip()
        version = asked if asked in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]
        return _result(rpc_id, {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION,
                           "title": "TALAIA values-at-risk"},
            "instructions": (
                "TALAIA answers 'what is at risk inside this polygon' for wildfire "
                "decision support in Spain. Start with talaia_exposure_summary for the "
                "aggregate picture, then talaia_list_assets for the specific sites worth "
                "acting on. Areas can be given as GeoJSON or as lon/lat plus radius_km. "
                "Every key has a maximum area per call - talaia_my_limits reports it, and "
                "a request above it is refused rather than truncated. Capacity figures are "
                "registered capacity, not live occupancy, and valuations are parametric "
                "replacement-cost estimates, not appraisals; say so when you report them."),
        })

    if method in ("notifications/initialized", "notifications/cancelled"):
        return None

    if method == "ping":
        return _result(rpc_id, {})

    if method == "tools/list":
        return _result(rpc_id, {"tools": TOOL_SPECS})

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        handler = HANDLERS.get(name)
        if handler is None:
            return _error(rpc_id, INVALID_PARAMS, f"Unknown tool {name!r}.")
        try:
            out = await handler(args, key)
        except Exception as exc:
            # A tool failure is reported inside the result, not as a protocol error:
            # the model is supposed to see it, understand it and adapt. A 403 for an
            # oversized area is exactly the kind of thing an agent can recover from by
            # splitting the request, but only if it is told.
            detail = getattr(exc, "detail", None) or str(exc)
            log.info("mcp tool %s failed: %s", name, detail)
            return _result(rpc_id, {
                "content": [{"type": "text", "text": f"{name} failed: {detail}"}],
                "isError": True})
        result: dict[str, Any] = {
            "content": [{"type": "text", "text": out["text"]}],
            "isError": bool(out.get("is_error")),
        }
        if out.get("structured") is not None:
            result["structuredContent"] = out["structured"]
        return _result(rpc_id, result)

    if is_notification:
        return None
    return _error(rpc_id, METHOD_NOT_FOUND, f"Unknown method {method!r}.")


mcp_router = APIRouter(tags=["mcp"])


@mcp_router.post("/mcp", summary="Model Context Protocol endpoint")
async def mcp_endpoint(request: Request,
                       key: ApiKey | None = Depends(require_api_key)) -> Response:
    """Streamable HTTP transport, stateless.

    Authenticated by the same dependency as every data endpoint, so an MCP client needs
    a TALAIA key and consumes the same rate limit and daily quota as a REST caller.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400,
                            content=_error(None, PARSE_ERROR, "Body is not valid JSON."))

    # A client may batch several messages into one array.
    if isinstance(body, list):
        if not body:
            return JSONResponse(status_code=400,
                                content=_error(None, INVALID_REQUEST, "Empty batch."))
        out = [r for r in [await dispatch(m, key) for m in body] if r is not None]
        return Response(status_code=202) if not out else JSONResponse(content=out)

    if not isinstance(body, dict):
        return JSONResponse(
            status_code=400,
            content=_error(None, INVALID_REQUEST, "Expected a JSON-RPC object or array."))

    response = await dispatch(body, key)
    if response is None:
        # Notifications get no body. 202 is what the transport specifies.
        return Response(status_code=202)
    return JSONResponse(content=response)


@mcp_router.get("/mcp", summary="MCP transport probe")
async def mcp_get(key: ApiKey | None = Depends(require_api_key)) -> Response:
    """This server is stateless, so there is no stream to open.

    405 is the specified way to say that, and it keeps clients from waiting on an SSE
    channel that will never produce anything.
    """
    return JSONResponse(
        status_code=405,
        content={"error": "This MCP server is stateless; POST JSON-RPC to /mcp.",
                 "server": SERVER_NAME, "version": SERVER_VERSION},
        headers={"Allow": "POST"})
