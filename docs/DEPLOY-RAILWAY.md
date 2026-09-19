# Deploying TALAIA to Railway

End state: one Railway service serving the API **and** the documentation website on a
single HTTPS domain, with a persistent volume holding the DuckDB store, self-service
signup enabled for the public, and an unlimited key for your own integration.

Expect **15–20 minutes**, most of it waiting for the first build and data load.

> Railway's UI labels move around occasionally. The concepts — service, variables,
> volume, domain — are stable even when the buttons move.

---

## 0. Before you start

| Need | Why |
|---|---|
| A GitHub repo containing this project | Railway builds from a connected repo |
| A Railway account | <https://railway.app> |
| ~1 GB of volume | The store lands around 150 MB, plus a 60 MB cached download |
| A service plan with ≥1 GB RAM | DuckDB is configured for a 1 GB memory limit |

Two decisions to make now, because they shape the variables you set in step 3:

1. **Do you want the public to self-register?** Yes → leave signup on. No → set
   `TALAIA_ALLOW_SIGNUP=false` and hand out keys yourself.
2. **Pick an admin secret.** You will need it to mint your own unlimited key. Generate
   one and keep it somewhere safe:

   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

---

## 1. Push the repository to GitHub

```bash
cd infrastructures-service
git remote add origin git@github.com:<you>/talaia.git
git push -u origin main
```

Confirm these are present at the repo root — Railway needs them:

```
Dockerfile        multi-stage build: Rust core → website → runtime
railway.json      builder, start command, health check
.dockerignore     keeps the local data/ and .venv out of the image
```

`data/` is git-ignored by design. **Do not commit your local `talaia.duckdb`** — the
service rebuilds it on the volume at first boot.

---

## 2. Create the service

1. Railway dashboard → **New Project** → **Deploy from GitHub repo**.
2. Pick the repo. Railway detects `Dockerfile` and `railway.json` and starts a build.
3. **Cancel or ignore this first build.** It will deploy without a volume and without
   variables. Set those up first (steps 3 and 4), then redeploy — otherwise the first
   boot loads ~60 MB of data onto ephemeral disk and throws it away.

---

## 3. Add the volume — do this before the real first boot

Service → **Variables / Settings → Volumes** → **New Volume**.

| Setting | Value |
|---|---|
| Mount path | `/data` |
| Size | 1 GB is comfortable |

This is the single most important step. Without it every redeploy starts from an empty
store and re-ingests everything — including re-downloading the 60 MB population grid —
and **every API key you or your users created is lost**, because keys live in the same
DuckDB file.

---

## 4. Set environment variables

Service → **Variables**. `PORT` is injected by Railway; do not set it.

### Required

```bash
TALAIA_DATA_DIR=/data
TALAIA_REQUIRE_AUTH=true
TALAIA_ADMIN_KEY=<the secret you generated in step 0>
```

### Recommended

```bash
# Self-service signup for the public (free tier: 250 km² per call, 60 req/min, 1000/day)
TALAIA_ALLOW_SIGNUP=true
TALAIA_SIGNUPS_PER_IP_PER_DAY=3

# Overpass is a volunteer service; more than one mirror is not optional.
TALAIA_OVERPASS_MIRRORS=https://overpass-api.de/api/interpreter,https://overpass.kumi.systems/api/interpreter,https://overpass.private.coffee/api/interpreter

# Hard wall-clock budget for a cold OpenStreetMap fetch.
TALAIA_OSM_DEADLINE_S=25

# Keep under the container's memory limit.
TALAIA_DUCKDB_MEMORY_LIMIT=1GB
TALAIA_DUCKDB_THREADS=4

# Pre-load the OpenStreetMap tile cache for your demo areas in the background at boot,
# so the first query does not pay an Overpass round-trip. Warm what you will show:
# `demo` is 364 tiles and takes a few hours; all of Spain is 41,377 and is not worth
# attempting. See docs/PRECACHING.md.
TALAIA_WARM_ON_BOOT=demo
```

### Optional

```bash
# Extra always-valid keys, "label:key" or bare "key", comma separated.
# Useful for CI. Keys minted through the admin API do not need to appear here.
TALAIA_API_KEYS=ci:talaia_sk_xxxxxxxxxxxxxxxx

# Raises the anonymous rate limit on the Catalan open-data portal.
TALAIA_SOCRATA_APP_TOKEN=<token>

# Lock down the metadata endpoints too (breaks the public docs site's live data).
TALAIA_PUBLIC_METADATA=false

# Restrict browser origins that may call the API.
TALAIA_CORS_ORIGINS=https://your-frontend.example

# Retune tier quotas without redeploying code. Partial objects patch the built-in tier.
# Malformed JSON is ignored and logged, so a typo cannot silently remove a cap.
TALAIA_TIER_LIMITS={"free":{"max_aoi_km2":500,"daily_quota":5000}}
```

---

## 5. Deploy and watch the build

