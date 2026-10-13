"""Pool-Prior: gemeinsames Tagesprofil aller Clubs.

Idee: jeder Club hat sein eigenes Niveau (Faktor), die Tagesform darueber ist
allen Clubs gemeinsam. Zunaechst werden die Niveaus alternativ geschaetzt
(`club_levels`), dann bilden die niveaunormierten Werte ein gemeinsames
Profil (`PoolShape`). Dessen `prior_for(gym)` skaliert die gemeinsame Form
zurueck auf das Club-Niveau und dient `Profile` als Prior.
"""

from __future__ import annotations

from datetime import date
from typing import Callable, Optional

from evo.forecast import SHRINK_K, Stat, _collect_observations, _wstats

Binned = dict[tuple[date, int], float]


def club_levels(binned_by_gym: dict[str, Binned],
                day_kind_by_gym: dict[str, Callable[[date], int]],
                today: date, half_life_days: float = 21.0,
                iterations: int = 5) -> dict[str, float]:
    """Niveaus je Club schaetzen: alternierende Tagesform- und Niveau-Schaetzung.

    Nur Beobachtungen mit datum < today, Altersgewicht mit Halbwertszeit.
    Rueckgabe normiert auf Mittelwert 1, nur Clubs mit mindestens einer
    Beobachtung.
    """
    gyms = [g for g, binned in binned_by_gym.items() if binned]
    obs: dict[str, list[tuple[date, int, float, float]]] = {}
    for g in gyms:
        rows = [(d, s, v, 0.5 ** ((today - d).days / half_life_days))
                for (d, s), v in binned_by_gym[g].items() if d < today]
        if rows:
            obs[g] = rows
    gyms = [g for g in gyms if g in obs]
    if not gyms:
        return {}

    levels = {g: 1.0 for g in gyms}
    for _ in range(iterations):
        # (a) gemeinsame Tagesform je (Tagesart, Slot) aus den normierten Werten
        shape_acc: dict[tuple[int, int], list[tuple[float, float]]] = {}
        for g in gyms:
            if levels[g] <= 0:
                continue
            art_f = day_kind_by_gym[g]
            for d, s, v, w in obs[g]:
                shape_acc.setdefault((art_f(d), s), []).append((v / levels[g], w))
        shape = {k: sum(v * w for v, w in rows) / sum(w for _, w in rows)
                 for k, rows in shape_acc.items() if sum(w for _, w in rows) > 0}

        # (b) Niveau je Club: gewichtete Mittel der wert/shape-Verhaeltnisse
        for g in gyms:
            art_f = day_kind_by_gym[g]
            num = den = 0.0
            for d, s, v, w in obs[g]:
                f = shape.get((art_f(d), s))
                if f is not None:
                    num += w * v
                    den += w * f
            levels[g] = num / den if den > 0 else 1.0

        # (c) normieren, Mittelwert aller Niveaus = 1
        mean = sum(levels.values()) / len(levels)
        if mean > 0:
            levels = {g: l / mean for g, l in levels.items()}
    return levels


class PoolShape:
    """Gemeinsames Tagesprofil (niveaunormiert) aller Clubs."""

    def __init__(self, binned_by_gym: dict[str, Binned],
                 day_kind_by_gym: dict[str, Callable[[date], int]],
                 levels: dict[str, float], today: date,
                 bin_minutes: int = 15, half_life_days: float = 21.0):
        self.bin = bin_minutes
        self.slots = 24 * 60 // bin_minutes
        self.levels = dict(levels)
        by_wd: dict[tuple[int, int], list[tuple[float, float]]] = {}
        by_type: dict[tuple[bool, int], list[tuple[float, float]]] = {}
        by_all: dict[int, list[tuple[float, float]]] = {}
        days_seen: set[date] = set()
        for g, binned in binned_by_gym.items():
            l = self.levels.get(g)
            if l is None or l <= 0:
                continue
            scaled = {k: v / l for k, v in binned.items()}
            w_by, w_ty, w_al, seen = _collect_observations(
                scaled, day_kind_by_gym[g], today, half_life_days, self.slots)
            for k, rows in w_by.items():
                by_wd.setdefault(k, []).extend(rows)
            for k, rows in w_ty.items():
                by_type.setdefault(k, []).extend(rows)
            for k, rows in w_al.items():
                by_all.setdefault(k, []).extend(rows)
            days_seen |= seen
        self.by_wd, self.by_type, self.by_all, self.days_seen = by_wd, by_type, by_all, days_seen
        self._cache: dict[tuple[int, int], Optional[Stat]] = {}

    def stat(self, kind: int, slot: int) -> Optional[Stat]:
        """Prognose der gemeinsamen Form; Schrumpfung wie Profile.seasonal."""
        key = (kind, slot)
        if key in self._cache:
            return self._cache[key]
        same = _wstats(self.by_wd.get(key, []))
        typ = _wstats(self.by_type.get((kind >= 5, slot), []))
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

    def prior_for(self, gym: str) -> Callable[[int, int], Optional[Stat]]:
        """Prior-Funktion fuer einen Club: gemeinsame Form mal Niveau."""
        l = self.levels.get(gym)
        if l is None:
            return lambda weekday, slot: None
        return lambda weekday, slot: _scale(self.stat(weekday, slot), l)


def _scale(st: Optional[Stat], l: float) -> Optional[Stat]:
    return None if st is None else Stat(st.mean * l, st.std * l, st.weight)


def build_prior(binned_by_gym: dict[str, Binned],
                day_kind_by_gym: dict[str, Callable[[date], int]],
                today: date, bin_minutes: int = 15,
                half_life_days: float = 21.0) -> tuple[dict[str, float], PoolShape]:
    levels = club_levels(binned_by_gym, day_kind_by_gym, today, half_life_days)
    shape = PoolShape(binned_by_gym, day_kind_by_gym, levels, today,
                      bin_minutes, half_life_days)
    return levels, shape
