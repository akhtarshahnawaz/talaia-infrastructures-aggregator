"""TALAIA application entrypoint.

Serves the API and the documentation website from one process, so there is one URL,
one deployment and no CORS story.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .connectors import registry
from .net import close_client
from .auth import apply_tier_overrides, registry as key_registry
from .mcp import mcp_router
from .routers.v1 import (admin_router, meta_router, public_router,
                         router as v1_router)
from .store import Store, StoreBusy, get_store, set_store

logging.basicConfig(level=logging.INFO,
                    format="%(levelname)-7s %(name)-22s %(message)s")
# httpx logs every request at INFO. A cold national ingest geocodes ~51,000 addresses,
# so that is ~65,000 lines of "HTTP/1.1 200" on stderr - which buries the handful of
# lines that matter and, on a hosted platform, is enough to hit a log rate limit and
# start dropping them. Our own connectors log what was fetched and how it went.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("talaia")

DESCRIPTION = """
**TALAIA** turns a polygon into a structured inventory of everything at risk inside it.

Named after the hilltop watchtowers that Mediterranean communities have used for
centuries to spot fire and raise the alarm.

Give it a fire perimeter - or a set of time-banded predicted perimeters from a spread
model - and it returns the schools, hospitals, care homes, farms, livestock, campsites,
industry, power, roads and people inside, each with contact details, a capacity estimate,
a replacement-cost valuation, a triage score and full source provenance.

* `POST /v1/exposure` - the full report
* `POST /v1/exposure/summary` - aggregates only, for agent loops
* `POST /v1/assets` - NDJSON stream
* `GET  /v1/sources` - live data-source catalogue
* `GET  /v1/taxonomy` - the classification vocabulary

## Authentication

Data endpoints require an API key, sent as either header:

```
X-API-Key: talaia_sk_...
Authorization: Bearer talaia_sk_...
```

Keys are rate limited per minute; `X-RateLimit-Remaining` reports the budget left.
`/v1/sources`, `/v1/taxonomy` and `/v1/stats` describe the service rather than returning
exposure data and are open by default.

