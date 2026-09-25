import json
import os
import shutil

import pytest

import secrets_tri_dummy as sec
from tracker_app import LocalFrame, TriLolaApp

# Anonymisierte Kopie einer echten Konfiguration (Fantasie-MACs) – unabhängig von bt_tracker/config
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


@pytest.fixture()
def app(tmp_path):
    shutil.copytree(FIXTURE_CONFIG, tmp_path / "config")
    clock = {"t": 0.0}
    pub = Pub()
    application = TriLolaApp(str(tmp_path), sec, pub, config_dir=str(tmp_path / "config"), clock=lambda: clock["t"])
    application._test_clock = clock
    application._test_pub = pub
    return application


def reading(sid, rssi, seq, present=True, count=2):
    return json.dumps({"message_type": "tag_rssi", "sensor_id": sid, "rssi": rssi, "present": present,
                       "sequence": seq, "sample_count": count})


def test_connect_publishes_initial_states(app):
    app.on_connect()
    pub = app._test_pub
    assert pub.last("bluecat/trilola/status") == "online"
    assert pub.last("bluecat/trilola/gps/state")["state"] == "inaktiv"
    assert pub.last("bluecat/trilola/room/state") == "außer Reichweite"
    assert pub.last("bluecat/config/tracker/engine/state") == "pf"
    for sid in app.store.sensors:
        assert pub.last(f"bluecat/config/sensors/{sid}/position/state") is not None
        assert pub.last(f"bluecat/config/sensors/{sid}/enabled/state") in ("ON", "OFF")
    # Discovery für deaktivierte/unpositionierte Sensoren ebenfalls vorhanden
    assert any(t == "homeassistant/switch/bluecat_kunibert_kiosk_enabled/config" for t, _, _ in pub.msgs)


def test_retained_tag_messages_are_ignored(app):
    app.on_message("bluecat/ron/sensor/state", reading("ron", -60, 1), retain=True)
    assert app.engine.sensors["ron"].last_seen is None
    app.on_message("bluecat/ron/sensor/state", reading("ron", -60, 2), retain=False)
    assert app.engine.sensors["ron"].last_seen is not None


def test_identity_heartbeat_keeps_engine_state(app):
    for k in range(6):
        app._test_clock["t"] = k
        for sid in ("ron", "kunibert", "tom_esp"):
            app.on_message(f"bluecat/{sid}/sensor/state", reading(sid, -70, k), now=k)
    particles = app.engine.particles.copy()
    node = app.engine.sensors["ron"]
    ident = {"sensor_id": "ron", "name": app.store.sensors["ron"].data["name"],
             "implementation": app.store.sensors["ron"].data["implementation"],
             "state_topic": app.store.sensors["ron"].data["topic"],
             "availability_topic": app.store.sensors["ron"].data["availability_topic"],
             "ble_mac": app.store.sensors["ron"].data["ble_addresses"][0], "timestamp": 99}
    app.on_message("bluecat/registry/ron/identity", json.dumps(ident), retain=True)
    assert app.engine.sensors["ron"] is node
    assert (app.engine.particles == particles).all()


def test_enabled_off_removes_sensor(app):
    app.on_message("bluecat/config/sensors/ron/enabled/set", "OFF")
    assert app.store.sensors["ron"].data["enabled"] is False
    assert "ron" not in app.engine.sensors
    assert app._test_pub.last("bluecat/config/sensors/ron/enabled/state") == "OFF"
    app.on_message("bluecat/config/sensors/ron/enabled/set", "ON")
    assert "ron" in app.engine.sensors


def test_position_needs_both_axes_for_new_sensor(app):
    app.on_message("bluecat/registry/neu/identity", json.dumps({"sensor_id": "neu", "ble_mac": "11:22:33:44:55:66"}))
    assert "neu" not in app.engine.sensors
    app.on_message("bluecat/config/sensors/neu/position_x/set", "120")
    assert "neu" not in app.engine.sensors
    app.on_message("bluecat/config/sensors/neu/position_y/set", "-50")
    assert "neu" in app.engine.sensors
    assert list(app.engine.sensors["neu"].pos) == [120.0, -50.0]


