import math
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from evo.evaluate import apply_calibration, calibration, evaluate
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


class CalibrationTestCase(unittest.TestCase):
    """calibration und apply_calibration mit von Hand nachrechenbaren Quantilen."""

    OFFSET = 3.0

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.snap_dir = Path(self.tmp.name) / "forecasts"
        self.snap_dir.mkdir()
        self.store = Store(str(Path(self.tmp.name) / "visits.db"))

    def add_samples(self, gym, ts_values):
        self.store.add_batch([(ts, Reading(gym, count=v)) for ts, v in ts_values])

    def log_err(self, est, ist):
        return math.log((ist + self.OFFSET) / (est + self.OFFSET))

    def test_quantiles_per_group(self):
        # 4 Zeilen in same_day, est 20, ist 20/40/40/80 -> Fehler aufsteigend sortiert
        t = [hour_ts(2026, 10, 13, 6 + i) for i in range(4)]
        made_ts = NOW_TS - 3600
        ists = (20.0, 40.0, 40.0, 80.0)
        for ti, target in enumerate(t):
            self.add_samples("EVO Alpha", [(target, ists[ti])])
        rows = [snap_row("2026-10-13", made_ts, "EVO Alpha", t[i], 20, 18, 22)
                for i in range(4)]
        write_csv(self.snap_dir, rows)

        res = calibration(self.snap_dir, self.store, TZ, NOW_TS, min_n=4)
        self.assertEqual(res["window_days"], 14)
        self.assertEqual(res["n"], 4)
        self.assertEqual(sorted(res["buckets"]), ["same_day"])
        errs = sorted(self.log_err(20, ist) for ist in ists)
        b = res["buckets"]["same_day"]
        self.assertFalse(b["pooled"])
        self.assertEqual(b["n"], 4)
        # lineare Interpolation: 10-%-Quantil bei Position 0.3, 90-%-Quantil bei 2.7
        self.assertAlmostEqual(b["q_lo"], errs[0] * 0.7 + errs[1] * 0.3)
        self.assertAlmostEqual(b["q_hi"], errs[2] * 0.3 + errs[3] * 0.7)

    def test_window_cuts_old_made_dates(self):
        t = hour_ts(2026, 10, 13, 10)
        made_new = NOW_TS - 3600                      # 13.10., im 3-Tage-Fenster
        made_old = NOW_TS - 5 * DAY - 3600            # 8.10., vor dem 3-Tage-Fenster
        self.add_samples("EVO Alpha", [(t, 20)])
        write_csv(self.snap_dir, [
            snap_row("2026-10-13", made_new, "EVO Alpha", t, 22, 20, 24),
            snap_row("2026-10-08", made_old, "EVO Alpha", t, 50, 45, 55),
        ])
        res = calibration(self.snap_dir, self.store, TZ, NOW_TS,
                          window_days=3, min_n=1)
        self.assertEqual(res["n"], 1)
        self.assertEqual(sorted(res["buckets"]), ["same_day"])
        # nur die neue Zeile zaehlt: Fehler log((20+3)/(22+3)), nicht der 50er
        self.assertAlmostEqual(res["buckets"]["same_day"]["q_lo"],
                               self.log_err(22, 20))
        # evaluate wertet beide Zeilen (kein Fenster)
        ev = evaluate(self.snap_dir, self.store, TZ, NOW_TS)
        self.assertEqual(ev["days"], 2)

    def test_thin_group_falls_back_to_pooled(self):
        # same_day mit 3 Werten (min_n 4 -> zu duenn), 1d mit 4 Werten
        t_same = [hour_ts(2026, 10, 13, 6 + i) for i in range(3)]
        t_1d = [hour_ts(2026, 10, 13, 6 + i) for i in range(4)]
        for target in t_same + t_1d:
            self.add_samples("EVO Alpha", [(target, 30)])
        rows = [snap_row("2026-10-13", NOW_TS - 3600, "EVO Alpha", t, 20, 18, 22)
                for t in t_same]
        rows += [snap_row("2026-10-12", NOW_TS - DAY - 3600, "EVO Alpha", t, 20, 18, 22)
                 for t in t_1d]
        write_csv(self.snap_dir, rows)

        res = calibration(self.snap_dir, self.store, TZ, NOW_TS, min_n=4)
        self.assertEqual(res["n"], 7)
        self.assertEqual(sorted(res["buckets"]), ["1d", "same_day"])
        own = res["buckets"]["1d"]
        self.assertFalse(own["pooled"])
        self.assertEqual(own["n"], 4)
        pooled = res["buckets"]["same_day"]
        self.assertTrue(pooled["pooled"])
        self.assertEqual(pooled["n"], 3)
        # Pooled-Quantile ueber alle 7 identischen Fehler
        e = self.log_err(20, 30)
        self.assertAlmostEqual(pooled["q_lo"], e)
        self.assertAlmostEqual(pooled["q_hi"], e)
        self.assertAlmostEqual(own["q_lo"], e)

    def test_too_few_values_omit_bucket(self):
        t = hour_ts(2026, 10, 13, 10)
        self.add_samples("EVO Alpha", [(t, 20)])
        write_csv(self.snap_dir, [
            snap_row("2026-10-13", NOW_TS - 3600, "EVO Alpha", t, 22, 20, 24),
        ])
        res = calibration(self.snap_dir, self.store, TZ, NOW_TS, min_n=500)
        self.assertEqual(res, {"window_days": 14, "n": 1, "buckets": {}})

    def test_empty_and_missing_directory(self):
        res = calibration(self.snap_dir, self.store, TZ, NOW_TS)
        self.assertEqual(res, {"window_days": 14, "n": 0, "buckets": {}})
        res = calibration(Path(self.tmp.name) / "gibtsnicht", self.store, TZ, NOW_TS)
        self.assertEqual(res, {"window_days": 14, "n": 0, "buckets": {}})

    def test_apply_calibration_groups_by_calendar_day(self):
        calib = {"window_days": 14, "n": 0, "buckets": {
            # Fehler auf Log-Skala: q_lo = log(0.5), q_hi = log(2)
            "same_day": {"q_lo": math.log(0.5), "q_hi": math.log(2.0),
                         "n": 100, "pooled": False},
            "1d": {"q_lo": math.log(0.4), "q_hi": math.log(2.5),
                   "n": 100, "pooled": False},
        }}
        now = NOW_TS
        same_day_ts = int((NOW + timedelta(hours=3)).timestamp())       # 13.10. 15:00
        one_day_ts = int((NOW + timedelta(days=1, hours=3)).timestamp())  # 14.10. 15:00
        three_day_ts = int((NOW + timedelta(days=3)).timestamp())       # 16.10., ohne Gruppe
        points = [
            {"ts": same_day_ts, "est": 47.0, "lo": 40.0, "hi": 54.0},
            {"ts": one_day_ts, "est": 47.0, "lo": 40.0, "hi": 54.0},
            {"ts": three_day_ts, "est": 47.0, "lo": 40.0, "hi": 54.0},
            {"ts": same_day_ts, "est": None, "lo": None, "hi": None},
        ]
        self.assertTrue(apply_calibration(points, calib, now, TZ))
        # same_day: (47+3)*0.5-3 = 22, (47+3)*2-3 = 97
        self.assertEqual(points[0]["lo"], 22.0)
        self.assertEqual(points[0]["hi"], 97.0)
        # 1d: (47+3)*0.4-3 = 17, (47+3)*2.5-3 = 122
        self.assertEqual(points[1]["lo"], 17.0)
        self.assertEqual(points[1]["hi"], 122.0)
        # 16.10. -> 2_3d, keine Gruppe: Band bleibt Modell-Band
        self.assertEqual(points[2]["lo"], 40.0)
        self.assertEqual(points[2]["hi"], 54.0)
        # est None bleibt unberuehrt
        self.assertIsNone(points[3]["lo"])
        self.assertIsNone(points[3]["hi"])

    def test_apply_calibration_clamps_at_zero_and_empty_calib(self):
        calib = {"window_days": 14, "n": 5, "buckets": {
            "same_day": {"q_lo": math.log(0.01), "q_hi": math.log(2.0),
                         "n": 5, "pooled": False},
        }}
        p = {"ts": NOW_TS + 3600, "est": 1.0, "lo": 0.0, "hi": 2.0}
        self.assertTrue(apply_calibration([p], calib, NOW_TS, TZ))
        self.assertEqual(p["lo"], 0.0)
        self.assertEqual(p["hi"], 5.0)
        self.assertFalse(apply_calibration([p], {"window_days": 14, "n": 0, "buckets": {}},
                                          NOW_TS, TZ))
        self.assertEqual(p["lo"], 0.0)
        self.assertEqual(p["hi"], 5.0)


if __name__ == "__main__":
    unittest.main()
