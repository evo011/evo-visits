#!/usr/bin/env python3
"""EVO Visits Tracker.

  python3 evo_visits.py serve          Dashboard + Sammler starten (http://127.0.0.1:8765)
  python3 evo_visits.py collect        einmal abrufen und speichern (fuer cron)
  python3 evo_visits.py probe          Seite analysieren, Rohdaten nach data/probe/ schreiben
  python3 evo_visits.py demo           Dashboard mit synthetischen Demo-Daten (eigene DB)
  python3 evo_visits.py context        Wetter + Schulferien der Clubs einmal abrufen und cachen
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
    ap.add_argument("command", choices=["serve", "collect", "probe", "demo", "context",
                                        "export-csv", "import-csv", "launchd"])
    ap.add_argument("--config", type=Path, default=None, help="Pfad zu config.json")
    ap.add_argument("--port", type=int, help="Port fuer das Dashboard")
    ap.add_argument("--interval", type=int, help="Abrufintervall in Minuten")
    ap.add_argument("--mode", choices=["auto", "static", "browser", "api"], help="Abrufstrategie")
    ap.add_argument("--no-collect", action="store_true", help="nur Dashboard, kein Sammler")
    ap.add_argument("--respect-window", action="store_true",
                    help="collect: nur innerhalb der aktiven Stunden abrufen")
    ap.add_argument("--active-hours", help="Aktives Fenster als HH:MM-HH:MM (ueberschreibt config)")
    ap.add_argument("--dir", type=Path, help="Verzeichnis fuer CSV-Export/-Import (Default: data/)")
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
    elif args.command == "export-csv":
        from evo.csvstore import export_monthly
        from evo.forecast import get_tz
        directory = args.dir or DATA_DIR
        changed = export_monthly(Store(cfg.db_path), directory, get_tz(cfg.timezone))
        if changed:
            for p in changed:
                print(f" geschrieben: {p}")
        else:
            print("Keine Änderungen, keine Dateien geschrieben.")
    elif args.command == "import-csv":
        from evo.csvstore import import_dir
        directory = args.dir or DATA_DIR
        n = import_dir(Store(cfg.db_path), directory)
        print(f"{n} Zeilen eingelesen aus {directory}")
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
