"""Sensorhöhen (3D-Abstand) und Feintuning per MQTT."""

import json
import math
import os
import shutil

import numpy as np
import pytest

import secrets_tri_dummy as sec
import tuning
from calibration_model import fit_pooled
from config_manager import ConfigStore, mount_height_cm, sensor_z_cm
from core.pf_engine import ParticleEngine
from core.sensor_node import SensorNode
from tracker_app import TriLolaApp

FIXTURE_CONFIG = os.path.join(os.path.dirname(__file__), "fixtures", "config")


class Pub:
    def __init__(self):
        self.msgs = []

    def publish(self, topic, payload, retain=False, qos=1):
        self.msgs.append((topic, payload, retain))

    def subscribe(self, topic, qos=1):
        pass

    def last(self, topic):
        for t, p, r in reversed(self.msgs):
            if t == topic:
                return p
        return None


def make_app(tmp_path):
    if not (tmp_path / "config").exists():
        shutil.copytree(FIXTURE_CONFIG, tmp_path / "config")
    pub = Pub()
    app = TriLolaApp(str(tmp_path), sec, pub, config_dir=str(tmp_path / "config"), clock=lambda: 0.0)
    app._test_pub = pub
    return app


# ---------------------------------------------------------------------------
# Höhen
# ---------------------------------------------------------------------------
def test_mount_height_defaults_and_floor_offset():
    assert mount_height_cm({"implementation": "shelly"}) == 105.0
    assert mount_height_cm({"implementation": "raspberry_pi"}) == 100.0
    assert mount_height_cm({"implementation": "shelly", "height_cm": 180}) == 180.0
    # gleiches Stockwerk: nur Montagehöhe; anderes Stockwerk: Differenz der Fußböden dazu
    assert sensor_z_cm({"height_cm": 120}, 600.0) == 120.0
    assert sensor_z_cm({"height_cm": 120, "floor_cm": 300.0}, 600.0) == pytest.approx(-180.0)


def test_expected_rssi_uses_slant_distance():
    engine = ParticleEngine({"PF_PARTICLES": 10, "TAG_HEIGHT_CM": 25.0, "RANDOM_SEED": 1})
    node = SensorNode("s", {"pos": [0.0, 0.0], "tx_power": -60.0, "n_factor": 2.0, "z_cm": 105.0})
    below = engine._expected_rssi(node, np.array([[0.0, 0.0]]), use_cache=False)[0]
    far = engine._expected_rssi(node, np.array([[400.0, 0.0]]), use_cache=False)[0]
    # direkt darunter: 80 cm Höhenunterschied statt 30-cm-Untergrenze
    assert below == pytest.approx(-60.0 - 20.0 * math.log10(0.8), abs=1e-6)
    assert far == pytest.approx(-60.0 - 20.0 * math.log10(math.hypot(4.0, 0.8)), abs=1e-6)
    flat = SensorNode("f", {"pos": [0.0, 0.0], "tx_power": -60.0, "n_factor": 2.0})  # ohne z: wie bisher 2D
    assert engine._expected_rssi(flat, np.array([[400.0, 0.0]]), use_cache=False)[0] == pytest.approx(-72.04, abs=0.01)


def test_legacy_horizontal_distance():
    node = SensorNode("s", {"pos": [0.0, 0.0], "z_cm": 125.0})
    assert node.horizontal_distance_cm(500.0, 25.0) == pytest.approx(math.sqrt(500 ** 2 - 100 ** 2))
    assert node.horizontal_distance_cm(50.0, 25.0) == pytest.approx(10.0)  # näher als der Höhenunterschied


def test_calibration_fit_with_heights_recovers_model():
    rng = np.random.default_rng(3)
    sensors = {"a": ([0, 0], 220.0), "b": ([500, 0], 105.0), "c": ([0, 600], 40.0), "d": ([500, 600], 180.0)}
    points = [(x, y) for x in (60, 180, 300, 440) for y in (80, 300, 520)]
    samples = {sid: [] for sid in sensors}
    for sid, (pos, z) in sensors.items():
        for pt in points:
            d = math.sqrt((pt[0] - pos[0]) ** 2 + (pt[1] - pos[1]) ** 2 + (z - 25.0) ** 2) / 100.0
            mu = -62.0 - 24.0 * math.log10(max(d, 0.3))
            samples[sid].append((pt, list(mu + rng.normal(0, 1.0, 12))))
    positions = {sid: pos for sid, (pos, _) in sensors.items()}
    heights = {sid: z for sid, (_, z) in sensors.items()}
    cfg3d, glob3d = fit_pooled(samples, positions, sensor_heights=heights, point_height_cm=25.0)
    cfg2d, glob2d = fit_pooled(samples, positions)
    assert glob3d["n_factor"] == pytest.approx(2.4, abs=0.1)
    assert all(abs(c["tx_power"] + 62.0) < 1.0 for c in cfg3d.values())
    rms = lambda cfg: np.mean([c["calibration_rms_db"] for c in cfg.values()])  # noqa: E731
    assert rms(cfg3d) < 0.6 * rms(cfg2d)  # 2D erklärt die Nahpunkte schlechter


