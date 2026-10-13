# Design-Prompt „Anzeigetafel“

Wiederverwendbarer Prompt für Daten-Dashboards im Stil von evo011.github.io/evo-visits. Platzhalter in [eckigen Klammern] je Projekt füllen.

```text
Gestalte [PRODUKT], ein Daten-Dashboard für [WER] in der Situation [WANN/WO, z. B. "abends mit dem Handy, kurz vor dem Losgehen"].
Die Seite beantwortet in drei Sekunden eine Frage: [KERNFRAGE, z. B. "Soll ich jetzt los, und wenn nicht, wann?"].

HALTUNG
Die Seite ist ein Werkzeug, kein Marketing. Sie soll sich anfühlen wie eine Abfahrtstafel am Bahnhof: sofort lesbar, sachlich, ruhig, präzise. Standard-Bedienelemente (Buttons, Tabs, Selects, Tabellen), keine erfundenen Controls. Die "Welt" des Produkts liefert nur Schrift, Farbe, Dichte und genau ein Signatur-Element.

AUFBAU (von oben nach unten)
1. Kopfzeile, sticky: kleines Logo + Produktname links (15 px, 650), rechts "Stand HH:MM" in gedämpfter Farbe und ein Text-Link. Darunter die Hauptauswahl als Segment-Control (aktiv: Tinte auf Kreide invertiert). Keine Status-Pillen, keine pulsierenden Punkte.
2. Aufmacher als Satz statt Kennzahl-Kachel. Zeile 1 (32 px, mobil 26 px, 650, letter-spacing -0.02em): die Hauptzahl mitten im Satz, z. B. "**22** Leute in Unterbilk." (Zahl 800, font-stretch 87.5 %). Zeile 2 (18 px, sekundär): Einordnung in Alltagssprache ("Ruhiger als sonst am Montagabend, üblich sind ~31."), das wertende Wort in der Statusfarbe. Zeile 3: die Handlungsempfehlung ("Beste Zeit heute: 21:30–22:30, etwa 9 Leute."). Darunter eine Meta-Zeile 13 px (Stand, Datenbasis).
3. Signatur-Element: [SIGNATUR, z. B. "Belegungsband: der Tag als 68 Viertelstunden-Striche wie auf einer Zähltafel"]. Gemessenes in Tinte, Prognose in Stahlgrau, die Empfehlung in Grün mit einer dünnen Klammer und Label darüber, eine "jetzt"-Marke. Tabs darüber wechseln den Zeitraum. Ablesen per Zeiger/Tippen als Textzeile über dem Element, kein schwebender Kasten. Darunter eine Kontextzeile mit Icon + Fakten, Trenner nur Kommas.
4. Empfehlungsliste im Rhythmus einer Abfahrtstafel: Zeit zuerst (18 px, 650, schmal gesetzt), dann Tag, Wert rechtsbündig mit Spanne klein darunter, Icon. Zeilen nur durch Abstand und dezentes Zebra getrennt, keine Linien zwischen Zeilen. Die beste Zeile: Zeit in Grün plus kleines Wort "beste". Einstellungen dazu in ein eingeklapptes <details> mit Live-Zusammenfassung als <summary>.
5. Analyse darunter (Charts, Heatmap, Tabellen), ganz unten Details in <details> (Methodik, Datenquellen, Protokoll), dann eine schlichte Fußzeile mit Quellen und Lizenzen.

FARBEN (CSS-Variablen, sonst keine Farben)
Hell (Default):  --chalk:#f4f4f1  --chalk-2:#e9e9e4  --ink:#17181a  --ink-2:#45484d  --ink-3:#6b6f75  --steel:#cfd1d2  --good:#2d8a4a  --bad:#cc3524
Dunkel:          --chalk:#151617  --chalk-2:#1f2022  --ink:#edece7  --ink-2:#b5b6b2  --ink-3:#8f918d  --steel:#34363a  --good:#55b574  --bad:#ee5a46
Grün heißt "gute Zeit / Empfehlung", Rot heißt "kritisch / voll". Sonst ist alles Tinte auf Kreide. Kein weiterer Akzent, keine Verläufe, keine Glows. Heatmaps: ein Ton, nur Helligkeit (chalk-2 → ink). Dunkelmodus über prefers-color-scheme plus data-theme.

SCHRIFT
Eine einzige Familie für alles: Archivo (variabel, wght 100–900, wdth 62–125 %), selbst gehostet per @font-face mit font-display: swap und Preload. Kein Google-Fonts-Link.
Feste Skala in px (Verhältnis ~1.2): 13 / 15 / 18 / 22 / 26 / 32 / 44. Body 15 px, Zeilenhöhe 1.5. Überschriften 22 px, 650, letter-spacing -0.01em, Satzschreibung. Labels 13 px, 500, sekundär.
Alle Zahlen tabular-nums; Zeiten und Zahlen in Listen zusätzlich font-stretch 87.5 %.
Keine Versalien, keine Eyebrows/Kicker über Überschriften, keine Abschnittsnummern.

LAYOUT UND FLÄCHEN
max-width 880 px, mittig, Innenabstand 20 px (mobil 16 px). Abschnitte ohne Karten: kein Rahmen, kein Hintergrund. Trennung nur durch Abstand (40 px, mobil 32 px) plus eine 1-px-Linie in --steel über jeder Abschnittsüberschrift.
Eine Radius-Regel: Bedienelemente 8 px, sonst nichts gerundet. Keine Schatten (außer Ablese-Overlays, falls nötig).
Icons aus einer echten Bibliothek (z. B. Phosphor, regular, 16 px, currentColor) als SVG-Sprite. Keine Emoji als Icons.
Logo: abgerundetes Quadrat in --ink mit Strichen in --chalk und einem Strich in --good, über CSS-Variablen gefärbt (damit es im Dunkelmodus sichtbar bleibt).

BEWEGUNG
Keine Lade-Choreografie, keine Einblend-Animationen, kein Pulsieren. Nur Zustandswechsel: transform/opacity, 140–220 ms, cubic-bezier(0.23, 1, 0.32, 1). Druck-Feedback auf allen Buttons: :active scale(0.97). Balken ändern ihre Höhe per transform: scaleY mit transform-origin bottom, nie per height. prefers-reduced-motion: Bewegung aus.

MOBIL (375 px ist das Hauptziel)
viewport-fit=cover, Safe-Area-Abstände an Kopfzeile und unten. -webkit-tap-highlight-color: transparent. Inputs/Selects 16 px bei pointer: coarse (kein Zoom beim Antippen). touch-action: manipulation und user-select: none nur auf Bedienelementen. Alle :hover-Stile in @media (hover: hover) and (pointer: fine). Tabellen werden unter 560 px zu Grid-Zeilen (drei Spalten, Nebeninfos zweizeilig in die Zellen), nie horizontales Scrollen der Seite. Tab-Leisten dürfen in sich scrollen. Touch-Ziele mindestens 40 px.

BROWSER-DETAILS
::selection in --good mit 28 % Deckkraft, caret-color --ink, Fokus-Ring 2 px --ink mit 2 px Abstand, scrollbar-color --steel, Link-Unterstreichung mit 3 px Abstand. theme-color je Farbschema. Live-Tab-Titel mit der Hauptzahl, z. B. "22 · Unterbilk – Produktname". Eigenes Favicon.

TEXTE
Deutsch, echte Umlaute, Alltagssprache, aktiv. Leere Zustände erklären, wann es Daten gibt ("Eine Prognose gibt es nach dem ersten vollen Messtag."). Mittelpunkt als Trenner höchstens einmal pro Zeile, sonst Kommas. Keine Gedankenstriche als Stilmittel.

NICHT MACHEN
Hero-Kachel mit großer Zahl + kleinem Label + drei gleichen Kacheln daneben. Karten mit Rahmen/Schatten als Grundcontainer. Farbiger linker Rand an Boxen. Neon auf Dunkelgrau, Korn, Lichtkegel, Deko-Raster. Pillen/Badges für jede Kleinigkeit. Mehrere Schriftfamilien, schmale "Sport"-Display-Schrift als Kostüm, Monospace als Deko. Emoji-Icons. Gestaffelte Einblend-Animationen.

PRÜFEN, BEVOR DU FERTIG BIST
Hell, dunkel und 375 px Breite im Browser ansehen. Keine horizontale Scrollbar, keine Konsolenfehler, Kontrast Text ≥ 4,5:1. Mit echten, unordentlichen Daten testen (lange Namen, 0, fehlende Werte), nicht nur mit hübschen Demo-Daten.
```
