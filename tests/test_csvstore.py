import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from evo.config import Config, in_active_window
from evo.context import SCHEMA
from evo.csvstore import (HEADER, WEATHER_HEADER, export_monthly, export_weather_monthly,
                          import_dir, import_weather_dir)
from evo.store import Reading, Store

TZ = ZoneInfo("Europe/Berlin")


def ts_utc(y, m, d, h, minute=0):
    return int(datetime(y, m, d, h, minute, tzinfo=ZoneInfo("UTC")).timestamp())


class CsvRoundtripTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name) / "csv"
        self.store = Store(str(Path(self.tmp.name) / "visits.db"))
        # 30.09.2026 23:30 UTC = 01.10.2026 01:30 in Europe/Berlin -> Monat 2026-10
        self.store.add_batch([
            (ts_utc(2026, 9, 30, 12, 0), Reading("Duesseldorf", 36, 80, None)),
            (ts_utc(2026, 9, 30, 23, 30), Reading("Koeln", 12, 40.5, None)),
            (ts_utc(2026, 10, 1, 8, 15), Reading("Koeln", 102.9, None, None)),
            (ts_utc(2026, 10, 1, 9, 0), Reading("Duesseldorf", 3, None, 45.5)),
        ])

    def test_export_two_files(self):
        changed = export_monthly(self.store, self.directory, TZ)
        self.assertEqual([p.name for p in changed], ["visits-2026-09.csv", "visits-2026-10.csv"])
        for name in ("visits-2026-09.csv", "visits-2026-10.csv"):
            self.assertTrue((self.directory / name).exists())

    def test_header_and_number_format(self):
        export_monthly(self.store, self.directory, TZ)
        content = (self.directory / "visits-2026-10.csv").read_text(encoding="utf-8")
        lines = content.split("\n")
        self.assertEqual(lines[0], ",".join(HEADER))
        rows = [r.split(",") for r in lines[1:] if r]
        self.assertEqual(
            rows,
            [
                # ts, local_time, gym, count, capacity, pct
                [str(ts_utc(2026, 9, 30, 23, 30)), "2026-10-01 01:30", "Koeln", "12", "40.5", ""],
                [str(ts_utc(2026, 10, 1, 8, 15)), "2026-10-01 10:15", "Koeln", "102.9", "", ""],
                [str(ts_utc(2026, 10, 1, 9, 0)), "2026-10-01 11:00", "Duesseldorf", "3", "", "45.5"],
            ],
        )

    def test_roundtrip_preserves_rows(self):
        export_monthly(self.store, self.directory, TZ)
        fresh = Store(str(Path(self.tmp.name) / "fresh.db"))
        n = import_dir(fresh, self.directory)
        self.assertEqual(n, 4)
        self.assertEqual(list(fresh.export_rows()), list(self.store.export_rows()))

    def test_second_export_unchanged(self):
        export_monthly(self.store, self.directory, TZ)
        before = (self.directory / "visits-2026-10.csv").read_bytes()
        changed = export_monthly(self.store, self.directory, TZ)
        self.assertEqual(changed, [])
        self.assertEqual((self.directory / "visits-2026-10.csv").read_bytes(), before)

    def test_import_missing_dir(self):
        self.assertEqual(import_dir(self.store, Path(self.tmp.name) / "gibt-es-nicht"), 0)

    def test_import_skips_broken_lines(self):
        d = Path(self.tmp.name) / "broken"
        d.mkdir()
        (d / "visits-2026-05.csv").write_text(
            "ts,local_time,gym,count,capacity,pct\n"
            "keine-zahl,2026-05-01 10:00,X,1,,\n"
            "1750000000,2026-05-01 10:00,Y,7,,\n",
            encoding="utf-8",
        )
        fresh = Store(str(Path(self.tmp.name) / "fresh.db"))
        self.assertEqual(import_dir(fresh, d), 1)
        rows = list(fresh.export_rows())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "Y")


