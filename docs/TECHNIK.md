# TriLola – Technik-Referenz

<img src="images/lola-icon.png" width="64" align="right" alt="Lola">

Nachschlagewerk für Entwickler und Neugierige: Architektur, MQTT-Topics, Dateiformate,
Kommandozeile und Details der Rollout-Werkzeuge. Für Einrichtung und Bedienung siehe die
[README](../README.md) und die [Bedienungsanleitung](BEDIENUNG.md); die Mathematik steht in
[Modell & Parameter](MODELL.md).

---

## Architektur

```
Halsband ──BLE──▶ Sensoren (ESP32 / Pi / Shelly) ──MQTT──▶ Tracker (bt_tracker) ──MQTT──▶ Home Assistant
                     ▲        │ Mesh: Sensoren hören sich gegenseitig          ▲
                     └────────┘ (Drift- und Qualitätsüberwachung)              │ MQTT (Grundriss, Positionen,
                                                                               │ Kartenbezug, Live-Daten)
PC: Rollout-Oberfläche (deploy/bluecat_gui.py) ─── SSH / Shelly-RPC / USB+OTA ─┴──▶ alle Geräte
```

* **Sensoren** sammeln Sichtungen des Halsbands in 2-s-Fenstern und senden
  Median + Anzahl (Protokoll v2). Sieht ein Sensor das Halsband 10 s nicht,
  meldet er „nicht gesehen“ – auch direkt nach dem Start.
* **Tracker** (`trilola_tracker.py` → `tracker_app.py`) verarbeitet jede
  Nachricht genau einmal, schätzt Position/Raum/Bewegung und publiziert an HA.
* **Zwei Modelle**, in HA umschaltbar („TriLola Modell“):
  * `pf` (Standard): RSSI-Partikelfilter mit Wänden und Räumen aus dem Grundriss.
  * `legacy`: bisheriges Modell (Distanzen → Locator → Partikelfilter → IMM), korrigiert.

---

## 0. Rollout – alle Knoten aus einer Datei

Alle Pis, Shellys und ESP32 werden aus **`deploy/fleet.toml`** konfiguriert und
vom PC aus ausgerollt. Kein Knoten braucht mehr eine eigene Secrets-Datei, ein
eigenes Skript oder eine eigene Firmware.

| Knoten | Wie | Was passiert |
|---|---|---|
| Raspberry Pi | SSH | venv, Sensor + ggf. Tracker nach `~/bluecat`, systemd-Dienste `bluecat-sensor` / `trilola`, BlueZ `-E`, alte Dienste abgelöst (Rollback möglich) |
| Shelly (Gen2+) | HTTP-RPC | ein generisches Skript `bluecat_shelly.js`, Sensor-ID/Name per KVS, BLE/MQTT/Eco-Mode geprüft |
| ESP32 | 1× USB, danach WLAN (OTA) | eine Firmware für alle; die Sensor-ID kommt per MQTT (`bluecat/provision/<ble-mac>`) und wird im NVS gespeichert |

### Rollout-Oberfläche (empfohlen)

**Doppelklick auf `rollout.bat`** (Linux/macOS: `./rollout.sh`). Beim ersten Start wird
eine eigene Python-Umgebung unter `deploy/.venv` angelegt; danach öffnet sich die
Oberfläche im Browser. Sie läuft nur auf diesem PC (`127.0.0.1`, mit Zugangsschlüssel im Link).

| Bereich | Was geht |
|---|---|
| **Geräte** | Pis, ESP32 und Shellys anlegen, bearbeiten, entfernen; Live-Status (online, IP, Version, sieht Lola); je Gerät Installieren/Aktualisieren; „Neue Geräte gefunden“ für Shellys im Netz und neu geflashte ESP32 (übernehmen, einem vorhandenen Eintrag zuordnen oder ausblenden) |
| **Karte** | Kartenbezug über ein frei wählbares Referenzgerät, Grundriss zeichnen, Geräte relativ zur Referenz platzieren, Livemodus (Abschnitt 4) |
| **Raspberry Pi** | SSH-Zugang einrichten (einmal Passwort), alte Einstellungen übernehmen (MQTT-Zugang, Ursprung), Installieren/Aktualisieren mit sudo-Passwort (nur im Speicher), Rollback |
| **ESP32** | einmal per USB flashen (Port-Auswahl, BLE-MAC wird automatisch erkannt und eingetragen), danach per WLAN |
| **Einstellungen** | MQTT-Broker (mit Verbindungstest), WLAN, OTA-Passwort (Generator), Shelly-Zugang, Tracker |
| **Wartung** | Home Assistant aufräumen (mit Vorschau), PlatformIO installieren, Konfiguration prüfen, ausgeblendete Geräte wieder anzeigen |
| **Protokoll** | Ausgabe aller Aktionen live |

