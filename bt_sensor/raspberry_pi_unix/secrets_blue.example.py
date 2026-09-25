"""Lokale Raspberry-Pi-Konfiguration.

Diese Datei nach ``secrets_blue.py`` kopieren und MQTT-Zugangsdaten
eintragen. TARGET_MAC und MESH_PEER_MACS sind optionale Offline-Fallbacks;
im Normalbetrieb kommen sie retained über MQTT vom Tracker.
"""

SENSOR_ID = "ron"
SENSOR_NAME = "Ron Raspberry Pi"
STATE_TOPIC = "bluecat/ron/sensor/state"
MESH_TOPIC = "bluecat/ron/sensor/mesh"
AVAILABILITY_TOPIC = "bluecat/ron/sensor/status"

MQTT_BROKER = "192.168.1.10"
MQTT_PORT = 1883
MQTT_USER = ""
MQTT_PASSWORD = ""

TARGET_MAC = ""
MESH_PEER_MACS = []
MESH_ENABLED = True
MESH_ADVERTISING_ENABLED = True
MESH_ADAPTER = "hci0"

# Aggregation (Standardwerte passen zu ESP32 und Shelly)
WINDOW_SEC = 2.0
ABSENT_TIMEOUT_SEC = 10.0
ABSENT_HEARTBEAT_SEC = 15.0
