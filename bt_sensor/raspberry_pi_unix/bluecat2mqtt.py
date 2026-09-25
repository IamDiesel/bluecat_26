"""BLE-to-MQTT-Sensor für Raspberry Pi/BlueZ (Protokoll v2).

* Sichtungen des Halsbands werden in 2-s-Fenstern gesammelt und als Median
  + Anzahl retained auf ``STATE_TOPIC`` publiziert.
* Nach ``ABSENT_TIMEOUT_SEC`` ohne Sichtung: ``present: false`` (danach als
  Heartbeat alle ``ABSENT_HEARTBEAT_SEC``); direkt nach dem MQTT-Connect
  ebenfalls, damit Home Assistant sofort einen Zustand hat.
* Mesh-Messungen anderer Sensoren gehen auf ``MESH_TOPIC``.
* Identity wird einmal retained beim Connect publiziert.

Optionale Werte aus ``secrets_blue.py``: SENSOR_ID, SENSOR_NAME, STATE_TOPIC,
MESH_TOPIC, AVAILABILITY_TOPIC, TARGET_MAC, MESH_ENABLED, MESH_PEER_MACS,
MESH_ADVERTISING_ENABLED, MESH_ADAPTER, WINDOW_SEC, ABSENT_TIMEOUT_SEC,
ABSENT_HEARTBEAT_SEC.
"""

import asyncio
import json
import re
import socket
import statistics
import subprocess
import time

import paho.mqtt.client as mqtt
from bleak import BleakScanner

try:
    import secrets_blue as sec
except SyntaxError as error:
    raise SystemExit(
        "secrets_blue.py enthält einen Syntaxfehler. Bitte secrets_blue.example.py kopieren und anpassen."
    ) from error

try:
    from dbus_next import Variant
    from dbus_next.aio import MessageBus
    from dbus_next.constants import BusType, PropertyAccess
    from dbus_next.service import ServiceInterface, dbus_property, method

    DBUS_ADVERTISING_AVAILABLE = True
except ImportError:
    DBUS_ADVERTISING_AVAILABLE = False


SENSOR_VERSION = "2.1.0"
SENSOR_ID = str(getattr(sec, "SENSOR_ID", "ron"))
SENSOR_NAME = str(getattr(sec, "SENSOR_NAME", "Ron Raspberry Pi"))
STATE_TOPIC = str(getattr(sec, "STATE_TOPIC", f"bluecat/{SENSOR_ID}/sensor/state"))
MESH_TOPIC = str(getattr(sec, "MESH_TOPIC", STATE_TOPIC.rsplit("/", 1)[0] + "/mesh"))
AVAILABILITY_TOPIC = str(getattr(sec, "AVAILABILITY_TOPIC", f"bluecat/{SENSOR_ID}/sensor/status"))
MESH_ENABLED = bool(getattr(sec, "MESH_ENABLED", True))
MESH_ADVERTISING_ENABLED = bool(getattr(sec, "MESH_ADVERTISING_ENABLED", True))
MESH_PREFIX = str(getattr(sec, "MESH_PREFIX", "TRILOLA_SENSOR:"))
MESH_MARKER = str(getattr(sec, "MESH_MARKER", "TRILOLA"))
MESH_ADAPTER = str(getattr(sec, "MESH_ADAPTER", "hci0"))
MARKER_COMPANY_ID = 0xFFFF
# Ältere ESP32-Firmware sendete "TRILOLA" ohne Company-ID → Bleak liest "TR"
# als Company-ID 0x5254 und den Rest als Nutzdaten.
LEGACY_ESP_COMPANY_ID = int.from_bytes(MESH_MARKER.encode("utf-8")[:2], "little")
LEGACY_ESP_PAYLOAD = MESH_MARKER.encode("utf-8")[2:]

WINDOW_SEC = float(getattr(sec, "WINDOW_SEC", getattr(sec, "MIN_PUBLISH_INTERVAL", 2.0)))
ABSENT_TIMEOUT_SEC = float(getattr(sec, "ABSENT_TIMEOUT_SEC", 10.0))
ABSENT_HEARTBEAT_SEC = float(getattr(sec, "ABSENT_HEARTBEAT_SEC", 15.0))
OFFLINE_RSSI = -130

