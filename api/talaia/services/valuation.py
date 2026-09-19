"""Replacement-cost valuation.

Fire risk management needs money as a triage signal, so TALAIA estimates what it would
cost to rebuild an asset - not what it would sell for. Market value is dominated by land,
which does not burn; replacement cost is what a recovery budget or an insurer faces.

    structure = footprint_m2 x floors x unit_cost(subcategory) x province_multiplier
    contents  = structure x contents_ratio(subcategory)
    livestock = livestock_units x value_per_unit(species)
    total     = structure + contents + livestock

Every estimate reports the branch that produced it (``method``), a ``confidence`` that
decays as we fall back to defaults, and the ``assumptions`` in plain language. These are
parametric triage figures, NOT appraisals, and the API says so on every response.
"""
from __future__ import annotations

from typing import Any

from ..models import Valuation
from ..taxonomy import get_subcategory

# Regional construction-cost variation. Barcelona builds dearer than Terol.
PROVINCE_MULTIPLIER: dict[str, float] = {
    "barcelona": 1.15, "girona": 1.08, "tarragona": 1.00, "lleida": 0.95,
    "madrid": 1.18, "guipuzcoa": 1.12, "vizcaya": 1.10, "baleares": 1.20,
    "malaga": 1.05, "valencia": 1.00, "sevilla": 0.98, "zaragoza": 0.96,
    "asturias": 0.95, "navarra": 1.05, "cantabria": 0.98, "alava": 1.05,
    "murcia": 0.93, "almeria": 0.92, "badajoz": 0.88, "caceres": 0.88,
    "teruel": 0.88, "soria": 0.88, "cuenca": 0.89, "jaen": 0.90,
}
DEFAULT_MULTIPLIER = 1.0

# Replacement value per LIVESTOCK UNIT (LSU), EUR. One LSU is roughly one adult bovine,
# so these are cow-equivalent values and must only ever be applied to LSU, never to a
# raw headcount - 280,000 hens are ~2,800 LSU, not 280,000.
LIVESTOCK_UNIT_VALUE: dict[str, float] = {
    "cattle": 1500.0, "pigs": 500.0, "poultry": 600.0, "sheep_goats": 1200.0,
    "horses": 3800.0, "rabbits": 750.0, "aquaculture": 1000.0,
    "mixed_livestock": 1100.0, "apiculture": 250.0, "animal_shelter": 900.0,
}
DEFAULT_LIVESTOCK_UNIT_VALUE = 1100.0

# Per-head replacement value by species group, EUR. Preferred when a species breakdown
# is available, because it prices a hen as a hen.
LIVESTOCK_HEAD_VALUE: dict[str, float] = {
    "cattle": 1200.0, "pigs": 150.0, "poultry": 6.0, "sheep_goats": 120.0,
    "horses": 3000.0, "rabbits": 15.0, "apiculture": 180.0, "aquaculture": 8.0,
    "animal_shelter": 400.0, "mixed_livestock": 300.0,
}


def _multiplier(address: dict[str, Any] | None) -> tuple[float, str]:
    if not address:
        return DEFAULT_MULTIPLIER, "national average"
    from ..norm import fold
    for key in ("province", "comarca", "region", "municipality"):
        val = fold(address.get(key))
        if val and val in PROVINCE_MULTIPLIER:
            return PROVINCE_MULTIPLIER[val], f"{address.get(key)} cost index"
    return DEFAULT_MULTIPLIER, "national average"


