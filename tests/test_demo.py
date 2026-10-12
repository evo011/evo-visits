import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from evo.config import Config
from evo.context import Context
from evo.demo import DEMO_LOC_KEY, DEMO_STATE, generate
from evo.store import Store

TZ = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 10, 12, 12, 0, tzinfo=TZ).timestamp()
NOW = int(NOW)
WEEKS = 6


class DemoTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.tmp.name) / "demo.db"))
        self.cfg = Config()
        self.n = generate(self.store, "Europe/Berlin", weeks=WEEKS, now=NOW)
        self.ctx = Context(self.store, self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def samples(self, gym: str, d: date, hour_from: int, hour_to: int):
        start = int(datetime(d.year, d.month, d.day, hour_from, tzinfo=TZ).timestamp())
        end = int(datetime(d.year, d.month, d.day, hour_to, tzinfo=TZ).timestamp())
        return [v for _, v in self.store.series(gym, start, end)]


class WeatherTests(DemoTestCase):
    def test_weather_range_and_content(self):
        hours = self.ctx.weather(DEMO_LOC_KEY, 0, 2**62)
        first = NOW - WEEKS * 7 * 86400
        self.assertLessEqual(min(hours), first - first % 3600)
        self.assertGreaterEqual(max(hours), NOW + 8 * 86400 - 3600)
        self.assertTrue(any((h.precip or 0) > 0 for h in hours.values()))
        self.assertTrue(any((h.temp or 0) > 18 for h in hours.values()))

    def test_all_hours_full_hour(self):
        hours = self.ctx.weather(DEMO_LOC_KEY, 0, 2**62)
        self.assertTrue(all(h.ts % 3600 == 0 for h in hours.values()))


class HolidayTests(DemoTestCase):
    def test_demo_ferien_present(self):
        today = datetime.fromtimestamp(NOW, TZ).date()
        self.assertEqual(self.ctx.school_holiday(DEMO_STATE, today + timedelta(days=4)), "Demo-Ferien")
        self.assertEqual(
            self.ctx.school_holiday(DEMO_STATE, today - timedelta(days=20)), "Demo-Herbstferien")
        self.assertIsNone(self.ctx.school_holiday(DEMO_STATE, today + timedelta(days=10)))

    def test_context_fetch_rows(self):
        status = {r["source"]: r for r in self.ctx.status()}
        self.assertIn(f"weather:{DEMO_LOC_KEY}", status)
        row = status[f"weather:{DEMO_LOC_KEY}"]
        self.assertEqual(row["ok"], 1)
        self.assertIsNone(row["error"])
        self.assertIn(f"school:{DEMO_STATE}:{datetime.fromtimestamp(NOW, TZ).date().year}", status)


class PublicHolidayPatternTests(DemoTestCase):
    def test_public_holiday_uses_sunday_pattern(self):
        # 3.10.2026 ist Tag der Deutschen Einheit -> Feiertag, Sonntagsmuster.
        gym = "Demo Mitte"
        unity = self.samples(gym, date(2026, 10, 3), 17, 19)
        weekdays = [self.samples(gym, date(2026, 10, d), 17, 19)
                    for d in (1, 2, 5, 6, 7, 8)]  # Werktage ohne Feiertag, ohne Ferien
        self.assertTrue(unity, "keine Messwerte am 3.10.")
        unity_avg = sum(unity) / len(unity)
        wd_avg = sum(sum(s) / len(s) for s in weekdays if s) / sum(1 for s in weekdays if s)
        self.assertLess(unity_avg, 0.8 * wd_avg,
                        f"Feiertag {unity_avg:.1f} sollte deutlich unter Werktagen {wd_avg:.1f} liegen")


if __name__ == "__main__":
    unittest.main()
