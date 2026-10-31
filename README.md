# n2k-viewer

NMEA2000-Viewer für Windows mit dem **Seeed USB-CAN Analyzer** (CH340, serielles 2-Mbaud-Protokoll).
Zeigt alle Nachrichten eines NMEA2000-Busses als PGN-Übersicht (mit Frequenz und dekodierten Werten) und als Trace.

## Start

```
pip install pyserial
python n2k_viewer.py
```

COM-Port wählen, Bitrate 250 kbit/s (NMEA2000-Standard), Modus „Nur Lauschen“ (sendet nichts auf den Bus), „Verbinden“.

## Funktionen

- **PGN-Übersicht:** pro Quelladresse und PGN eine Zeile mit Anzahl, Hz, Rohdaten und dekodiertem Text.
  Zeile anklicken → unten alle Felder einzeln, live aktualisiert.
- **Trace:** alle Frames mit Zeitstempel, CAN-ID, Priorität, PGN, SA, DA.
- **Fast-Packet** wird zusammengesetzt (z. B. 129029, 126996).
- **Log-Export** als CSV.

## Dateien

| Datei | Zweck |
|---|---|
| `n2k_viewer.py` | GUI (Tkinter), Seeed-Protokoll, Fast-Packet-Zusammensetzung |
| `n2k_pgns.py` | PGN-Definitionen und Decoder (ca. 60 PGNs), Plugin-Loader |
| `pgn_plugins/` | eigene / proprietäre PGNs als Plugins |
| `i18n.py`, `locales/` | Mehrsprachigkeit (`locales/<code>.json` je Sprache) |

## Eigene (proprietäre) PGNs hinzufügen

`pgn_plugins/_example_proprietary.py` unter einem Namen ohne führenden Unterstrich kopieren
(z. B. `meine_firma.py`) und anpassen. Beim Start bzw. über „Plugins neu laden“ wird sie geladen.

```python
from n2k_pgns import register, F, MANUFACTURERS
MANUFACTURERS[999] = "Meine Firma"

register(65350, "Meine Firma: Sensorwerte", manufacturer=999, fields=[
    F("Druck",      16, 16, scale=0.1,  unit="bar"),
    F("Temperatur", 32, 16, scale=0.01, offset=-273.15, unit="°C"),
    F("Status",     48, 8,  kind="enum", enum={0: "OK", 1: "Warnung"}),
])
```

Der Hersteller-Header (Bit 0–15) wird automatisch dekodiert, Nutzdaten beginnen bei Bit 16.
Wiederholte Gruppen: `Repeat`; Sonderfälle: `@decoder`. Details im Kopf von `n2k_pgns.py`.

## Fehleranzeige

Unter der Werkzeugleiste zeigen farbige Zähler (grün = keine Fehler, gelb/orange = frühere Fehler, rot = Fehler in den
letzten 10 s). Der Tab „Fehlerliste“ listet jedes Ereignis mit Zeit, Quelle, PGN und Details (höchstens 20 Einträge pro
Sekunde, die Zähler bleiben exakt). „Fehler zurücksetzen“ setzt alles zurück; beim Verbinden wird automatisch zurückgesetzt.

| Zähler | Quelle | Bedeutung |
|---|---|---|
| **CAN-Controller** | Adapter (Statusabfrage, 4×/s) | Empfangs- (REC) und Sendefehlerzähler (TEC) des CAN-Controllers, dazu Maximum seit dem Verbinden. Ab 96 orange, ab 128 rot (Error-Passive). |
| **Fast-Packet** | Viewer | verlorener/vertauschter Folgeframe, falsche Sequenznummer, neue Nachricht vor Ende der alten, ungültige Länge. Eine gestörte Nachricht zählt nur einmal. |
| **Serielle Daten** | Viewer | Synchronisationsverlust im Datenstrom des Adapters (Frame ohne Endbyte, verworfene Bytes). |
| **Frame-Format** | Viewer | 11-Bit-Frames auf dem Bus (NMEA2000 verwendet nur 29-Bit) – oft falsche Bitrate oder falscher Bus. |
| **ISO-NAK** | Bus | Geräte antworten auf eine Anfrage mit NAK / Zugriff verweigert / Adresse belegt (kann auch normal sein, z. B. nicht unterstütztes PGN – nur gelb). |

Grenzen:

- Die Controller-Zähler sind **Momentanwerte**, keine Summen: der Empfangszähler sinkt mit jedem fehlerfreien Frame wieder
  auf 0. Einzelne Fehler auf einem ausgelasteten Bus sind deshalb meist nicht sichtbar, nur anhaltende Probleme
  (z. B. Terminierung, falsche Bitrate, defekte Leitung). Die Viewer-seitigen Zähler sind für kurze Störungen aussagekräftiger.
- Bus-Off lässt sich nicht erkennen (der Zähler ist 8 Bit breit). Das Protokoll der Statusabfrage ist nur teilweise
  dokumentiert; meldet der Adapter nichts, steht „Adapter meldet keine Zähler“. Mit `"poll_status": false` in
  `n2k-viewer.json` lässt sich die Abfrage abschalten (`"status_interval"` = Abstand in Sekunden, Standard 0.25).
- In den ersten 2 s nach dem Verbinden werden angeschnittene Nachrichten nicht als Fehler gezählt.

## Sprachen / Languages

Oberfläche **und** dekodierte Feldnamen/Werte gibt es auf Deutsch und Englisch. Die Sprache wählst du oben rechts
im Viewer (Umschaltung sofort, wird gespeichert; erster Start: Systemsprache, sonst Englisch). Gespeichert werden
auch Port, Bitrate und Modus in `%APPDATA%\n2k-viewer.json` (bzw. `~/n2k-viewer.json`).

**Weitere Sprache hinzufügen** – ohne Programmierung:

1. `locales/en.json` nach `locales/fr.json` kopieren (Dateiname = Sprachcode).
2. `"__name__"` (Anzeigename, z. B. `"Français"`) und die Werte rechts übersetzen. Links steht immer der deutsche
   Quelltext, nicht ändern. Fehlende Einträge erscheinen automatisch auf Deutsch.
3. Viewer neu starten – die Sprache erscheint in der Auswahl. `python i18n.py fr` listet noch fehlende Texte.

Plugins können eigene Texte mit `add_translations("en", {...})` übersetzen (Beispiel in `pgn_plugins/_example_proprietary.py`).

## Hinweise

- Die Bitlayouts der Standard-PGNs sind nach bestem Wissen umgesetzt, aber nicht gegen die offizielle
  Spezifikation geprüft. Bei Abweichungen bitte ein Issue mit einem Mitschnitt anlegen.
- Der Seeed-Analyzer ist nicht galvanisch getrennt; Bus korrekt terminieren (CAN_H, CAN_L, GND).
- „Nur Lauschen“ sendet den Moduscode 0x02 (Silent) laut Seeed-/Copperhill-Protokoll; die Waveshare-Dokumentation
  nennt für Silent 0x01. Kommen im Lauschmodus keine Frames an, „Normal“ probieren (der Viewer sendet auch dort nichts).

## Lizenz

MIT – siehe [LICENSE](LICENSE).
