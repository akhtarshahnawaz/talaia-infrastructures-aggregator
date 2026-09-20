"""Spain-wide registries: hospitals, primary care, care homes and schools.

These are the national counterparts to the Catalan feeds in ``catalunya.py``. Outside
Catalonia they are the difference between "OpenStreetMap happened to tag this" and a
published registry entry, and two of them carry the capacity figures that dominate the
people-at-risk estimate: hospital beds and care-home places.

Four sources, and what each actually costs to load:

======================  ========  ==========  ===============================
source                  rows      geometry    note
======================  ========  ==========  ===============================
es.msan.hospitales      ~850      geocoded    CAMAS = staffed beds
es.msan.siap            ~13,400   geocoded    primary care + urgent care
es.csic.carehomes       ~5,400    published   plazas AND lat/lon already
es.meq.schools          ~35,300   geocoded    no enrolment - see below
======================  ========  ==========  ===============================

**Geocoding is the cost here.** Only the CSIC file ships coordinates; the other three
publish addresses. Resolving those against CartoCiudad means one HTTP request per
address - ~55,000 of them against a free service run by the national mapping agency -
which took roughly 75 minutes for a cold national load and is not a reasonable thing to
do to somebody else's server.

So the default is offline: one 3.2 MB place-name table resolves a municipality to a
point in memory, and the whole national school registry loads in under a minute with no
geocoding traffic at all. The price is precision. A municipality centroid is not a
building, and at the scale of a fire perimeter that can put an asset on the wrong side
of the line, so every such point is marked ``geocode_approximate`` and the sources page
says so. A deployment making real evacuation decisions should set
``TALAIA_GEOCODE_MODE=hybrid``, which keeps the offline pass and sends only what it
could not place to the street-level geocoder.

**On REGCESS.** REGCESS is the legal register of health centres, but it publishes no bulk
extract - it is a Struts search application with a CSRF token, AJAX-populated dropdowns,
and it returns HTTP 500 to scripted queries. SIAP, the Ministry's own Catálogo de Centros
de Atención Primaria, is the same Ministry's machine-readable directory of exactly those
centres, so that is what this loads. The `es.msan.siap` id says which one it is rather
than claiming to be REGCESS.

**On school enrolment.** The national registry (RCD) is a directory, not a statistical
return: it publishes address, type, ownership and phone, but no pupil counts. Enrolment
per centre is released by the autonomous communities, which is why `es.cat.enrolments`
exists for Catalonia and has no national twin. Outside Catalonia a school's occupancy is
therefore estimated from its class defaults, and the response says so through
``capacity.basis``.
"""
from __future__ import annotations

import csv
import io
import logging
import re
from typing import Any, AsyncIterator, Iterable

from ...models import SourceMeta
from ...net import get_client, request
from ...norm import (clean_phones, clean_text, clean_url, fold, to_float,
                     valid_lonlat)
from ..base import SPAIN, Connector, RawAsset, Tier
from ..registry import register

log = logging.getLogger("talaia.es.spain")

CNH_URL = ("https://www.sanidad.gob.es/estadEstudios/estadisticas/sisInfSanSNS/"
           "ofertaRecursos/hospitales/docs/CNH_2025.xlsx")
SIAP_URL = ("https://www.sanidad.gob.es/estadEstudios/estadisticas/docs/siap/"
            "2026_C_Catal_Centros_AP.xlsx")
CSIC_BASE = "https://envejecimientoenred.csic.es/wp-content/uploads/residencias"
CSIC_YEAR = "2022"
RCD_BASE = "https://www.educacion.gob.es/centros"


def _headers(rows: list[list[Any]]) -> tuple[dict[str, int], int]:
    """Locate the header row and map column name -> index.

    These workbooks carry a title banner above the table and the banner height changes
    between editions, so the header is found by content rather than assumed to be row 1.
    """
    for i, row in enumerate(rows[:12]):
        names = {str(c).strip().upper(): j for j, c in enumerate(row) if c is not None}
        if len(names) >= 5:
            return names, i
    return {}, 0


def _key(text: Any) -> str:
    """Fold a column name to bare lowercase letters and digits.

    Accents must be removed before the non-alphanumeric strip, not by it: dropping the
    accented character outright turns ``Denominación`` into ``denominacin``, which then
    matches nothing. Spanish public files vary accents, case and trailing spaces between
    editions, so every column lookup goes through this.
    """
    return re.sub(r"[^a-z0-9]", "", fold(text))


