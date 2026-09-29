"""Feintuning der Filterkoeffizienten zur Laufzeit.

Eine kuratierte Liste von Parametern (mit Grenzen, Einheit und Erklärung) lässt
sich per MQTT ändern, ohne den Tracker neu zu starten:

    bluecat/config/tracker/tuning/set    {"PF_MOVE_SPEED_CM_S": 70}
                                         {"reset": true}  bzw.  {"reset": ["PF_STUDENT_NU"]}
    bluecat/config/tracker/tuning/state  (retained) Schema, Standard- und aktuelle Werte

Die Abweichungen vom Standard liegen in ``config/tuning.json``. „Standard“ ist,
was ``secrets_tri.py`` (also ``fleet.toml``) bzw. der Code vorgibt.
"""

from __future__ import annotations

import json
import math
import os
from typing import Dict, Optional

TUNING_FILE = "tuning.json"

# key, Gruppe, Bezeichnung, Einheit, min, max, Schritt, Erklärung, Modelle
SCHEMA = [
    # --- Bewegung ---------------------------------------------------------------
    ("PF_MOVE_SPEED_CM_S", "Bewegung", "Typische Laufgeschwindigkeit", "cm/s", 20, 300, 5,
     "Streuung der Laufgeschwindigkeit je Richtung – im Mittel ist Lola im Modell etwa 1,25-mal so schnell. "
     "Höher = folgt schnellen Wechseln besser, springt aber leichter. Sinnvoll deutlich unter der Höchstgeschwindigkeit.",
     "pf"),
    ("PF_MOVE_TAU_SEC", "Bewegung", "Richtungsbeständigkeit", "s", 0.5, 10, 0.5,
     "Wie lange eine Laufrichtung beibehalten wird. Höher = glattere Bahnen, träger bei Richtungswechseln.", "pf"),
    ("PF_REST_DIFFUSION_CM", "Bewegung", "Unruhe in Ruhe", "cm/√s", 0, 20, 0.5,
     "Wie stark die Position in Ruhe wandern darf. Kleiner = ruhigere Anzeige, wenn sie schläft.", "pf"),
    ("PF_MOVE_DIFFUSION_CM", "Bewegung", "Zusätzliche Unruhe in Bewegung", "cm/√s", 0, 60, 1,
     "Zufällige Abweichung zusätzlich zur Laufbewegung.", "pf"),
    ("PF_MEAN_REST_SEC", "Bewegung", "Mittlere Ruhedauer (Modell)", "s", 2, 300, 1,
     "Wie lange das Modell im Mittel Ruhe annimmt. Höher = bleibt eher liegen, reagiert später auf Aufbrechen.",
     "pf"),
    ("PF_MEAN_MOVE_SEC", "Bewegung", "Mittlere Laufdauer (Modell)", "s", 2, 120, 1,
     "Wie lange das Modell im Mittel Bewegung annimmt.", "pf"),
    ("MAX_POSITION_SPEED_CM_S", "Bewegung", "Höchstgeschwindigkeit", "cm/s", 100, 800, 10,
     "Obergrenze der Laufgeschwindigkeit im Modell (dazu kommt nur noch die zufällige Unruhe).", "pf"),
    # --- Messmodell ------------------------------------------------------------------
    ("TAG_HEIGHT_CM", "Messmodell", "Halsbandhöhe über dem Fußboden", "cm", 0, 200, 1,
     "Stehende Katze ca. 25 cm, liegend ca. 10 cm. Zusammen mit den Sensorhöhen ergibt sich der echte Funkabstand.",
     "pf,legacy"),
    ("PF_STUDENT_NU", "Messmodell", "Ausreißer-Toleranz", "ν", 1, 30, 0.5,
     "Klein = einzelne verrückte Messwerte schaden kaum (robust). Groß = jeder Wert zählt voll (Normalverteilung).",
     "pf"),
    ("PF_SAME_SENSOR_CORRELATION_SEC", "Messmodell", "Gedächtnis je Sensor", "s", 0, 10, 0.5,
     "Schnell aufeinanderfolgende Werte desselben Sensors zählen anteilig (mindestens zu 20 %). "
     "Höher = ein einzelner Sensor dominiert weniger. 0 = jeder Wert zählt voll.",
     "pf"),
    ("PF_MISS_BASE_PROB", "Messmodell", "„Nicht gesehen“ trotz Nähe", "", 0.01, 0.5, 0.01,
     "Wie oft ein Sensor das Halsband trotz Nähe verpasst. Höher = „nicht gesehen“ schiebt Lola weniger weg.", "pf"),
    ("PRESENCE_LOST_SEC", "Anwesenheit", "„Weg“ melden nach", "s", 5, 600, 5,
     "So lange ohne ausreichend starke Sichtung, bis Lola als „außer Reichweite“ gilt (Karte und Home Assistant).",
     "pf,legacy"),
    ("PRESENCE_MIN_RSSI_DBM", "Anwesenheit", "Mindestsignal für „zu Hause“", "dBm", -120, -50, 1,
     "Schwächere Sichtungen halten die Anzeige nicht mehr am Leben (z. B. Lola draußen vor dem Fenster). "
     "-120 = jede Sichtung zählt, -100 (Standard) ignoriert nur extrem schwache. Typisch: -90 bis -85; "
     "zu hoch → sie gilt in entfernten Ecken als weg.",
     "pf,legacy"),
    ("SENSOR_TIMEOUT_SEC", "Messmodell", "Messwert gilt als aktuell", "s", 5, 120, 1,
     "So lange gilt die letzte Sichtung eines Sensors als aktuell – für die Zahl der beteiligten Sensoren und "
     "den Status „aktiv“ (im Partikelfilter nicht für die Position selbst).", "pf,legacy"),
    # --- Robustheit -------------------------------------------------------------------
    ("PF_PARTICLES", "Robustheit", "Anzahl Partikel", "", 300, 5000, 100,
     "Mehr = genauer und stabiler, braucht mehr Rechenzeit (Pi Zero: ≤ 800). Setzt den Filter zurück.", "pf"),
    ("PF_MAX_INJECT", "Robustheit", "Wiederfinden: max. Anteil neuer Partikel", "", 0, 0.5, 0.01,
     "Wenn die Messungen nicht mehr passen, werden neue Kandidaten verteilt. Höher = findet sie schneller wieder, "
     "springt aber leichter.", "pf"),
    ("PF_TRACK_RESET_SEC", "Robustheit", "Neustart nach Pause", "s", 30, 3600, 30,
     "Nach so langer Zeit ohne Sichtung beginnt die Suche von vorn.", "pf"),
    # --- Ausgabe ----------------------------------------------------------------------
    ("PF_OUTPUT_SMOOTHING_SEC", "Ausgabe", "Glättung der Anzeige in Ruhe", "s", 0, 60, 1,
     "Beruhigt die angezeigte Position, wenn sie liegt (0 = aus). Verzögert kleine echte Wechsel.", "pf"),
    ("PUBLISH_INTERVAL_SEC", "Ausgabe", "Home Assistant: höchstens alle", "s", 0.5, 30, 0.5,
     "Mindestabstand zwischen zwei Positionsmeldungen an Home Assistant.", "pf,legacy"),
    ("PUBLISH_MIN_MOVE_CM", "Ausgabe", "Home Assistant: erst ab Bewegung von", "cm", 0, 200, 5,
     "Kleinere Änderungen werden nicht gemeldet (weniger Einträge in der HA-Historie).", "pf,legacy"),
    ("MOVING_SPEED_CM_S", "Ausgabe", "„In Bewegung“ ab", "cm/s", 5, 150, 5,
     "Schwelle für den Sensor „Lola in Bewegung“.", "pf,legacy"),
    ("RADIO_DYNAMIC_TAU_SEC", "Ausgabe", "Funkkarte: Nachleuchten von Veränderungen", "s", 10, 900, 10,
     "Wie lange eine Störung auf der Karte „Veränderungen“ sichtbar bleibt.", "pf,legacy"),
]
KEYS = {row[0] for row in SCHEMA}
INT_KEYS = {"PF_PARTICLES"}


