#include <Arduino.h>
#include <WiFi.h>
#include <PubSubClient.h>
#include <NimBLEDevice.h>
#include <ArduinoJson.h>
#include <ctime>
#include <map>
#include <vector>
#include <string>
#include <strings.h>
#include "secrets_ble.h"

/*
 * Optionale Werte können in secrets_ble.h überschrieben werden:
 *
 *   #define SENSOR_ID "arnd_esp"
 *   #define SENSOR_NAME "Arnd ESP32"
 *   #define STATE_TOPIC "bluecat/arnd_esp/sensor/state"
 *   #define AVAILABILITY_TOPIC "bluecat/arnd_esp/sensor/status"
 *   #define MESH_ENABLED 1
 *   #define MESH_PEER_MACS "aa:bb:cc:dd:ee:ff,11:22:33:44:55:66"
 *   #define MESH_MARKER "TRILOLA"
 *
 * TARGET_MAC, WLAN- und MQTT-Zugangsdaten bleiben wie bisher in
 * secrets_ble.h. Ohne Überschreibungen werden die sicheren Standardwerte
 * unten verwendet.
 */
#ifndef SENSOR_ID
#define SENSOR_ID "arnd_esp"
#endif
#ifndef SENSOR_NAME
#define SENSOR_NAME "Arnd ESP32"
#endif
#ifndef STATE_TOPIC
#define STATE_TOPIC "bluecat/arnd_esp/sensor/state"
#endif
#ifndef AVAILABILITY_TOPIC
#define AVAILABILITY_TOPIC "bluecat/arnd_esp/sensor/status"
#endif
#ifndef TARGET_MAC
#define TARGET_MAC ""
#endif
#ifndef MESH_ENABLED
#define MESH_ENABLED 1
#endif
#ifndef MESH_PREFIX
#define MESH_PREFIX "TRILOLA_SENSOR:"
#endif
#ifndef MESH_PEER_MACS
#define MESH_PEER_MACS ""
#endif
#ifndef MESH_MARKER
#define MESH_MARKER "TRILOLA"
#endif

constexpr char DISCOVERY_ROOT[] = "homeassistant";
constexpr uint32_t MIN_PUBLISH_INTERVAL_MS = 2000;
constexpr uint32_t TIMEOUT_MS = 30000;
constexpr int OFFLINE_RSSI = -130;

WiFiClient espClient;
PubSubClient mqttClient(espClient);
NimBLEScan* pBLEScan = nullptr;
NimBLEAdvertising* pAdvertising = nullptr;

uint32_t last_publish_time = 0;
uint32_t last_seen_time = 0;
uint32_t tag_sequence = 0;
uint32_t last_identity_time = 0;
bool is_present = false;
bool is_active_scan = true;
String target_mac = TARGET_MAC;
String local_ble_mac;
std::vector<String> dynamic_mesh_peers;
std::map<std::string, uint32_t> mesh_last_publish;
std::map<std::string, uint32_t> mesh_sequences;

void setupWiFi();
void reconnectMQTT();
void publishDiscovery();
void mqttCallback(char* topic, byte* payload, unsigned int length);
void restartScan();
void publishTagRssi(int rssi, bool present);
void publishMeshRssi(
    const std::string& beaconMac,
    int rssi,
    const std::string& legacySensorId
);
void publishAvailability(const char* state);
void publishIdentity();
void updateRuntimeConfiguration(const String& topic, const String& payload);
uint64_t timestampSeconds();
bool isConfiguredMeshPeer(const std::string& address);

uint64_t timestampSeconds() {
    const time_t now = time(nullptr);
    return now > 1000000000 ? static_cast<uint64_t>(now) : 0;
}

void publishTagRssi(int rssi, bool present) {
    if (!mqttClient.connected()) {
        return;
    }
    ++tag_sequence;
    JsonDocument doc;
    doc["message_type"] = "tag_rssi";
    doc["sensor_id"] = SENSOR_ID;
    doc["rssi"] = rssi;
    doc["timestamp"] = timestampSeconds();
    doc["uptime_ms"] = millis();
    doc["sequence"] = tag_sequence;
    doc["sample_count"] = 1;
    doc["present"] = present;

    char buffer[384];
    serializeJson(doc, buffer, sizeof(buffer));
    mqttClient.publish(STATE_TOPIC, buffer, false);
    last_publish_time = millis();
}

