"""National Spanish registries: column matching, classification and capacity.

Every test here runs against `normalise`, which is pure. The network-bound `fetch` half
is exercised by actually ingesting; what is worth locking down in a unit test is the
mapping, because that is where a silent change of meaning hides.
"""
import pytest

from talaia.connectors.es.spain import (CSIC_PROVINCES, CareHomes, NationalHospitals,
                                        NationalSchools, PrimaryCareCentres, _col,
                                        _field, _key)


# -- column matching --------------------------------------------------------
def test_accents_are_folded_before_stripping_punctuation():
    """Removing the accented character instead of folding it turns Denominación into
    'denominacin', which matches nothing - this silently emptied the care-home feed."""
    assert _key("Denominación") == "denominacion"
    assert _key("Código Postal") == "codigopostal"
    assert _key("TELÉFONO") == "telefono"
    assert _key("Última modificación:") == "ultimamodificacion"


def test_columns_match_across_accent_and_case_variation():
    names = {"DENOMINACIÓN": 0, "C. Postal": 1, "Teléfono ": 2}
    assert _col(names, "denominacion") == 0
    assert _col(names, "C Postal") == 1
    assert _col(names, "TELEFONO") == 2


def test_an_exact_column_wins_over_a_substring():
    """'Via' must not resolve to 'Nombre via' just because it appears inside it."""
    row = {"Nombre via": "Cervantes", "Via": "Calle", "Num. via": "2"}
    assert _field(row, "Via") == "Calle"
    assert _field(row, "Nombre via") == "Cervantes"


def test_a_missing_column_is_none_not_a_wrong_guess():
    assert _col({"A": 0, "B": 1}, "Camas") is None
    assert _field({"A": "x"}, "Plazas") is None


# -- hospitals --------------------------------------------------------------
def _hospital(**over):
    base = {"ccn": "1531000730", "cod": "310121", "name": "Hospital Garcia Orcoyen",
            "address": "Calle Santa Soria 22", "phone": "848435000",
            "municipality": "Estella-Lizarra", "province": "Navarra",
            "postcode": "31200", "beds": 108, "class_code": "C11",
            "class_name": "Hospitales Generales", "ownership": "Privados",
            "complex": None}
    base.update(over)
    return base


def test_hospital_beds_become_a_capacity_with_a_stated_basis():
    a = list(NationalHospitals().normalise(_hospital()))[0]
    assert a.subcategory == "hospital"
    assert a.capacity["beds"] == 108
    assert a.capacity["people"] > 108, "beds alone undercount: staff and visitors too"
    assert "beds" in a.capacity["basis"].lower()
    assert a.needs_geocoding is True, "the CNH publishes no coordinates"


def test_a_psychiatric_hospital_is_not_filed_as_a_general_one():
    a = list(NationalHospitals().normalise(_hospital(class_code="C14")))[0]
    assert a.subcategory == "mental_health"


def test_a_hospital_without_beds_gets_no_invented_capacity():
    a = list(NationalHospitals().normalise(_hospital(beds=None)))[0]
    assert a.capacity == {}


def test_a_nameless_row_is_dropped():
    assert list(NationalHospitals().normalise(_hospital(name=None))) == []


# -- primary care -----------------------------------------------------------
def _siap(**over):
    base = {"ccaa": "ANDALUCÍA", "province": "ALMERÍA", "area": "DAP ALMERÍA",
            "municipality": "Almería", "code": "104002011", "id": "132000",
            "kind": "CENTRO SALUD", "name": "ALBORAN",
            "address": "CALLE ALPUJARRAS, S/N", "postcode": "04007",
            "locality": "ALMERÍA", "phone": "950186640",
            "ownership": "Pública Directa", "manager": "SERVICIO ANDALUZ DE SALUD"}
    base.update(over)
    return base


def test_a_health_centre_and_a_village_surgery_are_different_things():
    """A consultorio local opens a few hours a week; collapsing the two would
    misreport the occupancy and the criticality of both."""
    centre = list(PrimaryCareCentres().normalise(_siap()))[0]
    surgery = list(PrimaryCareCentres().normalise(_siap(kind="CONSULTORIO LOCAL")))[0]
    assert centre.subcategory == "primary_care"
    assert surgery.subcategory == "clinic"


def test_primary_care_claims_no_capacity_it_does_not_have():
    a = list(PrimaryCareCentres().normalise(_siap()))[0]
    assert a.capacity == {}
    assert a.needs_geocoding is True


def test_siap_keeps_the_health_area_for_context():
    a = list(PrimaryCareCentres().normalise(_siap()))[0]
    assert a.attributes["health_area"] == "DAP ALMERÍA"
    assert a.address["region"] == "ANDALUCÍA"


# -- care homes -------------------------------------------------------------
def _csic(**over):
    base = {"Denominación": "Residencia de Mayores Don Quijote", "Via": "Calle",
            "Nombre via": "Cervantes", "Num. via": "2", "C. Postal": "02250",
            "Municipio": "Abengibre", "Teléfono": "967 47 15 64",
            "Titularidad": "Privada", "Plazas": "57", "URL": "www.sercoama.com",
            "Latitud": "39,211072", "Longitud": "-1,543861",
            "Última modificación:": "06/10/2022", "_province": "albacete"}
    base.update(over)
    return base


def test_care_home_coordinates_are_read_with_a_decimal_comma():
    """The file is published with European decimal separators; parsing them as
    thousands separators would put every home in the Gulf of Guinea."""
    a = list(CareHomes().normalise(_csic()))[0]
    assert a.lat == pytest.approx(39.211072)
    assert a.lon == pytest.approx(-1.543861)
    assert a.needs_geocoding is False, "coordinates are published, so do not geocode"


