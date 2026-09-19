"""API key authentication, rate limiting and key management."""
import pytest
from fastapi import HTTPException

from talaia.auth import (ApiKey, KeyRegistry, generate_key, hash_key, key_prefix,
                         require_admin_key)
from talaia.config import settings
from talaia.store import Store


@pytest.fixture
async def store(tmp_path):
    s = Store(tmp_path / "auth.duckdb")
    s.connect()
    yield s
    s.close()


def test_generated_keys_are_unguessable_and_prefixed():
    keys = {generate_key() for _ in range(200)}
    assert len(keys) == 200, "no collisions"
    k = next(iter(keys))
    assert k.startswith("talaia_sk_")
    assert len(k) > 40


def test_only_the_hash_is_a_credential():
    k = generate_key()
    h = hash_key(k)
    assert k not in h
    assert hash_key(k) == hash_key(k)
    assert hash_key(k) != hash_key(generate_key())
    # The displayed prefix must not be enough to reconstruct the key.
    assert len(key_prefix(k)) < len(k)
    assert not k.startswith(key_prefix(k).replace("...", "") + "X")


async def test_unknown_and_revoked_keys_are_rejected(store):
    reg = KeyRegistry()
    await store.execute_write(
        "CREATE TABLE IF NOT EXISTS api_keys (key_hash VARCHAR PRIMARY KEY, "
        "prefix VARCHAR, label VARCHAR, rate_limit_per_min INTEGER, "
        "created_at TIMESTAMP, revoked_at TIMESTAMP, last_used_at TIMESTAMP, "
        "request_count BIGINT)")
    raw, record = await reg.create(store, label="test")

    assert reg.verify(raw) is not None
    assert reg.verify("talaia_sk_not_a_real_key") is None
    assert reg.verify("") is None

    assert await reg.revoke(store, record.prefix) is True
    assert reg.verify(raw) is None, "a revoked key must stop working immediately"


async def test_bootstrap_key_is_minted_when_none_exist(store, monkeypatch):
    monkeypatch.setattr(settings, "require_auth", True)
    monkeypatch.setattr(settings, "api_keys", "")
    reg = KeyRegistry()
    await reg.load(store)
    assert reg.bootstrap_key, "must not start with no way in"
    assert reg.verify(reg.bootstrap_key) is not None
    assert len(reg) == 1


async def test_env_keys_are_accepted(store, monkeypatch):
    raw = generate_key()
    monkeypatch.setattr(settings, "require_auth", True)
    monkeypatch.setattr(settings, "api_keys", f"agent:{raw}")
    reg = KeyRegistry()
    await reg.load(store)
    key = reg.verify(raw)
    assert key is not None and key.label == "agent" and key.source == "env"
    assert reg.bootstrap_key is None, "no bootstrap needed when env keys exist"


def test_rate_limit_allows_then_blocks():
    reg = KeyRegistry()
    key = ApiKey(key_hash="h", prefix="p", label="l", rate_limit_per_min=3)
    assert [reg.check_rate(key)[0] for _ in range(3)] == [True, True, True]
    allowed, remaining, retry_after = reg.check_rate(key)
    assert allowed is False and remaining == 0 and retry_after >= 1


def test_rate_limit_is_per_key():
    reg = KeyRegistry()
    a = ApiKey(key_hash="a", prefix="a", label="a", rate_limit_per_min=1)
    b = ApiKey(key_hash="b", prefix="b", label="b", rate_limit_per_min=1)
    assert reg.check_rate(a)[0] is True
    assert reg.check_rate(a)[0] is False
    assert reg.check_rate(b)[0] is True, "one key's budget must not affect another's"


def test_unlimited_when_limit_is_zero():
    reg = KeyRegistry()
    key = ApiKey(key_hash="h", prefix="p", label="l", rate_limit_per_min=0)
    assert all(reg.check_rate(key)[0] for _ in range(50))


async def test_key_listing_never_contains_secrets(store):
    reg = KeyRegistry()
    await store.execute_write(
        "CREATE TABLE IF NOT EXISTS api_keys (key_hash VARCHAR PRIMARY KEY, "
        "prefix VARCHAR, label VARCHAR, rate_limit_per_min INTEGER, "
        "created_at TIMESTAMP, revoked_at TIMESTAMP, last_used_at TIMESTAMP, "
        "request_count BIGINT)")
    raw, _ = await reg.create(store, label="secret-holder")
    listed = await reg.list_keys(store)
    blob = repr(listed)
    assert raw not in blob
    assert hash_key(raw) not in blob, "hashes are credentials-adjacent; do not expose them"


async def test_admin_endpoint_disabled_without_a_configured_key(monkeypatch):
    monkeypatch.setattr(settings, "admin_key", None)
    with pytest.raises(HTTPException) as exc:
        await require_admin_key(x_admin_key="anything")
    assert exc.value.status_code == 404


async def test_admin_rejects_a_wrong_key(monkeypatch):
    monkeypatch.setattr(settings, "admin_key", "correct-admin")
    with pytest.raises(HTTPException) as exc:
        await require_admin_key(x_admin_key="wrong-admin")
    assert exc.value.status_code == 401
    assert await require_admin_key(x_admin_key="correct-admin") is True


# ---------------------------------------------------------------------------
# Tiers, quotas and the area cap
# ---------------------------------------------------------------------------
from talaia.auth import TIERS, get_tier  # noqa: E402
from talaia.models import ExposureRequest  # noqa: E402
from talaia.routers.v1 import _enforce_key_limits  # noqa: E402

