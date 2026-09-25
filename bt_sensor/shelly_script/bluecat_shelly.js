// Bluecat BLE-Sensor fuer Shelly (Gen2+, Firmware >= 2.0) - Protokoll v2
//
// EIN Skript fuer alle Shellys. Geraetespezifisches steht im Shelly-KVS und wird
// von deploy/bluecat_deploy.py gesetzt:
//   bluecat.sensor_id   z. B. "shelly_wohnzimmer"      (sonst aus dem Geraetenamen)
//   bluecat.name        z. B. "Shelly Wohnzimmer"      (sonst Geraetename)
//   bluecat.legacy_id   alte Discovery-ID zum Aufraeumen (optional)
//   bluecat.ble_mac     nur falls WLAN-MAC + 2 nicht stimmt (optional)
//   bluecat.target_mac  Offline-Fallback fuer die Halsband-MAC (optional)
//
// Verhalten
// * Sichtungen des Halsbands in 2-s-Fenstern -> Median + Anzahl (retained)
// * 10 s ohne Sichtung -> "present": false, danach alle 15 s; direkt nach dem
//   MQTT-Connect ebenfalls
// * Mesh-Messungen auf .../sensor/mesh, Identity einmal beim Connect
// * BLE-MAC = WLAN-MAC + 2 (ESP32-Basisadresse)

let SCRIPT_VERSION = "2.1.0";

let window_ms = 2000;
let absent_timeout_ms = 10000;
let absent_heartbeat_ms = 15000;
let offline_rssi = -130;
let mesh_advertising_enabled = true;
let mesh_advertising_interval_ms = 8000;
// Flags + Manufacturer Data (Company-ID 0xFFFF) + "TRILOLA"
let mesh_adv_data = "0201060affffff5452494c4f4c41";

// ---- Zustand ---------------------------------------------------------------
let cfg = {};
let sensor_id = "";
let sensor_name = "";
let legacy_sensor_id = "";
let sensor_ble_mac = "";
let target_mac = "";
let mesh_peer_macs = [];
let state_topic = "";
let mesh_topic = "";
let availability_topic = "";
let identity_topic = "";
let mesh_peers_topic = "bluecat/registry/mesh_peers";
let target_mac_state_topic = "bluecat/config/target_mac/state";

let sequence = 0;
let mesh_sequences = {};
let fast_mac_lookup = {};
let tag_batch = [];
let mesh_batches = {};
let last_seen_ms = 0;
let last_absent_pub_ms = 0;
let is_present = false;
let subscribed = false;

// ---- Hilfsfunktionen -------------------------------------------------------
function sanitize(value) {
  let str = String(value || "shelly_sensor").toLowerCase();
  let result = "";
  for (let i = 0; i < str.length; i++) {
    let c = str[i];
    if ((c >= "a" && c <= "z") || (c >= "0" && c <= "9") || c === "_" || c === "-") result += c;
    else result += "_";
  }
  return result;
}

function hex_only(value) {
  let str = String(value || "").toLowerCase();
  let out = "";
  for (let i = 0; i < str.length; i++) {
    let c = str[i];
    if ((c >= "0" && c <= "9") || (c >= "a" && c <= "f")) out += c;
  }
  return out;
}

function normalize_mac(value) {
  let h = hex_only(value);
  if (h.length !== 12) return "";
  let out = "";
  for (let i = 0; i < 12; i += 2) {
    if (i > 0) out += ":";
    out += h.slice(i, i + 2);
  }
  return out;
}

function hex2(n) {
  let s = n.toString(16);
  return s.length < 2 ? "0" + s : s;
}

function ble_mac_from_wifi(wifi_mac) {
  let h = hex_only(wifi_mac);
  if (h.length !== 12) return "";
  let bytes = [];
  for (let i = 0; i < 12; i += 2) bytes.push(parseInt(h.slice(i, i + 2), 16));
  let carry = 2;
  for (let j = 5; j >= 0 && carry > 0; j--) {
    let v = bytes[j] + carry;
    bytes[j] = v & 255;
    carry = v >> 8;
  }
  let out = "";
  for (let k = 0; k < 6; k++) {
    if (k > 0) out += ":";
    out += hex2(bytes[k]);
  }
  return out;
}

function median(arr) {
  let len = arr.length;
  for (let i = 0; i < len; i++) {
    for (let j = 0; j < len - i - 1; j++) {
      if (arr[j] > arr[j + 1]) {
        let t = arr[j];
        arr[j] = arr[j + 1];
        arr[j + 1] = t;
      }
    }
  }
  let mid = Math.floor(len / 2);
  return (len % 2 === 0) ? (arr[mid - 1] + arr[mid]) / 2.0 : arr[mid];
}

function current_ip() {
  let w = Shelly.getComponentStatus("wifi");
  return (w && w.sta_ip) ? w.sta_ip : "";
}

