"""Zentrale Konfiguration und MQTT-Schnittstelle für TriLola.

Sensoren benötigen nur Broker-Zugangsdaten sowie ihre lokale ``sensor_id``;
alle veränderlichen Betriebsdaten (Position, Kalibrierung, BLE-MAC, aktiv)
werden über MQTT/Home Assistant verwaltet und als JSON im ``config``-Ordner
gespeichert. Änderungen werden validiert, atomar gespeichert und als retained
``state``-Nachricht bestätigt.
"""

from __future__ import annotations

import glob
import json
import os
import re
import tempfile
import time
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Optional, Tuple

REGISTRY_ROOT = "bluecat/registry"
IDENTITY_TOPIC_PATTERN = REGISTRY_ROOT + "/{sensor_id}/identity"
MESH_PEERS_TOPIC = REGISTRY_ROOT + "/mesh_peers"
CONFIG_ROOT = "bluecat/config"
TARGET_MAC_SET_TOPIC = CONFIG_ROOT + "/target_mac/set"
TARGET_MAC_STATE_TOPIC = CONFIG_ROOT + "/target_mac/state"
SENSOR_CONFIG_SET_PATTERN = CONFIG_ROOT + "/sensors/{sensor_id}/{field}/set"
SENSOR_CONFIG_STATE_PATTERN = CONFIG_ROOT + "/sensors/{sensor_id}/{field}/state"
ENGINE_SET_TOPIC = CONFIG_ROOT + "/tracker/engine/set"
ENGINE_STATE_TOPIC = CONFIG_ROOT + "/tracker/engine/state"
MESH_RELEARN_TOPIC = CONFIG_ROOT + "/mesh/relearn/set"

CALIBRATION_FIELDS = (
    "tx_power", "n_factor", "r_min", "r_max", "rssi_limit", "q_variance", "sigma_db", "detection_floor",
)
DEFAULT_CALIBRATION = {
    "tx_power": -59.0,
    "n_factor": 3.0,
    "r_min": 5.0,
    "r_max": 20.0,
    "rssi_limit": -110.0,
    "q_variance": 0.1,
    "sigma_db": 4.0,
    "detection_floor": -97.0,
}
DEFAULT_POSITION = [0.0, 0.0]
# Montagehöhe über dem Fußboden (cm), wenn nichts eingetragen ist
DEFAULT_MOUNT_HEIGHT_CM = 100.0
DEFAULT_MOUNT_HEIGHT_BY_TYPE = {"shelly": 105.0}   # Unterputz hinter dem Lichtschalter
DEFAULT_TAG_HEIGHT_CM = 25.0                       # Halsband einer stehenden Katze
MAX_MOUNT_HEIGHT_CM = 600.0
DEFAULT_TARGET_MAC = ""
ENGINES = ("pf", "legacy")

# Dateien im config-Ordner, die keine Sensor-Konfiguration sind
NON_SENSOR_FILES = {
    "radio_mesh_baseline.json", "radio_heatmap.json", "tracker_state.json",
    "floorplan.json", "viewer_config.json", "calibration_points.json", "georef.json", "tuning.json",
}


def normalize_ble_address(value: Any) -> str:
    """Gibt eine BLE-MAC ohne Trennzeichen in Kleinbuchstaben zurück."""
    normalized = re.sub(r"[^0-9a-fA-F]", "", str(value or "")).lower()
    if not re.fullmatch(r"[0-9a-f]{12}", normalized):
        raise ValueError(f"Ungültige BLE-Adresse: {value!r}")
    return normalized


def format_ble_address(value: Any) -> str:
    normalized = normalize_ble_address(value)
    return ":".join(normalized[index:index + 2] for index in range(0, 12, 2))


