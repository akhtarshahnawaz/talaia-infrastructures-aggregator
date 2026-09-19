"""Region resolution, block planning and warm progress reporting."""
from datetime import datetime, timedelta, timezone

import pytest

from talaia.config import settings
from talaia.geo import tiles_for_bounds
from talaia.regions import REGIONS, parse_bbox, resolve, resolve_many
from talaia.services.warm import WarmProgress, _blocks, estimate
from talaia.store import Store


# -- gazetteer -------------------------------------------------------------
def test_named_regions_and_groups_resolve():
    assert resolve("barcelona")[0].key == "barcelona"
    assert resolve("BARCELONA")[0].key == "barcelona", "names are case-insensitive"
    assert resolve("catalunya_provinces")[0].key == "prov_barcelona"
    assert len(resolve("demo")) == 4


def test_a_bbox_is_accepted_in_place_of_a_name():
    region = resolve("1.9,41.3,2.3,41.6")[0]
    assert region.bbox == (1.9, 41.3, 2.3, 41.6)


@pytest.mark.parametrize("text", [
    "1.9,41.3,2.3",              # too few
    "2.3,41.3,1.9,41.6",         # min_lon past max_lon
    "1.9,41.6,2.3,41.3",         # min_lat past max_lat
    "200,41.3,201,41.6",         # off the planet
    "a,b,c,d",
])
def test_malformed_bboxes_are_rejected_rather_than_guessed(text):
    assert parse_bbox(text) is None
    with pytest.raises(KeyError):
        resolve(text)


def test_unknown_region_names_say_what_is_available():
    with pytest.raises(KeyError) as exc:
        resolve("atlantis")
    assert "barcelona" in exc.value.args[0]


def test_resolve_many_deduplicates_but_keeps_order():
    out = resolve_many(["barcelona", "girona", "barcelona", "demo"])
    assert [r.key for r in out][:2] == ["barcelona", "girona"]
    assert len({r.key for r in out}) == len(out)


def test_every_region_is_a_sane_box_over_spain():
    for region in REGIONS.values():
        x0, y0, x1, y1 = region.bbox
        assert x0 < x1 and y0 < y1, region.key
        assert -19 <= x0 and x1 <= 5, region.key
        assert 27 <= y0 and y1 <= 44, region.key
        assert region.tile_count() > 0


# -- block planning ---------------------------------------------------------
def test_blocks_cover_every_tile_exactly_once():
    """A tile fetched by two blocks would be written twice, and the second write
    would clear the first block's rows for it."""
    bbox = (1.9, 41.3, 2.35, 41.65)
    deg = 0.05
    planned = [key for _, tiles in _blocks(bbox, deg, 4) for key, _ in tiles]
    assert len(planned) == len(set(planned)), "no tile in two blocks"
    assert set(planned) == {k for k, _ in tiles_for_bounds(bbox, deg)}


def test_a_block_bbox_contains_its_own_tiles():
    for block, tiles in _blocks((1.9, 41.3, 2.35, 41.65), 0.05, 4):
        for _, (tx0, ty0, tx1, ty1) in tiles:
            assert block[0] <= tx0 and tx1 <= block[2]
            assert block[1] <= ty0 and ty1 <= block[3]


def test_block_size_is_bounded():
    for _, tiles in _blocks((1.9, 41.3, 2.9, 42.3), 0.05, 4):
        assert len(tiles) <= 16


def test_blocks_align_to_the_global_tile_grid():
    """Tiles must land on the same grid the query path looks them up on, or a warm
    populates keys nothing will ever ask for."""
    deg = settings.osm_tile_deg
    keys = {k for _, tiles in _blocks((1.93, 41.31, 2.07, 41.44), deg, 4)
            for k, _ in tiles}
    assert keys <= {k for k, _ in tiles_for_bounds((1.9, 41.3, 2.1, 41.45), deg)}


def test_estimate_counts_shared_tiles_once():
    single = estimate([REGIONS["barcelona"]])["tiles"]
    doubled = estimate([REGIONS["barcelona"], REGIONS["barcelona"]])["tiles"]
    assert single == doubled


