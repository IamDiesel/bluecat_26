#include <Arduino.h>
#include <WiFi.h>
#include <ESPmDNS.h>
#include <ArduinoOTA.h>
#include <Preferences.h>
#include <PubSubClient.h>
#include <NimBLEDevice.h>
#include <ArduinoJson.h>
#include <algorithm>
#include <ctime>
#include <map>
#include <string>
#include <vector>
#include "secrets_ble.h"

/*
 * Bluecat BLE-Sensor (ESP32, NimBLE) – Protokoll v2, eine Firmware für alle
 *
 * secrets_ble.h (wird von deploy/bluecat_deploy.py erzeugt) enthält nur noch
 * die gemeinsamen Werte: WLAN, MQTT, OTA-Passwort. Die Sensor-ID kommt zur
 * Laufzeit per MQTT:
 *
 *   bluecat/provision/<ble-mac ohne ':'>   (retained)
 *   {"sensor_id": "arnd_esp", "name": "Arnd ESP32"}
 *
 * Die Werte werden im Flash (NVS) gespeichert. Ohne Provisionierung heißt der
 * Sensor "esp32_<letzte 6 Stellen der MAC>". Nach dem MQTT-Connect wartet die
 * Firmware bis zu PROVISION_WAIT_MS auf die Provisionierung, bevor sie sich
 * anmeldet – so entsteht kein Phantom-Sensor.
 *
 * Updates per WLAN (ArduinoOTA): pio run -e ota -t upload --upload-port <IP>
 *
 * Verhalten (Protokoll v2)
 * * Sichtungen des Halsbands in 2-s-Fenstern → Median + Anzahl (retained)
 * * 10 s ohne Sichtung → "present": false, danach alle 15 s; direkt nach dem
 *   Connect ebenfalls
 * * Mesh-Messungen auf .../sensor/mesh
 * * BLE-Callbacks schieben nur in eine Queue; MQTT läuft nur in loop()
 */
#ifndef FW_VERSION
#define FW_VERSION "2.1.0"
#endif
#ifndef DEFAULT_SENSOR_ID
#ifdef SENSOR_ID
#define DEFAULT_SENSOR_ID SENSOR_ID  // Kompatibilität zu alten secrets_ble.h
#else
#define DEFAULT_SENSOR_ID ""
#endif
#endif
#ifndef DEFAULT_SENSOR_NAME
#ifdef SENSOR_NAME
#define DEFAULT_SENSOR_NAME SENSOR_NAME
#else
#define DEFAULT_SENSOR_NAME ""
#endif
#endif
#ifndef OTA_PASSWORD
#define OTA_PASSWORD ""
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
constexpr uint32_t WINDOW_MS = 2000;
constexpr uint32_t ABSENT_TIMEOUT_MS = 10000;
constexpr uint32_t ABSENT_HEARTBEAT_MS = 15000;
constexpr uint32_t MQTT_RETRY_MS = 5000;
constexpr uint32_t WIFI_RETRY_MS = 10000;
constexpr uint32_t PROVISION_WAIT_MS = 4000;
constexpr int OFFLINE_RSSI = -130;
constexpr size_t MAX_WINDOW_SAMPLES = 32;
constexpr uint16_t MARKER_COMPANY_ID = 0xFFFF;  // "Test/Intern" laut Bluetooth SIG

struct ScanItem {
    uint8_t addr[6];  // NimBLE-native Reihenfolge (LSB zuerst)
    int8_t rssi;
    uint8_t meshMarker;
};

struct SampleWindow {
    int8_t samples[MAX_WINDOW_SAMPLES];
    uint8_t count = 0;
    void add(int8_t v) {
        if (count < MAX_WINDOW_SAMPLES) samples[count++] = v;
    }
};

WiFiClient espClient;
PubSubClient mqttClient(espClient);
Preferences prefs;
NimBLEScan* pBLEScan = nullptr;
QueueHandle_t scanQueue = nullptr;

// Identität (zur Laufzeit, aus NVS bzw. Provisionierung)
String sensor_id;
String sensor_name;
String state_topic;
String mesh_topic;
String availability_topic;
String provision_topic;

