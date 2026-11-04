"""Offline provider-contract and supervised training tests; no real fire labels invented."""
import csv
from datetime import date, datetime, timedelta, timezone
import json
import math
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock

import copernicus
import fire_ml
from feeds import FeedError
from local_ml import predict_proba

NOW = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)


def interval(start, coverage=0.8, water=0.1, nbr=0.5):
    end = date.fromisoformat(start) + timedelta(days=5)
    return {"interval": {"from": start + "T00:00:00Z", "to": end.isoformat() + "T00:00:00Z"},
            "outputs": {"indices": {"bands": {
                f"B{k}": {"stats": {"mean": x, "sampleCount": 100, "noDataCount": 100 * (1 - coverage)}}
                for k, x in enumerate((0.1, nbr, water))
            }}}}


class CopernicusTests(unittest.TestCase):
    def setUp(self):
        copernicus._TOKEN = None
        self.addCleanup(setattr, copernicus, "_TOKEN", None)

    def test_missing_credentials_is_unconfigured_without_network(self):
        with mock.patch.dict("os.environ", {}, clear=True), mock.patch.object(copernicus, "post_json") as request:
            result = copernicus.sentinel_indices_feed(42.87, 25.32)
        self.assertTrue(result["configuration_required"])
        request.assert_not_called()

    def test_token_is_cached_and_not_in_payload(self):
        reply = {"data": [interval("2026-10-04"), interval("2026-10-24", water=0.3, nbr=0.2)]}
        with mock.patch.dict("os.environ", {"CDSE_CLIENT_ID": "test-id", "CDSE_CLIENT_SECRET": "test-secret"}), \
                mock.patch.object(copernicus, "post_json", side_effect=[
                    {"access_token": "private-token", "expires_in": 300}, reply, reply,
                ]) as request, mock.patch.object(copernicus, "utc_now", lambda: NOW):
            result = copernicus.sentinel_indices_feed(42.87, 25.32)
            copernicus.sentinel_indices_feed(42.87, 25.32)
        self.assertEqual(request.call_count, 3)
        self.assertNotIn("private-token", json.dumps(result))
        self.assertNotIn("test-secret", json.dumps(result))
        self.assertEqual(result["comparison"]["water_like_change_percentage_points"], 20)
        self.assertEqual(result["comparison"]["nbr_mean_drop"], 0.3)

    def test_low_coverage_does_not_make_a_change_claim(self):
        with mock.patch.dict("os.environ", {"CDSE_CLIENT_ID": "x", "CDSE_CLIENT_SECRET": "y"}), \
                mock.patch.object(copernicus, "access_token", return_value="token"), \
                mock.patch.object(copernicus, "post_json", return_value={
                    "data": [interval("2026-10-04", coverage=0.2), interval("2026-10-24")]}):
            self.assertIsNone(copernicus.sentinel_indices_feed(42.87, 25.32)["comparison"])

    def test_all_masked_pixels_are_unknown_not_no_water(self):
        with mock.patch.dict("os.environ", {"CDSE_CLIENT_ID": "x", "CDSE_CLIENT_SECRET": "y"}), \
                mock.patch.object(copernicus, "access_token", return_value="token"), \
                mock.patch.object(copernicus, "post_json", return_value={"data": [interval("2026-10-04", coverage=0)]}):
            with self.assertRaisesRegex(FeedError, "No valid"):
                copernicus.sentinel_indices_feed(42.87, 25.32)

    def test_invalid_statistics_fail_explicitly(self):
        for data in ({"data": [None]}, {"data": [{"error": "quota"}]}, {"data": [{}]}, {"data": [interval("2026-10-04", water=math.nan)]}):
            with self.subTest(data=data), self.assertRaises(FeedError):
                copernicus.parse_statistics(data)

    def test_request_uses_correct_bands_mask_and_bounded_area(self):
        with mock.patch.object(copernicus, "utc_now", lambda: NOW):
            body = copernicus.statistics_request(42.87, 25.32)
        self.assertEqual(body["input"]["data"][0]["type"], "sentinel-2-l2a")
        self.assertIn("SCL", body["aggregation"]["evalscript"])
        self.assertIn("dataMask", body["aggregation"]["evalscript"])
        self.assertEqual(body["aggregation"]["aggregationInterval"]["of"], "P5D")
        self.assertLess(body["aggregation"]["resx"], 0.001)


class FireTrainingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.dataset = self.folder / "labels.csv"
        self.output = self.folder / "model.json"

    def dataset_rows(self):
        rows = []
        for i in range(250):
            hot = i % 5 == 0
            rows.append({"date": (date(2025, 1, 1) + timedelta(days=i)).isoformat(),
                         "latitude": 42.87, "longitude": 25.32, "fire_observed": int(hot),
                         "temperature_c": 35 if hot else 12, "humidity_pct": 20 if hot else 80,
                         "wind_kmh": 30 if hot else 5, "dry_days": 12 if hot else 1,
                         "soil_moisture": 0.1 if hot else 0.3})
        return rows

    def write(self, rows):
        with self.dataset.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    def test_synthetic_training_records_chronological_provenance(self):
        self.write(self.dataset_rows())
        model = fire_ml.train_file(self.dataset, self.output)
        self.assertGreater(model["validation"]["auc"], 0.95)
        self.assertLess(model["train_dates"][-1], model["test_dates"][0])
        self.assertFalse(model["validation"]["forecast_validated"])
        self.assertEqual(fire_ml.load_model(self.output)["schema"], fire_ml.SCHEMA)
        with self.assertRaisesRegex(ValueError, "already exists"):
            fire_ml.train_file(self.dataset, self.output)

    def test_missing_model_has_no_invented_scores(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertTrue(fire_ml.fire_ml_feed(42.87, 25.32)["configuration_required"])

    def test_mixed_locations_and_duplicate_days_rejected(self):
        rows = self.dataset_rows()
        rows[-1]["latitude"] = 43
        self.write(rows)
        with self.assertRaisesRegex(ValueError, "single fixed"):
            fire_ml.train_file(self.dataset, self.output)
        rows = self.dataset_rows()
        rows[-1]["date"] = rows[0]["date"]
        self.write(rows)
        with self.assertRaisesRegex(ValueError, "Duplicate dates"):
            fire_ml.train_file(self.dataset, self.output)

    def test_unseen_location_refuses_prediction(self):
        self.write(self.dataset_rows())
        fire_ml.train_file(self.dataset, self.output)
        with mock.patch.dict("os.environ", {"FIRE_MODEL_PATH": str(self.output)}), \
                mock.patch.object(fire_ml, "fetch_json") as fetch:
            with self.assertRaisesRegex(FeedError, "another location"):
                fire_ml.fire_ml_feed(43, 25)
            fetch.assert_not_called()

    def test_complete_forecast_generates_scores_only_for_configured_site(self):
        self.write(self.dataset_rows())
        fire_ml.train_file(self.dataset, self.output)
        times = [(NOW.replace(hour=0) + timedelta(hours=k)).strftime("%Y-%m-%dT%H:00") for k in range(72)]
        days = [(NOW.date() + timedelta(days=k)).isoformat() for k in range(-30, 3)]
        data = {"hourly": {"time": times, "temperature_2m": [35] * 72, "relative_humidity_2m": [20] * 72,
                           "wind_speed_10m": [30] * 72, "soil_moisture_0_to_7cm": [0.1] * 72},
                "daily": {"time": days, "precipitation_sum": [0] * len(days)}}
        with mock.patch.dict("os.environ", {"FIRE_MODEL_PATH": str(self.output)}), \
                mock.patch.object(fire_ml, "utc_now", lambda: NOW), \
                mock.patch.object(fire_ml, "fetch_json", return_value=data):
            result = fire_ml.fire_ml_feed(42.87, 25.32)
        self.assertEqual(len(result["outlook"]), 3)
        self.assertTrue(all(0 <= r["score"] <= 1 for r in result["outlook"]))

    def test_predict_proba_undoes_positive_class_weight(self):
        model = {"weights": [0], "mean": [0], "std": [1], "bias": 0, "positive_class_weight": 9}
        self.assertAlmostEqual(predict_proba(model, [0]), 0.1)
        with self.assertRaises(ValueError):
            predict_proba(model, [])


class DashboardWiringTests(unittest.TestCase):
    def test_all_referenced_dom_elements_exist(self):
        static = Path(__file__).with_name("static")
        script = (static / "app.js").read_text(encoding="utf-8")
        html = (static / "index.html").read_text(encoding="utf-8")
        referenced = set(re.findall(r'\$\("([^"]+)"\)', script))
        referenced.update(f"show-{name}" for name in ("fire", "burns", "flood", "scenes", "water", "rain", "drainage", "river", "places", "fwi"))
        referenced.update(f"mode-{name}" for name in ("rain", "fire", "places"))
        present = set(re.findall(r'id="([^"]+)"', html))
        self.assertEqual(referenced - present, set())
        self.assertEqual(len(re.findall(r'id="([^"]+)"', html)), len(present))


if __name__ == "__main__":
    unittest.main()
