"""API contract tests for map exploration; forecast samples are not observations."""
from datetime import datetime, timezone
import json
import math
import unittest
from unittest import mock

import map_data
import ml
import server
from feeds import FeedError

NOW = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)
DAYS = ["2026-11-03", "2026-11-04", "2026-11-05"]


def forecasts():
    return [{"latitude": 42.875, "longitude": 25.325,
             "hourly": {"time": ["2026-11-03T12:00"], "precipitation": [1.5]},
             "daily": {"time": DAYS, "precipitation_sum": [12, 8, 1]}} for _ in range(49)]


class RainfallTests(unittest.TestCase):
    def test_samples_show_model_resolution_and_whole_day_timing(self):
        with mock.patch.object(map_data, "fetch", return_value=json.dumps(forecasts())) as fetch, \
                mock.patch.object(map_data, "utc_now", lambda: NOW):
            result = map_data.rainfall_feed(42.87, 25.32)
        self.assertEqual(len(result["samples"]), 49)
        self.assertEqual(result["mesh_rows"], 7)
        self.assertEqual(result["samples"][-1]["col"], 6)
        self.assertEqual(result["dates"], DAYS)
        self.assertEqual(result["samples"][0]["total_mm"], 21)
        self.assertEqual(result["unique_model_cells"], 1)
        self.assertEqual(len(result["cells"]), 1)
        self.assertEqual(result["cells"][0]["geometry"]["type"], "Polygon")
        self.assertEqual(result["cells"][0]["current_hour"]["rain_mm"], 1.5)
        self.assertEqual(len(result["cells"][0]["hourly"]), 24)
        self.assertEqual(result["cells"][0]["hourly"][0]["rain_mm"], 1.5)
        self.assertIsNone(result["cells"][0]["hourly"][1]["rain_mm"])
        self.assertFalse(result["lineage"]["copernicus_satellite_measurement"])
        ring = result["cells"][0]["geometry"]["coordinates"][0]
        self.assertEqual(ring[0], ring[-1])
        self.assertAlmostEqual(ring[1][0] - ring[0][0], 0.25)
        self.assertIn("approximate", result["cells"][0]["geometry_kind"])
        self.assertIn("ecmwf_ifs025", fetch.call_args.args[0])
        self.assertIn("not radar", result["kind"])
        self.assertNotIn("flood_probability", result["samples"][0])

    def test_missing_or_invalid_samples_never_become_zero_rain(self):
        for invalid in (None, -2, math.nan, True):
            data = forecasts()
            data[3]["daily"]["precipitation_sum"][1] = invalid
            for index, reply in enumerate(data):
                reply["longitude"] = 25 + index * 0.01
            with self.subTest(invalid=invalid), \
                    mock.patch.object(map_data, "fetch", return_value=json.dumps(data)), \
                    mock.patch.object(map_data, "utc_now", lambda: NOW):
                result = map_data.rainfall_feed(42.87, 25.32)
                self.assertIsNone(result["samples"][3]["daily"][1]["rain_mm"])
                self.assertIsNone(result["samples"][3]["total_mm"])
                self.assertEqual(result["quality"]["unknown_daily_values"], 1)

    def test_malformed_provider_response_is_explicit(self):
        for data in ("{}", "invalid", json.dumps(forecasts()[:8])):
            with self.subTest(data=data), mock.patch.object(map_data, "fetch", return_value=data), self.assertRaises(FeedError):
                map_data.rainfall_feed(42.87, 25.32)

    def test_hourly_missing_and_conflicting_duplicate_cells_are_unknown(self):
        data = forecasts()
        data[0]["hourly"]["precipitation"] = [None]
        data[0]["longitude"] = 24.5
        with mock.patch.object(map_data, "fetch", return_value=json.dumps(data)), \
                mock.patch.object(map_data, "utc_now", lambda: NOW):
            result = map_data.rainfall_feed(42.87, 25.32)
            self.assertIsNone(result["samples"][0]["current_hour"]["rain_mm"])
        data = forecasts()
        for item in data:
            item["hourly"]["time"].append("2026-11-03T13:00")
            item["hourly"]["precipitation"].append(2)
        data[-1]["hourly"]["precipitation"][1] = 3
        with mock.patch.object(map_data, "fetch", return_value=json.dumps(data)), \
                mock.patch.object(map_data, "utc_now", lambda: NOW), self.assertRaisesRegex(FeedError, "conflicting"):
            map_data.rainfall_feed(42.87, 25.32)
        data = forecasts()
        data[-1]["daily"]["precipitation_sum"][0] = 45
        with mock.patch.object(map_data, "fetch", return_value=json.dumps(data)), \
                mock.patch.object(map_data, "utc_now", lambda: NOW), self.assertRaisesRegex(FeedError, "conflicting"):
            map_data.rainfall_feed(42.87, 25.32)


