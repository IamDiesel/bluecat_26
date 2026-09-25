import base64
import dataclasses
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import bluecat_deploy as bd  # noqa: E402
import bluecat_gui as gui  # noqa: E402
from fake_shelly import FakeShelly  # noqa: E402
from test_deploy import make_fleet  # noqa: E402


def comparable(fleet):
    data = dataclasses.asdict(fleet)
    data.pop("raw")
    data.pop("path")
    return data


def test_dump_roundtrip_keeps_meaning(tmp_path):
    fleet = make_fleet(tmp_path, host="1.2.3.4")
    raw = bd.read_fleet_data(fleet.path)
    raw["node"][0]["name"] = 'Kü"che \\ Ω'
    text = bd.dump_fleet_data(raw)
    again = bd.fleet_from_data(bd._toml().loads(text), fleet.path)
    assert comparable(again) == comparable(bd.fleet_from_data(raw, fleet.path))
    assert again.tracker["settings"] == {"PF_PARTICLES": 800}


def test_write_fleet_makes_backup(tmp_path):
    fleet = make_fleet(tmp_path)
    data = bd.read_fleet_data(fleet.path)
    data["node"] = [n for n in data["node"] if n["id"] != "kunibert"]
    bd.write_fleet_data(fleet.path, data)
    assert os.path.exists(fleet.path + ".bak")
    assert [n.id for n in bd.load_fleet(fleet.path).nodes] == ["ron", "arnd_esp", "shelly_wohnzimmer"]


def test_sanitize_normalizes_input():
    data = {"mqtt": {"host": " 10.0.0.2 ", "port": "1884"}, "tracker": {"origin_lat": "48,5", "origin_lon": "",
            "target_mac": "AA-BB-CC-DD-EE-FF"},
            "node": [{"id": "esp", "type": "esp32", "ble_mac": "AABBCCDDEEFF", "ssh_user": "pi", "_ui": 1}]}
    out = gui.sanitize_fleet(data)
    assert out["mqtt"] == {"host": "10.0.0.2", "port": 1884}
    assert out["tracker"]["origin_lat"] == 48.5 and "origin_lon" not in out["tracker"]
    assert out["tracker"]["target_mac"] == "aa:bb:cc:dd:ee:ff"
    assert out["node"] == [{"id": "esp", "type": "esp32", "ble_mac": "aa:bb:cc:dd:ee:ff"}]


def test_validate_flags_bad_values(tmp_path):
    fleet = make_fleet(tmp_path)
    data = bd.read_fleet_data(fleet.path)
    data["node"][2]["ble_mac"] = "kaputt"
    data["tracker"]["origin_lat"] = "abc"
    problems = bd.validate_fleet(bd.fleet_from_data(data, fleet.path))
    assert any("keine MAC" in p for p in problems)
    assert any("origin_lat" in p for p in problems)


def test_save_fleet_rules(tmp_path):
    fleet = make_fleet(tmp_path)
    app = gui.App(fleet.path)
    data = app.fleet_data()
    rev = app.rev()
    data["wifi"]["ssid"] = ""                       # weich: speichern erlaubt
    code, reply = app.save_fleet(data, rev)
    assert code == 200 and any("[wifi]" in p for p in reply["problems"])
    code, reply = app.save_fleet(data, rev)          # alte Revision → Konflikt
    assert code == 409
    dup = app.fleet_data()
    dup["node"].append(dict(dup["node"][0]))
    code, reply = app.save_fleet(dup, app.rev())    # hart: doppelte ID
    assert code == 400 and any("doppelt" in p for p in reply["problems"])
    app.live._stop()


