import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from evo.snapshot import HEADER, snapshot_forecasts

TZ = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 10, 12, 12, 0, tzinfo=TZ)      # Montag
NOW_TS = int(NOW.timestamp())


class FakeStore:
    def gyms(self):
        return [{"gym": "EVO Alpha"}, {"gym": "EVO Beta"}]


class FakeApp:
    """Duck-typed App: zwei Clubs, 48 h Forecast im 15-Minuten-Raster."""

    def __init__(self, forecast_fn=None):
        self.store = FakeStore()
        self.tz = TZ
        self.forecast_fn = forecast_fn or self._default_forecast

    @staticmethod
    def _default_forecast(gym):
        # wie Model.forecast: ab dem naechsten 15-min-Step nach now, 48 h
        start = (NOW_TS // 900 + 1) * 900
        out = []
        for i in range(48 * 4):
            ts = start + 15 * 60 * i
            full_hour = ts % 3600 == 0
            est = 42.0 if full_hour else 7.0
            weather = ({"temp": 13.5, "precip": 0.2, "sun_min": 12, "code": 3}
                       if full_hour else None)
            out.append([ts * 1000, est, est - 2, est + 2, -5.0, weather])
        return out

    def api_gym(self, q):
        gym = q["gym"][0]
        return {"gym": gym, "forecast": self.forecast_fn(gym)}


class SnapshotTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name) / "forecasts"

    def read_rows(self, name="forecast-2026-10.csv"):
        content = (self.directory / name).read_text(encoding="utf-8")
        lines = [l for l in content.split("\n") if l]
        return lines[0], [r.split(",") for r in lines[1:]]

    def test_snapshot_writes_header_and_full_hours(self):
        app = FakeApp()
        path = snapshot_forecasts(app, self.directory, NOW_TS, model_version="abcd123")
        self.assertEqual(path, self.directory / "forecast-2026-10.csv")
        header, rows = self.read_rows()
        self.assertEqual(header, ",".join(HEADER))
        # 48 h ab 12:15: volle Stunden 06-23 -> heute 13-23 Uhr (11), 13.10. 06-23 (18),
        # 14.10. 06-12 Uhr (7) = 36 Punkte je Club
        self.assertEqual(len(rows), 36 * 2)
        # sortiert nach (gym, target_ts)
        self.assertEqual([r[2] for r in rows[:36]], ["EVO Alpha"] * 36)
        self.assertEqual([r[2] for r in rows[36:]], ["EVO Beta"] * 36)
        self.assertEqual([int(r[3]) for r in rows[:36]], sorted(int(r[3]) for r in rows[:36]))
        first = rows[0]
        # erste volle Stunde nach now ist heute 13:00 lokal
        self.assertEqual(first[0], NOW.date().isoformat())
        self.assertEqual(first[1], str(NOW_TS))
        self.assertEqual(first[4], "2026-10-12 13:00")
        self.assertEqual(first[5], "1.0")
        self.assertEqual(first[6], "42")
        self.assertEqual(first[7], "40")
        self.assertEqual(first[8], "44")
        self.assertEqual(first[9], "-5")
        self.assertEqual(first[10], "13.5")
        self.assertEqual(first[11], "0.2")
        self.assertEqual(first[12], "12")
        self.assertEqual(first[13], "3")
        self.assertEqual(first[14], "abcd123")

    def test_second_call_same_day_returns_none(self):
        app = FakeApp()
        snapshot_forecasts(app, self.directory, NOW_TS, model_version="abcd123")
        before = (self.directory / "forecast-2026-10.csv").read_bytes()
        self.assertIsNone(snapshot_forecasts(app, self.directory, NOW_TS + 3600))
        self.assertEqual((self.directory / "forecast-2026-10.csv").read_bytes(), before)

    def test_next_day_appends(self):
        app = FakeApp()
        snapshot_forecasts(app, self.directory, NOW_TS, model_version="abcd123")
        one_day = len(self.read_rows()[1])
        tomorrow = NOW_TS + 86400
        path = snapshot_forecasts(app, self.directory, tomorrow, model_version="abcd123")
        self.assertEqual(path, self.directory / "forecast-2026-10.csv")
        _, rows = self.read_rows()
        # gleiche Fake-Forecast (statisch): Zeilen haengen sich an, made_date unterscheidet sich
        self.assertEqual(len(rows), 2 * one_day)
        made_dates = {r[0] for r in rows}
        self.assertEqual(made_dates, {NOW.date().isoformat(),
                                      (NOW + timedelta(days=1)).date().isoformat()})

    def test_no_points_writes_nothing(self):
        def empty(gym):
            # est immer None -> keine Punkte
            day0 = datetime(2026, 10, 12, tzinfo=TZ)
            return [[int((day0 + timedelta(hours=i)).timestamp()) * 1000,
                     None, None, None, None, None] for i in range(48)]

        app = FakeApp(forecast_fn=empty)
        self.assertIsNone(snapshot_forecasts(app, self.directory, NOW_TS))
        self.assertFalse(self.directory.exists())

    def test_model_version_default_local(self):
        import os
        from unittest import mock

        app = FakeApp()
        with mock.patch.dict(os.environ, {}, clear=True):
            snapshot_forecasts(app, self.directory, NOW_TS)
        _, rows = self.read_rows()
        self.assertEqual({r[14] for r in rows}, {"local"})

    def test_model_version_from_github_sha(self):
        import os
        from unittest import mock

        app = FakeApp()
        with mock.patch.dict(os.environ, {"GITHUB_SHA": "abcdef1234567890"}):
            snapshot_forecasts(app, self.directory, NOW_TS)
        _, rows = self.read_rows()
        self.assertEqual({r[14] for r in rows}, {"abcdef1"})


if __name__ == "__main__":
    unittest.main()