bool is_active_scan = true;
bool is_present = false;
bool live = false;             // Discovery/Identity publiziert, Messungen erlaubt
bool provision_seen = false;
bool standby = false;          // per Provisionierung abgemeldet: nichts publizieren, nur OTA/MQTT
bool ota_started = false;
uint32_t live_deadline_ms = 0;
uint32_t last_seen_ms = 0;
uint32_t last_absent_pub_ms = 0;
uint32_t window_start_ms = 0;
uint32_t last_mqtt_try_ms = 0;
uint32_t last_wifi_try_ms = 0;
uint32_t tag_sequence = 0;
uint32_t dropped_items = 0;
SampleWindow tag_window;
std::map<uint64_t, SampleWindow> mesh_windows;
std::map<uint64_t, uint32_t> mesh_sequences;

String target_mac_text = TARGET_MAC;
uint8_t target_addr[6];
bool target_valid = false;
std::vector<uint64_t> mesh_peers;
String local_ble_mac;
uint64_t local_key = 0;

// ---------------------------------------------------------------------------
// Hilfsfunktionen
// ---------------------------------------------------------------------------
uint64_t timestampSeconds() {
    const time_t now = time(nullptr);
    return now > 1000000000 ? static_cast<uint64_t>(now) : 0;
}

bool parseMac(const String& text, uint8_t out[6]) {
    uint8_t bytes[6];
    int n = 0;
    int nibble = -1;
    for (size_t i = 0; i < text.length() && n < 6; ++i) {
        char c = text[i];
        int v;
        if (c >= '0' && c <= '9') v = c - '0';
        else if (c >= 'a' && c <= 'f') v = c - 'a' + 10;
        else if (c >= 'A' && c <= 'F') v = c - 'A' + 10;
        else continue;
        if (nibble < 0) {
            nibble = v;
        } else {
            bytes[n++] = static_cast<uint8_t>((nibble << 4) | v);
            nibble = -1;
        }
    }
    if (n != 6) return false;
    for (int i = 0; i < 6; ++i) out[i] = bytes[5 - i];
    return true;
}

uint64_t macKey(const uint8_t a[6]) {
    uint64_t k = 0;
    for (int i = 0; i < 6; ++i) k |= static_cast<uint64_t>(a[i]) << (8 * i);
    return k;
}

String macString(uint64_t key) {
    char buf[18];
    uint8_t a[6];
    for (int i = 0; i < 6; ++i) a[i] = (key >> (8 * i)) & 0xFF;
    snprintf(buf, sizeof(buf), "%02x:%02x:%02x:%02x:%02x:%02x", a[5], a[4], a[3], a[2], a[1], a[0]);
    return String(buf);
}

String macCompact(const String& mac) {
    String out;
    for (size_t i = 0; i < mac.length(); ++i) {
        char c = mac[i];
        if (c == ':' || c == '-') continue;
        if (c >= 'A' && c <= 'F') c = c - 'A' + 'a';
        out += c;
    }
    return out;
}

bool validSensorId(const String& id) {
    if (id.length() == 0 || id.length() > 64) return false;
    for (size_t i = 0; i < id.length(); ++i) {
        char c = id[i];
        if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '_' || c == '-')) {
            return false;
        }
    }
    return true;
}

int medianOf(SampleWindow& w, int* outMin, int* outMax) {
    std::sort(w.samples, w.samples + w.count);
    *outMin = w.samples[0];
    *outMax = w.samples[w.count - 1];
    if (w.count % 2 == 1) return w.samples[w.count / 2];
    return (w.samples[w.count / 2 - 1] + w.samples[w.count / 2]) / 2;
}

void applyIdentity(const String& id, const String& name) {
    sensor_id = id;
    sensor_name = name.length() ? name : id;
    state_topic = "bluecat/" + sensor_id + "/sensor/state";
    mesh_topic = "bluecat/" + sensor_id + "/sensor/mesh";
    availability_topic = "bluecat/" + sensor_id + "/sensor/status";
    Serial.printf("Identität: %s (%s)\n", sensor_id.c_str(), sensor_name.c_str());
}

