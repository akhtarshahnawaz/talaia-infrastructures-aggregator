"""The Postgres backend, and its parity with the DuckDB one.

Two kinds of test here:

* ``_translate`` is pure string work, so it runs everywhere, always. It is the single
  place the two SQL dialects differ, which makes it the single place a dialect bug can
  hide.
* The rest need a real PostGIS. They are skipped unless TALAIA_TEST_DATABASE_URL points
  at one, so the suite stays runnable with nothing installed:

      docker run -d --name talaia-pg -e POSTGRES_PASSWORD=talaia \\
        -e POSTGRES_USER=talaia -e POSTGRES_DB=talaia -p 55432:5432 postgis/postgis:16-3.4
      TALAIA_TEST_DATABASE_URL=postgresql://talaia:talaia@localhost:55432/talaia pytest

The parity tests deliberately run the *same* assertions against both engines. A store
that passes only on the engine it was written for is how a migration silently changes
behaviour - a boundary point that used to be included, a JSON column that used to come
back as text - and those changes surface as wrong exposure reports, not as errors.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

import pytest

from talaia.store import Store

DSN = os.environ.get("TALAIA_TEST_DATABASE_URL", "")
needs_pg = pytest.mark.skipif(not DSN, reason="TALAIA_TEST_DATABASE_URL is not set")

AOI = "POLYGON((2.1 41.3,2.3 41.3,2.3 41.5,2.1 41.5,2.1 41.3))"
BBOX = (2.1, 41.3, 2.3, 41.5)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def asset(aid: str, lon: float, lat: float, **kw) -> dict:
    row = {"id": aid, "source_id": "t", "category": "health", "subcategory": "hospital",
           "name": aid, "wkt": f"POINT({lon} {lat})", "lon": lon, "lat": lat,
           "geometry_kind": "point"}
    row.update(kw)
    return row


# ---------------------------------------------------------------------------
# Dialect translation - no database required
# ---------------------------------------------------------------------------
class TestTranslate:
    def test_insert_or_replace_becomes_on_conflict(self):
        from talaia.pgstore import _translate

        sql = _translate(
            "INSERT OR REPLACE INTO key_usage (key_hash, day, requests) "
            "VALUES (?, ?, ?)")
        assert sql == (
            "INSERT INTO key_usage (key_hash, day, requests) VALUES (%s, %s, %s) "
            "ON CONFLICT (key_hash, day) DO UPDATE SET requests = EXCLUDED.requests")

    def test_the_conflict_target_is_the_whole_composite_key(self):
        """key_usage is keyed by (key_hash, day). Conflicting on key_hash alone would
        collapse every day's usage for a key into one row."""
        from talaia.pgstore import _translate

        assert "ON CONFLICT (key_hash, day)" in _translate(
            "INSERT OR REPLACE INTO key_usage (key_hash, day, requests) VALUES (?,?,?)")

    def test_primary_key_columns_are_not_in_the_update_list(self):
        """Assigning the key from EXCLUDED is a no-op at best and confusing at worst."""
        from talaia.pgstore import _translate

        sql = _translate(
            "INSERT OR REPLACE INTO assets (id, name) VALUES (?, ?)")
        assert "DO UPDATE SET name = EXCLUDED.name" in sql
        assert "id = EXCLUDED.id" not in sql

    def test_a_question_mark_inside_a_string_is_left_alone(self):
        """A literal '?' in a name is data, not a parameter. Rewriting it would shift
        every later placeholder by one and bind the wrong values to the wrong columns."""
        from talaia.pgstore import _translate

        assert _translate("SELECT * FROM assets WHERE name = 'what? really' AND id = ?") \
            == "SELECT * FROM assets WHERE name = 'what? really' AND id = %s"

    def test_an_unknown_table_is_refused_rather_than_guessed(self):
        from talaia.pgstore import _translate

        with pytest.raises(ValueError, match="no primary key is registered"):
            _translate("INSERT OR REPLACE INTO nonesuch (a, b) VALUES (?, ?)")

    def test_every_table_in_the_schema_has_a_registered_primary_key(self):
        """PRIMARY_KEYS is hand-maintained; a table added to the schema without one
        would only fail the first time something upserted into it."""
        import re
        from pathlib import Path

        from talaia.pgstore import PRIMARY_KEYS

        sql = (Path(__file__).resolve().parents[1] / "sql" / "schema_postgres.sql").read_text()
        tables = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", sql))
        assert tables and tables <= set(PRIMARY_KEYS)

    def test_ordinary_select_passes_through_unchanged(self):
        from talaia.pgstore import _translate

        assert _translate("SELECT count(*) FROM assets") == "SELECT count(*) FROM assets"


