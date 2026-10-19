"""Lokaler Webserver plus Hintergrund-Sammler in einem Prozess."""

from __future__ import annotations

import csv
import io
import json
import re
import threading
import time
import traceback
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

from .calendar_de import locate
from .evaluate import apply_calibration, calibration, empty_result, evaluate
from .config import WEB_DIR, Config, in_active_window
from .context import Context
from .fetch import collect_once
from .forecast import Profile, get_tz
from .model import (FeatureSource, Model, Pooled, backtest_pooled, build_pooled,
                    effects_help_pooled)
from .store import Store

WEATHER_LABEL = "Open-Meteo"
SCHOOL_LABEL = "OpenHolidays"
DEMO_WEATHER_LABEL = "Demo-Wetter (synthetisch)"
DEMO_SCHOOL_LABEL = "Demo-Ferien (synthetisch)"


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


class Collector(threading.Thread):
    def __init__(self, cfg: Config, store: Store, ctx: Optional[Context] = None):
        super().__init__(daemon=True, name="collector")
        self.cfg, self.store, self.ctx = cfg, store, ctx
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.next_run: Optional[int] = None
        self.last_error: Optional[str] = None
        self.tz = get_tz(cfg.timezone)

    def in_active_window(self, ts: float) -> bool:
        return in_active_window(self.cfg, ts)

    def run_once(self) -> dict:
        with self.lock:
            try:
                res = collect_once(self.cfg, self.store)
                if res.source == "skipped:min-interval":
                    return {"ok": False, "error": "Letzter Abruf ist keine 10 Minuten her."}
                if self.ctx is not None:
                    try:
                        self.ctx.refresh_for_gyms([g["gym"] for g in self.store.gyms()])
                    except Exception as e:
                        log(f"Kontext-Aktualisierung fehlgeschlagen: {e}")
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
    def __init__(self, cfg: Config, store: Store, collector: Optional[Collector],
                 demo: bool = False, ctx: Optional[Context] = None,
                 snapshot_dir: Optional[Path] = None):
        self.cfg, self.store, self.collector, self.demo = cfg, store, collector, demo
        self.ctx = ctx
        self.tz = get_tz(cfg.timezone)
        self.snapshot_dir = snapshot_dir if snapshot_dir is not None \
            else Path(cfg.db_path).parent / "forecasts"
        self._models: dict[str, tuple[tuple, Model]] = {}
        self._light: dict[str, tuple[tuple, Profile]] = {}
        self._pooled_cache: Optional[tuple[tuple, Pooled]] = None
        self._mlock = threading.Lock()
        self._backtests: dict[tuple, Optional[dict]] = {}
        self._accuracy: Optional[tuple] = None
        self._calibration: Optional[tuple] = None

    def now(self) -> int:
        if self.demo:
            gyms = self.store.gyms()
            return max((g["last_ts"] for g in gyms), default=int(time.time())) + 60
        return int(time.time())

    def location(self, gym: str):
        return locate(gym, self.cfg.extra.get("default_city", "Düsseldorf"),
                      self.cfg.extra.get("locations"))

    def _ctx_version(self) -> int:
        if self.ctx is None:
            return 0
        return max((int(r["fetched_at"]) for r in self.ctx.status()), default=0)

    def _pooled_inputs(self, now: int) -> tuple:
        """series_by_gym und feats_by_gym ueber den konfigurierten Zeitraum."""
        start = now - self.cfg.history_weeks * 7 * 86400
        series_by_gym = {}
        feats_by_gym = {}
        for g in self.store.gyms():
            gym = g["gym"]
            series_by_gym[gym] = self.store.series(gym, start, now)
            feats_by_gym[gym] = FeatureSource(self.location(gym), self.tz, self.ctx,
                                              start, now + 8 * 86400)
        return series_by_gym, feats_by_gym

    def pooled(self) -> "Pooled":
        """Gemeinsames Modell ueber alle Clubs, gecacht wie model()."""
        now = self.now()
        last_ts = max((g["last_ts"] for g in self.store.gyms()), default=0)
        key = (last_ts, now // (self.cfg.bin_minutes * 60), self._ctx_version())
        with self._mlock:
            hit = self._pooled_cache
            if hit and hit[0] == key:
                return hit[1]
        series_by_gym, feats_by_gym = self._pooled_inputs(now)
        bt_key = datetime.fromtimestamp(now, self.tz).date()
        if bt_key not in self._backtests:
            self._backtests[bt_key] = backtest_pooled(series_by_gym, feats_by_gym,
                                                      self.tz, self.cfg.bin_minutes,
                                                      self.cfg.half_life_days)
        use_effects = True
        if self._backtests[bt_key] is not None:
            use_effects = effects_help_pooled(self._backtests[bt_key]) is not False
        pooled = build_pooled(series_by_gym, feats_by_gym, self.tz, now,
                              self.cfg.bin_minutes, self.cfg.half_life_days,
                              use_effects=use_effects, n_boot=200)
        pooled.backtest = self._backtests[bt_key]
        with self._mlock:
            self._pooled_cache = (key, pooled)
        return pooled

    def model(self, gym: str, last_ts: int) -> Model:
        """Prognosemodell aus dem gemeinsamen Pool, gecacht bis sich Zeitraum oder Kontext aendert."""
        now = self.now()
        key = self._model_key(last_ts)
        with self._mlock:
            hit = self._models.get(gym)
            if hit and hit[0] == key:
                return hit[1]
        pooled = self.pooled()
        series = self.store.series(gym, now - self.cfg.history_weeks * 7 * 86400, now)
        model = pooled.model(gym, series)
        help_ = effects_help_pooled(pooled.backtest)
        model.verdict = "hilft" if help_ else ("verworfen" if help_ is False else "ungeprüft")
        with self._mlock:
            self._models[gym] = (key, model)
        return model

    def _model_key(self, last_ts: int) -> tuple:
        now = self.now()
        return (last_ts, now // (self.cfg.bin_minutes * 60), self._ctx_version())

    def _cached_model(self, gym: str, last_ts: int) -> Optional[Model]:
        """Model aus dem Cache, falls der Schluessel noch aktuell ist."""
        with self._mlock:
            hit = self._models.get(gym)
        if hit and hit[0] == self._model_key(last_ts):
            return hit[1]
        return None

    def light_profile(self, gym: str, last_ts: int) -> Profile:
        """Schlankes Wochenprofil ohne Kontext und ohne Modellbau (nur Feiertage via day_kind).
        Reicht fuer typical_now in der Uebersicht, kostet Bruchteile des vollen Modells."""
        now = self.now()
        key = (last_ts, now // (self.cfg.bin_minutes * 60))
        with self._mlock:
            hit = self._light.get(gym)
            if hit and hit[0] == key:
                return hit[1]
        feats = FeatureSource(self.location(gym), self.tz, None)
        series = self.store.series(gym, now - self.cfg.history_weeks * 7 * 86400, now)
        prof = Profile(series, self.tz, now, self.cfg.bin_minutes, self.cfg.half_life_days,
                       day_kind=feats.day_kind)
        with self._mlock:
            self._light[gym] = (key, prof)
        return prof

    def _sources(self, loc) -> dict:
        """Statuszeilen der Kontext-Quellen fuer einen Standort."""
        weather_label = DEMO_WEATHER_LABEL if self.demo else WEATHER_LABEL
        school_label = DEMO_SCHOOL_LABEL if self.demo else SCHOOL_LABEL
        rows = self.ctx.status() if self.ctx is not None else []
        weather = {"ok": None, "label": weather_label}
        school = {"ok": None, "label": school_label}
        prefix = f"weather:{loc.key}"
        for r in rows:
            if r["source"] == prefix:
                weather = {"fetched_at": r["fetched_at"], "ok": bool(r["ok"]), "n": r["n"],
                           "error": r["error"], "label": weather_label}
            elif r["source"].startswith(f"school:{loc.state}:"):
                if "fetched_at" not in school or r["fetched_at"] >= school["fetched_at"]:
                    school = {"fetched_at": r["fetched_at"], "ok": bool(r["ok"]), "n": r["n"],
                              "error": r["error"], "label": school_label}
        return {"weather": weather, "school": school}

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
            "context": self.ctx.status() if self.ctx is not None else [],
            "now": self.now(),
        }

    def api_gyms(self, _q) -> list[dict]:
        now = self.now()
        today = datetime.fromtimestamp(now, self.tz).date()
        slot = (datetime.fromtimestamp(now, self.tz).hour * 60
                + datetime.fromtimestamp(now, self.tz).minute) // self.cfg.bin_minutes
        out = []
        for g in self.store.gyms():
            model = self._cached_model(g["gym"], g["last_ts"])
            if model is not None:
                p = model.predict(now)
                typical = round(p["est"], 1) if p else None
                n_days = model.n_days
            else:
                prof = self.light_profile(g["gym"], g["last_ts"])
                st = prof.seasonal_for(today, slot)
                typical = round(st.mean, 1) if st else None
                n_days = prof.n_days
            loc = self.location(g["gym"])
            value = g["count"] if g["count"] is not None else g["pct"]
            fresh = now - g["last_ts"] <= max(3 * self.cfg.interval_minutes * 60, 1800)
            g.update({
                "value": value,
                "unit": "people" if g["count"] is not None else "percent",
                "typical_now": typical,
                "fresh": fresh,
                "n_days": n_days,
                "city": loc.city,
                "state": loc.state,
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
        model = self.model(gym, info["last_ts"])
        loc = model.feats.loc
        recent = self.store.series(gym, now - 3 * 3600, now)
        cap = info["capacity"] if info["count"] is not None else 100.0
        fc = model.forecast(hours=7 * 24, recent=recent)
        calib = self.calibration()
        calibrated = apply_calibration(fc, calib, now, self.tz)
        # Profil-Kurve fuer den heutigen Tag (zum Vergleich "so voll ist es normalerweise");
        # day_kind bildet Feiertage auf Sonntag ab, das Profil zaehlt sie mit
        day0 = datetime.fromtimestamp(now, self.tz).replace(hour=0, minute=0, second=0, microsecond=0)
        start0 = int(day0.timestamp())
        step = self.cfg.bin_minutes * 60
        typical_today = []
        for ts in range(start0, start0 + 86400, step):
            st = model.profile.seasonal_at(ts)
            typical_today.append([ts * 1000, round(st.mean, 1) if st else None])
        bt_full = self._backtests.get(datetime.fromtimestamp(now, self.tz).date())
        bt_gym = None
        if bt_full is not None:
            entry = bt_full["by_gym"].get(gym)
            if entry is not None:
                bt_gym = dict(entry, verdict=model.effects_summary()["verdict"])
        return {
            "gym": gym,
            "unit": self.store.unit(gym),
            "capacity": cap,
            "last": {"ts": info["last_ts"], "value": info["count"] if info["count"] is not None else info["pct"]},
            "history": [[ts * 1000, v] for ts, v in history],
            "today_actual": [[ts * 1000, v] for ts, v in self.store.series(gym, start0, now)],
            "typical_today": typical_today,
            "forecast": [[p["ts"] * 1000, p["est"], p["lo"], p["hi"],
                          p["effect_pct"], p["weather"]] for p in fc],
            "heatmap": model.profile.heatmap(),
            "n_days": model.n_days,
            "days": model.day_summaries(7),
            "effects": model.effects_summary(),
            "location": {"city": loc.city, "state": loc.state},
            "sources": self._sources(loc),
            "first_ts": info["first_ts"],
            "backtest": bt_gym,
            "band": {"source": "kalibriert" if calibrated else "modell",
                     "n": calib["n"], "window_days": calib["window_days"]},
            "now": now,
        }

    def api_weekly(self, q) -> dict:
        """Soll-Ist-Vergleich der letzten Wochen: Tageskurven je Club."""
        gym = q.get("gym", [""])[0]
        now = self.now()
        gyms = {g["gym"]: g for g in self.store.gyms()}
        if gym not in gyms:
            return {"error": "unbekannter Club"}
        feats = FeatureSource(self.location(gym), self.tz, None)
        bin_minutes = self.cfg.bin_minutes
        slots = 24 * 60 // bin_minutes
        start = now - self.cfg.history_weeks * 7 * 86400
        series = self.store.series(gym, start, now)
        today = datetime.fromtimestamp(now, self.tz).date()
        acc: dict = {}
        days: list[dict] = []
        excluded: list[dict] = []
        for ts, v in series:
            dt = datetime.fromtimestamp(ts, self.tz)
            d = dt.date()
            if d >= today:
                continue
            acc.setdefault(d, {}).setdefault((dt.hour * 60 + dt.minute) // bin_minutes, []).append(v)
        for d in sorted(acc):
            holiday = feats.holiday(d)
            if holiday is not None:
                excluded.append({"date": d.isoformat(), "reason": holiday})
                continue
            if feats.bridge(d):
                excluded.append({"date": d.isoformat(), "reason": "Brückentag"})
                continue
            values = []
            for slot in range(slots):
                vals = acc[d].get(slot)
                values.append(round(sum(vals) / len(vals), 1) if vals else None)
            days.append({"date": d.isoformat(), "weekday": d.weekday(), "values": values})
        return {"gym": gym, "bin_minutes": bin_minutes, "now": now,
                "weeks": self.cfg.history_weeks, "days": days, "excluded": excluded}

    def api_accuracy(self, _q) -> dict:
        """Echte Prognosegüte aus den Schnappschuss-CSVs, hoechstens alle 10 Minuten neu."""
        if self.demo:
            return empty_result(self.now())
        now = self.now()
        if self._accuracy is not None and now - self._accuracy[0] < 600:
            return self._accuracy[1]
        res = evaluate(self.snapshot_dir, self.store, self.tz, now)
        self._accuracy = (now, res)
        return res

    def calibration(self) -> dict:
        """Band-Kalibrierung aus den Schnappschuss-CSVs, hoechstens alle 10 Minuten neu."""
        if self.demo:
            return {"window_days": 14, "n": 0, "buckets": {}}
        now = self.now()
        if self._calibration is not None and now - self._calibration[0] < 600:
            return self._calibration[1]
        res = calibration(self.snapshot_dir, self.store, self.tz, now)
        self._calibration = (now, res)
        return res

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
    FONT_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

    class Handler(BaseHTTPRequestHandler):
        server_version = "evo-visits"

        def log_message(self, fmt, *args):  # ruhig bleiben, nur Fehler loggen
            pass

        def _send_icon(self, name: str) -> None:
            # Nur schlichte Dateinamen aus web/icons, kein Pfad-Traversal
            if not FONT_NAME_RE.fullmatch(name) or ".." in name:
                self._send(404, b"not found", "text/plain")
                return
            path = WEB_DIR / "icons" / name
            if not path.is_file():
                self._send(404, b"not found", "text/plain")
                return
            ctype = "image/svg+xml" if name.endswith(".svg") else "image/png"
            self._send(200, path.read_bytes(), ctype,
                       {"Cache-Control": "public, max-age=604800"})

        def _send_font(self, name: str) -> None:
            # Nur schlichte Dateinamen aus web/fonts, kein Pfad-Traversal
            if not FONT_NAME_RE.fullmatch(name) or ".." in name:
                self._send(404, b"not found", "text/plain")
                return
            path = WEB_DIR / "fonts" / name
            if not path.is_file():
                self._send(404, b"not found", "text/plain")
                return
            ctype = ("font/woff2" if name.endswith(".woff2")
                     else "text/plain; charset=utf-8")
            extra = {"Cache-Control": "public, max-age=604800"} if name.endswith(".woff2") else None
            self._send(200, path.read_bytes(), ctype, extra)

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
                elif method == "GET" and url.path.startswith("/fonts/"):
                    self._send_font(url.path[len("/fonts/"):])
                elif method == "GET" and url.path == "/manifest.webmanifest":
                    self._send(200, (WEB_DIR / "manifest.webmanifest").read_bytes(),
                               "application/manifest+json")
                elif method == "GET" and url.path == "/sw.js":
                    self._send(200, (WEB_DIR / "sw.js").read_bytes(),
                               "text/javascript; charset=utf-8")
                elif method == "GET" and url.path.startswith("/icons/"):
                    self._send_icon(url.path[len("/icons/"):])
                elif method == "GET" and url.path == "/api/status":
                    self._json(app.api_status(q))
                elif method == "GET" and url.path == "/api/gyms":
                    self._json(app.api_gyms(q))
                elif method == "GET" and url.path == "/api/gym":
                    self._json(app.api_gym(q))
                elif method == "GET" and url.path == "/api/weekly":
                    self._json(app.api_weekly(q))
                elif method == "GET" and url.path == "/api/accuracy":
                    self._json(app.api_accuracy(q))
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
    ctx = Context(store, cfg)
    collector = None
    if collect and not demo:
        collector = Collector(cfg, store, ctx)
        collector.start()
        log(f"Sammler aktiv: alle {cfg.interval_minutes} min zwischen {cfg.active_hours} ({cfg.url})")
        threading.Thread(target=ctx.refresh_for_gyms,
                         args=([g["gym"] for g in store.gyms()],),
                         daemon=True, name="context-refresh").start()
    app = App(cfg, store, collector, demo, ctx)
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
