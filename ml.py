"""Rain-to-flood machine learning, rain pile-up terrain analysis, lightning and fire-weather factors.

Three parts, each owned easily by a different team member:

* flood_ml_feed     - a logistic-regression model trained per location on ~10 years of
                      Copernicus ERA5 rainfall (C3S) and Copernicus GloFAS river discharge.
                      It is then fed today's GloFAS state and the ECMWF rainfall forecast.
* ponding_feed      - where rain water collects: D8 flow accumulation on a Copernicus DEM grid.
* weather_risk_feed - transparent, rule-based lightning and fire-weather factors from ECMWF.
                      There are no local labels for these yet, so they are NOT trained models.

Every coefficient that is a judgement call lives in FACTORS so the team can review and tune it.
"""
from datetime import date, timedelta
import math
import threading
import urllib.parse

from feeds import FeedError, fetch_json, record_time, utc_now
from local_geo import destination_point, percentile
from local_ml import predict_proba, roc_auc, train_logistic

ERA5 = "https://archive-api.open-meteo.com/v1/archive"
FORECAST = "https://api.open-meteo.com/v1/forecast"
GLOFAS = "https://flood-api.open-meteo.com/v1/flood"
ELEVATION = "https://api.open-meteo.com/v1/elevation"
HISTORY_YEARS = 10
ERA5_LAG_DAYS = 6
HORIZON_DAYS = 3

FACTORS = {
    # Thunderstorm potential from ECMWF convective available potential energy (J/kg).
    "cape_levels": [(2500, "very high"), (1000, "high"), (300, "moderate"), (0, "low")],
    # A thunderstorm with almost no rain and dry air can ignite fires ("dry lightning").
    "dry_lightning": {"min_cape": 500, "max_rain_mm": 0.2, "max_rh": 50},
    # Fire-weather points; levels below are summed points.
    "fire_points": {"temp_c": [(35, 3), (30, 2), (25, 1)],
                    "rh_pct": [(15, 3), (25, 2), (35, 1)],
                    "wind_kmh": [(40, 3), (25, 2), (15, 1)],
                    "dry_days": [(14, 3), (7, 2), (3, 1)],
                    "soil_moisture": [(0.10, 2), (0.18, 1)]},
    "fire_levels": [(10, "very high"), (7, "high"), (4, "moderate"), (0, "low")],
    "rain_day_mm": 2.0,
}

FEATURES = ["rain_next_3d_mm", "rain_past_3d_mm", "rain_past_30d_mm",
            "river_vs_median_log", "season_sin", "season_cos"]
_MODELS = {}
_MODEL_LOCK = threading.Lock()


def _series(data, key):
    daily = data.get("daily")
    if not isinstance(daily, dict) or not isinstance(daily.get("time"), list):
        raise FeedError("Provider returned no daily series.")
    return dict(zip(daily["time"], daily.get(key) or []))


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def build_samples(days, rain, flow, threshold, median):
    """Rows of FEATURES and 1/0 labels: will the river exceed `threshold` in the next 3 days?"""
    rows, labels = [], []
    for i in range(30, len(days) - HORIZON_DAYS):
        past30 = [rain.get(d) for d in days[i - 29:i + 1]]
        future = [rain.get(d) for d in days[i + 1:i + 1 + HORIZON_DAYS]]
        flows = [flow.get(d) for d in days[i + 1:i + 1 + HORIZON_DAYS]]
        if not all(map(_number, past30 + future + flows + [flow.get(days[i])])):
            continue
        rows.append(features(sum(future), sum(past30[-3:]), sum(past30), flow[days[i]], median, days[i]))
        labels.append(1 if max(flows) > threshold else 0)
    return rows, labels


def features(rain_next3, rain_past3, rain_past30, flow_now, median, day):
    angle = 2 * math.pi * date.fromisoformat(day).timetuple().tm_yday / 365.25
    return [rain_next3, rain_past3, rain_past30,
            math.log((flow_now + 1e-3) / (median + 1e-3)), math.sin(angle), math.cos(angle)]


