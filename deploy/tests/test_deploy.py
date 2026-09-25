import io
import os
import sys
import tarfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import bluecat_deploy as bd  # noqa: E402
from fake_shelly import FakeShelly  # noqa: E402

FLEET = """
[mqtt]
host = "192.168.10.44"
port = 1883
user = "u"
password = "p'w"
[wifi]
ssid = "Mein \\"WLAN\\""
password = "x"
[esp32]
ota_password = "ota"
[shelly]
password = "{shelly_pw}"
verify_wait_sec = 0
[tracker]
node = "ron"
target_mac = "02-C6-5C-45-2D-0C"
origin_lat = 48.1
origin_lon = 9.2
settings = {{ PF_PARTICLES = 800 }}
[[node]]
id = "ron"
type = "pi"
host = "192.168.10.51"
[[node]]
id = "kunibert"
type = "pi"
host = "192.168.10.43"
ssh_user = "fuchsi"
[[node]]
id = "arnd_esp"
type = "esp32"
name = "Arnd ESP32"
ble_mac = "02:f4:2d:dd:29:08"
[[node]]
id = "shelly_wohnzimmer"
type = "shelly"
name = "Shelly Wohnzimmer"
wifi_mac = "02:00:00:10:20:0c"
legacy_id = "shelly_wohnzimmer_licht"
host = "{host}"
"""


def make_fleet(tmp_path, host="", shelly_pw=""):
    path = tmp_path / "fleet.toml"
    path.write_text(FLEET.format(host=host, shelly_pw=shelly_pw), encoding="utf-8")
    return bd.load_fleet(str(path))


def test_fleet_and_validation(tmp_path):
    fleet = make_fleet(tmp_path, host="1.2.3.4")
    assert fleet.tracker_node.id == "ron"
    assert fleet.target_mac == "02:c6:5c:45:2d:0c"
    assert bd.validate_fleet(fleet) == []
    assert bd.wifi_to_ble_mac("02:00:00:10:20:0c") == "02:00:00:10:20:0e"
    assert bd.wifi_to_ble_mac("02:00:00:10:20:ff") == "02:00:00:10:21:01"


def test_generated_secrets_are_valid(tmp_path):
    fleet = make_fleet(tmp_path, host="1.2.3.4")
    ns = {}
    exec(bd.render_tracker_secrets(fleet), ns)
    assert ns["MQTT_PASSWORD"] == "p'w" and ns["ORIGIN_LAT"] == 48.1 and ns["PF_PARTICLES"] == 800
    ns = {}
    exec(bd.render_sensor_secrets(fleet, fleet.node("kunibert")), ns)
    assert ns["SENSOR_ID"] == "kunibert" and ns["TARGET_MAC"] == "02:c6:5c:45:2d:0c"
    header = bd.render_esp_secrets(fleet)
    assert '#define WIFI_SSID "Mein \\"WLAN\\""' in header


def test_pi_bundle_contents(tmp_path):
    fleet = make_fleet(tmp_path, host="1.2.3.4")
    with tarfile.open(fileobj=io.BytesIO(bd.build_pi_bundle(fleet, fleet.node("ron"))), mode="r:gz") as tar:
        names = set(tar.getnames())
        env = tar.extractfile("deploy.env").read().decode()
    assert {"pi_install.sh", "sensor/bluecat2mqtt.py", "tracker/trilola_tracker.py", "tracker/secrets_tri.py"} <= names
    assert not any(n.startswith("tracker/config/") for n in names)  # Konfiguration des Pi bleibt unangetastet
    assert "ROLE_TRACKER=1" in env
    with tarfile.open(fileobj=io.BytesIO(bd.build_pi_bundle(fleet, fleet.node("kunibert"))), mode="r:gz") as tar:
        assert not any(n.startswith("tracker/") for n in tar.getnames())


@pytest.mark.parametrize("password", ["", "geheim"])
def test_shelly_deploy_replaces_old_script(tmp_path, password):
    fake = FakeShelly(password=password, eco=True, ble=False)
    try:
        fleet = make_fleet(tmp_path, host=fake.host, shelly_pw=password)
        code = "// Bluecat BLE-Sensor für Shelly\n" + "x" * 3000 + "\nprint('ä');\n"
        bd.deploy_shelly(fleet, fleet.node("shelly_wohnzimmer"), code, fix_settings=True)
        script = fake.scripts[1]
        assert script["code"] == code and script["running"] and script["name"] == "bluecat"
        assert len(fake.scripts) == 1  # altes Skript wiederverwendet, kein zweites
        assert fake.kvs["bluecat.sensor_id"] == "shelly_wohnzimmer"
        assert fake.kvs["bluecat.legacy_id"] == "shelly_wohnzimmer_licht"
        assert fake.kvs["bluecat.target_mac"] == "02:c6:5c:45:2d:0c"
        assert fake.config["ble"]["enable"] and not fake.config["sys"]["device"]["eco_mode"]
        assert fake.rebooted  # BLE-Änderung verlangt Neustart
    finally:
        fake.close()


