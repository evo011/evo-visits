import math
import random
import time
import unittest
from collections import defaultdict, namedtuple
from datetime import date, timedelta

from evo.effects import (Row, backfit, band, feature_row, fit_effects, holiday_sd,
                         residual_sd_by_hour, weather_sd, weather_x)

W = namedtuple("W", "temp precip sunshine")

START = date(2026, 8, 1)
SLOTS = list(range(24, 92))  # 6:00 bis 22:45 Uhr


def bell(slot):
    """Wahres Basisprofil je Slot: Glockenkurve um 18 Uhr."""
    h = slot / 4
    return 60 * math.exp(-((h - 18) / 2) ** 2) + 10


def synth_rows(days=56, n_rain_days=12, noise=1.0, seed=7):
    """v = (base + 3) * exp(-0.25*rain + 0.15*school) - 3 plus Rauschen."""
    rnd = random.Random(seed)
    rain_days = set(rnd.sample(range(days), n_rain_days))
    school_days = set(range(20, 30))  # 10 aufeinanderfolgende Ferientage
    rows = []
    for i in range(days):
        d = START + timedelta(days=i)
        school = i in school_days
        for slot in SLOTS:
            if i in rain_days and slot >= 48:  # Regen nachmittags
                w = W(temp=15.0, precip=2.0, sunshine=None)
            else:
                w = W(temp=15.0, precip=None, sunshine=0.0)
            x, has_weather = feature_row(w, school, bridge=False)
            v = (bell(slot) + 3) * math.exp(-0.25 * x[0] + 0.15 * x[4]) - 3
            rows.append(Row(d, slot, max(0.0, v + rnd.gauss(0, noise)), x, has_weather))
    return rows


def true_profile(day, slot):
    return bell(slot)


def mean_profile(values):
    by_slot = defaultdict(list)
    for (_, s), v in values.items():
        by_slot[s].append(v)
    mean = {s: sum(v) / len(v) for s, v in by_slot.items()}

    def fn(day, slot):
        return mean.get(slot)
    return fn


class WeatherXTests(unittest.TestCase):
    def test_saturation_and_scales(self):
        self.assertEqual(weather_x(W(15, 2.0, 7200)), (1.0, 0.0, 0.0, 1.0))  # rain/sun gedeckelt
        self.assertEqual(weather_x(W(28, 0.0, 1800)), (0.0, 1.0, 0.0, 0.5))
        self.assertEqual(weather_x(W(3, 0.5, None)), (0.5, 0.0, 0.5, 0.0))
        self.assertEqual(weather_x(W(13, None, 0)), (0.0, 0.0, 0.0, 0.0))

    def test_none_cases(self):
        self.assertIsNone(weather_x(None))
        self.assertIsNone(weather_x(W(None, 1.0, 3600)))
        x, has_weather = feature_row(None, school=True, bridge=True)
        self.assertFalse(has_weather)
        self.assertEqual(x, (0.0, 0.0, 0.0, 0.0, 1.0, 1.0))


