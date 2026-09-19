"""Normalisers shared by every connector.

Spanish and Catalan open data arrive in a variety of shapes: UTM eastings, degrees-
minutes-seconds strings, phone numbers with assorted separators, accented place names,
and numeric fields typed as text. Everything that turns a registry record into the
canonical model lives here so connectors stay thin and the behaviour is tested once.
"""
from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Coordinates
# ---------------------------------------------------------------------------
# GRS80 / ETRS89 - the datum behind EPSG:25829-25831 used across Spanish open data.
_A = 6378137.0
_F = 1 / 298.257222101
_E2 = _F * (2 - _F)
_K0 = 0.9996
_FE = 500_000.0


def utm_to_wgs84(easting: float, northing: float, zone: int = 31,
                 northern: bool = True) -> tuple[float, float]:
    """Inverse transverse Mercator (Snyder 8-3..8-9). Returns ``(lon, lat)`` degrees.

    Implemented directly rather than via pyproj: it is ~40 lines, has no binary
    dependency, and keeps the container small. Accuracy is millimetre-level, far
    beyond what registry coordinates justify.
    """
    if not northern:
        northing -= 10_000_000.0
    x = easting - _FE
    y = northing
    e1 = (1 - math.sqrt(1 - _E2)) / (1 + math.sqrt(1 - _E2))
    m = y / _K0
    mu = m / (_A * (1 - _E2 / 4 - 3 * _E2**2 / 64 - 5 * _E2**3 / 256))
    phi1 = (mu
            + (3 * e1 / 2 - 27 * e1**3 / 32) * math.sin(2 * mu)
            + (21 * e1**2 / 16 - 55 * e1**4 / 32) * math.sin(4 * mu)
            + (151 * e1**3 / 96) * math.sin(6 * mu)
            + (1097 * e1**4 / 512) * math.sin(8 * mu))
    sin_phi1, cos_phi1, tan_phi1 = math.sin(phi1), math.cos(phi1), math.tan(phi1)
    ep2 = _E2 / (1 - _E2)
    c1 = ep2 * cos_phi1**2
    t1 = tan_phi1**2
    n1 = _A / math.sqrt(1 - _E2 * sin_phi1**2)
    r1 = _A * (1 - _E2) / (1 - _E2 * sin_phi1**2) ** 1.5
    d = x / (n1 * _K0)
    lat = phi1 - (n1 * tan_phi1 / r1) * (
        d**2 / 2
        - (5 + 3 * t1 + 10 * c1 - 4 * c1**2 - 9 * ep2) * d**4 / 24
        + (61 + 90 * t1 + 298 * c1 + 45 * t1**2 - 252 * ep2 - 3 * c1**2) * d**6 / 720)
    lon = (d
           - (1 + 2 * t1 + c1) * d**3 / 6
           + (5 - 2 * c1 + 28 * t1 - 3 * c1**2 + 8 * ep2 + 24 * t1**2) * d**5 / 120) / cos_phi1
    lon0 = math.radians((zone - 1) * 6 - 180 + 3)
    return math.degrees(lon) + math.degrees(lon0), math.degrees(lat)


_DMS_RE = re.compile(
    r"^\s*(-?\d+(?:[.,]\d+)?)\s*[º°d]\s*"      # degrees
    r"(?:(\d+(?:[.,]\d+)?)\s*['′m]\s*)?"   # minutes
    r"(?:(\d+(?:[.,]\d+)?)\s*(?:''|\"|″|s)\s*)?"  # seconds
    r"([NSEWO])?\s*$", re.I)