void setTargetMac(const String& text) {
    target_mac_text = text;
    target_mac_text.trim();
    target_mac_text.toLowerCase();
    target_valid = parseMac(target_mac_text, target_addr);
    Serial.printf("Ziel-MAC: %s (%s)\n", target_mac_text.c_str(), target_valid ? "gültig" : "leer/ungültig");
}

void addPeersFromList(const String& list) {
    int start = 0;
    while (start < (int)list.length()) {
        int end = list.indexOf(',', start);
        if (end < 0) end = list.length();
        uint8_t a[6];
        if (parseMac(list.substring(start, end), a)) {
            uint64_t key = macKey(a);
            if (key != local_key && std::find(mesh_peers.begin(), mesh_peers.end(), key) == mesh_peers.end()) {
                mesh_peers.push_back(key);
            }
        }
        start = end + 1;
    }
}

bool isPeer(uint64_t key) {
    return std::find(mesh_peers.begin(), mesh_peers.end(), key) != mesh_peers.end();
}

// ---------------------------------------------------------------------------
// BLE (läuft im NimBLE-Task – hier KEIN MQTT)
// ---------------------------------------------------------------------------
bool hasMeshMarker(NimBLEAdvertisedDevice* dev) {
    const std::string data = dev->getManufacturerData();
    const std::string marker(MESH_MARKER);
    const std::string prefix(MESH_PREFIX);
    if (data.size() >= 2 + marker.size() && static_cast<uint8_t>(data[0]) == 0xFF &&
        static_cast<uint8_t>(data[1]) == 0xFF && data.compare(2, marker.size(), marker) == 0) {
        return true;
    }
    if (data.compare(0, marker.size(), marker) == 0) return true;
    if (data.compare(0, prefix.size(), prefix) == 0) return true;
    const std::string name = dev->getName();
    return name.compare(0, prefix.size(), prefix) == 0;
}

class ScanCallbacks : public NimBLEAdvertisedDeviceCallbacks {
    void onResult(NimBLEAdvertisedDevice* dev) override {
        ScanItem item;
        memcpy(item.addr, dev->getAddress().getNative(), 6);
        item.rssi = static_cast<int8_t>(dev->getRSSI());
        item.meshMarker = MESH_ENABLED ? (hasMeshMarker(dev) ? 1 : 0) : 0;
        if (xQueueSend(scanQueue, &item, 0) != pdTRUE) {
            ++dropped_items;
        }
    }
};

void restartScan() {
    pBLEScan->stop();
    pBLEScan->clearResults();
    pBLEScan->setActiveScan(is_active_scan);
    pBLEScan->start(0, nullptr, false);
    Serial.printf("Scanner im Modus '%s' gestartet.\n", is_active_scan ? "active" : "passive");
}

// ---------------------------------------------------------------------------
// MQTT
// ---------------------------------------------------------------------------
void publishJson(const String& topic, JsonDocument& doc, bool retain) {
    char buffer[768];
    size_t len = serializeJson(doc, buffer, sizeof(buffer));
    mqttClient.publish(topic.c_str(), reinterpret_cast<const uint8_t*>(buffer), len, retain);
}

void publishTag(bool present, int rssi, int count, int rmin, int rmax) {
    if (!mqttClient.connected() || !live) return;
    JsonDocument doc;
    doc["message_type"] = "tag_rssi";
    doc["sensor_id"] = sensor_id;
    doc["present"] = present;
    doc["rssi"] = present ? rssi : OFFLINE_RSSI;
    doc["sample_count"] = count;
    if (present) {
        doc["rssi_min"] = rmin;
        doc["rssi_max"] = rmax;
    }
    doc["window_ms"] = WINDOW_MS;
    doc["timestamp"] = timestampSeconds();
    doc["uptime_ms"] = millis();
    doc["sequence"] = ++tag_sequence;
    publishJson(state_topic, doc, true);
}

