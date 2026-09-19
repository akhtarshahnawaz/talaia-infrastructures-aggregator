"""Outbound email, for address verification.

Backends are selected by what is configured rather than by a mode flag, because a
deployment that thinks it is sending mail while silently dropping it is the failure that
matters here. Most explicit wins:

* **resend** - one API key, no server to run. Chosen when ``TALAIA_RESEND_API_KEY`` is
  set. Sends over HTTPS with the httpx client already in use, so nothing new is added.
* **smtp** - any other provider. Standard library in a worker thread. Chosen when
  ``TALAIA_SMTP_HOST`` is set and no Resend key is.
* **console** - logs the message instead of sending it. Explicit opt-in via
  ``TALAIA_EMAIL_CONSOLE=true``; for local development only.
* **none** - nothing configured. :func:`available` returns False and signup refuses with
  503 rather than falling back to issuing unverified keys, which would quietly undo the
  whole point of verifying.

Every backend reports *why* a send failed through :func:`last_error`, because the common
Resend failure is a sender domain that has not been verified yet and the message the
provider returns says exactly that.

Nothing here ever emails an API key. The message carries a single-use verification link;
the key is shown once, in the browser, after the link is followed. Email is not a
confidential channel and a key mailed to someone sits in their inbox for years.
"""
from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage

from .config import settings

log = logging.getLogger("talaia.mailer")


_last_error: str | None = None


def backend() -> str:
    """Which backend will actually be used. Cheap enough to call per request."""
    if settings.resend_api_key:
        return "resend"
    if settings.smtp_host:
        return "smtp"
    if settings.email_console:
        return "console"
    return "none"


def available() -> bool:
    return backend() != "none"


def last_error() -> str | None:
    """Why the most recent send failed, for the operator-facing test endpoint."""
    return _last_error


def _from_address() -> str:
    if settings.email_from:
        return settings.email_from
    if backend() == "resend":
        # Resend's shared sender needs no DNS, but only delivers to the account owner.
        return settings.resend_default_from
    return f"talaia@{settings.smtp_host or 'localhost'}"


def using_shared_sender() -> bool:
    """True when mail goes out as Resend's onboarding address.

    Worth surfacing: in that state signup looks healthy and silently reaches nobody but
    the account owner, which is indistinguishable from working until someone else tries
    to register.
    """
    return backend() == "resend" and not settings.email_from