def parse_bool(value: Any) -> bool:
    """Robuste Bool-Auswertung für MQTT-Payloads ("OFF", "false", 0, …)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, dict):
        for key in ("enabled", "state", "value"):
            if key in value:
                return parse_bool(value[key])
        return False
    text = str(value).strip().lower()
    if text in {"1", "true", "on", "yes", "ja", "enabled", "aktiv"}:
        return True
    if text in {"0", "false", "off", "no", "nein", "disabled", "inaktiv", ""}:
        return False
    raise ValueError(f"Kein Wahrheitswert: {value!r}")


def _finite_float(value: Any, field: str) -> float:
    number = float(value)
    if not (-1.0e12 < number < 1.0e12):
        raise ValueError(f"{field} ist nicht plausibel.")
    return number


def validate_position(value: Any) -> list:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError("pos muss genau zwei Werte enthalten.")
    return [_finite_float(value[0], "x_cm"), _finite_float(value[1], "y_cm")]


def validate_height(value: Any) -> float:
    """Montagehöhe über dem eigenen Fußboden in cm."""
    number = _finite_float(value, "height_cm")
    if not 0.0 <= number <= MAX_MOUNT_HEIGHT_CM:
        raise ValueError(f"Höhe muss zwischen 0 und {MAX_MOUNT_HEIGHT_CM:.0f} cm liegen.")
    return round(number, 1)


def validate_floor(value: Any) -> Optional[float]:
    """Fußbodenhöhe des Stockwerks eines Sensors über Grund in cm; leer = wie die Wohnung."""
    if value is None or (isinstance(value, str) and value.strip().lower() in ("", "none", "null")):
        return None
    number = _finite_float(value, "floor_cm")
    if not -5000.0 <= number <= 50000.0:
        raise ValueError("Fußbodenhöhe nicht plausibel.")
    return round(number, 1)


def mount_height_cm(data: dict) -> float:
    """Eingetragene oder typabhängige Standard-Montagehöhe."""
    value = data.get("height_cm")
    if value is not None:
        return float(value)
    impl = str(data.get("implementation") or "").lower()
    for key, height in DEFAULT_MOUNT_HEIGHT_BY_TYPE.items():
        if key in impl:
            return height
    return DEFAULT_MOUNT_HEIGHT_CM


def sensor_z_cm(data: dict, apartment_floor_cm: float = 0.0) -> float:
    """Antennenhöhe relativ zum Fußboden der Wohnung (cm).

    Sensor auf demselben Stockwerk: nur die Montagehöhe. Steht er auf einem anderen
    Stockwerk (``floor_cm`` gesetzt), kommt der Höhenunterschied der Fußböden dazu."""
    floor = data.get("floor_cm")
    offset = 0.0 if floor is None else float(floor) - float(apartment_floor_cm or 0.0)
    return offset + mount_height_cm(data)


def validate_calibration(value: Any) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Kalibrierung muss ein JSON-Objekt sein.")
    result = dict(DEFAULT_CALIBRATION)
    for field in CALIBRATION_FIELDS:
        if field in value and value[field] is not None:
            result[field] = _finite_float(value[field], field)
    if result["n_factor"] <= 0:
        raise ValueError("n_factor muss größer als 0 sein.")
    if result["r_min"] < 0 or result["r_max"] < 0 or result["q_variance"] < 0:
        raise ValueError("Varianzen dürfen nicht negativ sein.")
    if result["sigma_db"] <= 0:
        raise ValueError("sigma_db muss größer als 0 sein.")
    if result["rssi_limit"] >= result["tx_power"]:
        raise ValueError("rssi_limit muss kleiner als tx_power sein.")
    return result


def validate_sensor_id(value: Any) -> str:
    sensor_id = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", sensor_id):
        raise ValueError(f"Ungültige sensor_id: {value!r}")
    return sensor_id


@dataclass
class SensorConfig:
    sensor_id: str
    data: dict
    filepath: Optional[str] = None


class ConfigStore:
    """Lädt, validiert und speichert die dauerhafte Tracker-Konfiguration."""

    def __init__(self, config_dir: str):
        self.config_dir = os.path.abspath(config_dir)
        self.sensors: dict = {}
        self.state_file = os.path.join(self.config_dir, "tracker_state.json")
        self.tracker_state = {"target_mac": DEFAULT_TARGET_MAC, "engine": None}
        self.last_change = 0.0
        self._load_tracker_state()

    # ------------------------------------------------------------------
    # Tracker-Zustand (Ziel-MAC, Modellwahl)
    # ------------------------------------------------------------------
    def _load_tracker_state(self):
        try:
            with open(self.state_file, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                self.tracker_state.update(data)
        except (OSError, json.JSONDecodeError):
            pass

    def _save_tracker_state(self):
        os.makedirs(self.config_dir, exist_ok=True)
        _atomic_write_json(self.state_file, self.tracker_state, self.config_dir)

    @property
    def target_mac(self) -> str:
        return str(self.tracker_state.get("target_mac") or "")

    @target_mac.setter
    def target_mac(self, value: str):
        self.tracker_state["target_mac"] = str(value or "")
        self._save_tracker_state()

    @property
    def engine(self) -> Optional[str]:
        value = self.tracker_state.get("engine")
        return value if value in ENGINES else None

    @engine.setter
    def engine(self, value: str):
        if value not in ENGINES:
            raise ValueError(f"Unbekanntes Modell: {value!r} (erlaubt: {', '.join(ENGINES)})")
        self.tracker_state["engine"] = value
        self._save_tracker_state()

    # ------------------------------------------------------------------
    # Sensoren
    # ------------------------------------------------------------------
    def load(self) -> dict:
        os.makedirs(self.config_dir, exist_ok=True)
        self.sensors.clear()
        topics: set = set()
        addresses: dict = {}
        for filepath in sorted(glob.glob(os.path.join(self.config_dir, "*.json"))):
            name = os.path.basename(filepath)
            if name in NON_SENSOR_FILES or name.startswith("."):
                continue
            try:
                with open(filepath, "r", encoding="utf-8") as handle:
                    raw = json.load(handle)
                if isinstance(raw, dict) and not ({"sensor_id", "topic", "pos"} & set(raw)):
                    continue  # keine Sensor-Datei (z. B. Einstellungen)
                config = self.normalize_sensor(raw, filepath)
                if config.sensor_id in self.sensors:
                    raise ValueError(f"Doppelte sensor_id: {config.sensor_id}")
                if config.data["topic"] in topics:
                    raise ValueError(f"Doppeltes MQTT-Topic: {config.data['topic']}")
                for address in config.data["ble_addresses"]:
                    if address in addresses and addresses[address] != config.sensor_id:
                        raise ValueError(f"BLE-MAC {address} doppelt ({addresses[address]}, {config.sensor_id})")
                    addresses[address] = config.sensor_id
                topics.add(config.data["topic"])
                self.sensors[config.sensor_id] = config
            except (OSError, json.JSONDecodeError, TypeError, ValueError, KeyError) as error:
                print(f"Konfiguration übersprungen ({filepath}): {error}")
        return self.sensors

    def normalize_sensor(self, raw: dict, filepath: Optional[str] = None) -> SensorConfig:
        if not isinstance(raw, dict):
            raise ValueError("Sensor-Konfiguration muss ein Objekt sein.")
        filename_id = os.path.splitext(os.path.basename(filepath))[0] if filepath else "sensor"
        sensor_id = validate_sensor_id(raw.get("sensor_id", filename_id))
        data = deepcopy(raw)
        data["sensor_id"] = sensor_id
        data["name"] = str(data.get("name") or sensor_id.replace("_", " ").title())
        data["implementation"] = str(data.get("implementation") or "unknown")
        data["enabled"] = parse_bool(data.get("enabled", True))
        data["topic"] = str(data.get("topic") or f"bluecat/{sensor_id}/sensor/state")
        data["availability_topic"] = str(data.get("availability_topic") or f"bluecat/{sensor_id}/sensor/status")
        data["pos"] = validate_position(data.get("pos", DEFAULT_POSITION))
        for key, check in (("height_cm", validate_height), ("floor_cm", validate_floor)):
            if data.get(key) is not None:
                try:
                    data[key] = check(data[key])
                except (TypeError, ValueError) as error:
                    print(f"Warnung {sensor_id}: {key} ignoriert ({error})")
                    data.pop(key, None)
        data["ble_addresses"] = self._normalize_addresses(data.get("ble_addresses", data.get("ble_address", [])))
        calibration_input = {field: data[field] for field in CALIBRATION_FIELDS if field in data}
        try:
            calibration = validate_calibration(calibration_input)
            issues = []
        except (TypeError, ValueError) as error:
            calibration = dict(DEFAULT_CALIBRATION)
            issues = [str(error)]
        data.update(calibration)
        if issues:
            data["calibration_status"] = "needs_recalibration"
            data["calibration_note"] = "; ".join(issues)
            print(f"Warnung {sensor_id}: unplausible Kalibrierung; Standardwerte verwendet ({data['calibration_note']})")
        else:
            data.setdefault("calibration_status", "uncalibrated")
        if "position_configured" in raw:
            data["position_configured"] = parse_bool(raw["position_configured"])
        else:
            # Nur eine explizit gesetzte, von (0,0) verschiedene Position zählt.
            data["position_configured"] = "pos" in raw and data["pos"] != DEFAULT_POSITION
        return SensorConfig(sensor_id, data, filepath)

    @staticmethod
    def _normalize_addresses(value: Any) -> list:
        if value in (None, ""):
            return []
        values = value if isinstance(value, list) else [value]
        normalized = []
        for address in values:
            if address in (None, ""):
                continue
            formatted = format_ble_address(address)
            if formatted not in normalized:
                normalized.append(formatted)
        return normalized

    def upsert_identity(self, identity: dict) -> Tuple[SensorConfig, bool, bool]:
        """Übernimmt eine Sensor-Identität.

        Rückgabe: (Konfiguration, geändert, neu). Unveränderte Identitäten
        (Heartbeats) lösen weder Speichern noch Neuaufbau aus. ``enabled``
        aus der Identity wird nur bei neuen Sensoren übernommen – ein in Home
        Assistant deaktivierter Sensor bleibt deaktiviert.
        """
        sensor_id = validate_sensor_id(identity.get("sensor_id"))
        current = self.sensors.get(sensor_id)
        is_new = current is None
        if is_new:
            filepath = os.path.join(self.config_dir, f"{sensor_id}.json")
            data = self.normalize_sensor(
                {
                    "sensor_id": sensor_id,
                    "name": identity.get("name", sensor_id),
                    "implementation": identity.get("implementation", "unknown"),
                    "enabled": parse_bool(identity.get("enabled", True)),
                    "topic": identity.get("state_topic"),
                    "pos": DEFAULT_POSITION,
                    "position_configured": False,
                },
                filepath,
            ).data
            current = SensorConfig(sensor_id, data, filepath)
        data = deepcopy(current.data)
        for source, target in (
            ("name", "name" if data.get("name_source") != "mqtt" else None),
            ("implementation", "implementation"),
            ("state_topic", "topic"),
            ("availability_topic", "availability_topic"),
            ("mesh_topic", "mesh_topic"),
            ("ip", "ip"),
            ("version", "firmware_version"),
            ("hostname", "hostname"),
        ):
            if target and identity.get(source):
                data[target] = str(identity[source])
        for other_id, other in self.sensors.items():
            if other_id != sensor_id and data.get("topic") == other.data.get("topic"):
                raise ValueError(f"MQTT-Topic ist bereits Sensor {other_id} zugeordnet.")
        if identity.get("ble_mac") and data.get("ble_mac_source") != "mqtt":
            addresses = self._normalize_addresses(identity["ble_mac"])
            for other_id, other in self.sensors.items():
                if other_id != sensor_id and set(addresses) & set(other.data.get("ble_addresses", [])):
                    raise ValueError("BLE-MAC ist bereits einem anderen Sensor zugeordnet.")
            data["ble_addresses"] = addresses
            data["ble_mac_source"] = "identity"
        changed = is_new or data != current.data
        if changed:
            current.data = data
            self.sensors[sensor_id] = current
            self.save(current)
        return current, changed, is_new

    def update_field(self, sensor_id: str, field: str, value: Any) -> SensorConfig:
        sensor_id = validate_sensor_id(sensor_id)
        if sensor_id not in self.sensors:
            raise KeyError(f"Unbekannter Sensor: {sensor_id}")
        config = self.sensors[sensor_id]
        data = deepcopy(config.data)
        if field == "ble_mac":
            if isinstance(value, dict):
                value = value.get("ble_mac", value.get("mac", ""))
            addresses = self._normalize_addresses(value)
            for other_id, other in self.sensors.items():
                if other_id != sensor_id and set(addresses) & set(other.data.get("ble_addresses", [])):
                    raise ValueError(f"BLE-MAC ist bereits Sensor {other_id} zugeordnet.")
            data["ble_addresses"] = addresses
            data["ble_mac_source"] = "mqtt"
        elif field == "position":
            if isinstance(value, dict):
                value = [value.get("x_cm"), value.get("y_cm")]
            data["pos"] = validate_position(value)
            data["position_configured"] = True
        elif field == "height":
            if isinstance(value, dict):
                value = value.get("height_cm")
            if value is None or (isinstance(value, str) and not value.strip()):
                data.pop("height_cm", None)       # zurück auf die Standardhöhe
            else:
                data["height_cm"] = validate_height(value)
        elif field == "floor":
            if isinstance(value, dict):
                value = value.get("floor_cm")
            floor = validate_floor(value)
            if floor is None:
                data.pop("floor_cm", None)
            else:
                data["floor_cm"] = floor
        elif field == "calibration":
            current = {key: data.get(key, DEFAULT_CALIBRATION[key]) for key in CALIBRATION_FIELDS}
            geometry = value.pop("calibration_geometry", None) if isinstance(value, dict) else None
            current.update(value)
            data.update(validate_calibration(current))
            data["calibration_status"] = "calibrated"
            if geometry == "3d":  # mit Sensorhöhen gerechnet (sonst bleibt die bisherige Angabe)
                data["calibration_geometry"] = "3d"
        elif field == "enabled":
            data["enabled"] = parse_bool(value)
        elif field in {"name", "implementation"}:
            data[field] = str(value)
            if field == "name":
                data["name_source"] = "mqtt"  # Identity überschreibt den Namen nicht mehr
        else:
            raise ValueError(f"Nicht unterstütztes Konfigurationsfeld: {field}")
        config.data = data
        self.save(config)
        return config

    def remove(self, sensor_id: str) -> Optional[SensorConfig]:
        """Entfernt einen Sensor; die Datei wird nach config/removed/ verschoben."""
        config = self.sensors.pop(validate_sensor_id(sensor_id), None)
        if config is not None and config.filepath and os.path.exists(config.filepath):
            target_dir = os.path.join(self.config_dir, "removed")
            os.makedirs(target_dir, exist_ok=True)
            os.replace(config.filepath, os.path.join(
                target_dir, f"{config.sensor_id}.{time.strftime('%Y%m%d-%H%M%S')}.json"))
        self.last_change = time.time()
        return config

    def save(self, config: SensorConfig) -> None:
        filepath = config.filepath or os.path.join(self.config_dir, f"{config.sensor_id}.json")
        config.filepath = filepath
        _atomic_write_json(filepath, _json_safe(config.data), self.config_dir)
        self.last_change = time.time()

    def snapshot(self) -> dict:
        return {sensor_id: deepcopy(config.data) for sensor_id, config in self.sensors.items()}


def _atomic_write_json(filepath, payload, directory):
    os.makedirs(os.path.dirname(filepath) or directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".tmp.", suffix=".json", dir=os.path.dirname(filepath) or directory, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=4, ensure_ascii=False)
            handle.write("\n")
        os.replace(temporary, filepath)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _json_safe(value: Any) -> Any:
    if hasattr(value, "tolist"):
        return value.tolist()
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def identity_payload(sensor_id: str, ble_mac: str, implementation: str, state_topic: str, enabled: bool = True) -> dict:
    return {
        "schema": 1,
        "sensor_id": validate_sensor_id(sensor_id),
        "ble_mac": format_ble_address(ble_mac),
        "implementation": str(implementation),
        "state_topic": str(state_topic),
        "enabled": bool(enabled),
        "timestamp": int(time.time()),
    }


def mesh_peers_payload(sensors: dict) -> dict:
    peers = []
    for sensor_id, config in sorted(sensors.items()):
        if not config.data.get("enabled", True):
            continue
        for address in config.data.get("ble_addresses", []):
            peers.append({"sensor_id": sensor_id, "ble_mac": address})
    # Kein Zeitstempel: identischer Inhalt → identisches retained Payload
    return {"schema": 1, "peers": peers}


def calibration_payload(data: dict) -> dict:
    return validate_calibration(data)
