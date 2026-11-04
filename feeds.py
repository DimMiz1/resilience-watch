"""Public data adapters. Catalogue footprints are never treated as hazard areas."""
import csv
from datetime import datetime, timedelta, timezone
import json
import math
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from local_helpers import haversine_km, observation_age_minutes, parse_firms_csv

CDSE = "https://stac.dataspace.copernicus.eu/v1"
GDACS = "https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH"
FIRMS_EUROPE = ("https://firms.modaps.eosdis.nasa.gov/data/active_fire/"
                "suomi-npp-viirs-c2/csv/SUOMI_VIIRS_C2_Europe_24h.csv")
MAX_BYTES = 12 * 1024 * 1024
TTL = 300


class FeedError(Exception):
    """A provider is unavailable or returned invalid data."""


def utc_now():
    return datetime.now(timezone.utc)


def iso_now():
    return utc_now().isoformat()


def fetch(url, timeout=25, data=None):
    request = urllib.request.Request(url, data=data, headers={"User-Agent": "ResilienceWatch/0.1 (hackathon prototype)"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise FeedError("Provider response exceeded the size limit.")
        return data.decode("utf-8-sig")
    except urllib.error.HTTPError as error:
        # Do not expose provider URLs: FIRMS URLs contain a private map key.
        raise FeedError(f"Provider returned HTTP {error.code}.") from error
    except (urllib.error.URLError, OSError, UnicodeError) as error:
        raise FeedError("Provider connection failed or timed out.") from error


def fetch_json(url):
    try:
        value = json.loads(fetch(url))
    except json.JSONDecodeError as error:
        raise FeedError("Provider returned invalid JSON.") from error
    if not isinstance(value, dict):
        raise FeedError("Provider returned an unexpected response.")
    return value


def coordinate(value, low, high):
    if isinstance(value, bool):
        raise ValueError("Coordinates must be numbers.")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError("Coordinates must be numbers.") from error
    if not math.isfinite(number) or not low <= number <= high:
        raise ValueError(f"Coordinate must be between {low} and {high}.")
    return number


def query_location(query):
    # Keep one local area's requests bounded. Avoid poles / antimeridian boxes.
    lat = coordinate(query.get("lat", ["42.8742"])[0], -80, 80)
    lon = coordinate(query.get("lon", ["25.3187"])[0], -178, 178)
    return lat, lon


def area_bbox(lat, lon):
    # Approximately 100 km in either direction at Bulgaria's latitude.
    dx = min(5, 1 / max(0.2, math.cos(math.radians(lat))))
    return [max(-180, lon - dx), lat - 0.9, min(180, lon + dx), lat + 0.9]


def age_minutes(timestamp):
    if not isinstance(timestamp, str) or not timestamp:
        return None
    try:
        return round(observation_age_minutes(timestamp, utc_now()), 1)
    except (ValueError, TypeError):
        return None


def record_time(timestamp):
    age = age_minutes(timestamp)
    return {"observed_at": timestamp, "age_minutes": age,
            "older_than_24h": None if age is None else age > 1440}


def normalize_gdacs(data, lat, lon):
    features = data.get("features")
    if not isinstance(features, list):
        raise FeedError("GDACS response has no event list.")
    events = []
    skipped = 0
    for feature in features:
        try:
            props = feature["properties"]
            if not isinstance(props, dict):
                raise ValueError("Invalid event properties.")
            if props.get("eventtype") not in ("FL", "WF"):
                continue
            geometry = feature["geometry"]
            if not isinstance(geometry, dict) or geometry.get("type") != "Point":
                raise ValueError("Expected an event centroid.")
            x, y = geometry["coordinates"][:2]
            x, y = coordinate(x, -180, 180), coordinate(y, -90, 90)
            distance = haversine_km(lat, lon, y, x)
            if distance > 150:
                continue
            event_id = str(props["eventid"])
            if not event_id.isdigit():
                raise ValueError("Invalid event ID.")
            event_type = props["eventtype"]
            events.append({
                "id": f"gdacs-{event_type}-{event_id}",
                "type": "flood" if event_type == "FL" else "fire",
                "label": str(props.get("name") or props.get("eventname") or event_type),
                "lat": y, "lon": x, "distance_km": round(distance, 1),
                "alert": str(props.get("alertlevel") or "Unknown"),
                "source": "GDACS", "geometry_kind": "event centroid, not extent",
                "starts_at": props.get("fromdate"), "ends_at": props.get("todate"),
                "source_reports_current": str(props.get("iscurrent")).lower() == "true",
                "report_url": "https://www.gdacs.org/report.aspx?" + urllib.parse.urlencode({
                    "eventtype": event_type, "eventid": event_id}),
                **record_time(props.get("datemodified")),
            })
        except (KeyError, TypeError, ValueError, IndexError):
            skipped += 1
    return {"events": sorted(events, key=lambda e: e["distance_km"]), "skipped_records": skipped}


def normalize_firms(text, lat, lon):
    try:
        rows, skipped = parse_firms_csv(text)
    except (ValueError, csv.Error) as error:
        raise FeedError("FIRMS returned invalid CSV. Check the provider status, map key and quota.") from error
    hotspots = []
    for index, row in enumerate(rows):
        y, x = row["lat"], row["lon"]
        distance = haversine_km(lat, lon, y, x)
        if distance > 150:
            continue
        hotspots.append({
            "id": f"firms-{index}", "type": "hotspot", "label": "VIIRS thermal anomaly",
            "lat": y, "lon": x, "distance_km": round(distance, 1),
            "confidence": row["confidence"],
            "source": "NASA FIRMS / VIIRS SNPP NRT",
            "geometry_kind": "sensor detection, not fire perimeter",
            "report_url": "https://firms.modaps.eosdis.nasa.gov/map/",
            **record_time(row["observed_at"]),
        })
    latest = max((row["observed_at"] for row in rows), default=None)
    return {"events": sorted(hotspots, key=lambda e: e["distance_km"]), "skipped_records": skipped,
            "latest_source_observation": latest, "source_observation_age_minutes": age_minutes(latest)}


def gdacs_feed(lat, lon):
    return normalize_gdacs(fetch_json(GDACS), lat, lon)


def firms_feed(lat, lon):
    key = os.environ.get("FIRMS_MAP_KEY", "").strip()
    if not key:
        if not (41 <= lat <= 45 and 22 <= lon <= 29):
            return {"configuration_required": True, "events": [],
                    "message": "Outside this prototype's Bulgaria public-feed support area. "
                               "Set FIRMS_MAP_KEY for area API coverage; local hotspot coverage is unknown."}
        result = normalize_firms(fetch(FIRMS_EUROPE), lat, lon)
        result.update(window_hours=24, mode="public-europe",
                      message="Public NASA Europe 24-hour CSV, filtered to 150 km. No API key required for Bulgaria.")
        return result
    bbox = ",".join(f"{n:.5f}" for n in area_bbox(lat, lon))
    url = ("https://firms.modaps.eosdis.nasa.gov/api/area/csv/"
           f"{urllib.parse.quote(key, safe='')}/VIIRS_SNPP_NRT/{bbox}/2")
    result = normalize_firms(fetch(url), lat, lon)
    result.update(window_hours=48, mode="area-api",
                  message="NASA area API, past two days; detections limited to the requested box and 150 km.")
    return result


def weather_feed(lat, lon):
    params = urllib.parse.urlencode({
        "latitude": lat, "longitude": lon, "timezone": "UTC", "models": "ecmwf_ifs025",
        "current": "temperature_2m,precipitation,wind_speed_10m,wind_direction_10m",
    })
    data = fetch_json("https://api.open-meteo.com/v1/forecast?" + params)
    current, units = data.get("current"), data.get("current_units")
    if not isinstance(current, dict) or not isinstance(units, dict) or not current.get("time"):
        raise FeedError("Weather provider returned no current model data.")
    return {"current": current, "units": units, **record_time(current["time"]),
            "kind": "Weather model estimate, not a local sensor or spread forecast."}


def satellite_feed(lat, lon):
    now = utc_now()
    params = urllib.parse.urlencode({
        "collections": "sentinel-1-grd,sentinel-2-l2a",
        "bbox": ",".join(str(n) for n in area_bbox(lat, lon)),
        "datetime": f"{(now - timedelta(days=14)).isoformat()}/{now.isoformat()}",
        "sortby": "-datetime", "limit": 8,
    })
    data = fetch_json(CDSE + "/search?" + params)
    if not isinstance(data.get("features"), list):
        raise FeedError("Copernicus returned no STAC feature list.")
    scenes, skipped = [], 0
    for feature in data["features"]:
        try:
            props = feature["properties"]
            if not isinstance(props, dict):
                raise ValueError("Invalid scene properties.")
            stamp = props.get("datetime") or props.get("start_datetime")
            geometry = feature["geometry"]
            validate_geometry(geometry)
            scenes.append({"id": feature["id"], "collection": feature.get("collection", "Sentinel"),
                           "cloud_percent": props.get("eo:cloud_cover"), "geometry": geometry,
                           "catalogue_url": "https://browser.stac.dataspace.copernicus.eu/",
                           **record_time(stamp)})
        except (KeyError, TypeError, ValueError):
            skipped += 1
    return {"scenes": scenes, "skipped_records": skipped,
            "message": "Acquisition footprints only. No imagery analysis or flood/fire extraction is performed.",
            "search_window_days": 14, "limit": 8}


def validate_geometry(geometry):
    if not isinstance(geometry, dict) or geometry.get("type") not in ("Polygon", "MultiPolygon"):
        raise ValueError("Unexpected footprint geometry.")
    polygons = [geometry.get("coordinates")] if geometry["type"] == "Polygon" else geometry.get("coordinates")
    if not isinstance(polygons, list) or not polygons:
        raise ValueError("Empty footprint.")
    for polygon in polygons:
        if not isinstance(polygon, list) or not polygon:
            raise ValueError("Invalid polygon.")
        for ring in polygon:
            if not isinstance(ring, list) or len(ring) < 4 or ring[0] != ring[-1]:
                raise ValueError("Invalid polygon ring.")
            for point in ring:
                if not isinstance(point, list) or len(point) < 2:
                    raise ValueError("Invalid footprint coordinate.")
                coordinate(point[0], -180, 180)
                coordinate(point[1], -90, 90)


SOURCES = {
    "gdacs": ("GDACS disaster reports", gdacs_feed,
              "Major flood and wildfire reports within 150 km by centroid. Not all local incidents are covered."),
    "firms": ("NASA FIRMS thermal anomalies", firms_feed,
              "Public Europe 24h download for Bulgaria, or 48h area API with a map key. Delayed detections, not confirmed fires or perimeters."),
    "weather": ("ECMWF IFS weather (via Open-Meteo)", weather_feed,
                "Model estimates at the selected point. Wind alone cannot predict fire spread."),
    "satellite": ("Copernicus Sentinel catalogue", satellite_feed,
                  "Latest 8 catalogue acquisitions intersecting the area over 14 days. Not hazard extents."),
}


class FeedCache:
    """Bounded cache; failures retain last successful data with an explicit stale flag."""

    def __init__(self):
        self.lock = threading.Lock()
        self.source_locks = {source: threading.Lock() for source in SOURCES}
        self.entries = {}

    def get(self, source, lat, lon):
        key = (source, round(lat, 4), round(lon, 4))
        with self.source_locks[source]:
            # One request per provider at a time; unrelated providers can load concurrently.
            with self.lock:
                entry = self.entries.get(key)
            if entry and time.monotonic() - entry["_checked"] < TTL:
                return self.public(entry)
            name, loader, limitation = SOURCES[source]
            old = entry or {}
            try:
                payload = loader(lat, lon)
                state = "unconfigured" if payload.get("configuration_required") else "available"
                entry = {"source": name, "status": state, "data": payload,
                         "fetched_at": iso_now() if state == "available" else None,
                         "last_attempt_at": iso_now(),
                         "error": None, "limitation": limitation}
            except FeedError as error:
                entry = {"source": name, "status": "stale" if old.get("data") else "error",
                         "data": old.get("data"), "fetched_at": old.get("fetched_at"),
                         "last_attempt_at": iso_now(), "error": str(error), "limitation": limitation}
            entry["_checked"] = time.monotonic()
            with self.lock:
                if len(self.entries) >= 128 and key not in self.entries:
                    del self.entries[next(iter(self.entries))]
                self.entries[key] = entry
            return self.public(entry)

    @staticmethod
    def public(entry):
        return {key: value for key, value in entry.items() if not key.startswith("_")}
