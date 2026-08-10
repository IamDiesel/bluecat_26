import numpy as np
from filters import TrackingFilter


class SensorNode:
    """Kapselt die Konfiguration, den Zustand und den Kalman-Filter eines Sensors."""

    def __init__(self, sensor_id, data_dict):
        self.sensor_id = sensor_id
        self.name = data_dict.get("name", sensor_id)
        self.topic = data_dict.get("topic", "")
        self.enabled = data_dict.get("enabled", True)
        self.pos = np.asarray(data_dict.get("pos", [0.0, 0.0]), dtype=float)
        self.ble_addresses = data_dict.get("ble_addresses", [])

        # Kalibrierung
        self.tx_power = float(data_dict.get("tx_power", -59.0))
        self.n_factor = float(data_dict.get("n_factor", 3.0))
        self.r_min = float(data_dict.get("r_min", 0.1))
        self.r_max = float(data_dict.get("r_max", 10.0))
        self.rssi_limit = float(data_dict.get("rssi_limit", -100.0))
        self.q_variance = float(data_dict.get("q_variance", 0.01))

        self.filter = TrackingFilter(
            tx_power=self.tx_power, r_min=self.r_min, r_max=self.r_max,
            rssi_limit=self.rssi_limit, q_variance=self.q_variance
        )
        self.reset_state()

    def reset_state(self):
        """Setzt Laufzeitdaten und Filter zurück."""
        self.last_seen = None
        self.last_device_timestamp = None
        self.rssi = None
        self.present = False
        self.last_sequence = None
        self.sample_seq = 0
        self.processed_seq = -1
        self.filtered_rssi = None
        self.distance_cm = None
        self.distance_std_cm = None
        self.filter.reset()

    def apply_reading(self, rssi, timestamp, present, sequence=None, device_timestamp=None):
        """Nimmt neue Rohdaten auf und filtert QoS-1 Duplikate."""
        if not np.isfinite(rssi):
            return False

        if sequence is not None and self.last_sequence is not None and sequence == self.last_sequence:
            # Duplikat: Nur die Online-Zeit aktualisieren
            self.last_seen = timestamp
            return False

        self.rssi = float(rssi)
        self.last_seen = timestamp
        self.present = present
        self.last_sequence = sequence
        self.last_device_timestamp = device_timestamp
        self.sample_seq += 1
        return True

    def process_filter(self):
        """Lässt den Kalman-Filter über neue Werte laufen und berechnet die Distanz."""
        if self.sample_seq != self.processed_seq:
            self.filtered_rssi = self.filter.update(self.rssi, sample_time=self.last_seen)
            self.distance_cm = self._rssi_to_distance_cm(self.filtered_rssi)
            self.distance_std_cm = self.filter.distance_std(self.distance_cm, self.n_factor)
            self.processed_seq = self.sample_seq
            return True
        return False

    def is_fresh(self, now, timeout_sec):
        """Prüft, ob der Sensor zeitnah geantwortet hat und präsent ist."""
        if self.last_seen is None or self.rssi is None:
            return False
        return self.present and self.rssi > -120.0 and (now - self.last_seen) <= timeout_sec

    def _rssi_to_distance_cm(self, rssi):
        if self.n_factor <= 0:
            return 10000.0
        exponent = (self.tx_power - rssi) / (10.0 * self.n_factor)
        exponent = float(np.clip(exponent, -6.0, 6.0))
        return float(10.0 ** exponent * 100.0)