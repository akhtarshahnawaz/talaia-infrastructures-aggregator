"""Taxonomy, valuation, scoring and conflation - the judgement-carrying parts."""
import json

import pytest

from talaia import taxonomy as tax
from talaia.services.conflation import conflate
from talaia.services.scoring import people_estimate, priority
from talaia.services.valuation import estimate


# -- taxonomy ---------------------------------------------------------------
def test_taxonomy_is_internally_consistent():
    assert len(tax.CATEGORIES) == 17
    for sub in tax.SUBCATEGORIES.values():
        assert sub.category in tax.CATEGORIES, sub.key
        assert 0 <= sub.vulnerability <= 100
        assert 0 <= sub.criticality <= 100
    for key in tax.CATEGORIES:
        assert tax.subcategories_for(key), f"{key} has no subcategories"
    for key in tax.HAZARDOUS | tax.RESPONSE_ASSETS:
        assert key in tax.SUBCATEGORIES
    json.dumps(tax.as_dict())  # must be serialisable for /v1/taxonomy


def test_layer_resolution_accepts_categories_and_subcategories():
    assert tax.resolve_layers(["healthcare", "campsite"]) == {"healthcare", "tourism"}
    assert tax.resolve_layers(["all"]) == set(tax.CATEGORIES)
    assert tax.resolve_layers(None) == set(tax.CATEGORIES)
    assert tax.resolve_layers(["nonsense"]) == set(tax.CATEGORIES)


def test_unknown_subcategory_degrades_instead_of_raising():
    assert tax.get_subcategory("does_not_exist").key == "other"
    assert tax.get_subcategory(None).key == "other"


# -- valuation ---------------------------------------------------------------
def test_measured_footprint_beats_defaults_and_says_so():
    measured = estimate(subcategory="hospital", footprint_m2=4200, floors=3,
                        address={"province": "Barcelona"}, geometry_kind="footprint")
    defaulted = estimate(subcategory="hospital", address={"province": "Barcelona"})
    assert measured.confidence > defaulted.confidence
    assert defaulted.confidence <= 0.35
    assert "measured_footprint" in measured.method
    assert any("measured from building polygon" in a for a in measured.assumptions)


def test_regional_multiplier_applies():
    bcn = estimate(subcategory="house", footprint_m2=140, floors=2,
                   address={"province": "Barcelona"}, geometry_kind="footprint")
    teruel = estimate(subcategory="house", footprint_m2=140, floors=2,
                      address={"province": "Teruel"}, geometry_kind="footprint")
    assert bcn.total_eur > teruel.total_eur


def test_unvaluable_classes_return_zero_not_a_guess():
    for sub in ("motorway", "power_line", "forest", "hydrant"):
        v = estimate(subcategory=sub)
        assert v.total_eur == 0
        assert v.method == "not_valued"


def test_livestock_is_priced_per_head_not_per_unit():
    """280,485 laying hens must not be valued like 280,485 cattle."""
    v = estimate(subcategory="poultry", capacity={"animals": 280485},
                 attributes={"species_head_capacity": {"Gallines i pollastres": 280485}})
    assert v.total_eur < 10_000_000, "hens priced as livestock units would give ~196M"
    assert "livestock_per_head" in v.method
    cattle = estimate(subcategory="cattle", capacity={"animals": 100},
                      attributes={"species_head_capacity": {"Boví": 100}})
    # One cow is worth far more than one hen.
    assert cattle.livestock_eur / 100 > v.livestock_eur / 280485 * 50


# -- scoring -----------------------------------------------------------------
def test_occupancy_flags_class_defaults():
    people, basis, is_default = people_estimate("primary_school", {"students": 400})
    assert people == 400 and not is_default
    people, basis, is_default = people_estimate("primary_school", {})
    assert is_default and "class default" in basis


def test_priority_favours_life_safety_and_urgency():
    kwargs = dict(total_value_eur=1_000_000, band_index=0, n_bands=3)
    care = priority(subcategory="care_home", people=90, band_minutes=30, **kwargs)
    forest = priority(subcategory="forest", people=0, band_minutes=30, **kwargs)
    assert care > forest
    now = priority(subcategory="care_home", people=90, band_minutes=0,
                   total_value_eur=1_000_000, band_index=0, n_bands=3)
    later = priority(subcategory="care_home", people=90, band_minutes=1440,
                     total_value_eur=1_000_000, band_index=2, n_bands=3)
    assert now > later


