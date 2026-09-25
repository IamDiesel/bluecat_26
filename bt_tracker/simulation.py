"""Realistischer Funk-Simulator für TriLola (Tests und A/B-Vergleiche).

Der Simulator ist absichtlich unabhängig vom Tracker-Code geschrieben
(eigene Wandgeometrie, eigene Statistik), damit Fehler im Tracker nicht
unbemerkt im Simulator „mitgespiegelt“ werden.

Abgebildete Effekte
-------------------
* Log-Distanz-Pfadverlust mit gerätespezifischer Empfangsverstärkung
* Wände (Dämpfung je Durchgang) und Türen
* räumlich korreliertes Shadowing je Sensor (Korrelationslänge ~1,5 m)
* langsame Körperabschattung (Ornstein-Uhlenbeck je Sensor)
* schnelles Fading und tiefe Ausreißer (Multipath)
* Empfangsschwelle und Paketverlust
* zwei Firmware-Varianten:
    - ``legacy``: ESP32/Pi senden einen Rohwert je 2 s, Shelly den Median,
      Offline-Meldung erst 30 s nach der letzten Sichtung
    - ``current``: alle senden Median + Anzahl je 2-s-Fenster, „nicht
      gesehen“ nach 10 s und danach als Heartbeat alle 15 s
* Sensor-zu-Sensor-Mesh inklusive Empfänger- und Senderdrift
* ein Katzen-Bewegungsmodell mit langen Ruhephasen und Wegen durch Türen

Einheiten: Positionen in cm, Pegel in dBm/dB, Zeiten in s.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

# ---------------------------------------------------------------------------
# Welt
# ---------------------------------------------------------------------------

# Sensorpositionen aus der echten Konfiguration (ohne kunibert_kiosk).
DEFAULT_SENSORS = {
    "arnd_esp": ((-224.4, 0.0), "esp32"),
    "kunibert": ((0.0, 0.0), "raspberry_pi"),
    "ron": ((77.1, -627.7), "raspberry_pi"),
    "shelly_schlafzimmer": ((-86.5, -520.5), "shelly"),
    "shelly_sz_lichtschrank": ((-213.0, -224.1), "shelly"),
    "shelly_wohnzimmer": ((-71.3, -626.2), "shelly"),
    "shelly_wohnzimmer_dim_sb": ((-219.8, -875.9), "shelly"),
    "tom_esp": ((-4.7, -829.5), "esp32"),
}

# Synthetischer Grundriss passend zu den Sensorpositionen.
ROOMS = {
    "Arbeitszimmer": [(-400, -150), (200, -150), (200, 150), (-400, 150)],
    "Schlafzimmer": [(-400, -570), (200, -570), (200, -150), (-400, -150)],
    "Wohnzimmer": [(-400, -1000), (200, -1000), (200, -570), (-400, -570)],
}
DOORS = {  # Türmitte, verbindet zwei Räume
    ("Arbeitszimmer", "Schlafzimmer"): (95.0, -150.0),
    ("Schlafzimmer", "Wohnzimmer"): (-340.0, -570.0),
}
INNER_WALL_DB = 6.0
# Wandsegmente (a, b, dB). Türen sind Lücken.
WALLS = [
    ((-400, -150), (50, -150), INNER_WALL_DB),
    ((140, -150), (200, -150), INNER_WALL_DB),
    ((-300, -570), (200, -570), INNER_WALL_DB),
    ((-400, -570), (-380, -570), INNER_WALL_DB),
    # Außenwände (blockierend, Dämpfung spielt innen keine Rolle)
    ((-400, 150), (200, 150), 12.0),
    ((-400, -1000), (200, -1000), 12.0),
    ((-400, -1000), (-400, 150), 12.0),
    ((200, -1000), (200, 150), 12.0),
]
REST_SPOTS = [
    ("Arbeitszimmer", (-300.0, 80.0)),
    ("Arbeitszimmer", (120.0, -60.0)),
    ("Schlafzimmer", (-300.0, -450.0)),
    ("Schlafzimmer", (100.0, -300.0)),
    ("Wohnzimmer", (-300.0, -900.0)),
    ("Wohnzimmer", (100.0, -760.0)),
    ("Wohnzimmer", (-150.0, -690.0)),
]

TAG_P0_REF = -60.0  # RSSI des Halsbands in 1 m bei Referenzempfänger
TYPE_RX_GAIN = {"esp32": -1.0, "shelly": -6.0, "raspberry_pi": 0.0}
TYPE_TX_1M = {"esp32": -58.0, "shelly": -64.0, "raspberry_pi": -60.0}


def room_of(point) -> Optional[str]:
    x, y = float(point[0]), float(point[1])
    for name, poly in ROOMS.items():
        xs = [p[0] for p in poly]
        ys = [p[1] for p in poly]
        if min(xs) <= x <= max(xs) and min(ys) <= y <= max(ys):
            return name
    return None


def _segments_cross(p, q, a, b) -> bool:
    """Echter Schnitt zweier Strecken (ohne Berührung an Endpunkten)."""

    def orient(u, v, w):
        return (v[0] - u[0]) * (w[1] - u[1]) - (v[1] - u[1]) * (w[0] - u[0])

    d1 = orient(a, b, p)
    d2 = orient(a, b, q)
    d3 = orient(p, q, a)
    d4 = orient(p, q, b)
    return (d1 * d2 < 0) and (d3 * d4 < 0)


def wall_loss_db(p, q) -> float:
    return float(sum(db for a, b, db in WALLS if _segments_cross(p, q, a, b)))


class ShadowField:
    """Stationäres Gauß-Feld mit quadratisch-exponentieller Kovarianz."""

    def __init__(self, rng, sigma_db=4.0, length_cm=150.0, features=96):
        self.sigma = float(sigma_db)
        self.k = rng.normal(0.0, 1.0 / length_cm, size=(features, 2))
        self.phi = rng.uniform(0.0, 2.0 * np.pi, size=features)
        self.scale = self.sigma * math.sqrt(2.0 / features)

    def __call__(self, p) -> float:
        p = np.asarray(p, dtype=float)
        return float(self.scale * np.sum(np.cos(self.k @ p + self.phi)))


# ---------------------------------------------------------------------------
# Bewegung
# ---------------------------------------------------------------------------

def _route(start_room, start, goal_room, goal):
    """Wegpunkte von start nach goal über die Türen (Räume liegen in Reihe)."""
    order = ["Arbeitszimmer", "Schlafzimmer", "Wohnzimmer"]
    i, j = order.index(start_room), order.index(goal_room)
    points = [start]
    step = 1 if j > i else -1
    for k in range(i, j, step):
        a, b = order[k], order[k + step]
        key = (a, b) if (a, b) in DOORS else (b, a)
        door = np.asarray(DOORS[key], dtype=float)
        # vor und hinter der Tür je 40 cm, damit keine Wand geschnitten wird
        direction = 1.0 if step > 0 else -1.0
        points.append(tuple(door + np.array([0.0, 40.0 * direction])))
        points.append(tuple(door - np.array([0.0, 40.0 * direction])))
    points.append(goal)
    return points


def cat_trajectory(rng, duration_s=1200.0, dt=0.1):
    """Erzeugt (t, pos, room, moving) in dt-Schritten."""
    spot_index = int(rng.integers(len(REST_SPOTS)))
    room, pos = REST_SPOTS[spot_index]
    pos = np.asarray(pos, dtype=float)
    t = 0.0
    times, positions, moving = [], [], []

    def emit(p, is_moving):
        times.append(t)
        positions.append(np.asarray(p, dtype=float).copy())
        moving.append(is_moving)

    while t < duration_s:
        # Ruhephase (lognormal, Median ~70 s)
        rest = float(np.clip(rng.lognormal(np.log(70.0), 0.7), 20.0, 400.0))
        jitter_center = pos.copy()
        end = t + rest
        while t < end and t < duration_s:
            # kleines Umlegen/Kopfbewegung: < 10 cm
            emit(jitter_center + rng.normal(0, 3.0, 2), False)
            t += dt
        if t >= duration_s:
            break
        # Weg zu einem anderen Platz
        choices = [k for k in range(len(REST_SPOTS)) if k != spot_index]
        spot_index = int(rng.choice(choices))
        goal_room, goal = REST_SPOTS[spot_index]
        speed = float(np.clip(rng.lognormal(np.log(80.0), 0.35), 40.0, 220.0))
        if rng.random() < 0.1:
            speed = 300.0  # Sprint
        waypoints = _route(room, tuple(pos), goal_room, goal)
        for a, b in zip(waypoints[:-1], waypoints[1:]):
            a = np.asarray(a, dtype=float)
            b = np.asarray(b, dtype=float)
            length = float(np.linalg.norm(b - a))
            steps = max(int(length / (speed * dt)), 1)
            for s in range(1, steps + 1):
                emit(a + (b - a) * s / steps, True)
                t += dt
        pos = np.asarray(goal, dtype=float)
        room = goal_room
    return np.asarray(times), np.asarray(positions), np.asarray(moving)


# ---------------------------------------------------------------------------
# Szenario
# ---------------------------------------------------------------------------

@dataclass
class ScenarioOptions:
    duration_s: float = 1200.0
    firmware: str = "current"  # "current" oder "legacy"
    shadow_sigma_db: float = 4.0
    fading_sigma_db: float = 3.0
    outlier_prob: float = 0.05
    body_sigma_db: float = 2.0
    n_true: float = 2.2
    detection_floor_dbm: float = -97.0
    rx_probability: float = 0.8
    advert_interval_s: float = 1.0
    unit_sigma_db: float = 2.0  # Exemplarstreuung der Empfänger
    receiver_drift: Optional[Tuple[str, float, float]] = None  # (sensor, t, dB)
    transmitter_drift: Optional[Tuple[str, float, float]] = None
    mesh: bool = True
    walls: bool = True
    dead_sensors: Tuple[str, ...] = ()  # senden gar nichts (z. B. Stromausfall)
    sensor_heights: Optional[Dict[str, float]] = None  # Antennenhöhe über Boden (cm); None = alles eben
    tag_height_cm: float = 25.0


@dataclass
class Event:
    t: float
    kind: str  # "tag" | "mesh"
    sensor: str
    rssi: float
    present: bool = True
    sample_count: int = 1
    transmitter: Optional[str] = None
    sequence: int = 0


@dataclass
class Scenario:
    sensors: Dict[str, Tuple[Tuple[float, float], str]]
    rx_gain: Dict[str, float]
    tx_1m: Dict[str, float]
    shadow: Dict[str, ShadowField]
    options: ScenarioOptions
    rng: np.random.Generator
    truth_t: np.ndarray = None
    truth_pos: np.ndarray = None
    truth_moving: np.ndarray = None
    events: List[Event] = field(default_factory=list)

    # -- Physik -------------------------------------------------------------
    def rx_gain_at(self, name, t):
        gain = self.rx_gain[name]
        drift = self.options.receiver_drift
        if drift and drift[0] == name and t >= drift[1]:
            gain += drift[2]
        return gain

    def tx_at(self, name, t):
        tx = self.tx_1m[name]
        drift = self.options.transmitter_drift
        if drift and drift[0] == name and t >= drift[1]:
            tx += drift[2]
        return tx

    def height_of(self, name) -> float:
        heights = self.options.sensor_heights
        return float(heights.get(name, self.options.tag_height_cm)) if heights else self.options.tag_height_cm

    def mean_tag_rssi(self, name, pos, t=0.0):
        sensor_pos = self.sensors[name][0]
        dz = self.height_of(name) - self.options.tag_height_cm
        d_m = max(math.sqrt((pos[0] - sensor_pos[0]) ** 2 + (pos[1] - sensor_pos[1]) ** 2 + dz * dz) / 100.0, 0.3)
        rssi = TAG_P0_REF + self.rx_gain_at(name, t) - 10.0 * self.options.n_true * math.log10(d_m)
        if self.options.walls:
            rssi -= wall_loss_db(pos, sensor_pos)
        rssi += self.shadow[name](pos)
        return rssi

    def true_position(self, t):
        idx = int(np.clip(np.searchsorted(self.truth_t, t), 0, len(self.truth_t) - 1))
        return self.truth_pos[idx]


def build_scenario(seed=0, options: Optional[ScenarioOptions] = None,
                   sensors=None) -> Scenario:
    options = options or ScenarioOptions()
    rng = np.random.default_rng(seed)
    sensors = dict(sensors or DEFAULT_SENSORS)
    rx_gain = {n: TYPE_RX_GAIN[kind] + rng.normal(0, options.unit_sigma_db)
               for n, (_, kind) in sensors.items()}
    tx_1m = {n: TYPE_TX_1M[kind] + rng.normal(0, 1.5) for n, (_, kind) in sensors.items()}
    shadow = {n: ShadowField(rng, options.shadow_sigma_db) for n in sensors}
    scenario = Scenario(sensors, rx_gain, tx_1m, shadow, options, rng)
    t, p, m = cat_trajectory(rng, options.duration_s)
    scenario.truth_t, scenario.truth_pos, scenario.truth_moving = t, p, m
    scenario.events = _generate_events(scenario)
    return scenario


def _generate_events(sc: Scenario) -> List[Event]:
    o = sc.options
    rng = sc.rng
    names = [n for n in sc.sensors if n not in set(o.dead_sensors)]
    events: List[Event] = []
    body = {n: 0.0 for n in names}
    phase = {n: rng.uniform(0, 2.0) for n in names}
    window = 2.0
    # Firmware-Zustand
    last_seen = {n: -1e9 for n in names}
    absent = {n: False for n in names}
    last_absent_pub = {n: -1e9 for n in names}
    last_raw_pub = {n: -1e9 for n in names}
    batch = {n: [] for n in names}
    seq = {n: 0 for n in names}
    next_flush = {n: phase[n] + window for n in names}

    def publish(n, t, rssi, present, count):
        seq[n] += 1
        latency = rng.uniform(0.05, 0.3)
        events.append(Event(t + latency, "tag", n, float(rssi), present, int(count), sequence=seq[n]))

    advert_t = 0.0
    tau_body = 30.0
    while advert_t < o.duration_s:
        pos = sc.true_position(advert_t)
        for n in names:
            # Körperabschattung (OU)
            a = math.exp(-o.advert_interval_s / tau_body)
            body[n] = a * body[n] + math.sqrt(1 - a * a) * rng.normal(0, o.body_sigma_db)
            # Fenster-Flush vor dieser Aussendung abarbeiten
            while next_flush[n] <= advert_t:
                tf = next_flush[n]
                if o.firmware == "current":
                    if batch[n]:
                        publish(n, tf, float(np.median(batch[n])), True, len(batch[n]))
                        batch[n] = []
                        absent[n] = False
                    elif tf - last_seen[n] >= 10.0:
                        if not absent[n] or tf - last_absent_pub[n] >= 15.0:
                            publish(n, tf, -130.0, False, 0)
                            absent[n] = True
                            last_absent_pub[n] = tf
                else:
                    kind = sc.sensors[n][1]
                    if kind == "shelly" and batch[n]:
                        publish(n, tf, float(np.median(batch[n])), True, len(batch[n]))
                        batch[n] = []
                    if (not absent[n]) and tf - last_seen[n] >= 30.0 and last_seen[n] > -1e8:
                        publish(n, tf, -130.0, False, 1)
                        absent[n] = True
                next_flush[n] += window
            rssi = sc.mean_tag_rssi(n, pos, advert_t) + body[n] + rng.normal(0, o.fading_sigma_db)
            if rng.random() < o.outlier_prob:
                rssi -= rng.uniform(8.0, 20.0)
            if rssi < o.detection_floor_dbm or rng.random() > o.rx_probability:
                continue
            rssi = round(rssi)
            last_seen[n] = advert_t
            if o.firmware == "current":
                batch[n].append(rssi)
            else:
                kind = sc.sensors[n][1]
                absent[n] = False
                if kind == "shelly":
                    batch[n].append(rssi)
                elif advert_t - last_raw_pub[n] >= 2.0:
                    publish(n, advert_t, rssi, True, 1)
                    last_raw_pub[n] = advert_t
        advert_t += o.advert_interval_s

    if o.mesh:
        link_shadow = {(r, tx): rng.normal(0, 3.0) for r in names for tx in names if r != tx}
        for r in names:
            for tx in names:
                if r == tx:
                    continue
                pr = sc.sensors[r][0]
                pt = sc.sensors[tx][0]
                dz = sc.height_of(r) - sc.height_of(tx)
                d_m = max(math.sqrt((pr[0] - pt[0]) ** 2 + (pr[1] - pt[1]) ** 2 + dz * dz) / 100.0, 0.3)
                base = -10.0 * o.n_true * math.log10(d_m) + link_shadow[(r, tx)]
                if o.walls:
                    base -= wall_loss_db(pr, pt)
                t = rng.uniform(0, 2.0)
                mseq = 0
                while t < o.duration_s:
                    rssi = sc.tx_at(tx, t) + sc.rx_gain_at(r, t) + base + rng.normal(0, 2.0)
                    if rssi > o.detection_floor_dbm:
                        mseq += 1
                        events.append(Event(t, "mesh", r, float(round(rssi)), True, 1, transmitter=tx,
                                            sequence=mseq))
                    t += 2.0
    events.sort(key=lambda e: e.t)
    return events


# ---------------------------------------------------------------------------
# Kalibrierung (simuliert den Ablauf von calibrate_sensor.py)
# ---------------------------------------------------------------------------

CALIBRATION_POINTS = [
    (-250, 50), (100, 60), (-50, -80),
    (-300, -300), (50, -250), (-100, -450),
    (-300, -700), (50, -700), (-100, -900), (100, -950),
]


def simulate_calibration_samples(sc: Scenario, points=CALIBRATION_POINTS, seconds=60.0):
    """Liefert {sensor: [(pos, [rssi...]), ...]} wie eine reale Messung."""
    o = sc.options
    rng = np.random.default_rng(12345)
    out = {n: [] for n in sc.sensors}
    for p in points:
        for n in sc.sensors:
            mean = sc.mean_tag_rssi(n, p, 0.0)
            samples = []
            body = 0.0
            for _ in range(int(seconds / o.advert_interval_s)):
                body = 0.97 * body + math.sqrt(1 - 0.97 ** 2) * rng.normal(0, o.body_sigma_db)
                rssi = mean + body + rng.normal(0, o.fading_sigma_db)
                if rng.random() < o.outlier_prob:
                    rssi -= rng.uniform(8, 20)
                if rssi >= o.detection_floor_dbm and rng.random() <= o.rx_probability:
                    samples.append(round(rssi))
            out[n].append((p, samples))
    return out


def legacy_calibration(samples_by_sensor, sensors) -> Dict[str, dict]:
    """Bisherige Einzel-Regression je Sensor (calibrate_sensor.py Modus 2)."""
    configs = {}
    for n, rows in samples_by_sensor.items():
        pos = np.asarray(sensors[n][0], dtype=float)
        dists, means, variances = [], [], []
        for p, s in rows:
            if len(s) < 3:
                continue
            s = np.asarray(s, dtype=float)
            med = np.median(s)
            mad = np.median(np.abs(s - med))
            inl = s[np.abs(s - med) <= max(3 * 1.4826 * mad, 2.0)]
            dists.append(max(np.linalg.norm(np.asarray(p) - pos) / 100.0, 0.05))
            means.append(float(np.mean(inl)))
            variances.append(float(np.var(inl, ddof=1)))
        cfg = {"tx_power": -59.0, "n_factor": 3.0, "r_min": 5.0, "r_max": 20.0, "q_variance": 0.1}
        if len(dists) >= 2:
            x = 10 * np.log10(dists)
            slope, intercept = np.polyfit(x, means, 1)
            if -slope > 0:
                cfg["tx_power"] = float(intercept)
                cfg["n_factor"] = float(-slope)
                cfg["r_min"] = variances[int(np.argmin(dists))]
                cfg["r_max"] = variances[int(np.argmax(dists))]
                cfg["q_variance"] = max(float(np.median(variances)) / 100.0, 0.001)
        configs[n] = cfg
    return configs


# ---------------------------------------------------------------------------
# Auswertung
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    name: str
    t: np.ndarray
    est: np.ndarray  # (T, 2), NaN wenn keine Ausgabe
    acc: np.ndarray
    truth: np.ndarray
    moving: np.ndarray
    room_est: List[Optional[str]]
    runtime_s: float = 0.0

    def metrics(self) -> dict:
        valid = np.all(np.isfinite(self.est), axis=1)
        err = np.linalg.norm(self.est - self.truth, axis=1)
        rest = valid & ~self.moving
        move = valid & self.moving
        # Ruhe ohne die ersten 10 s nach Ankunft
        settled = rest.copy()
        last_move = -1e9
        for i, t in enumerate(self.t):
            if self.moving[i]:
                last_move = t
            elif t - last_move < 10.0:
                settled[i] = False
        truth_rooms = [room_of(p) for p in self.truth]
        room_ok = [re == rt for re, rt in zip(self.room_est, truth_rooms)]
        room_ok = np.asarray(room_ok) & valid
        diffs = np.linalg.norm(np.diff(self.est, axis=0), axis=1)
        rest_pairs = settled[1:] & settled[:-1]
        jitter = float(np.sqrt(np.nanmean(diffs[rest_pairs] ** 2))) if rest_pairs.any() else float("nan")
        acc_ok = (err <= self.acc) & valid
        return {
            "coverage": float(valid.mean()),
            "rmse_rest": float(np.sqrt(np.mean(err[settled] ** 2))) if settled.any() else float("nan"),
            "median_rest": float(np.median(err[settled])) if settled.any() else float("nan"),
            "mean_move": float(np.mean(err[move])) if move.any() else float("nan"),
            "p90_all": float(np.percentile(err[valid], 90)) if valid.any() else float("nan"),
            "jitter_rest": jitter,
            "room_acc": float(room_ok.sum() / max(valid.sum(), 1)),
            "acc_calib": float(acc_ok.sum() / max(valid.sum(), 1)),
            "runtime_s": self.runtime_s,
        }


def run_engine(sc: Scenario, engine, name: str, room_fn: Optional[Callable] = None,
               tick_s: float = 1.0) -> RunResult:
    """Füttert eine Engine (Tracker-Schnittstelle) mit den Szenario-Ereignissen.

    Erwartet: ``engine.sensors[name].apply_reading(...)``, ``engine.process_tick(t)``,
    ``engine.observe_mesh(rx, tx, rssi, t)``. Ausgabe wird in 1-s-Schritten
    abgetastet (so, wie Home Assistant sie sehen würde).
    """
    import time as _time

    start = _time.perf_counter()
    grid_t = np.arange(0.0, sc.options.duration_s, 1.0)
    est = np.full((len(grid_t), 2), np.nan)
    acc = np.full(len(grid_t), np.nan)
    rooms: List[Optional[str]] = [None] * len(grid_t)
    current = (np.nan, np.nan, np.nan, None)
    gi = 0
    next_tick = tick_s

    def record(result):
        nonlocal current
        if result is None:
            return
        if getattr(result, "state", "inaktiv") == "aktiv" and result.x_cm is not None:
            room = getattr(result, "room", None)
            if room_fn is not None:
                room = room_fn((result.x_cm, result.y_cm))
            current = (result.x_cm, result.y_cm, result.accuracy_cm or np.nan, room)
        elif getattr(result, "state", None) == "inaktiv":
            current = (np.nan, np.nan, np.nan, None)

    def advance(to_t):
        nonlocal gi
        while gi < len(grid_t) and grid_t[gi] <= to_t:
            est[gi] = current[:2]
            acc[gi] = current[2]
            rooms[gi] = current[3]
            gi += 1

    for ev in sc.events:
        while next_tick <= ev.t:
            advance(next_tick)
            record(engine.process_tick(next_tick))
            next_tick += tick_s
        advance(ev.t)
        if ev.kind == "mesh":
            engine.observe_mesh(ev.sensor, ev.transmitter, ev.rssi, ev.t)
            continue
        node = engine.sensors.get(ev.sensor)
        if node is None:
            continue
        try:
            is_new = node.apply_reading(ev.rssi, ev.t, ev.present, ev.sequence, None,
                                        sample_count=ev.sample_count)
        except TypeError:
            is_new = node.apply_reading(ev.rssi, ev.t, ev.present, ev.sequence, None)
        if is_new:
            record(engine.process_tick(ev.t))
    advance(sc.options.duration_s + 1)
    truth = np.array([sc.true_position(t) for t in grid_t])
    moving = np.array([sc.truth_moving[min(np.searchsorted(sc.truth_t, t), len(sc.truth_t) - 1)]
                       for t in grid_t])
    if room_fn is None:
        rooms = [r if r is not None else (room_of(e) if np.all(np.isfinite(e)) else None)
                 for r, e in zip(rooms, est)]
    return RunResult(name, grid_t, est, acc, truth, moving, rooms, _time.perf_counter() - start)


def sensor_configs(sc: Scenario, calibration: Dict[str, dict], extra: Optional[dict] = None):
    configs = {}
    for n, (pos, kind) in sc.sensors.items():
        cfg = {
            "sensor_id": n, "name": n, "topic": f"bluecat/{n}/sensor/state",
            "pos": [float(pos[0]), float(pos[1])], "enabled": True, "position_configured": True,
            "implementation": kind, "rssi_limit": -110.0, "q_variance": 0.1,
        }
        cfg.update(calibration.get(n, {}))
        if sc.options.sensor_heights:  # Höhen bekannt → Tracker rechnet schräge Abstände
            cfg["z_cm"] = sc.height_of(n)
        if extra:
            cfg.update(extra)
        configs[n] = cfg
    return configs


def floorplan_dict() -> dict:
    """Grundriss des Simulators im Format von config/floorplan.json."""
    return {
        "walls": [
            {"a": list(a), "b": list(b), "attenuation_db": db, "blocking": True}
            for a, b, db in WALLS
        ],
        "rooms": [{"name": n, "polygon": [list(p) for p in poly]} for n, poly in ROOMS.items()],
    }
