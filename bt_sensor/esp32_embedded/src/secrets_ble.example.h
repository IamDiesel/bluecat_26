#pragma once

// Nach secrets_ble.h kopieren und lokale Zugangsdaten eintragen.
#define WIFI_SSID "MeinWLAN"
#define WIFI_PASSWORD "MeinWLANPasswort"

#define MQTT_BROKER "192.168.1.10"
#define MQTT_PORT 1883
#define MQTT_USER ""
#define MQTT_PASSWORD ""

#define SENSOR_ID "arnd_esp"
#define SENSOR_NAME "Arnd ESP32"
#define STATE_TOPIC "bluecat/arnd_esp/sensor/state"
#define AVAILABILITY_TOPIC "bluecat/arnd_esp/sensor/status"

// Optionaler Offline-Fallback. Ziel-MAC und Peer-Liste werden normalerweise
// nach der MQTT-Registrierung zentral vom Tracker verteilt.
#define TARGET_MAC ""
#define MESH_ENABLED 1
#define MESH_PEER_MACS ""
#define MESH_MARKER "TRILOLA"
