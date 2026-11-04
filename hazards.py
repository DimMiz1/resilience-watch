"""Copernicus / EU hazard layers, observed fire-spread tracking and evacuation guidance.

Everything here is derived from published observations or forecasts. Nothing is
invented when a source fails: the caller gets an explicit error / unknown state.
"""
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import time
import urllib.parse

from feeds import FeedError, age_minutes, coordinate, fetch, fetch_json, record_time, utc_now
from local_geo import bearing_deg, cluster_points, destination_point, percentile
from local_helpers import haversine_km

EFFIS_WFS = "https://maps.effis.emergency.copernicus.eu/effis"
CEMS_RM = "https://rapidmapping.emergency.copernicus.eu/backend/dashboard-api/public-activations-info/"
GLOFAS = "https://flood-api.open-meteo.com/v1/flood"
ELEVATION = "https://api.open-meteo.com/v1/elevation"
OVERPASS = ("https://overpass-api.de/api/interpreter",
            "https://overpass.private.coffee/api/interpreter",
            "https://overpass.kumi.systems/api/interpreter")
COMPASS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
PLACES_CACHE = Path(__file__).resolve().parent / ".cache"


def compass(bearing):
    return COMPASS[int((bearing % 360) / 45 + 0.5) % 8]


def parse_time(value):
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace(" ", "T", 1)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def rings(geometry):
    if not isinstance(geometry, dict):
        raise ValueError("missing geometry")
    if geometry.get("type") == "Polygon":
        polygons = [geometry["coordinates"]]
    elif geometry.get("type") == "MultiPolygon":
        polygons = geometry["coordinates"]
    else:
        raise ValueError("not a polygon")
    out = []
    for polygon in polygons:
        ring = [(coordinate(p[1], -90, 90), coordinate(p[0], -180, 180)) for p in polygon[0]]
        if len(ring) < 3:
            raise ValueError("degenerate polygon")
        out.append(ring)
    return out


def inside(lat, lon, ring):
    hit = False
    for (y1, x1), (y2, x2) in zip(ring, ring[1:] + ring[:1]):
        if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
            hit = not hit
    return hit


def distance_to_rings(lat, lon, polygon_rings):
    if any(inside(lat, lon, ring) for ring in polygon_rings):
        return 0.0
    return min(haversine_km(lat, lon, y, x) for ring in polygon_rings for y, x in ring)


def centroid(points):
    return (sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points))


# ---------------------------------------------------------------- Copernicus EFFIS

def effis_query(layer, lat, lon, half_deg):
    params = urllib.parse.urlencode({
        "SERVICE": "WFS", "VERSION": "2.0.0", "REQUEST": "GetFeature", "TYPENAMES": layer,
        "OUTPUTFORMAT": "geojson", "COUNT": 400,
        "BBOX": f"{lat - half_deg},{lon - half_deg * 1.4},{lat + half_deg},{lon + half_deg * 1.4},"
                "urn:ogc:def:crs:EPSG::4326",
    })
    data = fetch_json(EFFIS_WFS + "?" + params)
    if not isinstance(data.get("features"), list):
        raise FeedError("EFFIS returned no feature list.")
    return data["features"]


