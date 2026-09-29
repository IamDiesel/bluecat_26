"""Anwendungslogik des TriLola-Trackers (ohne MQTT-Verdrahtung).

``TriLolaApp`` kapselt Konfiguration, Engine, Home-Assistant-Ausgabe und das
Routing eingehender Nachrichten. Die MQTT-Anbindung (``trilola_tracker.py``)
ruft nur ``on_connect``, ``on_message`` und ``housekeeping`` auf. Dadurch
lässt sich die komplette Logik ohne Broker testen und aus Aufzeichnungen
nachspielen (``tools/replay.py``).

Alle öffentlichen Methoden sind über ``self.lock`` threadsicher.
"""

from __future__ import annotations

import dataclasses
import importlib
import json
import math
import os
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Optional

from config_manager import (
    CALIBRATION_FIELDS,
    CONFIG_ROOT,
    ENGINE_SET_TOPIC,
    ENGINE_STATE_TOPIC,
    ENGINES,
    MESH_PEERS_TOPIC,
    MESH_RELEARN_TOPIC,
    SENSOR_CONFIG_STATE_PATTERN,
    TARGET_MAC_SET_TOPIC,
    TARGET_MAC_STATE_TOPIC,
    ConfigStore,
    format_ble_address,
    mesh_peers_payload,
    mount_height_cm,
    parse_bool,
    sensor_z_cm,
)
import tuning
from core.engine import TrackingEngine
from core.floorplan import FloorPlan
from core.pf_engine import DEFAULTS as PF_DEFAULTS
from core.pf_engine import ParticleEngine
from network.ha_discovery import (
    AVAILABILITY_TOPIC,
    MOVING_STATE_TOPIC,
    ROOM_STATE_TOPIC,
    STATE_TOPIC_GPS,
    TRACKER_ATTR_TOPIC,
    TRACKER_ROOT,
    TRACKER_STATE_TOPIC,
    HADiscoveryBuilder,
)
from network.payload_parser import MeshBeacon, PayloadParser, SensorReading
from calibration_session import CalibrationSession
from radio_tomography import RadioTomography, autocal_suggestions

DEFAULT_PARAMS = {
    "TRACKING_ENGINE": "pf",
    "SENSOR_TIMEOUT_SEC": 30.0,
    # Anwesenheit: „weg“, wenn so lange kein Sensor sie mit mindestens diesem Signal gesehen hat.
    # -120 dBm = jede Sichtung zählt; -100 ignoriert nur extrem schwache Sichtungen.
    "PRESENCE_MIN_RSSI_DBM": -100.0,
    "PRESENCE_LOST_SEC": 30.0,
    "OFFLINE_TIMEOUT_SEC": 90.0,
    "MAX_SNAPSHOT_SKEW_SEC": 3.0,
    "POSITION_MEMORY_SEC": 10.0,
    "MAX_TRACK_DISTANCE_CM": 2000.0,
    "MAX_POSITION_RADIUS_CM": 2000.0,
    "MAX_POSITION_SPEED_CM_S": 350.0,
    "PARTICLE_COUNT": 1500,
    "PARTICLE_PROCESS_NOISE_CM": 75.0,
    "PARTICLE_INITIAL_SPREAD_CM": 250.0,
    "PARTICLE_MINIMUM_SIGMA_CM": 75.0,
    "PARTICLE_BOUNDS_MARGIN_CM": 500.0,
    "RADIO_BASELINE_LEARNING_SAMPLES": 30,
    "RADIO_LINK_TIMEOUT_SEC": 30.0,
    "RADIO_BASELINE_FILE": "config/radio_mesh_baseline.json",
    "RADIO_HEATMAP_FILE": "config/radio_heatmap.json",
    "RADIO_GRID_ENABLED": False,
    "RADIO_GRID_DYNAMIC": False,
    "FLOORPLAN_FILE": "config/floorplan.json",
    "PUBLISH_INTERVAL_SEC": 1.0,
    "PUBLISH_MIN_MOVE_CM": 5.0,
    "PUBLISH_HEARTBEAT_SEC": 60.0,
    "PUBLISH_DIAGNOSTICS": True,
    "MOVING_SPEED_CM_S": 30.0,
    "MOVING_WINDOW_SEC": 4.0,
    "MOVING_HOLD_SEC": 5.0,
    "SAVE_INTERVAL_SEC": 300.0,
    "RECORD_FILE": "",
    "LOG_POSITIONS": True,
    "ORIGIN_LAT": None,
    "ORIGIN_LON": None,
    "ORIGIN_BEARING_DEG": 0.0,
    "GEOREF_FILE": "config/georef.json",
    "LIVE_PUBLISH": True,
    "LIVE_INTERVAL_SEC": 1.0,
    "LIVE_CLOUD_POINTS": 120,
    "RADIO_MAP_INTERVAL_SEC": 10.0,
    "RADIO_MAP_CELL_CM": 50.0,
    "RADIO_DYNAMIC_TAU_SEC": 120.0,
    "TAG_HEIGHT_CM": 25.0,
}
PATH_PARAMS = ("RADIO_BASELINE_FILE", "RADIO_HEATMAP_FILE", "FLOORPLAN_FILE", "RECORD_FILE", "GEOREF_FILE")
FLOORPLAN_SET_TOPIC = CONFIG_ROOT + "/floorplan/set"
FLOORPLAN_STATE_TOPIC = CONFIG_ROOT + "/floorplan/state"
GEOREF_SET_TOPIC = CONFIG_ROOT + "/tracker/georef/set"
GEOREF_STATE_TOPIC = CONFIG_ROOT + "/tracker/georef/state"
RADIO_MAP_TOPIC = TRACKER_ROOT + "/radio_map"
CALIBRATION_SET_TOPIC = CONFIG_ROOT + "/calibration/set"
CALIBRATION_STATE_TOPIC = CONFIG_ROOT + "/calibration/state"
TUNING_SET_TOPIC = CONFIG_ROOT + "/tracker/tuning/set"
TUNING_STATE_TOPIC = CONFIG_ROOT + "/tracker/tuning/state"
CAL_KEYS = ("tx_power", "n_factor", "sigma_db", "r_min", "r_max")
LIVE_TOPIC = TRACKER_ROOT + "/live"
MAX_FLOORPLAN_WALLS = 2000
MAX_FLOORPLAN_ROOMS = 200
CONFIG_SET_RE = re.compile(
    r"^bluecat/config/sensors/([^/]+)/(ble_mac|position|position_x|position_y|calibration|"
    r"calibration_[a-z_]+|enabled|name|implementation|remove|height|floor)/set$"
)