def test_remove_shelly(tmp_path, monkeypatch):
    fake = FakeShelly()
    try:
        fleet = make_fleet(tmp_path, host=fake.host)
        published = []
        monkeypatch.setattr(bd, "mqtt_publish_many", lambda f, items: published.extend(items))
        monkeypatch.setattr(bd, "mqtt_collect", lambda f, topics, **k: {
            "homeassistant/sensor/bluecat_shelly_wohnzimmer_rssi/config": json.dumps({"unique_id": "bluecat_shelly_wohnzimmer_rssi"}),
            "homeassistant/sensor/bluecat_shelly_wohnzimmer_licht_rssi/config": json.dumps({"unique_id": "bluecat_shelly_wohnzimmer_licht_rssi"}),
            "homeassistant/sensor/bluecat_ron_rssi/config": json.dumps({"unique_id": "bluecat_ron_rssi"})})
        monkeypatch.setattr(bd.time, "sleep", lambda s: None)
        bd.cmd_remove(fleet, "shelly_wohnzimmer")
        topics = [t for t, _, _ in published]
        assert "bluecat/config/sensors/shelly_wohnzimmer/remove/set" in topics
        assert "homeassistant/sensor/bluecat_shelly_wohnzimmer_rssi/config" in topics
        assert "homeassistant/sensor/bluecat_shelly_wohnzimmer_licht_rssi/config" in topics
        assert "homeassistant/sensor/bluecat_ron_rssi/config" not in topics
        assert all(not s["running"] for s in fake.scripts.values())
        assert "shelly_wohnzimmer" not in [n.id for n in bd.load_fleet(fleet.path).nodes]
    finally:
        fake.close()


def test_remove_refuses_tracker_pi(tmp_path):
    fleet = make_fleet(tmp_path)
    with pytest.raises(bd.DeployError):
        bd.cmd_remove(fleet, "ron")


def test_esp_usb_learns_mac(tmp_path, monkeypatch):
    fleet = make_fleet(tmp_path)
    monkeypatch.setattr(bd, "ESP_DIR", str(tmp_path))
    os.makedirs(tmp_path / "src")
    monkeypatch.setattr(bd, "find_pio", lambda: ["pio"])
    monkeypatch.setattr(bd, "run_tee", lambda cmd, **k: (0, "Chip is ESP32\nMAC: 24:6F:28:AA:BB:CC\nok\n"))
    published = []
    monkeypatch.setattr(bd, "mqtt_publish_many", lambda f, items: published.extend(items))
    bd.cmd_esp_usb(fleet, None, "arnd_esp")
    assert bd.load_fleet(fleet.path).node("arnd_esp").ble_mac == "24:6f:28:aa:bb:ce"
    assert ("bluecat/provision/246f28aabbce", json.dumps({"sensor_id": "arnd_esp", "name": "Arnd ESP32"},
                                                         ensure_ascii=False), True) in published


