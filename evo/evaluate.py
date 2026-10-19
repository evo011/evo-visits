"""Echte Prognosegüte: gespeicherte Schnappschuss-Prognosen gegen Messwerte.

Liest die taeglichen Schnappschuss-CSVs (forecast-YYYY-MM.csv, siehe snapshot.py)
und vergleicht jede Prognose mit dem Mittelwert der Messwerte im Zielfenster.
Vorlauf-Gruppen: same_day (Prognose am selben Tag), 1d, 2_3d, 4_7d.

calibration bestimmt aus denselben Zeilen die 10-/90-%-Quantile der
log-Prognosefehler je Vorlauf-Gruppe; apply_calibration ueberstaempft damit
das Modell-Band der Prognosepunkte (80-%-Band mit echter Abdeckung).
"""

from __future__ import annotations

import csv
import math
from bisect import bisect_left
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from .effects import OFFSET

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


def _rows_with_ist(snap_dir: Path, store, tz, now: int) -> list:
    """Alle von evaluate gewerteten Zeilen mit Ist-Wert (bisect auf einmal geladenen Messwerten).

    Rueckgabe je Zeile: (gym, model, made_date, target_ts, est, lo, hi, ist, lw_err).
    Zeilen ohne Messwert im Zielfenster oder ohne Vorwochen-Messwert-Paar fallen weg;
    lw_err ist None, wenn es keinen Vorwochenwert gibt.
    """
    rows = _read_rows(Path(snap_dir), int(now))
    if not rows:
        return []
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
    out = []
    for gym, gym_rows in by_gym_rows.items():
        ts_list, vals = series[gym]
        for _, model, made, target_ts, est, lo, hi in gym_rows:
            ist = _mean_window(ts_list, vals, target_ts, target_ts + _SLOT)
            if ist is None:
                continue
            lw = _mean_window(ts_list, vals, target_ts - _WEEK, target_ts - _WEEK + _SLOT)
            lw_err = abs(lw - ist) if lw is not None else None
            out.append((gym, model, made, target_ts, est, lo, hi, ist, lw_err))
    return out


def evaluate(snap_dir: Path, store, tz, now: int) -> dict:
    """Vergleich aller gespeicherten Prognosen mit den gemessenen Werten."""
    now = int(now)
    rows = _rows_with_ist(Path(snap_dir), store, tz, now)
    if not rows:
        return empty_result(now)

    made_dates = set()
    allg: dict = {}
    by_gym: dict = {}
    by_model: dict = {}
    for gym, model, made, target_ts, est, lo, hi, ist, lw_err in rows:
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


def _quantile(values: list, q: float) -> float:
    """Lineares q-Quantil (q in 0..1) einer noch unsortierten Werteliste."""
    vs = sorted(values)
    pos = (len(vs) - 1) * q
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(vs) - 1)
    frac = pos - lo
    return vs[lo] * (1.0 - frac) + vs[hi] * frac


def calibration(snap_dir: Path, store, tz, now: int,
                window_days: int = 14, min_n: int = 500) -> dict:
    """10-/90-%-Quantile der log-Prognosefehler je Vorlauf-Gruppe aus den letzten Tagen.

    Nur Zeilen, die auch evaluate wertet, und nur mit made_date ab dem lokalen
    Datum von now minus window_days. Gruppen mit mindestens min_n Werten
    bekommen eigene Quantile, duenn belegte Gruppen die ueber alle Gruppen
    gepoolten (pooled: True); reicht auch das nicht, fehlt die Gruppe.
    """
    now = int(now)
    cutoff = datetime.fromtimestamp(now, tz).date() - timedelta(days=int(window_days))
    errs: dict = {b: [] for b in BUCKETS}
    for _, _, made, target_ts, est, _, _, ist, _ in _rows_with_ist(
            Path(snap_dir), store, tz, now):
        if made < cutoff:
            continue
        group = _bucket(made, datetime.fromtimestamp(target_ts, tz).date())
        errs[group].append(math.log((ist + OFFSET) / (est + OFFSET)))

    n = sum(len(v) for v in errs.values())
    buckets = {}
    pooled_vals = [e for v in errs.values() for e in v]
    pooled_ok = len(pooled_vals) >= int(min_n)
    for group in BUCKETS:
        values = errs[group]
        if len(values) >= int(min_n):
            buckets[group] = {"q_lo": _quantile(values, 0.1), "q_hi": _quantile(values, 0.9),
                              "n": len(values), "pooled": False}
        elif values and pooled_ok:
            buckets[group] = {"q_lo": _quantile(pooled_vals, 0.1),
                              "q_hi": _quantile(pooled_vals, 0.9),
                              "n": len(values), "pooled": True}
    return {"window_days": int(window_days), "n": n, "buckets": buckets}


def apply_calibration(points: list, calib: dict, now: int, tz) -> bool:
    """Ueberschreibt lo/hi der Prognosepunkte mit dem kalibrierten 80-%-Band.

    Die Gruppe jedes Punkts ergibt sich aus dem Kalenderabstand zwischen dem
    lokalen Datum von now und dem lokalen Datum des Zielzeitpunkts. Punkte mit
    est None oder ohne Quantile fuer ihre Gruppe bleiben unberuehrt.
    """
    buckets = (calib or {}).get("buckets") or {}
    if not buckets:
        return False
    today = datetime.fromtimestamp(int(now), tz).date()
    applied = False
    for p in points:
        est = p.get("est")
        if est is None:
            continue
        target = datetime.fromtimestamp(int(p["ts"]), tz).date()
        group = _bucket(today, target)
        b = buckets.get(group)
        if b is None:
            continue
        p["lo"] = round(max(0.0, (est + OFFSET) * math.exp(b["q_lo"]) - OFFSET), 1)
        p["hi"] = round((est + OFFSET) * math.exp(b["q_hi"]) - OFFSET, 1)
        applied = True
    return applied
