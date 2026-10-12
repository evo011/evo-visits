"""Static-Export des Dashboards: API-Antworten als JSON-Dateien plus index.html fuer GitHub Pages."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from .config import WEB_DIR, Config
from .server import App
from .store import Store

_UMAP = {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"}
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def slug(name: str) -> str:
    """Clubname als Dateiname-Slug: klein, Umlaute als ae/oe/ue/ss, Rest als Trennzeichen."""
    s = "".join(_UMAP.get(ch, ch) for ch in name.lower())
    s = _NON_ALNUM.sub("-", s)
    return s.strip("-")


class _FixedNowApp(App):
    """App mit festem Zeitpunkt, damit der Export reproduzierbar ist."""

    def __init__(self, cfg, store, ctx, now_ts: int):
        super().__init__(cfg, store, None, demo=False, ctx=ctx)
        self._fixed_now = int(now_ts)

    def now(self) -> int:
        return self._fixed_now


def _dump(data) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _write(path: Path, text: str, written: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    written.append(path)


def _inject_static(html: str, generated_at: int) -> str:
    marker = f'<script>window.EVO_STATIC = {{"generated_at": {generated_at}}};</script>\n'
    body_at = html.find("<body")
    first_script = html.find("<script>", body_at)
    if first_script < 0:
        return html + marker
    return html[:first_script] + marker + html[first_script:]


def _cleanup_old(out_dir: Path, subdir: str, keep: set) -> None:
    """Nicht mehr aktuelle Export-Dateien loeschen (nur innerhalb von out_dir/api/<subdir>)."""
    target = out_dir / "api" / subdir
    if not target.is_dir():
        return
    for p in target.iterdir():
        if p.is_file() and p.name not in keep:
            p.unlink()


def _copy_fonts(out_dir: Path, written: list) -> None:
    """web/fonts nach out_dir/fonts kopieren; alte Dateien dort vorher entfernen."""
    src = WEB_DIR / "fonts"
    dst = out_dir / "fonts"
    if dst.is_dir():
        for p in dst.iterdir():
            if p.is_file():
                p.unlink()
    if not src.is_dir():
        return
    dst.mkdir(parents=True, exist_ok=True)
    for p in sorted(src.iterdir()):
        if p.is_file():
            target = dst / p.name
            target.write_bytes(p.read_bytes())
            written.append(target)


def export_site(cfg: Config, store: Store, out_dir: Path,
                ctx=None, now: Optional[int] = None) -> dict:
    """Alle API-Endpunkte als Dateien nach out_dir schreiben und index.html dazulegen.

    Kein Netz-Abruf: frische Kontextdaten liefert vorher 'python3 evo_visits.py context'.
    """
    app = _FixedNowApp(cfg, store, ctx, now_ts=now) if now is not None \
        else App(cfg, store, None, demo=False, ctx=ctx)
    generated_at = now if now is not None else app.now()
    out_dir = Path(out_dir)
    (out_dir / "api").mkdir(parents=True, exist_ok=True)
    written: list = []

    _write(out_dir / ".nojekyll", "", written)
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    _write(out_dir / "index.html", _inject_static(html, generated_at), written)

    status = app.api_status({})
    status["static"] = True
    status["generated_at"] = generated_at
    _write(out_dir / "api" / "status.json", _dump(status), written)

    gyms = app.api_gyms({})
    for g in gyms:
        g["slug"] = slug(g["gym"])
    _write(out_dir / "api" / "gyms.json", _dump(gyms), written)

    gym_keep, weekly_keep = set(), set()
    days = str(int(cfg.history_weeks) * 7)
    for g in gyms:
        s = g["slug"]
        detail = app.api_gym({"gym": [g["gym"]], "days": [days]})
        _write(out_dir / "api" / "gym" / f"{s}.json", _dump(detail), written)
        gym_keep.add(f"{s}.json")
        weekly = app.api_weekly({"gym": [g["gym"]]})
        _write(out_dir / "api" / "weekly" / f"{s}.json", _dump(weekly), written)
        weekly_keep.add(f"{s}.json")

    (out_dir / "api" / "export.csv").write_bytes(app.export_csv())
    written.append(out_dir / "api" / "export.csv")

    _cleanup_old(out_dir, "gym", gym_keep)
    _cleanup_old(out_dir, "weekly", weekly_keep)

    _copy_fonts(out_dir, written)

    return {"gyms": len(gyms), "files": len(written), "out": str(out_dir)}
