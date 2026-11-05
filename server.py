"""Resilience Watch prototype. Run: py -3 resilience\\server.py"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import time
import urllib.parse

from feeds import FeedCache, SOURCES, coordinate, query_location
import hazards
import ml
import copernicus
import fire_ml
import map_data
import emergency
import network
import incidents
import watcher

SOURCES.update({
    "emergency": ("Country, region and official contacts", emergency.emergency_feed,
                  "Approximate OSM administrative lookup; curated authority links are not live alerts or verified dispatch boundaries."),
    "places": ("Nearby mapped facilities (OpenStreetMap)", hazards.places_feed,
               "Unverified assembly points and facilities, not designated safe evacuation destinations."),
    "rainfall": ("ECMWF rainfall forecast samples", map_data.rainfall_feed,
                "49 model samples at nominal 0.25-degree spacing; smoothed animation is interpolated forecast rain, not radar."),
    "fire_ml": ("Experimental fire ML (team-labelled data)", fire_ml.fire_ml_feed,
                "Requires a local, reviewed training dataset; no fire probabilities are invented without labels."),
    "sentinel_indices": ("Copernicus Sentinel-2 water and burn indicators", copernicus.sentinel_indices_feed,
                         "Direct CDSE statistics require OAuth credentials. Spectral indicators, not confirmed hazards."),
    "effis": ("Copernicus EMS â€“ EFFIS burnt areas", hazards.effis_feed,
              "Mapped burnt-area perimeters (MODIS 30 days + VIIRS NRT). Where land burned, not where fire is now."),
    "glofas": ("Copernicus EMS â€“ GloFAS river forecast", hazards.glofas_feed,
               "Daily discharge of a ~5 km river cell vs. its own 10-year record. Not street-level flooding."),
    "cems": ("Copernicus EMS â€“ Rapid Mapping activations", hazards.cems_feed,
             "Official activations; centroids only. Open the activation for mapped extents."),
    "flood_ml": ("Rain-to-flood ML model (ERA5 + GloFAS + ECMWF)", ml.flood_ml_feed,
                 "Logistic regression trained per location; validated on the newest 20% of the record. Not an official warning."),
    "ponding": ("Rain pile-up terrain (Copernicus DEM + ECMWF rain)", ml.ponding_feed,
                "1 km D8 drainage on the 90 m DEM. Where runoff concentrates, not mapped flooding."),
    "weather_risk": ("Lightning and fire-weather factors (ECMWF)", ml.weather_risk_feed,
                     "Transparent rule-based factors, not a trained model. EFFIS FWI is the official product."),
})
STATIC = Path(__file__).with_name("static")
CACHE = FeedCache()
EVACUATION_INPUTS = ("firms", "effis", "gdacs", "cems", "weather", "glofas", "flood_ml", "ponding")
DESTINATION_INPUTS = ("places", "firms", "effis", "gdacs", "cems")


def destinations(lat, lon, accuracy_m=None):
    with ThreadPoolExecutor(len(DESTINATION_INPUTS)) as pool:
        feeds = dict(zip(DESTINATION_INPUTS,
                         pool.map(lambda s: CACHE.get(s, lat, lon), DESTINATION_INPUTS)))
    return hazards.destination_candidates(lat, lon, feeds, accuracy_m)


def evacuate(lat, lon):
    with ThreadPoolExecutor(len(EVACUATION_INPUTS)) as pool:
        feeds = dict(zip(EVACUATION_INPUTS, pool.map(lambda s: CACHE.get(s, lat, lon), EVACUATION_INPUTS)))

    def data(source):
        return feeds[source].get("data") or {}

    wind = (data("weather").get("current") or {}).get("wind_direction_10m")
    tracks = hazards.fire_tracks(data("firms").get("events") or [], wind)
    flood_events = [e for e in data("gdacs").get("events") or [] if e.get("type") == "flood"]
    level = (data("glofas").get("peak") or {}).get("level")
    probability = data("flood_ml").get("peak_probability")
    extra = []
    # A river-cell score and coarse drainage points do not establish flooded areas.
    plan = hazards.evacuation_plan(lat, lon, tracks, data("effis").get("areas") or [], flood_events,
                                   data("cems").get("nearby") or [], level, extra_hazards=extra)
    plan["fire_tracks"] = tracks
    plan["flood_probability"] = probability
    plan["flood_score_limitation"] = "Experimental high-flow score; not a calibrated flood probability or a mapped hazard."
    plan["inputs"] = {s: {"source": f["source"], "status": f["status"], "error": f["error"]} for s, f in feeds.items()}
    plan["missing_inputs"] = [s for s, f in feeds.items() if f["status"] in ("error", "unconfigured")]
    context = CACHE.get("emergency", lat, lon)
    plan["emergency_context"] = context
    plan["official_guidance_first"] = "Follow the emergency services and official authorities for your actual location. This is advisory only."
    return plan


class Handler(BaseHTTPRequestHandler):
    def log_request(self, code="-", size="-"):
        # Hosted access logs must not retain GPS coordinates from query strings.
        self.log_message('"%s %s" %s %s', self.command, urllib.parse.urlsplit(self.path).path, code, size)

    def respond(self, status, content, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        # OSM's public tile policy requires a Referer identifying the web application.
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(content)

    def json(self, status, value):
        self.respond(status, json.dumps(value, allow_nan=False).encode(), "application/json; charset=utf-8")

    def do_GET(self):
        request = urllib.parse.urlsplit(self.path)
        if request.path == "/api/health":
            self.json(200, {"status": "ok", "mode": "prototype", "sources": sorted(SOURCES),
                            "evacuation_guidance": "advisory"})
            return
        if request.path == "/api/destinations":
            try:
                query = urllib.parse.parse_qs(request.query, keep_blank_values=True)
                if not query.get("lat") or not query.get("lon"):
                    raise ValueError("Latitude and longitude are required; no default location assumed.")
                lat, lon = query_location(query)
                accuracy = coordinate(query["accuracy_m"][0], 0, 100000) if "accuracy_m" in query else None
            except ValueError as error:
                self.json(400, {"error": str(error)})
                return
            self.json(200, destinations(lat, lon, accuracy))
            return
        if request.path == "/api/map":
            try:
                lat, lon = query_location(urllib.parse.parse_qs(request.query))
            except ValueError as error:
                self.json(400, {"error": str(error)})
                return
            self.json(200, map_data.map_layers(CACHE, lat, lon))
            return
        if request.path == "/api/weather/v1":
            try:
                lat, lon = query_location(urllib.parse.parse_qs(request.query))
            except ValueError as error:
                self.json(400, {"error": str(error)})
                return
            self.json(200, map_data.weather_api(CACHE, lat, lon))
            return
        if request.path == "/api/evacuate":
            try:
                lat, lon = query_location(urllib.parse.parse_qs(request.query))
            except ValueError as error:
                self.json(400, {"error": str(error)})
                return
            self.json(200, evacuate(lat, lon))
            return
        if request.path == "/api/incidents":
            self.json(200, operations_state())
            return
        if request.path == "/api/infrastructure/bridges":
            self.json(200, network.bridges())
            return
        if request.path == "/api/infrastructure/scenario":
            query = {k: v[0] for k, v in urllib.parse.parse_qs(request.query).items()}
            try:
                ids = [x for x in query.get("bridge", "").split(",") if x][:5]
                self.json(200, network.scenario(ids, query.get("hours", 24), query.get("threshold", 30),
                                                 query.get("journeys"), query.get("occupancy"),
                                                 query.get("value_of_time")))
            except ValueError as error:
                self.json(400, {"error": str(error)})
            return
        if request.path.startswith("/api/"):
            source = request.path[len("/api/"):]
            if source not in SOURCES:
                self.json(404, {"error": "Unknown source."})
                return
            try:
                lat, lon = query_location(urllib.parse.parse_qs(request.query))
            except ValueError as error:
                self.json(400, {"error": str(error)})
                return
            self.json(200, CACHE.get(source, lat, lon))
            return
        files = {"/": ("index.html", "text/html; charset=utf-8"),
                 "/about": ("overview.html", "text/html; charset=utf-8"),
                 "/impact": ("impact.html", "text/html; charset=utf-8"),
                 "/impact.js": ("impact.js", "text/javascript; charset=utf-8"),
                 "/operations": ("operations.html", "text/html; charset=utf-8"),
                 "/operations.js": ("operations.js", "text/javascript; charset=utf-8"),
                 "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                 "/rain_renderer.js": ("rain_renderer.js", "text/javascript; charset=utf-8"),
                 "/style.css": ("style.css", "text/css; charset=utf-8"),
                 "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
                 "/sw.js": ("sw.js", "text/javascript; charset=utf-8"),
                 "/offline.html": ("offline.html", "text/html; charset=utf-8"),
                 "/app-icon-192.png": ("app-icon-192.png", "image/png"),
                 "/app-icon-512.png": ("app-icon-512.png", "image/png"),
                 "/app-icon.svg": ("app-icon.svg", "image/svg+xml")}
        if request.path not in files:
            self.json(404, {"error": "Not found."})
            return
        filename, mime = files[request.path]
        self.respond(200, (STATIC / filename).read_bytes(), mime)

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if path not in ("/api/incidents", "/api/incidents/demo", "/api/incidents/reset", "/api/incidents/scan"):
            self.json(404, {"error": "Not found."})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 <= length <= 4096:
                raise ValueError("Request body too large.")
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("JSON object expected.")
            if path == "/api/incidents":
                incidents.update(body)
            elif path == "/api/incidents/demo":
                incidents.load_demo(body.get("key"), full=bool(body.get("full")))
            elif path == "/api/incidents/scan":
                global _last_scan
                if time.monotonic() - _last_scan < 60:
                    raise ValueError("Official sources were checked less than a minute ago.")
                _last_scan = time.monotonic()
                watcher.scan()
            else:
                incidents.reset(body.get("key"))
        except PermissionError as error:
            self.json(403, {"error": str(error)})
            return
        except ValueError as error:
            self.json(400, {"error": str(error)})
            return
        self.json(200, operations_state())


_last_scan = -60.0


def operations_state():
    return {**incidents.situation(), "sources": watcher.health()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"),
                        help="Bind address; use 0.0.0.0 behind an HTTPS reverse proxy or container platform.")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8088")))
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    try:
        with ThreadingHTTPServer((args.host, args.port), Handler) as server:
            if os.environ.get("WATCHER", "on").lower() != "off":
                watcher.start()
            print(f"Resilience Watch: http://{args.host}:{args.port}", flush=True)
            print("Advisory prototype. Follow official emergency services for your location.", flush=True)
            server.serve_forever()
    except OSError as error:
        print(f"Could not serve dashboard: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
