import numpy as np


class IMMCKFilter:
    """Interacting Multiple Model Kalman Filter (Legacy-Modell).

    Zwei Modelle für x = [X, Y, dX, dY]:
    * Modell 0 „Stillstand“: Geschwindigkeit ist 0, Position diffundiert nur
      minimal (echtes Zero-Velocity-Modell statt CV mit kleinem q).
    * Modell 1 „Bewegung“: Constant Velocity mit Beschleunigungsrauschen.

    Die Messung (Position aus dem Partikelfilter) kommt mit einem Messrauschen
    aus der PF-Unsicherheit. Hinweis: Die PF-Ausgabe ist selbst schon gefiltert;
    das neue Modell (``core/pf_engine.py``) vermeidet diese Kaskade.
    """

    def __init__(self, q_stop=25.0, q_cv=20000.0, max_dt=10.0):
        # q_stop: Positionsdiffusion in Ruhe [cm²/s]
        # q_cv:   Beschleunigungs-Spektraldichte in Bewegung [cm²/s³]
        #         20000 ≈ σ_a ≈ 140 cm/s² über 1 s – realistisch für eine Katze
        self.q_stop = float(q_stop)
        self.q_cv = float(q_cv)
        self.max_dt = float(max_dt)
        self.trans_prob = np.array([[0.95, 0.05], [0.10, 0.90]])
        self.H = np.array([[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]])
        self.reset()

    def reset(self):
        self.last_timestamp = None
        self.x = [np.zeros(4), np.zeros(4)]
        self.P = [np.eye(4) * 1000.0, np.eye(4) * 1000.0]
        self.mu = np.array([0.5, 0.5])
        self.last_covariance = None

    def _models(self, dt):
        f_cv = np.array([[1.0, 0.0, dt, 0.0], [0.0, 1.0, 0.0, dt], [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0]])
        f_stop = np.diag([1.0, 1.0, 0.0, 0.0])
        q_cv = self.q_cv * np.array([
            [dt ** 3 / 3, 0, dt ** 2 / 2, 0],
            [0, dt ** 3 / 3, 0, dt ** 2 / 2],
            [dt ** 2 / 2, 0, dt, 0],
            [0, dt ** 2 / 2, 0, dt],
        ])
        q_stop = np.diag([self.q_stop * dt, self.q_stop * dt, 1e-6, 1e-6])
        return [f_stop, f_cv], [q_stop, q_cv]

    def apply_movement(self, candidate_position, accuracy_cm, now):
        candidate_position = np.asarray(candidate_position, dtype=float).reshape(2)
        # accuracy ≈ RMS-Radius → Varianz je Achse = accuracy² / 2
        r_val = max(float(accuracy_cm) ** 2 / 2.0, 25.0)
        R = np.eye(2) * r_val

        if self.last_timestamp is None:
            self.last_timestamp = now
            for i in range(2):
                self.x[i] = np.array([candidate_position[0], candidate_position[1], 0.0, 0.0])
                self.P[i] = np.diag([r_val, r_val, 100.0 ** 2, 100.0 ** 2])
            self.last_covariance = R
            return candidate_position, False, "IMM Initialisierung"

        dt = float(np.clip(now - self.last_timestamp, 0.05, self.max_dt))
        self.last_timestamp = now

        c_bar = self.trans_prob.T @ self.mu
        mu_mix = (self.trans_prob * self.mu[:, None]) / np.maximum(c_bar, 1e-12)
        x_mix, P_mix = [], []
        for j in range(2):
            xj = sum(mu_mix[i, j] * self.x[i] for i in range(2))
            Pj = np.zeros((4, 4))
            for i in range(2):
                diff = (self.x[i] - xj).reshape(-1, 1)
                Pj += mu_mix[i, j] * (self.P[i] + diff @ diff.T)
            x_mix.append(xj)
            P_mix.append(Pj)

        F, Q = self._models(dt)
        likelihoods = np.zeros(2)
        for i in range(2):
            x_pred = F[i] @ x_mix[i]
            P_pred = F[i] @ P_mix[i] @ F[i].T + Q[i]
            y_res = candidate_position - self.H @ x_pred
            S = self.H @ P_pred @ self.H.T + R
            inv_S = np.linalg.inv(S)
            det_S = max(float(np.linalg.det(S)), 1e-9)
            exponent = float(np.clip(-0.5 * y_res @ inv_S @ y_res, -700, 700))
            likelihoods[i] = np.exp(exponent) / np.sqrt((2.0 * np.pi) ** 2 * det_S)
            K = P_pred @ self.H.T @ inv_S
            self.x[i] = x_pred + K @ y_res
            I_KH = np.eye(4) - K @ self.H
            self.P[i] = I_KH @ P_pred @ I_KH.T + K @ R @ K.T  # Joseph-Form

        mu = c_bar * likelihoods
        total = float(np.sum(mu))
        self.mu = mu / total if total > 0 and np.isfinite(total) else np.array([0.5, 0.5])

        x_comb = sum(self.mu[i] * self.x[i] for i in range(2))
        P_comb = np.zeros((4, 4))
        for i in range(2):
            diff = (self.x[i] - x_comb).reshape(-1, 1)
            P_comb += self.mu[i] * (self.P[i] + diff @ diff.T)
        self.last_covariance = P_comb[:2, :2]

        mode_str = "Stillstand" if self.mu[0] > self.mu[1] else "Bewegung"
        reason = f"IMM ({mode_str} {max(self.mu) * 100:.0f}%, PF-Unsicherheit {accuracy_cm:.0f} cm)"
        return x_comb[:2], False, reason

    @property
    def moving_probability(self):
        return float(self.mu[1])
