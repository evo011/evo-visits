import random
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from evo.config import Config
from evo.context import WeatherHour
from evo.fetch import FetchResult
from evo.forecast import get_tz
from evo.server import App, Collector
from evo.store import Reading, Store

TZ = get_tz("Europe/Berlin")
NOW = datetime(2026, 10, 12, 12, 0, tzinfo=TZ)      # Montag
NOW_TS = int(NOW.timestamp())
TODAY = NOW.date()
HOLIDAY_ISO = "2026-10-03"                           # Tag der Deutschen Einheit (Samstag)


class FakeContext:
    """Duck-typed Context: Wetter aus Dict, Statuszeilen statisch."""

    def __init__(self, weather_map=None, school_days=(), status_rows=()):
        self.weather_map = weather_map or {}
        self.school_days = set(school_days)
        self.status_rows = list(status_rows)
        self.refresh_calls = 0

    def weather(self, loc_key, start_ts, end_ts):
        return {ts: w for ts, w in self.weather_map.items() if start_ts <= ts <= end_ts}

    def school_holiday(self, state, d):
        return "Herbstferien" if d in self.school_days else None

    def status(self):
        return self.status_rows

    def refresh_for_gyms(self, gyms):
        self.refresh_calls += 1


class FixedNowApp(App):
    """App mit festem Zeitpunkt, damit die Tests deterministisch sind."""

    def now(self):
        return NOW_TS


def synth_weather(days_back=25, days_fwd=8):
    """Stundenwerte (UTC-Stundenbeginn) rund um den Testzeitraum."""
    out = {}
    start = NOW - timedelta(days=days_back)
    for i in range(days_back + days_fwd):
        midnight = datetime(start.date().year, start.date().month,
                            start.date().day, tzinfo=TZ) + timedelta(days=i)
        for h in range(24):
            ts = int((midnight + timedelta(hours=h)).timestamp())
            ts -= ts % 3600
            out[ts] = WeatherHour(ts, 15.0, 0.0, 2400.0, 2)
    return out


def synth_values(start_day, n_days, step_min=10, seed=1):
    """Messreihe: Werktagshoch am Abend, Wochenendniveau Sa/So."""
    rnd = random.Random(seed)
    out = []
    for i in range(n_days):
        d = start_day + timedelta(days=i)
        midnight = datetime(d.year, d.month, d.day, tzinfo=TZ)
        base = 60.0 if d.weekday() < 5 else 30.0
        for minute in range(6 * 60, 23 * 60, step_min):
            dt = midnight + timedelta(minutes=minute)
            v = base * (0.4 + 0.6 * min(1.0, (minute - 6 * 60) / (10 * 60))) \
                + max(0.0, 20.0 - 0.5 * abs(minute - 18 * 60)) + rnd.gauss(0, 1.0)
            out.append((int(dt.timestamp()), max(0.0, v)))
    return out


