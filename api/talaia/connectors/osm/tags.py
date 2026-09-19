"""OpenStreetMap tag -> TALAIA taxonomy crosswalk.

OSM has no schema, only convention, so classification is an ordered rule list: the first
matching rule wins and rules run most-specific first. Anything unmatched is dropped
rather than guessed - a wrongly classified asset is worse than a missing one when the
output drives an evacuation decision.
"""
from __future__ import annotations

from typing import Any

# Values of `amenity` worth fetching. Without an allowlist a tile returns benches,
# post boxes and bicycle parking, which inflate the payload and the asset count.
AMENITY_ALLOW = (
    "hospital|clinic|doctors|pharmacy|dentist|nursing_home|social_facility|childcare|"
    "kindergarten|school|college|university|library|archive|fire_station|police|"
    "ambulance_station|townhall|courthouse|prison|post_office|place_of_worship|"
    "restaurant|cafe|bar|pub|fast_food|food_court|bank|fuel|charging_station|"
    "marketplace|shelter|veterinary|animal_shelter|animal_boarding|community_centre|"
    "theatre|cinema|arts_centre|conference_centre|exhibition_centre|parking|"
    "waste_transfer_station|recycling|water_point|drinking_water|fountain|"
    "bus_station|ferry_terminal|driving_school|research_institute|grave_yard"
)
MAN_MADE_ALLOW = (
    "water_tower|water_works|wastewater_plant|reservoir_covered|storage_tank|silo|"
    "mast|tower|communications_tower|pipeline|works|pumping_station|petroleum_well|"
    "gasometer|chimney|bunker_silo"
)
POWER_ALLOW = "substation|plant|generator|transformer|portal"
LANDUSE_ALLOW = (
    "industrial|farmyard|quarry|retail|commercial|cemetery|vineyard|orchard|"
    "greenhouse_horticulture|forest|allotments|military"
)
LEISURE_ALLOW = (
    "park|sports_centre|pitch|stadium|swimming_pool|nature_reserve|golf_course|"
    "water_park|marina|fitness_centre"
)
BUILDING_ALLOW = (
    "hospital|school|kindergarten|university|college|church|chapel|cathedral|mosque|"
    "synagogue|farm|farm_auxiliary|barn|stable|cowshed|greenhouse|industrial|"
    "warehouse|commercial|retail|hotel|civic|public|fire_station|train_station|"
    "transportation|hangar|static_caravan|bungalow|dormitory|residential|apartments|"
    "house|detached|terrace|manor|castle"
)
HIGHWAY_ALLOW = (
    "motorway|trunk|primary|secondary|tertiary|unclassified|residential|track|"
    "motorway_link|trunk_link|primary_link|secondary_link|living_street"
)
RAILWAY_ALLOW = "rail|light_rail|narrow_gauge|subway|tram|funicular"
WATERWAY_ALLOW = "river|stream|canal"

# Linear features become `networks` rows, not assets.
LINEAR_SUBCATEGORY = {
    "motorway": "motorway", "motorway_link": "motorway",
    "trunk": "primary_road", "trunk_link": "primary_road",
    "primary": "primary_road", "primary_link": "primary_road",
    "secondary": "secondary_road", "secondary_link": "secondary_road",
    "tertiary": "secondary_road",
    "unclassified": "local_road", "residential": "local_road",
    "living_street": "local_road", "track": "track",
}


def _has(tags: dict, key: str, *values: str) -> bool:
    v = tags.get(key)
    return bool(v) and (not values or v in values)


def classify_linear(tags: dict[str, Any]) -> str | None:
    """Subcategory for a linear feature, or None if it is not one."""
    hw = tags.get("highway")
    if hw:
        return LINEAR_SUBCATEGORY.get(hw)
    if tags.get("power") == "line" or tags.get("power") == "minor_line":
        return "power_line"
    if tags.get("railway") in ("rail", "light_rail", "narrow_gauge", "subway",
                               "tram", "funicular"):
        return "railway"
    if tags.get("waterway") in ("river", "stream", "canal"):
        return None  # watercourses are context, not exposure; skipped for now
    if tags.get("man_made") == "pipeline":
        return "gas_infrastructure"
    return None