def normalize_floorplan(data) -> dict:
    """Nur bekannte Felder, Zahlen gerundet – so wird der Grundriss gespeichert und veröffentlicht."""
    if not isinstance(data, dict):
        raise ValueError("Grundriss muss ein JSON-Objekt sein")
    walls_in = data.get("walls") or []
    rooms_in = data.get("rooms") or []
    if not isinstance(walls_in, list) or not isinstance(rooms_in, list):
        raise ValueError("walls/rooms müssen Listen sein")
    if len(walls_in) > MAX_FLOORPLAN_WALLS or len(rooms_in) > MAX_FLOORPLAN_ROOMS:
        raise ValueError("Grundriss zu groß")

    def point(value):
        x, y = (float(v) for v in value)
        if not (math.isfinite(x) and math.isfinite(y)) or max(abs(x), abs(y)) > 1e6:
            raise ValueError("ungültiger Punkt")
        return [round(x, 1), round(y, 1)]

    def number(value, low, high):
        value = float(value)
        if not math.isfinite(value):
            raise ValueError("ungültige Zahl")
        return max(low, min(value, high))

    out = {"default_wall_db": round(number(data.get("default_wall_db", 5.0), 0.0, 60.0), 2), "walls": [], "rooms": []}
    if data.get("wall_scale") not in (None, ""):
        out["wall_scale"] = round(number(data["wall_scale"], 0.0, 10.0), 4)
    if data.get("floor_elevation_cm") not in (None, ""):
        # Fußboden der Wohnung über Grund (Stockwerk); Sensor-Höhen zählen ab diesem Boden
        out["floor_elevation_cm"] = round(number(data["floor_elevation_cm"], -5000.0, 50000.0), 1)
    for wall in walls_in:
        a, b = point(wall["a"]), point(wall["b"])
        if a == b:
            continue
        item = {"a": a, "b": b}
        if wall.get("attenuation_db") not in (None, ""):
            item["attenuation_db"] = round(number(wall["attenuation_db"], 0.0, 60.0), 2)
        item["blocking"] = bool(wall.get("blocking", True))
        out["walls"].append(item)
    for room in rooms_in:
        polygon = [point(p) for p in room["polygon"]]
        if len(polygon) < 3:
            continue
        out["rooms"].append({"name": str(room.get("name") or f"Raum {len(out['rooms']) + 1}")[:60],
                             "polygon": polygon})
    return out


