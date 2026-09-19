"""MCP and REST must answer the same question with the same numbers.

The two interfaces shape their output differently on purpose - REST returns the full
report to a program, MCP returns a summary sized for a context window - but the figures
underneath have to agree. If they ever drift, an agent and a dashboard looking at the
same fire would disagree about how many people are in it, and nothing in either response
would reveal which one was wrong.

These tests run both paths against one in-memory store and compare.
"""
import pytest

from talaia.mcp import _circle, dispatch
from talaia.models import ExposureRequest
from talaia.routers.v1 import _enforce_key_limits
from talaia.services.aggregator import build_report
from talaia.store import Store, get_store, set_store


@pytest.fixture
async def store(tmp_path):
    """A small, fully known store: two schools, a care home and two population cells."""
    s = Store(tmp_path / "parity.duckdb")
    s.connect()
    set_store(s)
    await s.upsert_assets([
        {"id": "a1", "source_id": "es.cat.schools", "source_ref": "1",
         "category": "education", "subcategory": "primary_school", "name": "Escola A",
         "wkt": "POINT(2.10 41.40)", "lon": 2.10, "lat": 41.40,
         "geometry_kind": "point", "address": {}, "contacts": {},
         "capacity": {"students": 300, "people": 330, "basis": "enrolment"},
         "attributes": {}, "footprint_m2": 1200, "floors": 2, "confidence": 0.9,
         "tile_key": None, "retrieved_at": None},
        {"id": "a2", "source_id": "es.msan.hospitales", "source_ref": "2",
         "category": "healthcare", "subcategory": "hospital", "name": "Hospital B",
         "wkt": "POINT(2.11 41.41)", "lon": 2.11, "lat": 41.41,
         "geometry_kind": "point", "address": {}, "contacts": {},
         "capacity": {"beds": 100, "people": 220, "basis": "beds"},
         "attributes": {}, "footprint_m2": 6000, "floors": 5, "confidence": 0.9,
         "tile_key": None, "retrieved_at": None},
        {"id": "a3", "source_id": "es.csic.carehomes", "source_ref": "3",
         "category": "social_care", "subcategory": "care_home", "name": "Residencia C",
         "wkt": "POINT(2.12 41.42)", "lon": 2.12, "lat": 41.42,
         "geometry_kind": "point", "address": {}, "contacts": {},
         "capacity": {"places": 80, "people": 106, "basis": "places"},
         "attributes": {}, "footprint_m2": 2000, "floors": 3, "confidence": 0.9,
         "tile_key": None, "retrieved_at": None},
    ])
    await s.upsert_popgrid([
        {"cell_id": "c1", "wkt": "POLYGON((2.09 41.39,2.12 41.39,2.12 41.42,"
                                 "2.09 41.42,2.09 41.39))",
         "population": 5000.0, "area_m2": 1e6, "source_id": "es.ine.popgrid",
         "year": 2011},
        {"cell_id": "c2", "wkt": "POLYGON((2.12 41.42,2.15 41.42,2.15 41.45,"
                                 "2.12 41.45,2.12 41.42))",
         "population": 1200.0, "area_m2": 1e6, "source_id": "es.ine.popgrid",
         "year": 2011},
    ])
    yield s
    s.close()


AOI = _circle(2.11, 41.41, 4.0)


async def _rest(**over):
    """The REST path: exactly what POST /v1/exposure runs after the limit check."""
    over.setdefault("aoi", AOI)
    req = _enforce_key_limits(ExposureRequest(live_osm=False, **over), None)
    return await build_report(req, get_store())


async def _mcp_tool(name, args):
    out = await dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": name, "arguments": args}}, None)
    assert "error" not in out, out
    result = out["result"]
    assert result["isError"] is False, result["content"][0]["text"]
    return result["structuredContent"]


# ---------------------------------------------------------------------------
async def test_the_summary_numbers_are_identical(store):
    """Every headline figure an operator might quote, compared field by field."""
    rest = await _rest()
    mcp = await _mcp_tool("talaia_exposure_summary",
                          {"aoi": AOI, "live_osm": False})

    r, m = rest.summary.model_dump(), mcp["summary"]
    for field in ("asset_count", "people_estimate", "people_from_registry",
                  "people_from_defaults", "population_resident", "total_value_eur",
                  "critical_assets", "hazardous_assets", "response_assets",
                  "livestock_units", "coverage_regime"):
        assert r[field] == m[field], f"{field} differs: REST {r[field]} vs MCP {m[field]}"
    assert r["aoi_area_km2"] == pytest.approx(m["aoi_area_km2"])


async def test_population_agrees_across_interfaces(store):
    rest = await _rest(include_population_grid=True)
    mcp = await _mcp_tool("talaia_population_grid", {"aoi": AOI})

    assert rest.population.total == pytest.approx(mcp["total"])
    assert rest.population.cell_count == mcp["cell_count"]
    assert rest.population.peak_density_per_km2 == pytest.approx(
        mcp["peak_density_per_km2"])
    assert ({c.cell_id for c in rest.population.cells}
            == {c["cell_id"] for c in mcp["cells"]})


