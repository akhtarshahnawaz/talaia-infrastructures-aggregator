-- TALAIA storage schema (PostgreSQL + PostGIS)
-- All geometry is WGS84 (EPSG:4326) lon/lat, the same contract as the DuckDB schema.
--
-- Why this file exists next to schema.sql rather than replacing it: DuckDB stays the
-- store for local development and the test suite, where a second service is a cost with
-- no benefit. Postgres is what a deployment runs, because it takes concurrent writers
-- and DuckDB takes exactly one.
--
-- Timestamps are `timestamp` (no zone) holding UTC, matching _utcnow() on the Python
-- side. Defaults use `timezone('utc', now())` rather than current_timestamp, which
-- would write the server's local time into a column everything else reads as UTC.

CREATE EXTENSION IF NOT EXISTS postgis;

-- ---------------------------------------------------------------------------
-- assets: the canonical, conflation-ready inventory (Tier A + materialised Tier B)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS assets (
    id            TEXT PRIMARY KEY,
    source_id     TEXT NOT NULL,
    source_ref    TEXT,
    category      TEXT NOT NULL,
    subcategory   TEXT NOT NULL,
    name          TEXT,
    geom          geometry(Geometry, 4326) NOT NULL,
    lon           DOUBLE PRECISION,
    lat           DOUBLE PRECISION,
    geometry_kind TEXT DEFAULT 'point',
    address       JSONB,
    contacts      JSONB,
    capacity      JSONB,
    attributes    JSONB,
    bbox_min_lon  DOUBLE PRECISION,
    bbox_min_lat  DOUBLE PRECISION,
    bbox_max_lon  DOUBLE PRECISION,
    bbox_max_lat  DOUBLE PRECISION,
    footprint_m2  DOUBLE PRECISION,
    floors        DOUBLE PRECISION,
    confidence    DOUBLE PRECISION DEFAULT 0.5,
    tile_key      TEXT,
    retrieved_at  TIMESTAMP,
    updated_at    TIMESTAMP DEFAULT timezone('utc', now())
);

-- ---------------------------------------------------------------------------
-- networks: linear infrastructure. Kept apart from assets because counting
-- "number of roads" is meaningless - length cut and access severance are not.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS networks (
    id           TEXT PRIMARY KEY,
    source_id    TEXT NOT NULL,
    source_ref   TEXT,
    category     TEXT NOT NULL,
    subcategory  TEXT NOT NULL,
    name         TEXT,
    geom         geometry(Geometry, 4326) NOT NULL,
    bbox_min_lon DOUBLE PRECISION,
    bbox_min_lat DOUBLE PRECISION,
    bbox_max_lon DOUBLE PRECISION,
    bbox_max_lat DOUBLE PRECISION,
    attributes   JSONB,
    tile_key     TEXT,
    retrieved_at TIMESTAMP,
    updated_at   TIMESTAMP DEFAULT timezone('utc', now())
);

-- ---------------------------------------------------------------------------
-- pop_grid: INE 1 km2 census population cells, area-weighted at query time
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pop_grid (
    cell_id    TEXT PRIMARY KEY,
    geom       geometry(Geometry, 4326) NOT NULL,
    population DOUBLE PRECISION NOT NULL,
    area_m2    DOUBLE PRECISION,
    source_id  TEXT,
    year       INTEGER
);

-- ---------------------------------------------------------------------------
-- osm_tile_cache: Tier B bookkeeping. A tile is the unit of cache validity.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS osm_tile_cache (
    tile_key      TEXT PRIMARY KEY,
    min_lon       DOUBLE PRECISION, min_lat DOUBLE PRECISION,
    max_lon       DOUBLE PRECISION, max_lat DOUBLE PRECISION,
    fetched_at    TIMESTAMP,
    status        TEXT DEFAULT 'ok',
    feature_count INTEGER DEFAULT 0,
    network_count INTEGER DEFAULT 0,
    error         TEXT,
    last_hit_at   TIMESTAMP
);

-- ---------------------------------------------------------------------------
-- enrichment_cache: Tier C. Keyed by content hash; effectively permanent.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS enrichment_cache (
    key        TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    payload    JSONB,
    created_at TIMESTAMP DEFAULT timezone('utc', now())
);