Erste Inbetriebnahme mit der Oberfläche:

1. **Einstellungen**: MQTT prüfen, WLAN-Name/-Passwort eintragen, OTA-Passwort erzeugen.
2. **Pis**: ⋯ → *SSH-Zugang einrichten*, dann ⋯ → *Alte Einstellungen übernehmen*, dann *Installieren*.
3. **Shellys**: *Einrichten* (bzw. *Im Netz suchen* für neue).
4. **ESP32**: *Wartung → ESP32-Werkzeug installieren* (einmalig), dann je ESP *Per USB flashen*.
   Ab dann reicht **Alle aktualisieren** oben rechts.
5. **Wartung → Home Assistant aufräumen**.
6. **Karte**: Kartenbezug setzen, Grundriss zeichnen, Geräte platzieren (Abschnitt 4).

Hinweis ESP32-OTA: Beim Update per WLAN verbindet sich der ESP32 zurück zum PC –
die Windows-Firewall fragt beim ersten Mal, ob Python Verbindungen annehmen darf (Privates Netzwerk: erlauben).

### Kommandozeile (gleiche Funktionen)

```bash
pip install -r deploy/requirements-gui.txt   # paho-mqtt, paramiko, pyserial (tomli bei Python < 3.11)

python deploy/bluecat_deploy.py check
python deploy/bluecat_deploy.py ssh-setup all       # SSH-Schlüssel auf die Pis, fragt je 1× das Passwort
python deploy/bluecat_deploy.py import-old all      # zeigt Einstellungen der alten Installation (nur lesen)
python deploy/bluecat_deploy.py esp usb arnd_esp --port COM5   # je ESP32 einmal; BLE-MAC wird eingetragen
python deploy/bluecat_deploy.py all --fix-settings  # Pis + Shellys + ESP32 (OTA)
python deploy/bluecat_deploy.py ha-cleanup --dry-run
python deploy/bluecat_deploy.py status
python deploy/bluecat_deploy.py pi ron --rollback   # zurück zu den alten Diensten
python deploy/bluecat_deploy.py remove shelly_kueche   # abmelden, stilllegen, aus fleet.toml löschen
```

**Neuer Knoten:** in der Oberfläche anlegen (oder Eintrag in `fleet.toml`), dann einrichten.
Ein Shelly braucht nur seine WLAN-MAC (findet die Suche), ein ESP32 gar nichts – die BLE-MAC
wird beim USB-Flash erkannt. Entfernte ESP32 gehen in den Standby (keine Messungen, OTA bleibt).

`deploy/fleet.toml` enthält Passwörter und ist in `.gitignore` eingetragen;
`fleet.example.toml` ist die Vorlage. Ebenfalls lokal und nicht im Git: `deploy/.venv`
(Python-Umgebung), `deploy/.gui_token` (Zugangsschlüssel der Oberfläche; bleibt über
Neustarts gleich), `deploy/.cache.json` (gefundene Shelly-IPs, ausgeblendete Geräte) und
`deploy/.tile_cache` (Kartenkacheln).

Sicherheit: Die Oberfläche lauscht nur auf `127.0.0.1`, jede Anfrage braucht den
Zugangsschlüssel aus dem Link, sudo-Passwörter bleiben nur im Speicher.

---

## 0a. Als App in Home Assistant (früher „Add-on“)

Dieselbe Oberfläche läuft als App auf **Home Assistant OS** – in der HA-Seitenleiste, mit
HA-Anmeldung, Daten in den HA-Backups. Der PC wird danach nur noch zum Aktualisieren der App
gebraucht.

**Was wo läuft:** Oberfläche und Rollout in der App (im Leerlauf fast keine Last). Der
**Tracker** bleibt auf seinem Pi (z. B. Ron) und rechnet dort; er kann auf Wunsch auch in die
App umziehen (*Einstellungen → Tracker → Läuft auf*, samt Konfiguration, gelernter Werte und
Kalibrierung – und genauso zurück). ESP32-Firmware für Updates per WLAN baut die App selbst
(beim ersten Mal ca. 1 GB Werkzeuge, nicht in den Backups); zum ersten USB-Flash eines neuen
ESP32 steckt man ihn an den HA-Rechner.

**Installieren (einmalig):**

1. In Home Assistant die App **„Terminal & SSH“** (oder „Advanced SSH & Web Terminal“)
   installieren, Passwort oder Schlüssel eintragen, starten.
2. Am PC `rollout.bat` → **Wartung → Als App auf Home Assistant**: IP von HA, SSH-Port,
   Benutzer, Passwort; Häkchen **„Daten übernehmen“** → *App installieren / aktualisieren*.
   Kopiert die App nach `/addons/trilola`, `fleet.toml`, Grundriss-Bild und Cache nach
   `addon_configs/local_trilola`, legt einen eigenen SSH-Schlüssel der App an und trägt ihn
   auf den Pis ein; dann baut HA die App (5–15 Minuten) und startet sie.
