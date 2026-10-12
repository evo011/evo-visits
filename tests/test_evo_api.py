"""Tests fuer die echte EVO-API und die Min-Interval-Sperre in collect_once."""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from evo import evo_api, fetch
from evo.config import Config
from evo.fetch import FetchResult, ProbeLog
from evo.store import Reading, Store

FIXTURES = Path(__file__).parent / "fixtures"


def make_cfg(**kw) -> Config:
    kw.setdefault("url", "https://visits.evofitness.de/")
    kw.setdefault("extra", {})["request_delay"] = 0
    return Config(**kw)


class ParseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.locations = json.loads((FIXTURES / "evo_locations.json").read_text())
        cls.unterbilk = json.loads((FIXTURES / "evo_current_unterbilk.json").read_text())

    def test_parse_locations_fixtures(self):
        locs = evo_api.parse_locations(self.locations)
        self.assertEqual(len(locs), 3)
        self.assertEqual(locs[0], ("13cbafa0-8b29-4aa8-bed7-34e972caa165", "EVO Alexanderplatz"))
        self.assertEqual(locs[2][1], "EVO Unterbilk")

    def test_parse_locations_skips_invalid(self):
        self.assertEqual(evo_api.parse_locations(None), [])
        self.assertEqual(evo_api.parse_locations([{"id": 1, "name": "x"}, {"id": "", "name": "y"},
                                                   {"id": "a", "name": ""}, "murks", []]), [])

    def test_parse_current_unterbilk(self):
        r = evo_api.parse_current(self.unterbilk)
        self.assertEqual((r.gym, r.count, r.capacity, r.pct), ("EVO Unterbilk", 36.0, 35.0, 102.9))

    def test_parse_current_invalid(self):
        self.assertIsNone(evo_api.parse_current(None))
        self.assertIsNone(evo_api.parse_current({"name": "", "current": 3}))


class OperatorIdTest(unittest.TestCase):
    def test_known_host_returns_constant(self):
        self.assertEqual(evo_api.operator_id(make_cfg()),
                         "c04bc7c3-903f-4804-a952-9abc0016b824")

    def test_extra_override_wins(self):
        cfg = make_cfg(extra={"request_delay": 0, "operator_id": "abc"})
        self.assertEqual(evo_api.operator_id(cfg), "abc")

    def test_unknown_host_read_from_config_js(self):
        cfg = make_cfg(url="https://visits.example.org/")
        config_js = "var x = {'visits.example.org': { operatorId: '12345678-90ab-cdef-1234-567890abcdef', foo: 1 }};"
        with mock.patch.object(evo_api, "http_get", return_value=("application/javascript", config_js)):
            self.assertEqual(evo_api.operator_id(cfg), "12345678-90ab-cdef-1234-567890abcdef")

    def test_unknown_host_without_match_raises(self):
        cfg = make_cfg(url="https://visits.example.org/")
        with mock.patch.object(evo_api, "http_get", return_value=("application/javascript", "var x = {};")):
            with self.assertRaises(fetch.FetchError):
                evo_api.operator_id(cfg)


class FetchEvoApiTest(unittest.TestCase):
    def setUp(self):
        self.locations_text = (FIXTURES / "evo_locations.json").read_text()
        self.locations = json.loads(self.locations_text)
        self.unterbilk = json.loads((FIXTURES / "evo_current_unterbilk.json").read_text())
        self.currents = {
            "13cbafa0-8b29-4aa8-bed7-34e972caa165": {"name": "EVO Alexanderplatz", "max_capacity": 50,
                                                      "current": 12, "percentageUsed": 24.0},
            "365ee508-dba5-4a0e-aa1b-86a562041ef2": None,  # wirft Fehler
            "ef41f8ff-4cdd-479f-963c-5f30e998841d": self.unterbilk,
        }

    def test_fetch_two_of_three_clubs(self):
        def fake_get(url, cfg):
            if "/locations?operator=" in url:
                return "application/json", self.locations_text
            loc_id = url.rsplit("/", 2)[-2]
            data = self.currents[loc_id]
            if data is None:
                raise fetch.FetchError("kaputt")
            return "application/json", json.dumps(data)

        with mock.patch.object(evo_api, "http_get", side_effect=fake_get):
            log = ProbeLog()
            res = evo_api.fetch_evo_api(make_cfg(), log)
        self.assertEqual(res.source, "evo-api:v1")
        got = {r.gym: (r.count, r.capacity, r.pct) for r in res.readings}
        self.assertEqual(got, {"EVO Alexanderplatz": (12.0, 50.0, 24.0),
                               "EVO Unterbilk": (36.0, 35.0, 102.9)})
        self.assertIn("evo_locations.json", log.files)
        self.assertTrue(any(k.startswith("evo_current_") for k in log.files))

    def test_too_many_failures_raise(self):
        def fake_get(url, cfg):
            if "/locations?operator=" in url:
                return "application/json", self.locations_text
            raise fetch.FetchError("alles kaputt")

        with mock.patch.object(evo_api, "http_get", side_effect=fake_get):
            with self.assertRaises(fetch.FetchError):
                evo_api.fetch_evo_api(make_cfg())


class CollectOnceLockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "t.db"))
        self.cfg = make_cfg(db_path=os.path.join(self.tmp.name, "t.db"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_skips_within_min_interval(self):
        self.store.add_readings(int(time.time()) - 60, [Reading(gym="EVO Unterbilk", count=5)])
        with mock.patch.object(fetch, "fetch_readings") as fr:
            res = fetch.collect_once(self.cfg, self.store)
            fr.assert_not_called()
        self.assertEqual(res.source, "skipped:min-interval")
        self.assertEqual(res.readings, [])
        self.assertEqual(self.store.last_runs(), [])

    def test_force_overrides_lock(self):
        self.store.add_readings(int(time.time()) - 60, [Reading(gym="EVO Unterbilk", count=5)])
        res = FetchResult([Reading(gym="EVO Unterbilk", count=7)], "evo-api:v1")
        with mock.patch.object(fetch, "fetch_readings", return_value=res):
            out = fetch.collect_once(self.cfg, self.store, force=True)
        self.assertEqual(out.source, "evo-api:v1")
        self.assertEqual(len(self.store.last_runs()), 1)

    def test_collects_after_min_interval(self):
        self.store.add_readings(int(time.time()) - 700, [Reading(gym="EVO Unterbilk", count=5)])
        res = FetchResult([Reading(gym="EVO Unterbilk", count=7)], "evo-api:v1")
        with mock.patch.object(fetch, "fetch_readings", return_value=res):
            out = fetch.collect_once(self.cfg, self.store)
        self.assertEqual(out.source, "evo-api:v1")

    def test_empty_store_collects(self):
        res = FetchResult([Reading(gym="EVO Unterbilk", count=7)], "evo-api:v1")
        with mock.patch.object(fetch, "fetch_readings", return_value=res):
            out = fetch.collect_once(self.cfg, self.store)
        self.assertEqual(out.source, "evo-api:v1")


if __name__ == "__main__":
    unittest.main()
