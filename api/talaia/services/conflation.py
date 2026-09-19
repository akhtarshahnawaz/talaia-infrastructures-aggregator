"""Entity resolution across registries.

A hospital in Manresa appears in OpenStreetMap (with a phone number), in Equipaments de
Catalunya (with an official id) and in the Catalogo Nacional de Hospitales (with a bed
count). Returning it three times would inflate the apparent exposure threefold, which for
a system whose whole job is quantifying exposure is the worst possible failure.

The pipeline is block -> score -> merge:

1. **Block** on (category, ~150 m grid cell) plus the eight neighbouring cells, turning
   an O(n^2) comparison into something near-linear.
2. **Score** with normalised name similarity and metric distance, with hard identifiers
   (shared official code) short-circuiting to a match.
3. **Merge** into a cluster whose *primary* record is chosen by source priority, filling
   empty fields from the rest and recording which source contributed what.

Pairs that look similar but miss the threshold are never silently dropped: they stay as
separate assets carrying ``possible_duplicate_of``, so a human or an agent can decide.
"""
from __future__ import annotations

import logging
import math
from collections import defaultdict
from typing import Any, Iterable

from rapidfuzz import fuzz

from ..geo import haversine_m
from ..norm import name_key

log = logging.getLogger("talaia.conflation")

# Higher wins when electing the primary record of a cluster.
SOURCE_PRIORITY: dict[str, int] = {
    "es.msan.hospitales": 100,
    "es.cat.schools": 95,
    "es.cat.livestock": 92,
    "es.cat.reses": 88,
    # SIAP rather than REGCESS: REGCESS publishes no bulk extract, and SIAP is the same
    # Ministry's machine-readable directory of the same centres. See connectors/es/spain.py.
    "es.msan.siap": 86,
    "es.csic.carehomes": 82,
    "es.meq.schools": 80,
    "es.cat.equipaments": 78,
    "es.ine.popgrid": 70,
    "osm": 50,
    "es.cat.munipoints": 5,
}
DEFAULT_PRIORITY = 40

CELL_M = 150.0
# Thresholds are calibrated against real pairs from the Catalan registries and OSM.
# `fuzz.ratio` over the sorted name key scores true duplicates at ~100 ("Hospital de Sant
# Joan de Deu, S.A." vs "HOSPITAL SANT JOAN DE DEU") while scoring the dangerous
# near-misses at 77-82 ("Escola Pia" vs "Escola Pia Annex", "CEIP La Font" vs "CEIP La
# Font del Bou"). token_set_ratio cannot be used: it returns 100 whenever one token set
# is a subset of the other, which is exactly the annex case.
STRONG_NAME = 90.0   # at up to FAR_M
WEAK_NAME = 60.0     # only at very close range, where distance carries the evidence
NEAR_M = 40.0
FAR_M = 150.0
SAME_NAME_M = 400.0
SUGGEST_M = 250.0
SUGGEST_NAME = 72.0


def _cell(lon: float, lat: float) -> tuple[int, int]:
    deg_lat = CELL_M / 111_320.0
    deg_lon = deg_lat / max(math.cos(math.radians(lat)), 1e-6)
    return int(lon / deg_lon), int(lat / deg_lat)


class _Union:
    def __init__(self, n: int):
        self.p = list(range(n))

    def find(self, a: int) -> int:
        while self.p[a] != a:
            self.p[a] = self.p[self.p[a]]
            a = self.p[a]
        return a

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def _priority(source_id: str) -> int:
    return SOURCE_PRIORITY.get(source_id, DEFAULT_PRIORITY)


def _similarity(a: dict, b: dict) -> tuple[float, float]:
    """Return ``(name_similarity_0_100, distance_m)``."""
    dist = haversine_m(a.get("lon") or 0.0, a.get("lat") or 0.0,
                       b.get("lon") or 0.0, b.get("lat") or 0.0)
    ka, kb = a.get("_name_key") or "", b.get("_name_key") or ""
    if not ka or not kb:
        return 0.0, dist
    # name_key already sorts tokens, so `ratio` here behaves as a token-sort ratio
    # that still penalises tokens present in only one of the names.
    return float(fuzz.ratio(ka, kb)), dist


