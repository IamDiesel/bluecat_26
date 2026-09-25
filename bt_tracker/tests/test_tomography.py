"""Funk-Tomographie, Mesh-Selbstkalibrierung und Kalibrierplan."""
import json
import os
import math
import shutil

import numpy as np
import pytest

import secrets_tri_dummy as sec
from radio_environment import RadioEnvironmentModel
from radio_tomography import RadioTomography, autocal_suggestions, fit_mesh_model, segments_cross, wall_arrays
from tracker_app import TriLolaApp

# Anonymisierte Kopie einer echten Konfiguration (Fantasie-MACs) – unabhängig von bt_tracker/config
FIXTURE_CONFIG = os.path.join(os.path.dirname(__file__), "fixtures", "config")

POS = {"arnd_esp": (-224.4, 0.0), "kunibert": (0.0, 0.0), "ron": (77.1, -627.7),
       "shelly_schlafzimmer": (-86.5, -520.5), "shelly_sz_lichtschrank": (-213.0, -224.1),
       "shelly_wohnzimmer": (-71.3, -626.2), "shelly_wohnzimmer_dim_sb": (-219.8, -875.9), "tom_esp": (-4.7, -829.5)}
TX = ["arnd_esp", "kunibert", "ron", "tom_esp"]
FLOOR = {"default_wall_db": 5, "walls": [{"a": [-400, -150], "b": [200, -150], "attenuation_db": 5},
                                          {"a": [-400, -570], "b": [200, -570], "attenuation_db": 5}], "rooms": []}
TRUE_WALLS = [9.0, 3.0]


def synthetic_links(seed=1, n=2.6):
    rng = np.random.default_rng(seed)
    gain = {k: rng.normal(0, 4) for k in POS}
    mean = np.mean(list(gain.values()))
    gain = {k: v - mean for k, v in gain.items()}
    a_tx = {k: -45 + rng.normal(0, 3) for k in TX}
    a_w, b_w, _ = wall_arrays(FLOOR)
    links = []
    for r in POS:
        for t in TX:
            if r == t:
                continue
            p, q = np.array(POS[r]), np.array(POS[t])
            d = max(np.hypot(*(p - q)) / 100, 0.3)
            walls = segments_cross(p[None], q[None], a_w, b_w)[0] @ np.array(TRUE_WALLS)
            links.append((r, t, a_tx[t] + gain[r] - 10 * n * math.log10(d) - walls + rng.normal(0, 1.0), 3.0))
    return links, gain


