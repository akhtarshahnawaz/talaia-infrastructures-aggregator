# Pre-caching Spain

## What is already local, and what is not

TALAIA has three data tiers, and only one of them is slow.

| Tier | Source | Where it lives | Cold cost |
|---|---|---|---|
| **A — resident** | Catalan registries, INE population grid | Bulk-loaded into the store at boot | none, always local |
| **B — on demand** | OpenStreetMap via Overpass | Tile cache in the same store | **20–300 s per cold block** |
| **C — enrichment** | CartoCiudad geocoding | Cached permanently | one lookup, then free |

So "pre-cache Spain" means one thing in practice: **warm the Tier B OpenStreetMap tile
cache**. Tiers A and C are already local after the first boot.

Warm tiles live in the store, so they survive redeploys.

---

## The honest arithmetic

Measured on this deployment, not estimated:

| What | Result |
|---|---|
| 9 tiles, central Barcelona (one dense block) | **275 s**, 121,151 assets, 25,111 networks, +54 MB |
| 69 tiles, rest of Barcelona metro | **675 s**, 103,414 assets, 67,396 networks, +82 MB |
| Barcelona metro total, 78 of 88 tiles warm | ~16 min, **224 MB** store including registries |
| Per tile, dense urban | ~13,500 assets |
| Per tile, metro average | ~1,500 assets, ~1.2 MB |

10 of the 88 tiles failed on the first pass — Overpass refused after sustained fetching.
The run reported `partial`, not `done`, and re-running it planned **88 tiles (78 already
fresh), 1 block**: only the failures were retried. Expect this. Budget two passes.

Tiles are 0.05° ≈ 4 km. Now scale that up:

| Region | Tiles | Blocks | Realistic? |
|---|---:|---:|---|
| `barcelona` (metro) | 88 | 6 | **yes** — ~16 min, measured |
| `demo` (BCN + Girona + Empordà + Tarragona) | 364 | 32 | **yes** — a few hours |
| `prov_girona` | 532 | 35 | yes, overnight |
| `catalunya` | 3,168 | 204 | overnight, ~1–3 GB |
| `spain_mainland` | 41,377 | 2,665 | **no** |

**Do not try to pre-cache all of Spain.** 41,377 tiles at the densities above is days of
Overpass time and tens of gigabytes, against a public service that will rate-limit you
long before you finish — warming 88 tiles was already enough to get 10 of them refused.
It is also unnecessary: nobody demos 41,000 tiles.

**Recommendation for a demo:** warm `demo`, or the two or three specific areas you will
actually show, and let everything else fall back to on-demand fetching. A cold area still
works — it just pays an Overpass round-trip on the first request, and degrades to
registry-only data with a warning if Overpass is down.

If you genuinely need national coverage, the tile cache is the wrong tool; take a
[Geofabrik](https://download.geofabrik.de/europe/spain.html) `spain-latest.osm.pbf`
extract and bulk-load it offline. That is not built here.

---

## Warming

### Offline, with the service stopped (fastest)

```bash
python -m talaia regions                      # what is available, with tile counts
python -m talaia warm barcelona --dry-run     # cost before committing
python -m talaia warm demo
python -m talaia warm 1.9,41.3,2.3,41.6       # or a raw bbox
```

Options: `--concurrency`, `--pause` (seconds between requests), `--max-tiles` (refuse to
start above a size), `--force` (refetch fresh tiles).

**It resumes.** The tile cache *is* the progress record, so a warm is just "fetch the
tiles that are stale". Interrupt it, re-run the same command, and it picks up — the run
above reported `88 tiles (9 already fresh)` because 9 were warmed earlier.

### Online, against a running deployment

On the DuckDB backend the API holds the single writer, so a CLI run is simply locked
out. On Postgres a CLI warm against a live service works — but the admin endpoint is
still the better tool, because it reports progress and can be stopped.
The admin endpoint runs the warm **inside** the serving process instead:

```bash
curl -X POST https://<your-app>/v1/admin/warm -H "X-Admin-Key: $ADMIN" \
  -H 'content-type: application/json' -d '{"regions":["demo"],"dry_run":true}'

curl -X POST https://<your-app>/v1/admin/warm -H "X-Admin-Key: $ADMIN" \
  -H 'content-type: application/json' -d '{"regions":["demo"],"max_tiles":500}'

curl -s https://<your-app>/v1/admin/warm  -H "X-Admin-Key: $ADMIN"   # progress
curl -X DELETE https://<your-app>/v1/admin/warm -H "X-Admin-Key: $ADMIN"  # stop
```

The API keeps serving throughout — verified: `/health` and `/v1/exposure` answered
normally for the whole of a warm run.

### On boot

```bash
TALAIA_WARM_ON_BOOT=demo
```

Runs in the background after the service is already answering, because a cold region
degrades latency but does not break anything.

### Pacing

| Variable | Default | Notes |
|---|---|---|
| `TALAIA_WARM_MAX_PARALLEL` | 2 | Overpass is donated infrastructure |
| `TALAIA_WARM_PAUSE_S` | 1.0 | Between requests |
| `TALAIA_WARM_GROUP_TILES` | 16 | Tiles per Overpass request (4×4 block) |
| `TALAIA_OSM_TILE_TTL_HOURS` | 336 (14 d) | How long a tile stays fresh |
| `TALAIA_OSM_ERROR_RETRY_MINUTES` | 30 | How long a failed tile is left alone by user requests |

The last one matters more than it looks. A tile that cannot be fetched is not "fresh", so
without a backoff every request covering it retries and pays the full 25-second deadline
— for ever. A partially warmed region would be *slower* than a cold one. With the backoff,
a request over an area containing permanently failing tiles went from **27 s to 2 ms**.
Bulk warming ignores the backoff, because retrying is precisely its job.

Deliberately gentler than the per-request path, which has a 25 s deadline because someone
is waiting on it. A bulk warm has no deadline and no reason to hurry.

---

## Checking coverage

```bash
curl -s https://<your-app>/v1/coverage | jq '.regions[:5]'
```

```
barcelona          10.2%   9/88 tiles
prov_barcelona      2.0%  15/744 tiles
catalunya           0.5%  15/3,168 tiles
```

Also available as the `talaia_coverage` MCP tool. Percentages are tiles **actually
warm** — a run in which every fetch failed reports `failed` and `0%`, not `done`.

---

## What warming does and does not fix

**Does:** removes the Overpass round-trip. Measured on a 28 km² area with `live_osm=true`:

```
warm area, Barcelona     2.1 s total    osm step 4 ms     5 tiles cached, 0 fetched
area with failed tiles    2.8 s total    osm step 2 ms     4 tiles cached, 0 fetched
same area before the backoff fix        osm step 25,000 ms
```

**Does not:** make a huge query cheap. Measured over 183k rows, a 100 km² area of central
Barcelona matches ~96,000 assets and takes ~0.9 s regardless of cache state, because the
cost is materialising the rows, not finding them. Pre-caching moves latency from
*seconds of network* to *hundreds of milliseconds of local work* — it does not remove it.

For a responsive demo, prefer `/v1/exposure/summary` (no asset array), narrow `layers`,
or a smaller AOI. Fire perimeters are usually small; it is the buffered 20 km variants
that get expensive.

One measured consequence worth knowing: pre-caching widens the density spread between
city and countryside by two orders of magnitude. The query planner now takes an exact
candidate count per query (2–5 ms) rather than estimating from a global average, because
after warming a city no single average describes both.