def train_location(lat, lon):
    """Train and validate on a chronological split; returns the model and honest skill numbers."""
    end = utc_now().date() - timedelta(days=ERA5_LAG_DAYS)
    start = end - timedelta(days=365 * HISTORY_YEARS)
    window = {"latitude": lat, "longitude": lon, "start_date": start.isoformat(), "end_date": end.isoformat()}
    rain = _series(fetch_json(ERA5 + "?" + urllib.parse.urlencode(
        {**window, "models": "era5", "daily": "precipitation_sum", "timezone": "UTC"})), "precipitation_sum")
    flow = _series(fetch_json(GLOFAS + "?" + urllib.parse.urlencode(
        {**window, "daily": "river_discharge"})), "river_discharge")
    days = sorted(set(rain) & set(flow))
    split = int(len(days) * 0.8)
    train_flow = [flow[d] for d in days[:split] if _number(flow[d])]
    if len(train_flow) < 365 * 3:
        raise FeedError("Not enough ERA5 / GloFAS history to train at this location.")
    median, threshold = percentile(train_flow, 50), percentile(train_flow, 90)
    rows, labels = build_samples(days[:split], rain, flow, threshold, median)
    test_rows, test_labels = build_samples(days[split - 30:], rain, flow, threshold, median)
    if len(set(labels)) < 2 or len(set(test_labels)) < 2:
        raise FeedError("The river never crossed its high-flow threshold in the record; no model trained.")
    model = train_logistic(rows, labels, epochs=250, lr=0.2)
    # Undo the positive-class weighting when interpreting logistic output.
    model["positive_class_weight"] = (len(labels) - sum(labels)) / sum(labels)
    scores = [predict_proba(model, r) for r in test_rows]
    persistence = [r[3] for r in test_rows]
    hits = sum(1 for s, y in zip(scores, test_labels) if s >= 0.5 and y)
    flagged, events = sum(1 for s in scores if s >= 0.5), sum(test_labels)
    return {
        "model": model, "median": median, "threshold": threshold, "trained_on": [start.isoformat(), end.isoformat()],
        "validation": {
            "method": "chronological split: oldest 80% train, newest 20% test (no shuffling)",
            "test_days": len(test_labels), "test_events": events,
            "auc": round(roc_auc(test_labels, scores), 3),
            "auc_river_level_only": round(roc_auc(test_labels, persistence), 3),
            "detected_events_pct": round(100 * hits / events, 1) if events else None,
            "false_alarm_pct": round(100 * (flagged - hits) / flagged, 1) if flagged else None,
            "brier_score": round(sum((s - y) ** 2 for s, y in zip(scores, test_labels)) / len(scores), 4),
            "forecast_validated": False,
            "caveat": "Test uses realized ERA5 future rainfall, not archived ECMWF forecasts. "
                      "Skill is retrospective and may overstate operational performance. Labels are modelled "
                      "GloFAS high flow, not observed flood damage; scores are not calibrated flood probabilities.",
        },
        "weights": {name: round(w, 3) for name, w in zip(FEATURES, model["weights"])},
    }


def cached_model(lat, lon):
    key = (round(lat, 2), round(lon, 2), utc_now().date().isoformat())
    with _MODEL_LOCK:
        if key not in _MODELS:
            if len(_MODELS) >= 16:
                del _MODELS[next(iter(_MODELS))]
            _MODELS[key] = train_location(lat, lon)
        return _MODELS[key]


def flood_ml_feed(lat, lon):
    trained = cached_model(lat, lon)
    today = utc_now().date()
    rain = _series(fetch_json(FORECAST + "?" + urllib.parse.urlencode({
        "latitude": lat, "longitude": lon, "models": "ecmwf_ifs025", "daily": "precipitation_sum",
        "past_days": 31, "forecast_days": 7, "timezone": "UTC"})), "precipitation_sum")
    flow = _series(fetch_json(GLOFAS + "?" + urllib.parse.urlencode({
        "latitude": lat, "longitude": lon, "daily": "river_discharge", "past_days": 3, "forecast_days": 7})),
        "river_discharge")
    outlook = []
    for offset in range(0, 4):
        day = today + timedelta(days=offset)
        past = [rain.get((day - timedelta(days=k)).isoformat()) for k in range(30)]
        future = [rain.get((day + timedelta(days=k)).isoformat()) for k in range(1, HORIZON_DAYS + 1)]
        river = flow.get(day.isoformat())
        if not all(map(_number, past + future + [river])):
            continue
        row = features(sum(future), sum(past[:3]), sum(past), river, trained["median"], day.isoformat())
        outlook.append({"date": day.isoformat(), "probability": round(predict_proba(trained["model"], row), 3),
                        "rain_next_3d_mm": round(sum(future), 1), "rain_past_30d_mm": round(sum(past), 1),
                        "river_m3s": river})
    if not outlook:
        raise FeedError("ECMWF / GloFAS inputs for today are incomplete; no prediction made.")
    peak = max(outlook, key=lambda o: o["probability"])
    return {
        "question": "Will the nearest GloFAS river cell exceed its 90th-percentile flow within 3 days?",
        "outlook": outlook, "peak_probability": peak["probability"], "peak_date": peak["date"],
        "threshold_m3s": round(trained["threshold"], 3), "median_m3s": round(trained["median"], 3),
        "trained_on": trained["trained_on"], "validation": trained["validation"],
        "feature_weights_standardized": trained["weights"],
        "data": "Copernicus ERA5 rainfall (C3S) + Copernicus GloFAS discharge to train; ECMWF IFS forecast to predict.",
        **record_time(utc_now().isoformat()),
        "score_kind": "class-weight-adjusted logistic high-flow score; not a calibrated flood probability",
        "message": "Statistical model of one ~5 km river cell. High flow is not proof of flooding. "
                   "Not validated against archived forecasts, not a street-level flood map or an official warning.",
    }