def test_engine_switch_keeps_mesh_learning(app):
    env = app.engine.radio_env
    app.on_message("bluecat/config/tracker/engine/set", "legacy")
    assert app.engine_kind == "legacy" and app.engine.radio_env is env
    assert app.store.engine == "legacy"


def test_target_mac_migration_and_set(app):
    app.on_message("bluecat/config/target_mac/state", "02:C6:5C:45:2D:0C", retain=True)
    assert app.store.target_mac == "02:c6:5c:45:2d:0c"
    app.on_message("bluecat/config/target_mac/set", "aa-bb-cc-dd-ee-ff")
    assert app.store.target_mac == "aa:bb:cc:dd:ee:ff"


def test_calibration_field_update(app):
    app.on_message("bluecat/config/sensors/ron/calibration_sigma_db/set", "5.5")
    assert app.engine.sensors["ron"].sigma_db == 5.5
    app.on_message("bluecat/config/sensors/ron/calibration_n_factor/set", "-1")  # ungültig
    assert app.store.sensors["ron"].data["n_factor"] > 0


def test_local_frame_bearing():
    north = LocalFrame(48.0, 10.0, 0.0)
    east = LocalFrame(48.0, 10.0, 90.0)
    lat, lon = north.to_gps(0, 10000)
    assert lat > 48.0 and abs(lon - 10.0) < 1e-6
    lat, lon = east.to_gps(0, 10000)  # +y zeigt nach Osten
    assert lon > 10.0 and abs(lat - 48.0) < 1e-6


def test_engine_switch_does_not_report_absence(app):
    for k in range(8):
        app._test_clock["t"] = k
        for sid in ("ron", "kunibert", "tom_esp", "shelly_wohnzimmer"):
            app.on_message(f"bluecat/{sid}/sensor/state", reading(sid, -70, k), now=k)
        app.housekeeping(k)
    assert app._test_pub.last("bluecat/trilola/gps/state")["state"] == "aktiv"
    app._test_clock["t"] = 8.5
    app.on_message("bluecat/config/tracker/engine/set", "legacy")
    app.housekeeping(9)
    assert app._test_pub.last("bluecat/trilola/gps/state")["state"] == "aktiv"


def test_remove_sensor(app):
    app.on_message("bluecat/config/sensors/kunibert_kiosk/remove/set", "PRESS")
    assert "kunibert_kiosk" not in app.store.sensors
    assert app._test_pub.last("homeassistant/switch/bluecat_kunibert_kiosk_enabled/config") == ""
    import os
    assert os.listdir(os.path.join(app.store.config_dir, "removed"))


# ---------------------------------------------------------------------------
# Grundriss, Kartenbezug, Live-Daten (Rollout-Oberfläche → Tracker)
# ---------------------------------------------------------------------------
def test_floorplan_set_is_saved_and_used(app):
    import os
    plan = {"walls": [{"a": [0, 0], "b": [0, 500], "attenuation_db": 7}],
            "rooms": [{"name": "Wohnzimmer", "polygon": [[-1000, -1000], [1000, -1000], [1000, 1000], [-1000, 1000]]}],
            "junk": 1}
    app.on_message("bluecat/config/floorplan/set", json.dumps(plan))
    state = app._test_pub.last("bluecat/config/floorplan/state")
    assert state["rooms"][0]["name"] == "Wohnzimmer" and "junk" not in state
    assert state["walls"][0]["attenuation_db"] == 7.0
    assert os.path.exists(app.params["FLOORPLAN_FILE"])
    assert app.engine.floorplan.has_rooms and app.floorplan.room_name([0, 0]) == "Wohnzimmer"
    # retained set-Nachrichten (z. B. nach Broker-Neustart) werden ignoriert
    app.on_message("bluecat/config/floorplan/set", json.dumps({"walls": [], "rooms": []}), retain=True)
    assert app.floorplan.has_rooms


