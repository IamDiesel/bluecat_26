"""Funk-Tomographie aus dem Sensor-Mesh: statische und dynamische Dämpfungskarte
sowie Selbstkalibrierung des Ausbreitungsmodells.

Messgrößen: Für jede gerichtete Strecke Empfänger r ← Sender t kennt das
``RadioEnvironmentModel`` einen gelernten Normalwert B_rt (Baseline) und die
aktuelle Abweichung davon.

**Dynamische Karte** (Personen, Türen, Möbel verschoben): Abweichung vom
Normalwert, bereinigt um Sender-/Empfängerdrift. Rekonstruktion per
regularisierter Radio-Tomographie (RTI) statt einfacher Mittelung, danach
zeitliche Glättung mit Abklingzeit ``tau`` – eine Störung verschwindet nicht
mit dem nächsten Messwert.

**Statische Karte** (Wände, Einbauten): Normalwerte gegen ein
Ausbreitungsmodell ohne Hindernisse

    B_rt = A_t + G_r − 10·n0·log10(d_rt) + ε ,  n0 ≈ 2 (Freiraum)

Was die Strecke mehr verliert als vorhergesagt, stammt von Hindernissen und
wird per RTI auf das Raster verteilt (dB/m).

**Selbstkalibrierung** (``fit_mesh_model``): dasselbe Modell mit Wänden aus
dem Grundriss und frei geschätztem Exponenten

    B_rt = A_t + G_r − 10·n·log10(d_rt) − Σ_w c_rtw·L_w + ε

liefert je Empfänger den relativen Gewinn G_r (→ ``tx_power`` für das
Halsband bis auf eine gemeinsame Konstante), den Pfadverlust-Exponenten n und
die Dämpfung L_w jeder gezeichneten Wand. Schwach bestimmte Größen bleiben
über Gauß-Priors nahe an plausiblen Startwerten.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

try:  # scipy ist für die Kalibrierung ohnehin Pflicht
    from scipy.optimize import lsq_linear
except ImportError:  # pragma: no cover
    lsq_linear = None


# ---------------------------------------------------------------------------
# Geometrie
# ---------------------------------------------------------------------------
def segments_cross(p: np.ndarray, q: np.ndarray, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Bool-Matrix (N, W): Strecke p_i→q_i schneidet Wand a_w→b_w."""
    if len(a) == 0 or len(p) == 0:
        return np.zeros((len(p), len(a)), dtype=bool)
    p = p[:, None, :]
    q = q[:, None, :]
    a = a[None, :, :]
    b = b[None, :, :]

    def orient(u, v, w):
        return (v[..., 0] - u[..., 0]) * (w[..., 1] - u[..., 1]) - (v[..., 1] - u[..., 1]) * (w[..., 0] - u[..., 0])

    return (orient(a, b, p) * orient(a, b, q) < 0) & (orient(p, q, a) * orient(p, q, b) < 0)


def wall_arrays(floorplan_data: Optional[dict]):
    walls = (floorplan_data or {}).get("walls") or []
    scale = float((floorplan_data or {}).get("wall_scale", 1.0) or 1.0)
    default = float((floorplan_data or {}).get("default_wall_db", 5.0) or 5.0)
    a = np.asarray([w["a"] for w in walls], dtype=float).reshape(-1, 2)
    b = np.asarray([w["b"] for w in walls], dtype=float).reshape(-1, 2)
    prior = np.asarray([float(w.get("attenuation_db", default)) * scale for w in walls], dtype=float)
    return a, b, prior