def classify(tags: dict[str, Any]) -> str | None:
    """Map OSM tags to a TALAIA subcategory. Returns None when nothing fits."""
    if not tags:
        return None
    t = tags

    # -- explicit hazards first: they change the recommended action -------------
    if _has(t, "amenity", "fuel") or _has(t, "shop", "fuel"):
        return "fuel_station"
    if t.get("man_made") in ("storage_tank", "gasometer") or _has(t, "landuse", "depot"):
        if "fuel" in str(t.get("content", "")) or "oil" in str(t.get("content", "")):
            return "fuel_depot"
        return "fuel_depot" if t.get("man_made") == "gasometer" else "warehouse"
    if t.get("man_made") == "pipeline" and t.get("substance") in ("gas", "oil", "fuel"):
        return "gas_infrastructure"
    if _has(t, "industrial", "chemical") or _has(t, "man_made", "works") and \
            "chem" in str(t.get("product", "")).lower():
        return "chemical_plant"
    if _has(t, "craft", "sawmill") or _has(t, "industrial", "sawmill"):
        return "sawmill"

    # -- emergency ---------------------------------------------------------------
    em = t.get("emergency")
    if em == "fire_hydrant":
        return "hydrant"
    if em in ("water_tank", "suction_point", "fire_water_pond"):
        return "water_point"
    if em == "assembly_point":
        return "emergency_shelter"
    if em == "ambulance_station" or _has(t, "amenity", "ambulance_station"):
        return "ambulance"
    if _has(t, "amenity", "fire_station") or _has(t, "building", "fire_station"):
        return "fire_station"
    if _has(t, "amenity", "police"):
        return "police"
    if _has(t, "aeroway", "helipad") or _has(t, "aeroway", "heliport"):
        return "helipad"

    # -- healthcare ---------------------------------------------------------------
    hc = t.get("healthcare")
    if _has(t, "amenity", "hospital") or hc == "hospital" or _has(t, "building", "hospital"):
        return "hospital"
    if _has(t, "amenity", "pharmacy") or hc == "pharmacy":
        return "pharmacy"
    if _has(t, "amenity", "clinic", "doctors", "dentist") or hc in ("centre", "clinic",
                                                                    "doctor", "dentist"):
        return "clinic"
    if hc == "psychiatry" or _has(t, "social_facility", "group_home") and \
            "mental" in str(t.get("social_facility:for", "")):
        return "mental_health"
    if _has(t, "amenity", "veterinary") or hc == "veterinary":
        return "veterinary"

    # -- social care ---------------------------------------------------------------
    sf_for = str(t.get("social_facility:for", ""))
    if _has(t, "amenity", "nursing_home") or t.get("social_facility") == "nursing_home":
        return "care_home"
    if t.get("social_facility"):
        if "senior" in sf_for or "elderly" in sf_for:
            return "care_home" if t.get("social_facility") != "day_care" else "day_centre"
        if "disab" in sf_for:
            return "disability_home"
        if "homeless" in sf_for:
            return "shelter"
        if "child" in sf_for:
            return "childcare_residential"
        return "social_services"
    if _has(t, "amenity", "shelter") and t.get("shelter_type") == "public_transport":
        return None

    # -- education ------------------------------------------------------------------
    if _has(t, "amenity", "kindergarten", "childcare") or _has(t, "building", "kindergarten"):
        return "nursery"
    if _has(t, "amenity", "university") or _has(t, "building", "university"):
        return "university"
    if _has(t, "amenity", "college") or _has(t, "building", "college"):
        return "vocational"
    if _has(t, "amenity", "school") or _has(t, "building", "school"):
        isced = str(t.get("isced:level", ""))
        if isced.startswith("0"):
            return "nursery"
        if isced.startswith("1"):
            return "primary_school"
        if isced.startswith(("2", "3")):
            return "secondary_school"
        return "school"
    if _has(t, "amenity", "library"):
        return "library"
    if _has(t, "amenity", "archive"):
        return "archive"
    if _has(t, "amenity", "research_institute"):
        return "research"
    if _has(t, "amenity", "driving_school"):
        return "vocational"

    # -- energy / utilities -----------------------------------------------------------
    pw = t.get("power")
    if pw == "substation" or pw == "transformer":
        return "substation"
    if pw == "plant":
        src = str(t.get("plant:source", ""))
        return "solar_farm" if "solar" in src else "wind_farm" if "wind" in src \
            else "power_plant"
    if pw == "generator":
        src = str(t.get("generator:source", ""))
        return "solar_farm" if "solar" in src else "wind_farm" if "wind" in src \
            else "power_plant"
    if pw == "tower" or pw == "portal":
        return "power_tower"
    mm = t.get("man_made")
    if mm == "water_tower":
        return "water_tower"
    if mm in ("water_works", "reservoir_covered"):
        return "water_treatment"
    if mm == "wastewater_plant":
        return "wastewater"
    if mm == "pumping_station":
        return "pumping_station"
    if mm in ("mast", "tower", "communications_tower"):
        ttype = str(t.get("tower:type", "")) + str(t.get("mast:type", ""))
        if "communication" in ttype or t.get("communication:mobile_phone") == "yes":
            return "telecom_mast"
        return "telecom_mast" if mm == "communications_tower" else None
    if mm == "silo" or mm == "bunker_silo":
        return "silo"
    if mm == "works":
        return "factory"
    if t.get("telecom") == "exchange" or t.get("telecom") == "data_center":
        return "data_centre" if t.get("telecom") == "data_center" else "telecom_exchange"

    # -- water bodies ------------------------------------------------------------------
    if t.get("water") == "reservoir" or _has(t, "landuse", "reservoir") or \
            _has(t, "natural", "water") and t.get("water") in ("reservoir", "pond", "lake"):
        return "reservoir"
    if _has(t, "amenity", "water_point", "drinking_water"):
        return "water_point"

    # -- transport -----------------------------------------------------------------------
    if _has(t, "aeroway", "aerodrome"):
        return "airport"
    if t.get("railway") in ("station", "halt") or _has(t, "public_transport", "station"):
        return "station"
    if _has(t, "amenity", "bus_station") or _has(t, "amenity", "ferry_terminal"):
        return "station"
    if _has(t, "amenity", "parking"):
        return "parking"
    if t.get("bridge") in ("yes", "viaduct"):
        return "bridge"
    if t.get("tunnel") == "yes":
        return "tunnel"

    # -- industry / agriculture ------------------------------------------------------------
    if _has(t, "landuse", "quarry") or _has(t, "man_made", "mineshaft"):
        return "quarry"
    if _has(t, "amenity", "waste_transfer_station") or _has(t, "landuse", "landfill") or \
            _has(t, "amenity", "recycling") and t.get("recycling_type") == "centre":
        return "waste_facility"
    if _has(t, "landuse", "industrial") or _has(t, "building", "industrial"):
        return "factory"
    if _has(t, "building", "warehouse"):
        return "warehouse"
    if _has(t, "craft"):
        return "workshop"
    if _has(t, "landuse", "farmyard") or _has(t, "place", "farm"):
        return "farm"
    if _has(t, "building", "greenhouse") or _has(t, "landuse", "greenhouse_horticulture"):
        return "greenhouse"
    if _has(t, "building", "farm", "farm_auxiliary", "barn", "stable", "cowshed"):
        return "farm_building"
    if _has(t, "landuse", "vineyard", "orchard"):
        return "orchard"
    if _has(t, "craft", "winery") or t.get("industrial") == "winery":
        return "winery"
    if _has(t, "amenity", "animal_shelter", "animal_boarding"):
        return "animal_shelter"

    # -- tourism ----------------------------------------------------------------------------
    tm = t.get("tourism")
    if tm in ("camp_site", "caravan_site", "camp_pitch"):
        return "campsite"
    if tm == "hotel" or _has(t, "building", "hotel"):
        return "hotel"
    if tm in ("hostel", "alpine_hut", "wilderness_hut"):
        return "hostel"
    if tm in ("guest_house", "chalet", "apartment"):
        return "rural_lodging"
    if tm == "museum":
        return "museum"
    if tm == "viewpoint":
        return "viewpoint"
    if tm == "picnic_site" or _has(t, "leisure", "picnic_table"):
        return "picnic_site"
    if tm in ("attraction", "theme_park", "zoo", "aquarium", "gallery"):
        return "attraction"

    # -- heritage ----------------------------------------------------------------------------
    hist = t.get("historic")
    if hist in ("castle", "fort", "city_gate", "tower"):
        return "castle"
    if hist in ("church", "chapel", "monastery", "wayside_shrine"):
        return "church"
    if hist in ("monument", "memorial"):
        return "monument"
    if hist == "archaeological_site":
        return "archaeological"
    if hist == "ruins":
        return "monument"
    if _has(t, "amenity", "place_of_worship") or _has(t, "building", "church", "chapel",
                                                      "cathedral", "mosque", "synagogue"):
        return "church"

    # -- environment --------------------------------------------------------------------------
    if _has(t, "leisure", "nature_reserve") or t.get("boundary") == "protected_area":
        return "protected_area"
    if _has(t, "natural", "wood") or _has(t, "landuse", "forest"):
        return "forest"
    if _has(t, "natural", "wetland"):
        return "wetland"
    if _has(t, "leisure", "park", "garden"):
        return "park"
    if _has(t, "leisure", "sports_centre", "stadium", "pitch", "swimming_pool",
            "golf_course", "water_park", "fitness_centre"):
        return "sports_facility"

    # -- commercial & civic ----------------------------------------------------------------------
    shop = t.get("shop")
    if shop in ("supermarket", "convenience", "grocery", "general"):
        return "supermarket"
    if shop:
        return "shop"
    if _has(t, "amenity", "marketplace"):
        return "market"
    if _has(t, "amenity", "restaurant", "cafe", "bar", "pub", "fast_food", "food_court"):
        return "restaurant"
    if _has(t, "amenity", "bank"):
        return "bank"
    if _has(t, "amenity", "townhall", "courthouse", "post_office") or t.get("office"):
        return "office"
    if _has(t, "amenity", "community_centre", "theatre", "cinema", "arts_centre",
            "conference_centre", "exhibition_centre"):
        return "attraction"
    if _has(t, "amenity", "grave_yard") or _has(t, "landuse", "cemetery"):
        return "other"

    # -- residential (last: the broadest bucket) ----------------------------------------------
    b = t.get("building")
    if b in ("apartments", "dormitory"):
        return "apartments"
    if b in ("house", "detached", "bungalow", "manor"):
        return "house"
    if b == "static_caravan":
        return "static_caravan"
    if b in ("residential", "terrace"):
        return "residential_building"
    if b in ("civic", "public"):
        return "office"
    if b in ("castle",):
        return "castle"
    return None


