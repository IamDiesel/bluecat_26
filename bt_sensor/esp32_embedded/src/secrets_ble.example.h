#pragma once
// Wird normalerweise von deploy/bluecat_deploy.py aus deploy/fleet.toml
// erzeugt ("python deploy/bluecat_deploy.py esp secrets"). Alle ESP32 teilen
// sich diese Werte – die Sensor-ID kommt zur Laufzeit per MQTT
// (bluecat/provision/<ble-mac>), siehe main.cpp.
#define WIFI_SSID "MeinWLAN"
#define WIFI_PASSWORD "MeinWLANPasswort"

#define MQTT_BROKER "192.168.1.10"
#define MQTT_PORT 1883
#define MQTT_USER ""
#define MQTT_PASSWORD ""

#define OTA_PASSWORD "bitte-aendern"

// Optional: Offline-Fallback, falls der Tracker nicht erreichbar ist
#define TARGET_MAC ""
#define MESH_ENABLED 1
#define MESH_PEER_MACS ""
