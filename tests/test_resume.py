"""A long ingest has to survive being interrupted.

The national school registry is ~51,000 addresses to geocode, the best part of an hour.
Writing the rows only after the last one resolved meant a restart anywhere in that window
threw the entire source away - and on a platform that redeploys on every push, that is
most windows. A deployment sat at three sources of twelve for a day because of it.

Two properties make it survivable, and they only work together:

* rows are written as each chunk of addresses resolves, so progress is durable; and
* a source is "done" when its run says so, not when it has any rows at all - otherwise
  the first chunk landing would make an interrupted source look finished forever.
"""
from datetime import datetime, timedelta, timezone

import pytest

from talaia.connectors.base import Connector, Coverage, RawAsset, Tier
from talaia.models import SourceMeta
from talaia.store import Store


@pytest.fixture(autouse=True)
def registry_loaded():
    """`_pending_sources` asks the connector registry what exists; without loading it the
    answer is "nothing" and every assertion about pending sources passes vacuously."""
    from talaia.connectors import registry
    registry.load_all()
    assert any(c.meta.id == "es.cat.reses" for c in registry.by_tier(Tier.RESIDENT))


@pytest.fixture
async def store(tmp_path):
    s = Store(tmp_path / "resume.duckdb")
    s.connect()
    yield s
    s.close()


def _meta(sid: str) -> SourceMeta:
    return SourceMeta(id=sid, name=sid, publisher="test", tier="resident",
                      coverage="test", country="ES", licence="CC0")


class Fake(Connector):
    """Yields address-only records, so every one goes through the geocode path."""
    tier = Tier.RESIDENT
    coverage = Coverage(bbox=(0.0, 40.0, 3.0, 43.0), label="test")

    def __init__(self, sid="test.src", n=25, fail_after=None):
        self.meta = _meta(sid)
        self.n = n
        self.fail_after = fail_after
        self.geocoded = 0

    async def fetch(self, **kwargs):
        for i in range(self.n):
            yield {"i": i}

    def normalise(self, raw):
        yield RawAsset(source_ref=str(raw["i"]), name=f"asset {raw['i']}",
                       subcategory="school", address=f"{raw['i']} Carrer Test, Girona",
                       needs_geocoding=True)

    async def _geocode_batch(self, items, store):
        """Stand in for CartoCiudad, and optionally die part-way like a restart."""
        if self.fail_after is not None and self.geocoded >= self.fail_after:
            raise RuntimeError("container stopped")
        self.geocoded += len(items)
        from talaia.norm import asset_id, point_wkt
        rows = [{
            "id": asset_id(self.meta.id, it.source_ref), "source_id": self.meta.id,
            "source_ref": it.source_ref, "category": "education",
            "subcategory": "school", "name": it.name, "wkt": point_wkt(2.8, 41.98),
            "lon": 2.8, "lat": 41.98, "geometry_kind": "point", "address": None,
            "contacts": None, "capacity": None, "attributes": None,
            "footprint_m2": None, "floors": None, "confidence": 0.5, "tile_key": None,
        } for it in items]
        return rows, 0


async def test_rows_land_before_the_whole_source_is_geocoded(store):
    """The point of chunking: an interrupted run keeps what it had already resolved."""
    conn = Fake(n=25, fail_after=10)
    with pytest.raises(RuntimeError):
        await conn.ingest(store, geocode_chunk=5)

    rows = await store.fetch("SELECT count(*) FROM assets WHERE source_id = ?",
                             [conn.meta.id])
    assert rows[0][0] == 10, "work completed before the interruption must be durable"


async def test_an_interrupted_source_is_still_pending(store):
    """Having rows is not the same as being finished. Without this the first chunk
    landing would make the source look loaded and it would never be resumed."""
    from talaia.main import _pending_sources

    conn = Fake(sid="es.cat.reses", n=25, fail_after=10)
    with pytest.raises(RuntimeError):
        await conn.ingest(store, geocode_chunk=5)

    stats = await store.source_stats()
    assert stats["es.cat.reses"]["rows"] == 10
    # The ingest caught the failure and recorded it. A container killed outright gets no
    # such chance, and leaves the last per-chunk "partial" standing instead - covered
    # below. Neither counts as complete, which is what matters here.
    assert stats["es.cat.reses"]["last_status"] == "error"

    pending = {c.meta.id for c in await _pending_sources(store)}
    assert "es.cat.reses" in pending, "an interrupted source must be picked up again"