class MapContractTests(unittest.TestCase):
    def test_weather_api_has_lineage_and_does_not_request_slow_research(self):
        cache = mock.Mock()
        cache.get.side_effect = lambda source, lat, lon: {
            "status": "error" if source == "weather" else "available", "source": source,
            "data": None if source == "weather" else {}, "error": "Provider down" if source == "weather" else None}
        result = map_data.weather_api(cache, 42.87, 25.32)
        self.assertEqual(result["schema"], "resilience-weather-v1")
        self.assertEqual(result["incomplete_inputs"], ["weather"])
        self.assertEqual({call.args[0] for call in cache.get.call_args_list}, {"rainfall", "weather", "emergency"})
        self.assertIn("not a Copernicus satellite", result["data_policy"]["forecast"])
        self.assertIn("sentinel_water_burn_indicators", result["data_policy"]["copernicus_evidence"])

    def test_missing_feeds_preserve_other_layers(self):
        cache = mock.Mock()
        cache.get.side_effect = lambda source, lat, lon: {
            "status": "error" if source == "rainfall" else "available",
            "source": source, "data": None if source == "rainfall" else {"cells": []},
            "error": "Provider unavailable" if source == "rainfall" else None,
        }
        result = map_data.map_layers(cache, 42.87, 25.32)
        self.assertEqual(result["schema"], "resilience-map-v1")
        self.assertEqual(result["incomplete_inputs"], ["rainfall"])
        self.assertEqual(set(result["feeds"]), set(map_data.MAP_SOURCES))
        self.assertEqual(result["feeds"]["ponding"]["status"], "available")
        self.assertEqual(cache.get.call_count, 4)
        self.assertEqual(result["geojson"]["type"], "FeatureCollection")
        self.assertEqual(result["geojson"]["features"], [])

    def test_map_sources_are_registered(self):
        self.assertTrue(set(map_data.MAP_SOURCES).issubset(server.SOURCES))

    def test_downhill_connections_slope_and_sampled_runoff(self):
        elevation = [100 + abs(r - 4) + abs(c - 4) for r in range(9) for c in range(9)]
        with mock.patch.object(ml, "fetch_json", side_effect=[
            {"elevation": elevation}, {"daily": {"time": DAYS, "precipitation_sum": [12, 8, 1]}},
        ]), mock.patch.object(ml, "utc_now", lambda: NOW):
            data = ml.ponding_feed(42.87, 25.32)
        by_position = {(c["row"], c["col"]): c for c in data["cells"]}
        top = data["collectors"][0]
        self.assertEqual(top["upstream_km2"], 81)
        self.assertEqual(top["inflow_m3_if_all_runs_off"], 1701000)
        self.assertIsNone(top["downstream"])
        self.assertGreater(by_position[0, 0]["downhill_slope_pct"], 0)
        self.assertIsNotNone(by_position[0, 0]["downstream"])
        self.assertEqual(data["valid_cells"], 81)
        self.assertEqual(data["rain_dates"], DAYS)
        self.assertIn("clipped", data["message"])
        self.assertEqual(data["missing_cells"], 0)
        feeds = {s: {"status": "error", "source": s, "data": None} for s in map_data.MAP_SOURCES}
        feeds["ponding"] = {"status": "stale", "source": "Copernicus DEM", "data": data,
                            "fetched_at": "2026-11-03T12:00:00Z", "limitation": data["message"]}
        geojson = map_data.map_features(feeds, 42.87, 25.32)
        feature = next(f for f in geojson["features"] if f["id"] == "collector-4-4")
        self.assertEqual(feature["geometry"]["type"], "Point")
        self.assertEqual(feature["geometry"]["coordinates"], [25.32, 42.87])
        self.assertEqual(feature["properties"]["status"], "stale")
        self.assertEqual(feature["properties"]["kind"], "candidate_terrain_minimum")
        lines = [f for f in geojson["features"] if f["geometry"]["type"] == "LineString"]
        self.assertEqual(len(lines), 80)
        self.assertTrue(all(f["properties"]["kind"] == "sampled_downhill_connection" for f in lines))

    def test_rain_geojson_has_forecast_kind_dates_and_model_cell(self):
        with mock.patch.object(map_data, "fetch", return_value=json.dumps(forecasts())), \
                mock.patch.object(map_data, "utc_now", lambda: NOW):
            rain = map_data.rainfall_feed(42.87, 25.32)
        feeds = {s: {"status": "error", "source": s, "data": None} for s in map_data.MAP_SOURCES}
        feeds["rainfall"] = {"status": "available", "source": "ECMWF", "data": rain}
        geojson = map_data.map_features(feeds, 42.87, 25.32)
        self.assertEqual(len(geojson["features"]), 50)
        props = geojson["features"][0]["properties"]
        self.assertEqual(props["kind"], "rainfall_forecast_sample")
        self.assertEqual(props["three_day_total_mm"], 21)
        self.assertEqual(props["daily"][0]["date"], DAYS[0])
        self.assertEqual(props["model_cell"]["lat"], 42.875)
        area = geojson["features"][-1]
        self.assertEqual(area["geometry"]["type"], "Polygon")
        self.assertEqual(area["properties"]["kind"], "approximate_rainfall_model_support")


if __name__ == "__main__":
    unittest.main()