class FitEffectsTests(unittest.TestCase):
    def test_recovers_known_effects(self):
        rows = synth_rows()
        fit = fit_effects(rows, true_profile)
        self.assertAlmostEqual(fit.beta["rain"], -0.25, delta=0.05)
        self.assertAlmostEqual(fit.beta["school"], 0.15, delta=0.05)
        lo, hi = fit.ci["rain"]
        self.assertLessEqual(lo, -0.25)
        self.assertGreaterEqual(hi, -0.25)
        lo, hi = fit.ci["school"]
        self.assertLessEqual(lo, 0.15)
        self.assertGreaterEqual(hi, 0.15)
        for f in ("warm", "cold", "sun", "bridge"):
            self.assertFalse(fit.used[f])
            self.assertEqual(fit.beta[f], 0.0)
        self.assertTrue(fit.used["rain"] and fit.used["school"])
        self.assertEqual(fit.active_days["rain"], 12)
        self.assertEqual(fit.active_days["school"], 10)

    def test_effect_fit_helpers_and_dict(self):
        fit = fit_effects(synth_rows(), true_profile)
        x = (0.5, 0.0, 0.0, 0.0, 1.0, 0.0)
        self.assertAlmostEqual(fit.log_effect(x), 0.5 * fit.beta["rain"] + fit.beta["school"])
        self.assertAlmostEqual(fit.multiplier(x), math.exp(fit.log_effect(x)))
        self.assertGreater(fit.param_sd(x), 0.0)
        d = fit.to_dict()
        self.assertEqual(set(d), {"rain", "warm", "cold", "sun", "school", "bridge"})
        self.assertLess(d["rain"]["effect_pct"], 0.0)
        self.assertGreater(d["school"]["effect_pct"], 0.0)
        self.assertFalse(d["bridge"]["used"])
        self.assertEqual(d["bridge"]["effect_pct"], 0.0)

    def test_min_active_days_threshold(self):
        rows = synth_rows(n_rain_days=3)
        fit = fit_effects(rows, true_profile)
        self.assertFalse(fit.used["rain"])
        self.assertEqual(fit.beta["rain"], 0.0)
        self.assertEqual(fit.ci["rain"], (0.0, 0.0))
        self.assertAlmostEqual(fit.beta["school"], 0.15, delta=0.05)  # school bleibt nutzbar

    def test_too_few_days_is_all_zero(self):
        rows = [Row(START, 24, 10.0, (1.0, 0, 0, 0, 0, 0), True)]
        fit = fit_effects(rows, true_profile)
        self.assertTrue(all(fit.beta[f] == 0.0 for f in fit.beta))
        self.assertFalse(any(fit.used.values()))

    def test_rows_without_profile_are_skipped(self):
        rows = synth_rows(days=20)
        calls = []

        def sparse(day, slot):
            calls.append((day, slot))
            return None if slot % 2 else bell(slot)

        fit = fit_effects(rows, sparse)
        self.assertEqual(fit.n_rows, len(rows) // 2)
        self.assertEqual(fit.n_days, 20)

    def test_runtime_70_days_200_boots(self):
        rows = synth_rows(days=70, seed=3)
        t0 = time.time()
        fit = fit_effects(rows, true_profile, n_boot=200)
        self.assertLess(time.time() - t0, 2.0)
        self.assertEqual(fit.n_days, 70)


class BackfitTests(unittest.TestCase):
    def test_profile_moves_toward_true_base(self):
        rows = synth_rows()
        raw = mean_profile({(r.day, r.slot): r.v for r in rows})
        fit, profile = backfit(rows, mean_profile, rounds=2)

        def mad(fn):
            return sum(abs(fn(START, s) - bell(s)) for s in SLOTS) / len(SLOTS)

        self.assertLess(mad(profile), mad(raw))

        # bereinigte Werte eines Regentags liegen nahe am Basisprofil
        rain_day = next(r for r in rows if r.x[0] >= 0.2)
        self.assertLess(abs(profile(rain_day.day, rain_day.slot) - bell(rain_day.slot)),
                        abs(rain_day.v - bell(rain_day.slot)))

    def test_backfit_returns_last_fit(self):
        rows = synth_rows(days=30)
        fit, _ = backfit(rows, mean_profile, rounds=1)
        self.assertTrue(fit.used["rain"] or fit.used["school"])
        self.assertGreater(fit.n_rows, 0)


class BandTests(unittest.TestCase):
    def test_band_symmetric_and_nonnegative(self):
        lo, hi = band(50.0, 2.0, 0.0, 0.0, 0.0)
        self.assertAlmostEqual(50.0 - lo, hi - 50.0)
        self.assertAlmostEqual(lo, 50.0 - 1.2816 * 2.0)
        self.assertEqual(band(0.5, 2.0, 0.0, 0.0, 0.0)[0], 0.0)  # lo nie negativ

    def test_band_widens_with_holiday_sd(self):
        narrow = band(50.0, 2.0, 0.1, 0.1, 0.0)
        wide = band(50.0, 2.0, 0.1, 0.1, holiday_sd(2))
        self.assertGreater(wide[1] - wide[0], narrow[1] - narrow[0])

    def test_holiday_sd_shrinks_with_days(self):
        self.assertGreater(holiday_sd(0), holiday_sd(4))
        self.assertGreater(holiday_sd(4), holiday_sd(16))

    def test_weather_sd_grows_and_caps_at_96h(self):
        fit = fit_effects(synth_rows(), true_profile)
        self.assertLess(weather_sd(fit, 0.0), weather_sd(fit, 24.0))
        self.assertLess(weather_sd(fit, 24.0), weather_sd(fit, 96.0))
        self.assertEqual(weather_sd(fit, 96.0), weather_sd(fit, 240.0))  # gedeckelt
        self.assertGreater(weather_sd(fit, 96.0), 0.0)

    def test_residual_sd_by_hour(self):
        triples = [(h, float(h), float(h)) for h in range(24) for _ in range(10)]
        triples.append((0, 100.0, 0.0))  # Ausreisser in Stunde 0
        sd = residual_sd_by_hour(triples)
        self.assertEqual(set(sd), set(range(24)))
        self.assertGreater(sd[0], sd[1])       # Ausreisser schlaegt durch
        self.assertGreaterEqual(sd[1], 1.0)    # Minimum 1.0
        thin = [(h, 5.0, 5.0) for h in range(5)]  # je Stunde nur 1 Wert -> Gesamt-SD
        sd2 = residual_sd_by_hour(thin)
        self.assertEqual(len(set(sd2.values())), 1)
        self.assertEqual(residual_sd_by_hour([]), {})


if __name__ == "__main__":
    unittest.main()
