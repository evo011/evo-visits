import math
import random
import time
import unittest
from datetime import date, datetime, timedelta

from evo.calendar_de import Location
from evo.context import WeatherHour
from evo.forecast import Profile, get_tz
from evo.model import FeatureSource, Model, backtest_compare, build_model, effects_help

TZ = get_tz("Europe/Berlin")
LOC = Location("düsseldorf", "Düsseldorf", "NW", 51.227, 6.773)
NOW = datetime(2026, 10, 12, 12, 0, tzinfo=TZ)      # Montag
NOW_TS = int(NOW.timestamp())
HOLIDAY = date(2026, 10, 3)                          # Tag der Deutschen Einheit (Samstag, NW)
SCHOOL_DAYS = {date(2026, 10, 5) + timedelta(days=i) for i in range(14)}  # Herbstferien-Fake


class FakeContext:
    """Duck-typed Context: Wetter aus Dict, Ferien aus Menge."""

    def __init__(self, weather, school_days):
        self.weather_map = weather
        self.school_days = school_days

    def weather(self, loc_key, start_ts, end_ts):
        return {ts: w for ts, w in self.weather_map.items() if start_ts <= ts <= end_ts}

    def school_holiday(self, state, d):
        return "Herbstferien" if d in self.school_days else None


def synth_weather(start_day, days, rain_days):
    """Stundenwerte UTC; Regen an rain_days nachmittags (14-18 Uhr lokal)."""
    weather = {}
    for i in range(days):
        d = start_day + timedelta(days=i)
        midnight_local = datetime(d.year, d.month, d.day, tzinfo=TZ)
        for h in range(24):
            ts = int((midnight_local + timedelta(hours=h)).timestamp())
            ts = ts - ts % 3600
            rain = d in rain_days and 14 <= h < 18
            weather[ts] = WeatherHour(ts,
                                      15.0 if not rain else 12.0,
                                      2.0 if rain else 0.0,
                                      0.0 if rain else 3600.0,
                                      65 if rain else 1)
    return weather


def make_feats(rain_days, end=NOW_TS, start_days=42, extra_days=8):
    start = datetime.fromtimestamp(end, TZ) - timedelta(days=start_days)
    weather = synth_weather(start.date(), start_days + extra_days, rain_days)
    ctx = FakeContext(weather, SCHOOL_DAYS)
    return FeatureSource(LOC, TZ, ctx, int(start.timestamp()),
                         int((start + timedelta(days=start_days + extra_days)).timestamp()))


def pattern(dt, day_kind):
    h = dt.hour + dt.minute / 60
    if not (6 <= h < 23):
        return None
    base = 60 if day_kind < 5 else 30
    return base * math.exp(-((h - 18) / 2) ** 2) + 10


def synth_series(feats, days=42, step_min=10, noise=2.0, seed=1, rain_factor=0.75):
    """Messreihe: Sonntagsmuster an Feiertagen, Regenfaktor bei Regen."""
    rnd = random.Random(seed)
    end = NOW
    t = end - timedelta(days=days)
    out = []
    while t < end:
        v = pattern(t, feats.day_kind(t.date()))
        if v is not None:
            w = feats.weather_at(int(t.timestamp()))
            if w is not None and w.precip and w.precip >= 0.2:
                v *= rain_factor
            out.append((int(t.timestamp()), max(0.0, v + rnd.gauss(0, noise))))
        t += timedelta(minutes=step_min)
    return out


RAINY_AFTERNOON = {date(2026, 9, 15) + timedelta(days=5 * i) for i in range(8)}  # 8 Regentage


def make_model(use_effects=True, **kw):
    feats = make_feats(RAINY_AFTERNOON)
    series = synth_series(feats)
    return Model(series, TZ, NOW_TS, feats, use_effects=use_effects, **kw), feats, series


class FeatureSourceTests(unittest.TestCase):
    def setUp(self):
        self.feats = make_feats(RAINY_AFTERNOON)

    def test_day_kind_holiday_and_normal(self):
        self.assertEqual(self.feats.day_kind(HOLIDAY), 6)
        self.assertEqual(self.feats.day_kind(date(2026, 10, 13)), 1)  # Dienstag

    def test_weather_at_uses_hour_end_key(self):
        # Stunde 13-14 Uhr lokal: Wert ist auf 14:00 (Stundenende) gestempelt
        ts = int(datetime(2026, 10, 11, 13, 30, tzinfo=TZ).timestamp())
        w = self.feats.weather_at(ts)
        self.assertIsNotNone(w)
        self.assertEqual(w.ts, ts - ts % 3600 + 3600)

    def test_memoized_calendar(self):
        d = date(2026, 10, 5)
        self.assertEqual(self.feats.school(d), "Herbstferien")
        self.assertIn(d, self.feats._memo["school"])