class ServerAppTestCase(unittest.TestCase):
    """App-Endpunkte direkt (ohne HTTP-Server) mit Store im TempDir."""

    GYMS = ("EVO Unterbilk", "EVO Schanze")

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.store = Store(str(Path(cls.tmp.name) / "visits.db"))
        start = TODAY - timedelta(days=21)
        for i, gym in enumerate(cls.GYMS):
            items = [(ts, Reading(gym, count=v))
                     for ts, v in synth_values(start, 21, seed=i + 1)]
            cls.store.add_batch(items)
        cls.ctx = FakeContext(
            weather_map=synth_weather(),
            status_rows=[
                {"source": "weather:düsseldorf", "fetched_at": NOW_TS - 3600,
                 "ok": 1, "n": 800, "error": None},
                {"source": "school:NW:2026", "fetched_at": NOW_TS - 86400,
                 "ok": 1, "n": 6, "error": None},
                {"source": "school:HH:2026", "fetched_at": NOW_TS - 86400,
                 "ok": 1, "n": 6, "error": None},
            ],
        )
        cfg = Config()
        cfg.db_path = str(Path(cls.tmp.name) / "visits.db")
        cls.app = FixedNowApp(cfg, cls.store, None, demo=False, ctx=cls.ctx)

    def q(self, **kw):
        return {k: [v] for k, v in kw.items()}

    def test_api_gym_fields(self):
        out = self.app.api_gym(self.q(gym="EVO Unterbilk"))
        self.assertNotIn("error", out)
        self.assertEqual(out["location"], {"city": "Düsseldorf", "state": "NW"})
        self.assertEqual(len(out["days"]), 7)
        for f in ("rain", "warm", "cold", "sun", "school", "bridge"):
            self.assertIn(f, out["effects"])
        for p in out["forecast"]:
            self.assertEqual(len(p), 6)
            # ohne Profilwert (geschlossen) ist auch der Effekt None
            if p[1] is not None:
                self.assertIsInstance(p[4], float)
            self.assertTrue(p[5] is None or isinstance(p[5], dict))
        self.assertEqual(out["sources"]["weather"]["label"], "Open-Meteo")
        self.assertEqual(out["sources"]["school"]["label"], "OpenHolidays")
        self.assertTrue(out["sources"]["weather"]["ok"])
        self.assertIn("verdict", out["backtest"])

    def test_api_gym_unknown_club(self):
        self.assertEqual(self.app.api_gym(self.q(gym="Gibts nicht")),
                         {"error": "unbekannter Club"})

    def test_api_gyms_city_state(self):
        out = self.app.api_gyms({})
        by_name = {g["gym"]: g for g in out}
        self.assertEqual(by_name["EVO Unterbilk"]["city"], "Düsseldorf")
        self.assertEqual(by_name["EVO Unterbilk"]["state"], "NW")
        self.assertEqual(by_name["EVO Schanze"]["city"], "Hamburg")
        self.assertEqual(by_name["EVO Schanze"]["state"], "HH")
        for g in out:
            self.assertIn("typical_now", g)

    def test_api_weekly(self):
        out = self.app.api_weekly(self.q(gym="EVO Unterbilk"))
        self.assertNotIn("error", out)
        self.assertEqual(out["gym"], "EVO Unterbilk")
        self.assertEqual(out["bin_minutes"], 15)
        self.assertGreaterEqual(out["weeks"], 1)
        dates = [d["date"] for d in out["days"]]
        excluded = [d["date"] for d in out["excluded"]]
        self.assertIn(HOLIDAY_ISO, excluded)
        self.assertNotIn(HOLIDAY_ISO, dates)
        self.assertNotIn(TODAY.isoformat(), dates)
        for day in out["days"]:
            self.assertEqual(len(day["values"]), 96)
            self.assertTrue(all(v is None or isinstance(v, float) for v in day["values"]))
            self.assertTrue(any(v is not None for v in day["values"]))
        reasons = {d["date"]: d["reason"] for d in out["excluded"]}
        self.assertEqual(reasons[HOLIDAY_ISO], "Tag der Deutschen Einheit")

    def test_api_weekly_unknown_club(self):
        self.assertEqual(self.app.api_weekly(self.q(gym="Gibts nicht")),
                         {"error": "unbekannter Club"})

    def test_api_status_has_context(self):
        out = self.app.api_status({})
        self.assertIn("context", out)
        self.assertEqual(len(out["context"]), 3)

    def test_api_gyms_builds_no_model(self):
        with mock.patch("evo.server.backtest_compare", side_effect=AssertionError), \
                mock.patch("evo.server.build_model", side_effect=AssertionError):
            out = self.app.api_gyms({})
        self.assertEqual(len(out), len(self.GYMS))
        for g in out:
            self.assertIsNotNone(g["typical_now"])

    def test_api_gyms_uses_cached_model_predict(self):
        model = self.app.model("EVO Unterbilk",
                               {g["gym"]: g for g in self.store.gyms()}["EVO Unterbilk"]["last_ts"])
        p = model.predict(NOW_TS)
        expected = round(p["est"], 1) if p else None
        with mock.patch.object(model, "predict", wraps=model.predict) as pred:
            out = self.app.api_gyms({})
        pred.assert_called()
        by_name = {g["gym"]: g for g in out}
        self.assertEqual(by_name["EVO Unterbilk"]["typical_now"], expected)

    def test_api_weekly_builds_no_model(self):
        with mock.patch("evo.server.backtest_compare", side_effect=AssertionError), \
                mock.patch("evo.server.build_model", side_effect=AssertionError):
            out = self.app.api_weekly(self.q(gym="EVO Unterbilk"))
        self.assertNotIn("error", out)
        self.assertTrue(out["days"])


class CollectorTestCase(unittest.TestCase):
    def test_run_once_refreshes_context_once(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(str(Path(tmp.name) / "visits.db"))
        store.add_batch([(NOW_TS, Reading("EVO Unterbilk", count=5))])
        cfg = Config()
        cfg.db_path = str(Path(tmp.name) / "visits.db")
        ctx = FakeContext()
        col = Collector(cfg, store, ctx)
        res = FetchResult([Reading("EVO Unterbilk", count=5)], "test")
        with mock.patch("evo.server.collect_once", return_value=res) as fetch:
            out = col.run_once()
        fetch.assert_called_once()
        self.assertTrue(out["ok"])
        self.assertEqual(ctx.refresh_calls, 1)


if __name__ == "__main__":
    unittest.main()
