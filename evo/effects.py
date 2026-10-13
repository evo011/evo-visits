"""Multiplikative Wetter- und Kalendereffekte auf ein Wochenprofil.

Modell: Jede Beobachtung ist ein (Datum, Slot)-Mittelwert v der Besucherzahl.
Der Profilwert s beschreibt den typischen Wert unter Normalbedingungen. Die
Zielgroesse y = log((v + 3) / (s + 3)) macht die Effekte multiplikativ:
Prognose = (s + 3) * exp(x . beta) - 3, unten bei 0 abgeschnitten.

Features (Reihenfolge fest): rain (Niederschlag mm/h, gesaettigt bei 1 mm),
warm (je 10 Grad ueber 18), cold (je 10 Grad unter 8), sun (Sonnenstunden je
Stunde, 0..1), school (Schulferien, 0/1), bridge (Brueckentag, 0/1).

Schaetzung: Ridge-Regression ohne Achsenabschnitt, beta = (X'X + lam I)^-1 X'y.
Features mit weniger aktiven Tagen als MIN_ACTIVE_DAYS bleiben exakt 0
(aktiv: rain ab 0.2, warm/cold ueber 0, sun ab 0.5, school/bridge = 1).
Unsicherheit per Tages-Block-Bootstrap: Tage werden mit Zuruecklegen gezogen,
die je Tag vorab summierten X'X- und X'y-Beitraege nur noch addiert; das CI
sind die 10./90. Perzentile der Bootstrap-Betas.

Gemeinsame Schaetzung (pooled): fit_effects_pooled schaetzt die Effekte
gleichzeitig ueber alle Gruppen, z.B. einen Regeneffekt aus den Daten aller
Staedte. Aktive Tage zaehlen dort je Stadt und Datum, Bootstrap-Bloecke sind
Kalendertage ueber alle Gruppen hinweg.

Backfitting: Regentage druecken das Profil, Ferientage heben es. Deshalb
werden die Werte um die geschaetzten Effekte bereinigt und das Profil daraus
neu gebaut, so bildet es Normalbedingungen ab.

Band: Punktprognose +/- z * Wurzel aus Residual-SD (je Stunde), Parameter-SD
(x' cov x auf Log-Skala), Wetter- und Feiertagsunsicherheit. Das Band wird
nicht an der Kapazitaet gekappt, echte Werte liegen teils darueber.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Callable, Optional

FEATURES = ("rain", "warm", "cold", "sun", "school", "bridge")
WEATHER_FEATURES = ("rain", "warm", "cold", "sun")
MIN_ACTIVE_DAYS = {"rain": 4, "warm": 4, "cold": 4, "sun": 4, "school": 3, "bridge": 2}
LAMBDA = 10.0
OFFSET = 3.0

# Ab wann ein Datum als "aktiver Tag" eines Features zaehlt.
_ACTIVE_MIN = {"rain": 0.2, "warm": 0.0, "cold": 0.0, "sun": 0.5, "school": 1.0, "bridge": 1.0}


def weather_x(w) -> Optional[tuple]:
    """(rain, warm, cold, sun) fuer ein Objekt mit temp/precip/sunshine, sonst None.

    precip und sunshine duerfen None sein (gezaehlt als 0); ohne Temperatur
    gibt es kein Wetter-Feature fuer die Zeile.
    """
    if w is None or w.temp is None:
        return None
    precip = w.precip if w.precip is not None else 0.0
    sunshine = w.sunshine if w.sunshine is not None else 0.0
    return (min(precip, 1.0),
            max(0.0, w.temp - 18.0) / 10.0,
            max(0.0, 8.0 - w.temp) / 10.0,
            min(max(sunshine / 3600.0, 0.0), 1.0))


def feature_row(w, school: bool, bridge: bool) -> tuple:
    """(x in FEATURES-Reihenfolge, has_weather) fuer eine Beobachtung."""
    wx = weather_x(w)
    has_weather = wx is not None
    rain, warm, cold, sun = wx if has_weather else (0.0, 0.0, 0.0, 0.0)
    x = (rain, warm, cold, sun, 1.0 if school else 0.0, 1.0 if bridge else 0.0)
    return x, has_weather


def _is_active(feature: str, x: float) -> bool:
    threshold = _ACTIVE_MIN[feature]
    if feature in ("warm", "cold"):
        return x > threshold
    return x >= threshold


@dataclass(frozen=True)
class Row:
    day: date
    slot: int
    v: float          # beobachteter Wert
    x: tuple          # Features in FEATURES-Reihenfolge
    has_weather: bool
    group: str = ""   # Clubname
    loc: str = ""     # Stadt-Schluessel


@dataclass
class EffectFit:
    beta: dict                 # log-Effekt je Feature, 0.0 fuer nicht genutzte
    ci: dict                   # 10./90. Perzentil der Bootstrap-Betas (log)
    active_days: dict
    used: dict                 # genug aktive Tage
    cov: list                  # 6x6 Bootstrap-Kovarianz (0 fuer nicht genutzte)
    feature_sd: dict           # SD jedes Features ueber die Trainingszeilen
    n_rows: int
    n_days: int

    def log_effect(self, x) -> float:
        return sum(x[i] * self.beta[f] for i, f in enumerate(FEATURES))

    def multiplier(self, x) -> float:
        return math.exp(self.log_effect(x))

    def param_sd(self, x) -> float:
        s = 0.0
        for i in range(len(FEATURES)):
            row = self.cov[i]
            for j in range(len(FEATURES)):
                s += x[i] * row[j] * x[j]
        return math.sqrt(max(0.0, s))

    def to_dict(self) -> dict:
        out = {}
        for f in FEATURES:
            b = self.beta[f]
            lo, hi = self.ci[f]
            out[f] = {"effect_pct": round((math.exp(b) - 1.0) * 100.0, 1),
                      "lo_pct": round((math.exp(lo) - 1.0) * 100.0, 1),
                      "hi_pct": round((math.exp(hi) - 1.0) * 100.0, 1),
                      "active_days": self.active_days[f],
                      "min_days": MIN_ACTIVE_DAYS[f],
                      "used": self.used[f]}
        return out


def _percentile(values: list, q: float) -> float:
    """Lineares q-Quantil (q in 0..1) nach Sortierung."""
    vs = sorted(values)
    if not vs:
        return 0.0
    pos = (len(vs) - 1) * q
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(vs) - 1)
    frac = pos - lo
    return vs[lo] * (1.0 - frac) + vs[hi] * frac


def _solve(A: list, b: list) -> list:
    """Loest A x = b per Gauss-Elimination mit Spaltenpivot, maximal 6x6."""
    n = len(b)
    M = [list(A[i]) + [b[i]] for i in range(n)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(M[r][col]))
        if abs(M[piv][col]) < 1e-12:
            return [0.0] * n
        if piv != col:
            M[col], M[piv] = M[piv], M[col]
        mcol = M[col]
        for r in range(col + 1, n):
            mrow = M[r]
            f = mrow[col] / mcol[col]
            if f:
                for c in range(col, n + 1):
                    mrow[c] -= f * mcol[c]
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        mi = M[i]
        s = mi[n] - sum(mi[j] * x[j] for j in range(i + 1, n))
        x[i] = s / mi[i]
    return x


def _fit(data: list, active_key: Callable, block_key: Callable,
         lam: float, n_boot: int, seed: int) -> EffectFit:
    """Ridge-Fit ueber (row, y)-Paare; aktiv/Block werden je Zeile bestimmt.

    active_key(row) definiert, was als ein "aktiver Tag" eines Features zaehlt,
    block_key(row) den Bootstrap-Block. n_days ist die Anzahl der Bloecke.
    """
    days = sorted({block_key(r) for r, _ in data})
    n_rows = len(data)

    active = {f: set() for f in FEATURES}
    for r, _ in data:
        for i, f in enumerate(FEATURES):
            if _is_active(f, r.x[i]):
                active[f].add(active_key(r))
    active_days = {f: len(active[f]) for f in FEATURES}
    used = {f: active_days[f] >= MIN_ACTIVE_DAYS[f] for f in FEATURES}
    if len(days) < 2:
        used = {f: False for f in FEATURES}

    feature_sd = {f: 0.0 for f in FEATURES}
    if n_rows:
        for i, f in enumerate(FEATURES):
            vals = [r.x[i] for r, _ in data]
            m = sum(vals) / n_rows
            feature_sd[f] = math.sqrt(sum((v - m) ** 2 for v in vals) / n_rows)

    n_feats = len(FEATURES)
    fit = EffectFit(beta={f: 0.0 for f in FEATURES},
                    ci={f: (0.0, 0.0) for f in FEATURES},
                    active_days=active_days, used=used,
                    cov=[[0.0] * n_feats for _ in range(n_feats)],
                    feature_sd=feature_sd, n_rows=n_rows, n_days=len(days))

    feats = [f for f in FEATURES if used[f]]
    if not feats:
        return fit
    k = len(feats)
    idx = [FEATURES.index(f) for f in feats]

    # X'X und X'y je Block vorab summieren, der Bootstrap addiert nur noch Bloecke.
    day_xtx = {d: [[0.0] * k for _ in range(k)] for d in days}
    day_xty = {d: [0.0] * k for d in days}
    for r, y in data:
        xs = [r.x[i] for i in idx]
        M = day_xtx[block_key(r)]
        b = day_xty[block_key(r)]
        for a in range(k):
            xa = xs[a]
            if xa:
                b[a] += xa * y
            Ma = M[a]
            for c in range(a, k):
                Ma[c] += xa * xs[c]
    for M in day_xtx.values():
        for a in range(k):
            for c in range(a):
                M[a][c] = M[c][a]

    A = [[0.0] * k for _ in range(k)]
    rhs = [0.0] * k
    for d in days:
        M = day_xtx[d]
        b = day_xty[d]
        for a in range(k):
            rhs[a] += b[a]
            Ma = M[a]
            Aa = A[a]
            for c in range(k):
                Aa[c] += Ma[c]
    for a in range(k):
        A[a][a] += lam
    beta_hat = _solve(A, rhs)
    for a, f in enumerate(feats):
        fit.beta[f] = beta_hat[a]

    # Tages-Block-Bootstrap
    rng = random.Random(seed)
    mats = [day_xtx[d] for d in days]
    vecs = [day_xty[d] for d in days]
    n_days = len(days)
    cols = [[] for _ in range(k)]
    if n_boot < 2:
        # Ohne Bootstrap gibt es keine Streuung: CI auf den Punktschaetzer, Kovarianz 0.
        for a, f in enumerate(feats):
            fit.ci[f] = (fit.beta[f], fit.beta[f])
        return fit
    for _ in range(n_boot):
        B = [[0.0] * k for _ in range(k)]
        brhs = [0.0] * k
        for _ in range(n_days):
            i = rng.randrange(n_days)
            M = mats[i]
            b = vecs[i]
            for a in range(k):
                brhs[a] += b[a]
                Ma = M[a]
                Ba = B[a]
                for c in range(a, k):
                    Ba[c] += Ma[c]
        for a in range(k):
            Ba = B[a]
            for c in range(a):
                Ba[c] = B[c][a]
            Ba[a] += lam
        sol = _solve(B, brhs)
        for a in range(k):
            cols[a].append(sol[a])

    n_b = len(cols[0])
    means = [sum(c) / n_b for c in cols]
    for a, f in enumerate(feats):
        fit.ci[f] = (_percentile(cols[a], 0.1), _percentile(cols[a], 0.9))
    for a in range(k):
        for c in range(k):
            s = sum((cols[a][i] - means[a]) * (cols[c][i] - means[c]) for i in range(n_b))
            fit.cov[idx[a]][idx[c]] = s / (n_b - 1)
    return fit


def fit_effects(rows: list, profile_fn: Callable[[date, int], Optional[float]],
                lam: float = LAMBDA, n_boot: int = 200, seed: int = 0) -> EffectFit:
    """Schaetzt die Effekte gegen ein gegebenes Profil (Normalbedingungen)."""
    data = []
    for r in rows:
        s = profile_fn(r.day, r.slot)
        if s is None:
            continue
        data.append((r, math.log((r.v + OFFSET) / (s + OFFSET))))
    return _fit(data, active_key=lambda r: r.day, block_key=lambda r: r.day,
                lam=lam, n_boot=n_boot, seed=seed)


def fit_effects_pooled(rows: list,
                       profile_fns: dict,
                       lam: float = LAMBDA, n_boot: int = 200, seed: int = 0) -> EffectFit:
    """Schaetzt die Effekte gemeinsam ueber alle Gruppen (je Gruppe ein Profil).

    Aktive Tage zaehlen je Stadt und Datum (row.loc), Bootstrap-Bloecke sind
    Kalendertage ueber alle Gruppen hinweg. Zeilen ohne Profil-Funktion fuer
    ihre Gruppe oder mit Profilwert None fallen weg.
    """
    data = []
    for r in rows:
        fn = profile_fns.get(r.group)
        if fn is None:
            continue
        s = fn(r.day, r.slot)
        if s is None:
            continue
        data.append((r, math.log((r.v + OFFSET) / (s + OFFSET))))
    return _fit(data, active_key=lambda r: (r.loc, r.day), block_key=lambda r: r.day,
                lam=lam, n_boot=n_boot, seed=seed)


def backfit(rows: list, build_profile: Callable[[dict], Callable[[date, int], Optional[float]]],
            rounds: int = 2, **fit_kwargs) -> tuple:
    """Effekte fitten, Werte bereinigen, Profil neu bauen - wiederholt.

    build_profile bekommt {(datum, slot): wert} und liefert ein profile_fn.
    Zurueck kommen der letzte Fit und das letzte (bereinigte) Profil.
    """
    values = {(r.day, r.slot): r.v for r in rows}
    profile = build_profile(values)
    fit = fit_effects(rows, profile, **fit_kwargs)
    for _ in range(rounds):
        cleaned = {}
        for r in rows:
            m = math.exp(fit.log_effect(r.x))
            cleaned[(r.day, r.slot)] = max(0.0, (r.v + OFFSET) / m - OFFSET)
        profile = build_profile(cleaned)
        fit = fit_effects(rows, profile, **fit_kwargs)
    return fit, profile


def backfit_pooled(rows: list,
                   build_profiles: Callable[[dict], dict],
                   rounds: int = 2, **fit_kwargs) -> tuple:
    """Gemeinsames Backfitting: Profile je Gruppe, ein Fit ueber alle Gruppen.

    build_profiles bekommt {gruppe: {(datum, slot): wert}} und liefert
    {gruppe: profile_fn}. Zurueck kommen der letzte Fit und die letzten
    (bereinigten) Profil-Funktionen.
    """
    values = defaultdict(dict)
    for r in rows:
        values[r.group][(r.day, r.slot)] = r.v
    profiles = build_profiles(dict(values))
    fit = fit_effects_pooled(rows, profiles, **fit_kwargs)
    for _ in range(rounds):
        cleaned = defaultdict(dict)
        for r in rows:
            m = math.exp(fit.log_effect(r.x))
            cleaned[r.group][(r.day, r.slot)] = max(0.0, (r.v + OFFSET) / m - OFFSET)
        profiles = build_profiles(dict(cleaned))
        fit = fit_effects_pooled(rows, profiles, **fit_kwargs)
    return fit, profiles


def _sd(values: list) -> float:
    if not values:
        return 0.0
    m = sum(values) / len(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / len(values))


def residual_sd_by_hour(triples: list) -> dict:
    """SD von (v - prognose) je Stunde; duenn belegte Stunden nehmen die Gesamt-SD."""
    by_hour = defaultdict(list)
    for hour, v, prog in triples:
        by_hour[hour].append(v - prog)
    if not by_hour:
        return {}
    total = _sd([d for ds in by_hour.values() for d in ds])
    out = {}
    for h, ds in by_hour.items():
        sd = _sd(ds) if len(ds) >= 8 else total
        out[h] = max(1.0, sd)
    return out


def weather_sd(fit: EffectFit, lead_hours: float) -> float:
    """Log-Skala-Unsicherheit durch Wetter; waechst mit dem Vorlauf, Deckel bei 96 h."""
    scale = min(1.0, 0.2 + lead_hours / 96.0)
    s = sum((fit.beta[f] * fit.feature_sd[f]) ** 2 for f in WEATHER_FEATURES)
    return scale * math.sqrt(s)


def holiday_sd(n_holiday_days_in_data: int) -> float:
    """Log-Skala-Unsicherheit fuer Feiertage, sinkt mit beobachteten Feiertagen."""
    return 0.35 / math.sqrt(1 + n_holiday_days_in_data)


def band(est: float, sd_res: float, sd_param: float, sd_weather: float,
         sd_holiday: float, z: float = 1.2816) -> tuple:
    """80-%-Prognoseband (z = 1.2816), unten bei 0, oben offen."""
    sd = math.sqrt(sd_res ** 2 + (est * sd_param) ** 2
                  + (est * sd_weather) ** 2 + (est * sd_holiday) ** 2)
    return max(0.0, est - z * sd), est + z * sd
