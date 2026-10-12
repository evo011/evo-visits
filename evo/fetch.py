"""Abruf der Live-Zahlen: plain HTTP mit Auto-Erkennung, optional Headless-Browser (Playwright).

Reihenfolge im Modus "auto":
  1. manuell konfigurierte Quelle (config.json -> source)
  2. zuletzt erfolgreiche Quelle (data/source.json)
  3. HTML laden: eingebettetes JSON, dann API-URLs aus HTML und JS-Bundles, dann Text-Heuristik
  4. Playwright: Seite rendern, alle JSON-Antworten mitschneiden, ggf. Club-Dropdown durchklicken
"""

from __future__ import annotations

import json
import re
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

from .config import DATA_DIR, Config
from .extract import (Candidate, best_from_html, best_from_json, clean_name, embedded_json_blobs,
                      single_from_html, single_from_json, visible_text_items)
from .store import Reading, Store

SOURCE_CACHE = DATA_DIR / "source.json"
TRACKER_HOSTS = ("google", "gstatic", "doubleclick", "facebook", "hotjar", "sentry", "cookiebot",
                 "usercentrics", "segment", "mixpanel", "cloudflareinsights", "clarity.ms",
                 "tiktok", "linkedin", "bing.com", "matomo", "onetrust", "trustarc")
URL_HINT = re.compile(r"api|visit|occupan|utili[sz]|auslastung|club|location|studio|gym|checkin|live|count",
                      re.I)


class FetchError(RuntimeError):
    pass


@dataclass
class FetchResult:
    readings: list[Reading]
    source: str
    candidate: Optional[Candidate] = None


@dataclass
class ProbeLog:
    lines: list[str] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)

    def add(self, line: str) -> None:
        self.lines.append(line)


# ---------------------------------------------------------------------------------------------
# HTTP

def _ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context()