def _poi_filters(b: str, prefix: str) -> str:
    return "\n  ".join([
        f'{prefix}[amenity~"^({AMENITY_ALLOW})$"]({b});',
        f'{prefix}[healthcare]({b});',
        f'{prefix}[social_facility]({b});',
        f'{prefix}[emergency~"^(fire_hydrant|water_tank|suction_point|assembly_point|'
        f'ambulance_station)$"]({b});',
        f'{prefix}[tourism]({b});',
        f'{prefix}[historic]({b});',
        f'{prefix}[shop]({b});',
        f'{prefix}[office]({b});',
        f'{prefix}[craft]({b});',
        f'{prefix}[man_made~"^({MAN_MADE_ALLOW})$"]({b});',
        f'{prefix}[power~"^({POWER_ALLOW})$"]({b});',
        f'{prefix}[landuse~"^({LANDUSE_ALLOW})$"]({b});',
        f'{prefix}[leisure~"^({LEISURE_ALLOW})$"]({b});',
        f'{prefix}[aeroway~"^(aerodrome|helipad|heliport)$"]({b});',
        f'{prefix}[railway~"^(station|halt)$"]({b});',
        f'{prefix}[building~"^({BUILDING_ALLOW})$"]({b});',
    ])


def build_query(bbox: tuple[float, float, float, float], timeout: int = 90) -> str:
    """Overpass QL for one bbox.

    Three output statements, each chosen to trade payload against usefulness:

    * nodes and ways -> ``out geom``: ways give building footprints, which raise
      valuation confidence from "subcategory default" to "measured area".
    * relations -> ``out center``: a ``landuse=forest`` multipolygon relation can carry
      tens of thousands of coordinates, so only its centroid is worth fetching.
    * linear ways -> ``out geom``: the geometry *is* the asset for a road.
    """
    x0, y0, x1, y1 = bbox
    b = f"{y0:.6f},{x0:.6f},{y1:.6f},{x1:.6f}"
    linear = "\n  ".join([
        f'way[highway~"^({HIGHWAY_ALLOW})$"]({b});',
        f'way[power~"^(line|minor_line)$"]({b});',
        f'way[railway~"^({RAILWAY_ALLOW})$"]({b});',
    ])
    return (f"[out:json][timeout:{timeout}];\n"
            f"(\n  {_poi_filters(b, 'nw')}\n);\nout geom tags;\n"
            f"(\n  {_poi_filters(b, 'relation')}\n);\nout center tags;\n"
            f"(\n  {linear}\n);\nout geom tags;\n")
