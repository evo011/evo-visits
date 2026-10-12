"""Heuristiken, die aus beliebigem JSON oder HTML Club-Namen und Besucherzahlen herausziehen.

Die genaue Struktur von visits.evofitness.de ist nicht dokumentiert. Statt auf einen festen
Selektor zu setzen, bewerten wir Kandidaten: Listen von Objekten mit einem namensartigen Feld
und einem zaehlerartigen Feld (JSON) bzw. Text-Bloecke "Clubname ... 42 Personen" (HTML).
"""

from __future__ import annotations

import html as html_lib
import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Iterable, Optional

from .store import Reading

KNOWN_CITIES = (
    "hamburg", "berlin", "dresden", "köln", "koeln", "cologne", "düsseldorf", "duesseldorf",
    "münchen", "muenchen", "munich", "wiesbaden", "frankfurt", "leipzig", "hannover", "bremen",
    "mainz", "bonn", "essen", "dortmund", "stuttgart", "nürnberg", "nuernberg", "potsdam",
)

NAME_HINTS = ("name", "title", "club", "location", "gym", "studio", "center", "centre",
              "label", "site", "branch", "standort", "city")
COUNT_HINTS = ("current", "count", "visitor", "visits", "visit", "people", "person", "checkedin",
               "checkin", "inside", "present", "occupancy", "attendance", "live", "now", "anzahl",
               "besucher", "personen", "amount", "number", "members", "value", "active")
CAP_HINTS = ("max", "maximum", "capacity", "limit", "kapazitaet", "kapazität", "allowed")
PCT_HINTS = ("percent", "percentage", "pct", "utilization", "utilisation", "auslastung", "load",
             "ratio", "fill", "relative")
COUNT_EXCLUDE = {"id", "zip", "postal", "plz", "lat", "lon", "lng", "latitude", "longitude",
                 "phone", "forecast", "expected", "predicted", "prediction", "avg", "average",
                 "history", "prev", "previous", "yesterday", "hour", "hours", "day", "days", "time",
                 "date", "year", "sort", "order", "index", "version", "price", "min", "week",
                 "timestamp", "updated", "created", "opening", "open", "close"}


def _tokens(key: str) -> list[str]:
    key = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", key)
    return [t for t in re.split(r"[^a-zA-Z0-9äöüÄÖÜ]+", key.lower()) if t]


def _hint_score(key: str, hints: Iterable[str]) -> int:
    toks = _tokens(key)
    score = 0
    for t in toks:
        for h in hints:
            if t == h:
                score += 3
            elif len(h) >= 4 and (t.startswith(h) or h in t):
                score += 2
    return score


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip().replace("%", "").replace(",", ".")
        if re.fullmatch(r"-?\d+(\.\d+)?", s):
            return float(s)
    return None


def _is_name(v: Any) -> bool:
    return isinstance(v, str) and 2 <= len(v.strip()) <= 80 and bool(re.search(r"[A-Za-zÄÖÜäöü]{2}", v)) \
        and not re.fullmatch(r"[0-9a-f\-]{16,}", v.strip().lower())


def _flatten(obj: dict, prefix: str = "", depth: int = 0) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in obj.items():
        key = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict) and depth < 2:
            out.update(_flatten(v, key, depth + 1))
        elif not isinstance(v, (list, dict)):
            out[key] = v
    return out


def clean_name(name: str) -> str:
    return re.sub(r"\s+", " ", html_lib.unescape(name)).strip()


@dataclass
class Candidate:
    readings: list[Reading]
    score: float
    keys: dict[str, Optional[str]] = field(default_factory=dict)
    where: str = ""   # Quelle (URL oder Art des eingebetteten JSON)
    path: str = ""    # Pfad der Club-Liste innerhalb des JSON

    def describe(self) -> str:
        keys = ", ".join(f"{k}={v}" for k, v in self.keys.items() if v)
        return f"{len(self.readings)} Clubs, score {self.score:.0f}, keys [{keys}], Pfad '{self.path}' @ {self.where}"