def effis_feed(lat, lon):
    """Copernicus EFFIS burnt-area perimeters (MODIS last 30 days + VIIRS near-real-time)."""
    areas, skipped = [], 0
    now = utc_now()
    for layer, kind in (("ms:modis.ba.poly.month", "EFFIS mapped burnt area (30 days)"),
                        ("ms:effis.nrt.ba.poly", "EFFIS near-real-time burnt area (VIIRS)")):
        for index, feature in enumerate(effis_query(layer, lat, lon, 1.5)):
            try:
                props = feature.get("properties") or {}
                polygon = rings(feature.get("geometry"))
                distance = distance_to_rings(lat, lon, polygon)
                if distance > 150:
                    continue
                started, updated = parse_time(props.get("FIREDATE")), parse_time(props.get("LASTUPDATE"))
                area = float(props["AREA_HA"]) if props.get("AREA_HA") not in (None, "") else None
                days = None
                if started and updated and updated >= started:
                    days = max(1.0, (updated - started).total_seconds() / 86400)
                c = centroid([p for ring in polygon for p in ring])
                areas.append({
                    "id": f"effis-{layer.split(':')[1]}-{props.get('id', index)}", "type": "burnt_area",
                    "label": " / ".join(str(props[k]) for k in ("COMMUNE", "PROVINCE", "COUNTRY") if props.get(k))
                             or kind,
                    "kind": kind, "lat": c[0], "lon": c[1], "distance_km": round(distance, 2),
                    "area_ha": area, "fire_started": props.get("FIREDATE"),
                    "growth_ha_per_day": round(area / days, 1) if area is not None and days else None,
                    "geometry": feature["geometry"], "source": "Copernicus EMS – EFFIS",
                    "report_url": "https://forest-fire.emergency.copernicus.eu/apps/effis_current_situation/",
                    "recent": bool(updated and now - updated < timedelta(days=3)) or layer.endswith("nrt.ba.poly"),
                    **record_time(props.get("LASTUPDATE")),
                })
            except (KeyError, TypeError, ValueError, IndexError):
                skipped += 1
    areas.sort(key=lambda a: a["distance_km"])
    return {"areas": areas[:60], "skipped_records": skipped,
            "message": "Mapped burnt-area perimeters. A perimeter is where land burned, not where fire is now."}


def fire_tracks(hotspots, wind_from_deg=None, link_km=2.0):
    """Group thermal detections into fires and measure observed movement between passes."""
    usable = [h for h in hotspots if parse_time(h.get("observed_at"))]
    tracks = []
    for group in cluster_points(usable, link_km):
        members = [usable[i] for i in group]
        times = sorted({parse_time(m["observed_at"]) for m in members})
        first = [(m["lat"], m["lon"]) for m in members if parse_time(m["observed_at"]) == times[0]]
        last = [(m["lat"], m["lon"]) for m in members if parse_time(m["observed_at"]) == times[-1]]
        a, b = centroid(first), centroid(last)
        moved = haversine_km(a[0], a[1], b[0], b[1]) if len(times) > 1 else 0.0
        hours = (times[-1] - times[0]).total_seconds() / 3600
        track = {
            "id": f"track-{group[0]}", "type": "fire_track", "lat": b[0], "lon": b[1],
            "detections": len(members), "passes": len(times),
            "first_seen": times[0].isoformat(), "last_seen": times[-1].isoformat(),
            "observed_shift_km": round(moved, 2), "hours_between_passes": round(hours, 1),
            "points": [[m["lat"], m["lon"]] for m in members],
            "distance_km": min(m["distance_km"] for m in members) if all("distance_km" in m for m in members) else None,
        }
        if len(times) > 1 and moved >= 0.3:
            track["observed_heading_deg"] = round(bearing_deg(a[0], a[1], b[0], b[1]))
            track["observed_heading"] = compass(track["observed_heading_deg"])
            track["observed_speed_kmh"] = round(moved / hours, 2) if hours > 0 else None
        if wind_from_deg is not None:
            downwind = (float(wind_from_deg) + 180) % 360
            track["downwind_deg"] = round(downwind)
            track["downwind"] = compass(downwind)
        tracks.append(track)
    tracks.sort(key=lambda t: (t["distance_km"] is None, t["distance_km"] or 0))
    return tracks


# ---------------------------------------------------------------- Copernicus GloFAS

