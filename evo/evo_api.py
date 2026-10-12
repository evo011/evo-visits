"""Echte EVO-API (visits.evofitness.de) als bevorzugter Abrufweg.

Die Seite laedt per JavaScript zwei Endpunkte:
  1. /api/v1/locations?operator=<operatorId>  -> Club-Liste [{"id": ..., "name": ...}]
  2. /api/v1/locations/<id>/current           -> {"name", "max_capacity", "current", "percentageUsed"}
`current` darf groesser als `max_capacity` sein und wird nicht gekappt.
Der /timeline-Endpunkt liefert kumulative Werte und wird nicht benutzt.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Optional
from urllib.parse import urlparse

from . import fetch as _fetch
from .config import Config
from .extract import clean_name
from .fetch import FetchError, FetchResult, ProbeLog
from .store import Reading

KNOWN_OPERATORS = {"visits.evofitness.de": "c04bc7c3-903f-4804-a952-9abc0016b824"}


def http_get(url: str, cfg: Config) -> tuple[str, str]:
    """Duennes HTTP-Wrapper; Tests patchen genau diese Funktion."""
    return _fetch.http_get(url, cfg, accept="application/json")


def _base_url(cfg: Config) -> str:
    parsed = urlparse(cfg.url)
    return f"{parsed.scheme or 'https'}://{parsed.netloc}"


def operator_id(cfg: Config) -> str:
    override = cfg.extra.get("operator_id")
    if override:
        return str(override)
    host = urlparse(cfg.url).netloc
    if host in KNOWN_OPERATORS:
        return KNOWN_OPERATORS[host]
    _, text = http_get(f"{_base_url(cfg)}/js/config.js", cfg)
    m = re.search(
        r"'" + re.escape(host) + r"'\s*:\s*\{[^}]*?operatorId:\s*'([0-9a-f-]{36})'", text)
    if not m:
        raise FetchError(f"Operator-ID fuer {host} nicht gefunden (js/config.js geprueft)")
    return m.group(1)


def parse_locations(data: Any) -> list[tuple[str, str]]:
    """(id, name) je Club; Eintraege mit fehlenden oder ungueltigen Feldern werden uebersprungen."""
    out: list[tuple[str, str]] = []
    if not isinstance(data, list):
        return out
    for item in data:
        if not isinstance(item, dict):
            continue
        loc_id, name = item.get("id"), item.get("name")
        if isinstance(loc_id, str) and loc_id and isinstance(name, str) and name:
            out.append((loc_id, clean_name(name)))
    return out


def parse_current(data: Any) -> Optional[Reading]:
    if not isinstance(data, dict):
        return None
    name = data.get("name")
    if not isinstance(name, str) or not name:
        return None

    def to_f(key: str) -> Optional[float]:
        v = data.get(key)
        return float(v) if isinstance(v, (int, float)) else None

    pct = to_f("percentageUsed")
    return Reading(gym=clean_name(name), count=to_f("current"), capacity=to_f("max_capacity"),
                    pct=None if pct is None else round(pct, 1))


def fetch_evo_api(cfg: Config, log: Optional[ProbeLog] = None) -> Optional[FetchResult]:
    base = _base_url(cfg)
    op = operator_id(cfg)
    _, text = http_get(f"{base}/api/v1/locations?operator={op}", cfg)
    try:
        locations = parse_locations(json.loads(text))
    except ValueError:
        raise FetchError(f"{base}/api/v1/locations: keine gueltige JSON-Antwort")
    if not locations:
        return None
    if log:
        log.files["evo_locations.json"] = text
        log.add(f"[evo-api] {len(locations)} Clubs geladen (operator {op})")

    delay = float(cfg.extra.get("request_delay", 0.3))
    readings: list[Reading] = []
    for i, (loc_id, name) in enumerate(locations):
        if i and delay > 0:
            time.sleep(delay)
        try:
            _, body = http_get(f"{base}/api/v1/locations/{loc_id}/current", cfg)
            reading = parse_current(json.loads(body))
        except (FetchError, ValueError) as e:
            if log:
                log.add(f"  x {name}: {e}")
            continue
        if reading is None:
            if log:
                log.add(f"  x {name}: keine verwertbaren Felder")
            continue
        readings.append(reading)
        if log:
            log.files[f"evo_current_{i}.json"] = body
            log.add(f"  {reading.gym}: count={reading.count} capacity={reading.capacity} "
                    f"pct={reading.pct}")
    if len(readings) * 2 < len(locations):
        raise FetchError(f"evo-api: nur {len(readings)} von {len(locations)} Clubs abrufbar")
    return FetchResult(readings, "evo-api:v1")
