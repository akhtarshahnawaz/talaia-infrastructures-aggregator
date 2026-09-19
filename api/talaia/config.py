"""Environment-driven configuration for TALAIA."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TALAIA_", env_file=".env", extra="ignore")

    # --- storage -------------------------------------------------------
    data_dir: Path = REPO_ROOT / "data"
    db_filename: str = "talaia.duckdb"
    duckdb_memory_limit: str = "1GB"
    duckdb_threads: int = 4

    # --- OpenStreetMap / Overpass --------------------------------------
    # Ordered by preference; the client rotates on failure and tracks per-mirror health.
    overpass_mirrors: str = (
        "https://overpass-api.de/api/interpreter,"
        "https://overpass.kumi.systems/api/interpreter,"
        "https://overpass.private.coffee/api/interpreter,"
        "https://maps.mail.ru/osm/tools/overpass/api/interpreter"
    )
    overpass_timeout_s: float = 60.0
    overpass_max_parallel: int = 3
    osm_tile_deg: float = 0.05          # ~4 km tiles
    osm_tile_ttl_hours: int = 24 * 14   # OSM changes slowly for infrastructure
    osm_max_tiles_per_request: int = 96 # guard against absurd AOIs
    # Hard wall-clock budget for the whole Tier B warm-up. Whatever has landed when the
    # budget expires is used; the rest is abandoned and reported as a warning. An
    # incident commander cannot wait three minutes for a volunteer tile server.
    osm_deadline_s: float = 25.0

    # --- upstream services ---------------------------------------------
    socrata_base: str = "https://analisi.transparenciacatalunya.cat/resource"
    socrata_app_token: str | None = None
    cartociudad_base: str = "https://www.cartociudad.es/geocoder/api/geocoder"
    catastro_base: str = "https://ovc.catastro.meh.es"
    http_timeout_s: float = 30.0
    http_retries: int = 3
    user_agent: str = "TALAIA/0.1 (HackBarna2026; values-at-risk infrastructure API)"

    # --- query guards ---------------------------------------------------
    max_aoi_km2: float = 25_000.0
    max_assets_returned: int = 20_000
    default_buffer_m: float = 0.0

    # --- feature flags ---------------------------------------------------
    enable_live_osm: bool = True
    enable_enrichment: bool = True
    auto_bootstrap: bool = True
    core_impl: str = "auto"  # auto | rust | python

    # --- server ----------------------------------------------------------
    cors_origins: str = "*"
    web_dist: Path = REPO_ROOT / "web" / "dist"

    @property
    def db_path(self) -> Path:
        return self.data_dir / self.db_filename

    @property
    def overpass_mirror_list(self) -> list[str]:
        return [m.strip() for m in self.overpass_mirrors.split(",") if m.strip()]

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    return s


settings = get_settings()