function rebuild_mac_lookup() {
  let lookup = {};
  for (let i = 0; i < mesh_peer_macs.length; i++) {
    let m = normalize_mac(mesh_peer_macs[i]);
    if (m && m !== sensor_ble_mac) lookup[m] = "mesh";
  }
  let t = normalize_mac(target_mac);
  if (t) lookup[t] = "target";
  fast_mac_lookup = lookup;
  print(sensor_name + ": Filterliste (Ziel " + (t || "leer") + ", " + mesh_peer_macs.length + " Peers)");
}

// ---- MQTT ------------------------------------------------------------------
function publish_tag(present, rssi, count, rmin, rmax) {
  if (!sensor_id || !MQTT.isConnected()) return;
  sequence += 1;
  let msg = {
    message_type: "tag_rssi", sensor_id: sensor_id, present: present,
    rssi: present ? rssi : offline_rssi, sample_count: count, window_ms: window_ms,
    timestamp: Math.floor(Date.now() / 1000), sequence: sequence
  };
  if (present) {
    msg.rssi_min = rmin;
    msg.rssi_max = rmax;
  }
  MQTT.publish(state_topic, JSON.stringify(msg), 1, true);
}

function publish_mesh(mac, rssi, count) {
  if (!sensor_id || !MQTT.isConnected()) return;
  mesh_sequences[mac] = (mesh_sequences[mac] || 0) + 1;
  MQTT.publish(mesh_topic, JSON.stringify({
    message_type: "sensor_beacon", sensor_id: sensor_id, beacon_mac: mac,
    rssi: rssi, sample_count: count, timestamp: Math.floor(Date.now() / 1000),
    sequence: mesh_sequences[mac]
  }), 0, false);
}

function publish_identity() {
  if (!sensor_ble_mac) return;
  let info = Shelly.getDeviceInfo();
  MQTT.publish(identity_topic, JSON.stringify({
    schema: 2, sensor_id: sensor_id, name: sensor_name, implementation: "shelly",
    state_topic: state_topic, mesh_topic: mesh_topic, availability_topic: availability_topic,
    ble_mac: sensor_ble_mac, ip: current_ip(), version: SCRIPT_VERSION,
    device_fw: info.ver || "", device_model: info.model || "", enabled: true
  }), 1, true);
}

function publish_discovery() {
  let info = Shelly.getDeviceInfo();
  let device = {
    identifiers: ["bluecat_sensor_" + sensor_id], name: sensor_name, manufacturer: "Bluecat",
    model: "Shelly BLE Sensor (" + (info.model || "Shelly") + ")", sw_version: SCRIPT_VERSION
  };
  let ip = current_ip();
  if (ip) device.configuration_url = "http://" + ip;
  MQTT.publish("homeassistant/sensor/bluecat_" + sensor_id + "_rssi/config", JSON.stringify({
    name: sensor_name + " RSSI", unique_id: "bluecat_" + sensor_id + "_rssi",
    state_topic: state_topic, availability_topic: availability_topic,
    value_template: "{{ value_json.rssi if value_json.present else 'None' }}",
    unit_of_measurement: "dBm", device_class: "signal_strength", state_class: "measurement",
    entity_category: "diagnostic", device: device
  }), 1, true);
  MQTT.publish("homeassistant/binary_sensor/bluecat_" + sensor_id + "_presence/config", JSON.stringify({
    name: sensor_name + " Praesenz", unique_id: "bluecat_" + sensor_id + "_presence",
    state_topic: state_topic, availability_topic: availability_topic,
    value_template: "{{ 'ON' if value_json.present else 'OFF' }}",
    payload_on: "ON", payload_off: "OFF", device_class: "presence",
    entity_category: "diagnostic", device: device
  }), 1, true);
}

function update_runtime_config(topic, message) {
  if (topic === target_mac_state_topic) {
    let value = String(message || "");
    try {
      let parsed = JSON.parse(message);
      if (parsed && typeof parsed === "object") value = String(parsed.target_mac || parsed.mac || "");
    } catch (e) { }
    target_mac = value;
    rebuild_mac_lookup();
    return;
  }
  if (topic === mesh_peers_topic) {
    try {
      let data = JSON.parse(message);
      if (data && Array.isArray(data.peers)) {
        let peers = [];
        for (let i = 0; i < data.peers.length; i++) {
          if (data.peers[i] && data.peers[i].ble_mac) peers.push(data.peers[i].ble_mac);
        }
        mesh_peer_macs = peers;
        rebuild_mac_lookup();
      }
    } catch (e) { }
  }
}

