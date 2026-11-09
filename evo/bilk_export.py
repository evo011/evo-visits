"""Export fuer das Bilk-Board: Tagesplan, Stundenzeilen, Belegungsbloecke als JSON."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from .bilk import PRICE_TIERS, load_latest
from .config import WEB_DIR
from .forecast import get_tz
from .static_export import _inject_static

NAME = "Schwimm' in Bilk"
SPORTBECKEN = "Sportbecken"
WEEKDAYS = ["Mo.", "Di.", "Mi.", "Do.", "Fr.", "Sa.", "So."]
LABELS = {
    "schulschwimmen": "Schulschwimmen",
    "schwimmverein": "Verein",
    "bgd": "Kurs",
    "externe-nutzer": "Externer Kurs",
    "kita": "Kita",
    "ogs": "OGS",
    "privatschule": "Privatschule",
}
TIMEZONE = "Europe/Berlin"


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _minutes(value: str) -> Optional[int]:
    """'HH:MM' als Minuten seit Mitternacht."""
    try:
        hour, minute = value.split(":")
        return int(hour) * 60 + int(minute)
    except (AttributeError, ValueError):
        return None


def _slot(value: str) -> str:
    return f"{value // 60:02d}:{value % 60:02d}"


def _months(dates: list[str]) -> list[str]:
    first = datetime.strptime(dates[0], "%Y-%m-%d")
    previous = (first.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    return sorted({previous, *(d[:7] for d in dates)})


def _max_ts(rows: dict, key: str = "ts") -> Optional[int]:
    values = [_as_int(row.get(key)) for row in rows.values()]
    values = [v for v in values if v is not None]
    return max(values) if values else None


def _ticket_row(tickets: dict, date: str, start: str) -> Optional[dict]:
    row = tickets.get(f"{date} {start}")
    if row is None:
        return None
    total = _as_int(row.get("total"))
    available = _as_int(row.get("available"))
    price = _as_int(row.get("price_cents"))
    if total is None or available is None or price is None:
        return None
    return {"total": total, "available": available, "sold": max(0, total - available),
            "price_cents": price, "tier": PRICE_TIERS.get(price)}


def _quarters(lanes: dict, date: str, start: str, end: str) -> list[dict]:
    out = []
    for row in lanes.values():
        if row.get("date") != date or not str(row.get("becken", "")).startswith(SPORTBECKEN):
            continue
        slot = row.get("slot", "")
        if not (start <= slot < end):
            continue
        free = _as_int(row.get("lanes_free"))
        total = _as_int(row.get("lanes_total"))
        if free is None or total is None:
            continue
        out.append({"slot": slot, "free": free, "total": total})
    out.sort(key=lambda q: q["slot"])
    return out


def _row(date: str, start: str, end: str, quarters: list[dict], tickets: Optional[dict],
         today: str, now_text: str) -> dict:
    avg = round(sum(q["free"] for q in quarters) / len(quarters), 2) if quarters else None
    lowest = min(q["free"] for q in quarters) if quarters else None
    return {
        "start": start,
        "end": end,
        "quarters": quarters,
        "avg_free": avg,
        "min_free": lowest,
        "tickets": tickets,
        "past": f"{date} {end}" <= now_text,
        "now": date == today and start <= now_text[11:] < end,
    }


def _best(rows: list[dict]) -> Optional[dict]:
    candidates = [r for r in rows
                  if not r["past"] and r["tickets"] is not None and r["avg_free"] is not None]
    if not candidates:
        return None
    return dict(min(candidates, key=lambda r: (
        -r["avg_free"],
        999 if r["tickets"]["tier"] is None else r["tickets"]["tier"],
        r["tickets"]["sold"],
        r["start"],
    )))


def _bookings(text: str) -> list[tuple[int, str, str]]:
    out = []
    for part in str(text).split(";"):
        fields = part.strip().split(":", 2)
        if len(fields) != 3:
            continue
        position = _as_int(fields[0].strip())
        if position is None:
            continue
        out.append((position, fields[1].strip(), fields[2].strip()))
    return out


def _lane_text(positions: list[int]) -> str:
    unique = sorted(set(positions))
    if len(unique) == 1:
        return str(unique[0])
    if unique == list(range(unique[0], unique[-1] + 1)):
        return f"{unique[0]}–{unique[-1]}"
    return ", ".join(str(p) for p in unique)


def _blocks(lanes: dict, date: str) -> list[dict]:
    runs: list[dict] = []
    by_position: dict[tuple[str, int], list[tuple[str, str, str]]] = {}
    for row in lanes.values():
        if row.get("date") != date:
            continue
        becken = str(row.get("becken", ""))
        slot = row.get("slot", "")
        for position, category, who in _bookings(row.get("bookings", "")):
            by_position.setdefault((becken, position), []).append((slot, category, who))

    for (becken, position), entries in by_position.items():
        entries.sort()
        current = None
        for slot, category, who in entries:
            minutes = _minutes(slot)
            if minutes is None:
                continue
            if (current is not None and minutes == _minutes(current["end"])
                    and (category, who) == (current["category"], current["who"])):
                current["end"] = _slot(minutes + 15)
                continue
            current = {"becken": becken, "start": slot, "end": _slot(minutes + 15),
                       "category": category, "who": who, "position": position}
            runs.append(current)

    merged: dict[tuple, dict] = {}
    for run in runs:
        key = (run["becken"], run["start"], run["end"], run["category"], run["who"])
        block = merged.get(key)
        if block is None:
            block = {"becken": run["becken"], "start": run["start"], "end": run["end"],
                     "positions": [], "category": run["category"], "who": run["who"]}
            merged[key] = block
        block["positions"].append(run["position"])

    out = []
    for block in merged.values():
        blocked = block.pop("positions")
        category = block.pop("category")
        who = block.pop("who")
        out.append({
            "becken": block["becken"],
            "start": block["start"],
            "end": block["end"],
            "lanes": _lane_text(blocked),
            "label": LABELS.get(category, category or "Belegt"),
            "who": who,
        })
    out.sort(key=lambda b: (
        0 if b["becken"].startswith(SPORTBECKEN) else 1, b["start"],
        _lane_text_sort(b["lanes"])))
    return out


def _lane_text_sort(text: str) -> int:
    first = text.split(",")[0].split("–")[0]
    value = _as_int(first)
    return value if value is not None else 999


def _day(date: str, label: str, days: dict, lanes: dict, tickets: dict,
         today: str, now_text: str) -> dict:
    info = days.get(date)
    if info is None:
        return {"date": date, "label": label, "status": "unbekannt",
                "open_start": "", "open_end": "", "note": "", "rows": [], "blocks": [],
                "best": None}
    status = info.get("status", "")
    open_start = info.get("open_start", "")
    open_end = info.get("open_end", "")
    day = {"date": date, "label": label, "status": status, "open_start": open_start,
           "open_end": open_end, "note": info.get("note", ""), "rows": [], "blocks": [],
           "best": None}
    if status == "geschlossen":
        return day

    start_min, end_min = _minutes(open_start), _minutes(open_end)
    if start_min is None or end_min is None or start_min >= end_min:
        return day
    edges = [open_start]
    edges += [_slot(m) for m in range(start_min + 1, end_min) if m % 60 == 0]
    edges.append(open_end)

    rows = []
    for a, b in zip(edges, edges[1:]):
        rows.append(_row(date, a, b, _quarters(lanes, date, a, b),
                         _ticket_row(tickets, date, a), today, now_text))
    day["rows"] = rows
    day["blocks"] = _blocks(lanes, date)
    day["best"] = _best(rows)
    return day


def build_payload(directory: Path, now_ts: int, tz) -> dict:
    """Tagesplan der drei naechsten Tage aus den Monats-CSVs."""
    directory = Path(directory)
    now = datetime.fromtimestamp(now_ts, tz)
    today = now.strftime("%Y-%m-%d")
    dates = [(now + timedelta(days=offset)).strftime("%Y-%m-%d") for offset in range(3)]
    months = _months(dates)

    tickets = load_latest(directory, "tickets", months)
    lanes = load_latest(directory, "lanes", months)
    days = load_latest(directory, "days", months)

    now_text = now.strftime("%Y-%m-%d %H:%M")
    payload_days = []
    for date in dates:
        label = f"{WEEKDAYS[datetime.strptime(date, '%Y-%m-%d').weekday()]}, {date[8:10]}.{date[5:7]}."
        payload_days.append(_day(date, label, days, lanes, tickets, today, now_text))

    current = next((d for d in payload_days if d["date"] == today), None)
    now_row = None
    if current is not None:
        now_row = next((dict(r) for r in current["rows"] if r["now"]), None)

    slot_now = _slot(now.hour * 60 + (now.minute // 15) * 15)
    lanes_free = lanes_total = None
    for row in lanes.values():
        if (row.get("date") == today and str(row.get("becken", "")).startswith(SPORTBECKEN)
                and row.get("slot") == slot_now):
            lanes_free = _as_int(row.get("lanes_free"))
            lanes_total = _as_int(row.get("lanes_total"))
            break

    lanes_ts = _max_ts(lanes)
    days_ts = _max_ts(days)
    lanes_ts = max([v for v in (lanes_ts, days_ts) if v is not None], default=None)

    return {
        "generated_at": int(now_ts),
        "name": NAME,
        "tickets_ts": _max_ts(tickets),
        "lanes_ts": lanes_ts,
        "now": {"date": today, "row": now_row, "lanes_free": lanes_free,
                "lanes_total": lanes_total},
        "days": payload_days,
    }


def export_bilk(directory: Path, out_dir: Path, now_ts: Optional[int] = None,
                web_dir: Optional[Path] = None) -> dict:
    """bilk/api/bilk.json schreiben und die Seite nach out_dir/bilk kopieren."""
    now_ts = int(datetime.now().timestamp()) if now_ts is None else int(now_ts)
    tz = get_tz(TIMEZONE)
    payload = build_payload(directory, now_ts, tz)

    target = Path(out_dir) / "bilk"
    (target / "api").mkdir(parents=True, exist_ok=True)
    (target / "api" / "bilk.json").write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    files = 1

    source = Path(web_dir) if web_dir is not None else WEB_DIR / "bilk"
    if (source / "index.html").is_file():
        html = (source / "index.html").read_text(encoding="utf-8")
        (target / "index.html").write_text(_inject_static(html, now_ts), encoding="utf-8")
        files += 1
        manifest = source / "manifest.webmanifest"
        if manifest.is_file():
            (target / "manifest.webmanifest").write_bytes(manifest.read_bytes())
            files += 1

    return {"out": str(target), "files": files, "days": len(payload["days"]),
            "payload": payload}
