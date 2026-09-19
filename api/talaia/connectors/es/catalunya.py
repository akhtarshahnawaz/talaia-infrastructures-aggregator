"""Catalonia connectors (Generalitat de Catalunya open-data portal).

Six registries, each with its own quirks - documented inline where the quirk would
otherwise silently corrupt data.
"""
from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Iterable

from ...models import SourceMeta
from ...norm import (clean_emails, clean_phones, clean_text, clean_url, fold, parse_dms,
                     title_case, to_float, utm_to_wgs84, valid_lonlat)
from ..base import CATALONIA, Connector, RawAsset, Tier
from ..registry import register
from ..socrata import latest_value, pages, query

log = logging.getLogger("talaia.es.cat")

PORTAL = "https://analisi.transparenciacatalunya.cat/d"


def _addr(row: dict, street_key: str, **extra: str) -> dict:
    out = {
        "street": clean_text(row.get(street_key)),
        "postcode": clean_text(row.get("cp") or row.get("codi_postal") or row.get("cpostal")),
        "municipality": title_case(row.get("nom_municipi") or row.get("municipi")
                                   or row.get("poblacio")),
        "comarca": title_case(row.get("nom_comarca") or row.get("comarca")),
        "region": "Catalunya",
        "country": "ES",
    }
    out.update({k: v for k, v in extra.items() if v})
    return {k: v for k, v in out.items() if v}


# ---------------------------------------------------------------------------
# 1. Schools
# ---------------------------------------------------------------------------
@register
class CatSchools(Connector):
    meta = SourceMeta(
        id="es.cat.schools",
        name="Directori de centres educatius de Catalunya",
        publisher="Departament d'Educacio i Formacio Professional",
        tier="resident", coverage="Catalonia", country="ES",
        licence="CC BY 4.0", licence_url="https://creativecommons.org/licenses/by/4.0/",
        url=f"{PORTAL}/kvmv-ahh4",
        categories=["education"],
        update_cadence="annual (per academic year)",
        provides=["name", "geometry", "address", "phone", "email", "website", "capacity"],
        limitations=[
            "Enrolment-derived capacity is not live occupancy.",
            "Coordinates are building-entrance points, not footprints.",
        ],
    )
    tier = Tier.RESIDENT
    coverage = CATALONIA
    DATASET = "kvmv-ahh4"

    def __init__(self) -> None:
        self._enrolments: dict[str, dict] = {}
        self._curs: str | None = None

    async def fetch(self, **kwargs: Any) -> AsyncIterator[dict]:
        # The dataset holds every academic year stacked together; without pinning to the
        # latest `curs` you get ~6x duplicate schools.
        self._curs = await latest_value(self.DATASET, "curs")
        log.info("schools: latest curs = %s", self._curs)
        self._enrolments = await CatEnrolments.aggregate(self._curs)
        where = f"curs='{self._curs}'" if self._curs else None
        async for row in pages(self.DATASET, where=where, order="codi_centre"):
            yield row

    @staticmethod
    def _level(row: dict) -> str:
        """Derive the school level from the per-stage flag columns."""
        flags = {k: str(row.get(k) or "").strip().lower()
                 for k in ("einf2c", "epri", "eso", "batx")}
        on = {k for k, v in flags.items() if v in ("s", "si", "sí", "1", "true", "x")}
        name = fold(row.get("denominaci_completa"))
        if "llar d infants" in name or "llar infants" in name or "escola bressol" in name:
            return "nursery"
        if "eso" in on or "batx" in on:
            return "secondary_school"
        if "epri" in on:
            return "primary_school"
        if "einf2c" in on:
            return "nursery"
        if "universitat" in name:
            return "university"
        if any(w in name for w in ("adults", "formacio", "fp ", "oficis")):
            return "vocational"
        if "musica" in name or "dansa" in name:
            return "music_school"
        return "school"

    def normalise(self, raw: dict) -> Iterable[RawAsset]:
        code = clean_text(raw.get("codi_centre"))
        if not code:
            return []
        # `coordenades_geo_x` is LONGITUDE and `_y` is LATITUDE. The dataset also carries
        # a `geo_1` GeoJSON member whose coordinates have lost their decimal point
        # (2253505 instead of 2.253505) - it must not be used.
        lon = to_float(raw.get("coordenades_geo_x"))
        lat = to_float(raw.get("coordenades_geo_y"))
        ll = valid_lonlat(lon, lat)
        if not ll:
            e, n = to_float(raw.get("coordenades_utm_x")), to_float(raw.get("coordenades_utm_y"))
            if e and n:
                ll = valid_lonlat(*utm_to_wgs84(e, n, 31))
        if not ll:
            return []

        enrol = self._enrolments.get(code, {})
        students = enrol.get("students")
        sub = self._level(raw)
        capacity: dict[str, Any] = {}
        if students:
            # Staff are roughly 12% on top of the student body.
            capacity = {"students": students, "people": round(students * 1.12),
                        "basis": f"enrolment {self._curs} + 12% staff",
                        "confidence": 0.75}
        return [RawAsset(
            source_ref=code, subcategory=sub,
            name=title_case(raw.get("denominaci_completa")),
            lon=ll[0], lat=ll[1],
            address=_addr(raw, "adre_a"),
            contacts={"phone": clean_phones(raw.get("tel_fon")),
                      "email": clean_emails(raw.get("e_mail_centre")),
                      "website": clean_url(raw.get("url")),
                      "operator": clean_text(raw.get("nom_titularitat"))},
            capacity=capacity,
            attributes={"curs": raw.get("curs"), "titularitat": raw.get("nom_titularitat"),
                        "naturalesa": raw.get("nom_naturalesa"),
                        "units": enrol.get("units"),
                        "codi_municipi": raw.get("codi_municipi")},
            confidence=0.85,
        )]