class TestNoDialectLeaksIntoSharedCode:
    """SQL outside the two backend modules has to run on either engine.

    Twice now a DuckDB-only construct has reached production in shared code and failed
    only once it was on Postgres: `INSERT OR REPLACE` (caught by the translator) and
    `INTERVAL 24 HOUR` in the signup throttle, which 500ed every signup. Both were
    invisible locally because local development ran the engine they were written for.

    Scanned from the AST rather than with grep, so a comment that merely mentions one of
    these - and several do, explaining exactly this - is not a finding.
    """

    SHARED = "every module except the two backends, which are allowed their own dialect"
    EXEMPT = {"store.py", "pgstore.py"}

    # (pattern, why it does not belong in shared code)
    BANNED = [
        (r"\bINTERVAL\s+\d", "DuckDB spells intervals `INTERVAL 24 HOUR`; Postgres needs "
                            "`INTERVAL '24 hours'`. Compute the cutoff in Python and "
                            "bind it - that works on both and uses the right clock."),
        (r"\bcurrent_timestamp\b", "this is the database session's clock, but every "
                                  "timestamp column holds naive UTC written by the app. "
                                  "Use a Python-computed UTC value and bind it."),
        (r"\barg_max\s*\(", "DuckDB-only; Postgres wants DISTINCT ON."),
        (r"\bquantile_cont\s*\(", "DuckDB-only; Postgres spells it percentile_cont."),
    ]

    def test_shared_modules_use_no_engine_specific_sql(self):
        import ast
        import pathlib
        import re

        pkg = pathlib.Path(__file__).resolve().parents[1] / "api" / "talaia"
        findings: list[str] = []
        for path in sorted(pkg.rglob("*.py")):
            if path.name in self.EXEMPT:
                continue
            tree = ast.parse(path.read_text(), filename=str(path))
            # Docstrings are string constants too, and several of them discuss these
            # very constructs in order to explain why the code avoids them. Skip them,
            # or the guard fires on its own documentation.
            docstrings = set()
            for holder in ast.walk(tree):
                if isinstance(holder, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                       ast.AsyncFunctionDef)):
                    body = getattr(holder, "body", None)
                    if (body and isinstance(body[0], ast.Expr)
                            and isinstance(body[0].value, ast.Constant)
                            and isinstance(body[0].value.value, str)):
                        docstrings.add(id(body[0].value))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                    continue
                if id(node) in docstrings:
                    continue
                for pattern, why in self.BANNED:
                    if re.search(pattern, node.value, re.IGNORECASE):
                        findings.append(
                            f"{path.relative_to(pkg.parent.parent)}:{node.lineno} "
                            f"matched /{pattern}/ - {why}")
        assert not findings, "engine-specific SQL in shared code:\n  " + "\n  ".join(findings)


# ---------------------------------------------------------------------------
# Live Postgres
# ---------------------------------------------------------------------------
@pytest.fixture
async def pg():
    from talaia.pgstore import PostgresStore

    store = PostgresStore(DSN)
    store.connect()
    for table in ("assets", "networks", "pop_grid", "osm_tile_cache",
                  "enrichment_cache", "source_runs", "signups", "key_usage"):
        await store.execute_write(f"DELETE FROM {table}")
    yield store
    await store.aclose()


@pytest.fixture
def duck(tmp_path):
    store = Store(tmp_path / "parity.duckdb")
    store.connect()
    yield store
    store.close()


@pytest.fixture(params=["duckdb", "postgres"])
async def either(request, tmp_path):
    """The same tests against both engines, so parity is asserted rather than assumed."""
    if request.param == "postgres":
        if not DSN:
            pytest.skip("TALAIA_TEST_DATABASE_URL is not set")
        from talaia.pgstore import PostgresStore

        store = PostgresStore(DSN)
        store.connect()
        for table in ("assets", "networks", "pop_grid", "enrichment_cache",
                      "source_runs", "osm_tile_cache"):
            await store.execute_write(f"DELETE FROM {table}")
        yield store
        await store.aclose()
    else:
        store = Store(tmp_path / "parity.duckdb")
        store.connect()
        yield store
        store.close()