void publishMeshRssi(
    const std::string& beaconMac,
    int rssi,
    const std::string& legacySensorId
) {
#if MESH_ENABLED
    if (!mqttClient.connected() || beaconMac.empty()) {
        return;
    }
    const uint32_t now = millis();
    if (now - mesh_last_publish[beaconMac] < MIN_PUBLISH_INTERVAL_MS) {
        return;
    }
    ++mesh_sequences[beaconMac];

    JsonDocument doc;
    doc["message_type"] = "sensor_beacon";
    doc["sensor_id"] = SENSOR_ID;
    doc["beacon_mac"] = beaconMac;
    if (!legacySensorId.empty() && legacySensorId != SENSOR_ID) {
        doc["beacon_sensor"] = legacySensorId;
    }
    doc["rssi"] = rssi;
    doc["timestamp"] = timestampSeconds();
    doc["uptime_ms"] = now;
    doc["sequence"] = mesh_sequences[beaconMac];

    char buffer[384];
    serializeJson(doc, buffer, sizeof(buffer));
    mqttClient.publish(STATE_TOPIC, buffer, false);
    mesh_last_publish[beaconMac] = now;
#else
    (void)beaconMac;
    (void)rssi;
    (void)legacySensorId;
#endif
}

void publishAvailability(const char* state) {
    if (mqttClient.connected()) {
        mqttClient.publish(AVAILABILITY_TOPIC, state, true);
    }
}

bool isConfiguredMeshPeer(const std::string& address) {
    String configured = MESH_PEER_MACS;
    configured.toLowerCase();
    String normalized = String(address.c_str());
    normalized.toLowerCase();
    normalized.replace(":", "");
    normalized.replace("-", "");

    int start = 0;
    while (start < configured.length()) {
        int end = configured.indexOf(',', start);
        if (end < 0) {
            end = configured.length();
        }
        String candidate = configured.substring(start, end);
        candidate.trim();
        candidate.replace(":", "");
        candidate.replace("-", "");
        if (candidate == normalized) {
            return true;
        }
        start = end + 1;
    }
    for (const String& peer : dynamic_mesh_peers) {
        if (peer == normalized) {
            return true;
        }
    }
    return false;
}

void publishIdentity() {
    if (!mqttClient.connected() || local_ble_mac.isEmpty()) {
        return;
    }
    JsonDocument doc;
    doc["schema"] = 1;
    doc["sensor_id"] = SENSOR_ID;
    doc["name"] = SENSOR_NAME;
    doc["implementation"] = "esp32";
    doc["state_topic"] = STATE_TOPIC;
    doc["availability_topic"] = AVAILABILITY_TOPIC;
    doc["ble_mac"] = local_ble_mac;
    doc["enabled"] = true;
    doc["timestamp"] = timestampSeconds();
    char buffer[512];
    serializeJson(doc, buffer, sizeof(buffer));
    const String topic =
        String("bluecat/registry/") + SENSOR_ID + "/identity";
    mqttClient.publish(topic.c_str(), buffer, true);
}

void updateRuntimeConfiguration(const String& topic, const String& payload) {
    const String targetStateTopic = "bluecat/config/target_mac/state";
    const String peersTopic = "bluecat/registry/mesh_peers";
    if (topic == targetStateTopic) {
        JsonDocument doc;
        DeserializationError error = deserializeJson(doc, payload);
        if (!error && doc.is<JsonObject>()) {
            const char* configured = doc["target_mac"] | nullptr;
            if (configured == nullptr) {
                configured = doc["mac"] | "";
            }
            target_mac = String(configured);
        } else {
            target_mac = payload;
        }
        target_mac.trim();
        target_mac.toLowerCase();
        return;
    }
    if (topic != peersTopic) {
        return;
    }
    JsonDocument doc;
    if (deserializeJson(doc, payload) || !doc["peers"].is<JsonArray>()) {
        return;
    }
    dynamic_mesh_peers.clear();
    for (JsonObject peer : doc["peers"].as<JsonArray>()) {
        const char* mac = peer["ble_mac"] | "";
        String normalized = String(mac);
        normalized.toLowerCase();
        normalized.replace(":", "");
        normalized.replace("-", "");
        if (!normalized.isEmpty()) {
            String own = local_ble_mac;
            own.toLowerCase();
            own.replace(":", "");
            if (normalized != own) {
                dynamic_mesh_peers.push_back(normalized);
            }
        }
    }
}