void publishMesh(uint64_t key, int rssi, int count) {
    if (!mqttClient.connected() || !live) return;
    JsonDocument doc;
    doc["message_type"] = "sensor_beacon";
    doc["sensor_id"] = sensor_id;
    doc["beacon_mac"] = macString(key);
    doc["rssi"] = rssi;
    doc["sample_count"] = count;
    doc["timestamp"] = timestampSeconds();
    doc["sequence"] = ++mesh_sequences[key];
    publishJson(mesh_topic, doc, false);
}

void publishIdentity() {
    if (!mqttClient.connected() || local_ble_mac.isEmpty()) return;
    JsonDocument doc;
    doc["schema"] = 2;
    doc["sensor_id"] = sensor_id;
    doc["name"] = sensor_name;
    doc["implementation"] = "esp32";
    doc["state_topic"] = state_topic;
    doc["mesh_topic"] = mesh_topic;
    doc["availability_topic"] = availability_topic;
    doc["ble_mac"] = local_ble_mac;
    doc["ip"] = WiFi.localIP().toString();
    doc["version"] = FW_VERSION;
    doc["hostname"] = "bluecat-" + sensor_id;
    doc["enabled"] = true;
    publishJson("bluecat/registry/" + sensor_id + "/identity", doc, true);
}

void publishDiscovery() {
    JsonDocument device;
    device["identifiers"][0] = "bluecat_sensor_" + sensor_id;
    device["name"] = sensor_name;
    device["manufacturer"] = "Bluecat";
    device["model"] = "ESP32 BLE Sensor";
    device["sw_version"] = FW_VERSION;
    const String root = String(DISCOVERY_ROOT);

    // Frühere, vom Tracker angelegte RSSI/Präsenz-Discovery (gleiche unique_id) entfernen
    mqttClient.publish((root + "/sensor/bluecat_" + sensor_id + "/rssi/config").c_str(), "", true);
    mqttClient.publish((root + "/binary_sensor/bluecat_" + sensor_id + "/presence/config").c_str(), "", true);

    JsonDocument rssiDoc;
    rssiDoc["name"] = sensor_name + " RSSI";
    rssiDoc["unique_id"] = "bluecat_" + sensor_id + "_rssi";
    rssiDoc["state_topic"] = state_topic;
    rssiDoc["availability_topic"] = availability_topic;
    rssiDoc["value_template"] = "{{ value_json.rssi if value_json.present else 'None' }}";
    rssiDoc["unit_of_measurement"] = "dBm";
    rssiDoc["device_class"] = "signal_strength";
    rssiDoc["state_class"] = "measurement";
    rssiDoc["entity_category"] = "diagnostic";
    rssiDoc["device"] = device;
    publishJson(root + "/sensor/bluecat_" + sensor_id + "_rssi/config", rssiDoc, true);

    JsonDocument presenceDoc;
    presenceDoc["name"] = sensor_name + " Präsenz";
    presenceDoc["unique_id"] = "bluecat_" + sensor_id + "_presence";
    presenceDoc["state_topic"] = state_topic;
    presenceDoc["availability_topic"] = availability_topic;
    presenceDoc["value_template"] = "{{ 'ON' if value_json.present else 'OFF' }}";
    presenceDoc["payload_on"] = "ON";
    presenceDoc["payload_off"] = "OFF";
    presenceDoc["device_class"] = "presence";
    presenceDoc["entity_category"] = "diagnostic";
    presenceDoc["device"] = device;
    publishJson(root + "/binary_sensor/bluecat_" + sensor_id + "_presence/config", presenceDoc, true);

    JsonDocument switchDoc;
    switchDoc["name"] = sensor_name + " Aktives Scannen";
    switchDoc["unique_id"] = "bluecat_" + sensor_id + "_scan_mode";
    switchDoc["command_topic"] = "bluecat/" + sensor_id + "/switch/scan_mode/set";
    switchDoc["state_topic"] = "bluecat/" + sensor_id + "/switch/scan_mode/state";
    switchDoc["availability_topic"] = availability_topic;
    switchDoc["icon"] = "mdi:bluetooth-audio";
    switchDoc["entity_category"] = "config";
    switchDoc["device"] = device;
    publishJson(root + "/switch/bluecat_" + sensor_id + "_scan_mode/config", switchDoc, true);
}

