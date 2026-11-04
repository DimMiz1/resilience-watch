"""Offline tests for ml.py and hazards.py. Providers are mocked; no network is used."""
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import random
import tempfile
import unittest
from unittest import mock

import hazards
import ml

NOW = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)


def daily(days, key, values):
    return {"daily": {"time": days, key: values}}


def synthetic_river(start, count, seed=4):
    """Rain that, after a wet spell, makes the river rise - the pattern the model must learn."""
    rnd = random.Random(seed)
    days = [(start + timedelta(days=i)).isoformat() for i in range(count)]
    rain = [rnd.choice([0, 0, 0, 0, 1, 3, 8, 25]) * 1.0 for _ in days]
    flow = []
    for i in range(count):
        flow.append(1.0 + 0.4 * sum(rain[max(0, i - 3):i + 1]))
    return days, rain, flow


class FloodModelTests(unittest.TestCase):
    def fake_fetch(self, url):
        if "archive-api" in url:
            return daily(self.days, "precipitation_sum", self.rain)
        if "flood-api" in url and "start_date" in url:
            return daily(self.days, "river_discharge", self.flow)
        if "flood-api" in url:
            days = [(NOW.date() + timedelta(days=k)).isoformat() for k in range(-3, 7)]
            return daily(days, "river_discharge", [1.2] * len(days))
        days = [(NOW.date() + timedelta(days=k)).isoformat() for k in range(-31, 7)]
        rain = [0.0] * 32 + [40.0, 30.0, 20.0, 0, 0, 0]
        return daily(days, "precipitation_sum", rain)

    def setUp(self):
        ml._MODELS.clear()
        self.days, self.rain, self.flow = synthetic_river(date(2016, 10, 1), 3650)
        patches = [mock.patch.object(ml, "fetch_json", self.fake_fetch),
                   mock.patch.object(ml, "utc_now", lambda: NOW)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_trains_validates_and_predicts_heavy_rain(self):
        result = ml.flood_ml_feed(42.87, 25.32)
        self.assertGreater(result["validation"]["auc"], 0.85)
        self.assertEqual(len(result["outlook"]), 4)
        first = result["outlook"][0]
        self.assertEqual(first["rain_next_3d_mm"], 90.0)
        self.assertGreater(first["probability"], 0.5)
        self.assertLess(result["outlook"][-1]["probability"], first["probability"])
        self.assertIn("feature_weights_standardized", result)
        self.assertFalse(result["validation"]["forecast_validated"])
        self.assertIn("brier_score", result["validation"])

    def test_model_is_cached_per_day(self):
        ml.flood_ml_feed(42.87, 25.32)
        with mock.patch.object(ml, "train_location", side_effect=AssertionError("retrained")):
            ml.flood_ml_feed(42.871, 25.321)

    def test_no_threshold_crossing_refuses_to_train(self):
        self.flow = [1.0] * len(self.days)
        with self.assertRaises(ml.FeedError):
            ml.flood_ml_feed(42.87, 25.32)


class TerrainTests(unittest.TestCase):
    def test_d8_flows_downhill_and_finds_pit(self):
        grid = [[9, 8, 9, 9],
                [8, 5, 6, 9],
                [9, 4, 1, 8],
                [9, 9, 8, 9]]
        accumulation, pits = ml.d8_accumulation(grid)
        self.assertEqual(pits, {(2, 2)})
        self.assertEqual(max(accumulation, key=accumulation.get), (2, 2))
        self.assertEqual(accumulation[(2, 2)], 16)

    def test_missing_cells_are_skipped(self):
        accumulation, _ = ml.d8_accumulation([[3, None], [2, 1]])
        self.assertNotIn((0, 1), accumulation)
        self.assertEqual(accumulation[(1, 1)], 3)

    def test_ponding_feed_reports_collectors(self):
        def fake(url):
            if "elevation" in url:
                return {"elevation": [abs(r - 4) + abs(c - 4) + 100 for r in range(9) for c in range(9)]}
            return daily(["2026-11-02", "2026-11-03", "2026-11-04"], "precipitation_sum", [10, 5, 0])
        with mock.patch.object(ml, "fetch_json", fake), mock.patch.object(ml, "utc_now", lambda: NOW):
            result = ml.ponding_feed(42.87, 25.32)
        top = result["collectors"][0]
        self.assertTrue(top["pit"])
        self.assertAlmostEqual(top["lat"], 42.87, places=3)
        self.assertEqual(result["rain_next_3d_mm"], 15.0)
        self.assertEqual(top["upstream_km2"], 81.0)

    def test_missing_rain_refuses_runoff_volume(self):
        with mock.patch.object(ml, "fetch_json", side_effect=[
            {"elevation": [100] * 81},
            daily(["2026-11-02", "2026-11-03", "2026-11-04"], "precipitation_sum", [10, None, 0]),
        ]), mock.patch.object(ml, "utc_now", lambda: NOW):
            with self.assertRaisesRegex(ml.FeedError, "rainfall forecast is incomplete"):
                ml.ponding_feed(42.87, 25.32)


class WeatherRiskTests(unittest.TestCase):
    def response(self, cape, rain, rh):
        hours = [(NOW + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00") for h in range(-2, 50)]
        days = [(NOW.date() - timedelta(days=k)).isoformat() for k in range(10, -1, -1)]
        return {"hourly": {"time": hours, "temperature_2m": [33] * len(hours), "relative_humidity_2m": [rh] * len(hours),
                           "wind_speed_10m": [30] * len(hours), "precipitation": [rain] * len(hours),
                           "cape": [cape] * len(hours), "soil_moisture_0_to_7cm": [0.08] * len(hours)},
                "daily": {"time": days, "precipitation_sum": [5] + [0] * 10}}

    def run_feed(self, *args):
        with mock.patch.object(ml, "fetch_json", lambda url: self.response(*args)), \
                mock.patch.object(ml, "utc_now", lambda: NOW):
            return ml.weather_risk_feed(42.87, 25.32)

    def test_hot_dry_windy_with_dry_lightning(self):
        result = self.run_feed(1500, 0.0, 20)
        self.assertEqual(result["lightning"]["level"], "high")
        self.assertEqual(len(result["lightning"]["dry_lightning_hours"]), 12)
        self.assertEqual(result["fire_weather"]["days_without_rain"], 9)
        self.assertEqual(result["fire_weather"]["level"], "very high")
        self.assertEqual(result["hours_checked"], 48)

    def test_rain_lowers_fire_weather_and_blocks_dry_lightning(self):
        result = self.run_feed(1500, 2.0, 20)
        self.assertEqual(result["lightning"]["dry_lightning_hours"], [])
        self.assertIn("raining", result["fire_weather"]["factors"])
        self.assertEqual(result["current_fire_weather"]["rain_mm"], 2)
        self.assertLess(result["current_fire_weather"]["points"], self.run_feed(1500, 0, 20)["current_fire_weather"]["points"])

    def test_short_hourly_series_is_an_explicit_error(self):
        data = self.response(500, 0, 50)
        data["hourly"]["cape"] = [500]
        with mock.patch.object(ml, "fetch_json", return_value=data):
            with self.assertRaisesRegex(ml.FeedError, "mismatched lengths"):
                ml.weather_risk_feed(42.87, 25.32)

    def test_missing_dry_spell_history_does_not_become_low_fire_risk(self):
        data = self.response(500, 0, 50)
        data["daily"]["precipitation_sum"][-2] = None
        with mock.patch.object(ml, "fetch_json", return_value=data), mock.patch.object(ml, "utc_now", lambda: NOW):
            with self.assertRaisesRegex(ml.FeedError, "Rain history is incomplete"):
                ml.weather_risk_feed(42.87, 25.32)


class HazardTests(unittest.TestCase):
    def setUp(self):
        self.cache_dir = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(hazards, "PLACES_CACHE", Path(self.cache_dir.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.cache_dir.cleanup)

    def test_places_parse_types_and_reject_incomplete_query(self):
        response = {"elements": [
            {"lat": 42.8, "lon": 25.3, "tags": {"emergency": "assembly_point"}},
            {"center": {"lat": 42.9, "lon": 25.3}, "tags": {"amenity": "hospital"}},
            {"lat": 42.85, "lon": 25.3, "tags": {"social_facility": "shelter"}},
        ]}
        with mock.patch.object(hazards, "fetch", return_value=json.dumps(response)) as fetch:
            result = hazards.places_feed(42.87, 25.32)
        self.assertEqual(fetch.call_args.kwargs["timeout"], 12)
        self.assertIn(b"assembly_point", fetch.call_args.kwargs["data"])
        self.assertEqual({p["kind"] for p in result["places"]}, {"assembly point", "hospital", "shelter"})
        self.assertEqual(result["observation_time"], "unknown")
        for invalid in ({"elements": [], "remark": "query timed out"}, {"elements": None},
                        {"elements": [{"lat": float("nan"), "lon": 25.3}]}):
            with mock.patch.object(hazards, "fetch", return_value=json.dumps(invalid)):
                with self.assertRaises(hazards.FeedError):
                    hazards.overpass_places(42.87, 25.32)

    def test_places_retry_busy_primary_server_before_mirrors(self):
        ok = json.dumps({"elements": [{"lat": 42.8, "lon": 25.3, "tags": {"emergency": "assembly_point"}}]})
        replies = [hazards.FeedError("Provider returned HTTP 502."), ok]
        with mock.patch.object(hazards, "fetch", side_effect=replies) as fetch, mock.patch.object(hazards.time, "sleep") as sleep:
            places = hazards.overpass_places(42.87, 25.32)
        self.assertEqual(len(places), 1)
        self.assertEqual([c.args[0] for c in fetch.call_args_list], [hazards.OVERPASS[0]] * 2)
        sleep.assert_any_call(2)
        with mock.patch.object(hazards, "fetch", side_effect=[hazards.FeedError("Provider connection failed or timed out."), ok]) as fetch:
            hazards.overpass_places(42.87, 25.32)
        self.assertEqual(fetch.call_args_list[1].args[0], hazards.OVERPASS[1])

    def test_places_fall_back_to_dated_last_good_copy_only_within_seven_days(self):
        mapped = [{"name": "Assembly", "kind": "assembly point", "lat": 42.8, "lon": 25.3, "distance_km": 2, "bearing": "N"}]
        with mock.patch.object(hazards, "overpass_places", return_value=mapped), mock.patch.object(hazards, "utc_now", lambda: NOW):
            hazards.places_feed(42.87, 25.32)
        down = mock.patch.object(hazards, "overpass_places", side_effect=hazards.FeedError("HTTP 502"))
        with down, mock.patch.object(hazards, "utc_now", lambda: NOW + timedelta(days=1)):
            cached = hazards.places_feed(42.871, 25.319)
        self.assertTrue(cached["cached_copy"])
        self.assertEqual(cached["retrieved_at"], NOW.isoformat())
        self.assertIn("Live OpenStreetMap lookup failed", cached["message"])
        with down, mock.patch.object(hazards, "utc_now", lambda: NOW + timedelta(days=8)):
            with self.assertRaisesRegex(hazards.FeedError, "502"):
                hazards.places_feed(42.87, 25.32)
        with down, self.assertRaisesRegex(hazards.FeedError, "502"):
            hazards.places_feed(50.0, 14.0)

    def test_places_are_never_claimed_to_be_verified_shelters(self):
        mapped = [{"name": "Assembly point", "kind": "assembly point", "lat": 42.8, "lon": 25.3,
                   "distance_km": 2, "bearing": "N"}]
        with mock.patch.object(hazards, "overpass_places", return_value=mapped):
            result = hazards.places_feed(42.87, 25.32)
        place = result["places"][0]
        self.assertFalse(place["verified_shelter"])
        self.assertEqual(place["access_status"], "unknown")
        self.assertEqual(place["route_safety"], "not assessed")
        self.assertIn("not authority-verified", result["message"])

    def test_assembly_candidates_not_lost_behind_hospitals(self):
        mapped = [{"name": "Hospital", "kind": "hospital", "distance_km": 0.1},
                  {"name": "Assembly", "kind": "assembly point", "distance_km": 5}]
        with mock.patch.object(hazards, "overpass_places", return_value=mapped):
            result = hazards.places_feed(42.87, 25.32)
        self.assertEqual(result["places"][0]["facility_role"], "assembly")
        self.assertFalse(result["places"][0]["verified_shelter"])

    def test_fire_track_heading(self):
        hotspots = [{"lat": 42.80, "lon": 25.30, "observed_at": "2026-11-02T01:00:00Z", "distance_km": 5},
                    {"lat": 42.805, "lon": 25.30, "observed_at": "2026-11-02T01:00:00Z", "distance_km": 5},
                    {"lat": 42.80, "lon": 25.32, "observed_at": "2026-11-02T11:00:00Z", "distance_km": 4}]
        track = hazards.fire_tracks(hotspots, wind_from_deg=270)[0]
        self.assertEqual(track["passes"], 2)
        self.assertIn(track["observed_heading"], ("E", "SE"))
        self.assertEqual(track["downwind"], "E")

    def test_glofas_levels(self):
        history = {"daily": {"river_discharge": [1.0] * 900 + [5.0] * 90 + [9.0] * 10}}
        days = [(NOW.date() + timedelta(days=k)).isoformat() for k in range(-7, 30)]
        forecast = {"latitude": 1, "longitude": 2, "daily": {"time": days, "river_discharge": [1] * len(days),
                    "river_discharge_median": [1] * 10 + [6] + [1] * (len(days) - 11),
                    "river_discharge_max": [1] * 10 + [12] + [1] * (len(days) - 11)}}
        with mock.patch.object(hazards, "fetch_json", side_effect=[forecast, history]), \
                mock.patch.object(hazards, "utc_now", lambda: NOW):
            result = hazards.glofas_feed(42.87, 25.32)
        self.assertEqual(result["peak"]["level"], "high")
        self.assertEqual(result["peak"]["worst_member_level"], "extreme")

    def test_cems_parsing(self):
        data = {"results": [{"code": "EMSR900", "centroid": "POINT (25.4 42.9)", "category": "Flood", "closed": False,
                             "activationTime": "2026-11-01T10:00:00", "name": "Floods"},
                            {"code": "BAD", "centroid": "nonsense"}]}
        with mock.patch.object(hazards, "fetch_json", return_value=data), \
                mock.patch.object(hazards, "utc_now", lambda: NOW):
            result = hazards.cems_feed(42.87, 25.32)
        self.assertEqual(result["nearby"][0]["code"], "EMSR900")
        self.assertTrue(result["nearby"][0]["active"])
        self.assertEqual(result["skipped_records"], 1)

    def test_recent_closed_cems_activation_is_not_active(self):
        data = {"results": [{"code": "EMSR900", "centroid": "POINT (25.4 42.9)", "category": "Flood",
                             "closed": True, "activationTime": "2026-11-01T10:00:00", "name": "Floods"}],
                "count": 400, "next": "provider-next-page"}
        with mock.patch.object(hazards, "fetch_json", return_value=data):
            result = hazards.cems_feed(42.87, 25.32)
        self.assertFalse(result["nearby"][0]["active"])
        self.assertTrue(result["has_more"])

    def test_evacuation_avoids_fire_side_and_ml_low_points(self):
        track = {"points": [[42.87, 25.40]], "downwind_deg": 90}
        low = [{"lat": 42.87, "lon": 25.32, "kind": "flood", "radius": 0.7}]
        places = lambda lat, lon: [{"name": "Hospital", "kind": "hospital", "lat": 42.87, "lon": 25.20,
                                    "distance_km": 9.8, "bearing": "W"}]
        with mock.patch.object(hazards, "elevations", lambda pts: [300.0] * len(pts)):
            plan = hazards.evacuation_plan(42.87, 25.32, [track], [], [], [], "normal", places, extra_hazards=low)
        self.assertEqual(plan["urgency"], "inside")
        self.assertIn("E", plan["avoid_directions"])
        self.assertNotIn("E", [o["direction"] for o in plan["recommended"]])
        self.assertIn(plan["recommended"][0]["direction"], ("W", "NW", "SW"))
        self.assertIn("openstreetmap.org/directions", plan["places"][0]["directions_url"])


if __name__ == "__main__":
    unittest.main()
