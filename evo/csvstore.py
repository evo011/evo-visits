"""Monats-CSV-Export und -Import als Ablage fuer Umgebungen ohne SQLite im Git."""

from __future__ import annotations

import csv
import io
from datetime import datetime
from pathlib import Path
from typing import Optional

from .context import SCHEMA
from .store import Reading, Store

HEADER = ["ts", "local_time", "gym", "count", "capacity", "pct"]
WEATHER_HEADER = ["ts", "local_time", "loc", "temp", "precip", "sunshine", "code"]


def _fmt(v: Optional[float]) -> str:
    if v is None:
        return ""
    if v == int(v):
        return str(int(v))
    return str(v)


def _parse(v: str) -> Optional[float]:
    v = v.strip()
    return float(v) if v else None


def export_monthly(store: Store, directory: Path, tz) -> list[Path]:
    """Schreibt je Monat eine visits-YYYY-MM.csv, nur wenn sich der Inhalt aendert."""
    months: dict[str, list[list[str]]] = {}
    for ts, gym, count, capacity, pct in store.export_rows():
        local = datetime.fromtimestamp(ts, tz)
        month = local.strftime("%Y-%m")
        months.setdefault(month, []).append(
            [str(ts), local.strftime("%Y-%m-%d %H:%M"), gym,
             _fmt(count), _fmt(capacity), _fmt(pct)]
        )
    directory.mkdir(parents=True, exist_ok=True)
    changed = []
    for month in sorted(months):
        path = directory / f"visits-{month}.csv"
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(HEADER)
        w.writerows(months[month])
        content = buf.getvalue()
        if path.exists() and path.read_text(encoding="utf-8") == content:
            continue
        path.write_text(content, encoding="utf-8")
        changed.append(path)
    return changed


def import_dir(store: Store, directory: Path) -> int:
    """Liest alle visits-*.csv ein; kaputte Zeilen werden uebersprungen."""
    if not directory.is_dir():
        return 0
    total = 0
    for path in sorted(directory.glob("visits-*.csv")):
        items = []
        with path.open(encoding="utf-8") as f:
            for row in csv.reader(f):
                if not row or row[0] == HEADER[0]:
                    continue
                try:
                    ts = int(row[0])
                    gym = row[2]
                    reading = Reading(gym, _parse(row[3]), _parse(row[4]), _parse(row[5]))
                except (ValueError, IndexError):
                    continue
                if gym:
                    items.append((ts, reading))
        total += store.add_batch(items)
    return total


def export_weather_monthly(store: Store, directory: Path, tz, now: int) -> list[Path]:
    """Schreibt je Monat eine weather-YYYY-MM.csv der gemessenen Stunden (ts <= now).

    Vorhersage-Stunden der Open-Meteo-API liegen in der Zukunft und bleiben draussen.
    """
    # Tabelle anlegen, falls der context-Schritt noch nie gelaufen ist
    with store.conn() as c:
        c.executescript(SCHEMA)
    months: dict[str, list[list[str]]] = {}
    with store.conn() as c:
        rows = c.execute(
            "SELECT ts, loc, temp, precip, sunshine, code FROM weather WHERE ts<=? ORDER BY ts, loc",
            (int(now),)).fetchall()
    for ts, loc, temp, precip, sunshine, code in rows:
        local = datetime.fromtimestamp(ts, tz)
        month = local.strftime("%Y-%m")
        months.setdefault(month, []).append(
            [str(ts), local.strftime("%Y-%m-%d %H:%M"), loc,
             _fmt(temp), _fmt(precip), _fmt(sunshine), _fmt(code)]
        )
    directory.mkdir(parents=True, exist_ok=True)
    changed = []
    for month in sorted(months):
        path = directory / f"weather-{month}.csv"
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(WEATHER_HEADER)
        w.writerows(months[month])
        content = buf.getvalue()
        if path.exists() and path.read_text(encoding="utf-8") == content:
            continue
        path.write_text(content, encoding="utf-8")
        changed.append(path)
    return changed


def import_weather_dir(store: Store, directory: Path) -> int:
    """Liest alle weather-*.csv per INSERT OR IGNORE ein (fetched_at=0).

    Vorhandene Zeilen bleiben stehen: frischere Werte aus dem API-Abruf werden nicht
    von alten CSV-Zeilen ueberschrieben.
    """
    if not directory.is_dir():
        return 0
    with store.conn() as c:
        c.executescript(SCHEMA)
    total = 0
    for path in sorted(directory.glob("weather-*.csv")):
        rows = []
        with path.open(encoding="utf-8") as f:
            for row in csv.reader(f):
                if not row or row[0] == WEATHER_HEADER[0]:
                    continue
                try:
                    ts = int(row[0])
                    loc = row[2]
                    code = _parse(row[6])
                    rows.append((loc, ts, _parse(row[3]), _parse(row[4]), _parse(row[5]),
                                 int(code) if code is not None else None, 0))
                except (ValueError, IndexError):
                    continue
        with store.conn() as c:
            c.executemany(
                "INSERT OR IGNORE INTO weather (loc, ts, temp, precip, sunshine, code, fetched_at) "
                "VALUES (?,?,?,?,?,?,?)", rows)
        total += len(rows)
    return total
