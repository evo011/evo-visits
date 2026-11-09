"""Schwimm' in Bilk: Ticketshop (Einlassfenster) und Belegungsplan (Bahnen je Viertelstunde).

Beide Quellen sind oeffentlich:
  1. Ticketshop der Baedergesellschaft Duesseldorf (fullcalendar-data, JSON).
  2. Belegungsplan der Website als SvelteKit-``__data.json`` (devalue-Format).
Gespeichert wird nur, was sich aendert: je Monat eine CSV mit allen Beobachtungen.
"""

from __future__ import annotations

import csv
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from . import fetch as _fetch
from .config import Config

TICKETS_URL = "https://shop.baeder-duesseldorf.de/ui/dps/fullcalendar-data"
TICKET_ARTICLE_ID = "1834"
LANES_URL = ("https://www.baeder-duesseldorf.de/belegungsplan/schwimm-in-bilk/"
             "__data.json?x-sveltekit-invalidated=001")
STANDORT_SLUG = "schwimm-in-bilk"

# Preisstufen Erwachsene laut Shop: 4,80 / 5,44 / 6,40 / 7,04 Euro.
PRICE_TIERS = {480: 1, 544: 2, 640: 3, 704: 4}

_START_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
_MONTH = "%Y-%m"


def http_get(url: str, cfg: Config) -> tuple[str, str]:
    """Duennes HTTP-Wrapper; Tests patchen genau diese Funktion."""
    return _fetch.http_get(url, cfg, accept="application/json")


def _iso(ts: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts))


def tickets_url(now_ts: int, days: int = 4) -> str:
    articles = json.dumps([{"id": TICKET_ARTICLE_ID, "quantity": 1}], separators=(",", ":"))
    query = urlencode({"articles": articles, "start": _iso(now_ts),
                       "end": _iso(now_ts + days * 86400)})
    return f"{TICKETS_URL}?{query}"


# ---------------------------------------------------------------------------------------------
# Ticketshop

@dataclass(frozen=True)
class TicketWindow:
    start: str
    end: str
    total: int
    available: int
    price_cents: int

    @property
    def sold(self) -> int:
        return max(0, self.total - self.available)

    @property
    def tier(self) -> Optional[int]:
        return PRICE_TIERS.get(self.price_cents)


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def parse_tickets(data: Any) -> list[TicketWindow]:
    if not isinstance(data, list):
        return []
    out: list[TicketWindow] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        start, end = item.get("start"), item.get("end")
        if not isinstance(start, str) or not _START_RE.match(start):
            continue
        if not isinstance(end, str) or not _START_RE.match(end):
            continue
        total = _as_int(item.get("totalAvailable"))
        available = _as_int(item.get("currentlyAvailable"))
        price = _as_int(item.get("price"))
        if total is None or available is None or price is None:
            continue
        out.append(TicketWindow(start, end, total, available, price))
    out.sort(key=lambda w: w.start)
    return out


# ---------------------------------------------------------------------------------------------
# Belegungsplan (SvelteKit __data.json / devalue)

class _Devalue:
    def __init__(self, data: list) -> None:
        self.data = data
        self.memo: dict[int, Any] = {}

    def value(self, index: Any) -> Any:
        if isinstance(index, bool) or not isinstance(index, int):
            raise _fetch.FetchError(f"Belegungsplan: ungueltiger Verweis {index!r}")
        if index in self.memo:
            return self.memo[index]
        if index < 0:
            special = {-1: None, -2: None, -3: float("nan"), -4: float("inf"),
                       -5: float("-inf"), -6: -0.0}
            if index not in special:
                raise _fetch.FetchError(f"Belegungsplan: ungueltiger Verweis {index}")
            result = special[index]
            self.memo[index] = result
            return result
        if index >= len(self.data):
            raise _fetch.FetchError(f"Belegungsplan: Verweis {index} ausserhalb der Daten "
                                    f"({len(self.data)} Eintraege)")
        self.memo[index] = None  # Platzhalter gegen Zyklen
        result = self._decode(self.data[index])
        self.memo[index] = result
        return result

    def _decode(self, node: Any) -> Any:
        if isinstance(node, dict):
            return {key: self.value(ref) for key, ref in node.items()}
        if isinstance(node, list):
            if node and isinstance(node[0], str):
                # Sondertyp, z. B. ["Date", <iso>]
                if node[0] == "Date" and len(node) >= 2:
                    iso = node[1] if isinstance(node[1], str) else self.value(node[1])
                    return iso if isinstance(iso, str) else None
                return None
            return [self.value(ref) for ref in node]
        return node


