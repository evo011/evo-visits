import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from evo.calendar_de import locate
from evo.config import Config
from evo.context import Context
from evo.store import Store

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class ContextTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.tmp.name) / "visits.db"))
        self.cfg = Config()
        self.clock = {"t": 1_791_734_400.0}  # 2026-10-08, passend zu den Fixture-Daten

        def now():
            return self.clock["t"]

        self.now = now

    def tearDown(self):
        self.tmp.cleanup()

    def make_context(self, http):
        return Context(self.store, self.cfg, now_fn=self.now, http=http)


class ClubLocationTests(unittest.TestCase):
    def test_club_keywords(self):
        cases = [
            ("EVO Unterbilk", "düsseldorf", "NW"),
            ("EVO LeFlair", "düsseldorf", "NW"),
            ("EVO Dern'sche Höfe", "wiesbaden", "HE"),
            ("EVO Prager Straße", "dresden", "SN"),
            ("EVO Prager Strasse", "dresden", "SN"),
            ("EVO Ehrenstraße", "köln", "NW"),
            ("EVO Belgisches Viertel", "köln", "NW"),
            ("EVO Glockenbach", "münchen", "BY"),
            ("EVO Schanze", "hamburg", "HH"),
            ("EVO Alexanderplatz", "berlin", "BE"),
        ]
        for gym, key, state in cases:
            loc = locate(gym, "Düsseldorf")
            self.assertEqual((loc.key, loc.state), (key, state), gym)

    def test_locate_priority_overrides_and_default(self):
        self.assertEqual(locate("Demo Nord", "Düsseldorf").key, "düsseldorf")
        self.assertEqual(locate("Demo Nord", "Düsseldorf", {"Demo Nord": "Hamburg"}).state, "HH")

    def test_explicit_city_beats_club_keyword(self):
        # 'EVO Frankfurt Westend' enthaelt das Berlin-Stichwort 'westend',
        # aber der explizite Stadtname greift zuerst.
        self.assertEqual(locate("EVO Frankfurt Westend", "Düsseldorf").key, "frankfurt")
        self.assertEqual(locate("EVO Westend", "Düsseldorf").key, "berlin")


class WeatherTests(ContextTestCase):
    def test_weather_stored_and_readable(self):
        calls = []

        def http(url):
            calls.append(url)
            return load_fixture("openmeteo_duesseldorf.json")

        ctx = self.make_context(http)
        loc = locate("EVO Unterbilk", "Düsseldorf")
        res = ctx.refresh_weather(loc)
        self.assertTrue(res["ok"])
        self.assertEqual(res["n"], 48)  # 6 Null-Stunden werden uebersprungen
        self.assertEqual(len(calls), 1)
        self.assertIn("latitude=51.227", calls[0])
        self.assertIn("longitude=6.773", calls[0])
        hours = ctx.weather("düsseldorf", 0, 2**62)
        self.assertEqual(len(hours), 48)
        first = hours[1_791_734_400]
        self.assertEqual(
            (first.ts, first.temp, first.precip, first.sunshine, first.code),
            (1_791_734_400, 13.8, 0.0, 351.04, 3),
        )

    def test_weather_cache_within_one_hour(self):
        calls = []

        def http(url):
            calls.append(url)
            return load_fixture("openmeteo_duesseldorf.json")

        ctx = self.make_context(http)
        loc = locate("EVO Unterbilk", "Düsseldorf")
        ctx.refresh_weather(loc)
        res = ctx.refresh_weather(loc)  # 0 s spaeter: aus dem Cache
        self.assertTrue(res["cached"])
        self.assertEqual(len(calls), 1)
        self.clock["t"] += 3601
        ctx.refresh_weather(loc)
        self.assertEqual(len(calls), 2)