class MyAdvertisedDeviceCallbacks : public NimBLEAdvertisedDeviceCallbacks {
    void onResult(NimBLEAdvertisedDevice* advertisedDevice) override {
        const std::string deviceMAC =
            advertisedDevice->getAddress().toString();

#if MESH_ENABLED
        const std::string manufacturer =
            advertisedDevice->getManufacturerData();
        const std::string prefix(MESH_PREFIX);
        const std::string marker(MESH_MARKER);
        bool isMeshBeacon = false;
        std::string legacySensorId;
        if (manufacturer.compare(0, marker.size(), marker) == 0) {
            isMeshBeacon = true;
        }
        if (manufacturer.compare(0, prefix.size(), prefix) == 0) {
            legacySensorId = manufacturer.substr(prefix.size());
            isMeshBeacon = !legacySensorId.empty();
        }
        const std::string localName = advertisedDevice->getName();
        if (localName.compare(0, prefix.size(), prefix) == 0) {
            legacySensorId = localName.substr(prefix.size());
            isMeshBeacon = !legacySensorId.empty();
        }
        if (!isMeshBeacon && isConfiguredMeshPeer(deviceMAC)) {
            isMeshBeacon = true;
        }
        if (isMeshBeacon) {
            publishMeshRssi(
                deviceMAC,
                advertisedDevice->getRSSI(),
                legacySensorId
            );
        }
#endif

        String normalizedTarget = target_mac;
        normalizedTarget.toLowerCase();
        normalizedTarget.replace(":", "");
        normalizedTarget.replace("-", "");
        String normalizedDevice = String(deviceMAC.c_str());
        normalizedDevice.toLowerCase();
        normalizedDevice.replace(":", "");
        normalizedDevice.replace("-", "");
        if (normalizedTarget.isEmpty() || normalizedDevice != normalizedTarget) {
            return;
        }

        last_seen_time = millis();
        is_present = true;
        const uint32_t now = millis();
        if (now - last_publish_time < MIN_PUBLISH_INTERVAL_MS) {
            return;
        }
        publishTagRssi(advertisedDevice->getRSSI(), true);
        Serial.printf(
            "[%s] RSSI: %d dBm\n",
            is_active_scan ? "ACTIVE" : "PASSIVE",
            advertisedDevice->getRSSI()
        );
    }
};

void setup() {
Serial.begin(115200);
    delay(10);
    Serial.println("\nStarte Bluecat BLE-Sensor (ESP32)...");

    // --- DER BULLETPROOF BLUETOOTH RESET ---
    // Zwingt die Hardware auf Null, falls sie einen Software-Reset überlebt hat
    esp_bt_controller_disable();
    delay(50);
    esp_bt_controller_deinit();
    delay(50);

    // Jetzt frisch und sicher initialisieren
    NimBLEDevice::init(SENSOR_NAME);
    local_ble_mac = NimBLEDevice::getAddress().toString().c_str();
    local_ble_mac.toLowerCase();
    pAdvertising = NimBLEDevice::getAdvertising();

    setupWiFi();
    mqttClient.setServer(MQTT_BROKER, MQTT_PORT);
    mqttClient.setCallback(mqttCallback);
    mqttClient.setBufferSize(1024);

    delay(2000);
    Serial.println("Starte NimBLE");


#if MESH_ENABLED
    NimBLEAdvertisementData advertisementData;
    // Nur ein kurzer Marker wird ausgesendet. Die Senderidentität kommt
    // aus der BLE-MAC; dadurch bleibt das Advertisement klein.
    advertisementData.setManufacturerData(std::string(MESH_MARKER));
    pAdvertising->setAdvertisementData(advertisementData);
    pAdvertising->start();
#endif

    pBLEScan = NimBLEDevice::getScan();
    pBLEScan->setAdvertisedDeviceCallbacks(
        new MyAdvertisedDeviceCallbacks(), true
    );
    pBLEScan->setActiveScan(is_active_scan);
    pBLEScan->setInterval(100);
    pBLEScan->setWindow(99);
    restartScan();
}

