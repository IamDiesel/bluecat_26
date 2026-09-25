"""TriLola-Modell „legacy“: Distanzen → Locator → Partikelfilter → IMM.

Das bisherige Modell, mit den Korrekturen aus dem Review:
* Empfängerdrift statt Senderdrift (Mesh-Offset)
* Median-of-3 statt 7er-Mittel als Vorfilter
* Partikelfilter-Update nur mit neuen Messungen (keine Mehrfachverwendung)
* Snapshot-Skew 3 s, verworfene Sensoren werden protokolliert
* inaktive Sensoren bleiben inaktiv (nicht nur einen Tick)
* inkrementeller Aufbau: unveränderte Sensoren behalten ihren Zustand
* Funk-Grid (SLAM) standardmäßig aus (``RADIO_GRID_ENABLED``)
"""

import os
from typing import Dict, List, Optional

import numpy as np

from core.movement_model import IMMCKFilter
from core.result import TrackingResult
from core.sensor_node import SensorNode, config_signature
from locator import LocatorEngine
from particle_filter import ParticleFilter
from radio_environment import RadioEnvironmentModel
from radio_map import RadioGridMap


class TrackingEngine:
    engine_name = "legacy"

    def __init__(self, config_params: Optional[dict] = None, floorplan=None):
        self.params = dict(config_params or {})
        self.floorplan = floorplan
        self.sensors: Dict[str, SensorNode] = {}
        self.locator: Optional[LocatorEngine] = None
        self.particle_filter: Optional[ParticleFilter] = None
        self.radio_env: Optional[RadioEnvironmentModel] = None
        self.grid_map: Optional[RadioGridMap] = None
        self.kinematic = IMMCKFilter()
        self._anchor_signature = None
        self.last_position = None
        self.last_position_time = None
        self.last_accuracy_cm = None
        self.last_result: Optional[TrackingResult] = None

    def _param(self, key, default):
        value = self.params.get(key)
        return default if value is None else value

    def set_floorplan(self, floorplan):
        self.floorplan = floorplan

    # ------------------------------------------------------------------
    def setup_sensors(self, sensor_configs: dict):
        wanted = {
            sid: data for sid, data in sensor_configs.items()
            if data.get("enabled", True) and data.get("position_configured", True)
        }
        for sid in list(self.sensors):
            if sid not in wanted:
                del self.sensors[sid]
        for sid, data in wanted.items():
            node = self.sensors.get(sid)
            if node is None or node.signature != config_signature(data):
                new_node = SensorNode(sid, data)
                if node is not None:
                    new_node.last_heard = node.last_heard
                    new_node.last_seen = node.last_seen
                self.sensors[sid] = new_node
            else:
                node.name = data.get("name", sid)
                node.ble_addresses = list(data.get("ble_addresses", []))
                node.topic = data.get("topic", node.topic)

        positions = {sid: node.pos for sid, node in self.sensors.items()}
        heights = {sid: node.antenna_z_cm for sid, node in self.sensors.items() if node.antenna_z_cm is not None}
        all_positions = list(positions.values())
        if self.radio_env is None:
            self.radio_env = RadioEnvironmentModel(
                positions,
                baseline_file=self.params.get("RADIO_BASELINE_FILE"),
                baseline_learning_samples=self._param("RADIO_BASELINE_LEARNING_SAMPLES", 30),
                link_timeout_sec=self._param("RADIO_LINK_TIMEOUT_SEC", 30.0),
                sensor_heights=heights,
            )
        else:
            self.radio_env.update_positions(positions, heights)

        signature = tuple(sorted((sid, tuple(np.round(p, 1))) for sid, p in positions.items()))
        if signature != self._anchor_signature:
            # Nur bei geänderter Sensorgeometrie neu aufbauen
            self._anchor_signature = signature
            self.locator = LocatorEngine(all_positions) if all_positions else None
            self.particle_filter = ParticleFilter(
                all_positions,
                particle_count=self._param("PARTICLE_COUNT", 1500),
                bounds_margin_cm=self._param("PARTICLE_BOUNDS_MARGIN_CM", 500.0),
                process_noise_cm=self._param("PARTICLE_PROCESS_NOISE_CM", 75.0),
                initial_spread_cm=self._param("PARTICLE_INITIAL_SPREAD_CM", 250.0),
                minimum_sigma_cm=self._param("PARTICLE_MINIMUM_SIGMA_CM", 75.0),
                random_seed=self.params.get("RANDOM_SEED"),
            ) if all_positions else None
            self.kinematic.reset()
            self.grid_map = None
            if self._param("RADIO_GRID_ENABLED", False) and positions:
                self.grid_map = RadioGridMap(positions, cell_size_cm=50.0)
                heatmap = self.params.get("RADIO_HEATMAP_FILE")
                if heatmap and os.path.exists(heatmap):
                    self.grid_map.load_heatmap(heatmap)
        if self.grid_map is not None:
            self.grid_map.update_static_mesh(self.radio_env, sensor_configs)

    def observe_mesh(self, receiver: str, transmitter: str, rssi: float, timestamp: float):
        if self.radio_env:
            self.radio_env.observe(receiver, transmitter, rssi, timestamp)

    # ------------------------------------------------------------------
    def process_tick(self, now: float) -> TrackingResult:
        timeout = self._param("SENSOR_TIMEOUT_SEC", 30.0)
        skew_limit = self._param("MAX_SNAPSHOT_SKEW_SEC", 3.0)
        offline_timeout = self._param("OFFLINE_TIMEOUT_SEC", 90.0)
        fresh_nodes = [n for n in self.sensors.values() if n.is_fresh(now, timeout)]
        latest_seen = max((n.last_seen for n in fresh_nodes), default=None)

        active_anchors, distances, stds, n_factors, qualities, names = [], [], [], [], [], []
        new_mask: List[bool] = []
        inactive_anchors, inactive_names, rejected_names, measurements = [], [], [], []
        new_sample_count = 0

        for name, node in self.sensors.items():
            node.pending.clear()  # Warteschlange nur für das PF-Modell
            if not node.is_fresh(now, timeout):
                if node.filtered_rssi is not None:
                    node.reset_filter()
                if node.last_seen is not None and node.is_online(now, offline_timeout):
                    inactive_anchors.append(node.pos)
                    inactive_names.append(name)
                continue

            offset = self.radio_env.get_receiver_offset(name, now) if self.radio_env else 0.0
            is_new = node.process_filter(offset)
            if is_new:
                new_sample_count += 1
            quality = self.radio_env.sensor_quality(name, now) if self.radio_env else 1.0

            if node.distance_cm is None:
                continue
            if node.distance_cm > self._param("MAX_TRACK_DISTANCE_CM", 2000.0):
                rejected_names.append(name)
                measurements.append(self._log(node, quality, False, "distance_limit", offset))
                continue
            if latest_seen is not None and latest_seen - node.last_seen > skew_limit:
                rejected_names.append(name)
                measurements.append(self._log(node, quality, False, "skew", offset))
                continue
            active_anchors.append(node.pos)
            # Funkdistanz ist schräg (Sensor höher als das Halsband) → Abstand am Boden
            flat = node.horizontal_distance_cm(node.distance_cm, self._param("TAG_HEIGHT_CM", 25.0))
            distances.append(flat)
            # Unsicherheit mitprojizieren: dh/dd = d/h (nahe am Sensor wird der Bodenabstand unsicherer)
            stds.append(node.distance_std_cm * min(node.distance_cm / max(flat, 1.0), 5.0))
            n_factors.append(node.n_factor)
            qualities.append(quality)
            names.append(name)
            new_mask.append(is_new)
            measurements.append(self._log(node, quality, True, "", offset))

        radio_diag = self.radio_env.diagnostics(now) if self.radio_env else {}

        if not active_anchors:
            return self._result("inaktiv", None, 0, [], rejected_names, inactive_names, measurements, radio_diag)

        if new_sample_count == 0 and self.last_position is not None:
            res = self._result("aktiv", self.last_position, len(active_anchors), names, rejected_names,
                               inactive_names, measurements, radio_diag)
            res.updated = False
            return res

        previous_pos = self.last_position
        if (
            previous_pos is not None and self.last_position_time is not None
            and now - self.last_position_time > self._param("POSITION_MEMORY_SEC", 10.0)
        ):
            previous_pos = None
            if self.particle_filter:
                self.particle_filter.reset()
            self.kinematic.reset()

        if not self.locator:
            return self._result("aktiv", previous_pos, len(active_anchors), names, rejected_names,
                                inactive_names, measurements, radio_diag, True, "Keine Sensorpositionen")

        seed_pos = self.locator.calculate_position(active_anchors, distances, inactive_anchors, stds, previous_pos)
        candidate_pos = None
        if self.particle_filter:
            mask = np.asarray(new_mask, dtype=bool)
            candidate_pos = self.particle_filter.update(
                np.asarray(active_anchors)[mask], np.asarray(distances)[mask], np.asarray(stds)[mask],
                n_factors=np.asarray(n_factors)[mask], grid_map=self.grid_map,
                sensor_qualities=np.asarray(qualities)[mask], timestamp=now, prior_position=seed_pos,
            ) if mask.any() else self.particle_filter.estimate
            self.last_accuracy_cm = self.particle_filter.last_accuracy_cm
        if candidate_pos is None:
            candidate_pos = seed_pos
            self.last_accuracy_cm = self.locator.last_accuracy_cm
        if candidate_pos is None:
            if self.particle_filter:
                self.particle_filter.reset(previous_pos)
            return self._result("aktiv", previous_pos, len(active_anchors), names, rejected_names,
                                inactive_names, measurements, radio_diag, True, "Locator-Fehler")

        candidate_pos = np.asarray(candidate_pos, dtype=float)
        if np.any(~np.isfinite(candidate_pos)) or np.linalg.norm(candidate_pos - self.locator.centroid) > self._param("MAX_POSITION_RADIUS_CM", 2000.0):
            return self._result("aktiv", previous_pos, len(active_anchors), names, rejected_names,
                                inactive_names, measurements, radio_diag, True, "Out of bounds")

        accuracy_cm = self.last_accuracy_cm or 9999.0
        final_pos, is_rejected, reason = self.kinematic.apply_movement(candidate_pos, accuracy_cm, now)
        # Veröffentlichte Genauigkeit bleibt die (konservative) PF-Angabe: Die
        # IMM-Kovarianz schrumpft in Ruhe durch korrelierte Messfehler zu stark.

        if self.grid_map is not None and self._param("RADIO_GRID_DYNAMIC", False):
            temp = {n: {"tx_power": s.tx_power, "n_factor": s.n_factor, "filtered_rssi": s.filtered_rssi}
                    for n, s in self.sensors.items()}
            self.grid_map.update_dynamic(active_anchors, names, final_pos, temp)

        self.last_position = final_pos
        self.last_position_time = now
        return self._result("aktiv", final_pos, len(active_anchors), names, rejected_names, inactive_names,
                            measurements, radio_diag, is_rejected, reason)

    # ------------------------------------------------------------------
    def _log(self, node: SensorNode, quality, used, reason, offset):
        def rnd(v, k=1):
            return None if v is None else round(float(v), k)
        return {
            "sensor": node.name,
            "raw_rssi": rnd(node.rssi, 2),
            "filtered_rssi": rnd(node.filtered_rssi, 2),
            "receiver_offset_db": round(float(offset), 2),
            "distance_cm": rnd(node.distance_cm),
            "distance_std_cm": rnd(node.distance_std_cm),
            "radio_quality": round(float(quality), 3),
            "sample_count": node.sample_count,
            "device_timestamp": node.last_device_timestamp,
            "used": used,
            "reason": reason,
        }

    def _result(self, state, pos, active_count, contrib, rejected, inactive, measurements, radio_diag,
                is_rejected=False, reason=""):
        room = None
        if pos is not None and self.floorplan is not None and getattr(self.floorplan, "has_rooms", False):
            room = self.floorplan.room_name(pos)
        result = TrackingResult(
            state=state,
            x_cm=float(pos[0]) if pos is not None else None,
            y_cm=float(pos[1]) if pos is not None else None,
            accuracy_cm=self.last_accuracy_cm if pos is not None else None,
            active_sensors_count=active_count,
            contributing_sensors=list(contrib),
            rejected_sensors=list(rejected),
            inactive_sensors=list(inactive),
            sensor_measurements=measurements,
            radio_diagnostics=radio_diag,
            estimate_rejected=is_rejected,
            rejection_reason=reason,
            room=room,
            moving_probability=round(self.kinematic.moving_probability, 3) if pos is not None else None,
            engine=self.engine_name,
            updated=True,
        )
        self.last_result = result
        return result

    def save_state(self):
        if self.radio_env:
            self.radio_env.save_baseline()
        heatmap = self.params.get("RADIO_HEATMAP_FILE")
        if self.grid_map is not None and heatmap:
            self.grid_map.export_heatmap(heatmap)
