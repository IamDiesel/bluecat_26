import numpy as np
from scipy.optimize import least_squares


class LocatorEngine:
    """Positionsschätzung aus RSSI-abgeleiteten Distanzen.

    Bei nur einem oder zwei aktiven Sensoren ist die Position mathematisch
    nicht eindeutig. In diesen Fällen wird eine schwache Priorinformation
    verwendet:

    * die bisherige Position, falls vorhanden
    * ansonsten die Richtung vom Netzwerk-Schwerpunkt zum aktiven
      Sensor bzw. zum Mittelpunkt des aktiven Sensorpaares
    * inaktive Sensoren werden nur als schwache negative Information genutzt

    Ein fehlender MQTT-Wert ist dabei ausdrücklich kein harter Beweis dafür,
    dass sich das Objekt außerhalb der Reichweite befindet.
    """

    def __init__(self, all_sensor_positions, negative_info_weight=0.15):
        positions = np.asarray(all_sensor_positions, dtype=float)
        if positions.size == 0:
            positions = np.empty((0, 2), dtype=float)
        positions = positions.reshape((-1, 2))

        self.all_sensors = positions
        self.centroid = (
            np.mean(positions, axis=0)
            if len(positions)
            else np.zeros(2, dtype=float)
        )
        self.negative_info_weight = float(negative_info_weight)
        self.last_accuracy_cm = None
        self.last_residual_rms_cm = None

    @staticmethod
    def _unit_vector(vector, fallback=(1.0, 0.0)):
        vector = np.asarray(vector, dtype=float)
        norm = np.linalg.norm(vector)
        if not np.isfinite(norm) or norm < 1e-9:
            return np.asarray(fallback, dtype=float)
        return vector / norm

    def calculate_position(
        self,
        active_anchors,
        distances,
        inactive_anchors=None,
        distance_stds=None,
        previous_position=None,
    ):
        """Berechnet eine Position und aktualisiert ``last_accuracy_cm``.

        ``distance_stds`` enthält die geschätzte Standardabweichung der
        einzelnen Distanzen in cm. Sie wird für eine gewichtete, robuste
        Least-Squares-Trilateration verwendet.
        """
        anchors = np.asarray(active_anchors, dtype=float).reshape((-1, 2))
        distances = np.asarray(distances, dtype=float).reshape(-1)
        inactive = np.asarray(
            inactive_anchors if inactive_anchors is not None else [],
            dtype=float,
        )
        if inactive.size == 0:
            inactive = np.empty((0, 2), dtype=float)
        else:
            inactive = inactive.reshape((-1, 2))

        if len(anchors) == 0 or len(distances) != len(anchors):
            self.last_accuracy_cm = None
            self.last_residual_rms_cm = None
            return None

        if np.any(~np.isfinite(anchors)):
            self.last_accuracy_cm = None
            self.last_residual_rms_cm = None
            return None

        if np.any(~np.isfinite(distances)) or np.any(distances < 0):
            self.last_accuracy_cm = None
            self.last_residual_rms_cm = None
            return None

        if distance_stds is not None:
            distance_stds = np.asarray(distance_stds, dtype=float).reshape(-1)
            if len(distance_stds) != len(distances):
                distance_stds = None
            else:
                if np.any(~np.isfinite(distance_stds)) or np.any(
                    distance_stds <= 0
                ):
                    distance_stds = None
                else:
                    distance_stds = np.maximum(distance_stds, 1.0)

        if previous_position is not None:
            previous_position = np.asarray(previous_position, dtype=float).reshape(2)
            if np.any(~np.isfinite(previous_position)):
                previous_position = None

        n = len(anchors)
        if n == 1:
            return self._calculate_1_sensor(
                anchors[0], distances[0], previous_position
            )
        if n == 2:
            return self._calculate_2_sensors(
                anchors,
                distances,
                inactive,
                distance_stds,
                previous_position,
            )

        return self._calculate_n_sensors(
            anchors, distances, distance_stds, previous_position
        )

    def _calculate_1_sensor(self, anchor, distance, previous_position):
        # Die bisherige Position ist die beste verfügbare Information über
        # die Seite des Kreises. Ohne Historie wird der Schwerpunkt-Prior
        # verwendet.
        if previous_position is not None:
            direction = self._unit_vector(previous_position - anchor)
        else:
            direction = self._unit_vector(anchor - self.centroid)

        self.last_residual_rms_cm = 0.0
        self.last_accuracy_cm = max(100.0, float(distance) * 0.50)
        return anchor + direction * distance

    def _calculate_2_sensors(
        self,
        anchors,
        distances,
        inactive_anchors,
        distance_stds,
        previous_position,
    ):
        p1, p2 = anchors
        r1, r2 = distances
        d = np.linalg.norm(p2 - p1)

        # Keine oder unbrauchbare Geometrie: gewichtete Least Squares.
        if d < 1e-9:
            return self._calculate_n_sensors(
                anchors, distances, distance_stds, previous_position
            )

        # Kreisschnittpunkte.
        if d > r1 + r2 or d < abs(r1 - r2):
            return self._calculate_n_sensors(
                anchors, distances, distance_stds, previous_position
            )

        a = (r1**2 - r2**2 + d**2) / (2.0 * d)
        h_squared = max(r1**2 - a**2, 0.0)
        h = np.sqrt(h_squared)

        p3 = p1 + a / d * (p2 - p1)
        perpendicular = np.array([-(p2 - p1)[1], (p2 - p1)[0]]) / d
        candidates = [p3 + h * perpendicular, p3 - h * perpendicular]

        reference = np.mean(anchors, axis=0)
        prior_direction = self._unit_vector(reference - self.centroid)
        scale = max(float(np.mean(distances)), 1.0)

        def candidate_cost(candidate):
            cost = 0.0
            direction = self._unit_vector(candidate - reference)

            if previous_position is not None:
                # Kontinuität ist stärker als die reine Schwerpunkt-Heuristik.
                cost += np.linalg.norm(candidate - previous_position) / scale
                cost += 0.25 * (1.0 - np.dot(direction, prior_direction))
            else:
                # Ohne Historie wird die gewünschte Richtung bevorzugt.
                cost += 1.0 - np.dot(direction, prior_direction)

            # Negative Information bleibt bewusst schwach: Ein stiller
            # Sensor kann auch Paketverlust oder Abschattung bedeuten.
            if len(inactive_anchors):
                inactive_distances = [
                    np.linalg.norm(candidate - anchor)
                    for anchor in inactive_anchors
                ]
                negative_score = np.mean(
                    [min(distance / scale, 3.0) for distance in inactive_distances]
                )
                cost -= self.negative_info_weight * negative_score

            return cost

        result = min(candidates, key=candidate_cost)
        self.last_residual_rms_cm = 0.0
        sigma = np.mean(distance_stds) if distance_stds is not None else 0.0
        self.last_accuracy_cm = max(50.0, float(np.mean(distances)) * 0.25, sigma)
        return result

    def _calculate_n_sensors(
        self, anchors, distances, distance_stds=None, previous_position=None
    ):
        """Robuste, gewichtete Trilateration für mindestens zwei Sensoren."""
        if distance_stds is None:
            distance_stds = np.ones(len(distances), dtype=float)
        else:
            distance_stds = np.maximum(np.asarray(distance_stds, dtype=float), 1.0)

        if previous_position is not None:
            initial_guess = previous_position
        else:
            initial_guess = np.average(
                anchors, axis=0, weights=1.0 / (distance_stds**2)
            )

        def error_function(guess):
            geometric_errors = np.linalg.norm(guess - anchors, axis=1) - distances
            return geometric_errors / distance_stds

        result = least_squares(
            error_function,
            initial_guess,
            loss="soft_l1",
            f_scale=1.0,
            max_nfev=200,
        )

        if not result.success or np.any(~np.isfinite(result.x)):
            self.last_residual_rms_cm = None
            self.last_accuracy_cm = max(
                100.0, float(np.mean(distances))
            )
            if previous_position is not None:
                return previous_position
            return np.asarray(initial_guess, dtype=float)

        position = np.asarray(result.x, dtype=float)
        raw_errors = np.linalg.norm(position - anchors, axis=1) - distances
        residual_rms = float(np.sqrt(np.mean(raw_errors**2)))
        self.last_residual_rms_cm = residual_rms

        # Grobe, aber realistischere Genauigkeitsschätzung. Die Geometrie
        # wird über die kleinste Singularzahl der Richtungsvektoren bewertet.
        directions = np.array(
            [
                self._unit_vector(position - anchor, fallback=(1.0, 0.0))
                for anchor in anchors
            ]
        )
        centered = directions - np.mean(directions, axis=0)
        singular_values = np.linalg.svd(centered, compute_uv=False)
        geometry_factor = 3.0
        if len(singular_values) and singular_values[-1] > 0.25:
            geometry_factor = 1.5

        self.last_accuracy_cm = max(
            float(np.median(distance_stds)) * geometry_factor,
            residual_rms * geometry_factor,
            25.0,
        )
        return position