async def test_a_source_killed_mid_chunk_is_still_pending(store):
    """The Railway case: the process is stopped, so no handler runs and the last thing
    written is a per-chunk progress marker."""
    from talaia.main import _pending_sources

    conn = Fake(sid="es.cat.reses", n=25, fail_after=10)
    with pytest.raises(RuntimeError):
        await conn.ingest(store, geocode_chunk=5)
    # Drop the error row the handler managed to write, leaving only the progress it had
    # recorded before it died.
    await store.execute_write(
        "DELETE FROM source_runs WHERE source_id = ? AND status = 'error'",
        ["es.cat.reses"])

    stats = await store.source_stats()
    assert stats["es.cat.reses"]["last_status"] == "partial"
    assert stats["es.cat.reses"]["rows"] == 10
    pending = {c.meta.id for c in await _pending_sources(store)}
    assert "es.cat.reses" in pending


async def test_a_completed_source_is_not_rerun(store):
    from talaia.main import _pending_sources

    conn = Fake(sid="es.cat.reses", n=12)
    assert await conn.ingest(store, geocode_chunk=5) == 12

    stats = await store.source_stats()
    assert stats["es.cat.reses"]["last_status"] == "ok"
    pending = {c.meta.id for c in await _pending_sources(store)}
    assert "es.cat.reses" not in pending


async def test_resuming_completes_the_source(store):
    """The recovery path end to end: interrupted, then run again to completion."""
    first = Fake(n=25, fail_after=10)
    with pytest.raises(RuntimeError):
        await first.ingest(store, geocode_chunk=5)

    second = Fake(n=25)
    assert await second.ingest(store, geocode_chunk=5) == 25

    rows = await store.fetch("SELECT count(*) FROM assets WHERE source_id = ?",
                             [first.meta.id])
    assert rows[0][0] == 25, "assets upsert by id, so a second pass must not duplicate"
    stats = await store.source_stats()
    assert stats["test.src"]["last_status"] == "ok"


async def test_source_stats_reports_the_latest_run_not_an_arbitrary_one(store):
    """The resume decision is made on this status, so picking a random run's status out
    of the group would resume finished sources and skip unfinished ones."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    await store.record_run("s", "partial", 10, now - timedelta(hours=2))
    await store.record_run("s", "ok", 25, now - timedelta(hours=1))

    stats = await store.source_stats()
    assert stats["s"]["last_status"] == "ok"


async def test_rows_without_any_run_record_count_as_loaded(store):
    """Data written before runs were recorded must not be re-ingested on every boot."""
    from talaia.main import _pending_sources

    conn = Fake(sid="es.cat.reses", n=6)
    await conn.ingest(store, geocode_chunk=100)
    await store.execute_write("DELETE FROM source_runs WHERE source_id = ?",
                              ["es.cat.reses"])

    stats = await store.source_stats()
    assert stats["es.cat.reses"]["last_status"] == "loaded"
    pending = {c.meta.id for c in await _pending_sources(store)}
    assert "es.cat.reses" not in pending


async def test_the_shared_http_client_is_not_reused_across_event_loops():
    """An httpx client's connections belong to the loop that made it, and `is_closed`
    stays False when that loop dies - so a cached client looks healthy and every request
    through it raises "Event loop is closed". This surfaced as an intermittent failure
    in the mail tests, depending on which test built the client first.
    """
    import asyncio

    from talaia import net

    await net.close_client()
    first = net.get_client()
    assert net.get_client() is first, "same loop must reuse the same client"

    def in_another_loop():
        async def grab():          # must be called *inside* the new loop, not as an
            client = net.get_client()  # argument evaluated before asyncio.run starts it
            await net.close_client()   # and closed inside it, for the same reason
            return id(client)
        return asyncio.run(grab())

    second_id = await asyncio.to_thread(in_another_loop)
    assert second_id != id(first), "a different loop must get its own client"

    # `first` is still open and belongs to *this* loop. The nested run replaced the
    # module global and then cleared it, so close_client() can no longer see it - and an
    # httpx client still open when its loop is torn down raises "Event loop is closed"
    # during finalisation, inside whichever unlucky test is running at the time.
    await first.aclose()
    await net.close_client()
