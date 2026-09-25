import json
import math
from dataclasses import dataclass
from typing import Optional, Union

from config_manager import normalize_ble_address


@dataclass
class SensorReading:
    """Halsband-Messung eines Sensors (Sichtung oder „nicht gesehen“)."""
    rssi: float
    present: bool
    sensor_id: Optional[str] = None
    tag_id: Optional[str] = None
    timestamp: Optional[float] = None
    sequence: Optional[int] = None
    sample_count: int = 1


@dataclass
class MeshBeacon:
    """Infrastruktur-Paket: Ein Sensor hat einen anderen Sensor gesehen."""
    transmitter_mac: Optional[str]
    transmitter_name: Optional[str]
    receiver_name: Optional[str]
    rssi: float


def _to_float(value, default=None):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _to_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class PayloadParser:
    """Wandelt Sensor-Payloads (JSON oder nackte Zahl) in Objekte um."""

    @staticmethod
    def parse(decoded_str: str) -> Union[SensorReading, MeshBeacon, None]:
        try:
            payload = json.loads(decoded_str)
        except (json.JSONDecodeError, TypeError):
            number = _to_float(decoded_str)
            if number is None:
                return None
            payload = number

        if isinstance(payload, bool):
            return None
        if isinstance(payload, (int, float)):
            rssi = float(payload)
            return SensorReading(rssi=rssi, present=rssi > -120.0)
        if not isinstance(payload, dict):
            return None

        message_type = str(payload.get("message_type", "tag_rssi")).lower()
        if message_type in {"sensor_beacon", "sensor_beacon_rssi", "anchor_beacon"}:
            return PayloadParser._parse_mesh_beacon(payload)
        if message_type not in {"tag_rssi", "tag"}:
            return None
        return PayloadParser._parse_sensor_reading(payload)

    @staticmethod
    def _parse_mesh_beacon(payload: dict) -> Optional[MeshBeacon]:
        rssi = _to_float(payload.get("rssi"))
        if rssi is None:
            return None
        source_mac = None
        for key in ("beacon_mac", "source_mac", "transmitter_mac"):
            val = payload.get(key)
            if val and str(val).strip():
                try:
                    source_mac = normalize_ble_address(val)
                    break
                except ValueError:
                    pass
        source_sensor = None
        for key in ("beacon_sensor", "source_sensor", "transmitter"):
            val = payload.get(key)
            if val and str(val).strip():
                source_sensor = str(val).strip()
                break
        receiver = payload.get("sensor_id")
        return MeshBeacon(
            transmitter_mac=source_mac,
            transmitter_name=source_sensor,
            receiver_name=str(receiver) if receiver else None,
            rssi=rssi,
        )

    @staticmethod
    def _parse_sensor_reading(payload: dict) -> Optional[SensorReading]:
        rssi = _to_float(payload.get("rssi"), -130.0)
        present = payload.get("present")
        if present is None:
            present = rssi > -120.0
        elif isinstance(present, str):
            present = present.strip().lower() not in {"false", "0", "off", "no"}
        else:
            present = bool(present)
        present = present and rssi > -120.0

        timestamp = _to_float(payload.get("timestamp"))
        if timestamp is not None:
            if timestamp > 1.0e12:
                timestamp /= 1000.0
            if timestamp <= 0:
                timestamp = None

        tag_id = None
        for key in ("tag_id", "device_id", "mac", "address", "uuid", "id"):
            val = payload.get(key)
            if val and str(val).strip():
                tag_id = str(val).strip()
                break

        count = _to_int(payload.get("sample_count"), 1)
        sensor_id = payload.get("sensor_id")
        return SensorReading(
            rssi=rssi,
            present=present,
            sensor_id=str(sensor_id) if sensor_id else None,
            tag_id=tag_id,
            timestamp=timestamp,
            sequence=_to_int(payload.get("sequence")),
            sample_count=max(count if count is not None else 1, 0),
        )
