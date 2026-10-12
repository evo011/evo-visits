import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from evo.config import Config, in_active_window
from evo.csvstore import HEADER, export_monthly, import_dir
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
