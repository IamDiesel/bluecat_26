import numpy as np

class IMMCKFilter:
    """Interacting Multiple Model Constrained Kalman Filter (IMM-CKF).
    
    Verschmilzt das Bayes'sche Resultat des Partikelfilters mit einem 
    physikalischen 4D-Kinematikmodell. Löst den Konflikt zwischen Jitter 
    und Lag durch zwei parallele Bewegungsmodelle (Stillstand vs. Bewegung).
    """
    
    def __init__(self, q_stop=1.0, q_cv=500.0):
        self.q_stop = float(q_stop)
        self.q_cv = float(q_cv)
        
        # Markov Übergangsmatrix: P(Stop->Stop), P(Stop->CV) | P(CV->Stop), P(CV->CV)
        self.trans_prob = np.array([
            [0.95, 0.05], 
            [0.05, 0.95]
        ])
        
        # Beobachtungsmatrix H (Wir messen nur Position X,Y, nicht die Geschwindigkeit)
        self.H = np.array([
            [1.0, 0.0, 0.0, 0.0], 
            [0.0, 1.0, 0.0, 0.0]
        ])
        self.reset()

    def reset(self):
        self.last_timestamp = None
        # Zustand x = [X, Y, dX, dY]^T für beide Modelle
        self.x = [np.zeros(4), np.zeros(4)]
        self.P = [np.eye(4) * 1000.0, np.eye(4) * 1000.0]
        # Modellwahrscheinlichkeiten mu = [P_stop, P_cv]
        self.mu = np.array([0.5, 0.5])

    def apply_movement(self, candidate_position, accuracy_cm, now):
        """
        Führt das IMM-Mixing, die Kalman-Prädiktion und das Update durch.
        """
        candidate_position = np.asarray(candidate_position, dtype=float).reshape(2)
        
        if self.last_timestamp is None:
            self.last_timestamp = now
            for i in range(2):
                self.x[i][:2] = candidate_position
            return candidate_position, False, "IMM Initialisierung"

        dt = max(now - self.last_timestamp, 0.05)
        dt = min(dt, 5.0)
        self.last_timestamp = now

        # ---------------------------------------------------------
        # 1. IMM INTERACTION / MIXING
        # ---------------------------------------------------------
        c_bar = np.dot(self.trans_prob.T, self.mu)
        mu_mix = (self.trans_prob * self.mu[:, None]) / c_bar

        x_mix = [np.zeros(4), np.zeros(4)]
        P_mix = [np.zeros((4, 4)), np.zeros((4, 4))]

        for j in range(2):
            for i in range(2):
                x_mix[j] += mu_mix[i, j] * self.x[i]
            for i in range(2):
                diff = (self.x[i] - x_mix[j]).reshape(-1, 1)
                P_mix[j] += mu_mix[i, j] * (self.P[i] + np.dot(diff, diff.T))

        # ---------------------------------------------------------
        # 2. KALMAN FILTERING (Für beide Modelle)
        # ---------------------------------------------------------
        F = np.array([
            [1.0, 0.0,  dt, 0.0],
            [0.0, 1.0, 0.0,  dt],
            [0.0, 0.0, 1.0, 0.0],
            [0.0, 0.0, 0.0, 1.0]
        ])

        # Prozessrauschen (Discrete White Noise Acceleration)
        G = np.array([
            [dt**2 / 2.0, 0.0],
            [0.0, dt**2 / 2.0],
            [dt, 0.0],
            [0.0, dt]
        ])
        Q_base = np.dot(G, G.T)
        Q = [self.q_stop * Q_base, self.q_cv * Q_base]

        # Messrauschen R_k wird direkt aus der Ungenauigkeit des Partikelfilters abgeleitet
        r_val = max((accuracy_cm / 2.0)**2, 25.0)
        R = np.eye(2) * r_val
        z = candidate_position

        likelihoods = np.zeros(2)

        for i in range(2):
            # Prädiktion
            x_pred = np.dot(F, x_mix[i])
            P_pred = np.dot(F, np.dot(P_mix[i], F.T)) + Q[i]

            # Innovation
            y_res = z - np.dot(self.H, x_pred)
            S = np.dot(self.H, np.dot(P_pred, self.H.T)) + R

            # Likelihood (Wie gut passt das Modell zur Messung?)
            det_S = max(np.linalg.det(S), 1e-9)
            inv_S = np.linalg.inv(S)
            exponent = -0.5 * np.dot(y_res.T, np.dot(inv_S, y_res))
            exponent = np.clip(exponent, -700, 700) # Verhindert Overflow
            
            likelihoods[i] = (1.0 / np.sqrt((2.0 * np.pi)**2 * det_S)) * np.exp(exponent)

            # Update
            K = np.dot(P_pred, np.dot(self.H.T, inv_S))
            self.x[i] = x_pred + np.dot(K, y_res)
            self.P[i] = np.dot(np.eye(4) - np.dot(K, self.H), P_pred)

        # ---------------------------------------------------------
        # 3. PROBABILITY UPDATE & COMBINATION
        # ---------------------------------------------------------
        self.mu = c_bar * likelihoods
        sum_mu = np.sum(self.mu)
        if sum_mu == 0:
            self.mu = np.array([0.5, 0.5])
        else:
            self.mu /= sum_mu

        x_comb = np.zeros(4)
        for i in range(2):
            x_comb += self.mu[i] * self.x[i]

        final_position = x_comb[:2]
        
        # Status für die Konsole/Logs aufbereiten
        mode_str = "Stillstand" if self.mu[0] > self.mu[1] else "Bewegung"
        confidence = max(self.mu) * 100.0
        reason = f"IMM-CKF aktiv ({mode_str} {confidence:.0f}%, PF-Noise: {accuracy_cm:.0f}cm)"

        # Da der KF mathematisch glättet, gibt es kein hartes "Rejected" mehr.
        return final_position, False, reason