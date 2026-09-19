# TALAIA — Execution Plan

Derived from `ARCHITECTURE.md`. Organised into **independent modules** with an explicit
dependency graph, so work can proceed in parallel wherever the graph allows.

## Dependency graph

```
         ┌──────────────────────────┐
         │ M0  Scaffold + Taxonomy  │  (blocking, small)
         └────────────┬─────────────┘
       ┌──────────────┼──────────────┬───────────────┐
       ▼              ▼              ▼               ▼
  ┌─────────┐   ┌───────────┐  ┌──────────┐   ┌────────────┐
  │ M1 Store│   │ M2 Conn.  │  │ M8 Rust  │   │ M9 Valuation│
  │ DuckDB  │   │ framework │  │ core+fb  │   │ + scoring   │
  └────┬────┘   └─────┬─────┘  └────┬─────┘   └──────┬──────┘
       │        ┌─────┼─────┬───────┴──┐             │
       │        ▼     ▼     ▼          ▼             │
       │     ┌────┐┌────┐┌────┐    ┌──────┐          │
       │     │M3  ││M4  ││M5  │    │M6    │          │
       │     │ CAT││ ES ││ OSM│    │Enrich│          │
       │     └──┬─┘└──┬─┘└──┬─┘    └──┬───┘          │
       │        └─────┴─────┴─────────┘              │
       │                 │        ┌──────────┐       │
       │                 │        │M7 Conflate│      │
       │                 │        └─────┬─────┘      │
       └─────────────────┴──────────────┴────────────┘
                              ▼
                     ┌─────────────────┐
                     │ M10 Aggregator  │
                     └────────┬────────┘
                              ▼
                     ┌─────────────────┐
                     │ M11 API layer   │
                     └────────┬────────┘
                    ┌─────────┴─────────┐
                    ▼                   ▼
            ┌──────────────┐    ┌───────────────┐
            │ M12 Website  │    │ M13 Deploy    │
            └──────┬───────┘    └───────┬───────┘
                   └──────────┬─────────┘
                              ▼
                     ┌─────────────────┐
                     │ M14 Verification│
                     └─────────────────┘
```

**Parallel waves**
- *Wave 1*: M0
- *Wave 2*: M1 ‖ M2 ‖ M8 ‖ M9
- *Wave 3*: M3 ‖ M4 ‖ M5 ‖ M6 ‖ M7
- *Wave 4*: M10 → M11
- *Wave 5*: M12 ‖ M13
- *Wave 6*: M14

---

## M0 — Scaffold, config, taxonomy
**Deps:** none · **Exit:** `python -c "import talaia"` works; taxonomy covers 17 categories.

1. Repo layout: `api/talaia/{connectors,routers,services}`, `core/` (Rust), `web/`, `sql/`, `scripts/`.
2. `pyproject.toml` + pinned deps; `.venv` bootstrap.
3. `config.py` — env-driven settings (data dir, Overpass mirrors, TTLs, feature flags).
4. `taxonomy.py` — 17 categories, subcategories, vulnerability/criticality defaults,
   human-bearing flag, valuation model key. Single source of truth.
5. `models.py` — Pydantic v2: `Asset`, `SourceMeta`, `ExposureRequest`, `ExposureReport`,
   `Valuation`, `Provenance`, `Capacity`, `BandExposure`.

## M1 — Storage layer (DuckDB)
**Deps:** M0 · **Exit:** insert 100k synthetic assets, polygon query < 20 ms, survives restart.

1. `sql/schema.sql` — `assets`, `osm_tile_cache`, `enrichment_cache`, `source_runs`, `pop_grid`.
2. `store.py` — connection lifecycle, spatial extension load, R-tree indexes, serialised
   writer task, batched upsert, polygon/bbox query, stats.
3. Parquet snapshot export/import for cold-start bootstrap.
4. Bench script proving the latency claim.

## M2 — Connector framework
**Deps:** M0 · **Exit:** a dummy connector registers, runs, and lands rows in the store.

1. `connectors/base.py` — `Connector` ABC, `SourceMeta`, `Tier` enum, coverage geometry.
2. `connectors/registry.py` — decorator registration, lookup by id/category/tier/coverage.
3. `http.py` — shared async client: retries, backoff, mirror rotation, rate limiting, ETag caching.
4. `norm.py` — shared normalisers: DMS→decimal, UTM31N→WGS84, phone/email cleanup,
   accent folding, address parsing, Catalan/Spanish text handling.

