"""Konfiguration: Defaults, optional ueberschrieben durch config.json im Projektordner."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9 ohne tzdata-Paket
    ZoneInfo = None

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
WEB_DIR = PROJECT_DIR / "web"


def in_active_window(cfg: "Config", ts: float) -> bool:
    """True, wenn ts (Unix-Sekunden) in cfg.active_hours liegt (lokal, cfg.timezone)."""
    try:
        tz = ZoneInfo(cfg.timezone) if ZoneInfo is not None else None
    except Exception:
        tz = None
    dt = datetime.fromtimestamp(ts, tz) if tz is not None else datetime.fromtimestamp(ts)
    minute = dt.hour * 60 + dt.minute
    start, end = cfg.active_window()
    return start <= minute <= end if start <= end else (minute >= start or minute <= end)


@dataclass
class Config:
    url: str = "https://visits.evofitness.de/"
    interval_minutes: int = 10
    # Nur in diesem Fenster wird abgefragt (Clubs haben 6 bis 23 Uhr offen).
    active_hours: str = "05:45-23:15"
    timezone: str = "Europe/Berlin"
    host: str = "127.0.0.1"
    port: int = 8765
    # auto: echte EVO-API, dann plain HTTP, dann Headless-Browser (falls Playwright installiert)
    # static: EVO-API und plain HTTP, kein Browser; browser: nur Playwright; api: nur die echte EVO-API
    mode: str = "auto"
    # Optional eigener Chromium-Pfad fuer den Browser-Modus (sonst der von 'playwright install')
    browser_path: Optional[str] = None
    # Bucket-Groesse fuer Profile und Prognose
    bin_minutes: int = 15
    # Wie viele Wochen Historie in die Prognose eingehen, und wie schnell alte Wochen an Gewicht verlieren
    history_weeks: int = 10
    half_life_days: float = 21.0
    db_path: str = str(DATA_DIR / "visits.db")
    # Optionaler manueller Override, falls die Auto-Erkennung danebenliegt. Beispiel:
    # {"json_url": "https://.../api/clubs", "list_path": "data.clubs",
    #  "name_key": "name", "count_key": "current", "capacity_key": "max"}
    source: Optional[dict[str, Any]] = None
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )
    extra: dict[str, Any] = field(default_factory=dict)

    def active_window(self) -> tuple[int, int]:
        """Aktives Fenster als (Startminute, Endminute) des Tages."""
        start, end = self.active_hours.split("-")
        return _to_minutes(start), _to_minutes(end)


def _to_minutes(hhmm: str) -> int:
    h, m = hhmm.strip().split(":")
    return int(h) * 60 + int(m)


def load_config(path: Optional[Path] = None) -> Config:
    path = path or PROJECT_DIR / "config.json"
    cfg = Config()
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
        known = set(asdict(cfg).keys())
        for key, value in raw.items():
            if key in known:
                setattr(cfg, key, value)
            else:
                cfg.extra[key] = value
    Path(cfg.db_path).parent.mkdir(parents=True, exist_ok=True)
    return cfg
