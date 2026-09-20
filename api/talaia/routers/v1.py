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
import asyncio
import logging
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..auth import (TIERS, ApiKey, get_tier, hash_key,
                    registry as key_registry, require_admin_key, require_api_key)
from ..config import settings
from ..connectors import registry
from ..geo import parse_aoi
from ..models import (ExposureReport, ExposureRequest, GeocodeRequest, SignupRequest,
                      SourceStatus, VerifyRequest)
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


@router.post("/population", summary="Resident population as a grid surface")
async def population(req: ExposureRequest,
                     key: ApiKey | None = Depends(require_api_key)) -> dict[str, Any]:
    """Census population for the AOI, broken down by 1 km grid cell.

    The same figures `/v1/exposure` returns under `population`, but without building the
    asset inventory - no store scan for assets, no OpenStreetMap fetch, no conflation and
    no scoring. That is most of the work in a full report, so this is the endpoint to
    poll when you want a population surface to map rather than a list of sites.

    Cells come back densest first, each with its full population, the share inside the
    AOI, and the earliest band covering its centroid. Set `include_geometry` for cell
    polygons.
    """
    import time
    import uuid as _uuid

    from ..services.population import population_for

    req = _enforce_key_limits(req, key)
    try:
        aoi = parse_aoi(req.aoi, buffer_metres=req.buffer_m,
                        band_property=req.band_property,
                        minutes_property=req.minutes_property)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if aoi.area_km2 > settings.max_aoi_km2:
        raise HTTPException(
            status_code=422,
            detail=(f"AOI area {aoi.area_km2:,.0f} km2 exceeds the "
                    f"{settings.max_aoi_km2:,.0f} km2 limit"))

    # Deliberately not build_report. That path queries assets, networks and
    # OpenStreetMap before it ever reaches the population overlay, and none of those
    # change a single number here - on a 28 km2 area it was 2.4 s of work to produce a
    # figure the grid query alone answers in a fraction of that.
    started = time.perf_counter()
    result = await population_for(get_store(), aoi, aoi.bands, include_cells=True,
                                  with_geometry=req.include_geometry)
    elapsed = round((time.perf_counter() - started) * 1000, 1)

    warnings: list[str] = []
    if result.cells_truncated:
        warnings.append(
            f"Population grid truncated to the {len(result.cells):,} densest cells of "
            f"{result.cell_count:,}. 'total' still counts every cell; only the "
            f"per-cell list is cut.")
    if result.method == "no_grid_coverage":
        warnings.append("No census population grid covers this area.")

    return {
        "request_id": f"req_{_uuid.uuid4().hex[:12]}",
        "generated_at": datetime.now(timezone.utc),
        "aoi_bbox": [round(v, 6) for v in aoi.bbox],
        "area_km2": round(aoi.area_km2, 4),
        "population": result,
        "warnings": warnings,
        "timing": {"total_ms": elapsed, "store_query_ms": elapsed},
    }


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
    # "sources" counts the ones holding rows. Without the registered total beside it, a
    # cold deploy reports 0 sources while /v1/sources lists a dozen, and the two numbers
    # look like a contradiction rather than a loading state.
    data["sources_registered"] = len(registry.all_connectors())
    # Linear features come only from OpenStreetMap - no registry publishes them - so this
    # count tracks tile-cache warmth, not the registry load.
    data["networks_note"] = ("Linear features are OpenStreetMap-derived and appear as the "
                             "tile cache warms; registries publish no road or power data.")
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
    # The cutoff is computed here and bound, rather than written as SQL. Two reasons,
    # both of which have bitten: `INTERVAL n HOUR` is DuckDB's spelling and a syntax
    # error in Postgres, and `current_timestamp` is the session's clock while
    # fetched_at holds naive UTC - so on a server set to anything but UTC the
    # comparison is silently off by the offset.
    cutoff = _utcnow() - timedelta(hours=int(settings.osm_tile_ttl_hours))
    rows = await store.fetch(
        "SELECT tile_key, min_lon, min_lat, feature_count FROM osm_tile_cache "
        "WHERE status = 'ok' AND fetched_at > ?", [cutoff])
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