void loop() {
    if (!mqttClient.connected()) {
        reconnectMQTT();
    }
    mqttClient.loop();

    const uint32_t now = millis();
    if (now - last_identity_time > 60000) {
        publishIdentity();
        last_identity_time = now;
    }
    if (is_present && now - last_seen_time > TIMEOUT_MS) {
        Serial.printf(
            "Ziel seit %lu ms nicht gesehen. Sende Offline-Payload.\n",
            TIMEOUT_MS
        );
        is_present = false;
        publishTagRssi(OFFLINE_RSSI, false);
    }
}

void setupWiFi() {
    Serial.printf("Verbinde mit WiFi %s ", WIFI_SSID);
    WiFi.mode(WIFI_STA);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    while (WiFi.status() != WL_CONNECTED) {
        delay(500);
        Serial.print(".");
    }
    Serial.println("\nWiFi verbunden!");
    Serial.println(WiFi.localIP());
    configTime(0, 0, "pool.ntp.org", "time.nist.gov");
}

void reconnectMQTT() {
    while (!mqttClient.connected()) {
        Serial.print("Verbinde zu MQTT Broker...");
        String clientId =
            String("bluecat_") + SENSOR_ID + "_" +
            String(random(0xffff), HEX);
        if (mqttClient.connect(
                clientId.c_str(),
                MQTT_USER,
                MQTT_PASSWORD,
                AVAILABILITY_TOPIC,
                1,
                true,
                "offline"
            )) {
            Serial.println(" erfolgreich.");
            mqttClient.subscribe(
                ("bluecat/" + String(SENSOR_ID) + "/switch/scan_mode/set").c_str()
            );
            mqttClient.subscribe("bluecat/registry/mesh_peers");
            mqttClient.subscribe("bluecat/config/target_mac/state");
            mqttClient.subscribe("bluecat/config/target_mac/set");
            publishDiscovery();
            publishAvailability("online");
            publishIdentity();
            mqttClient.publish(
                ("bluecat/" + String(SENSOR_ID) + "/switch/scan_mode/state").c_str(),
                is_active_scan ? "ON" : "OFF",
                true
            );
        } else {
            Serial.printf(" Fehler rc=%d; neuer Versuch in 5 s.\n",
                          mqttClient.state());
            delay(5000);
        }
    }
}

