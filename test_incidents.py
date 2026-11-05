import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import incidents
from test_network import synthetic

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


def ago(hours):
    return (NOW - timedelta(hours=hours)).isoformat()


class IncidentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = mock.patch.object(incidents, "STORE", Path(self.tmp.name) / "incidents.json")
        self.store.start()
        self.env = mock.patch.dict(os.environ, {"OPERATOR_KEY": ""})
        self.env.start()
        self.net = synthetic()

    def tearDown(self):
        self.env.stop()
        self.store.stop()
        self.tmp.cleanup()

    def update(self, **body):
        return incidents.update(body, net=self.net, now=NOW)

    def situation(self):
        return incidents.situation(net=self.net, now=NOW)

    def test_only_confirmed_closures_change_the_impact(self):
        self.update(crossing="X002", status="reported", at=ago(2))
        s = self.situation()
        self.assertIsNone(s["impact"])
        self.assertEqual(s["if_reports_confirmed"]["population_disconnected"], 200)
        self.update(crossing="X002", status="confirmed", at=ago(1))
        s = self.situation()
        self.assertEqual(s["impact"]["modelled"]["population_disconnected"], 200)
        self.assertEqual(s["actions"][0]["priority"], "critical")

    def test_no_person_hours_without_traffic_figures(self):
        self.update(crossing="X001", status="confirmed", at=ago(10))
        cost = self.situation()["cost"][0]
        self.assertIsNone(cost["person_hours"])
        self.update(crossing="X001", status="confirmed", vehicles_per_day=2400, occupancy=1.5)
        cost = self.situation()["cost"][0]
        self.assertEqual(cost["hours_closed"], 10.0)  # adding traffic must not move the failure time
        self.assertAlmostEqual(cost["person_hours"], 2400 / 24 * 1.5 * 0.5 * 10, places=0)

    def test_isolation_person_hours_and_repair_priority(self):
        self.update(crossing="X002", status="confirmed", at=ago(3))
        self.update(crossing="X001", status="confirmed", at=ago(1))
        s = self.situation()
        self.assertEqual(s["isolation_person_hours"], 600)
        self.assertEqual(s["repair_priority"][0]["crossing"], "X002")

    def test_reopen_ends_the_incident(self):
        self.update(crossing="X002", status="confirmed", at=ago(4))
        self.update(crossing="X002", status="reopened", at=ago(1))
        s = self.situation()
        self.assertIsNone(s["impact"])
        self.assertEqual(s["counts"]["reopened"], 1)
        with self.assertRaises(ValueError):
            self.update(crossing="X002", status="reopened")

    def test_validation(self):
        for body in ({"crossing": "X999", "status": "reported"}, {"crossing": "X001", "status": "gone"},
                     {"crossing": "X001", "status": "reported", "at": (NOW + timedelta(hours=1)).isoformat()},
                     {"crossing": "X001", "status": "reported", "at": "2026-11-20T10:00"},
                     {"crossing": "X001", "status": "confirmed", "occupancy": 9}):
            with self.subTest(body=body), self.assertRaises(ValueError):
                incidents.update(body, net=self.net, now=NOW)

    def test_operator_key_required_to_confirm(self):
        with mock.patch.dict(os.environ, {"OPERATOR_KEY": "secret"}):
            self.update(crossing="X001", status="reported")
            with self.assertRaises(PermissionError):
                self.update(crossing="X001", status="confirmed")
            self.update(crossing="X001", status="confirmed", key="secret")
            with self.assertRaises(PermissionError):
                incidents.reset("wrong")
        self.assertEqual(self.situation()["counts"]["confirmed"], 1)


if __name__ == "__main__":
    unittest.main()