3. In HA: Einstellungen → Apps → TriLola → „In der Seitenleiste anzeigen“.

**Aktualisieren:** Repo am PC aktualisieren, dann wie oben – ohne „Daten übernehmen“.

**Ohne SSH-App:** `python deploy/bluecat_deploy.py ha-addon --export C:\temp\app` legt den
Ordner `trilola` an; ihn in die Samba-Freigabe `addons` kopieren, `fleet.toml` nach
`addon_configs/local_trilola/`, dann App-Store → ⋮ → „Nach Updates suchen“ → „Lokale Apps“ →
TriLola. Den SSH-Zugang zu den Pis richtet man dann in der App ein (Pi → ⋯ → SSH-Zugang).

| Pfad in HA | Inhalt |
|---|---|
| `addon_configs/local_trilola/fleet.toml` | alle Geräte und Zugangsdaten |
| `addon_configs/local_trilola/plan/` | Grundriss-Bild, Kalibrierplan |
| `addon_configs/local_trilola/tracker/` | nur wenn der Tracker in der App läuft |
| `addon_configs/local_trilola/ssh/` | Schlüssel der App für die Pis |

Technik: Ingress (nur der HA-Supervisor darf zugreifen), Host-Netz (ESP32-OTA verbindet sich
zurück; Shelly-Suche), USB/seriell für den ESP32-Erstflash, Debian-Basis (PlatformIO braucht glibc).

**App am PC testen (Home-Assistant-Devcontainer):** Voraussetzungen Docker (Docker Desktop oder Docker Engine in
WSL/Ubuntu – dann in VS Code die Einstellung „Dev › Containers: Execute In WSL“ anhaken) und VS Code mit
der Erweiterung „Dev Containers“. Einmalig zwei Dateien kopieren: `deploy/devcontainer/devcontainer.json`
→ `.devcontainer.json` im Repo-Hauptordner und `deploy/devcontainer/tasks.json` → `.vscode/tasks.json`.
Repo in VS Code öffnen → „Reopen in Container“ (offizielles Image von Home Assistant mit
Supervisor). Dann
*Terminal → Run Task*: **„1. Home Assistant starten“**, danach **„2. TriLola: bauen und
starten“** (legt den Code nach `deploy/ha_addon/app`, baut die App und zeigt ihr Protokoll).
Home Assistant läuft im Container auf Port 80 (neue Installationen ab HA 2026.8; Port 8123 leitet bis zum Onboarding nur auf Port 80 um). VS Code leitet ihn weiter – die Adresse steht im Reiter „Ports“ in der Zeile „Home Assistant (80)“, meist <http://localhost>; beim ersten Mal ein Test-Konto anlegen,
dann Einstellungen → Apps → TriLola → „Öffnen“.

---

## 1. Tracker (zentral)

Der Tracker läuft auf dem Pi mit `[tracker] node = …` und wird vom Rollout installiert
(`~/bluecat/tracker`, Dienst `trilola.service`, Konfiguration unter `~/bluecat/tracker/config/`).
`node = "@addon"` lässt ihn in der Home-Assistant-App laufen (Abschnitt 0a). Wechselt man den
Ort in der Oberfläche, zieht er um: alter Tracker hält an und speichert, `config/` wird kopiert
(`bluecat_deploy.py tracker-config pull|push`), der neue startet – nie zwei gleichzeitig.
Manuelle Installation, z. B. zum Entwickeln – Voraussetzungen: Python 3.9+, MQTT-Broker, Home Assistant.

```bash
cd bt_tracker
pip install -r requirements-tracker.txt
cp secrets_tri.example.py secrets_tri.py      # MQTT-Zugang, ORIGIN_LAT/LON eintragen
python validate_setup.py                      # Konfiguration prüfen
python trilola_tracker.py
```

systemd-Dienst (`/etc/systemd/system/trilola.service`):

```ini
[Unit]
Description=TriLola Central Tracker
After=network-online.target

[Service]
WorkingDirectory=/pfad/zu/bt_tracker
ExecStart=/usr/bin/python3 /pfad/zu/bt_tracker/trilola_tracker.py
Restart=always
User=pi

[Install]
WantedBy=multi-user.target
```

Wichtige Einstellungen in `secrets_tri.py` (alle optional außer MQTT):