@pytest.mark.asyncio
class TestParity:
    async def test_an_asset_written_is_an_asset_found(self, either):
        await either.upsert_assets([asset("a1", 2.17, 41.39)])
        rows, _ = await either.query_assets(AOI, BBOX)
        assert [r["name"] for r in rows] == ["a1"]

    async def test_an_asset_outside_the_aoi_is_not_returned(self, either):
        await either.upsert_assets([asset("a1", 2.17, 41.39), asset("far", 9.9, 45.0)])
        rows, _ = await either.query_assets(AOI, BBOX)
        assert [r["name"] for r in rows] == ["a1"]

    async def test_upsert_is_idempotent_on_the_primary_key(self, either):
        await either.upsert_assets([asset("a1", 2.17, 41.39)])
        await either.upsert_assets([asset("a1", 2.17, 41.39, name="renamed")])
        rows, _ = await either.query_assets(AOI, BBOX)
        assert len(rows) == 1 and rows[0]["name"] == "renamed"

    async def test_a_point_exactly_on_the_aoi_boundary_is_included(self, either):
        """Both engines must agree here. Registry coordinates are rounded to six
        decimals and fire perimeters come off a raster, so landing exactly on an edge is
        common, and an evacuation list that drops the hospital on the line is wrong."""
        await either.upsert_assets([asset("edge", 2.1, 41.4)])
        rows, _ = await either.query_assets(AOI, BBOX)
        assert [r["name"] for r in rows] == ["edge"]

    async def test_json_columns_come_back_as_text(self, either):
        """The aggregator json.loads() these. A backend that helpfully pre-parsed them
        into dicts would be a silent behaviour change in the read path."""
        await either.upsert_assets([asset("a1", 2.17, 41.39, attributes={"beds": 100})])
        rows, _ = await either.query_assets(AOI, BBOX)
        import json
        assert json.loads(rows[0]["attributes"]) == {"beds": 100}

    async def test_categories_and_sources_filter(self, either):
        await either.upsert_assets([
            asset("h", 2.17, 41.39),
            asset("s", 2.18, 41.39, category="education", subcategory="school"),
        ])
        rows, _ = await either.query_assets(AOI, BBOX, categories=["education"])
        assert [r["name"] for r in rows] == ["s"]
        rows, _ = await either.query_assets(AOI, BBOX, sources=["nope"])
        assert rows == []

    async def test_population_is_area_weighted(self, either):
        await either.upsert_popgrid([{
            "cell_id": "c1", "population": 1000.0, "area_m2": 1e6,
            "source_id": "ine", "year": 2024,
            "wkt": "POLYGON((2.1 41.3,2.2 41.3,2.2 41.4,2.1 41.4,2.1 41.3))"}])
        cells = await either.query_population(
            "POLYGON((2.1 41.3,2.15 41.3,2.15 41.35,2.1 41.35,2.1 41.3))")
        assert len(cells) == 1
        # A quarter of the cell by area, so a quarter of its people.
        assert cells[0]["frac"] == pytest.approx(0.25, abs=0.01)

    async def test_a_clipped_network_reports_its_length_in_metres(self, either):
        await either.upsert_networks([{
            "id": "n1", "source_id": "t", "category": "transport", "subcategory": "road",
            "name": "R1", "wkt": "LINESTRING(2.1 41.35, 2.25 41.45)"}])
        nets = await either.query_networks(AOI, BBOX)
        assert len(nets) == 1
        # ~16.8 km on the spheroid; the two engines must not disagree by more than
        # rounding, because this number is multiplied by a cost per kilometre.
        assert nets[0]["clipped_m"] == pytest.approx(16754, rel=0.01)

    async def test_the_enrichment_cache_round_trips(self, either):
        await either.cache_put("k1", "geocode", {"lon": 1.0, "lat": 2.0})
        assert await either.cache_get("k1") == {"lon": 1.0, "lat": 2.0}
        assert await either.cache_get("missing") is None

    async def test_tile_freshness_and_failure_are_distinguished(self, either):
        now = _utcnow()
        await either.mark_tiles([
            {"tile_key": "ok", "min_lon": 0, "min_lat": 0, "max_lon": 1, "max_lat": 1,
             "fetched_at": now, "status": "ok", "feature_count": 5, "network_count": 2,
             "error": None},
            {"tile_key": "bad", "min_lon": 0, "min_lat": 0, "max_lon": 1, "max_lat": 1,
             "fetched_at": now, "status": "error", "feature_count": 0,
             "network_count": 0, "error": "upstream 500"},
        ])
        assert await either.tile_states(["ok", "bad", "new"]) == {
            "ok": "fresh", "bad": "failed", "new": "stale"}
        assert await either.stale_tiles(["ok", "bad", "new"]) == {"new"}

    async def test_source_stats_reports_the_newest_run(self, either):
        """arg_max on DuckDB, DISTINCT ON in Postgres. Both must pick the latest run:
        the resume decision is made on this status, so an old one restarts a finished
        ingest from scratch."""
        started = _utcnow()
        await either.record_run("t", "partial", 10, started)
        await asyncio.sleep(0.01)
        await either.record_run("t", "ok", 20, started)
        stats = await either.source_stats()
        assert stats["t"]["last_status"] == "ok"

    async def test_stats_counts_every_table(self, either):
        await either.upsert_assets([asset("a1", 2.17, 41.39)])
        await either.upsert_networks([{
            "id": "n1", "source_id": "net", "category": "transport",
            "subcategory": "road", "wkt": "LINESTRING(2.1 41.35, 2.25 41.45)"}])
        stats = await either.stats()
        assert stats["assets"] == 1 and stats["networks"] == 1
        assert stats["sources"] == 2
        assert stats["assets_by_category"] == {"health": 1}

    async def test_clearing_a_tile_removes_its_assets_and_networks(self, either):
        await either.upsert_assets([asset("a1", 2.17, 41.39, tile_key="T")])
        await either.upsert_networks([{
            "id": "n1", "source_id": "t", "category": "transport", "subcategory": "road",
            "wkt": "LINESTRING(2.1 41.35, 2.25 41.45)", "tile_key": "T"}])
        await either.clear_tile_assets(["T"])
        rows, _ = await either.query_assets(AOI, BBOX)
        assert rows == [] and await either.query_networks(AOI, BBOX) == []

    async def test_a_batch_containing_the_same_id_twice_is_accepted(self, either):
        """Registries really do this - a facility listed once per service it offers, a
        care home under two administrative records - and both collapse to one asset_id.

        DuckDB's INSERT OR REPLACE silently keeps the first. Postgres refuses the whole
        statement with "ON CONFLICT DO UPDATE command cannot affect row a second time",
        so two sources died on the first deploy. Both engines must now accept it, and
        agree on which row survives - which is why this asserts the surviving name
        rather than just a row count.
        """
        await either.upsert_assets([
            asset("dup", 2.17, 41.39, name="first"),
            asset("other", 2.18, 41.39),
            asset("dup", 2.17, 41.39, name="second"),
        ])
        rows, _ = await either.query_assets(AOI, BBOX)
        assert {r["name"] for r in rows} == {"first", "other"}

    async def test_duplicate_ids_across_separate_batches_still_update(self, either):
        """The within-batch fix must not break the ordinary case it sits next to."""
        await either.upsert_assets([asset("x", 2.17, 41.39, name="first")])
        await either.upsert_assets([asset("x", 2.17, 41.39, name="second")])
        rows, _ = await either.query_assets(AOI, BBOX)
        assert [r["name"] for r in rows] == ["second"]

    async def test_duplicate_population_cells_in_one_batch(self, either):
        """pop_grid is keyed by cell_id and loaded in 5,000-row batches from a national
        grid, so the same collision is possible there."""
        cell = {"wkt": "POLYGON((2.1 41.3,2.2 41.3,2.2 41.4,2.1 41.4,2.1 41.3))",
                "area_m2": 1e6, "source_id": "ine", "year": 2024}
        await either.upsert_popgrid([
            {"cell_id": "c1", "population": 100.0, **cell},
            {"cell_id": "c1", "population": 999.0, **cell},
        ])
        cells = await either.query_population(
            "POLYGON((2.1 41.3,2.15 41.3,2.15 41.35,2.1 41.35,2.1 41.3))")
        assert len(cells) == 1 and cells[0]["population"] == 100.0

    async def test_empty_input_is_not_an_error(self, either):
        assert await either.upsert_assets([]) == 0
        assert await either.upsert_networks([]) == 0
        assert await either.upsert_popgrid([]) == 0
        await either.mark_tiles([])
        await either.clear_tile_assets([])


