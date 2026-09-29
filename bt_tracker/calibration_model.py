"""Gepoolte, robuste Kalibrierung des Pegelmodells.

Statt je Sensor eine Gerade durch 2–4 Punkte zu legen, werden alle
Messpunkte aller Sensoren gemeinsam gefittet:

    m_ij = P0_i − 10·n·log10(d_ij) − s·A_ij + e_ij

* ``m_ij``  robuster Mittelwert der Rohwerte von Sensor i am Messpunkt j
* ``P0_i``  ``tx_power`` je Sensor (Empfänger-Exemplarstreuung, Einbauort)
* ``n``     gemeinsamer Pfadverlust-Exponent (optional je Sensor)
* ``A_ij``  Wanddämpfung laut Grundriss zwischen Punkt und Sensor
* ``s``     Skalierung der Grundriss-Dämpfungen (wird mitgeschätzt)

Die robuste Streuung der Residuen ist das realistische ``sigma_db``
(Shadowing), das das neue Modell je Sensor verwendet. ``r_min``/``r_max``
bleiben die Varianz von Einzelwerten (schnelles Fading) nah bzw. fern.
"""

from __future__ import annotations

from typing import Dict, Iterable, Optional, Sequence, Tuple

import numpy as np
from scipy.optimize import least_squares


def robust_point_statistics(samples: Sequence):
    """Robuster Mittelwert, Rohwert-Varianz und Anzahl.

    ``samples`` sind Rohwerte oder Paare ``(median, anzahl)`` aus Firmware-
    Fenstern. Bei Paaren wird die Varianz der Mediane auf die Varianz eines
    Einzelwerts hochgerechnet (Var(Median aus k) ≈ 1.57·σ²/k).
    """
    arr = np.asarray(samples, dtype=float)
    if arr.ndim == 2 and arr.shape[1] == 2:
        values = arr[:, 0]
        k = float(np.mean(np.clip(arr[:, 1], 1, None)))
        mean, var_medians, n = robust_point_statistics(values)
        factor = k / 1.57 if k > 1 else 1.0
        return mean, var_medians * factor, n
    values = arr
    median = float(np.median(values))
    mad = float(np.median(np.abs(values - median)))
    threshold = max(3.0 * 1.4826 * mad, 2.0)
    inliers = values[np.abs(values - median) <= threshold]
    if len(inliers) < 3:
        inliers = values
    variance = float(np.var(inliers, ddof=1)) if len(inliers) > 1 else 0.0
    return float(np.mean(inliers)), variance, len(inliers)


def fit_pooled(
    samples_by_sensor: Dict[str, Iterable[Tuple[Sequence[float], Sequence[float]]]],
    sensor_positions: Dict[str, Sequence[float]],
    floorplan=None,
    per_sensor_n: bool = False,
    min_samples: int = 3,
    sensor_heights: Optional[Dict[str, float]] = None,
    point_height_cm: float = 0.0,
):
    """Fittet das Modell. Liefert (configs je Sensor, globale Werte).

    ``sensor_heights``: Antennenhöhe je Sensor (cm, relativ zum Fußboden), ``point_height_cm``:
    Höhe des Halsbands an den Messpunkten. Ein Messpunkt (x, y, z) bringt seine eigene Höhe z mit.
    Ohne Angaben wird eben (2D) gerechnet."""
    heights = sensor_heights or {}
    rows = []  # (sensor_index, dist_m, wall_db, mean, variance, count)
    names = [n for n in samples_by_sensor if n in sensor_positions]
    index = {n: i for i, n in enumerate(names)}
    for name in names:
        pos = np.asarray(sensor_positions[name], dtype=float)
        for point, samples in samples_by_sensor[name]:
            if samples is None or len(samples) < min_samples:
                continue
            pz = float(point[2]) if len(point) > 2 else float(point_height_cm)
            point = np.asarray(point[:2], dtype=float)
            dz = float(heights.get(name, pz)) - pz
            dist_m = max(float(np.hypot(np.linalg.norm(point - pos), dz)) / 100.0, 0.3)
            walls = 0.0
            if floorplan is not None and getattr(floorplan, "has_walls", False):
                walls = float(floorplan.attenuation_db(point.reshape(1, 2), pos)[0])
            mean, var, count = robust_point_statistics(samples)
            rows.append((index[name], dist_m, walls, mean, var, count))
    if not rows:
        raise ValueError("Keine verwertbaren Messpunkte.")
    rows_arr = np.asarray(rows, dtype=float)
    s_idx = rows_arr[:, 0].astype(int)
    logd = 10.0 * np.log10(rows_arr[:, 1])
    walls = rows_arr[:, 2]
    means = rows_arr[:, 3]
    k = len(names)
    fit_walls = bool(np.any(walls > 0))
    n_count = k if per_sensor_n else 1

    def unpack(theta):
        p0 = theta[:k]
        n = theta[k:k + n_count]
        s = theta[k + n_count] if fit_walls else 0.0
        return p0, n, s

    def residuals(theta):
        p0, n, s = unpack(theta)
        n_row = n[s_idx] if per_sensor_n else n[0]
        return means - (p0[s_idx] - n_row * logd - s * walls)

    theta0 = np.concatenate([
        np.full(k, float(np.median(means))),
        np.full(n_count, 2.5),
        [1.0] if fit_walls else [],
    ])
    lower = np.concatenate([np.full(k, -110.0), np.full(n_count, 1.0), [0.0] if fit_walls else []])
    upper = np.concatenate([np.full(k, -20.0), np.full(n_count, 5.0), [3.0] if fit_walls else []])
    result = least_squares(residuals, theta0, loss="soft_l1", f_scale=3.0, bounds=(lower, upper))
    p0, n, s = unpack(result.x)
    res = residuals(result.x)
    global_sigma = max(1.4826 * float(np.median(np.abs(res - np.median(res)))), 2.0)

    configs = {}
    for name, i in index.items():
        mask = s_idx == i
        if not mask.any():
            continue
        sensor_res = res[mask]
        # Schrumpfen der Einzelschätzung zur globalen Streuung (wenige Punkte)
        m = int(mask.sum())
        local = float(np.sqrt(np.mean(sensor_res ** 2))) if m else global_sigma
        sigma = float(np.sqrt((m * local ** 2 + 4 * global_sigma ** 2) / (m + 4)))
        dists = rows_arr[mask, 1]
        variances = rows_arr[mask, 4]
        near = variances[dists <= 2.0]
        far = variances[dists >= 4.0]
        overall = float(np.median(variances))
        configs[name] = {
            "tx_power": round(float(p0[i]), 2),
            "n_factor": round(float(n[i] if per_sensor_n else n[0]), 3),
            "sigma_db": round(max(sigma, 2.0), 2),
            "r_min": round(float(np.median(near)) if len(near) else overall, 2),
            "r_max": round(float(np.median(far)) if len(far) else overall, 2),
            "calibration_points": m,
            "calibration_rms_db": round(local, 2),
        }
    globals_ = {
        "n_factor": round(float(n[0]), 3) if not per_sensor_n else None,
        "wall_scale": round(float(s), 3) if fit_walls else None,
        "sigma_db": round(global_sigma, 2),
        "points": int(len(rows)),
    }
    return configs, globals_