void goLive() {
    live = true;
    Serial.printf("Anmeldung als %s\n", sensor_id.c_str());
    mqttClient.subscribe(("bluecat/" + sensor_id + "/switch/scan_mode/set").c_str());
    publishDiscovery();
    mqttClient.publish(availability_topic.c_str(), "online", true);
    publishIdentity();
    mqttClient.publish(("bluecat/" + sensor_id + "/switch/scan_mode/state").c_str(), is_active_scan ? "ON" : "OFF", true);
    if (!is_present) {
        publishTag(false, OFFLINE_RSSI, 0, 0, 0);
        last_absent_pub_ms = millis();
    }
}

void handleProvision(const String& payload) {
    provision_seen = true;
    JsonDocument doc;
    if (deserializeJson(doc, payload) || !doc.is<JsonObject>()) return;
    const bool enabled = doc["enabled"] | true;
    if (!enabled) {
        if (!standby) {
            Serial.println("Abgemeldet (Standby) – keine Messungen mehr, OTA bleibt aktiv.");
            standby = true;
            live = false;
        }
        return;
    }
    if (standby) {
        Serial.println("Wieder angemeldet – Neustart.");
        mqttClient.disconnect();
        delay(200);
        ESP.restart();
        return;
    }
    String id = String(doc["sensor_id"] | "");
    String name = String(doc["name"] | "");
    if (!validSensorId(id)) {
        Serial.println("Provisionierung ignoriert: ungültige sensor_id");
        return;
    }
    if (id == sensor_id && (name.isEmpty() || name == sensor_name)) return;
    prefs.putString("sensor_id", id);
    prefs.putString("name", name);
    if (id != sensor_id) {
        // Neue ID: Topics, LWT, Hostname und OTA-Name ändern sich → sauber neu starten.
        Serial.printf("Neue Sensor-ID %s gespeichert – Neustart.\n", id.c_str());
        // Retained Spuren der alten ID entfernen (sonst bleibt ein Phantom-Sensor)
        mqttClient.publish(("bluecat/registry/" + sensor_id + "/identity").c_str(), "", true);
        mqttClient.publish(state_topic.c_str(), "", true);
        mqttClient.publish(availability_topic.c_str(), "", true);
        mqttClient.disconnect();
        delay(200);
        ESP.restart();
        return;
    }
    applyIdentity(id, name);  // nur der Name hat sich geändert
    if (live) {
        publishDiscovery();
        publishIdentity();
    }
}

void mqttCallback(char* topic, byte* payload, unsigned int length) {
    String t(topic);
    String p;
    p.reserve(length);
    for (unsigned int i = 0; i < length; ++i) p += static_cast<char>(payload[i]);

    if (t == provision_topic) {
        handleProvision(p);
        return;
    }
    if (t == "bluecat/config/target_mac/state") {
        JsonDocument doc;
        if (!deserializeJson(doc, p) && doc.is<JsonObject>()) {
            const char* v = doc["target_mac"] | (doc["mac"] | "");
            setTargetMac(String(v));
        } else {
            setTargetMac(p);
        }
        return;
    }
    if (t == "bluecat/registry/mesh_peers") {
        JsonDocument doc;
        if (deserializeJson(doc, p) || !doc["peers"].is<JsonArray>()) return;
        mesh_peers.clear();
        addPeersFromList(String(MESH_PEER_MACS));
        for (JsonObject peer : doc["peers"].as<JsonArray>()) {
            addPeersFromList(String(peer["ble_mac"] | ""));
        }
        Serial.printf("Mesh-Peers: %u\n", (unsigned)mesh_peers.size());
        return;
    }
    if (t == "bluecat/" + sensor_id + "/switch/scan_mode/set") {
        const bool newMode = p == "ON";
        if (newMode != is_active_scan) {
            is_active_scan = newMode;
            restartScan();
        }
        mqttClient.publish(("bluecat/" + sensor_id + "/switch/scan_mode/state").c_str(), is_active_scan ? "ON" : "OFF", true);
    }
}