| Schlüssel | Bedeutung |
|---|---|
| `ORIGIN_LAT`, `ORIGIN_LON` | GPS-Koordinate des Punkts (0, 0) für die HA-Karte (bequemer: Karte → Kartenbezug) |
| `ORIGIN_BEARING_DEG` | Richtung der +y-Achse im Uhrzeigersinn von Norden (0 = Norden) |
| `TRACKING_ENGINE` | `pf` (Standard) oder `legacy` |
| `FLOORPLAN_FILE` | Grundriss, Standard `config/floorplan.json` |
| `PF_PARTICLES` | Partikelanzahl (1500; auf einem Pi Zero z. B. 800) |
| `PUBLISH_INTERVAL_SEC` / `PUBLISH_MIN_MOVE_CM` / `PUBLISH_HEARTBEAT_SEC` | Drosselung der HA-Updates |
| `PUBLISH_DIAGNOSTICS` | Messdetails als Attribute (für den HA-Recorder ggf. `False`) |
| `RECORD_FILE` | Rohdaten aufzeichnen, z. B. `recordings/lola.jsonl` (für `tools/replay.py`) |
| `LIVE_PUBLISH`, `LIVE_INTERVAL_SEC`, `LIVE_CLOUD_POINTS` | Live-Daten für die Karte (Standard: an, 1/s, 120 Punkte der Aufenthaltswolke) |
| `RADIO_MAP_INTERVAL_SEC`, `RADIO_MAP_CELL_CM` | Funkkarte: Rechenintervall (10 s) und Rastergröße (50 cm) |
| `RADIO_DYNAMIC_TAU_SEC` | wie lange eine Veränderung auf der Karte nachleuchtet (120 s) |
| `TAG_HEIGHT_CM` | Halsbandhöhe über dem Fußboden (25 cm) |

Über MQTT einstellbar (die Rollout-Oberfläche nutzt genau diese Topics; der Tracker
speichert alles unter `config/` und übernimmt es sofort, ohne Neustart):

| Topic | Inhalt |
|---|---|
| `bluecat/config/floorplan/set` → `…/state` (retained) | Grundriss (Format siehe Abschnitt 4) |
| `bluecat/config/tracker/georef/set` → `…/state` (retained) | `{"lat", "lon", "bearing_deg", "reference", …}` – Ursprung und Ausrichtung, gespeichert in `config/georef.json` |
| `bluecat/config/sensors/<id>/position_x/set`, `…/position_y/set` | Sensorposition in cm |
| `bluecat/config/sensors/<id>/height/set`, `…/floor/set` | Montagehöhe über dem Fußboden bzw. Fußboden eines anderen Stockwerks über Grund (cm, leer = Standard/Wohnung) |
| `bluecat/config/tracker/tuning/set` → `…/state` (retained) | Filter-Feintuning (siehe unten), gespeichert in `config/tuning.json` |
| `bluecat/trilola/live` | ca. 1/s: Position, Genauigkeit, Raum, Bewegung, Aufenthaltswolke, welcher Sensor Lola sieht |
| `bluecat/trilola/radio_map` (retained) | Funkkarten (statisch + Veränderungen) und Funkstrecken (Abschnitt 4) |
| `bluecat/config/calibration/set` → `…/state` | Kalibrierung aus der Oberfläche (Abschnitt 5) |

`config/georef.json` hat Vorrang vor `ORIGIN_*` in `secrets_tri.py` – außer ein neuer Rollout
bringt dort andere Werte mit (dann gewinnt der Rollout). Die Oberfläche schreibt beides.

**Filter-Feintuning** – in der Oberfläche unter *Einstellungen → Filter-Feintuning*: rund 20
Stellschrauben in fünf Gruppen (Bewegung, Anwesenheit, Messmodell, Robustheit, Ausgabe) mit Grenzen,
Erklärung und Standardwert. Wirkt sofort ohne Neustart; *zurücksetzen* je Wert oder *Alles auf
Standard*. „Standard“ ist, was `secrets_tri.py` bzw. der Code vorgibt; Abweichungen stehen in
`config/tuning.json` auf dem Tracker-Pi. Per MQTT: `{"PF_MOVE_SPEED_CM_S": 70}`,
`{"reset": ["PF_MOVE_SPEED_CM_S"]}` oder `{"reset": true}`. Tipp: immer nur einen Wert ändern
und im Livemodus beobachten.

**Anwesenheit** (ab Tracker 2.4.0): Lola gilt als „außer Reichweite“ (Karte, Raum, HA-`not_home`),
sobald *„Weg“ melden nach* Sekunden lang kein Sensor sie mit mindestens *Mindestsignal für „zu Hause“*
gesehen hat. Standard −100 dBm / 30 s = jede Sichtung zählt (bisheriges Verhalten). Hält die Anzeige
zu lange, weil ein Sensor sie z. B. draußen vor dem Fenster noch schwach hört: Mindestsignal auf etwa
−90 bis −85 dBm setzen. Die Schätzung im Modell selbst bleibt davon unberührt.

