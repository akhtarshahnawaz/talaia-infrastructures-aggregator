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

# Signup is email-verified and needs a sender. Set up in step 9 - listed here so the
# whole variable set is in one place. Without a sender, /v1/signup returns 503.
TALAIA_PUBLIC_URL=https://<your-app>.up.railway.app
TALAIA_RESEND_API_KEY=re_xxxxxxxxxxxxxxxx
# Required before anyone but you can sign up - see step 9.
# TALAIA_EMAIL_FROM=TALAIA <noreply@your-domain.example>

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

**Around an hour on an empty volume**, dominated by geocoding: ~4,000 care-home
addresses and ~51,000 school addresses through CartoCiudad, plus the 60 MB
population-grid download. Queries answered before it finishes return OpenStreetMap-only
results with a warning rather than failing — by design.

Ingestion runs **inside the API process on purpose**: DuckDB allows one writer, so a
separate ingest process would be locked out while the service holds the file.

### Redeploying during the bootstrap

Every deploy restarts the container, which stops the ingest wherever it had got to. That
is survivable — rows are written as each chunk of addresses resolves, and a source whose
run did not reach `ok` is picked up again on the next boot:

```
INFO  es.meq.schools: geocoded 12,000/51,216 addresses (11,704 rows so far)
...
WARNING bootstrapping 1 source(s) not fully loaded yet: es.meq.schools
```

Resuming is cheap because geocoding results are cached permanently on the volume, so the
second pass re-reads them rather than re-asking CartoCiudad. It is still wasted wall
time. **If you can, avoid pushing during the first hour of a fresh volume**, and check
`GET /v1/sources` for `last_status` before assuming a deploy is fully loaded.

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

## 9. Set up email — required before anyone can sign up

Signup is email-verified: `POST /v1/signup` mails a single-use link and creates nothing
until it is followed. With no sender configured it returns `503` and issues no key, which
is deliberate — falling back to unverified keys would undo the control while looking fine.

**→ Full guide: [EMAIL-SETUP.md](EMAIL-SETUP.md).** The short version:

### Resend — two variables, no server

HTTPS, so Railway's block on outbound port 25 never comes into it.

1. [resend.com](https://resend.com) → **API Keys** → **Create API Key** (sending access is
   enough). Copy the `re_…` value; it is shown once.
2. In Railway → **Variables**:

   ```bash
   TALAIA_RESEND_API_KEY=re_xxxxxxxxxxxxxxxxxxxx
   TALAIA_PUBLIC_URL=https://<your-app>.up.railway.app
   ```

`TALAIA_PUBLIC_URL` is the one people skip. It builds the link inside the email, and
behind Railway's proxy the request's own host is the internal one — a link built from that
reaches nobody.

### Then verify a domain, before you tell anyone about the service

Without `TALAIA_EMAIL_FROM`, mail is sent as Resend's shared `onboarding@resend.dev`, and
**Resend delivers that only to the address owning your Resend account**. Your own test
arrives, so it looks like it works, while every other signup fails silently.

Resend → **Domains** → **Add Domain**, add the `MX` and `TXT` records it shows at your DNS
provider (Cloudflare users: **DNS only**, grey cloud), click **Verify**, then set:

```bash
TALAIA_EMAIL_FROM=TALAIA <noreply@yourdomain.com>
```

### Or SMTP, if you already have a provider

```bash
TALAIA_SMTP_HOST=smtp.sendgrid.net
TALAIA_SMTP_PORT=587
TALAIA_SMTP_USER=apikey
TALAIA_SMTP_PASSWORD=<your key>
TALAIA_SMTP_STARTTLS=true
TALAIA_EMAIL_FROM=TALAIA <noreply@yourdomain.com>
TALAIA_PUBLIC_URL=https://<your-app>.up.railway.app
```

