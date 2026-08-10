# Copy to secrets_tri.py and replace all placeholder values.
# Do not commit secrets_tri.py.
MQTT_BROKER = "192.168.X.X"
MQTT_PORT = 1883
MQTT_USER = "mqtt_user"
MQTT_PASSWORD = "mqtt_pass"

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
MAX_TRACK_DISTANCE_CM = 2000.0
MAX_POSITION_RADIUS_CM = 2000.0
MAX_POSITION_JUMP_CM = 500.0
MAX_POSITION_SPEED_CM_S = 250.0
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

# Erwartetes Sensor-zu-Sensor-Payload:
# {"message_type": "sensor_beacon",
#  "beacon_mac": "aa:bb:cc:dd:ee:ff",
#  "rssi": -61}
