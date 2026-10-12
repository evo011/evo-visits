"""Prognose der Besucherzahlen.

Modell, bewusst simpel und nachvollziehbar:
  1. Messwerte in Zeit-Slots (Standard 15 min) pro Kalendertag mitteln.
  2. Saisonprofil je (Wochentag, Slot): gewichteter Mittelwert ueber die vergangenen Wochen,
     juengere Wochen zaehlen mehr (Halbwertszeit konfigurierbar). Nachbar-Slots fliessen
     halb gewichtet ein, das glaettet Rauschen.
  3. Wenig Daten fuer genau diesen Wochentag? Dann wird zum Profil des Tagestyps
     (Werktag/Wochenende) bzw. aller Tage geschrumpft.
  4. Tageskorrektur: liegt der Club heute gerade ueber/unter dem Profil, wird diese
     Abweichung in die naechsten Stunden fortgeschrieben und klingt exponentiell ab.
  5. Band = gewichtete Standardabweichung der historischen Werte im Slot.
"""

from __future__ import annotations

import math
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from typing import Callable, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

SHRINK_K = 1.0          # Gewicht des Priors (in "aktuellen Beobachtungen")
NOWCAST_TAU_MIN = 120   # Abklingzeit der Tageskorrektur
NOWCAST_LOOKBACK_MIN = 45


def get_tz(name: str) -> tzinfo:
    if ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return datetime.now().astimezone().tzinfo  # lokale Zeit als Fallback


@dataclass
class Stat:
    mean: float
    std: float
    weight: float


def _wstats(obs: list[tuple[float, float]]) -> Optional[Stat]:
    w = sum(o[1] for o in obs)
    if w <= 0:
        return None
    m = sum(v * wt for v, wt in obs) / w
    var = sum(wt * (v - m) ** 2 for v, wt in obs) / w
    return Stat(m, math.sqrt(var), w)