---

## 2. Sensoren

Alle Sensoren sprechen **Protokoll v2**:

| Topic | Inhalt |
|---|---|
| `bluecat/<id>/sensor/state` (retained) | `{"message_type":"tag_rssi","present":true,"rssi":-71,"sample_count":3,…}` |
| `bluecat/<id>/sensor/mesh` | Sensor-zu-Sensor-Messungen (`sensor_beacon`) |
| `bluecat/<id>/sensor/status` (retained, LWT) | `online` / `offline` |
| `bluecat/registry/<id>/identity` (retained) | Sensor-ID, Topics, eigene BLE-MAC – einmal beim Connect |

Mesh-Beacons senden Manufacturer Data mit Company-ID `0xFFFF` + `TRILOLA` (alle
Plattformen gleich). Ältere ESP32-Beacons ohne Company-ID werden weiterhin erkannt.

### A. Raspberry Pi (`bt_sensor/raspberry_pi_unix`)

* Installation per `deploy/bluecat_deploy.py pi <id>` (siehe Abschnitt 0).
* Manuell: BlueZ Experimental Mode (`bluetoothd -E`), `pip install -r requirements.txt`,
  `secrets_blue.example.py` → `secrets_blue.py`.

### B. Shelly (`bt_sensor/shelly_script`, Firmware ≥ 2.0, Eco Mode aus)

* Ein Skript für alle Geräte: `bluecat_shelly.js`. Sensor-ID, Name und Ziel-MAC
  liest es beim Start aus dem Shelly-KVS (`bluecat.sensor_id`, `bluecat.name`, …),
  das `deploy/bluecat_deploy.py shelly` setzt.
* Die BLE-MAC wird aus der WLAN-MAC + 2 abgeleitet (ESP32-Basisadresse);
  falls das bei einem Gerät nicht stimmt, `ble_mac` in `fleet.toml` setzen.

### C. ESP32 (`bt_sensor/esp32_embedded`, PlatformIO)

`platformio.ini` pinnt `espressif32 @ ~6.5.0` (verhindert Bootloops) und NimBLE 1.4.
Eine Firmware für alle ESP32: `deploy/bluecat_deploy.py esp usb` erzeugt `src/secrets_ble.h`
aus `fleet.toml` und flasht; danach Updates per `esp ota` über WLAN (ArduinoOTA).
Die Sensor-ID kommt retained über `bluecat/provision/<ble-mac ohne Doppelpunkte>`.

---

## 3. Home Assistant

Der Tracker legt an (alle mit Availability über den Tracker-LWT):

* **Lola** (`device_tracker`) – Position auf der Karte, `not_home` wenn außer Reichweite
* **Lola Raum**, **Lola in Bewegung**, **Lola GPS Position** (mit allen Diagnose-Attributen)
* **TriLola Modell** (Auswahl `pf`/`legacy`), **Zielobjekt-MAC**, **Mesh-Baseline neu lernen**
* je Sensor: BLE-MAC, Position X/Y, Kalibrierwerte, Schalter „aktiv“

RSSI, Präsenz und „Aktives Scannen“ je Sensor legt die **Firmware** selbst an.

---

## 4. Karte: Kartenbezug, Grundriss und Live

Tab **Karte** in der Rollout-Oberfläche. Voraussetzung: der Tracker läuft (ab Version 2.1,
Funkkarten und Kalibrierung ab 2.2 – einmal „Aktualisieren“ auf dem Tracker-Pi) und der
MQTT-Broker ist erreichbar. Es gibt fünf Modi:

**Kartenbezug** – legt fest, wo das Haus auf der Welt liegt.
1. *Referenzgerät* wählen, z. B. den Pi, dessen Standort du am besten kennst. Alle
   Maße im Editor beziehen sich darauf.
2. Optional *Adresse* suchen (OpenStreetMap/Nominatim) – die Karte springt dorthin.
3. Den Referenz-Marker genau auf die Stelle ziehen (bzw. in die Karte klicken).
4. *Ausrichtung* drehen, bis Grundriss und Gebäude (Ansicht *Satellit*) übereinanderliegen.
   Gedreht wird um die Referenz.
5. *Speichern* – geht an den Tracker (sofort wirksam, HA-Karte stimmt) und in `fleet.toml`.

**Bearbeiten** – Grundriss und Gerätepositionen. Raster 1 m, gemessen ab der Referenz.
* *Raum ▭*: zwei gegenüberliegende Ecken klicken; *Raum ⬠*: Ecken klicken, Enter schließt.
* *Wand*: Anfang und Ende klicken, weitere Klicks setzen die Wand fort, Esc beendet.
  Wandtyp vorher wählen: Innenwand 5 dB, tragende Wand 10 dB, Außenwand 15 dB,
  Tür/Glas 2 dB (durchlässig – Lola kommt hindurch). Türen sonst als Lücke lassen.
