"""Exposure scoring: turn heterogeneous assets into one comparable triage ranking.

The score answers "what should the incident commander look at first?", which is not the
same as "what is worth the most". Life safety dominates, time-to-impact sharpens it, and
monetary value only breaks ties. Weights are explicit and documented so the output can be
argued with rather than taken on faith.
"""
from __future__ import annotations

import math
from typing import Any

from ..taxonomy import HAZARDOUS, RESPONSE_ASSETS, get_subcategory

WEIGHTS = {
    "people": 0.38,        # how many are exposed, log-scaled
    "criticality": 0.24,   # consequence of losing the asset class
    "vulnerability": 0.14, # likelihood fire destroys it
    "urgency": 0.16,       # how soon the front arrives
    "value": 0.08,         # replacement cost, log-scaled
}


def _log_scale(value: float, full: float) -> float:
    """Map 0..full onto 0..1 logarithmically, so 10 vs 100 people matters more than
    1000 vs 1090."""
    if value <= 0:
        return 0.0
    return min(1.0, math.log10(1 + value) / math.log10(1 + full))


def people_estimate(subcategory: str, capacity: dict[str, Any] | None
                    ) -> tuple[float, str, bool]:
    """Best available occupancy estimate.

    Returns ``(people, basis, is_default)``. The third value matters: an estimate derived
    from a class default is a guess, and a total that silently blends registry figures
    with guesses is not something an incident commander should act on. The report keeps
    the two apart.
    """
    spec = get_subcategory(subcategory)
    cap = capacity or {}
    for field, label in (("people", "capacity.people"), ("beds", "registered beds"),
                         ("students", "enrolment"), ("places", "registered places")):
        val = cap.get(field)
        if val:
            return float(val), cap.get("basis") or label, False
    if spec.people > 0:
        return spec.people, f"class default for '{spec.label}' (no registry figure)", True
    return 0.0, "no occupancy data", False


def urgency(band_minutes: float | None, band_index: int | None,
            n_bands: int) -> float:
    """1.0 for the fire front now, decaying with time to arrival."""
    if band_minutes is not None:
        # 0 min -> 1.0, 60 min -> ~0.7, 6 h -> ~0.35, 24 h -> ~0.15
        return 1.0 / (1.0 + (max(band_minutes, 0.0) / 120.0) ** 0.8)
    if band_index is None or n_bands <= 1:
        return 0.6
    return 1.0 - (band_index / max(n_bands - 1, 1)) * 0.7


def priority(*, subcategory: str, people: float, total_value_eur: float,
             band_minutes: float | None, band_index: int | None, n_bands: int,
             vulnerability: int | None = None,
             criticality: int | None = None) -> float:
    """Composite 0-100 triage score."""
    spec = get_subcategory(subcategory)
    vul = (vulnerability if vulnerability is not None else spec.vulnerability) / 100.0
    crit = (criticality if criticality is not None else spec.criticality) / 100.0

    score = (
        WEIGHTS["people"] * _log_scale(people, 1000.0)
        + WEIGHTS["criticality"] * crit
        + WEIGHTS["vulnerability"] * vul
        + WEIGHTS["urgency"] * urgency(band_minutes, band_index, n_bands)
        + WEIGHTS["value"] * _log_scale(total_value_eur, 50_000_000.0)
    )
    # A hazardous site threatens responders and neighbours, not just itself.
    if spec.key in HAZARDOUS:
        score = min(1.0, score * 1.25)
    # Losing a fire station mid-incident removes response capacity.
    if spec.key in RESPONSE_ASSETS:
        score = min(1.0, score * 1.10)
    return round(score * 100.0, 1)
