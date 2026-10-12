# EVO Visits Tracker

Schreibt die Live-Besucherzahlen von [visits.evofitness.de](https://visits.evofitness.de/) für alle EVO-Clubs in Deutschland mit, zeigt den Verlauf und sagt voraus, wie voll es in den nächsten Stunden und Tagen wird. Wetter, Feiertage, Brückentage und Schulferien fließen in die Prognose ein. Eine eigene Karte schlägt die beste Zeit fürs Training vor.

**Live:** https://evo011.github.io/evo-visits/ (wird etwa alle 15 Minuten zwischen 5 und 23 Uhr aktualisiert)

Nur Python 3.9+ mit Standardbibliothek, keine Pakete. Das Dashboard ist eine einzelne HTML-Datei ohne externe Abhängigkeiten.

![Dashboard mit Demo-Daten](docs/dashboard-demo.png)

## Datenquellen

| Quelle | Wofür | Abruf |
|---|---|---|
| EVO-API `visits.evofitness.de/api/v1/locations?operator=…` und `/locations/<id>/current` | Personen im Club (`current`), Richtwert (`max_capacity`, derzeit überall 35), Auslastung in Prozent | höchstens alle 10 Minuten, 1 + 15 Requests mit 0,3 s Abstand |
| [Open-Meteo](https://open-meteo.com/) | stündlich Temperatur, Niederschlag, Sonnenscheindauer, Wettercode je Stadt, 92 Tage zurück (real etwa 74) und 8 Tage voraus | höchstens stündlich je Stadt |
| [OpenHolidays](https://www.openholidaysapi.org/) | Schulferien je Bundesland | höchstens wöchentlich je Land und Jahr |
| lokal berechnet (`evo/calendar_de.py`) | gesetzliche Feiertage je Bundesland, Heiligabend, Silvester, Brückentage | – |

Die EVO-API ist nicht dokumentiert. Die Seite lädt sie per JavaScript, die Operator-ID für Deutschland steht in `js/config.js` der Seite und ist im Code hinterlegt. Der Endpunkt `/timeline` liefert kumulierte Werte (über 1000 %) und wird nicht verwendet. Wetter und Ferien werden in SQLite gecacht, Fehler dort brechen nichts ab: Die Prognose läuft dann ohne die betroffenen Effekte weiter.

Die 15 Clubs heißen in der API nur nach ihrem Viertel („EVO Unterbilk“). Die Zuordnung zu Stadt und Bundesland steht in `CLUB_CITIES` in `evo/calendar_de.py`: Berlin (Alexanderplatz, Rosenthaler Platz, Spittelmarkt), Dresden (Prager Straße), Düsseldorf (LeFlair, Unterbilk), Hamburg (Blankenese, Ottensen, Schanze, Stadthausbrücke), Köln (Belgisches Viertel, Ehrenstraße), München (Glockenbach, Maxvorstadt), Wiesbaden (Dern'sche Höfe). Neue Clubs ergänzt man dort oder per `"locations"` in `config.json`.

## Hosting: GitHub Actions und Pages

Das Repo ist öffentlich und enthält nur Code und Besucherzahlen, keine Secrets.

- `.github/workflows/collect.yml` läuft per Cron `4,19,34,49 3-22 * * *`. Cron rechnet in UTC, die Stunden 3–22 decken 5–23 Uhr Berliner Zeit in Sommer- und Winterzeit ab. Das genaue Fenster (05:00–23:00 Berlin) prüft das Skript selbst (`collect --respect-window`). Eine concurrency-Gruppe verhindert überlappende Läufe.
- Ablauf je Lauf: Monats-CSVs einlesen, abrufen, Wetter und Ferien auffrischen, `data/visits-YYYY-MM.csv` committen, statische Seite bauen (`export --out site`) und per `actions/deploy-pages` veröffentlichen.
- Die CSVs sind die Quelle der Wahrheit. `data/visits.db` (SQLite) kommt nicht ins Git, sondern liegt im Actions-Cache, damit Wetter und Ferien nicht bei jedem Lauf neu geholt werden.
- Schlägt der EVO-Abruf fehl, wird die Seite trotzdem mit dem letzten Stand deployt und der Lauf rot markiert.
- `.github/workflows/probe.yml` (manuell) prüft, ob EVO, Open-Meteo und OpenHolidays vom Runner aus erreichbar sind.
- In den Repo-Einstellungen muss unter Pages die Quelle „GitHub Actions“ gewählt sein.

Im Static-Modus liegen die API-Antworten als JSON-Dateien neben der `index.html` (`api/status.json`, `api/gyms.json`, `api/gym/<slug>.json`, `api/weekly/<slug>.json`). Der Button „Jetzt abrufen“ ist dort ausgeblendet.

## Lokal betreiben

```bash
python3 evo_visits.py probe     # prüfen, ob alle Clubs erkannt werden (Rohdaten unter data/probe/)
python3 evo_visits.py serve     # Sammler + Dashboard auf http://127.0.0.1:8765
python3 evo_visits.py demo      # Dashboard mit synthetischen Daten, Wetter und Ferien (klar als Demo markiert)
```

`serve` fragt alle 10 Minuten zwischen 05:30 und 23:30 Uhr ab und speichert in `data/visits.db`. Ein zweiter Abruf innerhalb von 10 Minuten wird übersprungen, auch über „Jetzt abrufen“. Wetter und Ferien frischt der Sammler nach jedem Abruf auf (gedrosselt auf stündlich bzw. wöchentlich).

Weitere Kommandos:

```bash
python3 evo_visits.py collect --respect-window   # einmal abrufen (für cron)
python3 evo_visits.py context                    # Wetter und Ferien sofort neu holen
python3 evo_visits.py export-csv                 # Monats-CSVs nach data/ schreiben
python3 evo_visits.py import-csv                 # Monats-CSVs aus data/ einlesen
python3 evo_visits.py export --out site          # statische Seite bauen
```

### Dauerbetrieb auf dem Mac (launchd)

```bash
python3 evo_visits.py launchd > ~/Library/LaunchAgents/local.evo-visits.plist
launchctl load ~/Library/LaunchAgents/local.evo-visits.plist
# stoppen: launchctl unload ~/Library/LaunchAgents/local.evo-visits.plist
```

Logs landen in `data/server.log`. Schläft der Laptop, entstehen Lücken, deshalb sammelt der GitHub-Workflow durchgehend. Lokale Daten lassen sich per `export-csv` in die Monats-CSVs übernehmen und committen.

## Wie die Prognose funktioniert

Bewusst nachvollziehbar statt Blackbox, alles in `evo/forecast.py`, `evo/effects.py` und `evo/model.py`.

1. **Wochenprofil.** Messwerte werden in 15-Minuten-Slots gemittelt. Für jeden Wochentag und Slot entsteht ein typischer Wert. Jüngere Wochen zählen mehr (Halbwertszeit 21 Tage), Nachbar-Slots fließen halb ein. Bei wenig Daten wird zum Profil aller Werktage bzw. Wochenendtage geschrumpft.
2. **Feiertage** zählen als Sonntag, beim Lernen und bei der Prognose.
3. **Effekte** wirken multiplikativ auf das Profil: Prognose = (Profil + 3) · e^(x·β) − 3. Geschätzt per Ridge-Regression (λ = 10) auf log((v + 3)/(s + 3)) mit den Merkmalen Regen (mm/h, gesättigt bei 1), Wärme über 18 °C und Kälte unter 8 °C (je 10 °C), Sonnenanteil der Stunde, Schulferientag und Brückentag. Ein Effekt gilt erst ab genug Tagen, an denen er auftritt (Wetter 4, Schulferien 3, Brückentage 2), vorher bleibt er 0.
4. **Backfitting.** Effekte schätzen, Messwerte um sie bereinigen, Profil neu bauen, zweimal. So beschreibt das Profil Normalbedingungen und nicht „die Woche, in der es oft geregnet hat“.
5. **Nowcast.** Ist der Club gerade voller oder leerer als vorhergesagt, wird die Abweichung fortgeschrieben und klingt mit zwei Stunden Zeitkonstante ab.
6. **Band (80 %).** Zusammengesetzt aus der Reststreuung je Stunde nach Abzug der Effekte, der Unsicherheit der Effekte selbst (Tages-Block-Bootstrap), einer Wetter-Unsicherheit, die mit der Vorlaufzeit bis 4 Tage wächst, und einem Zuschlag an Feiertagen, solange wenige davon in den Daten sind. An normalen Tagen wird das Band dadurch schmaler, bei unsicheren Lagen breiter.
7. **Backtest.** Die letzten 7 Tage werden mit dem jeweils vorher bekannten Wissen nachgerechnet: Modell mit Effekten, reines Wochenprofil und „gleiche Uhrzeit Vorwoche“. Helfen die Effekte nicht, werden sie abgeschaltet. Das Dashboard zeigt alle drei Fehler und die Trefferquote des Bands. Der Backtest nutzt gemessenes statt vorhergesagtes Wetter und ist damit etwas zu optimistisch.

Die Karte „Was die Auslastung beeinflusst“ zeigt je Effekt Wirkung, 80-%-Spanne, Datenbasis und Status sowie den Stand der Datenquellen.

## Beste Zeit fürs Gym

Einstellungen (im Browser gespeichert): Wochentage, Zeitfenster Mo–Fr (Standard 17–22 Uhr) und Sa/So (9–19 Uhr), Dauer 60/90/120 Minuten.

- **Nächste 7 Tage:** je Tag das Fenster mit der niedrigsten Prognose, mit Wetter und Feiertagen.
- **Generell:** aus der Historie (`/api/weekly`), Feiertage und Brückentage ausgenommen. Sortiert nach dem 75. Perzentil über die Wochen, also nach „verlässlich leer“ statt „im Schnitt leer“. Angezeigt werden typischer Wert (Median), „meist unter X“ (in 3 von 4 Wochen) und die Zahl der Wochen. Unter 4 Wochen gilt ein Vorschlag als vorläufig. Vorschläge überlappen sich nicht.
- Oben steht die eine perfekte Zeit der kommenden Woche.

## Konfiguration

Optionen mit Defaults stehen in `evo/config.py`, eigene Werte in `config.json` (Vorlage `config.example.json`): `interval_minutes` (10), `active_hours` („05:30-23:30“), `port` (8765), `mode` („auto“, „api“, „static“, „browser“), `history_weeks` (10), `half_life_days` (21). Kommandozeile geht vor: `--interval`, `--port`, `--mode`, `--active-hours`.

`mode` „auto“ nutzt zuerst die EVO-API und fällt sonst auf die alte Heuristik zurück (eingebettetes JSON, API-URLs in JS-Bundles, Text-Heuristik, optional Playwright mit `pip3 install playwright && python3 -m playwright install chromium`).

## Aufbau

| Datei | Inhalt |
|---|---|
| `evo/evo_api.py` | echte EVO-API (bevorzugt) |
| `evo/fetch.py`, `evo/extract.py` | Abruf-Reihenfolge, Heuristik als Fallback, `probe` |
| `evo/context.py` | Wetter- und Ferien-Cache |
| `evo/calendar_de.py` | Feiertage, Brückentage, Club → Stadt |
| `evo/forecast.py` | Wochenprofil |
| `evo/effects.py` | Effekte, Ridge, Backfitting, Bootstrap, Band |
| `evo/model.py` | Prognosemodell, Tagesübersicht, Backtest |
| `evo/server.py` | Webserver, API, Hintergrund-Sammler |
| `evo/static_export.py` | statische Seite für GitHub Pages |
| `evo/csvstore.py` | Monats-CSVs |
| `evo/demo.py` | Demo-Daten |
| `web/index.html` | Dashboard (eigene SVG-Charts, hell und dunkel) |

Tests: `python3 -m unittest discover -s tests -t .`. Echte, gekürzte Antworten von EVO, Open-Meteo und OpenHolidays liegen als Fixtures unter `tests/fixtures/`.

## Fair Use

Ein Abruf alle 10 bis 15 Minuten ist weniger Last als ein Mensch, der die Seite offen hat. Das Tool ist für den privaten Gebrauch. Bitte das Intervall nicht verkürzen.