## M3 — Catalonia connectors (6)
**Deps:** M2 · **Exit:** each ingests and lands normalised assets with contacts.

| Connector | Dataset | Notes discovered during probing |
|---|---|---|
| `es.cat.schools` | `kvmv-ahh4` | Filter latest `curs` (2025/2026). Use `coordenades_geo_x/y` as **lon/lat** — the `geo_1` field is corrupt (decimal point dropped). Carries phone + email + URL. |
| `es.cat.enrolments` | `xvme-26kg` | 470k rows; aggregate server-side by `codi_centre` for latest curs, join to schools as capacity. |
| `es.cat.reses` | `ivft-vegh` | **No coordinates** → Tier C geocoding required. Select residential/day-care typologies for elderly & disability. Has `capacitat`. |
| `es.cat.equipaments` | `8gmd-gz7i` | Pipe-delimited hierarchical `categoria`, sometimes multi-valued → crosswalk table. Source for CAP, hospitals, libraries, sports, town halls. |
| `es.cat.livestock` | `7bpt-5azk` | **Lat/lon are DMS strings** (`42.0º 8.0' 33.0498''`) → parser required. Use `total_ub` livestock units; filter `estat = Activa`. |
| `es.cat.munipoints` | `wpyq-we8x` | Fallback municipal centroids; flagged low-confidence, never a facility location. |

## M4 — Spain national connectors
**Deps:** M2 · **Exit:** national coverage for hospitals, population, care homes, schools.

1. `es.ine.popgrid` — 1 km² census population grid → `pop_grid` table (area-weighted overlay).
2. `es.msan.hospitales` — Catálogo Nacional de Hospitales; 2025 bed counts; **unique name
   matches only** when joining.
3. `es.msan.regcess` — registered healthcare centres; normalisation + geocoding.
4. `es.csic.carehomes` — 2022 care homes; **CC BY-NC-SA 4.0**, flagged non-commercial.
5. `es.meq.schools` — national non-university school registry (fills outside Catalonia).

Each declares coverage so the API can report which regime an AOI is in.

## M5 — OSM connector with tile cache
**Deps:** M2 · **Exit:** cold AOI fetches; second identical query served from cache, ≥50× faster.

1. Tag→taxonomy crosswalk covering all 17 categories (amenity/building/landuse/man_made/
   power/highway/tourism/historic/emergency/shop/office).
2. Tile cover (0.05°), stale detection, bbox grouping, parallel fetch with mirror rotation.
3. Overpass QL builder; node/way/relation handling with `out center`; polygon geometry retained
   where available (needed for footprint-based valuation).
4. Linear features → `networks` path, not `assets`.
5. Graceful degradation with `warnings[]`.

## M6 — Enrichment (Tier C)
**Deps:** M2 · **Exit:** RESES records get coordinates + quality; cadastral lookups cached.

1. `cartociudad.py` — geocoder (JSONP unwrap), retains `refCatastral` + quality state.
2. `catastro.py` — INSPIRE WFS building geometry + RCCOOR reverse lookup → footprint/floors.
3. `enrichment_cache` keyed by content hash; permanent.

## M7 — Conflation
**Deps:** M2 · **Exit:** known duplicate triple collapses to one asset with merged provenance.

Blocking → scoring (name + distance + hard ids) → merge with source priority → field-level
provenance → `possible_duplicate_of` for near-misses.

## M8 — Rust core + Python fallback
**Deps:** M0 · **Exit:** parity test passes; wheel builds; fallback works without Rust.

1. `core/` crate: pyo3 + geo + rstar + rayon; the four functions from §7.
2. `core_shim.py`: try Rust, else NumPy/Shapely implementation of the identical contract.
3. Parity + benchmark tests.

## M9 — Valuation & scoring
**Deps:** M0 · **Exit:** every asset gets a valuation with method/confidence/assumptions.

1. Unit-cost table by subcategory + province multiplier; contents ratios; livestock values.
2. Footprint resolution chain (cadastre → OSM polygon → default) with confidence decay.
3. Vulnerability/criticality scoring; composite priority score combining
   people × vulnerability × value × time-to-impact.

## M10 — Aggregator
**Deps:** M1, M3–M9 · **Exit:** polygon in → complete `ExposureReport` out.

Implements the §9 lifecycle: AOI normalisation, layer resolution, concurrent tier warm-up,
conflation, enrichment, valuation, Rust rollup, report assembly, warning collection.

