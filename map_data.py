"""Bounded, traceable map layers: forecast samples and terrain, never inferred hazard extents."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import math
import urllib.parse

from feeds import FeedError, coordinate, fetch, record_time, utc_now

MAP_SOURCES = ("rainfall", "ponding", "weather_risk", "glofas")
WEATHER_SOURCES = ("rainfall", "weather", "emergency")


def rainfall_feed(lat, lon):
    """A native-spacing forecast mesh, not advertised as radar-resolution rainfall."""
    points = []
    for north in (0.75, 0.5, 0.25, 0, -0.25, -0.5, -0.75):
        for east in (-0.75, -0.5, -0.25, 0, 0.25, 0.5, 0.75):
            points.append((lat + north, lon + east))
    params = urllib.parse.urlencode({
        "latitude": ",".join(f"{p[0]:.5f}" for p in points),
        "longitude": ",".join(f"{p[1]:.5f}" for p in points),
        "models": "ecmwf_ifs025", "daily": "precipitation_sum", "precipitation_unit": "mm",
        "hourly": "precipitation", "elevation": ",".join(["nan"] * len(points)),
        "forecast_days": 3, "timezone": "UTC",
    })
    try:
        replies = json.loads(fetch("https://api.open-meteo.com/v1/forecast?" + params))
    except json.JSONDecodeError as error:
        raise FeedError("ECMWF rainfall sampling returned invalid JSON.") from error
    if not isinstance(replies, list) or len(replies) != len(points):
        raise FeedError("ECMWF rainfall sampling returned an unexpected number of locations.")
    days = [(utc_now().date() + timedelta(days=k)).isoformat() for k in range(3)]
    samples = []
    missing_daily = 0
    missing_hourly = 0

    def amount_or_unknown(value):
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None
    for index, ((y, x), reply) in enumerate(zip(points, replies)):
        if not isinstance(reply, dict) or not isinstance(reply.get("daily"), dict):
            raise FeedError("ECMWF rainfall sampling returned no daily values.")
        daily = reply["daily"]
        times, amounts = daily.get("time"), daily.get("precipitation_sum")
        if not isinstance(times, list) or not isinstance(amounts, list) or len(times) != len(amounts):
            raise FeedError("ECMWF rainfall sampling has mismatched daily values.")
        rain = dict(zip(times, amounts))
        rain = {day: amount_or_unknown(rain.get(day)) for day in days}
        missing_daily += sum(value is None for value in rain.values())
        model_lat, model_lon = reply.get("latitude"), reply.get("longitude")
        try:
            model_lat, model_lon = coordinate(model_lat, -90, 90), coordinate(model_lon, -180, 180)
        except ValueError as error:
            raise FeedError("ECMWF returned invalid model-grid coordinates.") from error
        samples.append({
            "id": f"rain-{index}", "lat": round(y, 5), "lon": round(x, 5),
            "row": index // 7, "col": index % 7,
            "model_cell": {"lat": model_lat, "lon": model_lon},
            "daily": [{"date": d, "rain_mm": rain[d]} for d in days],
            "total_mm": round(sum(rain[d] for d in days), 1) if all(rain[d] is not None for d in days) else None,
        })
        hourly = reply.get("hourly") or {}
        hour_times, hour_rain = hourly.get("time"), hourly.get("precipitation")
        if not isinstance(hour_times, list) or not isinstance(hour_rain, list) or len(hour_times) != len(hour_rain):
            raise FeedError("ECMWF hourly precipitation is incomplete; current rainfall is unknown.")
        valid_hour = utc_now().strftime("%Y-%m-%dT%H:00")
        value = amount_or_unknown(dict(zip(hour_times, hour_rain)).get(valid_hour))
        samples[-1]["current_hour"] = {"valid_at": valid_hour + "Z", "rain_mm": value,
                                      "interval": "preceding hour ending at valid_at; model estimate, not live radar"}
        # Keep a bounded time series with missing values explicit, not zero-filled.
        upcoming = [(utc_now().replace(minute=0, second=0, microsecond=0) + timedelta(hours=k))
                    .strftime("%Y-%m-%dT%H:00") for k in range(24)]
        by_hour = dict(zip(hour_times, hour_rain))
        series = []
        for stamp in upcoming:
            amount = amount_or_unknown(by_hour.get(stamp))
            missing_hourly += amount is None
            series.append({"valid_at": stamp + "Z", "rain_mm": amount})
        samples[-1]["hourly"] = series
    cells = {}
    for sample in samples:
        y, x = sample["model_cell"]["lat"], sample["model_cell"]["lon"]
        key = (y, x)
        if key in cells:
            if any(cells[key][k] != sample[k] for k in ("daily", "current_hour", "hourly")):
                raise FeedError("Same model location returned conflicting rainfall; no area overlay generated.")
            continue
        # Approximate nominal grid support, not provider-supplied storm boundaries.
        west, east = max(-180, x - 0.125), min(180, x + 0.125)
        south, north = max(-90, y - 0.125), min(90, y + 0.125)
        cells[key] = {**sample, "id": f"rain-cell-{len(cells)}",
                      "geometry": {"type": "Polygon", "coordinates": [[
                          [west, south], [east, south], [east, north], [west, north], [west, south]]]},
                      "geometry_kind": "approximate 0.25-degree model support, not observed precipitation boundary"}
    return {
        "samples": samples, "cells": list(cells.values()), "dates": days, "sampling_spacing_degrees": 0.25,
        "mesh_rows": 7, "mesh_cols": 7,
        "quality": {"unknown_daily_values": missing_daily, "unknown_hourly_values": missing_hourly,
                    "message": f"{missing_daily} daily and {missing_hourly} hourly sample values missing or invalid; "
                               "these remain unknown, not zero, and affected raster triangles are masked."},
        "source": "ECMWF IFS 0.25 degrees via Open-Meteo",
        "lineage": {"producer": "ECMWF", "model": "IFS 0.25 degrees",
                    "delivery": "Open-Meteo", "observation_type": "numerical weather forecast",
                    "copernicus_satellite_measurement": False, "units": "mm",
                    "spatial_support_degrees": 0.25, "temporal_support": "preceding hour"},
        "kind": "forecast samples, not radar or satellite rainfall observations",
        "unique_model_cells": len({(s["model_cell"]["lat"], s["model_cell"]["lon"]) for s in samples}),
        **record_time(utc_now().isoformat()),
        "message": "49 native-spacing requests span 1.5 degrees. The animated view interpolates between "
                   "returned model centres; it does not track observed storms or provide radar pixels. "
                   "Raw polygons approximate nominal model support; duplicates are merged. "
                   "Elevation downscaling is disabled. The current-hour value is modelled precipitation for the "
                   "preceding hour; daily values are whole UTC days. Spatial smoothing and hourly cross-fades "
                   "are visual estimates, not errorless observations.",
    }


def weather_api(cache, lat, lon):
    """Versioned European-data weather contract; expensive research is not on the critical path."""
    with ThreadPoolExecutor(len(WEATHER_SOURCES)) as pool:
        feeds = dict(zip(WEATHER_SOURCES, pool.map(lambda source: cache.get(source, lat, lon), WEATHER_SOURCES)))
    return {
        "schema": "resilience-weather-v1", "location": {"lat": lat, "lon": lon},
        "generated_at": utc_now().isoformat(), "feeds": feeds,
        "incomplete_inputs": [source for source, feed in feeds.items() if feed["status"] != "available" or
                              (source == "rainfall" and any((feed.get("data") or {}).get("quality", {}).get(key, 0)
                               for key in ("unknown_daily_values", "unknown_hourly_values")))],
        "data_policy": {
            "focus": "European public weather and Earth-observation data",
            "forecast": "ECMWF IFS delivered by Open-Meteo; not a Copernicus satellite rain measurement",
            "copernicus_evidence": {
                "terrain": "/api/ponding", "river_forecast": "/api/glofas",
                "sentinel_water_burn_indicators": "/api/sentinel_indices", "emergency_mapping": "/api/cems",
            },
            "deployment": "Local Python processing; no LLM inference or AI credits needed at runtime. "
                          "Third-party APIs and map delivery remain external dependencies; not a certified sovereign deployment.",
        },
        "interpretation": "Rain intensity colours are a display scale, not official warning levels. "
                          "No measured flood depth, inundation boundary or safe evacuation route is supplied.",
    }


def map_layers(cache, lat, lon):
    with ThreadPoolExecutor(len(MAP_SOURCES)) as pool:
        feeds = dict(zip(MAP_SOURCES, pool.map(lambda source: cache.get(source, lat, lon), MAP_SOURCES)))
    unknown = [s for s, f in feeds.items() if f["status"] != "available" or
               (s == "rainfall" and any((f.get("data") or {}).get("quality", {}).get(key, 0)
                for key in ("unknown_daily_values", "unknown_hourly_values")))]
    return {
        "schema": "resilience-map-v1", "location": {"lat": lat, "lon": lon},
        "feeds": feeds, "incomplete_inputs": unknown, "generated_at": utc_now().isoformat(),
        "geojson": map_features(feeds, lat, lon),
        "interpretation": "Forecast rain, sampled terrain drainage and modelled river flow. "
                          "No flood extents, water depth, safe routes or confirmed fire ignition are inferred.",
    }


def map_features(feeds, lat, lon):
    """GeoJSON for GIS teammates; every feature keeps provenance and its data kind."""
    features = []

    def add(source, feature_id, geometry, kind, details):
        feed = feeds[source]
        features.append({
            "type": "Feature", "id": feature_id, "geometry": geometry,
            "properties": {"layer": source, "kind": kind, "source": feed["source"],
                           "status": feed["status"], "fetched_at": feed.get("fetched_at"),
                           "limitation": feed.get("limitation"), **details},
        })

    def point(y, x):
        return {"type": "Point", "coordinates": [x, y]}

    def data(source):
        feed = feeds[source]
        return (feed.get("data") or {}) if feed["status"] in ("available", "stale") else {}

    for sample in data("rainfall").get("samples", []):
        add("rainfall", sample["id"], point(sample["lat"], sample["lon"]), "rainfall_forecast_sample",
            {"daily": sample["daily"], "three_day_total_mm": sample["total_mm"],
             "model_cell": sample["model_cell"], "spatial_support": "ECMWF ~0.25 degree grid, not 4 km resolution"})
    for cell in data("rainfall").get("cells", []):
        add("rainfall", cell["id"], cell["geometry"], "approximate_rainfall_model_support",
            {"daily": cell["daily"], "current_hour": cell["current_hour"],
             "three_day_total_mm": cell["total_mm"], "model_cell": cell["model_cell"],
             "geometry_kind": cell["geometry_kind"]})
    terrain = data("ponding")
    for cell in terrain.get("collectors", []):
        add("ponding", f"collector-{cell['row']}-{cell['col']}", point(cell["lat"], cell["lon"]),
            "candidate_terrain_minimum" if cell["pit"] else "sampled_runoff_collector",
            {k: cell[k] for k in ("elevation_m", "upstream_km2", "downhill_slope_pct", "local_relief_m",
                                  "inflow_m3_if_all_runs_off")} | {
                "rain_dates": terrain["rain_dates"], "uniform_rain_mm": terrain["rain_next_3d_mm"],
                "runoff_assumption": "100% of rain on the grid-clipped sampled upstream area, not stored volume or depth",
            })
    for cell in terrain.get("cells", []):
        downstream = cell.get("downstream")
        if downstream:
            add("ponding", f"drainage-{cell['row']}-{cell['col']}", {
                "type": "LineString", "coordinates": [[cell["lon"], cell["lat"]],
                                                       [downstream["lon"], downstream["lat"]]],
            }, "sampled_downhill_connection",
                {"upstream_km2": cell["upstream_km2"], "slope_pct": cell["downhill_slope_pct"],
                 "interpretation": "Steepest sampled neighbour; not a surveyed river or drain."})
    river = data("glofas")
    cell = river.get("cell") or {}
    if all(isinstance(cell.get(k), (int, float)) and not isinstance(cell[k], bool)
           and math.isfinite(cell[k]) for k in ("lat", "lon")):
        add("glofas", "river-cell", point(cell["lat"], cell["lon"]), "modelled_river_discharge",
            {"peak": river["peak"], "thresholds": river["thresholds"]})
    weather = data("weather_risk")
    if weather.get("weather_factors"):
        add("weather_risk", "weather-factors", point(lat, lon), "forecast_weather_factors_and_rules",
            {"weather": weather["weather_factors"], "lightning_rule": weather["lightning"],
             "fire_weather_rule": weather["fire_weather"]})
    return {"type": "FeatureCollection", "features": features}
