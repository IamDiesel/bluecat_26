import json
import numpy as np
from dataclasses import dataclass
from typing import Optional, Union

# Wir importieren die Normalisierungsfunktion aus dem bestehenden config_manager
from config_manager import normalize_ble_address

@dataclass
class SensorReading:
    """Standard-Paket: Lola wurde von einem Sensor gesehen."""
    rssi: float
    present: bool
    sensor_id: Optional[str] = None
    tag_id: Optional[str] = None
    timestamp: Optional[float] = None
    sequence: Optional[int] = None

@dataclass
class MeshBeacon:
    """Infrastruktur-Paket: Ein Sensor hat einen anderen Sensor gesehen."""
    transmitter_mac: Optional[str]
    transmitter_name: Optional[str]
    receiver_name: Optional[str]
    rssi: float

class PayloadParser:
    """Zentrale Instanz, die wilde Strings und JSONs in saubere Objekte verwandelt."""
    
    @staticmethod
    def parse(decoded_str: str) -> Union[SensorReading, MeshBeacon, None]:
        try:
            payload = json.loads(decoded_str)
        except json.JSONDecodeError:
            try:
                # Unterstützung für alte Sensoren, die nur "-67.0" senden
                payload = float(decoded_str)
            except ValueError:
                return None  # Weder JSON noch Zahl, ignorieren

        if isinstance(payload, (int, float)):
            return SensorReading(
                rssi=float(payload),
                present=float(payload) > -120.0
            )

        if not isinstance(payload, dict):
            return None

        message_type = str(payload.get("message_type", "tag_rssi")).lower()
        
        # 1. Ist es ein Netzwerk-Infrastruktur Paket (SLAM)?
        if message_type in {"sensor_beacon", "sensor_beacon_rssi", "anchor_beacon"}:
            return PayloadParser._parse_mesh_beacon(payload)

        # 2. Ansonsten ist es ein normales Tracking-Paket
        return PayloadParser._parse_sensor_reading(payload)

    @staticmethod
    def _parse_mesh_beacon(payload: dict) -> MeshBeacon:
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
                
        return MeshBeacon(
            transmitter_mac=source_mac,
            transmitter_name=source_sensor,
            receiver_name=payload.get("sensor_id"),
            rssi=float(payload.get("rssi", -130.0))
        )

    @staticmethod
    def _parse_sensor_reading(payload: dict) -> SensorReading:
        rssi = float(payload.get("rssi", -130.0))
        
        # String/Bool Chaos für Präsenz aufräumen
        present = payload.get("present")
        if present is None:
            present = rssi > -120.0
        elif isinstance(present, str):
            present = present.strip().lower() not in {"false", "0", "off", "no"}
        else:
            present = bool(present)
        
        # Doppelte Absicherung
        present = present and (rssi > -120.0)

        # Millisekunden zu Sekunden konvertieren, falls nötig
        timestamp = payload.get("timestamp")
        if timestamp is not None:
            timestamp = float(timestamp)
            if timestamp > 1.0e12:
                timestamp /= 1000.0
            if not np.isfinite(timestamp) or timestamp <= 0:
                timestamp = None

        sequence = payload.get("sequence")
        if sequence is not None:
            try:
                sequence = int(sequence)
            except (TypeError, ValueError):
                sequence = None

        # Die ID-Schnitzeljagd (Verschiedene Firmware-Versionen senden verschiedene Keys)
        tag_id = None
        for key in ("tag_id", "device_id", "mac", "address", "uuid", "id"):
            val = payload.get(key)
            if val and str(val).strip():
                tag_id = str(val).strip()
                break

        return SensorReading(
            rssi=rssi,
            present=present,
            sensor_id=payload.get("sensor_id"),
            tag_id=tag_id,
            timestamp=timestamp,
            sequence=sequence
        )