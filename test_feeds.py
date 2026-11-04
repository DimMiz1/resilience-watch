from datetime import datetime, timezone
import io
import json
from pathlib import Path
import threading
import unittest
from unittest import mock
import urllib.error
import urllib.request

import feeds
from local_helpers import haversine_km, observation_age_minutes, parse_firms_csv
from server import Handler, ThreadingHTTPServer
import server


def event(kind="FL", lon=25.3, lat=42.8):
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {"eventtype": kind, "eventid": 1234, "name": "Flood report",
                           "datemodified": "2026-11-02T08:00:00", "alertlevel": "Orange",
                           "iscurrent": "true"}}


class HelperTests(unittest.TestCase):
    def test_distance_and_input_validation(self):
        self.assertEqual(haversine_km(42, 25, 42, 25), 0)
        self.assertTrue(110 < haversine_km(0, 0, 1, 0) < 112)
        self.assertTrue(20000 < haversine_km(0, 0, 0, 180) < 20020)
        for args in [(91, 0, 0, 0), (0, 0, 0, 181), (float("nan"), 0, 0, 0)]:
            with self.assertRaises(ValueError):
                haversine_km(*args)

    def test_observation_age(self):
        now = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
        for stamp in ("2026-11-02T11:00:00Z", "2026-11-02T11:00:00",
                      "2026-11-02T13:00:00+02:00"):
            self.assertEqual(observation_age_minutes(stamp, now), 60)
        self.assertEqual(observation_age_minutes("2026-11-02T13:00:00Z", now), 0)
        with self.assertRaises(ValueError):
            observation_age_minutes("bad", now)
        with self.assertRaises(ValueError):
            observation_age_minutes("2026-11-02T11:00:00", datetime(2026, 11, 2))

    def test_parser_rejects_missing_time_nonfinite_and_bad_dates(self):
        text = ("latitude,longitude,acq_date,acq_time\n"
                "42,25,2026-11-02,\n"
                "nan,25,2026-11-02,1200\n"
                "42,inf,2026-11-02,1200\n"
                "42,25,not-date,1200\n"
                "42,25,2026-11-02\n")
        rows, skipped = parse_firms_csv(text)
        self.assertEqual(rows, [])
        self.assertEqual(skipped, 5)