def _col(names: dict[str, int], *candidates: str) -> int | None:
    """First matching column: exact folded name, then folded substring."""
    folded = {_key(k): v for k, v in names.items()}
    for cand in candidates:
        want = _key(cand)
        if want and want in folded:
            return folded[want]
    for cand in candidates:
        want = _key(cand)
        if not want:
            continue
        for k, v in folded.items():
            if want in k:
                return v
    return None


def _field(row: dict, *candidates: str) -> str | None:
    """Same lookup against a dict row rather than a header index."""
    folded = {_key(k): v for k, v in row.items()}
    for cand in candidates:
        want = _key(cand)
        if want and want in folded:
            return clean_text(folded[want])
    for cand in candidates:
        want = _key(cand)
        if not want:
            continue
        for k, v in folded.items():
            if want in k:
                return clean_text(v)
    return None


async def _xlsx_rows(url: str, sheet: str | None = None) -> list[list[Any]]:
    """Download a workbook and return its rows. Held in memory: these are a few MB."""
    import openpyxl

    resp = await request("GET", url, timeout=300.0)
    wb = openpyxl.load_workbook(io.BytesIO(resp.content), read_only=True, data_only=True)
    ws = wb[sheet] if sheet and sheet in wb.sheetnames else wb[wb.sheetnames[0]]
    return [list(r) for r in ws.iter_rows(values_only=True)]


# ---------------------------------------------------------------------------
# 1. Catálogo Nacional de Hospitales - the bed counts
# ---------------------------------------------------------------------------
_HOSPITAL_KINDS = {
    "C11": "hospital", "C12": "hospital", "C13": "hospital",
    "C14": "mental_health", "C15": "hospital", "C16": "hospital",
    "C17": "hospital", "C18": "hospital",
}


@register
class NationalHospitals(Connector):
    meta = SourceMeta(
        id="es.msan.hospitales",
        description=(
            "The Ministry of Health's national hospital catalogue: every hospital in "
            "Spain, public and private, with its functional bed count and the health "
            "service that runs it. Published annually as a single spreadsheet."),
        used_for=(
            "Hospitals are both a concentration of people who cannot self-evacuate and a "
            "response asset a fire must not cut off. Bed count drives the people-at-risk "
            "estimate (beds x 2.2, covering staff and visitors) and the triage score "
            "treats them as critical infrastructure."),
        geocoding=(
            "This registry publishes a postal address and no coordinate, so every record "
            "has to be placed on the map before it can be counted inside a fire "
            "perimeter. TALAIA does that offline from a bulk place-name table, which "
            "resolves the municipality to its centre rather than the building. The point "
            "therefore means the town, not the street: out by a few hundred metres in a "
            "village and by several kilometres in a city. For production use, run the "
            "street-level geocoder with TALAIA_GEOCODE_MODE=hybrid."),
        name="Catálogo Nacional de Hospitales",
        publisher="Ministerio de Sanidad",
        tier="resident", coverage="Spain", country="ES",
        licence="Reutilización permitida (Ley 37/2007)",
        licence_url="https://www.sanidad.gob.es/avisoLegal/home.htm",
        url=("https://www.sanidad.gob.es/estadEstudios/estadisticas/sisInfSanSNS/"
             "ofertaRecursos/hospitales/home.htm"),
        categories=["healthcare"], update_cadence="annual",
        provides=["name", "beds", "address", "phone", "ownership", "hospital class"],
        limitations=[
            "Staffed beds as registered, not beds free at any moment.",
            "No coordinates published; positions are geocoded from the street address.",
            "A hospital complex may appear as several entries sharing one campus.",
        ],
    )
    tier = Tier.RESIDENT
    coverage = SPAIN

    async def fetch(self, **kwargs: Any) -> AsyncIterator[dict]:
        rows = await _xlsx_rows(CNH_URL, "DIRECTORIO DE HOSPITALES")
        names, start = _headers(rows)
        idx = {
            "ccn": _col(names, "CCN"), "cod": _col(names, "CODCNH"),
            "name": _col(names, "Nombre Centro"), "address": _col(names, "Dirección"),
            "phone": _col(names, "Teléfono"), "municipality": _col(names, "Municipio"),
            "province": _col(names, "Provincia"), "postcode": _col(names, "Código Postal"),
            "beds": _col(names, "CAMAS"),
            "class_code": _col(names, "Cód. Clase de Centro"),
            "class_name": _col(names, "Clase de Centro"),
            "ownership": _col(names, "Dependencia Funcional"),
            "complex": _col(names, "Nombre del Complejo"),
        }
        log.info("CNH: %s rows", len(rows) - start - 1)
        for row in rows[start + 1:]:
            if idx["name"] is None or not row[idx["name"]]:
                continue
            yield {k: (row[i] if i is not None and i < len(row) else None)
                   for k, i in idx.items()}

    def normalise(self, raw: dict) -> Iterable[RawAsset]:
        name = clean_text(raw.get("name"))
        if not name:
            return []
        code = str(raw.get("class_code") or "").strip().upper()
        sub = _HOSPITAL_KINDS.get(code, "hospital")
        beds = to_float(raw.get("beds"))
        capacity: dict[str, Any] = {}
        if beds:
            # Beds are the registry's own count of staffed places. People present is
            # higher: inpatients plus staff, plus outpatients during the day. The
            # multiplier is a planning convention, and the basis says where it came from.
            capacity = {"beds": beds, "people": round(beds * 2.2),
                        "basis": "registered staffed beds (CNH) x 2.2 for staff and visitors",
                        "confidence": 0.75}
        return [RawAsset(
            source_ref=str(raw.get("ccn") or raw.get("cod") or name),
            subcategory=sub, name=name,
            address={"street": clean_text(raw.get("address")),
                     "municipality": clean_text(raw.get("municipality")),
                     "province": clean_text(raw.get("province")),
                     "postcode": clean_text(raw.get("postcode")), "country": "ES"},
            contacts={"phone": clean_phones(raw.get("phone"))},
            capacity=capacity,
            attributes={"cnh_code": clean_text(raw.get("cod")),
                        "hospital_class": clean_text(raw.get("class_name")),
                        "ownership": clean_text(raw.get("ownership")),
                        "complex": clean_text(raw.get("complex")),
                        "beds_registered": beds},
            confidence=0.85, needs_geocoding=True)]


