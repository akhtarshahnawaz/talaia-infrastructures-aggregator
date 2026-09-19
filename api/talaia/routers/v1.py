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
import re
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..auth import (TIERS, ApiKey, get_tier, registry as key_registry,
                    require_admin_key, require_api_key)
from ..config import settings
from ..connectors import registry
from ..geo import parse_aoi
from ..models import (ExposureReport, ExposureRequest, GeocodeRequest, SignupRequest,
                      SourceStatus)
from ..regions import REGIONS, catalogue as region_catalogue, resolve_many
from ..services.aggregator import build_report
from ..store import get_store
from ..taxonomy import as_dict as taxonomy_dict

log = logging.getLogger("talaia.api")


def _enforce_key_limits(req: ExposureRequest, key: ApiKey | None) -> ExposureRequest:
    """Apply the caller's tier limits before any expensive work happens.

    Area is checked first and hardest. Rate and quota limits only slow an abuser down,
    but a single unbounded polygon can pull millions of rows and request hundreds of
    Overpass tiles in one call - so the area cap is the control that actually protects
    the service, and it is evaluated before the store is ever touched.
    """
    if key is None:
        return req
    if not key.unlimited_area:
        try:
            aoi = parse_aoi(req.aoi, buffer_metres=req.buffer_m,
                            band_property=req.band_property,
                            minutes_property=req.minutes_property)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if aoi.area_km2 > key.max_aoi_km2:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Area of interest is {aoi.area_km2:,.1f} km², above the "
                    f"{key.max_aoi_km2:,.0f} km² limit for the '{key.tier}' tier. "
                    f"Split the request into smaller polygons, or request a higher tier."
                ))
    if key.max_assets and (req.max_assets is None or req.max_assets > key.max_assets):
        req = req.model_copy(update={"max_assets": key.max_assets})
    return req


def _metadata_guard():
    """Open metadata endpoints unless the operator has closed them."""
    return [] if settings.public_metadata else [Depends(require_api_key)]


router = APIRouter(prefix="/v1", tags=["exposure"],
                   dependencies=[Depends(require_api_key)])
public_router = APIRouter(prefix="/v1", tags=["access"])
meta_router = APIRouter(prefix="/v1", tags=["metadata"],
                        dependencies=_metadata_guard())
admin_router = APIRouter(prefix="/v1/admin", tags=["admin"],
                         dependencies=[Depends(require_admin_key)])


# ---------------------------------------------------------------------------
# Data endpoints - always authenticated
# ---------------------------------------------------------------------------
@router.post("/exposure", response_model=ExposureReport,
             summary="Full values-at-risk report for a polygon")
async def exposure(req: ExposureRequest,
                   key: ApiKey | None = Depends(require_api_key)) -> ExposureReport:
    """Return everything of value inside the AOI.

    Accepts a GeoJSON Geometry, Feature or FeatureCollection. A FeatureCollection whose
    features carry a `band` property is treated as time-banded fire perimeters, and every
    asset is assigned to the earliest band that contains it.

    **Requires an API key** (`X-API-Key` or `Authorization: Bearer`).
    """
    req = _enforce_key_limits(req, key)
    try:
        return await build_report(req, get_store())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/exposure/summary", summary="Aggregates only - no asset array")
async def exposure_summary(req: ExposureRequest,
                           key: ApiKey | None = Depends(require_api_key)
                           ) -> dict[str, Any]:
    """The decision-relevant numbers without the long asset list.

    Intended for an agent loop that polls exposure as a fire evolves and only needs the
    asset detail once something crosses a threshold.
    """
    req = _enforce_key_limits(req, key)
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
async def assets_stream(req: ExposureRequest,
                        key: ApiKey | None = Depends(require_api_key)
                        ) -> StreamingResponse:
    """Stream assets as NDJSON so a large AOI can be consumed incrementally."""
    req = _enforce_key_limits(req, key)
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


@router.get("/me", summary="What your key is allowed to do")
async def me(key: ApiKey | None = Depends(require_api_key)) -> dict[str, Any]:
    """Your tier, limits and usage so far today.

    Worth calling before a large job: it tells you the maximum area you may request in a
    single call, so you can split a big AOI client-side instead of discovering the limit
    through a 403.
    """
    if key is None:
        return {"authenticated": False, "note": "Authentication is disabled."}
    used = key_registry.used_today(key)
    return {
        "authenticated": True,
        "prefix": key.prefix, "label": key.label, "tier": key.tier,
        "limits": key.limits(),
        "usage_today": used,
        "daily_remaining": (key.daily_quota - used) if key.daily_quota else "unlimited",
    }


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


@meta_router.get("/regions", summary="Named regions available for cache warming")
async def regions_list() -> dict[str, Any]:
    """The gazetteer the warmer understands, with each region's tile count.

    Tile count is the unit of cost: one tile is roughly one Overpass request's worth of
    work, so it is the number to look at before asking for a region to be warmed.
    """
    return {"tile_deg": settings.osm_tile_deg, "regions": region_catalogue()}