* Punkte rasten auf 10 cm und an vorhandenen Ecken/Geräten ein; Umschalt = rechtwinklig.
* *Auswahl*: Gerät ziehen oder den Abstand zur Referenz in Metern eintippen. Auch die
  Referenz lässt sich hier ziehen – das ändert nur ihre Lage im Haus, nicht den Kartenbezug.
  Raum/Wand anklicken → Name, Dämpfung, „Katze kommt nicht durch“, Ecken ziehen, löschen (Entf).
  Strg+Z macht rückgängig. Nicht platzierte Geräte stehen rechts unter *Platzieren*.
* *Höhe über Fußboden* je Gerät (Antenne, z. B. Schalterdose 1,05 m, Regal 1,80 m; leer =
  Standard: Shelly 1,05 m, sonst 1,00 m). Steht ein Gerät auf einem anderen Stockwerk, trägst
  du dort die Höhe seines Fußbodens über Grund ein; die Wohnung selbst bekommt unter
  *Stockwerk* die Höhe ihres Fußbodens über Grund. Der Tracker rechnet mit dem echten,
  schrägen Abstand $`\sqrt{r^2 + \Delta z^2}`$ zwischen Antenne und Halsband (`TAG_HEIGHT_CM`).
* *Speichern* schickt Grundriss, Positionen und Höhen an den Tracker; er übernimmt sie sofort.
* Option *Statische Funkkarte*: zeigt, wo das Mesh Dämpfung sieht – hilft beim Wändezeichnen.

**Bild** – Grundriss als Bild (PNG/JPG/WebP/GIF, z. B. Scan oder Exposé) unterlegen und
darauf abzeichnen. Das Bild bleibt auf dem Rechner mit der Oberfläche (`deploy/plan/`,
nicht im Git) und geht nicht an den Tracker.
* Ziehen am Mittelpunkt verschiebt, der Eckgriff skaliert und dreht.
* *Zwei Passpunkte* (genaueste Methode): Punkt im Bild → wohin er gehört, zweiter Punkt im
  Bild → wohin er gehört. Verschiebung, Maßstab und Drehung ergeben sich daraus.
  Gute Passpunkte: Positionen von Geräten, Hausecken im Satellitenbild.
* *Maßstab*: zwei Punkte im Bild anklicken und die echte Länge eintippen (z. B. eine
  bemaßte Wand).
* Breite, Drehung und Deckkraft lassen sich auch direkt eintippen.

**Live** – Grundriss mit Geräten auf der Karte, dazu:
* **Lola** mit Genauigkeitskreis, Spur der letzten 10 Minuten und *Aufenthaltswolke*
  (Stichprobe der Partikel – zeigt, wie sicher der Tracker ist).
* **Geräte**: grün = sieht Lola gerade (mit RSSI), grau = sieht sie nicht, rot = offline.
* **Funkkarte** (umschaltbar), beide in dB je Meter Funkweg, per Radio Tomographic
  Imaging aus den Sensor-zu-Sensor-Strecken (regularisierte Kleinste Quadrate, $`x \ge 0`$):
  * *Wände & Hindernisse* (statisch): gelernte Normalwerte der Strecken gegenüber freier
    Ausbreitung. Zeigt dauerhaft dämpfende Dinge – Wände, Schränke, Kühlschrank. Mit
    gezeichneten Wänden werden deren Dämpfungen mitgeschätzt; ohne Grundriss ist die
    Karte nur grob.
  * *Veränderungen* (dynamisch): was jetzt anders ist als der Normalwert, nach Abzug der
    Sensordrift – Person, geschlossene Tür. Steigt sofort an und klingt mit
    `RADIO_DYNAMIC_TAU_SEC` ab.
  * Nur Bereiche, die von Strecken gekreuzt werden, sind messbar. Mit wenigen Sensoren am
    Rand bleibt die Mitte unscharf – dort helfen weitere Sensoren mehr als Rechenkniffe.
  *Funkstrecken* zeigt die einzelnen Sensor-zu-Sensor-Links (grün ok, orange/rot gestört).
* Kartenhintergrund: OpenStreetMap oder Satellit (Esri World Imagery), umschaltbar.

**Kalibrieren** – Autokalibrierung aus dem Mesh und Kalibrierplan mit dem Halsband, siehe Abschnitt 5.

Kartenkacheln und Adresssuche laufen über die Oberfläche (erkennbarer User-Agent, Kacheln
30 Tage im Cache `deploy/.tile_cache`, Adresssuche höchstens 1×/s). Ohne Internet bleibt
die Karte grau, Grundriss und Livemodus funktionieren trotzdem.

