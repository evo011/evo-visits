"""Tests fuer Schwimm' in Bilk: Ticketshop, SvelteKit-Belegungsplan und Monats-CSVs."""

import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from evo import bilk, fetch
from evo.config import Config

FIXTURES = Path(__file__).parent / "fixtures"
TZ = ZoneInfo("Europe/Berlin")
NOW = 1794216000  # 2026-11-09 10:20 Europe/Berlin


def make_cfg() -> Config:
    return Config()


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


@unittest.skipUnless((FIXTURES / "bilk_tickets.json").exists(), "Bilk-Fixtures fehlen")
class TicketTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = load_fixture("bilk_tickets.json")
        cls.windows = bilk.parse_tickets(cls.raw)

    def test_count_and_first_window(self):
        self.assertEqual(len(self.windows), 43)
        first = self.windows[0]
        self.assertEqual((first.start, first.end), ("2026-11-09 10:00", "2026-11-09 11:00"))
        self.assertEqual((first.total, first.available, first.price_cents), (75, 59, 640))
        self.assertEqual(first.sold, 16)
        self.assertEqual(first.tier, 3)

    def test_tuesday_morning_window(self):
        window = next(w for w in self.windows if w.start == "2026-11-10 06:30")
        self.assertEqual((window.total, window.available, window.price_cents), (50, 46, 544))
        self.assertEqual(window.tier, 2)
        self.assertEqual(window.sold, 4)

    def test_sorted_by_start(self):
        starts = [w.start for w in self.windows]
        self.assertEqual(starts, sorted(starts))

    def test_invalid_entries_are_skipped(self):
        valid = {"start": "2026-11-09 10:00", "end": "2026-11-09 11:00",
                 "totalAvailable": 10, "currentlyAvailable": 3, "price": 480}
        bad = [
            "murks",
            None,
            [1, 2],
            {**valid, "start": "09.11.2026 10:00"},
            {**valid, "end": "2026-11-09T11:00"},
            {**valid, "totalAvailable": "10"},
            {**valid, "currentlyAvailable": True},
            {**valid, "price": 4.8},
            {"start": valid["start"], "end": valid["end"]},
        ]
        out = bilk.parse_tickets([valid] + bad)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].available, 3)
        self.assertEqual(bilk.parse_tickets(None), [])
        self.assertEqual(bilk.parse_tickets({"start": "x"}), [])
        self.assertEqual(bilk.parse_tickets([]), [])

    def test_sold_never_negative(self):
        window = bilk.TicketWindow("2026-11-09 10:00", "2026-11-09 11:00", 5, 7, 999)
        self.assertEqual(window.sold, 0)
        self.assertIsNone(window.tier)


@unittest.skipUnless((FIXTURES / "bilk_tickets.json").exists(), "Bilk-Fixtures fehlen")
class TicketsUrlTest(unittest.TestCase):
    def test_expected_query(self):
        url = bilk.tickets_url(NOW)
        self.assertIn('articles=%5B%7B%22id%22%3A%221834%22%2C%22quantity%22%3A1%7D%5D', url)
        self.assertIn("start=2026-11-09T09%3A20%3A00.000Z", url)
        self.assertIn("end=2026-11-13T09%3A20%3A00.000Z", url)

    def test_days_parameter(self):
        self.assertIn("end=2026-11-12T09%3A20%3A00.000Z", bilk.tickets_url(NOW, days=3))


