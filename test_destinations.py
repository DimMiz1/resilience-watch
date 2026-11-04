import unittest
from unittest import mock

import hazards
import server
from feeds import iso_now
from local_geo import destination_point


def feed(data=None, status="available"):
    return {"source": "Test provider", "status": status, "data": data or {},
            "fetched_at": iso_now(), "error": None, "limitation": "Test coverage"}


def place(name, lon, role="assembly"):
    return {"name": name, "kind": "assembly point", "facility_role": role,
            "lat": 42, "lon": lon, "distance_km": 999}


class DestinationTests(unittest.TestCase):
    def inputs(self, places):
        return {s: feed({"places": places} if s == "places" else {})
                for s in server.DESTINATION_INPUTS}

    def test_closest_candidate_excluded_and_hospital_never_refuge(self):
        feeds = self.inputs([place("Close", 25.01), place("Far", 25.09),
                             place("Hospital", 25.001, "medical")])
        feeds["firms"] = feed({"events": [{"lat": 42, "lon": 25.01, "type": "hotspot"}]})
        result = hazards.destination_candidates(42, 25, feeds, 10)
        self.assertEqual([p["name"] for p in result["candidates"]], ["Far"])
        self.assertEqual([p["name"] for p in result["excluded"]], ["Close"])
        self.assertTrue(result["origin_in_known_buffer"])
        self.assertEqual(result["candidates"][0]["hazard_conflicts"], [])
        self.assertFalse(result["candidates"][0]["verified_shelter"])
        self.assertEqual(result["candidates"][0]["route_safety"], "not assessed")
        self.assertIsNone(result["recommended_destination"])

    def test_sort_recomputes_distance_and_keeps_unknown_safety(self):
        result = hazards.destination_candidates(42, 25, self.inputs([
            place("Far", 25.1), place("Close", 25.01), place("Outside", 27)]), 10)
        self.assertEqual([p["name"] for p in result["candidates"]], ["Close", "Far"])
        self.assertEqual(result["status"], "candidates_screened")
        self.assertIsNone(result["origin_in_known_buffer"])
        self.assertTrue(all(p["safety_status"] == "unknown" for p in result["candidates"]))

    def test_stale_evidence_still_excludes_and_marks_partial(self):
        feeds = self.inputs([place("Close", 25.01)])
        feeds["gdacs"] = feed({"events": [{"lat": 42, "lon": 25.01, "type": "flood"}]}, "stale")
        result = hazards.destination_candidates(42, 25, feeds, 10)
        self.assertEqual(result["status"], "assessment_incomplete")
        self.assertIn("gdacs", result["incomplete_inputs"])
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["excluded"][0]["hazard_conflicts"], ["flood"])

    def test_missing_feeds_unknown_location_and_partial_records(self):
        feeds = self.inputs([])
        feeds["firms"] = feed(status="error")
        feeds["cems"] = feed({"has_more": True})
        feeds["effis"] = feed({"skipped_records": 2})
        result = hazards.destination_candidates(42, 25, feeds)
        self.assertEqual(result["status"], "assessment_incomplete")
        self.assertTrue(result["location_uncertain"])
        self.assertEqual(set(result["incomplete_inputs"]), {"firms", "cems", "effis"})
        self.assertEqual(result["safety_status"], "unknown")

    def test_unknown_or_old_retrieval_time_is_incomplete(self):
        feeds = self.inputs([])
        feeds["effis"]["fetched_at"] = "2000-01-01T00:00:00Z"
        feeds["cems"]["fetched_at"] = None
        result = hazards.destination_candidates(42, 25, feeds, 10)
        self.assertEqual(set(result["incomplete_inputs"]), {"effis", "cems"})
        self.assertTrue(hazards.destination_candidates(42, 25, feeds, 500)["location_uncertain"])

    def test_empty_candidates_are_not_all_clear(self):
        result = hazards.destination_candidates(42, 25, self.inputs([]), 10)
        self.assertEqual(result["status"], "no_eligible_candidates")
        self.assertEqual(result["safety_status"], "unknown")
        self.assertIsNone(result["recommended_destination"])

    def test_buffer_and_accuracy_thresholds(self):
        origin = (42, 25)
        locations = [destination_point(*origin, 90, km) for km in (2.999, 3.001)]
        places = [{**place(name, point[1]), "lat": point[0]}
                  for name, point in zip(("Inside", "Outside"), locations)]
        feeds = self.inputs(places)
        feeds["firms"] = feed({"events": [{"lat": 42, "lon": 25, "type": "hotspot"}]})
        result = hazards.destination_candidates(42, 25, feeds, 100)
        self.assertEqual([p["name"] for p in result["excluded"]], ["Inside"])
        self.assertEqual([p["name"] for p in result["candidates"]], ["Outside"])
        self.assertFalse(result["location_uncertain"])
        self.assertTrue(hazards.destination_candidates(42, 25, feeds, 100.01)["location_uncertain"])

    def test_fast_path_never_loads_slow_training_or_routing(self):
        with mock.patch.object(server.CACHE, "get", side_effect=lambda s, *_: feed()) as get:
            server.destinations(42, 25, 10)
        self.assertEqual({c.args[0] for c in get.call_args_list}, set(server.DESTINATION_INPUTS))
        self.assertNotIn("flood_ml", server.DESTINATION_INPUTS)


if __name__ == "__main__":
    unittest.main()