class Grid:
    """Rasterzellen über den Sensorpositionen (lokale cm-Koordinaten)."""

    def __init__(self, positions: Dict[str, Sequence[float]], cell_cm=50.0, margin_cm=150.0, max_cells=900):
        pts = np.asarray(list(positions.values()), dtype=float).reshape(-1, 2)
        self.lower = pts.min(axis=0) - margin_cm
        upper = pts.max(axis=0) + margin_cm
        span = upper - self.lower
        cell = float(cell_cm)
        while math.ceil(span[0] / cell) * math.ceil(span[1] / cell) > max_cells:
            cell *= 1.25
        self.cell = cell
        self.nx = max(int(math.ceil(span[0] / cell)), 1)
        self.ny = max(int(math.ceil(span[1] / cell)), 1)
        xs = self.lower[0] + (np.arange(self.nx) + 0.5) * cell
        ys = self.lower[1] + (np.arange(self.ny) + 0.5) * cell
        cx, cy = np.meshgrid(xs, ys)  # (ny, nx)
        self.centers = np.stack([cx.ravel(), cy.ravel()], axis=1)  # (C, 2), Zeile j*nx+i
        self.key = (round(float(self.lower[0]), 1), round(float(self.lower[1]), 1), self.nx, self.ny,
                    round(self.cell, 2))

    @property
    def size(self):
        return self.nx * self.ny

    def weights(self, segments: List[Tuple[np.ndarray, np.ndarray]], excess_cm=60.0) -> np.ndarray:
        """W (L, C): Zeile = Strecke, Summe über die Zellen = Länge in m.

        Mit dieser Normierung ist die Lösung x eine Dämpfung in dB/m und
        W·x die Gesamtdämpfung der Strecke in dB (wie im Legacy-Grid).
        """
        W = np.zeros((len(segments), self.size))
        for k, (a, b) in enumerate(segments):
            d = float(np.hypot(*(a - b)))
            if d < 1.0:
                continue
            path = (np.hypot(self.centers[:, 0] - a[0], self.centers[:, 1] - a[1])
                    + np.hypot(self.centers[:, 0] - b[0], self.centers[:, 1] - b[1]))
            inside = path < d + max(excess_cm, 0.6 * self.cell)
            count = int(inside.sum())
            if count:
                W[k, inside] = (d / 100.0) / count
        return W

    def smoothness(self) -> np.ndarray:
        """Differenzenoperator D (Nachbarzellen), für ||D·x||² (zwischengespeichert)."""
        if getattr(self, "_D", None) is not None:
            return self._D
        rows = []
        for j in range(self.ny):
            for i in range(self.nx):
                c = j * self.nx + i
                if i + 1 < self.nx:
                    rows.append((c, c + 1))
                if j + 1 < self.ny:
                    rows.append((c, c + self.nx))
        D = np.zeros((len(rows), self.size))
        for r, (c1, c2) in enumerate(rows):
            D[r, c1] = 1.0
            D[r, c2] = -1.0
        self._D = D
        return D

    def to_rows(self, x: np.ndarray, threshold=0.0) -> List[List[Optional[float]]]:
        grid = np.asarray(x, dtype=float).reshape(self.ny, self.nx)
        return [[round(float(v), 2) if v > threshold else None for v in row] for row in grid]


def solve_rti(W: np.ndarray, y: np.ndarray, grid: Grid, weight: Optional[np.ndarray] = None,
              ridge=0.02, smooth=0.15, upper=20.0) -> np.ndarray:
    """min ||diag(w)(W·x − y)||² + ridge·||x||² + smooth·||D·x||²,  0 ≤ x ≤ upper  (x in dB/m)."""
    if len(y) == 0:
        return np.zeros(grid.size)
    w = np.ones(len(y)) if weight is None else np.asarray(weight, dtype=float)
    D = grid.smoothness()
    A = np.vstack([W * w[:, None], math.sqrt(ridge) * np.eye(grid.size), math.sqrt(smooth) * D])
    rhs = np.concatenate([np.asarray(y, dtype=float) * w, np.zeros(grid.size), np.zeros(len(D))])
    if lsq_linear is not None:
        res = lsq_linear(A, rhs, bounds=(0.0, upper), lsmr_tol="auto", max_iter=200)
        return np.clip(res.x, 0.0, upper)
    x = np.linalg.lstsq(A, rhs, rcond=None)[0]  # Notlösung ohne scipy
    return np.clip(x, 0.0, upper)