def http_get(url: str, cfg: Config, accept: str = "*/*", timeout: int = 20) -> tuple[str, str]:
    req = urllib.request.Request(url, headers={
        "User-Agent": cfg.user_agent,
        "Accept": accept,
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.6",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
            ctype = resp.headers.get("Content-Type", "")
            body = resp.read(8_000_000)
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, ssl.SSLCertVerificationError):
            # python.org-Installationen auf macOS haben oft keine CA-Zertifikate; certifi hilft.
            try:
                import certifi  # type: ignore
                ctx = ssl.create_default_context(cafile=certifi.where())
                with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                    ctype = resp.headers.get("Content-Type", "")
                    body = resp.read(8_000_000)
            except ImportError:
                raise FetchError(
                    "SSL-Zertifikat konnte nicht geprueft werden. Auf macOS einmal "
                    "'/Applications/Python 3.x/Install Certificates.command' ausfuehren "
                    "oder 'pip3 install certifi'."
                ) from e
        else:
            raise FetchError(f"{url}: {reason}") from e
    charset = "utf-8"
    m = re.search(r"charset=([\w-]+)", ctype)
    if m:
        charset = m.group(1)
    return ctype, body.decode(charset, errors="replace")


def _json_or_none(text: str) -> Any:
    t = text.lstrip()
    if not t or t[0] not in "[{":
        return None
    try:
        return json.loads(t)
    except ValueError:
        return None


# ---------------------------------------------------------------------------------------------
# Quellen

def _from_manual(cfg: Config) -> Optional[FetchResult]:
    src = cfg.source or {}
    if not src.get("json_url"):
        return None
    _, text = http_get(src["json_url"], cfg, accept="application/json")
    data = json.loads(text)
    for part in filter(None, (src.get("list_path") or "").split(".")):
        data = data[int(part)] if isinstance(data, list) else data[part]
    readings = []
    for item in data if isinstance(data, list) else data.values():
        def get(key):
            if not key:
                return None
            v: Any = item
            for p in key.split("."):
                v = v.get(p) if isinstance(v, dict) else None
            return v
        name = get(src.get("name_key", "name"))
        if name is None:
            continue
        to_f = lambda v: float(v) if v not in (None, "") else None  # noqa: E731
        readings.append(Reading(gym=clean_name(str(name)), count=to_f(get(src.get("count_key"))),
                                capacity=to_f(get(src.get("capacity_key"))), pct=to_f(get(src.get("pct_key")))))
    return FetchResult(readings, f"manual:{src['json_url']}")


def _from_cached(cfg: Config) -> Optional[FetchResult]:
    if not SOURCE_CACHE.exists():
        return None
    try:
        cached = json.loads(SOURCE_CACHE.read_text())
    except ValueError:
        return None
    if cached.get("kind") != "json_url" or cached.get("page") != cfg.url:
        return None
    _, text = http_get(cached["url"], cfg, accept="application/json")
    data = _json_or_none(text)
    cand = best_from_json(data, cached["url"]) if data is not None else None
    if cand and len(cand.readings) >= max(1, int(cached.get("n", 1) * 0.5)):
        return FetchResult(cand.readings, f"json:{cached['url']}", cand)
    return None


def _remember(kind: str, url: str, n: int, cfg: Config) -> None:
    SOURCE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    SOURCE_CACHE.write_text(json.dumps({"kind": kind, "url": url, "n": n, "page": cfg.url,
                                        "found_at": int(time.time())}, indent=2))


def discover_urls(page: str, base: str, cfg: Config, log: Optional[ProbeLog] = None) -> list[str]:
    """Moegliche API-Endpunkte aus HTML und den eingebundenen JS-Bundles."""
    texts = [page]
    scripts = re.findall(r"<script[^>]+src=[\"']([^\"']+)[\"']", page, re.I)
    for src in scripts[:12]:
        url = urljoin(base, src)
        if any(t in urlparse(url).netloc for t in TRACKER_HOSTS):
            continue
        try:
            _, js = http_get(url, cfg)
            texts.append(js)
            if log:
                log.add(f"  JS-Bundle geladen: {url} ({len(js)} Zeichen)")
        except FetchError as e:
            if log:
                log.add(f"  JS-Bundle fehlgeschlagen: {url}: {e}")
    found: list[str] = []
    rx = re.compile(r"[\"'`]((?:https?://[^\"'`\s]+)|(?:/[A-Za-z0-9_\-./?=&%]+))[\"'`]")
    for text in texts:
        for m in rx.finditer(text):
            raw = m.group(1).split("${")[0]
            if not URL_HINT.search(raw) or re.search(r"\.(js|css|png|jpe?g|svg|webp|woff2?|ico|gif|mp4)(\?|$)", raw):
                continue
            url = urljoin(base, raw)
            host = urlparse(url).netloc
            if not host or any(t in host for t in TRACKER_HOSTS):
                continue
            if url not in found and url.rstrip("/") != base.rstrip("/"):
                found.append(url)
    # API-artige Pfade zuerst
    found.sort(key=lambda u: (0 if re.search(r"/api/|api\.", u) else 1, len(u)))
    return found[:25]


def _from_static(cfg: Config, log: Optional[ProbeLog] = None) -> Optional[FetchResult]:
    ctype, page = http_get(cfg.url, cfg, accept="text/html,application/json")
    if log:
        log.add(f"[static] {cfg.url}: {ctype}, {len(page)} Zeichen")
        log.files["page.html"] = page
    data = _json_or_none(page)
    if data is not None:
        cand = best_from_json(data, cfg.url)
        if cand:
            return FetchResult(cand.readings, f"json:{cfg.url}", cand)

    best: Optional[Candidate] = None
    for kind, blob in embedded_json_blobs(page):
        c = best_from_json(blob, f"embedded:{kind}")
        if log and c:
            log.add(f"  eingebettetes JSON ({kind}): {c.describe()}")
        if c and (best is None or c.score > best.score):
            best = c
    if best and len(best.readings) >= 2:
        return FetchResult(best.readings, best.where, best)

    urls = discover_urls(page, cfg.url, cfg, log)
    if log:
        log.add(f"  {len(urls)} Kandidaten-URLs gefunden")
        log.files["discovered_urls.txt"] = "\n".join(urls)
    for i, url in enumerate(urls):
        try:
            ct, body = http_get(url, cfg, accept="application/json")
        except FetchError as e:
            if log:
                log.add(f"  x {url}: {e}")
            continue
        data = _json_or_none(body)
        if data is None:
            continue
        c = best_from_json(data, url)
        if log:
            log.files[f"json_{i:02d}.json"] = json.dumps({"url": url, "data": data}, ensure_ascii=False, indent=1)[:500_000]
            log.add(f"  JSON {url}: {c.describe() if c else 'keine Club-Liste erkannt'}")
        if c and (best is None or c.score > best.score):
            best = c
            if len(c.readings) >= 2:
                _remember("json_url", url, len(c.readings), cfg)
    if best and len(best.readings) >= 2:
        src = f"json:{best.where}" if best.where.startswith("http") else best.where
        return FetchResult(best.readings, src, best)

    text_cand = best_from_html(page, "static-html-text")
    if log:
        log.add(f"  Text-Heuristik statisches HTML: {text_cand.describe() if text_cand else 'nichts'}")
    if text_cand and len(text_cand.readings) >= 2:
        return FetchResult(text_cand.readings, "static-html-text", text_cand)
    return None


def playwright_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
        return True
    except ImportError:
        return False


def _from_browser(cfg: Config, log: Optional[ProbeLog] = None) -> Optional[FetchResult]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=cfg.browser_path or None)
        try:
            context = browser.new_context(user_agent=cfg.user_agent, locale="de-DE",
                                          timezone_id=cfg.timezone)
            page = context.new_page()
            responses: list[Any] = []
            page.on("response", lambda r: responses.append(r))
            page.goto(cfg.url, wait_until="networkidle", timeout=60_000)
            page.wait_for_timeout(2000)

            def json_responses(start: int = 0) -> list[tuple[str, Any]]:
                out = []
                for r in responses[start:]:
                    if "json" not in (r.headers.get("content-type") or ""):
                        continue
                    try:
                        out.append((r.url, r.json()))
                    except Exception:
                        continue
                return out

            best: Optional[Candidate] = None
            for i, (url, data) in enumerate(json_responses()):
                c = best_from_json(data, url)
                if log:
                    log.files[f"browser_json_{i:02d}.json"] = json.dumps({"url": url, "data": data},
                                                                         ensure_ascii=False, indent=1)[:500_000]
                    log.add(f"  [browser] JSON {url}: {c.describe() if c else 'keine Club-Liste erkannt'}")
                if c and (best is None or c.score > best.score):
                    best = c
            html = page.content()
            if log:
                log.files["rendered.html"] = html
                text = "\n".join(t for t, _ in visible_text_items(html))
                log.files["rendered_text.txt"] = text
            text_cand = best_from_html(html, "browser-html-text")
            if log:
                log.add(f"  [browser] Text-Heuristik: {text_cand.describe() if text_cand else 'nichts'}")
            if text_cand and (best is None or text_cand.score > best.score):
                best = text_cand
            if best and len(best.readings) >= 2:
                if best.where.startswith("http"):
                    _remember("json_url", best.where, len(best.readings), cfg)
                    return FetchResult(best.readings, f"json:{best.where}", best)
                return FetchResult(best.readings, f"browser:{best.where}", best)

            # Einzel-Club-Ansicht mit Dropdown: alle Optionen durchklicken.
            readings = _iterate_select(page, responses, json_responses, log)
            if readings:
                return FetchResult(readings, "browser:select")
            if best:
                return FetchResult(best.readings, f"browser:{best.where}", best)
            return None
        finally:
            browser.close()