def cell(tomo, rows, x, y):
    g = tomo.grid
    return rows[int((y - g.lower[1]) // g.cell)][int((x - g.lower[0]) // g.cell)] or 0.0


def test_mesh_fit_recovers_walls_gains_and_n():
    links, gain = synthetic_links()
    fit = fit_mesh_model(links, POS, FLOOR)
    est = [w["estimate_db"] for w in fit["walls"]]
    assert abs(est[0] - 9.0) < 2.0 and abs(est[1] - 3.0) < 2.0
    assert abs(fit["n"] - 2.6) < 0.6
    assert max(abs(fit["gains_db"][k] - gain[k]) for k in POS) < 1.5


def test_tomography_static_and_dynamic():
    links, _ = synthetic_links()
    env = RadioEnvironmentModel(POS, baseline_learning_samples=5)
    rng = np.random.default_rng(2)
    for r, t, b, _ in links:
        for i in range(5):
            env.observe(r, t, b + rng.normal(0, 0.3), 1.0 + i)
    for i in range(6):  # Strecke Lichtschrank ↔ Ron wird 10 dB schwächer
        for r, t, b, _ in links:
            extra = -10.0 if {r, t} == {"shelly_sz_lichtschrank", "ron"} else 0.0
            env.observe(r, t, b + extra + rng.normal(0, 0.3), 10.0 + i)
    tomo = RadioTomography()
    data = tomo.update(env, 16.0, FLOOR)
    assert data["static_basis"] == "grundriss" and data["links_learned"] == len(links)
    static = data["static"]
    # die 9-dB-Wand bei y = −150 ist deutlich sichtbar, zwischen den Wänden wenig
    assert cell(tomo, static, -100, -150) > 3 * max(cell(tomo, static, -100, -380), 0.05)
    dyn = data["dynamic"]

    def along(rows):
        return max(cell(tomo, rows, -213 + f * 290, -224 - f * 403) for f in np.linspace(0.1, 0.9, 17))

    on_link = along(dyn)
    assert on_link > 0.5 and on_link > 5 * max(cell(tomo, dyn, -150, -850), 0.01)
    # Abklingen statt Verschwinden: ohne Störung 60 s später noch etwa 60 %
    for i in range(20):
        for r, t, b, _ in links:
            env.observe(r, t, b + rng.normal(0, 0.3), 20.0 + i)
    tomo.update(env, 76.0, FLOOR)
    later = along(tomo.grid.to_rows(tomo.dynamic))
    assert 0.3 * on_link < later < 0.8 * on_link


def test_autocal_keeps_level_and_orders_gains():
    links, gain = synthetic_links()
    configs = {k: {"tx_power": -60.0, "n_factor": 3.0} for k in POS}
    auto = autocal_suggestions(links, POS, configs, FLOOR)
    sugg = {k: v["suggested_tx_power"] for k, v in auto["sensors"].items()}
    assert abs(np.median(list(sugg.values())) - (-60.0)) < 0.5
    best, worst = max(gain, key=gain.get), min(gain, key=gain.get)
    assert sugg[best] > sugg[worst]


@pytest.fixture()
def app(tmp_path):
    shutil.copytree(FIXTURE_CONFIG, tmp_path / "config")
    clock = {"t": 0.0}

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

    pub = Pub()
    application = TriLolaApp(str(tmp_path), sec, pub, config_dir=str(tmp_path / "config"), clock=lambda: clock["t"])
    application._test_clock = clock
    application._test_pub = pub
    return application


def test_calibration_plan_end_to_end(app):
    """Punkte messen → auswerten → übernehmen, mit synthetischem Halsband (P0 −62, n 2.4)."""
    clock, pub = app._test_clock, app._test_pub
    app.on_message("bluecat/config/calibration/set", '{"cmd": "clear"}')
    positions = {sid: np.array(cfg.data["pos"]) for sid, cfg in app.store.sensors.items()
                 if cfg.data.get("position_configured")}
    points = [(-100, -50), (50, -300), (-250, -400), (0, -700), (-150, -800), (100, -500)]
    seq = 0
    rng = np.random.default_rng(3)
    for k, (x, y) in enumerate(points):
        app.on_message("bluecat/config/calibration/set", json.dumps({"cmd": "start", "x": x, "y": y, "duration_s": 30,
                                                                    "id": f"p{k}"}))
        assert pub.last("bluecat/config/calibration/state")["active"]["id"] == f"p{k}"
        for step in range(15):
            clock["t"] += 2.0
            seq += 1
            for sid, p in positions.items():
                d = max(np.hypot(p[0] - x, p[1] - y) / 100, 0.3)
                rssi = -62 - 24 * math.log10(d) + rng.normal(0, 1.5)
                app.on_message(f"bluecat/{sid}/sensor/state", json.dumps(
                    {"message_type": "tag_rssi", "sensor_id": sid, "rssi": round(rssi, 1), "present": True,
                     "sample_count": 3, "sequence": seq}))
            app.housekeeping()
        state = pub.last("bluecat/config/calibration/state")
        assert state["active"] is None and len(state["points"]) == k + 1
    app.on_message("bluecat/config/calibration/set", '{"cmd": "fit"}')
    fit = pub.last("bluecat/config/calibration/state")["fit"]
    assert abs(fit["globals"]["n_factor"] - 2.4) < 0.35
    for sid, cal in fit["sensors"].items():
        assert abs(cal["tx_power"] - (-62)) < 3.0, (sid, cal)
    app.on_message("bluecat/config/calibration/set", '{"cmd": "apply_fit"}')
    assert abs(app.store.sensors["ron"].data["tx_power"] - (-62)) < 3.0
    assert "übernommen" in pub.last("bluecat/config/calibration/state")["message"]


def test_calibration_errors_are_reported(app):
    app.on_message("bluecat/config/calibration/set", '{"cmd": "clear"}')
    app.on_message("bluecat/config/calibration/set", '{"cmd": "fit"}')
    assert pub_message(app).startswith("Fehler")
    app.on_message("bluecat/config/calibration/set", '{"cmd": "start", "x": "NaN", "y": 0}')
    assert pub_message(app).startswith("Fehler")
    app.on_message("bluecat/config/calibration/set", '{"cmd": "gibtsnicht"}')
    assert pub_message(app).startswith("Fehler")


def pub_message(app):
    return app._test_pub.last("bluecat/config/calibration/state")["message"]


def test_autocal_via_mqtt_updates_walls_and_tx(app):
    app.on_message("bluecat/config/floorplan/set", json.dumps(FLOOR))
    env = app.engine.radio_env
    env.relearn()  # die kopierte Beispielkonfiguration bringt echte Baselines mit
    links, _ = synthetic_links()
    for r, t, b, _ in links:
        for i in range(30):
            env.observe(r, t, b, 1.0 + i)
    app.on_message("bluecat/config/calibration/set", '{"cmd": "autocal"}')
    state = app._test_pub.last("bluecat/config/calibration/state")
    assert state["autocal"]["links"] >= 20 and len(state["autocal"]["walls"]) == 2
    app.on_message("bluecat/config/calibration/set", '{"cmd": "apply_autocal", "sensors": true, "n": true, "walls": true}')
    walls = app._test_pub.last("bluecat/config/floorplan/state")["walls"]
    assert abs(walls[0]["attenuation_db"] - 9.0) < 2.0
    assert app.store.sensors["ron"].data["calibration_status"] == "auto"
    radio = None
    app.housekeeping(now=100.0)
    radio = app._test_pub.last("bluecat/trilola/radio_map")
    assert radio["static"] is not None and "dynamic" in radio