def decode_sveltekit(payload: Any) -> Any:
    """Dekodiert den letzten data-Knoten eines SvelteKit-``__data.json``."""
    nodes = payload.get("nodes") if isinstance(payload, dict) else None
    if not isinstance(nodes, list):
        raise _fetch.FetchError("Belegungsplan: __data.json ohne nodes")
    data = None
    for node in nodes:
        if isinstance(node, dict) and node.get("type") == "data":
            data = node.get("data")
    if not isinstance(data, list) or not data:
        raise _fetch.FetchError("Belegungsplan: kein data-Knoten in __data.json")
    return _Devalue(data).value(0)


@dataclass(frozen=True)
class LaneSlot:
    date: str
    becken: str
    slot: str
    lanes_total: int
    lanes_free: int
    bookings: str


@dataclass(frozen=True)
class DayInfo:
    date: str
    status: str
    open_start: str
    open_end: str
    note: str


def _norm(value: Any) -> str:
    return "" if value is None else " ".join(str(value).split())


def _path(value: Any, *keys: str, where: str) -> Any:
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            raise _fetch.FetchError(f"Belegungsplan: {where} fehlt ({key})")
        value = value[key]
    return value


def parse_lanes(decoded: Any, slug: str = STANDORT_SLUG) -> tuple[list[LaneSlot], list[DayInfo]]:
    hauptinhalt = _path(decoded, "content", "hauptinhalt", where="Inhalt")
    if not isinstance(hauptinhalt, list):
        raise _fetch.FetchError("Belegungsplan: hauptinhalt ist keine Liste")
    plan = next((item for item in hauptinhalt
                 if isinstance(item, dict) and item.get("type") == "belegungsplan"), None)
    if plan is None:
        raise _fetch.FetchError("Belegungsplan: kein Belegungsplan im Inhalt")
    standorte = _path(plan, "content", "standorte", where="Standorte")
    if not isinstance(standorte, list):
        raise _fetch.FetchError("Belegungsplan: standorte ist keine Liste")
    standort = next((s for s in standorte
                     if isinstance(s, dict) and isinstance(s.get("standort"), dict)
                     and s["standort"].get("slug") == slug), None)
    if standort is None:
        raise _fetch.FetchError(f"Belegungsplan: Standort {slug!r} nicht gefunden")

    slots: list[LaneSlot] = []
    days: dict[str, DayInfo] = {}
    for becken in standort.get("becken") or []:
        if not isinstance(becken, dict):
            continue
        name = _norm(becken.get("name"))
        for day in becken.get("days") or []:
            if not isinstance(day, dict):
                continue
            date = _norm(day.get("date"))
            if date and date not in days:
                notes = [_norm(p.get("anmerkung")) for p in day.get("open_periods") or []
                         if isinstance(p, dict) and _norm(p.get("anmerkung"))]
                days[date] = DayInfo(date, _norm(day.get("status")), _norm(day.get("open_start")),
                                     _norm(day.get("open_end")), "; ".join(notes))
            for entry in day.get("slots") or []:
                if not isinstance(entry, dict):
                    continue
                lanes = [b for b in entry.get("bahnen") or [] if isinstance(b, dict)]
                used = [b for b in lanes if not b.get("belegt")]
                bookings = []
                for lane in sorted((b for b in lanes if b.get("belegt")),
                                   key=lambda b: (b.get("pos") if isinstance(b.get("pos"), int)
                                                  and not isinstance(b.get("pos"), bool) else 1 << 30)):
                    parts = (_norm(lane.get("pos")), _norm(lane.get("kategorie")),
                             _norm(lane.get("booking")))
                    bookings.append(":".join(parts))
                slots.append(LaneSlot(date, name, _norm(entry.get("start")), len(lanes),
                                      len(used), "; ".join(bookings)))
    for closed in standort.get("closed_dates") or []:
        if not isinstance(closed, dict):
            continue
        date = _norm(closed.get("date"))
        if date:
            days[date] = DayInfo(date, "geschlossen", "", "", _norm(closed.get("anmerkung")))

    slots.sort(key=lambda s: (s.date, s.becken, s.slot))
    return slots, [days[d] for d in sorted(days)]


# ---------------------------------------------------------------------------------------------
# Monats-CSVs

_HEADERS = {
    "tickets": ["ts", "local_time", "window_start", "window_end", "total", "available",
                "price_cents"],
    "lanes": ["ts", "date", "becken", "slot", "lanes_total", "lanes_free", "bookings"],
    "days": ["ts", "date", "status", "open_start", "open_end", "note"],
}
_COMPARE = {"tickets": (4, 5, 6), "lanes": (4, 5, 6), "days": (2, 3, 4, 5)}
_KEY = {"tickets": 2, "lanes": (1, 2, 3), "days": 1}