Trigger a redeploy (**Deployments → Redeploy**, or push a commit).

The build runs three stages and takes **4–8 minutes cold**:

| Stage | What happens | Roughly |
|---|---|---|
| `rust-builder` | Compiles the numeric core to an abi3 wheel | 2–4 min |
| `web-builder` | `npm ci` + Vite build of the website | 1–2 min |
| `runtime` | Python deps, DuckDB extensions baked in, files copied | 1–2 min |

**If the Rust stage fails the build still succeeds.** That stage is deliberately
fault-tolerant: the app falls back to a NumPy implementation of the same contract. You
will see `No Rust wheel; using the NumPy fallback` in the build log, and
`GET /v1/stats` will report `"core_impl": "python"`. It is slower, not broken.

---

## 6. Capture the bootstrap API key — it is shown once

With `TALAIA_REQUIRE_AUTH=true` and no keys configured, the service mints one on first
boot and prints it **once**. Open **Deployments → View Logs** and look for:

```
  ============================================================
   No API keys were configured, so one has been generated.
   This is shown ONCE and cannot be recovered:

     talaia_sk_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

   Store it now. ...
  ============================================================
```

Copy it now. Only its hash is stored, so it cannot be recovered — if you miss it, delete
the volume and redeploy, or use `TALAIA_ADMIN_KEY` to mint a replacement (step 8).

---

## 7. Wait for the data bootstrap

The service **starts serving immediately** and loads data in the background, so the
health check passes while ingestion is still running. Watch the logs:

```
WARNING talaia   store is empty - bootstrapping resident sources in the background
INFO    talaia   bootstrap: es.cat.schools -> 5,434 rows
INFO    talaia   bootstrap: es.cat.equipaments -> 24,545 rows
INFO    talaia   bootstrap: es.cat.livestock -> 19,889 rows
INFO    talaia   bootstrap: es.cat.reses -> 3,889 rows
INFO    talaia   bootstrap: es.ine.popgrid -> 63,522 rows
INFO    talaia   bootstrap complete: {...}
```

**5–10 minutes**, dominated by geocoding ~4,000 care-home addresses through CartoCiudad
and the 60 MB population-grid download. Queries answered before it finishes return
OpenStreetMap-only results with a warning rather than failing — by design.

Ingestion runs **inside the API process on purpose**: DuckDB allows one writer, so a
separate ingest process would be locked out while the service holds the file.

---

## 8. Generate a domain and mint your unlimited key

Service → **Settings → Networking → Generate Domain**. You get
`https://<name>.up.railway.app`.

Now mint the key for your own integration — no area cap, no rate limit, no daily quota:

```bash
export TALAIA=https://<name>.up.railway.app
export ADMIN=<your TALAIA_ADMIN_KEY>

curl -X POST "$TALAIA/v1/admin/keys" \
  -H "X-Admin-Key: $ADMIN" -H 'content-type: application/json' \
  -d '{"label":"deepfire-integration","tier":"unlimited"}'
```

```jsonc
{
  "api_key": "talaia_sk_…",          // shown once
  "prefix": "talaia_sk_BPbNXD...",
  "tier": "unlimited",
  "limits": { "max_aoi_km2": "unlimited", "rate_limit_per_min": "unlimited", … }
}
```

Put that key in your own project's environment. Anything else — the public, demos,
partners — goes through `/v1/signup` and gets the capped free tier.

Other management calls:

```bash
curl "$TALAIA/v1/admin/keys" -H "X-Admin-Key: $ADMIN"                   # prefixes only
curl -X DELETE "$TALAIA/v1/admin/keys/talaia_sk_XK7kHK..." -H "X-Admin-Key: $ADMIN"
```

---

## 9. Verify the deployment

```bash
export KEY=<your unlimited key>

curl "$TALAIA/health"
# {"status":"ok","assets":61943,"cached_tiles":0}

curl "$TALAIA/v1/stats" | jq
# check: assets > 60000, population_cells > 60000, core_impl, auth_required true

curl "$TALAIA/v1/me" -H "X-API-Key: $KEY" | jq
# check: tier "unlimited", all limits "unlimited"

# the gate is actually closed
curl -s -o /dev/null -w '%{http_code}\n' -X POST "$TALAIA/v1/exposure" \
  -H 'content-type: application/json' -d '{"aoi":{"type":"Point","coordinates":[1.8,41.7]}}'
# 401

# a real query
curl -X POST "$TALAIA/v1/exposure" \
  -H "X-API-Key: $KEY" -H 'content-type: application/json' \
  -d '{"aoi":{"type":"Polygon","coordinates":[[[1.80,41.71],[1.87,41.71],
        [1.87,41.755],[1.80,41.755],[1.80,41.71]]]},"live_osm":false}' \
  | jq '.summary'
```

Then open `$TALAIA` in a browser: the landing page should show live counts, `/signup`
should issue a key, and `/playground` should run a query once you paste one in.