def glofas_feed(lat, lon):
    """Copernicus EMS GloFAS river discharge forecast compared with the same cell's 10-year record."""
    today = utc_now().date()
    forecast = fetch_json(GLOFAS + "?" + urllib.parse.urlencode({
        "latitude": lat, "longitude": lon, "forecast_days": 30, "past_days": 7,
        "daily": "river_discharge,river_discharge_median,river_discharge_max,river_discharge_min"}))
    history = fetch_json(GLOFAS + "?" + urllib.parse.urlencode({
        "latitude": lat, "longitude": lon, "daily": "river_discharge",
        "start_date": (today - timedelta(days=3650)).isoformat(),
        "end_date": (today - timedelta(days=8)).isoformat()}))
    daily, past = forecast.get("daily"), history.get("daily")
    if not isinstance(daily, dict) or not isinstance(past, dict):
        raise FeedError("GloFAS returned no daily discharge.")
    record = [v for v in past.get("river_discharge") or [] if isinstance(v, (int, float))]
    if len(record) < 365:
        raise FeedError("GloFAS history is too short for local thresholds.")
    p50, p90, p99 = (percentile(record, q) for q in (50, 90, 99))
    peak_record = max(record)
    times = daily.get("time") or []
    upper = daily.get("river_discharge_max") or daily.get("river_discharge") or []
    median = daily.get("river_discharge_median") or daily.get("river_discharge") or []
    upcoming = [(t, m, u) for t, m, u in zip(times, median, upper)
                if t >= today.isoformat() and isinstance(m, (int, float))]
    if not upcoming:
        raise FeedError("GloFAS returned no forecast days.")
    peak_day, peak_median, peak_upper = max(upcoming, key=lambda r: r[1])
    worst_upper = max((u for _, _, u in upcoming if isinstance(u, (int, float))), default=peak_median)

    def level(value):
        if value > peak_record:
            return "extreme"
        if value > p99:
            return "high"
        if value > p90:
            return "elevated"
        return "normal"

    return {
        "cell": {"lat": forecast.get("latitude"), "lon": forecast.get("longitude")},
        "thresholds": {"median": round(p50, 3), "p90": round(p90, 3), "p99": round(p99, 3),
                       "record_max": round(peak_record, 3), "record_days": len(record)},
        "peak": {"date": peak_day, "median_m3s": peak_median,
                 "ensemble_max_m3s": worst_upper, "level": level(peak_median),
                 "worst_member_level": level(worst_upper)},
        "series": [{"date": t, "median": m, "max": u} for t, m, u in zip(times, median, upper)],
        **record_time(times[0] + "T00:00:00+00:00" if times else None),
        "message": "River discharge of the nearest ~5 km GloFAS river cell. Daily, not street-level flooding.",
    }


# ---------------------------------------------------------------- Copernicus EMS Rapid Mapping

def cems_feed(lat, lon):
    data = fetch_json(CEMS_RM + "?" + urllib.parse.urlencode({"limit": 300}))
    if not isinstance(data.get("results"), list):
        raise FeedError("CEMS Rapid Mapping returned no activation list.")
    now = utc_now()
    out, skipped = [], 0
    for item in data["results"]:
        try:
            text = str(item["centroid"])
            x, y = (float(v) for v in text[text.index("(") + 1:text.index(")")].split()[:2])
            x, y = coordinate(x, -180, 180), coordinate(y, -90, 90)
            activated = parse_time(item.get("activationTime"))
            distance = haversine_km(lat, lon, y, x)
            active = item.get("closed") is False
            out.append({
                "id": f"cems-{item['code']}", "code": str(item["code"]), "type": "activation",
                "category": str(item.get("category") or "Other"), "label": str(item.get("name") or item["code"]),
                "lat": y, "lon": x, "distance_km": round(distance, 1), "closed": bool(item.get("closed")),
                "active": active, "countries": item.get("countries") or [],
                "source": "Copernicus EMS – Rapid Mapping",
                "report_url": f"https://rapidmapping.emergency.copernicus.eu/{urllib.parse.quote(str(item['code']))}",
                **record_time(item.get("lastUpdate") or item.get("activationTime")),
            })
        except (KeyError, TypeError, ValueError):
            skipped += 1
    nearby = sorted((a for a in out if a["distance_km"] <= 600), key=lambda a: a["distance_km"])
    europe = [a for a in out if a["active"] and 34 <= a["lat"] <= 72 and -25 <= a["lon"] <= 45]
    return {"nearby": nearby[:20], "europe_active": europe[:30], "skipped_records": skipped,
            "records_returned": len(data["results"]), "provider_total": data.get("count"),
            "has_more": bool(data.get("next")), "active_europe_count_in_response": len(europe),
            "message": "Official CEMS activations, up to 300 records. Only explicitly open activations are active. "
                       "Centroids only; open the activation for mapped extents."}