@needs_pg
@pytest.mark.asyncio
class TestPostgresSpecific:
    async def test_writes_do_not_queue_behind_each_other(self, pg):
        """The whole reason this backend exists.

        On DuckDB a small administrative write issued while a bulk ingest is running
        waits for the ingest, because there is one writer and the ingest holds it. When
        the ingest stopped making progress on the deployment, that wait became
        unbounded, and every button in the admin panel stopped working. Here the two are
        separate transactions on separate connections, so the small write finishes while
        the big one is still going.
        """
        rows = [asset(f"big{i}", 2.1 + i * 1e-5, 41.4) for i in range(6000)]
        bulk = asyncio.create_task(pg.upsert_assets(rows))
        await asyncio.sleep(0.02)
        started = asyncio.get_running_loop().time()
        await pg.execute_write(
            "INSERT OR REPLACE INTO signups (id, email, organisation, ip, created_at, "
            "key_prefix) VALUES (?, ?, ?, ?, ?, ?)",
            ["s1", "a@b.c", "org", "1.2.3.4", _utcnow(), "tal_x"])
        waited = asyncio.get_running_loop().time() - started
        assert not bulk.done(), "the bulk write finished too fast to prove anything"
        assert waited < 0.5, f"the admin write queued behind the ingest ({waited:.2f}s)"
        await bulk

    async def test_two_ingests_make_progress_at_the_same_time(self, pg):
        """Two connectors loading different sources must not take turns."""
        order: list[str] = []

        async def ingest(tag: str, lon0: float):
            for chunk in range(3):
                await pg.upsert_assets(
                    [asset(f"{tag}{chunk}_{i}", lon0 + i * 1e-5, 41.4)
                     for i in range(2000)])
                order.append(tag)

        await asyncio.gather(ingest("A", 2.0), ingest("B", 3.0))
        # Strict alternation is not guaranteed, but one source finishing entirely before
        # the other starts would mean they serialised.
        assert order != ["A", "A", "A", "B", "B", "B"]
        assert (await pg.fetch("SELECT count(*) FROM assets"))[0][0] == 12000

    async def test_the_row_count_reports_what_was_stored(self, pg):
        """Three rows in, two distinct ids, so two rows landed - and the ingest log and
        /v1/sources should say two.

        This is one place the backends deliberately disagree. DuckDB returns the number
        of rows it was handed, so a source with duplicates has always over-reported
        itself there; it drops the extras silently and still claims the higher figure.
        Postgres has to identify the duplicates anyway in order to insert at all, so
        there is no reason for it to repeat that.
        """
        n = await pg.upsert_assets([
            asset("dup", 2.17, 41.39, name="first"),
            asset("other", 2.18, 41.39),
            asset("dup", 2.17, 41.39, name="second"),
        ])
        assert n == 2

    async def test_write_health_reports_no_single_writer(self, pg):
        health = pg.write_health()
        assert health["backend"] == "postgres"
        assert health["blocked"] is False and health["holder"] is None
        # The admin banner reads these keys on either backend.
        assert {"last_write_ok", "writes_refused", "queries_interrupted"} <= set(health)

    async def test_a_failed_write_does_not_wedge_the_next_one(self, pg):
        with pytest.raises(Exception):
            await pg.execute_write("INSERT INTO assets (id) VALUES (?)", ["no-geometry"])
        await pg.upsert_assets([asset("after", 2.17, 41.39)])
        rows, _ = await pg.query_assets(AOI, BBOX)
        assert [r["name"] for r in rows] == ["after"]

    async def test_the_duckdb_dialect_still_works_through_execute_write(self, pg):
        """auth.py and the signup router speak `?` and `INSERT OR REPLACE`. They are
        not rewritten for Postgres, so the translation has to hold at the call site."""
        now = _utcnow()
        await pg.execute_write(
            "INSERT OR REPLACE INTO api_keys (key_hash, prefix, label, tier, "
            "rate_limit_per_min, daily_quota, max_aoi_km2, max_assets, custom_limits, "
            "email, organisation, created_ip, created_at, revoked_at, last_used_at, "
            "request_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 0)",
            ["h1", "tal_ab", "lab", "free", 120, 0, 0.0, 20000, False, "e@x.com",
             "org", "1.2.3.4", now])
        # Re-running it must update, not duplicate and not fail.
        await pg.execute_write(
            "INSERT OR REPLACE INTO api_keys (key_hash, prefix, label, tier, "
            "rate_limit_per_min, daily_quota, max_aoi_km2, max_assets, custom_limits, "
            "email, organisation, created_ip, created_at, revoked_at, last_used_at, "
            "request_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 0)",
            ["h1", "tal_ab", "relabelled", "pro", 600, 0, 0.0, 20000, True, "e@x.com",
             "org", "1.2.3.4", now])
        rows = await pg.fetch(
            "SELECT label, tier FROM api_keys WHERE key_hash = ?", ["h1"])
        assert rows == [("relabelled", "pro")]

    async def test_timestamps_are_stored_as_utc(self, pg):
        """Every cutoff in the tile cache is computed in Python as naive UTC. A server
        on another timezone writing local time into the same column makes a 30-minute
        retry backoff quietly never match."""
        await pg.execute_write(
            "INSERT INTO source_runs (run_id, source_id, started_at, finished_at, "
            "status, rows) VALUES (?, ?, ?, timezone('utc', now()), ?, ?)",
            ["r1", "t", _utcnow(), "ok", 1])
        (finished,), = await pg.fetch(
            "SELECT finished_at FROM source_runs WHERE run_id = ?", ["r1"])
        assert abs((finished - _utcnow()).total_seconds()) < 60

    async def test_connect_is_idempotent(self, pg):
        """Boot applies the schema every time; a redeploy must not fail on it."""
        pg.connect()
        assert (await pg.fetch("SELECT count(*) FROM assets"))[0][0] == 0


