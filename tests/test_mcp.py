"""MCP protocol conformance and, above all, that MCP is gated like the REST API.

The point of these tests is the second part. An MCP server that quietly bypassed the
area cap would be a hole in exactly the control the tiers exist to enforce, and it would
be invisible - the tool would simply answer.
"""
import math

import pytest
from fastapi import HTTPException

from talaia.auth import TIERS, ApiKey, get_tier
from talaia.mcp import (SUPPORTED_PROTOCOLS, TOOL_SPECS, _MCP_ASSET_CAP, _aoi_from,
                        _circle, _eur, _request_from, dispatch)
from talaia.geo import area_km2, parse_aoi


def _key(tier: str) -> ApiKey:
    spec = get_tier(tier)
    return ApiKey(key_hash="h", prefix="talaia_sk_test...", label="t", tier=spec.name,
                  rate_limit_per_min=spec.rate_limit_per_min,
                  max_aoi_km2=spec.max_aoi_km2, daily_quota=spec.daily_quota,
                  max_assets=spec.max_assets, source="store")


def _rpc(method: str, params=None, rpc_id=1):
    msg = {"jsonrpc": "2.0", "method": method}
    if rpc_id is not None:
        msg["id"] = rpc_id
    if params is not None:
        msg["params"] = params
    return msg


# -- protocol ---------------------------------------------------------------
async def test_initialize_advertises_tools_and_echoes_a_known_protocol():
    out = await dispatch(_rpc("initialize", {"protocolVersion": "2025-03-26"}), None)
    assert out["jsonrpc"] == "2.0" and out["id"] == 1
    result = out["result"]
    assert result["protocolVersion"] == "2025-03-26", "echo what the client asked for"
    assert "tools" in result["capabilities"]
    assert result["serverInfo"]["name"] == "talaia"
    assert "not live occupancy" in result["instructions"]


async def test_an_unknown_protocol_falls_back_to_our_newest():
    out = await dispatch(_rpc("initialize", {"protocolVersion": "1999-01-01"}), None)
    assert out["result"]["protocolVersion"] == SUPPORTED_PROTOCOLS[0]


async def test_notifications_get_no_response():
    assert await dispatch(_rpc("notifications/initialized", rpc_id=None), None) is None
    # An unknown notification must also stay silent: replying to one is a protocol error.
    assert await dispatch(_rpc("notifications/nonsense", rpc_id=None), None) is None


async def test_unknown_method_with_an_id_returns_an_error():
    out = await dispatch(_rpc("tools/teleport"), None)
    assert out["error"]["code"] == -32601


async def test_non_jsonrpc_2_is_refused():
    out = await dispatch({"jsonrpc": "1.0", "id": 1, "method": "ping"}, None)
    assert out["error"]["code"] == -32600


async def test_ping_answers_empty():
    assert (await dispatch(_rpc("ping"), None))["result"] == {}


async def test_tools_list_is_well_formed():
    tools = (await dispatch(_rpc("tools/list"), None))["result"]["tools"]
    assert tools, "the server must advertise something"
    for t in tools:
        assert t["name"].startswith("talaia_")
        assert t["inputSchema"]["type"] == "object"
        assert len(t["description"]) > 80, f"{t['name']} needs a usable description"
        assert "handler" not in t, "handlers must not leak into the wire format"


async def test_unknown_tool_is_an_invalid_params_error():
    out = await dispatch(_rpc("tools/call", {"name": "talaia_nope", "arguments": {}}),
                         None)
    assert out["error"]["code"] == -32602


async def test_a_failing_tool_reports_inside_the_result_not_as_a_protocol_error():
    """An agent can only recover from a limit it is told about."""
    out = await dispatch(
        _rpc("tools/call", {"name": "talaia_geocode", "arguments": {}}), None)
    assert "error" not in out, "a tool failure is not a JSON-RPC error"
    assert out["result"]["isError"] is True
    assert "required" in out["result"]["content"][0]["text"]


async def test_taxonomy_tool_needs_no_store_or_key():
    out = await dispatch(_rpc("tools/call",
                              {"name": "talaia_taxonomy", "arguments": {}}), None)
    assert out["result"]["isError"] is False
    assert "education" in out["result"]["content"][0]["text"]
    assert out["result"]["structuredContent"]["counts"]["categories"] > 0