# ---------------------------------------------------------------- where rain water collects

def drainage_targets(grid):
    """Steepest downhill neighbour; sampled depressions are not confirmed closed basins."""
    if not grid or not grid[0] or any(len(row) != len(grid[0]) for row in grid):
        raise ValueError("Elevation grid must be non-empty and rectangular.")
    rows, cols = len(grid), len(grid[0])
    target, pits = {}, set()
    for r in range(rows):
        for c in range(cols):
            z = grid[r][c]
            if z is None:
                continue
            best, drop = None, 0.0
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    rr, cc = r + dr, c + dc
                    if (dr or dc) and 0 <= rr < rows and 0 <= cc < cols and grid[rr][cc] is not None:
                        slope = (z - grid[rr][cc]) / math.hypot(dr, dc)
                        if slope > drop:
                            best, drop = (rr, cc), slope
            if best:
                target[(r, c)] = best
            elif 0 < r < rows - 1 and 0 < c < cols - 1:
                pits.add((r, c))
    return target, pits


def d8_accumulation(grid):
    """Sampled upstream cell count (None = no data), plus interior local minima."""
    target, pits = drainage_targets(grid)
    rows, cols = len(grid), len(grid[0])
    order = sorted(target, key=lambda cell: -grid[cell[0]][cell[1]])
    accumulation = {(r, c): 1 for r in range(rows) for c in range(cols) if grid[r][c] is not None}
    for cell in order:
        accumulation[target[cell]] += accumulation[cell]
    return accumulation, pits