# ---------------------------------------------------------------------------
# 2. SIAP - primary care and urgent care centres
# ---------------------------------------------------------------------------
@register
class PrimaryCareCentres(Connector):
    meta = SourceMeta(
        id="es.msan.siap",
        description=(
            "SIAP, the Ministry of Health's register of primary-care centres — health "
            "centres and local clinics, the tier below a hospital. This is the only "
            "national source for them, and it is how TALAIA sees primary care outside "
            "Catalonia."),
        used_for=(
            "Primary-care centres are evacuation and triage points during an incident, "
            "and losing one displaces demand onto the nearest hospital. They are scored "
            "as response assets rather than by headcount, which is not published."),
        geocoding=(
            "This registry publishes a postal address and no coordinate, so every record "
            "has to be placed on the map before it can be counted inside a fire "
            "perimeter. TALAIA does that offline from a bulk place-name table, which "
            "resolves the municipality to its centre rather than the building. The point "
            "therefore means the town, not the street: out by a few hundred metres in a "
            "village and by several kilometres in a city. For production use, run the "
            "street-level geocoder with TALAIA_GEOCODE_MODE=hybrid."),
        name="Catálogo de Centros de Atención Primaria (SIAP)",
        publisher="Ministerio de Sanidad",
        tier="resident", coverage="Spain", country="ES",
        licence="Reutilización permitida (Ley 37/2007)",
        licence_url="https://www.sanidad.gob.es/avisoLegal/home.htm",
        url=("https://www.sanidad.gob.es/estadEstudios/estadisticas/sisInfSanSNS/"
             "ofertaRecursos/centrosSalud/home.htm"),
        categories=["healthcare"], update_cadence="annual",
        provides=["name", "address", "phone", "centre type", "health area", "ownership"],
        limitations=[
            "No capacity figures: a health centre's occupancy is a class default.",
            "No coordinates published; positions are geocoded from the street address.",
            "Stands in for REGCESS, which publishes no bulk extract.",
        ],
    )
    tier = Tier.RESIDENT
    coverage = SPAIN

    async def fetch(self, **kwargs: Any) -> AsyncIterator[dict]:
        rows = await _xlsx_rows(SIAP_URL, "Catálogo - 2026")
        names, start = _headers(rows)
        idx = {
            "ccaa": _col(names, "SIAP_CCAA.NOMBRE", "CCAA"),
            "province": _col(names, "SIAP_PROVINCIAS.NOMBRE", "PROVINCIA"),
            "area": _col(names, "SIAP_AREASALUD_CD.NOMBRE", "AREASALUD"),
            "municipality": _col(names, "MUNICIPIO"),
            "code": _col(names, "CODIGO_CCN"), "id": _col(names, "IDCENTRO"),
            "kind": _col(names, "TIPOCENTRO"),
            "name": _col(names, "SIAP_CENTROS.NOMBRE"),
            "address": _col(names, "DIRECCION"), "postcode": _col(names, "CP"),
            "locality": _col(names, "LOCALIDAD"), "phone": _col(names, "TELEFONO"),
            "ownership": _col(names, "T_NOMBRE"),
            "manager": _col(names, "D_GESTION"),
        }
        log.info("SIAP: %s rows", len(rows) - start - 1)
        for row in rows[start + 1:]:
            if idx["name"] is None or not row[idx["name"]]:
                continue
            yield {k: (row[i] if i is not None and i < len(row) else None)
                   for k, i in idx.items()}

    def normalise(self, raw: dict) -> Iterable[RawAsset]:
        name = clean_text(raw.get("name"))
        if not name:
            return []
        kind = str(raw.get("kind") or "").upper()
        # A consultorio local is a village surgery open a few hours a week; a centro de
        # salud is a full health centre. They differ enough in occupancy and in
        # criticality that collapsing them would misreport both.
        sub = "primary_care" if "SALUD" in kind else "clinic"
        municipality = clean_text(raw.get("municipality")) or clean_text(raw.get("locality"))
        return [RawAsset(
            source_ref=str(raw.get("code") or raw.get("id") or f"{name}|{municipality}"),
            subcategory=sub,
            name=clean_text(f"{raw.get('kind') or ''} {name}".strip()) or name,
            address={"street": clean_text(raw.get("address")),
                     "municipality": municipality,
                     "province": clean_text(raw.get("province")),
                     "postcode": clean_text(raw.get("postcode")),
                     "region": clean_text(raw.get("ccaa")), "country": "ES"},
            contacts={"phone": clean_phones(raw.get("phone")),
                      "operator": clean_text(raw.get("manager"))},
            attributes={"centre_type": clean_text(raw.get("kind")),
                        "health_area": clean_text(raw.get("area")),
                        "ownership": clean_text(raw.get("ownership")),
                        "siap_id": clean_text(raw.get("id"))},
            confidence=0.8, needs_geocoding=True)]


