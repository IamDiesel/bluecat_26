"""Zentrale Konfiguration und MQTT-Schnittstelle für TriLola.

Die MQTT-Konfiguration ist absichtlich klein und stabil gehalten.  Sensoren
benötigen nur Broker-Zugangsdaten sowie ihre lokale ``sensor_id``; alle
veränderlichen Betriebsdaten können anschließend über MQTT/Home Assistant
verwaltet werden.

Der Tracker bleibt dabei rückwärtskompatibel zu den JSON-Dateien im
``config``-Ordner.  MQTT-Änderungen werden validiert, atomar gespeichert und
als retained ``state``-Nachricht bestätigt.
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
from typing import Any


REGISTRY_ROOT = "bluecat/registry"
IDENTITY_TOPIC_PATTERN = REGISTRY_ROOT + "/{sensor_id}/identity"
MESH_PEERS_TOPIC = REGISTRY_ROOT + "/mesh_peers"
CONFIG_ROOT = "bluecat/config"
TARGET_MAC_SET_TOPIC = CONFIG_ROOT + "/target_mac/set"
TARGET_MAC_STATE_TOPIC = CONFIG_ROOT + "/target_mac/state"
SENSOR_CONFIG_SET_PATTERN = CONFIG_ROOT + "/sensors/{sensor_id}/{field}/set"
SENSOR_CONFIG_STATE_PATTERN = CONFIG_ROOT + "/sensors/{sensor_id}/{field}/state"

CALIBRATION_FIELDS = ("tx_power", "n_factor", "r_min", "r_max", "rssi_limit", "q_variance")
DEFAULT_CALIBRATION = {
    "tx_power": -59.0,
    "n_factor": 3.0,
    "r_min": 5.0,
    "r_max": 20.0,
    "rssi_limit": -110.0,
    "q_variance": 0.1,
}
DEFAULT_POSITION = [0.0, 0.0]
DEFAULT_TARGET_MAC = ""


def normalize_ble_address(value: Any) -> str:
    """Gibt eine BLE-MAC ohne Trennzeichen in Kleinbuchstaben zurück."""
    normalized = re.sub(r"[^0-9a-fA-F]", "", str(value or ""))
    if not re.fullmatch(r"[0-9a-f]{12}", normalized):
        raise ValueError(f"Ungültige BLE-Adresse: {value!r}")
    return normalized


def format_ble_address(value: Any) -> str:
    """Normalisiert eine MAC und formatiert sie als ``aa:bb:...``."""
    normalized = normalize_ble_address(value)
    return ":".join(normalized[index:index + 2] for index in range(0, 12, 2))


def _finite_float(value: Any, field: str) -> float:
    number = float(value)
    if not (-1.0e12 < number < 1.0e12):
        raise ValueError(f"{field} ist nicht plausibel.")
    return number


def validate_position(value: Any) -> list[float]:
    """Validiert eine zweidimensionale Position in Zentimetern."""
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError("pos muss genau zwei Werte enthalten.")
    return [_finite_float(value[0], "x_cm"), _finite_float(value[1], "y_cm")]


def validate_calibration(value: Any) -> dict[str, float]:
    """Validiert Kalibrierwerte und ergänzt fehlende Felder durch Defaults."""
    if not isinstance(value, dict):
        raise ValueError("Kalibrierung muss ein JSON-Objekt sein.")
    result = dict(DEFAULT_CALIBRATION)
    for field in CALIBRATION_FIELDS:
        if field in value:
            result[field] = _finite_float(value[field], field)
    if result["n_factor"] <= 0:
        raise ValueError("n_factor muss größer als 0 sein.")
    if result["r_min"] < 0 or result["r_max"] < 0 or result["q_variance"] < 0:
        raise ValueError("Varianzen dürfen nicht negativ sein.")
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
    """Normalisierte, serialisierbare Sensor-Konfiguration."""

    sensor_id: str
    data: dict[str, Any]
    filepath: str | None = None


class ConfigStore:
    """Lädt, validiert und speichert die dauerhafte Tracker-Konfiguration."""

    def __init__(self, config_dir: str):
        self.config_dir = os.path.abspath(config_dir)
        self.sensors: dict[str, SensorConfig] = {}
        self.target_mac = DEFAULT_TARGET_MAC
        self.last_change = 0.0

    def load(self) -> dict[str, SensorConfig]:
        os.makedirs(self.config_dir, exist_ok=True)
        self.sensors.clear()
        topics: set[str] = set()
        addresses: dict[str, str] = {}
        for filepath in sorted(glob.glob(os.path.join(self.config_dir, "*.json"))):
            if os.path.basename(filepath) == "radio_mesh_baseline.json":
                continue
            try:
                with open(filepath, "r", encoding="utf-8") as handle:
                    raw = json.load(handle)
                config = self.normalize_sensor(raw, filepath)
                if config.sensor_id in self.sensors:
                    raise ValueError(f"Doppelte sensor_id: {config.sensor_id}")
                if config.data["topic"] in topics:
                    raise ValueError(f"Doppeltes MQTT-Topic: {config.data['topic']}")
                for address in config.data["ble_addresses"]:
                    if address in addresses and addresses[address] != config.sensor_id:
                        raise ValueError(
                            f"BLE-MAC {address} doppelt ({addresses[address]}, {config.sensor_id})"
                        )
                    addresses[address] = config.sensor_id
                topics.add(config.data["topic"])
                # Auch deaktivierte Sensoren bleiben im Store, damit sie
                # später über MQTT wieder aktiviert werden können.
                self.sensors[config.sensor_id] = config
            except (OSError, json.JSONDecodeError, TypeError, ValueError, KeyError) as error:
                print(f"Konfiguration übersprungen ({filepath}): {error}")
        return self.sensors

    def normalize_sensor(self, raw: dict[str, Any], filepath: str | None = None) -> SensorConfig:
        if not isinstance(raw, dict):
            raise ValueError("Sensor-Konfiguration muss ein Objekt sein.")
        filename_id = (
            os.path.splitext(os.path.basename(filepath))[0]
            if filepath
            else "sensor"
        )
        sensor_id = validate_sensor_id(raw.get("sensor_id", filename_id))
        data = deepcopy(raw)
        data["sensor_id"] = sensor_id
        data["name"] = str(data.get("name") or sensor_id.replace("_", " ").title())
        data["implementation"] = str(data.get("implementation") or "unknown")
        data["enabled"] = bool(data.get("enabled", True))
        data["topic"] = str(data.get("topic") or f"bluecat/{sensor_id}/sensor/state")
        data["availability_topic"] = str(
            data.get("availability_topic") or f"bluecat/{sensor_id}/sensor/status"
        )
        data["pos"] = validate_position(data.get("pos", DEFAULT_POSITION))
        data["ble_addresses"] = self._normalize_addresses(
            data.get("ble_addresses", data.get("ble_address", []))
        )
        calibration_input = {
            field: data[field] for field in CALIBRATION_FIELDS if field in data
        }
        calibration_issues = []
        try:
            calibration = validate_calibration(calibration_input)
        except (TypeError, ValueError) as error:
            # Ein alter oder teilweise kaputter JSON-Stand darf den Tracker
            # nicht komplett stoppen. MQTT-Updates bleiben dagegen strikt.
            calibration = dict(DEFAULT_CALIBRATION)
            calibration_issues.append(str(error))
        data.update(calibration)
        if calibration_issues:
            data["calibration_status"] = "needs_recalibration"
            data["calibration_note"] = "; ".join(calibration_issues)
            print(
                f"Warnung {sensor_id}: unplausible Kalibrierung; "
                f"Standardwerte verwendet ({data['calibration_note']})"
            )
        else:
            data.setdefault("calibration_status", "uncalibrated")
        data.setdefault("position_configured", "pos" in raw)
        return SensorConfig(sensor_id, data, filepath)

    @staticmethod
    def _normalize_addresses(value: Any) -> list[str]:
        if value in (None, ""):
            return []
        values = value if isinstance(value, list) else [value]
        normalized = []
        for address in values:
            formatted = format_ble_address(address)
            if formatted not in normalized:
                normalized.append(formatted)
        return normalized

    def upsert_identity(self, identity: dict[str, Any]) -> SensorConfig:
        """Übernimmt eine Sensor-Identität, ohne Positionsdaten zu zerstören."""
        sensor_id = validate_sensor_id(identity.get("sensor_id"))
        current = self.sensors.get(sensor_id)
        if current is None:
            filepath = os.path.join(self.config_dir, f"{sensor_id}.json")
            current = SensorConfig(
                sensor_id,
                self.normalize_sensor(
                    {
                        "sensor_id": sensor_id,
                        "name": identity.get("name", sensor_id),
                        "implementation": identity.get("implementation", "unknown"),
                        "enabled": bool(identity.get("enabled", True)),
                        "topic": identity.get("state_topic"),
                        "pos": DEFAULT_POSITION,
                        "position_configured": False,
                    },
                    filepath,
                ).data,
                filepath,
            )
            self.sensors[sensor_id] = current
        data = current.data
        for source, target in (
            ("name", "name"),
            ("implementation", "implementation"),
            ("state_topic", "topic"),
            ("availability_topic", "availability_topic"),
        ):
            if identity.get(source):
                data[target] = str(identity[source])
        candidate_topic = data.get("topic")
        for other_id, other in self.sensors.items():
            if other_id != sensor_id and candidate_topic == other.data.get("topic"):
                raise ValueError(
                    f"MQTT-Topic ist bereits Sensor {other_id} zugeordnet."
                )
        if identity.get("ble_mac") and data.get("ble_mac_source") != "mqtt":
            addresses = self._normalize_addresses(identity["ble_mac"])
            for other_id, other in self.sensors.items():
                if other_id != sensor_id and set(addresses) & set(
                    other.data.get("ble_addresses", [])
                ):
                    raise ValueError("BLE-MAC ist bereits einem anderen Sensor zugeordnet.")
            data["ble_addresses"] = addresses
            data["ble_mac_source"] = "identity"
        data["enabled"] = bool(identity.get("enabled", data.get("enabled", True)))
        self.save(current)
        return current

    def update_field(self, sensor_id: str, field: str, value: Any) -> SensorConfig:
        sensor_id = validate_sensor_id(sensor_id)
        if sensor_id not in self.sensors:
            raise KeyError(f"Unbekannter Sensor: {sensor_id}")
        config = self.sensors[sensor_id]
        data = config.data
        if field == "ble_mac":
            if isinstance(value, dict):
                value = value.get("ble_mac", value.get("mac", ""))
            data["ble_addresses"] = self._normalize_addresses(value)
            data["ble_mac_source"] = "mqtt"
        elif field == "position":
            if isinstance(value, dict):
                value = [value.get("x_cm"), value.get("y_cm")]
            data["pos"] = validate_position(value)
            data["position_configured"] = True
        elif field == "calibration":
            current_calibration = {
                key: data.get(key, DEFAULT_CALIBRATION[key])
                for key in CALIBRATION_FIELDS
            }
            current_calibration.update(value)
            data.update(validate_calibration(current_calibration))
            data["calibration_status"] = "calibrated"
        elif field in {"enabled", "name", "implementation"}:
            data[field] = bool(value) if field == "enabled" else str(value)
        else:
            raise ValueError(f"Nicht unterstütztes Konfigurationsfeld: {field}")
        self.save(config)
        return config

    def save(self, config: SensorConfig) -> None:
        """Schreibt eine Konfiguration atomar und aktualisiert den Zeitstempel."""
        filepath = config.filepath or os.path.join(
            self.config_dir, f"{config.sensor_id}.json"
        )
        config.filepath = filepath
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        serializable = _json_safe(config.data)
        fd, temporary = tempfile.mkstemp(
            prefix=f".{config.sensor_id}.",
            suffix=".tmp",
            dir=self.config_dir,
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(serializable, handle, indent=4, ensure_ascii=False)
                handle.write("\n")
            os.replace(temporary, filepath)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        self.last_change = time.time()

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return {sensor_id: deepcopy(config.data) for sensor_id, config in self.sensors.items()}


def _json_safe(value: Any) -> Any:
    if hasattr(value, "tolist"):
        return value.tolist()
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def identity_payload(sensor_id: str, ble_mac: str, implementation: str, state_topic: str, enabled: bool = True) -> dict[str, Any]:
    """Erzeugt das einheitliche retained Identity-Payload."""
    return {
        "schema": 1,
        "sensor_id": validate_sensor_id(sensor_id),
        "ble_mac": format_ble_address(ble_mac),
        "implementation": str(implementation),
        "state_topic": str(state_topic),
        "enabled": bool(enabled),
        "timestamp": int(time.time()),
    }


def mesh_peers_payload(sensors: dict[str, SensorConfig]) -> dict[str, Any]:
    peers = []
    for sensor_id, config in sorted(sensors.items()):
        if not config.data.get("enabled", True):
            continue
        for address in config.data.get("ble_addresses", []):
            peers.append({"sensor_id": sensor_id, "ble_mac": address})
    return {"schema": 1, "generated_at": int(time.time()), "peers": peers}


def calibration_payload(data: dict[str, Any]) -> dict[str, float]:
    return validate_calibration(data)