def test_http_guard_and_job(tmp_path):
    fleet = make_fleet(tmp_path)
    app = gui.App(fleet.path)
    server = gui.ThreadingHTTPServer(("127.0.0.1", 0), None)
    port = server.server_address[1]
    server.RequestHandlerClass = gui.make_handler(app, port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    def call(path, body=None, token=app.token):
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                     method="GET" if body is None else "POST",
                                     headers={"X-Bluecat-Token": token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"{}")

    try:
        assert call("/api/fleet", token="falsch")[0] == 401
        code, data = call("/api/fleet")
        assert code == 200 and data["data"]["tracker"]["node"] == "ron"
        code, job = call("/api/jobs", {"action": "check"})
        assert code == 200
        for _ in range(100):
            code, state = call(f"/api/jobs/{job['id']}")
            if state["status"] not in ("wartet", "läuft"):
                break
            time.sleep(0.1)
        assert state["status"] == "ok", state
        assert any("keine Probleme" in line["text"] for line in state["lines"])
        assert call("/api/jobs", {"action": "gibtsnicht"})[0] == 400
    finally:
        server.shutdown()
        app.live._stop()


def test_scan_finds_slow_shelly_via_arp(tmp_path, monkeypatch):
    fleet = make_fleet(tmp_path)
    fleet.shelly["subnet"] = "192.168.10.0/29"
    monkeypatch.setattr(bd, "CACHE_FILE", str(tmp_path / "cache.json"))
    slow = {"ip": "192.168.10.5", "mac": "02:00:00:10:20:0c", "id": "x", "name": "", "model": "S", "gen": 2,
            "ver": "1", "auth": False}

    def probe(ip, timeout=2.5):
        return dict(slow) if str(ip) == "192.168.10.5" and timeout >= 5 else None

    monkeypatch.setattr(bd, "_probe_shelly", probe)
    monkeypatch.setattr(bd, "read_arp_table", lambda: {"02:00:00:10:20:0c": "192.168.10.5"})
    found = bd.scan_shellys(fleet)
    assert [d["ip"] for d in found] == ["192.168.10.5"]
    assert bd.load_cache()["shelly"]["02:00:00:10:20:0c"] == "192.168.10.5"


def test_arp_parsing(monkeypatch):
    windows = ("Schnittstelle: 192.168.10.127 --- 0x7\n"
               "  Internetadresse        Physische Adresse     Typ\n"
               "  192.168.10.1           2c-91-ab-12-34-56     dynamisch\n"
               "  192.168.10.63          02-00-00-0a-5e-20     dynamisch\n"
               "  192.168.10.255         ff-ff-ff-ff-ff-ff     statisch\n"
               "  224.0.0.22            01-00-5e-00-00-16     statisch\n")

    class Res:
        stdout = windows.encode("cp850")

    monkeypatch.setattr(bd.os.path, "exists", lambda p: False)
    monkeypatch.setattr(bd.subprocess, "run", lambda *a, **k: Res())
    table = bd.read_arp_table()
    assert table["02:00:00:0a:5e:20"] == "192.168.10.63"
    assert "ff:ff:ff:ff:ff:ff" not in table and not any(m.startswith("01:00:5e") for m in table)


def test_shelly_upload_survives_firmware_json_parser(tmp_path):
    """Echte Shellys lehnen \\u2013 & Co. ab – Umlaute/Sonderzeichen müssen als UTF-8 ankommen."""
    fake = FakeShelly()
    try:
        fleet = make_fleet(tmp_path, host=fake.host)
        node = fleet.node("shelly_wohnzimmer")
        node.name = "Shelly Wohnzimmer – Küche →"
        code = "// Bluecat BLE-Sensor – für Shelly → test\n" + "let x = 'ä';\n" * 400
        bd.deploy_shelly(fleet, node, code)
        script = next(sc for sc in fake.scripts.values() if sc.get("name") == "bluecat")
        assert script["code"] == code
        assert fake.kvs["bluecat.name"] == "Shelly Wohnzimmer – Küche →"
    finally:
        fake.close()


def test_utf8_chunks_respect_byte_limit():
    text = "aä→" * 1000
    pieces = list(bd.utf8_chunks(text, 1024))
    assert "".join(pieces) == text
    assert all(len(p.encode("utf-8")) <= 1024 for p in pieces)


def test_shelly_script_is_ascii():
    with open(bd.SHELLY_SCRIPT, encoding="utf-8") as handle:
        assert all(ord(c) < 128 for c in handle.read())


# ---------------------------------------------------------------------------
# Karte: Kacheln, Adresssuche, Plan-Speichern
# ---------------------------------------------------------------------------
class _Resp:
    def __init__(self, data):
        self.data = data

    def read(self):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_tile_proxy_caches(tmp_path, monkeypatch):
    monkeypatch.setattr(gui, "TILE_CACHE_DIR", str(tmp_path / "tiles"))
    calls = []

    def fake_urlopen(req, timeout=0):
        calls.append(req.full_url)
        assert "TriLola" in req.headers["User-agent"]
        return _Resp(b"\x89PNG-fake")

    monkeypatch.setattr(gui.urllib.request, "urlopen", fake_urlopen)
    assert gui.fetch_tile("osm", 18, 138000, 90000) == b"\x89PNG-fake"
    assert gui.fetch_tile("osm", 18, 138000, 90000) == b"\x89PNG-fake"
    assert len(calls) == 1 and calls[0] == "https://tile.openstreetmap.org/18/138000/90000.png"
    assert gui.fetch_tile("osm", 25, 0, 0) is None       # Zoom zu groß
    assert gui.fetch_tile("osm", 2, 9, 0) is None         # außerhalb
    assert gui.fetch_tile("evil", 1, 0, 0) is None

    def offline(req, timeout=0):
        raise OSError("offline")

    monkeypatch.setattr(gui.urllib.request, "urlopen", offline)
    monkeypatch.setattr(gui, "TILE_MAX_AGE_SEC", -1)      # abgelaufen, aber offline → alte Kachel
    assert gui.fetch_tile("osm", 18, 138000, 90000) == b"\x89PNG-fake"


def test_geocode(tmp_path, monkeypatch):
    fleet = make_fleet(tmp_path)
    app = gui.App(fleet.path)
    seen = {}

    def fake_urlopen(req, timeout=0):
        seen["url"] = req.full_url
        return _Resp(json.dumps([{"display_name": "Musterstraße 1, Stuttgart", "lat": "48.7", "lon": "9.1"}]).encode())

    monkeypatch.setattr(gui.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(gui.time, "sleep", lambda s: None)
    assert app.geocode("Musterstraße 1 Stuttgart") == [{"name": "Musterstraße 1, Stuttgart", "lat": 48.7, "lon": 9.1}]
    assert "nominatim.openstreetmap.org" in seen["url"] and "Musterstra%C3%9Fe" in seen["url"]
    assert app.geocode("ab") == []
    app.live._stop()


class _FakeClient:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, retain))

        class Info:
            def wait_for_publish(self, timeout=None):
                return True
        return Info()


