"""Regressions for the external bug report of 2026-09-20.

Named by report id so a reopened issue is traceable to the test that should have caught
it. The critical one, TALAIA-001, was introduced by this codebase's own OSM error-backoff
change: the backoff marker counted as a cache hit and took the warning with it, so an
unreachable upstream came back as "there are no roads here".
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from talaia.geo import ACCEPTED_GEOMETRY_TYPES, parse_aoi
from talaia.models import Timing
from talaia.store import Store


@pytest.fixture
async def store(tmp_path):
    s = Store(tmp_path / "bugreport.duckdb")
    s.connect()
    yield s
    s.close()


async def _mark(store, key, status, age_minutes=0):
    when = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=age_minutes)
    await store.execute_write(
        "INSERT OR REPLACE INTO osm_tile_cache "
        "(tile_key, min_lon, min_lat, max_lon, max_lat, fetched_at, status, "
        " feature_count, network_count, error, last_hit_at) "
        "VALUES (?, 0, 0, 1, 1, ?, ?, 0, 0, NULL, ?)", [key, when, status, when])


# -- TALAIA-001 -------------------------------------------------------------
async def test_a_failed_tile_is_not_counted_as_cached(store):
    """The whole bug in one assertion: a tile inside its retry backoff holds no data,
    so calling it cached reports an upstream outage as an empty area."""
    await _mark(store, "t/ok", "ok")
    await _mark(store, "t/bad", "error", age_minutes=1)

    states = await store.tile_states(["t/ok", "t/bad", "t/new"],
                                     error_backoff_minutes=30)
    assert states == {"t/ok": "fresh", "t/bad": "failed", "t/new": "stale"}


async def test_the_three_tile_states_are_distinguishable(store):
    """fresh / failed / stale, not "will I fetch this" - collapsing to two is what
    let a failure be read back as data."""
    await _mark(store, "t/fresh", "ok")
    await _mark(store, "t/expired", "ok", age_minutes=60 * 24 * 20)
    await _mark(store, "t/failed", "error", age_minutes=1)
    await _mark(store, "t/failed_old", "error", age_minutes=90)

    states = await store.tile_states(
        ["t/fresh", "t/expired", "t/failed", "t/failed_old"],
        ttl_hours=336, error_backoff_minutes=30)
    assert states["t/fresh"] == "fresh"
    assert states["t/expired"] == "stale", "an expired success must be refetched"
    assert states["t/failed"] == "failed"
    assert states["t/failed_old"] == "stale", "the backoff expires and we retry"


async def test_a_warm_over_failed_tiles_warns_every_time(store, monkeypatch):
    """Not only on the request that first hit the failure. The backoff suppresses the
    network call; it must never suppress the warning."""
    from shapely.geometry import box

    from talaia.connectors.osm.overpass import OpenStreetMap

    osm = OpenStreetMap()
    geometry = box(0.02, 0.02, 0.04, 0.04)
    for key, _ in __import__("talaia.geo", fromlist=["tiles_for_geometry"]) \
            .tiles_for_geometry(geometry, 0.05):
        await _mark(store, key, "error", age_minutes=1)

    async def never_called(*a, **k):  # pragma: no cover - asserts it is not reached
        raise AssertionError("a tile in backoff must not be refetched")

    monkeypatch.setattr(osm, "_fetch_bbox", never_called)

    for attempt in range(3):
        stats = await osm.warm(store, geometry)
        assert stats["tiles_failed"] > 0, f"attempt {attempt}"
        assert stats["tiles_cached"] == 0, "nothing is actually held"
        assert any("could not be fetched" in w for w in stats["warnings"]), (
            f"attempt {attempt} served a failure silently")


async def test_missing_is_worded_differently_from_absent(store, monkeypatch):
    """A caller has to be able to tell "we could not reach OSM" from "no roads here"."""
    from shapely.geometry import box

    from talaia.connectors.osm.overpass import OpenStreetMap
    from talaia.geo import tiles_for_geometry

    osm = OpenStreetMap()
    geometry = box(0.02, 0.02, 0.04, 0.04)
    for key, _ in tiles_for_geometry(geometry, 0.05):
        await _mark(store, key, "error", age_minutes=1)
    monkeypatch.setattr(osm, "_fetch_bbox", lambda *a, **k: [])

    warning = " ".join((await osm.warm(store, geometry))["warnings"])
    assert "MISSING" in warning
    assert "retry" in warning.lower()


def test_timing_reports_failed_tiles_separately():
    t = Timing(tiles_total=12, tiles_cached=0, tiles_failed=12)
    assert t.tiles_failed == 12
    assert t.tiles_cached == 0, "failures never inflate the cached count"


# -- TALAIA-005 -------------------------------------------------------------
def test_a_registry_capacity_keeps_its_own_confidence():
    """is_default describes the PEOPLE estimate. A REGA holding publishes a real
    headcount; guessing how many humans stand next to it must not downgrade the
    registry's own figure to the value used for a pure guess."""
    cap = {"animals": 180.0, "livestock_units": 24.0, "confidence": 0.55,
           "basis": "registered capacity in head (REGA)"}
    merged = dict(cap)
    merged.setdefault("people", None)
    merged.setdefault("basis", cap["basis"])
    merged.setdefault("confidence", 0.2)          # the fixed behaviour
    assert merged["confidence"] == 0.55


