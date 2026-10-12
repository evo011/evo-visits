"""Wetter (Open-Meteo) und Schulferien (OpenHolidays) pro Club-Standort, gecacht in SQLite.

Noch keine Verdrahtung in Prognose oder Dashboard -- dieses Modul beschafft und cachet nur.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional

from .calendar_de import Location, locate
from .config import Config
from .fetch import http_get as _http_get
from .store import Store

WEATHER_URL = ("https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
               "&hourly=temperature_2m,precipitation,sunshine_duration,weather_code"
               "&past_days=92&forecast_days=8&timeformat=unixtime&timezone=UTC")
HOLIDAY_URL = ("https://openholidaysapi.org/SchoolHolidays?countryIsoCode=DE"
               "&subdivisionCode=DE-{state}&languageIsoCode=DE&validFrom={year}-01-01&validTo={year}-12-31")

WEATHER_TTL = 3600        # Wetter max. stündlich neu abrufen
SCHOOL_TTL = 7 * 86400    # Ferien max. wöchentlich neu abrufen
RETRY_AFTER_FAIL = 600    # nach einem Fehler fruehestens nach 10 Minuten wieder versuchen

SCHEMA = """
CREATE TABLE IF NOT EXISTS weather (loc TEXT NOT NULL, ts INTEGER NOT NULL, temp REAL, precip REAL,
  sunshine REAL, code INTEGER, fetched_at INTEGER NOT NULL, PRIMARY KEY (loc, ts));
CREATE TABLE IF NOT EXISTS school_holidays (state TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL,
  name TEXT NOT NULL, PRIMARY KEY (state, start, name));
CREATE TABLE IF NOT EXISTS context_fetch (source TEXT PRIMARY KEY, fetched_at INTEGER NOT NULL,
  ok INTEGER NOT NULL, n INTEGER NOT NULL, error TEXT);
