# TALAIA — Architecture & Design

> **TALAIA** *(Catalan, /tə.ˈla.jə/)* — the hilltop watchtower that Mediterranean communities have
> used for centuries to spot fire and raise the alarm. Backronym:
> **T**erritorial **A**sset **L**ookup & **A**ggregation for **I**nfrastructure **A**wareness.
>
> One sentence: **give TALAIA a polygon, get back everything of value inside it.**

Built for HackBarna 2026 · DeepFire "Values at Risk" track.

---

## 1. Problem statement

A wildfire simulation produces geometry: a current fire perimeter, and a set of predicted
perimeters at t+1h, t+3h, t+6h, t+12h. That geometry is useless to an incident commander
on its own. The question that matters is:

> *What is inside those polygons, how many people, how much is it worth, who do I call, and
> how do I get there?*

Answering it means reconciling ~20 heterogeneous registries — Catalan open data, Spanish
national ministries, the cadastre, INE census grids, OpenStreetMap — each with different
schemas, coordinate conventions, languages, update cadences, licences and quality.

TALAIA is the **infrastructure layer**: a single API that turns a polygon into a structured,
provenance-tracked, valued inventory of exposed assets. It is deliberately *not* the fire
model and *not* the decision agent — it is the substrate both of those sit on.

### Position in the DeepFire stack

```
  ┌──────────────────────────────────────────────────────────────┐
  │  1. DeepFire API        fire detection + spread simulation    │
  │                         → time-banded perimeter polygons      │
  └───────────────────────────────┬──────────────────────────────┘
                                  │  GeoJSON FeatureCollection
                                  ▼
  ┌──────────────────────────────────────────────────────────────┐
  │  2. TALAIA  (this service)    polygon → values at risk        │
  │     aggregation · conflation · valuation · exposure rollup    │
  └───────────────────────────────┬──────────────────────────────┘
                                  │  structured ExposureReport
                                  ▼
  ┌──────────────────────────────────────────────────────────────┐
  │  3. Agentic risk manager      triage · prioritise · notify    │
  └──────────────────────────────────────────────────────────────┘
```

Design consequence: the output is shaped for **machine consumption by an LLM agent** first
and a human map second. Every asset carries a stable id, a category from a closed taxonomy,
a numeric vulnerability, a monetary estimate with its method, contact details where known,
and a provenance block. Aggregates are precomputed server-side so the agent never has to
sum 40,000 rows in its context window.

---

## 2. Design principles

| # | Principle | Consequence in the code |
|---|-----------|------------------------|
| 1 | **The polygon is the only required input.** | Everything else (layers, bands, buffers) has a sane default. A bare polygon returns a complete report. |
| 2 | **Latency is bounded by cache misses, not by data volume.** | Three-tier storage (§4). Warm AOI ≈ 10 ms; cold AOI ≈ one Overpass round-trip. |
| 3 | **Every value is attributable.** | No field enters the response without `source_id` + `retrieved_at`. Conflated records keep per-field provenance. |
| 4 | **Estimates are labelled as estimates.** | Valuations and populations carry `method`, `confidence`, `assumptions`. Capacity ≠ occupancy, ever. |
| 5 | **A new country is new config, not new architecture.** | Connectors are plugins registered in a registry; the taxonomy crosswalk is data. Nothing in the core knows Spain exists. |
| 6 | **Degrade, never fail.** | A dead upstream yields a `warnings[]` entry and a partial report, not a 500. |
| 7 | **The docs are generated from the running system.** | `/v1/sources` and `/v1/taxonomy` are introspection endpoints; the website renders them. Docs cannot drift. |

---

## 3. The canonical model

Everything from every source is normalised into one record type. This is the single most
important design decision in the system: it is what makes twenty registries queryable as one.

```jsonc
{
  "id": "tal_9f2c1a...",              // stable, deterministic (hash of source_id + source_ref)
  "category": "healthcare",           // closed taxonomy, 17 values
  "subcategory": "hospital",
  "name": "Hospital de Sant Joan de Déu",
  "geometry": { "type": "Point", "coordinates": [1.82, 41.73] },
  "geometry_kind": "point",           // point | footprint | line | area
  "address": { "street": "...", "postcode": "08242", "municipality": "Manresa",
               "province": "Barcelona", "region": "Catalunya", "country": "ES" },
  "contacts": { "phone": ["938..."], "email": ["..."], "website": "..." },
  "capacity": { "beds": 120, "people_estimate": 150, "basis": "registry_beds_2025" },
  "occupancy_note": "Registered capacity, not live occupancy.",
  "valuation": { "replacement_cost_eur": 18400000, "contents_eur": 3100000,
                 "method": "floorspace_unit_cost", "confidence": 0.55,
                 "assumptions": ["footprint 4200 m2", "3 floors", "1460 EUR/m2"] },
  "vulnerability": 85,                // 0-100, asset-class fire vulnerability
  "criticality": 95,                  // 0-100, consequence-of-loss
  "exposure": { "band": "0-3h", "distance_to_front_m": 412, "inside_aoi": true },
  "provenance": [
    { "source_id": "es.cat.equipaments", "source_ref": "12345",
      "retrieved_at": "2026-09-19T08:00:00Z", "fields": ["name","geometry","address"] },
    { "source_id": "es.msan.hospitales", "source_ref": "0801234",
      "retrieved_at": "2026-09-19T08:00:00Z", "fields": ["capacity.beds"] }
  ],
  "confidence": 0.82,
  "attributes": { /* source-specific passthrough, never lost */ }
}
```

