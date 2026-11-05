"""Official-source watcher: adapters turn official feeds into signals; a fusion rule sets incident status.

Every adapter returns signals in one shared format (see SIGNAL). The fusion rule (fuse) is automatic:
an official closure record confirms; observed flooding corroborated by an independent source confirms;
observed flooding or an official restriction makes a crossing "reported"; warnings only flag "suspected".
An operator can always override. Each source is isolated: if one fails, its last good result is kept
(marked outdated) and the others continue.
"""
import json
import math
import os
import re
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import hazards
import incidents
import network
from feeds import FeedError, fetch, fetch_json

CEMS_DETAIL = "https://rapidmapping.emergency.copernicus.eu/backend/dashboard-api/public-activations/"
METEOALARM = "https://feeds.meteoalarm.org/api/v1/warnings/feeds-bulgaria"
NUTS3 = "BG341"  # Burgas province
COUNTRY = "Bulgaria"
WARN_TYPES = {"rain", "flooding", "thunderstorm", "coastalevent"}
WARN_LEVELS = {"orange": 3, "red": 4}
TOP_CROSSINGS_ON_WARNING = 5
# Bulgarian national access point (Road Infrastructure Agency), DATEX II 2.3, public, one file per day and category.
DATEX = "https://datasheet.api.bg/files/{day}_{cat}.xml"
DATEX_CATS = {"roadworks_r01": "closure", "roadworks_r02": "restriction"}
DATEX_NS = "{http://datex2.eu/schema/2/2_0}"
ROAD_REF = re.compile(r"\b[IІ]{1,3}\s*-\s*(\d{1,5})")
# Record text that ties a closure to a hazard (flood, rainfall, torrent, emergency, landslide, collapse, compromised,
# disaster, erosion, subsidence, washout). Without it a record is planned works: context only (assumption F6).
HAZARD_WORDS = re.compile(r"наводн|валеж|пороен|авари|свлач|срут|компрометир|бедств|ерози|пропад|подкоп|flood|landslide", re.I)
MATCH_ON_ROAD_M = 400   # record point to crossing, when the record names the crossing's road (assumption F5)
MATCH_ANY_M = 150       # record point to crossing, any road
FLOOD_NEAR_M = 300      # flood extent to crossing: rivers are masked as permanent water, so match the approaches
REPLAY_DIR = Path(__file__).with_name("data") / "replay"
REPLAY_AOI = REPLAY_DIR / "emsr693_aoi.json"
RANK = {"suspected": 1, "reported": 2, "confirmed": 3}
SIGNAL = ("id", "source", "kind", "confidence", "crossings", "observed_at", "ref", "note")
# kind: closure | restriction | inundation | exposure | aoi | warning;  confidence: official | observed | modelled


class NotConfigured(Exception):
    """A source that needs an account or URL the deployment has not set. Not an error."""

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


def distance_m(lat, lon, line):
    """Metres from a point to a polyline of (lat, lon); local equirectangular projection."""
    ky, kx = 111320.0, 111320.0 * math.cos(math.radians(lat))
    pts = [((b - lon) * kx, (a - lat) * ky) for a, b in line]
    if len(pts) == 1:
        return math.hypot(*pts[0])
    best = math.inf
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        dx, dy = x2 - x1, y2 - y1
        t = max(0.0, min(1.0, -(x1 * dx + y1 * dy) / (dx * dx + dy * dy))) if dx or dy else 0.0
        best = min(best, math.hypot(x1 + t * dx, y1 + t * dy))
    return best


def _parse_time(text):
    if not text:
        return None
    value = datetime.fromisoformat(re.sub(r"([+-]\d\d)(\d\d)$", r"\1:\2", text.strip()))
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds") if value else None


