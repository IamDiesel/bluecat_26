"""Home-Assistant-Discovery des Trackers.

Eigentümerschaft (verhindert doppelte ``unique_id``s):
* **Firmware** publiziert je Sensor RSSI, Präsenz und den Scan-Schalter.
* **Tracker** publiziert die Lola-Entities und je Sensor nur die
  Konfigurations-Entities (BLE-MAC, Position, Kalibrierung, aktiv).

Alle Tracker-Entities hängen an der Availability des Trackers (LWT), damit
man Sensoren auch konfigurieren kann, wenn sie gerade offline sind.
"""

from config_manager import (
    ENGINE_SET_TOPIC,
    ENGINE_STATE_TOPIC,
    ENGINES,
    MESH_RELEARN_TOPIC,
    SENSOR_CONFIG_SET_PATTERN,
    SENSOR_CONFIG_STATE_PATTERN,
    TARGET_MAC_SET_TOPIC,
    TARGET_MAC_STATE_TOPIC,
)

TRACKER_VERSION = "2.4.0"
TRACKER_ROOT = "bluecat/trilola"
AVAILABILITY_TOPIC = TRACKER_ROOT + "/status"
STATE_TOPIC_GPS = TRACKER_ROOT + "/gps/state"
TRACKER_STATE_TOPIC = TRACKER_ROOT + "/tracker/state"
TRACKER_ATTR_TOPIC = TRACKER_ROOT + "/tracker/attributes"
ROOM_STATE_TOPIC = TRACKER_ROOT + "/room/state"
MOVING_STATE_TOPIC = TRACKER_ROOT + "/moving/state"
DISCOVERY_TOPIC_GPS = "homeassistant/sensor/bluecat_trilola_gps/config"

HA_DEVICE_CONFIG = {
    "identifiers": ["bluecat_trilola_engine"],
    "name": "Bluecat TriLola",
    "manufacturer": "Bluecat",
    "model": "TriLola Tracker",
    "sw_version": TRACKER_VERSION,
}

CALIBRATION_ENTITY_FIELDS = (
    ("tx_power", -120.0, 0.0, 0.01, "dBm"),
    ("n_factor", 0.5, 6.0, 0.001, None),
    ("sigma_db", 0.5, 20.0, 0.01, "dB"),
    ("r_min", 0.0, 200.0, 0.01, None),
    ("r_max", 0.0, 200.0, 0.01, None),
    ("q_variance", 0.0, 10.0, 0.001, None),
    ("rssi_limit", -130.0, 0.0, 0.1, "dBm"),
    ("detection_floor", -120.0, -40.0, 0.1, "dBm"),
)


def safe_id(sensor_id: str) -> str:
    return str(sensor_id).replace("-", "_")


def _availability():
    return {"availability_topic": AVAILABILITY_TOPIC, "payload_available": "online", "payload_not_available": "offline"}