def _row_key(kind: str, row: list[str]) -> Any:
    cols = _KEY[kind]
    if isinstance(cols, tuple):
        return tuple(row[i] for i in cols)
    return row[cols]


def _last_rows(path: Path, kind: str) -> dict[Any, list[str]]:
    last: dict[Any, list[str]] = {}
    if not path.exists():
        return last
    with path.open(encoding="utf-8", newline="") as f:
        for row in csv.reader(f):
            if not row or row[0] == "ts":
                continue
            last[_row_key(kind, row)] = row
    return last


def _append(directory: Path, kind: str, date_key: str, records: list[tuple[Any, list[str]]]) -> int:
    if not records:
        return 0
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"bilk-{kind}-{date_key[:7]}.csv"
    compare = _COMPARE[kind]
    last = _last_rows(path, kind)
    new_rows: list[list[str]] = []
    for key, row in records:
        previous = last.get(key)
        if previous is not None and all(previous[i] == row[i] for i in compare):
            continue
        last[key] = row
        new_rows.append(row)
    if not new_rows:
        return 0
    exists = path.exists()
    with path.open("a", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        if not exists:
            writer.writerow(_HEADERS[kind])
        writer.writerows(new_rows)
    return len(new_rows)


def store_tickets(directory: Path, windows: list[TicketWindow], ts: int, tz) -> int:
    local = datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d %H:%M")
    by_month: dict[str, list[tuple[str, list[str]]]] = {}
    for w in windows:
        row = [str(ts), local, w.start, w.end, str(w.total), str(w.available),
               str(w.price_cents)]
        by_month.setdefault(w.start[:7], []).append((w.start, row))
    return sum(_append(directory, "tickets", month, records)
               for month, records in sorted(by_month.items()))


def store_lanes(directory: Path, slots: list[LaneSlot], ts: int) -> int:
    by_month: dict[str, list[tuple[tuple[str, str, str], list[str]]]] = {}
    for s in slots:
        row = [str(ts), s.date, s.becken, s.slot, str(s.lanes_total), str(s.lanes_free),
               s.bookings]
        by_month.setdefault(s.date[:7], []).append(((s.date, s.becken, s.slot), row))
    return sum(_append(directory, "lanes", month, records)
               for month, records in sorted(by_month.items()))


def store_days(directory: Path, days: list[DayInfo], ts: int) -> int:
    by_month: dict[str, list[tuple[str, list[str]]]] = {}
    for d in days:
        row = [str(ts), d.date, d.status, d.open_start, d.open_end, d.note]
        by_month.setdefault(d.date[:7], []).append((d.date, row))
    return sum(_append(directory, "days", month, records)
               for month, records in sorted(by_month.items()))


def load_latest(directory: Path, kind: str, months: list[str]) -> dict:
    latest: dict = {}
    for month in months:
        path = directory / f"bilk-{kind}-{month}.csv"
        if not path.exists():
            continue
        with path.open(encoding="utf-8", newline="") as f:
            for row in csv.reader(f):
                if not row or row[0] == "ts":
                    continue
                latest[_row_key(kind, row)] = dict(zip(_HEADERS[kind], row))
    return latest


# ---------------------------------------------------------------------------------------------
# Ablauf

def lanes_due(directory: Path, now_ts: int, tz) -> bool:
    """Wahre, solange die lokale Minute unter 15 liegt oder der heutige Tag noch fehlt."""
    local = datetime.fromtimestamp(now_ts, tz)
    if local.minute < 15:
        return True
    days = load_latest(directory, "days", [local.strftime(_MONTH)])
    return local.strftime("%Y-%m-%d") not in days


def collect(cfg: Config, directory: Path, now_ts: Optional[int] = None,
            force_lanes: bool = False) -> dict:
    ts = int(time.time()) if now_ts is None else int(now_ts)
    tz = ZoneInfo(cfg.timezone)
    directory = Path(directory)

    _, text = http_get(tickets_url(ts), cfg)
    windows = parse_tickets(json.loads(text))
    tickets_new = store_tickets(directory, windows, ts, tz)

    result = {"windows": len(windows), "tickets_new": tickets_new,
              "lanes": None, "lanes_new": None, "days_new": None}
    if not (force_lanes or lanes_due(directory, ts, tz)):
        return result

    _, text = http_get(LANES_URL, cfg)
    decoded = decode_sveltekit(json.loads(text))
    slots, days = parse_lanes(decoded)
    result["lanes"] = len(slots)
    result["lanes_new"] = store_lanes(directory, slots, ts)
    result["days_new"] = store_days(directory, days, ts)
    return result
