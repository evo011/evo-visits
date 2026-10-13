import unittest
from datetime import date, datetime, timedelta

from evo.forecast import POOL_K, Profile, get_tz
from evo.pool import build_prior, club_levels

TZ = get_tz("Europe/Berlin")
TODAY = date(2026, 10, 12)
SLOT = 12 * 4  # 12:00 Uhr, im linearen Teil der Form


def tri(h):
    """Dreiecksform ueber 06-23 Uhr, Peak 18 Uhr."""
    if not (6 <= h < 23):
        return 0.0
    return (h - 6) / 12 if h <= 18 else (23 - h) / 5


def form(kind, h):
    """Gemeinsame Form; am Wochenende halbe Amplitude."""
    return 40.0 * tri(h) * (0.5 if kind >= 5 else 1.0)


def binned_for(level, days):
    return {(d, h * 4): level * form(d.weekday(), h)
            for d in days for h in range(6, 23)}


def days_back(n):
    return [TODAY - timedelta(days=i) for i in range(n, 0, -1)]


def days_of(weekday, n):
    """Die n juengsten Tage mit dem Wochentag vor TODAY."""
    out = []
    d = TODAY - timedelta(days=1)
    while len(out) < n:
        if d.weekday() == weekday:
            out.append(d)
        d -= timedelta(days=1)
    return out


def kinds(*gyms):
    return {g: (lambda d: d.weekday()) for g in gyms}


def now_ts():
    return int(datetime(TODAY.year, TODAY.month, TODAY.day, 12, tzinfo=TZ).timestamp())


class ClubLevelsTests(unittest.TestCase):
    def test_levels_ratio_1_2_3(self):
        days = days_back(5)
        binned = {"A": binned_for(1.0, days), "B": binned_for(2.0, days),
                  "C": binned_for(3.0, days)}
        lv = club_levels(binned, kinds("A", "B", "C"), TODAY)
        self.assertAlmostEqual(lv["A"], 0.5, delta=0.005)
        self.assertAlmostEqual(lv["B"], 1.0, delta=0.01)
        self.assertAlmostEqual(lv["C"], 1.5, delta=0.015)

    def test_future_values_do_not_count(self):
        days = days_back(5)
        clean = {"A": binned_for(1.0, days), "B": binned_for(2.0, days),
                 "C": binned_for(3.0, days)}
        lv_clean = club_levels(clean, kinds("A", "B", "C"), TODAY)
        dirty = {g: dict(b) for g, b in clean.items()}
        for g in dirty:
            dirty[g][(TODAY, SLOT)] = 9999.0
            dirty[g][(TODAY + timedelta(days=1), SLOT)] = 9999.0
        dirty["D"] = {(TODAY, SLOT): 5.0}  # nur heute -> keine Beobachtung
        lv_dirty = club_levels(dirty, kinds("A", "B", "C", "D"), TODAY)
        self.assertNotIn("D", lv_dirty)
        for g in ("A", "B", "C"):
            self.assertAlmostEqual(lv_clean[g], lv_dirty[g], delta=1e-9)


class PriorTests(unittest.TestCase):
    def pool(self, c_days=None):
        mondays = days_of(0, 2)
        saturdays = days_of(5, 2)
        binned = {"A": binned_for(1.0, mondays + saturdays),
                  "B": binned_for(2.0, mondays + saturdays),
                  "C": binned_for(3.0, c_days if c_days is not None else mondays)}
        return binned, build_prior(binned, kinds("A", "B", "C"), TODAY)

    def test_prior_is_level_times_shape(self):
        binned, (levels, shape) = self.pool()
        art = days_back(1)[0].weekday()
        st = shape.prior_for("B")(art, SLOT)
        self.assertIsNotNone(st)
        # Niveau B (1.0) mal gemeinsamer Form in B-Einheiten (= 2 * form)
        expected = 2.0 * form(art, 12)
        self.assertAlmostEqual(st.mean, expected, delta=0.01 * expected)

    def test_prior_fills_unseen_weekday(self):
        # C hat nur Montagsdaten, der Pool traegt die Samstagsform nach
        binned, (levels, shape) = self.pool()
        st = shape.prior_for("C")(5, SLOT)
        self.assertIsNotNone(st)
        expected = 3.0 * form(5, 12)  # wahres Niveau C mal Samstagsform
        self.assertAlmostEqual(st.mean, expected, delta=0.01 * expected)


class ProfileWithPoolPriorTests(unittest.TestCase):
    def test_slot_without_own_data_uses_prior(self):
        binned, (levels, shape) = PriorTests.pool(self)
        prof = Profile([], TZ, now_ts(), binned=binned["C"],
                       pool_prior=shape.prior_for("C"))
        st = prof.seasonal(5, SLOT)  # C hat keine Samstagsdaten
        self.assertIsNotNone(st)
        expected = 3.0 * form(5, 12)
        self.assertAlmostEqual(st.mean, expected, delta=0.01 * expected)

    def test_slot_with_own_data_merges_with_pool_k(self):
        binned, (levels, shape) = PriorTests.pool(self)
        d = days_of(0, 1)[0]
        w = 0.5 ** ((TODAY - d).days / 21.0)
        prior = shape.prior_for("C")(0, SLOT)
        self.assertAlmostEqual(prior.mean, 3.0 * form(0, 12), delta=0.6)
        own = {(d, SLOT): 90.0}  # weicht deutlich vom Prior ab
        prof = Profile([], TZ, now_ts(), binned=own,
                       pool_prior=shape.prior_for("C"))
        st = prof.seasonal(0, SLOT)
        expected = (w * 90.0 + POOL_K * prior.mean) / (w + POOL_K)
        self.assertAlmostEqual(st.mean, expected, delta=1e-6)
        self.assertAlmostEqual(st.weight, w, delta=1e-9)

    def test_prior_none_keeps_old_behaviour(self):
        binned, (levels, shape) = PriorTests.pool(self)
        ts = now_ts()
        prof_none = Profile([], TZ, ts, binned=binned["B"],
                            pool_prior=shape.prior_for("unbekannt"))
        prof_plain = Profile([], TZ, ts, binned=binned["B"])
        for wd in range(7):
            self.assertEqual(prof_none.seasonal(wd, SLOT), prof_plain.seasonal(wd, SLOT))


if __name__ == "__main__":
    unittest.main()