def ponding_feed(lat, lon, size=9, spacing_km=1.0):
    half = (size - 1) / 2
    points = []
    for r in range(size):
        north = destination_point(lat, lon, 0 if r <= half else 180, abs(half - r) * spacing_km)
        for c in range(size):
            points.append(destination_point(north[0], north[1], 90 if c >= half else 270, abs(c - half) * spacing_km))
    data = fetch_json(ELEVATION + "?" + urllib.parse.urlencode({
        "latitude": ",".join(f"{p[0]:.5f}" for p in points),
        "longitude": ",".join(f"{p[1]:.5f}" for p in points)}))
    values = data.get("elevation")
    if not isinstance(values, list) or len(values) != len(points):
        raise FeedError("Copernicus DEM elevation returned an unexpected response.")
    flat = [float(v) if _number(v) else None for v in values]
    grid = [flat[r * size:(r + 1) * size] for r in range(size)]
    rain = _series(fetch_json(FORECAST + "?" + urllib.parse.urlencode({
        "latitude": lat, "longitude": lon, "models": "ecmwf_ifs025", "daily": "precipitation_sum",
        "forecast_days": 3, "timezone": "UTC"})), "precipitation_sum")
    expected = [(utc_now().date() + timedelta(days=k)).isoformat() for k in range(3)]
    if not all(_number(rain.get(d)) for d in expected):
        raise FeedError("The 3-day rainfall forecast is incomplete; runoff volume is unknown.")
    if not any(v is not None for v in flat):
        raise FeedError("Copernicus DEM has no valid elevations for this area.")
    rain_3d = sum(rain[d] for d in expected)
    accumulation, pits = d8_accumulation(grid)
    targets, _ = drainage_targets(grid)
    cell_km2 = spacing_km * spacing_km
    cells = []
    for (r, c), count in accumulation.items():
        p = points[r * size + c]
        downstream = targets.get((r, c))
        neighbours = [grid[rr][cc] for rr in range(max(0, r - 1), min(size, r + 2))
                      for cc in range(max(0, c - 1), min(size, c + 2)) if grid[rr][cc] is not None]
        slope = 0.0
        if downstream:
            rr, cc = downstream
            slope = 100 * (grid[r][c] - grid[rr][cc]) / (spacing_km * 1000 * math.hypot(rr - r, cc - c))
        cells.append({"lat": round(p[0], 5), "lon": round(p[1], 5), "elevation_m": grid[r][c],
                      "row": r, "col": c,
                      "upstream_km2": round(count * cell_km2, 1), "pit": (r, c) in pits,
                      "downhill_slope_pct": round(slope, 2),
                      "local_relief_m": round(max(neighbours) - min(neighbours), 1),
                      "downstream": {"lat": round(points[downstream[0] * size + downstream[1]][0], 5),
                                     "lon": round(points[downstream[0] * size + downstream[1]][1], 5)} if downstream else None,
                      "inflow_m3_if_all_runs_off": round(count * cell_km2 * 1e6 * rain_3d / 1000)})
    cells.sort(key=lambda cell: (-cell["upstream_km2"], cell["elevation_m"]))
    collectors = [cell for cell in cells if cell["pit"] or cell["upstream_km2"] >= 4 * cell_km2][:12]
    return {"grid_km": size * spacing_km, "spacing_km": spacing_km, "rain_next_3d_mm": round(rain_3d, 1),
            "rain_dates": expected, "valid_cells": len(cells), "missing_cells": size * size - len(cells),
            "rain_assumption": "One ECMWF point forecast applied uniformly to sampled upstream cells.",
            "collectors": collectors, "cells": cells, **record_time(utc_now().isoformat()),
            "message": "Candidate low points and drainage lines from Copernicus DEM (90 m), sampled every 1 km, "
                       "with the ECMWF 3-day rain. Shows where runoff concentrates, not mapped flooding; "
                       "drains, soil and buildings are not modelled. Sampled uphill area is clipped at the grid edge; "
                       "flat cells and missing data can create false minima. Volumes assume 100% runoff from this "
                       "sampled area only, not water depth or a bound on actual catchment inflow."}


# ---------------------------------------------------------------- lightning and fire weather

def level(value, table):
    return next(name for limit, name in table if value >= limit)


def points(value, table, lower_is_worse=False):
    for limit, score in table:
        if (value <= limit) if lower_is_worse else (value >= limit):
            return score
    return 0