def evaluate_items(items: list[dict], where: str = "", require_name: bool = True) -> Optional[Candidate]:
    """Bewertet eine Liste von Objekten als moegliche Club-Liste."""
    rows = [_flatten(x) for x in items if isinstance(x, dict)]
    if not rows:
        return None
    n = len(rows)
    keys: set[str] = set()
    for r in rows:
        keys.update(r.keys())

    def frac(key: str, pred) -> float:
        return sum(1 for r in rows if pred(r.get(key))) / n

    name_key = None
    best = 0
    for k in keys:
        s = _hint_score(k, NAME_HINTS)
        if "slug" in _tokens(k):
            s = max(s, 1)
        if s and frac(k, _is_name) >= 0.8:
            vals = [clean_name(r[k]) for r in rows if _is_name(r.get(k))]
            if n > 1 and len(set(vals)) < 0.8 * len(vals):
                continue
            if s > best:
                name_key, best = k, s
    if require_name and not name_key:
        return None

    def pick(hints, exclude_hints=()) -> tuple[Optional[str], int]:
        chosen, top = None, 0
        for k in keys:
            toks = set(_tokens(k))
            if toks & COUNT_EXCLUDE and not toks & {"current", "now", "live"}:
                continue
            if exclude_hints and _hint_score(k, exclude_hints):
                continue
            s = _hint_score(k, hints)
            if s and frac(k, lambda v: _num(v) is not None) >= 0.6:
                vals = [_num(r.get(k)) for r in rows if _num(r.get(k)) is not None]
                if all(0 <= v <= 5000 for v in vals) and s > top:
                    chosen, top = k, s
        return chosen, top

    count_key, cs = pick(COUNT_HINTS, CAP_HINTS + PCT_HINTS)
    cap_key, _ = pick(CAP_HINTS)
    pct_key, ps = pick(PCT_HINTS, CAP_HINTS)
    if not count_key and not pct_key:
        return None

    readings: list[Reading] = []
    seen: set[str] = set()
    for i, r in enumerate(rows):
        name = clean_name(r[name_key]) if name_key and _is_name(r.get(name_key)) else f"#{i + 1}"
        if name in seen:
            continue
        count = _num(r.get(count_key)) if count_key else None
        cap = _num(r.get(cap_key)) if cap_key else None
        pct = _num(r.get(pct_key)) if pct_key else None
        # Ein "count", der nur Werte zwischen 0 und 1 mit Nachkommastellen hat, ist eher ein Anteil.
        if count is not None and 0 < count < 1 and not float(count).is_integer():
            pct, count = count * 100, None
        if pct is not None and pct <= 1 and pct_key and all(
            (_num(x.get(pct_key)) or 0) <= 1 for x in rows
        ):
            pct = pct * 100
        if count is None and pct is None:
            continue
        seen.add(name)
        readings.append(Reading(gym=name, count=count, capacity=cap, pct=pct))
    if not readings:
        return None

    city_bonus = sum(1 for rd in readings if any(c in rd.gym.lower() for c in KNOWN_CITIES))
    score = len(readings) * 10 + best * 2 + max(cs, ps) * 2 + city_bonus * 5
    return Candidate(readings, score, {"name": name_key, "count": count_key, "capacity": cap_key,
                                        "pct": pct_key}, where)