# ---------------------------------------------------------------- evacuation guidance

def elevations(points):
    if not points:
        return []
    data = fetch_json(ELEVATION + "?" + urllib.parse.urlencode({
        "latitude": ",".join(f"{p[0]:.5f}" for p in points),
        "longitude": ",".join(f"{p[1]:.5f}" for p in points)}))
    values = data.get("elevation")
    if not isinstance(values, list) or len(values) != len(points):
        raise FeedError("Elevation service returned an unexpected response.")
    return [float(v) if isinstance(v, (int, float)) else None for v in values]


def overpass_places(lat, lon, radius_m=25000):
    query = (f"[out:json][timeout:15];("
             f'nwr["emergency"="assembly_point"](around:{radius_m},{lat},{lon});'
             f'nwr["amenity"~"^(hospital|fire_station)$"](around:{radius_m},{lat},{lon});'
             f'nwr["social_facility"="shelter"](around:{radius_m},{lat},{lon});'
             f");out center 80;")
    last = None
    # The primary server often answers "at capacity" briefly; retry it before slower mirrors.
    attempts = [(OVERPASS[0], 0), (OVERPASS[0], 2), (OVERPASS[0], 4)] + [(url, 0) for url in OVERPASS[1:]]
    for url, wait in attempts:
        if wait and not (last and any(code in str(last) for code in ("HTTP 429", "HTTP 502", "HTTP 503", "HTTP 504"))):
            continue
        time.sleep(wait)
        try:
            text = fetch(url, timeout=12, data=urllib.parse.urlencode({"data": query}).encode())
            data = json.loads(text)
            if not isinstance(data, dict) or not isinstance(data.get("elements"), list):
                raise FeedError("OpenStreetMap returned no valid facility list.")
            if data.get("remark"):
                raise FeedError("OpenStreetMap query was incomplete; facility coverage is unknown.")
            break
        except (FeedError, json.JSONDecodeError) as error:
            last = error
    else:
        raise FeedError(f"OpenStreetMap places unavailable ({last}).")
    places = []
    for element in data["elements"]:
        if not isinstance(element, dict):
            raise FeedError("OpenStreetMap returned an invalid facility.")
        tags = element.get("tags") or {}
        y = element.get("lat", (element.get("center") or {}).get("lat"))
        x = element.get("lon", (element.get("center") or {}).get("lon"))
        try:
            y, x = coordinate(y, -90, 90), coordinate(x, -180, 180)
        except ValueError as error:
            raise FeedError("OpenStreetMap returned invalid facility coordinates.") from error
        kind = (tags.get("emergency") or tags.get("social_facility") or tags.get("amenity") or "place").replace("_", " ")
        places.append({"name": tags.get("name:en") or tags.get("name") or kind.title(), "kind": kind,
                       "lat": y, "lon": x, "distance_km": round(haversine_km(lat, lon, y, x), 2),
                       "bearing": compass(bearing_deg(lat, lon, y, x))})
    return places


def _places_cache_file(lat, lon):
    return PLACES_CACHE / f"places_{lat:.2f}_{lon:.2f}.json"