REGISTRY_IDENTITY_TOPIC = f"bluecat/registry/{SENSOR_ID}/identity"
MESH_PEERS_TOPIC = "bluecat/registry/mesh_peers"
TARGET_MAC_STATE_TOPIC = "bluecat/config/target_mac/state"
SCAN_MODE_CMD_TOPIC = f"bluecat/{SENSOR_ID}/switch/scan_mode/set"
SCAN_MODE_STATE_TOPIC = f"bluecat/{SENSOR_ID}/switch/scan_mode/state"
DISCOVERY_TOPIC_SENSOR = f"homeassistant/sensor/bluecat_{SENSOR_ID}_rssi/config"
DISCOVERY_TOPIC_PRESENCE = f"homeassistant/binary_sensor/bluecat_{SENSOR_ID}_presence/config"
DISCOVERY_TOPIC_SWITCH = f"homeassistant/switch/bluecat_{SENSOR_ID}_scan_mode/config"


def normalize_address(value):
    return re.sub(r"[^0-9a-f]", "", str(value or "").lower())


def _configured_peers():
    peers = getattr(sec, "MESH_PEER_MACS", [])
    if isinstance(peers, str):
        peers = [p for p in peers.split(",")]
    return {normalize_address(p) for p in peers if normalize_address(p)}


TARGET_MAC = normalize_address(getattr(sec, "TARGET_MAC", ""))
LOCAL_BLE_MAC = ""
MESH_PEER_MACS = _configured_peers()

# Laufzeitzustand (nur im asyncio-Thread verändert)
tag_window = []
mesh_windows = {}
last_seen_time = 0.0
last_absent_pub = 0.0
is_present = False
sequence = 0
mesh_sequences = {}

scanner = None
current_scan_mode = "active"
main_loop = None
mesh_bus = None
mesh_manager = None
mesh_advertisement = None
mesh_advertisement_path = "/com/bluecat/trilola/advertisement"

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"bluecat_{SENSOR_ID}_scanner")
if getattr(sec, "MQTT_USER", ""):
    client.username_pw_set(sec.MQTT_USER, getattr(sec, "MQTT_PASSWORD", "") or None)
client.will_set(AVAILABILITY_TOPIC, payload="offline", qos=1, retain=True)


if DBUS_ADVERTISING_AVAILABLE:
    class MeshAdvertisement(ServiceInterface):
        """BlueZ-Advertisement mit Company-ID 0xFFFF + TriLola-Marker."""

        def __init__(self):
            super().__init__("org.bluez.LEAdvertisement1")
            self._manufacturer_data = {MARKER_COMPANY_ID: Variant("ay", MESH_MARKER.encode("utf-8"))}

        @dbus_property(access=PropertyAccess.READ)
        def Type(self) -> "s":
            return "peripheral"

        @dbus_property(access=PropertyAccess.READ)
        def ManufacturerData(self) -> "a{qv}":
            return self._manufacturer_data

        @method()
        def Release(self):
            return

HA_DEVICE_CONFIG = {
    "identifiers": [f"bluecat_sensor_{SENSOR_ID}"],
    "name": SENSOR_NAME,
    "manufacturer": "Bluecat",
    "model": "Raspberry Pi BLE Sensor",
    "sw_version": SENSOR_VERSION,
}


def now_timestamp():
    return int(time.time())