# ---------------------------------------------------------------------------
# 3. CSIC care homes - places AND coordinates
# ---------------------------------------------------------------------------
# Filenames as published; the slugs are not always the modern province name
# (araba/alava, gipuzkoa/guipuzcoa), so they are listed rather than derived.
CSIC_PROVINCES = [
    "albacete", "alicante", "almeria", "araba", "asturias", "avila", "badajoz",
    "baleares", "barcelona", "bizkaia", "burgos", "caceres", "cadiz", "cantabria",
    "castellon", "ceuta", "ciudadreal", "cordoba", "coruna", "cuenca", "gipuzkoa",
    "girona", "granada", "guadalajara", "huelva", "huesca", "jaen", "laspalmas",
    "leon", "lleida", "lugo", "madrid", "malaga", "melilla", "murcia", "navarra",
    "ourense", "palencia", "pontevedra", "rioja", "salamanca", "segovia", "sevilla",
    "soria", "tarragona", "tenerife", "teruel", "toledo", "valencia", "valladolid",
    "zamora", "zaragoza",
]


@register
class CareHomes(Connector):
    meta = SourceMeta(
        id="es.csic.carehomes",
        description=(
            "The CSIC's national register of residential care homes for older people, "
            "published per province with the number of places each home is licensed for."),
        used_for=(
            "Care homes are the highest-priority asset class in a wildfire evacuation: "
            "the residents are the least able to move themselves and need the most "
            "notice. Licensed places x 1.33 estimates people present including night "
            "staff, and the triage score weights them accordingly."),
        geocoding=(
            "Most records carry published coordinates. The minority that do not are "
            "placed offline from a bulk place-name table, at the centre of their "
            "municipality rather than the building, and are marked geocode_approximate so "
            "they can be told apart. Setting TALAIA_GEOCODE_MODE=hybrid resolves those "
            "few to street level."),
        name="Residencias de mayores (CSIC, Envejecimiento en Red)",
        publisher="CSIC - Centro de Ciencias Humanas y Sociales",
        tier="resident", coverage="Spain", country="ES",
        licence="Reuse permitted, non-commercial; attribution required",
        licence_url="https://envejecimientoenred.csic.es/datos-abiertos/residencias/",
        url="https://envejecimientoenred.csic.es/datos-abiertos/residencias/",
        categories=["social_care"], update_cadence="irregular (2022 edition)",
        provides=["name", "places", "address", "phone", "ownership", "coordinates"],
        limitations=[
            "2022 edition; homes opened or closed since are missing.",
            "Places are licensed capacity, not residents present.",
            "Compiled by a research group from regional registers, so completeness "
            "varies by province.",
        ],
        commercial_use=False,
    )
    tier = Tier.RESIDENT
    coverage = SPAIN

    async def fetch(self, **kwargs: Any) -> AsyncIterator[dict]:
        ok = 0
        for province in CSIC_PROVINCES:
            # Most files sit under the year directory, but at least one province
            # (malaga) is published a level up. Trying both costs one extra 404 for
            # that province and avoids hardcoding which one the publisher misfiled.
            candidates = [f"{CSIC_BASE}/{CSIC_YEAR}/{CSIC_YEAR[2:]}_{province}.csv",
                          f"{CSIC_BASE}/{CSIC_YEAR[2:]}_{province}.csv"]
            resp = None
            for url in candidates:
                try:
                    resp = await request("GET", url, timeout=90.0, retries=1)
                    break
                except Exception as exc:
                    last = exc
            if resp is None:
                log.warning("CSIC %s: %s", province, last)
                continue
            # Published as ISO-8859-1 with a two-line banner above the header.
            text = resp.content.decode("latin-1", errors="replace")
            lines = text.splitlines()
            head = next((i for i, ln in enumerate(lines[:8])
                         if "Denominaci" in ln and ";" in ln), None)
            if head is None:
                log.warning("CSIC %s: no header row found", province)
                continue
            reader = csv.DictReader(lines[head:], delimiter=";")
            n = 0
            for row in reader:
                row["_province"] = province
                yield row
                n += 1
            ok += 1
            log.debug("CSIC %s: %s rows", province, n)
        log.info("CSIC care homes: %s/%s province files loaded", ok, len(CSIC_PROVINCES))

    def normalise(self, raw: dict) -> Iterable[RawAsset]:
        def get(*keys: str) -> str | None:
            return _field(raw, *keys)

        name = get("Denominacion")
        if not name:
            return []
        # Decimal comma, as published.
        lon = to_float(str(get("Longitud") or "").replace(",", "."))
        lat = to_float(str(get("Latitud") or "").replace(",", "."))
        ll = valid_lonlat(lon, lat)
        street = " ".join(x for x in (get("Nombre via"), get("Num. via")) if x)
        via = get("Via") or ""
        places = to_float(get("Plazas"))
        municipality = get("Municipio")
        capacity: dict[str, Any] = {}
        if places:
            # A care home is at capacity essentially all of the time, and its residents
            # cannot self-evacuate - which is why this is the highest-priority class in
            # the taxonomy. Staff are added at roughly one per three places.
            capacity = {"places": places, "people": round(places * 1.33),
                        "basis": "licensed places (CSIC) plus staff at 1 per 3 places",
                        "confidence": 0.8}
        return [RawAsset(
            source_ref=f"{raw.get('_province')}|{name}|{municipality or ''}",
            subcategory="care_home", name=name,
            lon=ll[0] if ll else None, lat=ll[1] if ll else None,
            address={"street": clean_text(f"{via} {street}".strip()) or None,
                     "municipality": municipality,
                     "postcode": get("C. Postal"), "country": "ES"},
            contacts={"phone": clean_phones(get("Telefono")),
                      "website": clean_url(get("URL"))},
            capacity=capacity,
            attributes={"ownership": get("Titularidad"),
                        "places_licensed": places,
                        "csic_province": raw.get("_province"),
                        "last_modified": get("ltima modificaci", "Ultima modificaci")},
            confidence=0.8,
            # Only the handful of rows without usable coordinates go to the geocoder.
            needs_geocoding=ll is None)]


