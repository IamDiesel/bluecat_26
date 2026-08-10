import time
import numpy as np


class TrackingFilter:
    def __init__(
            self,
            tx_power=-59.0,
            r_min=5.0,
            r_max=50.0,
            rssi_limit=-110.0,
            window_size=7,
            eps=4.0,
            min_samples=3,
            q_variance=0.1,
            innovation_gate_sigma=4.0,
    ):
        self.tx_power = float(tx_power)
        self.r_min = max(float(r_min), 0.0)
        self.r_max = max(float(r_max), 0.0)
        self.rssi_limit = float(rssi_limit)
        if self.rssi_limit >= self.tx_power:
            raise ValueError("rssi_limit muss kleiner als tx_power/RSSI@1m sein.")

        self.variance_floor = 0.25
        self.near_variance = max(min(self.r_min, self.r_max), self.variance_floor)
        self.far_variance = max(max(self.r_min, self.r_max), self.variance_floor)

        self.window_size = max(int(window_size), 3)
        self.eps = max(float(eps), 0.1)
        self.min_samples = max(int(min_samples), 2)
        self.history = []

        self.last_update_time = None
        self.x = np.zeros((2, 1))
        self.P = np.eye(2) * 500.0
        self.H = np.array([[1.0, 0.0]])
        self.q_variance = max(float(q_variance), 0.0)
        self.innovation_gate_sigma = max(float(innovation_gate_sigma), 1.0)
        self.is_initialized = False
        self.last_measurement_variance = self.far_variance

    def reset(self):
        self.history.clear()
        self.last_update_time = None
        self.x = np.zeros((2, 1))
        self.P = np.eye(2) * 500.0
        self.is_initialized = False
        self.last_measurement_variance = self.far_variance

    def dbscan_1d(self, current_rssi):
        self.history.append(float(current_rssi))
        if len(self.history) > self.window_size:
            self.history.pop(0)

        if len(self.history) < self.min_samples:
            return float(current_rssi)

        data = np.asarray(self.history, dtype=float)
        median = float(np.median(data))
        mad = float(np.median(np.abs(data - median)))
        robust_sigma = max(1.4826 * mad, 0.5)
        threshold = max(self.eps, 3.0 * robust_sigma)
        inliers = data[np.abs(data - median) <= threshold]

        if len(inliers) == 0:
            return median
        return float(np.mean(inliers))

    def calculate_dynamic_r(self, current_rssi):
        denominator = abs(self.rssi_limit - self.tx_power)
        if denominator < 1e-9:
            self.last_measurement_variance = self.far_variance
            return np.array([[self.last_measurement_variance]])

        rssi_val = max(float(current_rssi), self.rssi_limit)
        rssi_val = min(rssi_val, self.tx_power)
        factor = (abs(rssi_val - self.tx_power) / denominator) ** 2
        factor = min(max(factor, 0.0), 1.0)

        variance = self.near_variance + factor * (
                self.far_variance - self.near_variance
        )
        self.last_measurement_variance = max(
            float(variance), self.variance_floor
        )
        return np.array([[self.last_measurement_variance]])

    def distance_std(self, distance_cm, n_factor):
        n_factor = max(float(n_factor), 1e-6)
        derivative = distance_cm * np.log(10.0) / (10.0 * n_factor)
        return float(abs(derivative) * np.sqrt(self.last_measurement_variance))

    def update(self, raw_rssi, sample_time=None):
        raw_rssi = float(raw_rssi)
        if not np.isfinite(raw_rssi) or raw_rssi <= -120.0:
            self.reset()
            return raw_rssi

        now = time.monotonic() if sample_time is None else float(sample_time)

        filtered_rssi = self.dbscan_1d(raw_rssi)
        z = np.array([[filtered_rssi]])

        if not self.is_initialized:
            self.x = np.array([[filtered_rssi], [0.0]])
            self.P = np.eye(2) * 500.0
            self.last_update_time = now
            self.is_initialized = True
            self.calculate_dynamic_r(filtered_rssi)
            return filtered_rssi

        dt = now - self.last_update_time
        if not np.isfinite(dt) or dt <= 0.0:
            dt = 1e-3
        dt = min(dt, 10.0)
        self.last_update_time = now

        F = np.array([[1.0, dt], [0.0, 1.0]])
        Q = self.q_variance * np.array(
            [
                [(dt ** 4) / 4.0, (dt ** 3) / 2.0],
                [(dt ** 3) / 2.0, dt ** 2],
            ]
        )
        R_k = self.calculate_dynamic_r(filtered_rssi)

        x_pred = F @ self.x
        P_pred = F @ self.P @ F.T + Q

        innovation = z - self.H @ x_pred
        S = self.H @ P_pred @ self.H.T + R_k
        normalized_innovation = abs(float(innovation[0, 0])) / np.sqrt(
            max(float(S[0, 0]), 1e-9)
        )

        # DER NEUE STRENGE TÜRSTEHER
        if normalized_innovation > self.innovation_gate_sigma:
            if normalized_innovation > self.innovation_gate_sigma * 1.5:
                # Extremwert / Physikalische Teleportation:
                # Wert wird radikal blockiert, Filter bleibt auf letztem Kurs.
                self.x = x_pred
                self.P = P_pred
                return float(self.x[0, 0])

            # Stark abweichender Wert, aber noch im Grenzbereich:
            # Messvarianz wird extrem aufgeblasen (wenig Vertrauen).
            inflation = min(
                (normalized_innovation / self.innovation_gate_sigma) ** 3,
                500.0
            )
            R_k = R_k * inflation
            self.last_measurement_variance *= inflation
            S = self.H @ P_pred @ self.H.T + R_k

        K = P_pred @ self.H.T @ np.linalg.inv(S)

        self.x = x_pred + K @ innovation
        identity = np.eye(2)
        self.P = (identity - K @ self.H) @ P_pred
        self.P = (self.P + self.P.T) / 2.0

        return float(self.x[0, 0])