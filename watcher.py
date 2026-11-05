"""Official-source watcher: turns CEMS, GloFAS and Meteoalarm signals into *suspected* incidents.

Sources only ever raise flags. Confirming a failure stays with an authorised operator.
Each source is isolated: if one fails, its last good result is kept (marked outdated) and the others continue.
"""
import json
import os
import re
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import hazards
import incidents
import network
from feeds import fetch_json

CEMS_DETAIL = "https://rapidmapping.emergency.copernicus.eu/backend/dashboard-api/public-activations/"
METEOALARM = "https://feeds.meteoalarm.org/api/v1/warnings/feeds-bulgaria"
NUTS3 = "BG341"  # Burgas province
COUNTRY = "Bulgaria"
WARN_TYPES = {"rain", "flooding", "thunderstorm", "coastalevent"}
WARN_LEVELS = {"orange": 3, "red": 4}
TOP_CROSSINGS_ON_WARNING = 5
REPLAY_AOI = Path(__file__).with_name("data") / "emsr693_aoi.json"

_lock = threading.Lock()
_health = {}


def _now():
    return datetime.now(timezone.utc)


def wkt_rings(text):
    """POLYGON / MULTIPOLYGON WKT -> list of outer rings as (lat, lon)."""
    out = []
    for body in re.findall(r"\(\(([^()]+)\)", str(text)):
        ring = [(float(y), float(x)) for x, y in (p.split()[:2] for p in body.split(","))]
        if len(ring) >= 3:
            out.append(ring)
    if not out:
        raise ValueError("No polygon in WKT.")
    return out


def crossings_in(rings, net):
    return [cid for cid, c in net.crossings.items() if any(hazards.inside(c["lat"], c["lon"], r) for r in rings)]


# ---------------------------------------------------------------- sources -> signals
# A signal: {"id": stable id, "crossings": [...], "note": short text, "source": name}

def cems_signals(net, now=None):
    data = fetch_json(hazards.CEMS_RM + "?" + urllib.parse.urlencode({"limit": 100}))
    out = []
    for item in data.get("results") or []:
        if item.get("closed") is not False or COUNTRY not in (item.get("countries") or []):
            continue
        code = str(item["code"])
        detail = fetch_json(CEMS_DETAIL + "?" + urllib.parse.urlencode({"code": code}))
        record = (detail.get("results") or [detail])[0]
        rings = [r for a in record.get("aois") or [] for r in wkt_rings(a.get("extent"))] or wkt_rings(record.get("extent"))
        hit = crossings_in(rings, net)
        if hit:
            out.append({"id": code, "crossings": hit, "source": "Copernicus EMS",
                        "note": f"{code} ({item.get('category', 'event')}): crossing inside the official area of interest."})
    return out


def _glofas_thresholds(lat, lon, cache):
    """p99 and record discharge for a GloFAS cell; history is heavy, so it is cached for 30 days."""
    key = f"{lat},{lon}"
    hit = cache.get(key)
    if hit and time.time() - hit["at"] < 30 * 86400:
        return hit
    today = _now().date()
    past = fetch_json(hazards.GLOFAS + "?" + urllib.parse.urlencode({
        "latitude": lat, "longitude": lon, "daily": "river_discharge",
        "start_date": today.replace(year=today.year - 5).isoformat(), "end_date": today.isoformat()}))
    record = [v for v in (past.get("daily") or {}).get("river_discharge") or [] if isinstance(v, (int, float))]
    if len(record) < 365:
        raise ValueError("no river at this cell")
    cache[key] = {"at": time.time(), "p99": hazards.percentile(record, 99), "max": max(record)}
    return cache[key]


def glofas_signals(net, now=None, workers=2):
    cells = {}
    for cid, c in net.crossings.items():
        cells.setdefault((round(c["lat"], 1), round(c["lon"], 1)), []).append(cid)
    path = Path(os.environ.get("GLOFAS_CACHE", Path(__file__).with_name(".cache") / "glofas_thresholds.json"))
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}

    def check(item):
        (lat, lon), ids = item
        try:
            limits = _glofas_thresholds(lat, lon, cache)
            daily = fetch_json(hazards.GLOFAS + "?" + urllib.parse.urlencode({
                "latitude": lat, "longitude": lon, "daily": "river_discharge", "forecast_days": 10})).get("daily") or {}
        except ValueError:
            return None  # sea or no river: not an error
        except Exception as error:
            return error
        days = [(t, v) for t, v in zip(daily.get("time") or [], daily.get("river_discharge") or []) if isinstance(v, (int, float))]
        if not days:
            return None
        day, peak = max(days, key=lambda d: d[1])
        level = "extreme" if peak > limits["max"] else "high" if peak > limits["p99"] else None
        if level:
            return {"id": f"glofas-{lat}-{lon}-{day}", "crossings": ids, "source": "GloFAS",
                    "note": f"GloFAS {level} river discharge forecast for {day} ({peak:.1f} m³/s) in the nearest ~5 km river cell."}

    with ThreadPoolExecutor(workers) as pool:
        results = list(pool.map(check, cells.items()))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache), encoding="utf-8")
    except OSError:
        pass
    errors = [r for r in results if isinstance(r, Exception)]
    if errors and len(errors) == len([r for r in results if r is not None]):
        raise errors[0]
    return [r for r in results if isinstance(r, dict)]