# ---------------------------------------------------------------------------
# 2. Enrolments (join-only source)
# ---------------------------------------------------------------------------
@register
class CatEnrolments(Connector):
    """Aggregated server-side: 470k rows reduce to ~5k per-school totals in one query."""

    meta = SourceMeta(
        id="es.cat.enrolments",
        name="Matricules per centre i ensenyament",
        publisher="Departament d'Educacio i Formacio Professional",
        tier="enrichment", coverage="Catalonia", country="ES",
        licence="CC BY 4.0", url=f"{PORTAL}/xvme-26kg",
        categories=["education"], update_cadence="annual",
        provides=["capacity.students", "capacity.units"],
        limitations=["Enrolment is a headcount on the register, not attendance."],
    )
    tier = Tier.ENRICHMENT
    coverage = CATALONIA
    DATASET = "xvme-26kg"

    @classmethod
    async def aggregate(cls, curs: str | None = None) -> dict[str, dict]:
        curs = curs or await latest_value(cls.DATASET, "curs")
        try:
            rows = await query(
                cls.DATASET,
                select="codi_centre,sum(matr_cules_total) as students,sum(unitats) as units",
                where=f"curs='{curs}'" if curs else None,
                group="codi_centre", limit=50_000)
        except Exception as exc:
            log.warning("enrolment aggregate failed (schools keep default capacity): %s", exc)
            return {}
        out: dict[str, dict] = {}
        for r in rows:
            code = clean_text(r.get("codi_centre"))
            if code:
                out[code] = {"students": to_float(r.get("students")),
                             "units": to_float(r.get("units"))}
        log.info("enrolments: aggregated %s schools for curs %s", len(out), curs)
        return out

    async def fetch(self, **kwargs: Any) -> AsyncIterator[dict]:
        for code, vals in (await self.aggregate()).items():
            yield {"codi_centre": code, **vals}

    def normalise(self, raw: dict) -> Iterable[RawAsset]:
        return []  # join-only: contributes capacity to es.cat.schools, creates no assets


