# Controlling access: limits, tiers and keys

Everything in this document is operator-facing. Nothing here is reachable without either
`TALAIA_ADMIN_KEY` or shell access to the deployment.

---

## 1. Where limits come from

Four layers, checked in this order. Each is narrower than the one before it.

| Layer | Set by | Applies to | Change takes effect |
|---|---|---|---|
| **Service ceiling** | `TALAIA_MAX_AOI_KM2`, `TALAIA_MAX_ASSETS_RETURNED` | Everyone, including unlimited keys | Restart |
| **Tier** | `TALAIA_TIER_LIMITS`, else the built-in table | Every key on that tier | Restart |
| **Per-key override** | `POST`/`PATCH /v1/admin/keys`, or `talaia key` | One key | Immediately |
| **Per-request** | `max_assets`, `layers` in the request body | That call | Immediately |

The service ceiling is the one people forget. `TALAIA_MAX_AOI_KM2` defaults to 25,000 km²
and is **not** waived by the unlimited tier — an unlimited key asking for 30,000 km² gets
a 422, not a report. That is deliberate: the unlimited tier exists to remove *quota*
limits for your own integration, not to remove the guard that stops one request from
trying to build a report for half of Spain. Raise the ceiling if you genuinely need to.

### Built-in tiers

| Tier | Area / call | Rate | Daily | Assets / call | How to get one |
|---|---|---|---|---|---|
| `free` | 250 km² | 60/min | 1,000 | 2,000 | `POST /v1/signup` |
| `standard` | 2,500 km² | 300/min | 20,000 | 20,000 | Admin issues it |
| `unlimited` | — | — | — | 50,000 | Admin issues it |

`0` always means unlimited. Live values are published at `GET /v1/tiers`, and a caller
sees their own at `GET /v1/me`.

### Retuning a tier without a code change

```bash
TALAIA_TIER_LIMITS='{"free":{"max_aoi_km2":500,"daily_quota":5000}}'
```

Partial objects patch the built-in tier; unnamed fields keep their defaults. An unknown
tier name defines a new tier on top of the free-tier defaults:

```bash
TALAIA_TIER_LIMITS='{"partner":{"max_aoi_km2":10000,"rate_limit_per_min":600,"daily_quota":0}}'
```

Fields: `rate_limit_per_min`, `daily_quota`, `max_aoi_km2`, `max_assets`, `live_osm`,
`description`.

**Malformed JSON fails closed.** A typo logs an error and keeps the built-in limits, so a
bad edit cannot accidentally remove a cap. Check the boot log for
`tier limits overridden from the environment` to confirm it was read.

**Retuning a tier moves keys already issued on it.** Keys store their tier *name* and
resolve limits at load time, so raising the free cap lifts every existing free key on the
next restart — not only newly issued ones. The exception is a key whose limits an admin
set by hand: those are pinned and survive a tier change. That is what `custom_limits` in
the key listing means.

---

## 2. Creating an unlimited key

Three ways. Pick by whether the service is running.

### a. Environment — simplest, for your own integration

```bash
TALAIA_API_KEYS="deepfire:talaia_sk_your_own_long_random_string"
```

Comma-separated, each entry `label:key` or bare `key`. **Keys defined this way are
unlimited by design** — no area cap, no daily quota — because they exist so the
deployment owner can integrate without rate-limiting themselves. They are not stored in
the database and cannot be revoked at runtime; remove the variable and redeploy.

`TALAIA_RATE_LIMIT_PER_MIN` still applies to them (default 120). Set it to `0` for no
rate limit at all.

Generate a key with the same entropy the service uses:

```bash
python -c "import secrets;print('talaia_sk_'+secrets.token_urlsafe(32))"
```

### b. Admin API — while the service is running

```bash
curl -X POST https://<your-app>/v1/admin/keys \
  -H "X-Admin-Key: $TALAIA_ADMIN_KEY" \
  -H "content-type: application/json" \
  -d '{"label":"deepfire-integration","tier":"unlimited","email":"you@example.com"}'
```

