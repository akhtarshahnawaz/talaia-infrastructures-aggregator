"""The documentation has to describe the software that exists.

There is a lot of it now, and the failure mode is quiet: a setting gets renamed, the guide
keeps the old name, and the next person to follow it sets a variable that is silently
ignored. These checks are cheap and they catch exactly that.
"""
import pathlib
import re

import pytest

from talaia.config import Settings

ROOT = pathlib.Path(__file__).resolve().parents[1]

DOCS = ["README.md", ".env.example", "docs/API.md", "docs/EMAIL-SETUP.md",
        "docs/DEPLOY-RAILWAY.md", "docs/LIMITS-AND-KEYS.md", "docs/PRECACHING.md",
        "docs/MCP.md"]

REAL_SETTINGS = {f"TALAIA_{name.upper()}" for name in Settings.model_fields}

# Not settings: a shell variable used in examples, and a prose glob for a family of them.
NOT_SETTINGS = {"TALAIA_KEY", "TALAIA_API_KEY", "TALAIA_URL", "TALAIA_SMTP_"}


@pytest.mark.parametrize("doc", DOCS)
def test_every_documented_variable_exists(doc):
    text = (ROOT / doc).read_text()
    used = set(re.findall(r"TALAIA_[A-Z0-9_]+", text)) - NOT_SETTINGS
    unknown = used - REAL_SETTINGS
    assert not unknown, f"{doc} documents settings that do not exist: {sorted(unknown)}"


def test_the_email_settings_are_all_documented():
    """A sender that cannot be configured from the guide is a sender nobody sets up."""
    text = (ROOT / "docs/EMAIL-SETUP.md").read_text()
    for name in REAL_SETTINGS:
        if any(k in name for k in ("SMTP", "RESEND", "EMAIL")):
            assert name in text, f"{name} is undocumented in EMAIL-SETUP.md"


@pytest.mark.parametrize("doc", DOCS)
def test_internal_links_resolve(doc):
    """A guide that points at a file that does not exist is worse than no pointer."""
    path = ROOT / doc
    broken = []
    for target in re.findall(r"\]\((?!https?://|#|mailto:)([^)#]+)", path.read_text()):
        if not (path.parent / target).exists():
            broken.append(target)
    assert not broken, f"{doc} links to missing files: {broken}"


@pytest.mark.parametrize("doc", ["docs/API.md", "docs/EMAIL-SETUP.md"])
def test_the_table_of_contents_matches_the_headings(doc):
    text = (ROOT / doc).read_text()
    anchors = {a for a in re.findall(r"\]\(#([a-z0-9-]+)\)", text)}
    headings = {
        re.sub(r"[^a-z0-9 -]", "", h.lower()).replace(" ", "-")
        for h in re.findall(r"^#{2,3} (.+)$", text, re.M)}
    missing = {a for a in anchors if a not in headings}
    assert not missing, f"{doc} has links to headings that do not exist: {sorted(missing)}"


def test_the_backend_precedence_is_stated_the_same_way_everywhere():
    """Getting this wrong means someone sets SMTP, leaves a Resend key in place, and
    cannot work out why their SMTP server sees no traffic."""
    for doc in ("docs/EMAIL-SETUP.md", "docs/DEPLOY-RAILWAY.md"):
        text = (ROOT / doc).read_text()
        assert "Resend wins" in text or "resend` → `smtp" in text, (
            f"{doc} does not say which backend takes precedence")


def test_the_shared_sender_trap_is_warned_about_wherever_resend_is_set_up():
    """It is the one failure that looks exactly like success."""
    for doc in ("docs/EMAIL-SETUP.md", "docs/DEPLOY-RAILWAY.md", ".env.example"):
        text = (ROOT / doc).read_text().lower()
        assert "onboarding@resend.dev" in text
        assert "account" in text and "only" in text, (
            f"{doc} mentions the shared sender without explaining who it reaches")


def test_port_25_is_flagged_as_blocked():
    text = (ROOT / "docs/DEPLOY-RAILWAY.md").read_text()
    assert "25" in text and "block" in text.lower()


def test_the_console_backend_is_marked_development_only():
    """It returns a working verification link to every caller."""
    for doc in ("docs/EMAIL-SETUP.md", ".env.example"):
        text = (ROOT / doc).read_text()
        assert "TALAIA_EMAIL_CONSOLE" in text
        window = text[max(0, text.index("TALAIA_EMAIL_CONSOLE") - 500):]
        assert re.search(r"development|local|Never enable", window, re.I), (
            f"{doc} does not mark the console backend as development-only")


def test_httpx_does_not_log_every_request():
    """A cold national ingest makes ~65,000 requests. At INFO, httpx narrates all of
    them onto stderr, which buries the real log and can trip a platform log rate limit.
    """
    import logging

    import talaia.main  # noqa: F401  - importing configures logging

    assert logging.getLogger("httpx").level >= logging.WARNING
