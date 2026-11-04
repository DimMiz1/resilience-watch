"""Opt-in fire ML trained on team-reviewed daily weather and observed fire labels."""
import argparse
import csv
from datetime import date, timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import urllib.parse

from feeds import FeedError, fetch_json, record_time, utc_now
from local_ml import predict_proba, roc_auc, train_logistic

FEATURES = ("temperature_c", "humidity_pct", "wind_kmh", "dry_days", "soil_moisture")
SCHEMA = "resilience-fire-v1"


def values(row):
    numbers = [float(row[k]) for k in FEATURES]
    if not all(math.isfinite(x) for x in numbers):
        raise ValueError("Weather features must be finite.")
    t, rh, wind, dry, soil = numbers
    if not (-90 <= t <= 65 and 0 <= rh <= 100 and 0 <= wind <= 300 and 0 <= dry <= 366 and 0 <= soil <= 1):
        raise ValueError("Weather features are outside supported ranges.")
    return numbers


def train_file(dataset, output):
    """One location, one row per UTC day; labels must be reviewed observations."""
    with dataset.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) < 200:
        raise ValueError("Provide at least 200 labelled daily rows, including non-fire days.")
    parsed = []
    location = None
    for row in rows:
        stamp = date.fromisoformat(row["date"])
        point = (float(row["latitude"]), float(row["longitude"]))
        if not all(math.isfinite(x) for x in point) or not (-80 <= point[0] <= 80 and -178 <= point[1] <= 178):
            raise ValueError("Invalid dataset coordinates.")
        if location is None:
            location = point
        if point != location:
            raise ValueError("Use a single fixed location per model; do not mix sites.")
        if row["fire_observed"] not in ("0", "1"):
            raise ValueError("fire_observed must be 0 or 1, not an assumed label.")
        parsed.append((stamp, values(row), int(row["fire_observed"])))
    parsed.sort(key=lambda r: r[0])
    if len({r[0] for r in parsed}) != len(parsed):
        raise ValueError("Duplicate dates: provide one row per UTC day.")
    split = int(len(parsed) * 0.8)
    train, test = parsed[:split], parsed[split:]
    if any(len({r[2] for r in group}) < 2 for group in (train, test)):
        raise ValueError("Training and chronological test periods must both contain fire and non-fire days.")
    labels = [r[2] for r in train]
    model = train_logistic([r[1] for r in train], labels, epochs=250, lr=0.2)
    model["positive_class_weight"] = (len(labels) - sum(labels)) / sum(labels)
    scores = [predict_proba(model, r[1]) for r in test]
    payload = {
        "schema": SCHEMA, "features": list(FEATURES), "location": list(location),
        "model": model, "dataset_sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
        "train_dates": [train[0][0].isoformat(), train[-1][0].isoformat()],
        "test_dates": [test[0][0].isoformat(), test[-1][0].isoformat()],
        "validation": {"method": "chronological oldest 80% train / newest 20% test",
                       "test_days": len(test), "fire_days": sum(r[2] for r in test),
                       "auc": round(roc_auc([r[2] for r in test], scores), 3),
                       "brier_score": round(sum((s - r[2]) ** 2 for s, r in zip(scores, test)) / len(test), 4),
                       "forecast_validated": False},
        "label_definition": "Team-reviewed observed fire within the labelled local area on that UTC day.",
    }
    if output.exists():
        raise ValueError("Output already exists; choose a new model filename to preserve provenance.")
    output.write_text(json.dumps(payload, indent=2, allow_nan=False), encoding="utf-8")
    return payload


