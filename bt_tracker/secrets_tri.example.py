"""Lokale Tracker-Konfiguration.

Nach ``secrets_tri.py`` kopieren und Broker-Zugangsdaten eintragen.
``secrets_tri.py`` nicht committen. Alle Werte außer MQTT_* sind optional.
"""

MQTT_BROKER = "192.168.X.XX"
MQTT_PORT = 1883
MQTT_USER = ""
MQTT_PASSWORD = ""

# --- Lokales Koordinatensystem → GPS (für die HA-Karte) ---
ORIGIN_LAT = 47.930222          # Breite des Punkts (0, 0)
ORIGIN_LON = 10.289361          # Länge des Punkts (0, 0)
ORIGIN_BEARING_DEG = 0.0        # Richtung der +y-Achse, im Uhrzeigersinn von Norden

# Optional: BLE-Tag-ID, falls Sensoren eine ID mitsenden.
# BLE_TAG_ID = "AA:BB:CC:DD:EE:FF"

# --- Modell ---
# "pf"     = neues RSSI-Partikelfilter-Modell (Standard)
# "legacy" = bisheriges Modell (Distanz → Locator → PF → IMM), korrigiert
# Die Auswahl lässt sich auch in Home Assistant umschalten ("TriLola Modell").
TRACKING_ENGINE = "pf"
FLOORPLAN_FILE = "config/floorplan.json"   # optional, siehe README
# PF_PARTICLES = 1500                      # weniger = schneller (z. B. 800 auf einem Pi Zero)

# --- Zeitverhalten ---
SENSOR_TIMEOUT_SEC = 30.0       # ohne Sichtung → Sensor gilt als „sieht nichts“
POSITION_MEMORY_SEC = 10.0      # nur Legacy-Modell
MAX_SNAPSHOT_SKEW_SEC = 3.0     # nur Legacy-Modell

# --- Ausgabe an Home Assistant ---
PUBLISH_INTERVAL_SEC = 1.0      # höchstens so oft eine neue Position
PUBLISH_MIN_MOVE_CM = 5.0       # kleinere Änderungen nicht publizieren
PUBLISH_HEARTBEAT_SEC = 60.0    # spätestens dann trotzdem publizieren
PUBLISH_DIAGNOSTICS = True      # Messdetails als Attribute (größer für den HA-Recorder)

# --- Plausibilitätsgrenzen (cm bzw. cm/s) ---
MAX_TRACK_DISTANCE_CM = 5000.0
MAX_POSITION_RADIUS_CM = 2500.0
MAX_POSITION_SPEED_CM_S = 350.0

# --- Partikelfilter des Legacy-Modells ---
PARTICLE_COUNT = 1500
PARTICLE_PROCESS_NOISE_CM = 75.0
PARTICLE_INITIAL_SPREAD_CM = 250.0
PARTICLE_MINIMUM_SIGMA_CM = 75.0
PARTICLE_BOUNDS_MARGIN_CM = 500.0

# --- Sensor-zu-Sensor-Funkumgebung (Mesh) ---
RADIO_BASELINE_FILE = "config/radio_mesh_baseline.json"
RADIO_BASELINE_LEARNING_SAMPLES = 30
RADIO_LINK_TIMEOUT_SEC = 30.0
RADIO_GRID_ENABLED = False      # Funk-Grid (SLAM) des Legacy-Modells, experimentell

# --- Aufzeichnung für tools/replay.py (leer = aus) ---
RECORD_FILE = ""                # z. B. "recordings/trilola.jsonl"
