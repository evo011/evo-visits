"""Echte Prognosegüte: gespeicherte Schnappschuss-Prognosen gegen Messwerte.

Liest die taeglichen Schnappschuss-CSVs (forecast-YYYY-MM.csv, siehe snapshot.py)
und vergleicht jede Prognose mit dem Mittelwert der Messwerte im Zielfenster.
Vorlauf-Gruppen: same_day (Prognose am selben Tag), 1d, 2_3d, 4_7d.
"""

from __future__ import annotations

import csv
from bisect import bisect_left
from datetime import date, datetime
from pathlib import Path
from typing import Optional

BUCKETS = ("same_day", "1d", "2_3d", "4_7d")
_WEEK = 7 * 86400
_SLOT = 900


def empty_result(now: int) -> dict:
    return {"generated_at": int(now), "days": 0, "first_made": None,
            "buckets": list(BUCKETS), "all": {}, "by_gym": {}, "by_model": {}}


def _bucket(made: date, target: date) -> str:
    """Vorlauf-Gruppe aus dem Kalenderabstand made_date -> lokales Zieldatum."""
    diff = (target - made).days
    if diff <= 0:
        return "same_day"
    if diff == 1:
        return "1d"
    if diff <= 3:
        return "2_3d"
    return "4_7d"


def _opt_float(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _mean_window(ts_list: list, values: list, start: int, end: int) -> Optional[float]:
    """Mittelwert der Messwerte in [start, end); None ohne Messwert."""
    i = bisect_left(ts_list, start)
    total, n = 0.0, 0
    while i < len(ts_list) and ts_list[i] < end:
        total += values[i]
        n += 1
        i += 1
    return total / n if n else None


def _read_rows(snap_dir: Path, now: int) -> list:
    """Alle Zeilen aus forecast-*.csv mit abgelaufenem Zielfenster und est-Wert.

    Rueckgabe: (gym, model, made_date, target_ts, est, lo, hi).
    """
    out = []
    if not snap_dir.is_dir():
        return out
    for path in sorted(snap_dir.glob("forecast-*.csv")):
        with path.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                est = _opt_float(row.get("est"))
                if est is None:
                    continue
                try:
                    target_ts = int(row["target_ts"])
                    made = date.fromisoformat(row["made_date"])
                except (TypeError, ValueError):
                    continue
                if target_ts + _SLOT > now:
                    continue
                out.append((row.get("gym") or "", row.get("model") or "",
                            made, target_ts, est,
                            _opt_float(row.get("lo")), _opt_float(row.get("hi"))))
    return out


def _acc() -> dict:
    return {"n": 0, "err": 0.0, "bias": 0.0, "cov": 0, "nlw": 0, "lw": 0.0, "same": 0.0}


def _add(groups: dict, group: str, est: float, ist: float,
         lo: Optional[float], hi: Optional[float], lw_err: Optional[float]) -> None:
    a = groups.setdefault(group, _acc())
    a["n"] += 1
    a["err"] += abs(est - ist)
    a["bias"] += est - ist
    if lo is not None and hi is not None and lo <= ist <= hi:
        a["cov"] += 1
    if lw_err is not None:
        a["nlw"] += 1
        a["lw"] += lw_err
        a["same"] += abs(est - ist)


def _metrics(a: dict) -> dict:
    n = a["n"]
    out = {"n": n, "mae": round(a["err"] / n, 1), "bias": round(a["bias"] / n, 1),
           "coverage_pct": round(100 * a["cov"] / n)}
    if a["nlw"]:
        out["n_last_week"] = a["nlw"]
        out["mae_last_week"] = round(a["lw"] / a["nlw"], 1)
        out["mae_model_same"] = round(a["same"] / a["nlw"], 1)
    else:
        out["n_last_week"] = 0
    return out


def evaluate(snap_dir: Path, store, tz, now: int) -> dict:
    """Vergleich aller gespeicherten Prognosen mit den gemessenen Werten."""
    now = int(now)
    rows = _read_rows(Path(snap_dir), now)
    if not rows:
        return empty_result(now)

    by_gym_rows: dict = {}
    for r in rows:
        by_gym_rows.setdefault(r[0], []).append(r)

    # Messwerte je Club EINMAL laden, Auswertung per bisect
    series = {}
    for gym, gym_rows in by_gym_rows.items():
        min_t = min(r[3] for r in gym_rows)
        max_t = max(r[3] for r in gym_rows)
        s = store.series(gym, min_t - 8 * 86400, max_t + _SLOT)
        series[gym] = ([ts for ts, _ in s], [v for _, v in s])

    made_dates = set()
    allg: dict = {}
    by_gym: dict = {}
    by_model: dict = {}
    for gym, gym_rows in by_gym_rows.items():
        ts_list, vals = series[gym]
        for _, model, made, target_ts, est, lo, hi in gym_rows:
            ist = _mean_window(ts_list, vals, target_ts, target_ts + _SLOT)
            if ist is None:
                continue
            lw = _mean_window(ts_list, vals, target_ts - _WEEK, target_ts - _WEEK + _SLOT)
            # Fehler des naiven Ansatzes "wie Vorwoche" gegenueber dem Ist-Wert
            lw_err = abs(lw - ist) if lw is not None else None
            group = _bucket(made, datetime.fromtimestamp(target_ts, tz).date())
            made_dates.add(made)
            _add(allg, group, est, ist, lo, hi, lw_err)
            _add(by_gym.setdefault(gym, {}), group, est, ist, lo, hi, lw_err)
            _add(by_model.setdefault(model, {}), group, est, ist, lo, hi, lw_err)

    if not made_dates:
        return empty_result(now)
    return {
        "generated_at": now,
        "days": len(made_dates),
        "first_made": min(made_dates).isoformat(),
        "buckets": list(BUCKETS),
        "all": {g: _metrics(a) for g, a in allg.items()},
        "by_gym": {g: {k: _metrics(v) for k, v in acc.items()}
                   for g, acc in by_gym.items() if acc},
        "by_model": {m: {k: _metrics(v) for k, v in acc.items()}
                     for m, acc in by_model.items() if acc},
    }
