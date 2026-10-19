"""Taeglicher Schnappschuss der Prognose je Club als Monats-CSV im Repo.

Eine Datei forecast-YYYY-MM.csv, pro lokalem Kalendertag hoechstens ein Satz Zeilen
(nur volle Stunden 06:00-23:00). Spaeter vergleicht der Schnappschuss Vorhersage
gegen die tatsaechlich gemessenen Besucherzahlen.
"""

from __future__ import annotations

import csv
import hashlib
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

HEADER = ["made_date", "made_ts", "gym", "target_ts", "target_local", "lead_h",
          "est", "lo", "hi", "effect_pct", "temp", "precip", "sun_min", "code", "model"]


def _fmt(v) -> str:
    if v is None:
        return ""
    if v == int(v):
        return str(int(v))
    return str(v)


def default_model_version() -> str:
    """MODEL_VERSION aus der Umgebung, sonst 'c' + 7 Hex-Zeichen Hash ueber den Code.

    Gehasst werden alle *.py im Paketverzeichnis evo/, sortiert nach Dateiname,
    je Datei Name und Bytes. Nur der Code zaehlt, nicht jeder Daten-Commit.
    """
    env = os.environ.get("MODEL_VERSION")
    if env:
        return env
    h = hashlib.sha1()
    for path in sorted(Path(__file__).parent.glob("*.py"), key=lambda p: p.name):
        h.update(path.name.encode("utf-8"))
        h.update(path.read_bytes())
    return "c" + h.hexdigest()[:7]


def _has_made_date(path: Path, made_date: str) -> bool:
    with path.open(encoding="utf-8") as f:
        for row in csv.reader(f):
            if row and row[0] == made_date:
                return True
    return False


def snapshot_forecasts(app, directory: Path, now: int,
                       model_version: Optional[str] = None) -> Optional[Path]:
    """Haengt den Prognose-Schnappschuss fuer heute an forecast-YYYY-MM.csv an.

    Pro lokalem Kalendertag nur ein Schnappschuss: ist made_date schon in der
    Datei, passiert nichts (None). Ohne einzigen geeigneten Punkt ebenfalls None.
    """
    now = int(now)
    tz = app.tz
    made = datetime.fromtimestamp(now, tz)
    made_date = made.date().isoformat()
    directory = Path(directory)
    path = directory / f"forecast-{made.strftime('%Y-%m')}.csv"
    if path.exists() and _has_made_date(path, made_date):
        return None
    model = model_version if model_version is not None else default_model_version()

    rows = []
    for g in app.store.gyms():
        name = g["gym"]
        res = app.api_gym({"gym": [name], "days": ["1"]})
        for point in res.get("forecast") or []:
            ts_ms, est, lo, hi, effect_pct, weather = point
            if est is None:
                continue
            target_ts = int(ts_ms) // 1000
            local = datetime.fromtimestamp(target_ts, tz)
            if local.minute != 0 or not 6 <= local.hour <= 23:
                continue
            w = weather or {}
            rows.append([
                made_date, str(now), name, str(target_ts),
                local.strftime("%Y-%m-%d %H:%M"),
                f"{(target_ts - now) / 3600:.1f}",
                _fmt(est), _fmt(lo), _fmt(hi), _fmt(effect_pct),
                _fmt(w.get("temp")), _fmt(w.get("precip")),
                _fmt(w.get("sun_min")), _fmt(w.get("code")), model,
            ])
    if not rows:
        return None

    rows.sort(key=lambda r: (r[2], int(r[3])))
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists()
    with path.open("a", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        if is_new:
            w.writerow(HEADER)
        w.writerows(rows)
    return path
