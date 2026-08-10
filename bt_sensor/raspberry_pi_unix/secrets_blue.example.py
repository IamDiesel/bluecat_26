"""Lokale Raspberry-Pi-Konfiguration.

Diese Datei nach ``secrets_blue.py`` kopieren und nur MQTT-Zugangsdaten
eintragen. TARGET_MAC und MESH_PEER_MACS sind optionale Offline-Fallbacks;
im Normalbetrieb kommen sie über MQTT vom Tracker.
"""

SENSOR_ID = "ron"
SENSOR_NAME = "Ron Raspberry Pi"
STATE_TOPIC = "bluecat/ron/sensor/state"
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