def places_feed(lat, lon):
    """Live OSM lookup; if every mirror fails, reuse this tile's last good list (max 7 days), clearly dated."""
    cache_file = _places_cache_file(lat, lon)
    try:
        places = overpass_places(lat, lon)
    except FeedError as error:
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            age = utc_now() - datetime.fromisoformat(cached["retrieved_at"])
        except (OSError, ValueError, KeyError, TypeError):
            raise error from None
        if not timedelta(0) <= age <= timedelta(days=7):
            raise error
        cached["cached_copy"] = True
        cached["message"] = (f"Live OpenStreetMap lookup failed ({error}). Showing the last list retrieved "
                             f"{cached['retrieved_at'][:16].replace('T', ' ')} UTC. " + cached["message"])
        return cached
    for place in places:
        place.update(verified_shelter=False, access_status="unknown", capacity_status="unknown",
                     route_safety="not assessed",
                     facility_role="assembly" if place["kind"] == "assembly point" else
                                   "shelter" if place["kind"] == "shelter" else "medical" if place["kind"] == "hospital" else "response")
    # Preserve assembly/shelter candidates even when hospitals dominate a bounded result.
    places.sort(key=lambda place: (place["facility_role"] not in ("assembly", "shelter"), place["distance_km"]))
    result = {"places": places[:40], "search_radius_km": 25,
            "message": "OpenStreetMap mapped facilities, not authority-verified evacuation shelters. "
                       "Hospitals and fire stations are not necessarily public refuges. Opening, access, capacity, "
                       "road conditions and exposure to hazards are unknown. Follow local authorities.",
            "retrieved_at": utc_now().isoformat(), "observation_time": "unknown",
            "source_url": "https://www.openstreetmap.org",
            "coverage_note": "Bounded query: up to 80 mapped objects, up to 40 displayed with assembly/shelter candidates first; not an exhaustive shelter inventory."}
    try:
        PLACES_CACHE.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(result), encoding="utf-8")
    except OSError:
        pass
    return result


def hazard_points(fire_tracks_list, burnt, flood_events, activations):
    """Points to keep away from, with an avoid radius in km."""
    points = []
    for t in fire_tracks_list:
        points += [{"lat": p[0], "lon": p[1], "kind": "fire", "radius": 3.0,
                    "downwind": t.get("downwind_deg")} for p in t["points"]]
    for a in burnt:
        if a.get("recent"):
            points.append({"lat": a["lat"], "lon": a["lon"], "kind": "fire", "radius": 2.0 + math.sqrt((a.get("area_ha") or 0) / 100),
                           "downwind": None})
    for f in flood_events:
        points.append({"lat": f["lat"], "lon": f["lon"], "kind": "flood", "radius": 10.0, "downwind": None})
    for a in activations:
        if a.get("active") and a["category"].lower() in ("flood", "wildfire", "fire"):
            points.append({"lat": a["lat"], "lon": a["lon"], "kind": "flood" if a["category"].lower() == "flood" else "fire",
                           "radius": 15.0, "downwind": None})
    return points


