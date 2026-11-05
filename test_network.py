import unittest

import network


def synthetic():
    # Village V reaches hospital H over bridge X001 (5 min) or a 30-min detour; island I only via X002.
    nodes = [[0, 0], [0, 1], [1, 0], [0, 2]]
    edges = [
        {"a": 0, "b": 1, "min": 5.0, "oneway": False, "bridge": True},
        {"a": 0, "b": 2, "min": 15.0, "oneway": False, "bridge": False},
        {"a": 2, "b": 1, "min": 20.0, "oneway": False, "bridge": False},
        {"a": 1, "b": 3, "min": 4.0, "oneway": False, "bridge": True},
    ]
    return network.Network({
        "region": "Test", "source": "synthetic", "built_at": "now", "speeds_kmh": {"secondary": 60},
        "nodes": nodes, "edges": edges,
        "places": [{"name": "V", "population": 1000, "lat": 0, "lon": 0, "node": 0},
                   {"name": "I", "population": 200, "lat": 0, "lon": 2, "node": 3},
                   {"name": "U", "population": None, "lat": 1, "lon": 0, "node": 2}],
        "hospitals": [{"name": network.REGIONAL_HOSPITAL, "node": 1}],
        "crossings": [{"id": "X001", "name": "A", "road": "1", "lat": 0, "lon": 0.5, "length_m": 50, "edges": [0]},
                      {"id": "X002", "name": "B", "road": "1", "lat": 0, "lon": 1.5, "length_m": 50, "edges": [3]}],
    })


class NetworkTest(unittest.TestCase):
    def setUp(self):
        self.net = synthetic()

    def test_detour_and_threshold_access_loss(self):
        r = network.scenario(["X001"], threshold=30, net=self.net)
        v = next(s for s in r["settlements"] if s["name"] == "V")
        self.assertEqual((v["hospital_before"], v["hospital_after"], v["hospital_delta"]), (5.0, 35.0, 30.0))
        self.assertEqual(r["modelled"]["population_losing_access"], 1000)
        self.assertEqual(r["closed"][0]["detour_min"], 30.0)
        self.assertEqual(r["modelled"]["disconnected_settlements"], [])

    def test_disconnection_reported_separately_not_as_infinite_hours(self):
        r = network.scenario(["X002"], journeys=1000, occupancy=1.3, net=self.net)
        self.assertEqual(r["modelled"]["disconnected_settlements"], ["I"])
        self.assertEqual(r["modelled"]["population_disconnected"], 200)
        self.assertIsNone(r["closed"][0]["detour_min"])
        self.assertIsNone(r["modelled"]["person_hours_lost"])

    def test_person_hours_only_from_user_inputs(self):
        self.assertIsNone(network.scenario(["X001"], net=self.net)["modelled"]["person_hours_lost"])
        r = network.scenario(["X001"], hours=48, journeys=2000, occupancy=1.5, value_of_time=10, net=self.net)
        self.assertEqual(r["modelled"]["person_hours_lost"], 2000 * 2 * 1.5 * 30 / 60)
        self.assertEqual(r["modelled"]["value_of_time_eur"], 30000)
        self.assertIn("closed for 48 hours", r["wording"])

    def test_bridges_ranked_by_consequence(self):
        ranked = network.bridges(self.net)["crossings"]
        self.assertEqual([c["id"] for c in ranked], ["X002", "X001"])
        self.assertEqual(ranked[0]["population_disconnected"], 200)

    def test_rejects_unknown_bridge_and_bad_inputs(self):
        for kwargs in ({"ids": ["NOPE"]}, {"ids": []}, {"ids": ["X001"], "hours": 0}, {"ids": ["X001"], "occupancy": 9}):
            with self.assertRaises(ValueError):
                network.scenario(net=self.net, **kwargs)

    def test_bundled_extract_is_connected_and_labelled(self):
        net = network.load()
        self.assertEqual(len(net.regional), 1)
        listing = network.bridges(net)
        self.assertGreater(len(listing["crossings"]), 20)
        self.assertIn("not known", listing["note"])
        r = network.scenario([listing["crossings"][0]["id"]], net=net)
        self.assertTrue({"observed", "modelled", "assumed"} <= r.keys())
        self.assertTrue(all(h["snap_km"] < 1 for h in net.data["hospitals"]))


if __name__ == "__main__":
    unittest.main()