Every figure carries its method, confidence and source. Capacity is never live occupancy
and valuations are parametric estimates, not appraisals.
"""


async def _flush_usage_loop(store) -> None:
    """Persist per-key usage counters on a timer.

    Counters live in memory so quota accounting costs nothing per request; this task is
    what makes them survive a restart, and the shutdown path flushes once more.
    """
    while True:
        try:
            await asyncio.sleep(60)
            await key_registry.flush_usage(store)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover
            log.warning("usage flush failed: %s", exc)


# A source is finished when its last run said so. "loaded" covers rows written before
# runs were recorded; anything else - never run, interrupted part-way, failed - is work
# still to do.
_COMPLETE = {"ok", "loaded"}


async def _pending_sources(store) -> list:
    """Resident connectors that are not fully loaded.

    Checked per source rather than "is the store empty", because the bootstrap runs as a
    background task: a redeploy part-way through leaves some sources loaded and the rest
    at zero, and an emptiness check then decides everything is fine and never finishes
    the job. That is how a deployment sits at three sources of twelve indefinitely.

    Having rows is not enough either, now that a long ingest writes them as it goes: a
    source interrupted at address 30,000 of 51,000 holds rows and is still unfinished.
    The run status is what distinguishes the two, and re-running is safe because assets
    upsert by id and the geocode cache makes a second pass cheap.
    """
    from .connectors.base import Tier

    try:
        stats = await store.source_stats()
    except Exception:  # pragma: no cover - tables may not exist yet
        stats = {}
    pending = []
    for c in registry.by_tier(Tier.RESIDENT):
        entry = stats.get(c.meta.id)
        if not entry or not entry.get("rows"):
            pending.append(c)
        elif entry.get("last_status") not in _COMPLETE:
            pending.append(c)
    return pending


async def _bootstrap(store, connectors=None) -> None:
    """Load resident-tier connectors in-process."""
    from .connectors.base import Tier
    for cls in (connectors if connectors is not None
                else registry.by_tier(Tier.RESIDENT)):
        try:
            n = await cls().ingest(store)
            log.info("bootstrap: %s -> %s rows", cls.meta.id, f"{n:,}")
        except Exception as exc:
            log.error("bootstrap: %s failed: %s", cls.meta.id, exc)
    log.info("bootstrap complete: %s", await store.stats())


async def _warm_on_boot(store) -> None:
    """Pre-load the tile cache for the regions named in TALAIA_WARM_ON_BOOT.

    Deliberately in the background and deliberately after the service is already
    answering: a cold region degrades latency, it does not break anything, so there is
    no reason to make the deployment wait for it.
    """
    from .regions import resolve_many
    from .services.warm import warmer

    try:
        regions = resolve_many(settings.warm_on_boot_list)
    except KeyError as exc:
        log.error("TALAIA_WARM_ON_BOOT: %s", exc.args[0])
        return
    log.info("warming %s on boot", ", ".join(r.name for r in regions))
    warmer.start(store, regions)


@asynccontextmanager
async def lifespan(app: FastAPI):
    registry.load_all()
    changed = apply_tier_overrides()
    if changed:
        log.info("tier limits overridden from the environment: %s", ", ".join(changed))
    store = Store()
    store.connect()
    set_store(store)
    await key_registry.load(store)
    if settings.require_auth:
        log.info("authentication ENABLED - %d key(s) active", len(key_registry))
    else:
        log.warning("authentication DISABLED - every endpoint is open "
                    "(set TALAIA_REQUIRE_AUTH=true to gate the API)")
    if settings.allow_signup:
        from . import mailer
        if not settings.require_email_verification:
            log.warning("signup is OPEN and email verification is DISABLED - addresses "
                        "on issued keys are unchecked")
        elif not mailer.available():
            log.error("signup is enabled and requires email verification, but no mail "
                      "sender is configured - every signup will return 503. Set "
                      "TALAIA_RESEND_API_KEY or the TALAIA_SMTP_* variables.")
        else:
            log.info("mail backend: %s, sending as %s",
                     mailer.backend(), mailer._from_address())
            if mailer.using_shared_sender():
                log.warning("using Resend's shared sender; it only delivers to the "
                            "account owner. Verify a domain and set TALAIA_EMAIL_FROM "
                            "before opening signup to the public.")
    stats = await store.stats()
    log.info("TALAIA ready - %s assets, %s networks, %s cached tiles",
             f"{stats['assets']:,}", f"{stats['networks']:,}", stats["cached_tiles"])
    if settings.auto_bootstrap:
        # DuckDB is single-writer, so ingest has to happen inside the serving process:
        # a separate `python -m talaia ingest` would be locked out while the API holds
        # the file. Running it as a background task also means a cold deploy with an
        # empty volume self-heals, serving partial results until the load completes.
        pending = await _pending_sources(store)
        if pending:
            log.warning("bootstrapping %d source(s) not fully loaded yet: %s",
                        len(pending), ", ".join(c.meta.id for c in pending))
            app.state.bootstrap_task = asyncio.create_task(_bootstrap(store, pending))
    if settings.warm_on_boot_list:
        await _warm_on_boot(store)
    flusher = asyncio.create_task(_flush_usage_loop(store))
    yield
    flusher.cancel()
    from .services.warm import warmer
    await warmer.cancel()
    await key_registry.flush_usage(store)
    await close_client()
    store.close()


app = FastAPI(
    title="TALAIA", version="0.1.0", description=DESCRIPTION, lifespan=lifespan,
    # The website owns /docs; the generated OpenAPI UI lives at /swagger so the two
    # do not fight over the same path.
    docs_url="/swagger", redoc_url="/redoc", openapi_url="/openapi.json",
    contact={"name": "TALAIA", "url": "https://github.com/"},
    license_info={"name": "See /v1/sources for per-dataset licences"},
    openapi_tags=[
        {"name": "exposure", "description": "Polygon in, values at risk out."},
        {"name": "metadata", "description": "Sources, taxonomy and store status."},
        {"name": "utilities", "description": "Geocoding and helpers."},
    ],
)
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origin_list,
                   allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def timing_and_request_id(request: Request, call_next):
    rid = request.headers.get("x-request-id") or f"req_{uuid.uuid4().hex[:12]}"
    start = time.perf_counter()
    response = await call_next(request)
    response.headers["x-request-id"] = rid
    response.headers["x-response-time-ms"] = f"{(time.perf_counter()-start)*1000:.1f}"
    # Surface the caller's remaining budget so clients can pace themselves.
    key = getattr(request.state, "api_key", None)
    if key is not None:
        response.headers["x-ratelimit-limit"] = str(key.rate_limit_per_min)
        response.headers["x-ratelimit-remaining"] = str(
            getattr(request.state, "rate_remaining", 0))
        day_left = getattr(request.state, "daily_remaining", -1)
        if day_left >= 0:
            response.headers["x-quota-remaining-today"] = str(day_left)
    return response


@app.exception_handler(StoreBusy)
async def store_busy(request: Request, exc: StoreBusy):
    """503, not 500: the request was refused, not mishandled, and retrying may work.

    The message names what is holding the writer, because "nothing happened when I
    pressed the button" is the least actionable bug report there is.
    """
    log.error("write refused on %s: %s", request.url.path, exc)
    return JSONResponse(status_code=503, content={"detail": str(exc)},
                        headers={"Retry-After": "30"})


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):  # pragma: no cover
    """Log the detail, return a reference.

    Echoing the exception text told a caller which geometry library we use and what it
    objected to, which is of no use to them and of some use to someone else.
    """
    rid = request.headers.get("x-request-id") or f"req_{uuid.uuid4().hex[:12]}"
    log.exception("unhandled error on %s [%s]", request.url.path, rid)
    return JSONResponse(
        status_code=500,
        content={"error": "internal_error", "request_id": rid,
                 "detail": "An unexpected error occurred. Quote the request_id if you "
                           "report this."},
        headers={"x-request-id": rid})


@app.get("/health", tags=["metadata"])
async def health():
    try:
        stats = await get_store().stats()
        return {"status": "ok", "assets": stats["assets"],
                "cached_tiles": stats["cached_tiles"],
                "failed_tiles": stats.get("failed_tiles", 0)}
    except Exception as exc:
        return JSONResponse(status_code=503,
                            content={"status": "degraded", "detail": str(exc)})


app.include_router(v1_router)
app.include_router(meta_router)
app.include_router(public_router)
app.include_router(admin_router)
# Mounted on the same app so MCP clients authenticate with the same key and consume
# the same rate limit and quota as REST callers.
app.include_router(mcp_router)

# -- website ---------------------------------------------------------------
if settings.web_dist.exists():
    app.mount("/assets", StaticFiles(directory=settings.web_dist / "assets"),
              name="web-assets")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(settings.web_dist / "index.html")

    # Paths the single-page app knows how to render. Anything else is a typo or a dead
    # link, and answering 200 for it makes those indistinguishable from real pages to a
    # crawler or an uptime check.
    SPA_ROUTES = {"", "playground", "docs", "agents", "sources", "roadmap",
                  "methodology", "signup", "verify", "admin"}

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        """Serve the SPA. Unknown paths still render it, but with a 404 status."""
        candidate = settings.web_dist / path
        if candidate.is_file():
            return FileResponse(candidate)
        known = path.strip("/").split("/")[0] in SPA_ROUTES
        return FileResponse(settings.web_dist / "index.html",
                            status_code=200 if known else 404)
else:
    @app.get("/", include_in_schema=False)
    async def index_placeholder():
        return {"service": "TALAIA", "docs": "/docs",
                "note": "Website bundle not built. Run `npm run build` in web/."}