void tryConnectMqtt() {
    const uint32_t now = millis();
    if (mqttClient.connected() || WiFi.status() != WL_CONNECTED) return;
    if (last_mqtt_try_ms != 0 && now - last_mqtt_try_ms < MQTT_RETRY_MS) return;
    last_mqtt_try_ms = now;
    Serial.print("Verbinde zu MQTT Broker...");
    const String clientId = "bluecat_" + macCompact(local_ble_mac);
    if (!mqttClient.connect(clientId.c_str(), MQTT_USER, MQTT_PASSWORD, availability_topic.c_str(), 1, true, "offline")) {
        Serial.printf(" Fehler rc=%d\n", mqttClient.state());
        return;
    }
    Serial.println(" verbunden.");
    live = false;
    provision_seen = false;
    live_deadline_ms = millis() + PROVISION_WAIT_MS;
    mqttClient.subscribe(provision_topic.c_str());
    mqttClient.subscribe("bluecat/registry/mesh_peers");
    mqttClient.subscribe("bluecat/config/target_mac/state");
}

// ---------------------------------------------------------------------------
// Setup & Loop
// ---------------------------------------------------------------------------
void setupOta() {
    if (ota_started || WiFi.status() != WL_CONNECTED) return;
    ArduinoOTA.setHostname(("bluecat-" + sensor_id).c_str());
    if (strlen(OTA_PASSWORD) > 0) ArduinoOTA.setPassword(OTA_PASSWORD);
    ArduinoOTA.onStart([]() {
        Serial.println("OTA-Update startet – BLE-Scan wird angehalten.");
        if (pBLEScan) pBLEScan->stop();
        if (mqttClient.connected()) {
            mqttClient.publish(availability_topic.c_str(), "offline", true);
            mqttClient.disconnect();
        }
    });
    ArduinoOTA.onError([](ota_error_t error) {
        Serial.printf("OTA-Fehler %u – Neustart.\n", error);
        ESP.restart();
    });
    ArduinoOTA.begin();
    ota_started = true;
    Serial.printf("OTA bereit: bluecat-%s (%s)\n", sensor_id.c_str(), WiFi.localIP().toString().c_str());
}

void setupWiFi() {
    Serial.printf("Verbinde mit WiFi %s ", WIFI_SSID);
    WiFi.mode(WIFI_STA);
    WiFi.setAutoReconnect(true);
    WiFi.setHostname(("bluecat-" + sensor_id).c_str());
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    const uint32_t start = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - start < 20000) {
        delay(500);
        Serial.print(".");
    }
    Serial.println(WiFi.status() == WL_CONNECTED ? "\nWiFi verbunden." : "\nWiFi noch nicht verbunden – weiter im Loop.");
    configTime(0, 0, "pool.ntp.org", "time.nist.gov");
}