def _build(to: str, subject: str, text: str, html: str | None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = _from_address()
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    return msg


def _send_smtp(msg: EmailMessage) -> None:
    """Blocking send. Called in a worker thread."""
    host, port = settings.smtp_host, settings.smtp_port
    timeout = settings.smtp_timeout_s
    if settings.smtp_ssl:
        client = smtplib.SMTP_SSL(host, port, timeout=timeout,
                                  context=ssl.create_default_context())
    else:
        client = smtplib.SMTP(host, port, timeout=timeout)
    try:
        client.ehlo()
        if settings.smtp_starttls and not settings.smtp_ssl:
            client.starttls(context=ssl.create_default_context())
            client.ehlo()
        if settings.smtp_user:
            client.login(settings.smtp_user, settings.smtp_password or "")
        client.send_message(msg)
    finally:
        try:
            client.quit()
        except Exception:  # pragma: no cover - the message is already sent
            client.close()


async def _send_resend(to: str, subject: str, text: str, html: str | None) -> None:
    """POST one message to Resend. Raises with the provider's own message on failure."""
    from .net import get_client

    payload: dict[str, object] = {
        "from": _from_address(), "to": [to], "subject": subject, "text": text,
    }
    if html:
        payload["html"] = html
    resp = await get_client().post(
        settings.resend_api_url, json=payload,
        headers={"Authorization": f"Bearer {settings.resend_api_key}",
                 "Content-Type": "application/json"},
        timeout=settings.smtp_timeout_s)
    if resp.status_code >= 400:
        detail = resp.text[:300]
        try:
            body = resp.json()
            detail = str(body.get("message") or body.get("error") or detail)
        except Exception:
            pass
        raise RuntimeError(f"Resend returned {resp.status_code}: {detail}")


async def send(to: str, subject: str, text: str, html: str | None = None) -> bool:
    """Deliver one message. Returns True if it was handed to a transport.

    Never raises: a mail outage must surface as a clear 503 on signup, not as a 500 that
    tells the caller nothing and leaves a half-created account behind.
    """
    global _last_error
    mode = backend()
    if mode == "none":
        _last_error = "No mail backend configured."
        log.error("no mail backend configured; cannot send to %s", to)
        return False
    if mode == "console":
        _last_error = None
        log.warning("\n  ---- EMAIL (console backend, not sent) ----\n"
                    "  To: %s\n  Subject: %s\n\n%s\n"
                    "  -------------------------------------------", to, subject, text)
        return True
    try:
        if mode == "resend":
            await _send_resend(to, subject, text, html)
            log.info("verification email sent to %s via Resend", to)
        else:
            await asyncio.to_thread(_send_smtp, _build(to, subject, text, html))
            log.info("verification email sent to %s via %s", to, settings.smtp_host)
        _last_error = None
        return True
    except Exception as exc:
        # Keep the provider's own wording: "The domain is not verified" is the whole
        # diagnosis, and paraphrasing it would cost the operator the answer.
        _last_error = f"{type(exc).__name__}: {exc}"
        log.error("%s send to %s failed: %s", mode, to, _last_error)
        return False


# ---------------------------------------------------------------------------
# Message content
# ---------------------------------------------------------------------------
def verification_message(link: str, hours: int) -> tuple[str, str, str]:
    """Returns ``(subject, text, html)`` for the verification email."""
    subject = "Confirm your email for TALAIA API access"
    text = (
        "Someone asked for a TALAIA API key using this address.\n\n"
        "Confirm it by opening this link:\n\n"
        f"  {link}\n\n"
        f"The link works once and expires in {hours} hours.\n\n"
        "Your API key is shown on that page, not in this email - email is not a "
        "secure channel to send a credential over.\n\n"
        "If you did not request this, ignore this message. No key has been created "
        "and nothing further will be sent.\n\n"
        "TALAIA - values-at-risk infrastructure API\n"
    )
    html = f"""\
<!doctype html><html><body style="margin:0;padding:24px;background:#0b1220;
 font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#cbd5e1">
 <div style="max-width:520px;margin:0 auto;background:#111a2e;border:1px solid #1e293b;
  border-radius:12px;padding:28px">
  <div style="font-size:12px;letter-spacing:.14em;text-transform:uppercase;color:#f97316">
   TALAIA</div>
  <h1 style="margin:10px 0 16px;font-size:20px;color:#f1f5f9">Confirm your email</h1>
  <p style="margin:0 0 18px;line-height:1.6;font-size:14px">
   Someone asked for a TALAIA API key using this address. Confirm it to receive your key.
  </p>
  <p style="margin:0 0 22px">
   <a href="{link}" style="display:inline-block;background:#ea580c;color:#fff;
    text-decoration:none;padding:11px 20px;border-radius:8px;font-size:14px;
    font-weight:500">Confirm and get my key</a>
  </p>
  <p style="margin:0 0 14px;line-height:1.6;font-size:13px;color:#94a3b8">
   The link works once and expires in {hours} hours. Your key is shown on that page,
   not in this email &mdash; email is not a secure channel to send a credential over.
  </p>
  <p style="margin:0 0 14px;line-height:1.6;font-size:13px;color:#94a3b8">
   If you did not request this, ignore this message. No key has been created.
  </p>
  <p style="margin:18px 0 0;font-size:11px;color:#64748b;word-break:break-all">
   If the button does not work, paste this into your browser:<br>{link}
  </p>
 </div></body></html>
"""
    return subject, text, html
