"""Location context and curated official contacts, never inferred from hazard scores."""
import os
import threading
import time
import urllib.parse

from feeds import FeedError, coordinate, fetch_json, utc_now
from local_helpers import haversine_km

EU_NUMBER_SOURCE = "https://europa.eu/youreurope/citizens/travel/security-and-emergencies/emergency/index_en.htm"
EU_DIRECTORY = "https://civil-protection-humanitarian-aid.ec.europa.eu/what/civil-protection/national-disaster-management-system"
REVIEWED = "2026-11-03"
EU_COUNTRIES = {
    "AT": ("Austria", "austria"), "BE": ("Belgium", "belgium"), "BG": ("Bulgaria", "bulgaria"),
    "HR": ("Croatia", "republic-croatia"), "CY": ("Cyprus", "republic-cyprus"),
    "CZ": ("Czechia", "czech-republic"), "DK": ("Denmark", "denmark"), "EE": ("Estonia", "estonia"),
    "FI": ("Finland", "finland"), "FR": ("France", "france"), "DE": ("Germany", "germany"),
    "GR": ("Greece", "greece"), "HU": ("Hungary", "hungary"), "IE": ("Ireland", "ireland"),
    "IT": ("Italy", "italy"), "LV": ("Latvia", "latvia"), "LT": ("Lithuania", None),
    "LU": ("Luxembourg", "luxembourg"), "MT": ("Malta", "malta"), "NL": ("Netherlands", "netherlands"),
    "PL": ("Poland", "poland"), "PT": ("Portugal", "portugal"), "RO": ("Romania", "romania"),
    "SK": ("Slovakia", "slovakia"), "SI": ("Slovenia", "slovenia"), "ES": ("Spain", "spain"),
    "SE": ("Sweden", "sweden"),
}
NATIONAL = {
    "BG": [("BG-ALERT", "https://www.bg-alert.bg/"), ("Ministry of Interior", "https://www.mvr.bg/")],
    "IT": [("Dipartimento della Protezione Civile", "https://www.protezionecivile.gov.it/en/")],
    "ES": [("Proteccion Civil y Emergencias", "https://www.proteccioncivil.es/")],
    "CZ": [("HZS CR - fire and rescue service", "https://hzscr.gov.cz/")],
    "DE": [("BBK - civil protection", "https://www.bbk.bund.de/EN/Home/home_node.html")],
    "FR": [("Service Public - emergency contacts", "https://www.service-public.gouv.fr/particuliers/vosdroits/F33954")],
    "LT": [("Emergency Response Centre", "https://112.lt/en/main/")],
    "GB": [("UK emergency services", "https://www.gov.uk/guidance/999-and-112-the-uks-national-emergency-numbers")],
    "US": [("National 911 Program", "https://www.911.gov/calling-911/")],
}
# Match only a known administrative name within its own country, not a broad bounding box.
REGIONAL = [
    ("BG", "state", ("sofia-city", "sofia city"), "Sofia municipal emergency assistance",
     "https://www.sofia.bg/bg/emergency-assistance-prevention"),
    ("BG", "city", ("sofia",), "Sofia municipal emergency assistance",
     "https://www.sofia.bg/bg/emergency-assistance-prevention"),
    ("BG", "city", ("gabrovo",), "Gabrovo municipality", "https://gabrovo.bg/bg"),
    ("BG", "city", ("plovdiv",), "Plovdiv municipality", "https://www.plovdiv.bg/"),
    ("BG", "city", ("varna",), "Varna municipality", "https://www.varna.bg/"),
    ("IT", "state", ("lombardy", "lombardia"), "Regione Lombardia - allertaLOM",
     "https://www.allertalom.regione.lombardia.it/"),
    ("ES", "state", ("valencian community", "comunitat valenciana", "comunidad valenciana"),
     "112 Comunitat Valenciana", "https://www.112cv.gva.es/"),
    ("CZ", "city", ("prague", "praha"), "HZS hlavniho mesta Prahy",
     "https://hzscr.gov.cz/hlavni-mesto-praha/organizacni-slozky"),
]
EXTRA_NUMBERS = {
    "FR": [("Medical emergency (SAMU)", "15"), ("Fire brigade", "18"), ("Police", "17")],
    "CZ": [("Fire brigade", "150"), ("Ambulance", "155"), ("Police", "158")],
    "DE": [("Police", "110")],
    "GB": [("Alternative emergency number", "112")],
}
NUMBER_SOURCES = {
    "FR": NATIONAL["FR"][0][1],
    "CZ": "https://hzscr.gov.cz/tisnova-komunikace-v-ceske-republice",
    "DE": "https://www.bbk.bund.de/SharedDocs/Kurzmeldungen/DE/2026/02/om-11-tag-notruf.html",
    "GB": NATIONAL["GB"][0][1], "US": NATIONAL["US"][0][1],
}
_lock = threading.Lock()
_cache = {}
_last_request = 0.0


