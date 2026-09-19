# TALAIA

> **T**erritorial **A**sset **L**ookup & **A**ggregation for **I**nfrastructure **A**wareness
>
> *Talaia* (Catalan, /tə.ˈla.jə/) — the hilltop watchtower Mediterranean communities have
> used for centuries to spot fire and raise the alarm.

**Give it a polygon. Get back everything at risk inside it.**

Built for **HackBarna 2026**, DeepFire *“Values at Risk”* track.

---

## The problem

A wildfire model outputs geometry: a current perimeter, and predicted perimeters at
t+1 h, t+3 h, t+6 h. That geometry is useless on its own. The question that matters is:

> *What is inside those polygons, how many people, how much is it worth, who do I call,
> and how do I get there?*

Answering it means reconciling ~20 heterogeneous registries — Catalan open data, Spanish
ministries, the cadastre, census grids, OpenStreetMap — with different schemas, coordinate
conventions, languages, update cadences, licences and quality.

TALAIA is the **infrastructure layer**: one API that turns a polygon into a structured,
provenance-tracked, valued inventory. It is deliberately *not* the fire model and *not*
the decision agent — it is the substrate both sit on.

```
  DeepFire ──► time-banded perimeters ──► TALAIA ──► ExposureReport ──► agent
  (detection + spread)                    (this)      (structured)      (triage)
```

---

## Quick start

```bash
python -m venv .venv && .venv/bin/pip install -e .
PYTHONPATH=api .venv/bin/python -m talaia ingest      # load resident sources (~2 min)
PYTHONPATH=api .venv/bin/python -m uvicorn talaia.main:app --port 8000
```

Open <http://localhost:8000> for the website, `/swagger` for the OpenAPI UI.

**The API is gated by default.** With no keys configured, the service mints one at first
boot and prints it to the log once — copy it from there, or set `TALAIA_API_KEYS`.

```bash
curl -X POST localhost:8000/v1/exposure \
  -H "X-API-Key: talaia_sk_…" -H 'content-type: application/json' -d '{
  "aoi": {"type":"Polygon","coordinates":[[[1.80,41.71],[1.87,41.71],
                                           [1.87,41.755],[1.80,41.755],[1.80,41.71]]]}}'
```

Frontend development, against a running API:

```bash
cd web && npm install && npm run dev     # proxies /v1 to 127.0.0.1:8099
```

---

## What it returns

One call yields a summary, per-band aggregates, and every asset with identity, geometry,
occupancy, replacement cost, a triage score and per-field provenance.

```jsonc
{
  "summary": {
    "asset_count": 1072,
    "people_estimate": 65604,          // facility occupancy at capacity
    "people_from_registry": 21256,     //   ...of which backed by a registry figure
    "people_from_defaults": 44788,     //   ...of which inferred from class defaults
    "population_resident": 93640,      // census residents, counted separately
    "total_value_eur": 3188153581,
    "critical_assets": 164, "hazardous_assets": 5,
    "coverage_regime": "catalonia_full: regional registries + national + OpenStreetMap"
  },
  "bands": [ { "band": "0-1h", "asset_count": 65, "people_estimate": 8688,
               "population_resident": 25885, "total_value_eur": 381146117 } ],
  "assets": [ { "name": "Residència Els Companys", "subcategory": "care_home",
                "capacity": { "places": 64, "basis": "registered places (RESES)" },
                "contacts": { "phone": ["+34938…"] },
                "exposure": { "band": "0-1h", "priority_score": 88.4 },
                "provenance": [ { "source_id": "es.cat.reses", "fields": ["identity"] },
                                { "source_id": "osm", "fields": ["contacts.phone"] } ] } ],
  "warnings": [ "Conflation merged 394 duplicate record(s) …" ]
}
```

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/exposure` | Full report |
| `POST` | `/v1/exposure/summary` | Aggregates only — for agent polling loops |
| `POST` | `/v1/assets` | NDJSON stream |
| `GET` | `/v1/sources` | Live source catalogue (the website renders this) |
| `GET` | `/v1/taxonomy` | 17 categories, 116 subcategories, scoring parameters |
| `GET` | `/v1/stats` · `/health` | Store contents, liveness |
| `POST` | `/v1/geocode` | CartoCiudad passthrough, cached |
| `POST`/`GET`/`DELETE` | `/v1/admin/keys` | Mint, list and revoke API keys |

---

## Authentication

Data endpoints — `/v1/exposure`, `/v1/exposure/summary`, `/v1/assets`, `/v1/geocode` —
require an API key. Send it as either header:

```
X-API-Key: talaia_sk_…
Authorization: Bearer talaia_sk_…
```

Keys are **never accepted in a query string**, where they would leak into access logs,
browser history and referrer headers.

**How keys are stored.** Only a SHA-256 hash is persisted, plus a short non-secret prefix
so a key can be listed and revoked without being exposed. Comparison is constant-time. A
dump of the database yields no usable credentials, and a key is shown exactly once — at
creation.

**Managing keys.** While the service runs, use the admin API (enabled only when
`TALAIA_ADMIN_KEY` is set; otherwise the endpoints 404 rather than advertising
themselves):

```bash
curl -X POST localhost:8000/v1/admin/keys -H "X-Admin-Key: $ADMIN" \
     -H 'content-type: application/json' \
     -d '{"label":"deepfire-agent","rate_limit_per_min":30}'

