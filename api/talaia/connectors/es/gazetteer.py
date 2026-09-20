"""Offline place-name geocoding for Spain.

Several registries publish an address and no coordinates, and care homes are the case
that matters: they are the highest-priority asset class in a wildfire evacuation and
would otherwise be invisible. Something has to turn those addresses into points.

Doing it by asking a geocoder once per address means ~55,000 HTTP requests against a free
public service run by the national mapping agency, which takes the best part of an hour
and is not a reasonable thing to do to somebody else's server. So the default is a bulk
file instead: GeoNames publishes every Spanish populated place - 8,124 municipalities
among them - as one 3.2 MB download, which resolves a municipality name to a point in
memory with no network at all.

**This is approximate and the approximation is the whole trade.** A municipality centroid
is not a building. In a village that is a few hundred metres; in Madrid it is several
kilometres, which at the scale of a fire perimeter is the difference between inside and
outside. Every point placed this way is marked ``geocode_match_type="municipality"`` with
a low quality score so it is distinguishable downstream, and the sources page says so in
plain language. For a production deployment making evacuation decisions, run the
street-level geocoder instead: set ``TALAIA_GEOCODE_MODE=hybrid``.
"""
from __future__ import annotations

import csv
import io
import logging
import zipfile
from typing import Any

from ...config import settings
from ...net import request
from ...norm import fold

log = logging.getLogger("talaia.es.gazetteer")

CACHE_KEY = "gazetteer:es:v2"

# GeoNames feature codes, best first. ADM3 is the Spanish municipality; the PPLA* codes
# are seats of administrative divisions; PPL is any populated place.
_RANK = {"ADM3": 0, "PPLC": 1, "PPLA": 2, "PPLA2": 3, "PPLA3": 4, "PPLA4": 5, "PPL": 6}

# A municipality centroid, on the geocoder's own 0-1 scale. Deliberately equal to what a
# CartoCiudad "municipio" match scores, because it is the same claim about the world.
MUNICIPALITY_QUALITY = 0.3

_MEMO: dict[str, tuple[float, float]] | None = None


def _parse(raw: bytes) -> dict[str, tuple[float, float]]:
    """Build folded-name -> point from the GeoNames country dump."""
    table: dict[str, tuple[float, float, int]] = {}
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        name = next(n for n in zf.namelist() if n.upper().endswith(".TXT")
                    and "readme" not in n.lower())
        text = io.TextIOWrapper(zf.open(name), encoding="utf-8", newline="")
        for row in csv.reader(text, delimiter="\t", quoting=csv.QUOTE_NONE):
            if len(row) < 15:
                continue
            code = row[7]
            rank = _RANK.get(code)
            if rank is None:
                continue
            try:
                lat, lon = float(row[4]), float(row[5])
            except ValueError:
                continue
            # The official name, plus the alternates - which is how "Lleida" and
            # "Lerida", or "Girona" and "Gerona", both resolve.
            names = [row[1], row[2], *(row[3].split(",") if row[3] else [])]
            for candidate in names:
                key = fold(candidate)
                if not key or len(key) < 3:
                    continue
                previous = table.get(key)
                if previous is None or rank < previous[2]:
                    table[key] = (lon, lat, rank)
    return {k: (v[0], v[1]) for k, v in table.items()}


async def load(store=None) -> dict[str, tuple[float, float]]:
    """The name->point table, downloaded once and then cached on the volume."""
    global _MEMO
    if _MEMO is not None:
        return _MEMO
    if store is not None:
        cached = await store.cache_get(CACHE_KEY)
        if cached:
            _MEMO = {k: (v[0], v[1]) for k, v in cached.items()}
            log.info("gazetteer: %s names from cache", f"{len(_MEMO):,}")
            return _MEMO

    log.info("gazetteer: downloading %s", settings.geonames_url)
    resp = await request("GET", settings.geonames_url, timeout=120.0)
    table = _parse(resp.content)
    log.info("gazetteer: %s place names parsed", f"{len(table):,}")
    if store is not None:
        await store.cache_put(CACHE_KEY, "gazetteer",
                              {k: list(v) for k, v in table.items()})
    _MEMO = table
    return table


def reset() -> None:
    """Drop the in-process copy. For tests."""
    global _MEMO
    _MEMO = None


def _variants(value: Any) -> list[str]:
    """Spellings to try for one place name, most specific first.

    Spain names a lot of places twice. The registries write the province as
    ``Alicante/Alacant``, ``Araba/Álava``, ``Valencia/València``; municipalities arrive as
    ``Palma de Mallorca, Illes Balears`` or with the article moved to the end, as in
    ``Seu d'Urgell, la``. Folding the whole string produces something that matches
    nothing, and the record is then dropped for want of a coordinate.
    """
    text = str(value or "").strip()
    if not text:
        return []
    out: list[str] = []

    def add(candidate: str) -> None:
        key = fold(candidate)
        if key and len(key) >= 3 and key not in out:
            out.append(key)

    add(text)
    # "Municipality, Province" -> the municipality.
    head = text.split(",")[0]
    add(head)
    # "Seu d'Urgell, la" -> "la Seu d'Urgell".
    if "," in text:
        first, _, rest = text.partition(",")
        article = rest.strip()
        if article and len(article) <= 4:
            add(f"{article} {first.strip()}")
    # "Alicante/Alacant" -> each name on its own.
    for part in head.replace(" - ", "/").split("/"):
        add(part)
    return out


def lookup(table: dict[str, tuple[float, float]], municipality: Any,
           province: Any = None) -> dict[str, Any] | None:
    """Resolve a place name to a point, in the shape the geocoder returns.

    Tries the municipality, then the province - a province centroid is a poor answer but
    a better one than dropping a care home from the inventory entirely.
    """
    for value, match in ((municipality, "municipality"), (province, "province")):
        point = None
        for key in _variants(value):
            point = table.get(key)
            if point:
                break
        if point:
            return {
                "lon": point[0], "lat": point[1],
                "quality": MUNICIPALITY_QUALITY if match == "municipality" else 0.1,
                "match_type": match,
                "geocoder": "geonames-offline",
                "approximate": True,
            }
    return None