class HADiscoveryBuilder:
    @staticmethod
    def build_tracker() -> list:
        msgs = []
        avail = _availability()
        msgs.append((DISCOVERY_TOPIC_GPS, {
            "name": "Lola GPS Position",
            "unique_id": "bluecat_trilola_gps_sensor",
            "state_topic": STATE_TOPIC_GPS,
            "value_template": "{{ value_json.state }}",
            "json_attributes_topic": STATE_TOPIC_GPS,
            "json_attributes_template": "{{ value_json.attributes | tojson }}",
            "icon": "mdi:paw",
            "device": HA_DEVICE_CONFIG,
            **avail,
        }, True))
        msgs.append(("homeassistant/device_tracker/bluecat_trilola/config", {
            "name": "Lola",
            "unique_id": "bluecat_trilola_tracker",
            "state_topic": TRACKER_STATE_TOPIC,
            "json_attributes_topic": TRACKER_ATTR_TOPIC,
            "payload_reset": "None",
            "source_type": "gps",
            "icon": "mdi:cat",
            "device": HA_DEVICE_CONFIG,
            **avail,
        }, True))
        msgs.append(("homeassistant/sensor/bluecat_trilola_room/config", {
            "name": "Lola Raum",
            "unique_id": "bluecat_trilola_room",
            "state_topic": ROOM_STATE_TOPIC,
            "icon": "mdi:floor-plan",
            "device": HA_DEVICE_CONFIG,
            **avail,
        }, True))
        msgs.append(("homeassistant/binary_sensor/bluecat_trilola_moving/config", {
            "name": "Lola in Bewegung",
            "unique_id": "bluecat_trilola_moving",
            "state_topic": MOVING_STATE_TOPIC,
            "payload_on": "ON",
            "payload_off": "OFF",
            "device_class": "moving",
            "device": HA_DEVICE_CONFIG,
            **avail,
        }, True))
        msgs.append(("homeassistant/text/bluecat_trilola_target_mac/config", {
            "name": "TriLola Zielobjekt-MAC",
            "unique_id": "bluecat_trilola_target_mac",
            "command_topic": TARGET_MAC_SET_TOPIC,
            "state_topic": TARGET_MAC_STATE_TOPIC,
            "mode": "text",
            "entity_category": "config",
            "device": HA_DEVICE_CONFIG,
            **avail,
        }, True))
        msgs.append(("homeassistant/select/bluecat_trilola_engine/config", {
            "name": "TriLola Modell",
            "unique_id": "bluecat_trilola_engine_select",
            "command_topic": ENGINE_SET_TOPIC,
            "state_topic": ENGINE_STATE_TOPIC,
            "options": list(ENGINES),
            "entity_category": "config",
            "device": HA_DEVICE_CONFIG,
            **avail,
        }, True))
        msgs.append(("homeassistant/button/bluecat_trilola_mesh_relearn/config", {
            "name": "TriLola Mesh-Baseline neu lernen",
            "unique_id": "bluecat_trilola_mesh_relearn",
            "command_topic": MESH_RELEARN_TOPIC,
            "payload_press": "PRESS",
            "entity_category": "config",
            "device": HA_DEVICE_CONFIG,
            **avail,
        }, True))
        return msgs

    @staticmethod
    def build_sensor(sensor_id: str, data: dict) -> list:
        sid = safe_id(sensor_id)
        name = data.get("name", sensor_id)
        device = {
            "identifiers": [f"bluecat_sensor_{sensor_id}"],
            "name": name,
            "manufacturer": "Bluecat",
            "model": f"BLE-Sensor ({data.get('implementation', 'unknown')})",
        }
        if data.get("firmware_version"):
            device["sw_version"] = str(data["firmware_version"])
        if data.get("ip") and data.get("implementation") == "shelly":
            device["configuration_url"] = f"http://{data['ip']}"
        avail = _availability()
        msgs = [(f"homeassistant/text/bluecat_{sid}_ble_mac/config", {
            "name": f"{name} BLE-MAC",
            "unique_id": f"bluecat_{sid}_ble_mac",
            "command_topic": SENSOR_CONFIG_SET_PATTERN.format(sensor_id=sensor_id, field="ble_mac"),
            "state_topic": SENSOR_CONFIG_STATE_PATTERN.format(sensor_id=sensor_id, field="ble_mac"),
            "mode": "text",
            "entity_category": "config",
            "device": device,
            **avail,
        }, True)]
        msgs.append((f"homeassistant/switch/bluecat_{sid}_enabled/config", {
            "name": f"{name} aktiv",
            "unique_id": f"bluecat_{sid}_enabled",
            "command_topic": SENSOR_CONFIG_SET_PATTERN.format(sensor_id=sensor_id, field="enabled"),
            "state_topic": SENSOR_CONFIG_STATE_PATTERN.format(sensor_id=sensor_id, field="enabled"),
            "payload_on": "ON",
            "payload_off": "OFF",
            "entity_category": "config",
            "device": device,
            **avail,
        }, True))
        for axis in ("x", "y"):
            msgs.append((f"homeassistant/number/bluecat_{sid}_position_{axis}/config", {
                "name": f"{name} Position {axis.upper()}",
                "unique_id": f"bluecat_{sid}_position_{axis}",
                "command_topic": SENSOR_CONFIG_SET_PATTERN.format(sensor_id=sensor_id, field=f"position_{axis}"),
                "state_topic": SENSOR_CONFIG_STATE_PATTERN.format(sensor_id=sensor_id, field="position"),
                "value_template": f"{{{{ value_json.{axis}_cm }}}}",
                "unit_of_measurement": "cm",
                "mode": "box",
                "min": -100000.0,
                "max": 100000.0,
                "step": 0.1,
                "entity_category": "config",
                "device": device,
                **avail,
            }, True))
        msgs.append((f"homeassistant/number/bluecat_{sid}_height/config", {
            "name": f"{name} Höhe über Boden",
            "unique_id": f"bluecat_{sid}_height",
            "command_topic": SENSOR_CONFIG_SET_PATTERN.format(sensor_id=sensor_id, field="height"),
            "state_topic": SENSOR_CONFIG_STATE_PATTERN.format(sensor_id=sensor_id, field="position"),
            "value_template": "{{ value_json.height_cm }}",
            "unit_of_measurement": "cm",
            "mode": "box",
            "min": 0.0,
            "max": 600.0,
            "step": 1.0,
            "entity_category": "config",
            "device": device,
            **avail,
        }, True))
        for field, lo, hi, step, unit in CALIBRATION_ENTITY_FIELDS:
            entity = {
                "name": f"{name} Kalibrierung {field}",
                "unique_id": f"bluecat_{sid}_calibration_{field}",
                "command_topic": SENSOR_CONFIG_SET_PATTERN.format(sensor_id=sensor_id, field=f"calibration_{field}"),
                "state_topic": SENSOR_CONFIG_STATE_PATTERN.format(sensor_id=sensor_id, field="calibration"),
                "value_template": f"{{{{ value_json.{field} }}}}",
                "mode": "box",
                "min": lo,
                "max": hi,
                "step": step,
                "entity_category": "config",
                "device": device,
                **avail,
            }
            if unit:
                entity["unit_of_measurement"] = unit
            msgs.append((f"homeassistant/number/bluecat_{sid}_calibration_{field}/config", entity, True))
        return msgs

    @staticmethod
    def legacy_cleanup(sensor_id: str) -> list:
        """Leert die früher vom Tracker belegten RSSI/Präsenz-Discovery-Topics."""
        sid = safe_id(sensor_id)
        return [
            (f"homeassistant/sensor/bluecat_{sid}/rssi/config", "", True),
            (f"homeassistant/binary_sensor/bluecat_{sid}/presence/config", "", True),
        ]

    @staticmethod
    def build_all(sensor_configs: dict) -> list:
        msgs = HADiscoveryBuilder.build_tracker()
        for sensor_id, data in sensor_configs.items():
            msgs.extend(HADiscoveryBuilder.build_sensor(sensor_id, data))
        return msgs
