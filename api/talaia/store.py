"""DuckDB-backed spatial store.

Design notes
------------
* DuckDB is embedded: no second service, no connection pool, no network hop.
* Reads use per-thread cursors and run in a worker thread so the event loop never blocks.
* DuckDB is single-writer, so every write funnels through ``_write_lock``. Reads are
  unaffected and stay concurrent.
* Geometry is always WGS84 lon/lat. Distances/areas are computed by projecting to a local
  azimuthal approximation at query time rather than storing a projected duplicate.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import duckdb

from .config import settings

log = logging.getLogger("talaia.store")

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "sql" / "schema.sql"

ASSET_COLUMNS = [
    "id", "source_id", "source_ref", "category", "subcategory", "name", "wkt",
    "lon", "lat", "geometry_kind", "address", "contacts", "capacity", "attributes",
    "footprint_m2", "floors", "confidence", "tile_key", "retrieved_at",
]
NETWORK_COLUMNS = [
    "id", "source_id", "source_ref", "category", "subcategory", "name", "wkt",
    "attributes", "tile_key", "retrieved_at",
]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _j(value: Any) -> str:
    if value is None:
        return "{}"
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


class Store:
    def __init__(self, db_path: Path | str | None = None):
        self.db_path = Path(db_path) if db_path else settings.db_path
        self._con: duckdb.DuckDBPyConnection | None = None
        self._write_lock = asyncio.Lock()
        self._density_cache: float | None = None

    # -- lifecycle ---------------------------------------------------------
    def connect(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._con = duckdb.connect(str(self.db_path))
        self._con.execute("INSTALL spatial; LOAD spatial; INSTALL json; LOAD json;")
        self._con.execute(f"SET memory_limit='{settings.duckdb_memory_limit}'")
        self._con.execute(f"SET threads={settings.duckdb_threads}")
        self._apply_schema()
        self._warm_indexes()
        log.info("store ready at %s", self.db_path)

    def _apply_schema(self) -> None:
        """Apply DDL. Comments are stripped first: a ';' inside a '--' comment would
        otherwise split a statement in half and silently skip a table."""
        raw = SCHEMA_PATH.read_text()
        stripped = "\n".join(
            line.split("--", 1)[0] for line in raw.splitlines()
        )
        for stmt in [s.strip() for s in stripped.split(";") if s.strip()]:
            if stmt.upper().startswith(("INSTALL", "LOAD")):
                continue
            try:
                self._con.execute(stmt)
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("schema statement failed: %s -- %s", stmt[:70], exc)

    def _warm_indexes(self) -> None:
        """Touch each R-tree once at boot.

        The first spatial query on a cold connection pays ~400 ms to page the index in;
        steady state is ~15 ms. Paying that during startup instead of inside the first
        user request is the difference between a good and a bad first impression.
        """
        probe = "POLYGON((0 40,0.001 40,0.001 40.001,0 40.001,0 40))"
        for table in ("assets", "networks", "pop_grid"):
            try:
                self._con.execute(
                    f"SELECT count(*) FROM {table} "
                    f"WHERE ST_Intersects(geom, ST_GeomFromText('{probe}'))"
                ).fetchall()
            except Exception:  # pragma: no cover - table may not exist yet
                pass

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None

    @contextmanager
    def cursor(self):
        """A thread-local cursor. DuckDB cursors are cheap and isolate thread state."""
        if self._con is None:
            raise RuntimeError("Store.connect() has not been called")
        cur = self._con.cursor()
        try:
            cur.execute("LOAD spatial;")
            yield cur
        finally:
            cur.close()

    # -- generic helpers ---------------------------------------------------
    def _fetch(self, sql: str, params: Sequence[Any] | None = None) -> list[tuple]:
        with self.cursor() as cur:
            return cur.execute(sql, list(params or [])).fetchall()

    async def fetch(self, sql: str, params: Sequence[Any] | None = None) -> list[tuple]:
        return await asyncio.to_thread(self._fetch, sql, params)

    # -- writes ------------------------------------------------------------
    def _bulk_upsert(self, table: str, columns: list[str], rows: list[dict]) -> int:
        if not rows:
            return 0
        import pandas as pd

        norm: list[dict] = []
        for r in rows:
            item = {c: r.get(c) for c in columns}
            for jcol in ("address", "contacts", "capacity", "attributes"):
                if jcol in item:
                    item[jcol] = _j(item.get(jcol))
            item["retrieved_at"] = item.get("retrieved_at") or _utcnow()
            norm.append(item)
        df = pd.DataFrame(norm, columns=columns)

        select_parts = []
        for c in columns:
            if c == "wkt":
                select_parts.append("ST_GeomFromText(wkt) AS geom")
            elif c in ("address", "contacts", "capacity", "attributes"):
                select_parts.append(f"CAST({c} AS JSON) AS {c}")
            else:
                select_parts.append(c)
        target_cols = [("geom" if c == "wkt" else c) for c in columns]
        if table in ("assets", "networks"):
            # Precompute the geometry envelope once at write time so reads can use a
            # cheap, vectorised range filter instead of deserialising every geometry.
            for fn, col in (("ST_XMin", "bbox_min_lon"), ("ST_YMin", "bbox_min_lat"),
                            ("ST_XMax", "bbox_max_lon"), ("ST_YMax", "bbox_max_lat")):
                select_parts.append(f"{fn}(ST_GeomFromText(wkt)) AS {col}")
                target_cols.append(col)

        with self.cursor() as cur:
            cur.register("_incoming", df)
            cur.execute(
                f"INSERT OR REPLACE INTO {table} ({', '.join(target_cols)}) "
                f"SELECT {', '.join(select_parts)} FROM _incoming "
                f"WHERE wkt IS NOT NULL AND wkt <> ''"
            )
            cur.unregister("_incoming")
        return len(df)

    async def upsert_assets(self, rows: list[dict]) -> int:
        async with self._write_lock:
            n = await asyncio.to_thread(self._bulk_upsert, "assets", ASSET_COLUMNS, rows)
        self.invalidate_density()
        return n

    async def upsert_networks(self, rows: list[dict]) -> int:
        async with self._write_lock:
            return await asyncio.to_thread(self._bulk_upsert, "networks", NETWORK_COLUMNS, rows)

    def _upsert_popgrid(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        import pandas as pd

        df = pd.DataFrame(rows, columns=["cell_id", "wkt", "population", "area_m2",
                                         "source_id", "year"])
        with self.cursor() as cur:
            cur.register("_pg", df)
            cur.execute(
                "INSERT OR REPLACE INTO pop_grid (cell_id, geom, population, area_m2, "
                "source_id, year) SELECT cell_id, ST_GeomFromText(wkt), population, "
                "area_m2, source_id, year FROM _pg WHERE wkt IS NOT NULL"
            )
            cur.unregister("_pg")
        return len(df)

    async def upsert_popgrid(self, rows: list[dict]) -> int:
        async with self._write_lock:
            return await asyncio.to_thread(self._upsert_popgrid, rows)

    async def delete_source(self, source_id: str) -> None:
        async with self._write_lock:
            await asyncio.to_thread(
                self._fetch, "DELETE FROM assets WHERE source_id = ?", [source_id]
            )

    # -- spatial reads -----------------------------------------------------
    # -- query planning ----------------------------------------------------
    # Measured on this hardware (100k rows): evaluating ST_Intersects against the stored
    # GEOMETRY column costs ~0.13 ms/row because each blob must be deserialised, so the
    # R-tree path degrades linearly with matches. Reconstructing a point from the lon/lat
    # DOUBLE columns is ~7x cheaper but scans the table. The crossover is a few hundred
    # matched rows, so we estimate matches from AOI area x row density and pick a path.
    # Both paths return identical rows; a bad guess costs latency, never correctness.
    _RTREE_ROW_BUDGET = 250

    _EXACT_PRED = (
        "CASE WHEN geometry_kind = 'point' "
        "THEN ST_Within(ST_Point(lon, lat), ST_GeomFromText(?)) "
        "ELSE ST_Intersects(geom, ST_GeomFromText(?)) END"
    )

    def _density(self) -> float:
        """Rows per square degree over the populated extent. Cached, cheap to refresh."""
        if self._density_cache is not None:
            return self._density_cache
        try:
            with self.cursor() as cur:
                row = cur.execute(
                    "SELECT count(*), min(lon), max(lon), min(lat), max(lat) FROM assets"
                ).fetchone()
            n, x0, x1, y0, y1 = row
            if not n or x0 is None:
                self._density_cache = 0.0
            else:
                area = max((x1 - x0) * (y1 - y0), 1e-6)
                self._density_cache = n / area
        except Exception:
            self._density_cache = 0.0
        return self._density_cache

    def invalidate_density(self) -> None:
        self._density_cache = None

    def choose_strategy(self, bbox: tuple[float, float, float, float]) -> str:
        x0, y0, x1, y1 = bbox
        estimated = max((x1 - x0) * (y1 - y0), 0.0) * self._density()
        return "rtree" if estimated <= self._RTREE_ROW_BUDGET else "scan"

    def _query_assets(self, wkt: str, bbox: tuple[float, float, float, float],
                      categories: Sequence[str] | None, limit: int,
                      sources: Sequence[str] | None = None,
                      strategy: str | None = None) -> tuple[list[dict], str]:
        strategy = strategy or self.choose_strategy(bbox)
        x0, y0, x1, y1 = bbox
        params: list[Any] = []
        if strategy == "rtree":
            where = ["ST_Intersects(geom, ST_GeomFromText(?))"]
            params.append(wkt)
        else:
            where = [
                "bbox_max_lon >= ? AND bbox_min_lon <= ? "
                "AND bbox_max_lat >= ? AND bbox_min_lat <= ?",
                self._EXACT_PRED,
            ]
            params.extend([x0, x1, y0, y1, wkt, wkt])
        if categories:
            where.append(f"category IN ({','.join('?' * len(categories))})")
            params.extend(categories)
        if sources:
            where.append(f"source_id IN ({','.join('?' * len(sources))})")
            params.extend(sources)
        sql = f"""
            SELECT id, source_id, source_ref, category, subcategory, name,
                   ST_AsGeoJSON(geom) AS geojson, lon, lat, geometry_kind,
                   address, contacts, capacity, attributes,
                   footprint_m2, floors, confidence, retrieved_at
            FROM assets WHERE {' AND '.join(where)} LIMIT {int(limit)}
        """
        with self.cursor() as cur:
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()], strategy

    async def query_assets(self, wkt: str, bbox: tuple[float, float, float, float],
                           categories: Sequence[str] | None = None,
                           limit: int = 20_000,
                           sources: Sequence[str] | None = None,
                           strategy: str | None = None) -> tuple[list[dict], str]:
        return await asyncio.to_thread(
            self._query_assets, wkt, bbox, categories, limit, sources, strategy)

    def _query_networks(self, wkt: str, bbox: tuple[float, float, float, float],
                        subcategories: Sequence[str] | None,
                        limit: int) -> list[dict]:
        x0, y0, x1, y1 = bbox
        where = ["bbox_max_lon >= ? AND bbox_min_lon <= ? "
                 "AND bbox_max_lat >= ? AND bbox_min_lat <= ?",
                 "ST_Intersects(geom, ST_GeomFromText(?))"]
        params: list[Any] = [x0, x1, y0, y1, wkt]
        if subcategories:
            where.append(f"subcategory IN ({','.join('?' * len(subcategories))})")
            params.extend(subcategories)
        sql = f"""
            SELECT id, source_id, category, subcategory, name,
                   ST_AsGeoJSON(ST_Intersection(geom, ST_GeomFromText(?))) AS geojson,
                   ST_Length_Spheroid(ST_Intersection(geom, ST_GeomFromText(?))) AS clipped_m,
                   attributes
            FROM networks WHERE {' AND '.join(where)} LIMIT {int(limit)}
        """
        with self.cursor() as cur:
            cur.execute(sql, [wkt, wkt] + params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    async def query_networks(self, wkt: str, bbox: tuple[float, float, float, float],
                             subcategories: Sequence[str] | None = None,
                             limit: int = 20_000) -> list[dict]:
        return await asyncio.to_thread(self._query_networks, wkt, bbox, subcategories, limit)

    def _query_population(self, wkt: str) -> list[dict]:
        """Area-weighted census population: each cell contributes its overlap fraction."""
        sql = """
            SELECT cell_id, population,
                   ST_Area_Spheroid(ST_Intersection(geom, ST_GeomFromText(?)))
                     / NULLIF(ST_Area_Spheroid(geom), 0) AS frac,
                   ST_AsGeoJSON(geom) AS geojson
            FROM pop_grid
            WHERE ST_Intersects(geom, ST_GeomFromText(?))
        """
        with self.cursor() as cur:
            cur.execute(sql, [wkt, wkt])
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    async def query_population(self, wkt: str) -> list[dict]:
        return await asyncio.to_thread(self._query_population, wkt)

    # -- tile cache --------------------------------------------------------
    def _stale_tiles(self, tile_keys: list[str], ttl_hours: int) -> set[str]:
        if not tile_keys:
            return set()
        placeholders = ",".join("?" * len(tile_keys))
        sql = f"""
            SELECT tile_key FROM osm_tile_cache
            WHERE tile_key IN ({placeholders}) AND status = 'ok'
              AND fetched_at > (current_timestamp - INTERVAL {int(ttl_hours)} HOUR)
        """
        with self.cursor() as cur:
            fresh = {r[0] for r in cur.execute(sql, tile_keys).fetchall()}
        return set(tile_keys) - fresh

    async def stale_tiles(self, tile_keys: list[str], ttl_hours: int | None = None) -> set[str]:
        ttl = settings.osm_tile_ttl_hours if ttl_hours is None else ttl_hours
        return await asyncio.to_thread(self._stale_tiles, tile_keys, ttl)

    def _mark_tiles(self, tiles: list[dict]) -> None:
        import pandas as pd

        df = pd.DataFrame(tiles)
        with self.cursor() as cur:
            cur.register("_tiles", df)
            cur.execute(
                "INSERT OR REPLACE INTO osm_tile_cache "
                "(tile_key, min_lon, min_lat, max_lon, max_lat, fetched_at, status, "
                " feature_count, network_count, error, last_hit_at) "
                "SELECT tile_key, min_lon, min_lat, max_lon, max_lat, fetched_at, status, "
                "feature_count, network_count, error, fetched_at FROM _tiles"
            )
            cur.unregister("_tiles")

    async def mark_tiles(self, tiles: list[dict]) -> None:
        if not tiles:
            return
        async with self._write_lock:
            await asyncio.to_thread(self._mark_tiles, tiles)

    async def clear_tile_assets(self, tile_keys: list[str]) -> None:
        """Drop previously cached OSM rows for tiles about to be refreshed."""
        if not tile_keys:
            return
        placeholders = ",".join("?" * len(tile_keys))
        async with self._write_lock:
            await asyncio.to_thread(
                self._fetch,
                f"DELETE FROM assets WHERE tile_key IN ({placeholders})", tile_keys)
            await asyncio.to_thread(
                self._fetch,
                f"DELETE FROM networks WHERE tile_key IN ({placeholders})", tile_keys)

    # -- enrichment cache --------------------------------------------------
    async def cache_get(self, key: str) -> dict | None:
        rows = await self.fetch("SELECT payload FROM enrichment_cache WHERE key = ?", [key])
        if not rows:
            return None
        try:
            return json.loads(rows[0][0])
        except (TypeError, ValueError):
            return None

    async def cache_put(self, key: str, kind: str, payload: dict) -> None:
        async with self._write_lock:
            await asyncio.to_thread(
                self._fetch,
                "INSERT OR REPLACE INTO enrichment_cache (key, kind, payload, created_at) "
                "VALUES (?, ?, ?, current_timestamp)",
                [key, kind, _j(payload)],
            )

    # -- ingest audit ------------------------------------------------------
    async def record_run(self, source_id: str, status: str, rows: int,
                         started_at: datetime, error: str | None = None) -> None:
        async with self._write_lock:
            await asyncio.to_thread(
                self._fetch,
                "INSERT OR REPLACE INTO source_runs "
                "(run_id, source_id, started_at, finished_at, status, rows, error) "
                "VALUES (?, ?, ?, current_timestamp, ?, ?, ?)",
                [str(uuid.uuid4()), source_id, started_at, status, rows, error],
            )

    async def source_stats(self) -> dict[str, dict]:
        rows = await self.fetch(
            "SELECT source_id, count(*) FROM assets GROUP BY source_id")
        counts = {r[0]: r[1] for r in rows}
        net = await self.fetch(
            "SELECT source_id, count(*) FROM networks GROUP BY source_id")
        for sid, n in net:
            counts[sid] = counts.get(sid, 0) + n
        runs = await self.fetch(
            "SELECT source_id, max(finished_at), any_value(status), any_value(error) "
            "FROM source_runs GROUP BY source_id")
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
        def _one(sql: str) -> Any:
            rows = self._fetch(sql)
            return rows[0][0] if rows else 0

        return await asyncio.to_thread(lambda: {
            "assets": _one("SELECT count(*) FROM assets"),
            "networks": _one("SELECT count(*) FROM networks"),
            "population_cells": _one("SELECT count(*) FROM pop_grid"),
            "cached_tiles": _one("SELECT count(*) FROM osm_tile_cache WHERE status='ok'"),
            "enrichment_entries": _one("SELECT count(*) FROM enrichment_cache"),
            # Count population-grid sources too: they live in their own table, and a
            # landing page that says "6 sources" while /v1/sources lists 8 is a bug.
            "sources": _one(
                "SELECT count(*) FROM ("
                "  SELECT DISTINCT source_id FROM assets"
                "  UNION SELECT DISTINCT source_id FROM networks"
                "  UNION SELECT DISTINCT source_id FROM pop_grid)"),
            "db_size_mb": round(self.db_path.stat().st_size / 1e6, 2)
            if self.db_path.exists() else 0.0,
        })

    # -- snapshots ---------------------------------------------------------
    async def export_parquet(self, out_dir: Path) -> dict[str, int]:
        out_dir.mkdir(parents=True, exist_ok=True)
        result = {}
        for table in ("assets", "networks", "pop_grid"):
            path = out_dir / f"{table}.parquet"
            await asyncio.to_thread(
                self._fetch,
                f"COPY (SELECT * REPLACE (ST_AsWKB(geom) AS geom) FROM {table}) "
                f"TO '{path}' (FORMAT PARQUET)")
            rows = await self.fetch(f"SELECT count(*) FROM {table}")
            result[table] = rows[0][0]
        return result


_store: Store | None = None


def get_store() -> Store:
    global _store
    if _store is None:
        _store = Store()
        _store.connect()
    return _store


def set_store(store: Store) -> None:
    global _store
    _store = store