# ---------------------------------------------------------------------------
# 3. Social services registry (RESES)
# ---------------------------------------------------------------------------
@register
class CatSocialServices(Connector):
    meta = SourceMeta(
        id="es.cat.reses",
        name="Registre d'entitats, serveis i establiments socials (RESES)",
        publisher="Departament de Drets Socials i Inclusio",
        tier="resident", coverage="Catalonia", country="ES",
        licence="CC BY 4.0", url=f"{PORTAL}/ivft-vegh",
        categories=["social_care"], update_cadence="continuous",
        provides=["name", "address", "capacity.places", "operator"],
        limitations=[
            "Publishes no coordinates; locations are geocoded from the address via "
            "CartoCiudad, and the match quality is carried into each asset's confidence.",
            "Registered places are licensed capacity, not current residents.",
        ],
    )
    tier = Tier.RESIDENT
    coverage = CATALONIA
    DATASET = "ivft-vegh"

    # Typology text -> subcategory. Ordered: the first match wins.
    RULES: list[tuple[tuple[str, ...], str]] = [
        (("residencia assistida", "llar residencia", "residencial"), "care_home"),
        (("centre de dia",), "day_centre"),
        (("discapacitat",), "disability_home"),
        (("acolliment residencial", "exclusio social", "urgencia"), "shelter"),
        (("infants", "adolescents"), "childcare_residential"),
        (("malaltia mental", "salut mental"), "mental_health"),
    ]

    async def fetch(self, **kwargs: Any) -> AsyncIterator[dict]:
        async for row in pages(self.DATASET, order="registre"):
            yield row

    def _subcategory(self, tipologia: str) -> str | None:
        t = fold(tipologia)
        elderly = "gent gran" in t
        for keys, sub in self.RULES:
            if any(k in t for k in keys):
                # A day centre for the elderly and a residential home differ materially
                # in occupancy pattern, so keep them distinct.
                if sub == "care_home" and not elderly and "discapacitat" in t:
                    return "disability_home"
                return sub
        return None  # administrative services (home help, social work) are not premises

    def normalise(self, raw: dict) -> Iterable[RawAsset]:
        ref = clean_text(raw.get("registre"))
        sub = self._subcategory(raw.get("tipologia") or "")
        if not ref or not sub:
            return []
        places = to_float(raw.get("capacitat"))
        return [RawAsset(
            source_ref=ref, subcategory=sub,
            name=title_case(raw.get("nom")),
            lon=None, lat=None, needs_geocoding=True,
            address=_addr(raw, "adreca"),
            contacts={"operator": title_case(raw.get("entitat_titular"))},
            capacity={"places": places, "people": places,
                      "basis": "registered places (RESES)", "confidence": 0.7}
            if places else {},
            attributes={"tipologia": clean_text(raw.get("tipologia")),
                        "qualificacio": clean_text(raw.get("qualificacio")),
                        "nif": clean_text(raw.get("nif")),
                        "inscripcio": clean_text(raw.get("inscripcio"))},
            confidence=0.55,  # address-geocoded, so below a coordinate-bearing source
        )]


