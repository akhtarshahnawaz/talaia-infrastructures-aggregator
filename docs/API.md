# TALAIA API reference

Complete reference for the REST API and the MCP tools. Written to be read end to end by a
program or an agent that has never seen this service: every field is defined, every number
says what it means and where it came from, and every estimate says how it was made.

If you read nothing else, read [What the numbers mean](#4-what-the-numbers-mean). Most
ways of misusing this API come from treating a capacity figure as an occupancy figure or a
replacement cost as a market value.

---

## Contents

1. [What this service is](#1-what-this-service-is)
2. [Authentication and limits](#2-authentication-and-limits)
3. [Endpoints](#3-endpoints)
4. [What the numbers mean](#4-what-the-numbers-mean)
5. [The asset record, field by field](#5-the-asset-record-field-by-field)
6. [The taxonomy](#6-the-taxonomy)
7. [Capacity: how people counts are derived](#7-capacity-how-people-counts-are-derived)
8. [Valuation](#8-valuation)
9. [Triage scoring](#9-triage-scoring)
10. [Population](#10-population)
11. [Networks](#11-networks)
12. [Conflation and provenance](#12-conflation-and-provenance)
13. [Time bands](#13-time-bands)
14. [Warnings](#14-warnings)
15. [Errors](#15-errors)
16. [MCP tools](#16-mcp-tools)
17. [Data sources](#17-data-sources)
18. [Worked examples](#18-worked-examples)

---

## 1. What this service is

**Input:** a polygon. **Output:** a structured inventory of everything of value inside it.

TALAIA is the infrastructure layer between a wildfire spread model and a decision. It does
not detect fires and it does not predict them — it consumes perimeters and answers *what is
inside this shape, how many people, how much is it worth, who do I call, and how do I get
there.*

It is explicitly **not**:

| Not | Because |
|---|---|
| a fire model | it consumes perimeters, it does not produce them |
| a live occupancy feed | every capacity figure is a registered maximum |
| a property appraisal | valuations are parametric replacement cost for triage |
| uniformly deep | coverage varies by region, and every response says which regime applies |

**Coverage today:** Spain. Catalonia has six regional registries; the rest of Spain has four
national registries plus the census population grid; OpenStreetMap covers everywhere.
`summary.coverage_regime` on every response tells you which of the three you are in:

```
catalonia_full : regional registries + national sources + OpenStreetMap
spain_national : national registries + OpenStreetMap
osm_only       : OpenStreetMap only  (also raises a warning)
```

---

## 2. Authentication and limits

Every data endpoint requires an API key, in either header:

```
X-API-Key: talaia_sk_…
Authorization: Bearer talaia_sk_…
```

Keys are **never accepted in a query string** — they would leak into access logs, browser
history and referrer headers. Only a SHA-256 hash is stored, so a key is shown exactly once,
at creation, and cannot be recovered.

Get one at `POST /v1/signup` (self-service, free tier) or from the operator.

### Getting a key: two steps

Signup is **email-verified**. `POST /v1/signup` creates nothing — it mails a single-use
link and returns `202`:

```jsonc
{ "status": "verification_sent", "email": "you@org.example",
  "expires_in_hours": 24,
  "message": "Check you@org.example for a confirmation link. …" }
```

Following that link calls `GET /v1/verify?token=…` (or `POST /v1/verify` with
`{"token": "…"}`), which issues the key and returns it **once**:

```jsonc
{ "api_key": "talaia_sk_…", "prefix": "talaia_sk_bVIuVQ...",
  "tier": "free", "email_verified": true, "limits": { … },
  "warning": "Store this key now. It is hashed on arrival and cannot be shown again." }
```

The key is shown on that page and is **never emailed** — email is not a confidential
channel, and a credential mailed to someone sits in their inbox indefinitely. Only a hash
of the token is stored, the token works once, and requesting a new link invalidates the
previous one.

| Status | Meaning |
|---|---|
| `202` | Link sent. No key exists yet. |
| `409` | That address already has an active key, or that link was already used |
| `410` | The link expired — request a new one |
| `404` | The token is not valid |
| `429` | Per-IP daily signup cap reached |
| `503` | Verification is required but this deployment has no mail sender, or the send failed — no key is issued either way |

That last one is deliberate: falling back to issuing an unverified key would silently undo
verification while the operator believed addresses were being checked.

An operator can set `TALAIA_REQUIRE_EMAIL_VERIFICATION=false`, in which case `/v1/signup`
returns a key immediately with `email_verified: false` and a `note` saying the address was
never confirmed.

### Tiers

| Tier | Area per call | Rate | Daily | Assets per call |
|---|---|---|---|---|
| `free` | 250 km² | 60/min | 1,000 | 2,000 |
| `standard` | 2,500 km² | 300/min | 20,000 | 20,000 |
| `unlimited` | — | — | — | 50,000 |

`0` or `—` means unlimited. Live values: `GET /v1/tiers` (public). Your own: `GET /v1/me`.

**The area cap is the one that will stop you.** It is checked before any work begins, and it
is computed on `aoi` **plus `buffer_m`** — a 2 km polygon with a 20 km buffer is a 1,500 km²
request. Over the limit returns `403` naming your area and your allowance.

There is also a service-wide ceiling (`TALAIA_MAX_AOI_KM2`, default 25,000 km²) that applies
to **every** key including unlimited ones, and returns `422`. The unlimited tier removes
quota limits, not the guard against building a report for half a country.

Response headers on every call:

```
X-RateLimit-Limit: 60
X-RateLimit-Remaining: 57
X-Quota-Remaining-Today: 984      (omitted when the quota is unlimited)
X-Request-Id: req_a1b2c3d4e5f6
X-Response-Time-Ms: 812.4
```

---

## 3. Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `POST` | `/v1/exposure` | key | Full report: summary, bands, assets, networks, population, sources, warnings |
| `POST` | `/v1/exposure/summary` | key | Aggregates only, no asset array — for polling loops |
| `POST` | `/v1/assets` | key | The same assets as an NDJSON stream |
| `POST` | `/v1/population` | key | Population surface only — skips assets, OSM, conflation, scoring |
| `POST` | `/v1/geocode` | key | Spanish address → coordinates, cached |
| `GET` | `/v1/me` | key | Your tier, limits and usage today |
| `POST` | `/mcp` | key | Model Context Protocol endpoint |
| `GET` | `/v1/sources` | open¹ | Live catalogue of connectors with licence and load status |
| `GET` | `/v1/taxonomy` | open¹ | Every category and subcategory with its parameters |
| `GET` | `/v1/stats` | open¹ | Store contents, by source and category |
| `GET` | `/v1/tiers` | open | Tier limits |
| `GET` | `/v1/regions` · `/v1/coverage` | open¹ | Warmable regions, and what is cached |
| `POST` | `/v1/signup` | open | Request a key; sends a confirmation email |
| `GET`/`POST` | `/v1/verify` | open | Exchange the emailed token for the key, once |
| `GET` | `/health` | open | Liveness and row counts |
| `*` | `/v1/admin/*` | admin key | Key management, cache warming and mail diagnostics |

¹ Open by default; an operator can gate them with `TALAIA_PUBLIC_METADATA=false`.

### Request body

All the `POST /v1/*` data endpoints take the same object. Only `aoi` is required.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `aoi` | GeoJSON | **required** | Geometry, Feature or FeatureCollection. A FeatureCollection whose features carry a `band` property is read as time-banded perimeters — see [§13](#13-time-bands). |
| `layers` | string[] | all | Category or subcategory keys to include, e.g. `["healthcare","social_care"]`. A category key selects all of its subcategories. |
| `buffer_m` | number | `0` | Outward buffer in metres, applied to every band. **Counts towards your area cap.** |
| `band_property` | string | `"band"` | Feature property holding the band label |
| `minutes_property` | string | `"minutes"` | Feature property holding minutes-to-arrival |
| `include_assets` | bool | `true` | Return the asset array |
| `include_networks` | bool | `true` | Return roads, power and rail clipped to the AOI |
| `include_population` | bool | `true` | Run the census population overlay |
| `include_population_grid` | bool | `false` | Also return the individual 1 km cells — see [§10](#10-population) |
| `include_geometry` | bool | `true` | Return per-asset geometry (and cell polygons, when the grid is on) |
| `live_osm` | bool | server default | Fetch missing OpenStreetMap tiles. `false` guarantees a fast answer from local data only. |
| `enrich` | bool | server default | Run Tier C enrichment (geocoding) |
| `conflate` | bool | `true` | Merge cross-source duplicates |
| `max_assets` | int | tier | Cap the returned array. Clamped **down** to your tier's ceiling, never up. The summary still counts everything. |
| `sort_by` | enum | `priority` | `priority` · `distance` · `value` · `category` |

### Response envelope

```jsonc
{
  "request_id": "req_a1b2c3d4e5f6",
  "generated_at": "2026-09-20T09:15:22Z",
  "aoi_bbox": [1.80, 41.71, 1.87, 41.755],
  "summary":    { … },   // §4
  "bands":      [ … ],   // §13
  "assets":     [ … ],   // §5
  "assets_truncated": false,
  "networks":   { … },   // §11
  "population": { … },   // §10
  "sources":    [ … ],   // §17
  "warnings":   [ … ],   // §14
  "timing":     { … }
}
```

`timing` reports `total_ms`, `osm_fetch_ms`, `store_query_ms`, `conflation_ms`,
`enrichment_ms`, `scoring_ms`, the tile counts (`tiles_total`, `tiles_fetched`,
`tiles_cached`) and `core_impl` (`rust` or `python`). Useful for telling a slow network
apart from a slow query.

---

## 4. What the numbers mean

This section exists because the three most important figures are all easy to misread.

### `people_estimate` is capacity, not occupancy

It is the sum of *registered capacity* across assets in the area — how many people the
buildings are licensed or equipped to hold. It is **not** how many are there now, and it
does not subtract anyone already evacuated. A school's figure is its enrolment, which is
wrong at 3 a.m. and wrong in August.

The summary splits it so you can tell how much to trust it:

| Field | Meaning |
|---|---|
| `people_estimate` | total |
| `people_from_registry` | the part backed by a published capacity figure — beds, places, enrolment |
| `people_from_defaults` | the part inferred from the asset's class — treat as an order of magnitude |

A large `people_from_defaults` share means most of that number is a guess from building
type. Report the split, not just the total.

### `population_resident` is a different quantity

Census residents, area-weighted from 1 km grid cells. It counts *people who live there*,
which overlaps with `people_estimate` only loosely — a hospital's beds and the residents of
the surrounding blocks are different people. **Do not add the two together.** Use
`people_estimate` for facilities to evacuate and `population_resident` for the ambient
population.

### `total_value_eur` is replacement cost

What it would cost to rebuild, not what it would sell for. No land value, no business
interruption, no cultural or ecological value. It is a triage ordering device — good for
"this is worth ten times that", useless as an insurance figure. Every asset's `valuation`
carries the `method`, a `confidence` and the `assumptions` used.

---

## 5. The asset record, field by field

```jsonc
{
  "id": "tal_9f2c1a7b3e5d8c4a1b2f",
  "category": "social_care",
  "subcategory": "care_home",
  "name": "Residència Els Companys",
  "geometry": { "type": "Point", "coordinates": [1.8678, 41.7554] },
  "geometry_kind": "point",
  "lon": 1.8678, "lat": 41.7554,
  "address": { … }, "contacts": { … }, "capacity": { … }, "valuation": { … },
  "vulnerability": 75, "criticality": 100,
  "hazardous": false, "response_asset": false, "human_bearing": true,
  "exposure": { … },
  "occupancy_note": "Highest evacuation priority: low mobility, high dependency, 24h occupancy.",
  "provenance": [ … ],
  "confidence": 0.61,
  "possible_duplicate_of": [],
  "merged_count": 2,
  "attributes": { … }
}
```

| Field | Type | Meaning |
|---|---|---|
| `id` | string | Stable and deterministic — a hash of source id + source reference. The same asset keeps the same id across requests and across re-ingests. |
| `category` / `subcategory` | string | Closed vocabulary, [§6](#6-the-taxonomy). Never free text. |
| `name` | string \| null | As published. Null when the registry has none. |
| `geometry` | GeoJSON \| null | Present unless `include_geometry` is false. |
| `geometry_kind` | enum | `point` · `footprint` · `line` · `area`. **`point` means we know where it is, not how big it is** — about two thirds of assets, because Spanish registries publish points. Footprints come mostly from OpenStreetMap. |
| `lon` / `lat` | number | Representative point (centroid for footprints). Always present. |
| `vulnerability` | 0–100 | Likelihood fire destroys this asset class. A timber yard scores high, a concrete substation low. |
| `criticality` | 0–100 | Consequence of losing it, independent of how likely that is. A substation scores high on criticality and low on vulnerability. |
| `hazardous` | bool | Losing it makes the fire worse — fuel, gas, chemicals, explosives. Treat as an exclusion-zone trigger. |
| `response_asset` | bool | Helps fight the fire: fire stations, water tanks, helipads, hydrants. **These are not only at risk, they are resources** — losing one is a capability loss. |
| `human_bearing` | bool | People are normally present. Drives evacuation planning. |
| `occupancy_note` | string \| null | Plain-language caveat about when this class is actually occupied. |
| `confidence` | 0–1 | Overall confidence in the record, raised by corroboration across sources. |
| `merged_count` | int | How many source records became this one, [§12](#12-conflation-and-provenance). |
| `possible_duplicate_of` | string[] | Ids that looked similar but did not clear the merge threshold. Review these before counting. |
| `attributes` | object | Source-specific passthrough, never discarded. Keys vary by source. |

### `address`

`street`, `housenumber`, `postcode`, `municipality`, `comarca`, `province`, `region`,
`country` (default `ES`), `full`. Fields may be absent; nothing is invented.

### `contacts`

`phone[]`, `email[]`, `website`, `operator`, `emergency_contact`. Phones are normalised to
E.164 (`+34…`). Arrays because conflation merges contacts from several registries onto one
record — see `provenance` for which source each came from.

### `exposure`

| Field | Meaning |
|---|---|
| `band` / `band_index` | The **earliest** band containing this asset. Assets are assigned once, to the first front that reaches them. |
| `band_minutes` | Minutes until that band's front arrives |
| `distance_to_front_m` | Metres to the nearest band boundary |
| `inside_aoi` | Whether it is inside the union of all bands |
| `priority_score` | 0–100 composite triage score, [§9](#9-triage-scoring) |

---

## 6. The taxonomy

17 categories, 116 subcategories, closed. `GET /v1/taxonomy` returns the whole thing with
each subcategory's `vulnerability`, `criticality`, `human_bearing`, `unit_cost`,
`contents_ratio`, `footprint`, `floors` and `people` defaults.

```
population   education   healthcare   social_care   emergency   transport
energy       water       telecom      industry      agriculture livestock
residential  commercial  tourism      heritage      environment
```

Two sets cut across the categories and are worth filtering on directly:

- **`hazardous`** — fuel stations, gas, chemicals, explosives, timber, waste.
- **`response_assets`** — fire stations, water tanks, hydrants, helipads, civil protection.

Pass a category key in `layers` to select all of its subcategories, or a subcategory key for
just that one. Omit `layers` for everything.

---

## 7. Capacity: how people counts are derived

The `capacity` object carries whichever figures the source published, plus a `people`
estimate and — critically — a `basis` string saying how it was obtained and a `confidence`.

| Field | Unit | Typical source |
|---|---|---|
| `people` | persons | derived, see below |
| `beds` | beds | hospital registries |
| `students` | pupils | school enrolment returns |
| `places` | licensed places | care homes, day centres |
| `animals` | head | livestock registers |
| `livestock_units` | LSU/UB | normalised across species |
| `dwellings` | dwellings | residential |

### Derivation by source, with the multipliers used

| Source | Published figure | `people` | Confidence | Why |
|---|---|---|---|---|
| `es.msan.hospitales` | `CAMAS` (staffed beds) | beds × 2.2 | 0.75 | inpatients plus staff plus visitors |
| `es.csic.carehomes` | `Plazas` (licensed places) | places × 1.33 | 0.80 | residents plus staff at ~1 per 3 |
| `es.cat.schools` + `es.cat.enrolments` | enrolment | enrolment × 1.12 | 0.75 | pupils plus 12 % staff |
| `es.cat.reses` | registered places | places | 0.70 | as published |
| `es.cat.livestock` | head per species | — (`animals`) | 0.55 | REGA registered capacity, converted to livestock units |
| OpenStreetMap | `capacity:*` tags | as tagged | 0.45 | contributor-entered, unverified |
| anything else | — | class default × footprint | ≤0.35 | inferred from building type and size |

**`basis` always states which row above applied.** When you see
`"registered staffed beds (CNH) x 2.2 for staff and visitors"` you know both the number and
the assumption. When `basis` is absent or names a class default, the figure is an order of
magnitude, not a count.

### What has capacity today

| Layer | Coverage |
|---|---|
| Hospital beds | national — 731 of 825 hospitals |
| Care-home places | national — 6,626 homes |
| School enrolment | **Catalonia only** — 4,380 schools |

School enrolment outside Catalonia is a class default, because the national registry is a
directory with no pupil counts; enrolment is published per autonomous community.

---

## 8. Valuation

Replacement cost, built from footprint × floors × a per-class unit cost, plus contents at a
per-class ratio, plus livestock valued per head.

```jsonc
"valuation": {
  "replacement_cost_eur": 4184000,
  "contents_eur": 836800,
  "livestock_eur": 0,
  "total_eur": 5020800,
  "method": "default_footprint+source_floors",
  "confidence": 0.27,
  "assumptions": ["footprint 4200 m2 (class default)", "3 floors", "1460 EUR/m2"],
  "currency": "EUR"
}
```

`method` is a `+`-joined list of what was actually known:

| Token | Meaning | Effect on confidence |
|---|---|---|
| `measured_footprint` | polygon area from OSM or a registry | highest |
| `source_footprint` | footprint published as a number | high |
| `default_footprint` | **inferred from the class** | caps confidence at **0.35** |
| `source_floors` | floor count published | raises |
| `livestock_per_head` | per-species head price | high |
| `livestock_units` | priced per LSU | medium |
| `livestock_headcount` | head count without species | low |
| `class_default` | nothing known but the class | lowest |
| `not_valued` | no basis at all | `confidence: 0.0` |

A regional multiplier adjusts unit costs by province. Land is never valued.

**Read `confidence` before quoting a figure.** Anything at 0.35 is sized by a class default
footprint — the order of magnitude is meaningful, the digits are not.

---

## 9. Triage scoring

`exposure.priority_score`, 0–100, is a weighted blend designed so that **life safety
dominates**:

| Component | Weight | Scaling |
|---|---|---|
| People exposed | **0.38** | log, saturating at 1,000 |
| Criticality | 0.24 | linear |
| Urgency (how soon the front arrives) | 0.16 | linear in band order |
| Vulnerability | 0.14 | linear |
| Replacement value | **0.08** | log, saturating at €50 M |

Then two multipliers, capped at 100:

- **× 1.25** if `hazardous` — it will make the fire worse
- **× 1.10** if `response_asset` — losing it costs you capability

People and value are log-scaled on purpose: the difference between 10 and 100 people
matters far more than between 1,000 and 1,090, and the same for €100 k against €1 M. Value
carries the smallest weight by design — this ranks what to save, not what is expensive.

`summary.top_priority` gives the highest-scoring assets already sorted, which is usually
what you want rather than re-sorting the full array.

---

## 10. Population

```jsonc
"population": {
  "total": 179733.0,
  "method": "ine_grid_area_weighted",
  "cell_count": 31,
  "confidence": 0.75,
  "peak_density_per_km2": 33551.0,
  "by_band": { "0-1h": 40873.0, "1-3h": 350727.0 },
  "cells": [ … ],
  "cells_truncated": false,
  "note": "Census residents, area-weighted from 1 km2 grid cells. …"
}
```

Source is the 1 km census grid (GEOSTAT/INE, 2011 round). Each cell contributes the share
of its population equal to the share of its area inside the AOI.

Set `include_population_grid: true` for the per-cell breakdown, **densest first**:

| Cell field | Meaning |
|---|---|
| `cell_id` | Grid identifier, e.g. `1kmN2067E3662` |
| `lon` / `lat` | Cell centroid |
| `population` | Residents in the **whole** cell |
| `population_in_aoi` | The share inside your polygon — **these sum to `total` exactly** |
| `overlap_fraction` | 0–1, how much of the cell is inside |
| `density_per_km2` | The **whole cell's** density |
| `area_km2` | Cell area, ~1 |
| `band` | Earliest band covering the **centroid** |
| `geometry` | Cell polygon, only when `include_geometry` is also set |

Two traps, both deliberate design choices:

1. **`density_per_km2` is the whole cell's, not the clipped part's.** A cell half inside
   your polygon describes the same neighbourhood, half observed. Scaling its density by the
   overlap would invent a gradient at the AOI edge that does not exist on the ground.
2. **A cell's `band` is a centroid label for mapping.** Summing cells by it will *not*
   reproduce `by_band`, which is area-weighted and exclusive. **Quote `by_band`** for
   per-band population; use the cell label for placement.

The list is capped at the 5,000 densest cells with a warning; `total` always counts every
cell.

`POST /v1/population` returns this object alone. It takes a deliberate shortcut past the
report builder, because assets, networks and OpenStreetMap change none of these numbers —
measured on a 28 km² area over a warmed Barcelona, **2,664 ms through the full report
against 10 ms direct**, for byte-identical population figures.

**Limits:** census residents at home. Not tourists, not daytime workers, not commuters, and
not anyone already evacuated. The 2011 round is the latest freely redistributable edition.

---

## 11. Networks

Linear infrastructure is kept out of `assets` because counting "number of roads" is
meaningless, while kilometres cut and access severance are not.

```jsonc
"networks": {
  "by_class": [ { "subcategory": "road_primary", "label": "Primary road",
                  "length_km": 12.4, "feature_count": 7 } ],
  "total_length_km": 84.2,
  "access_routes": [ … ],
  "geojson": { … }
}
```

Lengths are **clipped to the AOI**, not the full length of each road.
`access_routes` lists roads crossing the AOI boundary — candidate access and egress.

**Networks come only from OpenStreetMap.** No registry in this service publishes linear
features, so a deployment whose tile cache is cold reports zero networks until an area is
queried or warmed. That is a loading state, not a bug.

---

## 12. Conflation and provenance

The same hospital may appear in a national registry, a regional one and OpenStreetMap.
Conflation merges them into one asset and records what each contributed.

**How records are matched:** grid blocking, then a fuzzy comparison of accent-folded,
token-sorted names combined with haversine distance. A pair merges when it is very close
with a weak name match, close with a strong one, or has a near-identical name within 400 m.
Identical official codes within one source merge outright.

**Which record wins:** a fixed source-priority table, highest first. The winner supplies
identity and geometry; the others fill gaps — a phone number here, a bed count there. A
measured footprint always beats no footprint regardless of priority.

```jsonc
"provenance": [
  { "source_id": "es.msan.hospitales", "source_ref": "1531000730",
    "fields": ["identity", "geometry"] },
  { "source_id": "osm", "source_ref": "way/123456789",
    "fields": ["contacts.phone", "footprint_m2"] }
]
```

Every merge raises `confidence` — independent registries agreeing is real evidence the
thing exists. `merged_count` says how many records became this one.

Pairs that were similar but *not* merged appear in `possible_duplicate_of`. **Check that
field before reporting a count**: they are either genuine duplicates the thresholds missed,
or genuinely distinct neighbours.

Set `conflate: false` to see the raw per-source records instead.

---

## 13. Time bands

Pass a `FeatureCollection` whose features carry a `band` property and TALAIA treats them as
time-ordered fire perimeters:

```jsonc
{"type": "FeatureCollection", "features": [
  {"type": "Feature", "properties": {"band": "0-1h", "minutes": 60},  "geometry": {…}},
  {"type": "Feature", "properties": {"band": "1-3h", "minutes": 180}, "geometry": {…}}
]}
```

Bands are ordered by `minutes` where present, otherwise by area. A label like `"0-3h"` or
`"t+90min"` is parsed for minutes if the property is missing.

Two different rules apply, and mixing them up is the commonest error:

- **Assets** are assigned to the **earliest** band containing them. Each asset appears once.
  Band asset counts are therefore exclusive and sum to the total.
- **Population** per band is **area-exclusive**: each band counts only the ground it adds
  over all earlier bands. Spread-model perimeters are nested — the 6 h shape contains the
  1 h shape — so overlaying them as published would count the same residents in every band.

Both columns are exclusive, so they are directly comparable.

---

## 14. Warnings

`warnings[]` is a list of plain-language strings. **Always read it.** A degraded upstream, a
truncated result or an uncovered area is reported here on an otherwise complete response,
because a partial answer during an incident beats an error page.

| Warning | What it means | What to do |
|---|---|---|
| `OpenStreetMap fetch exceeded the …s budget` | Tiles were abandoned at the deadline | Coverage may be partial; retry shortly, the cache keeps what landed |
| `…OpenStreetMap requests failed…` | Upstream errors | Registry data is unaffected; tiles retry after a backoff |
| `AOI covers N OSM tiles, above the …-tile limit` | Area too large for a live fetch | Results use resident sources only; warm the region or split the AOI |
| `Conflation merged N duplicate record(s)…` | Normal operation | Informational — explains why the count is below the raw row count |
| `Population grid truncated to the N densest cells…` | More than 5,000 cells | `total` is still complete; only the per-cell list is cut |
| `This AOI is outside Spain; only OpenStreetMap covers it` | No registry coverage | Capacity, contacts and valuations will be sparse |
| `Live OpenStreetMap fetch failed…` | Tier B unavailable | Results use locally held sources only |

---

## 15. Errors

| Status | Meaning |
|---|---|
| `401` | Missing, invalid or revoked API key |
| `403` | Area above **your tier's** cap. The message names your area and your allowance. |
| `404` | Geocoder found no match; or an admin route with no admin key configured |
| `409` | An active key already exists for that email |
| `422` | Malformed AOI, or area above the **service-wide** ceiling |
| `429` | Rate limit or daily quota exhausted — carries `Retry-After` |
| `500` | Unhandled error, with a typed envelope |

```jsonc
{ "detail": "Area of interest is 1,479.7 km², above the 250 km² limit for the 'free' tier. Split the request into smaller polygons, or request a higher tier." }
```

Upstream degradation is never an error. It is a warning on a complete response.

---

## 16. MCP tools

`POST /mcp` speaks the Model Context Protocol over stateless Streamable HTTP, behind the
same key and the same limits. See [MCP.md](MCP.md) for transports and client configuration.

| Tool | Returns |
|---|---|
| `talaia_exposure_summary` | Aggregates, per-band breakdown, top triage scores. No asset list. |
| `talaia_list_assets` | Individual assets, ranked, capped at 200 |
| `talaia_population_grid` | Densest census cells, with overlap and band |
| `talaia_geocode` | Address → coordinates |
| `talaia_taxonomy` | The vocabulary |
| `talaia_my_limits` | Tier, caps, usage today |
| `talaia_coverage` | Which regions are cached |

Areas are given as GeoJSON **or** as `lon` / `lat` / `radius_km`. Each tool returns readable
text plus `structuredContent` holding the same JSON the REST API would return. The figures
are identical by construction — both interfaces run the same limit check and the same report
builder, and a test suite compares them field by field.

Limit refusals arrive as a tool result with `isError: true` and the reason in plain
language, so a model can split the area and retry.

---

## 17. Data sources

`GET /v1/sources` renders this live, with per-source row counts, licences and load status.

**Catalonia — regional**

| id | Publisher | What it adds |
|---|---|---|
| `es.cat.schools` | Dept. d'Educació | Schools with phone and email |
| `es.cat.enrolments` | Dept. d'Educació | **Pupil counts per school** |
| `es.cat.equipaments` | DG Serveis Digitals | Hospitals, CAPs, libraries, sport, civic |
| `es.cat.livestock` | DARP (REGA) | Holdings with species and head counts |
| `es.cat.reses` | Dept. Drets Socials | Care homes with places |
| `es.cat.munipoints` | ICGC | Municipal centroids, low-confidence fallback |

**Spain — national**

| id | Publisher | What it adds |
|---|---|---|
| `es.msan.hospitales` | Ministerio de Sanidad | **Staffed bed counts** |
| `es.msan.siap` | Ministerio de Sanidad | Primary and urgent care centres |
| `es.csic.carehomes` | CSIC Envejecimiento en Red | **Licensed places**, with coordinates |
| `es.meq.schools` | Ministerio de Educación | School directory (no enrolment) |
| `es.ine.popgrid` | Eurostat GISCO / INE | 1 km census population cells |

**Everywhere**

| id | Publisher | What it adds |
|---|---|---|
| `osm` | OpenStreetMap | Footprints, roads, power, rail, contacts |

Licences differ and every response carries them. `es.csic.carehomes` is the restrictive one —
its terms forbid commercial use, and it is flagged `commercial_use: false`. Filter on that
field if it matters to you.

---

## 18. Worked examples

### A first call

```bash
curl -X POST "$TALAIA/v1/exposure" \
  -H "X-API-Key: $TALAIA_KEY" -H 'content-type: application/json' \
  -d '{"aoi": {"type":"Polygon","coordinates":[[[1.80,41.71],[1.87,41.71],
                                                 [1.87,41.755],[1.80,41.755],[1.80,41.71]]]}}'
```

### A spread simulation, band by band

```python
import httpx

aoi = {"type": "FeatureCollection", "features": [
    {"type": "Feature",
     "properties": {"band": f"t+{h//60}h", "minutes": h},
     "geometry": perimeter}
    for h, perimeter in simulate(horizons_minutes=[60, 180, 360, 720])
]}

r = httpx.post(f"{TALAIA}/v1/exposure",
               headers={"X-API-Key": KEY},
               json={"aoi": aoi, "buffer_m": 250}, timeout=180).json()

for b in r["bands"]:
    print(f"{b['band']}: {b['asset_count']} assets, "
          f"{b['people_estimate']:.0f} at facilities, "
          f"{b['population_resident']:.0f} residents, "
          f"{b['critical_assets']} critical")

# who to call first — human-bearing and high-scoring
for a in r["assets"]:
    if a["human_bearing"] and a["exposure"]["priority_score"] > 70:
        print(a["exposure"]["band"], a["name"],
              a["capacity"].get("people"), a["capacity"].get("basis"),
              a["contacts"]["phone"])
```

### A polling loop

```python
# cheap: aggregates only, no asset array
s = httpx.post(f"{TALAIA}/v1/exposure/summary",
               headers={"X-API-Key": KEY},
               json={"aoi": current_perimeter, "live_osm": False}).json()

if s["summary"]["people_estimate"] > THRESHOLD:
    full = httpx.post(f"{TALAIA}/v1/exposure",
                      headers={"X-API-Key": KEY},
                      json={"aoi": current_perimeter,
                            "layers": ["healthcare", "social_care", "education"],
                            "max_assets": 200}).json()
```

### Splitting an AOI that exceeds your cap

```python
me = httpx.get(f"{TALAIA}/v1/me", headers={"X-API-Key": KEY}).json()
cap = me["limits"]["max_aoi_km2"]          # "unlimited" or a number

# The tile cache makes adjacent calls nearly free after the first, so tiling a
# large perimeter costs far less than the first call suggests.
for tile in split_polygon(perimeter, max_km2=cap):
    part = httpx.post(f"{TALAIA}/v1/exposure/summary",
                      headers={"X-API-Key": KEY}, json={"aoi": tile}).json()
```

Deduplicate by asset `id` when combining tiles — ids are stable, so an asset appearing in
two overlapping tiles is the same asset.

### Mapping population

```python
p = httpx.post(f"{TALAIA}/v1/population",
               headers={"X-API-Key": KEY},
               json={"aoi": perimeter, "include_geometry": True}).json()

for c in p["population"]["cells"][:10]:
    print(c["lon"], c["lat"], c["density_per_km2"], c["band"])
```