curl localhost:8000/v1/admin/keys -H "X-Admin-Key: $ADMIN"          # prefixes only
curl -X DELETE localhost:8000/v1/admin/keys/talaia_sk_xTj2zp... -H "X-Admin-Key: $ADMIN"
```

With the service stopped (DuckDB is single-writer), the CLI does the same:

```bash
PYTHONPATH=api python -m talaia key create --label deepfire-agent --limit 30
PYTHONPATH=api python -m talaia key list
PYTHONPATH=api python -m talaia key revoke talaia_sk_xTj2zp...
```

**Rate limiting** is per key, sliding 60-second window, default 120 req/min and
overridable per key. Responses carry `X-RateLimit-Limit` and `X-RateLimit-Remaining`;
a 429 carries `Retry-After`.

**What stays open:** `/health` (so Railway's health check works), the documentation
website, and the three metadata endpoints `/v1/sources`, `/v1/taxonomy`, `/v1/stats`,
which describe the service rather than returning exposure data and are rendered by the
public site. Set `TALAIA_PUBLIC_METADATA=false` to gate those too.

---

## How it works

Full design in [ARCHITECTURE.md](ARCHITECTURE.md); build order in [PLAN.md](PLAN.md).

### Three storage tiers

| Tier | Sources | Strategy |
|---|---|---|
| **Resident** | National/regional registries, census grid | Bulk-loaded locally, R-tree, ~10 ms |
| **On demand** | OpenStreetMap | Fixed 0.05° global tile grid; only stale tiles fetched, then cached |
| **Enrichment** | CartoCiudad geocoding, cadastral refs | Per-record, cached permanently |

Caching by *query polygon* would hit ~0 %, because every fire perimeter is unique.
Caching by *fixed tile* hits often, because consecutive timesteps, buffered variants and
neighbouring fires cover the same ground. **Measured: a cold tile costs one Overpass
round-trip; the same area from cache returns in 1.9 ms.**

### An adaptive query planner

Evaluating `ST_Intersects` against a stored geometry column costs ~0.13 ms/row (every
blob must be deserialised), so the R-tree path degrades linearly with matches.
Reconstructing a point from indexed lon/lat columns is ~7× cheaper but scans. The store
estimates matches from AOI area × density and picks; both paths return identical rows.

| AOI | rows | R-tree | scan | chosen |
|---|---|---|---|---|
| 2 km | 9 | **2.9 ms** | 9.2 ms | R-tree |
| 17 km | 618 | 160.5 ms | **36.9 ms** | scan |
| 51 km | 5 788 | 1401.3 ms | **76.4 ms** | scan (18× faster) |

### Measured end-to-end latency

Warm store, `live_osm=false`, Catalonia data (62 k assets, 63 k population cells):

| AOI | assets | store | conflate | score | **total** |
|---|---|---|---|---|---|
| 2 km | 561 | 58 ms | 86 ms | 130 ms | **309 ms** |
| 5 km | 2 502 | 231 ms | 452 ms | 369 ms | **1.30 s** |
| 17 km | 5 135 | 270 ms | 548 ms | 822 ms | **2.19 s** |
| 34 km | 6 741 | 369 ms | 648 ms | 881 ms | **2.53 s** |

A warm OSM tile adds ~2 ms; a cold one costs an Overpass round-trip, hard-bounded by a
25 s budget after which unfinished tiles are abandoned with a warning rather than holding
up the response.

### A Rust numeric core

Band assignment, distance-to-front, cell overlay and rollups run in Rust (PyO3 + rayon).
A NumPy fallback implements the identical contract and a parity test asserts they agree —
so a missing Rust toolchain costs performance, not the deploy.

### Conflation

A hospital appears in OSM (phone), *Equipaments* (official id) and the *Catálogo de
Hospitales* (beds). Block on category + ~150 m cell → score on folded name similarity and
distance → merge with source priority, keeping **per-field provenance**.

The scorer is calibrated against real pairs. `token_set_ratio` returns 100 whenever one
name's tokens are a subset of the other's, which scores *“Escola Pia”* vs *“Escola Pia
Annex”* identically to a true duplicate; a plain ratio over the sorted name key separates
them (100 vs 77). Near-misses are never dropped — they carry `possible_duplicate_of`.

---

## Data sources

Rendered live at `/sources`. Currently loaded:

| Source | Publisher | Rows | Notes |
|---|---|---|---|
| `es.cat.schools` | Dept. d'Educació | 5 434 | Pinned to the latest `curs`; phone + email |
| `es.cat.enrolments` | Dept. d'Educació | join | 470 k rows aggregated server-side to per-school totals |
| `es.cat.equipaments` | DG Serveis Digitals | 24 545 | Hospitals, CAPs, libraries, sport, civic |
| `es.cat.livestock` | DARP (REGA) | 19 889 | Merged per holding; DMS coordinates |
| `es.cat.reses` | Dept. Drets Socials | 3 889 | Care homes — geocoded, no coordinates published |
| `es.cat.munipoints` | ICGC | 947 | Municipal centroids, low-confidence fallback |
| `es.ine.popgrid` | Eurostat GISCO / INE | 63 522 | 1 km census population cells |
| `osm` | OpenStreetMap | on demand | Global; roads, footprints, contacts |

Adding a country is a connector plus a crosswalk entry — storage, conflation, valuation,
scoring, the API and the docs pick it up automatically.

### Data quirks this handles

Each of these silently corrupts results if ignored, and each was found by checking the
live data rather than the documentation:

- **Schools** publish a `geo_1` GeoJSON member whose coordinates have lost their decimal
  point (`2253505` for `2.253505`). The separate lon/lat columns are used instead.
- **Livestock** coordinates are DMS *strings* (`42.0º 8.0' 33.0498''`). `float()` yields
  `42.0` — an error of up to ~100 km.