def _app_with_fake_mqtt(tmp_path, tracker_online=True):
    fleet = make_fleet(tmp_path)
    app = gui.App(fleet.path)
    app.live._stop()
    app.live.client = _FakeClient()
    app.live.connected = True
    app.live.tracker["status"] = "online" if tracker_online else "offline"
    return app


def test_plan_saving_publishes_to_tracker(tmp_path):
    app = _app_with_fake_mqtt(tmp_path)
    assert app.save_positions({"arnd_esp": [120.04, -35.5]}) == 1
    app.save_floorplan({"walls": [{"a": [0, 0], "b": [100, 0]}], "rooms": []})
    result = app.save_georef({"lat": 52.52001, "lon": 13.404954, "bearing_deg": 375, "reference": "ron",
                              "reference_lat": 52.5195, "reference_lon": 13.4050})
    pub = {t: p for t, p, r in app.live.client.published}
    assert pub["bluecat/config/sensors/arnd_esp/position_x/set"] == "120.0"
    assert pub["bluecat/config/sensors/arnd_esp/position_y/set"] == "-35.5"
    assert json.loads(pub["bluecat/config/floorplan/set"])["walls"][0]["b"] == [100, 0]
    georef = json.loads(pub["bluecat/config/tracker/georef/set"])
    assert georef["bearing_deg"] == 15.0 and georef["reference"] == "ron"
    assert result["published"] is True
    tracker = bd.load_fleet(app.fleet_path).tracker
    assert tracker["origin_lat"] == 52.52001 and tracker["origin_bearing_deg"] == 15.0
    assert tracker["reference"] == "ron" and tracker["reference_lon"] == 13.4050
    with pytest.raises(bd.DeployError):
        app.save_positions({"../x": [0, 0]})


def test_plan_saving_needs_tracker(tmp_path):
    app = _app_with_fake_mqtt(tmp_path, tracker_online=False)
    with pytest.raises(bd.DeployError):
        app.save_positions({"ron": [0, 0]})
    result = app.save_georef({"lat": 48.0, "lon": 9.0})
    assert result["published"] is False                   # nur fleet.toml
    assert bd.load_fleet(app.fleet_path).tracker["origin_lat"] == 48.0


def test_live_state_collects_plan_topics():
    live = gui.LiveState()

    class Msg:
        def __init__(self, topic, payload, retain=False):
            self.topic, self.payload, self.retain = topic, payload.encode(), retain

    live._on_message(None, None, Msg("bluecat/config/sensors/ron/position/state", '{"x_cm": 77.1, "y_cm": -627.7, "configured": true}'))
    live._on_message(None, None, Msg("bluecat/config/sensors/ron/enabled/state", "ON"))
    live._on_message(None, None, Msg("bluecat/config/floorplan/state", '{"walls": [], "rooms": []}'))
    live._on_message(None, None, Msg("bluecat/trilola/live", '{"state": "aktiv", "x_cm": 1}'))
    snap = live.plan_snapshot()
    assert snap["positions"]["ron"]["x_cm"] == 77.1 and snap["enabled"]["ron"] is True
    assert snap["plan"]["floorplan"] == {"walls": [], "rooms": []}
    assert snap["plan"]["live"]["x_cm"] == 1 and snap["plan"]["live_at"] is not None


