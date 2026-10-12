"""Deutsche Feiertage (lokal berechnet), Brueckentage und Club-Standorte.

Schulferien kommen aus der OpenHolidays-API (siehe context.py), weil sie sich nicht berechnen lassen.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache
from typing import Optional


@dataclass(frozen=True)
class Location:
    key: str        # Schluessel fuer Wetter-Cache, z. B. "düsseldorf"
    city: str
    state: str      # Bundesland-Kuerzel, z. B. "NW"
    lat: float
    lon: float


CITIES = {
    "hamburg": ("Hamburg", "HH", 53.551, 9.994),
    "berlin": ("Berlin", "BE", 52.520, 13.405),
    "potsdam": ("Potsdam", "BB", 52.391, 13.064),
    "dresden": ("Dresden", "SN", 51.050, 13.738),
    "leipzig": ("Leipzig", "SN", 51.340, 12.375),
    "köln": ("Köln", "NW", 50.938, 6.960),
    "düsseldorf": ("Düsseldorf", "NW", 51.227, 6.773),
    "bonn": ("Bonn", "NW", 50.735, 7.100),
    "essen": ("Essen", "NW", 51.456, 7.012),
    "dortmund": ("Dortmund", "NW", 51.514, 7.468),
    "münchen": ("München", "BY", 48.137, 11.575),
    "nürnberg": ("Nürnberg", "BY", 49.452, 11.077),
    "wiesbaden": ("Wiesbaden", "HE", 50.078, 8.240),
    "frankfurt": ("Frankfurt am Main", "HE", 50.110, 8.682),
    "mainz": ("Mainz", "RP", 49.993, 8.247),
    "stuttgart": ("Stuttgart", "BW", 48.776, 9.183),
    "hannover": ("Hannover", "NI", 52.375, 9.732),
    "bremen": ("Bremen", "HB", 53.079, 8.802),
}
ALIASES = {"koeln": "köln", "cologne": "köln", "duesseldorf": "düsseldorf", "dusseldorf": "düsseldorf",
           "muenchen": "münchen", "munich": "münchen", "nuernberg": "nürnberg", "nuremberg": "nürnberg"}

# Clubnamen der EVO-API enthalten keine Stadt ("EVO Unterbilk"): Stichwort -> Stadtschluessel.
CLUB_CITIES = {
    "alexanderplatz": "berlin", "rosenthalerplatz": "berlin", "spittelmarkt": "berlin",
    "frankfurtertor": "berlin", "westend": "berlin", "westkreuz": "berlin",
    "europacity": "berlin", "neuemälzerei": "berlin",
    "pragerstraße": "dresden", "pragerstrasse": "dresden",
    "leflair": "düsseldorf", "unterbilk": "düsseldorf",
    "blankenese": "hamburg", "ottensen": "hamburg", "stadthausbrücke": "hamburg", "schanze": "hamburg",
    "belgischesviertel": "köln", "ehrenstr": "köln",
    "maxvorstadt": "münchen", "glockenbach": "münchen", "lehel": "münchen", "rotkreuzplatz": "münchen",
    "dernschehöfe": "wiesbaden",
}


def _letters_only(s: str) -> str:
    """Kleingeschrieben, nur Buchstaben (inkl. Umlaute) -- "EVO Dern'sche Höfe" -> "evodernschehöfe"."""
    return "".join(ch for ch in s.lower() if ch.isalpha())


def city_location(name: str) -> Optional[Location]:
    key = ALIASES.get(name.strip().lower(), name.strip().lower())
    if key not in CITIES:
        return None
    city, state, lat, lon = CITIES[key]
    return Location(key, city, state, lat, lon)


def locate(gym: str, default_city: str, overrides: Optional[dict] = None) -> Location:
    """Standort eines Clubs: Override aus der Config, sonst Stadtname im Clubnamen, sonst Default."""
    ov = (overrides or {}).get(gym)
    if isinstance(ov, str):
        loc = city_location(ov)
        if loc:
            return loc
    elif isinstance(ov, dict) and {"lat", "lon", "state"} <= set(ov):
        key = ov.get("city", gym).lower()
        return Location(key, ov.get("city", gym), ov["state"], float(ov["lat"]), float(ov["lon"]))
    low = gym.lower()
    for alias, key in list(ALIASES.items()) + [(k, k) for k in CITIES]:
        if alias in low:
            return city_location(key)  # type: ignore[return-value]
    flat = _letters_only(gym)
    for word, key in CLUB_CITIES.items():
        if word in flat:
            return city_location(key)  # type: ignore[return-value]
    return city_location(default_city) or city_location("düsseldorf")  # type: ignore[return-value]


def easter_sunday(year: int) -> date:
    """Gauss/Anonymous-Gregorian-Algorithmus."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


@lru_cache(maxsize=256)
def public_holidays(year: int, state: str) -> dict[date, str]:
    """Gesetzliche Feiertage plus Heiligabend/Silvester (dort sind Gyms erfahrungsgemaess wie sonntags)."""
    e = easter_sunday(year)
    days = {
        date(year, 1, 1): "Neujahr",
        e - timedelta(days=2): "Karfreitag",
        e + timedelta(days=1): "Ostermontag",
        date(year, 5, 1): "Tag der Arbeit",
        e + timedelta(days=39): "Christi Himmelfahrt",
        e + timedelta(days=50): "Pfingstmontag",
        date(year, 10, 3): "Tag der Deutschen Einheit",
        date(year, 12, 24): "Heiligabend",
        date(year, 12, 25): "1. Weihnachtstag",
        date(year, 12, 26): "2. Weihnachtstag",
        date(year, 12, 31): "Silvester",
    }
    if state in ("BW", "BY", "ST"):
        days[date(year, 1, 6)] = "Heilige Drei Könige"
    if state == "BE" or (state == "MV" and year >= 2023):
        days[date(year, 3, 8)] = "Internationaler Frauentag"
    if state in ("BW", "BY", "HE", "NW", "RP", "SL"):
        days[e + timedelta(days=60)] = "Fronleichnam"
    if state in ("BY", "SL"):
        days[date(year, 8, 15)] = "Mariä Himmelfahrt"
    if state == "TH":
        days[date(year, 9, 20)] = "Weltkindertag"
    if state in ("BB", "HB", "HH", "MV", "NI", "SN", "ST", "SH", "TH"):
        days[date(year, 10, 31)] = "Reformationstag"
    if state in ("BW", "BY", "NW", "RP", "SL"):
        days[date(year, 11, 1)] = "Allerheiligen"
    if state == "SN":
        d = date(year, 11, 22)
        while d.weekday() != 2:
            d -= timedelta(days=1)
        days[d] = "Buß- und Bettag"
    return days


def holiday_name(d: date, state: str) -> Optional[str]:
    return public_holidays(d.year, state).get(d)


def is_bridge_day(d: date, state: str) -> bool:
    """Werktag zwischen Feiertag und Wochenende, oder 'zwischen den Jahren'."""
    if d.weekday() >= 5 or holiday_name(d, state):
        return False
    if d.month == 12 and 27 <= d.day <= 30:
        return True
    prev, nxt = d - timedelta(days=1), d + timedelta(days=1)

    def off(x: date) -> bool:
        return x.weekday() >= 5 or holiday_name(x, state) is not None

    return (holiday_name(prev, state) is not None and off(nxt)) or (holiday_name(nxt, state) is not None and off(prev))
