"""Lokaler Webserver plus Hintergrund-Sammler in einem Prozess."""

from __future__ import annotations

import csv
import io
import json
import threading
import time
import traceback
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlparse

from .config import WEB_DIR, Config
from .fetch import collect_once
from .forecast import Profile, backtest, build_profile, get_tz
from .store import Store


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


class Collector(threading.Thread):
    def __init__(self, cfg: Config, store: Store):
        super().__init__(daemon=True, name="collector")
        self.cfg, self.store = cfg, store
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.next_run: Optional[int] = None
        self.last_error: Optional[str] = None
        self.tz = get_tz(cfg.timezone)

    def in_active_window(self, ts: float) -> bool:
        dt = datetime.fromtimestamp(ts, self.tz)
        minute = dt.hour * 60 + dt.minute
        start, end = self.cfg.active_window()
        return start <= minute <= end if start <= end else (minute >= start or minute <= end)

    def run_once(self) -> dict:
        with self.lock:
            try:
                res = collect_once(self.cfg, self.store)
                self.last_error = None
                log(f"{len(res.readings)} Clubs gespeichert ({res.source})")
                return {"ok": True, "n": len(res.readings), "source": res.source}
            except Exception as e:
                self.last_error = str(e)
                log(f"Abruf fehlgeschlagen: {e}")
                return {"ok": False, "error": str(e)}

    def run(self) -> None:
        step = max(1, int(self.cfg.interval_minutes)) * 60
        while not self.stop_event.is_set():
            now = time.time()
            if self.in_active_window(now):
                self.run_once()
            # auf volle Intervalle ausrichten (z. B. :00, :10, :20)
            self.next_run = int((time.time() // step + 1) * step)
            self.stop_event.wait(max(1.0, self.next_run - time.time()))


class App:
    def __init__(self, cfg: Config, store: Store, collector: Optional[Collector], demo: bool = False):
        self.cfg, self.store, self.collector, self.demo = cfg, store, collector, demo
        self.tz = get_tz(cfg.timezone)
        self._profiles: dict[str, tuple[tuple, Profile]] = {}
        self._plock = threading.Lock()
        self._backtests: dict[tuple, Optional[dict]] = {}

    def now(self) -> int:
        if self.demo:
            gyms = self.store.gyms()
            return max((g["last_ts"] for g in gyms), default=int(time.time())) + 60
        return int(time.time())

    def profile(self, gym: str, last_ts: int) -> Profile:
        now = self.now()
        key = (last_ts, now // (self.cfg.bin_minutes * 60))
        with self._plock:
            hit = self._profiles.get(gym)
            if hit and hit[0] == key:
                return hit[1]
        prof = build_profile(self.store, gym, self.cfg, now)
        with self._plock:
            self._profiles[gym] = (key, prof)
        return prof

    # --- API -------------------------------------------------------------------------------

    def api_status(self, _q) -> dict:
        c = self.collector
        return {
            "url": self.cfg.url, "demo": self.demo, "interval_minutes": self.cfg.interval_minutes,
            "active_hours": self.cfg.active_hours, "timezone": self.cfg.timezone,
            "collector": None if c is None else {
                "next_run": c.next_run, "last_error": c.last_error,
                "active_now": c.in_active_window(time.time()),
            },
            "runs": self.store.last_runs(15),
            "now": self.now(),
        }

    def api_gyms(self, _q) -> list[dict]:
        now = self.now()
        out = []
        for g in self.store.gyms():
            prof = self.profile(g["gym"], g["last_ts"])
            st = prof.seasonal_at(now)
            value = g["count"] if g["count"] is not None else g["pct"]
            fresh = now - g["last_ts"] <= max(3 * self.cfg.interval_minutes * 60, 1800)
            g.update({
                "value": value,
                "unit": "people" if g["count"] is not None else "percent",
                "typical_now": round(st.mean, 1) if st else None,
                "fresh": fresh,
                "n_days": prof.n_days,
            })
            out.append(g)
        return out

    def api_gym(self, q) -> dict:
        gym = q.get("gym", [""])[0]
        days = float(q.get("days", ["7"])[0])
        now = self.now()
        since = now - int(days * 86400) if days > 0 else 0
        history = self.store.series(gym, since, now)
        gyms = {g["gym"]: g for g in self.store.gyms()}
        if gym not in gyms:
            return {"error": "unbekannter Club"}
        info = gyms[gym]
        prof = self.profile(gym, info["last_ts"])
        recent = self.store.series(gym, now - 3 * 3600, now)
        cap = info["capacity"] if info["count"] is not None else 100.0
        fc = prof.forecast(hours=7 * 24, recent=recent, cap=cap)
        # Profil-Kurve fuer den heutigen Tag (zum Vergleich "so voll ist es normalerweise")
        day0 = datetime.fromtimestamp(now, self.tz).replace(hour=0, minute=0, second=0, microsecond=0)
        start0 = int(day0.timestamp())
        step = self.cfg.bin_minutes * 60
        typical_today = []
        for ts in range(start0, start0 + 86400, step):
            st = prof.seasonal_at(ts)
            typical_today.append([ts * 1000, round(st.mean, 1) if st else None])
        bt_key = (gym, datetime.fromtimestamp(now, self.tz).date())
        if bt_key not in self._backtests:
            full = self.store.series(gym, now - self.cfg.history_weeks * 7 * 86400, now)
            self._backtests[bt_key] = backtest(full, self.tz, self.cfg.bin_minutes, self.cfg.half_life_days)
        return {
            "gym": gym,
            "unit": self.store.unit(gym),
            "capacity": cap,
            "last": {"ts": info["last_ts"], "value": info["count"] if info["count"] is not None else info["pct"]},
            "history": [[ts * 1000, v] for ts, v in history],
            "today_actual": [[ts * 1000, v] for ts, v in self.store.series(gym, start0, now)],
            "typical_today": typical_today,
            "forecast": [[p["ts"] * 1000, p["est"], p["lo"], p["hi"]] for p in fc],
            "heatmap": prof.heatmap(),
            "n_days": prof.n_days,
            "first_ts": info["first_ts"],
            "backtest": self._backtests[bt_key],
            "now": now,
        }

    def api_collect(self, _q) -> dict:
        if self.demo or self.collector is None:
            return {"ok": False, "error": "Im Demo-Modus wird nichts abgerufen."}
        return self.collector.run_once()

    def export_csv(self) -> bytes:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["timestamp_utc", "local_time", "gym", "count", "capacity", "pct"])
        for ts, gym, count, cap, pct in self.store.export_rows():
            local = datetime.fromtimestamp(ts, self.tz).strftime("%Y-%m-%d %H:%M")
            w.writerow([ts, local, gym, count, cap, pct])
        return buf.getvalue().encode("utf-8")


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "evo-visits"

        def log_message(self, fmt, *args):  # ruhig bleiben, nur Fehler loggen
            pass

        def _send(self, status: int, body: bytes, ctype: str, extra: Optional[dict] = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data, status: int = 200) -> None:
            self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _route(self, method: str) -> None:
            url = urlparse(self.path)
            q = parse_qs(url.query)
            try:
                if method == "GET" and url.path in ("/", "/index.html"):
                    self._send(200, (WEB_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
                elif method == "GET" and url.path == "/api/status":
                    self._json(app.api_status(q))
                elif method == "GET" and url.path == "/api/gyms":
                    self._json(app.api_gyms(q))
                elif method == "GET" and url.path == "/api/gym":
                    self._json(app.api_gym(q))
                elif method == "POST" and url.path == "/api/collect":
                    self._json(app.api_collect(q))
                elif method == "GET" and url.path == "/api/export.csv":
                    self._send(200, app.export_csv(), "text/csv; charset=utf-8",
                               {"Content-Disposition": 'attachment; filename="evo-visits.csv"'})
                else:
                    self._send(404, b"not found", "text/plain")
            except Exception as e:
                traceback.print_exc()
                self._json({"error": f"{type(e).__name__}: {e}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

    return Handler


def serve(cfg: Config, store: Store, demo: bool = False, collect: bool = True) -> None:
    collector = None
    if collect and not demo:
        collector = Collector(cfg, store)
        collector.start()
        log(f"Sammler aktiv: alle {cfg.interval_minutes} min zwischen {cfg.active_hours} ({cfg.url})")
    app = App(cfg, store, collector, demo)
    httpd = ThreadingHTTPServer((cfg.host, cfg.port), make_handler(app))
    log(f"Dashboard: http://{cfg.host}:{cfg.port}/" + ("  (DEMO-DATEN)" if demo else ""))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("Beendet.")
    finally:
        if collector:
            collector.stop_event.set()
        httpd.server_close()