-- ---------------------------------------------------------------------------
-- source_runs: ingest audit trail, surfaced by /v1/sources
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS source_runs (
    run_id      TEXT PRIMARY KEY,
    source_id   TEXT NOT NULL,
    started_at  TIMESTAMP,
    finished_at TIMESTAMP,
    status      TEXT,
    rows        INTEGER DEFAULT 0,
    error       TEXT
);

-- ---------------------------------------------------------------------------
-- api_keys: only a SHA-256 hash is stored. The prefix is a non-secret handle used
-- for listing and revocation, so a key can be identified without being exposed.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS api_keys (
    key_hash           TEXT PRIMARY KEY,
    prefix             TEXT,
    label              TEXT,
    tier               TEXT DEFAULT 'free',
    rate_limit_per_min INTEGER DEFAULT 120,
    daily_quota        INTEGER DEFAULT 0,
    max_aoi_km2        DOUBLE PRECISION DEFAULT 0,
    max_assets         INTEGER DEFAULT 20000,
    -- FALSE means "follow whatever this tier currently allows", so retuning a
    -- tier moves every key on it. TRUE pins the per-key values an admin set by
    -- hand, which a tier change must not silently overwrite.
    custom_limits      BOOLEAN DEFAULT FALSE,
    email              TEXT,
    organisation       TEXT,
    created_ip         TEXT,
    created_at         TIMESTAMP,
    revoked_at         TIMESTAMP,
    last_used_at       TIMESTAMP,
    request_count      BIGINT DEFAULT 0,
    -- Whether the address was confirmed by following a mailed link. Keys issued before
    -- verification existed, and those minted by an admin or from the environment, are
    -- not retroactively marked as verified.
    email_verified     BOOLEAN DEFAULT FALSE
);

-- Daily usage, flushed from memory periodically rather than written per request.
-- On Postgres that batching is a courtesy rather than a necessity, but a hot row per
-- key per day is still worth not contending on.
CREATE TABLE IF NOT EXISTS key_usage (
    key_hash TEXT,
    day      DATE,
    requests BIGINT DEFAULT 0,
    PRIMARY KEY (key_hash, day)
);

-- Signups awaiting email confirmation. Only a hash of the token is stored, for the
-- same reason as API keys: a leaked database must not hand anyone a working link.
-- Rows are kept after use so a consumed token cannot be replayed.
CREATE TABLE IF NOT EXISTS pending_signups (
    token_hash   TEXT PRIMARY KEY,
    email        TEXT NOT NULL,
    organisation TEXT,
    use_case     TEXT,
    ip           TEXT,
    created_at   TIMESTAMP,
    expires_at   TIMESTAMP,
    consumed_at  TIMESTAMP
);

-- Signup throttling by client address, so one actor cannot mint keys in bulk.
CREATE TABLE IF NOT EXISTS signups (
    id           TEXT PRIMARY KEY,
    email        TEXT,
    organisation TEXT,
    ip           TEXT,
    created_at   TIMESTAMP,
    key_prefix   TEXT
);

-- GiST, PostGIS's spatial index. Unlike the DuckDB R-tree this is usable by the
-- planner for every spatial predicate, which is why the Postgres read path has one
-- plan where the DuckDB one has to choose between an index and a bbox scan.
CREATE INDEX IF NOT EXISTS assets_geom_idx   ON assets   USING GIST (geom);
CREATE INDEX IF NOT EXISTS networks_geom_idx ON networks USING GIST (geom);
CREATE INDEX IF NOT EXISTS popgrid_geom_idx  ON pop_grid USING GIST (geom);

-- Filters that ride along with the spatial predicate on every exposure query.
CREATE INDEX IF NOT EXISTS assets_source_idx   ON assets (source_id);
CREATE INDEX IF NOT EXISTS assets_category_idx ON assets (category);
CREATE INDEX IF NOT EXISTS assets_tile_idx     ON assets (tile_key);
CREATE INDEX IF NOT EXISTS networks_tile_idx   ON networks (tile_key);
-- source_stats reads the newest run per source on every /v1/sources request.
CREATE INDEX IF NOT EXISTS source_runs_recent_idx ON source_runs (source_id, finished_at DESC);
