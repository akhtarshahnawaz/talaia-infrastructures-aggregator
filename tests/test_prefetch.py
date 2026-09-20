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


async def test_a_record_with_no_place_at_all_is_left_to_the_coordinate_test(store):
    """The population grid publishes cells, not addresses. Judging it by municipality
    name would mean naming a city silently emptied the source."""
    class Cells(Fake):
        def normalise(self, raw):
            yield RawAsset(source_ref=str(raw["i"]), name="cell", subcategory="school",
                           lon=2.8, lat=41.98)          # in the Girona box
        async def fetch(self, **kwargs):
            for i in range(4):
                yield {"i": i}

    conn = Cells()
    assert await conn.ingest(store, select=build_filter(place="Girona")) == 4


async def test_a_place_outside_the_named_box_is_still_dropped(store):
    class Cells(Fake):
        def normalise(self, raw):
            yield RawAsset(source_ref=str(raw["i"]), name="cell", subcategory="school",
                           lon=2.17, lat=41.39)         # Barcelona, not Girona
        async def fetch(self, **kwargs):
            for i in range(4):
                yield {"i": i}

    conn = Cells()
    assert await conn.ingest(store, select=build_filter(place="Girona")) == 0


def test_a_known_place_name_also_supplies_a_bounding_box():
    """So a source with coordinates and no municipality column can still be narrowed."""
    assert build_filter(place="Girona").bbox is not None
    assert build_filter(place="Girona").places == frozenset({"girona"})


def test_an_unknown_place_name_is_still_a_valid_text_filter():
    """The gazetteer only covers Catalonia; naming a Castilian province must still work
    against the registries' own municipality column."""
    f = build_filter(place="Cuenca")
    assert f.bbox is None and f.places == frozenset({"cuenca"}) and f.is_partial


# ---------------------------------------------------------------------------
# "Nothing happens when I press the button."
# ---------------------------------------------------------------------------
async def test_a_write_gives_up_rather_than_waiting_for_a_stuck_holder(store, monkeypatch):
    """Every symptom of the outage traced to this: reads answered in 0.3s and writes
    never answered at all, so revoke, delete, clear, dataset load and cache warm each
    hung until the browser gave up - silently, because nothing had failed yet.
    """
    import asyncio

    from talaia.config import settings
    from talaia.store import StoreBusy

    monkeypatch.setattr(settings, "write_lock_timeout_s", 0.2)
    await store._write_lock.acquire()          # stand in for a stalled ingest
    store._write_holder, store._write_since = "wedged", __import__("time").monotonic()
    try:
        with pytest.raises(StoreBusy) as exc:
            await store.execute_write("SELECT 1")
        assert "wedged" in str(exc.value)
        assert "Nothing was changed" in str(exc.value)
    finally:
        store._write_holder = store._write_since = None
        store._write_lock.release()

    # And it recovers once the holder lets go.
    await store.execute_write("SELECT 1")
    assert store.write_health()["writes_refused"] == 1


async def test_write_health_names_the_holder_while_it_runs(store):
    """So the panel can say which thing is wedged instead of showing an idle screen."""
    import asyncio

    started = asyncio.Event()
    release = asyncio.Event()

    async def holder():
        async with store._writing("slow-ingest"):
            started.set()
            await release.wait()

    task = asyncio.create_task(holder())
    await started.wait()
    health = store.write_health()
    assert health["holder"] == "slow-ingest"
    release.set()
    await task
    assert store.write_health()["holder"] is None
    assert store.write_health()["last_write_ok"] is not None


# ---------------------------------------------------------------------------
# Sizing DuckDB to the container it is actually in.
# ---------------------------------------------------------------------------
def test_duckdb_is_clamped_to_a_small_container(monkeypatch):
    """Told it may use 1GB inside a 512MB container, DuckDB takes that literally: it
    spills relentlessly or gets killed, a bulk write that should take a second takes
    minutes, and it holds the single writer the whole time."""
    from talaia import store as store_mod
    from talaia.config import settings
    from talaia.store import Store

    monkeypatch.setattr(settings, "duckdb_memory_limit", "1GB")
    monkeypatch.setattr(settings, "duckdb_threads", 4)
    monkeypatch.setattr(store_mod, "container_limits", lambda: {}, raising=False)
    monkeypatch.setattr(
        "talaia.diagnostics.container_limits",
        lambda: {"memory_limit_mb": 512, "cpu_quota_cores": 0.5})

    memory, threads = Store("/tmp/unused.duckdb")._sized_for_container()
    assert memory == "256MB", "half of the container, leaving room for everything else"
    assert threads == 1, "four threads on half a core is thrash, not parallelism"


