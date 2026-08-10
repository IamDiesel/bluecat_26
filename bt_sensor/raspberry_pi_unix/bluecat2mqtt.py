"""BLE-to-MQTT-Sensor für Raspberry Pi/BlueZ.

Der Pi filtert nur das gewünschte BLE-Gerät und begrenzt die Publish-Rate.
Die eigentliche RSSI-Filterung erfolgt einheitlich im Tracker. Dadurch sind
Pi, ESP32 und Shelly mathematisch vergleichbar.

Optionale Werte werden aus ``secrets_blue.py`` gelesen:
SENSOR_ID, SENSOR_NAME, STATE_TOPIC, AVAILABILITY_TOPIC, TARGET_MAC,
TIMEOUT_SECONDS und MESH_ENABLED.
"""

import asyncio
import json
import re
import subprocess
import time
from bleak import BleakScanner
import paho.mqtt.client as mqtt
try:
    import secrets_blue as sec
except SyntaxError as error:
    raise SystemExit(
        "secrets_blue.py enthält einen Syntaxfehler. "
        "Bitte secrets_blue.example.py kopieren und anpassen."
    ) from error

try:
    from dbus_next import Variant
    from dbus_next.aio import MessageBus
    from dbus_next.constants import BusType, PropertyAccess
    from dbus_next.service import ServiceInterface, dbus_property, method

    DBUS_ADVERTISING_AVAILABLE = True
except ImportError:
    DBUS_ADVERTISING_AVAILABLE = False


SENSOR_ID = str(getattr(sec, "SENSOR_ID", "ron"))
SENSOR_NAME = str(getattr(sec, "SENSOR_NAME", "Ron Raspberry Pi"))
STATE_TOPIC = str(
    getattr(sec, "STATE_TOPIC", f"bluecat/{SENSOR_ID}/sensor/state")
)
AVAILABILITY_TOPIC = str(
    getattr(
        sec,
        "AVAILABILITY_TOPIC",
        f"bluecat/{SENSOR_ID}/sensor/status",
    )
)
TARGET_MAC = str(getattr(sec, "TARGET_MAC", "")).lower()
LOCAL_BLE_MAC = ""
MESH_ENABLED = bool(getattr(sec, "MESH_ENABLED", True))
MESH_ADVERTISING_ENABLED = bool(
    getattr(sec, "MESH_ADVERTISING_ENABLED", True)
)
MESH_PREFIX = str(getattr(sec, "MESH_PREFIX", "TRILOLA_SENSOR:"))
MESH_MARKER = str(getattr(sec, "MESH_MARKER", "TRILOLA"))
MESH_ADAPTER = str(getattr(sec, "MESH_ADAPTER", "hci0"))
REGISTRY_IDENTITY_TOPIC = f"bluecat/registry/{SENSOR_ID}/identity"
MESH_PEERS_TOPIC = "bluecat/registry/mesh_peers"
TARGET_MAC_STATE_TOPIC = "bluecat/config/target_mac/state"
TARGET_MAC_SET_TOPIC = "bluecat/config/target_mac/set"


def normalize_address(value):
    return str(value).replace(":", "").replace("-", "").strip().lower()


configured_mesh_peers = getattr(sec, "MESH_PEER_MACS", [])
if isinstance(configured_mesh_peers, str):
    configured_mesh_peers = [configured_mesh_peers]
MESH_PEER_MACS = {
    normalize_address(value)
    for value in configured_mesh_peers
    if value
}

DISCOVERY_TOPIC_SENSOR = (
    f"homeassistant/sensor/bluecat_{SENSOR_ID}_rssi/config"
)
DISCOVERY_TOPIC_PRESENCE = (
    f"homeassistant/binary_sensor/bluecat_{SENSOR_ID}_presence/config"
)
DISCOVERY_TOPIC_SWITCH = (
    f"homeassistant/switch/bluecat_{SENSOR_ID}_scan_mode/config"
)
SCAN_MODE_CMD_TOPIC = f"bluecat/{SENSOR_ID}/switch/scan_mode/set"
SCAN_MODE_STATE_TOPIC = f"bluecat/{SENSOR_ID}/switch/scan_mode/state"

