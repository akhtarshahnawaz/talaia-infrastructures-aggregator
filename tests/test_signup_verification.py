"""Email verification: no key exists until a mailed link is followed.

Includes a real SMTP conversation against a local sink, because the interesting failure
is a deployment that believes it is sending mail and is not.
"""
import asyncio
import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from talaia import mailer
from talaia.auth import KeyRegistry, hash_key, registry as key_registry
from talaia.config import settings
from talaia.models import SignupRequest, VerifyRequest
from talaia.routers.v1 import signup, verify, verify_get
from talaia.store import Store, set_store


class _Request:
    """Minimal stand-in for a Starlette request: headers, client and base_url."""

    def __init__(self, ip="203.0.113.10", headers=None):
        self.headers = headers or {}
        self.client = type("C", (), {"host": ip})()
        self.url = type("U", (), {"scheme": "https"})()
        self.base_url = "https://talaia.example/"


@pytest.fixture
async def store(tmp_path, monkeypatch):
    s = Store(tmp_path / "verify.duckdb")
    s.connect()
    set_store(s)
    key_registry._by_hash.clear()
    monkeypatch.setattr(settings, "allow_signup", True)
    monkeypatch.setattr(settings, "require_email_verification", True)
    monkeypatch.setattr(settings, "signups_per_ip_per_day", 3)
    monkeypatch.setattr(settings, "public_url", "https://talaia.example")
    monkeypatch.setattr(settings, "smtp_host", "")
    monkeypatch.setattr(settings, "email_console", False)
    yield s
    s.close()


@pytest.fixture
def outbox(monkeypatch):
    """Capture what would be sent, without a transport."""
    sent: list[dict] = []

    async def fake(to, subject, text, html=None):
        sent.append({"to": to, "subject": subject, "text": text, "html": html})
        return True

    monkeypatch.setattr(settings, "smtp_host", "mail.example")
    monkeypatch.setattr(mailer, "send", fake)
    return sent


def _link(message: str) -> str:
    m = re.search(r"https://\S+/verify\?token=\S+", message)
    assert m, f"no verification link in:\n{message}"
    return m.group(0).rstrip(".")


def _token(message: str) -> str:
    return _link(message).split("token=", 1)[1]


# -- the core guarantee -----------------------------------------------------
async def test_signup_creates_no_key_until_the_link_is_followed(store, outbox):
    out = await signup(SignupRequest(email="Someone@Example.com"), _Request())

    assert out["status"] == "verification_sent"
    assert "api_key" not in out, "a key must not be issued before confirmation"
    assert (await store.fetch("SELECT count(*) FROM api_keys"))[0][0] == 0
    assert len(outbox) == 1 and outbox[0]["to"] == "someone@example.com"

    result = await verify(VerifyRequest(token=_token(outbox[0]["text"])), _Request())
    assert result["api_key"].startswith("talaia_sk_")
    assert result["email_verified"] is True
    assert key_registry.verify(result["api_key"]) is not None


async def test_the_email_never_carries_the_key_itself(store, outbox):
    """Mail is not a confidential channel and a key sent there lives in an inbox."""
    await signup(SignupRequest(email="a@example.com"), _Request())
    body = outbox[0]["text"] + (outbox[0]["html"] or "")
    assert "talaia_sk_" not in body


async def test_only_a_hash_of_the_token_is_stored(store, outbox):
    await signup(SignupRequest(email="a@example.com"), _Request())
    token = _token(outbox[0]["text"])
    stored = (await store.fetch("SELECT token_hash FROM pending_signups"))[0][0]
    assert stored == hash_key(token)
    assert token not in stored


async def test_a_token_works_exactly_once(store, outbox):
    await signup(SignupRequest(email="a@example.com"), _Request())
    token = _token(outbox[0]["text"])
    await verify(VerifyRequest(token=token), _Request())

    with pytest.raises(HTTPException) as exc:
        await verify(VerifyRequest(token=token), _Request())
    assert exc.value.status_code == 409
    assert (await store.fetch("SELECT count(*) FROM api_keys"))[0][0] == 1


async def test_an_expired_token_is_refused(store, outbox):
    await signup(SignupRequest(email="a@example.com"), _Request())
    token = _token(outbox[0]["text"])
    # Bound as naive UTC, exactly as the signup path writes it. Using the database's
    # current_timestamp here instead would set the column in local time and the test
    # would pass or fail depending on the host's offset - the same confusion that made
    # the OSM tile backoff silently never fire.
    await store.execute_write(
        "UPDATE pending_signups SET expires_at = ?",
        [datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)])

    with pytest.raises(HTTPException) as exc:
        await verify(VerifyRequest(token=token), _Request())
    assert exc.value.status_code == 410
    assert (await store.fetch("SELECT count(*) FROM api_keys"))[0][0] == 0