# ---------------------------------------------------------------------------
# 4. Equipaments de Catalunya
# ---------------------------------------------------------------------------
@register
class CatFacilities(Connector):
    meta = SourceMeta(
        id="es.cat.equipaments",
        name="Equipaments de Catalunya",
        publisher="Direccio General de Serveis Digitals i Experiencia Ciutadana",
        tier="resident", coverage="Catalonia", country="ES",
        licence="CC BY 4.0", url=f"{PORTAL}/8gmd-gz7i",
        categories=["healthcare", "education", "heritage", "commercial", "emergency",
                    "environment", "tourism", "transport"],
        update_cadence="continuous",
        provides=["name", "geometry", "address", "website", "category"],
        limitations=["Category granularity varies; ~6% of records carry no category."],
    )
    tier = Tier.RESIDENT
    coverage = CATALONIA
    DATASET = "8gmd-gz7i"

    # `categoria` is a pipe-delimited hierarchy, sometimes several concatenated.
    # Matched against the FOLDED string, most specific first.
    # Note the geminate: "Instal*lacions" folds to "installacions" with a double L.
    # Spelling it with one L silently dropped ~3,000 sports facilities into `other`.
    RULES: list[tuple[tuple[str, ...], str]] = [
        # health
        (("atencio primaria", "cap)"), "primary_care"),
        (("hospital",), "hospital"),
        (("centres sociosanitaris", "salut mental"), "mental_health"),
        (("farmacia", "farmacies"), "pharmacy"),
        (("centres sanitaris", "salut"), "clinic"),
        # emergency
        (("bombers",), "fire_station"),
        (("policia", "mossos", "guardia urbana"), "police"),
        (("proteccio civil",), "civil_protection"),
        # education & research
        (("instituts universitaris de recerca", "estructures universitaries de recerca",
          "centres de recerca", "recerca"), "research"),
        (("universitats",), "university"),
        (("educacio infantil de 1r cicle", "llars d infants", "escoles bressol"), "nursery"),
        (("educacio primaria",), "primary_school"),
        (("educacio secundaria", "batxillerat"), "secondary_school"),
        (("educacio infantil",), "nursery"),
        (("escoles de musica", "escoles d art", "conservatori", "escoles de dansa"),
         "music_school"),
        (("formacio de persones adultes", "formacio professional", "ensenyaments"),
         "vocational"),
        (("biblioteques",), "library"),
        (("arxius",), "archive"),
        (("formacio", "educacio"), "school"),
        # sport & leisure
        (("installacions esportives", "instalacions esportives", "poliesportiu",
          "pavello", "piscina", "rocodrom", "padel", "esports"), "sports_facility"),
        # culture & heritage
        (("museus", "colleccions", "centres d interpretacio"), "museum"),
        (("esglesies", "ermites", "monestirs", "santuaris"), "church"),
        (("castells",), "castle"),
        (("jaciments", "arqueolog"), "archaeological"),
        (("monuments",), "monument"),
        # transport
        (("aeroports", "aerodroms", "heliports"), "airport"),
        (("estacions d autobusos", "estacions de tren", "estacions"), "station"),
        (("ports",), "port"),
        (("inspeccio tecnica de vehicles", "itv"), "workshop"),
        # tourism
        (("campings",), "campsite"),
        (("albergs",), "hostel"),
        (("cases de colonies", "turisme rural"), "rural_lodging"),
        (("hotels",), "hotel"),
        (("oficines de turisme",), "attraction"),
        # environment / utilities
        (("deixalleries",), "waste_facility"),
        (("depuradores",), "wastewater"),
        (("parcs naturals", "espais naturals"), "protected_area"),
        (("parcs", "jardins"), "park"),
        # commerce & administration (broad, so last)
        (("mercats",), "market"),
        (("cinemes", "espais escenics", "teatres", "auditoris", "espais d arts visuals",
          "centres culturals", "ateneus", "casals", "cases de cultura",
          "equipaments civics", "altres espais aptes", "cultura"), "attraction"),
        (("oficines", "ajuntaments", "ens locals", "administracio", "registres",
          "habitatge", "treball", "consells comarcals", "generalitat"), "office"),
        (("cementiris", "tanatoris"), "other"),
        (("punt tic", "tecnologia"), "office"),
    ]

    async def fetch(self, **kwargs: Any) -> AsyncIterator[dict]:
        async for row in pages(self.DATASET, order="idequipament"):
            yield row

    def _subcategory(self, categoria: str, name: str) -> str:
        blob = fold(categoria)
        for keys, sub in self.RULES:
            if any(k in blob for k in keys):
                return sub
        nm = fold(name)
        for keys, sub in self.RULES:
            if any(k in nm for k in keys):
                return sub
        return "other"

    def normalise(self, raw: dict) -> Iterable[RawAsset]:
        ref = clean_text(raw.get("idequipament"))
        if not ref:
            return []
        ll = valid_lonlat(to_float(raw.get("longitud")), to_float(raw.get("latitud")))
        if not ll:
            e, n = to_float(raw.get("utmx")), to_float(raw.get("utmy"))
            if e and n:
                ll = valid_lonlat(*utm_to_wgs84(e, n, 31))
        if not ll:
            return []
        name = title_case(raw.get("nom") or raw.get("alies"))
        street = " ".join(str(p) for p in (raw.get("tipus_via"), raw.get("via"),
                                           raw.get("num")) if p)
        return [RawAsset(
            source_ref=ref,
            subcategory=self._subcategory(raw.get("categoria") or "", name or ""),
            name=name, lon=ll[0], lat=ll[1],
            address=_addr(raw, "via", street=clean_text(street)),
            contacts={"website": clean_url(raw.get("web"))},
            attributes={"categoria": clean_text(raw.get("categoria")),
                        "localitzacio": clean_text(raw.get("localitzacio")),
                        "codi_municipi": raw.get("codi_municipi")},
            confidence=0.8,
        )]


