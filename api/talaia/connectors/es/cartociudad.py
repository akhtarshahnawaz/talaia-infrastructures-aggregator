"""CartoCiudad geocoding (IGN) - Tier C enrichment.

Several Spanish registries publish an address but no coordinates; the social-services
registry RESES is the important case, because residential care homes are the single
highest-priority asset class in a wildfire evacuation and would otherwise be invisible.

Results are cached permanently by normalised query: an address does not move, and the
service is a public good that should not be hammered. Every result keeps the geocoder's
own quality signal so downstream confidence reflects how the point was obtained.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from typing import Any

from ...config import settings
from ...net import RateLimiter, request
from ...norm import clean_text, valid_lonlat

log = logging.getLogger("talaia.cartociudad")

_limiter = RateLimiter(0.05)
_JSONP = re.compile(r"^[\w.]+\((.*)\)\s*;?\s*$", re.S)

# CartoCiudad `type` values, best to worst. A portal is a street number; a municipio is
# a town centroid and must never be presented as a facility location.
QUALITY = {"portal": 0.9, "Portal": 0.9, "callejero": 0.75, "Callejero": 0.75,
           "poblacion": 0.45, "Municipio": 0.3, "municipio": 0.3,
           "provincia": 0.1, "Comunidad autonoma": 0.05}


def _cache_key(query: str) -> str:
    return "geocode:" + hashlib.sha1(query.strip().lower().encode("utf-8")).hexdigest()[:24]


def _parse(body: str) -> dict | None:
    body = body.strip()
    m = _JSONP.match(body)
    if m:
        body = m.group(1)
    try:
        data = json.loads(body)
    except ValueError:
        return None
    if isinstance(data, dict) and data.get("type") == "FeatureCollection":
        feats = data.get("features") or []
        if not feats:
            return None
        f = feats[0]
        props = f.get("properties") or {}
        coords = (f.get("geometry") or {}).get("coordinates") or []
        if len(coords) < 2:
            return None
        return {"lon": coords[0], "lat": coords[1], **props}
    if isinstance(data, list) and data:
        data = data[0]
    if isinstance(data, dict) and data.get("lat") is not None:
        return data
    return None


def _to_result(parsed: dict | None) -> dict | None:
    if not parsed:
        return None
    ll = valid_lonlat(parsed.get("lng") or parsed.get("lon"), parsed.get("lat"))
    if not ll:
        return None
    gtype = parsed.get("type") or parsed.get("tip_via") or "unknown"
    return {
        "lon": ll[0], "lat": ll[1],
        "quality": QUALITY.get(str(gtype), 0.4),
        "match_type": str(gtype),
        "address": clean_text(parsed.get("address")),
        "municipality": clean_text(parsed.get("muni")),
        "province": clean_text(parsed.get("province")),
        "postcode": clean_text(parsed.get("postalCode")),
        # A cadastral reference is a free hard identifier for conflation and unlocks
        # building geometry from the cadastre.
        "cadastral_ref": clean_text(parsed.get("refCatastral")),
        "geocoder": "cartociudad",
    }


async def geocode(query: str, store=None) -> dict | None:
    """Geocode one free-text Spanish address. Returns None on failure, never raises."""
    query = clean_text(query) or ""
    if len(query) < 5:
        return None
    key = _cache_key(query)
    if store is not None:
        cached = await store.cache_get(key)
        if cached is not None:
            return cached or None

    result: dict | None = None
    upstream_answered = False
    try:
        resp = await request(
            "GET", f"{settings.cartociudad_base}/findJsonp",
            params={"q": query, "outputformat": "geojson"},
            rate_limiter=_limiter, retries=2)
        upstream_answered = True
        result = _to_result(_parse(resp.text))
    except Exception as exc:
        log.debug("geocode failed for %r: %s", query[:60], exc)

    # Fall back to the municipality alone, flagged as the low-quality match it is.
    if result is None and upstream_answered and "," in query:
        municipality = query.rsplit(",", 1)[-1].strip()
        if len(municipality) > 2:
            try:
                resp = await request(
                    "GET", f"{settings.cartociudad_base}/findJsonp",
                    params={"q": municipality, "outputformat": "geojson"},
                    rate_limiter=_limiter, retries=1)
                fallback = _to_result(_parse(resp.text))
                if fallback:
                    fallback["quality"] = min(fallback.get("quality", 0.3), 0.3)
                    fallback["match_type"] = "municipality_fallback"
                    result = fallback
            except Exception:
                pass

    if store is not None and (result or upstream_answered):
        # Only cache a negative when the service genuinely answered "no match".
        # Caching transport failures would permanently poison the cache for addresses
        # that are perfectly resolvable once the network recovers.
        await store.cache_put(key, "geocode", result or {})
    return result


async def geocode_many(queries: list[str], store=None, concurrency: int = 8
                       ) -> list[dict | None]:
    """Geocode a batch with bounded concurrency."""
    sem = asyncio.Semaphore(concurrency)

    async def one(q: str):
        async with sem:
            return await geocode(q, store)

    return await asyncio.gather(*(one(q) for q in queries))
