# Änderungen

## 2.6.3
- Karte auf schmalen Bildschirmen (Handy, HA-App): die Modi erscheinen als Symbole in einer Zeile, der
  Hintergrund als ein Umschalt-Knopf (Karte → Sat. → Aus), die Zeichenwerkzeuge kompakt darunter – nichts wird
  mehr seitlich abgeschnitten. Der Seitenkopf scrollt auf dem Handy mit weg und verdeckt die Karte nicht.

## 2.6.2
- Hotspots: dritte Darstellung **Cluster** – nahe Aufenthaltsorte werden je nach Zoomstufe zu Kreisen mit Dauer
  und Anteil zusammengefasst; Klick auf einen Kreis zoomt hinein.
- Kippy-GPS: gespeichert werden jetzt die Rohpunkte mit Uhrzeit; der laufende Tag wird nur noch ab dem letzten
  Abruf ergänzt statt ganz neu geladen. Bereits geladene Tage bleiben gültig (kein erneuter Komplett-Abruf).
- Die Karte fragt die App nur noch nach Änderungen: ohne neue Daten kommt eine kurze „unverändert“-Antwort.

## 2.6.1
- Hotspots: Darstellung als **Wolken** oder **Kästchen**, Raster einstellbar (drinnen 25 cm – 1 m, draußen
  5 – 50 m; Standard 50 cm bzw. 10 m). Draußen werden dafür die GPS-Punkte einmalig neu von Kippy geladen.
- Hotspots drinnen bleiben beim Herauszoomen sichtbar (vorher kleiner als ein Pixel); Hinweis im Kasten
  „Drinnen“, falls keine Daten ankommen.
- App-Version auf der Seite **Geräte** und in Home Assistant als Entität „App-Version“ am TriLola-Gerät. Die
  Oberfläche am PC zeigt die installierte App-Version ebenfalls (über MQTT) und meldet, wenn das Repo neuer ist.
- Keine „invalid escape sequence“-Warnung mehr beim Start von `rollout.bat`.

## 2.6.0
- Neuer Kartenmodus **Hotspots**: Wärmekarte, wo Lola sich aufhält – für Tag, Woche, Monat oder Jahr, drinnen
  (TriLola) und draußen (GPS-Verlauf der Kippy-Integration über `kippy.export_history`), mit Räumen und
  Lieblingsplätzen. Die App zeichnet dafür die Aufenthaltsdauer drinnen dauerhaft auf (etwa ein Jahr).
- Neue App-Berechtigungen: Zugriff auf die Home-Assistant-API (Kippy-Dienst, Verlauf) und lesend auf das
  HA-Konfigurationsverzeichnis (die exportierte GPS-Datei in `www/`).
- Kalibrieren: Höhe je Punkt, einzelne Messungen löschen, laufende Messung abbrechen.

## 2.5.3
- App-Update aus `rollout.bat` wartet, bis Home Assistant die neue Version kennt, und aktualisiert dann richtig
  (vorher konnte „No update available“ erscheinen); die Update-Anzeige in Home Assistant wird sofort aufgefrischt.
- Pi-Installation: USB-Bluetooth-Stick wird erkannt (Firmware, eingebauter Chip aus), Bluetooth wird vor jedem
  Sensor-Start entsperrt und eingeschaltet.
- Wartung: Tracker-Stand über MQTT sichern und wiederherstellen (z. B. nach defekter SD-Karte).

## 2.5.2
- Kartenbezug springt nach dem Speichern nicht mehr zurück; Funkkarte wandert im Kartenbezug mit.
- Tracker rechnet die Wände-und-Hindernisse-Karte nach verschobenen Sensoren neu.

## 2.5.1
- Beschriftungen auf der Karte überdecken sich nicht mehr; Karte lädt beim ersten Öffnen.
- Filterparameter geprüft: „Nicht gesehen trotz Nähe“ = 0 zerstört den Filter nicht mehr, Bereiche und Texte korrigiert.

## 2.5.0
- Positionen, Höhen und Kalibrierpunkte als Tabelle; CSV-Import aus SweetHome3D und CSV-Export.
- Höhe je Kalibrierpunkt; Bildmitte zum Eintippen.

## 2.4.0
- Anwesenheit: „Weg“ nach einstellbarer Zeit bzw. unter einem Mindestsignal.
