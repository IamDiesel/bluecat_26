# TriLola

Die Oberfläche zum Katzen-Tracker Lola: Geräte einrichten und aktualisieren (Raspberry Pis,
Shellys, ESP32), Karte mit Grundriss und Livemodus, Kalibrierung und Filter-Feintuning.

## Öffnen

Einstellungen → Apps → TriLola → „In der Seitenleiste anzeigen“. Die Anmeldung übernimmt
Home Assistant, einen eigenen Zugang gibt es nicht.

## Wo läuft was?

- **Oberfläche und Rollout:** in dieser App. Im Leerlauf braucht sie fast nichts.
- **Tracker (Rechenarbeit):** standardmäßig auf einem Raspberry Pi (z. B. Ron). Unter
  *Einstellungen → Tracker → Läuft auf* lässt er sich auch in diese App holen und wieder
  zurückschicken; Konfiguration, Mesh-Baselines und Kalibrierung ziehen mit um.
- **ESP32-Firmware bauen (Updates per WLAN):** in dieser App. Beim ersten Mal lädt sie die
  Werkzeuge (ca. 1 GB, nicht in den Backups) – das dauert einige Minuten.
- **ESP32 zum ersten Mal per USB flashen:** den ESP32 an diesen Home-Assistant-Rechner stecken.

## Daten

`addon_configs/local_trilola` (per Samba/SSH sichtbar, Teil der HA-Backups):

| Datei | Inhalt |
|---|---|
| `fleet.toml` | alle Geräte und Zugangsdaten (WLAN, MQTT, Shelly, OTA) |
| `plan/` | Grundriss-Bild, Kalibrierplan |
| `tracker/` | nur wenn der Tracker in der App läuft: Konfiguration und gelernte Werte |
| `ssh/` | Schlüssel, mit dem die App die Pis erreicht |

## Aktualisieren

Am PC im Repo `rollout.bat` starten → *Wartung* → „App installieren / aktualisieren“
(ohne „Daten übernehmen“). Die App wird dann in Home Assistant neu gebaut.

## Netzwerk

Die App nutzt das Host-Netz: ESP32-Updates per WLAN verbinden sich zurück zur App, und die
Shelly-Suche liest die Nachbartabelle. Die Oberfläche selbst antwortet nur Home Assistant.
