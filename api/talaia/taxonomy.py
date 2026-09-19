"""TALAIA canonical taxonomy.

The single source of truth for asset classification. Served at ``/v1/taxonomy`` so the
website, the API and any downstream agent share one vocabulary and cannot drift.

Every subcategory carries the parameters the valuation and scoring engines need:

``vulnerability``  0-100, likelihood the asset class is damaged/destroyed by fire exposure.
``criticality``    0-100, consequence of losing it (life-safety + service continuity).
``human_bearing``  whether the asset normally contains people who may need evacuating.
``unit_cost``      EUR per m2 of gross floor area, replacement (not market) cost.
``contents_ratio`` contents value as a fraction of structure value.
``footprint``      default ground footprint m2, used only when no geometry is available.
``floors``         default storey count, same caveat.
``people``         default occupancy estimate when the registry gives no capacity.

Unit costs are derived from Spanish construction-cost references (Modulo Basico de
Construccion family) rounded to the nearest 10 EUR. They are parametric triage inputs,
NOT appraisals - see ``docs``/methodology.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class Category:
    key: str
    label: str
    label_es: str
    label_ca: str
    description: str
    vulnerability: int
    criticality: int
    human_bearing: bool
    valuation_model: str  # floorspace | livestock | linear | area | none


@dataclass(frozen=True)
class Subcategory:
    key: str
    category: str
    label: str
    vulnerability: int
    criticality: int
    human_bearing: bool
    unit_cost: float = 0.0
    contents_ratio: float = 0.3
    footprint: float = 400.0
    floors: float = 1.0
    people: float = 0.0
    notes: str = ""
    aliases: tuple[str, ...] = field(default_factory=tuple)


# --------------------------------------------------------------------------
# Categories
# --------------------------------------------------------------------------
_CATEGORIES: list[Category] = [
    Category("population", "Population", "Poblacion", "Poblacio",
             "Resident population from census grids. Exposure, never monetised.",
             50, 100, True, "none"),
    Category("education", "Education", "Educacion", "Educacio",
             "Schools, nurseries, universities and training centres.",
             70, 90, True, "floorspace"),
    Category("healthcare", "Healthcare", "Sanidad", "Salut",
             "Hospitals, primary care centres, clinics and pharmacies.",
             65, 100, True, "floorspace"),
    Category("social_care", "Social care", "Servicios sociales", "Serveis socials",
             "Care homes, day centres and residential social services. Low-mobility occupants.",
             75, 100, True, "floorspace"),
    Category("emergency", "Emergency services", "Emergencias", "Emergencies",
             "Fire, police, civil protection, ambulance bases and helipads.",
             45, 100, True, "floorspace"),
    Category("transport", "Transport", "Transporte", "Transport",
             "Roads, railways, stations, airports and structures. Access and evacuation.",
             40, 95, False, "linear"),
    Category("energy", "Energy", "Energia", "Energia",
             "Substations, power lines, generation and fuel infrastructure.",
             70, 95, False, "floorspace"),
    Category("water", "Water", "Agua", "Aigua",
             "Reservoirs, treatment works, towers and hydrants. Suppression resource.",
             30, 85, False, "floorspace"),
    Category("telecom", "Telecommunications", "Telecomunicaciones", "Telecomunicacions",
             "Masts, towers and exchanges. Loss cascades into comms blackout.",
             75, 90, False, "floorspace"),
    Category("industry", "Industry", "Industria", "Industria",
             "Factories, warehouses, fuel depots, waste and hazardous sites.",
             65, 80, True, "floorspace"),
    Category("agriculture", "Agriculture", "Agricultura", "Agricultura",
             "Farms, silos, greenhouses, crops and forestry holdings.",
             85, 55, False, "floorspace"),
    Category("livestock", "Livestock", "Ganaderia", "Ramaderia",
             "Livestock holdings. Animals cannot self-evacuate; drives early action.",
             90, 70, False, "livestock"),
    Category("residential", "Residential", "Residencial", "Residencial",
             "Dwellings and residential buildings.",
             70, 85, True, "floorspace"),
    Category("commercial", "Commercial", "Comercial", "Comercial",
             "Shops, offices, markets and services.",
             60, 55, True, "floorspace"),
    Category("tourism", "Tourism & leisure", "Turismo", "Turisme",
             "Campsites, hotels, rural lodging. Transient, unfamiliar occupants.",
             85, 80, True, "floorspace"),
    Category("heritage", "Heritage", "Patrimonio", "Patrimoni",
             "Monuments, churches, museums and archives. Irreplaceable losses.",
             70, 75, True, "floorspace"),
    Category("environment", "Environment", "Medio ambiente", "Medi ambient",
             "Protected areas, habitats and forest stands.",
             90, 60, False, "area"),
]

CATEGORIES: dict[str, Category] = {c.key: c for c in _CATEGORIES}


# --------------------------------------------------------------------------
# Subcategories
# --------------------------------------------------------------------------
def _s(key, cat, label, vul, crit, human, cost=0.0, contents=0.3,
       fp=400.0, fl=1.0, ppl=0.0, notes="", aliases=()):
    return Subcategory(key, cat, label, vul, crit, human, cost, contents, fp, fl, ppl,
                       notes, tuple(aliases))


_SUBCATEGORIES: list[Subcategory] = [
    # -- education ---------------------------------------------------------
    _s("nursery", "education", "Nursery / infant school", 75, 95, True, 1180, .25, 600, 1, 60,
       "Occupants have no independent evacuation capability."),
    _s("primary_school", "education", "Primary school", 70, 95, True, 1180, .25, 1800, 2, 300),
    _s("secondary_school", "education", "Secondary school", 70, 92, True, 1250, .25, 2600, 2, 500),
    _s("school", "education", "School (unspecified level)", 70, 92, True, 1200, .25, 2000, 2, 300),
    _s("vocational", "education", "Vocational / adult training", 65, 80, True, 1250, .35, 2200, 2, 300),
    _s("university", "education", "University faculty", 65, 85, True, 1450, .45, 4000, 3, 900),
    _s("music_school", "education", "Music / arts school", 65, 70, True, 1250, .40, 800, 2, 100),
    _s("library", "education", "Library", 70, 70, True, 1350, .55, 900, 2, 80),
    _s("research", "education", "Research centre", 60, 80, True, 1650, .90, 2000, 2, 150),

    # -- healthcare --------------------------------------------------------
    _s("hospital", "healthcare", "Hospital", 60, 100, True, 2100, .80, 6000, 4, 400,
       "Evacuation is slow and resource-intensive; many occupants are non-ambulatory."),
    _s("primary_care", "healthcare", "Primary care centre (CAP)", 65, 95, True, 1550, .65, 1400, 2, 90),
    _s("clinic", "healthcare", "Clinic / specialist centre", 65, 85, True, 1550, .65, 900, 2, 50),
    _s("pharmacy", "healthcare", "Pharmacy", 65, 70, True, 1200, .60, 120, 1, 6),
    _s("mental_health", "healthcare", "Mental health facility", 65, 95, True, 1600, .55, 1600, 2, 80),
    _s("veterinary", "healthcare", "Veterinary clinic", 65, 60, True, 1200, .55, 300, 1, 10),

    # -- social care -------------------------------------------------------
    _s("care_home", "social_care", "Residential care home (elderly)", 75, 100, True, 1480, .40, 2200, 3, 90,
       "Highest evacuation priority: low mobility, high dependency, 24h occupancy."),
    _s("day_centre", "social_care", "Day centre", 70, 85, True, 1350, .35, 800, 1, 45,
       "Daytime occupancy only."),
    _s("disability_home", "social_care", "Supported housing (disability)", 75, 100, True, 1450, .40, 900, 2, 30),
    _s("shelter", "social_care", "Social shelter / emergency housing", 70, 90, True, 1280, .30, 900, 2, 50),
    _s("childcare_residential", "social_care", "Residential childcare", 75, 100, True, 1400, .35, 900, 2, 30),
    _s("social_services", "social_care", "Social services office", 60, 70, True, 1200, .35, 500, 2, 25),

    # -- emergency ---------------------------------------------------------
    _s("fire_station", "emergency", "Fire station", 40, 100, True, 1450, .95, 1400, 1, 25,
       "Loss during an incident is doubly damaging: asset and response capacity."),
    _s("police", "emergency", "Police station", 45, 95, True, 1450, .70, 1200, 2, 40),
    _s("civil_protection", "emergency", "Civil protection base", 45, 95, True, 1350, .70, 900, 1, 20),
    _s("ambulance", "emergency", "Ambulance station", 45, 95, True, 1350, .90, 700, 1, 15),
    _s("helipad", "emergency", "Helipad", 20, 90, False, 350, .10, 900, 1, 0,
       "Critical for medical evacuation and aerial suppression staging."),
    _s("emergency_shelter", "emergency", "Designated evacuation point", 40, 95, True, 1200, .25, 1500, 1, 0),
    _s("water_point", "emergency", "Firefighting water point", 15, 90, False, 0, .0, 0, 1, 0,
       "Suppression resource, not a loss item."),

    # -- transport ---------------------------------------------------------
    _s("motorway", "transport", "Motorway", 25, 95, False, 0, .0, 0, 1, 0),
    _s("primary_road", "transport", "Primary road", 30, 95, False, 0, .0, 0, 1, 0),
    _s("secondary_road", "transport", "Secondary road", 35, 85, False, 0, .0, 0, 1, 0),
    _s("local_road", "transport", "Local road", 40, 70, False, 0, .0, 0, 1, 0),
    _s("track", "transport", "Track / forest road", 55, 60, False, 0, .0, 0, 1, 0,
       "Often the only access to isolated holdings, and the first to be cut."),
    _s("railway", "transport", "Railway line", 35, 90, False, 0, .0, 0, 1, 0),
    _s("station", "transport", "Station / interchange", 50, 85, True, 1600, .50, 2000, 2, 200),
    _s("airport", "transport", "Airport / aerodrome", 45, 90, True, 1800, .70, 8000, 2, 300),
    _s("bridge", "transport", "Bridge", 30, 95, False, 0, .0, 0, 1, 0,
       "Single points of failure for evacuation routes."),
    _s("tunnel", "transport", "Tunnel", 25, 90, False, 0, .0, 0, 1, 0),
    _s("port", "transport", "Port / harbour", 40, 80, True, 900, .60, 5000, 1, 40,
       "Marine access point; can serve as an evacuation route when roads are cut."),
    _s("parking", "transport", "Parking", 45, 35, False, 420, .05, 1500, 1, 0),
    _s("fuel_station", "transport", "Fuel station", 90, 85, True, 900, 1.20, 350, 1, 8,
       "Hazardous: accelerant on site. Treat as an exclusion-zone trigger."),

    # -- energy ------------------------------------------------------------
    _s("substation", "energy", "Electrical substation", 65, 98, False, 2400, 2.50, 1200, 1, 0,
       "Loss cascades into wide-area outage affecting pumping and comms."),
    _s("power_line", "energy", "Power transmission line", 75, 95, False, 0, .0, 0, 1, 0),
    _s("power_tower", "energy", "Transmission tower", 55, 90, False, 0, .0, 0, 1, 0),
    _s("power_plant", "energy", "Power plant", 60, 95, False, 2600, 2.00, 6000, 1, 30),
    _s("solar_farm", "energy", "Solar farm", 70, 70, False, 480, .60, 20000, 1, 0),
    _s("wind_farm", "energy", "Wind farm", 55, 70, False, 0, .0, 0, 1, 0),
    _s("gas_infrastructure", "energy", "Gas infrastructure", 85, 95, False, 1800, 1.80, 800, 1, 0,
       "Hazardous: pressurised flammable product."),
    _s("fuel_depot", "energy", "Fuel depot", 95, 95, False, 1200, 2.50, 3000, 1, 10,
       "Hazardous: major accelerant, mandatory exclusion zone."),

    # -- water -------------------------------------------------------------
    _s("reservoir", "water", "Reservoir", 5, 85, False, 0, .0, 0, 1, 0,
       "Suppression resource: candidate aerial water pickup."),
    _s("water_treatment", "water", "Water treatment works", 45, 90, False, 1600, 1.50, 2500, 1, 10),
    _s("water_tower", "water", "Water tower", 30, 80, False, 1400, .30, 200, 1, 0),
    _s("pumping_station", "water", "Pumping station", 55, 85, False, 1500, 1.40, 300, 1, 0),
    _s("hydrant", "water", "Hydrant", 10, 80, False, 0, .0, 0, 1, 0),
    _s("wastewater", "water", "Wastewater treatment", 45, 75, False, 1500, 1.30, 3000, 1, 8),

    # -- telecom -----------------------------------------------------------
    _s("telecom_mast", "telecom", "Telecom mast", 70, 92, False, 0, .0, 60, 1, 0,
       "Loss removes mobile coverage used for public warning."),
    _s("telecom_exchange", "telecom", "Telephone exchange", 60, 90, False, 2000, 3.00, 600, 2, 5),
    _s("data_centre", "telecom", "Data centre", 55, 85, False, 3200, 4.00, 2000, 1, 15),

    # -- industry ----------------------------------------------------------
    _s("factory", "industry", "Factory", 65, 75, True, 720, .90, 4000, 1, 80),
    _s("warehouse", "industry", "Warehouse", 70, 60, True, 520, .80, 5000, 1, 20),
    _s("hazardous_site", "industry", "Hazardous / Seveso site", 90, 98, True, 1500, 2.00, 6000, 1, 40,
       "Regulated major-accident hazard. Fire contact risks toxic release."),
    _s("chemical_plant", "industry", "Chemical plant", 90, 95, True, 1900, 2.20, 5000, 1, 60),
    _s("quarry", "industry", "Quarry / extraction", 30, 45, False, 0, .0, 0, 1, 10),
    _s("waste_facility", "industry", "Waste / recycling facility", 85, 65, True, 620, .70, 4000, 1, 20,
       "High fuel load; prone to deep-seated, long-duration fires."),
    _s("sawmill", "industry", "Sawmill / timber yard", 95, 60, True, 560, 1.10, 3000, 1, 25,
       "Extreme fuel load."),
    _s("workshop", "industry", "Workshop", 65, 50, True, 700, .80, 400, 1, 8),

    # -- agriculture -------------------------------------------------------
    _s("farm", "agriculture", "Farm holding", 85, 60, True, 480, .70, 1200, 1, 4),
    _s("farm_building", "agriculture", "Agricultural building", 88, 45, False, 320, .55, 800, 1, 0),
    _s("greenhouse", "agriculture", "Greenhouse", 92, 45, False, 260, .80, 2500, 1, 0),
    _s("silo", "agriculture", "Silo / grain store", 80, 60, False, 900, 1.50, 300, 1, 0,
       "Stored crop value often exceeds the structure."),
    _s("winery", "agriculture", "Winery / cellar", 70, 60, True, 1100, 1.60, 1800, 1, 15),
    _s("orchard", "agriculture", "Orchard / permanent crop", 90, 40, False, 0, .0, 0, 1, 0),
    _s("beehives", "agriculture", "Apiary", 95, 30, False, 0, .0, 0, 1, 0),
    _s("forestry", "agriculture", "Forestry holding", 95, 45, False, 0, .0, 0, 1, 0),

    # -- livestock ---------------------------------------------------------
    _s("cattle", "livestock", "Cattle holding", 90, 75, False, 420, .40, 1500, 1, 2,
       "Animals require transport to evacuate; decision lead time is hours."),
    _s("pigs", "livestock", "Pig holding", 92, 70, False, 450, .45, 1800, 1, 2),
    _s("poultry", "livestock", "Poultry holding", 95, 65, False, 400, .55, 2000, 1, 2,
       "Highly susceptible to smoke and heat stress; mass mortality events are common."),
    _s("sheep_goats", "livestock", "Sheep / goat holding", 88, 70, False, 350, .35, 1000, 1, 2),
    _s("horses", "livestock", "Equine holding", 85, 75, False, 520, .45, 900, 1, 2),
    _s("rabbits", "livestock", "Rabbit holding", 92, 55, False, 400, .45, 900, 1, 1),
    _s("apiculture", "livestock", "Beekeeping holding", 95, 40, False, 0, .0, 0, 1, 0),
    _s("aquaculture", "livestock", "Aquaculture", 45, 50, False, 700, .70, 1200, 1, 3),
    _s("mixed_livestock", "livestock", "Mixed livestock holding", 90, 70, False, 420, .45, 1500, 1, 2),
    _s("animal_shelter", "livestock", "Animal shelter / kennels", 88, 75, True, 700, .40, 700, 1, 6,
       "Companion animals: high public salience, needs dedicated transport."),

    # -- residential -------------------------------------------------------
    _s("house", "residential", "Detached house", 75, 85, True, 1050, .35, 140, 2, 3),
    _s("apartments", "residential", "Apartment building", 60, 90, True, 980, .35, 500, 4, 45),
    _s("residential_building", "residential", "Residential building", 68, 85, True, 1000, .35, 300, 3, 20),
    _s("isolated_dwelling", "residential", "Isolated dwelling / masia", 90, 88, True, 1100, .35, 220, 2, 4,
       "Wildland-urban interface: highest structural ignition risk, often single access."),
    _s("static_caravan", "residential", "Static caravan / mobile home", 95, 85, True, 700, .40, 40, 1, 3),

    # -- commercial --------------------------------------------------------
    _s("shop", "commercial", "Shop", 60, 50, True, 1100, .70, 200, 1, 8),
    _s("supermarket", "commercial", "Supermarket", 60, 65, True, 1000, .90, 1800, 1, 60,
       "Doubles as a resilience asset: food and water supply during isolation."),
    _s("office", "commercial", "Office", 55, 50, True, 1250, .55, 900, 3, 90),
    _s("market", "commercial", "Market", 65, 55, True, 1000, .70, 1500, 1, 80),
    _s("restaurant", "commercial", "Restaurant / bar", 65, 45, True, 1150, .70, 250, 1, 40),
    _s("bank", "commercial", "Bank", 50, 50, True, 1300, .60, 300, 2, 12),
    _s("sports_facility", "commercial", "Sports facility", 55, 60, True, 950, .40, 1800, 1, 120,
       "Often designated as an emergency reception centre."),

    # -- tourism -----------------------------------------------------------
    _s("campsite", "tourism", "Campsite", 98, 95, True, 260, .40, 10000, 1, 350,
       "Extreme: tents and caravans offer no protection, occupants do not know the terrain."),
    _s("hotel", "tourism", "Hotel", 60, 85, True, 1450, .55, 2000, 4, 220),
    _s("rural_lodging", "tourism", "Rural lodging", 85, 85, True, 1200, .45, 350, 2, 20,
       "Typically deep in the wildland-urban interface with a single access track."),
    _s("hostel", "tourism", "Hostel / refuge", 80, 88, True, 1150, .40, 500, 2, 60),
    _s("attraction", "tourism", "Visitor attraction", 65, 70, True, 1200, .60, 1200, 1, 150),
    _s("picnic_site", "tourism", "Picnic / recreation site", 90, 70, True, 0, .0, 0, 1, 40,
       "Common ignition source and a place where the public gathers in forest."),
    _s("viewpoint", "tourism", "Viewpoint", 70, 55, True, 0, .0, 0, 1, 15),

    # -- heritage ----------------------------------------------------------
    _s("church", "heritage", "Church / place of worship", 65, 75, True, 1500, .90, 800, 1, 120,
       "Frequently used as a village assembly point."),
    _s("monument", "heritage", "Monument", 60, 70, False, 2200, 1.00, 300, 1, 0,
       "Replacement cost understates loss: heritage value is not substitutable."),
    _s("castle", "heritage", "Castle / fortification", 55, 75, True, 2400, .80, 1500, 2, 30),
    _s("museum", "heritage", "Museum", 60, 80, True, 1800, 1.50, 1500, 2, 100),
    _s("archive", "heritage", "Archive", 65, 85, True, 1600, 1.80, 900, 2, 20,
       "Contents are unique; loss is permanent."),
    _s("archaeological", "heritage", "Archaeological site", 55, 70, False, 0, .0, 0, 1, 0),

    # -- environment -------------------------------------------------------
    _s("protected_area", "environment", "Protected natural area", 90, 70, False, 0, .0, 0, 1, 0),
    _s("forest", "environment", "Forest stand", 95, 55, False, 0, .0, 0, 1, 0),
    _s("nature_reserve", "environment", "Nature reserve", 92, 70, False, 0, .0, 0, 1, 0),
    _s("wetland", "environment", "Wetland", 45, 65, False, 0, .0, 0, 1, 0),
    _s("park", "environment", "Park / green space", 75, 45, False, 0, .0, 0, 1, 20),

    # -- population --------------------------------------------------------
    _s("population_cell", "population", "Census population cell", 50, 100, True, 0, .0, 0, 1, 0,
       "1 km2 INE census grid cell, area-weighted to the AOI."),

    # -- fallback ----------------------------------------------------------
    _s("other", "commercial", "Other / unclassified", 55, 40, True, 900, .40, 300, 1, 5),
]

SUBCATEGORIES: dict[str, Subcategory] = {s.key: s for s in _SUBCATEGORIES}

# Hazard classes that should trigger an exclusion-zone recommendation downstream.
HAZARDOUS = {"fuel_station", "fuel_depot", "gas_infrastructure", "hazardous_site",
             "chemical_plant", "waste_facility", "sawmill"}

# Assets that help fight the fire rather than merely being at risk.
RESPONSE_ASSETS = {"fire_station", "water_point", "hydrant", "reservoir", "helipad",
                   "ambulance", "civil_protection", "emergency_shelter", "sports_facility"}


def get_subcategory(key: str | None) -> Subcategory:
    """Resolve a subcategory, falling back to ``other`` so callers never crash."""
    if key and key in SUBCATEGORIES:
        return SUBCATEGORIES[key]
    return SUBCATEGORIES["other"]


def category_of(sub_key: str | None) -> str:
    return get_subcategory(sub_key).category


def subcategories_for(category: str) -> list[Subcategory]:
    return [s for s in _SUBCATEGORIES if s.category == category]


def resolve_layers(layers: list[str] | None) -> set[str]:
    """Expand a user ``layers`` selection into a concrete set of category keys.

    Accepts category keys, subcategory keys, ``all``, or ``None`` (meaning all).
    """
    if not layers:
        return set(CATEGORIES)
    out: set[str] = set()
    for raw in layers:
        item = raw.strip().lower()
        if item in ("all", "*"):
            return set(CATEGORIES)
        if item in CATEGORIES:
            out.add(item)
        elif item in SUBCATEGORIES:
            out.add(SUBCATEGORIES[item].category)
    return out or set(CATEGORIES)


def as_dict() -> dict:
    """Serialisable taxonomy for ``/v1/taxonomy`` and the website."""
    return {
        "categories": [
            {**asdict(c), "subcategories": [asdict(s) for s in subcategories_for(c.key)]}
            for c in _CATEGORIES
        ],
        "hazardous_subcategories": sorted(HAZARDOUS),
        "response_asset_subcategories": sorted(RESPONSE_ASSETS),
        "counts": {"categories": len(_CATEGORIES), "subcategories": len(_SUBCATEGORIES)},
    }
