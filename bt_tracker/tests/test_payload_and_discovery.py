from network.ha_discovery import HADiscoveryBuilder
from network.payload_parser import MeshBeacon, PayloadParser, SensorReading


def test_reading_with_sample_count():
    r = PayloadParser.parse('{"message_type":"tag_rssi","sensor_id":"x","rssi":-71.5,"present":true,"sample_count":3}')
    assert isinstance(r, SensorReading) and r.present and r.sample_count == 3 and r.rssi == -71.5


def test_absent_and_garbage():
    r = PayloadParser.parse('{"message_type":"tag_rssi","rssi":-130,"present":false,"sample_count":0}')
    assert isinstance(r, SensorReading) and not r.present
    assert PayloadParser.parse('{"message_type":"status","x":1}') is None
    assert PayloadParser.parse('{"message_type":"tag_rssi","rssi":"abc"}').present is False
    assert PayloadParser.parse("online") is None
    assert PayloadParser.parse("true") is None


def test_mesh():
    m = PayloadParser.parse('{"message_type":"sensor_beacon","sensor_id":"r","beacon_mac":"AA:BB:CC:DD:EE:FF","rssi":-60}')
    assert isinstance(m, MeshBeacon) and m.transmitter_mac == "aabbccddeeff" and m.receiver_name == "r"


def test_discovery_unique_and_owned():
    configs = {f"s-{i}": {"name": f"S{i}", "implementation": "esp32"} for i in range(5)}
    msgs = HADiscoveryBuilder.build_all(configs)
    uids = [m[1]["unique_id"] for m in msgs]
    assert len(uids) == len(set(uids))
    # Der Tracker publiziert keine RSSI-/Präsenz-Entities mehr (gehören der Firmware)
    assert not any(uid.endswith("_rssi") or uid.endswith("_presence") for uid in uids)
    assert all(m[1].get("availability_topic") == "bluecat/trilola/status" for m in msgs)
    cleanup = HADiscoveryBuilder.legacy_cleanup("s-1")
    assert all(payload == "" and retain for _, payload, retain in cleanup)
