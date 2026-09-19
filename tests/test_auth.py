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
