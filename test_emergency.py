"""Offline administrative lookup and curated contact regression checks."""
import unittest
from unittest import mock

import emergency
from feeds import FeedError


def response(code="BG", **props):
    return {"features": [{"properties": {"countrycode": code, **props},
                           "geometry": {"type": "Point", "coordinates": [23.322, 42.6977]}}]}


class ContactTests(unittest.TestCase):
    def test_all_eu_countries_use_verified_common_number(self):
        self.assertEqual(len(emergency.EU_COUNTRIES), 27)
        for code in emergency.EU_COUNTRIES:
            data = emergency.contact_context(code, {})
            self.assertEqual(data["primary_number"], "112")
            self.assertEqual(data["number_source"], emergency.EU_NUMBER_SOURCE)
            self.assertTrue(data["national_authorities"])

    def test_other_country_numbers_and_no_universal_default(self):
        for code, number in (("US", "911"), ("GB", "999"), ("XX", None)):
            data = emergency.contact_context(code, {"country": "Unknown country"})
            self.assertEqual(data["primary_number"], number)
        self.assertFalse(emergency.contact_context("XX", {})["national_authorities"])

    def test_bulgarian_guidance_only_in_bulgaria(self):
        bg = emergency.normalize_context(response(city="Sofia", state="Sofia-City"), 42.6977, 23.3219)
        self.assertIn("BG-ALERT", str(bg["national_authorities"]))
        self.assertIn("sofia.bg", bg["regional_authorities"][0]["url"])
        self.assertEqual(len(bg["regional_authorities"]), 1)
        region_only = emergency.contact_context("BG", {"state": "Sofia-City"})
        self.assertIn("sofia.bg", region_only["regional_authorities"][0]["url"])
        for code in emergency.EU_COUNTRIES:
            if code != "BG":
                self.assertNotIn("BG-ALERT", str(emergency.contact_context(code, {})))

    def test_regions_match_country_and_admin_level(self):
        cases = [
            ("IT", {"state": "Lombardy"}, "lombardia.it"),
            ("ES", {"state": "Comunitat Valenciana"}, "gva.es"),
            ("CZ", {"city": "Prague"}, "hzscr.gov.cz"),
        ]
        for code, props, domain in cases:
            result = emergency.contact_context(code, props)
            self.assertIn(domain, result["regional_authorities"][0]["url"])
        for code, props in (("FR", {"city": "Sofia"}), ("BG", {"state": "Sofia"}),
                            ("ES", {"city": "Valencia", "state": "Another region"})):
            result = emergency.contact_context(code, props)
            self.assertFalse(result["regional_authorities"])
            self.assertIn("not configured", result["regional_status"])

    def test_additional_service_numbers_have_sources(self):
        for code in emergency.EXTRA_NUMBERS:
            for record in emergency.contact_context(code, {})["additional_numbers"]:
                self.assertTrue(record["number"].isdigit())
                self.assertTrue(record["source_url"].startswith("https://"))

    def test_unknown_and_distant_locations_never_choose_a_number(self):
        invalid = [{"features": []}, {"features": [None]}, response(code=""), response(state=3)]
        distant = response()
        distant["features"][0]["geometry"]["coordinates"] = [9, 45]
        invalid.append(distant)
        for value in invalid:
            with self.assertRaises(FeedError):
                emergency.normalize_context(value, 42.6977, 23.3219)

    def test_reverse_result_cached_and_provider_errors_propagate(self):
        with mock.patch.object(emergency, "_cache", {}), mock.patch.object(emergency, "_last_request", 0), \
                mock.patch.object(emergency, "fetch_json", return_value=response(city="Sofia")) as fetch:
            first = emergency.emergency_feed(42.6977, 23.3219)
            self.assertEqual(first, emergency.emergency_feed(42.6977, 23.3219))
            self.assertEqual(fetch.call_count, 1)
        with mock.patch.object(emergency, "_cache", {}), mock.patch.object(emergency, "_last_request", 0), \
                mock.patch.object(emergency, "fetch_json", side_effect=FeedError("Unavailable")):
            with self.assertRaises(FeedError):
                emergency.emergency_feed(42.6977, 23.3219)


if __name__ == "__main__":
    unittest.main()