def test_height_via_mqtt_and_state(tmp_path):
    app = make_app(tmp_path)
    baselines = len(app.engine.radio_env.baseline_links())
    app.on_message("bluecat/config/sensors/ron/height/set", "180")
    state = app._test_pub.last("bluecat/config/sensors/ron/position/state")
    assert state["height_cm"] == 180.0 and state["height_set"] is True and state["z_cm"] == 180.0
    # ron wurde früher eben (2D) kalibriert → Halsband-Modell bleibt eben, Mesh kennt die Höhe
    assert state["height_active"] is False and app.engine.sensors["ron"].z_cm is None
    assert app.engine.radio_env.sensor_heights["ron"] == 180.0
    assert len(app.engine.radio_env.baseline_links()) == baselines  # Höhe nachtragen verwirft keine Baselines
    # neue Kalibrierung mit Höhen → ab jetzt schräg
    app.on_message("bluecat/config/sensors/ron/calibration/set",
                   json.dumps({"tx_power": -61.0, "calibration_geometry": "3d"}))
    assert app.engine.sensors["ron"].z_cm == 180.0
    assert app._test_pub.last("bluecat/config/sensors/ron/position/state")["height_active"] is True
    # unkalibrierter Sensor nutzt die Höhe sofort
    tom = app.store.sensors["tom_esp"].data
    assert (tom.get("calibration_status") in ("calibrated", "auto")) or app.engine.sensors["tom_esp"].z_cm is not None
    app.on_message("bluecat/config/sensors/ron/height/set", "9999")      # ungültig → bleibt
    assert app.store.sensors["ron"].data["height_cm"] == 180.0
    app.on_message("bluecat/config/sensors/ron/height/set", "")          # leer → Standardhöhe
    state = app._test_pub.last("bluecat/config/sensors/ron/position/state")
    assert state["height_set"] is False and state["height_cm"] == 100.0
    # Sensor im Stockwerk darunter: Wohnung 600 cm, Sensor-Fußboden 300 cm
    app.on_message("bluecat/config/floorplan/set", json.dumps({"walls": [], "rooms": [], "floor_elevation_cm": 600}))
    app.on_message("bluecat/config/sensors/ron/floor/set", "300")
    assert app.engine.sensors["ron"].z_cm == pytest.approx(-200.0)
    app.on_message("bluecat/config/sensors/ron/floor/set", "")
    assert app.engine.sensors["ron"].z_cm == 100.0
    # nach Neustart noch da
    app.on_message("bluecat/config/sensors/ron/height/set", "150")
    again = make_app(tmp_path)
    assert again.engine.sensors["ron"].z_cm == 150.0


def test_settings_files_are_not_sensors(tmp_path):
    shutil.copytree(FIXTURE_CONFIG, tmp_path / "config")
    (tmp_path / "config" / "georef.json").write_text('{"lat": 1, "lon": 2}')
    (tmp_path / "config" / "tuning.json").write_text('{"PF_STUDENT_NU": 6}')
    (tmp_path / "config" / "sonstiges.json").write_text('{"foo": 1}')
    store = ConfigStore(str(tmp_path / "config"))
    store.load()
    assert not {"georef", "tuning", "sonstiges"} & set(store.sensors)


def test_discovery_has_height_entity(tmp_path):
    app = make_app(tmp_path)
    app.on_connect()
    topics = {t for t, _, _ in app._test_pub.msgs}
    assert "homeassistant/number/bluecat_ron_height/config" in topics


# ---------------------------------------------------------------------------
# Feintuning
# ---------------------------------------------------------------------------
def test_tuning_set_reset_and_persist(tmp_path):
    app = make_app(tmp_path)
    app.on_connect()
    state = app._test_pub.last("bluecat/config/tracker/tuning/state")
    params = {p["key"]: p for p in state["params"]}
    assert params["PF_MOVE_SPEED_CM_S"]["default"] == 90.0 and not params["PF_MOVE_SPEED_CM_S"]["overridden"]
    assert params["TAG_HEIGHT_CM"]["value"] == 25.0

    app.on_message("bluecat/config/tracker/tuning/set", json.dumps({"PF_MOVE_SPEED_CM_S": 60, "TAG_HEIGHT_CM": 12}))
    assert app.engine.params["PF_MOVE_SPEED_CM_S"] == 60 and app.params["TAG_HEIGHT_CM"] == 12
    state = app._test_pub.last("bluecat/config/tracker/tuning/state")
    assert state["message"].startswith("Übernommen")
    assert {p["key"] for p in state["params"] if p["overridden"]} == {"PF_MOVE_SPEED_CM_S", "TAG_HEIGHT_CM"}

    # ungültig: nichts ändert sich
    app.on_message("bluecat/config/tracker/tuning/set", json.dumps({"PF_STUDENT_NU": 999}))
    assert app._test_pub.last("bluecat/config/tracker/tuning/state")["message"].startswith("Fehler")
    app.on_message("bluecat/config/tracker/tuning/set", json.dumps({"EVIL": 1}))
    assert "EVIL" not in app.params

    # Partikelzahl → Filter neu, Mesh bleibt
    env = app.engine.radio_env
    app.on_message("bluecat/config/tracker/tuning/set", json.dumps({"PF_PARTICLES": 800}))
    assert app.engine.n == 800 and app.engine.radio_env is env

    # übersteht Neustart
    again = make_app(tmp_path)
    assert again.params["PF_MOVE_SPEED_CM_S"] == 60 and again.engine.n == 800

    # teilweise und ganz zurücksetzen
    again.on_message("bluecat/config/tracker/tuning/set", json.dumps({"reset": ["TAG_HEIGHT_CM"]}))
    assert again.params["TAG_HEIGHT_CM"] == 25.0 and again.params["PF_MOVE_SPEED_CM_S"] == 60
    again.on_message("bluecat/config/tracker/tuning/set", json.dumps({"reset": True}))
    assert again.tuning == {} and again.engine.n == 1500
    assert not (tmp_path / "config" / "tuning.json").exists()