Grundriss-Format (`config/floorplan.json` auf dem Tracker-Pi, Koordinaten in cm):

```json
{"default_wall_db": 5.0,
 "walls": [{"a": [x1, y1], "b": [x2, y2], "attenuation_db": 6.0, "blocking": true}],
 "rooms": [{"name": "Wohnzimmer", "polygon": [[x, y], [x, y], [x, y]]}]}
```

Wirkung im Modell `pf`: Wanddämpfung im Messmodell, keine Bewegung durch blockierende
Wände, Raumwahrscheinlichkeiten und der Sensor „Lola Raum“.

`desktop/trilola_gui.py` (älterer Desktop-Editor mit Hintergrundbild) arbeitet nur mit
lokalen Dateien unter `bt_tracker/config/` und erreicht den Tracker-Pi nicht – für den
laufenden Betrieb die Karte in der Rollout-Oberfläche verwenden.

---

## 5. Kalibrierung

Zwei Wege, beide in der Oberfläche unter **Karte → Kalibrieren**. Beide berechnen erst
einen Vorschlag; am Tracker ändert sich erst etwas mit *Übernehmen*.

### A. Automatisch aus dem Mesh (ohne Herumlaufen)

Die Sensoren hören sich an bekannten Positionen gegenseitig. Aus den gelernten
Normalwerten aller Strecken schätzt der Tracker robust (Huber, mit Vorwissen):

```math
B_{r\leftarrow t} = A_t + G_r - 10\,n\,\log_{10} d_{rt} - \textstyle\sum_w c_w\,L_w + \varepsilon
```

* $`G_r`$ = relative Empfangsstärke je Sensor → Vorschlag für `tx_power`. Das Gesamtniveau
  kann das Mesh nicht sehen (Halsband ≠ Sensor-Sender); es wird an bereits kalibrierten
  Sensoren ausgerichtet, sonst bleibt der Mittelwert wie bisher.
* $`n`$ = Pfadverlust, $`L_w`$ = Dämpfung jeder gezeichneten Wand (nur Wände, die von
  mindestens zwei Strecken gekreuzt werden).
* Genauer als geraten, aber gröber als Plan B – mit dem Halsband kalibrierte Sensoren sind
  deshalb standardmäßig abgewählt.

### B. Kalibrierplan mit dem Halsband (genau)

1. *Punkte vorschlagen* verteilt 1–3 Messpunkte je gezeichnetem Raum (Abstand zu den
   Geräten); Punkte lassen sich verschieben, ergänzen, entfernen.
2. Halsband auf den Punkt legen, *Messen* (60–180 s), mindestens 1 m Abstand halten.
   Die Karte zeigt den Fortschritt; Punkte lassen sich jederzeit wiederholen.
3. Ab drei Punkten *Auswerten*, Ergebnis prüfen, *Übernehmen*.

Konsolenwerkzeug mit denselben Rohdaten:

```bash
python calibrate_sensor.py      # Modus 1: Raumkalibrierung, Modus 2: nur neu fitten
```

Gefittet wird ein gemeinsames Modell über alle Punkte und Sensoren:

```math
RSSI_{ij} = P_{0,i} - 10\,n\,\log_{10} d_{ij} - s\cdot\text{Wände}_{ij} + \varepsilon
```

* $`d_{ij}`$ ist der schräge Abstand zwischen Messpunkt (auf Halsbandhöhe) und Sensorantenne.
  Nach dem Eintragen der Höhen einmal neu *Auswerten* – die Rohdaten bleiben erhalten.
* `tx_power` = $`P_{0,i}`$ je Sensor, `n_factor` gemeinsam, `sigma_db` = Streuung der
  Residuen (Shadowing), `r_min`/`r_max` = Streuung einzelner Werte nah/fern,
  `wall_scale` = Korrektur der Grundriss-Dämpfungen.
* Rohdaten liegen in `config/calibration_points.json` (Oberfläche und Konsole teilen sie).
* Nach größeren Umbauten (Sensor versetzt) in HA **„Mesh-Baseline neu lernen“** drücken,
  dann sind auch die Funkkarten wieder sauber.

---

## 6. Modelle im Detail

Kurzfassung – ausführlich mit allen Formeln und Parametern in [Modell & Parameter](MODELL.md).

### `pf` – RSSI-Partikelfilter (Standard)

Jede Sensor-Nachricht wird einzeln und in Zeitreihenfolge eingearbeitet:

```math
z_i = P_{0,i} - 10\,n_i\log_{10}\sqrt{\lVert x - s_i\rVert^2 + (h_i - h_\text{Halsband})^2} - W_i(x) + b_i + \varepsilon,\quad
\varepsilon \sim t_\nu(0, \sigma_i),\quad \sigma_i^2 = \sigma_{\text{shadow}}^2 + 1{,}57\,\sigma_{\text{fading}}^2/k
```