class DecodeSvelteKitTest(unittest.TestCase):
    def test_handmade_example(self):
        payload = {"nodes": [
            {"type": "skip"},
            {"type": "data", "data": [
                {"a": 1, "b": 2, "undef": -1, "undef2": -2, "nan": -3, "inf": -4,
                 "ninf": -5, "neg": -6, "date": 3, "other": 4, "list": 5, "list2": 5},
                "hello",
                42,
                ["Date", "2026-11-09T10:00:00.000Z"],
                ["Foo", 1],
                [1, 2],
            ]},
        ]}
        decoded = bilk.decode_sveltekit(payload)
        self.assertEqual(decoded["a"], "hello")
        self.assertEqual(decoded["b"], 42)
        self.assertIsNone(decoded["undef"])
        self.assertIsNone(decoded["undef2"])
        self.assertTrue(math.isnan(decoded["nan"]))
        self.assertEqual(decoded["date"], "2026-11-09T10:00:00.000Z")
        self.assertIsNone(decoded["other"])
        self.assertEqual(decoded["list"], ["hello", 42])
        self.assertIs(decoded["list"], decoded["list2"])
        self.assertEqual(decoded["inf"], float("inf"))
        self.assertEqual(decoded["ninf"], float("-inf"))
        self.assertEqual(decoded["neg"], -0.0)
        self.assertEqual(math.copysign(1.0, decoded["neg"]), -1.0)

    def test_last_data_node_wins(self):
        payload = {"nodes": [
            {"type": "data", "data": [{"x": 1}, "erste"]},
            {"type": "skip"},
            {"type": "data", "data": [{"x": 1}, "zweite"]},
        ]}
        self.assertEqual(bilk.decode_sveltekit(payload), {"x": "zweite"})

    def test_missing_data_node_raises(self):
        with self.assertRaises(fetch.FetchError) as ctx:
            bilk.decode_sveltekit({"nodes": [{"type": "skip"}]})
        self.assertTrue(str(ctx.exception).startswith("Belegungsplan: "))

    def test_index_out_of_range_raises(self):
        with self.assertRaises(fetch.FetchError) as ctx:
            bilk.decode_sveltekit({"nodes": [{"type": "data", "data": [{"x": 99}]}]})
        self.assertTrue(str(ctx.exception).startswith("Belegungsplan: "))