def weather_risk_feed(lat, lon):
    data = fetch_json(FORECAST + "?" + urllib.parse.urlencode({
        "latitude": lat, "longitude": lon, "models": "ecmwf_ifs025", "timezone": "UTC",
        "hourly": "temperature_2m,relative_humidity_2m,wind_speed_10m,precipitation,cape,soil_moisture_0_to_7cm",
        "daily": "precipitation_sum", "past_days": 30, "forecast_days": 3}))
    hourly = data.get("hourly")
    if not isinstance(hourly, dict) or not isinstance(hourly.get("time"), list):
        raise FeedError("ECMWF returned no hourly forecast.")
    now = utc_now().strftime("%Y-%m-%dT%H:00")
    keys = ("temperature_2m", "relative_humidity_2m", "wind_speed_10m", "precipitation", "cape", "soil_moisture_0_to_7cm")
    if any(not isinstance(hourly.get(k), list) or len(hourly[k]) != len(hourly["time"]) for k in keys[:5]):
        raise FeedError("ECMWF hourly variables have incomplete or mismatched lengths.")
    soil = hourly.get(keys[5])
    if soil is not None and (not isinstance(soil, list) or len(soil) != len(hourly["time"])):
        raise FeedError("ECMWF soil-moisture series has a mismatched length.")
    hours = [dict(time=t, **{k: (hourly.get(k) or [None] * len(hourly["time"]))[i] for k in keys})
             for i, t in enumerate(hourly["time"]) if t >= now][:48]
    hours = [h for h in hours if all(_number(h[k]) for k in keys[:5])]
    if not hours:
        raise FeedError("ECMWF hourly forecast is incomplete.")
    daily = _series(data, "precipitation_sum")
    dry_days = 0
    for lag in range(1, 31):
        day = (utc_now().date() - timedelta(days=lag)).isoformat()
        if not _number(daily.get(day)):
            raise FeedError("Rain history is incomplete; dry-spell fire-weather factor is unknown.")
        if daily[day] >= FACTORS["rain_day_mm"]:
            break
        dry_days += 1
    dry = FACTORS["dry_lightning"]
    storm = max(hours, key=lambda h: h["cape"])
    dry_strike = [h["time"] for h in hours if h["cape"] >= dry["min_cape"]
                  and h["precipitation"] <= dry["max_rain_mm"] and h["relative_humidity_2m"] <= dry["max_rh"]]
    worst, worst_score, worst_why = None, -1, {}
    for h in hours:
        why = {"temperature_c": points(h["temperature_2m"], FACTORS["fire_points"]["temp_c"]),
               "humidity_pct": points(h["relative_humidity_2m"], FACTORS["fire_points"]["rh_pct"], True),
               "wind_kmh": points(h["wind_speed_10m"], FACTORS["fire_points"]["wind_kmh"]),
               "days_without_rain": points(dry_days, FACTORS["fire_points"]["dry_days"])}
        if _number(h["soil_moisture_0_to_7cm"]):
            why["dry_soil"] = points(h["soil_moisture_0_to_7cm"], FACTORS["fire_points"]["soil_moisture"], True)
        if h["precipitation"] > 0.5:
            why["raining"] = -3
        score = sum(why.values())
        if score > worst_score:
            worst, worst_score, worst_why = h, score, why
    current = hours[0]
    current_points = {
        "temperature": points(current["temperature_2m"], FACTORS["fire_points"]["temp_c"]),
        "humidity": points(current["relative_humidity_2m"], FACTORS["fire_points"]["rh_pct"], True),
        "wind": points(current["wind_speed_10m"], FACTORS["fire_points"]["wind_kmh"]),
        "dry_days": points(dry_days, FACTORS["fire_points"]["dry_days"]),
    }
    if _number(current["soil_moisture_0_to_7cm"]):
        current_points["dry_soil"] = points(current["soil_moisture_0_to_7cm"], FACTORS["fire_points"]["soil_moisture"], True)
    if current["precipitation"] > 0.5:
        current_points["rain"] = -3
    current_score = max(0, sum(current_points.values()))
    return {
        "current_fire_weather": {"level": level(current_score, FACTORS["fire_levels"]),
                                 "valid_at": current["time"], "rain_mm": current["precipitation"],
                                 "humidity_pct": current["relative_humidity_2m"], "points": current_score,
                                 "message": "Wet/cool conditions can suppress the weather indicator, not prove zero ignition. "
                                            "This is a rule, not active fire evidence or a seasonal probability."},
        "lightning": {"level": level(storm["cape"], FACTORS["cape_levels"]), "max_cape_jkg": storm["cape"],
                      "at": storm["time"], "storm_hours": sum(1 for h in hours if h["cape"] >= 1000),
                      "dry_lightning_hours": dry_strike[:12]},
        "fire_weather": {"level": level(max(0, worst_score), FACTORS["fire_levels"]), "points": worst_score,
                         "at": worst["time"], "factors": worst_why, "days_without_rain": dry_days,
                         "dry_days_censored": dry_days == 30,
                         "temperature_c": worst["temperature_2m"], "humidity_pct": worst["relative_humidity_2m"],
                         "wind_kmh": worst["wind_speed_10m"]},
        "hours_checked": len(hours), "factors_config": FACTORS, **record_time(hours[0]["time"] + "+00:00"),
        "weather_factors": {
            "valid_from": hours[0]["time"], "valid_to": hours[-1]["time"],
            "rain_total_mm": round(sum(h["precipitation"] for h in hours), 1),
            "peak_hourly_rain_mm": max(h["precipitation"] for h in hours),
            "peak_rain_at": max(hours, key=lambda h: h["precipitation"])["time"],
            "max_wind_kmh": max(h["wind_speed_10m"] for h in hours),
            "min_humidity_pct": min(h["relative_humidity_2m"] for h in hours),
            "max_temperature_c": max(h["temperature_2m"] for h in hours),
            "mean_topsoil_moisture": round(sum(h["soil_moisture_0_to_7cm"] for h in hours) / len(hours), 3)
                if all(_number(h["soil_moisture_0_to_7cm"]) for h in hours) else None,
        },
        "message": "Rule-based ECMWF indicators for the next 48 h at this point. Not trained, not official "
                   "fire danger: see the EFFIS Fire Weather Index layer for the Copernicus product.",
    }
