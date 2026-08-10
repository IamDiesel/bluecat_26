"""Modelle für die Funkqualität des Sensor-Netzes.

Die Sensoren können sich gegenseitig als BLE-Beacons ankündigen. Ein
Empfänger publiziert den RSSI eines bekannten Sender-Sensors über MQTT. Diese
Werte werden nicht als zusätzliche Objektmessungen verwendet, sondern zur
Bewertung der aktuellen Funkqualität jedes Sensors.

Erwartetes MQTT-Payload für eine Sensor-zu-Sensor-Messung:

    {
        "message_type": "sensor_beacon",
        "beacon_mac": "aa:bb:cc:dd:ee:ff",
        "rssi": -61
    }

Das MQTT-Topic beziehungsweise ``sensor_id`` identifiziert den empfangenden
Sensor. Der Tracker löst ``beacon_mac`` vor dem Aufruf dieses Modells in die
interne Sender-ID auf.
"""

from __future__ import annotations

import json
import math
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass

import numpy as np


@dataclass
class LinkBaseline:
    """Stabile Referenzwerte einer gerichteten Sensorverbindung."""

    receiver: str
    transmitter: str
    distance_cm: float
    baseline_rssi: float
    variance: float
    sample_count: int


class RadioEnvironmentModel:
    """Bewertet Sensoren anhand ihrer Sensor-zu-Sensor-Funkverbindungen.

    Die Baseline sollte idealerweise in einer bekannten, stabilen Umgebung
    gelernt oder aus einer JSON-Datei geladen werden. Ohne Baseline bleiben
    alle Sensorqualitäten bei 1.0; das Modell ist damit abwärtskompatibel.
    """

    def __init__(
        self,
        sensor_positions,
        baseline_file=None,
        baseline_learning_samples=30,
        recent_window_size=30,
        link_timeout_sec=15.0,
    ):
        self.sensor_positions = {
            name: np.asarray(position, dtype=float).reshape(2)
            for name, position in sensor_positions.items()
        }
        self.baseline_file = baseline_file
        self.baseline_learning_samples = max(int(baseline_learning_samples), 3)
        self.recent_window_size = max(int(recent_window_size), 5)
        self.link_timeout_sec = max(float(link_timeout_sec), 1.0)

        self._baselines = {}
        self._recent_values = defaultdict(
            lambda: deque(maxlen=self.recent_window_size)
        )
        self._learning_values = defaultdict(list)
        self._last_seen = {}

        if self.baseline_file:
            try:
                self.load_baseline(self.baseline_file)
            except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
                self._baselines.clear()

    @staticmethod
    def _key(receiver, transmitter):
        return str(receiver), str(transmitter)

    def _distance_between(self, receiver, transmitter):
        receiver_position = self.sensor_positions[receiver]
        transmitter_position = self.sensor_positions[transmitter]
        return float(np.linalg.norm(receiver_position - transmitter_position))

    def observe(self, receiver, transmitter, rssi, timestamp=None):
        """Verarbeitet eine RSSI-Messung vom ``transmitter`` zum ``receiver``."""
        receiver = str(receiver)
        transmitter = str(transmitter)
        if (
            receiver not in self.sensor_positions
            or transmitter not in self.sensor_positions
            or receiver == transmitter
        ):
            return False

        rssi = float(rssi)
        if not np.isfinite(rssi):
            return False

        now = time.monotonic() if timestamp is None else float(timestamp)
        key = self._key(receiver, transmitter)
        self._last_seen[key] = now

        if key not in self._baselines:
            samples = self._learning_values[key]
            samples.append(rssi)
            if len(samples) >= self.baseline_learning_samples:
                self._create_baseline(key, samples)
            return True

        self._recent_values[key].append(rssi)
        return True

    def _create_baseline(self, key, samples):
        values = np.asarray(samples, dtype=float)
        median = float(np.median(values))
        mad = float(np.median(np.abs(values - median)))
        threshold = max(3.0 * 1.4826 * mad, 2.0)
        inliers = values[np.abs(values - median) <= threshold]
        if len(inliers) < 3:
            inliers = values

        receiver, transmitter = key
        variance = float(np.var(inliers, ddof=1 if len(inliers) > 1 else 0))
        self._baselines[key] = LinkBaseline(
            receiver=receiver,
            transmitter=transmitter,
            distance_cm=self._distance_between(receiver, transmitter),
            baseline_rssi=float(np.mean(inliers)),
            variance=max(variance, 1.0),
            sample_count=len(inliers),
        )
        self._recent_values[key].extend(inliers[-self.recent_window_size :])
        self._learning_values.pop(key, None)

    def _link_quality(self, key, now):
        baseline = self._baselines.get(key)
        recent = self._recent_values.get(key)
        last_seen = self._last_seen.get(key)
        if (
            baseline is None
            or not recent
            or last_seen is None
            or now - last_seen > self.link_timeout_sec
        ):
            return None

        current_median = float(np.median(np.asarray(recent, dtype=float)))
        deviation = current_median - baseline.baseline_rssi

        scale = max(5.0, 2.0 * math.sqrt(baseline.variance))
        score = math.exp(-0.5 * (deviation / scale) ** 2)
        return float(np.clip(score, 0.1, 1.0))

    def sensor_quality(self, sensor_name, timestamp=None):
        """Liefert eine Qualität zwischen 0.1 und 1.0 für einen Sensor."""
        sensor_name = str(sensor_name)
        now = time.monotonic() if timestamp is None else float(timestamp)
        scores = []

        for key in self._baselines:
            receiver, transmitter = key
            if sensor_name in (receiver, transmitter):
                score = self._link_quality(key, now)
                if score is not None:
                    scores.append(score)

        if not scores:
            return 1.0
        return float(np.clip(np.median(scores), 0.1, 1.0))

    def get_hardware_offset(self, transmitter_name, now):
        """Berechnet den aktuellen Sendeleistungs-Abfall (Delta A) des Sensors.
        
        Basierend auf Paper 2: \Delta A_l = R_{Ml} - \overline{R_{Ml}}
        """
        transmitter_name = str(transmitter_name)
        offsets = []
        
        for key, baseline in self._baselines.items():
            receiver, transmitter = key
            # Wir betrachten hier nur Links, bei denen der gefragte Sensor der Sender ist
            if transmitter == transmitter_name:
                recent = self._recent_values.get(key)
                last_seen = self._last_seen.get(key)
                
                # Nur frische Mesh-Daten verwenden
                if recent and last_seen is not None and (now - last_seen) <= self.link_timeout_sec:
                    current_median = float(np.median(np.asarray(recent, dtype=float)))
                    
                    # Die Differenz zwischen Ist-Wert und Baseline
                    offset = current_median - baseline.baseline_rssi
                    offsets.append(offset)
                    
        if not offsets:
            return 0.0
            
        # Den robusten Median aller Mesh-Beobachtungen dieses Senders zurückgeben
        return float(np.median(offsets))

    def diagnostics(self, timestamp=None):
        """Gibt diagnostische Linkdaten für MQTT/Logging zurück."""
        now = time.monotonic() if timestamp is None else float(timestamp)
        result = {}
        for key, baseline in self._baselines.items():
            receiver, transmitter = key
            result[f"{receiver}->{transmitter}"] = {
                "receiver": receiver,
                "transmitter": transmitter,
                "distance_cm": round(baseline.distance_cm, 1),
                "baseline_rssi": round(baseline.baseline_rssi, 2),
                "quality": round(self._link_quality(key, now) or 1.0, 3),
                "samples": baseline.sample_count,
            }
        return result

    def save_baseline(self, filepath=None):
        """Speichert gelernte Baselines atomar als JSON."""
        filepath = filepath or self.baseline_file
        if not filepath:
            return

        directory = os.path.dirname(os.path.abspath(filepath))
        os.makedirs(directory, exist_ok=True)
        payload = [
            {
                "receiver": baseline.receiver,
                "transmitter": baseline.transmitter,
                "distance_cm": baseline.distance_cm,
                "baseline_rssi": baseline.baseline_rssi,
                "variance": baseline.variance,
                "sample_count": baseline.sample_count,
            }
            for baseline in self._baselines.values()
        ]
        temp_path = f"{filepath}.tmp"
        with open(temp_path, "w", encoding="utf-8") as file:
            json.dump(payload, file, indent=2)
        os.replace(temp_path, filepath)

    def load_baseline(self, filepath=None):
        """Lädt Baselines; eine fehlende Datei ist kein Fehler."""
        filepath = filepath or self.baseline_file
        if not filepath or not os.path.exists(filepath):
            return

        with open(filepath, "r", encoding="utf-8") as file:
            entries = json.load(file)

        for entry in entries:
            receiver = str(entry["receiver"])
            transmitter = str(entry["transmitter"])
            key = self._key(receiver, transmitter)
            if (
                receiver not in self.sensor_positions
                or transmitter not in self.sensor_positions
                or receiver == transmitter
            ):
                continue
            self._baselines[key] = LinkBaseline(
                receiver=receiver,
                transmitter=transmitter,
                distance_cm=float(entry.get("distance_cm", 0.0)),
                baseline_rssi=float(entry["baseline_rssi"]),
                variance=max(float(entry.get("variance", 1.0)), 1.0),
                sample_count=int(entry.get("sample_count", 0)),
            )