"""


@dataclass(frozen=True)
class WeatherHour:
    ts: int                 # Stundenbeginn, Unix-Sekunden UTC
    temp: Optional[float]   # Grad C
    precip: Optional[float] # mm in dieser Stunde
    sunshine: Optional[float]  # Sekunden Sonnenschein in dieser Stunde (0..3600)
    code: Optional[int]     # WMO weather code


def _default_http(cfg: Config) -> Callable[[str], str]:
    def get(url: str) -> str:
        return _http_get(url, cfg, accept="application/json")[1]
    return get


class Context:
    def __init__(self, store: Store, cfg: Config, now_fn: Callable[[], float] = time.time,
                 http: Optional[Callable[[str], str]] = None):
        self.store = store
        self.cfg = cfg
        self._now = now_fn
        self._http = http or _default_http(cfg)
        with store.conn() as c:
            c.executescript(SCHEMA)

    # ------------------------------------------------------------------ interne Helfer

    def _fetch_row(self, source: str) -> Optional[tuple]:
        with self.store.conn() as c:
            return c.execute(
                "SELECT fetched_at, ok, n, error FROM context_fetch WHERE source=?", (source,)
            ).fetchone()

    def _needs_fetch(self, source: str, ttl: int, force: bool) -> bool:
        if force:
            return True
        row = self._fetch_row(source)
        if row is None:
            return True
        fetched_at, ok = row[0], row[1]
        age = self._now() - fetched_at
        if ok and age < ttl:
            return False
        if not ok and age < RETRY_AFTER_FAIL:
            return False
        return True

    def _cached_result(self, source: str) -> dict:
        _, ok, n, error = self._fetch_row(source)
        return {"ok": bool(ok), "n": int(n), "error": error, "cached": True}

    def _record(self, source: str, ok: bool, n: int, error: Optional[str] = None) -> None:
        err = (error or "")[:300] or None
        with self.store.conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO context_fetch (source, fetched_at, ok, n, error) VALUES (?,?,?,?,?)",
                (source, int(self._now()), int(ok), n, err),
            )

    # ------------------------------------------------------------------ Wetter

    def refresh_weather(self, loc: Location, force: bool = False) -> dict:
        source = f"weather:{loc.key}"
        if not self._needs_fetch(source, WEATHER_TTL, force):
            return self._cached_result(source)
        try:
            data = json.loads(self._http(WEATHER_URL.format(lat=loc.lat, lon=loc.lon)))
            hourly = data["hourly"]
            times = hourly["time"]
            fetched_at = int(self._now())
            rows = []
            cols = {k: hourly.get(k) or [] for k in
                    ("temperature_2m", "precipitation", "sunshine_duration", "weather_code")}
            for i, ts in enumerate(times):
                temp = cols["temperature_2m"][i] if i < len(cols["temperature_2m"]) else None
                precip = cols["precipitation"][i] if i < len(cols["precipitation"]) else None
                sun = cols["sunshine_duration"][i] if i < len(cols["sunshine_duration"]) else None
                code = cols["weather_code"][i] if i < len(cols["weather_code"]) else None
                if temp is None and precip is None and sun is None and code is None:
                    continue  # aeltere Stunden aus past_days sind komplett null
                rows.append((loc.key, int(ts), temp, precip, sun,
                            int(code) if code is not None else None, fetched_at))
            with self.store.conn() as c:
                c.executemany(
                    "INSERT OR REPLACE INTO weather (loc, ts, temp, precip, sunshine, code, fetched_at) "
                    "VALUES (?,?,?,?,?,?,?)", rows)
                c.execute(
                    "INSERT OR REPLACE INTO context_fetch (source, fetched_at, ok, n, error) "
                    "VALUES (?,?,?,?,NULL)", (source, fetched_at, 1, len(rows)))
            return {"ok": True, "n": len(rows), "error": None, "cached": False}
        except Exception as e:
            self._record(source, False, 0, str(e))
            return {"ok": False, "n": 0, "error": str(e)[:300], "cached": False}

    # ------------------------------------------------------------------ Schulferien

    def refresh_school_holidays(self, state: str, year: int, force: bool = False) -> dict:
        source = f"school:{state}:{year}"
        if not self._needs_fetch(source, SCHOOL_TTL, force):
            return self._cached_result(source)
        url = HOLIDAY_URL.format(state=state, year=year)
        try:
            data = json.loads(self._http(url))
            rows = []
            for item in data:
                start, end = str(item["startDate"]), str(item["endDate"])
                name = None
                names = item.get("name") or []
                for entry in names:
                    if entry.get("language") == "DE":
                        name = entry.get("text")
                        break
                if name is None and names:
                    name = names[0].get("text")
                if name:
                    rows.append((state, start, end, name))
            with self.store.conn() as c:
                c.execute("DELETE FROM school_holidays WHERE state=? AND start LIKE ?",
                          (state, f"{year}-%"))
                c.executemany(
                    "INSERT OR REPLACE INTO school_holidays (state, start, end, name) VALUES (?,?,?,?)",
                    rows)
                c.execute(
                    "INSERT OR REPLACE INTO context_fetch (source, fetched_at, ok, n, error) "
                    "VALUES (?,?,?,?,NULL)", (source, int(self._now()), 1, len(rows)))
            return {"ok": True, "n": len(rows), "error": None, "cached": False}
        except Exception as e:
            self._record(source, False, 0, str(e))
            return {"ok": False, "n": 0, "error": str(e)[:300], "cached": False}

    # ------------------------------------------------------------------ Komfort

    def refresh_for_gyms(self, gyms: list, force: bool = False) -> dict:
        """Alle noetigen Orte und Laender der Clubs einmal auffrischen."""
        locations: dict[str, Location] = {}
        for gym in gyms:
            loc = locate(gym, self.cfg.extra.get("default_city", "Düsseldorf"),
                         self.cfg.extra.get("locations"))
            locations[loc.key] = loc
        weather = {key: self.refresh_weather(loc, force) for key, loc in sorted(locations.items())}
        today = datetime.fromtimestamp(self._now(), tz=timezone.utc).date()
        first = today - timedelta(weeks=self.cfg.history_weeks)
        last = today + timedelta(days=8)
        years = sorted(set(range(first.year, last.year + 1)))
        school = {}
        for state in sorted({loc.state for loc in locations.values()}):
            for year in years:
                school[f"{state}:{year}"] = self.refresh_school_holidays(state, year, force)
        return {"weather": weather, "school": school}

    # ------------------------------------------------------------------ Lesezugriff

    def weather(self, loc_key: str, start_ts: int, end_ts: int) -> dict:
        with self.store.conn() as c:
            rows = c.execute(
                "SELECT ts, temp, precip, sunshine, code FROM weather "
                "WHERE loc=? AND ts>=? AND ts<=? ORDER BY ts", (loc_key, start_ts, end_ts)).fetchall()
        return {int(ts): WeatherHour(int(ts), temp, precip, sun, code) for ts, temp, precip, sun, code in rows}

    def school_holiday(self, state: str, d: date) -> Optional[str]:
        iso = d.isoformat()
        with self.store.conn() as c:
            row = c.execute(
                "SELECT name FROM school_holidays WHERE state=? AND start<=? AND end>=? "
                "ORDER BY start LIMIT 1", (state, iso, iso)).fetchone()
        return row[0] if row else None

    def status(self) -> list:
        with self.store.conn() as c:
            rows = c.execute(
                "SELECT source, fetched_at, ok, n, error FROM context_fetch ORDER BY source").fetchall()
        return [dict(zip(("source", "fetched_at", "ok", "n", "error"), r)) for r in rows]