# ---------------------------------------------------------------------------
# Selbstkalibrierung aus den Mesh-Baselines
# ---------------------------------------------------------------------------
def fit_mesh_model(links: List[Tuple[str, str, float, float]], positions: Dict[str, Sequence[float]],
                   floorplan_data: Optional[dict] = None, n_prior=(2.2, 0.6), wall_sigma_db=4.0,
                   gain_sigma_db=6.0, tx_sigma_db=6.0, fixed_n: Optional[float] = None, use_walls=True,
                   iterations=6, heights: Optional[Dict[str, float]] = None) -> dict:
    """Robuste lineare Schätzung (IRLS, Huber) des Mesh-Ausbreitungsmodells.

    ``links``: [(Empfänger, Sender, Baseline-RSSI, Varianz)].
    Rückgabe: Gewinne je Empfänger, Sendeterme, n, Wanddämpfungen (mit
    Unsicherheit und Anzahl kreuzender Strecken), Residuen je Strecke.
    """
    links = [lk for lk in links if lk[0] in positions and lk[1] in positions and lk[0] != lk[1]]
    if len(links) < 3:
        raise ValueError("Zu wenige gelernte Funkstrecken (mindestens 3).")
    receivers = sorted({lk[0] for lk in links})
    transmitters = sorted({lk[1] for lk in links})
    ri = {n: i for i, n in enumerate(receivers)}
    ti = {n: i for i, n in enumerate(transmitters)}
    pos = {k: np.asarray(v, dtype=float) for k, v in positions.items()}
    p = np.asarray([pos[lk[0]] for lk in links])
    q = np.asarray([pos[lk[1]] for lk in links])
    hz = heights or {}
    dz = np.asarray([float(hz.get(lk[0], 0.0)) - float(hz.get(lk[1], 0.0)) for lk in links])
    dist_m = np.maximum(np.sqrt(np.sum((p - q) ** 2, axis=1) + dz * dz) / 100.0, 0.3)  # Antennenabstand
    logd = 10.0 * np.log10(dist_m)
    y = np.asarray([lk[2] for lk in links], dtype=float)
    var = np.asarray([max(float(lk[3]), 1.0) for lk in links])

    a_w, b_w, prior_w = wall_arrays(floorplan_data if use_walls else None)
    crossing = segments_cross(p, q, a_w, b_w).astype(float)  # (L, Wn)
    n_walls = crossing.shape[1]

    nr, nt = len(receivers), len(transmitters)
    fit_n = fixed_n is None
    cols = nr + nt + (1 if fit_n else 0) + n_walls
    X = np.zeros((len(links), cols))
    for k, (r, t, _, _) in enumerate(links):
        X[k, ri[r]] = 1.0
        X[k, nr + ti[t]] = 1.0
    c = nr + nt
    target = y.copy()
    if fit_n:
        X[:, c] = -logd
        c += 1
    else:
        target = target + fixed_n * logd
    if n_walls:
        X[:, c:c + n_walls] = -crossing

    # Priors als Zusatzzeilen: Σ G = 0 (Eichung), G ~ N(0, σG), A ~ N(median, 20), n, L_w
    prior_rows, prior_rhs, prior_sd = [], [], []

    def add(col_vec, value, sd):
        prior_rows.append(col_vec)
        prior_rhs.append(value)
        prior_sd.append(sd)

    gauge = np.zeros(cols)
    gauge[:nr] = 1.0
    add(gauge, 0.0, 0.05)
    for i in range(nr):
        v = np.zeros(cols)
        v[i] = 1.0
        add(v, 0.0, gain_sigma_db)
    base = float(np.median(y + (fixed_n if not fit_n else n_prior[0]) * logd))
    mean_tx = np.zeros(cols)
    mean_tx[nr:nr + nt] = 1.0 / nt
    add(mean_tx, base, 30.0)                  # Niveau der Sender: kaum eingeschränkt
    for i in range(nt):                       # Sender untereinander ähnlich (gleiche Hardwareklassen)
        v = -mean_tx.copy()
        v[nr + i] += 1.0
        add(v, 0.0, tx_sigma_db)
    if fit_n:
        v = np.zeros(cols)
        v[nr + nt] = 1.0
        add(v, n_prior[0], n_prior[1])
    for w in range(n_walls):
        v = np.zeros(cols)
        v[cols - n_walls + w] = 1.0
        add(v, float(prior_w[w]), wall_sigma_db)
    P = np.asarray(prior_rows)
    prhs = np.asarray(prior_rhs)
    psd = np.asarray(prior_sd)

    sigma_link = np.sqrt(var + 4.0)  # Messstreuung + Modellfehler
    weights = np.ones(len(links))
    theta = np.zeros(cols)
    for _ in range(iterations):
        wl = weights / sigma_link
        A = np.vstack([X * wl[:, None], P / psd[:, None]])
        rhs = np.concatenate([target * wl, prhs / psd])
        theta, *_ = np.linalg.lstsq(A, rhs, rcond=None)
        if n_walls:  # Wände dämpfen nie negativ
            theta[cols - n_walls:] = np.maximum(theta[cols - n_walls:], 0.0)
        res = target - X @ theta
        scale = max(1.4826 * float(np.median(np.abs(res))), 1.5)
        z = np.abs(res) / (1.345 * scale)
        weights = np.where(z <= 1.0, 1.0, 1.0 / np.maximum(z, 1e-9))

    # Unsicherheit (Laplace-Näherung)
    wl = weights / sigma_link
    A = np.vstack([X * wl[:, None], P / psd[:, None]])
    try:
        cov = np.linalg.inv(A.T @ A)
    except np.linalg.LinAlgError:
        cov = np.linalg.pinv(A.T @ A)
    sd = np.sqrt(np.maximum(np.diag(cov), 0.0))
    res = target - X @ theta
    robust_sigma = 1.4826 * float(np.median(np.abs(res - np.median(res))))

    out = {
        "gains_db": {r: round(float(theta[ri[r]]), 2) for r in receivers},
        "gains_sd_db": {r: round(float(sd[ri[r]]), 2) for r in receivers},
        "tx_terms_db": {t: round(float(theta[nr + ti[t]]), 2) for t in transmitters},
        "n": round(float(theta[nr + nt]) if fit_n else float(fixed_n), 3),
        "n_sd": round(float(sd[nr + nt]), 3) if fit_n else 0.0,
        "residual_sigma_db": round(robust_sigma, 2),
        "links": len(links),
        "receiver_links": {r: int(sum(1 for lk in links if lk[0] == r)) for r in receivers},
        "residuals": [(lk[0], lk[1], float(res[k])) for k, lk in enumerate(links)],
        "wall_loss_db": [float(crossing[k] @ theta[cols - n_walls:]) if n_walls else 0.0 for k in range(len(links))],
        "walls": [],
    }
    for w in range(n_walls):
        col = cols - n_walls + w
        out["walls"].append({
            "index": w,
            "prior_db": round(float(prior_w[w]), 2),
            "estimate_db": round(float(theta[col]), 2),
            "sd_db": round(float(sd[col]), 2),
            "links": int(crossing[:, w].sum()),
        })
    return out