BIG = {"type": "Polygon", "coordinates": [[[1.0, 41.0], [2.5, 41.0], [2.5, 42.2],
                                           [1.0, 42.2], [1.0, 41.0]]]}       # ~19,000 km2
SMALL = {"type": "Polygon", "coordinates": [[[1.80, 41.70], [1.85, 41.70], [1.85, 41.75],
                                             [1.80, 41.75], [1.80, 41.70]]]}  # ~23 km2


def _key(tier: str) -> ApiKey:
    spec = get_tier(tier)
    return ApiKey(key_hash="h", prefix="p", label="l", tier=spec.name,
                  rate_limit_per_min=spec.rate_limit_per_min,
                  daily_quota=spec.daily_quota, max_aoi_km2=spec.max_aoi_km2,
                  max_assets=spec.max_assets)


def test_tier_table_is_coherent():
    free, std, unlimited = TIERS["free"], TIERS["standard"], TIERS["unlimited"]
    assert free.max_aoi_km2 < std.max_aoi_km2
    assert unlimited.max_aoi_km2 == 0, "0 means unlimited"
    assert free.daily_quota < std.daily_quota
    assert unlimited.daily_quota == 0 and unlimited.rate_limit_per_min == 0
    assert get_tier("nonsense").name == "free", "unknown tiers fall back to the safest"
    assert get_tier(None).name == "free"


def test_free_tier_is_blocked_above_its_area_limit():
    with pytest.raises(HTTPException) as exc:
        _enforce_key_limits(ExposureRequest(aoi=BIG), _key("free"))
    assert exc.value.status_code == 403
    assert "above the" in exc.value.detail and "free" in exc.value.detail


def test_free_tier_allows_a_small_area():
    req = _enforce_key_limits(ExposureRequest(aoi=SMALL), _key("free"))
    assert req.max_assets == TIERS["free"].max_assets


def test_unlimited_tier_bypasses_the_area_cap():
    key = _key("unlimited")
    assert key.unlimited_area
    req = _enforce_key_limits(ExposureRequest(aoi=BIG), key)
    assert req.max_assets == TIERS["unlimited"].max_assets


def test_buffer_counts_towards_the_area_limit():
    """A caller must not be able to inflate the AOI past the cap with buffer_m."""
    key = _key("free")
    _enforce_key_limits(ExposureRequest(aoi=SMALL), key)          # fine bare
    with pytest.raises(HTTPException) as exc:
        _enforce_key_limits(ExposureRequest(aoi=SMALL, buffer_m=20_000), key)
    assert exc.value.status_code == 403


def test_max_assets_is_clamped_down_never_up():
    key = _key("free")
    asked_high = _enforce_key_limits(
        ExposureRequest(aoi=SMALL, max_assets=50_000), key)
    assert asked_high.max_assets == TIERS["free"].max_assets
    asked_low = _enforce_key_limits(ExposureRequest(aoi=SMALL, max_assets=10), key)
    assert asked_low.max_assets == 10, "a caller may still ask for fewer"


def test_no_key_means_no_limits_applied():
    req = ExposureRequest(aoi=BIG, max_assets=50_000)
    assert _enforce_key_limits(req, None) is req


async def test_created_keys_carry_their_tier_limits(store):
    reg = KeyRegistry()
    await store.execute_write(
        "CREATE TABLE IF NOT EXISTS api_keys (key_hash VARCHAR PRIMARY KEY, "
        "prefix VARCHAR, label VARCHAR, tier VARCHAR, rate_limit_per_min INTEGER, "
        "daily_quota INTEGER, max_aoi_km2 DOUBLE, max_assets INTEGER, email VARCHAR, "
        "organisation VARCHAR, created_ip VARCHAR, created_at TIMESTAMP, "
        "revoked_at TIMESTAMP, last_used_at TIMESTAMP, request_count BIGINT)")
    raw_free, free = await reg.create(store, label="signup", tier="free",
                                      email="a@b.example")
    raw_unl, unl = await reg.create(store, label="internal", tier="unlimited")

    assert free.max_aoi_km2 == TIERS["free"].max_aoi_km2
    assert unl.unlimited_area and unl.rate_limit_per_min == 0

    # Limits must survive a reload from storage, not just live in memory.
    reloaded = KeyRegistry()
    await reloaded.load(store)
    again = reloaded.verify(raw_free)
    assert again is not None and again.tier == "free"
    assert again.max_aoi_km2 == TIERS["free"].max_aoi_km2


async def test_usage_counters_survive_a_restart(store):
    reg = KeyRegistry()
    await store.execute_write(
        "CREATE TABLE IF NOT EXISTS api_keys (key_hash VARCHAR PRIMARY KEY, "
        "prefix VARCHAR, label VARCHAR, tier VARCHAR, rate_limit_per_min INTEGER, "
        "daily_quota INTEGER, max_aoi_km2 DOUBLE, max_assets INTEGER, email VARCHAR, "
        "organisation VARCHAR, created_ip VARCHAR, created_at TIMESTAMP, "
        "revoked_at TIMESTAMP, last_used_at TIMESTAMP, request_count BIGINT)")
    raw, record = await reg.create(store, label="quota", tier="free")
    for _ in range(7):
        reg.record_use(record)
    await reg.flush_usage(store)

    restarted = KeyRegistry()
    await restarted.load(store)
    key = restarted.verify(raw)
    assert restarted.used_today(key) == 7, \
        "a restart must not hand everyone a fresh daily quota"
