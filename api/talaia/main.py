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
from .routers.v1 import router as v1_router
from .store import Store, get_store, set_store

logging.basicConfig(level=logging.INFO,
                    format="%(levelname)-7s %(name)-22s %(message)s")
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

Every figure carries its method, confidence and source. Capacity is never live occupancy
and valuations are parametric estimates, not appraisals.
"""


async def _bootstrap(store) -> None:
    """Load every resident-tier connector once, in-process."""
    from .connectors.base import Tier
    for cls in registry.by_tier(Tier.RESIDENT):
        try:
            n = await cls().ingest(store)
            log.info("bootstrap: %s -> %s rows", cls.meta.id, f"{n:,}")
        except Exception as exc:
            log.error("bootstrap: %s failed: %s", cls.meta.id, exc)
    log.info("bootstrap complete: %s", await store.stats())


@asynccontextmanager
async def lifespan(app: FastAPI):
    registry.load_all()
    store = Store()
    store.connect()
    set_store(store)
    stats = await store.stats()
    log.info("TALAIA ready - %s assets, %s networks, %s cached tiles",
             f"{stats['assets']:,}", f"{stats['networks']:,}", stats["cached_tiles"])
    if stats["assets"] == 0 and settings.auto_bootstrap:
        # DuckDB is single-writer, so ingest has to happen inside the serving process:
        # a separate `python -m talaia ingest` would be locked out while the API holds
        # the file. Running it as a background task also means a cold deploy with an
        # empty volume self-heals, serving OSM-only results until the load completes.
        log.warning("store is empty - bootstrapping resident sources in the background")
        app.state.bootstrap_task = asyncio.create_task(_bootstrap(store))
    yield
    await close_client()
    store.close()


app = FastAPI(
    title="TALAIA", version="0.1.0", description=DESCRIPTION, lifespan=lifespan,
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
    return response


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):  # pragma: no cover
    log.exception("unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500,
                        content={"error": "internal_error",
                                 "detail": f"{type(exc).__name__}: {exc}"})


@app.get("/health", tags=["metadata"])
async def health():
    try:
        stats = await get_store().stats()
        return {"status": "ok", "assets": stats["assets"],
                "cached_tiles": stats["cached_tiles"]}
    except Exception as exc:
        return JSONResponse(status_code=503,
                            content={"status": "degraded", "detail": str(exc)})


app.include_router(v1_router)

# -- website ---------------------------------------------------------------
if settings.web_dist.exists():
    app.mount("/assets", StaticFiles(directory=settings.web_dist / "assets"),
              name="web-assets")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(settings.web_dist / "index.html")

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        """Serve the SPA, letting client-side routing handle unknown paths."""
        candidate = settings.web_dist / path
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(settings.web_dist / "index.html")
else:
    @app.get("/", include_in_schema=False)
    async def index_placeholder():
        return {"service": "TALAIA", "docs": "/docs",
                "note": "Website bundle not built. Run `npm run build` in web/."}
