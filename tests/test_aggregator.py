"""End-to-end report assembly against a temporary store. No network."""
import pytest

from talaia.models import ExposureRequest
from talaia.norm import asset_id, point_wkt
from talaia.services.aggregator import build_report
from talaia.store import Store

BANDED = {"type": "FeatureCollection", "features": [
    {"type": "Feature", "properties": {"band": "0-1h", "minutes": 60},
     "geometry": {"type": "Polygon", "coordinates": [[[1.80, 41.70], [1.82, 41.70],
                                                      [1.82, 41.72], [1.80, 41.72],
                                                      [1.80, 41.70]]]}},
    {"type": "Feature", "properties": {"band": "1-3h", "minutes": 180},
     "geometry": {"type": "Polygon", "coordinates": [[[1.78, 41.68], [1.86, 41.68],
                                                      [1.86, 41.76], [1.78, 41.76],
                                                      [1.78, 41.68]]]}},
]}


@pytest.fixture
async def store(tmp_path):
    s = Store(tmp_path / "t.duckdb")
    s.connect()
    rows = [
        # inside band 0
        ("care_home", "social_care", "Residencia Els Companys", 1.810, 41.710,
         {"places": 64}, "es.cat.reses"),
        ("hospital", "healthcare", "Hospital de Manresa", 1.815, 41.715,
         {"beds": 120}, "es.cat.equipaments"),
        ("fuel_station", "transport", "Benzinera", 1.812, 41.712, {}, "osm"),
        # inside band 1 only
        ("primary_school", "education", "Escola La Font", 1.840, 41.740,
         {"students": 300}, "es.cat.schools"),
        ("campsite", "tourism", "Camping Freixa", 1.850, 41.750, {}, "osm"),
        ("cattle", "livestock", "Mas Gran", 1.845, 41.745,
         {"animals": 120, "livestock_units": 96}, "es.cat.livestock"),
        # outside both
        ("house", "residential", "Far away", 2.500, 42.500, {}, "osm"),
    ]
    payload = []
    for sub, cat, name, lon, lat, cap, src in rows:
        payload.append({
            "id": asset_id(src, name), "source_id": src, "source_ref": name,
            "category": cat, "subcategory": sub, "name": name,
            "wkt": point_wkt(lon, lat), "lon": lon, "lat": lat,
            "geometry_kind": "point", "address": {"municipality": "Manresa"},
            "contacts": {"phone": ["+34938000000"]}, "capacity": cap,
            "attributes": {}, "footprint_m2": None, "floors": None,
            "confidence": 0.8, "tile_key": None})
    await s.upsert_assets(payload)
    await s.upsert_popgrid([{
        "cell_id": "c1",
        "wkt": "POLYGON((1.78 41.68, 1.88 41.68, 1.88 41.78, 1.78 41.78, 1.78 41.68))",
        "population": 10000.0, "area_m2": 1e6, "source_id": "test", "year": 2011}])
    yield s
    s.close()


async def test_banded_report(store):
    req = ExposureRequest(aoi=BANDED, live_osm=False)
    report = await build_report(req, store)

    # Assets outside every band are excluded by the spatial query.
    assert report.summary.asset_count == 6
    assert [b.band for b in report.bands] == ["0-1h", "1-3h"]

    # Each asset lands in the EARLIEST band containing it, so bands partition the set.
    assert sum(b.asset_count for b in report.bands) == report.summary.asset_count
    assert report.bands[0].asset_count == 3
    assert report.bands[1].asset_count == 3

    # Hazard and criticality flags propagate.
    assert report.summary.hazardous_assets == 1
    assert report.bands[0].hazardous_assets == 1

    # Occupancy is split by provenance rather than silently blended.
    s = report.summary
    assert s.people_from_registry + s.people_from_defaults == pytest.approx(
        s.people_estimate, abs=1.0)
    assert s.people_from_registry > 0

    # Population is area-weighted and exclusive per band.
    assert report.population.total > 0
    assert sum(b.population_resident for b in report.bands) <= report.population.total * 1.01

    assert s.coverage_regime.startswith("catalonia_full")
    assert report.timing.total_ms > 0


async def test_sorting_and_truncation(store):
    report = await build_report(
        ExposureRequest(aoi=BANDED, live_osm=False, sort_by="priority"), store)
    scores = [a.exposure.priority_score for a in report.assets]
    assert scores == sorted(scores, reverse=True)

    capped = await build_report(
        ExposureRequest(aoi=BANDED, live_osm=False, max_assets=2), store)
    assert len(capped.assets) == 2 and capped.assets_truncated
    # The summary still reflects everything, not just the returned page.
    assert capped.summary.asset_count == 6


async def test_layer_filter(store):
    report = await build_report(
        ExposureRequest(aoi=BANDED, layers=["social_care"], live_osm=False), store)
    assert {a.category for a in report.assets} == {"social_care"}


async def test_summary_mode_omits_assets(store):
    report = await build_report(
        ExposureRequest(aoi=BANDED, include_assets=False, live_osm=False), store)
    assert report.assets == [] and report.summary.asset_count == 6


async def test_aoi_outside_coverage_is_flagged(store):
    far = {"type": "Polygon", "coordinates": [[[10.0, 50.0], [10.1, 50.0],
                                               [10.1, 50.1], [10.0, 50.1], [10.0, 50.0]]]}
    report = await build_report(ExposureRequest(aoi=far, live_osm=False), store)
    assert report.summary.coverage_regime.startswith("osm_only")
    assert any("outside Spain" in w for w in report.warnings)


async def test_oversized_aoi_is_rejected(store):
    huge = {"type": "Polygon", "coordinates": [[[-10.0, 30.0], [10.0, 30.0],
                                                [10.0, 50.0], [-10.0, 50.0], [-10.0, 30.0]]]}
    with pytest.raises(ValueError, match="exceeds"):
        await build_report(ExposureRequest(aoi=huge, live_osm=False), store)