MIN_PUBLISH_INTERVAL = float(getattr(sec, "MIN_PUBLISH_INTERVAL", 2.0))
TIMEOUT_SECONDS = float(getattr(sec, "SENSOR_TIMEOUT_SEC", 30.0))
OFFLINE_RSSI = -130

last_publish_time = 0.0
last_seen_time = 0.0
is_present = False
sequence = 0
mesh_sequences = {}
mesh_last_publish = {}

scanner = None
current_scan_mode = "active"
main_loop = None
mesh_bus = None
mesh_manager = None
mesh_advertisement = None
mesh_advertisement_path = "/com/bluecat/trilola/advertisement"

client = mqtt.Client(
    mqtt.CallbackAPIVersion.VERSION1,
    client_id=f"bluecat_{SENSOR_ID}_scanner",
)
if getattr(sec, "MQTT_USER", "") and getattr(sec, "MQTT_PASSWORD", ""):
    client.username_pw_set(sec.MQTT_USER, sec.MQTT_PASSWORD)
client.will_set(AVAILABILITY_TOPIC, payload="offline", retain=True)


if DBUS_ADVERTISING_AVAILABLE:
    class MeshAdvertisement(ServiceInterface):
        """Kleines BlueZ-Advertisement mit festem TriLola-Marker."""

        def __init__(self, sensor_id):
            super().__init__("org.bluez.LEAdvertisement1")
            self._manufacturer_data = {
                0xFFFF: Variant(
                    "ay",
                    MESH_MARKER.encode("utf-8"),
                )
            }

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
}


def now_timestamp():
    """UTC-Epochzeit in Sekunden; Gerätezeit bleibt MQTT-unabhängig."""
    return int(time.time())