void setup() {
    Serial.begin(115200);
    delay(10);
    Serial.printf("\nStarte Bluecat BLE-Sensor (ESP32) %s\n", FW_VERSION);

    esp_bt_controller_disable();
    delay(50);
    esp_bt_controller_deinit();
    delay(50);

    scanQueue = xQueueCreate(64, sizeof(ScanItem));
    NimBLEDevice::init("bluecat");
    local_ble_mac = NimBLEDevice::getAddress().toString().c_str();
    local_ble_mac.toLowerCase();
    uint8_t own[6];
    if (parseMac(local_ble_mac, own)) local_key = macKey(own);
    provision_topic = "bluecat/provision/" + macCompact(local_ble_mac);

    prefs.begin("bluecat", false);
    String id = prefs.getString("sensor_id", DEFAULT_SENSOR_ID);
    String name = prefs.getString("name", DEFAULT_SENSOR_NAME);
    if (!validSensorId(id)) {
        String compact = macCompact(local_ble_mac);
        id = "esp32_" + compact.substring(compact.length() - 6);
    }
    applyIdentity(id, name);
    Serial.printf("BLE-MAC %s – Provisionierungs-Topic %s\n", local_ble_mac.c_str(), provision_topic.c_str());

    setTargetMac(String(TARGET_MAC));
    addPeersFromList(String(MESH_PEER_MACS));

    setupWiFi();
    setupOta();
    mqttClient.setServer(MQTT_BROKER, MQTT_PORT);
    mqttClient.setCallback(mqttCallback);
    mqttClient.setBufferSize(1024);

#if MESH_ENABLED
    NimBLEAdvertising* adv = NimBLEDevice::getAdvertising();
    NimBLEAdvertisementData data;
    std::string manufacturer;
    manufacturer.push_back(static_cast<char>(MARKER_COMPANY_ID & 0xFF));
    manufacturer.push_back(static_cast<char>(MARKER_COMPANY_ID >> 8));
    manufacturer += MESH_MARKER;
    data.setManufacturerData(manufacturer);
    adv->setAdvertisementData(data);
    adv->start();
#endif

    pBLEScan = NimBLEDevice::getScan();
    pBLEScan->setAdvertisedDeviceCallbacks(new ScanCallbacks(), true);
    pBLEScan->setMaxResults(0);         // keine Ergebnisliste speichern (Heap!)
    pBLEScan->setDuplicateFilter(false);
    pBLEScan->setInterval(100);
    pBLEScan->setWindow(99);
    restartScan();
    window_start_ms = millis();
}

void drainQueue() {
    ScanItem item;
    while (xQueueReceive(scanQueue, &item, 0) == pdTRUE) {
        if (target_valid && memcmp(item.addr, target_addr, 6) == 0) {
            tag_window.add(item.rssi);
            continue;
        }
#if MESH_ENABLED
        const uint64_t key = macKey(item.addr);
        if (key != local_key && (item.meshMarker || isPeer(key))) {
            if (mesh_windows.size() < 32 || mesh_windows.count(key)) {
                mesh_windows[key].add(item.rssi);
            }
        }
#endif
    }
}

void flushWindow(uint32_t now) {
    if (tag_window.count > 0) {
        int rmin, rmax;
        const uint8_t count = tag_window.count;
        const int median = medianOf(tag_window, &rmin, &rmax);
        tag_window.count = 0;
        is_present = true;
        last_seen_ms = now;
        publishTag(true, median, count, rmin, rmax);
    } else if (now - last_seen_ms >= ABSENT_TIMEOUT_MS || last_seen_ms == 0) {
        if (is_present || now - last_absent_pub_ms >= ABSENT_HEARTBEAT_MS) {
            is_present = false;
            last_absent_pub_ms = now;
            publishTag(false, OFFLINE_RSSI, 0, 0, 0);
        }
    }
    for (auto& entry : mesh_windows) {
        if (entry.second.count == 0) continue;
        int rmin, rmax;
        const uint8_t count = entry.second.count;
        const int median = medianOf(entry.second, &rmin, &rmax);
        entry.second.count = 0;
        publishMesh(entry.first, median, count);
    }
    if (dropped_items) {
        Serial.printf("Warnung: %lu Scan-Ergebnisse verworfen (Queue voll)\n", (unsigned long)dropped_items);
        dropped_items = 0;
    }
}

void loop() {
    const uint32_t now = millis();
    if (WiFi.status() != WL_CONNECTED && now - last_wifi_try_ms > WIFI_RETRY_MS) {
        last_wifi_try_ms = now;
        WiFi.reconnect();
    }
    setupOta();
    if (ota_started) ArduinoOTA.handle();
    tryConnectMqtt();
    mqttClient.loop();
    if (mqttClient.connected() && !live && !standby && (provision_seen || (int32_t)(millis() - live_deadline_ms) >= 0)) {
        goLive();
    }
    drainQueue();
    if (now - window_start_ms >= WINDOW_MS) {
        window_start_ms = now;
        flushWindow(now);
    }
    delay(5);
}