def best_from_json(data: Any, where: str = "") -> Optional[Candidate]:
    """Durchsucht ein JSON-Dokument rekursiv nach der plausibelsten Club-Liste."""
    best: Optional[Candidate] = None

    def consider(c: Optional[Candidate]) -> None:
        nonlocal best
        if c and (best is None or c.score > best.score):
            best = c

    def walk(node: Any, path: str, depth: int) -> None:
        if depth > 10:
            return
        if isinstance(node, list):
            dicts = [x for x in node if isinstance(x, dict)]
            if dicts and len(dicts) >= 0.8 * len(node):
                consider(evaluate_items(dicts, path or "$"))
            for i, x in enumerate(node[:200]):
                walk(x, f"{path}[{i}]", depth + 1)
        elif isinstance(node, dict):
            vals = list(node.values())
            # Objekt, dessen Werte Club-Objekte sind: {"hamburg-altona": {"current": 12}, ...}
            if len(vals) >= 2 and all(isinstance(v, dict) for v in vals):
                items = []
                for k, v in node.items():
                    item = dict(v)
                    if not any(_hint_score(x, NAME_HINTS) for x in v):
                        item["name"] = str(k)
                    items.append(item)
                consider(evaluate_items(items, f"{path}{{}}"))
            for k, v in node.items():
                walk(v, f"{path}.{k}" if path else str(k), depth + 1)

    walk(data, "", 0)
    if best is not None:
        best.path, best.where = best.where, where
    return best


def single_from_json(data: Any) -> Optional[Reading]:
    """Fuer Antworten, die genau einen Club beschreiben (Name kommt von aussen)."""
    if isinstance(data, list) and len(data) == 1:
        data = data[0]
    if not isinstance(data, dict):
        return None
    c = evaluate_items([data], require_name=False)
    if c and c.readings:
        return c.readings[0]
    for v in data.values():
        if isinstance(v, dict):
            r = single_from_json(v)
            if r:
                return r
    return None


# ---------------------------------------------------------------------------------------------
# HTML

def embedded_json_blobs(page: str) -> list[tuple[str, Any]]:
    """JSON, das direkt im HTML steckt: script-Tags, __NEXT_DATA__, data-page (Inertia), State-Objekte."""
    blobs: list[tuple[str, Any]] = []
    for m in re.finditer(r"<script([^>]*)>(.*?)</script>", page, re.S | re.I):
        attrs, body = m.group(1), m.group(2).strip()
        if not body:
            continue
        if re.search(r"application/(ld\+)?json", attrs, re.I) or "__NEXT_DATA__" in attrs:
            try:
                blobs.append(("script-json", json.loads(body)))
            except ValueError:
                pass
            continue
        for sm in re.finditer(r"(?:window\.)?[\w$]*(?:__\w+__|STATE|DATA|state|data)\w*\s*=\s*([\[{])", body):
            raw = _balanced(body, sm.start(1))
            if raw:
                try:
                    blobs.append(("script-state", json.loads(raw)))
                except ValueError:
                    pass
        for pm in re.finditer(r"JSON\.parse\(\s*(['\"])(.*?)(?<!\\)\1\s*\)", body, re.S):
            try:
                blobs.append(("json-parse", json.loads(json.loads(f'"{pm.group(2)}"'))))
            except ValueError:
                pass
    for am in re.finditer(r"[\w:-]+\s*=\s*([\"'])\s*([\[{].{40,}?)\1", page, re.S):
        raw = html_lib.unescape(am.group(2))
        try:
            blobs.append(("attribute", json.loads(raw)))
        except ValueError:
            pass
    return blobs


def _balanced(text: str, start: int) -> Optional[str]:
    open_ch = text[start]
    close_ch = "}" if open_ch == "{" else "]"
    depth, in_str, esc = 0, None, False
    for i in range(start, min(len(text), start + 2_000_000)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == in_str:
                in_str = None
        elif ch in "\"'":
            in_str = ch
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


class _TextCollector(HTMLParser):
    SKIP = {"script", "style", "noscript", "template", "svg", "head", "option"}
    VOID = {"br", "img", "hr", "input", "meta", "link", "source", "wbr", "area", "base", "col"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, str]] = []
        self.items: list[tuple[str, str]] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.VOID:
            return
        a = dict(attrs)
        ctx = " ".join(filter(None, [tag, a.get("class") or "", a.get("id") or ""])).lower()
        self.stack.append((tag, ctx))
        if tag in self.SKIP:
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                for t, _ in self.stack[i:]:
                    if t in self.SKIP:
                        self._skip -= 1
                del self.stack[i:]
                break

    def handle_data(self, data):
        if self._skip > 0:
            return
        text = re.sub(r"\s+", " ", data).strip()
        if text:
            ctx = " ".join(c for _, c in self.stack[-5:])
            self.items.append((text, ctx))