def test_floorplan_invalid_keeps_old(app):
    app.on_message("bluecat/config/floorplan/set", '{"walls": [{"a": [0, "x"], "b": [1, 1]}]}')
    assert app._test_pub.last("bluecat/config/floorplan/state") == {"default_wall_db": 5.0, "walls": [], "rooms": []}


def test_georef_set_persists_and_moves_gps(app, tmp_path):
    app.on_message("bluecat/config/tracker/georef/set",
                   json.dumps({"lat": 52.52001, "lon": 13.404954, "bearing_deg": 12.5, "reference": "kunibert"}))
    state = app._test_pub.last("bluecat/config/tracker/georef/state")
    assert state["lat"] == 52.52001 and state["bearing_deg"] == 12.5 and state["reference"] == "kunibert"
    assert app.frame.to_gps(0, 0) == (52.52001, 13.404954)
    # Neustart: georef.json gewinnt, solange secrets unverändert sind
    again = TriLolaApp(str(tmp_path), sec, Pub(), config_dir=str(tmp_path / "config"))
    assert again.frame.lat == 52.52001 and again.georef["reference"] == "kunibert"

    class NewSecrets:
        ORIGIN_LAT = 50.0
        ORIGIN_LON = 8.0
        ORIGIN_BEARING_DEG = 0.0

    rolled = TriLolaApp(str(tmp_path), NewSecrets, Pub(), config_dir=str(tmp_path / "config"))
    assert rolled.frame.lat == 50.0  # neuer Rollout mit anderen Werten gewinnt


def test_georef_invalid_is_rejected(app):
    app.on_message("bluecat/config/tracker/georef/set", '{"lat": 123, "lon": 9}')
    assert app.frame.lat == sec.ORIGIN_LAT


def test_live_topic_with_cloud_and_rate_limit(app):
    clock, pub = app._test_clock, app._test_pub
    for i in range(12):
        clock["t"] = 1.0 + i * 0.5
        for sid, rssi in (("ron", -62), ("shelly_wohnzimmer", -60), ("tom_esp", -75), ("kunibert", -80)):
            app.on_message(f"bluecat/{sid}/sensor/state", reading(sid, rssi, i + 1))
        app.housekeeping()
    lives = [p for t, p, r in pub.msgs if t == "bluecat/trilola/live"]
    assert 4 <= len(lives) <= 8  # ~1/s über 5.5 s
    last = lives[-1]
    assert last["state"] == "aktiv" and "x_cm" in last and len(last["cloud"]) == 120
    assert last["sensors"]["ron"]["present"] is True
    assert not any(r for t, p, r in pub.msgs if t == "bluecat/trilola/live")


def test_georef_nan_and_corrupt_file(app, tmp_path):
    app.on_message("bluecat/config/tracker/georef/set", '{"lat": 48.7, "lon": 9.1, "bearing_deg": NaN}')
    assert app.frame.lat == sec.ORIGIN_LAT and app.frame.bearing == 0.0
    (tmp_path / "config" / "georef.json").write_text("[1, 2]")
    again = TriLolaApp(str(tmp_path), sec, Pub(), config_dir=str(tmp_path / "config"))
    assert again.frame.lat == sec.ORIGIN_LAT


def test_floorplan_nan_rejected(app):
    app.on_message("bluecat/config/floorplan/set", '{"default_wall_db": NaN, "walls": [], "rooms": []}')
    assert app._test_pub.last("bluecat/config/floorplan/state") == {"default_wall_db": 5.0, "walls": [], "rooms": []}
    app.on_message("bluecat/config/floorplan/set",
                   '{"walls": [{"a": [0, 0], "b": [1, 1], "attenuation_db": Infinity}], "rooms": []}')
    assert app._test_pub.last("bluecat/config/floorplan/state")["walls"] == []


def test_connect_publishes_plan_topics(app):
    app.on_connect()
    pub = app._test_pub
    assert pub.last("bluecat/config/floorplan/state") is not None
    assert pub.last("bluecat/config/tracker/georef/state")["lat"] == sec.ORIGIN_LAT
    radio = pub.last("bluecat/trilola/radio_map")
    assert radio is not None and radio["nx"] > 0 and radio["ny"] > 0
