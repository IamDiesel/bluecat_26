"""Modelle für die Funkqualität des Sensor-Netzes (Mesh).

Die Sensoren kündigen sich gegenseitig als BLE-Beacons an. Ein Empfänger
publiziert den RSSI eines bekannten Sender-Sensors über MQTT. Diese Werte
dienen nicht als Positionsmessung, sondern

* zur Schätzung von **Hardware-Drift**: Weicht ein Link von seiner gelernten
  Baseline ab, wird die Abweichung in einen Sender- und einen Empfängeranteil
  zerlegt: ``R(t→r) − B(t→r) = a_t + b_r + ε``. Für die Halsband-Messungen
  zählt nur der Empfängeranteil ``b_r`` – das Halsband sendet, der Sensor
  empfängt.
* zur Bewertung der **Funkqualität** eines Sensors: Links, deren Abweichung
  sich nicht durch Drift erklären lässt (z. B. eine Person im Funkweg),
  senken die Qualität und damit das Gewicht des Sensors.

Die Baseline wird einmalig gelernt und bewusst nicht nachgeführt: eine
dauerhafte Empfängerdrift soll relativ zum Kalibrierzeitpunkt korrigiert
bleiben. Nach einer Neukalibrierung wird die Baseline mit
:meth:`RadioEnvironmentModel.relearn` neu gelernt.

Erwartetes MQTT-Payload für eine Sensor-zu-Sensor-Messung::

    {"message_type": "sensor_beacon", "sensor_id": "<empfänger>",
     "beacon_mac": "aa:bb:cc:dd:ee:ff", "rssi": -61}
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
    learned_at: float = 0.0


class RadioEnvironmentModel:
    def __init__(
        self,
        sensor_positions,
        baseline_file=None,
        baseline_learning_samples=30,
        recent_window_size=15,
        link_timeout_sec=30.0,
        offset_ridge=0.5,
        offset_limit_db=10.0,
        offset_refresh_sec=5.0,
        offset_limit_apply_db=8.0,
        suspect_offset_db=4.0,
        sensor_heights=None,
    ):
        self.sensor_positions = {
            str(name): np.asarray(position, dtype=float).reshape(2)
            for name, position in sensor_positions.items()
        }
        # Antennenhöhen (cm, relativ zum Fußboden der Wohnung) – für den echten Abstand zweier Sensoren
        self.sensor_heights = {str(n): float(h) for n, h in (sensor_heights or {}).items() if h is not None}
        self.baseline_file = baseline_file
        self.baseline_learning_samples = max(int(baseline_learning_samples), 3)
        self.recent_window_size = max(int(recent_window_size), 5)
        self.link_timeout_sec = max(float(link_timeout_sec), 1.0)
        self.offset_ridge = max(float(offset_ridge), 0.0)
        self.offset_limit_db = min(abs(float(offset_limit_db)), abs(float(offset_limit_apply_db)))
        self.suspect_offset_db = abs(float(suspect_offset_db))
        self.baseline_suspect = False
        self.offset_refresh_sec = max(float(offset_refresh_sec), 0.0)

        self._baselines = {}
        self._recent_values = defaultdict(lambda: deque(maxlen=self.recent_window_size))
        self._learning_values = defaultdict(list)
        self._last_seen = {}
        self._offset_cache_time = None
        self._offsets_rx = {}
        self._offsets_tx = {}
        self._residual_score = {}

        if self.baseline_file:
            try:
                self.load_baseline(self.baseline_file)
            except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
                self._baselines.clear()

    # ------------------------------------------------------------------
    # Verwaltung
    # ------------------------------------------------------------------
    def update_positions(self, sensor_positions, sensor_heights=None):
        """Übernimmt neue Sensorpositionen, ohne Gelerntes zu verwerfen.

        Baselines deaktivierter Sensoren bleiben erhalten (werden beim
        Reaktivieren wieder genutzt). Verworfen wird eine Baseline nur, wenn
        sich der Abstand der beiden Sensoren tatsächlich geändert hat.
        """
        self.sensor_positions = {
            str(n): np.asarray(p, dtype=float).reshape(2) for n, p in sensor_positions.items()
        }
        if sensor_heights is not None:
            self.sensor_heights = {str(n): float(h) for n, h in sensor_heights.items() if h is not None}
        for key, baseline in list(self._baselines.items()):
            if not self._baseline_valid(baseline) and self._both_known(baseline):
                self._baselines.pop(key, None)
                self._recent_values.pop(key, None)
        self._offset_cache_time = None

    def _both_known(self, baseline):
        return baseline.receiver in self.sensor_positions and baseline.transmitter in self.sensor_positions

    def _baseline_valid(self, baseline):
        """Baseline passt zu den aktuellen Positionen (beide Sensoren aktiv)."""
        if not self._both_known(baseline):
            return False
        distance = self._distance_between(baseline.receiver, baseline.transmitter)
        return abs(distance - baseline.distance_cm) <= 5.0

    def relearn(self):
        """Verwirft alle Baselines; sie werden aus neuen Messungen gelernt."""
        self._baselines.clear()
        self._learning_values.clear()
        self._recent_values.clear()
        self._offset_cache_time = None

    @staticmethod
    def _key(receiver, transmitter):
        return str(receiver), str(transmitter)

    def _distance_between(self, receiver, transmitter):
        """Abstand am Boden (cm). Bewusst ohne Höhen: Wer eine Höhe nur nachträgt, soll die gelernten
        Baselines nicht verlieren – nach echtem Umhängen „Mesh-Baseline neu lernen“."""
        return float(np.linalg.norm(self.sensor_positions[receiver] - self.sensor_positions[transmitter]))

    # ------------------------------------------------------------------
    # Messungen
    # ------------------------------------------------------------------
    def observe(self, receiver, transmitter, rssi, timestamp=None):
        receiver = str(receiver)
        transmitter = str(transmitter)
        if (
            receiver not in self.sensor_positions
            or transmitter not in self.sensor_positions
            or receiver == transmitter
        ):
            return False
        try:
            rssi = float(rssi)
        except (TypeError, ValueError):
            return False
        if not np.isfinite(rssi) or rssi <= -120.0:
            return False

        now = time.monotonic() if timestamp is None else float(timestamp)
        key = self._key(receiver, transmitter)
        self._last_seen[key] = now
        existing = self._baselines.get(key)
        if existing is not None and not self._baseline_valid(existing):
            self._baselines.pop(key, None)
            self._recent_values.pop(key, None)
        if key not in self._baselines:
            samples = self._learning_values[key]
            samples.append(rssi)
            if len(samples) >= self.baseline_learning_samples:
                self._create_baseline(key, samples, now)
            return True
        self._recent_values[key].append(rssi)
        return True

    def _create_baseline(self, key, samples, now):
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
            baseline_rssi=float(np.median(inliers)),
            variance=max(variance, 1.0),
            sample_count=len(inliers),
            learned_at=time.time(),
        )
        self._learning_values.pop(key, None)
        self._offset_cache_time = None

    def _fresh_deviations(self, now):
        """Liefert {(r, t): (Abweichung, Streuung)} aller frischen Links."""
        result = {}
        for key, baseline in self._baselines.items():
            if not self._baseline_valid(baseline):
                continue
            recent = self._recent_values.get(key)
            last_seen = self._last_seen.get(key)
            if not recent or len(recent) < 3 or last_seen is None:
                continue
            if now - last_seen > self.link_timeout_sec:
                continue
            current = float(np.median(np.asarray(recent, dtype=float)))
            result[key] = (current - baseline.baseline_rssi, math.sqrt(baseline.variance))
        return result

    # ------------------------------------------------------------------
    # Drift-Zerlegung
    # ------------------------------------------------------------------
    def _refresh_offsets(self, now):
        if (
            self._offset_cache_time is not None
            and abs(now - self._offset_cache_time) < self.offset_refresh_sec
        ):
            return
        self._offset_cache_time = now
        deviations = self._fresh_deviations(now)
        self._offsets_rx, self._offsets_tx, self._residual_score = {}, {}, {}
        self.baseline_suspect = False
        if not deviations:
            return
        names = sorted({n for key in deviations for n in key})
        index = {n: i for i, n in enumerate(names)}
        count = len(names)
        rows, values = [], []
        for (r, t), (dev, _) in deviations.items():
            row = np.zeros(2 * count)
            row[index[t]] = 1.0            # a_t (Sender)
            row[count + index[r]] = 1.0    # b_r (Empfänger)
            rows.append(row)
            values.append(dev)
        a_matrix = np.asarray(rows)
        y = np.asarray(values)
        # Robuste Ridge-Regression (IRLS mit Huber-Gewichten).
        weights = np.ones(len(y))
        solution = np.zeros(2 * count)
        ridge = math.sqrt(self.offset_ridge) * np.eye(2 * count)
        for _ in range(5):
            w_sqrt = np.sqrt(weights)[:, None]
            lhs = np.vstack([a_matrix * w_sqrt, ridge])
            rhs = np.concatenate([y * w_sqrt[:, 0], np.zeros(2 * count)])
            solution, *_ = np.linalg.lstsq(lhs, rhs, rcond=None)
            residual = y - a_matrix @ solution
            scale = 4.0
            weights = np.where(np.abs(residual) <= scale, 1.0, scale / np.maximum(np.abs(residual), 1e-9))
        residual = y - a_matrix @ solution

        rx_links = defaultdict(int)
        tx_links = defaultdict(int)
        per_sensor = defaultdict(list)
        for ((r, t), (dev, sigma)), res in zip(deviations.items(), residual):
            rx_links[r] += 1
            tx_links[t] += 1
            z = res / max(2.0 * sigma, 5.0)
            per_sensor[r].append(z)
            per_sensor[t].append(z)
        limit = self.offset_limit_db
        raw_rx = {}
        # Gleichtakt entfernen: Dass alle Empfänger zugleich driften, ist
        # physikalisch unplausibel und von einer Sender-/Umgebungsänderung
        # nicht unterscheidbar (a_t + b_r ist nur bis auf eine Konstante
        # bestimmt). Einzelne Driften bleiben erhalten.
        rx_values = [solution[count + index[n]] for n in names if rx_links[n] >= 2]
        common = float(np.median(rx_values)) if len(rx_values) >= 3 else 0.0
        solution[count:] -= common
        solution[:count] += common
        for n in names:
            b = float(solution[count + index[n]])
            a = float(solution[index[n]])
            raw_rx[n] = b
            # Ohne mindestens zwei Links ist die Zerlegung nicht bestimmt.
            self._offsets_rx[n] = float(np.clip(b, -limit, limit)) if rx_links[n] >= 2 else 0.0
            self._offsets_tx[n] = float(np.clip(a, -limit, limit)) if tx_links[n] >= 2 else 0.0
            if per_sensor[n]:
                self._residual_score[n] = float(
                    np.clip(np.median([math.exp(-0.5 * z * z) for z in per_sensor[n]]), 0.1, 1.0)
                )
        # Plausibilität: Driften viele Sensoren gleichzeitig stark, passt eher
        # die Baseline nicht (z. B. nach Firmware-Update oder Umbau) als die
        # Hardware. Dann keine Korrektur anwenden und Neulernen empfehlen.
        large = [n for n, b in raw_rx.items() if abs(b) > self.suspect_offset_db and rx_links[n] >= 2]
        considered = [n for n in raw_rx if rx_links[n] >= 2]
        self.baseline_suspect = len(considered) >= 3 and len(large) > 0.5 * len(considered)
        if self.baseline_suspect:
            self._offsets_rx = {n: 0.0 for n in self._offsets_rx}
            self._offsets_tx = {n: 0.0 for n in self._offsets_tx}
            self._residual_score = {}

    def get_receiver_offset(self, sensor_name, now=None):
        """Empfängerdrift ``b_r`` in dB (negativ = Sensor hört schlechter).

        Korrektur einer Halsband-Messung: ``rssi_korr = rssi_roh − b_r``.
        """
        now = time.monotonic() if now is None else float(now)
        self._refresh_offsets(now)
        return float(self._offsets_rx.get(str(sensor_name), 0.0))

    def get_transmitter_offset(self, sensor_name, now=None):
        now = time.monotonic() if now is None else float(now)
        self._refresh_offsets(now)
        return float(self._offsets_tx.get(str(sensor_name), 0.0))

    def get_hardware_offset(self, sensor_name, now=None):
        """Kompatibilitätsname: liefert die (richtige) Empfängerdrift."""
        return self.get_receiver_offset(sensor_name, now)

    def sensor_quality(self, sensor_name, timestamp=None):
        """Qualität 0.1…1.0 aus Link-Abweichungen, die Drift nicht erklärt."""
        now = time.monotonic() if timestamp is None else float(timestamp)
        self._refresh_offsets(now)
        return float(self._residual_score.get(str(sensor_name), 1.0))

    # ------------------------------------------------------------------
    # Diagnose & Persistenz
    # ------------------------------------------------------------------
    def diagnostics(self, timestamp=None):
        now = time.monotonic() if timestamp is None else float(timestamp)
        self._refresh_offsets(now)
        deviations = self._fresh_deviations(now)
        links = {}
        for key, baseline in self._baselines.items():
            receiver, transmitter = key
            dev = deviations.get(key)
            links[f"{receiver}->{transmitter}"] = {
                "baseline_rssi": round(baseline.baseline_rssi, 1),
                "deviation_db": round(dev[0], 1) if dev else None,
            }
        return {
            "baseline_suspect": self.baseline_suspect,
            "links_learned": len(self._baselines),
            "links_learning": len(self._learning_values),
            "receiver_offsets_db": {k: round(v, 2) for k, v in self._offsets_rx.items() if v},
            "transmitter_offsets_db": {k: round(v, 2) for k, v in self._offsets_tx.items() if v},
            "sensor_quality": {k: round(v, 3) for k, v in self._residual_score.items()},
            "links": links,
        }

    def baseline_links(self):
        """[(Empfänger, Sender, Normalwert dBm, Varianz)] aller gültigen Baselines."""
        return [(b.receiver, b.transmitter, float(b.baseline_rssi), float(b.variance))
                for b in self._baselines.values() if self._baseline_valid(b)]

    def link_residuals(self, timestamp=None):
        """[(Empfänger, Sender, Abweichung dB, Rest nach Drift dB)] aller frischen Links.

        Negativer Rest = Strecke ist gerade schwächer als gelernt, ohne dass
        Sender- oder Empfängerdrift das erklärt → Hindernis/Störung auf dem Weg.
        """
        now = time.monotonic() if timestamp is None else float(timestamp)
        self._refresh_offsets(now)
        out = []
        for (receiver, transmitter), (dev, _sigma) in self._fresh_deviations(now).items():
            if receiver not in self.sensor_positions or transmitter not in self.sensor_positions:
                continue
            residual = dev - self._offsets_rx.get(receiver, 0.0) - self._offsets_tx.get(transmitter, 0.0)
            out.append((receiver, transmitter, float(dev), float(residual)))
        return out

    def save_baseline(self, filepath=None):
        filepath = filepath or self.baseline_file
        if not filepath:
            return
        directory = os.path.dirname(os.path.abspath(filepath))
        os.makedirs(directory, exist_ok=True)
        payload = [
            {
                "receiver": b.receiver,
                "transmitter": b.transmitter,
                "distance_cm": b.distance_cm,
                "baseline_rssi": b.baseline_rssi,
                "variance": b.variance,
                "sample_count": b.sample_count,
                "learned_at": b.learned_at,
            }
            for b in self._baselines.values()
        ]
        temp_path = f"{filepath}.tmp"
        with open(temp_path, "w", encoding="utf-8") as file:
            json.dump(payload, file, indent=2)
        os.replace(temp_path, filepath)

    def load_baseline(self, filepath=None):
        filepath = filepath or self.baseline_file
        if not filepath or not os.path.exists(filepath):
            return
        with open(filepath, "r", encoding="utf-8") as file:
            entries = json.load(file)
        for entry in entries:
            receiver = str(entry["receiver"])
            transmitter = str(entry["transmitter"])
            if receiver == transmitter or "baseline_rssi" not in entry:
                continue
            # Auch Baselines gerade inaktiver Sensoren laden (und wieder speichern);
            # ob sie zu den aktuellen Positionen passen, prüft _baseline_valid.
            self._baselines[self._key(receiver, transmitter)] = LinkBaseline(
                receiver=receiver,
                transmitter=transmitter,
                distance_cm=float(entry.get("distance_cm", 0.0)),
                baseline_rssi=float(entry["baseline_rssi"]),
                variance=max(float(entry.get("variance", 1.0)), 1.0),
                sample_count=int(entry.get("sample_count", 0)),
                learned_at=float(entry.get("learned_at", 0.0)),
            )
        # Baselines, deren Sensoren inzwischen versetzt wurden, verwerfen
        self.update_positions(self.sensor_positions)
