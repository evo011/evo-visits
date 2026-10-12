#!/usr/bin/env python3
"""EVO Visits Tracker.

  python3 evo_visits.py serve      Dashboard + Sammler starten (http://127.0.0.1:8765)
  python3 evo_visits.py collect    einmal abrufen und speichern (fuer cron)
  python3 evo_visits.py probe      Seite analysieren, Rohdaten nach data/probe/ schreiben
  python3 evo_visits.py demo       Dashboard mit synthetischen Demo-Daten (eigene DB)
  python3 evo_visits.py launchd    macOS-Autostart (LaunchAgent-plist) ausgeben
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from evo.config import DATA_DIR, PROJECT_DIR, load_config
from evo.store import Store


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="EVO Visits Tracker")
    ap.add_argument("command", choices=["serve", "collect", "probe", "demo", "launchd"])
    ap.add_argument("--config", type=Path, default=None, help="Pfad zu config.json")
    ap.add_argument("--port", type=int, help="Port fuer das Dashboard")
    ap.add_argument("--interval", type=int, help="Abrufintervall in Minuten")
    ap.add_argument("--mode", choices=["auto", "static", "browser"], help="Abrufstrategie")
    ap.add_argument("--no-collect", action="store_true", help="nur Dashboard, kein Sammler")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    if args.port:
        cfg.port = args.port
    if args.interval:
        cfg.interval_minutes = args.interval
    if args.mode:
        cfg.mode = args.mode

    if args.command == "serve":
        from evo.server import serve
        serve(cfg, Store(cfg.db_path), collect=not args.no_collect)
    elif args.command == "collect":
        from evo.fetch import collect_once
        res = collect_once(cfg, Store(cfg.db_path))
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
        store = Store(cfg.db_path)
        if not store.gyms():
            print(f"Erzeuge Demo-Daten ... {generate(store, cfg.timezone)} Messpunkte")
        serve(cfg, store, demo=True)
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