def parse_dms(value: Any) -> float | None:
    """Parse ``42.0º 8.0' 33.0498''`` into decimal degrees.

    The Catalan livestock-farm registry publishes coordinates in exactly this format,
    which silently parses as ``42.0`` if you merely call ``float()`` on it - an error of
    up to ~100 km. Returns ``None`` when the value is not DMS so callers can fall back.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    m = _DMS_RE.match(text)
    if not m:
        try:
            return float(text.replace(",", "."))
        except ValueError:
            return None
    deg = float(m.group(1).replace(",", "."))
    minutes = float((m.group(2) or "0").replace(",", "."))
    seconds = float((m.group(3) or "0").replace(",", "."))
    hemi = (m.group(4) or "").upper()
    sign = -1.0 if (deg < 0 or hemi in ("S", "W", "O")) else 1.0
    return sign * (abs(deg) + minutes / 60.0 + seconds / 3600.0)


def to_float(value: Any) -> float | None:
    """Tolerant numeric parse: handles ``"1.234,5"``, ``"1,5"``, ``""`` and ``None``."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if not isinstance(value, bool) else None
    text = str(value).strip()
    if not text or text.lower() in ("na", "n/a", "-", "null", "none"):
        return None
    text = re.sub(r"[^\d,.\-+eE]", "", text)
    if not text:
        return None
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".") if text.rfind(",") > text.rfind(".") \
            else text.replace(",", "")
    elif "," in text:
        # A single comma is a decimal separator in Spanish locales.
        text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def valid_lonlat(lon: Any, lat: Any) -> tuple[float, float] | None:
    """Validate a coordinate pair, rejecting the null island and out-of-range values."""
    try:
        lo, la = float(lon), float(lat)
    except (TypeError, ValueError):
        return None
    if not (-180 <= lo <= 180 and -90 <= la <= 90):
        return None
    if abs(lo) < 1e-9 and abs(la) < 1e-9:
        return None
    if math.isnan(lo) or math.isnan(la):
        return None
    return lo, la


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------
_LEGAL_FORMS = {
    "sa", "sl", "slu", "sau", "scp", "sccl", "sl.", "s.a", "s.l", "cb", "sc",
    "fundacio", "fundacion", "foundation", "assoc", "associacio", "asociacion",
}
_NOISE_WORDS = {"de", "del", "la", "el", "les", "los", "las", "i", "y", "d", "l", "en"}


def fold(text: Any) -> str:
    """Accent- and case-fold for matching. ``Sant Joan de Déu`` -> ``sant joan de deu``."""
    if text is None:
        return ""
    s = unicodedata.normalize("NFKD", str(text))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace("·", "").replace("'", " ").replace("’", " ")
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    return re.sub(r"\s+", " ", s).strip().lower()


def name_key(text: Any) -> str:
    """Aggressive normalisation for conflation: folded, legal forms and noise removed."""
    tokens = [t for t in fold(text).split()
              if t not in _LEGAL_FORMS and t not in _NOISE_WORDS and len(t) > 1]
    return " ".join(sorted(tokens))


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    s = re.sub(r"\s+", " ", str(value)).strip()
    return s or None


def title_case(value: Any) -> str | None:
    """Fix ALL-CAPS registry names without destroying acronyms."""
    s = clean_text(value)
    if not s:
        return None
    if s.isupper() and len(s) > 4:
        return " ".join(w if len(w) <= 3 and w.isupper() else w.capitalize()
                        for w in s.split())
    return s


# ---------------------------------------------------------------------------
# Addresses
# ---------------------------------------------------------------------------
# Catalan/Spanish street-type abbreviations as they appear in the registries.
_STREET_TYPES = {
    "c": "Carrer", "c/": "Carrer", "cr": "Carrer", "carrer": "Carrer",
    "cl": "Calle", "calle": "Calle",
    "av": "Avinguda", "avda": "Avinguda", "avgda": "Avinguda", "avinguda": "Avinguda",
    "pl": "Plaça", "pca": "Plaça", "placa": "Plaça", "pza": "Plaza", "plaza": "Plaza",
    "ptge": "Passatge", "pge": "Passatge", "passatge": "Passatge",
    "ps": "Passeig", "pg": "Passeig", "passeig": "Passeig", "paseo": "Paseo",
    "ctra": "Carretera", "ctr": "Carretera", "cra": "Carretera", "carretera": "Carretera",
    "rbla": "Rambla", "rambla": "Rambla", "trav": "Travessera", "tv": "Travessera",
    "urb": "Urbanitzacio", "pol": "Poligon", "ronda": "Ronda", "cami": "Cami",
    "ca": "Cami", "bda": "Baixada", "pje": "Pasaje", "gv": "Gran Via",
}