def visible_text_items(page: str) -> list[tuple[str, str]]:
    p = _TextCollector()
    try:
        p.feed(page)
    except Exception:
        pass
    return p.items


COUNT_PATTERNS = [
    (re.compile(r"(\d{1,4})\s*(?:/|von|of|aus)\s*(\d{1,4})\b"), "ratio"),
    (re.compile(r"(\d{1,4})\s*(?:Personen|Person|Besucher(?:innen)?|Mitglieder|Leute|Sportler|"
                r"people|persons|visitors|members|Trainierende)\b", re.I), "count"),
    (re.compile(r"(\d{1,3}(?:[.,]\d)?)\s*%"), "pct"),
]
STOP_NAMES = {"visits", "evo fitness", "evo", "auslastung", "live", "jetzt", "aktuell", "heute",
              "personen", "besucher", "prognose", "vorhersage", "club wählen", "wähle deinen club",
              "alle clubs", "menü", "menu", "login", "impressum", "datenschutz"}
NAME_CTX = re.compile(r"\b(h[1-6]|[\w-]*(name|title|club|location|studio|gym|standort|headline)[\w-]*)\b")
COUNT_CTX = re.compile(r"[\w-]*(count|current|visitor|people|persons|number|value|besucher|anzahl|live)[\w-]*")


def _match_count(text: str, ctx: str) -> Optional[Reading]:
    for rx, kind in COUNT_PATTERNS:
        m = rx.search(text)
        if not m:
            continue
        if kind == "ratio":
            a, b = float(m.group(1)), float(m.group(2))
            if b >= a and b > 0:
                return Reading(gym="", count=a, capacity=b)
        elif kind == "count":
            return Reading(gym="", count=float(m.group(1)))
        else:
            return Reading(gym="", pct=float(m.group(1).replace(",", ".")))
    if re.fullmatch(r"\d{1,4}", text) and COUNT_CTX.search(ctx):
        return Reading(gym="", count=float(text))
    return None


def _looks_like_name(text: str, ctx: str) -> bool:
    low = text.lower().strip(" :")
    if low in STOP_NAMES or not (3 <= len(text) <= 60) or not re.search(r"[A-Za-zÄÖÜäöü]{3}", text):
        return False
    if re.search(r"\d{1,4}\s*(%|personen|besucher|people|/)", low):
        return False
    return bool(NAME_CTX.search(ctx)) or any(low.startswith(c) for c in KNOWN_CITIES)


def best_from_html(page: str, where: str = "html") -> Optional[Candidate]:
    """Text-Heuristik: letzter Name-Kandidat davor bekommt die naechste Zahl."""
    items = visible_text_items(page)
    readings: list[Reading] = []
    seen: set[str] = set()
    current: Optional[str] = None
    for text, ctx in items:
        if _looks_like_name(text, ctx):
            current = clean_name(text)
            continue
        if current and current not in seen:
            r = _match_count(text, ctx)
            if r:
                r.gym = current
                readings.append(r)
                seen.add(current)
    if not readings:
        return None
    city_bonus = sum(1 for rd in readings if any(c in rd.gym.lower() for c in KNOWN_CITIES))
    return Candidate(readings, len(readings) * 8 + city_bonus * 5, {"mode": "text"}, where)


def single_from_html(page: str, exclude: Iterable[str] = ()) -> Optional[Reading]:
    """Erste plausible Zahl auf einer Einzel-Club-Ansicht."""
    excl = {e.strip().lower() for e in exclude}
    for text, ctx in visible_text_items(page):
        if text.strip().lower() in excl:
            continue
        r = _match_count(text, ctx)
        if r:
            return r
    return None
