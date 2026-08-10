import numpy as np

class RadioGridMap:
    """Voxel-basiertes Tomographie-Modell zur Kartierung von Funkwiderständen."""

    def __init__(self, sensor_positions, cell_size_cm=50.0, margin_cm=200.0):
        self.cell_size = float(cell_size_cm)
        positions = np.asarray(list(sensor_positions.values()), dtype=float)
        
        if positions.size > 0:
            self.min_bounds = np.min(positions, axis=0) - margin_cm
            self.max_bounds = np.max(positions, axis=0) + margin_cm
        else:
            self.min_bounds = np.array([0.0, 0.0])
            self.max_bounds = np.array([1000.0, 1000.0])

        # Raster-Dimensionen berechnen
        width = int(np.ceil((self.max_bounds[0] - self.min_bounds[0]) / self.cell_size))
        height = int(np.ceil((self.max_bounds[1] - self.min_bounds[1]) / self.cell_size))
        
        # Grid speichert Dämpfung in dB pro Meter (dB/m)
        self.grid = np.zeros((width, height), dtype=float)
        
        # Integrations-Auflösung für schnelles Sampling (z.B. 10 Punkte pro Strahl)
        self.ray_steps = 10
        self._t_vector = np.linspace(0, 1, self.ray_steps)

    def _get_indices(self, points):
        """Übersetzt reale X/Y Koordinaten sicher in Grid-Indizes."""
        idx = ((points - self.min_bounds) / self.cell_size).astype(int)
        idx[..., 0] = np.clip(idx[..., 0], 0, self.grid.shape[0] - 1)
        idx[..., 1] = np.clip(idx[..., 1], 0, self.grid.shape[1] - 1)
        return idx[..., 0], idx[..., 1]

    def update_static_mesh(self, radio_environment, sensors_config, iterations=15, relaxation=0.2):
        """SIRT-Algorithmus: Findet Wände anhand der stationären Mesh-Messungen."""
        baselines = radio_environment._baselines
        if not baselines:
            return

        for _ in range(iterations):
            for key, baseline in baselines.items():
                receiver, transmitter = key
                if receiver not in sensors_config or transmitter not in sensors_config:
                    continue
                
                pos_r = np.asarray(sensors_config[receiver]["pos"])
                pos_t = np.asarray(sensors_config[transmitter]["pos"])
                dist_m = max(baseline.distance_cm / 100.0, 0.1)
                
                # Erwarteter RSSI im leeren Raum
                tx_power = sensors_config[transmitter].get("tx_power", -59.0)
                n_factor = sensors_config[receiver].get("n_factor", 3.0)
                ideal_rssi = tx_power - 10.0 * n_factor * np.log10(dist_m)
                
                # Gemessene Gesamtdämpfung (darf nicht negativ sein)
                target_attenuation = max(ideal_rssi - baseline.baseline_rssi, 0.0)
                
                # Punkte entlang des Strahls
                ray_points = pos_t + self._t_vector[:, None] * (pos_r - pos_t)
                idx_x, idx_y = self._get_indices(ray_points)
                
                # Aktuelle Dämpfung auf dem Strahl berechnen
                current_attenuation = np.mean(self.grid[idx_x, idx_y]) * dist_m
                error = target_attenuation - current_attenuation
                
                # Zelle updaten (Fehler gleichmäßig auf alle Zellen im Strahl verteilen)
                correction = (error * relaxation) / (dist_m + 1e-6)
                self.grid[idx_x, idx_y] += correction
                self.grid = np.clip(self.grid, 0.0, 50.0) # Physikalisches Limit: Max 50 dB/m

    def update_dynamic(self, active_anchors, anchor_names, target_pos, sensors_config, alpha=0.05):
        """SLAM: Aktualisiert das Grid anhand des wandernden Lolas (Radar-Strahl)."""
        target_pos = np.asarray(target_pos)
        
        for pos, name in zip(active_anchors, anchor_names):
            dist_cm = max(np.linalg.norm(target_pos - pos), 10.0)
            dist_m = dist_cm / 100.0
            
            tx_power = sensors_config[name].get("tx_power", -59.0)
            n_factor = sensors_config[name].get("n_factor", 3.0)
            ideal_rssi = tx_power - 10.0 * n_factor * np.log10(dist_m)
            
            # Tatsächlich gefilterter RSSI
            filtered_rssi = sensors_config[name].get("filtered_rssi")
            if filtered_rssi is None:
                continue
                
            measured_attenuation = max(ideal_rssi - filtered_rssi, 0.0)
            
            # Zellen auf dem Strahl aktualisieren
            ray_points = pos + self._t_vector[:, None] * (target_pos - pos)
            idx_x, idx_y = self._get_indices(ray_points)
            
            # Exponentieller gleitender Mittelwert (EMA)
            measured_db_per_m = measured_attenuation / dist_m
            self.grid[idx_x, idx_y] = (1 - alpha) * self.grid[idx_x, idx_y] + alpha * measured_db_per_m

    def get_expected_attenuation_vectorized(self, anchors, particles):
        """Berechnet massiv parallel die erwartete Dämpfung für alle Partikel."""
        # anchors: (S, 2), particles: (P, 2)
        S = anchors.shape[0]
        P = particles.shape[0]
        
        # Vektorisierte Streckeninterpolation: Shape (P, S, steps, 2)
        t_vec = self._t_vector[None, None, :, None]
        ray_points = anchors[None, :, None, :] + t_vec * (particles[:, None, None, :] - anchors[None, :, None, :])
        
        idx_x, idx_y = self._get_indices(ray_points)
        
        # Grid abfragen. Shape: (P, S, steps)
        sampled_db_per_m = self.grid[idx_x, idx_y]
        
        # Durchschnittliche Dämpfung pro Strahl (Shape: P, S)
        mean_db_per_m = np.mean(sampled_db_per_m, axis=2)
        
        # Mit realer Distanz multiplizieren
        dists_m = np.linalg.norm(particles[:, None, :] - anchors[None, :, :], axis=2) / 100.0
        
        return mean_db_per_m * dists_m

    def export_heatmap(self, filepath="config/radio_heatmap.json"):
        """Exportiert das Grid und die Raumgrenzen für die Visualisierung."""
        import json
        import os
        
        export_data = {
            "cell_size_cm": self.cell_size,
            "min_bounds_x": float(self.min_bounds[0]),
            "min_bounds_y": float(self.min_bounds[1]),
            "grid_data": self.grid.tolist()
        }
        
        os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
        temp_path = f"{filepath}.tmp"
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(export_data, f, indent=2)
            os.replace(temp_path, filepath)
        except Exception as e:
            print(f"Fehler beim Speichern der Heatmap: {e}")