### Taxonomy (17 categories)

`population` · `education` · `healthcare` · `social_care` · `emergency` · `transport` ·
`energy` · `water` · `telecom` · `industry` · `agriculture` · `livestock` · `residential` ·
`commercial` · `tourism` · `heritage` · `environment`

Each category carries defaults — a baseline `vulnerability`, a `criticality`, a valuation
model, and whether it is *human-bearing* (drives evacuation planning) or *asset-only*.
Subcategories refine those. The table lives in `talaia/taxonomy.py` and is served at
`/v1/taxonomy`, so the agent and the website read the same source of truth.

### Networks are not assets

Roads, power lines, railways and watercourses are **linear** and are modelled separately:
they are clipped to the AOI and reported as `networks` with per-class length, plus
`access_routes` analysis (which roads enter the AOI, whether they are severed by a band).
Summing "number of roads" is meaningless; kilometres cut and access severance are not.

---

## 4. The three-tier data strategy

This is the efficiency core of the system. The naive approaches both fail:
preloading all of Europe's OSM is too much disk and too slow to build; querying every
upstream API live is too slow and rate-limited. TALAIA splits sources by *cardinality and
volatility*.

```
                          ┌──────────── query: polygon ────────────┐
                          ▼                                        │
  ┌────────────────────────────────────────────┐                   │
  │ TIER A — RESIDENT                          │   R-tree lookup   │
  │ National/regional registries, fully loaded │   ~3-10 ms        │
  │ Schools, hospitals, care homes, farms,     │◄──────────────────┤
  │ INE population grid, municipal points      │                   │
  │ ~500k rows · refreshed nightly             │                   │
  └────────────────────────────────────────────┘                   │
  ┌────────────────────────────────────────────┐                   │
  │ TIER B — MATERIALISED ON DEMAND            │   tile-cached     │
  │ OpenStreetMap via Overpass                 │   miss: ~1-3 s    │
  │ Decompose AOI → 0.05° tiles → fetch only   │◄──────────────────┤
  │ stale/missing tiles → normalise into A     │   hit:  ~5 ms     │
  │ Unbounded source, bounded working set      │                   │
  └────────────────────────────────────────────┘                   │
  ┌────────────────────────────────────────────┐                   │
  │ TIER C — LAZY ENRICHMENT                   │   per-asset       │
  │ CartoCiudad geocoding, cadastral refs,     │   cached forever  │
  │ ministry bed counts, enrolment joins       │◄──────────────────┘
  │ Runs only for assets actually returned     │
  └────────────────────────────────────────────┘
```

### Why tile caching is the right answer for OSM

A fire AOI is small (tens of km²) but arbitrary. Caching *by query polygon* has a ~0% hit
rate; caching *by fixed tile* has a very high one, because consecutive simulation timesteps,
buffered variants and neighbouring fires all overlap the same ground.

The AOI is covered by a fixed 0.05°(~4 km) grid. For each covering tile we check
`osm_tile_cache.fetched_at`. Fresh tiles are served from DuckDB. Stale/missing tiles are
grouped into as few Overpass bbox queries as possible, fetched **in parallel across mirrors**,
normalised into the same `assets` table, and the tile is stamped fresh. The working set is
therefore proportional to *ground actually queried*, not to the size of Spain — and old tiles
can be evicted by LRU without any correctness impact, because a miss is always recoverable.

Overpass reliability is treated as a first-class problem: the connector round-robins a
configurable mirror list with per-mirror health tracking, exponential backoff and a hard
deadline, and on total failure degrades to Tier A with a warning rather than failing the
request. *(During development, the primary `overpass-api.de` endpoint was unreachable from
the build network while `maps.mail.ru` responded — which is exactly why this is multi-mirror.)*

### Storage engine: DuckDB + spatial

Chosen over PostGIS deliberately:

* **Embedded** — no second Railway service, no connection pool, no network hop per query.
* **Fast enough by a wide margin** — measured on this machine: R-tree spatial filter over
  500,000 points in **3.3 ms**. Our resident tier is that order of magnitude.
* **Columnar + Parquet-native** — bulk ingest of registry dumps is a single `COPY`; cold-start
  bootstrap can hydrate from committed Parquet snapshots.
* **Zero memory pressure** — data lives on disk (Railway volume at `/data`), memory-mapped and
  paged by the engine. The requirement "keep a local copy but don't keep it all in memory" is
  satisfied by the engine itself, not by hand-rolled caching.

The trade-off is single-writer concurrency. Handled by routing all writes through one
serialised writer task; reads are concurrent and unaffected. If the service ever needs
horizontal write scale, `Store` is an interface and a PostGIS implementation drops in behind it.

---

## 5. Conflation — turning N registries into one inventory

A hospital in Manresa appears in OSM (with a phone number), in *Equipaments de Catalunya*
(with an official id), and in the *Catálogo Nacional de Hospitales* (with a bed count).
Returning it three times would be actively harmful to a decision agent: it triples the
apparent exposure.

The conflation pipeline:

1. **Block** — candidates must share a category and fall in the same ~150 m grid cell
   (plus neighbours). Reduces an O(n²) comparison to near-linear.
2. **Score** — normalised name similarity (accent- and case-folded, legal-form stripped,
   token-set ratio) combined with haversine distance, plus hard signals (shared cadastral
   reference, shared official code) that short-circuit to a match.
3. **Merge** — the cluster elects a **primary** by source priority (official registry >
   cadastre > OSM), then fills missing fields from the rest. Field-level provenance is
   recorded, so `capacity.beds` can come from the ministry while `contacts.phone` comes
   from OSM, and the response says so.
4. **Never silently drop** — unmerged duplicates below threshold stay as separate assets
   with a `possible_duplicate_of` hint. The agent can decide; the pipeline does not guess.

---

## 6. Valuation

Fire risk management needs money as a triage signal. TALAIA computes **replacement cost**,
not market value, because that is what an insurer or a recovery budget actually faces.

```
  structure_eur = footprint_m² × floors × unit_cost(subcategory) × province_multiplier
  contents_eur  = structure_eur × contents_ratio(subcategory)
  livestock_eur = livestock_units × unit_value(species)
  total_eur     = structure_eur + contents_eur + livestock_eur
```

Inputs by preference: cadastral footprint area → OSM building polygon area → subcategory
default footprint. Floors from cadastre/OSM `building:levels` → subcategory default. Unit
costs are a documented table derived from Spanish construction-cost references, with a
per-province multiplier.

Every valuation emits `method` (which branch ran), `confidence` (0–1, degrading as we fall
back to defaults) and `assumptions` (human-readable list). **A valuation whose footprint was
a default carries confidence ≤ 0.35 and says so.** The methodology page on the website
documents the full table.

Human exposure is deliberately **never monetised** — people, students, patients and residents
are reported as counts with their basis, and the agent is instructed to treat them as a
separate, dominant axis.

---

## 7. Exposure computation & the Rust core

Given up to ~10⁵ assets and K band polygons, per request we must: assign each asset to the
earliest band it falls in, area-weight population grid cells against each band, compute
distance to the fire front, and roll everything up by band × category.

That is a tight numeric loop, so it lives in **`talaia_core`, a Rust crate exposed via PyO3**
(`geo` for predicates, `rstar` for indexing, `rayon` for parallelism). It exposes:

* `assign_bands(xs, ys, band_polygons) -> band_index[]`
* `area_weighted_overlay(cells, polygon) -> weighted_sum`
* `distance_to_front(xs, ys, front_lines) -> metres[]`
* `rollup(values, categories, bands) -> aggregates`

A **pure NumPy/Shapely fallback implementing the identical contract** ships alongside
(`TALAIA_CORE=python`), and the import is `try/except`. This is not decoration: it means a
Rust toolchain failure in a Railway build degrades performance instead of breaking the
deploy. A parity test asserts both implementations agree.

---

## 8. Extensibility — adding a country

The contract a connector signs:

```python
class Connector(ABC):
    meta: SourceMeta            # id, name, publisher, licence, coverage, cadence, url
    tier: Tier                  # RESIDENT | ON_DEMAND | ENRICHMENT
    async def fetch(self, ctx)  -> AsyncIterator[dict]    # raw records
    def normalise(self, raw)    -> Iterable[Asset]        # → canonical model
```

