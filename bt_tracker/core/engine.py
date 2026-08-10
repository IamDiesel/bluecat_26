import time
import numpy as np
from dataclasses import dataclass
from typing import Optional, List, Dict, Any

from particle_filter import ParticleFilter
from locator import LocatorEngine
from radio_environment import RadioEnvironmentModel
from radio_map import RadioGridMap

from core.sensor_node import SensorNode
from core.movement_model import IMMCKFilter

@dataclass
class TrackingResult:
    """Standardisiertes Rückgabe-Objekt der Tracking-Engine nach einem Tick."""
    state: str  # "aktiv" oder "inaktiv"
    x_cm: Optional[float]
    y_cm: Optional[float]
    accuracy_cm: Optional[float]
    active_sensors_count: int
    contributing_sensors: List[str]
    rejected_sensors: List[str]
    inactive_sensors: List[str]
    sensor_measurements: List[Dict[str, Any]]
    radio_diagnostics: dict
    estimate_rejected: bool
    rejection_reason: str

class TrackingEngine:
    """Orchestriert alle Filter, Modelle und Berechnungen."""

    def __init__(self, config_params: dict):
        # Konfigurationen
        self.params = config_params
        
        # Komponenten
        self.sensors: Dict[str, SensorNode] = {}
        self.locator: Optional[LocatorEngine] = None
        self.particle_filter: Optional[ParticleFilter] = None
        self.radio_env: Optional[RadioEnvironmentModel] = None
        self.grid_map: Optional[RadioGridMap] = None
        
        # NEU: Interacting Multiple Model Constrained Kalman Filter (Paper 3)
        self.kinematic = IMMCKFilter(q_stop=1.0, q_cv=500.0)
        
        # Status-Historie
        self.last_position = None
        self.last_position_time = None
        self.last_accuracy_cm = None

    def setup_sensors(self, sensor_configs: dict):
        """Baut die Sensor-Objekte und die abhängigen mathematischen Modelle (neu) auf."""
        self.sensors.clear()
        sensor_positions = {}
        all_positions = []

        # 1. SensorNodes initialisieren
        for sensor_id, data in sensor_configs.items():
            if not data.get("enabled", True) or not data.get("position_configured", True):
                continue
            
            node = SensorNode(sensor_id, data)
            self.sensors[sensor_id] = node
            sensor_positions[sensor_id] = node.pos
            all_positions.append(node.pos)

        # 2. Locator und Radio Environment
        self.locator = LocatorEngine(all_positions) if all_positions else None
        
        old_radio = self.radio_env
        self.radio_env = RadioEnvironmentModel(
            sensor_positions, 
            baseline_file=self.params.get("RADIO_BASELINE_FILE", "config/radio_mesh_baseline.json"),
            baseline_learning_samples=self.params.get("RADIO_BASELINE_LEARNING_SAMPLES", 30),
            link_timeout_sec=self.params.get("RADIO_LINK_TIMEOUT_SEC", 15.0)
        )
        
        # Lernfortschritt des alten Meshs retten
        if old_radio is not None:
            self.radio_env._baselines = old_radio._baselines
            self.radio_env._learning_values = old_radio._learning_values
            self.radio_env._recent_values = old_radio._recent_values
            self.radio_env._last_seen = old_radio._last_seen

        # 3. Partikelfilter und Heatmap
        if all_positions:
            self.particle_filter = ParticleFilter(
                all_positions, 
                particle_count=self.params.get("PARTICLE_COUNT", 1500),
                bounds_margin_cm=self.params.get("PARTICLE_BOUNDS_MARGIN_CM", 500.0),
                process_noise_cm=self.params.get("PARTICLE_PROCESS_NOISE_CM", 75.0),
                initial_spread_cm=self.params.get("PARTICLE_INITIAL_SPREAD_CM", 250.0),
                minimum_sigma_cm=self.params.get("PARTICLE_MINIMUM_SIGMA_CM", 75.0)
            )
            
        self.grid_map = RadioGridMap(sensor_positions, cell_size_cm=50.0)
        self.grid_map.update_static_mesh(self.radio_env, sensor_configs)

    def observe_mesh(self, receiver: str, transmitter: str, rssi: float, timestamp: float):
        """Leitet Mesh-Messungen für die statische Wand-Erkennung (SLAM) weiter."""
        if self.radio_env:
            self.radio_env.observe(receiver, transmitter, rssi, timestamp)

    def process_tick(self, now: float) -> TrackingResult:
        """Der Haupt-Berechnungszyklus. Wird bei neuen Daten oder im Hintergrund aufgerufen."""
        
        # 1. Daten sortieren und filtern
        latest_seen = max((s.last_seen for s in self.sensors.values() if s.last_seen is not None), default=None)
        
        active_anchors, distances, distance_stds, n_factors, sensor_qualities = [], [], [], [], []
        active_names, inactive_anchors, inactive_names, rejected_names = [], [], [], []
        measurements = []
        new_sample_count = 0

        for name, node in self.sensors.items():
            is_fresh = node.is_fresh(now, self.params.get("SENSOR_TIMEOUT_SEC", 30.0))
            
            if not is_fresh:
                # WICHTIG: Alten Status für die Timeout-Prüfung retten!
                old_last_seen = node.last_seen
                old_present = node.present
                
                node.reset_state()
                
                # Jetzt mit den geretteten Werten auf echten Timeout prüfen
                if old_last_seen is not None and (not old_present or now - old_last_seen > self.params.get("SENSOR_TIMEOUT_SEC", 30.0)):
                    inactive_anchors.append(node.pos)
                    inactive_names.append(name)
                continue

            # --- SCHRITT 1: ECHTZEIT-RSSI-KORREKTUR (Paper 2) ---
            # Nur bei wirklich NEUEN Daten den RSSI korrigieren!
            if node.sample_seq != node.processed_seq:
                hardware_offset = 0.0
                if self.radio_env:
                    hardware_offset = self.radio_env.get_hardware_offset(name, now)
                
                # Mathematische Korrektur: R_Nl(korrigiert) = R_Nl(roh) - \Delta A_l
                if node.rssi is not None:
                    node.rssi -= hardware_offset
            # ---------------------------------------------------

            if node.process_filter():
                new_sample_count += 1

            radio_quality = self.radio_env.sensor_quality(name, now) if self.radio_env else 1.0
            
            # Zu weit weg?
            if node.distance_cm > self.params.get("MAX_TRACK_DISTANCE_CM", 2000.0):
                rejected_names.append(name)
                measurements.append(self._create_measurement_log(node, radio_quality, used=False, reason="distance_limit"))
                continue
                
            # Zu starker Zeitversatz im Snapshot?
            skew_limit = self.params.get("MAX_SNAPSHOT_SKEW_SEC", 1.0)
            if latest_seen is not None and latest_seen - node.last_seen <= skew_limit and node.distance_cm is not None:
                active_anchors.append(node.pos)
                distances.append(node.distance_cm)
                distance_stds.append(node.distance_std_cm)
                n_factors.append(node.n_factor)
                sensor_qualities.append(radio_quality)
                active_names.append(name)
                measurements.append(self._create_measurement_log(node, radio_quality, used=True))

        radio_diag = self.radio_env.diagnostics() if self.radio_env else {}

        # 2. Abbruchbedingungen prüfen
        if not active_anchors:
            return self._build_result("inaktiv", None, 0, [], rejected_names, inactive_names, measurements, radio_diag)

        if new_sample_count == 0 and self.last_position is not None:
            # Keine echten neuen Daten -> alten Zustand beibehalten, nur Timouts melden
            return self._build_result("aktiv", self.last_position, len(active_anchors), active_names, rejected_names, inactive_names, measurements, radio_diag)

        # Positions-Gedächtnis prüfen
        previous_pos = self.last_position
        if previous_pos is not None and self.last_position_time is not None and now - self.last_position_time > self.params.get("POSITION_MEMORY_SEC", 10.0):
            previous_pos = None
            if self.particle_filter:
                self.particle_filter.reset()
            self.kinematic.reset()

        if not self.locator:
            return self._build_result("aktiv", previous_pos, len(active_anchors), active_names, rejected_names, inactive_names, measurements, radio_diag, True, "Keine Sensorpositionen")

        # 3. Geometrie (Locator -> Partikelfilter)
        seed_pos = self.locator.calculate_position(active_anchors, distances, inactive_anchors, distance_stds, previous_pos)
        candidate_pos = None
        
        if self.particle_filter:
            candidate_pos = self.particle_filter.update(
                active_anchors, distances, distance_stds, n_factors=n_factors,
                grid_map=self.grid_map, sensor_qualities=sensor_qualities,
                timestamp=now, prior_position=seed_pos
            )
            self.last_accuracy_cm = self.particle_filter.last_accuracy_cm

        if candidate_pos is None:
            candidate_pos = seed_pos
            self.last_accuracy_cm = self.locator.last_accuracy_cm

        if candidate_pos is None:
            if self.particle_filter:
                self.particle_filter.reset(previous_pos)
            return self._build_result("aktiv", previous_pos, len(active_anchors), active_names, rejected_names, inactive_names, measurements, radio_diag, True, "Locator-Fehler")

        # 4. Validierung und Kinematik (IMM-CKF anstatt Gummiband)
        candidate_pos = np.asarray(candidate_pos, dtype=float)
        if np.any(~np.isfinite(candidate_pos)) or np.linalg.norm(candidate_pos - self.locator.centroid) > self.params.get("MAX_POSITION_RADIUS_CM", 2000.0):
            return self._build_result("aktiv", previous_pos, len(active_anchors), active_names, rejected_names, inactive_names, measurements, radio_diag, True, "Out of bounds")

        accuracy_cm = self.last_accuracy_cm or 9999.0
        final_pos, is_rejected, reject_reason = self.kinematic.apply_movement(candidate_pos, accuracy_cm, now)

        # 5. Dynamische Heatmap (SLAM) updaten
        is_hard_rejection = is_rejected and not reject_reason.startswith("IMM")
        if not is_hard_rejection and self.grid_map:
            # Für SLAM benötigen wir temporär die rohen Configs der Sensoren.
            sensor_configs_temp = {name: {"tx_power": node.tx_power, "n_factor": node.n_factor, "filtered_rssi": node.filtered_rssi} for name, node in self.sensors.items()}
            self.grid_map.update_dynamic(active_anchors, active_names, final_pos, sensor_configs_temp)

        # Status abspeichern
        self.last_position = final_pos
        self.last_position_time = now

        return self._build_result("aktiv", final_pos, len(active_anchors), active_names, rejected_names, inactive_names, measurements, radio_diag, is_rejected, reject_reason)

    def _create_measurement_log(self, node: SensorNode, quality: float, used: bool, reason: str = "") -> dict:
        return {
            "sensor": node.name,
            "raw_rssi": round(float(node.rssi), 2) if node.rssi else None,
            "filtered_rssi": round(float(node.filtered_rssi), 2) if node.filtered_rssi else None,
            "distance_cm": round(float(node.distance_cm), 1) if node.distance_cm else None,
            "distance_std_cm": round(float(node.distance_std_cm), 1) if node.distance_std_cm else None,
            "radio_quality": round(float(quality), 3),
            "device_timestamp": node.last_device_timestamp,
            "used": used,
            "reason": reason
        }

    def _build_result(self, state, pos, active_count, contrib, rejected, inactive, measurements, radio_diag, is_rejected=False, reason=""):
        return TrackingResult(
            state=state,
            x_cm=pos[0] if pos is not None else None,
            y_cm=pos[1] if pos is not None else None,
            accuracy_cm=self.last_accuracy_cm,
            active_sensors_count=active_count,
            contributing_sensors=contrib,
            rejected_sensors=rejected,
            inactive_sensors=inactive,
            sensor_measurements=measurements,
            radio_diagnostics=radio_diag,
            estimate_rejected=is_rejected,
            rejection_reason=reason
        )