def destination_candidates(lat, lon, feeds, accuracy_m=None):
    """Screen mapped refuge candidates, never certify a destination or travel route."""
    def data(source):
        return feeds[source].get("data") or {}

    incomplete = []
    for source, feed in feeds.items():
        age = age_minutes(feed.get("fetched_at"))
        payload = data(source)
        if (feed.get("status") != "available" or age is None or not -5 <= age <= 10
                or payload.get("skipped_records") or payload.get("has_more")):
            incomplete.append(source)
    # Keep even stale known evidence for exclusion; missing evidence never means safe.
    points = hazard_points([], data("effis").get("areas") or [],
                           [e for e in data("gdacs").get("events") or [] if e["type"] == "flood"],
                           data("cems").get("nearby") or [])
    for source in ("firms", "gdacs"):
        for event in data(source).get("events") or []:
            if event["type"] != "flood":
                points.append({"lat": event["lat"], "lon": event["lon"],
                               "kind": "fire", "radius": 3.0, "downwind": None})
    candidates, excluded = [], []
    for place in data("places").get("places") or []:
        if place.get("facility_role") not in ("assembly", "shelter"):
            continue
        y = coordinate(place["lat"], -90, 90)
        x = coordinate(place["lon"], -180, 180)
        distance = haversine_km(lat, lon, y, x)
        if distance > 25:
            continue
        conflicts = sorted({p["kind"] for p in points
                            if haversine_km(y, x, p["lat"], p["lon"]) <= p["radius"]})
        item = {**place, "distance_km": round(distance, 3),
                "distance_basis": "straight line, not walking distance",
                "verified_shelter": False, "access_status": "unknown",
                "capacity_status": "unknown", "route_safety": "not assessed",
                "safety_status": "unknown", "hazard_conflicts": conflicts,
                "screening": "excluded_known_buffer" if conflicts else "no_known_buffer_conflict",
                "map_url": "https://www.openstreetmap.org/?" + urllib.parse.urlencode(
                    {"mlat": y, "mlon": x}) + f"#map=16/{y}/{x}"}
        (excluded if conflicts else candidates).append(item)
    candidates.sort(key=lambda p: (p["distance_km"], p["name"]))
    excluded.sort(key=lambda p: (p["distance_km"], p["name"]))
    nearest = min((haversine_km(lat, lon, p["lat"], p["lon"]) - p["radius"] for p in points),
                  default=None)
    uncertain = accuracy_m is None or accuracy_m > 100
    return {
        "schema": "resilience-destinations-v1",
        "location": {"lat": lat, "lon": lon, "accuracy_m": accuracy_m},
        "generated_at": utc_now().isoformat(),
        "status": "assessment_incomplete" if incomplete or uncertain else
                  "candidates_screened" if candidates else "no_eligible_candidates",
        "safety_status": "unknown", "recommended_destination": None,
        "location_uncertain": uncertain, "incomplete_inputs": incomplete,
        "candidates": candidates[:6], "excluded": excluded, "hazard_count": len(points),
        "origin_in_known_buffer": None if nearest is None else
                                  nearest <= (accuracy_m or 0) / 1000,
        "inputs": {s: {k: f.get(k) for k in ("source", "status", "fetched_at", "error", "limitation")}
                   for s, f in feeds.items()},
        "basis": "Assembly/shelter map tags only; exclude destinations inside known report-centre "
                 "buffers (fire 3 km, recent EFFIS burn area-based, flood 10 km, CEMS 15 km). "
                 "Remaining candidates sorted by straight-line proximity, not predicted safety. "
                 "Buffers are illustrative screening assumptions, not measured hazard extents.",
        "limitations": ["No verified shelter opening, access or capacity",
                        "No road network, closures or route exposure assessment",
                        "No validated street-level inundation or fire-spread prediction",
                        "Bounded facility inventory; nearest mapped candidate is not necessarily nearest refuge",
                        "Provider retrieval time is not observation time; reports may be delayed"],
        "message": "Confirm a designated destination with local authorities before travelling. "
                   "No safe destination or route has been established."
    }


def score_direction(origin, candidate, hazards, origin_elevation, candidate_elevation, flood_risk):
    """Return (score, reasons). Higher is better. None when the candidate is inside a hazard buffer."""
    reasons = []
    clearance = math.inf
    for h in hazards:
        d = haversine_km(candidate[0], candidate[1], h["lat"], h["lon"]) - h["radius"]
        if d <= 0:
            return None, [f"inside a {h['kind']} buffer"]
        clearance = min(clearance, d)
        if h["kind"] == "fire" and h.get("downwind") is not None:
            from_fire = bearing_deg(h["lat"], h["lon"], candidate[0], candidate[1])
            off = abs((from_fire - h["downwind"] + 180) % 360 - 180)
            if off < 35 and haversine_km(candidate[0], candidate[1], h["lat"], h["lon"]) < 40:
                return None, ["downwind of an active fire"]
    score = min(clearance, 60.0) if hazards else 30.0
    if hazards:
        reasons.append(f"{clearance:.1f} km clear of the nearest known hazard buffer"
                       if math.isfinite(clearance) else "no hazard in range")
    if flood_risk and origin_elevation is not None and candidate_elevation is not None:
        gain = candidate_elevation - origin_elevation
        score += max(-30.0, min(30.0, gain / 10))
        reasons.append(f"{gain:+.0f} m elevation vs. here")
    if hazards and math.isfinite(clearance):
        here = min(haversine_km(origin[0], origin[1], h["lat"], h["lon"]) - h["radius"] for h in hazards)
        change = clearance - here
        if change < 0:
            score += change * 2
            reasons.append(f"{abs(change):.1f} km less clearance than here")
        else:
            reasons.append(f"+{change:.1f} km clearance vs. here")
    return score, reasons