def _is_match(a: dict, b: dict, name_sim: float, dist: float) -> bool:
    # Hard identifier: same official code in the same source family.
    ra, rb = a.get("source_ref"), b.get("source_ref")
    if ra and ra == rb and a.get("source_id") == b.get("source_id"):
        return True
    if not a.get("_name_key") or not b.get("_name_key"):
        # Without names, only near-coincident points of the same subcategory merge.
        return dist <= NEAR_M and a.get("subcategory") == b.get("subcategory")
    if dist <= NEAR_M and name_sim >= WEAK_NAME:
        return True
    if dist <= FAR_M and name_sim >= STRONG_NAME:
        return True
    if dist <= SAME_NAME_M and name_sim >= 97.0:
        return True
    return False


def _merge_cluster(members: list[dict]) -> dict:
    """Elect a primary and fill its gaps from the others, tracking field provenance."""
    members = sorted(members, key=lambda m: (-_priority(m["source_id"]),
                                             -(m.get("confidence") or 0)))
    primary = dict(members[0])
    provenance: list[dict] = [{
        "source_id": primary["source_id"], "source_ref": primary.get("source_ref"),
        "retrieved_at": primary.get("retrieved_at"),
        "fields": ["identity", "geometry"],
    }]

    contacts = dict(primary.get("contacts") or {})
    contacts.setdefault("phone", list(contacts.get("phone") or []))
    contacts.setdefault("email", list(contacts.get("email") or []))
    capacity = dict(primary.get("capacity") or {})
    address = dict(primary.get("address") or {})
    attributes = dict(primary.get("attributes") or {})

    for other in members[1:]:
        gained: list[str] = []

        oc = other.get("contacts") or {}
        for phone in (oc.get("phone") or []):
            if phone not in contacts["phone"]:
                contacts["phone"].append(phone); gained.append("contacts.phone")
        for email in (oc.get("email") or []):
            if email not in contacts["email"]:
                contacts["email"].append(email); gained.append("contacts.email")
        if not contacts.get("website") and oc.get("website"):
            contacts["website"] = oc["website"]; gained.append("contacts.website")
        if not contacts.get("operator") and oc.get("operator"):
            contacts["operator"] = oc["operator"]; gained.append("contacts.operator")

        ocap = other.get("capacity") or {}
        for field in ("people", "beds", "students", "places", "animals",
                      "livestock_units", "dwellings"):
            if not capacity.get(field) and ocap.get(field):
                capacity[field] = ocap[field]
                capacity.setdefault("basis", ocap.get("basis"))
                gained.append(f"capacity.{field}")

        oaddr = other.get("address") or {}
        for field, val in oaddr.items():
            if val and not address.get(field):
                address[field] = val; gained.append(f"address.{field}")

        if not primary.get("name") and other.get("name"):
            primary["name"] = other["name"]; gained.append("name")
        # A measured footprint beats no footprint, whatever the source priority.
        if not primary.get("footprint_m2") and other.get("footprint_m2"):
            primary["footprint_m2"] = other["footprint_m2"]
            primary["geometry_kind"] = other.get("geometry_kind", primary.get("geometry_kind"))
            gained.append("footprint_m2")
        if not primary.get("floors") and other.get("floors"):
            primary["floors"] = other["floors"]; gained.append("floors")

        for k, v in (other.get("attributes") or {}).items():
            attributes.setdefault(f"{other['source_id']}:{k}" if k in attributes else k, v)

        provenance.append({
            "source_id": other["source_id"], "source_ref": other.get("source_ref"),
            "retrieved_at": other.get("retrieved_at"),
            "fields": sorted(set(gained)) or ["corroboration"],
        })

    primary["contacts"] = contacts
    primary["capacity"] = capacity
    primary["address"] = address
    primary["attributes"] = attributes
    primary["_provenance"] = provenance
    primary["merged_count"] = len(members)
    # Corroboration across independent registries is genuine evidence of existence.
    base = primary.get("confidence") or 0.5
    primary["confidence"] = round(min(0.98, base + 0.06 * (len(members) - 1)), 3)
    return primary