class SchoolHolidayTests(ContextTestCase):
    def test_school_holidays(self):
        ctx = self.make_context(lambda url: load_fixture("openholidays_nw_2026.json"))
        res = ctx.refresh_school_holidays("NW", 2026)
        self.assertTrue(res["ok"])
        self.assertEqual(res["n"], 6)
        self.assertEqual(ctx.school_holiday("NW", date(2026, 4, 1)), "Osterferien")
        self.assertEqual(ctx.school_holiday("NW", date(2026, 3, 30)), "Osterferien")  # Startdatum
        self.assertEqual(ctx.school_holiday("NW", date(2026, 4, 11)), "Osterferien")  # Enddatum
        self.assertIsNone(ctx.school_holiday("NW", date(2026, 4, 12)))
        self.assertEqual(ctx.school_holiday("NW", date(2026, 12, 23)), "Weihnachtsferien")
        self.assertIsNone(ctx.school_holiday("HH", date(2026, 4, 1)))

    def test_school_cache_within_a_week(self):
        calls = []

        def http(url):
            calls.append(url)
            return load_fixture("openholidays_nw_2026.json")

        ctx = self.make_context(http)
        ctx.refresh_school_holidays("NW", 2026)
        res = ctx.refresh_school_holidays("NW", 2026)
        self.assertTrue(res["cached"])
        self.assertEqual(len(calls), 1)
        self.clock["t"] += 8 * 86400
        ctx.refresh_school_holidays("NW", 2026)
        self.assertEqual(len(calls), 2)


class FailureTests(ContextTestCase):
    def test_failure_keeps_cache_and_is_reported(self):
        good = lambda url: load_fixture("openmeteo_duesseldorf.json")  # noqa: E731
        ctx = self.make_context(good)
        loc = locate("EVO Unterbilk", "Düsseldorf")
        ctx.refresh_weather(loc)
        self.assertEqual(len(ctx.weather("düsseldorf", 0, 2**62)), 48)

        def boom(url):
            raise RuntimeError("Netz kaputt")

        ctx._http = boom
        self.clock["t"] += 3601
        res = ctx.refresh_weather(loc)
        self.assertFalse(res["ok"])
        self.assertIn("Netz kaputt", res["error"])
        row = [r for r in ctx.status() if r["source"] == "weather:düsseldorf"][0]
        self.assertEqual(row["ok"], 0)
        self.assertIn("Netz kaputt", row["error"])
        self.assertEqual(len(ctx.weather("düsseldorf", 0, 2**62)), 48)  # Cache bleibt lesbar

    def test_no_retry_within_ten_minutes(self):
        calls = []

        def boom(url):
            calls.append(url)
            raise RuntimeError("down")

        ctx = self.make_context(boom)
        loc = locate("EVO Unterbilk", "Düsseldorf")
        ctx.refresh_weather(loc)
        self.clock["t"] += 500  # Fehler ist noch frisch: kein erneuter Versuch
        res = ctx.refresh_weather(loc)
        self.assertFalse(res["ok"])
        self.assertEqual(len(calls), 1)
        self.clock["t"] += 601  # jetzt aelter als 10 Minuten
        ctx.refresh_weather(loc)
        self.assertEqual(len(calls), 2)


class RefreshForGymsTests(ContextTestCase):
    def test_deduplicates_locations_and_states(self):
        urls = []

        def http(url):
            urls.append(url)
            if "open-meteo" in url:
                return load_fixture("openmeteo_duesseldorf.json")
            return load_fixture("openholidays_nw_2026.json")

        ctx = self.make_context(http)
        res = ctx.refresh_for_gyms(["EVO Unterbilk", "EVO LeFlair", "EVO Schanze"])
        weather_urls = [u for u in urls if "open-meteo" in u]
        holiday_urls = [u for u in urls if "openholidaysapi" in u]
        self.assertEqual(len(weather_urls), 2)  # Düsseldorf + Hamburg, dedupliziert
        self.assertTrue(any("latitude=51.227" in u for u in weather_urls))
        self.assertTrue(any("latitude=53.551" in u for u in weather_urls))
        self.assertEqual(len(holiday_urls), 2)  # NW + HH, je ein Jahr
        self.assertTrue(any("DE-NW" in u for u in holiday_urls))
        self.assertTrue(any("DE-HH" in u for u in holiday_urls))
        self.assertTrue(all(r["ok"] for r in res["weather"].values()))
        self.assertEqual(set(res["weather"]), {"düsseldorf", "hamburg"})
        self.assertEqual(set(res["school"]), {"NW:2026", "HH:2026"})


if __name__ == "__main__":
    unittest.main()