# -- progress reporting -----------------------------------------------------
def test_a_run_where_every_tile_failed_is_not_reported_as_done():
    p = WarmProgress(tiles_total=6, tiles_failed=6, blocks_total=1, blocks_done=1)
    assert p.settle() == "failed"
    assert p.as_dict()["percent"] == 0.0, "nothing landed, so nothing is warm"


def test_partial_failure_is_named_as_such():
    p = WarmProgress(tiles_total=10, tiles_done=7, tiles_failed=3)
    assert p.settle() == "partial"
    assert p.as_dict()["percent"] == 70.0


def test_already_fresh_tiles_count_towards_progress():
    """A resumed run starts part-warm; reporting 0% would hide that."""
    p = WarmProgress(tiles_total=10, tiles_already_fresh=8, tiles_done=2)
    assert p.settle() == "done"
    assert p.as_dict()["percent"] == 100.0


def test_error_list_is_capped_but_the_count_is_not():
    p = WarmProgress(tiles_total=1, errors=[f"boom {i}" for i in range(500)])
    d = p.as_dict()
    assert len(d["errors"]) == 10
    assert d["error_count"] == 500


# -- failed-tile backoff ----------------------------------------------------
@pytest.fixture
async def store(tmp_path):
    s = Store(tmp_path / "tiles.duckdb")
    s.connect()
    yield s
    s.close()


async def _mark(store, key, status, age_minutes=0):
    """Write a tile exactly as production does: naive UTC, bound as a parameter.

    Using the database's current_timestamp here instead would make these tests pass on a
    UTC machine and fail everywhere else - which is the same confusion that made the
    backoff silently never fire.
    """
    when = (datetime.now(timezone.utc).replace(tzinfo=None)
            - timedelta(minutes=age_minutes))
    await store.execute_write(
        "INSERT OR REPLACE INTO osm_tile_cache "
        "(tile_key, min_lon, min_lat, max_lon, max_lat, fetched_at, status, "
        " feature_count, network_count, error, last_hit_at) "
        "VALUES (?, 0, 0, 1, 1, ?, ?, 0, 0, NULL, ?)", [key, when, status, when])


async def test_a_fresh_tile_is_not_refetched(store):
    await _mark(store, "t/ok", "ok")
    assert await store.stale_tiles(["t/ok"]) == set()


async def test_a_recently_failed_tile_is_left_alone(store):
    """Otherwise one broken tile makes every request covering it pay the full OSM
    deadline, for ever - a warmed region would be slower than a cold one."""
    await _mark(store, "t/bad", "error", age_minutes=1)
    assert await store.stale_tiles(["t/bad"], error_backoff_minutes=30) == set()


async def test_the_backoff_expires(store):
    await _mark(store, "t/bad", "error", age_minutes=90)
    assert await store.stale_tiles(["t/bad"], error_backoff_minutes=30) == {"t/bad"}


async def test_a_bulk_warm_retries_failures_immediately(store):
    """The request path backs off; the warm exists precisely to retry."""
    await _mark(store, "t/bad", "error", age_minutes=1)
    assert await store.stale_tiles(["t/bad"], error_backoff_minutes=0) == {"t/bad"}


async def test_an_expired_ok_tile_is_still_refetched(store):
    await _mark(store, "t/old", "ok", age_minutes=60 * 24 * 20)
    assert await store.stale_tiles(["t/old"], ttl_hours=336) == {"t/old"}


async def test_an_unknown_tile_is_always_stale(store):
    assert await store.stale_tiles(["t/never-seen"]) == {"t/never-seen"}


async def test_the_backoff_is_immune_to_the_server_timezone(store):
    """The cutoff must come from UTC, not the database's session-local clock.

    fetched_at is stored as naive UTC. Comparing it to DuckDB's current_timestamp, which
    is local, puts a UTC+2 host two hours out - enough that a 30-minute window never
    matches at all, while a 14-day TTL hides the same error completely.
    """
    await _mark(store, "t/bad", "error", age_minutes=5)
    assert await store.stale_tiles(["t/bad"], error_backoff_minutes=30) == set()
    await _mark(store, "t/edge", "ok", age_minutes=60)
    assert await store.stale_tiles(["t/edge"], ttl_hours=2) == set()
    assert await store.stale_tiles(["t/edge"], ttl_hours=1) == {"t/edge"}
