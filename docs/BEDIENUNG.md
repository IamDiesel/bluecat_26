# TriLola – Bedienung

<img src="images/lola-icon.png" width="72" align="right" alt="Lola">

Diese Seite beschreibt, wie du die Wohnung in TriLola abbildest, die Geräte platzierst,
kalibrierst und Lola live verfolgst – und was jede Option und Stellschraube bewirkt.
Die Installation steht in der [README](../README.md), die Mathematik dahinter in
[Modell & Parameter](MODELL.md).

**Inhalt**

- [Überblick: die fünf Reiter](#überblick-die-fünf-reiter)
- [Die Wohnung einrichten – empfohlene Reihenfolge](#die-wohnung-einrichten--empfohlene-reihenfolge)
  - [1. Kartenbezug](#1-kartenbezug)
  - [2. Grundriss-Bild (optional)](#2-grundriss-bild-optional)
  - [3. Räume und Wände zeichnen](#3-räume-und-wände-zeichnen)
  - [4. Geräte platzieren](#4-geräte-platzieren)
  - [5. Höhen und Stockwerk](#5-höhen-und-stockwerk)
  - [6. Kalibrieren](#6-kalibrieren)
- [Livemodus](#livemodus)
- [Einstellungen](#einstellungen)
- [Filter-Feintuning](#filter-feintuning)
- [Wartung und Protokoll](#wartung-und-protokoll)
- [Häufige Fragen](#häufige-fragen)

---

## Überblick: die fünf Reiter

| Reiter | Wofür |
|---|---|
| **Geräte** | alle Pis, ESP32 und Shellys mit Live-Status (online, IP, Version, „sieht Lola“); einrichten, aktualisieren, bearbeiten, entfernen; neu gefundene Geräte übernehmen |
| **Karte** | Kartenbezug, Grundriss, Geräte platzieren, Kalibrieren und der **Livemodus** |
| **Einstellungen** | Zugangsdaten (MQTT, WLAN, OTA, Shelly), der Tracker und das **Filter-Feintuning** |
| **Wartung** | App in Home Assistant, Home Assistant aufräumen, ESP32-Werkzeug, Prüfungen |
| **Protokoll** | die Ausgabe jeder Aktion, live |

Oben zeigen zwei Anzeigen, ob der **MQTT-Broker** erreichbar und der **Tracker** online ist.
Geräte lassen sich auch ohne beide einrichten. Grundriss, Positionen, Kalibrierung und Feintuning
speichert aber der Tracker selbst – dafür muss er laufen.

---

## Die Wohnung einrichten – empfohlene Reihenfolge

Alles passiert im Reiter **Karte**. Oben links wählst du den Modus:
**Live · Bearbeiten · Bild · Kalibrieren · Kartenbezug**. Rechts oben schaltest du den
Kartenhintergrund um (**Karte** = OpenStreetMap, **Satellit**, **Aus**).

Maße und Raster zeigt die Oberfläche relativ zu einem **Referenzgerät**, und am Referenzgerät
hängt auch der Kartenbezug. Nimm ein Gerät, dessen Standort du genau kennst und das nicht
wandert – etwa den Tracker-Pi.

### 1. Kartenbezug

*Wo liegt die Wohnung auf der Welt?* Das braucht Home Assistant, um Lola auf seiner Karte
richtig anzuzeigen. Für die Ortung in der Wohnung selbst ist es nicht nötig.

1. Modus **Kartenbezug** → **Referenzgerät** wählen.
2. Optional **Adresse suchen** – die Karte springt dorthin.
3. Den **Referenz-Marker** genau auf den Standort des Geräts ziehen (beim ersten Mal geht auch
   ein Klick in die Karte). Hintergrund *Satellit* hilft.
4. **Ausrichtung** drehen, bis Grundriss und Gebäude übereinanderliegen. Gedreht wird um die Referenz.
5. **Speichern** – der Tracker übernimmt es sofort, falls er online ist, sonst beim nächsten Rollout.

### 2. Grundriss-Bild (optional)

Ein Scan, ein Exposé oder ein Foto des Grundrisses als Vorlage zum Abzeichnen.
Das Bild bleibt auf dem Rechner mit der Oberfläche und geht nicht an den Tracker.

1. Modus **Bild** → Bild hochladen (PNG, JPG, WebP, GIF).
2. Ausrichten – am genauesten mit **zwei Passpunkten**: einen Punkt im Bild anklicken, dann die
   Stelle, wo er hingehört; dasselbe für einen zweiten Punkt. Verschiebung, Maßstab und Drehung
   ergeben sich daraus. Gute Passpunkte sind Geräte oder Hausecken im Satellitenbild.
3. Alternativ **Maßstab**: zwei Punkte im Bild anklicken und die echte Länge eintippen
   (z. B. eine bemaßte Wand). Ziehen am Mittelpunkt verschiebt, der Eckgriff skaliert und dreht.
4. **Deckkraft** nach Geschmack; Breite und Drehung lassen sich auch eintippen.

### 3. Räume und Wände zeichnen

Modus **Bearbeiten**. Der Grundriss macht das Modell deutlich besser: Wände dämpfen den Funk,
Lola kann nicht durch Wände laufen, und aus den Räumen wird der Sensor „Lola Raum“.

| Werkzeug | Bedienung |
|---|---|
| **Raum ▭** | erste Ecke klicken, dann die gegenüberliegende |
| **Raum ⬠** | Ecken nacheinander klicken; Enter oder Klick auf den ersten Punkt schließt |
| **Wand** | Anfang und Ende klicken, weitere Klicks setzen die Wand fort; Esc beendet |
| **Auswahl** | Raum/Wand anklicken → Name, Dämpfung, „Katze kommt nicht durch“, Ecken ziehen, löschen (Entf) |

Punkte rasten auf 10 cm und an vorhandenen Ecken und Geräten ein; **Umschalt** hält Linien
rechtwinklig; **Strg+Z** macht rückgängig; Raster 1 m.

**Wandtyp beim Zeichnen** (rechts) – bestimmt, wie stark die Wand den Funk dämpft:

| Wandtyp | Dämpfung | Lola kommt durch? |
|---|---|---|
| Innenwand | 5 dB | nein |
| Tragende Wand | 10 dB | nein |
| Außenwand | 15 dB | nein |
| Tür / Glas (durchlässig) | 2 dB | ja |

Offene Türen zeichnest du am besten als **Lücke** in der Wand. Die Dämpfungen sind Startwerte –
die [Kalibrierung](#6-kalibrieren) korrigiert sie.

Die Option **Statische Funkkarte** (rechts unter *Anzeige*) blendet ein, wo das Mesh dauerhaft
Dämpfung sieht. Das hilft, vergessene Wände oder große Möbel (Schrank, Kühlschrank) zu finden.

**Speichern** schickt den Grundriss an den Tracker; er wirkt sofort.

### 4. Geräte platzieren

Noch nicht platzierte Geräte stehen rechts unter **Noch nicht platziert** → **Platzieren** →
auf die Stelle klicken. Platzierte Geräte verschiebst du mit **Auswahl** oder tippst den Abstand
zur Referenz in Metern ein.

**Ein Gerät rechnet erst mit, wenn es platziert und gespeichert ist.** Danach aber sofort:
Der Tracker nimmt es ohne Neustart auf. Solange es nicht kalibriert ist, rechnet es mit
Standardwerten. Die Mesh-Verbindungen zu den anderen Sensoren (*Funkstrecken*) erscheinen erst,
wenn der Tracker für jede Strecke rund 30 Messungen gesammelt hat – das dauert einige Minuten.

**Gute Verteilung** bringt mehr als jede Stellschraube:

* Sensoren um die Wohnung **herum** verteilen, nicht in einer Reihe; Räume, in denen Lola oft
  ist, sollten von mindestens zwei Sensoren „gesehen“ werden.
* Nicht direkt hinter Metall, Heizkörpern oder im Schrank.
* Mehr Sensoren helfen mehr als genauere Kalibrierung – vor allem in der Wohnungsmitte.

### 5. Höhen und Stockwerk

Das Halsband hängt knapp über dem Boden, die Sensoren oft auf Schalter- oder Regalhöhe. Unter
einem Sensor ist Lola deshalb nicht „0 m“ entfernt. TriLola rechnet mit dem echten, schrägen
Abstand (Formel in [Modell → Geometrie](MODELL.md#2-geometrie-schräger-abstand)).

* **Höhe über Fußboden (m)** – bei jedem Gerät unter **Auswahl**: Höhe der Antenne ab dem eigenen
  Fußboden, z. B. Schalterdose 1,05 m, Regal 1,80 m. Leer = Standard (Shelly 1,05 m, sonst 1,00 m).
* **Anderes Stockwerk (m)** – nur, wenn das Gerät nicht in der Wohnung steht: die Höhe *seines*
  Fußbodens über Grund.
* **Stockwerk → Fußboden der Wohnung über Grund (m)** – nur nötig, wenn ein Gerät auf einem anderen
  Stockwerk steht; dann zählt der Höhenunterschied der Fußböden mit.
* Die **Halsbandhöhe** (Standard 25 cm) stellst du im [Feintuning](#filter-feintuning) ein.

> Eine ältere Kalibrierung ohne Höhen bleibt bewusst „eben“, bis du neu kalibrierst. Die Oberfläche
> weist beim Gerät darauf hin. Mesh und Funkkarte nutzen die Höhen sofort.

### 6. Kalibrieren

Jeder Sensor hört etwas anders: Einbauort, Gehäuse, Antenne. Die Kalibrierung lernt je Sensor,
wie stark das Halsband in 1 m Abstand ankommt, und gemeinsam für alle, wie schnell das Signal mit
der Entfernung abfällt und wie stark die gezeichneten Wände wirklich dämpfen. Modus **Kalibrieren** bietet zwei Wege. Beide berechnen zuerst einen **Vorschlag**;
am Tracker ändert sich erst etwas mit **Übernehmen**.

**A · Autokalibrierung (Mesh)** – ohne Herumlaufen. Die Sensoren hören sich gegenseitig an
bekannten Positionen; daraus schätzt der Tracker die relative Empfangsstärke jedes Sensors, den
Abfall mit der Entfernung und die Dämpfung jeder gezeichneten Wand. Gut für den Anfang oder
nach dem Hinzufügen eines Sensors. Bereits mit dem Halsband kalibrierte Sensoren sind
standardmäßig abgewählt, weil Weg B genauer ist.

**B · Kalibrierplan mit dem Halsband** – genauer:

1. **Punkte vorschlagen** verteilt 1–3 Messpunkte je Raum mit Abstand zu den Geräten.
   Punkte lassen sich verschieben, mit **Punkt hinzufügen** ergänzen oder entfernen.
2. Das Halsband an den Punkt legen (etwa in Halsbandhöhe) und **Messen** (60–180 s).
   Selbst mindestens 1 m Abstand halten – der Körper dämpft.
3. Ab drei Punkten **Auswerten**, Ergebnis ansehen, **Übernehmen**.

Die Rohdaten bleiben erhalten: Nach geänderten Höhen oder Wänden genügt erneutes **Auswerten**.

**Mesh-Baseline neu lernen** (Taste in Home Assistant): nach Umbauten, einem Firmware-Wechsel oder
wenn ein Sensor an derselben Stelle höher/tiefer oder anders montiert wurde. Dann lernt der Tracker
die Normalwerte *aller* Sensor-zu-Sensor-Strecken neu – die braucht er für Drift-Korrektur und
Funkkarte. Seitlich versetzte Sensoren (neue Position gespeichert) lernt er automatisch neu.

---

## Livemodus

Modus **Live** zeigt den Grundriss mit Geräten und Lola, rechts die Details:

* **Lola** – Punkt mit **Genauigkeitskreis**; rechts Raum, Genauigkeit, Bewegung, Abstand zur
  Referenz, Alter der Daten und das aktive Modell.
* **Geräte** – grün = sieht Lola gerade (mit Signalstärke), grau = sieht sie nicht, rot = offline.

**Anzeige** (rechts, an/aus):

| Option | zeigt |
|---|---|
| Grundriss · Grundriss-Bild · Geräte · Lola | die jeweilige Ebene |
| Spur (10 min) | Lolas Weg der letzten 10 Minuten, seit die Karte offen ist |
| Aufenthaltswolke | eine Stichprobe der Partikel: wo Lola nach Meinung des Filters sein könnte. Eng = sicher, breit oder mehrere Wolken = unsicher |
| Funkstrecken | die einzelnen Sensor-zu-Sensor-Verbindungen: grün ok, orange/rot gestört (z. B. jemand steht dazwischen) |
| Lola folgen | die Karte schwenkt mit |

**Funkkarte** – was das Mesh über die Wohnung „sieht“, in dB Dämpfung je Meter Funkweg:

* **Veränderungen** – was *jetzt* anders ist als normal: Personen, geschlossene Türen, verschobene
  Möbel. Steigt schnell an (die Karte wird alle 10 s neu berechnet) und klingt dann langsam ab
  (einstellbar: *Funkkarte: Nachleuchten*).
* **Wände & Hindernisse** – was *dauerhaft* dämpft: Wände, Schränke, Kühlschrank.
* **Aus**.

Messbar ist nur, was von Strecken gekreuzt wird. Mit wenigen Sensoren am Rand bleibt die Mitte
unscharf – dort hilft ein weiterer Sensor mehr als jede Einstellung.

---

## Einstellungen

| Bereich | Feld | Bedeutung |
|---|---|---|
| **MQTT-Broker** | Adresse, Port, Benutzer, Passwort | Broker in Home Assistant (Mosquitto); *Verbindung testen* |
| **WLAN** | Name, Passwort | nur für die ESP32-Firmware; nach einer Änderung alle ESP32 aktualisieren, solange sie noch im alten WLAN sind |
| **ESP32-Updates** | OTA-Passwort | schützt Updates per WLAN. Nach einem Wechsel einfach alle ESP32 aktualisieren – TriLola merkt sich das alte Passwort, bis alle umgestellt sind |
| **Shellys** | Benutzer, Passwort, Netz für die Suche | Zugang zur Shelly-Weboberfläche; leeres Netz = das Netz des Brokers |
| **Tracker** | Läuft auf | welcher Pi rechnet – oder „Home Assistant (in dieser App)“. Ein Wechsel zieht Konfiguration, gelernte Werte und Kalibrierung mit um |
| | Halsband-MAC (Lola) | welches Halsband verfolgt wird |
| | Modell | **Partikelfilter (pf)** – Standard, nutzt Grundriss und Räume; **Klassisch (legacy)** – das ältere Modell, zum Vergleich |
| | Ursprung Breite/Länge, Ausrichtung | Kartenbezug als Zahlen (bequemer über Karte → Kartenbezug) |

Passwortfelder zeigen Punkte; das Schloss daneben blendet den Inhalt ein.

---

## Filter-Feintuning

*Einstellungen → Filter-Feintuning* bietet gut 20 Stellschrauben in fünf Gruppen. Jede zeigt
Grenzen, Einheit, Erklärung und Standardwert; ein **●** markiert Werte, die vom Standard abweichen.

* Nach **Übernehmen** wirkt es sofort, ohne Neustart, und bleibt beim Tracker gespeichert
  (**Verwerfen** nimmt ungespeicherte Änderungen zurück).
* **Zurücksetzen** je Wert oder **Alles auf Standard**.
* **Vorgehen:** immer nur *eine* Stellschraube ändern und im Livemodus beobachten. Die meisten
  Probleme löst eine bessere Sensorverteilung oder Kalibrierung, nicht das Feintuning.

Die wichtigsten Stellschrauben und wann du sie brauchst:

| Symptom | Stellschraube | Richtung |
|---|---|---|
| Lola bleibt zu lange sichtbar, obwohl sie draußen ist | **Mindestsignal für „zu Hause“** | auf etwa −90 dBm anheben (−85 = strenger) |
| … und verschwindet zu spät | **„Weg“ melden nach** | kürzer, z. B. 15 s |
| Position zittert, während sie schläft | **Unruhe in Ruhe** / **Glättung der Anzeige in Ruhe** | kleiner / 5–15 s |
| Position hinkt beim Rennen hinterher | **Typische Laufgeschwindigkeit** | größer |
| Position springt zwischen Räumen | **Ausreißer-Toleranz** / **Gedächtnis je Sensor** | kleiner / größer |
| Nach dem Rausgehen oder Neustart findet sie der Filter nur langsam wieder | **Wiederfinden: max. Anteil neuer Partikel** | größer |
| „Lola in Bewegung“ schaltet zu oft | **„In Bewegung“ ab** | größer |
| Home-Assistant-Verlauf wird zu voll | **Home Assistant: höchstens alle** / **erst ab Bewegung von** | größer |
| Pi ist ausgelastet | **Anzahl Partikel** | kleiner (Pi Zero: ≤ 800) |

Jede Stellschraube – mit Formelzeichen, Standard, Bereich und genauer Wirkung auf das Modell –
ist in [Modell & Parameter → Parameterübersicht](MODELL.md#9-parameterübersicht) beschrieben.

---

## Wartung und Protokoll

| Wartung | wozu |
|---|---|
| **Als App auf Home Assistant** (am PC) bzw. **Home-Assistant-App** (in der App) | Oberfläche als App installieren/aktualisieren – siehe [README](../README.md#trilola-als-app-in-home-assistant) |
| **Home Assistant aufräumen** | findet Einträge von Geräten, die es nicht mehr gibt; *Vorschau* ändert nichts |
| **ESP32-Werkzeug** | PlatformIO installieren/aktualisieren; *Sensor-IDs neu zuweisen* |
| **Übersicht & Prüfung** | Konfiguration prüfen, Status aller Knoten als Text |
| **Ausgeblendete Geräte** | gefundene Geräte, die du bewusst nicht einbindest, wieder anzeigen |
| **Gemerkte Passwörter** | sudo-Passwörter dieser Sitzung vergessen |
| **Konfigurationsdatei** | wo `fleet.toml` liegt; vor jeder Änderung entsteht `fleet.toml.bak` |

Der Reiter **Protokoll** zeigt jede Aktion (Installieren, Flashen, Aufräumen …) mit ihrer
vollständigen Ausgabe. Passwörter werden dort durch `***` ersetzt.

---

## Häufige Fragen

**Ein Gerät ist „offline“ oder ein Update meldet „keine Antwort“.**
Kurz vom Strom nehmen und wieder einstecken; prüfen, ob die angezeigte IP noch stimmt.
ESP32 mit sehr alter Firmware (< 2.1) brauchen einmal einen USB-Flash.

**Ich habe einen Sensor versetzt.**
Neue Position speichern – seine Mesh-Strecken lernt der Tracker dann selbst neu. Danach den Sensor
neu kalibrieren (Autokalibrierung reicht für den Anfang). Nur wenn er an derselben Stelle höher,
tiefer oder anders montiert wurde, **Mesh-Baseline neu lernen** drücken.

**Lola steht auf der Karte in der falschen Wohnung/Straße.**
Das ist nur der Kartenbezug – im Modus **Kartenbezug** Referenz-Marker und Ausrichtung korrigieren.
Die Position *in* der Wohnung bleibt davon unberührt.

**Was bedeuten die Diagnose-Attribute in Home Assistant?**
„Lola GPS Position“ enthält je Sensor die gemessene und die erwartete Signalstärke, die
Abweichung, die Mesh-Drift und die Funkqualität. Große Abweichungen bei einem Sensor deuten auf
eine falsche Position oder Kalibrierung hin. Die Begriffe erklärt [Modell & Parameter](MODELL.md).
