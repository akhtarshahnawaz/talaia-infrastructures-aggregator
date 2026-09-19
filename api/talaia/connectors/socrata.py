"""Socrata (SODA 2.0) paging helper.

The Catalan open-data portal serves every dataset used here through Socrata, so paging,
server-side aggregation and filtering are shared rather than repeated six times.
"""
from __future__ import annotations

import logging
from typing import Any, AsyncIterator

from ..config import settings
from ..net import RateLimiter, get_json

log = logging.getLogger("talaia.socrata")

# Socrata throttles anonymous clients; one request at a time with a small gap is polite
# and still fast enough to pull 30k rows in a couple of seconds.
_limiter = RateLimiter(0.15)


def _headers() -> dict[str, str]:
    return {"X-App-Token": settings.socrata_app_token} if settings.socrata_app_token else {}


async def query(dataset: str, *, select: str | None = None, where: str | None = None,
                group: str | None = None, order: str | None = None,
                limit: int = 50_000) -> list[dict]:
    """Single (usually aggregated) query."""
    params: dict[str, Any] = {"$limit": limit}
    if select:
        params["$select"] = select
    if where:
        params["$where"] = where
    if group:
        params["$group"] = group
    if order:
        params["$order"] = order
    url = f"{settings.socrata_base}/{dataset}.json"
    return await get_json(url, params=params, headers=_headers(), rate_limiter=_limiter)


async def pages(dataset: str, *, select: str | None = None, where: str | None = None,
                order: str = ":id", page_size: int = 25_000) -> AsyncIterator[dict]:
    """Yield every row, paging with a stable ``$order`` so rows are never skipped.

    Socrata does not guarantee ordering without ``$order``, which silently produces
    duplicated and missing rows across pages.
    """
    offset = 0
    url = f"{settings.socrata_base}/{dataset}.json"
    while True:
        params: dict[str, Any] = {"$limit": page_size, "$offset": offset, "$order": order}
        if select:
            params["$select"] = select
        if where:
            params["$where"] = where
        rows = await get_json(url, params=params, headers=_headers(), rate_limiter=_limiter)
        if not rows:
            return
        for row in rows:
            yield row
        if len(rows) < page_size:
            return
        offset += page_size


async def latest_value(dataset: str, column: str) -> str | None:
    """Most recent value of a column - used to pin datasets to their current edition."""
    rows = await query(dataset, select=column, group=column, order=f"{column} DESC", limit=1)
    return rows[0].get(column) if rows else None