def evacuation_plan(lat, lon, tracks, burnt, flood_events, activations, flood_level, places_loader=overpass_places,
                    extra_hazards=()):
    """extra_hazards: dicts with lat, lon, kind, radius (km), e.g. ML-flagged low points."""
    origin = (lat, lon)
    hazards = hazard_points(tracks, burnt, flood_events, activations)
    hazards += [{"downwind": None, **h} for h in extra_hazards]
    flood_risk = flood_level in ("elevated", "high", "extreme") or any(h["kind"] == "flood" for h in hazards)
    candidates = [(b, km, destination_point(lat, lon, b, km)) for b in range(0, 360, 45) for km in (5, 15)]
    errors = []
    try:
        heights = elevations([origin] + [c[2] for c in candidates])
    except FeedError as error:
        heights = [None] * (len(candidates) + 1)
        errors.append(f"Copernicus DEM elevation unavailable: {error}")
    options = []
    for (b, km, point), height in zip(candidates, heights[1:]):
        score, reasons = score_direction(origin, point, hazards, heights[0], height, flood_risk)
        options.append({"bearing_deg": b, "direction": compass(b), "distance_km": km,
                        "lat": round(point[0], 5), "lon": round(point[1], 5), "elevation_m": height,
                        "usable": score is not None, "score": None if score is None else round(score, 1),
                        "reasons": reasons})
    best = {}
    for option in options:
        if option["usable"] and (option["direction"] not in best or option["score"] > best[option["direction"]]["score"]):
            best[option["direction"]] = option
    ranked = sorted(best.values(), key=lambda o: -o["score"])
    avoid = sorted({o["direction"] for o in options if not o["usable"]}, key=COMPASS.index)
    nearest = min((haversine_km(lat, lon, h["lat"], h["lon"]) - h["radius"] for h in hazards), default=None)
    if nearest is not None and nearest <= 0:
        urgency = "inside"
    elif nearest is not None and nearest < 10:
        urgency = "near"
    elif hazards or flood_risk:
        urgency = "watch"
    else:
        urgency = "none_detected"
    places = []
    try:
        for place in places_loader(lat, lon):
            point = (place["lat"], place["lon"])
            blocked = next((h["kind"] for h in hazards
                            if haversine_km(point[0], point[1], h["lat"], h["lon"]) <= h["radius"]), None)
            place["in_hazard_buffer"] = blocked
            place["directions_url"] = ("https://www.openstreetmap.org/directions?" + urllib.parse.urlencode(
                {"engine": "fossgis_osrm_car", "route": f"{lat},{lon};{point[0]},{point[1]}"}))
            places.append(place)
        preferred = {o["direction"] for o in ranked[:3]}
        places.sort(key=lambda p: (p["in_hazard_buffer"] is not None, p["bearing"] not in preferred, p["distance_km"]))
    except FeedError as error:
        errors.append(str(error))
    return {"urgency": urgency, "nearest_hazard_km": None if nearest is None else round(max(0.0, nearest), 1),
            "flood_risk_considered": flood_risk, "hazard_count": len(hazards),
            "origin_elevation_m": heights[0], "recommended": ranked[:3], "avoid_directions": avoid,
            "all_options": options, "places": places[:12], "errors": errors,
            "basis": "Distance from known hazard buffers, fire wind direction and Copernicus DEM height. "
                     "No road closures, traffic or shelter capacity are known."}