- **Livestock** rows repeat per (holding × species × production type), and `total_ub`
  repeats the same figure across a species group. Naive summing inflated capacity ~45 %.
- **`total_ub` is a headcount, not a livestock unit** despite the name: its maximum is
  280 485 against an egg producer. Valuing that as livestock units gave €196 M for one
  farm; priced per species it is €2.9 M.
- **`Instal·lacions`** folds to `installacions` (geminate ll). Matching a single `l`
  silently dropped ~3 000 sports facilities.
- **CartoCiudad returns nothing** when a query includes the postcode or region, and does
  not resolve abbreviated street types. Expanding `Pça.de l'` → `Plaça de l'` and dropping
  the postcode took the care-home geocoding hit rate from 2/4067 to 3 889/4 067.

---

## Deployment

Single Railway service: multi-stage Dockerfile (Rust → Node → Python) serving the API and
the website from one process. Mount a volume at `/data`; an empty store self-bootstraps
in-process on boot (DuckDB is single-writer, so a separate ingest process would be locked
out).

```bash
docker build -t talaia . && docker run -p 8000:8000 -v talaia-data:/data talaia
```

See `.env.example` for configuration.

---

## Tests

```bash
.venv/bin/python -m pytest tests/ -q      # 41 tests
```

Covers coordinate transforms against published ground truth, AOI banding, the taxonomy,
valuation branches, conflation precision and recall, Rust/NumPy parity, and end-to-end
report assembly.

---

## Limitations

Stated plainly, and repeated in the API responses:

- **Capacity is not occupancy.** Every figure is a registered maximum. A school's
  enrolment is not the children present at 3 a.m.
- **Valuations are not appraisals.** Parametric replacement-cost estimates for triage,
  each carrying its method, confidence and assumptions.
- **Coverage is uneven.** Catalonia has six dedicated registries; the rest of Spain is
  national data plus OSM; outside Spain is OSM only. Every response says which regime.
- **The population grid is the 2011 census round** — the most recent pan-European 1 km
  grid freely downloadable without registration. Good for relative exposure; treat
  absolute counts as indicative.
- **Livestock capacity is a licensed maximum**, Catalonia-wide roughly 4× the census herd.
- **TALAIA does not predict fire.** It consumes perimeters.