@meta_router.get("/coverage", summary="Which areas are already cached")
async def coverage() -> dict[str, Any]:
    """How much of each named region is warm.

    Worth checking before a demo: a region reported at 100% answers from local storage in
    milliseconds, while a cold one pays an Overpass round-trip on the first request.
    """
    store = get_store()
    # INTERVAL takes a literal, not a bind parameter. The value is an int from
    # configuration, never from the request, so interpolating it is safe here.
    rows = await store.fetch(
        "SELECT tile_key, min_lon, min_lat, feature_count FROM osm_tile_cache "
        "WHERE status = 'ok' AND fetched_at > "
        f"(current_timestamp - INTERVAL {int(settings.osm_tile_ttl_hours)} HOUR)")
    fresh = {r[0]: (r[1], r[2], r[3] or 0) for r in rows}
    deg = settings.osm_tile_deg
    out = []
    for region in REGIONS.values():
        x0, y0, x1, y1 = region.bbox
        hit = [v for v in fresh.values()
               if x0 - deg <= v[0] <= x1 and y0 - deg <= v[1] <= y1]
        total = region.tile_count()
        out.append({"key": region.key, "name": region.name,
                    "tiles_total": total, "tiles_cached": len(hit),
                    "percent": round(100.0 * len(hit) / total, 1) if total else 0.0,
                    "cached_features": sum(v[2] for v in hit)})
    out.sort(key=lambda r: (-r["percent"], r["tiles_total"]))
    return {"tile_deg": deg, "ttl_hours": settings.osm_tile_ttl_hours,
            "fresh_tiles_total": len(fresh), "regions": out}


# ---------------------------------------------------------------------------
# Public - self-service access
# ---------------------------------------------------------------------------
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]{2,}$")


def _client_ip(request: Request) -> str:
    """Real client address behind Railway's proxy.

    The first entry of X-Forwarded-For is the originating client; later entries are
    proxies. Falling back to request.client would see only the proxy and make the
    per-IP signup limit meaningless.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@public_router.get("/tiers", summary="Access tiers and their limits")
async def tiers() -> dict[str, Any]:
    """What each tier allows. Published so a caller can size requests before signing up."""
    return {
        "signup_enabled": settings.allow_signup,
        "signup_tier": settings.signup_tier,
        "tiers": [
            {"name": t.name, "description": t.description,
             "rate_limit_per_min": t.rate_limit_per_min or "unlimited",
             "daily_quota": t.daily_quota or "unlimited",
             "max_aoi_km2": t.max_aoi_km2 or "unlimited",
             "max_assets": t.max_assets,
             "self_service": t.name == settings.signup_tier}
            for t in TIERS.values()
        ],
    }


@public_router.post("/signup", summary="Create an account and receive an API key")
async def signup(req: SignupRequest, request: Request) -> dict[str, Any]:
    """Self-service access.

    Issues a free-tier key immediately. Abuse controls: one active key per email address,
    a per-IP daily signup cap, and the tier's own area, rate and quota limits.

    **The key is returned once and cannot be recovered** - only its hash is stored.
    """
    if not settings.allow_signup:
        raise HTTPException(
            status_code=403,
            detail="Self-service signup is disabled on this deployment. "
                   "Contact the operator for a key.")

    email = req.email.strip().lower()
    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=422, detail="That does not look like an email address.")

    store = get_store()
    ip = _client_ip(request)

    existing = await store.fetch(
        "SELECT prefix FROM api_keys WHERE email = ? AND revoked_at IS NULL", [email])
    if existing:
        raise HTTPException(
            status_code=409,
            detail=(f"An active key already exists for {email} (prefix "
                    f"{existing[0][0]}). Keys cannot be re-displayed, so ask the "
                    f"operator to revoke it if you need a new one."))

    recent = await store.fetch(
        "SELECT count(*) FROM signups WHERE ip = ? AND created_at > "
        "(current_timestamp - INTERVAL 24 HOUR)", [ip])
    if recent and recent[0][0] >= settings.signups_per_ip_per_day:
        raise HTTPException(
            status_code=429,
            detail=(f"This address has already created "
                    f"{settings.signups_per_ip_per_day} keys in the last 24 hours."),
            headers={"Retry-After": "3600"})

    label = (req.organisation or email.split("@")[0])[:64]
    raw, record = await key_registry.create(
        store, label=label, tier=settings.signup_tier, email=email,
        organisation=req.organisation, created_ip=ip)
    await store.execute_write(
        "INSERT INTO signups (id, email, organisation, ip, created_at, key_prefix) "
        "VALUES (?, ?, ?, ?, current_timestamp, ?)",
        [uuid.uuid4().hex, email, req.organisation, ip, record.prefix])
    log.info("signup: %s tier=%s from %s", record.prefix, record.tier, ip)

    spec = get_tier(record.tier)
    return {
        "api_key": raw,
        "prefix": record.prefix,
        "tier": record.tier,
        "limits": record.limits(),
        "tier_description": spec.description,
        "usage": {
            "header": "X-API-Key: <your key>",
            "alternative": "Authorization: Bearer <your key>",
            "check_limits": "GET /v1/me",
        },
        "warning": "Store this key now. It is hashed on arrival and cannot be shown again.",
    }


# ---------------------------------------------------------------------------
# Admin - key management
# ---------------------------------------------------------------------------
@admin_router.post("/keys", summary="Mint a new API key")
async def create_key(payload: dict[str, Any]) -> dict[str, Any]:
    """Create a key. **The secret is returned once and cannot be recovered afterwards**,
    because only its hash is stored."""
    label = str(payload.get("label") or "unnamed")[:64]
    tier = str(payload.get("tier") or "standard")
    if tier not in TIERS:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown tier {tier!r}. Available: {', '.join(TIERS)}.")
    limit = payload.get("rate_limit_per_min")
    area = payload.get("max_aoi_km2")
    quota = payload.get("daily_quota")
    raw, record = await key_registry.create(
        get_store(), label=label, tier=tier,
        rate_limit_per_min=int(limit) if limit is not None else None,
        max_aoi_km2=float(area) if area is not None else None,
        daily_quota=int(quota) if quota is not None else None,
        email=payload.get("email"), organisation=payload.get("organisation"))
    return {"api_key": raw, "prefix": record.prefix, "label": record.label,
            "tier": record.tier, "limits": record.limits(),
            "warning": "Store this now. It is not recoverable."}


@admin_router.get("/keys", summary="List keys (prefixes only, never secrets)")
async def list_keys() -> list[dict[str, Any]]:
    return await key_registry.list_keys(get_store())


@admin_router.patch("/keys/{prefix}", summary="Change a key's tier or limits")
async def update_key(prefix: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Re-tier an existing key in place, keeping the secret.

    This is how a self-service signup becomes an unlimited integration key without the
    holder having to swap credentials.
    """
    tier = payload.get("tier")
    if tier is not None and str(tier) not in TIERS:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown tier {tier!r}. Available: {', '.join(TIERS)}.")
    limit, area, quota = (payload.get("rate_limit_per_min"),
                          payload.get("max_aoi_km2"), payload.get("daily_quota"))
    record = await key_registry.update(
        get_store(), prefix, tier=str(tier) if tier else None,
        rate_limit_per_min=int(limit) if limit is not None else None,
        max_aoi_km2=float(area) if area is not None else None,
        daily_quota=int(quota) if quota is not None else None)
    if record is None:
        raise HTTPException(status_code=404, detail="No active key with that prefix.")
    return {"prefix": record.prefix, "tier": record.tier, "limits": record.limits()}