# ---------------------------------------------------------------------------
# 4. Registro Estatal de Centros Docentes no Universitarios
# ---------------------------------------------------------------------------
# Ordered most specific first, and matched on ALL tokens being present. The registry
# names centres in Spanish, Catalan and Galician, so the tokens are stems that survive
# accent folding: "educaci" covers educacion / educacio / educacion, "primari" covers
# primaria / primaria / primaria. A centre spanning infant to secondary is classified as
# a plain school rather than forced into one stage it only partly is.
_SCHOOL_RULES: list[tuple[tuple[str, ...], str]] = [
    (("infantil", "primari", "secundari"), "school"),
    (("infantil", "primari"), "primary_school"),
    (("primari", "secundari"), "school"),
    (("escuela infantil",), "nursery"),
    (("escola infantil",), "nursery"),
    (("educaci infantil",), "nursery"),
    (("educacio infantil",), "nursery"),
    (("educacion infantil",), "nursery"),
    (("infantil",), "nursery"),
    (("conservatori",), "music_school"),
    (("musica",), "music_school"),
    (("music",), "music_school"),
    (("danza",), "music_school"),
    (("dansa",), "music_school"),
    (("arte dramatico",), "music_school"),
    (("artes plasticas",), "music_school"),
    (("idiomas",), "vocational"),
    (("idiomes",), "vocational"),
    (("adultos",), "vocational"),
    (("adultes",), "vocational"),
    (("adults",), "vocational"),
    (("personas adultas",), "vocational"),
    (("formacion profesional",), "vocational"),
    (("formacio profesional",), "vocational"),
    (("ensenanzas deportivas",), "vocational"),
    (("deportiv",), "vocational"),
    (("secundari",), "secondary_school"),
    (("instituto",), "secondary_school"),
    (("institut",), "secondary_school"),
    (("bachillerato",), "secondary_school"),
    (("primari",), "primary_school"),
    (("colegio rural agrupado",), "primary_school"),
    (("universidad",), "university"),
    (("biblioteca",), "library"),
]