def _iterate_select(page, responses, json_responses, log: Optional[ProbeLog]) -> list[Reading]:
    readings: list[Reading] = []
    for sel in page.query_selector_all("select"):
        options = sel.eval_on_selector_all(
            "option", "els => els.map(e => [e.value, (e.textContent || '').trim()])")
        options = [(v, t) for v, t in options if v and t]
        if len(options) < 3:
            continue
        if log:
            log.add(f"  [browser] Dropdown mit {len(options)} Optionen, klicke durch")
        labels = [t for _, t in options]
        for value, label in options:
            start = len(responses)
            try:
                sel.select_option(value)
                page.wait_for_load_state("networkidle", timeout=15_000)
                page.wait_for_timeout(800)
            except Exception:
                continue
            reading = None
            for _, data in json_responses(start):
                reading = single_from_json(data)
                if reading:
                    break
            if not reading:
                reading = single_from_html(page.content(), exclude=labels)
            if reading:
                reading.gym = clean_name(label)
                readings.append(reading)
                if log:
                    log.add(f"    {label}: {reading.count if reading.count is not None else reading.pct}")
        if readings:
            break
    return readings


# ---------------------------------------------------------------------------------------------
# Oeffentliche API

def fetch_readings(cfg: Config) -> FetchResult:
    errors: list[str] = []
    steps = []
    if cfg.mode in ("auto", "static"):
        steps += [("manual", _from_manual), ("cached", _from_cached), ("static", _from_static)]
    if cfg.mode in ("auto", "browser") and playwright_available():
        steps.append(("browser", _from_browser))
    for name, fn in steps:
        try:
            res = fn(cfg)
        except Exception as e:  # einzelne Strategie darf scheitern, die naechste probiert es
            errors.append(f"{name}: {e}")
            continue
        if res and res.readings:
            return res
    hint = "" if playwright_available() else (
        " Tipp: 'pip3 install playwright && python3 -m playwright install chromium' "
        "aktiviert den Browser-Modus fuer JavaScript-Seiten.")
    raise FetchError("Keine Besucherzahlen erkannt. " + " | ".join(errors) + hint +
                     " Details: 'python3 evo_visits.py probe'.")