# ---------------------------------------------------------------------------
# Admin - cache warming
# ---------------------------------------------------------------------------
@admin_router.post("/warm", summary="Pre-load the OSM tile cache for a region")
async def start_warm(payload: dict[str, Any]) -> dict[str, Any]:
    """Warm tiles in the background so a demo area answers from local storage.

    Runs in this process on purpose. DuckDB takes one writer, and the API holds it while
    it is up, so a separate CLI run would simply be locked out.

    Pass ``dry_run`` to see the tile and block count without fetching anything.
    """
    from ..services.warm import estimate, warmer

    names = payload.get("regions") or payload.get("region") or []
    if isinstance(names, str):
        names = [names]
    if not names:
        raise HTTPException(
            status_code=422,
            detail="Give 'regions': a list of region keys, group names or bboxes. "
                   "See GET /v1/regions.")
    try:
        regions = resolve_many([str(n) for n in names])
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=exc.args[0]) from exc

    est = estimate(regions)
    if payload.get("dry_run"):
        return {"dry_run": True, "regions": [r.key for r in regions], **est}
    try:
        progress = warmer.start(
            get_store(), regions,
            force=bool(payload.get("force")),
            concurrency=payload.get("concurrency"),
            pause_s=payload.get("pause_s"),
            max_tiles=int(payload["max_tiles"]) if payload.get("max_tiles") else None)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"started": True, "regions": [r.key for r in regions], "estimate": est,
            "progress": progress.as_dict(),
            "poll": "GET /v1/admin/warm"}


@admin_router.get("/warm", summary="Progress of the running or last warm")
async def warm_status() -> dict[str, Any]:
    from ..services.warm import warmer
    return {"running": warmer.running, **warmer.progress.as_dict()}


@admin_router.delete("/warm", summary="Stop the running warm")
async def stop_warm() -> dict[str, Any]:
    """Stops after the in-flight blocks finish. Tiles already fetched stay cached, so
    restarting resumes rather than repeating."""
    from ..services.warm import warmer
    cancelled = await warmer.cancel()
    if not cancelled:
        raise HTTPException(status_code=404, detail="No warm is running.")
    return {"cancelled": True, **warmer.progress.as_dict()}


@admin_router.delete("/keys/{prefix}", summary="Revoke a key by prefix")
async def revoke_key(prefix: str) -> dict[str, Any]:
    ok = await key_registry.revoke(get_store(), prefix)
    if not ok:
        raise HTTPException(status_code=404, detail="No active key with that prefix.")
    return {"revoked": prefix}
