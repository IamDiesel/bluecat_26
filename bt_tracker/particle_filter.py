"""Robuster kinematischer Partikelfilter für die BLE-Positionsschätzung."""

from __future__ import annotations

import time
import numpy as np


class ParticleFilter:
    """Schätzt eine 2D-Position aus verrauschten Distanzmessungen.

    Nutzt ein kinematisches Modell (Position + Geschwindigkeit) mit Trägheit.
    Beinhaltet Augmented Monte Carlo Localization (AMCL): Bei Orientierungsverlust
    werden Rettungs-Partikel ringförmig um die messenden Sensoren injiziert.
    """

    def __init__(
        self,
        anchor_positions,
        particle_count=1500,
        bounds_margin_cm=500.0,
        process_noise_cm=75.0,
        initial_spread_cm=250.0,
        minimum_sigma_cm=75.0,
        velocity_noise_cm_s=40.0,  # Neues Rauschen für Geschwindigkeitsänderungen
        velocity_damping=0.85,     # Trägheit (1.0 = gleitet endlos, 0.0 = keine Trägheit)
        recovery_ratio=0.10,       # 10% der Partikel sind für AMCL Homecoming
        random_seed=None,
    ):
        positions = np.asarray(anchor_positions, dtype=float)
        if positions.size == 0:
            raise ValueError("Mindestens ein Sensor ist für den Partikelfilter nötig.")
        self.anchor_positions = positions.reshape((-1, 2))

        self.particle_count = max(int(particle_count), 100)
        self.bounds_margin_cm = max(float(bounds_margin_cm), 0.0)
        self.process_noise_cm = max(float(process_noise_cm), 1.0)
        self.initial_spread_cm = max(float(initial_spread_cm), 1.0)
        self.minimum_sigma_cm = max(float(minimum_sigma_cm), 1.0)

        self.velocity_noise_cm_s = float(velocity_noise_cm_s)
        self.velocity_damping = float(velocity_damping)
        self.recovery_ratio = float(recovery_ratio)
        self.rng = np.random.default_rng(random_seed)

        min_position = np.min(self.anchor_positions, axis=0)
        max_position = np.max(self.anchor_positions, axis=0)
        self.lower_bounds = min_position - self.bounds_margin_cm
        self.upper_bounds = max_position + self.bounds_margin_cm

        # Status ist nun 4D: [x, y, dx, dy]
        self.particles = None
        self.weights = None
        self.estimate = None
        self.covariance = None
        self.last_update_time = None
        self.last_accuracy_cm = None
        self.effective_sample_size = 0.0
        # Adaptive AMCL-Injektion (w_slow/w_fast als Log-Mittel der Likelihood)
        self.alpha_slow = 0.01
        self.alpha_fast = 0.2
        self.log_w_slow = None
        self.log_w_fast = None

    def reset(self, initial_position=None):
        """Setzt den Filter zurück und initialisiert die 4D-Partikel."""
        self.particles = np.zeros((self.particle_count, 4))

        if initial_position is None:
            self.particles[:, :2] = self.rng.uniform(
                self.lower_bounds,
                self.upper_bounds,
                size=(self.particle_count, 2),
            )
            self.estimate = np.mean(self.particles[:, :2], axis=0)
        else:
            initial_position = np.asarray(initial_position, dtype=float).reshape(2)
            self.particles[:, :2] = (
                initial_position
                + self.rng.normal(
                    0.0,
                    self.initial_spread_cm,
                    size=(self.particle_count, 2),
                )
            )
            self._clip_particles()
            self.estimate = initial_position.copy()

        self.weights = np.full(self.particle_count, 1.0 / self.particle_count)
        self.covariance = np.eye(2) * self.initial_spread_cm**2
        self.last_update_time = None
        self.last_accuracy_cm = None
        self.effective_sample_size = float(self.particle_count)
        self.log_w_slow = None
        self.log_w_fast = None

    def _clip_particles(self):
        """Hält die Positionen innerhalb der erlaubten Raumgrenzen."""
        self.particles[:, :2] = np.clip(
            self.particles[:, :2], self.lower_bounds, self.upper_bounds
        )

    def _predict(self, now):
        """Kinematische Vorhersage (Bewegung anhand der Trägheit)."""
        if self.last_update_time is None:
            self.last_update_time = now
            return

        dt = max(now - self.last_update_time, 0.05)
        dt = min(dt, 5.0)

        # 1. Position = Position + Geschwindigkeit * dt
        self.particles[:, 0] += self.particles[:, 2] * dt
        self.particles[:, 1] += self.particles[:, 3] * dt

        # 2. Dämpfung der Geschwindigkeit (Abbremsen ohne neuen Schub)
        # Dämpfung pro Sekunde (unabhängig von der Update-Rate)
        damping = self.velocity_damping ** dt
        self.particles[:, 2] *= damping
        self.particles[:, 3] *= damping

        # 3. Prozessrauschen auf Position und Geschwindigkeit addieren
        pos_noise = self.rng.normal(0.0, self.process_noise_cm * np.sqrt(dt), size=(self.particle_count, 2))
        vel_noise = self.rng.normal(0.0, self.velocity_noise_cm_s * np.sqrt(dt), size=(self.particle_count, 2))

        self.particles[:, :2] += pos_noise
        self.particles[:, 2:] += vel_noise

        self._clip_particles()
        self.last_update_time = now

    def _systematic_resample(self, anchors, distances, sigmas, inject_ratio=None):
        """Resampling mit (adaptiver) AMCL Homecoming Injection."""
        ratio = self.recovery_ratio if inject_ratio is None else inject_ratio
        keep_count = int(self.particle_count * (1.0 - ratio))
        inject_count = self.particle_count - keep_count

        # 1. Standard-Resampling für die verbleibenden "guten" Partikel
        cumulative = np.cumsum(self.weights)
        cumulative[-1] = 1.0
        positions = (self.rng.random() + np.arange(keep_count)) / keep_count
        indices = np.searchsorted(cumulative, positions)
        kept = self.particles[indices].copy()

        # Leichter Jitter für die überlebenden Partikel
        kept[:, :2] += self.rng.normal(0.0, self.process_noise_cm * 0.15, size=(keep_count, 2))

        # 2. AMCL Homecoming: Rettungspartikel um aktive Sensoren auswerfen
        injected = np.zeros((inject_count, 4))
        if len(anchors) > 0 and inject_count > 0:
            anchor_indices = self.rng.choice(len(anchors), size=inject_count)
            angles = self.rng.uniform(0, 2 * np.pi, size=inject_count)
            # Distanzen mit Sensorunsicherheit verrauschen
            radii = self.rng.normal(distances[anchor_indices], sigmas[anchor_indices])
            radii = np.maximum(radii, 10.0)  # Verhindert negative Distanzen

            injected[:, 0] = anchors[anchor_indices, 0] + radii * np.cos(angles)
            injected[:, 1] = anchors[anchor_indices, 1] + radii * np.sin(angles)
            # dx und dy bleiben auf 0.0 initilisiert

        self.particles = np.vstack((kept, injected))
        self.weights.fill(1.0 / self.particle_count)
        self._clip_particles()

    def update(
        self,
        active_anchors,
        distances,
        distance_stds,
        n_factors=None,       # NEU: N-Faktoren der Sensoren
        grid_map=None,        # NEU: Das Tomographie-Modell
        sensor_qualities=None,
        timestamp=None,
        prior_position=None,
    ):
        """Führt einen Partikelfilter-Update durch."""
        anchors = np.asarray(active_anchors, dtype=float).reshape((-1, 2))
        distances = np.asarray(distances, dtype=float).reshape(-1)
        sigmas = np.asarray(distance_stds, dtype=float).reshape(-1)

        if len(anchors) == 0 or len(anchors) != len(distances) or len(anchors) != len(sigmas):
            return None
        if np.any(~np.isfinite(anchors)) or np.any(~np.isfinite(distances)) or np.any(distances < 0):
            return None

        if sensor_qualities is None:
            qualities = np.ones(len(anchors), dtype=float)
        else:
            qualities = np.asarray(sensor_qualities, dtype=float).reshape(-1)
            if len(qualities) != len(anchors):
                qualities = np.ones(len(anchors), dtype=float)
                
        qualities = np.clip(qualities, 0.1, 1.0)
        sigmas = np.maximum(sigmas / np.sqrt(qualities), self.minimum_sigma_cm)

        now = time.monotonic() if timestamp is None else float(timestamp)
        if self.particles is None:
            self.reset(prior_position)
        self._predict(now)

        # 1. Physische Distanz berechnen (Shape: P, S)
        predicted_distances = np.linalg.norm(
            self.particles[:, None, :2] - anchors[None, :, :],
            axis=2,
        )

        # 2. NEU: Tomographie-Dämpfung anwenden
        if grid_map is not None and n_factors is not None:
            attenuation_db = grid_map.get_expected_attenuation_vectorized(anchors, self.particles[:, :2])
            n_factors_arr = np.asarray(n_factors, dtype=float).reshape(1, -1)
            # Scheinbare Distanz = Echte Distanz * 10^(Dämpfung / (10 * n_factor))
            distance_multiplier = 10.0 ** (attenuation_db / (10.0 * n_factors_arr))
            predicted_distances = predicted_distances * distance_multiplier

        # 3. Residuen-Berechnung mit den tomographisch korrigierten Distanzen
        residuals = predicted_distances - distances[None, :]
        normalized_residuals = residuals / sigmas[None, :]

        # Robuste Soft-L1-Log-Likelihood
        loss = 2.0 * (np.sqrt(1.0 + normalized_residuals**2) - 1.0)
        log_likelihood = -0.5 * np.sum(loss, axis=1)
        # Mittlere Likelihood (gewichtet mit den bisherigen Gewichten) für AMCL
        max_ll = float(np.max(log_likelihood))
        likelihood = np.exp(log_likelihood - max_ll)
        weighted = self.weights * likelihood
        weighted_sum = float(np.sum(weighted))
        if weighted_sum > 0.0 and np.isfinite(weighted_sum):
            # mittlere Log-Likelihood je Messung (unabhängig von der Anzahl Sensoren)
            log_avg = (max_ll + np.log(weighted_sum)) / len(anchors)
            if self.log_w_slow is None:
                self.log_w_slow = self.log_w_fast = log_avg
            else:
                self.log_w_slow += self.alpha_slow * (log_avg - self.log_w_slow)
                self.log_w_fast += self.alpha_fast * (log_avg - self.log_w_fast)

        # Bayes: neue Gewichte = alte Gewichte × Likelihood
        if not np.isfinite(weighted_sum) or weighted_sum <= 0.0:
            self.weights.fill(1.0 / self.particle_count)
        else:
            self.weights = weighted / weighted_sum

        self.effective_sample_size = float(1.0 / np.sum(np.square(self.weights)))

        # Wenn die effektive Sample-Größe extrem einbricht, sind die Partikel 
        # am falschen Ort gestrandet. Wir injizieren 20% neues Chaos.
        if self.effective_sample_size < self.particle_count * 0.1 and prior_position is not None:
            replace_count = int(self.particle_count * 0.2)
            idx_to_replace = self.rng.choice(self.particle_count, replace_count, replace=False)

            # 20% der Partikel um den neuen Sensor-Seed (prior_position) verstreuen
            random_scatter = self.rng.normal(0, 150.0, (replace_count, 2))
            self.particles[idx_to_replace, :2] = np.asarray(prior_position, dtype=float).reshape(1, 2) + random_scatter
            self.particles[idx_to_replace, 2:] = 0.0
            self._clip_particles()
            
            # Gewichte für diese injizierten Partikel zurücksetzen
            self.weights[idx_to_replace] = 1.0 / self.particle_count
            
            # Sample Size neu berechnen, da wir die Gewichte manipuliert haben
            likelihood_sum = float(np.sum(self.weights))
            if likelihood_sum > 0:
                self.weights /= likelihood_sum
                self.effective_sample_size = float(1.0 / np.sum(np.square(self.weights)))
        # ------------------------------------------

        # Schätzung VOR dem Resampling (sonst verzerren ungewichtete
        # Rettungspartikel den Mittelwert in Richtung der Sensoren).
        self.estimate = np.average(self.particles[:, :2], axis=0, weights=self.weights)
        centered = self.particles[:, :2] - self.estimate
        self.covariance = centered.T @ (centered * self.weights[:, None])

        inject_ratio = 0.0
        if self.log_w_slow is not None:
            inject_ratio = max(0.0, 1.0 - float(np.exp(min(self.log_w_fast - self.log_w_slow, 0.0))))
            inject_ratio = min(inject_ratio, self.recovery_ratio)
        if self.effective_sample_size < self.particle_count * 0.35 or inject_ratio > 0.02:
            self._systematic_resample(anchors, distances, sigmas, inject_ratio=inject_ratio)
            if inject_ratio > 0.02:
                self.log_w_fast = self.log_w_slow

        estimate_residuals = (np.linalg.norm(self.estimate[None, :] - anchors, axis=1) - distances)
        residual_rms = float(np.sqrt(np.mean(np.square(estimate_residuals))))
        spread = float(np.sqrt(max(np.trace(self.covariance), 0.0)))

        self.last_accuracy_cm = max(self.minimum_sigma_cm, spread, 2.0 * residual_rms)
        return self.estimate.copy()