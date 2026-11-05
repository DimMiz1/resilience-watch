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

    def test_fusion_rules(self):
        ev = lambda kind, src: {"kind": kind, "source": src}
        self.assertEqual(watcher.fuse([ev("closure", "Road agency")])[0], "confirmed")
        self.assertEqual(watcher.fuse([ev("inundation", "GFM"), ev("exposure", "GloFAS")])[0], "confirmed")
        self.assertEqual(watcher.fuse([ev("inundation", "GFM")])[0], "reported")
        self.assertEqual(watcher.fuse([ev("restriction", "Road agency")])[0], "reported")
        self.assertEqual(watcher.fuse([ev("exposure", "GloFAS"), ev("warning", "Meteoalarm"), ev("aoi", "CEMS")])[0], "suspected")

    def test_evidence_accumulates_and_auto_confirms(self):
        flood = {**signal("X001", sid="gfm-1"), "source": "GFM", "kind": "inundation", "observed_at": NOW.isoformat()}
        river = {**signal("X001", sid="glofas-1"), "source": "GloFAS", "kind": "exposure", "observed_at": NOW.isoformat()}
        self.assertEqual(watcher.apply([flood], self.net, NOW), ["X001"])
        self.assertEqual(self.statuses(), {"X001": "reported"})
        self.assertEqual(watcher.apply([river], self.net, NOW), ["X001"])
        item = incidents._read()[0]
        self.assertEqual((item["status"], item["history"][-1]["by"], len(item["evidence"])), ("confirmed", "automatic rule", 2))
        self.assertIsNotNone(incidents.situation(self.net, NOW)["impact"])

    def test_datex_closure_matches_crossing_on_named_road(self):
        xml = ('<?xml version="1.0" encoding="UTF-16"?><d2LogicalModel xmlns="http://datex2.eu/schema/2/2_0">'
               '<situationRecord id="abc"><situationRecordCreationTime>2026-11-20T10:00:00+0200</situationRecordCreationTime>'
               '<overallStartTime>2026-11-20T09:00:00+0200</overallStartTime>'
               '<value lang="bg">Път I-1 затворен поради наводнение</value><pointCoordinates><latitude>0.002</latitude>'
               '<longitude>0.5</longitude></pointCoordinates></situationRecord></d2LogicalModel>')
        out = watcher.datex_records(xml, "closure", self.net, NOW, "test")
        self.assertEqual(out[0]["crossings"], ["X001"])  # 222 m away, road 1 named in the record
        planned = watcher.datex_records(xml.replace(" поради наводнение", ""), "closure", self.net, NOW, "test")
        self.assertEqual(planned[0]["kind"], "planned")  # no hazard wording: planned works, context only
        watcher.apply(out, self.net, NOW)
        item = incidents._read()[0]
        self.assertEqual((item["status"], item["failed_at"]), ("confirmed", "2026-11-20T07:00:00+00:00"))

    def test_flood_extent_near_approach(self):
        square = {"type": "Polygon", "coordinates": [[[1.502, -0.001], [1.51, -0.001], [1.51, 0.001], [1.502, 0.001], [1.502, -0.001]]]}
        out = watcher.flood_records({"features": [{"geometry": square}]}, self.net, "GFM", "test", NOW.isoformat())
        self.assertEqual(out[0]["crossings"], ["X002"])  # ~220 m from the polygon edge

    def test_unconfigured_source_is_not_an_error(self):
        with mock.patch.dict(os.environ, {"FLOOD_EXTENT_URL": ""}):
            watcher.scan(self.net, NOW, {"Flood extent (GFM / CEMS)": watcher.flood_extent_signals})
        self.assertEqual(watcher.health()["Flood extent (GFM / CEMS)"]["status"], "not configured")

    def test_full_replay_uses_recorded_road_agency_data(self):
        incidents.load_demo(full=True, now=NOW)
        items = {i["name"]: i for i in incidents._read() if i["status"] != "suspected"}
        kiten = items["Road 99 bridge near Kiten"]
        self.assertEqual(kiten["status"], "reported")
        self.assertEqual(kiten["evidence"][0]["source"], "Road agency (DATEX II)")


if __name__ == "__main__":
    unittest.main()