# ---------------------------------------------------------------------------
# The admin panel, end to end, on Postgres
# ---------------------------------------------------------------------------
SMALL_AOI = {"type": "Polygon",
             "coordinates": [[[1.80, 41.70], [1.85, 41.70], [1.85, 41.75],
                              [1.80, 41.75], [1.80, 41.70]]]}


@needs_pg
class TestAdminEndToEnd:
    """Every button the admin panel has, over HTTP, against a real Postgres.

    These are the operations that stopped working on the deployment, and they stopped
    working because each one is a write and every write queued behind a stalled ingest.
    Unit-testing the registry would not have caught that; only going through the app,
    against a real database, does.
    """

    @pytest.fixture
    def client(self, monkeypatch):
        from fastapi.testclient import TestClient

        for name, value in {
            "TALAIA_DATABASE_URL": DSN, "TALAIA_ADMIN_KEY": "admin-secret",
            "TALAIA_REQUIRE_AUTH": "true", "TALAIA_AUTO_BOOTSTRAP": "false",
            "TALAIA_ALLOW_SIGNUP": "true", "TALAIA_REQUIRE_EMAIL_VERIFICATION": "false",
            "TALAIA_ENABLE_LIVE_OSM": "false", "TALAIA_API_KEYS": "",
            "TALAIA_WARM_ON_BOOT": "",
        }.items():
            monkeypatch.setenv(name, value)

        import talaia.config as config
        config.get_settings.cache_clear()
        fresh = config.Settings()
        monkeypatch.setattr(config, "settings", fresh)
        for module in ("talaia.main", "talaia.store", "talaia.pgstore", "talaia.auth",
                       "talaia.routers.v1", "talaia.diagnostics"):
            import importlib
            mod = importlib.import_module(module)
            if hasattr(mod, "settings"):
                monkeypatch.setattr(mod, "settings", fresh)

        import psycopg
        with psycopg.connect(DSN, autocommit=True) as con:
            con.execute("DELETE FROM api_keys")
            con.execute("DELETE FROM signups")

        from talaia.main import app
        with TestClient(app) as c:
            yield c

    ADMIN = {"X-Admin-Key": "admin-secret"}

    def test_a_key_can_be_created_used_revoked_and_deleted(self, client):
        made = client.post("/v1/admin/keys", headers=self.ADMIN,
                           json={"label": "e2e", "tier": "free"})
        assert made.status_code == 200, made.text
        key, prefix = made.json()["api_key"], made.json()["prefix"]

        used = client.post("/v1/exposure", headers={"X-API-Key": key},
                           json={"aoi": SMALL_AOI})
        assert used.status_code == 200, used.text

        revoked = client.delete(f"/v1/admin/keys/{prefix}", headers=self.ADMIN)
        assert revoked.status_code == 200, revoked.text
        # Revoking is not deleting. The row stays, carrying the date it was revoked, so
        # the audit trail survives - that distinction is the whole reason both buttons
        # exist, and a panel that hides revoked keys makes them look identical.
        listed = {k["prefix"]: k for k in client.get("/v1/admin/keys",
                                                     headers=self.ADMIN).json()}
        assert prefix in listed and listed[prefix]["revoked_at"]

        after = client.post("/v1/exposure", headers={"X-API-Key": key},
                            json={"aoi": SMALL_AOI})
        assert after.status_code == 401, "a revoked key must stop working immediately"

        deleted = client.delete(f"/v1/admin/keys/{prefix}?purge=true", headers=self.ADMIN)
        assert deleted.status_code == 200, deleted.text
        assert prefix not in {k["prefix"] for k in
                              client.get("/v1/admin/keys", headers=self.ADMIN).json()}

    def test_admin_writes_are_not_refused_while_an_ingest_runs(self, client):
        """The symptom that started all of this: the panel's buttons doing nothing.

        A bulk write is started directly on the store while HTTP requests that also
        write go through the app. On the single-writer backend those requests waited for
        the ingest and the caller saw a hung button; here they must all answer.
        """
        import threading

        from talaia.store import get_store

        store = get_store()
        done = threading.Event()

        def bulk():
            async def run():
                for chunk in range(12):
                    await store.upsert_assets(
                        [asset(f"bulk{chunk}_{i}", 1.80 + i * 1e-6, 41.71)
                         for i in range(5000)])
            asyncio.run(run())
            done.set()

        worker = threading.Thread(target=bulk, daemon=True)
        worker.start()
        try:
            codes, overlapped = [], False
            for i in range(5):
                r = client.post("/v1/admin/keys", headers=self.ADMIN,
                                json={"label": f"during-{i}", "tier": "free"})
                codes.append(r.status_code)
                # Was the ingest still going when this one answered? If the bulk had
                # already finished, a passing assertion below would prove nothing.
                overlapped = overlapped or not done.is_set()
        finally:
            worker.join(timeout=120)
        assert overlapped, "the ingest finished too fast for this to test anything"
        assert codes == [200] * 5, f"admin writes were refused during an ingest: {codes}"
        assert done.is_set(), "the bulk write never finished"

    def test_diagnostics_names_the_backend_and_postgis(self, client):
        d = client.get("/v1/diagnostics").json()
        assert d["backend"] == "postgres"
        assert d["database"]["postgis"], "PostGIS is required for the spatial queries"
        assert d["write_health"]["blocked"] is False
        blob = repr(d).lower()
        for secret in ("admin-secret", "password", "talaia_sk_"):
            assert secret not in blob, f"diagnostics leaked {secret!r}"