@unittest.skipUnless((FIXTURES / "bilk_lanes_data.json").exists(), "Bilk-Fixtures fehlen")
class LanesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.decoded = bilk.decode_sveltekit(load_fixture("bilk_lanes_data.json"))
        cls.slots, cls.days = bilk.parse_lanes(cls.decoded)

    def slot(self, becken: str, date: str, start: str) -> bilk.LaneSlot:
        return next(s for s in self.slots
                    if s.becken == becken and s.date == date and s.slot == start)

    def test_slot_count(self):
        self.assertEqual(len(self.slots), 232)
        self.assertEqual(len(self.days), 2)

    def test_becken_names_normalized(self):
        names = {s.becken for s in self.slots}
        self.assertEqual(names, {"Sportbecken Bilk", "Kursbecken Bilk"})

    def test_sportbecken_free_slot(self):
        slot = self.slot("Sportbecken Bilk", "2026-11-09", "06:30")
        self.assertEqual((slot.lanes_total, slot.lanes_free), (6, 6))
        self.assertEqual(slot.bookings, "")

    def test_sportbecken_single_booking(self):
        slot = self.slot("Sportbecken Bilk", "2026-11-09", "07:00")
        self.assertEqual(slot.lanes_free, 5)
        self.assertEqual(slot.bookings, "1:externe-nutzer:Gipfelkurs")

    def test_sportbecken_mixed_bookings(self):
        slot = self.slot("Sportbecken Bilk", "2026-11-09", "16:00")
        self.assertEqual((slot.lanes_total, slot.lanes_free), (6, 3))
        self.assertEqual(slot.bookings,
                         "1:schulschwimmen:BK Bachstr.; 2:schulschwimmen:BK Bachstr.; "
                         "6:bgd:Schwimmtraining Bronze")

    def test_booking_whitespace_normalized(self):
        slot = self.slot("Sportbecken Bilk", "2026-11-10", "13:15")
        self.assertEqual(slot.lanes_free, 4)
        self.assertEqual(slot.bookings,
                         "1:ogs:OGS KGS Im Dahlacker; 6:ogs:OGS KGS Im Dahlacker")

    def test_kursbecken_booking(self):
        slot = self.slot("Kursbecken Bilk", "2026-11-09", "16:00")
        self.assertEqual((slot.lanes_total, slot.lanes_free), (2, 1))
        self.assertEqual(slot.bookings, "2:kita:Kinderladen e.V")

    def test_days(self):
        self.assertEqual([d.date for d in self.days], ["2026-11-09", "2026-11-10"])
        for day in self.days:
            self.assertEqual((day.status, day.open_start, day.open_end, day.note),
                             ("geöffnet", "06:30", "21:00", ""))

    def test_other_standort(self):
        slots, days = bilk.parse_lanes(self.decoded, slug="strandbad-loerick")
        self.assertEqual(slots, [])
        self.assertEqual(len(days), 2)
        for day in days:
            self.assertEqual(day.status, "geschlossen")
            self.assertEqual((day.open_start, day.open_end), ("", ""))
            self.assertEqual(day.note, "Die Freibadsaison im Strandbad Lörick ist beendet.")

    def test_unknown_standort_raises(self):
        with self.assertRaises(fetch.FetchError):
            bilk.parse_lanes(self.decoded, slug="gibt-es-nicht")


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def window(self, available=59, start="2026-11-09 10:00"):
        return bilk.TicketWindow(start, "2026-11-09 11:00", 75, available, 640)

    def test_tickets_only_on_change(self):
        first = bilk.store_tickets(self.dir, [self.window()], NOW, TZ)
        self.assertEqual(first, 1)
        second = bilk.store_tickets(self.dir, [self.window()], NOW, TZ)
        self.assertEqual(second, 0)
        changed = bilk.store_tickets(self.dir, [self.window(available=50)], NOW, TZ)
        self.assertEqual(changed, 1)

        text = (self.dir / "bilk-tickets-2026-11.csv").read_text()
        self.assertEqual(text.count("ts,local_time,window_start"), 1)
        self.assertEqual(len(text.strip().splitlines()), 3)
        latest = bilk.load_latest(self.dir, "tickets", ["2026-11"])
        self.assertEqual(latest["2026-11-09 10:00"]["available"], "50")

    def test_tickets_month_split(self):
        windows = [self.window(), bilk.TicketWindow("2026-12-01 08:00", "2026-12-01 09:00",
                                                    40, 40, 480)]
        bilk.store_tickets(self.dir, windows, NOW, TZ)
        self.assertTrue((self.dir / "bilk-tickets-2026-11.csv").exists())
        self.assertTrue((self.dir / "bilk-tickets-2026-12.csv").exists())

    def test_lanes_only_on_change(self):
        slot = bilk.LaneSlot("2026-11-09", "Sportbecken Bilk", "16:00", 6, 3, "1:x:Y")
        self.assertEqual(bilk.store_lanes(self.dir, [slot], NOW), 1)
        self.assertEqual(bilk.store_lanes(self.dir, [slot], NOW), 0)
        changed = bilk.LaneSlot("2026-11-09", "Sportbecken Bilk", "16:00", 6, 4, "")
        self.assertEqual(bilk.store_lanes(self.dir, [changed], NOW), 1)
        latest = bilk.load_latest(self.dir, "lanes", ["2026-11"])
        self.assertEqual(latest[("2026-11-09", "Sportbecken Bilk", "16:00")]["lanes_free"], "4")

    def test_days_only_on_change(self):
        day = bilk.DayInfo("2026-11-09", "geöffnet", "06:30", "21:00", "")
        self.assertEqual(bilk.store_days(self.dir, [day], NOW), 1)
        self.assertEqual(bilk.store_days(self.dir, [day], NOW), 0)
        closed = bilk.DayInfo("2026-11-09", "geschlossen", "", "", "")
        self.assertEqual(bilk.store_days(self.dir, [closed], NOW), 1)
        latest = bilk.load_latest(self.dir, "days", ["2026-11"])
        self.assertEqual(latest["2026-11-09"]["status"], "geschlossen")

    def test_load_latest_skips_missing_months(self):
        self.assertEqual(bilk.load_latest(self.dir, "days", ["2026-01", "2026-02"]), {})


class LanesDueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def ts(self, hour: int, minute: int) -> int:
        # 2026-11-09 in Europe/Berlin (MEZ, UTC+1)
        from datetime import datetime
        dt = datetime(2026, 11, 9, hour, minute, tzinfo=TZ)
        return int(dt.timestamp())

    def test_first_quarter_hour(self):
        self.assertTrue(bilk.lanes_due(self.dir, self.ts(10, 5), TZ))
        self.assertTrue(bilk.lanes_due(self.dir, self.ts(10, 0), TZ))

    def test_missing_day_row(self):
        self.assertTrue(bilk.lanes_due(self.dir, self.ts(10, 30), TZ))

    def test_day_row_present(self):
        bilk.store_days(self.dir, [bilk.DayInfo("2026-11-09", "geöffnet", "06:30", "21:00", "")],
                        self.ts(10, 0))
        self.assertFalse(bilk.lanes_due(self.dir, self.ts(10, 30), TZ))
        self.assertFalse(bilk.lanes_due(self.dir, self.ts(23, 45), TZ))


@unittest.skipUnless((FIXTURES / "bilk_tickets.json").exists()
                     and (FIXTURES / "bilk_lanes_data.json").exists(), "Bilk-Fixtures fehlen")
class CollectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.tickets_text = (FIXTURES / "bilk_tickets.json").read_text()
        self.lanes_text = (FIXTURES / "bilk_lanes_data.json").read_text()
        self.cfg = make_cfg()

    def tearDown(self):
        self.tmp.cleanup()

    def fake_get(self, url, cfg):
        if "fullcalendar-data" in url:
            return "application/json", self.tickets_text
        if "belegungsplan" in url:
            return "application/json", self.lanes_text
        raise AssertionError(f"unerwartete URL: {url}")

    def test_collect_writes_and_is_idempotent(self):
        with mock.patch.object(bilk, "http_get", side_effect=self.fake_get):
            res = bilk.collect(self.cfg, self.dir, now_ts=NOW, force_lanes=True)
        self.assertEqual(res["windows"], 43)
        self.assertEqual(res["tickets_new"], 43)
        self.assertEqual(res["lanes"], 232)
        self.assertEqual(res["lanes_new"], 232)
        self.assertEqual(res["days_new"], 2)
        self.assertTrue((self.dir / "bilk-tickets-2026-11.csv").exists())
        self.assertTrue((self.dir / "bilk-lanes-2026-11.csv").exists())
        self.assertTrue((self.dir / "bilk-days-2026-11.csv").exists())

        with mock.patch.object(bilk, "http_get", side_effect=self.fake_get):
            res = bilk.collect(self.cfg, self.dir, now_ts=NOW)
        self.assertEqual(res["tickets_new"], 0)
        self.assertIsNone(res["lanes"])
        self.assertIsNone(res["lanes_new"])

    def test_tickets_survive_lane_failure(self):
        def fake_get(url, cfg):
            if "fullcalendar-data" in url:
                return "application/json", self.tickets_text
            raise fetch.FetchError("Belegungsplan kaputt")

        with mock.patch.object(bilk, "http_get", side_effect=fake_get):
            with self.assertRaises(fetch.FetchError):
                bilk.collect(self.cfg, self.dir, now_ts=NOW, force_lanes=True)

        path = self.dir / "bilk-tickets-2026-11.csv"
        self.assertTrue(path.exists())
        self.assertEqual(len(path.read_text().strip().splitlines()), 44)


if __name__ == "__main__":
    unittest.main()
