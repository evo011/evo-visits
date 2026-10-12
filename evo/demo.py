"""Synthetische Demo-Daten, nur damit man das Dashboard vor der ersten echten Woche sehen kann.

Landen in einer eigenen Datenbank (data/demo.db) und tragen erkennbar falsche Club-Namen.
Das Demo-Wetter und die Demo-Ferien sind ebenfalls frei erfunden -- sie sollen nur zeigen,
wie die Kontext-Effekte im Dashboard wirken, und haben kein echtes Pendant.
"""

from __future__ import annotations

import math
import random
import time
from datetime import date, datetime, timedelta
from typing import Optional

from .calendar_de import holiday_name
from .config import Config
from .context import Context, WeatherHour
from .forecast import get_tz
from .store import Reading, Store

DEMO_GYMS = {"Demo Nord": 1.0, "Demo Mitte": 1.35, "Demo Süd": 0.8, "Demo West": 1.1}

# Demo-Clubs haben keinen echten Standort; locate() wuerde den Default nehmen.
DEMO_LOC_KEY = "düsseldorf"
DEMO_STATE = "NW"


def typical(dt: datetime, scale: float, weekday: Optional[int] = None) -> float:
    wd = dt.weekday() if weekday is None else weekday
    h = dt.hour + dt.minute / 60
    if wd < 5:
        base = 8 + 18 * math.exp(-((h - 7.5) / 1.2) ** 2) + 14 * math.exp(-((h - 12.5) / 1.3) ** 2) \
            + 55 * math.exp(-((h - 18.5) / 1.6) ** 2)
        if wd == 4:
            base *= 0.8
    else:
        base = 6 + 40 * math.exp(-((h - 12.0) / 2.6) ** 2)
    if h < 6 or h > 23:
        base = 0
    return base * scale


def _demo_weather(rnd: random.Random, first_ts: int, end_ts: int, tz, fetched_at: int) -> list[tuple]:
    """Stuendliches Demo-Wetter je Lokaltag: Temperaturgang, Regenphase, Sonne."""
    rows = []
    params: dict = {}
    ts = first_ts - first_ts % 3600
    while ts <= end_ts:
        dt = datetime.fromtimestamp(ts, tz)
        d = dt.date()
        if d not in params:
            tbase = 12 + rnd.gauss(0, 4)
            cloud = rnd.random()
            rainy = rnd.random() < 0.3
            rain_start = rnd.randint(8, 16)
            rain_len = rnd.randint(2, 6)
            rain_rate = rnd.uniform(0.5, 3.0)
            params[d] = (tbase, cloud, rainy, rain_start, rain_len, rain_rate)
        tbase, cloud, rainy, rain_start, rain_len, rain_rate = params[d]
        h = dt.hour
        temp = tbase + 5 * math.sin(2 * math.pi * (h - 9) / 24)
        precip = rain_rate if rainy and rain_start <= h < rain_start + rain_len else 0.0
        sun = 3600 * (1 - cloud) if precip <= 0 and 7 <= h <= 18 else 0.0
        code = 63 if precip >= 2 else 61 if precip > 0 else 0 if sun > 2400 else 2 if sun > 0 else 3
        rows.append((DEMO_LOC_KEY, ts, round(temp, 1), round(precip, 2), round(sun), code, fetched_at))
        ts += 3600
    return rows


def _weather_factor(w: Optional[WeatherHour]) -> float:
    factor = 1.0
    if w is None:
        return factor
    if (w.precip or 0) > 0:
        factor *= 1.15
    if w.temp is not None and w.temp > 18:
        factor *= 1 - 0.12 * (w.temp - 18) / 10
    return factor


def generate(store: Store, tz_name: str, weeks: int = 6, interval_min: int = 10, seed: int = 7,
             now: Optional[float] = None) -> int:
    rnd = random.Random(seed)
    tz = get_tz(tz_name)
    now = int(now if now is not None else time.time())
    ctx = Context(store, Config())  # Konstruktor legt das Schema an, macht kein Netz

    start = now - weeks * 7 * 86400
    start -= start % (interval_min * 60)
    first_ts = start - 86400 - (start - 86400) % 3600
    end_ts = now + 8 * 86400
    end_ts += (-end_ts) % 3600

    weather_rows = _demo_weather(rnd, first_ts, end_ts, tz, now)

    today = datetime.fromtimestamp(now, tz).date()
    herbst = (today - timedelta(days=24), today - timedelta(days=15))
    ferien = (today + timedelta(days=3), today + timedelta(days=6))
    holiday_rows = [
        (DEMO_STATE, herbst[0].isoformat(), herbst[1].isoformat(), "Demo-Herbstferien"),
        (DEMO_STATE, ferien[0].isoformat(), ferien[1].isoformat(), "Demo-Ferien"),
    ]
    fetch_rows = [
        (f"weather:{DEMO_LOC_KEY}", now, 1, len(weather_rows), None),
        (f"school:{DEMO_STATE}:{today.year}", now, 1, len(holiday_rows), None),
    ]
    with store.conn() as c:
        c.executemany(
            "INSERT OR REPLACE INTO weather (loc, ts, temp, precip, sunshine, code, fetched_at) "
            "VALUES (?,?,?,?,?,?,?)", weather_rows)
        c.executemany(
            "INSERT OR REPLACE INTO school_holidays (state, start, end, name) VALUES (?,?,?,?)",
            holiday_rows)
        c.executemany(
            "INSERT OR REPLACE INTO context_fetch (source, fetched_at, ok, n, error) VALUES (?,?,?,?,?)",
            fetch_rows)

    weather = ctx.weather(DEMO_LOC_KEY, first_ts, end_ts)
    batch: list[tuple[int, Reading]] = []
    level = {g: 1.0 for g in DEMO_GYMS}
    vacation: dict = {}
    for ts in range(start, now, interval_min * 60):
        dt = datetime.fromtimestamp(ts, tz)
        if not (6 <= dt.hour < 23):
            continue
        d = dt.date()
        if d not in vacation:
            vacation[d] = ctx.school_holiday(DEMO_STATE, d) is not None
        factor = _weather_factor(weather.get(ts - ts % 3600 + 3600))
        if vacation[d]:
            factor *= 1.10
        # gesetzlicher Feiertag -> Sonntagsmuster
        wd = 6 if holiday_name(d, DEMO_STATE) else None
        for gym, scale in DEMO_GYMS.items():
            # langsam wandernder Tageslevel, damit die Prognose etwas zu tun hat
            level[gym] = 0.98 * level[gym] + 0.02 * (1 + rnd.gauss(0, 0.25))
            v = typical(dt, scale, weekday=wd) * level[gym] * factor + rnd.gauss(0, 3)
            batch.append((ts, Reading(gym=gym, count=float(max(0, round(v))), capacity=round(110 * scale))))
    return store.add_batch(batch)