The secret is in the response and **is never shown again** — only its SHA-256 hash is
stored. Optional `rate_limit_per_min`, `max_aoi_km2`, `daily_quota` override the tier for
that key alone and pin it against future tier changes.

### c. CLI — while the service is stopped

DuckDB takes one writer, and the API holds it. Stop the service first.

```bash
python -m talaia key create --tier unlimited --label deepfire-integration
python -m talaia key list
python -m talaia key revoke talaia_sk_abc123...
```

### Upgrading an existing key

Someone signed up on the free tier and needs more. Change the tier in place — the secret
does not change, so they do not have to redeploy anything:

```bash
curl -X PATCH https://<your-app>/v1/admin/keys/talaia_sk_abc123... \
  -H "X-Admin-Key: $TALAIA_ADMIN_KEY" \
  -H "content-type: application/json" \
  -d '{"tier":"standard"}'
```

Or offline: `python -m talaia key update talaia_sk_abc123... --tier standard`

---

## 3. Signup controls

| Variable | Default | Effect |
|---|---|---|
| `TALAIA_ALLOW_SIGNUP` | `true` | `false` makes the service invite-only; `/v1/signup` returns 403 |
| `TALAIA_SIGNUPS_PER_IP_PER_DAY` | `3` | Per-address cap on new keys |
| `TALAIA_SIGNUP_TIER` | `free` | Tier handed to self-service signups |
| `TALAIA_REQUIRE_EMAIL_VERIFICATION` | `true` | No key until a mailed link is followed |
| `TALAIA_VERIFICATION_TTL_HOURS` | `24` | How long a confirmation link lives |
| `TALAIA_PUBLIC_URL` | — | Base URL for the link. **Set this on Railway** — the request's own host is the internal one. |
| `TALAIA_SMTP_HOST` … | — | Mail sender. Without one, signup returns 503. |

**Verification is required by default.** `POST /v1/signup` mails a single-use link and
creates nothing; the key is issued and shown once when the link is followed, and is never
emailed. With verification on and no mail sender configured, signup fails closed with a
503 — falling back to unverified issuance would undo the control while looking fine.

Keys minted by an admin, by the CLI, or from `TALAIA_API_KEYS` are not marked verified,
because nobody confirmed an address for them. `GET /v1/admin/keys` shows `email_verified`
per key.

Also enforced: one active key per email address. Behind Railway's proxy the client
address is taken from the first `X-Forwarded-For` entry.

---

## 4. What each limit actually protects

Worth being explicit, because it drives where to spend a limit budget.

**The area cap is the load-bearing control.** Rate limits and daily quotas only slow an
abuser down. A single unbounded polygon is *one* request that can match millions of rows,
request hundreds of Overpass tiles and hold the process for minutes — it would pass a
60/minute rate limit without noticing. So area is checked before the store is touched, on
`aoi` **plus `buffer_m`**: a 20 km buffer on a small polygon is a large query, and
checking the unbuffered shape would be an obvious way through.

`max_assets` only ever clamps down. A free key asking for 20,000 gets 2,000; a free key
asking for 10 still gets 10.

Quotas are counted in memory and flushed to disk every 60 seconds, because a database
write per request would funnel the whole service through DuckDB's single writer. They are
restored on boot, so a restart does not hand everyone a fresh allowance. The cost of that
design is that up to 60 seconds of counts can be lost in a hard crash.

---

## 5. Reading the current state

```bash
curl -s https://<your-app>/v1/tiers                                   # public
curl -s https://<your-app>/v1/me -H "X-API-Key: $KEY"                 # one key
curl -s https://<your-app>/v1/admin/keys -H "X-Admin-Key: $ADMIN"     # all keys
```

The admin listing shows prefixes, tiers, the limits actually in force, `custom_limits`,
lifetime request counts and today's usage. **It never returns secrets** — they are not
stored in a recoverable form, so a leaked database does not leak usable credentials.

If `TALAIA_ADMIN_KEY` is unset, every `/v1/admin/*` route returns 404 rather than 401:
there is no admin surface to probe.