def test_tuning_schema_defaults_within_limits():
    from core.pf_engine import DEFAULTS
    from tracker_app import DEFAULT_PARAMS
    for key, _g, _l, _u, low, high, _s, _h, _e in tuning.SCHEMA:
        default = DEFAULT_PARAMS.get(key, DEFAULTS.get(key))
        assert default is not None and low <= default <= high, key


def test_tuning_file_robust(tmp_path):
    (tmp_path / "tuning.json").write_text("[1, 2]")
    assert tuning.load(str(tmp_path)) == {}
    (tmp_path / "tuning.json").write_text('{"PF_STUDENT_NU": 999, "PF_MOVE_SPEED_CM_S": 70, "UNBEKANNT": 1}')
    assert tuning.load(str(tmp_path)) == {"PF_MOVE_SPEED_CM_S": 70.0}


def test_engine_switch_republishes_tuning(tmp_path):
    app = make_app(tmp_path)
    app.on_message("bluecat/config/tracker/engine/set", "legacy")
    assert app._test_pub.last("bluecat/config/tracker/tuning/state")["engine"] == "legacy"


# ---------------------------------------------------------------------------
# Anwesenheit: schwache Sichtungen halten Lola nicht ewig „aktiv“
# ---------------------------------------------------------------------------
def _reading(sid, rssi, seq, present=True):
    return json.dumps({"message_type": "tag_rssi", "sensor_id": sid, "rssi": rssi, "present": present,
                       "sequence": seq, "sample_count": 2})


def _feed(app, t, rssi_by_sensor, seq):
    for sid, rssi in rssi_by_sensor.items():
        app.on_message(f"bluecat/{sid}/sensor/state", _reading(sid, rssi, seq), now=t)


def test_presence_lost_after_only_weak_sightings(tmp_path):
    clock = {"t": 0.0}
    if not (tmp_path / "config").exists():
        shutil.copytree(FIXTURE_CONFIG, tmp_path / "config")
    pub = Pub()
    app = TriLolaApp(str(tmp_path), sec, pub, config_dir=str(tmp_path / "config"), clock=lambda: clock["t"])
    app.on_message("bluecat/config/tracker/tuning/set",
                   json.dumps({"PRESENCE_MIN_RSSI_DBM": -88, "PRESENCE_LOST_SEC": 20}))
    strong = {"ron": -65, "kunibert": -70, "tom_esp": -72}
    for k in range(8):                       # gut zu sehen → aktiv
        clock["t"] = float(k)
        _feed(app, float(k), strong, k)
    assert pub.last("bluecat/trilola/tracker/state") != "not_home"
    assert pub.last("bluecat/trilola/gps/state")["state"] == "aktiv"
    for k in range(8, 40):                   # nur noch ein schwacher Sensor sieht sie ab und zu
        clock["t"] = float(k)
        app.on_message("bluecat/ron/sensor/state", _reading("ron", -95, k), now=float(k))
        app.housekeeping(now=float(k))
    assert pub.last("bluecat/trilola/gps/state")["state"] == "inaktiv"
    assert pub.last("bluecat/trilola/room/state") == "außer Reichweite"
    for k in range(40, 44):                  # wieder stark → sofort wieder da
        clock["t"] = float(k)
        _feed(app, float(k), strong, k)
    assert pub.last("bluecat/trilola/gps/state")["state"] == "aktiv"


def test_presence_default_keeps_old_behaviour(tmp_path):
    clock = {"t": 0.0}
    shutil.copytree(FIXTURE_CONFIG, tmp_path / "config")
    pub = Pub()
    app = TriLolaApp(str(tmp_path), sec, pub, config_dir=str(tmp_path / "config"), clock=lambda: clock["t"])
    for k in range(8):
        clock["t"] = float(k)
        _feed(app, float(k), {"ron": -65, "kunibert": -70, "tom_esp": -72}, k)
    for k in range(8, 60):                   # Standard -100 dBm: schwache Sichtungen zählen weiter
        clock["t"] = float(k)
        app.on_message("bluecat/ron/sensor/state", _reading("ron", -95, k), now=float(k))
        app.housekeeping(now=float(k))
    assert pub.last("bluecat/trilola/gps/state")["state"] == "aktiv"
