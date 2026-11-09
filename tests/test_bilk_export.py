"""Tests fuer den Bilk-Export: Tagesplan, Stundenzeilen, Belegungsbloecke und Dateien."""

import json
import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

from evo import bilk, bilk_export

FIXTURES = Path(__file__).parent / "fixtures"
TZ = ZoneInfo("Europe/Berlin")
NOW = 1794216960  # 2026-11-09 10:36 Europe/Berlin


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


def prepare(directory: Path) -> None:
    tickets = bilk.parse_tickets(load_fixture("bilk_tickets.json"))
    slots, days = bilk.parse_lanes(bilk.decode_sveltekit(load_fixture("bilk_lanes_data.json")))
    bilk.store_tickets(directory, tickets, NOW, TZ)
    bilk.store_lanes(directory, slots, NOW)
    bilk.store_days(directory, days, NOW)


def by_start(day: dict, start: str) -> dict:
    return next(row for row in day["rows"] if row["start"] == start)


@unittest.skipUnless((FIXTURES / "bilk_tickets.json").exists(), "Bilk-Fixtures fehlen")
class PayloadTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        directory = Path(cls.tmp.name)
        prepare(directory)
        cls.payload = bilk_export.build_payload(directory, NOW, TZ)
        cls.days = {d["date"]: d for d in cls.payload["days"]}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_three_days(self):
        self.assertEqual([d["date"] for d in self.payload["days"]],
                         ["2026-11-09", "2026-11-10", "2026-11-11"])
        self.assertEqual(self.days["2026-11-09"]["label"], "Mo., 09.11.")
        self.assertEqual(self.days["2026-11-10"]["label"], "Di., 10.11.")
        unknown = self.days["2026-11-11"]
        self.assertEqual(unknown["status"], "unbekannt")
        self.assertEqual((unknown["open_start"], unknown["open_end"], unknown["note"]), ("", "", ""))
        self.assertEqual(unknown["rows"], [])
        self.assertEqual(unknown["blocks"], [])
        self.assertIsNone(unknown["best"])

    def test_meta(self):
        self.assertEqual(self.payload["generated_at"], NOW)
        self.assertEqual(self.payload["name"], "Schwimm' in Bilk")
        self.assertEqual(self.payload["tickets_ts"], NOW)
        self.assertEqual(self.payload["lanes_ts"], NOW)

    def test_hour_rows(self):
        day = self.days["2026-11-09"]
        self.assertEqual(day["status"], "geöffnet")
        self.assertEqual(len(day["rows"]), 15)
        self.assertEqual((day["rows"][0]["start"], day["rows"][0]["end"]), ("06:30", "07:00"))
        self.assertEqual((day["rows"][-1]["start"], day["rows"][-1]["end"]), ("20:00", "21:00"))
        first = day["rows"][0]
        self.assertEqual(len(first["quarters"]), 2)
        self.assertEqual(first["avg_free"], 6.0)
        self.assertEqual(first["min_free"], 6)
        self.assertIsNone(first["tickets"])
        self.assertTrue(first["past"])
        self.assertFalse(first["now"])

    def test_current_hour(self):
        row = by_start(self.days["2026-11-09"], "10:00")
        self.assertEqual((row["end"], row["now"], row["past"]), ("11:00", True, False))
        self.assertEqual([q["free"] for q in row["quarters"]], [4, 4, 4, 4])
        self.assertEqual(row["tickets"], {"total": 75, "available": 59, "sold": 16,
                                          "price_cents": 640, "tier": 3})

    def test_afternoon_row(self):
        row = by_start(self.days["2026-11-09"], "16:00")
        self.assertEqual([q["free"] for q in row["quarters"]], [3, 3, 3, 4])
        self.assertEqual((row["min_free"], row["avg_free"]), (3, 3.25))
        self.assertEqual(row["tickets"]["sold"], 1)
        self.assertEqual(row["tickets"]["tier"], 1)

    def test_evening_rows(self):
        row = by_start(self.days["2026-11-09"], "19:00")
        self.assertEqual((row["tickets"]["sold"], row["tickets"]["tier"]), (6, 2))
        last = by_start(self.days["2026-11-09"], "20:00")
        self.assertIsNone(last["tickets"])
        self.assertEqual(last["avg_free"], 4.5)

    def test_best(self):
        best = self.days["2026-11-09"]["best"]
        self.assertEqual((best["start"], best["end"]), ("11:00", "12:00"))
        self.assertEqual((best["avg_free"], best["tickets"]["tier"], best["tickets"]["sold"]),
                         (5.5, 1, 1))

    def test_now(self):
        now = self.payload["now"]
        self.assertEqual(now["date"], "2026-11-09")
        self.assertEqual((now["lanes_free"], now["lanes_total"]), (4, 6))
        self.assertEqual(now["row"]["start"], "10:00")

    def test_blocks(self):
        blocks = self.days["2026-11-09"]["blocks"]
        expected = [
            {"becken": "Sportbecken Bilk", "start": "07:00", "end": "08:00",
             "lanes": "1", "label": "Externer Kurs", "who": "Gipfelkurs"},
            {"becken": "Sportbecken Bilk", "start": "16:15", "end": "20:45",
             "lanes": "1–2", "label": "Verein", "who": "FSD"},
            {"becken": "Sportbecken Bilk", "start": "16:00", "end": "16:45",
             "lanes": "6", "label": "Kurs", "who": "Schwimmtraining Bronze"},
            {"becken": "Kursbecken Bilk", "start": "11:00", "end": "11:30",
             "lanes": "1–2", "label": "Kurs", "who": "Aqua Gymnastik"},
        ]
        for block in expected:
            self.assertIn(block, blocks)
        sport = [b for b in blocks if b["becken"] == "Sportbecken Bilk"]
        self.assertEqual(len(sport), 10)
        self.assertEqual(blocks[0]["becken"].startswith("Sportbecken"), True)
        self.assertEqual([b["start"] for b in sport], sorted(b["start"] for b in sport))

    def test_tomorrow(self):
        day = self.days["2026-11-10"]
        first = by_start(day, "06:30")
        self.assertEqual((first["tickets"]["total"], first["tickets"]["sold"],
                          first["tickets"]["tier"]), (50, 4, 2))
        best = day["best"]
        self.assertEqual(best["start"], "06:30")
        self.assertEqual(best["avg_free"], 6.0)
        self.assertFalse(first["past"])


