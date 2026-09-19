"""Shared async HTTP client: retries, backoff, mirror rotation, rate limiting."""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any, Iterable

import httpx

from .config import settings

log = logging.getLogger("talaia.net")

_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.http_timeout_s, connect=10.0),
            headers={"User-Agent": settings.user_agent},
            follow_redirects=True,
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


class RateLimiter:
    """Minimum interval between calls, per logical upstream."""

    def __init__(self, min_interval_s: float = 0.0):
        self.min_interval = min_interval_s
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        if self.min_interval <= 0:
            return
        async with self._lock:
            delta = time.monotonic() - self._last
            if delta < self.min_interval:
                await asyncio.sleep(self.min_interval - delta)
            self._last = time.monotonic()


async def request(method: str, url: str, *, retries: int | None = None,
                  rate_limiter: RateLimiter | None = None, **kwargs: Any) -> httpx.Response:
    """One URL with retry + exponential backoff and jitter.

    Retries on 429/5xx and transport errors; 4xx other than 429 fail fast because
    retrying a bad request only wastes an upstream's goodwill.
    """
    attempts = settings.http_retries if retries is None else retries
    client = get_client()
    last_exc: Exception | None = None
    for attempt in range(attempts):
        if rate_limiter:
            await rate_limiter.wait()
        try:
            resp = await client.request(method, url, **kwargs)
            if resp.status_code < 400:
                return resp
            if resp.status_code == 429 or resp.status_code >= 500:
                raise httpx.HTTPStatusError(
                    f"{resp.status_code} from {url}", request=resp.request, response=resp)
            resp.raise_for_status()
        except (httpx.HTTPStatusError, httpx.TransportError, httpx.TimeoutException) as exc:
            last_exc = exc
            if attempt == attempts - 1:
                break
            backoff = min(2 ** attempt, 8) + random.random()
            log.debug("retry %s/%s for %s after %.1fs (%s)",
                      attempt + 1, attempts, url, backoff, exc)
            await asyncio.sleep(backoff)
    raise last_exc if last_exc else RuntimeError(f"request failed: {url}")


async def get_json(url: str, **kwargs: Any) -> Any:
    resp = await request("GET", url, **kwargs)
    return resp.json()


class MirrorPool:
    """Round-robins equivalent endpoints and remembers which ones are failing.

    Overpass in particular is a volunteer service: individual mirrors go down, rate-limit,
    or are unreachable from a given network. Health is tracked so a dead mirror is tried
    last rather than blocking every request behind its timeout.
    """

    def __init__(self, urls: Iterable[str], cooldown_s: float = 120.0):
        self.urls = list(urls)
        self.cooldown = cooldown_s
        self._penalty: dict[str, float] = {u: 0.0 for u in self.urls}
        self._idx = 0

    def ordered(self) -> list[str]:
        now = time.monotonic()
        healthy = [u for u in self.urls if self._penalty.get(u, 0.0) <= now]
        penalised = [u for u in self.urls if self._penalty.get(u, 0.0) > now]
        if healthy:
            self._idx = (self._idx + 1) % len(healthy)
            healthy = healthy[self._idx:] + healthy[:self._idx]
        return healthy + penalised

    def penalise(self, url: str) -> None:
        self._penalty[url] = time.monotonic() + self.cooldown

    def reward(self, url: str) -> None:
        self._penalty[url] = 0.0

    @property
    def healthy_count(self) -> int:
        now = time.monotonic()
        return sum(1 for u in self.urls if self._penalty.get(u, 0.0) <= now)

    async def post(self, *, data: Any = None, timeout: float | None = None,
                   attempts_per_mirror: int = 1) -> httpx.Response:
        """POST to the first mirror that answers. Raises only if all mirrors fail."""
        errors: list[str] = []
        for url in self.ordered():
            try:
                resp = await request("POST", url, data=data, retries=attempts_per_mirror,
                                     timeout=timeout or settings.http_timeout_s)
                self.reward(url)
                return resp
            except Exception as exc:
                self.penalise(url)
                errors.append(f"{url.split('/')[2]}: {type(exc).__name__}")
                log.debug("mirror failed %s: %s", url, exc)
        raise RuntimeError("all mirrors failed -- " + "; ".join(errors))
