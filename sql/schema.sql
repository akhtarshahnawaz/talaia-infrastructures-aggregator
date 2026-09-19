-- TALAIA storage schema (DuckDB + spatial)
-- All geometry is WGS84 (EPSG:4326) lon/lat.

INSTALL spatial;
LOAD spatial;
INSTALL json;
LOAD json;

-- ---------------------------------------------------------------------------
-- assets: the canonical, conflation-ready inventory (Tier A + materialised Tier B)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS assets (
    id            VARCHAR PRIMARY KEY,
    source_id     VARCHAR NOT NULL,
    source_ref    VARCHAR,
    category      VARCHAR NOT NULL,
    subcategory   VARCHAR NOT NULL,
    name          VARCHAR,
    geom          GEOMETRY NOT NULL,
    lon           DOUBLE,
    lat           DOUBLE,
    geometry_kind VARCHAR DEFAULT 'point',
    address       JSON,
    contacts      JSON,
    capacity      JSON,
    attributes    JSON,
    bbox_min_lon  DOUBLE,
    bbox_min_lat  DOUBLE,
    bbox_max_lon  DOUBLE,
    bbox_max_lat  DOUBLE,
    footprint_m2  DOUBLE,
    floors        DOUBLE,
    confidence    DOUBLE DEFAULT 0.5,
    tile_key      VARCHAR,
    retrieved_at  TIMESTAMP,
    updated_at    TIMESTAMP DEFAULT current_timestamp
);

-- ---------------------------------------------------------------------------
-- networks: linear infrastructure. Kept apart from assets because counting
-- "number of roads" is meaningless - length cut and access severance are not.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS networks (
    id           VARCHAR PRIMARY KEY,
    source_id    VARCHAR NOT NULL,
    source_ref   VARCHAR,
    category     VARCHAR NOT NULL,
    subcategory  VARCHAR NOT NULL,
    name         VARCHAR,
    geom         GEOMETRY NOT NULL,
    bbox_min_lon DOUBLE,
    bbox_min_lat DOUBLE,
    bbox_max_lon DOUBLE,
    bbox_max_lat DOUBLE,
    attributes   JSON,
    tile_key     VARCHAR,
    retrieved_at TIMESTAMP,
    updated_at   TIMESTAMP DEFAULT current_timestamp
);

-- ---------------------------------------------------------------------------
-- pop_grid: INE 1 km2 census population cells, area-weighted at query time
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pop_grid (
    cell_id    VARCHAR PRIMARY KEY,
    geom       GEOMETRY NOT NULL,
    population DOUBLE NOT NULL,
    area_m2    DOUBLE,
    source_id  VARCHAR,
    year       INTEGER
);

-- ---------------------------------------------------------------------------
-- osm_tile_cache: Tier B bookkeeping. A tile is the unit of cache validity.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS osm_tile_cache (
    tile_key      VARCHAR PRIMARY KEY,
    min_lon       DOUBLE, min_lat DOUBLE, max_lon DOUBLE, max_lat DOUBLE,
    fetched_at    TIMESTAMP,
    status        VARCHAR DEFAULT 'ok',
    feature_count INTEGER DEFAULT 0,
    network_count INTEGER DEFAULT 0,
    error         VARCHAR,
    last_hit_at   TIMESTAMP
);

-- ---------------------------------------------------------------------------
-- enrichment_cache: Tier C. Keyed by content hash; effectively permanent.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS enrichment_cache (
    key        VARCHAR PRIMARY KEY,
    kind       VARCHAR NOT NULL,
    payload    JSON,
    created_at TIMESTAMP DEFAULT current_timestamp
);

-- ---------------------------------------------------------------------------
-- source_runs: ingest audit trail, surfaced by /v1/sources
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS source_runs (
    run_id      VARCHAR PRIMARY KEY,
    source_id   VARCHAR NOT NULL,
    started_at  TIMESTAMP,
    finished_at TIMESTAMP,
    status      VARCHAR,
    rows        INTEGER DEFAULT 0,
    error       VARCHAR
);

-- ---------------------------------------------------------------------------
-- api_keys: only a SHA-256 hash is stored. The prefix is a non-secret handle used
-- for listing and revocation, so a key can be identified without being exposed.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS api_keys (
    key_hash           VARCHAR PRIMARY KEY,
    prefix             VARCHAR,
    label              VARCHAR,
    tier               VARCHAR DEFAULT 'free',
    rate_limit_per_min INTEGER DEFAULT 120,
    daily_quota        INTEGER DEFAULT 0,
    max_aoi_km2        DOUBLE  DEFAULT 0,
    max_assets         INTEGER DEFAULT 20000,
    email              VARCHAR,
    organisation       VARCHAR,
    created_ip         VARCHAR,
    created_at         TIMESTAMP,
    revoked_at         TIMESTAMP,
    last_used_at       TIMESTAMP,
    request_count      BIGINT DEFAULT 0
);

-- Daily usage, flushed from memory periodically. Writing on every request would
-- serialise the whole service behind DuckDB's single writer.
CREATE TABLE IF NOT EXISTS key_usage (
    key_hash VARCHAR,
    day      DATE,
    requests BIGINT DEFAULT 0,
    PRIMARY KEY (key_hash, day)
);

-- Signup throttling by client address, so one actor cannot mint keys in bulk.
CREATE TABLE IF NOT EXISTS signups (
    id           VARCHAR PRIMARY KEY,
    email        VARCHAR,
    organisation VARCHAR,
    ip           VARCHAR,
    created_at   TIMESTAMP,
    key_prefix   VARCHAR
);

CREATE INDEX IF NOT EXISTS assets_geom_idx   ON assets   USING RTREE (geom);
CREATE INDEX IF NOT EXISTS networks_geom_idx ON networks USING RTREE (geom);
CREATE INDEX IF NOT EXISTS popgrid_geom_idx  ON pop_grid USING RTREE (geom);