def conflate(assets: Iterable[dict]) -> tuple[list[dict], dict]:
    """Collapse duplicates. Returns ``(assets, stats)``."""
    items = [dict(a) for a in assets]
    for a in items:
        a["_name_key"] = name_key(a.get("name"))
    n = len(items)
    if n < 2:
        for a in items:
            a.setdefault("_provenance", [{
                "source_id": a["source_id"], "source_ref": a.get("source_ref"),
                "retrieved_at": a.get("retrieved_at"), "fields": ["identity", "geometry"]}])
            a.setdefault("merged_count", 1)
        return items, {"input": n, "output": n, "merged": 0, "suggested": 0}

    buckets: dict[tuple, list[int]] = defaultdict(list)
    for i, a in enumerate(items):
        lon, lat = a.get("lon"), a.get("lat")
        if lon is None or lat is None:
            continue
        buckets[(a["category"], *_cell(lon, lat))].append(i)

    uf = _Union(n)
    suggestions: dict[int, set[int]] = defaultdict(set)
    compared = 0

    # Walk assets, not buckets. Iterating buckets and pulling in their eight neighbours
    # revisits every neighbourhood up to nine times; anchoring on the asset and only
    # considering higher-indexed partners evaluates each pair exactly once, which is
    # where almost all of the conflation time was going on dense urban AOIs.
    cell_of: list[tuple | None] = [None] * n
    for i, a in enumerate(items):
        lon, lat = a.get("lon"), a.get("lat")
        if lon is not None and lat is not None:
            cell_of[i] = _cell(lon, lat)

    for i, a in enumerate(items):
        cell = cell_of[i]
        if cell is None:
            continue
        cx, cy = cell
        cat = a["category"]
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for j in buckets.get((cat, cx + dx, cy + dy), ()):
                    if j <= i:
                        continue
                    if uf.find(i) == uf.find(j):
                        continue
                    compared += 1
                    sim, dist = _similarity(a, items[j])
                    if _is_match(a, items[j], sim, dist):
                        uf.union(i, j)
                    elif dist <= SUGGEST_M and sim >= SUGGEST_NAME:
                        suggestions[i].add(j)
                        suggestions[j].add(i)

    clusters: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        clusters[uf.find(i)].append(i)

    out: list[dict] = []
    merged_count = 0
    # Map every input index to the id of the asset that survives for it, built as the
    # output is assembled. Searching `out` for each cluster instead made this step
    # quadratic and it dominated the whole request on dense AOIs.
    survivor_of: dict[int, str] = {}
    for root, idxs in clusters.items():
        if len(idxs) == 1:
            a = items[idxs[0]]
            a.setdefault("_provenance", [{
                "source_id": a["source_id"], "source_ref": a.get("source_ref"),
                "retrieved_at": a.get("retrieved_at"), "fields": ["identity", "geometry"]}])
            a["merged_count"] = 1
            out.append(a)
            survivor_of[idxs[0]] = a["id"]
        else:
            merged_count += len(idxs) - 1
            merged = _merge_cluster([items[i] for i in idxs])
            out.append(merged)
            for i in idxs:
                survivor_of[i] = merged["id"]

    for a in out:
        a.setdefault("possible_duplicate_of", [])
    by_id = {a["id"]: a for a in out}
    suggested = 0
    for i, others in suggestions.items():
        src = by_id.get(survivor_of.get(i, ""))
        if not src:
            continue
        for j in others:
            tgt = survivor_of.get(j)
            if tgt and tgt != src["id"] and tgt not in src["possible_duplicate_of"]:
                src["possible_duplicate_of"].append(tgt)
                suggested += 1

    for a in out:
        a.pop("_name_key", None)

    stats = {"input": n, "output": len(out), "merged": merged_count,
             "suggested": suggested, "comparisons": compared}
    log.debug("conflation: %s", stats)
    return out, stats