Adding Portugal means: write connectors under `connectors/pt/`, add their subcategories to
the crosswalk if they are genuinely new, register them. The aggregator, store, conflation,
valuation, scoring, API and website pick them up automatically — including the docs, because
the Data Sources page renders `/v1/sources`.

Coverage is explicit: every connector declares a bounding geometry, and a query outside all
declared coverage returns a clear `warnings[]` entry ("no resident sources cover this AOI;
results are OSM-only") rather than a misleadingly empty report.

---

## 9. Request lifecycle

```
POST /v1/exposure
  │
  ├─ 1. Validate & normalise AOI  (GeoJSON → bands, optional buffer, area guard)
  ├─ 2. Resolve layers → categories → which connectors are relevant
  ├─ 3. Tier B warm-up: tile cover → stale tiles → parallel Overpass fetch → upsert
  │      (skipped entirely if `live_osm=false`)
  ├─ 4. Tier A query: single R-tree spatial select across all resident sources
  ├─ 5. Conflate the union
  ├─ 6. Tier C enrichment for the surviving set (cached)
  ├─ 7. Valuation + vulnerability scoring
  ├─ 8. Rust core: band assignment, population overlay, distance, rollup
  ├─ 9. Assemble ExposureReport (summary → exposure → assets → networks → sources → warnings)
  └─ Response
```

Steps 3 and 4 overlap; step 6 is concurrent per asset. The report is assembled
**summary-first** so a streaming consumer gets the decision-relevant numbers before the
long asset array.

---

## 9b. Two front doors, one enforcement point

The service is consumed two ways: HTTP by programs, and MCP by agents. They are the same
code path, not two implementations.

```
           POST /v1/exposure            POST /mcp
                   │                        │
                   ├──── require_api_key ───┤     auth, rate limit, daily quota
                   │                        │
                   ├── _enforce_key_limits ─┤     area cap, asset ceiling
                   │                        │
                   └────── build_report ────┘     one aggregator
```

The obvious way to build the MCP server would be a second service holding its own copy of
the limit logic. That fails the first time a quota changes, and it fails silently and in
the direction of letting people through — the tool simply answers. Mounting MCP on the
same app behind the same dependency makes divergence impossible rather than unlikely.

The two differ only in response shaping. REST returns the full report to a program; MCP
returns a text summary plus structured content, capped at 200 assets, because the
consumer is a context window and a megabyte of JSON there is both useless and expensive.

The stdio bridge (`talaia.mcp_stdio`) is a pure proxy for the same reason: anything it
could decide locally, an attacker could decide locally too, by running their own copy.

## 9c. Pre-caching as a first-class operation

Tier B is the only tier with cold-start cost, so warming it is an explicit operation
rather than something that happens by accident.

The per-request warm and the bulk warm have opposite constraints. The request path has a
25-second deadline and abandons what has not arrived, because someone is waiting. The
bulk path has no deadline, runs two requests in parallel with a pause between them, and
commits after every block — because nobody is waiting and the real risk is being refused
by a donated Overpass mirror.

Resumption needs no state of its own: **the tile cache is the progress record**. A warm is
"fetch the tiles that are stale", run repeatedly, so an interrupted run resumes by being
re-run. Failed tiles are recorded with their error and left stale, which makes retry the
default rather than something to remember.

Because a warm writes, and DuckDB takes one writer, it must run inside the serving
process when the service is up — hence `POST /v1/admin/warm` rather than a cron job.

## 10. Deployment

Single Railway service. Multi-stage Dockerfile: Rust builder → Node builder (website) →
slim Python runtime carrying the compiled wheel and the static bundle. FastAPI serves both
the API and the site, so there is one URL and no CORS story.

A Railway volume mounts at `/data` for the DuckDB file. On boot the service checks whether
the resident tier is populated; if not it schedules a background bootstrap, so a cold deploy
self-heals and serves OSM-only results in the meantime rather than serving nothing.

---

## 11. Explicit non-goals and honest limitations

* **Not real-time occupancy.** Every capacity figure is a registered maximum. A school's
  enrolment is not the number of children present at 3 a.m. This is stamped on every record.
* **Not an appraisal.** Valuations are parametric replacement-cost estimates for triage.
* **Not a fire model.** TALAIA consumes perimeters; it does not predict them.
* **Coverage is uneven.** Catalonia is deep (six dedicated registries); the rest of Spain is
  national-registry + OSM; outside Spain is OSM-only. The API says which regime you are in.
* **Licences differ.** The CSIC care-home dataset is the restrictive one: its terms forbid
  use for advertising, sale or commercialisation and require a citation, so it is flagged
  `commercial_use: false`. It names no Creative Commons identifier, so neither does TALAIA.
  Each source carries its licence in `/v1/sources` and in the response's `sources` block,
  so a downstream user can filter on it.