void publishDiscovery() {
    const String sensorId = SENSOR_ID;
    const String discoverySensor =
        String(DISCOVERY_ROOT) + "/sensor/bluecat_" + sensorId + "_rssi/config";
    const String discoveryPresence =
        String(DISCOVERY_ROOT) + "/binary_sensor/bluecat_" + sensorId +
        "_presence/config";
    const String discoverySwitch =
        String(DISCOVERY_ROOT) + "/switch/bluecat_" + sensorId +
        "_scan_mode/config";
    const String commandTopic =
        "bluecat/" + sensorId + "/switch/scan_mode/set";
    const String switchStateTopic =
        "bluecat/" + sensorId + "/switch/scan_mode/state";

    JsonDocument deviceDoc;
    deviceDoc["identifiers"][0] = String("bluecat_sensor_") + sensorId;
    deviceDoc["name"] = SENSOR_NAME;
    deviceDoc["manufacturer"] = "Bluecat";
    deviceDoc["model"] = "ESP32 BLE Sensor";

    JsonDocument sensorDoc;
    sensorDoc["name"] = String(SENSOR_NAME) + " RSSI";
    sensorDoc["unique_id"] = String("bluecat_") + sensorId + "_rssi";
    sensorDoc["state_topic"] = STATE_TOPIC;
    sensorDoc["availability_topic"] = AVAILABILITY_TOPIC;
    sensorDoc["value_template"] =
        "{% if value_json.message_type is not defined or "
        "value_json.message_type == 'tag_rssi' %}"
        "{{ value_json.rssi }}{% endif %}";
    sensorDoc["unit_of_measurement"] = "dBm";
    sensorDoc["device_class"] = "signal_strength";
    sensorDoc["entity_category"] = "diagnostic";
    sensorDoc["device"] = deviceDoc;
    char sensorBuffer[768];
    serializeJson(sensorDoc, sensorBuffer, sizeof(sensorBuffer));
    mqttClient.publish(discoverySensor.c_str(), sensorBuffer, true);

    JsonDocument presenceDoc;
    presenceDoc["name"] = String(SENSOR_NAME) + " Präsenz";
    presenceDoc["unique_id"] = String("bluecat_") + sensorId + "_presence";
    presenceDoc["state_topic"] = STATE_TOPIC;
    presenceDoc["availability_topic"] = AVAILABILITY_TOPIC;
    presenceDoc["value_template"] =
        "{% if value_json.message_type is not defined or "
        "value_json.message_type == 'tag_rssi' %}"
        "{{ 'ON' if value_json.present is defined and value_json.present "
        "else ('ON' if value_json.rssi|float > -120 else 'OFF') }}"
        "{% endif %}";
    presenceDoc["payload_on"] = "ON";
    presenceDoc["payload_off"] = "OFF";
    presenceDoc["device_class"] = "presence";
    presenceDoc["entity_category"] = "diagnostic";
    presenceDoc["device"] = deviceDoc;
    char presenceBuffer[768];
    serializeJson(presenceDoc, presenceBuffer, sizeof(presenceBuffer));
    mqttClient.publish(discoveryPresence.c_str(), presenceBuffer, true);

    JsonDocument switchDoc;
    switchDoc["name"] = String(SENSOR_NAME) + " Aktives Scannen";
    switchDoc["unique_id"] = String("bluecat_") + sensorId + "_scan_mode";
    switchDoc["command_topic"] = commandTopic;
    switchDoc["state_topic"] = switchStateTopic;
    switchDoc["availability_topic"] = AVAILABILITY_TOPIC;
    switchDoc["icon"] = "mdi:bluetooth-audio";
    switchDoc["entity_category"] = "config";
    switchDoc["device"] = deviceDoc;
    char switchBuffer[768];
    serializeJson(switchDoc, switchBuffer, sizeof(switchBuffer));
    mqttClient.publish(discoverySwitch.c_str(), switchBuffer, true);
}

void mqttCallback(char* topic, byte* payload, unsigned int length) {
    String incomingTopic(topic);
    String incomingPayload;
    for (unsigned int i = 0; i < length; ++i) {
        incomingPayload += static_cast<char>(payload[i]);
    }
    if (incomingTopic == "bluecat/registry/mesh_peers" ||
        incomingTopic == "bluecat/config/target_mac/state" ||
        incomingTopic == "bluecat/config/target_mac/set") {
        updateRuntimeConfiguration(
            incomingTopic == "bluecat/config/target_mac/set"
                ? String("bluecat/config/target_mac/state")
                : incomingTopic,
            incomingPayload
        );
        return;
    }
    const String commandTopic =
        "bluecat/" + String(SENSOR_ID) + "/switch/scan_mode/set";
    if (strcmp(topic, commandTopic.c_str()) != 0) {
        return;
    }
    String command = incomingPayload;
    const bool newMode = command == "ON";
    if (newMode != is_active_scan) {
        is_active_scan = newMode;
        restartScan();
        const String stateTopic =
            "bluecat/" + String(SENSOR_ID) + "/switch/scan_mode/state";
        mqttClient.publish(
            stateTopic.c_str(),
            is_active_scan ? "ON" : "OFF",
            true
        );
    }
}

void restartScan() {
    pBLEScan->stop();
    pBLEScan->clearResults();
    pBLEScan->setActiveScan(is_active_scan);
    pBLEScan->start(0, nullptr, false);
    Serial.printf(
        "Scanner im Modus '%s' gestartet.\n",
        is_active_scan ? "active" : "passive"
    );
}
