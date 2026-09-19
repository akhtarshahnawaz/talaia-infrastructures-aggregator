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

    # -- loading -----------------------------------------------------------
    def load_env_keys(self) -> None:
        for entry in settings.api_key_list:
            # "label:key" or just "key"
            label, _, raw = entry.rpartition(":")
            raw = raw.strip()
            if not raw:
                continue
            self._by_hash[hash_key(raw)] = ApiKey(
                key_hash=hash_key(raw), prefix=key_prefix(raw),
                label=label or "env", rate_limit_per_min=settings.rate_limit_per_min,
                source="env")

    async def load(self, store) -> None:
        """Load env keys plus any stored keys. Mints a bootstrap key if none exist."""
        self._by_hash.clear()
        self.load_env_keys()
        try:
            rows = await store.fetch(
                "SELECT key_hash, prefix, label, rate_limit_per_min, revoked_at "
                "FROM api_keys")
            for key_hash, prefix, label, limit, revoked_at in rows:
                if revoked_at is not None:
                    continue
                self._by_hash[key_hash] = ApiKey(
                    key_hash=key_hash, prefix=prefix, label=label or "stored",
                    rate_limit_per_min=int(limit or settings.rate_limit_per_min),
                    source="store")
        except Exception as exc:  # pragma: no cover - table may not exist yet
            log.warning("could not load stored API keys: %s", exc)

        self._loaded = True
        if settings.require_auth and not self._by_hash:
            raw = generate_key()
            await self.create(store, label="bootstrap", raw=raw)
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
                     rate_limit_per_min: int | None = None) -> tuple[str, ApiKey]:
        raw = raw or generate_key()
        limit = rate_limit_per_min or settings.rate_limit_per_min
        record = ApiKey(key_hash=hash_key(raw), prefix=key_prefix(raw), label=label,
                        rate_limit_per_min=limit, source="store")
        await store.execute_write(
            "INSERT OR REPLACE INTO api_keys "
            "(key_hash, prefix, label, rate_limit_per_min, created_at, revoked_at, "
            " last_used_at, request_count) VALUES (?, ?, ?, ?, ?, NULL, NULL, 0)",
            [record.key_hash, record.prefix, label, limit,
             datetime.now(timezone.utc).replace(tzinfo=None)])
        self._by_hash[record.key_hash] = record
        log.info("api key created: %s (%s)", record.prefix, label)
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

    async def list_keys(self, store) -> list[dict]:
        rows = await store.fetch(
            "SELECT prefix, label, rate_limit_per_min, created_at, revoked_at, "
            "last_used_at, request_count FROM api_keys ORDER BY created_at DESC")
        out = [{"prefix": p, "label": lbl, "rate_limit_per_min": lim,
                "created_at": created, "revoked_at": revoked,
                "last_used_at": used, "request_count": count, "source": "store"}
               for p, lbl, lim, created, revoked, used, count in rows]
        for key in self._by_hash.values():
            if key.source == "env":
                out.append({"prefix": key.prefix, "label": key.label,
                            "rate_limit_per_min": key.rate_limit_per_min,
                            "created_at": None, "revoked_at": None,
                            "last_used_at": None, "request_count": None,
                            "source": "env"})
        return out

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

    request.state.api_key = key
    request.state.rate_remaining = remaining
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