Port 587 with STARTTLS, or 465 with `TALAIA_SMTP_SSL=true`. **Port 25 is blocked on
Railway** — it will time out. If `TALAIA_RESEND_API_KEY` is also set, Resend wins; unset it
to use SMTP. Per-provider settings are in [EMAIL-SETUP.md](EMAIL-SETUP.md#3-option-b--smtp).

### Check it now, not later

```bash
curl -s $TALAIA/v1/admin/email -H "X-Admin-Key: $ADMIN"          # what is configured
curl -X POST $TALAIA/v1/admin/email/test -H "X-Admin-Key: $ADMIN" \
  -H 'content-type: application/json' -d '{"to":"you@example.com"}'
```

You want `"sent": true` and `"using_shared_sender": false`. A failure returns the
provider's own message, which is usually the whole diagnosis:

```jsonc
{ "sent": false, "error": "RuntimeError: Resend returned 403: The yourdomain.com domain
                           is not verified. Please verify your domain on
                           https://resend.com/domains" }
```

The boot log also states the active backend, and warns when signup is open while
verification is off or unsendable.

> **Running a closed deployment?** Set `TALAIA_ALLOW_SIGNUP=false` and skip this step
> entirely — mint keys with the admin API instead. Do not use
> `TALAIA_REQUIRE_EMAIL_VERIFICATION=false` to dodge it on a public URL: that hands a key
> to anyone who types any address.

---

## 10. Verify the deployment

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

## 11. The admin panel

With `TALAIA_ADMIN_KEY` set, open **`https://<your-app>.up.railway.app/admin`** and sign
in with that value. Keys, signups, usage, tiers, cache warming and mail diagnostics, all
from the browser — see [LIMITS-AND-KEYS.md](LIMITS-AND-KEYS.md#1b-the-admin-panel).

The page is not linked from the site navigation, and the admin routes return `404` rather
than `401` when no admin key is configured, so a deployment that does not use it does not
advertise it. The key lives in `sessionStorage` and is gone when the tab closes.

---

## 12. Custom domain (optional)

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

### Mail, after the first setup

Configured in [step 9](#9-set-up-email--required-before-anyone-can-sign-up) and covered in
full by [EMAIL-SETUP.md](EMAIL-SETUP.md). Worth re-running after any change to the sender,
the domain's DNS, or the provider account:

```bash
curl -s $TALAIA/v1/admin/email -H "X-Admin-Key: $ADMIN"
curl -X POST $TALAIA/v1/admin/email/test -H "X-Admin-Key: $ADMIN" \
  -H 'content-type: application/json' -d '{"to":"you@example.com"}'
```

A provider key can be revoked, and a domain's DNS can be edited by someone who does not
know it is load-bearing. Neither shows up anywhere until a signup fails, so this is worth
checking whenever signups go quiet.

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
| Build fails: `docker VOLUME at Line N is not supported, use Railway Volumes` | A `VOLUME` instruction in the Dockerfile | Delete it. Railway rejects `VOLUME`; persistence comes from the volume you mount in step 3 |
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

### Email

| Symptom | Cause | Fix |
|---|---|---|
| `503` on `/v1/signup` | No sender configured | Step 9. A misspelled variable is silently ignored. |
| Your test arrives, nobody else's does | Resend's shared sender only reaches the account owner | Verify a domain, set `TALAIA_EMAIL_FROM` |
| `403 … domain is not verified` | `TALAIA_EMAIL_FROM` is on an unverified domain | Verify it, or clear the variable |
| SMTP times out | Port 25, which Railway blocks | 587 + STARTTLS, or 465 + `TALAIA_SMTP_SSL=true` |
| Link in the email 404s | `TALAIA_PUBLIC_URL` wrong or unset | Public URL, no trailing slash |

More in [EMAIL-SETUP.md](EMAIL-SETUP.md#6-troubleshooting).

---

## Security checklist before you share the URL

- [ ] `TALAIA_REQUIRE_AUTH=true`
- [ ] `TALAIA_ADMIN_KEY` set to a long random value, stored in a password manager
- [ ] Bootstrap key captured, or deliberately discarded after minting your own
- [ ] Your unlimited key is in your project's env, never in client-side code
- [ ] `TALAIA_ALLOW_SIGNUP` reflects what you actually want
- [ ] If signup is open: a mail sender is configured and `POST /v1/admin/email/test` returns `sent: true`
- [ ] `using_shared_sender` is `false` — otherwise only you can ever register
- [ ] `TALAIA_REQUIRE_EMAIL_VERIFICATION` is `true` on any public URL
- [ ] `TALAIA_EMAIL_CONSOLE` is **not** set — it returns a working verification link to every caller
- [ ] `TALAIA_CORS_ORIGINS` narrowed if a browser app will call this
- [ ] Volume attached at `/data` — keys and data both live there
- [ ] Verified a keyless request returns 401
- [ ] You know `/admin` exists and that anyone with the admin key has full control there
