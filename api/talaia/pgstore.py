"""PostgreSQL + PostGIS store.

Why this exists
---------------
DuckDB is an excellent embedded analytical engine and a poor fit for a service that
several people and several ingests use at once, because it takes exactly one writer. In
this application that single writer was not a throughput limit, it was an availability
one: an ingest that stopped making progress held the writer, and every later write -
creating an API key, revoking one, recording a signup, marking a cached tile - queued
behind it until the request timed out. From the outside that looks like "none of the
buttons do anything", which is precisely what it looked like.

Postgres removes the whole class of problem rather than the particular instance:

* MVCC, so readers never block writers and writers never block readers. Two ingests, a
  signup and a tile-cache write genuinely proceed at the same time.
* Row-level locking, so concurrent writers only contend when they touch the same row.
  Two connectors writing different sources never meet.
* A stuck statement is visible in ``pg_stat_activity`` and can be cancelled by name with
  ``pg_cancel_backend``, instead of being an opaque thread inside this process.
* Crash recovery that does not depend on this process shutting down cleanly, so a
  restart - or a ``SIGKILL`` mid-ingest - cannot leave a file that refuses writes.

The public API is deliberately identical to ``store.Store``. Everything above this layer
- all twelve connectors, conflation, scoring, valuation, the aggregator, the routers, the
MCP server - goes through these methods and does not know which engine answered.

Placeholders
------------
Call sites across the codebase are written in DuckDB's ``?`` style, and several use
``INSERT OR REPLACE``. Rather than rewrite thirty call sites into a dialect neither
engine speaks natively, ``_translate`` converts both on the way in. That keeps the DuckDB
path byte-for-byte unchanged - it is still what the test suite and local development run
- and confines the dialect difference to one tested function.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

import psycopg
from psycopg_pool import AsyncConnectionPool

from .config import settings
from .store import (ASSET_COLUMNS, NETWORK_COLUMNS, StoreBusy, _as_mb, _j,  # noqa: F401
                    _utcnow)

log = logging.getLogger("talaia.pgstore")

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "sql" / "schema_postgres.sql"

# Every table's primary key, so `INSERT OR REPLACE` can be rewritten into the
# `ON CONFLICT` form Postgres wants. Hardcoded rather than introspected because the
# schema is ours and a wrong guess here would silently duplicate rows.
PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    "assets": ("id",),
    "networks": ("id",),
    "pop_grid": ("cell_id",),
    "osm_tile_cache": ("tile_key",),
    "enrichment_cache": ("key",),
    "source_runs": ("run_id",),
    "api_keys": ("key_hash",),
    "key_usage": ("key_hash", "day"),
    "pending_signups": ("token_hash",),
    "signups": ("id",),
}

# A `?` that is not inside a single-quoted literal. Matching the literal first and
# handing it back unchanged is what keeps a question mark inside a string from being
# turned into a parameter.
_QMARK = re.compile(r"'(?:[^']|'')*'|\?")
_OR_REPLACE = re.compile(
    r"^\s*INSERT\s+OR\s+REPLACE\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]*)\)",
    re.IGNORECASE | re.DOTALL)

# Columns that hold JSON. Staged as text and cast on the way in, so a value the
# pipeline produced as a string is stored as jsonb without a round trip through a
# Python dict.
_JSON_COLS = ("address", "contacts", "capacity", "attributes", "payload")

_STAGE_TYPES = {
    "lon": "DOUBLE PRECISION", "lat": "DOUBLE PRECISION",
    "footprint_m2": "DOUBLE PRECISION", "floors": "DOUBLE PRECISION",
    "confidence": "DOUBLE PRECISION", "population": "DOUBLE PRECISION",
    "area_m2": "DOUBLE PRECISION", "min_lon": "DOUBLE PRECISION",
    "min_lat": "DOUBLE PRECISION", "max_lon": "DOUBLE PRECISION",
    "max_lat": "DOUBLE PRECISION", "year": "INTEGER",
    "feature_count": "INTEGER", "network_count": "INTEGER",
    "retrieved_at": "TIMESTAMP", "fetched_at": "TIMESTAMP",
}


def _qmarks(sql: str) -> str:
    return _QMARK.sub(lambda m: m.group(0) if m.group(0)[0] == "'" else "%s", sql)


def _translate(sql: str) -> str:
    """Rewrite one DuckDB statement into its Postgres equivalent.

    Two transformations, both purely syntactic:

    1. ``INSERT OR REPLACE INTO t (a, b, c)`` becomes ``INSERT INTO t (a, b, c) ...
       ON CONFLICT (pk) DO UPDATE SET b = EXCLUDED.b, c = EXCLUDED.c``. Note that this
       is genuinely closer to the intent than DuckDB's own verb: `INSERT OR REPLACE`
       deletes and reinserts, so a column the statement does not mention reverts to its
       default. `ON CONFLICT DO UPDATE` leaves it alone.
    2. ``?`` becomes ``%s``, except inside a string literal.
    """
    m = _OR_REPLACE.match(sql)
    if m:
        table = m.group(1)
        columns = [c.strip() for c in m.group(2).split(",") if c.strip()]
        pk = PRIMARY_KEYS.get(table.lower())
        if pk is None:
            raise ValueError(
                f"INSERT OR REPLACE INTO {table}: no primary key is registered for that "
                f"table, so the ON CONFLICT target cannot be determined. Add it to "
                f"PRIMARY_KEYS in pgstore.py.")
        updates = [f"{c} = EXCLUDED.{c}" for c in columns if c.lower() not in pk]
        body = sql[m.end():]
        action = (f" DO UPDATE SET {', '.join(updates)}" if updates else " DO NOTHING")
        sql = (f"INSERT INTO {table} ({', '.join(columns)}){body} "
               f"ON CONFLICT ({', '.join(pk)}){action}")
    return _qmarks(sql)


class PostgresStore:
    """Same surface as ``store.Store``; concurrent underneath."""

    backend = "postgres"

    def __init__(self, dsn: str | None = None):
        self.dsn = dsn or settings.database_url
        if not self.dsn:
            raise RuntimeError("PostgresStore needs a DSN (TALAIA_DATABASE_URL)")
        self._pool: AsyncConnectionPool | None = None
        self._pool_open = False
        self._open_lock = asyncio.Lock()
        self._density_cache: float | None = None
        self._last_write_ok: datetime | None = None
        self._writes_in_flight: int = 0
        self._writes_failed: int = 0
        self._interrupts: int = 0
        # Kept so diagnostics and anything else that reaches for it still works; on
        # Postgres there is no local database file.
        self.db_path = settings.db_path

    # -- lifecycle ---------------------------------------------------------
    def connect(self) -> None:
        """Apply the schema and build the pool.

        Deliberately synchronous, matching ``Store.connect()``, so boot ordering in
        ``main.lifespan`` and the CLI is unchanged. The schema runs over a one-shot
        blocking connection; the async pool this process actually serves from is opened
        on first use, because an event loop may not be running yet.
        """
        self._apply_schema()
        if self._pool is not None:
            # Already built. Boot re-applies the schema on every deploy, which is
            # idempotent, but replacing a live pool here would orphan its connections
            # and leave `_pool_open` describing the pool that just went away.
            return
        self._pool = AsyncConnectionPool(
            self.dsn,
            min_size=settings.pg_pool_min,
            max_size=settings.pg_pool_max,
            timeout=settings.pg_pool_timeout_s,
            max_lifetime=settings.pg_pool_max_lifetime_s,
            kwargs={"application_name": "talaia",
                    "options": f"-c statement_timeout={int(settings.pg_statement_timeout_s * 1000)}"},
            open=False,
        )
        log.info("postgres store ready (pool %d-%d)",
                 settings.pg_pool_min, settings.pg_pool_max)

    def _connect_sync(self) -> psycopg.Connection:
        """A one-off blocking connection, for schema work and diagnostics.

        ``TimeZone=UTC`` matters more than it looks: every timestamp column here is
        `timestamp without time zone` holding UTC, and several comparisons are against
        cutoffs computed in Python. On a server set to anything else, `current_timestamp`
        written into one of those columns would be local time, and a 30-minute retry
        backoff would quietly never match.
        """
        return psycopg.connect(
            self.dsn, autocommit=True, application_name="talaia-admin",
            options="-c timezone=UTC",
            connect_timeout=int(settings.pg_connect_timeout_s))

    def _apply_schema(self) -> None:
        raw = SCHEMA_PATH.read_text()
        stripped = "\n".join(line.split("--", 1)[0] for line in raw.splitlines())
        statements = [s.strip() for s in stripped.split(";") if s.strip()]
        with self._connect_sync() as con:
            with con.cursor() as cur:
                for stmt in statements:
                    try:
                        cur.execute(stmt)
                    except Exception as exc:
                        if "postgis" in stmt.lower():
                            raise RuntimeError(
                                "PostGIS is not available on this database. TALAIA "
                                "stores geometry, so a plain Postgres image is not "
                                "enough - deploy the PostGIS template (see "
                                "docs/DEPLOY-RAILWAY.md) and point TALAIA_DATABASE_URL "
                                f"at it. The server said: {exc}") from exc
                        log.warning("schema statement failed: %s -- %s",
                                    stmt[:70], exc)
                for stmt in self._MIGRATIONS:
                    try:
                        cur.execute(stmt)
                    except Exception as exc:  # pragma: no cover - already applied
                        log.debug("migration skipped (%s): %s", stmt[:48], exc)

    _MIGRATIONS: list[str] = [
        "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS tier TEXT DEFAULT 'free'",
        "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS daily_quota INTEGER DEFAULT 0",
        "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS max_aoi_km2 DOUBLE PRECISION DEFAULT 0",
        "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS max_assets INTEGER DEFAULT 20000",
        "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS email TEXT",
        "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS organisation TEXT",
        "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS created_ip TEXT",
        "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS custom_limits BOOLEAN DEFAULT FALSE",
        "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS email_verified BOOLEAN DEFAULT FALSE",
    ]

    async def _opened(self) -> AsyncConnectionPool:
        if self._pool is None:
            raise RuntimeError("PostgresStore.connect() has not been called")
        if not self._pool_open:
            async with self._open_lock:
                if not self._pool_open:
                    await self._pool.open(wait=True, timeout=settings.pg_pool_timeout_s)
                    self._pool_open = True
        return self._pool

    async def aclose(self) -> None:
        """Close the pool and wait for it. This is the one to call from shutdown."""
        pool, self._pool = self._pool, None
        self._pool_open = False
        if pool is not None:
            await pool.close()

    def close(self) -> None:
        """Synchronous close, for parity with ``Store.close()``.

        An async pool cannot be shut down from a synchronous frame while its loop is
        running, so with a loop present this schedules the close and returns. Callers
        inside an event loop - the app's shutdown, the test fixtures - should await
        ``aclose()`` instead and get a clean wait rather than a best effort.
        """
        pool, self._pool = self._pool, None
        self._pool_open = False
        if pool is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop, so nothing is holding a connection either
        task = loop.create_task(pool.close())
        # Without this the interpreter reports "Task was destroyed but it is pending"
        # when the loop stops first, which looks like a fault and is not one.
        task.add_done_callback(lambda t: t.cancelled() or t.exception())

    # -- generic helpers ---------------------------------------------------
    async def fetch(self, sql: str, params: Sequence[Any] | None = None) -> list[tuple]:
        pool = await self._opened()
        async with pool.connection() as con:
            async with con.cursor() as cur:
                await cur.execute(_translate(sql), list(params or []))
                return await cur.fetchall() if cur.description else []

    def fetch_sync(self, sql: str, params: Sequence[Any] | None = None) -> list[tuple]:
        """For diagnostics, which must answer even when the pool is the problem."""
        with self._connect_sync() as con:
            with con.cursor() as cur:
                cur.execute(_translate(sql), list(params or []))
                return cur.fetchall() if cur.description else []

    async def execute_write(self, sql: str, params: Sequence[Any] | None = None) -> None:
        """Run one write.

        No lock. That is the entire point of this backend: `execute_write` on DuckDB had
        to serialise behind the single writer, so an admin action could be held up
        indefinitely by an unrelated bulk ingest. Here they are separate transactions on
        separate connections and neither waits for the other.
        """
        async with self._write("execute_write"):
            pool = await self._opened()
            async with pool.connection() as con:
                async with con.cursor() as cur:
                    await cur.execute(_translate(sql), list(params or []))

    @asynccontextmanager
    async def _write(self, what: str):
        """Bookkeeping only - it takes no lock and blocks nothing.

        ``write_health`` and the admin banner were built against the DuckDB backend and
        still want to know whether writes are working. Here the interesting facts are
        how many are in flight and when the last one succeeded, not who is holding a
        lock, because nobody is.
        """
        self._writes_in_flight += 1
        try:
            yield
            self._last_write_ok = _utcnow()
        except Exception:
            self._writes_failed += 1
            raise
        finally:
            self._writes_in_flight -= 1

    # -- health ------------------------------------------------------------
    def write_health(self) -> dict[str, Any]:
        stats = {}
        if self._pool is not None:
            try:
                s = self._pool.get_stats()
                stats = {"size": s.get("pool_size"), "available": s.get("pool_available"),
                         "waiting": s.get("requests_waiting")}
            except Exception:  # pragma: no cover - stats are advisory
                pass
        return {
            # There is no single writer to be blocked behind, so this is structurally
            # false rather than "false for now". The admin banner keys are kept so the
            # web app renders either backend without a special case.
            "blocked": False,
            "holder": None,
            "held_for_s": 0.0,
            "last_write_ok": self._last_write_ok.isoformat() if self._last_write_ok else None,
            "writes_refused": 0,
            "queries_interrupted": self._interrupts,
            "backend": "postgres",
            "writes_in_flight": self._writes_in_flight,
            "writes_failed": self._writes_failed,
            "pool": stats,
        }

    def interrupt_live_queries(self) -> int:
        """Cancel our own long-running statements.

        On DuckDB this was a blunt instrument aimed at a thread that could not otherwise
        be reached. Here it is a precise one: Postgres knows exactly which backends are
        running which statements, and cancelling one leaves every other connection
        untouched. Scoped to this application's connections so it can never disturb
        anything else sharing the database.
        """
        cancelled = 0
        try:
            with self._connect_sync() as con:
                with con.cursor() as cur:
                    cur.execute(
                        "SELECT pg_cancel_backend(pid) FROM pg_stat_activity "
                        "WHERE application_name = 'talaia' AND state = 'active' "
                        "AND pid <> pg_backend_pid() "
                        "AND now() - query_start > make_interval(secs => %s)",
                        [float(settings.write_stuck_after_s)])
                    cancelled = sum(1 for (ok,) in cur.fetchall() if ok)
        except Exception as exc:  # pragma: no cover - best effort by nature
            log.warning("could not cancel running statements: %s", exc)
        self._interrupts += cancelled
        return cancelled

    def checkpoint(self) -> None:
        """No-op. Postgres manages its own WAL; there is nothing to fold in by hand."""

    async def checkpoint_now(self) -> None:
        """Refresh planner statistics after a bulk load.

        The DuckDB store checkpointed here to bound WAL growth. Postgres needs no such
        help, but the same moment - the end of an ingest - is exactly when the planner's
        row estimates for the table have just gone stale, so the call is worth keeping
        and worth repurposing.
        """
        try:
            pool = await self._opened()
            async with pool.connection() as con:
                await con.execute("ANALYZE assets")
                await con.execute("ANALYZE networks")
        except Exception as exc:  # pragma: no cover - advisory
            log.debug("post-ingest ANALYZE skipped: %s", exc)

    # -- bulk writes -------------------------------------------------------
    @staticmethod
    def _decl(columns: Sequence[str]) -> str:
        return ", ".join(f"{c} {_STAGE_TYPES.get(c, 'TEXT')}" for c in columns)

    async def _copy_upsert(self, table: str, columns: list[str], values: list[tuple],
                           select_parts: list[str], target_cols: list[str],
                           where: str = "") -> int:
        """Stage with COPY, then upsert in one statement.

        COPY is Postgres's bulk path and is an order of magnitude faster than even a
        multi-row INSERT: the rows go over the wire in a single stream with no statement
        parsing per chunk. Staging into a temp table first, rather than COPYing straight
        into the target, is what lets the geometry be built and the conflict resolved
        server-side in one pass.

        ``ON COMMIT DROP`` ties the temp table to this transaction, so a pooled
        connection can never hand the next caller a half-filled staging table.
        """
        if not values:
            return 0
        pk = PRIMARY_KEYS[table]
        updates = [f"{c} = EXCLUDED.{c}" for c in target_cols if c.lower() not in pk]
        action = f"DO UPDATE SET {', '.join(updates)}" if updates else "DO NOTHING"
        pool = await self._opened()
        async with pool.connection() as con:
            async with con.cursor() as cur:
                await cur.execute(
                    f"CREATE TEMP TABLE _incoming ({self._decl(columns)}) ON COMMIT DROP")
                copy_sql = f"COPY _incoming ({', '.join(columns)}) FROM STDIN"
                async with cur.copy(copy_sql) as copy:
                    for row in values:
                        await copy.write_row(row)
                await cur.execute(
                    f"INSERT INTO {table} ({', '.join(target_cols)}) "
                    f"SELECT {', '.join(select_parts)} FROM _incoming {where} "
                    f"ON CONFLICT ({', '.join(pk)}) {action}")
        return len(values)

    def _prepare(self, columns: list[str], rows: list[dict]) -> tuple[list[str], list[tuple]]:
        columns = list(columns)
        if "retrieved_at" not in columns:
            columns.append("retrieved_at")
        stamp = _utcnow()
        values: list[tuple] = []
        for r in rows:
            item = []
            for c in columns:
                v = r.get(c)
                if c in _JSON_COLS:
                    v = _j(v)
                elif c == "retrieved_at":
                    v = v or stamp
                item.append(v)
            values.append(tuple(item))
        return columns, values

    @staticmethod
    def _geom_selects(columns: list[str], with_bbox: bool) -> tuple[list[str], list[str]]:
        select_parts, target_cols = [], []
        for c in columns:
            if c == "wkt":
                select_parts.append("ST_GeomFromText(wkt, 4326) AS geom")
                target_cols.append("geom")
            elif c in _JSON_COLS:
                select_parts.append(f"{c}::jsonb AS {c}")
                target_cols.append(c)
            else:
                select_parts.append(c)
                target_cols.append(c)
        if with_bbox:
            # Precomputed at write time so a caller that wants a cheap rectangular
            # filter - or a sanity check on a geometry - does not pay for the geometry.
            for fn, col in (("ST_XMin", "bbox_min_lon"), ("ST_YMin", "bbox_min_lat"),
                            ("ST_XMax", "bbox_max_lon"), ("ST_YMax", "bbox_max_lat")):
                select_parts.append(f"{fn}(ST_GeomFromText(wkt, 4326)) AS {col}")
                target_cols.append(col)
        return select_parts, target_cols

    async def upsert_assets(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        async with self._write("upsert_assets"):
            columns, values = self._prepare(ASSET_COLUMNS, rows)
            sel, tgt = self._geom_selects(columns, with_bbox=True)
            n = await self._copy_upsert("assets", columns, values, sel, tgt,
                                        "WHERE wkt IS NOT NULL AND wkt <> ''")
        self.invalidate_density()
        return n

    async def upsert_networks(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        async with self._write("upsert_networks"):
            columns, values = self._prepare(NETWORK_COLUMNS, rows)
            sel, tgt = self._geom_selects(columns, with_bbox=True)
            return await self._copy_upsert("networks", columns, values, sel, tgt,
                                           "WHERE wkt IS NOT NULL AND wkt <> ''")

    async def upsert_popgrid(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        cols = ["cell_id", "wkt", "population", "area_m2", "source_id", "year"]
        values = [tuple(r.get(c) for c in cols) for r in rows]
        sel = ["cell_id", "ST_GeomFromText(wkt, 4326)", "population", "area_m2",
               "source_id", "year"]
        tgt = ["cell_id", "geom", "population", "area_m2", "source_id", "year"]
        async with self._write("upsert_popgrid"):
            return await self._copy_upsert("pop_grid", cols, values, sel, tgt,
                                           "WHERE wkt IS NOT NULL")

    async def delete_source(self, source_id: str) -> None:
        await self.execute_write("DELETE FROM assets WHERE source_id = ?", [source_id])

    # -- spatial reads -----------------------------------------------------
    # One plan, not two. The DuckDB backend chooses between its R-tree and a vectorised
    # bbox scan because its R-tree is only usable for a handful of matches. A PostGIS
    # GiST index has no such ceiling and the planner costs it properly against a
    # sequential scan on its own, so second-guessing it here would only be a way to be
    # wrong occasionally.
    def choose_strategy(self, bbox, table: str = "assets") -> str:
        return "gist"

    def invalidate_density(self) -> None:
        self._density_cache = None

    async def query_assets(self, wkt: str, bbox, categories: Sequence[str] | None = None,
                           limit: int = 20_000, sources: Sequence[str] | None = None,
                           strategy: str | None = None) -> tuple[list[dict], str]:
        where = ["ST_Intersects(geom, ST_GeomFromText(%s, 4326))"]
        params: list[Any] = [wkt]
        if categories:
            where.append("category = ANY(%s)")
            params.append(list(categories))
        if sources:
            where.append("source_id = ANY(%s)")
            params.append(list(sources))
        sql = f"""
            SELECT id, source_id, source_ref, category, subcategory, name,
                   ST_AsGeoJSON(geom) AS geojson, lon, lat, geometry_kind,
                   address::text, contacts::text, capacity::text, attributes::text,
                   footprint_m2, floors, confidence, retrieved_at
            FROM assets WHERE {' AND '.join(where)} LIMIT {int(limit)}
        """
        cols = ["id", "source_id", "source_ref", "category", "subcategory", "name",
                "geojson", "lon", "lat", "geometry_kind", "address", "contacts",
                "capacity", "attributes", "footprint_m2", "floors", "confidence",
                "retrieved_at"]
        rows = await self.fetch(sql, params)
        return [dict(zip(cols, r)) for r in rows], "gist"

    async def query_networks(self, wkt: str, bbox,
                             subcategories: Sequence[str] | None = None,
                             limit: int = 20_000) -> list[dict]:
        where = ["ST_Intersects(geom, ST_GeomFromText(%s, 4326))"]
        params: list[Any] = [wkt, wkt, wkt]
        if subcategories:
            where.append("subcategory = ANY(%s)")
        sql = f"""
            SELECT id, source_id, category, subcategory, name,
                   ST_AsGeoJSON(ST_Intersection(geom, ST_GeomFromText(%s, 4326))) AS geojson,
                   ST_Length(geography(ST_Intersection(geom, ST_GeomFromText(%s, 4326))))
                     AS clipped_m,
                   attributes::text
            FROM networks WHERE {' AND '.join(where)} LIMIT {int(limit)}
        """
        if subcategories:
            params.append(list(subcategories))
        cols = ["id", "source_id", "category", "subcategory", "name", "geojson",
                "clipped_m", "attributes"]
        rows = await self.fetch(sql, params)
        return [dict(zip(cols, r)) for r in rows]

    async def query_population(self, wkt: str) -> list[dict]:
        sql = """
            SELECT cell_id, population, area_m2,
                   ST_Area(geography(ST_Intersection(geom, ST_GeomFromText(%s, 4326))))
                     / NULLIF(ST_Area(geography(geom)), 0) AS frac,
                   ST_X(ST_Centroid(geom)) AS lon,
                   ST_Y(ST_Centroid(geom)) AS lat,
                   ST_AsGeoJSON(geom) AS geojson
            FROM pop_grid
            WHERE ST_Intersects(geom, ST_GeomFromText(%s, 4326))
        """
        cols = ["cell_id", "population", "area_m2", "frac", "lon", "lat", "geojson"]
        rows = await self.fetch(sql, [wkt, wkt])
        return [dict(zip(cols, r)) for r in rows]

    # -- tile cache --------------------------------------------------------
    async def stale_tiles(self, tile_keys: list[str], ttl_hours: int | None = None,
                          error_backoff_minutes: int | None = None) -> set[str]:
        if not tile_keys:
            return set()
        ttl = settings.osm_tile_ttl_hours if ttl_hours is None else ttl_hours
        backoff = (settings.osm_error_retry_minutes if error_backoff_minutes is None
                   else error_backoff_minutes)
        now = _utcnow()
        ttl_cutoff = now - timedelta(hours=int(ttl))
        error_cutoff = (now - timedelta(minutes=int(backoff)) if backoff > 0 else None)
        rows = await self.fetch(
            "SELECT tile_key FROM osm_tile_cache WHERE tile_key = ANY(%s) "
            "AND ((status = 'ok' AND fetched_at > %s) "
            "  OR (status <> 'ok' AND %s::timestamp IS NOT NULL AND fetched_at > %s))",
            [list(tile_keys), ttl_cutoff, error_cutoff, error_cutoff])
        return set(tile_keys) - {r[0] for r in rows}

    async def tile_states(self, tile_keys: list[str], ttl_hours: int | None = None,
                          error_backoff_minutes: int | None = None) -> dict[str, str]:
        if not tile_keys:
            return {}
        ttl = settings.osm_tile_ttl_hours if ttl_hours is None else ttl_hours
        backoff = (settings.osm_error_retry_minutes if error_backoff_minutes is None
                   else error_backoff_minutes)
        now = _utcnow()
        ttl_cutoff = now - timedelta(hours=int(ttl))
        error_cutoff = (now - timedelta(minutes=int(backoff)) if backoff > 0 else None)
        states = {k: "stale" for k in tile_keys}
        rows = await self.fetch(
            "SELECT tile_key, status, fetched_at FROM osm_tile_cache "
            "WHERE tile_key = ANY(%s)", [list(tile_keys)])
        for key, status, fetched_at in rows:
            if fetched_at is None:
                continue
            if status == "ok" and fetched_at > ttl_cutoff:
                states[key] = "fresh"
            elif (status != "ok" and error_cutoff is not None
                  and fetched_at > error_cutoff):
                states[key] = "failed"
        return states

    _TILE_COLS = ["tile_key", "min_lon", "min_lat", "max_lon", "max_lat", "fetched_at",
                  "status", "feature_count", "network_count", "error"]

    async def mark_tiles(self, tiles: list[dict]) -> None:
        if not tiles:
            return
        values = [tuple(t.get(c) for c in self._TILE_COLS) for t in tiles]
        sel = [*self._TILE_COLS, "fetched_at"]
        tgt = [*self._TILE_COLS, "last_hit_at"]
        async with self._write("mark_tiles"):
            await self._copy_upsert("osm_tile_cache", self._TILE_COLS, values, sel, tgt)

    async def clear_tile_assets(self, tile_keys: list[str]) -> None:
        if not tile_keys:
            return
        async with self._write("clear_tile_assets"):
            pool = await self._opened()
            async with pool.connection() as con:
                async with con.cursor() as cur:
                    await cur.execute(
                        "DELETE FROM assets WHERE tile_key = ANY(%s)", [list(tile_keys)])
                    await cur.execute(
                        "DELETE FROM networks WHERE tile_key = ANY(%s)", [list(tile_keys)])

    # -- enrichment cache --------------------------------------------------
    async def cache_get(self, key: str) -> dict | None:
        rows = await self.fetch(
            "SELECT payload::text FROM enrichment_cache WHERE key = %s", [key])
        if not rows:
            return None
        value = rows[0][0]
        if isinstance(value, dict):  # a jsonb that psycopg already parsed
            return value
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return None

    async def cache_put(self, key: str, kind: str, payload: dict) -> None:
        await self.execute_write(
            "INSERT INTO enrichment_cache (key, kind, payload, created_at) "
            "VALUES (%s, %s, %s::jsonb, timezone('utc', now())) "
            "ON CONFLICT (key) DO UPDATE SET kind = EXCLUDED.kind, "
            "payload = EXCLUDED.payload, created_at = EXCLUDED.created_at",
            [key, kind, _j(payload)])

    # -- ingest audit ------------------------------------------------------
    async def record_run(self, source_id: str, status: str, rows: int,
                         started_at: datetime, error: str | None = None) -> None:
        await self.execute_write(
            "INSERT INTO source_runs "
            "(run_id, source_id, started_at, finished_at, status, rows, error) "
            "VALUES (%s, %s, %s, timezone('utc', now()), %s, %s, %s)",
            [str(uuid.uuid4()), source_id, started_at, status, rows, error])

    async def source_stats(self) -> dict[str, dict]:
        counts: dict[str, int] = {}
        for table in ("assets", "networks", "pop_grid"):
            for sid, n in await self.fetch(
                    f"SELECT source_id, count(*) FROM {table} GROUP BY source_id"):
                if sid:
                    counts[sid] = counts.get(sid, 0) + n
        # DISTINCT ON is Postgres's arg_max: one row per source_id, and ORDER BY decides
        # which one. NULLS LAST so a run still in progress - finished_at not yet set -
        # cannot outrank the completed run that carries the real status.
        runs = await self.fetch(
            "SELECT DISTINCT ON (source_id) source_id, finished_at, status, error "
            "FROM source_runs ORDER BY source_id, finished_at DESC NULLS LAST")
        out: dict[str, dict] = {}
        for sid, cnt in counts.items():
            out[sid] = {"rows": cnt, "last_run_at": None, "last_status": "loaded",
                        "last_error": None}
        for sid, finished, status, err in runs:
            entry = out.setdefault(sid, {"rows": 0, "last_status": "never_run",
                                         "last_error": None})
            entry["last_run_at"] = finished
            entry["last_status"] = status
            entry["last_error"] = err
        return out

    async def stats(self) -> dict:
        async def one(sql: str, params: list | None = None) -> Any:
            rows = await self.fetch(sql, params or [])
            return rows[0][0] if rows else 0

        ttl_cutoff = _utcnow() - timedelta(hours=settings.osm_tile_ttl_hours)
        by_category = await self.fetch(
            "SELECT category, count(*) FROM assets GROUP BY 1 ORDER BY 2 DESC")
        by_source = await self.fetch(
            "SELECT source_id, count(*) FROM assets GROUP BY 1 ORDER BY 2 DESC")
        return {
            "assets": await one("SELECT count(*) FROM assets"),
            "networks": await one("SELECT count(*) FROM networks"),
            "population_cells": await one("SELECT count(*) FROM pop_grid"),
            "cached_tiles": await one(
                "SELECT count(*) FROM osm_tile_cache WHERE status='ok' "
                "AND fetched_at > %s", [ttl_cutoff]),
            "failed_tiles": await one(
                "SELECT count(*) FROM osm_tile_cache WHERE status <> 'ok'"),
            "enrichment_entries": await one("SELECT count(*) FROM enrichment_cache"),
            "sources": await one(
                "SELECT count(*) FROM ("
                "  SELECT source_id FROM assets"
                "  UNION SELECT source_id FROM networks"
                "  UNION SELECT source_id FROM pop_grid) s"),
            "assets_by_category": dict(by_category),
            "assets_by_source": dict(by_source),
            "db_size_mb": round(
                (await one("SELECT pg_database_size(current_database())")) / 1e6, 2),
        }

    async def export_parquet(self, out_dir: Path) -> dict[str, int]:
        raise NotImplementedError(
            "Parquet export is a DuckDB feature. Snapshot a Postgres deployment with "
            "pg_dump, or run the exporter against a DuckDB copy of the data.")