def _public_base(request: Request) -> str:
    """Absolute base URL for links we put in an email.

    Behind a proxy the request's own host is the internal one, so an explicit
    TALAIA_PUBLIC_URL wins. The forwarded headers are the fallback, and the raw request
    URL the last resort - a wrong link here means a verification mail nobody can use.
    """
    if settings.public_url:
        return settings.public_url.rstrip("/")
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    if host:
        return f"{proto}://{host}"
    return str(request.base_url).rstrip("/")


async def _issue_key(store, *, email: str, organisation: str | None, ip: str | None,
                     verified: bool) -> dict[str, Any]:
    """Mint the free-tier key and return the one-time payload."""
    label = (organisation or email.split("@")[0])[:64]
    raw, record = await key_registry.create(
        store, label=label, tier=settings.signup_tier, email=email,
        organisation=organisation, created_ip=ip)
    if verified:
        await store.execute_write(
            "UPDATE api_keys SET email_verified = TRUE WHERE key_hash = ?",
            [record.key_hash])
    await store.execute_write(
        "INSERT INTO signups (id, email, organisation, ip, created_at, key_prefix) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [uuid.uuid4().hex, email, organisation, ip, _utcnow(), record.prefix])
    log.info("key issued: %s tier=%s verified=%s", record.prefix, record.tier, verified)

    spec = get_tier(record.tier)
    return {
        "api_key": raw,
        "prefix": record.prefix,
        "tier": record.tier,
        "email": email,
        "email_verified": verified,
        "limits": record.limits(),
        "tier_description": spec.description,
        "usage": {
            "header": "X-API-Key: <your key>",
            "alternative": "Authorization: Bearer <your key>",
            "check_limits": "GET /v1/me",
        },
        "warning": "Store this key now. It is hashed on arrival and cannot be shown again.",
    }


def _utcnow() -> datetime:
    """Naive UTC, matching every timestamp column in the schema.

    Every `created_at`, `fetched_at` and `expires_at` is `TIMESTAMP` holding UTC with no
    zone, so anything compared against them has to be the same thing. Asking the database
    for `current_timestamp` instead returns the session's clock, which is only UTC by
    luck of how the server happens to be configured.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _signup_guards(store, email: str, ip: str) -> None:
    """Checks that apply before anything is created or any mail is sent."""
    if not settings.allow_signup:
        raise HTTPException(
            status_code=403,
            detail="Self-service signup is disabled on this deployment. "
                   "Contact the operator for a key.")
    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=422,
                            detail="That does not look like an email address.")

    existing = await store.fetch(
        "SELECT prefix FROM api_keys WHERE email = ? AND revoked_at IS NULL", [email])
    if existing:
        raise HTTPException(
            status_code=409,
            detail=(f"An active key already exists for {email} (prefix "
                    f"{existing[0][0]}). Keys cannot be re-displayed, so ask the "
                    f"operator to revoke it if you need a new one."))

    recent = await store.fetch(
        "SELECT count(*) FROM signups WHERE ip = ? AND created_at > ?",
        [ip, _utcnow() - timedelta(hours=24)])
    if recent and recent[0][0] >= settings.signups_per_ip_per_day:
        raise HTTPException(
            status_code=429,
            detail=(f"This address has already created "
                    f"{settings.signups_per_ip_per_day} keys in the last 24 hours."),
            headers={"Retry-After": "3600"})


@public_router.post("/signup", status_code=202,
                    summary="Request an API key; confirm by email")
async def signup(req: SignupRequest, request: Request) -> dict[str, Any]:
    """Start self-service access.

    Sends a single-use confirmation link to the address given. **No key exists until that
    link is followed**, so an address nobody controls produces nothing.

    The key is shown on the confirmation page rather than mailed: email is not a
    confidential channel, and a credential sent by mail sits in an inbox indefinitely.

    Abuse controls: a confirmed address is required, one active key per address, a per-IP
    daily cap, and the tier's own area, rate and quota limits.
    """
    from .. import mailer

    email = req.email.strip().lower()
    store = get_store()
    ip = _client_ip(request)
    await _signup_guards(store, email, ip)

    if not settings.require_email_verification:
        # Explicitly disabled by the operator - issue immediately, and say plainly in the
        # response that the address was never checked.
        payload = await _issue_key(store, email=email, organisation=req.organisation,
                                   ip=ip, verified=False)
        payload["note"] = ("Email verification is disabled on this deployment; this "
                           "address was not confirmed.")
        return payload

    if not mailer.available():
        # Falling back to issuing a key here would silently undo verification, which is
        # worse than refusing: the operator would believe addresses were being checked.
        log.error("signup attempted with no mail backend configured")
        raise HTTPException(
            status_code=503,
            detail=("Email verification is required but this deployment has no mail "
                    "sender configured, so no key can be issued. Contact the operator."))

    token = secrets.token_urlsafe(32)
    ttl = max(1, settings.verification_ttl_hours)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    # Supersede any earlier unconsumed request for this address, so the most recent mail
    # is the one that works and an old link cannot be used later.
    await store.execute_write(
        "UPDATE pending_signups SET expires_at = ? "
        "WHERE email = ? AND consumed_at IS NULL", [now, email])
    await store.execute_write(
        "INSERT OR REPLACE INTO pending_signups "
        "(token_hash, email, organisation, use_case, ip, created_at, expires_at, "
        " consumed_at) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
        [hash_key(token), email, req.organisation, req.use_case, ip, now,
         now + timedelta(hours=ttl)])

    link = f"{_public_base(request)}/verify?token={token}"
    subject, text, html = mailer.verification_message(link, ttl)
    sent = await mailer.send(email, subject, text, html)
    if not sent:
        raise HTTPException(
            status_code=503,
            detail=("Could not send the confirmation email just now. No key was "
                    "created. Please try again shortly."))

    log.info("verification sent to %s from %s", email, ip)
    payload: dict[str, Any] = {
        "status": "verification_sent",
        "email": email,
        "expires_in_hours": ttl,
        "message": (f"Check {email} for a confirmation link. It works once and expires "
                    f"in {ttl} hours. Your key is shown after you follow it."),
    }
    if settings.email_console:
        # Console backend is local development only, where nothing was actually sent.
        payload["verification_link"] = link
    return payload


@public_router.post("/verify", summary="Confirm an email and receive the key")
async def verify(req: VerifyRequest, request: Request) -> dict[str, Any]:
    """Exchange a confirmation token for the API key. Single use.

    The key is returned once, here, and only its hash is stored afterwards.
    """
    store = get_store()
    token = (req.token or "").strip()
    if not token:
        raise HTTPException(status_code=422, detail="A token is required.")

    rows = await store.fetch(
        "SELECT email, organisation, ip, expires_at, consumed_at FROM pending_signups "
        "WHERE token_hash = ?", [hash_key(token)])
    if not rows:
        raise HTTPException(
            status_code=404,
            detail="That confirmation link is not valid. Request a new one at /signup.")

    email, organisation, ip, expires_at, consumed_at = rows[0]
    if consumed_at is not None:
        raise HTTPException(
            status_code=409,
            detail=("That link has already been used. A key was issued and shown once; "
                    "ask the operator to revoke it if you need another."))
    if expires_at is not None and expires_at < datetime.now(timezone.utc).replace(tzinfo=None):
        raise HTTPException(
            status_code=410,
            detail="That confirmation link has expired. Request a new one at /signup.")

    # Re-run the guards: time passed between the request and the click, and an address
    # may have been given a key by another route in between.
    await _signup_guards(store, email, ip or _client_ip(request))
    # Consume before issuing, so a double click cannot produce two keys.
    await store.execute_write(
        "UPDATE pending_signups SET consumed_at = ? WHERE token_hash = ?",
        [datetime.now(timezone.utc).replace(tzinfo=None), hash_key(token)])
    return await _issue_key(store, email=email, organisation=organisation, ip=ip,
                            verified=True)


@public_router.get("/verify", summary="Confirm an email and receive the key")
async def verify_get(token: str, request: Request) -> dict[str, Any]:
    """Same as the POST form, for following the link straight from a mail client."""
    return await verify(VerifyRequest(token=token), request)


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


@admin_router.delete("/keys", summary="Revoke every active key for an email address")
async def revoke_by_email(email: str) -> dict[str, Any]:
    """For "I have lost my key", which is an email-shaped problem: the person asking
    cannot read a prefix off anything they still have."""
    revoked = await key_registry.revoke_by_email(get_store(), email)
    if not revoked:
        raise HTTPException(
            status_code=404,
            detail=f"No active key for {email}.")
    return {"email": email.strip().lower(), "revoked": revoked}


@admin_router.get("/usage", summary="Requests per key per day")
async def usage(days: int = 14) -> dict[str, Any]:
    """Daily request counts, joined to the key that made them.

    Counters live in memory and flush on a timer, so today's figure trails reality by up
    to a minute. Earlier days are settled.
    """
    days = max(1, min(int(days), 90))
    store = get_store()
    since = (datetime.now(timezone.utc).replace(tzinfo=None)
             - timedelta(days=days)).date()
    rows = await store.fetch(
        "SELECT u.day, k.prefix, k.label, k.email, k.tier, u.requests "
        "FROM key_usage u LEFT JOIN api_keys k ON k.key_hash = u.key_hash "
        "WHERE u.day >= ? ORDER BY u.day DESC, u.requests DESC", [since])
    by_day: dict[str, int] = {}
    by_key: dict[str, dict[str, Any]] = {}
    for day, prefix, label, email, tier, requests in rows:
        key = str(day)
        by_day[key] = by_day.get(key, 0) + int(requests or 0)
        ident = prefix or "(revoked or unknown)"
        entry = by_key.setdefault(ident, {"prefix": ident, "label": label,
                                          "email": email, "tier": tier, "requests": 0})
        entry["requests"] += int(requests or 0)
    return {
        "days": days,
        "total_requests": sum(by_day.values()),
        "by_day": [{"day": d, "requests": n} for d, n in sorted(by_day.items())],
        "by_key": sorted(by_key.values(), key=lambda r: -r["requests"]),
        "note": ("Today's counts flush from memory on a timer and may trail by up to a "
                 "minute. Keys revoked since a request was made show as unknown."),
    }


@admin_router.get("/signups", summary="Recent signups and pending verifications")
async def signups(limit: int = 50) -> dict[str, Any]:
    """Both halves of the funnel: addresses that confirmed, and addresses still sitting
    on an unfollowed link."""
    limit = max(1, min(int(limit), 500))
    store = get_store()
    completed = await store.fetch(
        "SELECT email, organisation, ip, created_at, key_prefix FROM signups "
        "ORDER BY created_at DESC LIMIT ?", [limit])
    pending = await store.fetch(
        "SELECT email, organisation, ip, created_at, expires_at FROM pending_signups "
        "WHERE consumed_at IS NULL ORDER BY created_at DESC LIMIT ?", [limit])
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return {
        "completed": [{"email": e, "organisation": o, "ip": i, "created_at": c,
                       "key_prefix": k} for e, o, i, c, k in completed],
        "pending": [{"email": e, "organisation": o, "ip": i, "created_at": c,
                     "expires_at": x, "expired": bool(x and x < now)}
                    for e, o, i, c, x in pending],
    }


@admin_router.delete("/signups/pending", summary="Clear a pending verification")
async def clear_pending(email: str) -> dict[str, Any]:
    """Drop unfollowed confirmation links for an address, so it can start over.

    Useful when someone mistyped their address, or when mail was misconfigured and the
    links that went out are unreachable.
    """
    store = get_store()
    target = email.strip().lower()
    before = await store.fetch(
        "SELECT count(*) FROM pending_signups WHERE lower(email) = ? "
        "AND consumed_at IS NULL", [target])
    await store.execute_write(
        "DELETE FROM pending_signups WHERE lower(email) = ? AND consumed_at IS NULL",
        [target])
    return {"email": target, "cleared": int(before[0][0]) if before else 0}


@admin_router.get("/email", summary="What the mail sender is configured to do")
async def email_status() -> dict[str, Any]:
    """Report the active backend without sending anything."""
    from .. import mailer

    return {
        "backend": mailer.backend(),
        "available": mailer.available(),
        "from": mailer._from_address() if mailer.available() else None,
        "using_shared_sender": mailer.using_shared_sender(),
        "verification_required": settings.require_email_verification,
        "last_error": mailer.last_error(),
        "note": (
            "Sending as Resend's shared onboarding address. It needs no DNS setup, but "
            "Resend will only deliver to the address that owns the account - everyone "
            "else's signup will fail. Verify a domain and set TALAIA_EMAIL_FROM before "
            "opening signup to the public."
            if mailer.using_shared_sender() else None),
    }


@admin_router.post("/email/test", summary="Send a test email and report what happened")
async def email_test(payload: dict[str, Any]) -> dict[str, Any]:
    """Send a real message to an address you control.

    Returns the provider's own error when it fails, which is the whole diagnosis for the
    usual case - an unverified sender domain. Worth running once after configuring mail
    and before opening signup, because otherwise the first person to find out is a user
    whose confirmation never arrives.
    """
    from .. import mailer

    to = str(payload.get("to") or "").strip()
    if not to or not _EMAIL_RE.match(to):
        raise HTTPException(status_code=422,
                            detail="Give 'to': an address you can check.")
    if not mailer.available():
        raise HTTPException(
            status_code=503,
            detail="No mail backend configured. Set TALAIA_RESEND_API_KEY "
                   "(simplest) or the TALAIA_SMTP_* variables.")

    sent = await mailer.send(
        to, "TALAIA mail test",
        "This is a test from your TALAIA deployment.\n\n"
        "If you are reading it, verification emails will reach this address.\n")
    return {
        "sent": sent,
        "backend": mailer.backend(),
        "from": mailer._from_address(),
        "to": to,
        "error": mailer.last_error(),
        "hint": (
            "Resend only delivers to the account owner's address until a domain is "
            "verified. Verify one at resend.com/domains, then set TALAIA_EMAIL_FROM to "
            "an address on it."
            if not sent and mailer.backend() == "resend" else None),
    }


# ---------------------------------------------------------------------------
# Admin - loading a source on demand
# ---------------------------------------------------------------------------
def _boot_task_state(request: Request) -> dict[str, Any]:
    """What became of the bootstrap task started at boot.

    It runs detached in the background, so when it dies its traceback goes to the log and
    nothing else notices - the store simply stops filling and the panel shows sources
    that never load. Surfacing it here turns "nothing is happening" into an answer.
    """
    task = getattr(request.app.state, "bootstrap_task", None)
    if task is None:
        return {"state": "not_started",
                "note": "Nothing was pending at boot, or auto-bootstrap is off."}
    if not task.done():
        return {"state": "running"}
    if task.cancelled():
        return {"state": "cancelled"}
    exc = task.exception()
    return {"state": "failed", "error": f"{type(exc).__name__}: {exc}"[:300]} if exc \
        else {"state": "finished"}


@admin_router.get("/prefetch", summary="What is loaded, and any run in progress")
async def prefetch_status(request: Request) -> dict[str, Any]:
    """Every registered source with what it holds, plus the live run if there is one."""
    from ..connectors import registry
    from ..connectors.base import Tier
    from ..services.prefetch import prefetcher

    stats = await get_store().source_stats()
    sources = []
    for cls in registry.all_connectors():
        entry = stats.get(cls.meta.id, {})
        sources.append({
            "id": cls.meta.id,
            "name": cls.meta.name,
            "tier": cls.meta.tier,
            "coverage": cls.meta.coverage,
            "rows": entry.get("rows", 0),
            "last_run_at": entry.get("last_run_at"),
            "last_status": entry.get("last_status", "never_run"),
            "last_error": entry.get("last_error"),
            # On-demand sources fill from live queries and cache warming, not from here.
            "prefetchable": cls.tier is Tier.RESIDENT,
        })
    sources.sort(key=lambda s: (s["rows"] > 0, s["id"]))
    return {"sources": sources, "run": prefetcher.progress.as_dict(),
            "running": prefetcher.running, "boot_bootstrap": _boot_task_state(request),
            "write_health": get_store().write_health()}


# The 52 provinces as the national registries spell them. They are the column the
# `place` filter matches, and an operator cannot be expected to guess the accents.
_ES_PROVINCES = [
    "A Coruña", "Álava", "Albacete", "Alicante", "Almería", "Asturias", "Ávila",
    "Badajoz", "Barcelona", "Burgos", "Cáceres", "Cádiz", "Cantabria", "Castellón",
    "Ceuta", "Ciudad Real", "Córdoba", "Cuenca", "Girona", "Granada", "Guadalajara",
    "Guipúzcoa", "Huelva", "Huesca", "Illes Balears", "Jaén", "La Rioja", "Las Palmas",
    "León", "Lleida", "Lugo", "Madrid", "Málaga", "Melilla", "Murcia", "Navarra",
    "Ourense", "Palencia", "Pontevedra", "Salamanca", "Santa Cruz de Tenerife",
    "Segovia", "Sevilla", "Soria", "Tarragona", "Teruel", "Toledo", "Valencia",
    "Valladolid", "Vizcaya", "Zamora", "Zaragoza",
]


@meta_router.get("/diagnostics", summary="Container limits, disk and write health")
async def diagnostics() -> dict[str, Any]:
    """Unauthenticated on purpose.

    It reports limits, counters and free space - nothing secret, no data, no key
    material - and it exists for the case where the deployment is wedged and the person
    who needs to know why is not standing next to it. Requiring a credential to read a
    memory limit would mean handing one over to get a bug fixed.
    """
    from ..diagnostics import report

    return report(get_store())


@admin_router.post("/unstick", summary="Cancel whatever is holding the database writer")
async def unstick() -> dict[str, Any]:
    """Free the single writer by hand, without restarting anything.

    The watchdog does this on its own after a couple of minutes; this is the same lever
    for someone who is watching and does not want to wait. It cancels every running
    query, so an unlucky read fails too - which only matters if writes were fine, and if
    writes were fine you would not be pressing it.
    """
    store = get_store()
    before = store.write_health()
    cancelled = await asyncio.to_thread(store.interrupt_live_queries)
    await asyncio.sleep(0.5)
    return {"cancelled": cancelled, "was": before, "now": store.write_health()}


@admin_router.get("/places", summary="Place names the prefetch filter will match")
async def prefetch_places() -> dict[str, Any]:
    """Suggestions for the place box.

    The provinces are fixed and always offered, because they are what the national
    registries put in their province column and the filter matches them whether or not
    anything is loaded yet. Municipalities are read back out of what *is* loaded, so the
    spelling offered is literally the spelling stored.
    """
    municipalities: list[str] = []
    try:
        rows = await get_store().fetch(
            "SELECT DISTINCT address->>'municipality' AS m FROM assets "
            "WHERE m IS NOT NULL AND m <> '' ORDER BY m LIMIT 2000")
        municipalities = [r[0] for r in rows]
    except Exception as exc:  # pragma: no cover - empty or mid-migration store
        log.debug("municipality suggestions unavailable: %s", exc)
    return {"provinces": _ES_PROVINCES,
            "municipalities": municipalities,
            "regions": [r["key"] for r in region_catalogue()]}


@admin_router.post("/prefetch", summary="Load one or more sources now")
async def start_prefetch(payload: dict[str, Any]) -> dict[str, Any]:
    """Load sources in the background, optionally only part of them.

    ``place`` is the useful one on a big registry: a municipality or province as the
    publisher spells it, matched accent- and case-insensitively, applied before anything
    is geocoded. ``region`` takes a gazetteer key and filters on coordinates, which
    narrows what is stored but not the geocoding on an address-only source. ``limit``
    caps the records considered and works on anything.

    Runs in this process on purpose: DuckDB takes one writer and the API holds it.
    """
    from ..services.prefetch import build_filter, prefetcher, resolve_sources

    ids = payload.get("sources") or payload.get("source") or []
    if isinstance(ids, str):
        ids = [ids]
    limit = payload.get("limit")
    try:
        connectors = resolve_sources([str(i) for i in ids] or None)
        select = build_filter(place=payload.get("place"),
                              region=payload.get("region"),
                              limit=int(limit) if limit is not None else None)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not connectors:
        raise HTTPException(status_code=422, detail="No sources to load.")
    try:
        progress = prefetcher.start(get_store(), connectors, select)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"started": True, **progress.as_dict()}


@admin_router.delete("/prefetch", summary="Stop the running prefetch")
async def stop_prefetch() -> dict[str, Any]:
    """Stops after the chunk in flight. Rows already written stay - the source is left
    marked partial, so a later full load still picks it up."""
    from ..services.prefetch import prefetcher

    stopped = await prefetcher.cancel()
    return {"stopped": stopped, **prefetcher.progress.as_dict()}


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


@admin_router.delete("/keys/{prefix}", summary="Revoke, or delete, a key by prefix")
async def revoke_key(prefix: str, purge: bool = False) -> dict[str, Any]:
    """Revoke by default; ``?purge=true`` removes the row and its usage history.

    Revoking keeps the record, which is what an operator tracing a sudden 401 needs.
    Purging is for a test key, a typo, or an erasure request, where the row itself is
    the thing to be rid of.
    """
    if purge:
        ok = await key_registry.purge(get_store(), prefix)
        if not ok:
            raise HTTPException(status_code=404, detail="No key with that prefix.")
        return {"deleted": prefix}
    ok = await key_registry.revoke(get_store(), prefix)
    if not ok:
        raise HTTPException(status_code=404, detail="No active key with that prefix.")
    return {"revoked": prefix}