class Profile:
    def __init__(self, series: list[tuple[int, float]], tz: tzinfo, now_ts: Optional[int] = None,
                 bin_minutes: int = 15, half_life_days: float = 21.0,
                 day_kind: Optional[Callable[[date], int]] = None,
                 binned: Optional[dict[tuple[date, int], float]] = None):
        self.tz = tz
        self.bin = bin_minutes
        self.slots = 24 * 60 // bin_minutes
        self.now_ts = now_ts or int(time.time())
        self.day_kind = day_kind or (lambda d: d.weekday())
        today = datetime.fromtimestamp(self.now_ts, tz).date()

        # 1. pro (Datum, Slot) mitteln; vorgegebene Werte uebernehmen statt series
        if binned is not None:
            self.binned = binned
        else:
            acc: dict[tuple[date, int], list[float]] = defaultdict(list)
            for ts, v in series:
                dt = datetime.fromtimestamp(ts, tz)
                acc[(dt.date(), self.slot_of(dt))].append(v)
            self.binned = {k: sum(v) / len(v) for k, v in acc.items()}

        # 2. Beobachtungen je (Tagesart, Slot) mit Altersgewicht; der heutige Tag bleibt aussen vor,
        #    er wird ueber die Tageskorrektur beruecksichtigt.
        self.by_wd: dict[tuple[int, int], list[tuple[float, float]]] = defaultdict(list)
        self.by_type: dict[tuple[bool, int], list[tuple[float, float]]] = defaultdict(list)
        self.by_all: dict[int, list[tuple[float, float]]] = defaultdict(list)
        self.days_seen: set[date] = set()
        for (d, s), v in self.binned.items():
            if d >= today:
                continue
            self.days_seen.add(d)
            w = 0.5 ** ((today - d).days / half_life_days)
            for ds, f in ((0, 1.0), (-1, 0.5), (1, 0.5)):
                ss = s + ds
                if 0 <= ss < self.slots:
                    self.by_wd[(self.day_kind(d), ss)].append((v, w * f))
                    self.by_type[(self.day_kind(d) >= 5, ss)].append((v, w * f))
                    self.by_all[ss].append((v, w * f))
        self._cache: dict[tuple[int, int], Optional[Stat]] = {}

    def slot_of(self, dt: datetime) -> int:
        return (dt.hour * 60 + dt.minute) // self.bin

    def seasonal(self, weekday: int, slot: int) -> Optional[Stat]:
        key = (weekday, slot)
        if key in self._cache:
            return self._cache[key]
        same = _wstats(self.by_wd.get(key, []))
        typ = _wstats(self.by_type.get((weekday >= 5, slot), []))
        allx = _wstats(self.by_all.get(slot, []))
        prior = typ or allx
        if same is None and prior is None:
            res = None
        elif same is None:
            res = prior
        elif prior is None:
            res = same
        else:
            w = same.weight
            mean = (w * same.mean + SHRINK_K * prior.mean) / (w + SHRINK_K)
            std = same.std if same.weight >= 1.5 else max(same.std, prior.std)
            res = Stat(mean, std, w)
        self._cache[key] = res
        return res

    def seasonal_for(self, d: date, slot: int) -> Optional[Stat]:
        return self.seasonal(self.day_kind(d), slot)

    def seasonal_at(self, ts: int) -> Optional[Stat]:
        dt = datetime.fromtimestamp(ts, self.tz)
        return self.seasonal_for(dt.date(), self.slot_of(dt))

    def nowcast_offset(self, recent: list[tuple[int, float]]) -> Optional[float]:
        """Mittlere Abweichung der juengsten Messwerte vom Profil."""
        cutoff = self.now_ts - NOWCAST_LOOKBACK_MIN * 60
        diffs = []
        for ts, v in recent:
            if ts < cutoff or ts > self.now_ts:
                continue
            st = self.seasonal_at(ts)
            if st:
                diffs.append(v - st.mean)
        return sum(diffs) / len(diffs) if diffs else None

    def forecast(self, hours: int = 48, recent: Optional[list[tuple[int, float]]] = None,
                 cap: Optional[float] = None) -> list[dict]:
        step = self.bin * 60
        start = (self.now_ts // step + 1) * step
        offset = self.nowcast_offset(recent or []) or 0.0
        last_recent = max((ts for ts, _ in recent or []), default=None)
        if last_recent is None or self.now_ts - last_recent > 30 * 60:
            offset = 0.0
        out = []
        for ts in range(start, self.now_ts + hours * 3600 + 1, step):
            st = self.seasonal_at(ts)
            if st is None:
                out.append({"ts": ts, "est": None, "lo": None, "hi": None})
                continue
            h_min = (ts - self.now_ts) / 60
            est = st.mean + offset * math.exp(-h_min / NOWCAST_TAU_MIN)
            hi = est + st.std
            out.append({"ts": ts, "est": round(max(est, 0.0), 1),
                        "lo": round(max(est - st.std, 0.0), 1), "hi": round(max(hi, 0.0), 1)})
        return out

    def heatmap(self) -> list[list[Optional[float]]]:
        """7 x 24 Matrix: typischer Wert je Tagesart und Stunde (ohne Slot-Glaettung)."""
        acc: dict[tuple[int, int], list[float]] = defaultdict(list)
        for (d, s), v in self.binned.items():
            acc[(self.day_kind(d), s * self.bin // 60)].append(v)
        return [[round(sum(acc[(wd, h)]) / len(acc[(wd, h)]), 1) if acc.get((wd, h)) else None
                 for h in range(24)] for wd in range(7)]

    @property
    def n_days(self) -> int:
        return len(self.days_seen)


def build_profile(store, gym: str, cfg, now_ts: Optional[int] = None) -> Profile:
    now_ts = now_ts or int(time.time())
    since = now_ts - cfg.history_weeks * 7 * 86400
    series = store.series(gym, since, now_ts)
    return Profile(series, get_tz(cfg.timezone), now_ts, cfg.bin_minutes, cfg.half_life_days)


def backtest(series: list[tuple[int, float]], tz: tzinfo, bin_minutes: int = 15,
             half_life_days: float = 21.0, days: int = 7) -> Optional[dict]:
    """Wie gut haette das Modell die letzten `days` Tage vorhergesagt?

    Liefert die mittlere absolute Abweichung (MAE) des Modells und, als Vergleich, die eines
    naiven "gleiche Uhrzeit letzte Woche"-Ansatzes. Nur Tage mit mindestens 7 Tagen Vorlauf zaehlen.
    """
    if not series:
        return None
    full = Profile(series, tz, series[-1][0] + 86400, bin_minutes, half_life_days)
    last_day = datetime.fromtimestamp(series[-1][0], tz).date()
    first_day = last_day - timedelta(days=days - 1)
    errors, naive = [], []
    for i in range(days):
        d = first_day + timedelta(days=i)
        day_start = int(datetime(d.year, d.month, d.day, tzinfo=tz).timestamp())
        train = [(t, v) for t, v in series if t < day_start]
        if len({datetime.fromtimestamp(t, tz).date() for t, _ in train}) < 7:
            continue
        prof = Profile(train, tz, day_start, bin_minutes, half_life_days)
        for (bd, s), v in full.binned.items():
            if bd != d:
                continue
            st = prof.seasonal(d.weekday(), s)
            prev = full.binned.get((d - timedelta(days=7), s))
            if st is not None and prev is not None:
                errors.append(abs(v - st.mean))
                naive.append(abs(v - prev))
    if not errors:
        return None
    return {"mae": round(sum(errors) / len(errors), 1),
            "mae_last_week": round(sum(naive) / len(naive), 1), "n": len(errors)}