# ---------------------------------------------------------------------------
# Laufende Karten
# ---------------------------------------------------------------------------
class RadioTomography:
    """Hält Raster, statische und geglättete dynamische Karte."""

    def __init__(self, cell_cm=50.0, margin_cm=150.0, tau_sec=120.0, static_n=2.0):
        self.cell_cm = cell_cm
        self.margin_cm = margin_cm
        self.tau = float(tau_sec)
        self.static_n = float(static_n)
        self.grid: Optional[Grid] = None
        self.dynamic = None
        self.dynamic_time = None
        self.static = None
        self.static_key = None
        self.last_autocal: Optional[dict] = None
        self.static_basis = None

    def _static_map(self, baselines, positions, floorplan_data, grid, pos, heights=None):
        """Zusatzdämpfung der Normalwerte gegenüber dem hindernisfreien Modell.

        Mit Grundriss: Modell inkl. Wände schätzen (Gewinne sauber bestimmt),
        dann Wand- plus Restdämpfung darstellen. Ohne Grundriss: Freiraummodell
        mit eng gekoppelten Sender-/Empfängertermen, damit Hindernisse nicht in
        den Gerätekonstanten verschwinden – nur grob, weil echte Unterschiede
        zwischen den Geräten dann als Dämpfung erscheinen können.
        """
        a_w, _, _ = wall_arrays(floorplan_data)
        if len(a_w):
            model = fit_mesh_model(baselines, positions, floorplan_data, heights=heights)
            excess = [(r, t, loss - res) for (r, t, res), loss in zip(model["residuals"], model["wall_loss_db"])]
            self.static_basis = "grundriss"
        else:
            model = fit_mesh_model(baselines, positions, n_prior=(self.static_n, 0.2), gain_sigma_db=1.0,
                                   tx_sigma_db=1.0, use_walls=False, heights=heights)
            excess = [(r, t, -res) for r, t, res in model["residuals"]]
            self.static_basis = "freiraum"
        W = grid.weights([(pos[r], pos[t]) for r, t, _ in excess])
        return solve_rti(W, np.asarray([e for _, _, e in excess]), grid, smooth=0.3)

    def _ensure_grid(self, positions):
        grid = Grid(positions, self.cell_cm, self.margin_cm)
        if self.grid is None or grid.key != self.grid.key:
            self.grid = grid
            self.dynamic = None
            self.static = None
            self.static_key = None
        return self.grid

    def update(self, env, now: float, floorplan_data: Optional[dict] = None) -> Optional[dict]:
        positions = dict(env.sensor_positions)
        if len(positions) < 2:
            return None
        grid = self._ensure_grid(positions)
        pos = {k: np.asarray(v, dtype=float) for k, v in positions.items()}

        # --- dynamisch: Abweichung vom Normalwert, ohne Drift -------------------
        residuals = env.link_residuals(now)
        dyn_links = [(r, t, dev, res) for r, t, dev, res in residuals if r in pos and t in pos]
        link_out = [{"rx": r, "tx": t, "dev_db": round(dev, 1), "residual_db": round(res, 1)}
                    for r, t, dev, res in dyn_links]
        if dyn_links:
            W = grid.weights([(pos[r], pos[t]) for r, t, _, _ in dyn_links])
            y = np.asarray([-res for _, _, _, res in dyn_links])  # positiv = zusätzliche Dämpfung
            x_now = solve_rti(W, y, grid)
        else:
            x_now = np.zeros(grid.size)
        if self.dynamic is None or self.dynamic_time is None:
            self.dynamic = x_now
        else:
            dt = max(now - self.dynamic_time, 0.0)
            alpha = 1.0 - math.exp(-dt / max(self.tau, 1e-6))
            # Anstieg sofort sichtbar, Abklingen langsam (Spitzenwert mit Gedächtnis)
            rising = x_now > self.dynamic
            self.dynamic = np.where(rising, self.dynamic + np.maximum(alpha, 0.5) * (x_now - self.dynamic),
                                    self.dynamic + alpha * (x_now - self.dynamic))
        self.dynamic_time = now

        # --- statisch: Normalwerte gegen Freiraum-Modell --------------------------
        baselines = env.baseline_links()
        walls_key = tuple((tuple(w["a"]), tuple(w["b"]), w.get("attenuation_db"))
                          for w in (floorplan_data or {}).get("walls") or [])
        heights = dict(getattr(env, "sensor_heights", {}) or {})
        key = (grid.key, tuple(sorted((r, t, round(b, 1)) for r, t, b, _ in baselines)), walls_key,
               (floorplan_data or {}).get("wall_scale"), tuple(sorted((k, round(v)) for k, v in heights.items())),
               # verschobene Sensoren: neu rechnen, auch wenn Raster und Normalwerte gleich bleiben
               tuple(sorted((k, round(float(v[0])), round(float(v[1]))) for k, v in pos.items())))
        if key != self.static_key:
            self.static_key = key
            self.static = None
            if len(baselines) >= 3:
                try:
                    self.static = self._static_map(baselines, positions, floorplan_data, grid, pos, heights)
                except (ValueError, np.linalg.LinAlgError):
                    self.static = None

        data = {
            "static_basis": self.static_basis,
            "cell_cm": round(grid.cell, 1),
            "origin_cm": [round(float(grid.lower[0]), 1), round(float(grid.lower[1]), 1)],
            "nx": grid.nx,
            "ny": grid.ny,
            "unit": "dB/m",
            "tau_sec": self.tau,
            "dynamic": grid.to_rows(self.dynamic, threshold=0.05),
            "static": grid.to_rows(self.static, threshold=0.05) if self.static is not None else None,
            "links": link_out,
            "baseline_suspect": bool(getattr(env, "baseline_suspect", False)),
            "links_learned": len(baselines),
        }
        data["values"] = data["dynamic"]  # Kompatibilität (Version 2.1)
        if self.last_autocal and self.last_autocal.get("walls"):
            data["walls"] = self.last_autocal["walls"]
        return data