def collect_once(cfg: Config, store: Store) -> FetchResult:
    ts = int(time.time())
    try:
        res = fetch_readings(cfg)
    except Exception as e:
        store.log_run(False, 0, error=str(e), ts=ts)
        raise
    n = store.add_readings(ts, res.readings)
    store.log_run(True, n, source=res.source, ts=ts)
    return res


def probe(cfg: Config) -> Path:
    """Alles mitschneiden, was die Seite liefert, und die Heuristik-Ergebnisse protokollieren."""
    out = DATA_DIR / "probe" / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    log = ProbeLog()
    log.add(f"Probe {cfg.url} um {time.strftime('%Y-%m-%d %H:%M:%S')}")
    for name, fn in (("static", _from_static), ("browser", _from_browser)):
        if name == "browser" and not playwright_available():
            log.add("[browser] Playwright nicht installiert, uebersprungen")
            continue
        try:
            res = fn(cfg, log)
            if res:
                log.add(f"=> {name}: {len(res.readings)} Clubs via {res.source}")
                for r in res.readings[:40]:
                    log.add(f"     {r.gym}: count={r.count} capacity={r.capacity} pct={r.pct}")
            else:
                log.add(f"=> {name}: nichts erkannt")
        except Exception as e:
            log.add(f"=> {name}: Fehler {type(e).__name__}: {e}")
    for fname, content in log.files.items():
        (out / fname).write_text(content, encoding="utf-8")
    (out / "summary.txt").write_text("\n".join(log.lines), encoding="utf-8")
    print("\n".join(log.lines))
    print(f"\nAlle Rohdaten liegen in {out}")
    return out