# ---------------------------------------------------------------------------
# 5. Livestock farms
# ---------------------------------------------------------------------------
@register
class CatLivestock(Connector):
    meta = SourceMeta(
        id="es.cat.livestock",
        name="Registre d'explotacions ramaderes (REGA)",
        publisher="Departament d'Agricultura, Ramaderia, Pesca i Alimentacio",
        tier="resident", coverage="Catalonia", country="ES",
        licence="CC BY 4.0", url=f"{PORTAL}/7bpt-5azk",
        categories=["livestock"], update_cadence="continuous",
        provides=["name", "geometry", "address", "capacity.animals",
                  "capacity.livestock_units", "species breakdown"],
        limitations=[
            "Registered capacity is not the number of animals currently present.",
            "Coordinates are published as degrees-minutes-seconds text and must be parsed; "
            "float() on them yields an error of up to ~100 km.",
            "The `total_ub` column is a headcount despite its name, and repeats across "
            "the rows of a species group.",
        ],
    )
    tier = Tier.RESIDENT
    coverage = CATALONIA
    DATASET = "7bpt-5azk"

    # Catalan species name -> (subcategory, LSU coefficient, EUR per head).
    #
    # `total_ub` is NOT a normalised livestock unit despite the column name: the largest
    # value in the registry is 280,485 against an egg producer, which is a headcount of
    # laying hens, and `cap_n_m_total_animals` is empty throughout. Treating it as
    # livestock units would value a hen like a dairy cow, so it is read as capacity in
    # head and converted with Eurostat LSU coefficients.
    SPECIES: list[tuple[tuple[str, ...], tuple[str, float, float]]] = [
        (("bovi", "vacu"),                       ("cattle",         0.80, 1200.0)),
        (("porci",),                             ("pigs",           0.30,  150.0)),
        (("galls dindi", "indiot"),              ("poultry",        0.030,  12.0)),
        (("gallines", "pollastres", "aviram"),   ("poultry",        0.010,   6.0)),
        (("anecs", "oques", "palmipedes"),       ("poultry",        0.014,  14.0)),
        (("guatlles", "perdius", "faisans"),     ("poultry",        0.005,   4.0)),
        (("estruc",),                            ("poultry",        0.100, 300.0)),
        (("ovi",),                               ("sheep_goats",    0.10,  120.0)),
        (("cabrum", "capri"),                    ("sheep_goats",    0.10,  130.0)),
        (("equid", "cavall"),                    ("horses",         0.80, 3000.0)),
        (("camell", "dromedari", "llama"),       ("horses",         0.80, 2500.0)),
        (("conill", "cunicola"),                 ("rabbits",        0.020,  15.0)),
        (("apicola", "abelles"),                 ("apiculture",     0.000, 180.0)),
        (("aquicultura", "piscicultura"),        ("aquaculture",    0.000,   8.0)),
        (("cervols", "cabirols", "caca"),        ("cattle",         0.30,  600.0)),
        (("gossos", "gats", "canina", "felina"), ("animal_shelter", 0.000, 400.0)),
    ]
    DEFAULT_SPECIES = ("mixed_livestock", 0.30, 300.0)

    @classmethod
    def species_info(cls, especie: str) -> tuple[str, float, float]:
        """(subcategory, LSU coefficient, EUR per head) for a Catalan species name."""
        e = fold(especie)
        for keys, info in cls.SPECIES:
            if any(k in e for k in keys):
                return info
        return cls.DEFAULT_SPECIES

    async def fetch(self, **kwargs: Any) -> AsyncIterator[dict]:
        """Yield one record per holding, not per sub-exploitation.

        The registry publishes a row per (holding x species x production type) - up to 22
        rows for a single `codi_rega`. Emitting them as separate assets would multiply
        exposure; keeping only the last would discard most of a mixed holding's animals.
        Rows are therefore merged per holding. Within a species group `total_ub` repeats
        the same capacity on every row, so it is taken as a maximum per species and summed
        across species - summing rows naively inflated capacity by ~45% on mixed holdings.
        """
        merged: dict[str, dict] = {}
        async for row in pages(self.DATASET, order="codi_rega"):
            if fold(row.get("estat_explotaci_")) not in ("activa", ""):
                continue
            key = clean_text(row.get("codi_rega"))
            if not key:
                continue
            species = clean_text(row.get("esp_cie")) or "?"
            heads = to_float(row.get("total_ub")) or 0.0
            entry = merged.setdefault(key, {**row, "_species": {}, "_rows": 0})
            entry["_rows"] += 1
            entry["_species"][species] = max(entry["_species"].get(species, 0.0), heads)
        log.info("livestock: %s active holdings merged from sub-exploitation rows",
                 len(merged))
        for rec in merged.values():
            yield rec

    def _subcategory(self, especie: str) -> str:
        return self.species_info(especie)[0]

    def normalise(self, raw: dict) -> Iterable[RawAsset]:
        ref = clean_text(raw.get("codi_rega"))
        if not ref:
            return []
        # Coordinates arrive as "42.0º 8.0' 33.0498''". float() would yield 42.0 - a
        # silent error of up to ~100 km - so they go through the DMS parser.
        lat = parse_dms(raw.get("latitud"))
        lon = parse_dms(raw.get("longitud"))
        ll = valid_lonlat(lon, lat)
        if not ll:
            e, n = to_float(raw.get("coor_x")), to_float(raw.get("coor_y"))
            if e and n:
                ll = valid_lonlat(*utm_to_wgs84(e, n, 31))
        if not ll:
            return []

        species_map: dict[str, float] = raw.get("_species") or {}
        if not species_map:
            sp = clean_text(raw.get("esp_cie"))
            if sp:
                species_map = {sp: to_float(raw.get("total_ub")) or 0.0}

        heads = 0.0
        lsu = 0.0
        breakdown: dict[str, float] = {}
        groups: dict[str, float] = {}
        for species, count in species_map.items():
            group, coeff, _per_head = self.species_info(species)
            count = float(count or 0.0)
            heads += count
            lsu += count * coeff
            if count:
                breakdown[species] = count
            groups[group] = groups.get(group, 0.0) + count * coeff

        dominant = max(groups, key=lambda k: groups[k]) if groups else "mixed_livestock"
        sub = dominant if len([g for g, v in groups.items() if v > 0]) <= 1 \
            else "mixed_livestock"

        return [RawAsset(
            source_ref=ref, subcategory=sub,
            name=title_case(raw.get("nom_explotaci") or raw.get("marca_oficial")),
            lon=ll[0], lat=ll[1],
            address=_addr(raw, "adre_a_explotaci"),
            capacity={"animals": round(heads) or None,
                      "livestock_units": round(lsu, 1) or None,
                      "basis": "registered capacity in head (REGA), converted to "
                               "livestock units with Eurostat coefficients",
                      "confidence": 0.55},
            attributes={"species": sorted(breakdown),
                        "species_head_capacity": {k: round(v)
                                                  for k, v in breakdown.items()},
                        "dominant_group": dominant,
                        "sub_exploitations": raw.get("_rows", 1),
                        "holding_type": clean_text(raw.get("tipus_explotaci")),
                        "production_system": clean_text(raw.get("sistema_productiu")),
                        "rega": ref},
            confidence=0.8,
        )]


