import math
import os
import random
import tempfile
import unittest
from datetime import datetime, timedelta

from evo.forecast import Profile, backtest, get_tz
from evo.store import Reading, Store

TZ = get_tz("Europe/Berlin")


def pattern(dt):
    h = dt.hour + dt.minute / 60
    if not (6 <= h < 23):
        return None
    base = 60 if dt.weekday() < 5 else 30
    return base * math.exp(-((h - 18) / 2) ** 2) + 10


def synth(days=28, step_min=10, noise=2.0, seed=1, end=None):
    rnd = random.Random(seed)
    end = end or datetime(2026, 10, 12, 12, 0, tzinfo=TZ)
    t = end - timedelta(days=days)
    out = []
    while t < end:
        v = pattern(t)
        if v is not None:
            out.append((int(t.timestamp()), max(0.0, v + rnd.gauss(0, noise))))
        t += timedelta(minutes=step_min)
    return out, end


class ForecastTests(unittest.TestCase):
    def test_profile_recovers_weekly_pattern(self):
        series, end = synth()
        prof = Profile(series, TZ, int(end.timestamp()))
        tue_18 = prof.seasonal(1, 18 * 4)
        sat_18 = prof.seasonal(5, 18 * 4)
        self.assertAlmostEqual(tue_18.mean, 70, delta=4)
        self.assertAlmostEqual(sat_18.mean, 40, delta=4)
        self.assertIsNone(prof.seasonal(1, 3 * 4))  # nachts keine Daten

    def test_forecast_shape_and_bounds(self):
        series, end = synth()
        prof = Profile(series, TZ, int(end.timestamp()))
        fc = prof.forecast(hours=48, recent=series[-6:], cap=100)
        self.assertEqual(len(fc), 48 * 4)
        vals = [p for p in fc if p["est"] is not None]
        self.assertTrue(vals)
        for p in vals:
            self.assertLessEqual(p["lo"], p["est"])
            self.assertLessEqual(p["est"], p["hi"])
            self.assertGreaterEqual(p["lo"], 0)

    def test_nowcast_shifts_near_term_only(self):
        series, end = synth()
        now = int(end.timestamp())
        prof = Profile(series, TZ, now)
        busy = [(ts, v + 20) for ts, v in series if ts > now - 40 * 60]
        base = prof.forecast(hours=24, recent=[(ts, v) for ts, v in series if ts > now - 40 * 60])
        shifted = prof.forecast(hours=24, recent=busy)
        self.assertGreater(shifted[0]["est"] - base[0]["est"], 15)
        late = [i for i, p in enumerate(base) if p["est"] is not None][-1]
        self.assertLess(abs(shifted[late]["est"] - base[late]["est"]), 1.5)

    def test_little_data_falls_back_to_day_type(self):
        series, end = synth(days=3)  # nur Fr bis So vorhanden, Dienstag nie gesehen
        prof = Profile(series, TZ, int(end.timestamp()))
        self.assertIsNotNone(prof.seasonal(1, 18 * 4))

    def test_backtest_beats_last_week_on_noisy_data(self):
        series, _ = synth(days=35, noise=8.0)
        bt = backtest(series, TZ, days=7)
        self.assertIsNotNone(bt)
        self.assertLess(bt["mae"], bt["mae_last_week"])

    def test_heatmap(self):
        series, end = synth()
        hm = Profile(series, TZ, int(end.timestamp())).heatmap()
        self.assertEqual((len(hm), len(hm[0])), (7, 24))
        self.assertIsNone(hm[0][3])
        self.assertGreater(hm[0][18], hm[0][8])


class StoreTests(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            s = Store(os.path.join(d, "t.db"))
            s.add_readings(100, [Reading("A", count=5, capacity=50), Reading("B", pct=40.0), Reading("C")])
            s.add_readings(200, [Reading("A", count=7, capacity=50)])
            gyms = {g["gym"]: g for g in s.gyms()}
            self.assertEqual(set(gyms), {"A", "B"})
            self.assertEqual(gyms["A"]["count"], 7)
            self.assertEqual(gyms["A"]["n_samples"], 2)
            self.assertEqual(s.series("A", 0), [(100, 5.0), (200, 7.0)])
            self.assertEqual(s.series("B", 0), [(100, 40.0)])


if __name__ == "__main__":
    unittest.main()