def estimate(*, subcategory: str, footprint_m2: float | None = None,
             floors: float | None = None, address: dict[str, Any] | None = None,
             capacity: dict[str, Any] | None = None,
             attributes: dict[str, Any] | None = None,
             geometry_kind: str = "point") -> Valuation:
    """Value one asset. Never raises; an unvaluable asset returns a zeroed Valuation."""
    spec = get_subcategory(subcategory)
    assumptions: list[str] = []
    confidence = 0.8
    method_parts: list[str] = []

    # -- footprint ---------------------------------------------------------
    if footprint_m2 and footprint_m2 > 0:
        area = float(footprint_m2)
        if geometry_kind == "footprint":
            assumptions.append(f"footprint {area:,.0f} m2 measured from building polygon")
            method_parts.append("measured_footprint")
        else:
            assumptions.append(f"footprint {area:,.0f} m2 from source attributes")
            method_parts.append("source_footprint")
            confidence -= 0.1
    elif spec.footprint > 0:
        area = spec.footprint
        assumptions.append(
            f"footprint {area:,.0f} m2 assumed (class default for '{spec.label}')")
        method_parts.append("default_footprint")
        confidence = min(confidence, 0.35)
    else:
        area = 0.0

    # -- floors ------------------------------------------------------------
    if floors and floors > 0:
        n_floors = float(floors)
        assumptions.append(f"{n_floors:g} storeys from source")
        method_parts.append("source_floors")
    else:
        n_floors = spec.floors
        if area > 0:
            assumptions.append(f"{n_floors:g} storeys assumed (class default)")
            confidence -= 0.08

    mult, mult_label = _multiplier(address)
    if mult != DEFAULT_MULTIPLIER:
        assumptions.append(f"regional cost index {mult:.2f} ({mult_label})")

    # -- structure & contents ----------------------------------------------
    structure = 0.0
    contents = 0.0
    if area > 0 and spec.unit_cost > 0:
        gross = area * n_floors
        structure = gross * spec.unit_cost * mult
        contents = structure * spec.contents_ratio
        assumptions.append(
            f"{spec.unit_cost:,.0f} EUR/m2 replacement cost for '{spec.label}'")
        if spec.contents_ratio:
            assumptions.append(
                f"contents at {spec.contents_ratio:.0%} of structure value")

    # -- livestock ----------------------------------------------------------
    livestock = 0.0
    cap = capacity or {}
    units = cap.get("livestock_units")
    animals = cap.get("animals")

    # Preferred path: price each species at its own per-head value.
    breakdown = (attributes or {}).get("species_head_capacity") or {}
    if breakdown:
        from ..connectors.es.catalunya import CatLivestock
        for species, count in breakdown.items():
            group, _coeff, per_head = CatLivestock.species_info(species)
            livestock += float(count or 0) * LIVESTOCK_HEAD_VALUE.get(group, per_head)
        if livestock:
            total_head = sum(float(v or 0) for v in breakdown.values())
            assumptions.append(
                f"{total_head:,.0f} head across {len(breakdown)} species, priced per "
                f"species (not per livestock unit)")
            method_parts.append("livestock_per_head")
    elif units:
        per = LIVESTOCK_UNIT_VALUE.get(spec.key, DEFAULT_LIVESTOCK_UNIT_VALUE)
        livestock = float(units) * per
        assumptions.append(
            f"{float(units):,.0f} livestock units at {per:,.0f} EUR/unit")
        method_parts.append("livestock_units")
    elif animals and spec.category == "livestock":
        per_head = LIVESTOCK_UNIT_VALUE.get(spec.key, DEFAULT_LIVESTOCK_UNIT_VALUE) / 6.0
        livestock = float(animals) * per_head
        assumptions.append(
            f"{float(animals):,.0f} animals at {per_head:,.0f} EUR/head (unit data absent)")
        method_parts.append("livestock_headcount")
        confidence -= 0.15

    total = structure + contents + livestock
    if total <= 0:
        return Valuation(method="not_valued", confidence=0.0,
                         assumptions=["No replacement-cost model applies to this asset "
                                      "class (e.g. roads, forests, hydrants)."])

    return Valuation(
        replacement_cost_eur=round(structure, 2),
        contents_eur=round(contents, 2),
        livestock_eur=round(livestock, 2),
        total_eur=round(total, 2),
        method="+".join(method_parts) or "class_default",
        confidence=round(max(0.05, min(confidence, 0.9)), 2),
        assumptions=assumptions,
    )