def get_local_bluetooth_mac():
    """Ermittelt die Controller-MAC unabhängig von einer Python-venv."""
    try:
        result = subprocess.run(["bluetoothctl", "show"], capture_output=True, text=True, timeout=5, check=False)
        match = re.search(r"Controller\s+([0-9A-Fa-f:]{17})", result.stdout)
        if match:
            return match.group(1).lower()
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def get_local_ip():
    """IP der Schnittstelle, über die der Broker erreicht wird."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect((str(sec.MQTT_BROKER), int(sec.MQTT_PORT)))
            return probe.getsockname()[0]
    except OSError:
        return ""


def publish_json(topic, payload, retain=False):
    if client.is_connected():
        client.publish(topic, json.dumps(payload), qos=1, retain=retain)


def publish_identity():
    if not LOCAL_BLE_MAC:
        print("Warnung: lokale BLE-MAC unbekannt – Identity wird nicht gesendet.")
        return
    publish_json(REGISTRY_IDENTITY_TOPIC, {
        "schema": 2,
        "sensor_id": SENSOR_ID,
        "name": SENSOR_NAME,
        "implementation": "raspberry_pi",
        "state_topic": STATE_TOPIC,
        "mesh_topic": MESH_TOPIC,
        "availability_topic": AVAILABILITY_TOPIC,
        "ble_mac": LOCAL_BLE_MAC,
        "ip": get_local_ip(),
        "version": SENSOR_VERSION,
        "hostname": socket.gethostname(),
        "enabled": True,
    }, retain=True)


def publish_tag(present, rssi=OFFLINE_RSSI, count=0, rssi_min=None, rssi_max=None):
    global sequence
    sequence += 1
    payload = {
        "message_type": "tag_rssi",
        "sensor_id": SENSOR_ID,
        "present": bool(present),
        "rssi": rssi if present else OFFLINE_RSSI,
        "sample_count": int(count),
        "window_ms": int(WINDOW_SEC * 1000),
        "timestamp": now_timestamp(),
        "sequence": sequence,
    }
    if present:
        payload["rssi_min"] = rssi_min
        payload["rssi_max"] = rssi_max
    publish_json(STATE_TOPIC, payload, retain=True)


def publish_mesh(beacon_mac, rssi, count, legacy_sensor_id=None):
    mesh_sequences[beacon_mac] = mesh_sequences.get(beacon_mac, 0) + 1
    payload = {
        "message_type": "sensor_beacon",
        "sensor_id": SENSOR_ID,
        "beacon_mac": beacon_mac,
        "rssi": rssi,
        "sample_count": count,
        "timestamp": now_timestamp(),
        "sequence": mesh_sequences[beacon_mac],
    }
    if legacy_sensor_id and legacy_sensor_id != SENSOR_ID:
        payload["beacon_sensor"] = legacy_sensor_id
    publish_json(MESH_TOPIC, payload)


def publish_discovery_messages():
    publish_json(DISCOVERY_TOPIC_SENSOR, {
        "name": f"{SENSOR_NAME} RSSI",
        "unique_id": f"bluecat_{SENSOR_ID}_rssi",
        "state_topic": STATE_TOPIC,
        "availability_topic": AVAILABILITY_TOPIC,
        "value_template": "{{ value_json.rssi if value_json.present else 'None' }}",
        "unit_of_measurement": "dBm",
        "device_class": "signal_strength",
        "state_class": "measurement",
        "entity_category": "diagnostic",
        "device": HA_DEVICE_CONFIG,
    }, retain=True)
    publish_json(DISCOVERY_TOPIC_PRESENCE, {
        "name": f"{SENSOR_NAME} Präsenz",
        "unique_id": f"bluecat_{SENSOR_ID}_presence",
        "state_topic": STATE_TOPIC,
        "availability_topic": AVAILABILITY_TOPIC,
        "value_template": "{{ 'ON' if value_json.present else 'OFF' }}",
        "payload_on": "ON",
        "payload_off": "OFF",
        "device_class": "presence",
        "entity_category": "diagnostic",
        "device": HA_DEVICE_CONFIG,
    }, retain=True)
    publish_json(DISCOVERY_TOPIC_SWITCH, {
        "name": f"{SENSOR_NAME} Aktives Scannen",
        "unique_id": f"bluecat_{SENSOR_ID}_scan_mode",
        "command_topic": SCAN_MODE_CMD_TOPIC,
        "state_topic": SCAN_MODE_STATE_TOPIC,
        "availability_topic": AVAILABILITY_TOPIC,
        "icon": "mdi:bluetooth-audio",
        "entity_category": "config",
        "device": HA_DEVICE_CONFIG,
    }, retain=True)


def update_runtime_configuration(topic, payload):
    """Übernimmt retained Ziel- und Peer-Konfiguration zur Laufzeit."""
    global TARGET_MAC, MESH_PEER_MACS
    if topic == TARGET_MAC_STATE_TOPIC:
        if isinstance(payload, dict):
            payload = payload.get("target_mac", payload.get("mac", ""))
        TARGET_MAC = normalize_address(payload)
        print(f"Ziel-MAC: {TARGET_MAC or '(leer)'}")
        return
    if topic != MESH_PEERS_TOPIC or not isinstance(payload, dict):
        return
    peers = set(_configured_peers())
    for peer in payload.get("peers", []):
        if isinstance(peer, dict) and peer.get("ble_mac"):
            peers.add(normalize_address(peer["ble_mac"]))
    MESH_PEER_MACS = peers - {normalize_address(LOCAL_BLE_MAC)}


def on_connect(client_instance, userdata, flags, reason_code, properties=None):
    if reason_code.is_failure:
        print(f"Fehler bei MQTT-Verbindung: {reason_code}")
        return
    print(f"Mit MQTT verbunden: {sec.MQTT_BROKER}")
    # Ziel-MAC nur aus dem vom Tracker validierten State übernehmen (nicht /set)
    for topic in (SCAN_MODE_CMD_TOPIC, MESH_PEERS_TOPIC, TARGET_MAC_STATE_TOPIC):
        client_instance.subscribe(topic, qos=1)
    # Frühere, vom Tracker angelegte RSSI/Präsenz-Discovery (gleiche unique_id) entfernen
    client_instance.publish(f"homeassistant/sensor/bluecat_{SENSOR_ID}/rssi/config", "", retain=True, qos=1)
    client_instance.publish(f"homeassistant/binary_sensor/bluecat_{SENSOR_ID}/presence/config", "", retain=True, qos=1)
    publish_discovery_messages()
    client_instance.publish(AVAILABILITY_TOPIC, "online", retain=True, qos=1)
    publish_identity()
    client_instance.publish(SCAN_MODE_STATE_TOPIC, "ON" if current_scan_mode == "active" else "OFF", retain=True, qos=1)
    if main_loop is not None:
        main_loop.call_soon_threadsafe(_publish_initial_state)


def _publish_initial_state():
    global last_absent_pub
    if not is_present:
        publish_tag(False)
        last_absent_pub = time.monotonic()


def on_disconnect(client_instance, userdata, flags, reason_code, properties=None):
    if reason_code != 0:
        print("MQTT-Verbindung verloren; automatischer Reconnect läuft.")


async def change_scan_mode(new_mode):
    global scanner, current_scan_mode
    if new_mode != current_scan_mode:
        current_scan_mode = new_mode
        if scanner:
            await scanner.stop()
        scanner = BleakScanner(detection_callback, **get_scanner_kwargs(current_scan_mode))
        await scanner.start()
    client.publish(SCAN_MODE_STATE_TOPIC, "ON" if current_scan_mode == "active" else "OFF", retain=True, qos=1)


def on_message(client_instance, userdata, msg):
    try:
        text = msg.payload.decode("utf-8").strip()
    except UnicodeDecodeError:
        return
    if msg.topic in {MESH_PEERS_TOPIC, TARGET_MAC_STATE_TOPIC}:
        try:
            update_runtime_configuration(msg.topic, json.loads(text))
        except json.JSONDecodeError:
            update_runtime_configuration(msg.topic, text)
        return
    if msg.topic == SCAN_MODE_CMD_TOPIC and main_loop is not None:
        new_mode = "active" if text.upper() == "ON" else "passive"
        asyncio.run_coroutine_threadsafe(change_scan_mode(new_mode), main_loop)


client.on_connect = on_connect
client.on_disconnect = on_disconnect
client.on_message = on_message


def get_scanner_kwargs(mode):
    kwargs = {"scanning_mode": mode}
    if mode == "passive":
        # BlueZ verlangt im Passiv-Modus or_patterns. Gefiltert wird auf die
        # üblichen Flags-Werte (AD-Typ 0x01) und den TriLola-Marker. Geräte
        # ganz ohne Flags (selten) sieht nur der aktive Modus.
        patterns = [(0, 0x01, bytes([flags])) for flags in (0x02, 0x04, 0x05, 0x06, 0x0A, 0x1A)]
        patterns.append((0, 0xFF, MARKER_COMPANY_ID.to_bytes(2, "little")))
        kwargs["bluez"] = {"or_patterns": patterns}
    return kwargs


def _decode_legacy_sensor_id(advertisement_data):
    local_name = getattr(advertisement_data, "local_name", None)
    if isinstance(local_name, str) and local_name.startswith(MESH_PREFIX):
        return local_name[len(MESH_PREFIX):].strip()
    prefix = MESH_PREFIX.encode("utf-8")
    for value in list(advertisement_data.manufacturer_data.values()) + list(advertisement_data.service_data.values()):
        if isinstance(value, bytes) and value.startswith(prefix):
            try:
                return value[len(prefix):].decode("utf-8").strip()
            except UnicodeDecodeError:
                return None
    return None


def _has_mesh_marker(advertisement_data):
    marker = MESH_MARKER.encode("utf-8")
    for company, value in advertisement_data.manufacturer_data.items():
        if not isinstance(value, (bytes, bytearray)):
            continue
        if company == MARKER_COMPANY_ID and bytes(value).startswith(marker):
            return True  # Protokoll v2 (alle Plattformen)
        if company == LEGACY_ESP_COMPANY_ID and bytes(value).startswith(LEGACY_ESP_PAYLOAD):
            return True  # alte ESP32-Firmware
    return False


def detection_callback(device, advertisement_data):
    address = normalize_address(device.address)
    rssi = int(advertisement_data.rssi)
    if TARGET_MAC and address == TARGET_MAC:
        tag_window.append(rssi)
        return
    if not MESH_ENABLED or address == normalize_address(LOCAL_BLE_MAC):
        return
    legacy_id = _decode_legacy_sensor_id(advertisement_data)
    if address in MESH_PEER_MACS or _has_mesh_marker(advertisement_data) or legacy_id:
        entry = mesh_windows.setdefault(address, {"values": [], "legacy": legacy_id})
        entry["values"].append(rssi)


def _format_mac(address):
    return ":".join(address[i:i + 2] for i in range(0, 12, 2))


async def window_task():
    """Alle WINDOW_SEC: Median publizieren bzw. „nicht gesehen“ melden."""
    global last_seen_time, last_absent_pub, is_present
    while True:
        await asyncio.sleep(WINDOW_SEC)
        now = time.monotonic()
        if tag_window:
            values = list(tag_window)
            tag_window.clear()
            is_present = True
            last_seen_time = now
            publish_tag(True, round(statistics.median(values), 1), len(values), min(values), max(values))
        elif now - last_seen_time >= ABSENT_TIMEOUT_SEC:
            if is_present or now - last_absent_pub >= ABSENT_HEARTBEAT_SEC:
                if is_present:
                    print(f"Ziel seit {ABSENT_TIMEOUT_SEC:.0f} s nicht gesehen.")
                is_present = False
                last_absent_pub = now
                publish_tag(False)
        for address, entry in list(mesh_windows.items()):
            if entry["values"]:
                values = entry["values"]
                publish_mesh(_format_mac(address), round(statistics.median(values), 1), len(values), entry["legacy"])
                entry["values"] = []


async def start_mesh_advertising():
    """Startet optionales BLE-Advertising über BlueZ (fehlertolerant)."""
    global mesh_bus, mesh_manager, mesh_advertisement
    if not MESH_ENABLED or not MESH_ADVERTISING_ENABLED:
        return
    if not DBUS_ADVERTISING_AVAILABLE:
        print("Mesh-Senden deaktiviert: Paket 'dbus-next' fehlt.")
        return
    try:
        mesh_bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        introspection = await mesh_bus.introspect("org.bluez", f"/org/bluez/{MESH_ADAPTER}")
        adapter = mesh_bus.get_proxy_object("org.bluez", f"/org/bluez/{MESH_ADAPTER}", introspection)
        mesh_manager = adapter.get_interface("org.bluez.LEAdvertisingManager1")
        mesh_advertisement = MeshAdvertisement()
        mesh_bus.export(mesh_advertisement_path, mesh_advertisement)
        await mesh_manager.call_register_advertisement(mesh_advertisement_path, {})
        print(f"BLE-Mesh-Advertising aktiv: 0x{MARKER_COMPANY_ID:04X} {MESH_MARKER}")
    except Exception as error:
        print(f"BLE-Mesh-Advertising nicht verfügbar: {error}")
        if mesh_bus is not None:
            try:
                mesh_bus.disconnect()
            except Exception:
                pass
        mesh_bus = mesh_manager = mesh_advertisement = None


async def stop_mesh_advertising():
    global mesh_bus, mesh_manager, mesh_advertisement
    if mesh_manager is not None:
        try:
            await mesh_manager.call_unregister_advertisement(mesh_advertisement_path)
        except Exception:
            pass
    if mesh_bus is not None:
        try:
            mesh_bus.unexport(mesh_advertisement_path)
            mesh_bus.disconnect()
        except Exception:
            pass
    mesh_bus = mesh_manager = mesh_advertisement = None


async def main():
    global main_loop, scanner, LOCAL_BLE_MAC, MESH_PEER_MACS
    main_loop = asyncio.get_running_loop()
    LOCAL_BLE_MAC = get_local_bluetooth_mac()
    MESH_PEER_MACS = MESH_PEER_MACS - {normalize_address(LOCAL_BLE_MAC)}
    client.connect_async(sec.MQTT_BROKER, sec.MQTT_PORT, 60)
    client.loop_start()
    await start_mesh_advertising()
    scanner = BleakScanner(detection_callback, **get_scanner_kwargs(current_scan_mode))
    await scanner.start()
    task = asyncio.create_task(window_task())
    print(f"Starte {SENSOR_NAME} ({SENSOR_ID}); Ziel={_format_mac(TARGET_MAC) if TARGET_MAC else 'über MQTT'}")
    try:
        while True:
            await asyncio.sleep(1)
    finally:
        task.cancel()
        if scanner:
            await scanner.stop()
        await stop_mesh_advertising()
        client.publish(AVAILABILITY_TOPIC, "offline", retain=True, qos=1)
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