def test_georef_rejects_nan(tmp_path):
    app = _app_with_fake_mqtt(tmp_path)
    with pytest.raises(bd.DeployError):
        app.save_georef({"lat": "nan", "lon": 9.0})
    with pytest.raises(bd.DeployError):
        app.save_georef({"lat": 48.0, "lon": 9.0, "bearing_deg": "inf"})


def test_http_map_routes(tmp_path, monkeypatch):
    fleet = make_fleet(tmp_path)
    app = gui.App(fleet.path)
    monkeypatch.setattr(gui, "fetch_tile", lambda layer, z, x, y: b"\x89PNGtile")
    server = gui.ThreadingHTTPServer(("127.0.0.1", 0), None)
    port = server.server_address[1]
    server.RequestHandlerClass = gui.make_handler(app, port)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def get(path, headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers, error.read()

    try:
        token = {"X-Bluecat-Token": app.token}
        code, _, body = get("/api/plan", token)
        assert code == 200 and "sensors" in json.loads(body)
        code, _, body = get("/api/live", token)
        assert code == 200 and "live" in json.loads(body)
        assert get("/api/plan")[0] == 401
        code, headers, _ = get("/")
        assert code == 200 and "script-src 'self'" in headers["Content-Security-Policy"]
        code, headers, body = get("/vendor/leaflet.js", {"Sec-Fetch-Site": "same-origin"})
        assert code == 200 and b"Leaflet" in body[:400]
        assert get("/vendor/leaflet.js", {"Sec-Fetch-Site": "cross-site"})[0] == 403
        assert get("/vendor/../bluecat_gui.py")[0] == 404
        code, headers, body = get("/tiles/osm/18/1/2.png", {"Sec-Fetch-Site": "same-origin"})
        assert code == 200 and body == b"\x89PNGtile" and headers["Content-Type"] == "image/png"
        assert get("/tiles/osm/18/1/2.png", {"Sec-Fetch-Site": "cross-site"})[0] == 403
        assert get("/tiles/osm/x/1/2.png")[0] == 404
    finally:
        server.shutdown()
        app.live._stop()


def test_map_math_matches_tracker():
    """Die Koordinatenumrechnung der Karte (JS) muss exakt zum Tracker (LocalFrame) passen."""
    import re
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node nicht installiert")
    html = open(os.path.join(os.path.dirname(HERE), "gui", "index.html"), encoding="utf-8").read()
    start = html.index("  function mPerDeg(lat)")
    end = html.index("  function cmPerPixel()")
    js = ("const L = {latLng: (a, b) => ({lat: a, lng: b})}; const M = {geoDraft: null, geo: null};\n" + html[start:end] +
          "\nconst out = [];\nfor (const [lat, lon, b, x, y] of JSON.parse(process.argv[1])) {"
          " M.geo = {lat, lon, bearing: b}; const ll = toLL(x, y); const back = toXY(ll);"
          " const o = originFor(x, y, 48.0, 9.0, b); const again = toLL(x, y, o);"
          " out.push([ll.lat, ll.lng, back[0], back[1], again.lat, again.lng]); }\n"
          "console.log(JSON.stringify(out));")
    cases = [[52.52001, 13.404954, 0, 77.1, -627.7], [52.52001, 13.404954, 15, -224.4, 0.0],
             [52.5, 13.4, 340, 1234.5, -987.6], [-33.9, 151.2, 90, 500, 500]]
    res = json.loads(subprocess.run([node, "-e", js, json.dumps(cases)], capture_output=True, text=True,
                                    check=True).stdout)
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "bt_tracker"))
    from tracker_app import LocalFrame
    for (lat, lon, b, x, y), (jlat, jlon, bx, by, alat, alon) in zip(cases, res):
        plat, plon = LocalFrame(lat, lon, b).to_gps(x, y)
        assert abs(plat - jlat) < 2e-7 and abs(plon - jlon) < 2e-7      # gleiche Formel wie der Tracker
        assert abs(bx - x) < 0.01 and abs(by - y) < 0.01                  # toXY ist die Umkehrung
        assert abs(alat - 48.0) < 1e-9 and abs(alon - 9.0) < 1e-9        # originFor trifft die Referenz