* Partikelzustand $`[x, y, v_x, v_y, \text{Modus}]`$, Modus Ruhe/Bewegung (Markov-Wechsel),
  Geschwindigkeit als Ornstein-Uhlenbeck-Prozess, Wände sperren Bewegungen.
* „Nicht gesehen“: $`P(\text{miss}\mid x) = p_0 + (1-p_0)\,\Phi\big((\text{floor}-\mu_i(x))/\sigma\big)`$.
* Wiederholte Messungen desselben Sensors in Ruhe sind korreliert (Shadowing) und werden
  anteilig gewichtet ($`\beta = \min(1, \Delta t / 6\,\text{s})`$).
* Adaptive Rettungspartikel (AMCL, $`w_\text{slow}/w_\text{fast}`$) um den meldenden Sensor.
* $`b_i`$ = Empfängerdrift aus dem Mesh (siehe unten).

### `legacy` – bisheriges Modell, korrigiert

1D-Kalman je Sensor (Median-of-3-Vorfilter) → Distanz → Locator → Partikelfilter
(nur neue Messungen) → IMM (echtes Stillstandsmodell). Das Funk-Grid (SLAM) ist
standardmäßig aus (`RADIO_GRID_ENABLED`).

### Mesh: Drift und Funkqualität

Abweichungen der Sensor-zu-Sensor-Links von ihrer Baseline werden in Sender- und
Empfängeranteil zerlegt ($`R_{t\to r} - B_{t\to r} = a_t + b_r`$, robuste Ridge-Regression,
Gleichtakt entfernt). Für das Halsband zählt nur $`b_r`$. Weichen viele Sensoren zugleich
stark ab, gilt die Baseline als verdächtig (`baseline_suspect`) und es wird nicht korrigiert.

---

## 7. Werkzeuge

| Befehl | Zweck |
|---|---|
| `python validate_setup.py` | Konfiguration prüfen |
| `python -m pytest tests` | Tests (inkl. Simulator-Regressionstests) |
| `python -m tools.ab_compare --seeds 8` | Modelle im Simulator vergleichen |
| `python -m tools.replay recordings/lola.jsonl --csv out.csv` | echte Aufzeichnung mit beiden Modellen nachspielen |
| `python -m pytest deploy/tests` (im Repo-Hauptordner) | Tests für Rollout und Oberfläche (mit nachgebauten Shellys, SSH-, MQTT- und Kartendiensten) |

`simulation.py` bildet Wände, räumlich korreliertes Shadowing, Körperabschattung,
Ausreißer, Paketverlust, Sensor-Drift und das Verhalten beider Firmware-Generationen nach.

---

## 8. Umstieg von der alten Version

1. Rollout nach Abschnitt 0. Er übernimmt Tracker-Konfiguration, Mesh-Baseline und
   Koordinatenursprung automatisch aus der alten Installation auf dem Pi und löst die
   alten Dienste ab (Rollback jederzeit möglich).
2. *Wartung → Home Assistant aufräumen*, danach in HA die MQTT-Integration einmal neu laden.
   Leere Einträge wie `kunibert_kiosk` bietet das Aufräumen zum Entfernen an.
3. „Mesh-Baseline neu lernen“ drücken, sobald alle Sensoren mit der neuen Firmware laufen.
4. Karte: Kartenbezug prüfen, Grundriss zeichnen, Positionen kontrollieren (Abschnitt 4).
5. Raumkalibrierung durchführen (Abschnitt 5).
6. Optional: `RECORD_FILE` setzen und beide Modelle mit `tools/replay.py` auf echten Daten vergleichen.

---

## 9. Was nicht ins Git gehört

Die `.gitignore` hält Zugangsdaten und Daten deiner Installation aus dem Repository:

| Datei / Ordner | enthält |
|---|---|
| `deploy/fleet.toml` (+ `.bak`), `secrets_*.py`, `secrets_ble.h`, `deploy/.gui_token` | WLAN-, MQTT-, OTA-, Shelly-Passwörter, Standort |
| `bt_sensor/esp32_embedded/.pio/` | gebaute Firmware – WLAN-, MQTT- und OTA-Passwort im Klartext |
| `bt_tracker/config/`, `config/` | MAC-Adressen, Positionen, Kalibrierung, Grundriss, Standort (`georef.json`) |
| `deploy/plan/`, `deploy/.cache.json`, `recordings/` | Grundriss-Bild, Shelly-IPs, Messaufzeichnungen |

Die Tests verwenden eine anonymisierte Kopie unter `bt_tracker/tests/fixtures/config/`.