def test_hazardous_sites_are_boosted():
    common = dict(people=8, total_value_eur=900_000, band_minutes=60,
                  band_index=1, n_bands=3)
    assert priority(subcategory="fuel_station", **common) > \
           priority(subcategory="shop", **common)


# -- conflation ---------------------------------------------------------------
def _asset(i, src, name, lon, lat, cat="healthcare", sub="hospital", **kw):
    d = {"id": i, "source_id": src, "source_ref": i, "category": cat, "subcategory": sub,
         "name": name, "lon": lon, "lat": lat, "contacts": {}, "capacity": {},
         "address": {}, "attributes": {}, "confidence": 0.6}
    d.update(kw)
    return d


def test_three_registries_collapse_to_one_with_field_provenance():
    rows = [
        _asset("1", "osm", "Hospital de Sant Joan de Déu", 1.8200, 41.7300,
               contacts={"phone": ["+34938001122"], "email": []},
               footprint_m2=4200, geometry_kind="footprint"),
        _asset("2", "es.cat.equipaments", "HOSPITAL SANT JOAN DE DEU", 1.82008, 41.73005,
               address={"municipality": "Manresa"}),
        _asset("3", "es.msan.hospitales", "Hospital Sant Joan de Deu", 1.8201, 41.7301,
               capacity={"beds": 120}),
    ]
    out, stats = conflate(rows)
    assert len(out) == 1 and stats["merged"] == 2
    merged = out[0]
    # The official registry wins identity; the others contribute what they uniquely have.
    assert merged["source_id"] == "es.msan.hospitales"
    assert merged["capacity"]["beds"] == 120
    assert merged["contacts"]["phone"] == ["+34938001122"]
    assert merged["footprint_m2"] == 4200
    assert merged["address"]["municipality"] == "Manresa"
    assert {p["source_id"] for p in merged["_provenance"]} == {
        "osm", "es.cat.equipaments", "es.msan.hospitales"}
    assert merged["confidence"] > 0.6, "corroboration should raise confidence"


def test_an_annex_is_not_merged_into_its_parent():
    """token_set_ratio scores these 100; the calibrated scorer must not merge them."""
    rows = [_asset("6", "osm", "Escola Pia", 1.8300, 41.7400, cat="education", sub="school"),
            _asset("7", "osm", "Escola Pia Annex", 1.8318, 41.7402, cat="education",
                   sub="school")]
    out, stats = conflate(rows)
    assert len(out) == 2 and stats["merged"] == 0
    assert any(a["possible_duplicate_of"] for a in out), "near-misses must be flagged"


def test_distant_namesakes_are_not_merged():
    rows = [_asset("a", "osm", "Farmàcia Roca", 1.90, 41.80, sub="pharmacy"),
            _asset("b", "osm", "Farmàcia Roca", 2.40, 41.90, sub="pharmacy")]
    out, _ = conflate(rows)
    assert len(out) == 2


def test_conflation_is_a_noop_on_a_single_asset():
    out, stats = conflate([_asset("1", "osm", "X", 1.0, 41.0)])
    assert len(out) == 1 and out[0]["merged_count"] == 1 and out[0]["_provenance"]


# ---------------------------------------------------------------------------
# Population grid: the per-cell breakdown
# ---------------------------------------------------------------------------
from talaia.geo import Band  # noqa: E402
from talaia.services.population import MAX_CELLS, _build_cells  # noqa: E402
from shapely.geometry import box as _box  # noqa: E402


def _row(cell_id, lon, lat, pop, frac=1.0, area_m2=1e6):
    return {"cell_id": cell_id, "lon": lon, "lat": lat, "population": pop,
            "frac": frac, "area_m2": area_m2,
            "geojson": '{"type":"Polygon","coordinates":[[[0,0],[0,1],[1,1],[1,0],[0,0]]]}'}


def test_a_partly_covered_cell_contributes_only_its_overlap():
    cells, _, _ = _build_cells([_row("a", 2.0, 41.0, 1000, frac=0.25)], [],
                               with_geometry=False)
    assert cells[0].population == 1000
    assert cells[0].population_in_aoi == 250