def expand_street(value: Any) -> str | None:
    """Expand a leading street-type abbreviation: ``Pca.de l'Hospital, 2`` ->
    ``Placa de l'Hospital 2``.

    CartoCiudad resolves expanded street types reliably and abbreviated ones not at all,
    so this is the difference between geocoding a care home and losing it.
    """
    s = clean_text(value)
    if not s:
        return None
    s = re.sub(r"^([A-Za-z\u00C0-\u017F/]{1,8})\.\s*", r"\1 ", s)
    parts = s.split(None, 1)
    if parts:
        head = parts[0].rstrip(".").rstrip("/").lower()
        from unicodedata import normalize as _n
        folded = "".join(ch for ch in _n("NFKD", head) if not __import__("unicodedata").combining(ch))
        if folded in _STREET_TYPES:
            s = _STREET_TYPES[folded] + (" " + parts[1] if len(parts) > 1 else "")
    # "Carrer Creueta, 51" -> "Carrer Creueta 51": CartoCiudad prefers no comma
    s = re.sub(r",\s*(\d+)", r" \1", s)
    return re.sub(r"\s+", " ", s).strip()


def geocode_query(street: Any, municipality: Any, country: str = "Spain") -> str | None:
    """Build an address string in the form CartoCiudad actually resolves.

    Including the postcode or the autonomous community makes the service return an empty
    result, verified against live responses - so the query is street + municipality only.
    """
    st = expand_street(street)
    muni = clean_text(municipality)
    if not st and not muni:
        return None
    if not st:
        return str(muni)
    return f"{st}, {muni}" if muni else st


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def clean_phones(value: Any) -> list[str]:
    """Extract Spanish phone numbers, tolerating ``;``/``/``/`` - `` separated lists."""
    if value is None:
        return []
    out: list[str] = []
    for chunk in re.split(r"[;,/|]| y | i ", str(value)):
        digits = re.sub(r"[^\d+]", "", chunk)
        if digits.startswith("00"):
            digits = "+" + digits[2:]
        core = digits.lstrip("+")
        if core.startswith("34") and len(core) == 11:
            core = core[2:]
        if 9 <= len(core) <= 11 and core.isdigit():
            out.append(f"+34{core}" if len(core) == 9 else f"+{core}")
    seen, uniq = set(), []
    for p in out:
        if p not in seen:
            seen.add(p); uniq.append(p)
    return uniq


def clean_emails(value: Any) -> list[str]:
    if value is None:
        return []
    found = _EMAIL_RE.findall(str(value).lower())
    seen, out = set(), []
    for e in found:
        if e not in seen:
            seen.add(e); out.append(e)
    return out


def clean_url(value: Any) -> str | None:
    s = clean_text(value)
    if not s or "." not in s:
        return None
    if not s.startswith(("http://", "https://")):
        s = "https://" + s.lstrip("/")
    return s if re.match(r"^https?://[\w.-]+\.\w{2,}", s) else None


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
def asset_id(source_id: str, source_ref: str) -> str:
    """Deterministic id, so re-ingesting a source updates rows instead of duplicating."""
    digest = hashlib.sha1(f"{source_id}::{source_ref}".encode("utf-8")).hexdigest()
    return f"tal_{digest[:20]}"


def point_wkt(lon: float, lat: float) -> str:
    return f"POINT({lon:.7f} {lat:.7f})"


def first(*values: Any) -> Any:
    for v in values:
        if v not in (None, "", []):
            return v
    return None
