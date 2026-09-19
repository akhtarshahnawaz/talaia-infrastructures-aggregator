"""API key authentication and per-key rate limiting.

Design constraints that shaped this:

* **Keys are never stored in recoverable form.** Only a SHA-256 hash is persisted, plus a
  short non-secret prefix so a key can be identified in a list or a log line without
  exposing it. A leaked database does not leak usable credentials.
* **Comparison is constant-time** (``hmac.compare_digest``) so response timing does not
  reveal how much of a guessed key was correct.
* **Secure by default.** Authentication is on unless explicitly disabled. If it is on and
  no keys exist, the service mints one at boot and prints it once, rather than silently
  starting wide open or refusing to start.
* **Two sources of truth.** Environment keys (``TALAIA_API_KEYS``) cover zero-setup
  deploys; database keys are managed at runtime through the admin endpoints, because
  DuckDB is single-writer and a separate CLI process cannot write while the API is up.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Deque

from fastapi import Depends, HTTPException, Request, Security
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from .config import settings

log = logging.getLogger("talaia.auth")

KEY_PREFIX = "talaia_sk_"
PREFIX_DISPLAY_LEN = len(KEY_PREFIX) + 6


@dataclass(frozen=True)
class Tier:
    """A quota bundle. ``0`` always means unlimited for that dimension.

    The area cap is the important one. Rate limits only slow an abuser down; an
    unbounded polygon is a single request that can pull millions of rows, hammer
    Overpass for hundreds of tiles and pin the process for minutes. Capping area is
    what actually protects the service.
    """
    name: str
    rate_limit_per_min: int
    daily_quota: int
    max_aoi_km2: float
    max_assets: int
    live_osm: bool
    description: str


TIERS: dict[str, Tier] = {
    "free": Tier(
        name="free", rate_limit_per_min=60, daily_quota=1_000, max_aoi_km2=250.0,
        max_assets=2_000, live_osm=True,
        description="Self-service. Enough for a municipality-sized incident."),
    "standard": Tier(
        name="standard", rate_limit_per_min=300, daily_quota=20_000,
        max_aoi_km2=2_500.0, max_assets=20_000, live_osm=True,
        description="Issued on request. Comarca to province scale."),
    "unlimited": Tier(
        name="unlimited", rate_limit_per_min=0, daily_quota=0, max_aoi_km2=0.0,
        max_assets=50_000, live_osm=True,
        description="Internal and integration keys. No rate, quota or area limit."),
}
DEFAULT_TIER = "free"

_TIER_FIELDS = {"rate_limit_per_min": int, "daily_quota": int, "max_aoi_km2": float,
                "max_assets": int, "live_osm": bool, "description": str}


def apply_tier_overrides(raw: str | None = None) -> list[str]:
    """Merge ``TALAIA_TIER_LIMITS`` over the built-in tiers.

    Quotas are an operational dial, not a design decision: the right free-tier area cap
    is whatever the box can serve on the day. Reading them from the environment means
    retuning is a dashboard edit and a restart, not a code change and a rebuild.

    A partial object patches an existing tier; an unknown name defines a new one on top
    of the free-tier defaults. Returns the names that changed, for logging.
    """
    import json
    from dataclasses import replace

    text = (raw if raw is not None else settings.tier_limits or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        log.error("TALAIA_TIER_LIMITS is not valid JSON (%s); built-in tiers kept", exc)
        return []
    if not isinstance(parsed, dict):
        log.error("TALAIA_TIER_LIMITS must be a JSON object; built-in tiers kept")
        return []

    changed: list[str] = []
    for name, patch in parsed.items():
        if not isinstance(patch, dict):
            log.error("TALAIA_TIER_LIMITS[%r] must be an object; ignored", name)
            continue
        key = str(name).lower()
        base = TIERS.get(key) or replace(TIERS[DEFAULT_TIER], name=key,
                                         description=f"Custom tier {key!r}.")
        fields = {}
        for field, caster in _TIER_FIELDS.items():
            if field in patch:
                try:
                    fields[field] = caster(patch[field])
                except (TypeError, ValueError):
                    log.error("TALAIA_TIER_LIMITS[%r].%s is not a %s; ignored",
                              key, field, caster.__name__)
        unknown = set(patch) - set(_TIER_FIELDS)
        if unknown:
            log.warning("TALAIA_TIER_LIMITS[%r]: ignoring unknown field(s) %s",
                        key, ", ".join(sorted(unknown)))
        if not fields:
            continue
        TIERS[key] = replace(base, name=key, **fields)
        changed.append(key)
    return changed


def get_tier(name: str | None) -> Tier:
    return TIERS.get((name or DEFAULT_TIER).lower(), TIERS[DEFAULT_TIER])


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def generate_key() -> str:
    """A new secret. 32 url-safe bytes ~= 256 bits of entropy."""
    return KEY_PREFIX + secrets.token_urlsafe(32)


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.strip().encode("utf-8")).hexdigest()


def key_prefix(raw: str) -> str:
    """Non-secret identifier shown in listings and logs."""
    return raw.strip()[:PREFIX_DISPLAY_LEN] + "..."


@dataclass
class ApiKey:
    key_hash: str
    prefix: str
    label: str
    rate_limit_per_min: int = 120
    source: str = "env"
    revoked: bool = False
    tier: str = "unlimited"
    max_aoi_km2: float = 0.0
    daily_quota: int = 0
    max_assets: int = 50_000
    email: str | None = None

    @property
    def unlimited_area(self) -> bool:
        return self.max_aoi_km2 <= 0

    def limits(self) -> dict:
        """Serialisable view of what this key may do - surfaced at /v1/me."""
        return {
            "tier": self.tier,
            "rate_limit_per_min": self.rate_limit_per_min or "unlimited",
            "daily_quota": self.daily_quota or "unlimited",
            "max_aoi_km2": self.max_aoi_km2 or "unlimited",
            "max_assets": self.max_assets,
        }


@dataclass
class _Window:
    hits: Deque[float] = field(default_factory=deque)


class KeyRegistry:
    """In-memory view of valid keys, refreshed from the store on demand."""

    def __init__(self) -> None:
        self._by_hash: dict[str, ApiKey] = {}
        self._windows: dict[str, _Window] = {}
        self._bootstrap_key: str | None = None
        self._loaded = False
        # Daily usage lives in memory and is flushed on a timer. Writing a row per
        # request would funnel every call through DuckDB's single writer and make
        # quota accounting the slowest part of the service.
        self._daily: dict[str, int] = {}
        self._daily_day: str = _today()
        self._dirty: set[str] = set()

    # -- loading -----------------------------------------------------------
    def load_env_keys(self) -> None:
        for entry in settings.api_key_list:
            # "label:key" or just "key"
            label, _, raw = entry.rpartition(":")
            raw = raw.strip()
            if not raw:
                continue
            # Keys set by the operator through the environment are deliberately
            # unlimited: they exist so the deployment owner can integrate without
            # rate-limiting themselves. TALAIA_RATE_LIMIT_PER_MIN still applies unless
            # it is set to 0. Everything else - area, quota, asset ceiling - is open.
            spec = TIERS["unlimited"]
            self._by_hash[hash_key(raw)] = ApiKey(
                key_hash=hash_key(raw), prefix=key_prefix(raw),
                label=label or "env", rate_limit_per_min=settings.rate_limit_per_min,
                source="env", tier="unlimited", max_aoi_km2=spec.max_aoi_km2,
                daily_quota=spec.daily_quota, max_assets=spec.max_assets)

    async def load(self, store) -> None:
        """Load env keys plus any stored keys. Mints a bootstrap key if none exist."""
        self._by_hash.clear()
        self.load_env_keys()
        try:
            rows = await store.fetch(
                "SELECT key_hash, prefix, label, tier, rate_limit_per_min, daily_quota, "
                "max_aoi_km2, max_assets, email, revoked_at, custom_limits FROM api_keys")
            for (key_hash, prefix, label, tier, limit, quota, max_aoi, max_assets,
                 email, revoked_at, custom) in rows:
                if revoked_at is not None:
                    continue
                spec = get_tier(tier)
                if custom:
                    # An admin set these by hand; a tier retune must not overwrite them.
                    limits = dict(rate_limit_per_min=int(limit or 0),
                                  max_aoi_km2=float(max_aoi or 0),
                                  daily_quota=int(quota or 0),
                                  max_assets=int(max_assets or spec.max_assets))
                else:
                    # Resolve from the CURRENT tier definition rather than the values
                    # frozen at creation, so raising a tier's cap moves every key on it
                    # instead of only the ones issued afterwards.
                    limits = dict(rate_limit_per_min=spec.rate_limit_per_min,
                                  max_aoi_km2=spec.max_aoi_km2,
                                  daily_quota=spec.daily_quota,
                                  max_assets=spec.max_assets)
                self._by_hash[key_hash] = ApiKey(
                    key_hash=key_hash, prefix=prefix, label=label or "stored",
                    source="store", tier=spec.name, email=email, **limits)
        except Exception as exc:  # pragma: no cover - table may not exist yet
            log.warning("could not load stored API keys: %s", exc)
        await self._restore_usage(store)

        self._loaded = True
        if settings.require_auth and not self._by_hash:
            raw = generate_key()
            # Unlimited, not the signup default: this is the operator's only credential
            # on a fresh deployment, printed once into the boot log. Handing them a key
            # that refuses anything over 250 km2 would make the first real query fail
            # with a quota error they have no obvious way to lift.
            await self.create(store, label="bootstrap", raw=raw, tier="unlimited")
            self._bootstrap_key = raw
            log.warning(
                "\n"
                "  ============================================================\n"
                "   No API keys were configured, so one has been generated.\n"
                "   This is shown ONCE and cannot be recovered:\n\n"
                "     %s\n\n"
                "   Store it now. Create more at POST /v1/admin/keys, or set\n"
                "   TALAIA_API_KEYS to manage keys through the environment.\n"
                "  ============================================================", raw)

    @property
    def bootstrap_key(self) -> str | None:
        return self._bootstrap_key

    def __len__(self) -> int:
        return len(self._by_hash)

    # -- management ---------------------------------------------------------
    async def create(self, store, *, label: str, raw: str | None = None,
                     tier: str = DEFAULT_TIER,
                     rate_limit_per_min: int | None = None,
                     max_aoi_km2: float | None = None,
                     daily_quota: int | None = None,
                     email: str | None = None, organisation: str | None = None,
                     created_ip: str | None = None) -> tuple[str, ApiKey]:
        raw = raw or generate_key()
        spec = get_tier(tier)
        custom = any(v is not None
                     for v in (rate_limit_per_min, max_aoi_km2, daily_quota))
        record = ApiKey(
            key_hash=hash_key(raw), prefix=key_prefix(raw), label=label,
            rate_limit_per_min=(rate_limit_per_min if rate_limit_per_min is not None
                                else spec.rate_limit_per_min),
            source="store", tier=spec.name,
            max_aoi_km2=(max_aoi_km2 if max_aoi_km2 is not None else spec.max_aoi_km2),
            daily_quota=(daily_quota if daily_quota is not None else spec.daily_quota),
            max_assets=spec.max_assets, email=email)
        await store.execute_write(
            "INSERT OR REPLACE INTO api_keys "
            "(key_hash, prefix, label, tier, rate_limit_per_min, daily_quota, "
            " max_aoi_km2, max_assets, custom_limits, email, organisation, created_ip, "
            " created_at, revoked_at, last_used_at, request_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 0)",
            [record.key_hash, record.prefix, label, record.tier,
             record.rate_limit_per_min, record.daily_quota, record.max_aoi_km2,
             record.max_assets, custom, email, organisation, created_ip,
             datetime.now(timezone.utc).replace(tzinfo=None)])
        self._by_hash[record.key_hash] = record
        log.info("api key created: %s tier=%s%s (%s)", record.prefix, record.tier,
                 " custom-limits" if custom else "", label)
        return raw, record

    async def revoke(self, store, prefix: str) -> bool:
        rows = await store.fetch(
            "SELECT key_hash FROM api_keys WHERE prefix = ? AND revoked_at IS NULL",
            [prefix])
        if not rows:
            return False
        for (key_hash,) in rows:
            await store.execute_write(
                "UPDATE api_keys SET revoked_at = ? WHERE key_hash = ?",
                [datetime.now(timezone.utc).replace(tzinfo=None), key_hash])
            self._by_hash.pop(key_hash, None)
        log.info("api key revoked: %s", prefix)
        return True

    async def update(self, store, prefix: str, *, tier: str | None = None,
                     rate_limit_per_min: int | None = None,
                     max_aoi_km2: float | None = None,
                     daily_quota: int | None = None) -> ApiKey | None:
        """Re-tier or re-limit an existing key in place.

        The secret is unchanged, so an upgrade does not force the holder to swap
        credentials - which is the difference between "you are now on standard" and
        "here is a new key, please redeploy".
        """
        rows = await store.fetch(
            "SELECT key_hash FROM api_keys WHERE prefix = ? AND revoked_at IS NULL",
            [prefix])
        if not rows:
            return None
        key_hash = rows[0][0]
        record = self._by_hash.get(key_hash)
        spec = get_tier(tier) if tier else get_tier(record.tier if record else None)
        custom = any(v is not None
                     for v in (rate_limit_per_min, max_aoi_km2, daily_quota))
        values = ApiKey(
            key_hash=key_hash, prefix=prefix,
            label=record.label if record else "stored", source="store", tier=spec.name,
            rate_limit_per_min=(rate_limit_per_min if rate_limit_per_min is not None
                                else spec.rate_limit_per_min),
            max_aoi_km2=(max_aoi_km2 if max_aoi_km2 is not None else spec.max_aoi_km2),
            daily_quota=(daily_quota if daily_quota is not None else spec.daily_quota),
            max_assets=spec.max_assets, email=record.email if record else None)
        await store.execute_write(
            "UPDATE api_keys SET tier = ?, rate_limit_per_min = ?, daily_quota = ?, "
            "max_aoi_km2 = ?, max_assets = ?, custom_limits = ? WHERE key_hash = ?",
            [values.tier, values.rate_limit_per_min, values.daily_quota,
             values.max_aoi_km2, values.max_assets, custom, key_hash])
        self._by_hash[key_hash] = values
        log.info("api key updated: %s -> tier=%s", prefix, values.tier)
        return values

    async def list_keys(self, store) -> list[dict]:
        rows = await store.fetch(
            "SELECT prefix, label, tier, email, created_at, revoked_at, request_count, "
            "custom_limits, email_verified FROM api_keys ORDER BY created_at DESC")
        # Report the limits actually in force, which for an uncustomised key means the
        # current tier definition rather than the columns written at creation time.
        live_by_prefix = {k.prefix: k for k in self._by_hash.values()}
        out = []
        for p, lbl, tier, email, created, revoked, count, custom, verified in rows:
            live = live_by_prefix.get(p)
            spec = get_tier(tier)
            out.append({
                "prefix": p, "label": lbl, "tier": tier,
                "rate_limit_per_min": (live.rate_limit_per_min if live
                                       else spec.rate_limit_per_min),
                "daily_quota": live.daily_quota if live else spec.daily_quota,
                "max_aoi_km2": live.max_aoi_km2 if live else spec.max_aoi_km2,
                "custom_limits": bool(custom),
                "email": email, "email_verified": bool(verified), "created_at": created, "revoked_at": revoked,
                "request_count": count, "source": "store",
                "used_today": self.used_today_by_prefix(p)})
        for key in self._by_hash.values():
            if key.source == "env":
                out.append({"prefix": key.prefix, "label": key.label, "tier": key.tier,
                            "rate_limit_per_min": key.rate_limit_per_min,
                            "daily_quota": key.daily_quota,
                            "max_aoi_km2": key.max_aoi_km2, "custom_limits": False,
                            "email": None, "email_verified": False,
                            "created_at": None, "revoked_at": None,
                            "request_count": None, "source": "env",
                            "used_today": self.used_today(key)})
        return out

    # -- daily quota ---------------------------------------------------------
    def _roll_day(self) -> None:
        today = _today()
        if today != self._daily_day:
            self._daily.clear()
            self._dirty.clear()
            self._daily_day = today

    def used_today(self, key: ApiKey) -> int:
        self._roll_day()
        return self._daily.get(key.key_hash, 0)

    def used_today_by_prefix(self, prefix: str) -> int:
        self._roll_day()
        for record in self._by_hash.values():
            if record.prefix == prefix:
                return self._daily.get(record.key_hash, 0)
        return 0

    def check_daily(self, key: ApiKey) -> tuple[bool, int, int]:
        """Returns ``(allowed, used, remaining)``. A zero quota means unlimited."""
        self._roll_day()
        if key.daily_quota <= 0:
            return True, self._daily.get(key.key_hash, 0), -1
        used = self._daily.get(key.key_hash, 0)
        if used >= key.daily_quota:
            return False, used, 0
        return True, used, key.daily_quota - used

    def record_use(self, key: ApiKey) -> None:
        self._roll_day()
        self._daily[key.key_hash] = self._daily.get(key.key_hash, 0) + 1
        self._dirty.add(key.key_hash)

    async def _restore_usage(self, store) -> None:
        """Reload today's counters so a restart does not hand everyone a fresh quota."""
        self._daily_day = _today()
        try:
            rows = await store.fetch(
                "SELECT key_hash, requests FROM key_usage WHERE day = ?",
                [self._daily_day])
            self._daily = {h: int(n or 0) for h, n in rows}
        except Exception as exc:  # pragma: no cover
            log.debug("could not restore usage counters: %s", exc)
            self._daily = {}

    async def flush_usage(self, store) -> int:
        """Persist dirty counters. Called on a timer and at shutdown."""
        if not self._dirty:
            return 0
        day = self._daily_day
        pending = list(self._dirty)
        self._dirty.clear()
        written = 0
        for key_hash in pending:
            try:
                await store.execute_write(
                    "INSERT OR REPLACE INTO key_usage (key_hash, day, requests) "
                    "VALUES (?, ?, ?)",
                    [key_hash, day, self._daily.get(key_hash, 0)])
                await store.execute_write(
                    "UPDATE api_keys SET last_used_at = ?, request_count = "
                    "COALESCE(request_count, 0) + 1 WHERE key_hash = ?",
                    [datetime.now(timezone.utc).replace(tzinfo=None), key_hash])
                written += 1
            except Exception as exc:  # pragma: no cover
                log.debug("usage flush failed for %s: %s", key_hash[:8], exc)
        return written

    # -- verification --------------------------------------------------------
    def verify(self, raw: str) -> ApiKey | None:
        """Constant-time lookup of a presented key."""
        if not raw:
            return None
        candidate = hash_key(raw)
        # Compare against every known hash rather than using a dict hit, so the work
        # done does not depend on whether a prefix matched.
        found: ApiKey | None = None
        for key_hash, record in self._by_hash.items():
            if hmac.compare_digest(candidate, key_hash):
                found = record
        return found

    def check_rate(self, key: ApiKey) -> tuple[bool, int, int]:
        """Sliding 60 s window. Returns ``(allowed, remaining, retry_after_s)``."""
        limit = key.rate_limit_per_min
        if limit <= 0:
            return True, -1, 0
        now = time.monotonic()
        window = self._windows.setdefault(key.key_hash, _Window())
        while window.hits and now - window.hits[0] > 60.0:
            window.hits.popleft()
        if len(window.hits) >= limit:
            retry = int(61 - (now - window.hits[0]))
            return False, 0, max(retry, 1)
        window.hits.append(now)
        return True, limit - len(window.hits), 0