def _write_json_atomic(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def load_params(sec, base_dir: str) -> dict:
    params = {}
    for key, default in DEFAULT_PARAMS.items():
        params[key] = getattr(sec, key, default)
    for key in dir(sec):
        if key.startswith("PF_"):
            params[key] = getattr(sec, key)
    for key in PATH_PARAMS:
        value = params.get(key)
        if value and not os.path.isabs(value):
            params[key] = os.path.join(base_dir, value)
    if params.get("ORIGIN_LAT") is None or params.get("ORIGIN_LON") is None:
        # Rückwärtskompatibel: früherer lokaler Ordner "secrets/tri.py".
        # Achtung: Ein Ordner namens "secrets" verdeckt das Python-Standardmodul
        # gleichen Namens – besser ORIGIN_* in secrets_tri.py eintragen.
        try:
            legacy = importlib.import_module("secrets.tri")
            params["ORIGIN_LAT"] = getattr(legacy, "ORIGIN_LAT", params.get("ORIGIN_LAT"))
            params["ORIGIN_LON"] = getattr(legacy, "ORIGIN_LON", params.get("ORIGIN_LON"))
        except Exception:
            pass
    return params


class LocalFrame:
    """Lokale cm-Koordinaten → WGS84. ``bearing_deg``: Richtung der +y-Achse
    im Uhrzeigersinn von Norden (0 = +y zeigt nach Norden)."""

    def __init__(self, lat, lon, bearing_deg=0.0):
        self.valid = lat is not None and lon is not None
        self.lat = float(lat or 0.0)
        self.lon = float(lon or 0.0)
        self.bearing = math.radians(float(bearing_deg or 0.0))
        lat_rad = math.radians(self.lat)
        # WGS84-Näherung, Fehler < 0,01 %
        self.m_per_deg_lat = 111132.92 - 559.82 * math.cos(2 * lat_rad) + 1.175 * math.cos(4 * lat_rad)
        self.m_per_deg_lon = 111412.84 * math.cos(lat_rad) - 93.5 * math.cos(3 * lat_rad)

    def to_gps(self, x_cm, y_cm):
        x = float(x_cm) / 100.0
        y = float(y_cm) / 100.0
        east = x * math.cos(self.bearing) + y * math.sin(self.bearing)
        north = -x * math.sin(self.bearing) + y * math.cos(self.bearing)
        return (round(self.lat + north / self.m_per_deg_lat, 7), round(self.lon + east / self.m_per_deg_lon, 7))


class MovementDetector:
    """„In Bewegung“ aus der Positionsänderung über ein Zeitfenster."""

    def __init__(self, speed_cm_s=30.0, window_sec=4.0, hold_sec=5.0):
        self.speed = float(speed_cm_s)
        self.window = float(window_sec)
        self.hold = float(hold_sec)
        self.history = deque()
        self.last_trigger = None

    def reset(self):
        self.history.clear()
        self.last_trigger = None

    def update(self, t, x, y) -> bool:
        self.history.append((t, x, y))
        while self.history and t - self.history[0][0] > self.window:
            self.history.popleft()
        t0, x0, y0 = self.history[0]
        if t - t0 >= 0.5 * self.window:
            if math.hypot(x - x0, y - y0) / max(t - t0, 1e-6) > self.speed:
                self.last_trigger = t
        return self.last_trigger is not None and t - self.last_trigger <= self.hold


class TriLolaApp:
    def __init__(self, base_dir: str, sec, publisher, config_dir: Optional[str] = None, clock=time.monotonic):
        self.lock = threading.RLock()
        self.base_dir = base_dir
        self.clock = clock
        self.publisher = publisher
        self.params = load_params(sec, base_dir)
        self.store = ConfigStore(config_dir or os.path.join(base_dir, "config"))
        self.store.load()
        # Feintuning: Standard = secrets_tri.py bzw. Code, Abweichungen aus config/tuning.json
        self.tuning_defaults = self._tuning_defaults()
        self.tuning = tuning.load(self.store.config_dir)
        self.params.update(self.tuning)
        self._tuning_message = ""
        self.georef = self._load_georef()
        self.frame = LocalFrame(self.georef.get("lat"), self.georef.get("lon"), self.georef.get("bearing_deg", 0.0))
        if not self.frame.valid:
            print("Warnung: ORIGIN_LAT/ORIGIN_LON fehlen in secrets_tri.py – GPS-Koordinaten beziehen sich auf 0/0.")
        self.floorplan_data = self._load_floorplan_data()
        self.floorplan = FloorPlan.load(self.params.get("FLOORPLAN_FILE"))
        if self.floorplan.has_walls or self.floorplan.has_rooms:
            print(f"Grundriss geladen: {len(self.floorplan.wall_db)} Wände, {len(self.floorplan.rooms)} Räume.")
        self.target_tag_id = getattr(sec, "BLE_TAG_ID", None) or None
        self.observed_tag_id = self.target_tag_id
        kind = self.store.engine or str(self.params.get("TRACKING_ENGINE") or "pf")
        self.engine_kind = kind if kind in ENGINES else "pf"
        self.engine = self._make_engine(self.engine_kind)
        self.engine.setup_sensors(self.active_sensor_configs())
        self.movement = MovementDetector(self.params["MOVING_SPEED_CM_S"], self.params["MOVING_WINDOW_SEC"],
                                         self.params["MOVING_HOLD_SEC"])
        self._pub = {"state": None, "time": -1e9, "pos": None, "acc": None, "room": None, "moving": None,
                     "tracker_state": None}
        self._last_save = self.clock()
        self._last_live = -1e9
        self._last_radio_map = -1e9
        self._live_rng = __import__("numpy").random.default_rng()
        self.tomo = RadioTomography(cell_cm=float(self.params["RADIO_MAP_CELL_CM"]),
                                    tau_sec=float(self.params["RADIO_DYNAMIC_TAU_SEC"]))
        self.calibration = CalibrationSession(self.store.config_dir, clock=self.clock)
        self._last_cal_publish = -1e9
        self._inactive_grace_until = -1e9
        self._last_strong_sighting = -1e9
        self._recorder = None
        if self.params.get("RECORD_FILE"):
            os.makedirs(os.path.dirname(self.params["RECORD_FILE"]) or ".", exist_ok=True)
            self._recorder = open(self.params["RECORD_FILE"], "a", encoding="utf-8", buffering=1)

    # ------------------------------------------------------------------
    # Kartenbezug & Grundriss
    # ------------------------------------------------------------------
    def _load_georef(self) -> dict:
        """Ursprung (lat/lon) und Ausrichtung. config/georef.json (aus der Oberfläche) hat Vorrang,
        solange secrets_tri.py seitdem nicht geändert wurde – ein neuer Rollout mit anderen Werten gewinnt."""
        secrets_value = [self.params.get("ORIGIN_LAT"), self.params.get("ORIGIN_LON"),
                         float(self.params.get("ORIGIN_BEARING_DEG") or 0.0)]
        from_secrets = {"lat": secrets_value[0], "lon": secrets_value[1], "bearing_deg": secrets_value[2],
                        "source": "secrets"}
        path = self.params.get("GEOREF_FILE")
        if not path or not os.path.exists(path):
            return from_secrets
        try:
            with open(path, "r", encoding="utf-8") as handle:
                saved = json.load(handle)
        except (OSError, json.JSONDecodeError) as error:
            print(f"georef.json nicht lesbar: {error}")
            return from_secrets
        try:
            if not isinstance(saved, dict):
                raise ValueError("kein Objekt")
            for key in ("lat", "lon", "bearing_deg"):
                if not math.isfinite(float(saved.get(key, 0.0))):
                    raise ValueError(f"{key} ungültig")
        except (TypeError, ValueError) as error:
            print(f"georef.json ungültig ({error}) – nutze secrets_tri.py")
            return from_secrets
        if saved.get("secrets_seen") != secrets_value and secrets_value[0] is not None:
            print("Kartenbezug aus secrets_tri.py (neuer Rollout) ersetzt georef.json.")
            return from_secrets
        saved["source"] = "georef"
        return saved

    def _set_georef(self, payload):
        try:
            data = json.loads(payload)
            lat, lon = float(data["lat"]), float(data["lon"])
            bearing = float(data.get("bearing_deg", 0.0))
            if not all(math.isfinite(v) for v in (lat, lon, bearing)):
                raise ValueError("ungültige Zahl")
            bearing %= 360.0
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError("Koordinaten außerhalb des gültigen Bereichs")
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            print(f"Ungültiger Kartenbezug: {error}")
            self._publish(GEOREF_STATE_TOPIC, self._georef_state(), retain=True)
            return
        georef = {"lat": round(lat, 8), "lon": round(lon, 8), "bearing_deg": round(bearing, 3),
                  "secrets_seen": [self.params.get("ORIGIN_LAT"), self.params.get("ORIGIN_LON"),
                                   float(self.params.get("ORIGIN_BEARING_DEG") or 0.0)]}
        for key in ("reference", "reference_lat", "reference_lon"):
            if data.get(key) not in (None, ""):
                georef[key] = data[key]
        if self.params.get("GEOREF_FILE"):
            _write_json_atomic(self.params["GEOREF_FILE"], georef)
        georef["source"] = "georef"
        self.georef = georef
        self.frame = LocalFrame(lat, lon, bearing)
        self._publish(GEOREF_STATE_TOPIC, self._georef_state(), retain=True)
        self._pub["time"] = -1e9  # GPS-Position sofort neu senden
        print(f"Kartenbezug gesetzt: {lat:.7f}, {lon:.7f}, Ausrichtung {bearing:.1f}°")

    def _georef_state(self) -> dict:
        state = {k: v for k, v in self.georef.items() if k != "secrets_seen"}
        state["valid"] = self.frame.valid
        return state

    def _load_floorplan_data(self) -> dict:
        path = self.params.get("FLOORPLAN_FILE")
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    return normalize_floorplan(json.load(handle))
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
                print(f"Grundriss {path} nicht lesbar: {error}")
        return {"default_wall_db": 5.0, "walls": [], "rooms": []}

    def _set_floorplan(self, payload):
        try:
            data = normalize_floorplan(json.loads(payload))
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            print(f"Ungültiger Grundriss: {error}")
            self._publish(FLOORPLAN_STATE_TOPIC, self.floorplan_data, retain=True)
            return
        if self.params.get("FLOORPLAN_FILE"):
            _write_json_atomic(self.params["FLOORPLAN_FILE"], data)
        old_floor = self._apartment_floor_cm()
        self.floorplan_data = data
        self.floorplan = FloorPlan.from_dict(data, source=self.params.get("FLOORPLAN_FILE"))
        self.engine.set_floorplan(self.floorplan)
        if self._apartment_floor_cm() != old_floor:  # Sensoren auf anderen Stockwerken: neue Höhenunterschiede
            self.engine.setup_sensors(self.active_sensor_configs())
            for sensor_id in self.store.sensors:
                for topic, msg, retain in self._sensor_state_messages(sensor_id):
                    self._publish(topic, msg, retain)
        self._publish(FLOORPLAN_STATE_TOPIC, data, retain=True)
        self._pub["room"] = None
        print(f"Grundriss aktualisiert: {len(data['walls'])} Wände, {len(data['rooms'])} Räume.")

    def _publish_radio_map(self, now, force=False):
        env = getattr(self.engine, "radio_env", None)
        if env is None or (not force and now - self._last_radio_map < float(self.params["RADIO_MAP_INTERVAL_SEC"])):
            return
        self._last_radio_map = now
        try:
            data = self.tomo.update(env, now, self.floorplan_data)
        except Exception as error:  # Karte darf das Tracking nie stören
            print(f"Funkkarte nicht berechenbar: {error}")
            return
        if data is not None:
            self._publish(RADIO_MAP_TOPIC, data, retain=True)

    # ------------------------------------------------------------------
    # Kalibrierplan & Autokalibrierung
    # ------------------------------------------------------------------
    def _publish_calibration(self):
        self._last_cal_publish = self.clock()
        self._publish(CALIBRATION_STATE_TOPIC, self.calibration.state(), retain=True)

    def _positioned_configs(self):
        return {sid: self._with_height(cfg.data) for sid, cfg in self.store.sensors.items()
                if cfg.data.get("enabled", True) and cfg.data.get("position_configured", True)}

    def _handle_calibration(self, payload):
        session = self.calibration
        try:
            cmd = json.loads(payload) if payload.strip().startswith("{") else {"cmd": payload.strip()}
            action = str(cmd.get("cmd", ""))
            if action == "start":
                session.start(cmd["x"], cmd["y"], cmd.get("duration_s", 90), cmd.get("id"), cmd.get("label", ""),
                              cmd.get("z"))
                print(f"Kalibrierung: Messung bei ({session.active['x']}, {session.active['y']}) für "
                      f"{session.active['duration_s']:.0f} s")
            elif action == "cancel":
                session.cancel()
            elif action == "delete":
                session.delete(str(cmd.get("id", "")))
            elif action == "clear":
                session.clear()
            elif action == "fit":
                configs = self._positioned_configs()
                session.run_fit({sid: d["pos"] for sid, d in configs.items()}, configs, self.floorplan,
                                per_sensor_n=bool(cmd.get("per_sensor_n")),
                                sensor_heights={sid: d["antenna_z_cm"] for sid, d in configs.items()},
                                tag_height_cm=float(self.params["TAG_HEIGHT_CM"]))
                session.message = "Auswertung fertig – bitte prüfen und übernehmen."
            elif action == "apply_fit":
                self._apply_fit()
            elif action == "autocal":
                env = getattr(self.engine, "radio_env", None)
                if env is None:
                    raise ValueError("Kein Mesh verfügbar")
                configs = self._positioned_configs()
                session.autocal = autocal_suggestions(env.baseline_links(), dict(env.sensor_positions), configs,
                                                      self.floorplan_data,
                                                      heights={sid: d["antenna_z_cm"] for sid, d in configs.items()})
                session.autocal["time"] = time.time()
                self.tomo.last_autocal = session.autocal
                session.message = (f"Autokalibrierung aus {session.autocal['links']} Funkstrecken – "
                                   "bitte prüfen und übernehmen.")
            elif action == "apply_autocal":
                self._apply_autocal(cmd)
            else:
                raise ValueError(f"unbekannter Befehl {action!r}")
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            session.message = f"Fehler: {error}"
            print(f"Kalibrierung: {error}")
        self._publish_calibration()

    def _apply_sensor_calibration(self, values: dict, status: str):
        for sid, cal in values.items():
            if sid not in self.store.sensors:
                continue
            config = self.store.update_field(sid, "calibration", {k: float(cal[k]) for k in CAL_KEYS if k in cal})
            for key in ("calibration_points", "calibration_rms_db"):
                if key in cal:
                    config.data[key] = cal[key]
            config.data["calibration_status"] = status
            config.data["calibration_geometry"] = "3d"  # mit Höhen gerechnet
            self.store.save(config)
            for topic, msg, retain in self._sensor_state_messages(sid):
                self._publish(topic, msg, retain)
        self.engine.setup_sensors(self.active_sensor_configs())

    def _apply_fit(self):
        fit = self.calibration.fit
        if not fit:
            raise ValueError("Erst auswerten")
        self._apply_sensor_calibration(fit["sensors"], "calibrated")
        scale = (fit.get("globals") or {}).get("wall_scale")
        if scale and self.floorplan_data.get("walls"):
            data = dict(self.floorplan_data)
            data["wall_scale"] = round(float(data.get("wall_scale", 1.0) or 1.0) * float(scale), 3)
            self._set_floorplan(json.dumps(data))
        self.calibration.message = f"Kalibrierung übernommen ({len(fit['sensors'])} Sensoren)."
        print(self.calibration.message)

    def _apply_autocal(self, cmd):
        auto = self.calibration.autocal
        if not auto:
            raise ValueError("Erst Autokalibrierung ausführen")
        chosen = cmd.get("sensors", True)
        ids = set(auto["sensors"]) if chosen is True else set(chosen or []) & set(auto["sensors"])
        values = {}
        for sid in ids:
            item = {"tx_power": auto["sensors"][sid]["suggested_tx_power"]}
            if cmd.get("n", True):
                item["n_factor"] = auto["n_factor"]
            values[sid] = item
        if values:
            self._apply_sensor_calibration(values, "auto")
        walls_done = 0
        if cmd.get("walls", True) and auto.get("walls") and self.floorplan_data.get("walls"):
            data = json.loads(json.dumps(self.floorplan_data))
            scale = float(data.get("wall_scale", 1.0) or 1.0)
            if len(auto["walls"]) == len(data["walls"]):
                for wall, est in zip(data["walls"], auto["walls"]):
                    effective = est["estimate_db"] if est["links"] >= 2 else \
                        float(wall.get("attenuation_db", data.get("default_wall_db", 5.0))) * scale
                    wall["attenuation_db"] = round(effective, 2)
                    walls_done += est["links"] >= 2
                data["wall_scale"] = 1.0
                self._set_floorplan(json.dumps(data))
        self.calibration.message = f"Autokalibrierung übernommen: {len(values)} Sensoren, {walls_done} Wände."
        print(self.calibration.message)

    def _publish_live(self, result, now):
        if not self.params.get("LIVE_PUBLISH", True):
            return
        if now - self._last_live < float(self.params["LIVE_INTERVAL_SEC"]):
            return
        self._last_live = now
        timeout = float(self.params.get("SENSOR_TIMEOUT_SEC", 30.0))
        sensors = {}
        for sid, node in self.engine.sensors.items():
            fresh = node.is_fresh(now, timeout) if hasattr(node, "is_fresh") else False
            sensors[sid] = {"rssi": None if node.rssi is None else round(float(node.rssi), 1),
                            "present": bool(node.present) and fresh}
        live = {"state": result.state, "engine": self.engine_kind, "sensors": sensors,
                "wall_time": round(time.time(), 1)}
        if result.state == "aktiv" and result.x_cm is not None:
            live.update({"x_cm": round(float(result.x_cm), 1), "y_cm": round(float(result.y_cm), 1),
                         "accuracy_cm": round(float(result.accuracy_cm or 0.0), 1),
                         "room": result.room, "moving": bool(self._pub.get("moving"))})
            particles = getattr(self.engine, "particles", None)
            logw = getattr(self.engine, "logw", None)
            count = int(self.params.get("LIVE_CLOUD_POINTS", 120) or 0)
            if particles is not None and logw is not None and count > 0 and len(particles):
                import numpy as np
                w = np.exp(logw - np.max(logw))
                w /= w.sum()
                idx = self._live_rng.choice(len(particles), size=min(count, len(particles)), p=w)
                live["cloud"] = [[round(float(particles[i, 0])), round(float(particles[i, 1]))] for i in idx]
        self._publish(LIVE_TOPIC, live, retain=False)

    # ------------------------------------------------------------------
    # Engine & Konfiguration
    # ------------------------------------------------------------------
    def _make_engine(self, kind):
        engine_params = dict(self.params)
        engine_params["PARTICLE_COUNT"] = self.params.get("PARTICLE_COUNT")
        if kind == "legacy":
            return TrackingEngine(engine_params, floorplan=self.floorplan)
        return ParticleEngine(engine_params, floorplan=self.floorplan)

    def _apartment_floor_cm(self) -> float:
        return float(self.floorplan_data.get("floor_elevation_cm") or 0.0)

    @staticmethod
    def _height_active(data: dict) -> bool:
        """Höhen im Halsband-Modell nur, wenn die Kalibrierung dazu passt: Werte, die früher eben (2D)
        gefittet wurden, gelten weiter eben – bis zur nächsten Auswertung mit Höhen."""
        legacy = data.get("calibration_status") in ("calibrated", "auto") and data.get("calibration_geometry") != "3d"
        return not legacy

    def _with_height(self, data: dict) -> dict:
        """Konfiguration plus Antennenhöhe über dem Fußboden der Wohnung (``antenna_z_cm``) und die im
        Halsband-Modell verwendete Höhe ``z_cm`` (None = eben)."""
        out = dict(data)
        antenna = round(sensor_z_cm(out, self._apartment_floor_cm()), 1)
        out["antenna_z_cm"] = antenna
        out["z_cm"] = antenna if self._height_active(out) else None
        return out

    def active_sensor_configs(self) -> dict:
        return {sid: self._with_height(cfg.data) for sid, cfg in self.store.sensors.items()
                if cfg.data.get("enabled", True)}

    def all_sensor_configs(self) -> dict:
        return {sid: dict(cfg.data) for sid, cfg in self.store.sensors.items()}

    def _publish(self, topic, payload, retain=False):
        self.publisher.publish(topic, payload, retain=retain)

    def _sensor_state_messages(self, sensor_id):
        data = self.store.sensors[sensor_id].data
        pattern = SENSOR_CONFIG_STATE_PATTERN
        return [
            (pattern.format(sensor_id=sensor_id, field="ble_mac"),
             data["ble_addresses"][0] if data.get("ble_addresses") else "", True),
            (pattern.format(sensor_id=sensor_id, field="position"),
             {"x_cm": float(data["pos"][0]), "y_cm": float(data["pos"][1]),
              "height_cm": mount_height_cm(data), "height_set": data.get("height_cm") is not None,
              "floor_cm": data.get("floor_cm"),
              "z_cm": round(sensor_z_cm(data, self._apartment_floor_cm()), 1),
              "height_active": self._height_active(data),
              "configured": bool(data.get("position_configured"))}, True),
            (pattern.format(sensor_id=sensor_id, field="calibration"),
             {k: data.get(k) for k in CALIBRATION_FIELDS}, True),
            (pattern.format(sensor_id=sensor_id, field="enabled"), "ON" if data.get("enabled", True) else "OFF", True),
        ]

    # ------------------------------------------------------------------
    # MQTT-Einstieg
    # ------------------------------------------------------------------
    def on_connect(self):
        with self.lock:
            self.publisher.subscribe("bluecat/#")
            self._publish(AVAILABILITY_TOPIC, "online", retain=True)
            for sensor_id in self.store.sensors:
                for topic, payload, retain in HADiscoveryBuilder.legacy_cleanup(sensor_id):
                    self._publish(topic, payload, retain)
            for topic, payload, retain in HADiscoveryBuilder.build_all(self.all_sensor_configs()):
                self._publish(topic, payload, retain)
            for sensor_id in self.store.sensors:
                for topic, payload, retain in self._sensor_state_messages(sensor_id):
                    self._publish(topic, payload, retain)
            if self.store.target_mac:
                self._publish(TARGET_MAC_STATE_TOPIC, self.store.target_mac, retain=True)
            self._publish(ENGINE_STATE_TOPIC, self.engine_kind, retain=True)
            self._publish(MESH_PEERS_TOPIC, mesh_peers_payload(self.store.sensors), retain=True)
            self._publish(FLOORPLAN_STATE_TOPIC, self.floorplan_data, retain=True)
            self._publish(GEOREF_STATE_TOPIC, self._georef_state(), retain=True)
            self._publish(CALIBRATION_STATE_TOPIC, self.calibration.state(), retain=True)
            self._publish_tuning()
            now = self.clock()
            result = self._presence_filter(self.engine.process_tick(now), now)
            self._publish_result(result, now, force=True)
            self._publish_radio_map(now, force=True)

    def on_message(self, topic: str, payload: str, retain: bool = False, now: Optional[float] = None):
        with self.lock:
            now = self.clock() if now is None else now
            if self._recorder and topic.startswith("bluecat/") and not topic.startswith(TRACKER_ROOT + "/"):
                self._recorder.write(json.dumps({"t": now, "wall": time.time(), "topic": topic,
                                                 "payload": payload, "retain": retain}, ensure_ascii=False) + "\n")
            try:
                self._route(topic, payload, retain, now)
            except Exception as error:
                print(f"Fehler bei {topic}: {error}")

    def housekeeping(self, now: Optional[float] = None):
        with self.lock:
            now = self.clock() if now is None else now
            try:
                result = self._presence_filter(self.engine.process_tick(now), now)
                self._publish_result(result, now)
                self._publish_live(result, now)
                self._publish_radio_map(now)
                if self.calibration.tick():
                    print("Kalibrierung: " + self.calibration.message)
                    self._publish_calibration()
                elif self.calibration.active is not None and now - self._last_cal_publish >= 2.0:
                    self._publish_calibration()
            except Exception as error:
                print(f"Fehler im Housekeeping: {error}")
            if now - self._last_save >= float(self.params["SAVE_INTERVAL_SEC"]):
                self._last_save = now
                self.save_state()

    def save_state(self):
        with self.lock:
            try:
                self.engine.save_state()
            except Exception as error:
                print(f"Fehler beim Speichern: {error}")

    def shutdown(self):
        with self.lock:
            self.save_state()
            try:
                self._publish(AVAILABILITY_TOPIC, "offline", retain=True)
            except Exception:
                pass
            if self._recorder:
                self._recorder.close()
                self._recorder = None

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------
    def _route(self, topic, payload, retain, now):
        if topic.startswith(TRACKER_ROOT + "/") or topic == MESH_PEERS_TOPIC:
            return
        if topic.startswith("bluecat/registry/") and topic.endswith("/identity"):
            if payload:
                self._handle_identity(payload)
            return
        if topic == TARGET_MAC_STATE_TOPIC:
            # Migration: retained Ziel-MAC eines älteren Trackers übernehmen
            if retain and payload and not self.store.target_mac:
                self._set_target_mac(payload)
            return
        if topic == TARGET_MAC_SET_TOPIC:
            self._set_target_mac(payload)
            return
        if topic == ENGINE_SET_TOPIC:
            self._switch_engine(payload.strip().lower())
            return
        if topic == FLOORPLAN_SET_TOPIC:
            if not retain:
                self._set_floorplan(payload)
            return
        if topic == CALIBRATION_SET_TOPIC:
            if not retain:
                self._handle_calibration(payload)
            return
        if topic == GEOREF_SET_TOPIC:
            if not retain:
                self._set_georef(payload)
            return
        if topic == TUNING_SET_TOPIC:
            if not retain:
                self._handle_tuning(payload)
            return
        if topic == MESH_RELEARN_TOPIC:
            if not retain and self.engine.radio_env is not None:
                self.engine.radio_env.relearn()
                print("Mesh-Baselines werden neu gelernt.")
            return
        if topic.startswith(CONFIG_ROOT + "/"):
            match = CONFIG_SET_RE.match(topic)
            if match:
                self._handle_sensor_config(match.group(1), match.group(2), payload)
            return
        if retain:
            return  # alte Messwerte vom Broker nicht als aktuelle Messung werten
        parsed = PayloadParser.parse(payload)
        if parsed is None:
            return
        if isinstance(parsed, MeshBeacon):
            self._handle_mesh(topic, parsed, now)
        elif isinstance(parsed, SensorReading):
            self._handle_reading(topic, parsed, now)

    def _resolve_sensor(self, payload_sensor_id, topic):
        sensors = self.engine.sensors
        if payload_sensor_id and payload_sensor_id in sensors:
            return payload_sensor_id
        for sid, node in sensors.items():
            if node.topic == topic:
                return sid
        base = topic.rsplit("/", 1)[0]
        for sid, node in sensors.items():
            if node.topic and node.topic.rsplit("/", 1)[0] == base:
                return sid
        return None

    def _sensor_by_mac(self, mac):
        if not mac:
            return None
        try:
            wanted = format_ble_address(mac)
        except ValueError:
            return None
        for sid, cfg in self.store.sensors.items():
            if wanted in cfg.data.get("ble_addresses", []):
                return sid
        return None

    def _handle_mesh(self, topic, beacon: MeshBeacon, now):
        receiver = self._resolve_sensor(beacon.receiver_name, topic)
        if not receiver:
            return
        transmitter = self._sensor_by_mac(beacon.transmitter_mac) or beacon.transmitter_name
        if transmitter and transmitter != receiver:
            self.engine.observe_mesh(receiver, transmitter, beacon.rssi, now)

    def _check_target_tag(self, reading: SensorReading) -> bool:
        if self.target_tag_id is not None:
            return reading.tag_id == str(self.target_tag_id)
        if reading.tag_id is None:
            return self.observed_tag_id is None
        if self.observed_tag_id is None:
            self.observed_tag_id = reading.tag_id
            print(f"BLE-Tag automatisch ausgewählt: {self.observed_tag_id}")
            return True
        return reading.tag_id == self.observed_tag_id

    def _handle_reading(self, topic, reading: SensorReading, now):
        sensor_id = self._resolve_sensor(reading.sensor_id, topic)
        if not sensor_id or not self._check_target_tag(reading):
            return
        self.calibration.on_reading(sensor_id, reading.rssi, reading.present, reading.sample_count)
        node = self.engine.sensors[sensor_id]
        if node.apply_reading(reading.rssi, now, reading.present, reading.sequence, reading.timestamp,
                              sample_count=reading.sample_count):
            if reading.present and node.rssi is not None and \
                    float(node.rssi) >= float(self.params.get("PRESENCE_MIN_RSSI_DBM", -100.0)):
                self._last_strong_sighting = now
            result = self._presence_filter(self.engine.process_tick(now), now)
            self._publish_result(result, now)
            self._publish_live(result, now)

    def _presence_filter(self, result, now):
        """Meldet Lola als weg, wenn seit PRESENCE_LOST_SEC keine ausreichend starke Sichtung kam.

        Einzelne schwache Sichtungen (z. B. von draußen durchs Fenster) halten die Position sonst
        beliebig lange „aktiv“. Die Schätzung im Modell bleibt unberührt – nur die Ausgabe.
        """
        if result.state != "aktiv" or result.x_cm is None:
            return result
        lost_after = float(self.params.get("PRESENCE_LOST_SEC", 30.0))
        if now - self._last_strong_sighting <= lost_after:
            return result
        return dataclasses.replace(result, state="inaktiv", x_cm=None, y_cm=None, accuracy_cm=None,
                                   active_sensors_count=0, room=None)

    # ------------------------------------------------------------------
    def _handle_identity(self, payload):
        try:
            identity = json.loads(payload)
            config, changed, is_new = self.store.upsert_identity(identity)
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            print(f"Ungültige Sensor-Identität: {error}")
            return
        if not changed:
            return
        self.engine.setup_sensors(self.active_sensor_configs())
        for topic, msg, retain in HADiscoveryBuilder.build_sensor(config.sensor_id, config.data):
            self._publish(topic, msg, retain)
        for topic, msg, retain in self._sensor_state_messages(config.sensor_id):
            self._publish(topic, msg, retain)
        self._publish(MESH_PEERS_TOPIC, mesh_peers_payload(self.store.sensors), retain=True)
        print(("Neuer Sensor registriert: " if is_new else "Sensor-Identität aktualisiert: ") + config.sensor_id)

    def _set_target_mac(self, payload):
        try:
            target = ""
            if payload:
                parsed = json.loads(payload) if payload.strip().startswith("{") else payload
                target = parsed.get("target_mac", parsed.get("mac", "")) if isinstance(parsed, dict) else str(parsed)
                target = format_ble_address(target) if target.strip() else ""
            self.store.target_mac = target
            self._publish(TARGET_MAC_STATE_TOPIC, target, retain=True)
            print(f"Zielobjekt-MAC: {target or '(leer)'}")
        except (ValueError, json.JSONDecodeError, AttributeError) as error:
            print(f"Ungültige Zielobjekt-MAC: {error}")

    def _switch_engine(self, kind):
        if kind not in ENGINES:
            print(f"Unbekanntes Modell: {kind}")
            return
        if kind != self.engine_kind:
            self._install_engine(kind)
            print(f"Tracking-Modell gewechselt: {kind}")
        self.store.engine = kind
        self._publish(ENGINE_STATE_TOPIC, kind, retain=True)
        self._publish_tuning()

    def _install_engine(self, kind):
        """Neue Engine aufbauen; Mesh-Baselines und „wer sieht Lola gerade“ übernehmen."""
        old = self.engine
        new = self._make_engine(kind)
        new.radio_env = old.radio_env  # gelernte Mesh-Baselines behalten
        new.setup_sensors(self.active_sensor_configs())
        for sid, node in new.sensors.items():
            previous = old.sensors.get(sid)
            if previous is not None:
                node.last_heard, node.last_seen = previous.last_heard, previous.last_seen
                node.present, node.rssi = previous.present, previous.rssi
        # Das neue Modell braucht einige Messungen; so lange nicht „weg“ melden
        self._inactive_grace_until = self.clock() + 30.0
        self.engine = new
        self.engine_kind = kind
        self.movement.reset()

    # ------------------------------------------------------------------
    # Feintuning
    # ------------------------------------------------------------------
    def _tuning_defaults(self) -> dict:
        defaults = {}
        for key in tuning.KEYS:
            value = self.params.get(key)
            if value is None:
                value = PF_DEFAULTS.get(key)
            if value is not None:
                defaults[key] = float(value)
        return defaults

    def _publish_tuning(self):
        self._publish(TUNING_STATE_TOPIC, tuning.state(self.tuning_defaults, self.tuning, self.engine_kind,
                                                       self._tuning_message), retain=True)

    def _handle_tuning(self, payload):
        try:
            data = json.loads(payload)
            if not isinstance(data, dict):
                raise ValueError("JSON-Objekt erwartet")
            reset = data.pop("reset", None)
            overrides = {} if reset is True else dict(self.tuning)
            if isinstance(reset, list):
                for key in reset:
                    overrides.pop(str(key), None)
            for key, value in tuning.validate(data).items():
                if abs(value - self.tuning_defaults.get(key, math.nan)) < 1e-9:
                    overrides.pop(key, None)  # Standardwert = keine Abweichung
                else:
                    overrides[key] = value
            self._apply_tuning(overrides)
            changed = ", ".join(sorted(data)) or "Standardwerte"
            self._tuning_message = f"Übernommen: {changed}"
            print(f"Feintuning übernommen: {overrides or 'Standard'}")
        except (ValueError, TypeError, OSError, json.JSONDecodeError) as error:
            self._tuning_message = f"Fehler: {error}"
            print(f"Feintuning abgelehnt: {error}")
        self._publish_tuning()

    def _apply_tuning(self, overrides: dict):
        values = dict(self.tuning_defaults)
        values.update(overrides)
        old_particles = int(self.params.get("PF_PARTICLES") or PF_DEFAULTS["PF_PARTICLES"])
        tuning.save(self.store.config_dir, overrides)  # erst speichern – schlägt das fehl, bleibt alles beim Alten
        self.params.update(values)
        self.tuning = dict(overrides)
        self.engine.params.update(values)
        self.movement.speed = float(self.params["MOVING_SPEED_CM_S"])
        self.tomo.tau = float(self.params["RADIO_DYNAMIC_TAU_SEC"])
        if self.engine_kind == "pf" and int(values.get("PF_PARTICLES", old_particles)) != old_particles:
            self._install_engine("pf")  # Partikelzahl ändert die Filtergröße → neu aufbauen

    def _remove_sensor(self, sensor_id):
        config = self.store.sensors.get(sensor_id)
        if config is None:
            return
        # Discovery- und State-Topics des Trackers für diesen Sensor leeren
        for topic, _, _ in HADiscoveryBuilder.build_sensor(sensor_id, config.data):
            self._publish(topic, "", retain=True)
        for topic, _, _ in self._sensor_state_messages(sensor_id):
            self._publish(topic, "", retain=True)
        self.store.remove(sensor_id)
        self.engine.setup_sensors(self.active_sensor_configs())
        self._publish(MESH_PEERS_TOPIC, mesh_peers_payload(self.store.sensors), retain=True)
        print(f"Sensor entfernt: {sensor_id} (Datei unter config/removed/)")

    def _handle_sensor_config(self, sensor_id, field, payload):
        if field == "remove":
            self._remove_sensor(sensor_id)
            return
        try:
            if sensor_id not in self.store.sensors:
                self.store.upsert_identity({"sensor_id": sensor_id, "name": sensor_id})
            value = payload
            if field not in {"ble_mac", "position_x", "position_y", "enabled"} and payload.strip().startswith("{"):
                value = json.loads(payload)
            data = self.store.sensors[sensor_id].data
            axis = None
            if field in {"position_x", "position_y"}:
                axis = field[-1]
                position = list(data.get("pos", [0.0, 0.0]))
                position[0 if axis == "x" else 1] = float(value)
                value, field = position, "position"
            elif field.startswith("calibration_"):
                key = field[len("calibration_"):]
                if key not in CALIBRATION_FIELDS:
                    raise ValueError(f"Unbekanntes Kalibrierfeld: {key}")
                value, field = {key: float(value)}, "calibration"
            elif field == "enabled":
                value = parse_bool(value)
            was_configured = bool(data.get("position_configured"))
            axes = set(data.get("position_axes_set", []))
            config = self.store.update_field(sensor_id, field, value)
            if field == "position" and axis and not was_configured:
                axes.add(axis)
                if axes != {"x", "y"}:
                    # Erst mit beiden Achsen gilt die Position als gesetzt.
                    config.data["position_configured"] = False
                    config.data["position_axes_set"] = sorted(axes)
                else:
                    config.data.pop("position_axes_set", None)
                self.store.save(config)
            elif field == "position":
                config.data.pop("position_axes_set", None)
                self.store.save(config)
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            print(f"Ungültige Konfiguration {sensor_id}/{field}: {error}")
            if sensor_id in self.store.sensors:  # HA auf den gültigen Stand zurücksetzen
                for topic, msg, retain in self._sensor_state_messages(sensor_id):
                    self._publish(topic, msg, retain)
            return
        self.engine.setup_sensors(self.active_sensor_configs())
        for topic, msg, retain in self._sensor_state_messages(sensor_id):
            self._publish(topic, msg, retain)
        if field in {"name", "implementation"}:
            for topic, msg, retain in HADiscoveryBuilder.build_sensor(sensor_id, config.data):
                self._publish(topic, msg, retain)
        if field in {"ble_mac", "enabled"}:
            self._publish(MESH_PEERS_TOPIC, mesh_peers_payload(self.store.sensors), retain=True)
        print(f"Konfiguration aktualisiert: {sensor_id}/{field}")

    # ------------------------------------------------------------------
    # Ausgabe
    # ------------------------------------------------------------------
    def _publish_result(self, result, now, force=False):
        pub = self._pub
        timestamp = datetime.now(timezone.utc).isoformat()
        diagnostics = bool(self.params.get("PUBLISH_DIAGNOSTICS", True))
        if result.state != "aktiv" or result.x_cm is None:
            if pub["state"] == "inaktiv" and not force:
                return
            if now < self._inactive_grace_until and pub["state"] == "aktiv" and not force:
                return  # kurz nach Modellwechsel: letzte Position stehen lassen
            attrs = {
                "active_sensors": 0,
                "inactive_sensors": result.inactive_sensors,
                "engine": self.engine_kind,
                "timestamp_utc": timestamp,
            }
            if diagnostics:
                attrs["sensor_measurements"] = result.sensor_measurements
                attrs["radio_environment"] = result.radio_diagnostics
            self._publish(STATE_TOPIC_GPS, {"state": "inaktiv", "attributes": attrs}, retain=True)
            self._publish(TRACKER_STATE_TOPIC, "not_home", retain=True)
            self._publish(TRACKER_ATTR_TOPIC, {"source": "trilola", "engine": self.engine_kind}, retain=True)
            self._publish(ROOM_STATE_TOPIC, "außer Reichweite", retain=True)
            self._publish(MOVING_STATE_TOPIC, "OFF", retain=True)
            pub.update({"state": "inaktiv", "time": now, "pos": None, "acc": None, "room": None,
                        "moving": False, "tracker_state": "not_home"})
            self.movement.reset()
            if pub.get("logged_inactive") is not True:
                print("Keine gültige Position – Lola außer Reichweite.")
                pub["logged_inactive"] = True
            return

        pub["logged_inactive"] = False
        x, y = float(result.x_cm), float(result.y_cm)
        moving = self.movement.update(now, x, y) if result.updated else bool(pub["moving"])
        accuracy = float(result.accuracy_cm) if result.accuracy_cm is not None else 999.0
        elapsed = now - pub["time"]
        moved = math.inf if pub["pos"] is None else math.hypot(x - pub["pos"][0], y - pub["pos"][1])
        acc_changed = pub["acc"] is None or abs(accuracy - pub["acc"]) > 0.2 * max(pub["acc"], 1.0)
        due = (
            force
            or pub["state"] != "aktiv"
            or (result.updated and elapsed >= float(self.params["PUBLISH_INTERVAL_SEC"])
                and (moved >= float(self.params["PUBLISH_MIN_MOVE_CM"]) or acc_changed))
            or elapsed >= float(self.params["PUBLISH_HEARTBEAT_SEC"])
        )
        if moving != pub["moving"] or force:
            self._publish(MOVING_STATE_TOPIC, "ON" if moving else "OFF", retain=True)
            pub["moving"] = moving
        room = result.room or "unbekannt"
        if room != pub["room"] or force:
            self._publish(ROOM_STATE_TOPIC, room, retain=True)
            pub["room"] = room
        if not due:
            return

        lat, lon = self.frame.to_gps(x, y)
        attrs = {
            "latitude": lat,
            "longitude": lon,
            "x_cm": round(x, 1),
            "y_cm": round(y, 1),
            "gps_accuracy": round(accuracy / 100.0, 2),
            "position_uncertainty_cm": round(accuracy, 1),
            "room": result.room,
            "room_probabilities": result.room_probabilities,
            "moving": moving,
            "engine": self.engine_kind,
            "active_sensors": result.active_sensors_count,
            "contributing_sensors": result.contributing_sensors,
            "rejected_sensors": result.rejected_sensors,
            "inactive_sensors": result.inactive_sensors,
            "estimate_rejected": result.estimate_rejected,
            "rejection_reason": result.rejection_reason,
            "timestamp_utc": timestamp,
        }
        if diagnostics:
            attrs["sensor_measurements"] = result.sensor_measurements
            attrs["radio_environment"] = result.radio_diagnostics
        self._publish(STATE_TOPIC_GPS, {"state": "aktiv", "attributes": attrs}, retain=True)
        if pub["tracker_state"] != "None" or force:
            self._publish(TRACKER_STATE_TOPIC, "None", retain=True)
            pub["tracker_state"] = "None"
        self._publish(TRACKER_ATTR_TOPIC, {
            "latitude": lat, "longitude": lon, "gps_accuracy": round(accuracy / 100.0, 2),
            "x_cm": round(x, 1), "y_cm": round(y, 1), "room": result.room, "moving": moving,
            "source": "trilola", "engine": self.engine_kind,
        }, retain=True)
        pub.update({"state": "aktiv", "time": now, "pos": (x, y), "acc": accuracy})
        if self.params.get("LOG_POSITIONS", True):
            print(f"Lola → X {x:7.1f} | Y {y:7.1f} cm | ±{accuracy:.0f} cm | Raum {room} | "
                  f"{'Bewegung' if moving else 'Ruhe'} | Sensoren {len(result.contributing_sensors)} | {self.engine_kind}")