async def test_expiry_is_judged_in_utc_not_the_servers_local_time(store, outbox):
    """A token with hours left must not read as expired on a host east of UTC."""
    await signup(SignupRequest(email="a@example.com"), _Request())
    token = _token(outbox[0]["text"])
    stored = (await store.fetch("SELECT expires_at FROM pending_signups"))[0][0]
    ahead = (stored - datetime.now(timezone.utc).replace(tzinfo=None)).total_seconds()
    assert 23 * 3600 < ahead <= 24 * 3600, "expiry is stored in UTC, like the comparison"
    assert (await verify(VerifyRequest(token=token), _Request()))["api_key"]


@pytest.mark.parametrize("token", ["not-a-real-token-value-at-all",
                                   "talaia_sk_looks_like_a_key_but_is_not"])
async def test_a_made_up_token_gets_nothing(store, outbox, token):
    with pytest.raises(HTTPException) as exc:
        await verify(VerifyRequest(token=token), _Request())
    assert exc.value.status_code == 404
    assert (await store.fetch("SELECT count(*) FROM api_keys"))[0][0] == 0


@pytest.mark.parametrize("token", ["", "   ", "short"])
def test_an_empty_or_tiny_token_is_rejected_by_the_contract(token):
    """Guessing space is the point: the model refuses anything too short to be a token
    before a lookup ever happens."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        VerifyRequest(token=token)


async def test_requesting_again_invalidates_the_previous_link(store, outbox):
    """Otherwise every address accumulates live links that never stop working."""
    r = _Request()
    await signup(SignupRequest(email="a@example.com"), r)
    first = _token(outbox[0]["text"])
    await signup(SignupRequest(email="a@example.com"), r)
    second = _token(outbox[1]["text"])
    assert first != second

    with pytest.raises(HTTPException) as exc:
        await verify(VerifyRequest(token=first), r)
    assert exc.value.status_code == 410
    assert (await verify(VerifyRequest(token=second), r))["api_key"]


async def test_the_get_form_works_for_a_link_clicked_in_a_mail_client(store, outbox):
    await signup(SignupRequest(email="a@example.com"), _Request())
    out = await verify_get(_token(outbox[0]["text"]), _Request())
    assert out["api_key"].startswith("talaia_sk_")


# -- the guards still apply, at both steps ----------------------------------
async def test_the_address_is_checked_before_any_mail_goes_out(store, outbox):
    with pytest.raises(HTTPException) as exc:
        await signup(SignupRequest(email="not-an-email"), _Request())
    assert exc.value.status_code == 422
    assert outbox == [], "no mail for an address that cannot be one"


async def test_an_address_that_already_has_a_key_is_refused(store, outbox):
    await signup(SignupRequest(email="a@example.com"), _Request())
    await verify(VerifyRequest(token=_token(outbox[0]["text"])), _Request())

    with pytest.raises(HTTPException) as exc:
        await signup(SignupRequest(email="a@example.com"), _Request())
    assert exc.value.status_code == 409


async def test_the_per_ip_cap_counts_issued_keys_not_attempts(store, outbox, monkeypatch):
    monkeypatch.setattr(settings, "signups_per_ip_per_day", 2)
    r = _Request(ip="198.51.100.7")
    for i in range(2):
        await signup(SignupRequest(email=f"u{i}@example.com"), r)
        await verify(VerifyRequest(token=_token(outbox[-1]["text"])), r)

    with pytest.raises(HTTPException) as exc:
        await signup(SignupRequest(email="u2@example.com"), r)
    assert exc.value.status_code == 429


async def test_a_cap_reached_between_request_and_click_still_blocks(store, outbox,
                                                                    monkeypatch):
    """The guards run again at verification, because time passes in between."""
    r = _Request(ip="198.51.100.9")
    await signup(SignupRequest(email="late@example.com"), r)
    token = _token(outbox[0]["text"])

    monkeypatch.setattr(settings, "allow_signup", False)
    with pytest.raises(HTTPException) as exc:
        await verify(VerifyRequest(token=token), r)
    assert exc.value.status_code == 403


# -- failure modes that must not hand out keys ------------------------------
async def test_no_mail_backend_refuses_rather_than_issuing_unverified(store):
    """Falling back here would silently undo verification while the operator believed
    addresses were being checked."""
    assert mailer.backend() == "none"
    with pytest.raises(HTTPException) as exc:
        await signup(SignupRequest(email="a@example.com"), _Request())
    assert exc.value.status_code == 503
    assert (await store.fetch("SELECT count(*) FROM api_keys"))[0][0] == 0


async def test_a_send_failure_leaves_no_key_behind(store, monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", "mail.example")

    async def fails(*a, **k):
        return False

    monkeypatch.setattr(mailer, "send", fails)
    with pytest.raises(HTTPException) as exc:
        await signup(SignupRequest(email="a@example.com"), _Request())
    assert exc.value.status_code == 503
    assert (await store.fetch("SELECT count(*) FROM api_keys"))[0][0] == 0


async def test_verification_can_be_turned_off_but_says_so(store, monkeypatch):
    monkeypatch.setattr(settings, "require_email_verification", False)
    out = await signup(SignupRequest(email="a@example.com"), _Request())
    assert out["api_key"].startswith("talaia_sk_")
    assert out["email_verified"] is False
    assert "not confirmed" in out["note"]


# -- link construction ------------------------------------------------------
async def test_the_link_uses_the_configured_public_url(store, outbox):
    await signup(SignupRequest(email="a@example.com"), _Request())
    assert _link(outbox[0]["text"]).startswith("https://talaia.example/verify?token=")


async def test_behind_a_proxy_the_forwarded_host_is_used(store, outbox, monkeypatch):
    """The request's own host is the internal one on Railway; a link built from it
    would reach nobody."""
    monkeypatch.setattr(settings, "public_url", "")
    req = _Request(headers={"x-forwarded-host": "talaia.up.railway.app",
                            "x-forwarded-proto": "https"})
    await signup(SignupRequest(email="a@example.com"), req)
    assert _link(outbox[0]["text"]).startswith(
        "https://talaia.up.railway.app/verify?token=")


# -- backend selection ------------------------------------------------------
def test_the_backend_is_chosen_by_what_is_configured(monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", "")
    monkeypatch.setattr(settings, "email_console", False)
    assert mailer.backend() == "none" and mailer.available() is False

    monkeypatch.setattr(settings, "email_console", True)
    assert mailer.backend() == "console" and mailer.available() is True

    monkeypatch.setattr(settings, "smtp_host", "mail.example")
    assert mailer.backend() == "smtp", "a real transport outranks the console"


async def test_the_console_backend_never_claims_to_have_sent(monkeypatch, caplog):
    monkeypatch.setattr(settings, "smtp_host", "")
    monkeypatch.setattr(settings, "email_console", True)
    assert await mailer.send("a@example.com", "s", "body text") is True
    assert "not sent" in caplog.text


# -- a real SMTP conversation ------------------------------------------------
class _SMTPSink:
    """A minimal SMTP server that accepts one message and records it.

    Worth the twenty lines: monkeypatching `send` proves the routing, not that the
    transport works. This exercises smtplib against a real socket.
    """

    def __init__(self):
        self.messages: list[str] = []
        self._server = None

    async def start(self):
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self._server.sockets[0].getsockname()[1]

    async def _handle(self, reader, writer):
        async def say(line):
            writer.write(line.encode() + b"\r\n")
            await writer.drain()

        await say("220 sink ESMTP")
        body, in_data = [], False
        while True:
            raw = await reader.readline()
            if not raw:
                break
            line = raw.decode(errors="replace").rstrip("\r\n")
            if in_data:
                if line == ".":
                    self.messages.append("\n".join(body))
                    in_data = False
                    await say("250 OK queued")
                else:
                    body.append(line)
                continue
            upper = line.upper()
            if upper.startswith(("HELO", "EHLO")):
                await say("250-sink")
                await say("250 HELP")
            elif upper.startswith(("MAIL", "RCPT")):
                await say("250 OK")
            elif upper.startswith("DATA"):
                in_data = True
                await say("354 End data with <CR><LF>.<CR><LF>")
            elif upper.startswith("QUIT"):
                await say("221 Bye")
                break
            else:
                await say("250 OK")
        writer.close()

    async def stop(self):
        self._server.close()
        await self._server.wait_closed()


async def test_a_message_actually_reaches_an_smtp_server(monkeypatch):
    sink = _SMTPSink()
    port = await sink.start()
    monkeypatch.setattr(settings, "smtp_host", "127.0.0.1")
    monkeypatch.setattr(settings, "smtp_port", port)
    monkeypatch.setattr(settings, "smtp_starttls", False)
    monkeypatch.setattr(settings, "smtp_ssl", False)
    monkeypatch.setattr(settings, "smtp_user", "")
    monkeypatch.setattr(settings, "email_from", "talaia@example.com")

    subject, text, html = mailer.verification_message(
        "https://talaia.example/verify?token=abc123", 24)
    try:
        assert await mailer.send("someone@example.com", subject, text, html) is True
    finally:
        await sink.stop()

    assert len(sink.messages) == 1
    wire = sink.messages[0]
    assert "To: someone@example.com" in wire
    assert "abc123" in wire, "the link has to survive the transport"
    assert "talaia_sk_" not in wire


async def test_an_unreachable_server_is_reported_not_raised(monkeypatch):
    """Signup turns this into a clean 503; an exception here would be a 500 that tells
    the caller nothing."""
    monkeypatch.setattr(settings, "smtp_host", "127.0.0.1")
    monkeypatch.setattr(settings, "smtp_port", 9)  # discard port, nothing listening
    monkeypatch.setattr(settings, "smtp_starttls", False)
    monkeypatch.setattr(settings, "smtp_timeout_s", 2.0)
    assert await mailer.send("a@example.com", "s", "t") is False