def test_shelly_wrong_device_is_refused(tmp_path):
    fake = FakeShelly(mac="AABBCCDDEEFF")
    try:
        fleet = make_fleet(tmp_path, host=fake.host)
        with pytest.raises(bd.DeployError, match="falsche IP"):
            bd.deploy_shelly(fleet, fleet.node("shelly_wohnzimmer"), "code")
        assert not any(m == "KVS.Set" for m, _ in fake.calls)
    finally:
        fake.close()


def test_shelly_wrong_password(tmp_path):
    fake = FakeShelly(password="richtig")
    try:
        fleet = make_fleet(tmp_path, host=fake.host, shelly_pw="falsch")
        with pytest.raises(bd.DeployError):
            bd.deploy_shelly(fleet, fleet.node("shelly_wohnzimmer"), "code")
    finally:
        fake.close()


def test_provision_messages(tmp_path):
    fleet = make_fleet(tmp_path, host="1.2.3.4")
    items = bd.provision_messages(fleet)
    assert items == [("bluecat/provision/02f42ddd2908", '{"sensor_id": "arnd_esp", "name": "Arnd ESP32"}', True)]


def test_ha_cleanup_classification(tmp_path, monkeypatch):
    fleet = make_fleet(tmp_path, host="1.2.3.4")
    retained = {
        "homeassistant/sensor/bluecat_ron_rssi/config": '{"unique_id": "bluecat_ron_rssi"}',
        "homeassistant/number/bluecat_ron_position_x/config": '{"unique_id": "bluecat_ron_position_x"}',
        "homeassistant/sensor/bluecat_trilola_gps/config": '{"unique_id": "bluecat_trilola_gps_sensor"}',
        "homeassistant/sensor/bluecat_arnd_rssi/config": '{"unique_id": "bluecat_arnd_rssi"}',
        "homeassistant/sensor/bluecat_ron/rssi/config": '{"unique_id": "bluecat_ron_rssi"}',
        "homeassistant/sensor/other/config": '{"unique_id": "irgendwas"}',
    }
    published = []
    monkeypatch.setattr(bd, "mqtt_collect", lambda fleet, topics, **k: retained if topics[0].startswith("homeassistant") else {})
    monkeypatch.setattr(bd, "mqtt_publish_many", lambda f, items: published.extend(items))
    bd.cmd_ha_cleanup(fleet, yes=True)
    removed = {t for t, _, _ in published}
    assert removed == {"homeassistant/sensor/bluecat_arnd_rssi/config", "homeassistant/sensor/bluecat_ron/rssi/config"}


def test_version_tuple():
    assert bd.version_tuple("2.1.0") >= (2, 1)
    assert bd.version_tuple("2.0") < (2, 1)
    assert bd.version_tuple(None) < (2, 1)


def test_cleanup_removes_tracker_orphans(tmp_path, monkeypatch):
    fleet = make_fleet(tmp_path, host="1.2.3.4")
    state = {"bluecat/config/sensors/ron/enabled/state": "ON",
             "bluecat/config/sensors/kunibert_kiosk/enabled/state": "ON"}
    registry = {"bluecat/registry/esp32_877352/identity": '{"sensor_id": "esp32_877352"}',
                "bluecat/registry/ron/identity": '{"sensor_id": "ron"}'}
    published = []

    def collect(fleet, topics, **k):
        if "enabled/state" in topics[0]:
            return state
        if "registry" in topics[0]:
            return registry
        return {}

    monkeypatch.setattr(bd, "mqtt_collect", collect)
    monkeypatch.setattr(bd, "mqtt_publish_many", lambda f, items: published.extend(items))
    monkeypatch.setattr(bd.time, "sleep", lambda s: None)
    bd.cmd_ha_cleanup(fleet, yes=True)
    topics = [t for t, _, _ in published]
    assert "bluecat/config/sensors/kunibert_kiosk/remove/set" in topics
    assert "bluecat/config/sensors/ron/remove/set" not in topics
    assert "bluecat/registry/esp32_877352/identity" in topics