def get_local_bluetooth_mac():
    """Ermittelt die Controller-MAC unabhängig von einer Python-venv."""
    try:
        result = subprocess.run(
            ["bluetoothctl", "show"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        match = re.search(
            r"Controller\s+([0-9A-Fa-f:]{17})", result.stdout
        )
        if match:
            return match.group(1).lower()
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def publish_json(topic, payload, retain=False):
    if client.is_connected():
        client.publish(topic, json.dumps(payload), qos=1, retain=retain)


def publish_identity():
    """Registriert den Sensor retained beim Tracker."""
    if not LOCAL_BLE_MAC:
        return
    publish_json(
        REGISTRY_IDENTITY_TOPIC,
        {
            "schema": 1,
            "sensor_id": SENSOR_ID,
            "name": SENSOR_NAME,
            "implementation": "raspberry_pi",
            "state_topic": STATE_TOPIC,
            "availability_topic": AVAILABILITY_TOPIC,
            "ble_mac": LOCAL_BLE_MAC,
            "enabled": True,
            "timestamp": now_timestamp(),
        },
        retain=True,
    )


def update_runtime_configuration(topic, payload):
    """Übernimmt retained Ziel- und Peer-Konfiguration zur Laufzeit."""
    global TARGET_MAC, MESH_PEER_MACS
    if topic == TARGET_MAC_STATE_TOPIC:
        if isinstance(payload, dict):
            payload = payload.get("target_mac", payload.get("mac", ""))
        TARGET_MAC = str(payload or "").lower()
        return
    if topic != MESH_PEERS_TOPIC or not isinstance(payload, dict):
        return
    peers = set()
    for peer in payload.get("peers", []):
        if not isinstance(peer, dict) or not peer.get("ble_mac"):
            continue
        try:
            peers.add(normalize_address(peer["ble_mac"]))
        except (TypeError, ValueError):
            continue
    MESH_PEER_MACS = peers - {normalize_address(LOCAL_BLE_MAC)}


def publish_tag_rssi(rssi, present=True):
    global sequence, last_publish_time
    sequence += 1
    publish_json(
        STATE_TOPIC,
        {
            "message_type": "tag_rssi",
            "sensor_id": SENSOR_ID,
            "rssi": int(rssi),
            "timestamp": now_timestamp(),
            "sequence": sequence,
            "sample_count": 1,
            "present": bool(present),
        },
    )
    last_publish_time = time.time()


def publish_offline():
    publish_tag_rssi(OFFLINE_RSSI, present=False)


def publish_mesh_rssi(beacon_mac, rssi, legacy_sensor_id=None):
    """Publiziert eine Sensor-zu-Sensor-Messung auf dem Empfänger-Topic."""
    if not MESH_ENABLED or not beacon_mac:
        return
    now = time.time()
    if now - mesh_last_publish.get(beacon_mac, 0.0) < MIN_PUBLISH_INTERVAL:
        return
    mesh_sequences[beacon_mac] = mesh_sequences.get(beacon_mac, 0) + 1
    payload = {
        "message_type": "sensor_beacon",
        "sensor_id": SENSOR_ID,
        "beacon_mac": beacon_mac,
        "rssi": int(rssi),
        "timestamp": now_timestamp(),
        "sequence": mesh_sequences[beacon_mac],
    }
    if legacy_sensor_id and legacy_sensor_id != SENSOR_ID:
        payload["beacon_sensor"] = legacy_sensor_id
    publish_json(
        STATE_TOPIC,
        payload,
    )
    mesh_last_publish[beacon_mac] = now


def publish_discovery_messages():
    sensor_payload = {
        "name": f"{SENSOR_NAME} RSSI",
        "unique_id": f"bluecat_{SENSOR_ID}_rssi",
        "state_topic": STATE_TOPIC,
        "availability_topic": AVAILABILITY_TOPIC,
        "value_template": (
            "{% if value_json.message_type is not defined or "
            "value_json.message_type == 'tag_rssi' %}"
            "{{ value_json.rssi }}{% endif %}"
        ),
        "unit_of_measurement": "dBm",
        "device_class": "signal_strength",
        "entity_category": "diagnostic",
        "device": HA_DEVICE_CONFIG,
    }
    client.publish(
        DISCOVERY_TOPIC_SENSOR,
        json.dumps(sensor_payload),
        retain=True,
        qos=1,
    )
    presence_payload = {
        "name": f"{SENSOR_NAME} Präsenz",
        "unique_id": f"bluecat_{SENSOR_ID}_presence",
        "state_topic": STATE_TOPIC,
        "availability_topic": AVAILABILITY_TOPIC,
        "value_template": (
            "{% if value_json.message_type is not defined or "
            "value_json.message_type == 'tag_rssi' %}"
            "{{ 'ON' if value_json.present is defined and value_json.present "
            "else ('ON' if value_json.rssi|float > -120 else 'OFF') }}"
            "{% endif %}"
        ),
        "payload_on": "ON",
        "payload_off": "OFF",
        "device_class": "presence",
        "entity_category": "diagnostic",
        "device": HA_DEVICE_CONFIG,
    }
    client.publish(
        DISCOVERY_TOPIC_PRESENCE,
        json.dumps(presence_payload),
        retain=True,
        qos=1,
    )
    switch_payload = {
        "name": f"{SENSOR_NAME} Aktives Scannen",
        "unique_id": f"bluecat_{SENSOR_ID}_scan_mode",
        "command_topic": SCAN_MODE_CMD_TOPIC,
        "state_topic": SCAN_MODE_STATE_TOPIC,
        "availability_topic": AVAILABILITY_TOPIC,
        "icon": "mdi:bluetooth-audio",
        "entity_category": "config",
        "device": HA_DEVICE_CONFIG,
    }
    client.publish(
        DISCOVERY_TOPIC_SWITCH,
        json.dumps(switch_payload),
        retain=True,
        qos=1,
    )


def on_connect(client_instance, userdata, flags, rc):
    if rc == 0:
        print(f"Mit MQTT verbunden: {sec.MQTT_BROKER}")
        client_instance.subscribe(SCAN_MODE_CMD_TOPIC, qos=1)
        client_instance.subscribe(MESH_PEERS_TOPIC, qos=1)
        client_instance.subscribe(TARGET_MAC_STATE_TOPIC, qos=1)
        client_instance.subscribe(TARGET_MAC_SET_TOPIC, qos=1)
        publish_discovery_messages()
        client_instance.publish(AVAILABILITY_TOPIC, "online", retain=True, qos=1)
        publish_identity()
        client_instance.publish(
            SCAN_MODE_STATE_TOPIC,
            "ON" if current_scan_mode == "active" else "OFF",
            retain=True,
            qos=1,
        )
    else:
        print(f"Fehler bei MQTT-Verbindung: {rc}")


def on_disconnect(client_instance, userdata, rc):
    if rc != 0:
        print("MQTT-Verbindung verloren; automatischer Reconnect läuft.")


async def change_scan_mode(new_mode):
    global scanner, current_scan_mode
    if new_mode == current_scan_mode:
        return
    current_scan_mode = new_mode
    if scanner:
        await scanner.stop()
    scanner = BleakScanner(
        detection_callback,
        **get_scanner_kwargs(current_scan_mode),
    )
    await scanner.start()
    client.publish(
        SCAN_MODE_STATE_TOPIC,
        "ON" if current_scan_mode == "active" else "OFF",
        retain=True,
        qos=1,
    )


def on_message(client_instance, userdata, msg):
    if msg.topic in {MESH_PEERS_TOPIC, TARGET_MAC_STATE_TOPIC}:
        try:
            update_runtime_configuration(
                msg.topic,
                json.loads(msg.payload.decode("utf-8")),
            )
        except (UnicodeDecodeError, json.JSONDecodeError):
            update_runtime_configuration(
                msg.topic, msg.payload.decode("utf-8").strip()
            )
        return
    if msg.topic == TARGET_MAC_SET_TOPIC:
        # Ein direktes Set-Topic wird ebenfalls akzeptiert; der Tracker
        # bestätigt es anschließend als retained state.
        update_runtime_configuration(
            TARGET_MAC_STATE_TOPIC, msg.payload.decode("utf-8").strip()
        )
        return
    if msg.topic != SCAN_MODE_CMD_TOPIC or main_loop is None:
        return
    command = msg.payload.decode("utf-8").strip().upper()
    new_mode = "active" if command == "ON" else "passive"
    asyncio.run_coroutine_threadsafe(change_scan_mode(new_mode), main_loop)


client.on_connect = on_connect
client.on_disconnect = on_disconnect
client.on_message = on_message


def get_scanner_kwargs(mode):
    kwargs = {"scanning_mode": mode}
    if mode == "passive" and not MESH_ENABLED:
        kwargs["bluez"] = {
            "or_patterns": [(0, 0xFF, b"\x4C\x00\x02\x15")]
        }
    return kwargs


def _decode_mesh_sensor_id(advertisement_data):
    """Liest optional die Legacy-ID aus dem Sensor-Beacon."""
    local_name = getattr(advertisement_data, "local_name", None)
    if isinstance(local_name, str) and local_name.startswith(MESH_PREFIX):
        return local_name[len(MESH_PREFIX) :].strip()

    fields = list(advertisement_data.manufacturer_data.values())
    fields += list(advertisement_data.service_data.values())
    prefix = MESH_PREFIX.encode("utf-8")
    for value in fields:
        if isinstance(value, bytes) and value.startswith(prefix):
            try:
                return value[len(prefix) :].decode("utf-8").strip()
            except UnicodeDecodeError:
                return None
    return None


def _has_mesh_marker(advertisement_data):
    fields = list(advertisement_data.manufacturer_data.values())
    fields += list(advertisement_data.service_data.values())
    marker = MESH_MARKER.encode("utf-8")
    return any(
        isinstance(value, bytes) and value.startswith(marker)
        for value in fields
    )


def _is_mesh_beacon(address, advertisement_data):
    """Akzeptiert Custom-Beacons oder konfigurierte Standard-Advertisements."""
    return (
        _has_mesh_marker(advertisement_data)
        or bool(_decode_mesh_sensor_id(advertisement_data))
        or (
        normalize_address(address) in MESH_PEER_MACS
        )
    )


async def start_mesh_advertising():
    """Startet optionales BLE-Advertising über BlueZ.

    Fehlt dbus-next, BlueZ oder die Advertising-Berechtigung, bleibt der
    Sensor als Scanner/MQTT-Sensor funktionsfähig.
    """
    global mesh_bus, mesh_manager, mesh_advertisement
    if not MESH_ENABLED or not MESH_ADVERTISING_ENABLED:
        return
    if not DBUS_ADVERTISING_AVAILABLE:
        print("Mesh-Senden deaktiviert: Paket 'dbus-next' fehlt.")
        return

    try:
        mesh_bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        introspection = await mesh_bus.introspect(
            "org.bluez", f"/org/bluez/{MESH_ADAPTER}"
        )
        adapter = mesh_bus.get_proxy_object(
            "org.bluez",
            f"/org/bluez/{MESH_ADAPTER}",
            introspection,
        )
        mesh_manager = adapter.get_interface(
            "org.bluez.LEAdvertisingManager1"
        )
        mesh_advertisement = MeshAdvertisement(SENSOR_ID)
        mesh_bus.export(mesh_advertisement_path, mesh_advertisement)
        await mesh_manager.call_register_advertisement(
            mesh_advertisement_path,
            {},
        )
        print(f"BLE-Mesh-Advertising aktiv: {MESH_MARKER}")
    except Exception as error:
        print(f"BLE-Mesh-Advertising nicht verfügbar: {error}")
        if mesh_bus is not None:
            mesh_bus.disconnect()
        mesh_bus = None
        mesh_manager = None
        mesh_advertisement = None


async def stop_mesh_advertising():
    global mesh_bus, mesh_manager, mesh_advertisement
    if mesh_manager is not None:
        try:
            await mesh_manager.call_unregister_advertisement(
                mesh_advertisement_path
            )
        except Exception:
            pass
    if mesh_bus is not None:
        mesh_bus.unexport(mesh_advertisement_path)
        mesh_bus.disconnect()
    mesh_bus = None
    mesh_manager = None
    mesh_advertisement = None


def detection_callback(device, advertisement_data):
    global last_publish_time, last_seen_time, is_present
    address = str(device.address).lower()
    if MESH_ENABLED:
        legacy_sensor_id = _decode_mesh_sensor_id(advertisement_data)
        if _is_mesh_beacon(address, advertisement_data):
            publish_mesh_rssi(
                address,
                advertisement_data.rssi,
                legacy_sensor_id,
            )

    if not TARGET_MAC or normalize_address(address) != normalize_address(TARGET_MAC):
        return

    last_seen_time = time.time()
    if (
        is_present
        and last_seen_time - last_publish_time < MIN_PUBLISH_INTERVAL
    ):
        return
    is_present = True
    publish_tag_rssi(advertisement_data.rssi)
    print(
        f"[{current_scan_mode.upper()}] "
        f"RSSI {advertisement_data.rssi} dBm"
    )


async def watchdog():
    global is_present
    while True:
        if is_present and time.time() - last_seen_time > TIMEOUT_SECONDS:
            print(f"Ziel seit {TIMEOUT_SECONDS:.0f}s nicht gesehen.")
            is_present = False
            publish_offline()
        await asyncio.sleep(1)


def connect_mqtt():
    while True:
        try:
            client.connect(sec.MQTT_BROKER, sec.MQTT_PORT, 60)
            return
        except (ConnectionRefusedError, OSError) as error:
            print(f"MQTT nicht erreichbar: {error}; neuer Versuch in 5 s.")
            time.sleep(5)


async def main():
    global main_loop, scanner, current_scan_mode, LOCAL_BLE_MAC
    main_loop = asyncio.get_running_loop()
    LOCAL_BLE_MAC = get_local_bluetooth_mac()
    connect_mqtt()
    client.loop_start()
    await start_mesh_advertising()
    scanner = BleakScanner(
        detection_callback,
        **get_scanner_kwargs(current_scan_mode),
    )
    await scanner.start()
    watchdog_task = asyncio.create_task(watchdog())
    print(
        f"Starte {SENSOR_NAME} ({SENSOR_ID}); "
        f"Ziel={TARGET_MAC or 'NICHT KONFIGURIERT'}"
    )
    try:
        while True:
            await asyncio.sleep(1)
    finally:
        watchdog_task.cancel()
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
