# EVO Visits Tracker

Schreibt die Live-Besucherzahlen von [visits.evofitness.de](https://visits.evofitness.de/) für alle EVO-Clubs in Deutschland mit, zeigt den Verlauf und sagt voraus, wie voll es in den nächsten Stunden und Tagen wird. Wetter, Feiertage, Brückentage und Schulferien fließen in die Prognose ein. Eine eigene Karte schlägt die beste Zeit fürs Training vor.

**Live:** https://evo011.github.io/evo-visits/ (Abruf zur vollen Viertelstunde von 5:45 bis 23:15 Uhr, also eine Viertelstunde vor Öffnung bis eine Viertelstunde nach Schließung; die Seite steht 1–2 Minuten später)

Nur Python 3.9+ mit Standardbibliothek, keine Pakete. Das Dashboard ist eine einzelne HTML-Datei ohne externe Abhängigkeiten. Die Schrift (Archivo, SIL Open Font License) liegt in `web/fonts/` und wird mit ausgeliefert, es gibt keine Anfragen an Google Fonts oder andere Dritte. Die Wetter-Icons stammen aus [Phosphor Icons](https://phosphoricons.com) (MIT) und sind als SVG-Sprite eingebettet.

Das Design folgt einer Anzeigetafel: Kreide und Tinte, Grün für „gute Zeit“, Rot für „voll“. Oben steht ein Satz statt einer Kennzahl-Kachel („10 Leute in Unterbilk. Eher ruhig …“). Darunter zeigt das Belegungsband den Tag von 6 bis 23 Uhr als Viertelstunden-Striche: gemessen in Tinte, Prognose in Grau, das beste Fenster in Grün, per Tab für die nächsten 7 Tage.

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

- `.github/workflows/collect.yml` setzt sich selbst fort: Der letzte Job jedes Laufs (`chain`) wartet bis zur nächsten vollen Viertelstunde (:00, :15, :30, :45) und startet den Workflow dann per `workflow_dispatch` neu. Das eingebaute `GITHUB_TOKEN` darf das, ein eigenes Token ist nicht nötig. Nachts schläft die Kette bis 05:45 Uhr (höchstens 5 Stunden am Stück, Grenze für Job-Laufzeiten). Ein nachts per Hand gestarteter Lauf wartet so lange; wer sofort deployen will, bricht den schlafenden Lauf ab (`gh run cancel`, notfalls `force-cancel` über die API). Laufen oder warten schon andere Läufe, setzt sie keinen neuen an, so entstehen keine doppelten Ketten.
- Der Cron `0,15,30,45 3-22 * * *` ist nur Reserve, falls die Kette abreißt. GitHubs Cron-Trigger hat am 12.10.2026 über eine Stunde lang nicht ausgelöst, deshalb die Kette. Cron rechnet in UTC, die Stunden 3–22 decken 5:45–23:15 Uhr Berliner Zeit in Sommer- und Winterzeit ab. Das genaue Fenster (05:45–23:15 Berlin) prüft das Skript selbst (`collect --respect-window`). Eine concurrency-Gruppe verhindert überlappende Läufe.
- Reißt die Kette ab und der Cron springt nicht an, genügt ein Start per Hand: `gh workflow run collect.yml -R evo011/evo-visits`.
- Ablauf je Lauf: Monats-CSVs einlesen, abrufen, Wetter und Ferien auffrischen, Monats-CSVs schreiben, statische Seite bauen (`export --out site`, beim ersten Lauf des Tages mit Prognose-Schnappschuss), CSVs committen und die Seite per `actions/deploy-pages` veröffentlichen.
- Die CSVs sind die Quelle der Wahrheit (siehe „Daten im Repo“). `data/visits.db` (SQLite) kommt nicht ins Git, sondern liegt im Actions-Cache, damit Wetter und Ferien nicht bei jedem Lauf neu geholt werden. Räumt GitHub den Cache, baut der nächste Lauf die Datenbank aus den CSVs wieder auf.
- Schlägt der EVO-Abruf fehl, wird die Seite trotzdem mit dem letzten Stand deployt und der Lauf rot markiert.
- `.github/workflows/probe.yml` (manuell) prüft, ob EVO, Open-Meteo und OpenHolidays vom Runner aus erreichbar sind.
- In den Repo-Einstellungen muss unter Pages die Quelle „GitHub Actions“ gewählt sein.

Im Static-Modus liegen die API-Antworten als JSON-Dateien neben der `index.html` (`api/status.json`, `api/gyms.json`, `api/gym/<slug>.json`, `api/weekly/<slug>.json`). Der Button „Jetzt abrufen“ ist dort ausgeblendet.

## Als App installieren

Die Seite ist eine installierbare Web-App: Auf Android/Chrome über das Menü „App installieren“ bzw. „Zum Startbildschirm hinzufügen“, auf dem iPhone über Safari → Teilen → „Zum Home-Bildschirm“, im Desktop-Chrome/Edge über das Installieren-Symbol in der Adresszeile. Ein kleiner Service Worker fragt immer zuerst das Netz nach dem frischen Stand und zeigt offline die zuletzt gesehene Seite. Neue Icons erzeugt man mit `python3 tools/make_icons.py`.

## Lokal betreiben

```bash
python3 evo_visits.py probe     # prüfen, ob alle Clubs erkannt werden (Rohdaten unter data/probe/)
python3 evo_visits.py serve     # Sammler + Dashboard auf http://127.0.0.1:8765
python3 evo_visits.py demo      # Dashboard mit synthetischen Daten, Wetter und Ferien (klar als Demo markiert)
```

`serve` fragt alle 10 Minuten zwischen 05:45 und 23:15 Uhr ab und speichert in `data/visits.db`. Ein zweiter Abruf innerhalb von 10 Minuten wird übersprungen, auch über „Jetzt abrufen“. Wetter und Ferien frischt der Sammler nach jedem Abruf auf (gedrosselt auf stündlich bzw. wöchentlich).

Weitere Kommandos:

```bash
python3 evo_visits.py collect --respect-window   # einmal abrufen (für cron)
python3 evo_visits.py context                    # Wetter und Ferien sofort neu holen
python3 evo_visits.py export-csv                 # Monats-CSVs (Besucher, Wetter) nach data/ schreiben
python3 evo_visits.py import-csv                 # Monats-CSVs aus data/ einlesen
python3 evo_visits.py export --out site          # statische Seite bauen
python3 evo_visits.py export --out site --forecast-snapshot data/forecasts   # dazu Prognose-Schnappschuss (einmal pro Tag)
python3 evo_visits.py evaluate                   # echte Prognosegüte aus den Schnappschüssen
```

## Daten im Repo

Alles, was für spätere Auswertungen zählt, liegt als Monats-CSV in `data/` und wird von jedem Lauf mitcommittet. Nichts davon wird gelöscht.

| Datei | Inhalt | Umfang |
|---|---|---|
| `data/visits-YYYY-MM.csv` | jeder Abruf: `ts,local_time,gym,count,capacity,pct` | 15 Clubs alle 15 min, etwa 32.000 Zeilen im Monat |
| `data/weather-YYYY-MM.csv` | gemessenes Wetter je Stadt und Stunde: `ts,local_time,loc,temp,precip,sunshine,code` (nur vergangene Stunden) | 7 Städte, etwa 5.000 Zeilen im Monat |
| `data/forecasts/forecast-YYYY-MM.csv` | Schnappschuss der eigenen 7-Tage-Prognose, einmal pro Tag: `made_date,made_ts,gym,target_ts,target_local,lead_h,est,lo,hi,effect_pct,temp,precip,sun_min,code,model` | stündlich 6–23 Uhr je Club, etwa 1.900 Zeilen am Tag |

Open-Meteo liefert nur etwa 74 Tage Wetter zurück, deshalb wird das Wetter selbst archiviert. Die Schnappschüsse halten fest, was die Seite zum jeweiligen Zeitpunkt vorhergesagt hat, inklusive des vorhergesagten Wetters und der Modellversion (`model` = Commit). Damit lässt sich die echte Prognosegüte je Vorlaufzeit messen und lassen sich spätere Modellversionen fair vergleichen.

### Dauerbetrieb auf dem Mac (launchd)

```bash
python3 evo_visits.py launchd > ~/Library/LaunchAgents/local.evo-visits.plist
launchctl load ~/Library/LaunchAgents/local.evo-visits.plist
# stoppen: launchctl unload ~/Library/LaunchAgents/local.evo-visits.plist
```

Logs landen in `data/server.log`. Schläft der Laptop, entstehen Lücken, deshalb sammelt der GitHub-Workflow durchgehend. Lokale Daten lassen sich per `export-csv` in die Monats-CSVs übernehmen und committen.

## Wie die Prognose funktioniert

Bewusst nachvollziehbar statt Blackbox, alles in `evo/forecast.py`, `evo/pool.py`, `evo/effects.py` und `evo/model.py`.

1. **Wochenprofil.** Messwerte werden in 15-Minuten-Slots gemittelt. Für jeden Wochentag und Slot entsteht ein typischer Wert. Jüngere Wochen zählen mehr (Halbwertszeit 21 Tage), Nachbar-Slots fließen halb ein.
2. **Alle Clubs lernen gemeinsam.** Jeder Club ist sein eigenes Niveau mal einer gemeinsamen Tagesform („Unterbilk ist etwa 1,3-mal so voll wie der Schnitt“). Niveaus und Form werden abwechselnd aus allen Clubs geschätzt (`evo/pool.py`). Das eigene Profil eines Clubs wird zur gemeinsamen Form mal seinem Niveau hin geschrumpft, die gemeinsame Form zählt dabei wie zwei eigene Beobachtungen. Mit wenigen Wochen Daten prägt sie das Profil stark, später setzen sich die Eigenheiten des Clubs durch.
3. **Feiertage** zählen als Sonntag, beim Lernen und bei der Prognose, je Bundesland des Clubs.
4. **Effekte** wirken multiplikativ auf das Profil: Prognose = (Profil + 3) · e^(x·β) − 3. Geschätzt per Ridge-Regression (λ = 10) auf log((v + 3)/(s + 3)) mit den Merkmalen Regen (mm/h, gesättigt bei 1), Wärme über 18 °C und Kälte unter 8 °C (je 10 °C), Sonnenanteil der Stunde, Schulferientag und Brückentag. Die Effekte werden einmal gemeinsam über alle Clubs geschätzt, jeder Club gegen sein eigenes Profil. Ein Effekt gilt erst ab genug Tagen, an denen er auftritt (Wetter 4, Schulferien 3, Brückentage 2), gezählt je Stadt und Datum: Regen in Hamburg und Köln am selben Tag sind zwei Tage. Vorher bleibt er 0.
5. **Backfitting.** Effekte schätzen, Messwerte um sie bereinigen, Profile neu bauen, zweimal. So beschreibt das Profil Normalbedingungen und nicht „die Woche, in der es oft geregnet hat“.
6. **Nowcast.** Ist der Club gerade voller oder leerer als vorhergesagt, wird die Abweichung fortgeschrieben und klingt mit zwei Stunden Zeitkonstante ab.
7. **Band (80 %).** Zusammengesetzt aus der Reststreuung je Stunde nach Abzug der Effekte, der Unsicherheit der Effekte selbst (Tages-Block-Bootstrap), einer Wetter-Unsicherheit, die mit der Vorlaufzeit bis 4 Tage wächst, und einem Zuschlag an Feiertagen, solange wenige davon in den Daten sind. An normalen Tagen wird das Band dadurch schmaler, bei unsicheren Lagen breiter.
8. **Backtest.** Die letzten 7 Tage werden mit dem jeweils vorher bekannten Wissen nachgerechnet (ab 7 Tagen Vorlauf je Club): gemeinsames Modell mit Effekten, gemeinsames Modell ohne Effekte („nur Wochenprofil“), das frühere Einzelmodell nur aus den Daten des Clubs („nur eigene Daten“) und „gleiche Uhrzeit Vorwoche“. Helfen die Effekte über alle Clubs zusammen nicht, werden sie abgeschaltet. Das Dashboard zeigt alle Fehler und die Trefferquote des Bands. Der Backtest nutzt gemessenes statt vorhergesagtes Wetter und ist damit etwas zu optimistisch.
9. **Echte Prognosegüte.** `evo/evaluate.py` vergleicht die täglichen Schnappschüsse in `data/forecasts/` mit den später gemessenen Werten: mittlerer Fehler, Verzerrung und Trefferquote des Bands je Vorlauf (am selben Tag, 1 Tag, 2–3 Tage, 4–7 Tage vorher), dazu „wie Vorwoche“ auf denselben Zeitpunkten. Getrennt nach Club und nach Modellversion (`model` = Commit), so lässt sich jede Änderung am Modell an echten Prognosen messen. Auf der Seite steht das Ergebnis in der Karte „Was die Auslastung beeinflusst“, auf der Kommandozeile per `python3 evo_visits.py evaluate`, statisch als `api/accuracy.json`.

Die Karte „Was die Auslastung beeinflusst“ zeigt je Effekt Wirkung, 80-%-Spanne, Datenbasis und Status sowie den Stand der Datenquellen.

## Beste Zeit fürs Gym

Einstellungen (im Browser gespeichert): Wochentage, Zeitfenster Mo–Fr (Standard 17–22 Uhr) und Sa/So (9–19 Uhr), Dauer 60/90/120 Minuten.

- **Nächste 7 Tage:** je Tag das Fenster mit der niedrigsten Prognose, mit Wetter und Feiertagen.
- **Generell:** aus der Historie (`/api/weekly`), Feiertage und Brückentage ausgenommen. Sortiert nach dem 75. Perzentil über die Wochen, also nach „verlässlich leer“ statt „im Schnitt leer“. Angezeigt werden typischer Wert (Median), „meist unter X“ (in 3 von 4 Wochen) und die Zahl der Wochen. Unter 4 Wochen gilt ein Vorschlag als vorläufig. Vorschläge überlappen sich nicht.
- Oben steht die eine perfekte Zeit der kommenden Woche.

## Konfiguration

Optionen mit Defaults stehen in `evo/config.py`, eigene Werte in `config.json` (Vorlage `config.example.json`): `interval_minutes` (10), `active_hours` („05:45-23:15“), `port` (8765), `mode` („auto“, „api“, „static“, „browser“), `history_weeks` (10), `half_life_days` (21). Kommandozeile geht vor: `--interval`, `--port`, `--mode`, `--active-hours`.

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
| `evo/csvstore.py` | Monats-CSVs für Besucher und Wetter |
| `evo/snapshot.py` | täglicher Prognose-Schnappschuss |
| `evo/pool.py` | gemeinsame Tagesform und Niveaus aller Clubs |
| `evo/evaluate.py` | echte Prognosegüte aus den Schnappschüssen |
| `evo/demo.py` | Demo-Daten |
| `web/index.html` | Dashboard (eigene SVG-Charts, hell und dunkel) |

Tests: `python3 -m unittest discover -s tests -t .`. Echte, gekürzte Antworten von EVO, Open-Meteo und OpenHolidays liegen als Fixtures unter `tests/fixtures/`.

## Fair Use

Ein Abruf alle 10 bis 15 Minuten ist weniger Last als ein Mensch, der die Seite offen hat. Das Tool ist für den privaten Gebrauch. Bitte das Intervall nicht verkürzen.