class WeatherRoundtripTestCase(unittest.TestCase):
    """Wetter-CSVs: Export schliesst zukuenftige Stunden aus, Import ueberschreibt nichts."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name) / "csv"
        self.store = Store(str(Path(self.tmp.name) / "visits.db"))
        with self.store.conn() as c:
            c.executescript(SCHEMA)
        # 01.10.2026 12:00 und 13:00 UTC Berlin = vergangen, 15:00 UTC = Zukunft
        self.past = ts_utc(2026, 10, 1, 12, 0)
        self.past2 = ts_utc(2026, 10, 1, 13, 0)
        self.future = ts_utc(2026, 10, 1, 15, 0)
        self.now = ts_utc(2026, 10, 1, 14, 0)
        with self.store.conn() as c:
            c.executemany(
                "INSERT OR REPLACE INTO weather (loc, ts, temp, precip, sunshine, code, fetched_at) "
                "VALUES (?,?,?,?,?,?,?)",
                [
                    ("düsseldorf", self.past, 12.5, 0.0, 2400, 2, self.now - 600),
                    ("koeln", self.past, None, None, None, None, self.now - 600),
                    ("koeln", self.past2, 11.0, 0.3, None, 61, self.now - 600),
                    ("düsseldorf", self.future, 20.0, 0.0, 3600, 0, self.now - 600),
                ])

    def test_export_excludes_future_hours(self):
        changed = export_weather_monthly(self.store, self.directory, TZ, self.now)
        self.assertEqual([p.name for p in changed], ["weather-2026-10.csv"])
        content = (self.directory / "weather-2026-10.csv").read_text(encoding="utf-8")
        lines = content.split("\n")
        self.assertEqual(lines[0], ",".join(WEATHER_HEADER))
        rows = [r.split(",") for r in lines[1:] if r]
        # sortiert nach (ts, loc), leere Zellen fuer None, kompakte Zahlen
        self.assertEqual(
            rows,
            [
                [str(self.past), "2026-10-01 14:00", "düsseldorf", "12.5", "0", "2400", "2"],
                [str(self.past), "2026-10-01 14:00", "koeln", "", "", "", ""],
                [str(self.past2), "2026-10-01 15:00", "koeln", "11", "0.3", "", "61"],
            ])

    def test_roundtrip_into_fresh_store(self):
        export_weather_monthly(self.store, self.directory, TZ, self.now)
        fresh = Store(str(Path(self.tmp.name) / "fresh.db"))
        n = import_weather_dir(fresh, self.directory)
        self.assertEqual(n, 3)
        with fresh.conn() as c:
            rows = c.execute(
                "SELECT loc, ts, temp, precip, sunshine, code, fetched_at FROM weather "
                "ORDER BY ts, loc").fetchall()
        self.assertEqual(rows, [
            ("düsseldorf", self.past, 12.5, 0.0, 2400.0, 2, 0),
            ("koeln", self.past, None, None, None, None, 0),
            ("koeln", self.past2, 11.0, 0.3, None, 61, 0),
        ])

    def test_import_does_not_overwrite_existing_row(self):
        export_weather_monthly(self.store, self.directory, TZ, self.now)
        # frischere API-Zeile im Store, fetched_at != 0
        with self.store.conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO weather (loc, ts, temp, precip, sunshine, code, fetched_at) "
                "VALUES (?,?,?,?,?,?,?)",
                ("koeln", self.past, 99.0, 1.0, 100.0, 5, self.now))
        import_weather_dir(self.store, self.directory)
        with self.store.conn() as c:
            row = c.execute(
                "SELECT temp, fetched_at FROM weather WHERE loc='koeln' AND ts=?",
                (self.past,)).fetchone()
        self.assertEqual(row, (99.0, self.now))

    def test_second_export_unchanged(self):
        export_weather_monthly(self.store, self.directory, TZ, self.now)
        before = (self.directory / "weather-2026-10.csv").read_bytes()
        changed = export_weather_monthly(self.store, self.directory, TZ, self.now)
        self.assertEqual(changed, [])
        self.assertEqual((self.directory / "weather-2026-10.csv").read_bytes(), before)

    def test_import_missing_dir_and_schema_creation(self):
        # Verzeichnis fehlt -> 0
        self.assertEqual(import_weather_dir(self.store, Path(self.tmp.name) / "gibt-es-nicht"), 0)
        # Store ohne weather-Tabelle: Schema wird angelegt, Import klappt
        d = Path(self.tmp.name) / "weatherval"
        d.mkdir()
        (d / "weather-2026-05.csv").write_text(
            "ts,local_time,loc,temp,precip,sunshine,code\n"
            f"{self.past},2026-05-01 14:00,düsseldorf,3.5,,,7\n",
            encoding="utf-8")
        fresh = Store(str(Path(self.tmp.name) / "fresh2.db"))
        self.assertEqual(import_weather_dir(fresh, d), 1)
        with fresh.conn() as c:
            row = c.execute("SELECT loc, ts, temp, fetched_at FROM weather").fetchone()
        self.assertEqual(row, ("düsseldorf", self.past, 3.5, 0))


class ActiveWindowTestCase(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.cfg.active_hours = "05:30-23:30"
        self.cfg.timezone = "Europe/Berlin"

    def _ts(self, local_hhmm):
        h, m = local_hhmm.split(":")
        return datetime(2026, 10, 12, int(h), int(m), tzinfo=TZ).timestamp()

    def test_outside(self):
        self.assertFalse(in_active_window(self.cfg, self._ts("05:00")))

    def test_inside(self):
        self.assertTrue(in_active_window(self.cfg, self._ts("12:00")))

    def test_window_boundaries_0545_2315(self):
        cfg = Config()
        cfg.active_hours = "05:45-23:15"
        cfg.timezone = "Europe/Berlin"
        self.assertFalse(in_active_window(cfg, self._ts("05:44")))
        self.assertTrue(in_active_window(cfg, self._ts("05:45")))
        self.assertTrue(in_active_window(cfg, self._ts("23:15")))
        self.assertFalse(in_active_window(cfg, self._ts("23:16")))


class CollectWindowCliTestCase(unittest.TestCase):
    def test_collect_respects_window(self):
        import evo_visits

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config()
            cfg.db_path = str(Path(tmp) / "visits.db")
            with mock.patch("evo_visits.load_config", return_value=cfg), \
                 mock.patch("evo.config.in_active_window", return_value=False), \
                 mock.patch("evo.fetch.collect_once") as collect:
                code = evo_visits.main([
                    "collect", "--respect-window", "--active-hours", "00:00-00:01",
                    "--config", str(Path(tmp) / "gibt-es-nicht.json"),
                ])
        self.assertEqual(code, 0)
        collect.assert_not_called()
