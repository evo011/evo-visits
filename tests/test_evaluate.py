import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from evo.evaluate import evaluate
from evo.snapshot import HEADER
from evo.store import Reading, Store

TZ = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 10, 13, 12, 0, tzinfo=TZ)
NOW_TS = int(NOW.timestamp())
DAY = 86400


def local_dt(y, m, d, h, minute=0):
    return datetime(y, m, d, h, minute, tzinfo=TZ)


def hour_ts(y, m, d, h):
    return int(local_dt(y, m, d, h).timestamp())


def write_csv(directory, rows, name="forecast-2026-10.csv"):
    lines = [",".join(HEADER)]
    for r in rows:
        lines.append(",".join(str(x) for x in r))
    (directory / name).write_text("\n".join(lines) + "\n", encoding="utf-8")


def snap_row(made_date, made_ts, gym, target_ts, est, lo, hi, model="abcd123"):
    target_local = datetime.fromtimestamp(target_ts, TZ).strftime("%Y-%m-%d %H:%M")
    lead_h = (target_ts - made_ts) / 3600
    return (made_date, made_ts, gym, target_ts, target_local, f"{lead_h:.1f}",
            est, lo, hi, "", "", "", "", "", model)


class EvaluateTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.snap_dir = Path(self.tmp.name) / "forecasts"
        self.snap_dir.mkdir()
        self.store = Store(str(Path(self.tmp.name) / "visits.db"))

    def add_samples(self, gym, ts_values):
        self.store.add_batch([(ts, Reading(gym, count=v)) for ts, v in ts_values])

    def test_metrics_per_group(self):
        # Gruppe same_day: 2 Zeilen am 13.10. gemacht, Ziel 13.10. 10:00/11:00
        # Gruppe 1d: 2 Zeilen am 12.10. gemacht, Ziel 13.10. 10:00/11:00
        t10 = hour_ts(2026, 10, 13, 10)
        t11 = hour_ts(2026, 10, 13, 11)
        made_today = NOW_TS - 3600                    # 13.10. 11:00
        made_yday = NOW_TS - 86400 - 3600             # 12.10. 11:00
        d_today, d_yday = "2026-10-13", "2026-10-12"

        # Messwerte im Zielfenster: t10 -> Mittel 20, t11 -> Mittel 30
        self.add_samples("EVO Alpha", [(t10, 10), (t10 + 600, 30), (t11, 25), (t11 + 600, 35)])
        # Vorwochenwerte nur fuer t10 (Mittel 18), fuer t11 keine
        self.add_samples("EVO Alpha", [(t10 - 7 * DAY, 18), (t10 - 7 * DAY + 600, 18)])

        rows = [
            # same_day: est 22/28 -> |err| 2/2, bias -2/+2 -> 0, Band 20<=21<=24, 26<=30<=32
            snap_row(d_today, made_today, "EVO Alpha", t10, 22, 20, 24),
            snap_row(d_today, made_today, "EVO Alpha", t11, 28, 26, 32),
            # 1d: est 25/34 -> |err| 5/4, bias +5/+4 -> 4.5, Band: 30 ausserhalb, 33 drin
            snap_row(d_yday, made_yday, "EVO Alpha", t10, 25, 20, 28),
            snap_row(d_yday, made_yday, "EVO Alpha", t11, 34, 33, 40),
        ]
        write_csv(self.snap_dir, rows)

        res = evaluate(self.snap_dir, self.store, TZ, NOW_TS)
        self.assertEqual(res["days"], 2)
        self.assertEqual(res["first_made"], "2026-10-12")
        self.assertEqual(res["buckets"], ["same_day", "1d", "2_3d", "4_7d"])

        sd = res["all"]["same_day"]
        self.assertEqual(sd["n"], 2)
        self.assertEqual(sd["mae"], 2.0)
        self.assertEqual(sd["bias"], 0.0)
        self.assertEqual(sd["coverage_pct"], 100)
        # Vorwoche nur fuer die t10-Zeile: ist 20, Vorwochenwert 18 -> |18-20| = 2.
        # Die falsche Formel |22-18| = 4 wuerde hier einen anderen Wert liefern.
        self.assertEqual(sd["n_last_week"], 1)
        self.assertEqual(sd["mae_last_week"], 2.0)
        self.assertEqual(sd["mae_model_same"], 2.0)

        one = res["all"]["1d"]
        self.assertEqual(one["n"], 2)
        self.assertEqual(one["mae"], 4.5)
        self.assertEqual(one["bias"], 4.5)
        self.assertEqual(one["coverage_pct"], 50)
        # ist 20, Vorwochenwert 18 -> |18-20| = 2 (nicht |25-18| = 7),
        # Modellfehler auf derselben Zeile: |25-20| = 5
        self.assertEqual(one["n_last_week"], 1)
        self.assertEqual(one["mae_last_week"], 2.0)
        self.assertEqual(one["mae_model_same"], 5.0)

        self.assertNotIn("2_3d", res["all"])
        self.assertNotIn("4_7d", res["all"])
        self.assertEqual(res["by_gym"]["EVO Alpha"]["same_day"]["n"], 2)
        self.assertEqual(res["by_model"]["abcd123"]["1d"]["mae"], 4.5)

    def test_two_gyms_and_two_snapshot_days(self):
        t = hour_ts(2026, 10, 13, 10)
        made_today = NOW_TS - 2 * 3600              # 13.10. 10:00 -> same_day
        made_yday = NOW_TS - 86400 - 2 * 3600       # 12.10. 10:00 -> 1d
        d_today, d_yday = "2026-10-13", "2026-10-12"
        self.add_samples("EVO Alpha", [(t, 40)])
        self.add_samples("EVO Beta", [(t, 80)])
        # Vorwoche fuer beide
        self.add_samples("EVO Alpha", [(t - 7 * DAY, 44)])
        self.add_samples("EVO Beta", [(t - 7 * DAY, 70)])
        write_csv(self.snap_dir, [
            snap_row(d_today, made_today, "EVO Alpha", t, 41, 40, 44),
            snap_row(d_today, made_today, "EVO Beta", t, 82, 76, 84),
            snap_row(d_yday, made_yday, "EVO Alpha", t, 42, 40, 44),
            snap_row(d_yday, made_yday, "EVO Beta", t, 78, 76, 84),
        ])
        res = evaluate(self.snap_dir, self.store, TZ, NOW_TS)
        self.assertEqual(res["days"], 2)
        self.assertEqual(res["first_made"], "2026-10-12")
        a_sd = res["by_gym"]["EVO Alpha"]["same_day"]
        a_1d = res["by_gym"]["EVO Alpha"]["1d"]
        # Alpha same_day: est 41, ist 40, Vorwoche 44 -> Vorwochenfehler |44-40| = 4 (nicht |41-44| = 3)
        self.assertEqual(a_sd["mae"], 1.0)
        self.assertEqual(a_sd["bias"], 1.0)
        self.assertEqual(a_sd["mae_last_week"], 4.0)
        self.assertEqual(a_sd["mae_model_same"], 1.0)
        # Alpha 1d: est 42, ist 40, Vorwoche 44 -> |44-40| = 4 (nicht |42-44| = 2)
        self.assertEqual(a_1d["mae"], 2.0)
        self.assertEqual(a_1d["bias"], 2.0)
        self.assertEqual(a_1d["mae_last_week"], 4.0)
        self.assertEqual(a_1d["mae_model_same"], 2.0)
        # Beta same_day: est 82, ist 80, Vorwoche 70 -> |70-80| = 10 (nicht |82-70| = 12)
        b_sd = res["by_gym"]["EVO Beta"]["same_day"]
        self.assertEqual(b_sd["mae"], 2.0)
        self.assertEqual(b_sd["bias"], 2.0)
        self.assertEqual(b_sd["mae_last_week"], 10.0)
        # Beta 1d: est 78, ist 80, Vorwoche 70 -> |70-80| = 10 (nicht |78-70| = 8)
        b_1d = res["by_gym"]["EVO Beta"]["1d"]
        self.assertEqual(b_1d["mae"], 2.0)
        self.assertEqual(b_1d["bias"], -2.0)
        self.assertEqual(b_1d["mae_last_week"], 10.0)
        # gesamt: same_day (1+2)/2 = 1.5, 1d (2+2)/2 = 2.0
        self.assertEqual(res["all"]["same_day"]["mae"], 1.5)
        self.assertEqual(res["all"]["same_day"]["n"], 2)
        self.assertEqual(res["all"]["1d"]["mae"], 2.0)
        self.assertEqual(res["all"]["1d"]["n"], 2)

    def test_future_and_missing_ist_skipped(self):
        t_past = hour_ts(2026, 10, 13, 10)
        t_future = NOW_TS + 2 * 3600 + 1800   # Ziel endet erst nach now
        t_no_data = hour_ts(2026, 10, 13, 11)  # kein Messwert im Store
        made_ts = NOW_TS - 3600
        self.add_samples("EVO Alpha", [(t_past, 20)])
        write_csv(self.snap_dir, [
            snap_row("2026-10-13", made_ts, "EVO Alpha", t_past, 22, 20, 24),
            snap_row("2026-10-13", made_ts, "EVO Alpha", t_future, 22, 20, 24),
            snap_row("2026-10-13", made_ts, "EVO Alpha", t_no_data, 22, 20, 24),
        ])
        res = evaluate(self.snap_dir, self.store, TZ, NOW_TS)
        self.assertEqual(res["all"]["same_day"]["n"], 1)

    def test_last_week_only_where_samples_exist(self):
        # Alpha hat Vorwochenwert, Beta nicht
        t = hour_ts(2026, 10, 13, 10)
        made_ts = NOW_TS - 3600
        self.add_samples("EVO Alpha", [(t, 20), (t - 7 * DAY, 30)])
        self.add_samples("EVO Beta", [(t, 50)])
        write_csv(self.snap_dir, [
            snap_row("2026-10-13", made_ts, "EVO Alpha", t, 22, 20, 24),
            snap_row("2026-10-13", made_ts, "EVO Beta", t, 48, 46, 50),
        ])
        res = evaluate(self.snap_dir, self.store, TZ, NOW_TS)
        a = res["by_gym"]["EVO Alpha"]["same_day"]
        b = res["by_gym"]["EVO Beta"]["same_day"]
        self.assertEqual(a["n_last_week"], 1)
        # ist 20, Vorwoche 30 -> |30-20| = 10 (falsche Formel |22-30| = 8)
        self.assertEqual(a["mae_last_week"], 10.0)
        self.assertEqual(a["mae_model_same"], 2.0)
        self.assertEqual(b["n_last_week"], 0)
        self.assertNotIn("mae_last_week", b)
        self.assertNotIn("mae_model_same", b)

    def test_missing_directory_empty_result(self):
        res = evaluate(Path(self.tmp.name) / "gibtsnicht", self.store, TZ, NOW_TS)
        self.assertEqual(res, {"generated_at": NOW_TS, "days": 0, "first_made": None,
                              "buckets": ["same_day", "1d", "2_3d", "4_7d"],
                              "all": {}, "by_gym": {}, "by_model": {}})

    def test_bucket_assignment(self):
        # Ziel 13.10. 10:00 (Vergangenheit); made 13.10 -> same_day, 12.10 -> 1d,
        # 11./10.10 -> 2_3d, 9. und 6.10 -> 4_7d (ab 4 Tagen Abstand)
        t = hour_ts(2026, 10, 13, 10)
        self.add_samples("EVO Alpha", [(t, 20)])
        rows = []
        for day_offset in (0, 1, 2, 3, 4, 7):
            made_d = (NOW - timedelta(days=day_offset)).date()
            made_ts = t - day_offset * DAY - 3600
            rows.append(snap_row(made_d.isoformat(), made_ts, "EVO Alpha", t, 22, 20, 24))
        write_csv(self.snap_dir, rows)
        res = evaluate(self.snap_dir, self.store, TZ, NOW_TS)
        counts = {g: m["n"] for g, m in res["all"].items()}
        self.assertEqual(counts, {"same_day": 1, "1d": 1, "2_3d": 2, "4_7d": 2})


if __name__ == "__main__":
    unittest.main()