def test_a_care_home_without_coordinates_falls_back_to_geocoding():
    a = list(CareHomes().normalise(_csic(Latitud="", Longitud="")))[0]
    assert a.lon is None and a.needs_geocoding is True


def test_places_drive_an_occupancy_above_the_licensed_count():
    a = list(CareHomes().normalise(_csic()))[0]
    assert a.subcategory == "care_home"
    assert a.capacity["places"] == 57
    assert a.capacity["people"] > 57, "residents plus staff"
    assert a.attributes["ownership"] == "Privada"


def test_the_street_is_assembled_from_the_three_address_columns():
    a = list(CareHomes().normalise(_csic()))[0]
    assert "Cervantes" in a.address["street"] and "2" in a.address["street"]


def test_the_province_file_list_is_plausible():
    assert 50 <= len(CSIC_PROVINCES) <= 53
    assert len(set(CSIC_PROVINCES)) == len(CSIC_PROVINCES)


# -- schools ----------------------------------------------------------------
def _school(kind, name="Joaquín Tena Sicilia", locality="Abla"):
    return {"region": "ANDALUCÍA", "province": "Almería", "locality": locality,
            "kind": kind, "name": name, "code": "04000018",
            "ownership": "Centro público", "address": "San Segundo, 7",
            "postcode": "04510", "phone": "950359901"}


@pytest.mark.parametrize("kind,expected", [
    ("Colegio de Educación Infantil y Primaria", "primary_school"),
    ("Colexio de Educación Infantil e Primaria", "primary_school"),
    ("Col.legi Públic d'Educació Infantil i Primària", "primary_school"),
    ("Escuela Infantil", "nursery"),
    ("Centro Privado de Educación Infantil", "nursery"),
    ("Instituto de Educación Secundaria", "secondary_school"),
    ("Centro Privado de Educación Secundaria", "secondary_school"),
    ("Centro de Educación de Adultos", "vocational"),
    ("Centre de Formació d'Adults", "vocational"),
    ("Centro Público Integrado de Formación Profesional", "vocational"),
    ("Escuela Oficial de Idiomas", "vocational"),
    ("Conservatorio Profesional de Música", "music_school"),
    ("Escuela Privada de Música", "music_school"),
    ("Centro Privado de Educación Infantil Primaria y Secundaria", "school"),
    ("Centro Docente Privado", "school"),
])
def test_school_types_classify_across_three_languages(kind, expected):
    assert list(NationalSchools().normalise(_school(kind)))[0].subcategory == expected


def test_an_infant_and_primary_school_is_primary_not_a_nursery():
    """'educación infantil' is a substring of 'educación infantil y primaria', so a
    first-match-wins rule would file every CEIP in the country as a creche."""
    ceip = list(NationalSchools().normalise(
        _school("Colegio de Educación Infantil y Primaria")))[0]
    assert ceip.subcategory == "primary_school"


def test_a_blank_generic_type_still_classifies_from_the_name():
    a = list(NationalSchools().normalise(_school("", name="Instituto Cervantes")))[0]
    assert a.subcategory == "secondary_school"


def test_a_specific_name_that_is_only_the_town_gets_the_official_name():
    """'Centro de Educación de Adultos' + 'Abla' reads as a town on its own."""
    a = list(NationalSchools().normalise(
        _school("Centro de Educación de Adultos", name="Abla")))[0]
    assert a.name == "Centro de Educación de Adultos Abla"


def test_a_real_specific_name_is_left_alone_for_conflation():
    """The short name matches OpenStreetMap's far better than the official long form."""
    a = list(NationalSchools().normalise(
        _school("Colegio de Educación Infantil y Primaria")))[0]
    assert a.name == "Joaquín Tena Sicilia"


def test_schools_carry_no_enrolment_because_the_registry_has_none():
    a = list(NationalSchools().normalise(_school("Escuela Infantil")))[0]
    assert a.capacity == {}
    assert a.attributes["rcd_code"] == "04000018"


# -- shared contract --------------------------------------------------------
@pytest.mark.parametrize("cls", [NationalHospitals, PrimaryCareCentres, CareHomes,
                                 NationalSchools])
def test_every_national_connector_declares_spanish_coverage(cls):
    """Coverage is what rejects a mangled coordinate at ingest, so it must be set."""
    assert cls.coverage.label == "Spain"
    assert cls.meta.country == "ES"
    assert cls.meta.tier == "resident"
    assert cls.meta.limitations, "a source with no stated limitations is a source nobody checked"


@pytest.mark.parametrize("cls", [NationalHospitals, PrimaryCareCentres, CareHomes,
                                 NationalSchools])
def test_every_national_source_has_a_conflation_priority(cls):
    """Without an entry these sources silently sort below OpenStreetMap when merging."""
    from talaia.services.conflation import SOURCE_PRIORITY

    assert cls.meta.id in SOURCE_PRIORITY
    assert SOURCE_PRIORITY[cls.meta.id] > SOURCE_PRIORITY["osm"]


def test_every_published_province_file_is_listed():
    """Four slugs were guessed wrong on the first pass (lacoruna, larioja, santacruz,
    malaga) and each silently dropped a province's care homes - 49 of 52 files loaded
    and the only symptom was a warning nobody was reading."""
    assert "coruna" in CSIC_PROVINCES and "lacoruna" not in CSIC_PROVINCES
    assert "rioja" in CSIC_PROVINCES and "larioja" not in CSIC_PROVINCES
    assert "tenerife" in CSIC_PROVINCES and "santacruz" not in CSIC_PROVINCES
    assert "malaga" in CSIC_PROVINCES
    assert len(CSIC_PROVINCES) == 52, "50 provinces plus Ceuta and Melilla"