class ProfileDayKindTests(unittest.TestCase):
    def test_holiday_uses_sunday_profile(self):
        feats = make_feats(set())
        series = synth_series(feats)
        # 3.10. ist Samstag-Feiertag -> Sonntag-Profil; Vergleich: normaler Samstag
        prof = Profile(series, TZ, NOW_TS, day_kind=feats.day_kind)
        holiday_slot = prof.seasonal_for(HOLIDAY, 18 * 4)
        sat = date(2026, 10, 10)
        normal_sat = prof.seasonal_for(sat, 18 * 4)
        sun = date(2026, 10, 4)
        sunday = prof.seasonal_for(sun, 18 * 4)
        self.assertIsNotNone(holiday_slot)
        self.assertLess(abs(holiday_slot.mean - sunday.mean),
                        abs(holiday_slot.mean - (normal_sat.mean if normal_sat else 100)))
        self.assertLess(holiday_slot.mean, 45)  # Sonntagsniveau, nicht Werktagsniveau


class ModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model, cls.feats, cls.series = make_model()

    def test_rain_effect_detected(self):
        fit = self.model.fit
        self.assertLess(fit.beta["rain"], -0.1)
        self.assertTrue(fit.used["rain"])

    def test_predict_lower_on_rain(self):
        rain_day = max(RAINY_AFTERNOON)
        dry_day = rain_day + timedelta(days=1)
        ts_rain = int(datetime(rain_day.year, rain_day.month, rain_day.day, 16, 0, tzinfo=TZ).timestamp())
        ts_dry = int(datetime(dry_day.year, dry_day.month, dry_day.day, 16, 0, tzinfo=TZ).timestamp())
        p_rain = self.model.predict(ts_rain)
        p_dry = self.model.predict(ts_dry)
        self.assertIsNotNone(p_rain)
        self.assertIsNotNone(p_dry)
        self.assertLess(p_rain["est"], p_dry["est"])

    def test_band_wider_on_holiday(self):
        # 25.12. ist Feiertag in NW, 22.12. ein normaler Dienstag; gleiche Uhrzeit.
        # Das Band am Feiertag ist wegen sd_holiday breiter.
        ts_h = int(datetime(2026, 12, 25, 16, 0, tzinfo=TZ).timestamp())
        ts_n = int(datetime(2026, 12, 22, 16, 0, tzinfo=TZ).timestamp())
        p_h = self.model.predict(ts_h)
        p_n = self.model.predict(ts_n)
        self.assertIsNotNone(p_h)
        self.assertIsNotNone(p_n)
        self.assertEqual(p_h["holiday"], "1. Weihnachtstag")
        self.assertIsNone(p_n["holiday"])
        self.assertGreater(p_h["hi"] - p_h["lo"], p_n["hi"] - p_n["lo"])

    def test_predict_fields(self):
        ts = int(datetime(2026, 10, 13, 16, 0, tzinfo=TZ).timestamp())
        p = self.model.predict(ts)
        for key in ("ts", "est", "lo", "hi", "mult", "effect_pct", "weather",
                    "holiday", "school", "bridge"):
            self.assertIn(key, p)
        self.assertIsNone(p["holiday"])
        self.assertEqual(p["school"], "Herbstferien")
        if p["weather"] is not None:
            for key in ("temp", "precip", "sun_min", "code"):
                self.assertIn(key, p["weather"])

    def test_forecast_shape(self):
        fc = self.model.forecast(hours=48, recent=self.series[-6:])
        self.assertEqual(len(fc), 192)
        self.assertEqual(fc[1]["ts"] - fc[0]["ts"], 15 * 60)
        vals = [p for p in fc if p["est"] is not None]
        self.assertTrue(vals)
        for p in vals:
            self.assertLessEqual(p["lo"], p["est"])
            self.assertLessEqual(p["est"], p["hi"])
            self.assertGreaterEqual(p["lo"], 0)

    def test_forecast_points_carry_effect_and_weather_fields(self):
        fc = self.model.forecast(hours=48, recent=self.series[-6:])
        for p in fc:
            for key in ("ts", "est", "lo", "hi", "effect_pct", "weather",
                        "holiday", "school", "bridge"):
                self.assertIn(key, p)
            if p["est"] is None:
                self.assertIsNone(p["effect_pct"])
                self.assertIsNone(p["weather"])
            else:
                self.assertIsInstance(p["effect_pct"], float)
                self.assertIn("temp", p["weather"])
                self.assertIn("precip", p["weather"])
                self.assertIn("sun_min", p["weather"])
                self.assertIn("code", p["weather"])

    def test_single_holiday_in_training_data(self):
        # 42 Tage ab 31.8.: einziger NW-Feiertag darin ist der 3.10.
        self.assertEqual(self.model.n_holiday_days, 1)

    def test_day_summaries(self):
        ds = self.model.day_summaries(7)
        self.assertEqual(len(ds), 7)
        self.assertEqual(ds[0]["date"], "2026-10-12")
        for day in ds:
            for key in ("date", "weekday", "holiday", "school", "bridge", "weather", "effect_pct"):
                self.assertIn(key, day)
            self.assertIsNotNone(day["weather"])
            for key in ("tmin", "tmax", "precip", "sun_h", "code"):
                self.assertIn(key, day["weather"])

    def test_effects_summary(self):
        s = self.model.effects_summary()
        self.assertIn("rain", s)
        self.assertEqual(s["n_days"], 42)  # Reihe endet am Vortag, heute ist nicht dabei
        self.assertTrue(s["use_effects"])

    def test_no_effects_all_zero(self):
        m, _, _ = make_model(use_effects=False)
        self.assertTrue(all(b == 0.0 for b in m.fit.beta.values()))
        self.assertFalse(any(m.fit.used.values()))
        fc = m.forecast(hours=2, recent=None)
        self.assertTrue(all(p["est"] is None or p["est"] >= 0 for p in fc))