class FeedTests(unittest.TestCase):
    def test_gdacs_filters_types_distance_and_exposes_uncertainty(self):
        data = {"features": [event(), event("WF"), event("EQ"), event(lon=0), {}]}
        result = feeds.normalize_gdacs(data, 42.87, 25.32)
        self.assertEqual(len(result["events"]), 2)
        self.assertEqual(result["skipped_records"], 1)
        record = result["events"][0]
        self.assertEqual(record["geometry_kind"], "event centroid, not extent")
        self.assertEqual(record["alert"], "Orange")
        self.assertTrue(record["source_reports_current"])
        self.assertIn("eventid=1234", record["report_url"])

    def test_gdacs_missing_list_is_an_error_not_an_empty_success(self):
        with self.assertRaises(feeds.FeedError):
            feeds.normalize_gdacs({}, 42, 25)

    def test_firms_csv_has_utc_times_and_reports_invalid_rows(self):
        text = ("latitude,longitude,acq_date,acq_time,confidence\n"
                "42.9,25.3,2026-11-02,45,n\n"
                "90,0,2026-11-02,1200,n\n"
                "bad,25,2026-11-02,1200,n\n")
        result = feeds.normalize_firms(text, 42.87, 25.32)
        self.assertEqual(len(result["events"]), 1)
        self.assertEqual(result["events"][0]["observed_at"], "2026-11-02T00:45:00+00:00")
        self.assertEqual(result["latest_source_observation"], "2026-11-02T12:00:00+00:00")
        self.assertEqual(result["skipped_records"], 1)
        with self.assertRaises(feeds.FeedError):
            feeds.normalize_firms("Invalid MAP_KEY", 42, 25)

    def test_firms_public_feed_requires_no_key_in_bulgaria(self):
        csv_text = "latitude,longitude,acq_date,acq_time\n"
        with mock.patch.dict(feeds.os.environ, {"FIRMS_MAP_KEY": ""}), \
                mock.patch.object(feeds, "fetch", return_value=csv_text) as get:
            data = feeds.firms_feed(42, 25)
        self.assertEqual(data["mode"], "public-europe")
        self.assertEqual(data["window_hours"], 24)
        self.assertEqual(data["events"], [])
        get.assert_called_once_with(feeds.FIRMS_EUROPE)

    def test_firms_missing_key_outside_supported_region_is_explicit(self):
        with mock.patch.dict(feeds.os.environ, {"FIRMS_MAP_KEY": ""}):
            data = feeds.firms_feed(0, 0)
        self.assertTrue(data["configuration_required"])
        self.assertIn("unknown", data["message"])

    def test_bad_key_is_not_silently_replaced_by_a_different_source(self):
        with mock.patch.dict(feeds.os.environ, {"FIRMS_MAP_KEY": "test-key"}), \
                mock.patch.object(feeds, "fetch", side_effect=feeds.FeedError("HTTP 403")) as get:
            with self.assertRaises(feeds.FeedError):
                feeds.firms_feed(42, 25)
        self.assertEqual(get.call_count, 1)

    def test_bad_coordinates_rejected(self):
        for lat in ("nan", "inf", "81", "hello"):
            with self.assertRaises(ValueError):
                feeds.query_location({"lat": [lat]})
        with self.assertRaises(ValueError):
            feeds.query_location({"lon": ["179"]})

    def test_weather_missing_current_is_an_error(self):
        with mock.patch.object(feeds, "fetch_json", return_value={}):
            with self.assertRaises(feeds.FeedError):
                feeds.weather_feed(42, 25)

    def test_satellite_only_exposes_catalogue_not_download_credentials(self):
        raw = {"features": [{"id": "test", "geometry": {"type": "Polygon", "coordinates": [
            [[25, 42], [26, 42], [26, 43], [25, 42]]]},
                             "properties": {"datetime": "2026-11-02T08:00:00Z"},
                             "assets": {"secret": "not returned"}}]}
        with mock.patch.object(feeds, "fetch_json", return_value=raw):
            data = feeds.satellite_feed(42, 25)
        self.assertIn("No imagery analysis", data["message"])
        self.assertNotIn("assets", data["scenes"][0])

    def test_bad_footprints_are_excluded_and_counted(self):
        raw = {"features": [{"id": "test", "geometry": {"type": "Polygon", "coordinates": []},
                             "properties": {"datetime": "2026-11-02T08:00:00Z"}}]}
        with mock.patch.object(feeds, "fetch_json", return_value=raw):
            data = feeds.satellite_feed(42, 25)
        self.assertEqual(data["scenes"], [])
        self.assertEqual(data["skipped_records"], 1)

    def test_source_error_and_stale_cache_are_not_available(self):
        cache = feeds.FeedCache()
        with mock.patch.dict(feeds.SOURCES, {"gdacs": ("GDACS", mock.Mock(side_effect=feeds.FeedError("outage")), "limited")}):
            result = cache.get("gdacs", 42, 25)
        self.assertEqual(result["status"], "error")
        self.assertIsNone(result["data"])
        cache = feeds.FeedCache()
        loader = mock.Mock(side_effect=[{"events": []}, feeds.FeedError("outage")])
        with mock.patch.dict(feeds.SOURCES, {"gdacs": ("GDACS", loader, "limited")}):
            good = cache.get("gdacs", 42, 25)
            self.assertEqual(cache.get("gdacs", 42, 25), good)
            self.assertEqual(loader.call_count, 1)
            cache.entries[("gdacs", 42, 25)]["_checked"] -= feeds.TTL + 1
            stale = cache.get("gdacs", 42, 25)
        self.assertEqual(stale["status"], "stale")
        self.assertEqual(stale["data"], {"events": []})
        self.assertEqual(stale["fetched_at"], good["fetched_at"])
        self.assertEqual(stale["error"], "outage")

    def test_http_errors_do_not_expose_keys(self):
        url = "https://provider.test/SECRET_KEY/data"
        error = urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
        with mock.patch.object(feeds.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(feeds.FeedError) as caught:
                feeds.fetch(url)
        self.assertNotIn("SECRET_KEY", str(caught.exception))

    def test_empty_valid_feeds_are_not_fabricated(self):
        self.assertEqual(feeds.normalize_gdacs({"features": []}, 42, 25)["events"], [])
        self.assertEqual(feeds.normalize_firms("latitude,longitude,acq_date,acq_time\n", 42, 25)["events"], [])


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def test_health_and_static_page(self):
        with urllib.request.urlopen(self.base + "/api/health") as response:
            health = json.load(response)
        self.assertEqual(health["evacuation_guidance"], "advisory")
        self.assertIn("flood_ml", health["sources"])
        self.assertIn("emergency", health["sources"])
        with urllib.request.urlopen(self.base) as response:
            text = response.read().decode()
        self.assertIn("Follow local emergency services", text)
        self.assertNotIn("BG-ALERT", text)
        self.assertIn('id="emergency-call"', text)
        self.assertIn('id="map"', text)
        self.assertIn('id="evacuate"', text)
        self.assertIn("CASSINI 12", text)
        self.assertIn('id="map-detail"', text)
        self.assertIn('id="rain-window"', text)

    def test_bad_api_query_and_unknown_path(self):
        for path, code in [("/api/gdacs?lat=NaN", 400), ("/api/evacuate?lat=95", 400), ("/api/map?lat=NaN", 400),
                           ("/api/emergency?lat=NaN", 400), ("/api/weather/v1?lat=NaN", 400),
                           ("/api/destinations", 400), ("/api/destinations?lat=42", 400),
                           ("/api/destinations?lat=&lon=25", 400),
                           ("/api/destinations?lat=42&lon=25&accuracy_m=-1", 400),
                           ("/api/destinations?lat=42&lon=25&accuracy_m=NaN", 400),
                           ("/api/destinations?lat=42&lon=25&accuracy_m=", 400),
                           ("/api/route", 404), ("/../server.py", 404)]:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(self.base + path)
            self.assertEqual(caught.exception.code, code)

    def test_map_api_preserves_partial_status_and_geojson(self):
        payload = {"schema": "resilience-map-v1", "location": {"lat": 42.87, "lon": 25.32},
                   "incomplete_inputs": ["rainfall"], "feeds": {"rainfall": {"status": "error"}},
                   "geojson": {"type": "FeatureCollection", "features": []}}
        with mock.patch.object(server.map_data, "map_layers", return_value=payload) as build:
            with urllib.request.urlopen(self.base + "/api/map?lat=42.87&lon=25.32") as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(json.load(response), payload)
        build.assert_called_once_with(server.CACHE, 42.87, 25.32)

    def test_emergency_api_preserves_country_context(self):
        payload = {"status": "available", "data": {"country_code": "GB", "primary_number": "999"}}
        with mock.patch.object(server.CACHE, "get", return_value=payload) as get:
            with urllib.request.urlopen(self.base + "/api/emergency?lat=51.5&lon=-0.1") as response:
                self.assertEqual(json.load(response), payload)
        get.assert_called_once_with("emergency", 51.5, -0.1)

    def test_versioned_weather_route(self):
        payload = {"schema": "resilience-weather-v1", "feeds": {}, "incomplete_inputs": []}
        with mock.patch.object(server.map_data, "weather_api", return_value=payload) as build:
            with urllib.request.urlopen(self.base + "/api/weather/v1?lat=42.87&lon=25.32") as response:
                self.assertEqual(json.load(response), payload)
        build.assert_called_once_with(server.CACHE, 42.87, 25.32)

    def test_destination_route_preserves_location_and_uncertainty(self):
        payload = {"schema": "resilience-destinations-v1", "safety_status": "unknown"}
        with mock.patch.object(server, "destinations", return_value=payload) as build:
            with urllib.request.urlopen(self.base + "/api/destinations?lat=42.87&lon=25.32&accuracy_m=12") as response:
                self.assertEqual(json.load(response), payload)
        build.assert_called_once_with(42.87, 25.32, 12)

    def test_phone_installation_assets_are_served(self):
        for path, mime in [("/manifest.webmanifest", "application/manifest+json"),
                           ("/sw.js", "text/javascript"), ("/offline.html", "text/html"),
                           ("/app-icon-192.png", "image/png"), ("/app-icon-512.png", "image/png"),
                           ("/app-icon.svg", "image/svg+xml")]:
            with urllib.request.urlopen(self.base + path) as response:
                self.assertIn(mime, response.headers["Content-Type"])
                self.assertTrue(response.read())

    def test_project_overview_is_professional_and_has_no_sharing_workflow(self):
        with urllib.request.urlopen(self.base + "/about") as response:
            text = response.read().decode()
            self.assertIn("Project overview", text)
            self.assertIn('href="/"', text)
            self.assertNotIn("mentor", text.lower())
            self.assertNotIn("mailto:", text.lower())
            self.assertNotIn("sharing", text.lower())
            self.assertNotIn('src="/app.js"', text)
            self.assertNotIn("leaflet", text)
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(self.base + "/share.js")
        self.assertEqual(error.exception.code, 404)

    def test_access_logs_do_not_record_gps_queries(self):
        with mock.patch.object(Handler, "log_message") as log:
            with urllib.request.urlopen(self.base + "/about?lat=42.123456&secret=private"):
                pass
        rendered = " ".join(str(arg) for call in log.call_args_list for arg in call.args)
        self.assertIn("/about", rendered)
        self.assertNotIn("42.123456", rendered)
        self.assertNotIn("private", rendered)


if __name__ == "__main__":
    unittest.main()