def test_a_generous_container_keeps_the_configured_values(monkeypatch):
    from talaia.config import settings
    from talaia.store import Store

    monkeypatch.setattr(settings, "duckdb_memory_limit", "1GB")
    monkeypatch.setattr(settings, "duckdb_threads", 4)
    monkeypatch.setattr(
        "talaia.diagnostics.container_limits",
        lambda: {"memory_limit_mb": 8192, "cpu_quota_cores": 8})

    assert Store("/tmp/unused.duckdb")._sized_for_container() == ("1GB", 4)


def test_no_cgroup_means_no_clamping(monkeypatch):
    """A normal machine reports nothing; the configured values stand."""
    from talaia.config import settings
    from talaia.store import Store

    monkeypatch.setattr(settings, "duckdb_memory_limit", "1GB")
    monkeypatch.setattr(settings, "duckdb_threads", 4)
    monkeypatch.setattr("talaia.diagnostics.container_limits", lambda: {})
    assert Store("/tmp/unused.duckdb")._sized_for_container() == ("1GB", 4)


def test_diagnostics_reports_without_a_credential(store):
    """It exists for a wedged deployment, so it must not need the keys to that
    deployment - and must therefore carry nothing worth protecting."""
    from talaia.diagnostics import report

    r = report(store)
    assert set(r) >= {"container", "disk", "duckdb", "write_health", "notes"}
    blob = repr(r).lower()
    for secret in ("talaia_sk_", "admin", "password", "api_key", "authorization"):
        assert secret not in blob, f"diagnostics leaked {secret!r}"


# ---------------------------------------------------------------------------
# Offline geocoding, and saying so.
# ---------------------------------------------------------------------------
def test_every_source_explains_itself():
    """The sources page is the only place a reader finds out what a number means."""
    from talaia.connectors import registry

    registry.load_all()
    for cls in registry.all_connectors():
        m = cls.meta
        assert len(m.description) > 60, f"{m.id} has no real description"
        assert len(m.used_for) > 60, f"{m.id} does not say how TALAIA uses it"


def test_sources_that_need_geocoding_say_the_positions_are_approximate():
    """A point derived from a place name is a different kind of claim from a surveyed
    one, and the difference decides whether a building is inside a fire perimeter."""
    from talaia.connectors import registry
    from talaia.connectors.es import catalunya, spain  # noqa: F401

    registry.load_all()
    needs = {"es.cat.reses", "es.meq.schools", "es.msan.hospitales", "es.msan.siap",
             "es.csic.carehomes"}
    by_id = {c.meta.id: c.meta for c in registry.all_connectors()}
    for sid in needs:
        note = by_id[sid].geocoding
        assert note, f"{sid} is geocoded but does not say so"
        assert "approximat" in note.lower() or "not the building" in note.lower() \
            or "rather than the building" in note.lower(), \
            f"{sid} does not admit the position is approximate"
        assert "hybrid" in note, f"{sid} does not say how to get real positions"


async def test_the_gazetteer_resolves_bilingual_and_article_suffixed_names():
    """Spain names a lot of places twice, and the registries pick either one."""
    from talaia.connectors.es import gazetteer

    table = {"girona": (2.83, 41.98), "alacant": (-0.48, 38.35),
             "la seu d urgell": (1.46, 42.36)}
    assert gazetteer.lookup(table, "Girona")
    assert gazetteer.lookup(table, "Alicante/Alacant")
    assert gazetteer.lookup(table, "Seu d'Urgell, la")
    assert gazetteer.lookup(table, "Nowhere At All") is None


async def test_an_offline_placement_is_flagged_as_approximate():
    """Downstream has to be able to tell the two kinds of point apart."""
    from talaia.connectors.es import gazetteer

    hit = gazetteer.lookup({"girona": (2.83, 41.98)}, "Girona")
    assert hit["approximate"] is True
    assert hit["geocoder"] == "geonames-offline"
    assert hit["quality"] <= 0.3, "a town centre must not outrank a street address"
