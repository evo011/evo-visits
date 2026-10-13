#!/usr/bin/env python3
"""EVO Visits Tracker.

  python3 evo_visits.py serve          Dashboard + Sammler starten (http://127.0.0.1:8765)
  python3 evo_visits.py collect        einmal abrufen und speichern (fuer cron)
  python3 evo_visits.py probe          Seite analysieren, Rohdaten nach data/probe/ schreiben
  python3 evo_visits.py demo           Dashboard mit synthetischen Demo-Daten (eigene DB)
  python3 evo_visits.py context        Wetter + Schulferien der Clubs einmal abrufen und cachen
  python3 evo_visits.py export         Static-Seite (JSON + index.html) fuer GitHub Pages schreiben
  python3 evo_visits.py export-csv     Monats-Dateien visits-YYYY-MM.csv nach data/ schreiben
  python3 evo_visits.py import-csv     Monats-Dateien aus data/ wieder einlesen
  python3 evo_visits.py launchd        macOS-Autostart (LaunchAgent-plist) ausgeben
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from evo.config import DATA_DIR, PROJECT_DIR, load_config
from evo.store import Store


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="EVO Visits Tracker")
    ap.add_argument("command", choices=["serve", "collect", "probe", "demo", "context", "export",
                                        "export-csv", "import-csv", "evaluate", "launchd"])
    ap.add_argument("--config", type=Path, default=None, help="Pfad zu config.json")
    ap.add_argument("--port", type=int, help="Port fuer das Dashboard")
    ap.add_argument("--interval", type=int, help="Abrufintervall in Minuten")
    ap.add_argument("--mode", choices=["auto", "static", "browser", "api"], help="Abrufstrategie")
    ap.add_argument("--no-collect", action="store_true", help="nur Dashboard, kein Sammler")
    ap.add_argument("--respect-window", action="store_true",
                    help="collect: nur innerhalb der aktiven Stunden abrufen")
    ap.add_argument("--active-hours", help="Aktives Fenster als HH:MM-HH:MM (ueberschreibt config)")
    ap.add_argument("--dir", type=Path, help="Verzeichnis fuer CSV-Export/-Import (Default: data/)")
    ap.add_argument("--out", type=Path, help="Ausgabeverzeichnis fuer 'export' (Default: site/)")
    ap.add_argument("--forecast-snapshot", type=Path,
                    help="export: zusaetzlich Prognose-Schnappschuss in dieses Verzeichnis")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    if args.port:
        cfg.port = args.port
    if args.interval:
        cfg.interval_minutes = args.interval
    if args.mode:
        cfg.mode = args.mode
    if args.active_hours:
        cfg.active_hours = args.active_hours

    if args.command == "serve":
        from evo.server import serve
        serve(cfg, Store(cfg.db_path), collect=not args.no_collect)
    elif args.command == "collect":
        from evo.config import in_active_window
        from evo.fetch import collect_once
        if args.respect_window and not in_active_window(cfg, time.time()):
            print(f"Außerhalb des Abruffensters ({cfg.active_hours}), nichts abgerufen.")
            return 0
        try:
            res = collect_once(cfg, Store(cfg.db_path))
        except Exception as e:
            print(f"Abruf fehlgeschlagen: {e}", file=sys.stderr)
            return 1
        if res.source == "skipped:min-interval":
            print("Letzter Abruf vor weniger als 10 Minuten, übersprungen.")
        else:
            print(f"{len(res.readings)} Clubs gespeichert ({res.source})")
        for r in res.readings:
            print(f"  {r.gym}: {r.count if r.count is not None else str(r.pct) + ' %'}"
                  + (f" / {r.capacity:g}" if r.capacity else ""))
    elif args.command == "probe":
        from evo.fetch import probe
        probe(cfg)
    elif args.command == "demo":
        from evo.demo import generate
        from evo.server import serve
        cfg.db_path = str(DATA_DIR / "demo.db")
        # Demo-DB immer neu anlegen, damit die Demo zur aktuellen Version passt
        for suffix in ("", "-wal", "-shm"):
            Path(cfg.db_path + suffix).unlink(missing_ok=True)
        store = Store(cfg.db_path)
        print(f"Erzeuge Demo-Daten ... {generate(store, cfg.timezone)} Messpunkte")
        serve(cfg, store, demo=True)
    elif args.command == "context":
        from evo.context import Context
        store = Store(cfg.db_path)
        gyms = [g["gym"] for g in store.gyms()]
        if not gyms:
            print("Keine Clubs in der Datenbank -- vorher 'collect' ausführen.")
            return 0
        res = Context(store, cfg).refresh_for_gyms(gyms, force=True)
        for kind, title in (("weather", "Wetter"), ("school", "Schulferien")):
            print(f"{title}:")
            for source, r in res[kind].items():
                if r["ok"]:
                    print(f"  {source}: ok, {r['n']} Zeilen" + (" (aus Cache)" if r["cached"] else ""))
                else:
                    print(f"  {source}: FEHLER, {r['error']}")
    elif args.command == "export":
        from evo.context import Context
        from evo.snapshot import snapshot_forecasts
        from evo.static_export import export_site
        store = Store(cfg.db_path)
        out_dir = args.out or (PROJECT_DIR / "site")
        snap_dir = args.forecast_snapshot or (Path(cfg.db_path).parent / "forecasts")
        res = export_site(cfg, store, out_dir, ctx=Context(store, cfg), snapshot_dir=snap_dir)
        print(f"{res['gyms']} Clubs, {res['files']} Dateien nach {res['out']} geschrieben")
        if args.forecast_snapshot and not res["app"].demo:
            try:
                path = snapshot_forecasts(res["app"], args.forecast_snapshot, res["app"].now())
            except Exception as e:
                print(f"Prognose-Schnappschuss fehlgeschlagen: {e}", file=sys.stderr)
                return 0
            if path:
                print(f" Prognose-Schnappschuss: {path}")
            else:
                print(" Kein neuer Prognose-Schnappschuss (heute schon vorhanden oder keine Punkte).")
    elif args.command == "export-csv":
        from evo.csvstore import export_monthly, export_weather_monthly
        from evo.forecast import get_tz
        directory = args.dir or DATA_DIR
        store = Store(cfg.db_path)
        tz = get_tz(cfg.timezone)
        changed = export_monthly(store, directory, tz)
        changed += export_weather_monthly(store, directory, tz, int(time.time()))
        if changed:
            for p in changed:
                print(f" geschrieben: {p}")
        else:
            print("Keine Änderungen, keine Dateien geschrieben.")
    elif args.command == "import-csv":
        from evo.csvstore import import_dir, import_weather_dir
        directory = args.dir or DATA_DIR
        store = Store(cfg.db_path)
        n = import_dir(store, directory)
        wn = import_weather_dir(store, directory)
        print(f"{n} Zeilen eingelesen aus {directory}")
        print(f"{wn} Wetterzeilen eingelesen aus {directory}")
    elif args.command == "evaluate":
        from evo.evaluate import evaluate
        from evo.forecast import get_tz
        store = Store(cfg.db_path)
        snap_dir = args.dir or (DATA_DIR / "forecasts")
        res = evaluate(snap_dir, store, get_tz(cfg.timezone), int(time.time()))
        if not res["all"]:
            print("Keine auswertbaren Prognosen gefunden.")
            return 0
        labels = {"same_day": "am selben Tag", "1d": "einen Tag vorher",
                  "2_3d": "2–3 Tage vorher", "4_7d": "4–7 Tage vorher"}
        def de(v):
            return None if v is None else f"{v:.1f}".replace(".", ",")
        for group in res["buckets"]:
            m = res["all"].get(group)
            if not m:
                continue
            line = (f"{labels[group]}: {m['n']} Werte, ±{de(m['mae'])} daneben, "
                    f"Band {m['coverage_pct']} %")
            if m.get("mae_last_week") is not None:
                line += f", Vorwoche ±{de(m['mae_last_week'])}"
            print(line)
        print(f"{res['days']} Tage Prognosen ausgewertet (seit {res['first_made']}).")
    elif args.command == "launchd":
        print(LAUNCHD.format(python=sys.executable, script=PROJECT_DIR / "evo_visits.py",
                             cwd=PROJECT_DIR, log=DATA_DIR / "server.log"))
    return 0


LAUNCHD = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>local.evo-visits</string>
  <key>ProgramArguments</key>
  <array>
    <string>{python}</string>
    <string>{script}</string>
    <string>serve</string>
  </array>
  <key>WorkingDirectory</key><string>{cwd}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>"""


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