def test_density_uses_the_whole_cell_not_the_clipped_part():
    """A cell half inside the AOI is the same neighbourhood, half observed. Scaling its
    density by the overlap would invent a gradient at the AOI edge."""
    full, _, _ = _build_cells([_row("a", 2.0, 41.0, 1000, frac=1.0)], [],
                              with_geometry=False)
    half, _, _ = _build_cells([_row("a", 2.0, 41.0, 1000, frac=0.5)], [],
                              with_geometry=False)
    assert full[0].density_per_km2 == half[0].density_per_km2 == 1000


def test_cells_come_back_densest_first():
    rows = [_row("a", 2.0, 41.0, 100), _row("b", 2.1, 41.0, 900),
            _row("c", 2.2, 41.0, 500)]
    cells, _, peak = _build_cells(rows, [], with_geometry=False)
    assert [c.cell_id for c in cells] == ["b", "c", "a"]
    assert peak == 900


def test_truncation_keeps_the_densest_cells():
    rows = [_row(f"c{i}", 2.0 + i / 1000, 41.0, float(i))
            for i in range(MAX_CELLS + 50)]
    cells, truncated, _ = _build_cells(rows, [], with_geometry=False)
    assert truncated is True
    assert len(cells) == MAX_CELLS
    assert cells[0].population == float(MAX_CELLS + 49), "densest survived the cut"


def test_geometry_is_omitted_unless_asked_for():
    without, _, _ = _build_cells([_row("a", 2.0, 41.0, 10)], [], with_geometry=False)
    with_geom, _, _ = _build_cells([_row("a", 2.0, 41.0, 10)], [], with_geometry=True)
    assert without[0].geometry is None
    assert with_geom[0].geometry["type"] == "Polygon"


def test_a_cell_is_labelled_with_the_earliest_band_reaching_it():
    bands = [Band(label="0-1h", index=0, geometry=_box(1.9, 40.9, 2.1, 41.1), minutes=60),
             Band(label="1-3h", index=1, geometry=_box(1.5, 40.5, 2.5, 41.5), minutes=180)]
    rows = [_row("inner", 2.0, 41.0, 10), _row("outer", 2.3, 41.3, 10),
            _row("beyond", 3.0, 42.0, 10)]
    cells, _, _ = _build_cells(rows, bands, with_geometry=False)
    got = {c.cell_id: c.band for c in cells}
    assert got["inner"] == "0-1h", "earliest band wins, not the largest"
    assert got["outer"] == "1-3h"
    assert got["beyond"] is None, "a centroid outside every band gets no label"


def test_an_unbanded_aoi_leaves_every_cell_unlabelled():
    single = [Band(label="aoi", index=0, geometry=_box(1.9, 40.9, 2.1, 41.1))]
    cells, _, _ = _build_cells([_row("a", 2.0, 41.0, 10)], single, with_geometry=False)
    assert cells[0].band is None


def test_rows_without_geometry_or_overlap_are_skipped():
    rows = [_row("ok", 2.0, 41.0, 10),
            {**_row("nofrac", 2.0, 41.0, 10), "frac": None},
            {**_row("nolon", 2.0, 41.0, 10), "lon": None}]
    cells, _, _ = _build_cells(rows, [], with_geometry=False)
    assert [c.cell_id for c in cells] == ["ok"]


# ---------------------------------------------------------------------------
# Geocoding a batch
# ---------------------------------------------------------------------------
async def test_a_batch_resolves_each_distinct_address_only_once(monkeypatch):
    """Registry addresses repeat heavily, and every worker reads the cache before any
    of them writes it - so without dedup each duplicate costs a live request. This was
    a third of the traffic on the national school load."""
    import talaia.connectors.es.cartociudad as cc

    calls: list[str] = []

    async def fake(query, store=None):
        calls.append(query)
        return {"lon": 1.0, "lat": 41.0, "query": query}

    monkeypatch.setattr(cc, "geocode", fake)
    queries = ["A", "B", "A", "A", "C", "B"]
    out = await cc.geocode_many(queries, None)

    assert sorted(calls) == ["A", "B", "C"], "one request per distinct address"
    assert [r["query"] for r in out] == queries, "results map back to every position"


async def test_blank_queries_are_not_sent_upstream(monkeypatch):
    import talaia.connectors.es.cartociudad as cc

    calls: list[str] = []

    async def fake(query, store=None):
        calls.append(query)
        return {"lon": 1.0, "lat": 41.0}

    monkeypatch.setattr(cc, "geocode", fake)
    out = await cc.geocode_many(["", "X", ""], None)
    assert calls == ["X"]
    assert out[0] is None and out[2] is None and out[1] is not None
