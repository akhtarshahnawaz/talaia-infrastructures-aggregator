"""Normalisers. Each case here is a real failure mode observed in the source data."""
import pytest

from talaia.geo import haversine_m
from talaia.norm import (asset_id, clean_emails, clean_phones, clean_url, expand_street,
                         fold, geocode_query, laea_to_wgs84, name_key, parse_dms,
                         parse_grid_id, to_float, utm_to_wgs84, valid_lonlat)


def test_utm_matches_published_coordinates():
    """The Catalan school registry publishes both UTM and lon/lat for each record;
    our inverse must reproduce its own lon/lat."""
    lon, lat = utm_to_wgs84(438227, 4655818, 31)
    assert haversine_m(lon, lat, 2.253505, 42.05198) < 5.0


def test_dms_parsing_prevents_a_100km_error():
    """Livestock coordinates are DMS strings; float() silently yields the degree part."""
    lat = parse_dms("42.0º 8.0' 33.0498''")
    assert lat == pytest.approx(42.14251, abs=1e-4)
    assert parse_dms("1.0º 4.0' 16.269''") == pytest.approx(1.07119, abs=1e-4)
    # The naive reading is wrong by ~16 km, which is why the parser exists.
    assert haversine_m(1.07119, 42.14251, 1.07119, 42.0) > 15_000


def test_dms_passes_through_decimals_and_rejects_junk():
    assert parse_dms("41.3872") == pytest.approx(41.3872)
    assert parse_dms("") is None
    assert parse_dms(None) is None


def test_laea_inverse_places_barcelona():
    """Grid cell 1kmN2064E3660 is the densest Spanish census cell: central Barcelona."""
    e, n, size = parse_grid_id("1kmN2064E3660")
    assert (e, n, size) == (3_660_000.0, 2_064_000.0, 1000.0)
    lon, lat = laea_to_wgs84(e + size / 2, n + size / 2)
    assert haversine_m(lon, lat, 2.1734, 41.3851) < 8_000


def test_numeric_parsing_handles_spanish_locale():
    assert to_float("1.234,5") == 1234.5
    assert to_float("1,5") == 1.5
    assert to_float("") is None
    assert to_float("n/a") is None


def test_null_island_is_rejected():
    assert valid_lonlat(0, 0) is None
    assert valid_lonlat(2.25, 42.05) == (2.25, 42.05)
    assert valid_lonlat(999, 42) is None


def test_phone_and_email_cleanup():
    assert clean_phones("938592793") == ["+34938592793"]
    assert clean_phones("93 859 27 93 / 0034938592794") == ["+34938592793", "+34938592794"]
    assert clean_phones(None) == []
    assert clean_emails("A8068033@xtec.cat") == ["a8068033@xtec.cat"]
    assert clean_url("www.escola.cat") == "https://www.escola.cat"
    assert clean_url("not a url") is None


def test_name_key_matches_across_accents_case_and_legal_forms():
    assert name_key("Hospital de Sant Joan de Déu, S.A.") == name_key("HOSPITAL SANT JOAN DEU")
    assert fold("Instal·lacions esportives") == "installacions esportives"


def test_street_expansion_is_what_makes_geocoding_work():
    assert expand_street("Pça.de l'Hospital, 2") == "Plaça de l'Hospital 2"
    assert expand_street("Ctr.de Camprodon, 9") == "Carretera de Camprodon 9"
    assert expand_street("C. Creueta, 51") == "Carrer Creueta 51"
    # CartoCiudad returns nothing when the postcode or region is included.
    q = geocode_query("C. Portal Nou, 12", "Girona")
    assert q == "Carrer Portal Nou 12, Girona"
    assert "17004" not in q and "Catalunya" not in q


def test_asset_ids_are_deterministic():
    assert asset_id("es.cat.schools", "8068033") == asset_id("es.cat.schools", "8068033")
    assert asset_id("es.cat.schools", "1") != asset_id("osm", "1")