class EmptyDirectoryTest(unittest.TestCase):
    def test_no_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = bilk_export.build_payload(Path(tmp), NOW, TZ)
        self.assertEqual([d["status"] for d in payload["days"]],
                         ["unbekannt", "unbekannt", "unbekannt"])
        self.assertIsNone(payload["now"]["row"])
        self.assertIsNone(payload["tickets_ts"])
        self.assertIsNone(payload["lanes_ts"])


class ExportTest(unittest.TestCase):
    def test_writes_json_and_html(self):
        with tempfile.TemporaryDirectory() as data, tempfile.TemporaryDirectory() as out, \
                tempfile.TemporaryDirectory() as web:
            prepare(Path(data))
            web_dir = Path(web)
            (web_dir / "index.html").write_text(
                "<html><body><script>console.log(1);</script></body></html>", encoding="utf-8")
            (web_dir / "manifest.webmanifest").write_text('{"name": "Bilk"}', encoding="utf-8")
            res = bilk_export.export_bilk(Path(data), Path(out), now_ts=NOW, web_dir=web_dir)
            target = Path(out) / "bilk"
            self.assertEqual(res["days"], 3)
            payload = json.loads((target / "api" / "bilk.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["generated_at"], NOW)
            html = (target / "index.html").read_text(encoding="utf-8")
            self.assertIn("window.EVO_STATIC", html)
            self.assertTrue((target / "manifest.webmanifest").is_file())

    def test_without_index_only_json(self):
        with tempfile.TemporaryDirectory() as data, tempfile.TemporaryDirectory() as out, \
                tempfile.TemporaryDirectory() as web:
            prepare(Path(data))
            bilk_export.export_bilk(Path(data), Path(out), now_ts=NOW, web_dir=Path(web))
            target = Path(out) / "bilk"
            self.assertTrue((target / "api" / "bilk.json").is_file())
            self.assertFalse((target / "index.html").exists())


if __name__ == "__main__":
    unittest.main()