PNG_1x1 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def test_plan_image_store(tmp_path, monkeypatch):
    monkeypatch.setattr(gui, "PLAN_DIR", str(tmp_path / "plan"))
    assert gui.load_plan_image_meta() is None
    with pytest.raises(bd.DeployError):                       # kein Bild
        gui.save_plan_image({"data": "data:image/png;base64," + base64.b64encode(b"<svg>").decode(),
                             "width_px": 1, "height_px": 1})
    with pytest.raises(bd.DeployError):                       # kaputtes base64
        gui.save_plan_image({"data": "%%%", "width_px": 1, "height_px": 1})
    with pytest.raises(bd.DeployError):                       # Lage mit NaN
        gui.save_plan_image({"data": PNG_1x1, "width_px": 1, "height_px": 1, "center": ["nan", 0]})
    meta = gui.save_plan_image({"data": "data:image/png;base64," + PNG_1x1, "width_px": 700, "height_px": 1150,
                                "center": [10, -20], "width_cm": 900, "rotation_deg": -30, "name": "EG.png"})
    assert meta["rotation_deg"] == 330.0 and meta["width_cm"] == 900.0 and meta["original_name"] == "EG.png"
    assert gui.load_plan_image_meta()["file"] == meta["file"]
    moved = gui.update_plan_image({"center": [1, 2], "opacity": 5})
    assert moved["center"] == [1.0, 2.0] and moved["opacity"] == 1.0 and moved["width_px"] == 700
    with pytest.raises(bd.DeployError):
        gui.update_plan_image({"width_cm": 1})               # unter 10 cm
    gui.delete_plan_image()
    assert gui.load_plan_image_meta() is None and not any(p.name.startswith("plan_image") for p in (tmp_path / "plan").iterdir())
    with pytest.raises(bd.DeployError):
        gui.update_plan_image({"center": [0, 0]})


def test_calibration_plan_store(tmp_path, monkeypatch):
    monkeypatch.setattr(gui, "PLAN_DIR", str(tmp_path / "plan"))
    assert gui.load_calibration_plan() == []
    saved = gui.save_calibration_plan([{"id": "k1", "x": 10.04, "y": "-5", "label": "Flur"}])
    assert saved == [{"id": "k1", "x": 10.0, "y": -5.0, "label": "Flur"}] and gui.load_calibration_plan() == saved
    for bad in ([{"id": "../x", "x": 0, "y": 0}], [{"id": "k", "x": "inf", "y": 0}], [{"id": "k"}], "x",
                [{"id": f"k{i}", "x": 0, "y": 0} for i in range(61)]):
        with pytest.raises(bd.DeployError):
            gui.save_calibration_plan(bad)


def test_calibration_command(tmp_path):
    app = _app_with_fake_mqtt(tmp_path)
    app.calibration_command({"cmd": "start", "x": "12.5", "y": -3, "duration_s": 60, "id": "k1", "evil": 1})
    topic, payload, retain = app.live.client.published[-1]
    assert topic == "bluecat/config/calibration/set" and not retain
    assert json.loads(payload) == {"cmd": "start", "x": 12.5, "y": -3.0, "duration_s": 60.0, "id": "k1"}
    app.calibration_command({"cmd": "apply_autocal", "sensors": ["ron"], "n": True, "walls": False})
    assert json.loads(app.live.client.published[-1][1])["sensors"] == ["ron"]
    for bad in ({"cmd": "rm -rf"}, {"cmd": "start", "x": "nan", "y": 0}):
        with pytest.raises(bd.DeployError):
            app.calibration_command(bad)
    app.live.tracker["status"] = "offline"
    with pytest.raises(bd.DeployError):
        app.calibration_command({"cmd": "fit"})


