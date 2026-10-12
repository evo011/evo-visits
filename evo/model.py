"""Prognosemodell mit Wetter-, Ferien- und Feiertagseffekten.

FeatureSource besorgt Kalender- und Wettermerkmale fuer einen Standort,
Model verheiratet das Wochenprofil (forecast.Profile) mit den geschaetzten
multiplikativen Effekten (effects.py) zu einer Prognose mit Band.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from typing import Optional

from . import effects
from .calendar_de import Location, holiday_name, is_bridge_day
from .context import WeatherHour
from .forecast import NOWCAST_LOOKBACK_MIN, NOWCAST_TAU_MIN, Profile


class FeatureSource:
    """Kalender und Wetter fuer einen Standort."""

    def __init__(self, loc: Location, tz, ctx=None, start_ts: int = 0, end_ts: int = 0):
        self.loc = loc
        self.tz = tz
        self.ctx = ctx
        self.weather: dict[int, WeatherHour] = {}
        if ctx is not None:
            self.weather = ctx.weather(loc.key, start_ts - 7200, end_ts + 7200)
        self._memo: dict[str, dict[date, object]] = {}

    def holiday(self, d: date) -> Optional[str]:
        m = self._memo.setdefault("holiday", {})
        if d not in m:
            m[d] = holiday_name(d, self.loc.state)
        return m[d]

    def school(self, d: date) -> Optional[str]:
        m = self._memo.setdefault("school", {})
        if d not in m:
            m[d] = self.ctx.school_holiday(self.loc.state, d) if self.ctx is not None else None
        return m[d]

    def bridge(self, d: date) -> bool:
        m = self._memo.setdefault("bridge", {})
        if d not in m:
            m[d] = is_bridge_day(d, self.loc.state)
        return m[d]

    def day_kind(self, d: date) -> int:
        return 6 if self.holiday(d) else d.weekday()

    def weather_at(self, ts: int) -> Optional[WeatherHour]:
        # Open-Meteo stempelt Niederschlag/Sonne auf das Ende der vorherigen Stunde
        start = ts - ts % 3600
        return self.weather.get(start + 3600) or self.weather.get(start)

    def x(self, ts: int) -> tuple[tuple, bool]:
        d = datetime.fromtimestamp(ts, self.tz).date()
        return effects.feature_row(self.weather_at(ts), self.school(d) is not None, self.bridge(d))


class Model:
    def __init__(self, series, tz, now_ts: int, feats: FeatureSource, bin_minutes: int = 15,
                 half_life_days: float = 21.0, use_effects: bool = True, n_boot: int = 200):
        self.tz = tz
        self.bin = bin_minutes
        self.now_ts = now_ts
        self.feats = feats
        self.use_effects = use_effects
        self.verdict = "ungeprüft"  # wird von build_model aus dem Backtest gesetzt

        # Rohwerte wie Profile in (Datum, Slot) mitteln
        binned = Profile(series, tz, now_ts, bin_minutes, half_life_days).binned

        today = datetime.fromtimestamp(now_ts, tz).date()
        step = bin_minutes * 60
        built: list[Profile] = []

        def make_profile(values):
            return Profile([], tz, now_ts, bin_minutes, half_life_days,
                           day_kind=feats.day_kind, binned=values)

        def build_profile(values):
            prof = make_profile(values)
            built.append(prof)

            def profile_fn(d, slot):
                st = prof.seasonal_for(d, slot)
                return st.mean if st is not None else None

            return profile_fn

        rows = []
        for (d, slot), v in binned.items():
            if d >= today:
                continue
            ts = int(datetime(d.year, d.month, d.day, tzinfo=tz).timestamp()) + slot * step
            x, has_weather = feats.x(ts)
            rows.append(effects.Row(d, slot, v, x, has_weather))

        self.n_days = len({r.day for r in rows})
        self.n_rows = len(rows)
        # Feiertage in den Trainingsdaten (echte Feiertage, nicht die Sonntage,
        # die day_kind ebenfalls auf 6 abbildet)
        self.n_holiday_days = len({r.day for r in rows if feats.holiday(r.day) is not None})

        if use_effects:
            self.fit, _ = effects.backfit(rows, build_profile, rounds=2, n_boot=n_boot)
            self.profile = built[-1]  # letztes bereinigtes Profil aus dem Backfitting
        else:
            self.fit = effects.fit_effects([], lambda d, s: None, n_boot=n_boot)
            self.profile = make_profile({(r.day, r.slot): r.v for r in rows})

        # Residuen-SD je Stunde gegenueber der finalen Prognose
        triples = []
        for r in rows:
            s = self._profile_mean(r.day, r.slot)
            if s is None:
                continue
            pred = max(0.0, (s + effects.OFFSET) * self.fit.multiplier(r.x) - effects.OFFSET)
            hour = (r.slot * bin_minutes) // 60
            triples.append((hour, r.v, pred))
        self.res_sd = effects.residual_sd_by_hour(triples)

    def _profile_mean(self, d: date, slot: int) -> Optional[float]:
        st = self.profile.seasonal_for(d, slot)
        return st.mean if st is not None else None

    def predict(self, ts: int) -> Optional[dict]:
        dt = datetime.fromtimestamp(ts, self.tz)
        d, slot = dt.date(), (dt.hour * 60 + dt.minute) // self.bin
        s = self._profile_mean(d, slot)
        if s is None:
            return None
        x, has_weather = self.feats.x(ts)
        mult = self.fit.multiplier(x)
        est = max(0.0, (s + effects.OFFSET) * mult - effects.OFFSET)

        hour = dt.hour
        sd_res = self.res_sd.get(hour)
        if sd_res is None:
            st = self.profile.seasonal_for(d, slot)
            sd_res = st.std if st is not None else 1.0
        sd_res = max(1.0, sd_res)
        lead_hours = max(0, (ts - self.now_ts) / 3600)
        sd_param = self.fit.param_sd(x)
        sd_weather = effects.weather_sd(self.fit, lead_hours)
        sd_holiday = effects.holiday_sd(self.n_holiday_days) if self.feats.holiday(d) else 0.0
        lo, hi = effects.band(est, sd_res, sd_param, sd_weather, sd_holiday)

        w = self.feats.weather_at(ts)
        weather = None
        if w is not None:
            weather = {"temp": w.temp, "precip": w.precip,
                       "sun_min": round(w.sunshine / 60.0) if w.sunshine is not None else None,
                       "code": w.code}
        return {"ts": ts, "est": round(est, 1), "lo": round(lo, 1), "hi": round(hi, 1),
                "mult": mult, "effect_pct": round((mult - 1.0) * 100.0, 1),
                "weather": weather,
                "holiday": self.feats.holiday(d), "school": self.feats.school(d),
                "bridge": self.feats.bridge(d)}

    def forecast(self, hours: int = 48, recent=None) -> list[dict]:
        recent = recent or []
        step = self.bin * 60
        start = (self.now_ts // step + 1) * step
        offset = self._nowcast_offset(recent)
        out = []
        for ts in range(start, self.now_ts + hours * 3600 + 1, step):
            p = self.predict(ts)
            if p is None:
                d = datetime.fromtimestamp(ts, self.tz).date()
                out.append({"ts": ts, "est": None, "lo": None, "hi": None,
                            "effect_pct": None, "weather": None,
                            "holiday": self.feats.holiday(d),
                            "school": self.feats.school(d),
                            "bridge": self.feats.bridge(d)})
                continue
            est, lo, hi = p["est"], p["lo"], p["hi"]
            if offset is not None:
                decay = math.exp(-((ts - self.now_ts) / 60) / NOWCAST_TAU_MIN)
                est += offset * decay
                lo += offset * decay
                hi += offset * decay
            point = dict(p)
            point.update({"ts": ts, "est": round(max(est, 0.0), 1),
                          "lo": round(max(lo, 0.0), 1), "hi": round(hi, 1)})
            out.append(point)
        return out

    def _nowcast_offset(self, recent: list) -> Optional[float]:
        """Mittlere Abweichung der juengsten Messwerte von der Modellprognose."""
        cutoff = self.now_ts - NOWCAST_LOOKBACK_MIN * 60
        last_recent = max((ts for ts, _ in recent), default=None)
        if last_recent is None or self.now_ts - last_recent > 30 * 60:
            return None
        diffs = []
        for ts, v in recent:
            if ts < cutoff or ts > self.now_ts:
                continue
            p = self.predict(ts)
            if p is not None:
                diffs.append(v - p["est"])
        return sum(diffs) / len(diffs) if diffs else None

    def day_summaries(self, days: int = 7) -> list[dict]:
        today = datetime.fromtimestamp(self.now_ts, self.tz).date()
        out = []
        for i in range(days):
            d = today + timedelta(days=i)
            temps, precips, suns, codes = [], [], [], []
            for hour in range(6, 23):
                ts = int(datetime(d.year, d.month, d.day, hour, tzinfo=self.tz).timestamp())
                w = self.feats.weather_at(ts)
                if w is None:
                    continue
                if w.temp is not None:
                    temps.append(w.temp)
                if w.precip is not None:
                    precips.append(w.precip)
                if w.sunshine is not None:
                    suns.append(w.sunshine)
                if w.code is not None:
                    codes.append(w.code)
            weather = None
            if temps:
                weather = {"tmin": round(min(temps), 1), "tmax": round(max(temps), 1),
                           "precip": round(sum(precips), 1),
                           "sun_h": round(sum(suns) / 3600.0, 1),
                           "code": max(codes) if codes else None}
            effect_pcts = []
            for hour in range(6, 23):
                ts = int(datetime(d.year, d.month, d.day, hour, tzinfo=self.tz).timestamp())
                st = self._profile_mean(d, (hour * 60) // self.bin)
                if st is None:
                    continue
                x, _ = self.feats.x(ts)
                effect_pcts.append((self.fit.multiplier(x) - 1.0) * 100.0)
            out.append({"date": d.isoformat(), "weekday": d.weekday(),
                        "holiday": self.feats.holiday(d), "school": self.feats.school(d),
                        "bridge": self.feats.bridge(d), "weather": weather,
                        "effect_pct": round(sum(effect_pcts) / len(effect_pcts), 1)
                        if effect_pcts else None})
        return out

    def effects_summary(self) -> dict:
        out = self.fit.to_dict()
        for f in effects.FEATURES:
            if not self.fit.used[f]:
                status = "zu wenig Daten"
            elif not self.use_effects:
                status = "verworfen"
            else:
                status = "aktiv"
            out[f]["status"] = status
        out.update({"n_days": self.n_days, "n_rows": self.n_rows,
                    "n_holiday_days": self.n_holiday_days, "use_effects": self.use_effects,
                    "verdict": self.verdict})
        return out


def backtest_compare(series, tz, feats: FeatureSource, bin_minutes: int = 15,
                     half_life_days: float = 21.0, days: int = 7,
                     n_boot: int = 40) -> Optional[dict]:
    """Backtest der letzten `days` Tage: Effekt-Modell vs. reines Profil vs. Vorwoche."""
    if not series:
        return None
    step = bin_minutes * 60
    last_day = datetime.fromtimestamp(series[-1][0], tz).date()
    first_day = last_day - timedelta(days=days - 1)

    # alle (Datum, Slot)-Mittelwerte einmal vorberechnen
    binned_all = Profile(series, tz, int(datetime(last_day.year, last_day.month, last_day.day,
                                                  tzinfo=tz).timestamp())
                         + 86400, bin_minutes, half_life_days).binned

    errors_model, errors_profile, errors_last_week, hits = [], [], [], 0
    n_slots = 0
    n_test_days = 0
    for i in range(days):
        d = first_day + timedelta(days=i)
        day_start = int(datetime(d.year, d.month, d.day, tzinfo=tz).timestamp())
        train = [(t, v) for t, v in series if t < day_start]
        train_days = {datetime.fromtimestamp(t, tz).date() for t, _ in train}
        if len(train_days) < 7:
            continue
        model = Model(train, tz, day_start, feats, bin_minutes, half_life_days,
                       use_effects=True, n_boot=n_boot)
        plain = Model(train, tz, day_start, feats, bin_minutes, half_life_days,
                      use_effects=False, n_boot=n_boot)
        n_test_days += 1
        for slot in range(24 * 60 // bin_minutes):
            v = binned_all.get((d, slot))
            if v is None:
                continue
            ts = day_start + slot * step
            p_model = model.predict(ts)
            p_plain = plain.predict(ts)
            prev = binned_all.get((d - timedelta(days=7), slot))
            if p_model is None or p_plain is None or prev is None:
                continue
            errors_model.append(abs(v - p_model["est"]))
            errors_profile.append(abs(v - p_plain["est"]))
            errors_last_week.append(abs(v - prev))
            if p_model["lo"] <= v <= p_model["hi"]:
                hits += 1
            n_slots += 1
    if not n_slots:
        return None
    return {"mae_model": round(sum(errors_model) / n_slots, 1),
            "mae_profile": round(sum(errors_profile) / n_slots, 1),
            "mae_last_week": round(sum(errors_last_week) / n_slots, 1),
            "coverage_pct": round(100.0 * hits / n_slots),
            "n": n_slots, "days": n_test_days,
            "first_day": first_day.isoformat(), "last_day": last_day.isoformat(),
            "weather_note": "Backtest mit gemessenem statt vorhergesagtem Wetter"}


def effects_help(bt: Optional[dict]) -> Optional[bool]:
    """True, wenn das Effekt-Modell im Backtest nicht schlechter ist."""
    if bt is None:
        return None
    return bt["mae_model"] <= bt["mae_profile"]


def build_model(series, tz, now_ts, feats: FeatureSource, bin_minutes: int = 15,
                half_life_days: float = 21.0, bt: Optional[dict] = None,
                n_boot: int = 200) -> Model:
    """Live-Modell; Effekte bleiben an, es sei denn der Backtest verwirft sie."""
    help_ = effects_help(bt)
    model = Model(series, tz, now_ts, feats, bin_minutes, half_life_days,
                  use_effects=help_ is not False, n_boot=n_boot)
    model.verdict = "hilft" if help_ else ("verworfen" if help_ is False else "ungeprüft")
    return model
