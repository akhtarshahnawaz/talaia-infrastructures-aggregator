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
    # Ceiling on DuckDB's spill directory, so a big write cannot fill the volume.
    duckdb_temp_limit: str = "2GB"

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
    # How long a tile that failed upstream is left alone before a user request retries
    # it. Without this, one permanently failing tile makes every request touching it pay
    # the full deadline, for ever - a partially warmed region would be slower than a cold
    # one. Bulk warming ignores the backoff, because retrying is the whole point there.
    osm_error_retry_minutes: int = 30
    # Regions warmed in the background at boot, comma separated. Names come from the
    # gazetteer in talaia.regions, or "minlon,minlat,maxlon,maxlat" for a raw bbox.
    # Empty means no pre-warming: tiles are fetched on first demand as usual.
    warm_on_boot: str = ""
    # Pacing for bulk warming. Deliberately gentler than the per-request path: a bulk
    # warm is not latency-sensitive and Overpass is a donated public resource.
    warm_max_parallel: int = 2
    warm_pause_s: float = 1.0
    warm_group_tiles: int = 16

    # --- upstream services ---------------------------------------------
    socrata_base: str = "https://analisi.transparenciacatalunya.cat/resource"
    socrata_app_token: str | None = None
    cartociudad_base: str = "https://www.cartociudad.es/geocoder/api/geocoder"
    # CartoCiudad resolves one address per request - there is no batch endpoint - so a
    # national registry costs one call per distinct address and these two numbers set
    # how long that takes. 20/s is deliberately polite towards a free public service
    # funded by the Spanish mapping agency: at that rate the 51,216 school addresses
    # take ~43 minutes, and almost all of a cold boot is this. Raise it if you are in a
    # hurry and accept the manners; results are cached permanently, so you pay once.
    geocode_min_interval_s: float = 0.05
    # How addresses become points.
    #   offline - bulk place-name table only. No network, whole registry in seconds,
    #             municipality-centroid accuracy. The default: it is the only option
    #             that does not put 55,000 requests through somebody else's free server.
    #   hybrid  - offline first, then the street-level geocoder for what is left.
    #             What a production deployment making evacuation decisions should use.
    #   street  - street-level geocoder first, offline only as a fallback.
    geocode_mode: str = "offline"
    geonames_url: str = "https://download.geonames.org/export/dump/ES.zip"
    geocode_concurrency: int = 8
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

    # --- authentication ----------------------------------------------------
    # Secure by default: the API is gated unless this is explicitly turned off.
    require_auth: bool = True
    # Comma-separated. Either "key" or "label:key".
    api_keys: str = ""
    # Enables the runtime key-management endpoints when set.
    admin_key: str | None = None
    rate_limit_per_min: int = 120
    # Per-tier limit overrides as JSON, so quotas can be retuned from the Railway
    # dashboard without a code change or a rebuild. Partial objects are merged onto the
    # built-in tier; an unknown name defines a new tier on top of the free-tier defaults.
    #   {"free": {"max_aoi_km2": 500, "daily_quota": 5000},
    #    "partner": {"max_aoi_km2": 10000, "rate_limit_per_min": 600}}
    tier_limits: str = ""
    # /v1/sources, /v1/taxonomy and /v1/stats describe the service rather than returning
    # exposure data, and the public documentation site renders them. They stay open by
    # default; set this to false to gate absolutely everything.
    public_metadata: bool = True
    # Self-service signup. Issues a free-tier key; disable to make the service invite-only.
    allow_signup: bool = True
    signups_per_ip_per_day: int = 3
    signup_tier: str = "free"
    # Email verification. On by default: without it the address on a key is an unchecked
    # string, so there is no way to reach the holder and no cost to registering under
    # someone else's name. Turning it off is a deliberate choice for a closed deployment.
    require_email_verification: bool = True
    verification_ttl_hours: int = 24
    # Absolute base URL used to build the verification link. Behind Railway's proxy the
    # request's own host header is not reliably the public one.
    public_url: str = ""

    # --- outbound email ----------------------------------------------------
    # Backends are chosen by what is set, most explicit first: a Resend API key, then
    # SMTP, then the development console. With nothing configured, signup returns 503
    # rather than silently issuing unverified keys.
    #
    # Resend needs one value and no server to run: get a key at resend.com/api-keys.
    resend_api_key: str = ""
    resend_api_url: str = "https://api.resend.com/emails"
    # Resend's shared sender. It works with no DNS setup at all, but only delivers to
    # the address that owns the Resend account - verify a domain to mail anyone else.
    resend_default_from: str = "TALAIA <onboarding@resend.dev>"

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True
    smtp_ssl: bool = False
    smtp_timeout_s: float = 20.0
    email_from: str = ""
    # Development only: log the message instead of sending it.
    email_console: bool = False

    # How long a write may wait for the single writer before giving up. Long enough
    # that a big bulk upsert ahead of it finishes, short enough that a caller gets an
    # answer rather than a hung request.
    write_lock_timeout_s: float = 25.0

    # Automatic recovery from a wedged write. A write that has made no progress for
    # `write_stuck_after_s` gets its query interrupted, which frees the single writer
    # and lets the service carry on. If interrupting does not work either - the thread
    # is blocked somewhere Python cannot reach - the process exits after
    # `write_fatal_after_s` so the platform restarts it, because a service that cannot
    # write is not serving and a restart is what a human would do anyway.
    write_watchdog_interval_s: float = 10.0
    write_stuck_after_s: float = 90.0
    # Interrupting turned out not to free the deadlock it was written for - DuckDB was
    # not executing anything, so there was nothing to cancel - so do not sit there for
    # seven minutes hoping. Try it, then restart.
    write_fatal_after_s: float = 240.0
    write_watchdog_may_exit: bool = True

    # --- server ----------------------------------------------------------
    cors_origins: str = "*"
    web_dist: Path = REPO_ROOT / "web" / "dist"

    @property
    def db_path(self) -> Path:
        return self.data_dir / self.db_filename

    @property
    def warm_on_boot_list(self) -> list[str]:
        return [r.strip() for r in self.warm_on_boot.split(",") if r.strip()]

    @property
    def overpass_mirror_list(self) -> list[str]:
        return [m.strip() for m in self.overpass_mirrors.split(",") if m.strip()]

    @property
    def api_key_list(self) -> list[str]:
        return [k.strip() for k in self.api_keys.split(",") if k.strip()]

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    return s


settings = get_settings()