## M11 — API layer
**Deps:** M10 · **Exit:** all endpoints respond; OpenAPI clean; errors structured.

`POST /v1/exposure` · `POST /v1/exposure/summary` · `POST /v1/assets` (+ NDJSON stream) ·
`GET /v1/sources` · `GET /v1/taxonomy` · `POST /v1/geocode` · `GET /v1/stats` · `GET /health`.
Plus CORS, request ids, timing headers, structured error envelope.

## M12 — Website
**Deps:** M11 · **Exit:** builds, served by FastAPI, playground calls the live API.

Vite + React + TS + Tailwind + MapLibre. Pages: Landing · **Playground** (draw polygon →
live report) · Docs (quickstart, endpoints, schema, DeepFire integration recipe) ·
**Data Sources** (rendered from `/v1/sources`) · Methodology (conflation, valuation, caveats).

## M13 — Deployment
**Deps:** M11 · **Exit:** image builds; boots with empty volume; self-bootstraps.

Multi-stage Dockerfile (Rust → Node → Python), `railway.json`, volume at `/data`,
health check, background bootstrap on empty DB, `.env.example`.

## M14 — Verification
**Deps:** all · **Exit:** the checklist below is green, with evidence.

1. Unit tests: normalisers (DMS, UTM), taxonomy, valuation, conflation, core parity.
2. Integration: real ingest of Catalan registries; real AOI query over Bages.
3. Performance: warm-AOI latency measured; cache speed-up measured.
4. End-to-end: DeepFire-shaped time-banded FeatureCollection → banded report.
5. Website build + served + playground round-trip.
6. Final report of what works, what is partial, and what is stubbed — stated plainly.

---

# Status at end of build

| Module | Status | Evidence / note |
|---|---|---|
| M0 Scaffold, taxonomy, models | **done** | 17 categories, 116 subcategories, no orphans, serialisable |
| M1 Store (DuckDB) | **done** | Adaptive planner verified optimal at every AOI size; durable across restart |
| M2 Connector framework | **done** | 8 connectors registered through the decorator |
| M3 Catalonia connectors (6) | **done** | 54 704 rows loaded from live registries |
| M4 Spain national | **partial** | Population grid done (63 522 cells). **Not built:** Catálogo Nacional de Hospitales bed counts, REGCESS, CSIC care homes, national school registry |
| M5 OSM tile cache | **done** | Cold tile → 1.9 ms warm; per-tile failure attribution; 25 s deadline |
| M6 Enrichment | **partial** | CartoCiudad geocoding done (3 889 care homes located). **Not built:** cadastral building geometry / RCCOOR footprints |
| M7 Conflation | **done** | Calibrated scorer; 3 013 duplicates merged on the demo AOI |
| M8 Rust core + fallback | **done** | Wheel builds; 4 parity tests pass |
| M9 Valuation & scoring | **done** | Per-species livestock pricing; confidence decays to ≤0.35 on defaults |
| M10 Aggregator | **done** | Full lifecycle; degrades to warnings on every upstream failure |
| M11 API | **done** | 8 endpoints + OpenAPI, all verified 200 |
| M12 Website | **done** | 5 pages, live playground, sources rendered from the API |
| M13 Deployment | **partial** | Dockerfile, railway.json and .env.example written. **The image has not been built or deployed** — no Docker daemon was available in the build environment |
| M14 Verification | **done** | 41 tests; endpoint sweep; latency table; browser walkthrough |

## Known gaps, stated plainly

1. **Spanish national registries beyond the population grid are not implemented.** The
   conflation source-priority table already contains their ids, and the connector
   contract is the same, so each is a self-contained addition — but today, outside
   Catalonia the service returns OpenStreetMap only, and says so via `coverage_regime`.
2. **No cadastral footprints.** Building areas come from OSM polygons where present
   (~61 % of OSM assets in the test AOI) and from class defaults otherwise, which caps
   valuation confidence at 0.35 for the remainder.
3. **The Docker image is unbuilt and unverified.** It is written from the same commands
   used successfully here, but it has not been executed.
4. **The population grid is the 2011 census round.** The 2021 pan-European 1 km grid is
   not freely downloadable without registration from this environment.
5. **Overpass was largely unreachable from the build network** (only one mirror
   responded, intermittently). The multi-mirror pool, per-tile failure attribution and
   25 s deadline were all built and tested against that reality, but throughput on a
   healthy network has not been measured.