# ---------------------------------------------------------------------------
# Migrating an existing DuckDB deployment
# ---------------------------------------------------------------------------
@needs_pg
@pytest.mark.asyncio
class TestMigration:
    """`talaia migrate` has to carry the things that cannot be rebuilt.

    Assets can be re-ingested from the sources. API keys cannot: they are held by users,
    only their hashes exist, and re-ingesting would leave every one of them dead with no
    way to tell whose they were.
    """

    async def _seed(self, tmp_path):
        from talaia.auth import KeyRegistry

        src = Store(tmp_path / "old.duckdb")
        src.connect()
        registry = KeyRegistry()
        raw, record = await registry.create(src, label="pre-existing", tier="free",
                                            email="user@example.com")
        await src.upsert_assets([asset("keep-me", 2.17, 41.39, attributes={"beds": 7})])
        await src.upsert_networks([{
            "id": "road", "source_id": "t", "category": "transport",
            "subcategory": "road", "wkt": "LINESTRING(2.1 41.35, 2.25 41.45)"}])
        await src.upsert_popgrid([{
            "cell_id": "c1", "population": 1000.0, "area_m2": 1e6, "source_id": "ine",
            "year": 2024,
            "wkt": "POLYGON((2.1 41.3,2.2 41.3,2.2 41.4,2.1 41.4,2.1 41.3))"}])
        await src.cache_put("geocode:abc", "geocode", {"lon": 2.1, "lat": 41.4})
        await src.record_run("t", "ok", 1, _utcnow())
        src.close()
        return raw, record

    async def _migrate(self, tmp_path, pg):
        import argparse

        from talaia.cli import cmd_migrate

        for table in ("assets", "networks", "pop_grid", "enrichment_cache",
                      "source_runs", "api_keys", "key_usage", "signups",
                      "pending_signups"):
            await pg.execute_write(f"DELETE FROM {table}")
        rc = await cmd_migrate(argparse.Namespace(
            db=str(tmp_path / "old.duckdb"), to=DSN, batch=500))
        assert rc == 0

    async def test_an_existing_api_key_still_works_afterwards(self, tmp_path, pg):
        raw, record = await self._seed(tmp_path)
        await self._migrate(tmp_path, pg)

        from talaia.auth import KeyRegistry

        moved = KeyRegistry()
        await moved.load(pg)
        assert moved.verify(raw) is not None, (
            "the key a user is already holding stopped working across the migration")
        assert moved.verify(raw).label == "pre-existing"

    async def test_assets_networks_and_population_come_across(self, tmp_path, pg):
        await self._seed(tmp_path)
        await self._migrate(tmp_path, pg)

        rows, _ = await pg.query_assets(AOI, BBOX)
        assert [r["name"] for r in rows] == ["keep-me"]
        import json
        assert json.loads(rows[0]["attributes"]) == {"beds": 7}
        assert len(await pg.query_networks(AOI, BBOX)) == 1
        cells = await pg.query_population(
            "POLYGON((2.1 41.3,2.15 41.3,2.15 41.35,2.1 41.35,2.1 41.3))")
        assert cells[0]["frac"] == pytest.approx(0.25, abs=0.01)

    async def test_the_enrichment_cache_survives(self, tmp_path, pg):
        """An hour of somebody else's geocoder. Losing it means asking for it again."""
        await self._seed(tmp_path)
        await self._migrate(tmp_path, pg)
        assert await pg.cache_get("geocode:abc") == {"lon": 2.1, "lat": 41.4}

    async def test_running_it_twice_does_not_duplicate(self, tmp_path, pg):
        """A migration that fails halfway has to be safe to re-run."""
        await self._seed(tmp_path)
        await self._migrate(tmp_path, pg)
        import argparse

        from talaia.cli import cmd_migrate
        assert await cmd_migrate(argparse.Namespace(
            db=str(tmp_path / "old.duckdb"), to=DSN, batch=500)) == 0

        rows, _ = await pg.query_assets(AOI, BBOX)
        assert len(rows) == 1
        assert (await pg.fetch("SELECT count(*) FROM api_keys"))[0][0] == 1


