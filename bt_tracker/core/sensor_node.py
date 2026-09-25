import math
from collections import deque

import numpy as np

from filters import TrackingFilter

# Felder, deren Änderung einen neuen SensorNode erfordert.
CONFIG_SIGNATURE_FIELDS = (
    "pos", "tx_power", "n_factor", "r_min", "r_max", "rssi_limit", "q_variance",
    "sigma_db", "detection_floor", "topic", "z_cm", "antenna_z_cm",
)


def config_signature(data: dict) -> tuple:
    sig = []
    for key in CONFIG_SIGNATURE_FIELDS:
        value = data.get(key)
        if isinstance(value, (list, tuple, np.ndarray)):
            value = tuple(round(float(v), 3) for v in value)
        elif isinstance(value, float):
            value = round(value, 4)
        sig.append(value)
    return tuple(sig)


class SensorNode:
    """Konfiguration, Laufzeitzustand und (Legacy-)Filter eines Sensors.

    Zeitbegriffe:
    * ``last_heard``: letzte Nachricht jeglicher Art (auch „nicht gesehen“)
    * ``last_seen``:  letzte Nachricht mit Halsband-Sichtung
    """

    def __init__(self, sensor_id, data_dict):
        self.sensor_id = sensor_id
        self.signature = config_signature(data_dict)
        self.name = data_dict.get("name", sensor_id)
        self.topic = data_dict.get("topic", "")
        self.enabled = data_dict.get("enabled", True)
        self.pos = np.asarray(data_dict.get("pos", [0.0, 0.0]), dtype=float)
        # Antennenhöhe über dem Fußboden der Wohnung (cm); None = wie das Halsband (reines 2D)
        z = data_dict.get("z_cm")
        self.z_cm = None if z is None else float(z)
        # physische Antennenhöhe (Mesh), auch wenn das Halsband-Modell noch eben rechnet
        antenna = data_dict.get("antenna_z_cm", z)
        self.antenna_z_cm = None if antenna is None else float(antenna)
        self.ble_addresses = list(data_dict.get("ble_addresses", []))
        self.implementation = data_dict.get("implementation", "unknown")

        # Kalibrierung
        self.tx_power = float(data_dict.get("tx_power", -59.0))
        self.n_factor = float(data_dict.get("n_factor", 3.0))
        self.r_min = float(data_dict.get("r_min", 5.0))
        self.r_max = float(data_dict.get("r_max", 20.0))
        self.rssi_limit = float(data_dict.get("rssi_limit", -110.0))
        self.q_variance = float(data_dict.get("q_variance", 0.1))
        # Neues Modell: Shadowing-Streuung und Empfangsschwelle
        self.sigma_db = float(data_dict.get("sigma_db", 4.0))
        self.detection_floor = float(data_dict.get("detection_floor", -97.0))

        self.filter = TrackingFilter(
            tx_power=self.tx_power, r_min=self.r_min, r_max=self.r_max,
            rssi_limit=self.rssi_limit, q_variance=self.q_variance,
        )
        self.pending = deque(maxlen=64)
        self.last_heard = None
        self.last_seen = None
        self.reset_state()

    # ------------------------------------------------------------------
    def reset_filter(self):
        """Setzt nur die Filter- und abgeleiteten Werte zurück."""
        self.filtered_rssi = None
        self.corrected_rssi = None
        self.distance_cm = None
        self.distance_std_cm = None
        self.processed_seq = self.sample_seq
        self.filter.reset()

    def reset_state(self):
        """Setzt den kompletten Laufzeitzustand zurück (außer Zeitstempeln)."""
        self.last_device_timestamp = None
        self.rssi = None
        self.present = False
        self.last_sequence = None
        self.sample_count = 0
        self.sample_seq = 0
        self.pending.clear()
        self.reset_filter()
        self.processed_seq = -1

    def fast_fading_variance(self, rssi):
        """Varianz eines Einzelwerts (r_min nah … r_max fern), wie kalibriert."""
        span = self.tx_power - self.rssi_limit
        if span <= 1e-6:
            return np.full_like(np.asarray(rssi, dtype=float), max(self.r_max, 0.25))
        frac = np.clip((self.tx_power - np.asarray(rssi, dtype=float)) / span, 0.0, 1.0)
        return np.maximum(self.r_min + frac * (self.r_max - self.r_min), 0.25)

    # ------------------------------------------------------------------
    def apply_reading(self, rssi, timestamp, present, sequence=None, device_timestamp=None,
                      sample_count=1):
        """Nimmt eine Nachricht auf. Gibt True zurück, wenn sie neu ist."""
        try:
            rssi = float(rssi)
        except (TypeError, ValueError):
            return False
        if not np.isfinite(rssi):
            return False

        self.last_heard = timestamp
        if sequence is not None and self.last_sequence is not None and sequence == self.last_sequence:
            return False  # QoS-1-Duplikat

        present = bool(present) and rssi > -120.0
        self.last_sequence = sequence
        self.last_device_timestamp = device_timestamp
        self.present = present
        try:
            count = int(sample_count) if sample_count is not None else 1
        except (TypeError, ValueError):
            count = 1
        if present:
            self.rssi = rssi
            self.last_seen = timestamp
            self.sample_count = max(count, 1)
            self.sample_seq += 1
            self.pending.append((float(timestamp), rssi, True, self.sample_count))
        else:
            self.sample_count = 0
            self.pending.append((float(timestamp), None, False, 0))
        return True

    def is_fresh(self, now, timeout_sec):
        """Sensor sieht das Halsband aktuell (präsent und nicht zu alt)."""
        if self.last_seen is None or self.rssi is None or not self.present:
            return False
        return (now - self.last_seen) <= timeout_sec

    def is_online(self, now, timeout_sec):
        return self.last_heard is not None and (now - self.last_heard) <= timeout_sec

    def height_offset_cm(self, tag_height_cm) -> float:
        """Höhenunterschied Sensor ↔ Halsband (cm)."""
        return 0.0 if self.z_cm is None else self.z_cm - float(tag_height_cm)

    def horizontal_distance_cm(self, slant_cm, tag_height_cm, minimum_cm=10.0) -> float:
        """Schräge Funkdistanz → Abstand am Boden (Legacy-Pfad, 2D-Trilateration)."""
        dz = self.height_offset_cm(tag_height_cm)
        return float(math.sqrt(max(float(slant_cm) ** 2 - dz * dz, minimum_cm ** 2)))

    # ------------------------------------------------------------------
    # Legacy-Pfad (1D-Kalman → Distanz)
    # ------------------------------------------------------------------
    def process_filter(self, offset_db=0.0):
        """Filtert neue Werte; ``offset_db`` ist die Empfängerdrift (wird abgezogen)."""
        if self.sample_seq != self.processed_seq and self.rssi is not None:
            self.corrected_rssi = self.rssi - float(offset_db)
            self.filtered_rssi = self.filter.update(self.corrected_rssi, sample_time=self.last_seen)
            self.distance_cm = self._rssi_to_distance_cm(self.filtered_rssi)
            self.distance_std_cm = self.filter.distance_std(self.distance_cm, self.n_factor)
            self.processed_seq = self.sample_seq
            return True
        return False

    def _rssi_to_distance_cm(self, rssi):
        if self.n_factor <= 0:
            return 10000.0
        exponent = (self.tx_power - rssi) / (10.0 * self.n_factor)
        exponent = float(np.clip(exponent, -2.0, 2.5))  # 1 cm … ~316 m
        return float(10.0 ** exponent * 100.0)
