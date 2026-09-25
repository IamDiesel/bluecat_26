import json

import pytest

from config_manager import ConfigStore, mesh_peers_payload, parse_bool


def write(path, name, data):
    (path / name).write_text(json.dumps(data), encoding="utf-8")


def test_parse_bool():
    assert parse_bool("OFF") is False
    assert parse_bool("false") is False
    assert parse_bool("0") is False
    assert parse_bool("ON") is True
    assert parse_bool(1) is True
    with pytest.raises(ValueError):
        parse_bool("vielleicht")


def test_position_configured_rules(tmp_path):
    write(tmp_path, "a.json", {"pos": [0.0, 0.0]})
    write(tmp_path, "b.json", {"pos": [10.0, 5.0]})
    write(tmp_path, "c.json", {"pos": [0.0, 0.0], "position_configured": True})
    write(tmp_path, "floorplan.json", {"walls": []})
    write(tmp_path, "tracker_state.json", {"target_mac": ""})
    store = ConfigStore(str(tmp_path))
    sensors = store.load()
    assert set(sensors) == {"a", "b", "c"}
    assert sensors["a"].data["position_configured"] is False
    assert sensors["b"].data["position_configured"] is True
    assert sensors["c"].data["position_configured"] is True


def test_identity_heartbeat_is_noop_and_keeps_disabled(tmp_path):
    store = ConfigStore(str(tmp_path))
    store.load()
    identity = {"sensor_id": "s1", "name": "S1", "state_topic": "bluecat/s1/sensor/state", "ble_mac": "AA:BB:CC:DD:EE:01"}
    _, changed, new = store.upsert_identity(identity)
    assert changed and new
    store.update_field("s1", "enabled", "OFF")
    mtime = (tmp_path / "s1.json").stat().st_mtime_ns
    _, changed, new = store.upsert_identity({**identity, "timestamp": 123, "enabled": True})
    assert not changed and not new
    assert store.sensors["s1"].data["enabled"] is False
    assert (tmp_path / "s1.json").stat().st_mtime_ns == mtime


def test_tracker_state_persists(tmp_path):
    store = ConfigStore(str(tmp_path))
    store.target_mac = "aa:bb:cc:dd:ee:ff"
    store.engine = "legacy"
    again = ConfigStore(str(tmp_path))
    assert again.target_mac == "aa:bb:cc:dd:ee:ff"
    assert again.engine == "legacy"
    with pytest.raises(ValueError):
        again.engine = "quatsch"


def test_mesh_peers_payload_is_stable(tmp_path):
    store = ConfigStore(str(tmp_path))
    store.upsert_identity({"sensor_id": "s1", "ble_mac": "aa:bb:cc:dd:ee:01"})
    assert mesh_peers_payload(store.sensors) == mesh_peers_payload(store.sensors)


def test_duplicate_ble_mac_rejected(tmp_path):
    store = ConfigStore(str(tmp_path))
    store.upsert_identity({"sensor_id": "a", "ble_mac": "aa:bb:cc:dd:ee:01"})
    store.upsert_identity({"sensor_id": "b", "ble_mac": "aa:bb:cc:dd:ee:02"})
    with pytest.raises(ValueError):
        store.update_field("b", "ble_mac", "AA:BB:CC:DD:EE:01")


def test_user_name_survives_identity(tmp_path):
    store = ConfigStore(str(tmp_path))
    store.upsert_identity({"sensor_id": "a", "name": "Firmware-Name"})
    store.update_field("a", "name", "Mein Name")
    store.upsert_identity({"sensor_id": "a", "name": "Firmware-Name"})
    assert store.sensors["a"].data["name"] == "Mein Name"