class BacktestCompareTests(unittest.TestCase):
    def test_model_beats_profile_with_real_rain_effect(self):
        # Regen auch in den letzten 7 Tagen, damit der Backtest etwas zu holen hat
        rain_days = {date(2026, 9, 15) + timedelta(days=5 * i) for i in range(8)}
        feats = make_feats(rain_days)
        series = synth_series(feats, rain_factor=0.7)
        bt = backtest_compare(series, TZ, feats, n_boot=40)
        self.assertIsNotNone(bt)
        for key in ("mae_model", "mae_profile", "mae_last_week", "coverage_pct", "n",
                    "days", "first_day", "last_day", "weather_note"):
            self.assertIn(key, bt)
        self.assertLess(bt["mae_model"], bt["mae_profile"])
        self.assertTrue(effects_help(bt))
        self.assertGreaterEqual(bt["coverage_pct"], 0)
        self.assertLessEqual(bt["coverage_pct"], 100)
        self.assertEqual(bt["days"], 7)
        self.assertEqual(bt["last_day"], "2026-10-12")  # Reihe endet am 12.10. mittags

    def test_no_rain_effect_still_produces_dict(self):
        # Reihe ohne Regeneffekt im Wert, Wetter trotzdem zufaellig verregnet
        rain_days = {date(2026, 9, 15) + timedelta(days=4 * i) for i in range(10)}
        feats = make_feats(rain_days)
        series = synth_series(feats, rain_factor=1.0)
        bt = backtest_compare(series, TZ, feats, n_boot=40)
        self.assertIsNotNone(bt)
        self.assertIsInstance(effects_help(bt), bool)
        for key in ("mae_model", "mae_profile", "mae_last_week", "coverage_pct", "n"):
            self.assertIn(key, bt)

    def test_too_little_history_is_none(self):
        feats = make_feats(RAINY_AFTERNOON)
        series = synth_series(feats, days=5)
        bt = backtest_compare(series, TZ, feats)
        self.assertIsNone(bt)
        self.assertIsNone(effects_help(bt))
        model = build_model(series, TZ, NOW_TS, feats, bt=None)
        self.assertEqual(model.verdict, "ungeprüft")
        self.assertTrue(model.use_effects)

    def test_bad_backtest_disables_effects(self):
        feats = make_feats(RAINY_AFTERNOON)
        series = synth_series(feats)
        bt = {"mae_model": 10.0, "mae_profile": 5.0}
        model = build_model(series, TZ, NOW_TS, feats, bt=bt)
        self.assertEqual(model.verdict, "verworfen")
        self.assertFalse(model.use_effects)
        self.assertTrue(all(b == 0.0 for b in model.fit.beta.values()))
        s = model.effects_summary()
        self.assertEqual(s["verdict"], "verworfen")

    def test_good_backtest_keeps_effects(self):
        feats = make_feats(RAINY_AFTERNOON)
        series = synth_series(feats)
        bt = {"mae_model": 5.0, "mae_profile": 10.0}
        model = build_model(series, TZ, NOW_TS, feats, bt=bt)
        self.assertEqual(model.verdict, "hilft")
        self.assertTrue(model.use_effects)
        s = model.effects_summary()
        self.assertEqual(s["rain"]["status"], "aktiv")
        self.assertEqual(s["verdict"], "hilft")

    def test_summary_feature_status(self):
        model, _, _ = make_model()
        s = model.effects_summary()
        for f in ("rain", "warm", "cold", "sun", "school", "bridge"):
            self.assertIn(s[f]["status"], ("aktiv", "verworfen", "zu wenig Daten"))
        self.assertEqual(s["verdict"], "ungeprüft")

    def test_backtest_runtime(self):
        feats = make_feats(RAINY_AFTERNOON)
        series = synth_series(feats)
        t0 = time.time()
        bt = backtest_compare(series, TZ, feats, n_boot=40)
        self.assertIsNotNone(bt)
        self.assertLess(time.time() - t0, 15.0)


if __name__ == "__main__":
    unittest.main()