# ---------------------------------------------------------------------------
# Self-service signup, end to end, on Postgres
# ---------------------------------------------------------------------------
@needs_pg
class TestSignupEndToEnd:
    """Signup returned 500 on the first Postgres deploy.

    The per-IP throttle asked for `created_at > (current_timestamp - INTERVAL 24 HOUR)`,
    which is DuckDB's spelling of an interval and a syntax error in Postgres. It was
    never covered because the existing signup tests drive the registry directly rather
    than the route, so nothing executed that particular string against a database.
    """

    @pytest.fixture
    def client(self, monkeypatch):
        from fastapi.testclient import TestClient

        for name, value in {
            "TALAIA_DATABASE_URL": DSN, "TALAIA_ADMIN_KEY": "admin-secret",
            "TALAIA_REQUIRE_AUTH": "true", "TALAIA_AUTO_BOOTSTRAP": "false",
            "TALAIA_ALLOW_SIGNUP": "true", "TALAIA_REQUIRE_EMAIL_VERIFICATION": "false",
            "TALAIA_ENABLE_LIVE_OSM": "false", "TALAIA_API_KEYS": "",
            "TALAIA_WARM_ON_BOOT": "", "TALAIA_SIGNUPS_PER_IP_PER_DAY": "2",
        }.items():
            monkeypatch.setenv(name, value)

        import importlib

        import talaia.config as config
        config.get_settings.cache_clear()
        fresh = config.Settings()
        monkeypatch.setattr(config, "settings", fresh)
        for module in ("talaia.main", "talaia.store", "talaia.pgstore", "talaia.auth",
                       "talaia.routers.v1", "talaia.diagnostics", "talaia.mailer"):
            mod = importlib.import_module(module)
            if hasattr(mod, "settings"):
                monkeypatch.setattr(mod, "settings", fresh)

        import psycopg
        with psycopg.connect(DSN, autocommit=True) as con:
            con.execute("DELETE FROM api_keys")
            con.execute("DELETE FROM signups")
            con.execute("DELETE FROM pending_signups")

        from talaia.main import app
        with TestClient(app) as c:
            yield c

    def test_signup_issues_a_key(self, client):
        r = client.post("/v1/signup", json={"email": "a@example.com",
                                            "organisation": "Org"})
        assert r.status_code in (200, 202), r.text
        assert r.json().get("api_key", "").startswith("talaia_sk_")

    def test_the_per_ip_throttle_actually_counts(self, client):
        """The query that broke. It has to run *and* return the right number - a version
        that merely parsed but compared against the wrong clock would let the cap through
        silently."""
        for i in range(2):
            r = client.post("/v1/signup", json={"email": f"t{i}@example.com"})
            assert r.status_code in (200, 202), r.text
        blocked = client.post("/v1/signup", json={"email": "t2@example.com"})
        assert blocked.status_code == 429, blocked.text
        assert "24 hours" in blocked.json()["detail"]

    def test_one_active_key_per_address(self, client):
        assert client.post("/v1/signup",
                           json={"email": "dup@example.com"}).status_code in (200, 202)
        again = client.post("/v1/signup", json={"email": "dup@example.com"})
        assert again.status_code == 409

    def test_a_signup_row_is_recorded_with_a_usable_timestamp(self, client):
        """created_at is written by the app as naive UTC. If the database wrote its own
        clock instead, the throttle above would compare against the wrong instant."""
        assert client.post("/v1/signup",
                           json={"email": "ts@example.com"}).status_code in (200, 202)
        import psycopg
        with psycopg.connect(DSN, autocommit=True) as con:
            (created,), = con.execute(
                "SELECT created_at FROM signups WHERE email = 'ts@example.com'").fetchall()
        assert abs((created - _utcnow()).total_seconds()) < 120

    def test_regions_endpoint_runs_its_freshness_query(self, client):
        """/v1/regions carried the same INTERVAL spelling, so it would have 500ed too."""
        r = client.get("/v1/regions")
        assert r.status_code == 200, r.text
        assert isinstance(r.json(), (list, dict))
