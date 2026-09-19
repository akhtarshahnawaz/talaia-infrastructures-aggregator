# Setting up email

TALAIA sends exactly one kind of message: a signup confirmation link. Without a working
sender, `POST /v1/signup` returns `503` and issues nothing — it fails closed rather than
handing out keys to unverified addresses.

Two ways to configure it. **Resend takes one variable and no server.** SMTP works with any
provider if you already have one.

---

## Contents

1. [What actually gets sent](#1-what-actually-gets-sent)
2. [Option A — Resend (recommended)](#2-option-a--resend-recommended)
3. [Option B — SMTP](#3-option-b--smtp)
4. [Every variable](#4-every-variable)
5. [Testing it](#5-testing-it)
6. [Troubleshooting](#6-troubleshooting)
7. [Local development](#7-local-development)

---

## 1. What actually gets sent

One message per signup, containing a single-use link:

```
Subject: Confirm your email for TALAIA API access

Someone asked for a TALAIA API key using this address.
Confirm it by opening this link:

  https://your-app.up.railway.app/verify?token=…

The link works once and expires in 24 hours.
Your API key is shown on that page, not in this email.
```

**The API key is never emailed.** Email is not a confidential channel and a credential
sent there sits in an inbox indefinitely, so the link leads to a page that shows the key
once. That also means your sender only ever carries low-risk content — useful when
choosing how much to invest in deliverability.

Volume is one message per new user. Every provider's free tier is far more than enough.

---

## 2. Option A — Resend (recommended)

HTTPS, so there is no SMTP port to unblock and nothing to run. Railway blocks outbound
port 25; this sidesteps the question entirely.

### Step 1 — Get an API key

1. Sign up at [resend.com](https://resend.com).
2. Go to **API Keys** → **Create API Key**.
3. Permission **Sending access** is enough. Copy the key — it starts `re_` and is shown
   once.

### Step 2 — Set two variables

```bash
TALAIA_RESEND_API_KEY=re_xxxxxxxxxxxxxxxxxxxx
TALAIA_PUBLIC_URL=https://your-app.up.railway.app
```

`TALAIA_PUBLIC_URL` builds the link in the email. Behind a proxy the request's own host is
the internal one, so a link built from it reaches nobody. Set it explicitly.

That is enough to start. **But read the next step before letting anyone else sign up.**

### Step 3 — Verify a sending domain

With no `TALAIA_EMAIL_FROM`, mail goes out as Resend's shared `onboarding@resend.dev`.
That needs no DNS at all, and **Resend will only deliver it to the address that owns your
Resend account.**

This is the trap worth naming: your own test arrives, so everything looks correct, while
every other signup silently fails. TALAIA warns about this in three places — the boot log,
`GET /v1/admin/email` (`using_shared_sender: true`), and the test endpoint — but it cannot
fix it for you.

To send to anyone:

1. Resend → **Domains** → **Add Domain**, enter a domain you control.
2. Resend shows DNS records to add. Typically:

   | Type | Name | Purpose |
   |---|---|---|
   | `MX` | `send.yourdomain.com` | Receives bounces |
   | `TXT` | `send.yourdomain.com` | SPF — authorises Resend to send as you |
   | `TXT` | `resend._domainkey` | DKIM — signs your mail |
   | `TXT` | `_dmarc` | DMARC policy (recommended, sometimes optional) |

   Add them at whatever hosts your DNS — Cloudflare, Namecheap, Route 53. If you use
   Cloudflare, set these records to **DNS only** (grey cloud), not proxied.
3. Click **Verify**. Propagation is usually minutes; it can take up to a few hours.
4. Once verified, set:

   ```bash
   TALAIA_EMAIL_FROM=TALAIA <noreply@yourdomain.com>
   ```

   The address must be on the verified domain. The display name is free text.

### Step 4 — Confirm

```bash
curl -s $TALAIA/v1/admin/email -H "X-Admin-Key: $ADMIN"
```

You want `"using_shared_sender": false`. Then send a real test — see
[§5](#5-testing-it).

---

## 3. Option B — SMTP

Any provider. Uses the Python standard library in a worker thread, so it adds no
dependency.

```bash
TALAIA_SMTP_HOST=smtp.example.com
TALAIA_SMTP_PORT=587
TALAIA_SMTP_USER=your-username
TALAIA_SMTP_PASSWORD=your-password
TALAIA_SMTP_STARTTLS=true
TALAIA_EMAIL_FROM=TALAIA <noreply@yourdomain.com>
TALAIA_PUBLIC_URL=https://your-app.up.railway.app
```

**If `TALAIA_RESEND_API_KEY` is also set, Resend wins.** Unset it to use SMTP.

### Settings for common providers

Starting points — confirm against the provider's current documentation, since these do
change.

| Provider | Host | Port | Username | Password |
|---|---|---|---|---|
| Resend (SMTP) | `smtp.resend.com` | 587 | `resend` | your `re_…` API key |
| SendGrid | `smtp.sendgrid.net` | 587 | `apikey` (literally) | your API key |
| Mailgun | `smtp.mailgun.org` | 587 | `postmaster@mg.yourdomain` | mailbox password |
| Postmark | `smtp.postmarkapp.com` | 587 | server token | same server token |
| Brevo | `smtp-relay.brevo.com` | 587 | your login | SMTP key |
| Amazon SES | `email-smtp.<region>.amazonaws.com` | 587 | SES **SMTP** credentials | — |
| Gmail | `smtp.gmail.com` | 587 | your address | **app password**, not your account password |

Two provider-specific notes:

- **Gmail** needs 2-step verification enabled, then an [app
  password](https://myaccount.google.com/apppasswords). Your normal password will be
  rejected. Gmail also rate-limits hard and is a poor fit for anything public — fine for a
  demo, not for open signup.
- **Amazon SES** starts in a sandbox that only sends to addresses you have verified. You
  must request production access before it will mail strangers. Its SMTP credentials are
  generated in the SES console and are *not* your IAM access keys.

### Ports and TLS

| Port | Setting |
|---|---|
| 587 (submission, usual) | `TALAIA_SMTP_STARTTLS=true`, `TALAIA_SMTP_SSL=false` |
| 465 (implicit TLS) | `TALAIA_SMTP_SSL=true` — STARTTLS is then ignored |
| 25 | Blocked on Railway and most clouds. Do not. |

---

## 4. Every variable

| Variable | Default | Meaning |
|---|---|---|
| `TALAIA_PUBLIC_URL` | — | **Base URL for the link.** Set this; the proxy's host is internal. |
| `TALAIA_RESEND_API_KEY` | — | Selects the Resend backend |
| `TALAIA_EMAIL_FROM` | — | Sender. Must be on a verified domain. Unset + Resend = shared sender. |
| `TALAIA_SMTP_HOST` | — | Selects the SMTP backend (when no Resend key) |
| `TALAIA_SMTP_PORT` | `587` | |
| `TALAIA_SMTP_USER` | — | Omit for an unauthenticated relay |
| `TALAIA_SMTP_PASSWORD` | — | |
| `TALAIA_SMTP_STARTTLS` | `true` | Upgrade the connection after connecting |
| `TALAIA_SMTP_SSL` | `false` | Implicit TLS from the first byte (port 465) |
| `TALAIA_SMTP_TIMEOUT_S` | `20` | Also the Resend HTTP timeout |
| `TALAIA_EMAIL_CONSOLE` | `false` | **Development only** — log instead of send |
| `TALAIA_REQUIRE_EMAIL_VERIFICATION` | `true` | `false` issues keys without checking the address |
| `TALAIA_VERIFICATION_TTL_HOURS` | `24` | Link lifetime |
| `TALAIA_RESEND_API_URL` | Resend's endpoint | Override only for a proxy or a mock |
| `TALAIA_RESEND_DEFAULT_FROM` | `TALAIA <onboarding@resend.dev>` | Sender used when `TALAIA_EMAIL_FROM` is unset and Resend is active. Change it only if Resend changes its shared address. |

**Backend precedence:** `resend` → `smtp` → `console` → none. Chosen by what is set, not
by a mode flag, so there is no way to configure a backend and accidentally run a different
one.

---

## 5. Testing it

Do this once, before anyone tries to sign up. The first person to discover a broken sender
should not be a user whose confirmation never arrives.

```bash
export TALAIA=https://your-app.up.railway.app
export ADMIN=<your TALAIA_ADMIN_KEY>

# What is configured?
curl -s $TALAIA/v1/admin/email -H "X-Admin-Key: $ADMIN"

# Send a real message to an address you can check
curl -X POST $TALAIA/v1/admin/email/test -H "X-Admin-Key: $ADMIN" \
  -H 'content-type: application/json' -d '{"to":"you@example.com"}'
```

Success:

```jsonc
{ "sent": true, "backend": "resend", "from": "TALAIA <noreply@yourdomain.com>",
  "to": "you@example.com", "error": null }
```

Failure returns the **provider's own wording**, which is usually the entire diagnosis:

```jsonc
{ "sent": false, "backend": "resend",
  "error": "RuntimeError: Resend returned 403: The yourdomain.com domain is not verified.
            Please verify your domain on https://resend.com/domains" }
```

Then run the real thing end to end — sign up with an address you control and follow the
link:

```bash
curl -X POST $TALAIA/v1/signup -H 'content-type: application/json' \
  -d '{"email":"you@example.com","organisation":"Test"}'
# -> 202 {"status":"verification_sent", …}   and no key exists yet
```

---

## 6. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `503` on signup, `"no mail sender configured"` | Neither `TALAIA_RESEND_API_KEY` nor `TALAIA_SMTP_HOST` is set | Set one. Check for typos — a misspelled variable is simply ignored. |
| Test succeeds for you, everyone else's signup fails | Resend's shared sender only reaches the account owner | Verify a domain, set `TALAIA_EMAIL_FROM` |
| `Resend returned 403: … domain is not verified` | `TALAIA_EMAIL_FROM` is on an unverified domain | Verify it, or clear the variable to fall back to the shared sender |
| `Resend returned 401: API key is invalid` | Wrong or revoked key | Reissue. Keys start `re_`. |
| `Resend returned 422` | Malformed `from` — missing domain, or bad `Name <addr>` syntax | Use `Name <user@domain.com>` or a bare address |
| `SMTPAuthenticationError` | Wrong credentials; for Gmail, an account password instead of an app password | Generate an app password |
| `ConnectionRefusedError` / timeout | Wrong port, or port 25 | Use 587 with STARTTLS, or 465 with `TALAIA_SMTP_SSL=true` |
| `SSLError: WRONG_VERSION_NUMBER` | TLS mode does not match the port | 587 → STARTTLS; 465 → `SMTP_SSL` |
| Mail sends, link 404s or points at the wrong host | `TALAIA_PUBLIC_URL` unset or wrong | Set it to the public URL, no trailing slash |
| Mail arrives in spam | No SPF/DKIM on the sender domain | Verify the domain properly; do not send from a domain you do not control |
| Signup returns a key with no email at all | `TALAIA_REQUIRE_EMAIL_VERIFICATION=false` | Set it back to `true` |

Logs carry the detail. On boot you will see one of:

```
INFO   mail backend: resend, sending as TALAIA <noreply@yourdomain.com>
WARN   using Resend's shared sender; it only delivers to the account owner …
ERROR  signup is enabled and requires email verification, but no mail sender is configured …
WARN   signup is OPEN and email verification is DISABLED …
```

---

## 7. Local development

No account needed. Log the message instead of sending it:

```bash
TALAIA_EMAIL_CONSOLE=true
TALAIA_PUBLIC_URL=http://127.0.0.1:8000
```

The message is written to the log, **and the verification link is returned in the signup
response** so you can follow it without a mailbox:

```jsonc
{ "status": "verification_sent",
  "verification_link": "http://127.0.0.1:8000/verify?token=…" }
```

That field only ever appears with the console backend. Never enable it on a public
deployment — it hands every caller a working link for any address they name.

---

## See also

- [DEPLOY-RAILWAY.md](DEPLOY-RAILWAY.md) — where this fits in a deployment
- [LIMITS-AND-KEYS.md](LIMITS-AND-KEYS.md) — signup controls, tiers and quotas
- [API.md](API.md#2-authentication-and-limits) — the signup and verification endpoints