# -- limits are the same ones the REST API enforces --------------------------
def test_the_area_cap_applies_to_mcp_requests():
    from talaia.routers.v1 import _enforce_key_limits

    free = _key("free")
    big = _request_from({"lon": 2.0, "lat": 41.5, "radius_km": 40}, include_assets=False)
    assert area_km2(parse_aoi(big.aoi).union) > free.max_aoi_km2
    with pytest.raises(HTTPException) as exc:
        _enforce_key_limits(big, free)
    assert exc.value.status_code == 403
    assert "free" in exc.value.detail


def test_an_unlimited_key_is_not_capped_by_its_tier():
    from talaia.routers.v1 import _enforce_key_limits

    big = _request_from({"lon": 2.0, "lat": 41.5, "radius_km": 40}, include_assets=False)
    assert _enforce_key_limits(big, _key("unlimited")) is not None


def test_buffer_counts_towards_the_area_cap_from_mcp_too():
    from talaia.routers.v1 import _enforce_key_limits

    req = _request_from({"lon": 2.0, "lat": 41.5, "radius_km": 2, "buffer_m": 20_000},
                        include_assets=False)
    with pytest.raises(HTTPException):
        _enforce_key_limits(req, _key("free"))


def test_mcp_asset_limit_cannot_exceed_the_key_ceiling():
    from talaia.routers.v1 import _enforce_key_limits

    free = _key("free")
    req = _request_from({"lon": 2.0, "lat": 41.5, "radius_km": 1},
                        include_assets=True, max_assets=_MCP_ASSET_CAP)
    assert _enforce_key_limits(req, free).max_assets <= free.max_assets


def test_every_tool_that_reads_data_is_declared_with_an_area_input():
    """A data tool without an area input would have nothing for the cap to measure."""
    for spec in TOOL_SPECS:
        if spec["name"] in ("talaia_exposure_summary", "talaia_list_assets"):
            assert "aoi" in spec["inputSchema"]["properties"]
            assert "lon" in spec["inputSchema"]["properties"]


# -- area helpers -----------------------------------------------------------
def test_circle_is_round_on_the_ground_not_in_degrees():
    poly = parse_aoi(_circle(2.0, 41.5, 10.0)).union
    assert area_km2(poly) == pytest.approx(math.pi * 100, rel=0.02)


def test_circle_radius_holds_at_high_latitude():
    for lat in (28.0, 41.5, 43.5):
        assert area_km2(parse_aoi(_circle(-3.0, lat, 5.0)).union) == \
            pytest.approx(math.pi * 25, rel=0.02)


def test_geojson_takes_precedence_over_centre_and_radius():
    aoi = {"type": "Point", "coordinates": [1.0, 41.0]}
    assert _aoi_from({"aoi": aoi, "lon": 2.0, "lat": 42.0}) == aoi


@pytest.mark.parametrize("args", [{}, {"lon": 2.0}, {"lat": 41.0}])
def test_an_area_is_required(args):
    with pytest.raises(ValueError):
        _aoi_from(args)


@pytest.mark.parametrize("radius", [-1, 0, 500])
def test_absurd_radii_are_refused(radius):
    with pytest.raises(ValueError):
        _aoi_from({"lon": 2.0, "lat": 41.0, "radius_km": radius})


def test_currency_formatting_is_readable():
    assert _eur(1_500_000_000) == "EUR 1.5bn"
    assert _eur(2_400_000) == "EUR 2.4m"
    assert _eur(950) == "EUR 950"


def test_the_website_does_not_claim_a_route_the_api_owns():
    """The API serves /mcp, and the SPA catch-all sits behind it - so a marketing page
    routed at /mcp is unreachable in a browser and answers 401 instead. It lives at
    /agents; this pins that so the clash cannot come back silently."""
    import pathlib

    app_tsx = pathlib.Path(__file__).resolve().parents[1] / "web" / "src" / "App.tsx"
    source = app_tsx.read_text()
    assert 'path="/agents"' in source
    assert 'path="/mcp"' not in source, "/mcp belongs to the API, not the website"
