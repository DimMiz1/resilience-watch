import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import incidents
import watcher
from test_network import synthetic

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


def signal(*ids, sid="EMSR999"):
    return {"id": sid, "crossings": list(ids), "source": "test", "note": "official test signal"}


class WatcherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        for patch in (mock.patch.object(incidents, "STORE", Path(self.tmp.name) / "i.json"),
                      mock.patch.dict(os.environ, {"OPERATOR_KEY": ""}),
                      mock.patch.dict(watcher._health, clear=True)):
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self.tmp.cleanup)
        self.net = synthetic()

    def statuses(self):
        return {i["crossing"]: i["status"] for i in incidents._read()}

    def test_wkt_and_area_match(self):
        rings = watcher.wkt_rings("MULTIPOLYGON (((0.2 -1, 0.8 -1, 0.8 1, 0.2 1, 0.2 -1)))")
        self.assertEqual(watcher.crossings_in(rings, self.net), ["X001"])

    def test_signals_only_flag_and_never_downgrade_or_duplicate(self):
        incidents.update({"crossing": "X002", "status": "confirmed"}, self.net, NOW)
        created = watcher.apply([signal("X001", "X002")], self.net, NOW)
        self.assertEqual(created, ["X001"])
        self.assertEqual(self.statuses(), {"X002": "confirmed", "X001": "suspected"})
        self.assertEqual(watcher.apply([signal("X001")], self.net, NOW), [])

    def test_reopened_crossing_not_reflagged_by_same_signal(self):
        watcher.apply([signal("X001")], self.net, NOW)
        incidents.update({"crossing": "X001", "status": "reopened"}, self.net, NOW)
        self.assertEqual(watcher.apply([signal("X001")], self.net, NOW), [])
        self.assertEqual(watcher.apply([signal("X001", sid="EMSR1000")], self.net, NOW), ["X001"])

    def test_failing_source_is_isolated_and_keeps_last_good(self):
        calls = {"n": 0}

        def flaky(net, now):
            calls["n"] += 1
            if calls["n"] > 1:
                raise TimeoutError("down")
            return [signal("X001")]

        sources = {"Copernicus EMS": flaky, "Meteoalarm": lambda net, now: [signal("X002", sid="warn")]}
        watcher.scan(self.net, NOW, sources)
        watcher.scan(self.net, NOW, sources)
        h = watcher.health()
        self.assertEqual(h["Copernicus EMS"]["status"], "outdated")
        self.assertEqual(h["Copernicus EMS"]["last_ok"], NOW.isoformat(timespec="seconds"))
        self.assertIn("TimeoutError", h["Copernicus EMS"]["error"])
        self.assertEqual(h["Meteoalarm"]["status"], "ok")
        self.assertEqual(self.statuses(), {"X001": "suspected", "X002": "suspected"})

    def test_meteoalarm_orange_rain_for_burgas(self):
        info = {"language": "en-GB", "onset": "2026-11-20T00:00:00+02:00", "expires": "2026-11-21T00:00:00+02:00",
                "parameter": [{"valueName": "awareness_level", "value": "3; orange; Severe"},
                              {"valueName": "awareness_type", "value": "10; Rain"}],
                "area": [{"areaDesc": "Burgas", "geocode": [{"value": "BG341"}]}]}
        green = {**info, "parameter": [{"valueName": "awareness_level", "value": "1; green; Minor"},
                                       {"valueName": "awareness_type", "value": "10; Rain"}]}
        feed = {"warnings": [{"alert": {"info": [info]}}, {"alert": {"info": [green]}}]}
        with mock.patch.object(watcher, "fetch_json", return_value=feed):
            out = watcher.meteoalarm_signals(self.net, NOW)
        self.assertEqual(len(out), 1)
        self.assertIn("orange rain", out[0]["note"])

    def test_replay_uses_stored_official_area(self):
        out = watcher.replay_signals()
        self.assertEqual(out[0]["id"], "EMSR693")
        self.assertTrue(out[0]["crossings"])


if __name__ == "__main__":
    unittest.main()