def test_a_class_default_still_gets_the_low_confidence():
    merged: dict = {"basis": "class default for 'University faculty'"}
    merged.setdefault("confidence", 0.2)
    assert merged["confidence"] == 0.2


# -- TALAIA-007 -------------------------------------------------------------
@pytest.mark.parametrize("bad", [
    {"type": "Banana", "coordinates": []},
    {"type": "polygon", "coordinates": []},        # case matters in GeoJSON
    {"type": "", "coordinates": []},
])
def test_an_unknown_geometry_type_raises_valueerror_not_a_library_error(bad):
    """GeometryTypeError is not a ValueError, so it escaped the 422 handling and
    surfaced as a 500 carrying the exception class."""
    with pytest.raises(ValueError) as exc:
        parse_aoi(bad)
    assert "not supported" in str(exc.value)
    assert "GeometryTypeError" not in str(exc.value)


def test_the_error_names_what_is_accepted():
    with pytest.raises(ValueError) as exc:
        parse_aoi({"type": "Banana", "coordinates": []})
    assert "Polygon" in str(exc.value) and "FeatureCollection" in str(exc.value)


def test_the_accepted_types_cover_what_a_fire_model_emits():
    for needed in ("Polygon", "MultiPolygon", "Feature", "FeatureCollection"):
        assert needed in ACCEPTED_GEOMETRY_TYPES


# -- TALAIA-008 -------------------------------------------------------------
def test_an_unclosed_ring_is_accepted_but_reported():
    """Leniency is right for simulation output; silence is not - the caller is
    reasoning about a different shape than the one we measured."""
    aoi = parse_aoi({"type": "Polygon",
                     "coordinates": [[[1.8, 41.7], [1.9, 41.7], [1.9, 41.8]]]})
    assert aoi.area_km2 > 0
    assert any("not closed" in n for n in aoi.notes)


def test_a_properly_closed_ring_is_not_flagged():
    aoi = parse_aoi({"type": "Polygon",
                     "coordinates": [[[1.8, 41.7], [1.9, 41.7], [1.9, 41.8],
                                      [1.8, 41.7]]]})
    assert aoi.notes == []


def test_an_unclosed_ring_inside_a_feature_collection_is_found():
    aoi = parse_aoi({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"band": "0-1h"},
         "geometry": {"type": "Polygon",
                      "coordinates": [[[1.8, 41.7], [1.9, 41.7], [1.9, 41.8]]]}}]})
    assert any("not closed" in n for n in aoi.notes)


def test_notes_are_deduplicated_across_bands():
    ring = [[1.8, 41.7], [1.9, 41.7], [1.9, 41.8]]
    aoi = parse_aoi({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"band": b},
         "geometry": {"type": "Polygon", "coordinates": [ring]}}
        for b in ("0-1h", "1-3h", "3-6h")]})
    assert len([n for n in aoi.notes if "not closed" in n]) == 1


# -- TALAIA-003 -------------------------------------------------------------
def test_the_catalan_facilities_feed_maps_its_phone_columns():
    """The feed publishes telefon1, telefon2 and email. Mapping only a `web` column it
    does not even have left 24,545 assets with an empty contacts block."""
    from talaia.connectors.es.catalunya import CatFacilities

    raw = {"idequipament": "546925", "nom": "Ajuntament de Gualba",
           "categoria": "Administració pública|Ens locals|Ajuntaments",
           "latitud": "41.732072", "longitud": "2.50183895",
           "telefon1": "93-8487027", "telefon2": "93-8487070",
           "email": "gualba@diba.cat", "poblacio": "Gualba",
           "tipus_via": "pg.", "via": "Pg. Montseny", "num": "13"}
    out = list(CatFacilities().normalise(raw))
    assert out, "the fixture should classify"
    contacts = out[0].contacts
    assert contacts["phone"] == ["+34938487027", "+34938487070"]
    assert contacts["email"] == ["gualba@diba.cat"]


def test_several_phone_columns_merge_and_deduplicate():
    from talaia.norm import clean_phones

    assert clean_phones("93-8487027", "938487070") == ["+34938487027", "+34938487070"]
    assert clean_phones("938487027", "93-848-7027") == ["+34938487027"]
    assert clean_phones(None, None) == [] and clean_phones() == []


# -- TALAIA-004 -------------------------------------------------------------
async def test_bootstrap_resumes_a_partial_load(store):
    """A redeploy part-way through leaves some sources loaded and the rest at zero. An
    "is the store empty" check then decides everything is fine, which is how a
    deployment sits at three sources of twelve indefinitely."""
    from talaia.connectors import registry as connector_registry
    from talaia.main import _pending_sources

    connector_registry.load_all()
    await store.upsert_assets([
        {"id": "x1", "source_id": "es.cat.equipaments", "source_ref": "1",
         "category": "education", "subcategory": "school", "name": "One",
         "wkt": "POINT(2 41)", "lon": 2.0, "lat": 41.0, "geometry_kind": "point",
         "address": {}, "contacts": {}, "capacity": {}, "attributes": {},
         "footprint_m2": None, "floors": None, "confidence": 0.5,
         "tile_key": None, "retrieved_at": None}])

    pending = {c.meta.id for c in await _pending_sources(store)}
    assert "es.cat.equipaments" not in pending, "already loaded"
    assert "es.ine.popgrid" in pending, "never loaded, so it must be picked up"
    assert "es.msan.hospitales" in pending