def test_http_plan_image_route(tmp_path, monkeypatch):
    monkeypatch.setattr(gui, "PLAN_DIR", str(tmp_path / "plan"))
    fleet = make_fleet(tmp_path)
    app = gui.App(fleet.path)
    server = gui.ThreadingHTTPServer(("127.0.0.1", 0), None)
    port = server.server_address[1]
    server.RequestHandlerClass = gui.make_handler(app, port)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def call(path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        hdr = {"X-Bluecat-Token": app.token, "Content-Type": "application/json", **(headers or {})}
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers=hdr)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    try:
        assert call("/plan-image", headers={"Sec-Fetch-Site": "same-origin"})[0] == 404
        code, body = call("/api/plan/image", {"data": PNG_1x1, "width_px": 1, "height_px": 1})
        assert code == 200 and json.loads(body)["image"]["width_px"] == 1
        code, body = call("/plan-image", headers={"Sec-Fetch-Site": "same-origin"})
        assert code == 200 and body.startswith(b"\x89PNG")
        assert call("/plan-image", headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
        code, body = call("/api/plan")
        assert json.loads(body)["image"]["width_px"] == 1
        assert call("/api/plan/image", {"data": "AAAA", "width_px": 1, "height_px": 1})[0] == 400
        assert call("/api/calibration", {"cmd": "fit"})[0] == 400           # Tracker offline
    finally:
        server.shutdown()
        app.live._stop()


def test_heights_and_tuning_publish(tmp_path):
    app = _app_with_fake_mqtt(tmp_path)
    assert app.save_heights({"ron": {"height_cm": 180, "floor_cm": ""}, "tom_esp": {"height_cm": None}}) == 2
    pub = [(t, p) for t, p, r in app.live.client.published]
    assert ("bluecat/config/sensors/ron/height/set", "180.0") in pub
    assert ("bluecat/config/sensors/ron/floor/set", "") in pub
    assert ("bluecat/config/sensors/tom_esp/height/set", "") in pub
    for bad in ({"ron": {"height_cm": 900}}, {"ron": {"height_cm": "nan"}}, {"../x": {"height_cm": 1}}, {"ron": 5}):
        with pytest.raises(bd.DeployError):
            app.save_heights(bad)
    app.tuning_command({"PF_MOVE_SPEED_CM_S": "70", "reset": ["TAG_HEIGHT_CM"]})
    topic, payload, retain = app.live.client.published[-1]
    assert topic == "bluecat/config/tracker/tuning/set" and not retain
    assert json.loads(payload) == {"PF_MOVE_SPEED_CM_S": 70.0, "reset": ["TAG_HEIGHT_CM"]}
    for bad in ({}, {"bad key": 1}, {"PF_X": "inf"}, {"reset": "ja"}):
        with pytest.raises(bd.DeployError):
            app.tuning_command(bad)
    app.live.tracker["status"] = "offline"
    with pytest.raises(bd.DeployError):
        app.tuning_command({"PF_MOVE_SPEED_CM_S": 70})


def test_live_state_reads_tracker_version_and_tuning():
    live = gui.LiveState()

    class Msg:
        def __init__(self, topic, payload, retain=True):
            self.topic, self.payload, self.retain = topic, payload.encode(), retain

    live._on_message(None, None, Msg(gui.TRACKER_DISCOVERY_TOPIC, json.dumps({"device": {"sw_version": "2.3.0"}})))
    live._on_message(None, None, Msg("bluecat/config/tracker/tuning/state", '{"params": [], "engine": "pf"}'))
    live._on_message(None, None, Msg("bluecat/config/sensors/ron/position/state",
                                     '{"x_cm": 1, "y_cm": 2, "height_cm": 150, "height_set": true, "configured": true}'))
    snap = live.plan_snapshot()
    assert snap["tracker"]["version"] == "2.3.0" and snap["plan"]["tuning"]["engine"] == "pf"
    assert snap["positions"]["ron"]["height_cm"] == 150