function on_mqtt_connect() {
  if (!sensor_id) return;  // Konfiguration noch nicht geladen
  print(sensor_name + ": MQTT verbunden (BLE-MAC " + sensor_ble_mac + ", IP " + current_ip() + ")");
  // Fruehere, vom Tracker angelegte RSSI/Praesenz-Discovery (gleiche unique_id) entfernen
  MQTT.publish("homeassistant/sensor/bluecat_" + sensor_id + "/rssi/config", "", 1, true);
  MQTT.publish("homeassistant/binary_sensor/bluecat_" + sensor_id + "/presence/config", "", 1, true);
  if (legacy_sensor_id && legacy_sensor_id !== sensor_id) {
    MQTT.publish("homeassistant/sensor/bluecat_" + legacy_sensor_id + "_rssi/config", "", 1, true);
    MQTT.publish("homeassistant/binary_sensor/bluecat_" + legacy_sensor_id + "_presence/config", "", 1, true);
  }
  MQTT.publish(availability_topic, "online", 1, true);
  if (!subscribed) {
    MQTT.subscribe(mesh_peers_topic, update_runtime_config);
    MQTT.subscribe(target_mac_state_topic, update_runtime_config);
    subscribed = true;
  }
  publish_discovery();
  publish_identity();
  if (!is_present) {
    publish_tag(false, offline_rssi, 0, 0, 0);
    last_absent_pub_ms = Date.now();
  }
}

// ---- Fenster-Takt ----------------------------------------------------------
function flush_window() {
  let now = Date.now();
  if (tag_batch.length > 0) {
    let batch = tag_batch;
    tag_batch = [];
    let count = batch.length;
    let m = median(batch);
    is_present = true;
    last_seen_ms = now;
    publish_tag(true, m, count, batch[0], batch[count - 1]);
  } else if (now - last_seen_ms >= absent_timeout_ms) {
    if (is_present || now - last_absent_pub_ms >= absent_heartbeat_ms) {
      if (is_present) print(sensor_name + ": Ziel nicht mehr gesehen");
      is_present = false;
      last_absent_pub_ms = now;
      publish_tag(false, offline_rssi, 0, 0, 0);
    }
  }
  let macs = Object.keys(mesh_batches);
  for (let i = 0; i < macs.length; i++) {
    let values = mesh_batches[macs[i]];
    if (values.length > 0) publish_mesh(macs[i], median(values), values.length);
  }
  mesh_batches = {};
}

function advertise_mesh_once() {
  if (!mesh_advertising_enabled) return;
  Shelly.call("BLE.AdvertiseOnce", { adv_data: mesh_adv_data }, function (result, error_code, error_message) {
    if (error_code) print("BLE.AdvertiseOnce Fehler " + error_code + ": " + error_message);
  });
}

// ---- Start -----------------------------------------------------------------
function kvs_items_to_cfg(res) {
  let out = {};
  if (!res || !res.items) return out;
  if (Array.isArray(res.items)) {          // neuere Firmware: [{key, value, etag}]
    for (let i = 0; i < res.items.length; i++) out[res.items[i].key] = res.items[i].value;
  } else {                                 // aeltere Firmware: {key: {value, etag}}
    let keys = Object.keys(res.items);
    for (let j = 0; j < keys.length; j++) out[keys[j]] = res.items[keys[j]].value;
  }
  return out;
}

function start(config) {
  cfg = config;
  let info = Shelly.getDeviceInfo();
  sensor_id = sanitize(cfg["bluecat.sensor_id"] || info.name || info.id);
  sensor_name = cfg["bluecat.name"] || info.name || ("Shelly " + sensor_id);
  legacy_sensor_id = cfg["bluecat.legacy_id"] || "";
  sensor_ble_mac = normalize_mac(cfg["bluecat.ble_mac"]) || ble_mac_from_wifi(info.mac);
  target_mac = cfg["bluecat.target_mac"] || "";
  state_topic = "bluecat/" + sensor_id + "/sensor/state";
  mesh_topic = "bluecat/" + sensor_id + "/sensor/mesh";
  availability_topic = "bluecat/" + sensor_id + "/sensor/status";
  identity_topic = "bluecat/registry/" + sensor_id + "/identity";
  print("Bluecat " + SCRIPT_VERSION + ": " + sensor_id + " (" + sensor_name + ")");
  rebuild_mac_lookup();

  MQTT.setConnectHandler(on_mqtt_connect);
  if (MQTT.isConnected()) on_mqtt_connect();
  Timer.set(window_ms, true, flush_window);
  Timer.set(mesh_advertising_interval_ms, true, advertise_mesh_once);
  advertise_mesh_once();

  BLE.Scanner.Start({ duration_ms: BLE.Scanner.INFINITE_SCAN, active: false });
  BLE.Scanner.Subscribe(function (ev, res) {
    if (ev !== BLE.Scanner.SCAN_RESULT) return;
    let mac = String(res.addr).toLowerCase();
    let role = fast_mac_lookup[mac];
    if (!role) return;
    if (role === "target") {
      tag_batch.push(res.rssi);
    } else {
      if (!mesh_batches[mac]) mesh_batches[mac] = [];
      if (mesh_batches[mac].length < 16) mesh_batches[mac].push(res.rssi);
    }
  });
}

Shelly.call("KVS.GetMany", { match: "bluecat.*" }, function (res, error_code) {
  start(error_code ? {} : kvs_items_to_cfg(res));
});