async def test_the_same_assets_come_back_in_the_same_order(store):
    rest = await _rest()
    mcp = await _mcp_tool("talaia_list_assets",
                          {"aoi": AOI, "live_osm": False, "limit": 100})

    assert [a.id for a in rest.assets] == [a["id"] for a in mcp["assets"]]
    by_id = {a.id: a for a in rest.assets}
    for row in mcp["assets"]:
        asset = by_id[row["id"]]
        assert row["name"] == asset.name
        assert row["subcategory"] == asset.subcategory
        assert row["priority_score"] == pytest.approx(
            asset.exposure.priority_score, abs=0.05), "score rounded, not recomputed"
        assert row["value_eur"] == pytest.approx(asset.valuation.total_eur, abs=1)
        assert row["people"] == asset.capacity.people
        assert row["hazardous"] == asset.hazardous


async def test_category_breakdown_matches(store):
    rest = await _rest()
    mcp = await _mcp_tool("talaia_exposure_summary", {"aoi": AOI, "live_osm": False})
    r = {c.category: (c.count, c.total_value_eur) for c in rest.summary.by_category}
    m = {c["category"]: (c["count"], c["total_value_eur"]) for c in mcp["summary"]["by_category"]}
    assert r == m


async def test_a_banded_aoi_bands_the_same_way_on_both(store):
    bands = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"band": "0-1h", "minutes": 60},
         "geometry": _circle(2.11, 41.41, 1.5)},
        {"type": "Feature", "properties": {"band": "1-3h", "minutes": 180},
         "geometry": _circle(2.11, 41.41, 4.0)}]}
    rest = await _rest(aoi=bands)
    mcp = await _mcp_tool("talaia_exposure_summary", {"aoi": bands, "live_osm": False})

    assert [b.band for b in rest.bands] == [b["band"] for b in mcp["bands"]]
    for rb, mb in zip(rest.bands, mcp["bands"]):
        assert rb.asset_count == mb["asset_count"]
        assert rb.people_estimate == pytest.approx(mb["people_estimate"])
        assert rb.population_resident == pytest.approx(mb["population_resident"])


async def test_warnings_are_not_swallowed_by_the_mcp_layer(store):
    """A caller who only ever sees the MCP surface must still learn that a result was
    degraded or truncated."""
    rest = await _rest()
    mcp = await _mcp_tool("talaia_exposure_summary", {"aoi": AOI, "live_osm": False})
    assert rest.warnings == mcp["warnings"]


async def test_an_mcp_circle_and_the_same_geojson_agree(store):
    """lon/lat/radius_km is a convenience, not a second geometry engine."""
    via_geojson = await _mcp_tool("talaia_exposure_summary",
                                  {"aoi": _circle(2.11, 41.41, 4.0), "live_osm": False})
    via_centre = await _mcp_tool("talaia_exposure_summary",
                                 {"lon": 2.11, "lat": 41.41, "radius_km": 4.0,
                                  "live_osm": False})
    assert via_geojson["summary"] == via_centre["summary"]


async def test_layer_filtering_behaves_the_same(store):
    rest = await _rest(layers=["healthcare"])
    mcp = await _mcp_tool("talaia_exposure_summary",
                          {"aoi": AOI, "live_osm": False, "layers": ["healthcare"]})
    assert rest.summary.asset_count == mcp["summary"]["asset_count"] == 1


async def test_the_mcp_asset_cap_trims_the_list_without_changing_the_count(store):
    """Truncating the list must not quietly truncate the totals with it."""
    mcp = await _mcp_tool("talaia_list_assets",
                          {"aoi": AOI, "live_osm": False, "limit": 1})
    assert len(mcp["assets"]) == 1
    assert mcp["total_matched"] == 3, "the count still reflects everything matched"
    assert mcp["truncated"] is True


async def test_the_population_endpoint_agrees_with_the_full_report(store):
    """/v1/population takes a deliberate shortcut past build_report - assets, networks
    and OpenStreetMap change none of these numbers. The shortcut is only safe while it
    produces exactly what the long way round produces."""
    from talaia.models import ExposureRequest
    from talaia.routers.v1 import population as population_endpoint

    fast = await population_endpoint(
        ExposureRequest(aoi=AOI, include_geometry=False), None)
    full = await _rest(include_population_grid=True, include_geometry=False)

    a, b = fast["population"], full.population
    assert a.total == b.total
    assert a.cell_count == b.cell_count
    assert a.peak_density_per_km2 == b.peak_density_per_km2
    assert [c.cell_id for c in a.cells] == [c.cell_id for c in b.cells]
    assert [c.population_in_aoi for c in a.cells] == [c.population_in_aoi for c in b.cells]


async def test_the_population_endpoint_still_reports_the_area_it_measured(store):
    from talaia.models import ExposureRequest
    from talaia.routers.v1 import population as population_endpoint

    out = await population_endpoint(ExposureRequest(aoi=AOI), None)
    assert out["area_km2"] > 0
    assert len(out["aoi_bbox"]) == 4
    assert out["request_id"].startswith("req_")