def meteoalarm_signals(net, now=None):
    now = now or _now()
    data = fetch_json(METEOALARM)
    out = []
    for warning in data.get("warnings") or []:
        for info in warning.get("alert", {}).get("info") or []:
            if not str(info.get("language", "")).startswith("en"):
                continue
            params = {p.get("valueName"): str(p.get("value", "")) for p in info.get("parameter") or []}
            colour = params.get("awareness_level", "").split(";")[1].strip().lower() if ";" in params.get("awareness_level", "") else ""
            kind = params.get("awareness_type", "").split(";")[-1].strip().lower()
            areas = {g.get("value") for a in info.get("area") or [] for g in a.get("geocode") or []}
            try:
                live = datetime.fromisoformat(info["onset"]) <= now < datetime.fromisoformat(info["expires"])
            except (KeyError, ValueError):
                live = False
            if colour in WARN_LEVELS and kind in WARN_TYPES and NUTS3 in areas and live:
                top = [c["id"] for c in network.bridges(net)["crossings"][:TOP_CROSSINGS_ON_WARNING]]
                out.append({"id": f"meteoalarm-{kind}-{colour}-{info['onset'][:13]}", "crossings": top, "source": "Meteoalarm",
                            "note": f"Official {colour} {kind} warning for Burgas until {info['expires'][:16]}: inspect high-consequence crossings."})
    return out


SOURCES = {"Copernicus EMS": cems_signals, "GloFAS": glofas_signals, "Meteoalarm": meteoalarm_signals}


# ---------------------------------------------------------------- apply

def _already(signal_id, crossing, items):
    tag = f"[auto {signal_id}]"
    return any(i["crossing"] == crossing and (i["status"] != "reopened" or any(tag in h.get("note", "") for h in i["history"]))
               for i in items)


def apply(signals, net=None, now=None):
    """Create suspected incidents for signals; never downgrades or duplicates an existing incident."""
    net = net or network.load()
    created = []
    for s in signals:
        for cid in s["crossings"]:
            if _already(s["id"], cid, incidents._read()):
                continue
            incidents.update({"crossing": cid, "status": "suspected",
                              "note": f"[auto {s['id']}] {s['note']}"[:200]}, net, now)
            created.append(cid)
    return created


def scan(net=None, now=None, sources=None):
    net, now = net or network.load(), now or _now()
    flagged = []
    for name, fn in (sources or SOURCES).items():
        start = time.monotonic()
        with _lock:
            prev = _health.get(name, {})
        try:
            signals = fn(net, now)
            new = apply(signals, net, now)
            flagged += new
            entry = {"status": "ok", "last_ok": now.isoformat(timespec="seconds"), "signals": len(signals),
                     "new_flags": len(new), "detail": "; ".join(s["note"] for s in signals[:3]) or "No active signal for this region."}
        except Exception as error:  # one failing source must never stop the others
            entry = {**prev, "status": "outdated" if prev.get("last_ok") else "unavailable",
                     "error": f"{type(error).__name__}: {str(error)[:120]}"}
        entry.update(checked_at=now.isoformat(timespec="seconds"), seconds=round(time.monotonic() - start, 1))
        with _lock:
            _health[name] = entry
    return flagged


def health():
    with _lock:
        return {name: dict(_health.get(name, {"status": "not checked yet"})) for name in SOURCES}


def replay_signals(net=None):
    """EMSR693 (Burgas flood, 5 Sep 2023): crossings inside the official area of interest, from a stored copy."""
    net = net or network.load()
    data = json.loads(REPLAY_AOI.read_text(encoding="utf-8"))
    return [{"id": "EMSR693", "source": "Copernicus EMS", "crossings": crossings_in(wkt_rings(data["extent"]), net),
             "note": "EMSR693 (Flood, 5 Sep 2023): crossing inside the official area of interest."}]


def start(minutes=None):
    minutes = float(minutes or os.environ.get("WATCH_MINUTES", 15))

    def loop():
        while True:
            try:
                scan()
            except Exception:
                pass
            time.sleep(minutes * 60)

    threading.Thread(target=loop, name="official-source-watcher", daemon=True).start()
