"""Lokale Tracker-Konfiguration.

Nach ``secrets_tri.py`` kopieren und Broker-Zugangsdaten eintragen.
"""



# Copy to secrets_tri.py and replace all placeholder values.
# Do not commit secrets_tri.py.
MQTT_BROKER = "192.168.X.XX"
MQTT_PORT = 1883
MQTT_USER = ""
MQTT_PASSWORD = ""

# Optional: BLE device identifier. If omitted, the first tagged device is
# selected automatically; payloads without an ID remain supported.
# BLE_TAG_ID = "AA:BB:CC:DD:EE:FF"

# Optional runtime tuning.
SENSOR_TIMEOUT_SEC = 30.0
MAX_SNAPSHOT_SKEW_SEC = 1.0
POSITION_MEMORY_SEC = 10.0
Q_VARIANCE = 0.1

# Plausibilitätsgrenzen für die lokale Trackingfläche.
# Werte in cm bzw. cm/s.
MAX_TRACK_DISTANCE_CM = 5000.0
MAX_POSITION_RADIUS_CM = 2500.0
MAX_POSITION_JUMP_CM = 1500.0
MAX_POSITION_SPEED_CM_S = 350.0
MAX_PUBLISH_ACCURACY_CM = 1000.0

# Partikelfilter
PARTICLE_COUNT = 1500
PARTICLE_PROCESS_NOISE_CM = 75.0
PARTICLE_INITIAL_SPREAD_CM = 250.0
PARTICLE_MINIMUM_SIGMA_CM = 75.0
PARTICLE_BOUNDS_MARGIN_CM = 500.0

# Sensor-zu-Sensor-Funkumgebung
RADIO_BASELINE_FILE = "config/radio_mesh_baseline.json"
RADIO_BASELINE_LEARNING_SAMPLES = 30
RADIO_LINK_TIMEOUT_SEC = 15.0

# --- Lokales Koordinatensystem Nullpunkt / GPS ---
ORIGIN_LAT = 47.930222
ORIGIN_LON = 10.289361