def contact_context(code, properties):
    """Only curated numbers and links may become actionable emergency contacts."""
    code = code.upper()
    country = EU_COUNTRIES[code][0] if code in EU_COUNTRIES else {"GB": "United Kingdom", "US": "United States"}.get(code)
    if not country:
        country = properties.get("country") or code
    number = "112" if code in EU_COUNTRIES else {"GB": "999", "US": "911"}.get(code)
    national = NATIONAL.get(code, [])
    if not national and code in EU_COUNTRIES:
        slug = EU_COUNTRIES[code][1]
        national = [(f"{country} civil protection - EU authority directory", f"{EU_DIRECTORY}/{slug}_en")]
    regional = list(dict.fromkeys((name, url) for c, field, aliases, name, url in REGIONAL
                                 if c == code and str(properties.get(field, "")).casefold() in aliases))
    return {
        "country_code": code, "country": country,
        "region": properties.get("state") or properties.get("county"),
        "city": properties.get("city"),
        "primary_number": number,
        "number_source": EU_NUMBER_SOURCE if code in EU_COUNTRIES else NUMBER_SOURCES.get(code),
        "additional_numbers": [{"service": service, "number": value, "source_url": NUMBER_SOURCES[code]}
                               for service, value in EXTRA_NUMBERS.get(code, [])],
        "national_authorities": [{"name": name, "url": url} for name, url in national],
        "regional_authorities": [{"name": name, "url": url} for name, url in regional],
        "regional_status": "configured" if regional else "not configured for this administrative area",
        "contacts_reviewed": REVIEWED,
        "message": "Approximate OSM administrative context, not a dispatch-jurisdiction determination. "
                   "Confirm the country, especially near borders. Links are curated official sources, not live alerts. "
                   "A call connects where your phone is, not remotely to the selected map area.",
    }


def normalize_context(data, lat, lon):
    features = data.get("features")
    if not isinstance(features, list) or not features:
        raise FeedError("Country and region could not be identified. No emergency number was assumed.")
    feature = features[0]
    if not isinstance(feature, dict):
        raise FeedError("Administrative lookup returned an invalid location.")
    props, geometry = feature.get("properties"), feature.get("geometry")
    if not isinstance(props, dict) or not isinstance(geometry, dict):
        raise FeedError("Administrative lookup returned no country context.")
    code, point = props.get("countrycode"), geometry.get("coordinates")
    if not isinstance(code, str) or len(code) != 2 or not code.isascii() or not code.isalpha():
        raise FeedError("Administrative lookup returned no valid country code.")
    try:
        if geometry.get("type") != "Point" or not isinstance(point, list) or len(point) != 2:
            raise ValueError("Invalid point.")
        x, y = coordinate(point[0], -180, 180), coordinate(point[1], -90, 90)
        if haversine_km(lat, lon, y, x) > 5:
            raise ValueError("No nearby administrative match.")
    except ValueError as error:
        raise FeedError("Country lookup is too uncertain. No emergency number was assumed.") from error
    for field in ("city", "state", "county", "country"):
        if props.get(field) is not None and not isinstance(props[field], str):
            raise FeedError("Administrative lookup returned an invalid region name.")
    result = contact_context(code, props)
    result.update(retrieved_at=utc_now().isoformat(),
                  location_source="Photon / OpenStreetMap contributors",
                  location_source_url="https://www.openstreetmap.org/copyright")
    return result


def emergency_feed(lat, lon):
    """One bounded lookup per selected point; cache in memory and rate-limit demo use."""
    global _last_request
    key = (round(lat, 4), round(lon, 4))
    with _lock:
        cached = _cache.get(key)
        if cached and time.monotonic() - cached[0] < 86400:
            return cached[1]
        wait = 1.1 - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()
        endpoint = os.environ.get("PHOTON_REVERSE_URL", "https://photon.komoot.io/reverse")
        query = urllib.parse.urlencode({"lat": lat, "lon": lon, "limit": 1, "lang": "en"})
        result = normalize_context(fetch_json(endpoint + "?" + query), lat, lon)
        if len(_cache) >= 128:
            del _cache[next(iter(_cache))]
        _cache[key] = (time.monotonic(), result)
        return result