def validate(changes: dict) -> Dict[str, float]:
    """Prüft Änderungen gegen das Schema; unbekannte Schlüssel oder Werte außerhalb der Grenzen → ValueError."""
    if not isinstance(changes, dict):
        raise ValueError("Feintuning muss ein JSON-Objekt sein")
    limits = {row[0]: (row[4], row[5]) for row in SCHEMA}
    out = {}
    for key, value in changes.items():
        if key not in KEYS:
            raise ValueError(f"unbekannter Parameter {key}")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(f"{key}: ungültige Zahl")
        low, high = limits[key]
        if not low <= number <= high:
            raise ValueError(f"{key}: {number:g} liegt nicht zwischen {low:g} und {high:g}")
        out[key] = int(round(number)) if key in INT_KEYS else number
    return out


def load(config_dir: str) -> Dict[str, float]:
    path = os.path.join(config_dir, TUNING_FILE)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError("kein JSON-Objekt")
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"tuning.json ungültig ({error}) – Standardwerte")
        return {}
    out = {}
    for key, value in data.items():  # einzelne kaputte Werte verwerfen, den Rest behalten
        if key not in KEYS:
            continue
        try:
            out.update(validate({key: value}))
        except (ValueError, TypeError) as error:
            print(f"tuning.json: {error} – Standardwert")
    return out


def save(config_dir: str, overrides: Dict[str, float]):
    os.makedirs(config_dir, exist_ok=True)
    path = os.path.join(config_dir, TUNING_FILE)
    if not overrides:
        if os.path.exists(path):
            os.remove(path)
        return
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(overrides, handle, indent=2, sort_keys=True)
    os.replace(tmp, path)


def state(defaults: Dict[str, float], overrides: Dict[str, float], engine: str,
          message: Optional[str] = None) -> dict:
    params = []
    for key, group, label, unit, low, high, step, help_text, engines in SCHEMA:
        default = defaults.get(key)
        params.append({
            "key": key, "group": group, "label": label, "unit": unit, "min": low, "max": high, "step": step,
            "help": help_text, "engines": engines.split(","), "default": default,
            "value": overrides.get(key, default), "overridden": key in overrides,
        })
    return {"params": params, "engine": engine, "message": message or ""}