@register
class NationalSchools(Connector):
    meta = SourceMeta(
        id="es.meq.schools",
        description=(
            "The Ministry of Education's state register of non-university teaching "
            "centres: every school, nursery, language school and vocational college in "
            "Spain, with its type and ownership. The largest single registry TALAIA "
            "holds."),
        used_for=(
            "Schools concentrate children during the day and are frequently designated "
            "shelters at night, so they matter twice. Outside Catalonia this is the only "
            "national source, and enrolment is not published — headcount falls back to a "
            "class-size estimate from the centre type."),
        geocoding=(
            "This registry publishes a postal address and no coordinate, so every record "
            "has to be placed on the map before it can be counted inside a fire "
            "perimeter. TALAIA does that offline from a bulk place-name table, which "
            "resolves the municipality to its centre rather than the building. The point "
            "therefore means the town, not the street: out by a few hundred metres in a "
            "village and by several kilometres in a city. For production use, run the "
            "street-level geocoder with TALAIA_GEOCODE_MODE=hybrid."),
        name="Registro Estatal de Centros Docentes no Universitarios",
        publisher="Ministerio de Educación, Formación Profesional y Deportes",
        tier="resident", coverage="Spain", country="ES",
        licence="Reutilización permitida (Ley 37/2007)",
        licence_url="https://www.educacionfpydeportes.gob.es/",
        url="https://www.educacion.gob.es/centros",
        categories=["education"], update_cadence="continuous",
        provides=["name", "centre type", "address", "postcode", "phone", "ownership"],
        limitations=[
            "A directory, not a statistical return: no pupil numbers. Enrolment is "
            "published per community, which is why Catalonia has its own feed.",
            "No coordinates published; positions are geocoded from the street address.",
            "One registry entry can cover several buildings on a campus.",
        ],
    )
    tier = Tier.RESIDENT
    coverage = SPAIN

    async def fetch(self, **kwargs: Any) -> AsyncIterator[dict]:
        """Drive the registry's own Excel export.

        The portal offers no bulk file, but its search results page has an export button,
        and the export ignores the geographic filter and returns the whole country. So
        the sequence is: open a session, enter the search form, run a default search,
        then ask for the export - four requests for 35,000 rows, which is far gentler on
        the service than paginating HTML per province.
        """
        import xlrd

        client = get_client()
        await client.get(f"{RCD_BASE}/home.do", timeout=60.0)
        page = await client.post(f"{RCD_BASE}/verBuscadorComunidad",
                                 data={"idComunidad": "03"}, timeout=60.0)
        # Submit the form exactly as rendered: defaults mean "no filter".
        form: dict[str, str] = {}
        for m in re.finditer(
                r'<input[^>]*name=["\']([^"\']+)["\'][^>]*?(?:value=["\']([^"\']*)["\'])?[^>]*>',
                page.text, re.I):
            form.setdefault(m.group(1), m.group(2) or "")
        for m in re.finditer(r'<select[^>]*name=["\']([^"\']+)["\'](.*?)</select>',
                             page.text, re.S | re.I):
            opts = re.findall(r'value=["\']([^"\']*)["\']', m.group(2))
            form[m.group(1)] = opts[0] if opts else ""
        await client.post(f"{RCD_BASE}/buscarCentros", data=form, timeout=180.0)
        export = await client.post(f"{RCD_BASE}/exportarListadoCentrosExcel",
                                   data={}, timeout=300.0)
        if not export.content or not export.content.startswith(b"\xd0\xcf"):
            raise RuntimeError(
                f"RCD export did not return a workbook "
                f"({export.status_code}, {len(export.content)} bytes)")

        sheet = xlrd.open_workbook(file_contents=export.content).sheet_by_index(0)
        header = [str(sheet.cell_value(0, j)).strip().upper()
                  for j in range(sheet.ncols)]
        names = {h: j for j, h in enumerate(header)}
        idx = {
            "region": _col(names, "COMUNIDAD AUTÓNOMA", "COMUNIDAD"),
            "province": _col(names, "PROVINCIA"), "locality": _col(names, "LOCALIDAD"),
            "kind": _col(names, "DENOMINACIÓN GENÉRICA", "GENERICA"),
            "name": _col(names, "DENOMINACIÓN ESPECÍFICA", "ESPECIFICA"),
            "code": _col(names, "CÓDIGO"), "ownership": _col(names, "NATURALEZA"),
            "address": _col(names, "DOMICILIO"), "postcode": _col(names, "CÓD POSTAL"),
            "phone": _col(names, "TELÉFONO"),
        }
        log.info("RCD: %s rows", sheet.nrows - 1)
        for i in range(1, sheet.nrows):
            yield {k: (sheet.cell_value(i, j) if j is not None and j < sheet.ncols
                       else None) for k, j in idx.items()}

    def normalise(self, raw: dict) -> Iterable[RawAsset]:
        name = clean_text(raw.get("name")) or clean_text(raw.get("kind"))
        if not name:
            return []
        kind = clean_text(raw.get("kind")) or ""
        code = clean_text(raw.get("code"))
        locality = clean_text(raw.get("locality"))
        # Classify on the generic denomination, falling back to the specific name when
        # the registry leaves the generic blank - 5,556 of 35,328 rows do.
        folded = fold(f"{kind} {name if not kind else ''}")
        sub = next((sub for tokens, sub in _SCHOOL_RULES
                    if all(t in folded for t in tokens)), "school")
        # "Centro de Educación de Adultos" + "Abla" reads as a town, not a centre. When
        # the specific name is missing or is just the locality, compose the official name.
        display = clean_text(raw.get("name"))
        if kind and (not display or fold(display) == fold(locality)):
            display = f"{kind} {display}".strip()
        name = display or name
        return [RawAsset(
            source_ref=str(code or f"{name}|{locality or ''}"),
            subcategory=sub, name=name,
            address={"street": clean_text(raw.get("address")),
                     "municipality": locality,
                     "province": clean_text(raw.get("province")),
                     "postcode": clean_text(raw.get("postcode")),
                     "region": clean_text(raw.get("region")), "country": "ES"},
            contacts={"phone": clean_phones(raw.get("phone"))},
            attributes={"centre_type": kind, "rcd_code": code,
                        "ownership": clean_text(raw.get("ownership"))},
            confidence=0.8, needs_geocoding=True)]