def autocal_suggestions(baselines, positions, sensor_configs: Dict[str, dict],
                        floorplan_data: Optional[dict] = None, min_links=2,
                        heights: Optional[Dict[str, float]] = None) -> dict:
    """Vorschläge aus dem Mesh: tx_power je Sensor (relativ), n und Wanddämpfungen.

    Die absolute Sendeleistung des Halsbands lässt sich aus dem Mesh nicht
    bestimmen (die Sensoren senden anders als das Halsband). Das Gesamtniveau
    bleibt deshalb erhalten: Median der bisherigen ``tx_power − Gewinn``.
    """
    model = fit_mesh_model(baselines, positions, floorplan_data, heights=heights)
    usable = {sid: g for sid, g in model["gains_db"].items()
              if model["receiver_links"].get(sid, 0) >= min_links and sid in sensor_configs}
    if not usable:
        raise ValueError("Kein Sensor hört genug andere Sensoren (mindestens 2 Funkstrecken).")
    # Niveau an den mit dem Halsband kalibrierten Sensoren ausrichten (die kennen das echte Halsband)
    calibrated = [sid for sid in usable if sensor_configs[sid].get("calibration_status") == "calibrated"]
    basis = calibrated if len(calibrated) >= 2 else list(usable)
    levels = [float(sensor_configs[sid].get("tx_power", -59.0)) - usable[sid] for sid in basis]
    reference = float(np.median(levels))
    sensors = {}
    for sid, gain in usable.items():
        current = float(sensor_configs[sid].get("tx_power", -59.0))
        sensors[sid] = {
            "current_tx_power": round(current, 2),
            "suggested_tx_power": round(reference + gain, 2),
            "gain_db": gain,
            "sd_db": model["gains_sd_db"][sid],
            "links": model["receiver_links"][sid],
            "current_n_factor": float(sensor_configs[sid].get("n_factor", 3.0)),
            "calibration_status": sensor_configs[sid].get("calibration_status", "uncalibrated"),
            "delta_db": round(reference + gain - current, 2),
        }
    return {
        "n_factor": model["n"],
        "n_sd": model["n_sd"],
        "reference_level_db": round(reference, 2),
        "reference_basis": "kalibrierte Sensoren" if basis is calibrated else "alle Sensoren",
        "sensors": sensors,
        "walls": model["walls"],
        "residual_sigma_db": model["residual_sigma_db"],
        "links": model["links"],
    }
