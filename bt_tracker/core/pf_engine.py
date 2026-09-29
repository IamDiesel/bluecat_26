"""TriLola-Modell „pf“: RSSI-Partikelfilter mit sequenziellen Updates.

Grundidee
---------
Ein einziger Bayes-Filter verarbeitet jede Sensor-Nachricht genau einmal, in
Zeitreihenfolge und direkt im Pegelraum (dB):

    RSSI_i = P0_i − 10·n_i·log10(d_i) − Wände_i(x) + b_i + ε
    ε ~ Student-t(ν, σ_i),  σ_i² = σ_shadow,i² + 1.57·σ_fading,i²/k

* ``P0_i``  = ``tx_power`` (Halsband-RSSI in 1 m an diesem Sensor)
* ``b_i``   = Empfängerdrift aus dem Mesh (``RadioEnvironmentModel``)
* ``k``     = Anzahl Rohwerte hinter dem gemeldeten Median
* Wände     = aus ``config/floorplan.json`` (optional)

Übernommene Stärken des bisherigen Modells
-----------------------------------------
* robuste Likelihood (Soft-L1 → Student-t, Ausreißer schaden wenig)
* Ruhe- vs. Bewegungsmodell (IMM → Modus je Partikel, „Jump-Markov“-PF)
* Rettungspartikel um den meldenden Sensor (AMCL → adaptiv statt konstant)
* schwache negative Information (jetzt als Messmodell „nicht gesehen“)
* Funkqualität/Drift aus dem Mesh (jetzt richtig: Empfängerseite)
* Diagnose-Attribute für Home Assistant (gleiches Format)

Neu
---
* keine Mehrfachverwendung von Messungen, kein Snapshot-Skew
* ehrliche Unsicherheit aus der Partikelwolke, Raumwahrscheinlichkeiten
* optionaler Grundriss: Wanddämpfung und Bewegungssperre durch Wände
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional

import numpy as np

from core.floorplan import FloorPlan
from core.result import TrackingResult
from core.sensor_node import SensorNode, config_signature
from radio_environment import RadioEnvironmentModel

try:  # scipy ist ohnehin Abhängigkeit des Trackers; Fallback für Minimal-Setups
    from scipy.special import ndtr as _norm_cdf
except ImportError:  # pragma: no cover
    def _norm_cdf(x):
        return 0.5 * (1.0 + np.vectorize(math.erf)(np.asarray(x) / math.sqrt(2.0)))


DEFAULTS = {
    "PF_PARTICLES": 1500,
    "PF_STUDENT_NU": 4.0,
    "PF_MAX_SAMPLE_COUNT": 4,           # Obergrenze für k (Median aus k Rohwerten)
    "PF_REST_DIFFUSION_CM": 4.0,        # cm/√s in Ruhe
    "PF_MOVE_DIFFUSION_CM": 15.0,       # cm/√s zusätzlich in Bewegung
    "PF_MOVE_SPEED_CM_S": 90.0,         # stationäre Geschwindigkeitsstreuung (OU)
    "PF_MOVE_TAU_SEC": 2.0,             # Korrelationszeit der Geschwindigkeit
    "PF_MEAN_REST_SEC": 10.0,           # Modellzeitkonstante Ruhe→Bewegung (bewusst kurz, s. A/B-Test)
    "PF_MEAN_MOVE_SEC": 10.0,           # mittlere Bewegungsdauer
    "PF_SAME_SENSOR_CORRELATION_SEC": 6.0,
    "PF_MISS_BASE_PROB": 0.1,           # „nicht gesehen“ trotz Nähe (Störung)
    "PF_RESAMPLE_ESS": 0.5,
    "PF_ALPHA_SLOW": 0.005,
    "PF_ALPHA_FAST": 0.1,
    "PF_MAX_INJECT": 0.2,
    "PF_ROUGHEN_CM": 3.0,
    "PF_ACCURACY_FLOOR_CM": 30.0,
    "PF_BOUNDS_MARGIN_CM": 300.0,
    "PF_TRACK_RESET_SEC": 600.0,        # nach so langer Pause neu initialisieren
    "PF_OUTPUT_SMOOTHING_SEC": 0.0,     # optionale Ausgabeglättung in Ruhe
    "MAX_POSITION_SPEED_CM_S": 350.0,
    "SENSOR_TIMEOUT_SEC": 30.0,
    "OFFLINE_TIMEOUT_SEC": 90.0,
    "RADIO_BASELINE_LEARNING_SAMPLES": 30,
    "RADIO_LINK_TIMEOUT_SEC": 30.0,
    "TAG_HEIGHT_CM": 25.0,              # Halsbandhöhe über dem Fußboden
}


def _p(params, key):
    value = params.get(key)
    return DEFAULTS[key] if value is None else value


class ParticleEngine:
    engine_name = "pf"

    def __init__(self, config_params: Optional[dict] = None, floorplan: Optional[FloorPlan] = None):
        self.params = dict(config_params or {})
        self.floorplan = floorplan if floorplan is not None else FloorPlan()
        self.sensors: Dict[str, SensorNode] = {}
        self.radio_env: Optional[RadioEnvironmentModel] = None
        self.rng = np.random.default_rng(self.params.get("RANDOM_SEED"))
        self.n = int(_p(self.params, "PF_PARTICLES"))
        self.lower = np.array([-500.0, -500.0])
        self.upper = np.array([500.0, 500.0])
        self._reset_filter()
        self.last_result: Optional[TrackingResult] = None
        self._last_logs: Dict[str, dict] = {}
        self._smoothed = None

    # ------------------------------------------------------------------
    # Aufbau
    # ------------------------------------------------------------------
    def _reset_filter(self):
        self.particles = None  # (N, 4): x, y, vx, vy
        self.mode = None       # (N,)  0 = Ruhe, 1 = Bewegung
        self.logw = None
        self.time = None
        self.last_positive_time = None
        self.s_slow = None
        self.s_fast = None
        self.sensor_last_update: Dict[str, float] = {}
        self._smoothed = None

    def set_floorplan(self, floorplan: FloorPlan):
        self.floorplan = floorplan or FloorPlan()
        self._update_bounds()
        self._wall_cache_clear()

    def setup_sensors(self, sensor_configs: dict):
        """Inkrementell: unveränderte Sensoren behalten ihren Zustand."""
        wanted = {}
        for sensor_id, data in sensor_configs.items():
            if not data.get("enabled", True) or not data.get("position_configured", True):
                continue
            wanted[sensor_id] = data
        for sensor_id in list(self.sensors):
            if sensor_id not in wanted:
                del self.sensors[sensor_id]
        for sensor_id, data in wanted.items():
            node = self.sensors.get(sensor_id)
            if node is None or node.signature != config_signature(data):
                new_node = SensorNode(sensor_id, data)
                if node is not None:  # Zeitstempel übernehmen, Filter neu
                    new_node.last_heard = node.last_heard
                    new_node.last_seen = node.last_seen
                    new_node.present = node.present
                    new_node.rssi = node.rssi
                self.sensors[sensor_id] = new_node
            else:
                node.name = data.get("name", sensor_id)
                node.ble_addresses = list(data.get("ble_addresses", []))
                node.topic = data.get("topic", node.topic)
        positions = {sid: node.pos for sid, node in self.sensors.items()}
        heights = {sid: node.antenna_z_cm for sid, node in self.sensors.items() if node.antenna_z_cm is not None}
        if self.radio_env is None:
            self.radio_env = RadioEnvironmentModel(
                positions,
                baseline_file=self.params.get("RADIO_BASELINE_FILE"),
                baseline_learning_samples=_p(self.params, "RADIO_BASELINE_LEARNING_SAMPLES"),
                link_timeout_sec=_p(self.params, "RADIO_LINK_TIMEOUT_SEC"),
                sensor_heights=heights,
            )
        else:
            self.radio_env.update_positions(positions, heights)
        self._update_bounds()
        self._wall_cache_clear()

    def _update_bounds(self):
        fp_bounds = self.floorplan.bounds() if self.floorplan else None
        if fp_bounds is not None:
            lower, upper = fp_bounds
            self.lower, self.upper = lower - 50.0, upper + 50.0
        elif self.sensors:
            pos = np.array([n.pos for n in self.sensors.values()])
            margin = float(_p(self.params, "PF_BOUNDS_MARGIN_CM"))
            self.lower, self.upper = pos.min(axis=0) - margin, pos.max(axis=0) + margin
        if self.particles is not None:
            self.particles[:, :2] = np.clip(self.particles[:, :2], self.lower, self.upper)

    def _wall_cache_clear(self):
        self._wall_cache = {}

    def observe_mesh(self, receiver: str, transmitter: str, rssi: float, timestamp: float):
        if self.radio_env:
            self.radio_env.observe(receiver, transmitter, rssi, timestamp)

    # ------------------------------------------------------------------
    # Partikel
    # ------------------------------------------------------------------
    def _sample_positions(self, count):
        """Gleichverteilt innerhalb der Räume (falls definiert) bzw. Grenzen."""
        if count <= 0:
            return np.empty((0, 2))
        if not self.floorplan.has_rooms:
            return self.rng.uniform(self.lower, self.upper, size=(count, 2))
        out = np.empty((0, 2))
        for _ in range(20):
            cand = self.rng.uniform(self.lower, self.upper, size=(count * 3, 2))
            cand = cand[self.floorplan.room_index(cand) >= 0]
            out = np.vstack([out, cand])
            if len(out) >= count:
                break
        if len(out) < count:
            out = np.vstack([out, self.rng.uniform(self.lower, self.upper, size=(count - len(out), 2))])
        return out[:count]

    def _initialize(self, t):
        self.particles = np.zeros((self.n, 4))
        self.particles[:, :2] = self._sample_positions(self.n)
        self.mode = (self.rng.random(self.n) < 0.2).astype(np.int8)
        self.logw = np.full(self.n, -math.log(self.n))
        self.time = t
        self.s_slow = None
        self.s_fast = None
        self.sensor_last_update = {}
        self._smoothed = None
        self._wall_cache_clear()

    def _predict(self, dt):
        if dt <= 0:
            return
        steps = int(min(max(math.ceil(dt / 1.0), 1), 10))
        h = dt / steps
        p = self.params
        rest_diff = float(_p(p, "PF_REST_DIFFUSION_CM"))
        move_diff = float(_p(p, "PF_MOVE_DIFFUSION_CM"))
        sigma_v = float(_p(p, "PF_MOVE_SPEED_CM_S"))
        tau = max(float(_p(p, "PF_MOVE_TAU_SEC")), 0.1)
        vmax = float(_p(p, "MAX_POSITION_SPEED_CM_S"))
        p_start = 1.0 - math.exp(-h / max(float(_p(p, "PF_MEAN_REST_SEC")), 1.0))
        p_stop = 1.0 - math.exp(-h / max(float(_p(p, "PF_MEAN_MOVE_SEC")), 0.5))
        decay = math.exp(-h / tau)
        vel_noise = sigma_v * math.sqrt(max(1.0 - decay * decay, 0.0))
        for _ in range(steps):
            u = self.rng.random(self.n)
            start = (self.mode == 0) & (u < p_start)
            stop = (self.mode == 1) & (u < p_stop)
            self.mode[start] = 1
            self.mode[stop] = 0
            self.particles[start, 2:] = self.rng.normal(0.0, sigma_v, size=(int(start.sum()), 2))
            self.particles[self.mode == 0, 2:] = 0.0

            moving = self.mode == 1
            old = self.particles[:, :2].copy()
            if moving.any():
                v = self.particles[moving, 2:] * decay + self.rng.normal(0.0, vel_noise, size=(int(moving.sum()), 2))
                speed = np.linalg.norm(v, axis=1)
                too_fast = speed > vmax
                v[too_fast] *= (vmax / speed[too_fast])[:, None]
                self.particles[moving, 2:] = v
            diff = np.where(moving, math.hypot(rest_diff, move_diff), rest_diff) * math.sqrt(h)
            self.particles[:, :2] += self.particles[:, 2:] * h + self.rng.normal(0.0, 1.0, size=(self.n, 2)) * diff[:, None]
            self.particles[:, :2] = np.clip(self.particles[:, :2], self.lower, self.upper)
            blocked = self.floorplan.blocked(old, self.particles[:, :2])
            if blocked.any():
                self.particles[blocked, :2] = old[blocked]
                self.particles[blocked, 2:] *= -0.3  # abprallen
        self._wall_cache_clear()

    # ------------------------------------------------------------------
    # Messmodell
    # ------------------------------------------------------------------
    def _expected_rssi(self, node: SensorNode, points: np.ndarray, use_cache=True):
        # Schräger Abstand: Boden-Abstand und Höhenunterschied Sensor ↔ Halsband
        dz = node.height_offset_cm(_p(self.params, "TAG_HEIGHT_CM"))
        d = np.sqrt(np.sum((points - node.pos[None, :]) ** 2, axis=1) + dz * dz)
        mu = node.tx_power - 10.0 * node.n_factor * np.log10(np.maximum(d, 30.0) / 100.0)
        if self.floorplan.has_walls:
            if use_cache and node.sensor_id in self._wall_cache:
                walls = self._wall_cache[node.sensor_id]
            else:
                walls = self.floorplan.attenuation_db(points, node.pos)
                if use_cache:
                    self._wall_cache[node.sensor_id] = walls
            mu = mu - walls
        return mu

    def _sigma(self, node: SensorNode, mu, count, quality):
        fading = node.fast_fading_variance(mu)
        k = max(min(int(count), int(_p(self.params, "PF_MAX_SAMPLE_COUNT"))), 1)
        var = node.sigma_db ** 2 + (1.57 if k > 1 else 1.0) * fading / k
        return np.sqrt(var) / math.sqrt(max(quality, 0.1))

    def _tempering(self, sensor_id, t):
        tau = float(_p(self.params, "PF_SAME_SENSOR_CORRELATION_SEC"))
        last = self.sensor_last_update.get(sensor_id)
        self.sensor_last_update[sensor_id] = t
        if tau <= 0 or last is None:
            return 1.0
        return float(np.clip((t - last) / tau, 0.2, 1.0))

    def _update(self, node: SensorNode, t, rssi, present, count):
        quality = self.radio_env.sensor_quality(node.sensor_id, t) if self.radio_env else 1.0
        offset = self.radio_env.get_receiver_offset(node.sensor_id, t) if self.radio_env else 0.0
        pts = self.particles[:, :2]
        mu = self._expected_rssi(node, pts) + offset
        beta = self._tempering(node.sensor_id, t)
        if present:
            sigma = self._sigma(node, mu, count, quality)
            nu = float(_p(self.params, "PF_STUDENT_NU"))
            r = (rssi - mu) / sigma
            ll = -0.5 * (nu + 1.0) * np.log1p(r * r / nu) - np.log(sigma)
        else:
            sigma = np.sqrt(node.sigma_db ** 2 + 9.0) / math.sqrt(max(quality, 0.1))
            p_det = _norm_cdf((mu - node.detection_floor) / sigma)
            base = float(_p(self.params, "PF_MISS_BASE_PROB"))
            # nie log(0): bei base = 0 und p_det = 1 würden alle Gewichte NaN
            ll = np.log(np.maximum(base + (1.0 - base) * (1.0 - p_det), 1e-9))
        ll = beta * ll
        prior = self.logw
        joint = prior + ll
        m = float(np.max(joint))
        if not math.isfinite(m):
            return offset  # Messung passt zu keinem Partikel (numerisch) → verwerfen statt Gewichte zu zerstören
        log_marginal = m + math.log(float(np.sum(np.exp(joint - m))))
        self.logw = joint - log_marginal
        if present:
            # log p(z | bisherige Daten) je Einzelmessung → AMCL-Mittelwerte
            per_meas = log_marginal / max(beta, 1e-6)
            a_slow = float(_p(self.params, "PF_ALPHA_SLOW"))
            a_fast = float(_p(self.params, "PF_ALPHA_FAST"))
            if self.s_slow is None:
                self.s_slow = self.s_fast = per_meas
            else:
                self.s_slow += a_slow * (per_meas - self.s_slow)
                self.s_fast += a_fast * (per_meas - self.s_fast)
        return offset

    def _ess(self):
        w = np.exp(self.logw)
        return 1.0 / float(np.sum(w * w))

    def _resample(self, node: Optional[SensorNode], rssi: Optional[float]):
        inject_frac = 0.0
        if self.s_slow is not None and self.s_fast is not None:
            inject_frac = max(0.0, 1.0 - math.exp(min(self.s_fast - self.s_slow, 0.0)))
            inject_frac = min(inject_frac, float(_p(self.params, "PF_MAX_INJECT")))
        ess_limit = float(_p(self.params, "PF_RESAMPLE_ESS")) * self.n
        # kleine Schwelle gegen ständiges Einstreuen, aber nie über dem eingestellten Maximum
        threshold = min(0.02, 0.5 * float(_p(self.params, "PF_MAX_INJECT")))
        n_inject = int(round(inject_frac * self.n)) if inject_frac > threshold else 0
        if self._ess() >= ess_limit and n_inject == 0:
            return
        w = np.exp(self.logw)
        w /= w.sum()
        keep = self.n - n_inject
        positions = (self.rng.random() + np.arange(keep)) / keep
        idx = np.searchsorted(np.cumsum(w), positions)
        idx = np.minimum(idx, self.n - 1)
        new_particles = self.particles[idx].copy()
        new_mode = self.mode[idx].copy()
        rough = float(_p(self.params, "PF_ROUGHEN_CM"))
        if rough > 0:
            new_particles[:, :2] += self.rng.normal(0.0, rough, size=(keep, 2))
        if n_inject:
            inj = np.zeros((n_inject, 4))
            half = n_inject // 2 if (node is not None and rssi is not None) else 0
            if half:
                # Ring um den meldenden Sensor (Radius aus invertiertem Pegel)
                n_f = max(node.n_factor, 1.0)
                log_r = (node.tx_power - rssi) / (10.0 * n_f)
                slant = 100.0 * 10.0 ** (log_r + self.rng.normal(0.0, node.sigma_db / (10.0 * n_f), size=half))
                dz = node.height_offset_cm(_p(self.params, "TAG_HEIGHT_CM"))
                radius = np.sqrt(np.maximum(slant * slant - dz * dz, 0.0))  # auf den Boden projiziert
                radius = np.clip(radius, 10.0, 3000.0)
                ang = self.rng.uniform(0.0, 2.0 * np.pi, size=half)
                inj[:half, 0] = node.pos[0] + radius * np.cos(ang)
                inj[:half, 1] = node.pos[1] + radius * np.sin(ang)
            inj[half:, :2] = self._sample_positions(n_inject - half)
            inj[:, :2] = np.clip(inj[:, :2], self.lower, self.upper)
            new_particles = np.vstack([new_particles, inj])
            new_mode = np.concatenate([new_mode, (self.rng.random(n_inject) < 0.5).astype(np.int8)])
            # Nach einer Injektion nicht sofort erneut injizieren
            self.s_fast = self.s_slow
        new_particles[:, :2] = np.clip(new_particles[:, :2], self.lower, self.upper)
        self.particles = new_particles
        self.mode = new_mode
        self.logw = np.full(self.n, -math.log(self.n))
        self._wall_cache_clear()

    # ------------------------------------------------------------------
    # Schätzung
    # ------------------------------------------------------------------
    def _estimate(self):
        w = np.exp(self.logw)
        w /= w.sum()
        pts = self.particles[:, :2]
        mean = w @ pts
        room = None
        room_probs: Dict[str, float] = {}
        if self.floorplan.has_rooms:
            idx = self.floorplan.room_index(pts)
            for i, r in enumerate(self.floorplan.rooms):
                room_probs[r.name] = room_probs.get(r.name, 0.0) + float(w[idx == i].sum())
            outside = float(w[idx < 0].sum())
            if outside > 0.01:
                room_probs["außerhalb"] = outside
            best = max(room_probs, key=room_probs.get)
            room = best if best != "außerhalb" else None
            if room is not None and room_probs[best] >= 0.5:
                mean_room = self.floorplan.room_name(mean)
                if mean_room != room:
                    sel = np.isin(idx, [i for i, r in enumerate(self.floorplan.rooms) if r.name == room])
                    ws = w[sel]
                    mean = (ws @ pts[sel]) / ws.sum()
        centered = pts - mean
        cov = centered.T @ (centered * w[:, None])
        accuracy = max(float(math.sqrt(max(np.trace(cov), 0.0))), float(_p(self.params, "PF_ACCURACY_FLOOR_CM")))
        p_move = float(w[self.mode == 1].sum())
        return mean, accuracy, room, room_probs, p_move

    # ------------------------------------------------------------------
    # Tick
    # ------------------------------------------------------------------
    def process_tick(self, now: float) -> TrackingResult:
        samples = []
        for sid, node in self.sensors.items():
            while node.pending:
                t, rssi, present, count = node.pending.popleft()
                samples.append((t, sid, rssi, present, count))
        samples.sort(key=lambda s: s[0])

        reset_after = float(_p(self.params, "PF_TRACK_RESET_SEC"))
        updated = False
        for t, sid, rssi, present, count in samples:
            node = self.sensors.get(sid)
            if node is None:
                continue
            if present and (
                self.particles is None
                or (self.last_positive_time is not None and t - self.last_positive_time > reset_after)
            ):
                self._initialize(t)
            if self.particles is None:
                continue  # ohne Sichtung keine Initialisierung aus reinen Negativdaten
            dt = max(t - self.time, 0.0)
            self._predict(dt)
            self.time = max(self.time, t)
            offset = self._update(node, t, rssi, present, count)
            self._log_sensor(node, t, rssi, present, count, offset)
            self._resample(node if present else None, rssi if present else None)
            if present:
                self.last_positive_time = t
            updated = True

        return self._build_result(now, updated)

    def _log_sensor(self, node, t, rssi, present, count, offset):
        self._last_logs[node.sensor_id] = {
            "t": t, "rssi": rssi, "present": present, "count": count, "offset": offset,
        }

    def _build_result(self, now, updated) -> TrackingResult:
        timeout = float(_p(self.params, "SENSOR_TIMEOUT_SEC"))
        offline_timeout = float(_p(self.params, "OFFLINE_TIMEOUT_SEC"))
        contributing, inactive, rejected = [], [], []
        for sid, node in self.sensors.items():
            if node.is_fresh(now, timeout):
                contributing.append(sid)
            elif node.is_online(now, offline_timeout) and node.last_seen is not None:
                inactive.append(sid)
        radio_diag = self.radio_env.diagnostics(now) if self.radio_env else {}

        if self.particles is None or not contributing:
            result = TrackingResult(
                state="inaktiv", x_cm=None, y_cm=None, accuracy_cm=None,
                active_sensors_count=0, contributing_sensors=[], rejected_sensors=[],
                inactive_sensors=inactive, sensor_measurements=self._measurement_logs(None, now),
                radio_diagnostics=radio_diag, engine=self.engine_name, updated=updated,
            )
            self._smoothed = None
            self.last_result = result
            return result

        if not updated and self.last_result is not None and self.last_result.state == "aktiv":
            # Nur Zustandslisten aktualisieren, Schätzung unverändert.
            self.last_result.contributing_sensors = contributing
            self.last_result.inactive_sensors = inactive
            self.last_result.active_sensors_count = len(contributing)
            self.last_result.updated = False
            return self.last_result

        mean, accuracy, room, room_probs, p_move = self._estimate()
        tau = float(_p(self.params, "PF_OUTPUT_SMOOTHING_SEC"))
        if tau > 0 and p_move < 0.3 and self._smoothed is not None:
            dt = max(now - self._smoothed[1], 0.0)
            a = 1.0 - math.exp(-dt / tau)
            mean = self._smoothed[0] + a * (mean - self._smoothed[0])
        self._smoothed = (mean.copy(), now)

        logs = self._measurement_logs(mean, now)
        for entry in logs:
            if entry["used"] and entry.get("residual_db") is not None and abs(entry["residual_db"]) > 3.0 * entry["sigma_db"]:
                rejected.append(entry["sensor"])
        result = TrackingResult(
            state="aktiv", x_cm=float(mean[0]), y_cm=float(mean[1]), accuracy_cm=accuracy,
            active_sensors_count=len(contributing), contributing_sensors=contributing,
            rejected_sensors=rejected, inactive_sensors=inactive, sensor_measurements=logs,
            radio_diagnostics=radio_diag, room=room,
            room_probabilities={k: round(v, 3) for k, v in room_probs.items()},
            moving_probability=round(p_move, 3), engine=self.engine_name, updated=updated,
        )
        self.last_result = result
        return result

    def _measurement_logs(self, estimate, now) -> List[dict]:
        logs = []
        timeout = float(_p(self.params, "SENSOR_TIMEOUT_SEC"))
        for sid, node in self.sensors.items():
            entry = self._last_logs.get(sid)
            if entry is None:
                continue
            item = {
                "sensor": node.name,
                "raw_rssi": None if entry["rssi"] is None else round(float(entry["rssi"]), 1),
                "filtered_rssi": None if entry["rssi"] is None else round(float(entry["rssi"] - entry["offset"]), 1),
                "receiver_offset_db": round(float(entry["offset"]), 2),
                "sample_count": entry["count"],
                "present": entry["present"],
                "age_s": round(now - entry["t"], 1),
                "radio_quality": round(self.radio_env.sensor_quality(sid, now), 3) if self.radio_env else 1.0,
                "used": bool(entry["present"]) and node.is_fresh(now, timeout),
                "reason": "" if entry["present"] else "nicht gesehen",
                "distance_cm": None,
                "sigma_db": round(float(node.sigma_db), 2),
            }
            if estimate is not None:
                mu = float(self._expected_rssi(node, estimate.reshape(1, 2), use_cache=False)[0])
                item["expected_rssi"] = round(mu, 1)
                item["distance_cm"] = round(float(np.linalg.norm(estimate - node.pos)), 1)
                if entry["rssi"] is not None:
                    item["residual_db"] = round(float(entry["rssi"] - entry["offset"] - mu), 1)
            logs.append(item)
        return logs

    # ------------------------------------------------------------------
    def save_state(self):
        if self.radio_env:
            self.radio_env.save_baseline()