def load_model(path):
    try:
        if path.stat().st_size > 1024 * 1024:
            raise ValueError("model too large")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload["schema"] != SCHEMA or payload["features"] != list(FEATURES):
            raise ValueError("unsupported schema")
        model = payload["model"]
        for key in ("mean", "std", "weights"):
            if len(model[key]) != len(FEATURES) or not all(math.isfinite(x) for x in model[key]):
                raise ValueError("invalid model dimensions")
        if any(s <= 0 for s in model["std"]) or not math.isfinite(model["bias"]):
            raise ValueError("invalid scaling")
        if not math.isfinite(model["positive_class_weight"]) or model["positive_class_weight"] <= 0:
            raise ValueError("invalid class weighting")
        lat, lon = payload["location"]
        if not math.isfinite(lat) or not math.isfinite(lon):
            raise ValueError("invalid location")
        for key in ("validation", "train_dates", "test_dates"):
            if key not in payload:
                raise ValueError("missing provenance")
        return payload
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise FeedError("Fire model file is unreadable or invalid; retrain with fire_ml.py.") from error


def fire_ml_feed(lat, lon):
    path = os.environ.get("FIRE_MODEL_PATH", "").strip()
    if not path:
        return {"configuration_required": True,
                "message": "No labelled fire model configured. The team can train fire_ml.py on reviewed EFFIS "
                           "fire/non-fire daily labels, then set FIRE_MODEL_PATH. Weather rules remain separate."}
    trained = load_model(Path(path))
    if abs(lat - trained["location"][0]) > 0.01 or abs(lon - trained["location"][1]) > 0.01:
        raise FeedError("The fire model was trained for another location; no geographic transfer is assumed.")
    data = fetch_json("https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode({
        "latitude": lat, "longitude": lon, "models": "ecmwf_ifs025", "timezone": "UTC",
        "hourly": "temperature_2m,relative_humidity_2m,wind_speed_10m,soil_moisture_0_to_7cm",
        "daily": "precipitation_sum", "past_days": 30, "forecast_days": 3,
    }))
    hourly, daily = data.get("hourly", {}), data.get("daily", {})
    keys = ("temperature_2m", "relative_humidity_2m", "wind_speed_10m", "soil_moisture_0_to_7cm")
    times = hourly.get("time")
    if not isinstance(times, list) or any(not isinstance(hourly.get(k), list) or len(hourly[k]) != len(times) for k in keys):
        raise FeedError("ECMWF fire-model inputs are incomplete.")
    rain = dict(zip(daily.get("time", []), daily.get("precipitation_sum", [])))
    outlook = []
    for offset in range(3):
        day = utc_now().date() + timedelta(days=offset)
        indices = [i for i, t in enumerate(times) if t.startswith(day.isoformat())]
        if len(indices) != 24:
            continue
        columns = [[hourly[k][i] for i in indices] for k in keys]
        if any(any(not isinstance(x, (int, float)) or not math.isfinite(x) for x in col) for col in columns):
            continue
        dry = 0
        complete = True
        for lag in range(1, 31):
            mm = rain.get((day - timedelta(days=lag)).isoformat())
            if not isinstance(mm, (int, float)) or not math.isfinite(mm):
                complete = False
                break
            if mm >= 2:
                break
            dry += 1
        if not complete:
            continue
        raw = [max(columns[0]), min(columns[1]), max(columns[2]), dry, sum(columns[3]) / 24]
        try:
            row = values(dict(zip(FEATURES, raw)))
        except ValueError as error:
            raise FeedError("ECMWF fire-model features are outside supported ranges.") from error
        outlook.append({"date": day.isoformat(), "score": round(predict_proba(trained["model"], row), 3),
                        "features": dict(zip(FEATURES, row)), "dry_days_censored": dry == 30})
    if not outlook:
        raise FeedError("No complete ECMWF day for the fire model; fire likelihood is unknown.")
    return {"outlook": outlook, "validation": trained["validation"], "train_dates": trained["train_dates"],
            "test_dates": trained["test_dates"], **record_time(utc_now().isoformat()),
            "message": "Experimental local fire-occurrence score, not a calibrated probability or spread forecast. "
                       "Validation uses the team's labels and historical weather, not archived forecast features. "
                       "Do not use this model for evacuation decisions; use official EFFIS products."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = train_file(args.dataset, args.output)
    except (OSError, ValueError, KeyError) as error:
        parser.exit(1, f"Fire training failed: {error}\n")
    print(json.dumps(result["validation"], indent=2))


if __name__ == "__main__":
    main()