# ---------------------------------------------------------------- adapters: source -> signals (format: SIGNAL)

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
            out.append({"id": code, "crossings": hit, "source": "Copernicus EMS", "kind": "aoi", "confidence": "official",
                        "observed_at": _iso(now or _now()), "ref": f"https://rapidmapping.emergency.copernicus.eu/{code}",
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
            return {"id": f"glofas-{lat}-{lon}-{day}", "crossings": ids, "source": "GloFAS", "kind": "exposure",
                    "confidence": "modelled", "observed_at": _iso(now or _now()), "ref": hazards.GLOFAS,
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
                            "kind": "warning", "confidence": "official", "observed_at": _iso(now), "ref": METEOALARM,
                            "note": f"Official {colour} {kind} warning for Burgas until {info['expires'][:16]}: inspect high-consequence crossings."})
    return out


def _bbox(net, pad=0.05):
    lats = [c["lat"] for c in net.crossings.values()]
    lons = [c["lon"] for c in net.crossings.values()]
    return min(lats) - pad, min(lons) - pad, max(lats) + pad, max(lons) + pad


def datex_records(text, kind, net, now, ref):
    """DATEX II SituationPublication -> one signal per record in the region (crossings may be empty)."""
    root = ET.fromstring(re.sub(r'encoding="[^"]+"', "", text, count=1))
    south, west, north, east = _bbox(net)
    out = []
    for rec in root.iter(DATEX_NS + "situationRecord"):
        pts = [(float(p.findtext(DATEX_NS + "latitude")), float(p.findtext(DATEX_NS + "longitude")))
               for p in rec.iter(DATEX_NS + "pointCoordinates")]
        if not any(south <= a <= north and west <= b <= east for a, b in pts):
            continue
        start = _parse_time(rec.findtext(f".//{DATEX_NS}overallStartTime"))
        end = _parse_time(rec.findtext(f".//{DATEX_NS}overallEndTime"))
        if (start and start > now) or (end and end < now):
            continue
        texts = [v.text.strip() for v in rec.iter(DATEX_NS + "value") if v.text and v.text.strip()]
        joined = " ".join(texts)
        roads = set(ROAD_REF.findall(joined))
        record_kind = kind if HAZARD_WORDS.search(joined) else "planned"
        hit = []
        for cid, c in net.crossings.items():
            # Record points are locations along the road, often kilometres apart: never interpolate between them.
            d = min(distance_m(c["lat"], c["lon"], [p]) for p in pts)
            if c.get("road") in roads and d <= MATCH_ON_ROAD_M or not roads and d <= MATCH_ANY_M:
                hit.append(cid)
        created = _parse_time(rec.findtext(DATEX_NS + "situationRecordCreationTime")) or start or now
        summary = max(texts, key=len)[:90] if texts else ""
        label = "planned works" if record_kind == "planned" else kind
        out.append({"id": f"datex-{rec.get('id')}", "source": "Road agency (DATEX II)", "kind": record_kind,
                    "confidence": "official", "crossings": hit, "observed_at": _iso(min(created, now)),
                    "valid_from": _iso(start), "ref": ref,
                    "note": f"Official road {label} record: {summary}"})
    return out


def datex_signals(net, now=None):
    """Live: today's files (Bulgarian date), falling back to yesterday's if today's is not published yet."""
    now = now or _now()
    out = []
    for cat, kind in DATEX_CATS.items():
        for back in (0, 1):
            url = DATEX.format(day=(now + timedelta(hours=2) - timedelta(days=back)).strftime("%Y%m%d"), cat=cat)
            try:
                text = fetch(url)
            except FeedError:
                if back:
                    raise
                continue
            out += datex_records(text, kind, net, now, url)
            break
    return out


def flood_records(geojson, net, source, ref, observed_at):
    """Observed flood-extent polygons (GFM or CEMS delineation) -> crossings with flooding on or near the approaches."""
    rings = []
    for f in geojson.get("features") or [geojson]:
        g = f.get("geometry") or f
        polys = [g["coordinates"]] if g.get("type") == "Polygon" else g.get("coordinates") or [] if g.get("type") == "MultiPolygon" else []
        rings += [[(y, x) for x, y, *_ in poly[0]] for poly in polys if poly and len(poly[0]) >= 3]
    hit = []
    for cid, c in net.crossings.items():
        pad = 0.01
        near = [r for r in rings if any(abs(a - c["lat"]) < pad and abs(b - c["lon"]) < pad * 1.4 for a, b in r)
                or hazards.inside(c["lat"], c["lon"], r)]
        if any(hazards.inside(c["lat"], c["lon"], r) or distance_m(c["lat"], c["lon"], r + r[:1]) <= FLOOD_NEAR_M for r in near):
            hit.append(cid)
    return [{"id": f"flood-{source}-{observed_at}", "source": source, "kind": "inundation", "confidence": "observed",
             "crossings": hit, "observed_at": observed_at, "ref": ref,
             "note": f"{source}: observed flood extent within {FLOOD_NEAR_M} m of the crossing."}] if hit else []


def flood_extent_signals(net, now=None):
    """Copernicus GFM (Sentinel-1) or CEMS delineation flood extent as GeoJSON from FLOOD_EXTENT_URL."""
    url = os.environ.get("FLOOD_EXTENT_URL")
    if not url:
        raise NotConfigured("Set FLOOD_EXTENT_URL to a GFM or CEMS flood-extent GeoJSON (GFM needs a free account).")
    return flood_records(fetch_json(url), net, os.environ.get("FLOOD_EXTENT_SOURCE", "Copernicus GFM"), url, _iso(now or _now()))


SOURCES = {"Road agency (DATEX II)": datex_signals, "Copernicus EMS": cems_signals,
           "Flood extent (GFM / CEMS)": flood_extent_signals, "GloFAS": glofas_signals, "Meteoalarm": meteoalarm_signals}


# ---------------------------------------------------------------- fusion

def fuse(evidence):
    """Automatic status from independent evidence. Rules F1-F4 are labelled assumptions (see DESIGN.md)."""
    by = {}
    for e in evidence:
        by.setdefault(e.get("kind", "aoi"), set()).add(e["source"])
    if "closure" in by:
        return "confirmed", "F1 official closure record"
    flood = by.get("inundation", set())
    if flood and len(flood | by.get("exposure", set()) | by.get("restriction", set())) >= 2:
        return "confirmed", "F2 observed flooding + independent source"
    if flood or "restriction" in by:
        return "reported", "F3 observed flooding or official restriction"
    return "suspected", "F4 early warning"


def _evidence(s):
    return {"signal": s["id"], "source": s["source"], "kind": s.get("kind", "aoi"), "confidence": s.get("confidence", "official"),
            "observed_at": s.get("observed_at"), "ref": s.get("ref"), "note": s["note"][:160]}


def apply(signals, net=None, now=None):
    """Fuse new signals into incidents. Never downgrades; a signal already used for a crossing is not reused,
    so an operator's reopening is not undone by the same evidence. Returns crossings whose status changed."""
    net, now = net or network.load(), now or _now()
    by = {}
    for s in signals:
        for cid in s["crossings"]:
            by.setdefault(cid, []).append(s)
    changed = []
    for cid, sigs in by.items():
        items = [i for i in incidents._read() if i["crossing"] == cid]
        active = next((i for i in items if i["status"] != "reopened"), None)
        seen = {e["signal"] for i in items for e in i.get("evidence", [])}
        new = [s for s in sigs if s["id"] not in seen]
        if not new:
            continue
        status, rule = fuse((active or {}).get("evidence", []) + [_evidence(s) for s in new])
        if active and RANK[status] <= RANK[active["status"]]:
            status = active["status"]
        lead = next((s for s in new if s.get("kind") == "closure"), new[0])
        at = lead.get("valid_from") if status == "confirmed" and lead.get("valid_from") else lead.get("observed_at")
        body = {"crossing": cid, "status": status, "note": f"[auto {rule}] {lead['note']}"[:200],
                "evidence": [_evidence(s) for s in new]}
        if not active or status != active["status"]:
            body["at"] = at
            changed.append(cid)
        incidents.update(body, net, now, system=True)
    return changed


def scan(net=None, now=None, sources=None):
    net, now = net or network.load(), now or _now()
    changed = []
    for name, fn in (sources or SOURCES).items():
        start = time.monotonic()
        with _lock:
            prev = _health.get(name, {})
        try:
            signals = fn(net, now)
            new = apply(signals, net, now)
            changed += new
            matched = [s for s in signals if s["crossings"]]
            entry = {"status": "ok", "last_ok": now.isoformat(timespec="seconds"), "signals": len(signals),
                     "new_flags": len(new), "detail": (f"{len(signals)} record(s) in region, {len(matched)} at a crossing. " if signals else "")
                     + ("; ".join(s["note"] for s in matched[:3]) or "No active signal at a crossing.")}
        except NotConfigured as note:
            entry = {"status": "not configured", "detail": str(note)}
        except Exception as error:  # one failing source must never stop the others
            entry = {**prev, "status": "outdated" if prev.get("last_ok") else "unavailable",
                     "error": f"{type(error).__name__}: {str(error)[:120]}"}
        entry.update(checked_at=now.isoformat(timespec="seconds"), seconds=round(time.monotonic() - start, 1))
        with _lock:
            _health[name] = entry
    return changed


def health():
    with _lock:
        return {name: dict(_health.get(name, {"status": "not checked yet"})) for name in SOURCES}


# ---------------------------------------------------------------- replay of recorded official data

def replay_signals(net=None):
    """EMSR693 (Burgas flood, 5 Sep 2023): crossings inside the official area of interest, from a stored copy."""
    net = net or network.load()
    data = json.loads(REPLAY_AOI.read_text(encoding="utf-8"))
    return [{"id": "EMSR693", "source": "Copernicus EMS", "kind": "aoi", "confidence": "official",
             "crossings": crossings_in(wkt_rings(data["extent"]), net), "observed_at": "2023-09-05T17:00:00+00:00",
             "ref": data["source"], "note": "EMSR693 (Flood, 5 Sep 2023): crossing inside the official area of interest."}]


EVENT = datetime(2023, 9, 5, 17, 0, tzinfo=timezone.utc)  # EMSR693 event time, 20:00 local
REPLAY_FILES = [(datetime(2023, 9, 15, 9, 0, tzinfo=timezone.utc), "datex_20230915_roadworks_r02.xml", "restriction"),
                (datetime(2023, 9, 18, 9, 0, tzinfo=timezone.utc), "datex_20230918_roadworks_r02.xml", "restriction")]


def _shift(signals, delta):
    for s in signals:
        for key in ("observed_at", "valid_from"):
            if s.get(key):
                original = datetime.fromisoformat(s[key])
                s[key] = _iso(original + delta)
                if key == "observed_at":
                    s["note"] = f"{s['note'][:150]} [record of {original:%d %b %Y}]"
    return signals


def replay(net, now, full=False, key=None):
    """Plays the recorded 2023 official data through the same adapters and fusion rule, shifted to end at `now`.
    Short: the first 6.5 hours. Full: 13 days, up to the last road-agency record."""
    end = REPLAY_FILES[-1][0] if full else EVENT + timedelta(hours=6, minutes=30)
    delta = now - end
    road = next(c["id"] for c in network.bridges(net)["crossings"] if c["road"] == "9901")
    apply(_shift(replay_signals(net), delta), net, EVENT + delta)
    incidents.update({"crossing": road, "status": "reported", "at": _iso(EVENT + delta), "key": key,
                      "note": "EMSR693 activation text: 'The main road Tsarevo - Sinemoretz is closed.' Exact bridge not confirmed."},
                     net, EVENT + delta)
    incidents.update({"crossing": road, "status": "confirmed", "at": _iso(EVENT + timedelta(minutes=30) + delta),
                      "expected_hours": 72, "key": key, "note": "Operator confirmed from the activation text."},
                     net, EVENT + timedelta(minutes=30) + delta)
    if not full:
        return
    incidents.update({"crossing": road, "status": "reopened", "at": _iso(EVENT + timedelta(hours=72) + delta), "key": key,
                      "note": "Assumed: reopening time is not in any record; set to the 72-hour planning value."},
                     net, EVENT + timedelta(hours=72) + delta)
    for when, name, kind in REPLAY_FILES:
        text = (REPLAY_DIR / name).read_text(encoding="utf-8")
        apply(_shift(datex_records(text, kind, net, when, f"https://datasheet.api.bg/files/{name[6:]}"), delta), net, when + delta)


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