---

## 10. Custom domain (optional)

Settings → **Networking → Custom Domain** → enter `talaia.yourdomain.org`, then add the
`CNAME` Railway shows you at your DNS provider. TLS is issued automatically. If you set
`TALAIA_CORS_ORIGINS`, add the new origin there too.

---

## Operations

### Redeploying

Push to the connected branch. The volume survives, so data and API keys persist and the
bootstrap does not re-run.

### Refreshing the registry data

The resident tier is a snapshot. To refresh it, add a Railway **Cron** service running
the same image with:

```
python -m talaia ingest
```

⚠️ **Only when the API service is stopped.** DuckDB permits a single writer, and a cron
container writing to the same volume while the API holds it will fail with a lock error.
For a periodic refresh, the safer pattern is a scheduled redeploy against an emptied
store, or adding an authenticated admin re-ingest endpoint that runs in-process.

OpenStreetMap needs no maintenance: tiles refresh themselves when their 14-day TTL
expires.

### Warming the tile cache on a live deployment

The same single-writer constraint rules out a cron warm, so the admin endpoint runs it
**inside** the serving process. The API keeps answering throughout.

```bash
# What it would cost, before committing to it
curl -X POST $TALAIA/v1/admin/warm -H "X-Admin-Key: $ADMIN" \
  -H 'content-type: application/json' -d '{"regions":["demo"],"dry_run":true}'

# Start it, with a guard against an accidentally huge region
curl -X POST $TALAIA/v1/admin/warm -H "X-Admin-Key: $ADMIN" \
  -H 'content-type: application/json' -d '{"regions":["demo"],"max_tiles":500}'

curl -s $TALAIA/v1/admin/warm -H "X-Admin-Key: $ADMIN"        # progress and ETA
curl -X DELETE $TALAIA/v1/admin/warm -H "X-Admin-Key: $ADMIN" # stop
curl -s $TALAIA/v1/coverage                                    # what is warm
```

Warms resume: re-running skips tiles that are still fresh. A run reports `partial` or
`failed` rather than `done` if tiles did not land, and failed tiles stay stale so the
next run retries them. Expect Overpass to start refusing after sustained bulk fetching —
pace with `TALAIA_WARM_PAUSE_S` and come back later rather than pushing through.

### Backups

```bash
railway run cp /data/talaia.duckdb /data/talaia-$(date +%F).duckdb
```

Railway volumes are not snapshotted for you. **The api_keys table lives in this file** —
losing it revokes every key in existence.

### Costs and sizing

| Resource | Typical |
|---|---|
| Image | ~700 MB |
| Volume | ~150 MB store + 60 MB cache |
| Memory | 400–900 MB under load (DuckDB capped at 1 GB) |
| CPU | Bursty; a 17 km AOI is ~2 s of mostly single-core work |

Scale vertically before horizontally: **the DuckDB file cannot be shared between
replicas.** Two instances on one volume will fight over the write lock. For horizontal
scale, implement the PostGIS backend behind the existing `Store` interface.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Could not set lock on file` | Two processes on one volume | Run a single replica; stop the API before any CLI write |
| `/v1/stats` shows `"core_impl": "python"` | Rust stage failed | Check the build log; it is a performance loss, not an outage |
| Everything returns 401 | No key, or it was lost with the volume | Mint one with `TALAIA_ADMIN_KEY`, or redeploy for a new bootstrap key |
| `/v1/admin/keys` returns 404 | `TALAIA_ADMIN_KEY` not set | Set it and redeploy — the surface hides itself when unconfigured |
| Assets = 0 long after boot | Bootstrap failed | Grep the logs for `bootstrap:`; usually an upstream outage. Redeploy retries |
| Responses warn about OpenStreetMap | Overpass mirrors unreachable | Expected degradation. Confirm `TALAIA_OVERPASS_MIRRORS` lists several |
| First query of the day is slow | Cold R-tree page-in | Warmed at boot; a cold *OSM tile* is the other cause and is capped at 25 s |
| Signup returns 409 | One active key per email | Revoke the old key first |
| Health check fails during deploy | Build finished but app crashed | Check logs; `healthcheckTimeout` is already 300 s in `railway.json` |
| Users report 403 on big areas | Working as intended | Free tier is 250 km². Issue a `standard` or `unlimited` key |

---

## Security checklist before you share the URL

- [ ] `TALAIA_REQUIRE_AUTH=true`
- [ ] `TALAIA_ADMIN_KEY` set to a long random value, stored in a password manager
- [ ] Bootstrap key captured, or deliberately discarded after minting your own
- [ ] Your unlimited key is in your project's env, never in client-side code
- [ ] `TALAIA_ALLOW_SIGNUP` reflects what you actually want
- [ ] `TALAIA_CORS_ORIGINS` narrowed if a browser app will call this
- [ ] Volume attached at `/data` — keys and data both live there
- [ ] Verified a keyless request returns 401