# ---------------------------------------------------------------------------
# 6. Municipal reference points
# ---------------------------------------------------------------------------
@register
class CatMunicipalPoints(Connector):
    meta = SourceMeta(
        id="es.cat.munipoints",
        name="Punts de referencia municipals",
        publisher="Institut Cartografic i Geologic de Catalunya (ICGC)",
        tier="resident", coverage="Catalonia", country="ES",
        licence="CC BY 4.0", url=f"{PORTAL}/wpyq-we8x",
        categories=["commercial"], update_cadence="rare",
        provides=["municipality centroid"],
        limitations=[
            "Municipal seats, NOT facility locations. Used only as a labelled fallback "
            "and always flagged with low confidence.",
        ],
    )
    tier = Tier.RESIDENT
    coverage = CATALONIA
    DATASET = "wpyq-we8x"

    async def fetch(self, **kwargs: Any) -> AsyncIterator[dict]:
        async for row in pages(self.DATASET, order="codi_municipi"):
            yield row

    def normalise(self, raw: dict) -> Iterable[RawAsset]:
        ref = clean_text(raw.get("codi_municipi_ine") or raw.get("codi_municipi"))
        if not ref:
            return []
        ll = valid_lonlat(to_float(raw.get("longitud")), to_float(raw.get("latitud")))
        if not ll:
            e, n = to_float(raw.get("utm_x")), to_float(raw.get("utm_y"))
            if e and n:
                ll = valid_lonlat(*utm_to_wgs84(e, n, 31))
        if not ll:
            return []
        return [RawAsset(
            source_ref=ref, subcategory="office",
            name=f"{title_case(raw.get('municipi'))} (municipal reference point)",
            lon=ll[0], lat=ll[1],
            address={"municipality": title_case(raw.get("municipi")),
                     "comarca": title_case(raw.get("comarca")),
                     "province": title_case(raw.get("prov_ncia")),
                     "region": "Catalunya", "country": "ES"},
            attributes={"ine_code": ref, "reference_point": True},
            confidence=0.25,
        )]