registry = KeyRegistry()


def _present_key(request: Request, x_api_key: str | None,
                 authorization: str | None) -> str | None:
    """Accept either ``X-API-Key: <key>`` or ``Authorization: Bearer <key>``."""
    if x_api_key:
        return x_api_key.strip()
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    # Query parameters are deliberately NOT accepted: they end up in access logs,
    # browser history and referrer headers.
    return None


# Declared through fastapi.security so both schemes are advertised in the OpenAPI
# document and Swagger UI renders an Authorize button.
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False,
                               description="Your TALAIA API key")
_bearer = HTTPBearer(auto_error=False, description="Bearer <your TALAIA API key>")
_admin_header = APIKeyHeader(name="X-Admin-Key", auto_error=False,
                             description="Administrative key for managing API keys")


async def require_api_key(
    request: Request,
    x_api_key: str | None = Security(_api_key_header),
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> ApiKey | None:
    """FastAPI dependency guarding every data endpoint."""
    if not settings.require_auth:
        return None

    raw = _present_key(request, x_api_key,
                       f"Bearer {credentials.credentials}" if credentials else None)
    if not raw:
        raise HTTPException(
            status_code=401,
            detail="Missing API key. Send it as 'X-API-Key: <key>' or "
                   "'Authorization: Bearer <key>'.",
            headers={"WWW-Authenticate": "Bearer"})

    key = registry.verify(raw)
    if key is None:
        # Log the prefix only - never the key itself.
        log.warning("rejected API key %s from %s", key_prefix(raw),
                    request.client.host if request.client else "unknown")
        raise HTTPException(status_code=401, detail="Invalid or revoked API key.",
                            headers={"WWW-Authenticate": "Bearer"})

    allowed, remaining, retry_after = registry.check_rate(key)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit of {key.rate_limit_per_min} requests/minute exceeded.",
            headers={"Retry-After": str(retry_after)})

    day_ok, used, day_remaining = registry.check_daily(key)
    if not day_ok:
        raise HTTPException(
            status_code=429,
            detail=(f"Daily quota of {key.daily_quota:,} requests exhausted for the "
                    f"'{key.tier}' tier. It resets at 00:00 UTC."),
            headers={"Retry-After": "3600"})

    registry.record_use(key)
    request.state.api_key = key
    request.state.rate_remaining = remaining
    request.state.daily_remaining = day_remaining
    return key


async def require_admin_key(
    x_admin_key: str | None = Security(_admin_header),
) -> bool:
    """Guards key management. Disabled entirely unless an admin key is configured."""
    if not settings.admin_key:
        raise HTTPException(
            status_code=404,
            detail="Key management is disabled. Set TALAIA_ADMIN_KEY to enable it.")
    if not x_admin_key or not hmac.compare_digest(
            hash_key(x_admin_key), hash_key(settings.admin_key)):
        raise HTTPException(status_code=401, detail="Invalid admin key.",
                            headers={"WWW-Authenticate": "Bearer"})
    return True
