"""Loading part of a source, on demand, from the admin panel.

The whole point is cost. A cold volume is about an hour, nearly all of it geocoding
~55,000 addresses, and an operator who wants one province visible before a demo should
not pay for the other fifty-one. So the filter has to bite *before* the geocoder is
asked anything - a filter applied to the results would narrow the data and save none of
the time.

The other half is that a partial load must never look like a finished one, or the boot
bootstrap will consider the source done and the rest will never arrive.
"""
import pytest

from talaia.connectors.base import Connector, Coverage, IngestFilter, RawAsset, Tier
from talaia.models import SourceMeta
from talaia.services.prefetch import Prefetcher, build_filter, resolve_sources
from talaia.store import Store


@pytest.fixture(autouse=True)
def registry_loaded():
    from talaia.connectors import registry
    registry.load_all()


@pytest.fixture
async def store(tmp_path):
    s = Store(tmp_path / "prefetch.duckdb")
    s.connect()
    yield s
    s.close()


class Fake(Connector):
    """Two municipalities, all address-only, so everything goes through the filter."""
    tier = Tier.RESIDENT
    coverage = Coverage(bbox=(0.0, 40.0, 4.0, 43.0), label="test")

    def __init__(self, sid="test.src"):
        self.meta = SourceMeta(id=sid, name=sid, publisher="t", tier="resident",
                               coverage="test", country="ES", licence="CC0")
        self.meta_id = sid
        self.asked: list[str] = []

    async def fetch(self, **kwargs):
        for i in range(10):
            yield {"i": i, "muni": "Girona" if i % 2 else "Màlaga"}

    def normalise(self, raw):
        yield RawAsset(source_ref=str(raw["i"]), name=f"a{raw['i']}", subcategory="school",
                       address={"municipality": raw["muni"], "province": raw["muni"]},
                       needs_geocoding=True)

    async def _geocode_batch(self, items, store):
        self.asked.extend(i.source_ref for i in items)
        from talaia.norm import asset_id, point_wkt
        rows = [{
            "id": asset_id(self.meta.id, it.source_ref), "source_id": self.meta.id,
            "source_ref": it.source_ref, "category": "education", "subcategory": "school",
            "name": it.name, "wkt": point_wkt(2.8, 41.98), "lon": 2.8, "lat": 41.98,
            "geometry_kind": "point", "address": None, "contacts": None, "capacity": None,
            "attributes": None, "footprint_m2": None, "floors": None, "confidence": 0.5,
            "tile_key": None,
        } for it in items]
        return rows, 0


async def test_a_place_filter_stops_work_before_the_geocoder(store):
    """The saving, not just the narrowing: the discarded records are never looked up."""
    conn = Fake()
    n = await conn.ingest(store, select=build_filter(place="Girona"))
    assert n == 5
    assert len(conn.asked) == 5, (
        "the filter must run before geocoding, or a partial load costs the same as a "
        "full one")


async def test_a_place_filter_ignores_accents_and_case(store):
    """Registries spell Màlaga with the accent; an operator types what is on the keyboard."""
    conn = Fake()
    assert await conn.ingest(store, select=build_filter(place="malaga")) == 5


async def test_several_places_can_be_named_at_once(store):
    conn = Fake()
    assert await conn.ingest(store, select=build_filter(place="Girona, Malaga")) == 10


async def test_a_limit_caps_the_records_considered(store):
    conn = Fake()
    n = await conn.ingest(store, select=build_filter(limit=3))
    assert 0 < n <= 3


async def test_a_partial_load_is_never_recorded_as_complete(store):
    """Otherwise the boot bootstrap sees a loaded source and the rest never arrives."""
    from talaia.main import _pending_sources

    conn = Fake(sid="es.meq.schools")
    await conn.ingest(store, select=build_filter(place="Girona"))

    stats = await store.source_stats()
    assert stats["es.meq.schools"]["last_status"] == "partial"
    assert "places=girona" in stats["es.meq.schools"]["last_error"]
    assert "es.meq.schools" in {c.meta.id for c in await _pending_sources(store)}


async def test_an_unfiltered_load_is_recorded_as_complete(store):
    conn = Fake(sid="es.meq.schools")
    await conn.ingest(store)
    stats = await store.source_stats()
    assert stats["es.meq.schools"]["last_status"] == "ok"


def test_an_unknown_region_is_rejected_by_name():
    with pytest.raises(ValueError, match="Unknown region"):
        build_filter(region="atlantis")


def test_a_region_key_becomes_a_bounding_box():
    f = build_filter(region="barcelona")
    assert f.bbox is not None and f.is_partial


def test_a_nonsense_limit_is_rejected():
    with pytest.raises(ValueError, match="positive"):
        build_filter(limit=0)


def test_an_unknown_source_is_named_in_the_error():
    with pytest.raises(ValueError, match="es.nope"):
        resolve_sources(["es.nope"])


def test_no_sources_named_means_every_resident_source():
    ids = {c.meta.id for c in resolve_sources(None)}
    assert "es.meq.schools" in ids and "es.cat.equipaments" in ids


async def test_one_failing_source_does_not_stop_the_others(store):
    """The operator asked for all of them; partial data beats none."""
    class Broken(Fake):
        async def fetch(self, **kwargs):
            raise RuntimeError("upstream is down")
            yield  # pragma: no cover

    broken = Broken(sid="es.msan.siap")
    good = Fake(sid="es.msan.hospitales")
    p = Prefetcher()
    p.start(store, [type("B", (), {"meta": broken.meta, "__call__": lambda s: broken})(),
                    type("G", (), {"meta": good.meta, "__call__": lambda s: good})()],
            IngestFilter())
    await p._task

    by_id = {s.source_id: s for s in p.progress.sources}
    assert by_id["es.msan.siap"].status == "failed"
    assert "upstream is down" in by_id["es.msan.siap"].error
    assert by_id["es.msan.hospitales"].status == "ok"
    assert p.progress.status == "partial"


async def test_only_one_prefetch_runs_at_a_time(store):
    """DuckDB takes a single writer, and two runs would contend without finishing sooner."""
    conn = Fake()
    p = Prefetcher()
    p.start(store, [type("C", (), {"meta": conn.meta, "__call__": lambda s: conn})()],
            IngestFilter())
    with pytest.raises(RuntimeError, match="already running"):
        p.start(store, [], IngestFilter())
    await p._task
