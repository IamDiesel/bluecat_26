"""Home-Assistant-App: Paket, Ingress, Tracker im Add-on und Umzug."""

import io
import json
import os
import socket
import sys
import tarfile
import threading
import urllib.error
import urllib.request

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import bluecat_deploy as bd  # noqa: E402
import bluecat_gui as gui  # noqa: E402
from test_deploy import make_fleet  # noqa: E402


def bundle_members():
    data = bd.build_addon_bundle("9.9.9-test")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        return {m.name: m for m in tar.getmembers()}, tar, data


def test_addon_bundle_contents_and_privacy():
    data = bd.build_addon_bundle("9.9.9-test")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        members = {m.name: m for m in tar.getmembers()}
        config = tar.extractfile(members["trilola/config.yaml"]).read().decode()
        run_sh = members["trilola/run.sh"]
        texts = {n: tar.extractfile(m).read() for n, m in members.items() if m.isfile()}
    assert 'version: "9.9.9-test"' in config and "ingress: true" in config
    assert run_sh.mode & 0o111
    for needed in ("trilola/Dockerfile", "trilola/icon.png", "trilola/app/deploy/bluecat_gui.py",
                   "trilola/app/deploy/gui/index.html", "trilola/app/deploy/gui/vendor/leaflet.js",
                   "trilola/app/deploy/remote/pi_install.sh", "trilola/app/deploy/requirements-gui.txt",
                   "trilola/app/bt_tracker/trilola_tracker.py", "trilola/app/bt_tracker/tuning.py",
                   "trilola/app/bt_tracker/core/pf_engine.py", "trilola/app/bt_tracker/requirements-tracker.txt",
                   "trilola/app/bt_sensor/esp32_embedded/platformio.ini",
                   "trilola/app/bt_sensor/esp32_embedded/src/main.cpp",
                   "trilola/app/bt_sensor/raspberry_pi_unix/bluecat2mqtt.py",
                   "trilola/app/bt_sensor/shelly_script/bluecat_shelly.js"):
        assert needed in members, needed
    for name in members:
        base = os.path.basename(name)
        assert base not in ("fleet.toml", "fleet.toml.bak", ".cache.json", ".gui_token", "secrets_tri.py",
                            "secrets_blue.py", "secrets_ble.h", "error.txt"), name
        assert "/config/" not in name and "/.pio/" not in name and "/tests/" not in name and "/plan/" not in name, name
        assert "/." not in name.replace("trilola/.", ""), name   # keine versteckten Ordner (.tracker_move, .venv …)
    # keine echten Zugangsdaten der lokalen fleet.toml im Paket
    local = os.path.join(bd.HERE, "fleet.toml")
    if os.path.exists(local):
        fleet = bd.load_fleet(local)
        secrets_ = [v for v in (fleet.wifi.get("password"), fleet.mqtt.get("password"),
                                fleet.esp32.get("ota_password"), fleet.shelly.get("password")) if v and len(v) >= 6]
        for value in secrets_:
            assert not any(value.encode() in body for body in texts.values())


def test_tracker_config_pack_unpack_is_safe(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "ron.json").write_text('{"a": 1}')
    (src / "notes.txt").write_text("x")
    packed = bd._pack_json_dir(str(src))
    with tarfile.open(fileobj=io.BytesIO(packed), mode="r:gz") as tar:
        assert tar.getnames() == ["ron.json"]
    # bösartiges Archiv: Pfade und Links werden ignoriert
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, body in (("../evil.json", b"{}"), ("sub/x.json", b"{}"), ("ok.json", b"{}")):
            ti = tarfile.TarInfo(name)
            ti.size = len(body)
            tar.addfile(ti, io.BytesIO(body))
        link = tarfile.TarInfo("link.json")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tar.addfile(link)
    dest = tmp_path / "dest"
    assert bd._unpack_json_tar(buf.getvalue(), str(dest)) == ["ok.json"]
    assert sorted(os.listdir(dest)) == ["ok.json"] and not (tmp_path / "evil.json").exists()


def test_tracker_in_addon_fleet(tmp_path):
    fleet = make_fleet(tmp_path)
    data = bd.read_fleet_data(fleet.path)
    data["tracker"]["node"] = bd.TRACKER_ADDON
    fleet = bd.write_fleet_data(fleet.path, data)
    assert fleet.tracker_in_addon and not any(n.tracker for n in fleet.nodes)
    assert not [p for p in bd.validate_fleet(fleet) if "Tracker" in p]
    bundle = bd.build_pi_bundle(fleet, fleet.node("ron"))
    with tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz") as tar:
        env = tar.extractfile("deploy.env").read().decode()
        assert "ROLE_TRACKER=0" in env and not any(n.startswith("tracker/") for n in tar.getnames())
    secrets_py = bd.render_tracker_secrets(fleet)
    assert "MQTT_BROKER = '192.168.10.44'" in secrets_py or 'MQTT_BROKER = "192.168.10.44"' in secrets_py


def test_pi_install_stops_moved_tracker():
    script = open(os.path.join(bd.HERE, "remote", "pi_install.sh"), encoding="utf-8").read()
    assert "disable --now trilola.service" in script


def _local_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return None


def test_ingress_guard(tmp_path, monkeypatch):
    monkeypatch.setattr(gui, "INGRESS_PEERS", {"127.0.0.1"})  # im Test spielt localhost den Supervisor
    fleet = make_fleet(tmp_path)
    app = gui.App(fleet.path)
    server = gui.ThreadingHTTPServer(("0.0.0.0", 0), None)
    port = server.server_address[1]
    server.RequestHandlerClass = gui.make_handler(app, port, ingress=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def get(host, path, headers=None):
        req = urllib.request.Request(f"http://{host}:{port}{path}", headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if path == "/":
                    assert "frame-ancestors 'self'" in resp.headers["Content-Security-Policy"]  # HA-iframe
                return resp.status, resp.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    try:
        # Supervisor (hier: localhost) ohne Token und mit beliebigem Host-Header
        code, body = get("127.0.0.1", "/api/state", {"Host": "homeassistant.local:8123"})
        assert code == 200 and "addon" in json.loads(body)
        code, body = get("127.0.0.1", "/")
        assert code == 200 and b'src="vendor/leaflet.js"' in body
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/scan", data=b"{}", method="POST",
                                     headers={"Content-Type": "text/plain"})
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(req, timeout=10)
        assert err.value.code == 403   # kein JSON → kein Formular-CSRF
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/scan", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json", "Sec-Fetch-Site": "cross-site"})
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(req, timeout=10)
        assert err.value.code == 403
        ip = _local_ip()
        if ip and not ip.startswith("127."):
            assert get(ip, "/api/state")[0] == 403   # alle anderen: abgewiesen
    finally:
        server.shutdown()
        app.live._stop()


def test_frontend_uses_relative_urls():
    html = open(gui.UI_FILE, encoding="utf-8").read()
    assert 'href="/vendor' not in html and 'src="/vendor' not in html
    assert '"/tiles/' not in html and '"/plan-image' not in html
    assert 'path.replace(/^\\//, "")' in html


def test_tracker_move_steps(tmp_path):
    fleet = make_fleet(tmp_path)   # Tracker auf ron
    app = gui.App(fleet.path)
    try:
        app.local_tracker = gui.LocalTracker(str(tmp_path / "trk"))
        steps, title = app._tracker_move_steps(fleet, "ron", bd.TRACKER_ADDON, lambda nid: {"S": nid})
        assert steps[0]["args"] == ["tracker-stop", "ron"] and steps[0]["critical"] and steps[0]["env"] == {"S": "ron"}
        assert steps[1]["args"][:3] == ["tracker-config", "pull", "ron"]
        assert steps[1]["args"][4] == os.path.join(bd.TRACKER_HOME, "config")
        assert steps[2]["label"] == "fleet.toml umstellen" and "call" in steps[3] and "Home Assistant" in title
        # fleet.toml wird erst im Job umgestellt
        assert bd.load_fleet(fleet.path).tracker["node"] == "ron"

        class J:
            def log(self, *a):
                pass
        steps[2]["call"](J())
        moved = bd.load_fleet(fleet.path)
        assert moved.tracker_in_addon

        steps, _ = app._tracker_move_steps(moved, bd.TRACKER_ADDON, "kunibert", lambda nid: {})
        assert "call" in steps[0] and steps[1]["label"] == "fleet.toml umstellen"
        assert steps[2]["args"][:3] == ["tracker-config", "push", "kunibert"] and steps[-1]["args"] == ["pi", "kunibert"]

        steps, _ = app._tracker_move_steps(fleet, "ron", "kunibert", lambda nid: {})   # Pi → Pi
        assert [s.get("args", ["call"])[0] for s in steps] == ["tracker-stop", "tracker-config", "call",
                                                               "tracker-config", "pi"]
        with pytest.raises(bd.DeployError):   # veralteter Ausgangsort
            app._tracker_move_steps(moved, "ron", "kunibert", lambda nid: {})
        with pytest.raises(bd.DeployError):
            app._tracker_move_steps(moved, bd.TRACKER_ADDON, bd.TRACKER_ADDON, lambda nid: {})
        app.local_tracker = None
        with pytest.raises(bd.DeployError):   # außerhalb der App kein Umzug in die App
            app._tracker_move_steps(fleet, "ron", bd.TRACKER_ADDON, lambda nid: {})
    finally:
        app.live._stop()


def test_local_tracker_stop_escalates(tmp_path, monkeypatch):
    lt = gui.LocalTracker(str(tmp_path))
    import subprocess
    lt.proc = subprocess.Popen([sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"],
                               start_new_session=True)
    lt.wanted = True
    lt.stop(timeout=1.0)
    assert lt.proc.poll() is not None and not lt.wanted


def test_ssh_uses_home_key(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh" / "id_ed25519").write_text("x")
    fleet = make_fleet(tmp_path)
    opts = bd._ssh_opts(fleet.node("ron"))
    assert opts[opts.index("-i") + 1] == str(tmp_path / ".ssh" / "id_ed25519")
    assert "UserKnownHostsFile=" + str(tmp_path / ".ssh" / "known_hosts") in opts


def test_stage_and_export_without_fleet(tmp_path, monkeypatch):
    target = os.path.join(bd.ADDON_SRC_DIR, "app")
    existed = os.path.isdir(target)
    assert bd.main(["--fleet", str(tmp_path / "fehlt.toml"), "ha-addon", "--export", str(tmp_path / "out")]) == 0
    assert os.path.isfile(tmp_path / "out" / "trilola" / "config.yaml")
    if not existed:
        return  # Staging nicht im Repo erzwingen, wenn es vorher nicht da war
    assert bd.main(["--fleet", str(tmp_path / "fehlt.toml"), "ha-addon", "--stage"]) == 0
    assert os.path.isfile(os.path.join(target, "deploy", "bluecat_gui.py"))
    assert not os.path.exists(os.path.join(target, "deploy", "ha_addon"))


FAKE_HA = r"""#!/bin/sh
# Fake-„ha“: protokolliert Aufrufe; MODE steuert CLI-Variante und App-Zustand
echo "$*" >> "$HA_LOG"
case "$1" in
  apps) [ "$HA_CLI" = apps ] || { echo "Error: unknown command \"apps\" for \"ha\"" >&2; exit 1; } ;;
  addons) ;;
  store|supervisor) [ "$2" = reload ] && exit 0 ;;
esac
if [ "$2" = info ]; then
  case "$HA_STATE" in
    missing) echo '{"name": "TriLola", "version": null, "update_available": false}' ;;
    update) echo '{"name": "TriLola", "version": "2.3.0-1", "update_available": true}' ;;
    current) echo '{"name": "TriLola", "version": "2.4.0-1", "update_available": false}' ;;
  esac
fi
exit 0
"""


@pytest.mark.parametrize("shell", ["sh", "bash", "zsh"])
@pytest.mark.parametrize("cli", ["apps", "addons"])
@pytest.mark.parametrize("state,expected", [("missing", "install"), ("update", "update"), ("current", "rebuild")])
def test_ha_install_script_all_shells(tmp_path, shell, cli, state, expected):
    import shutil as _shutil
    import subprocess
    if not _shutil.which(shell):
        pytest.skip(f"{shell} nicht installiert")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "ha"
    fake.write_text(FAKE_HA)
    fake.chmod(0o755)
    log = tmp_path / "ha.log"
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "HA_LOG": str(log), "HA_CLI": cli, "HA_STATE": state}
    res = subprocess.run([shell, "-c", bd.ha_install_script(start=True)], env=env, capture_output=True, text=True,
                         timeout=60)
    assert res.returncode == 0, res.stdout + res.stderr
    calls = log.read_text().splitlines()
    assert f"{cli} {expected} local_trilola" in calls
    assert f"{cli} restart local_trilola" in calls
    assert not any(c.startswith("apps ") and c != "apps --help" for c in calls) or cli == "apps"


# ---------------------------------------------------------------------------
# Passwörter nie im Protokoll; OTA-Passwort wechseln ohne ESP32 auszusperren
# ---------------------------------------------------------------------------
def test_redact_hides_all_fleet_passwords():
    data = {"mqtt": {"password": "mqttGeheim"}, "wifi": {"password": "wlanGeheim"},
            "esp32": {"ota_password": "Neu!2026", "ota_password_previous": ["Alt'Pass"]},
            "shelly": {"password": "abc"}}  # zu kurz → bleibt (sonst würde jedes „abc“ geschwärzt)
    secrets = bd.fleet_secrets(data)
    line = "Options: {'auth': 'Neu!2026', 'old': 'Alt\\'Pass'} mqttGeheim wlanGeheim abc"
    out = bd.redact(line, secrets)
    for secret in ("Neu!2026", "Alt\\'Pass", "Alt'Pass", "mqttGeheim", "wlanGeheim"):
        assert secret not in out
    assert "abc" in out and out.count("***") == 4


def test_remember_previous_ota_password():
    old = {"esp32": {"ota_password": "eins"}}
    new = {"esp32": {"ota_password": "zwei"}}
    gui.remember_previous_ota_password(old, new)
    assert new["esp32"]["ota_password_previous"] == ["eins"]
    newer = {"esp32": {"ota_password": "drei", "ota_password_previous": ["eins"]}}
    gui.remember_previous_ota_password(new, newer)
    assert newer["esp32"]["ota_password_previous"] == ["zwei", "eins"]
    back = {"esp32": {"ota_password": "eins", "ota_password_previous": ["zwei", "eins"]}}
    gui.remember_previous_ota_password(newer, back)       # zurück auf ein altes: nicht doppelt
    assert back["esp32"]["ota_password_previous"] == ["drei", "zwei"]
    same = {"esp32": {"ota_password": "eins"}}
    gui.remember_previous_ota_password({"esp32": {"ota_password": "eins"}}, same)
    assert "ota_password_previous" not in same


def test_ota_falls_back_to_previous_password_and_redacts(tmp_path, monkeypatch, capsys):
    fleet = make_fleet(tmp_path, host="1.2.3.4")
    fleet.esp32["ota_password"] = "NeuesPw1"
    fleet.esp32["ota_password_previous"] = ["AltesPw1"]
    fleet.nodes[2].host = "192.168.10.129"
    monkeypatch.setattr(bd, "write_esp_secrets", lambda f: None)
    monkeypatch.setattr(bd, "cmd_esp_provision", lambda f: None)
    monkeypatch.setattr(bd, "read_identities", lambda f: {"_raw": {}})
    monkeypatch.setattr(bd, "find_pio", lambda: ["pio"])
    monkeypatch.setattr(bd, "run", lambda *a, **k: None)
    tried = []

    def fake_upload(cmd, secrets, env=None):
        pw = env["BLUECAT_OTA_PASSWORD"]
        tried.append(pw)
        text = bd.redact(f"Options: {{'auth': '{pw}'}}", secrets)
        print(text)
        return (0, text) if pw == "AltesPw1" else (1, text + "\nAuthentication Failed")

    monkeypatch.setattr(bd, "run_redacted", fake_upload)
    bd.cmd_esp_ota(fleet, ["arnd_esp"])
    assert tried == ["NeuesPw1", "AltesPw1"]
    out = capsys.readouterr().out
    assert "NeuesPw1" not in out and "AltesPw1" not in out and "aktualisiert" in out


def test_ota_no_response_message(tmp_path, monkeypatch, capsys):
    fleet = make_fleet(tmp_path, host="1.2.3.4")
    fleet.nodes[2].host = "192.168.10.129"
    monkeypatch.setattr(bd, "write_esp_secrets", lambda f: None)
    monkeypatch.setattr(bd, "cmd_esp_provision", lambda f: None)
    monkeypatch.setattr(bd, "read_identities", lambda f: {"_raw": {}, "arnd_esp": {"ip": "192.168.10.77"}})
    monkeypatch.setattr(bd, "find_pio", lambda: ["pio"])
    monkeypatch.setattr(bd, "run", lambda *a, **k: None)
    monkeypatch.setattr(bd, "run_redacted", lambda cmd, s, env=None: (1, "No response from the ESP"))
    with pytest.raises(bd.DeployError):
        bd.cmd_esp_ota(fleet, ["arnd_esp"])
    out = capsys.readouterr().out
    assert "keine Antwort von 192.168.10.129" in out and "zuletzt gemeldet: 192.168.10.77" in out


def test_run_redacted_streams_and_hides(tmp_path):
    code, out = bd.run_redacted([sys.executable, "-c", "print('auth=Geheim123'); print('ok')"], ["Geheim123"])
    assert code == 0 and "Geheim123" not in out and "auth=***" in out and "ok" in out


def test_pi_install_powers_bluetooth_for_sensor():
    script = open(os.path.join(bd.HERE, "remote", "pi_install.sh"), encoding="utf-8").read()
    bt = script.index("rfkill unblock bluetooth")
    assert bt < script.index("write_unit bluecat-sensor.service")      # vor dem Start des Sensors
    assert "bluetoothctl power on" in script and "Powered: yes" in script
    assert "disable-bt" in script                                        # Hinweis, falls BT abgeschaltet ist


def test_pi_install_prefers_usb_bluetooth_stick(tmp_path):
    """USB-Stick erkannt → Firmware, eingebauter Chip aus (Pi 4: disable-bt, falsches -pi5 wird korrigiert), Neustart."""
    script = open(os.path.join(bd.HERE, "remote", "pi_install.sh"), encoding="utf-8").read()
    start = script.index("REBOOT_NEEDED=0")
    end = script.index("# Bluetooth einschalten")
    block = script[start:end].replace("/sys/bus/usb/devices", str(tmp_path / "usb")) \
        .replace("/proc/device-tree/model", str(tmp_path / "model")).replace("/boot/firmware/config.txt", str(tmp_path / "config.txt"))
    dev = tmp_path / "usb" / "1-1.3:1.0"
    dev.mkdir(parents=True)
    for name, value in (("bInterfaceClass", "e0"), ("bInterfaceSubClass", "01"), ("bInterfaceProtocol", "01")):
        (dev / name).write_text(value + "\n")
    (tmp_path / "model").write_bytes(b"Raspberry Pi 4 Model B Rev 1.5\0")
    (tmp_path / "config.txt").write_text("[all]\ndtoverlay=disable-bt-pi5\n")
    harness = ("set -euo pipefail\nlog(){ echo \"LOG $*\"; }\nwarn(){ echo \"WARN $*\"; }\n"
               "sudo(){ if [ \"$1\" = apt-get ] || [ \"$1\" = systemctl ]; then echo \"SUDO $*\"; else \"$@\"; fi; }\n"
               "dpkg(){ return 1; }\nsystemctl(){ return 1; }\nROLE_SENSOR=1\nBLUETOOTH=auto\nSTAMP=x\n" + block +
               "echo REBOOT=$REBOOT_NEEDED\n")
    import subprocess
    out = subprocess.run(["bash", "-c", harness], capture_output=True, text=True, check=True).stdout
    assert "SUDO apt-get install -y -qq firmware-realtek" in out and "REBOOT=1" in out
    assert (tmp_path / "config.txt").read_text() == "[all]\ndtoverlay=disable-bt\n"
    out = subprocess.run(["bash", "-c", harness], capture_output=True, text=True, check=True).stdout
    assert "REBOOT=0" in out                                            # zweiter Lauf: nichts mehr zu tun
    (dev / "bInterfaceClass").write_text("08\n")                         # kein Bluetooth-Stick
    (tmp_path / "config.txt").write_text("[all]\n")
    out = subprocess.run(["bash", "-c", harness], capture_output=True, text=True, check=True).stdout
    assert "REBOOT=0" in out and (tmp_path / "config.txt").read_text() == "[all]\n"


def test_sensor_unit_unblocks_bluetooth_before_start(tmp_path):
    script = open(os.path.join(bd.HERE, "remote", "pi_install.sh"), encoding="utf-8").read()
    func = script[script.index("write_unit() {"):script.index("# Alte Sensor-Dienste")]
    pre = script[script.index("    BT_PRE=\"\""):script.index("    write_unit bluecat-sensor.service")]
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for tool in ("rfkill", "bluetoothctl", "timeout"):
        (bindir / tool).write_text("#!/bin/sh\n")
        (bindir / tool).chmod(0o755)
    harness = (f"set -euo pipefail\nexport PATH={bindir}:$PATH\nUNIT_DIR={tmp_path}\nBASE=/home/pi/bluecat\nSTAMP=x\n"
               "USER=pi\nsudo(){ \"$@\"; }\n" + func + pre +
               'write_unit bluecat-sensor.service "Sensor" "$BASE/sensor" bluecat2mqtt.py "$BT_PRE"\n')
    import subprocess
    subprocess.run(["bash", "-c", harness], check=True, capture_output=True, text=True)
    unit = (tmp_path / "bluecat-sensor.service").read_text()
    assert f"ExecStartPre=-+{bindir}/rfkill unblock bluetooth\n" in unit
    assert f"ExecStartPre=-+{bindir}/timeout 10 {bindir}/bluetoothctl power on\n" in unit
    assert unit.index("ExecStartPre") < unit.index("ExecStart=/home/pi/bluecat/venv/bin/python -u bluecat2mqtt.py")


FAKE_HA_STORE = r"""#!/bin/sh
# Fake-„ha“ mit trägem Store: die neue Version erscheint erst nach dem dritten Reload
echo "$*" >> "$HA_LOG"
case "$1" in
  store|supervisor) n=$(cat "$HA_DIR/reloads" 2>/dev/null || echo 0); echo $((n + 1)) > "$HA_DIR/reloads"; exit 0 ;;
esac
if [ "$2" = update ]; then echo "$NEW" > "$HA_DIR/installed"; exit 0; fi
if [ "$2" = info ]; then
  n=$(cat "$HA_DIR/reloads" 2>/dev/null || echo 0)
  inst=$(cat "$HA_DIR/installed" 2>/dev/null || echo "2.5.1-1")
  if [ "$n" -ge 3 ]; then latest="$NEW"; else latest="2.5.1-1"; fi
  if [ "$inst" != "$latest" ]; then upd=true; else upd=false; fi
  echo "{\"name\": \"TriLola\", \"version\": \"$inst\", \"version_latest\": \"$latest\", \"update_available\": $upd}"
fi
exit 0
"""


@pytest.mark.parametrize("shell", ["sh", "zsh"])
def test_ha_install_waits_for_store_and_refreshes_entity(tmp_path, shell, monkeypatch):
    import shutil as _shutil
    import subprocess
    if not _shutil.which(shell):
        pytest.skip(f"{shell} nicht installiert")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "ha").write_text(FAKE_HA_STORE)
    (bindir / "curl").write_text('#!/bin/sh\necho "curl $*" >> "$HA_LOG"\n'
                                 'case "$*" in *core/api/states*) echo \'[{"entity_id": "update.trilola_update"}, '
                                 '{"entity_id": "update.anderes_update"}]\' ;; esac\n')
    (bindir / "sleep").write_text("#!/bin/sh\nexit 0\n")                 # nicht wirklich warten
    for f in ("ha", "curl", "sleep"):
        (bindir / f).chmod(0o755)
    new = "2.5.3-20260928170000"
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "HA_LOG": str(tmp_path / "ha.log"), "HA_DIR": str(tmp_path),
           "NEW": new, "SUPERVISOR_TOKEN": "tok"}
    res = subprocess.run([shell, "-c", bd.ha_install_script(start=True, version=new)], env=env, capture_output=True,
                         text=True, timeout=60)
    assert res.returncode == 0, res.stdout + res.stderr
    log = (tmp_path / "ha.log").read_text()
    assert "apps update local_trilola" in log and "rebuild" not in log     # erst warten, dann echtes Update
    assert f"» Installiert: {new}" in res.stdout
    posts = [line for line in log.splitlines() if "update_entity" in line]
    assert len(posts) == 1 and "update.trilola_update" in posts[0]
    with pytest.raises(bd.DeployError):
        bd.ha_install_script(version="1.0; rm -rf /")


def test_app_version_file_and_ha_entity(tmp_path, monkeypatch):
    members, tar, data = bundle_members()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
        assert t.extractfile("trilola/app/VERSION").read().decode().strip() == "9.9.9-test"
    (tmp_path / "VERSION").write_text("2.6.0-20260929183012\n")
    monkeypatch.setattr(bd, "REPO", str(tmp_path))
    assert bd.installed_app_version() == {"version": "2.6.0", "full": "2.6.0-20260929183012",
                                          "installed": "29.09.2026 18:30"}
    msgs = gui.app_version_discovery("2.6.0")
    config = json.loads(msgs[0][1])
    assert config["device"] == {"identifiers": ["bluecat_trilola_engine"]}   # hängt am TriLola-Gerät
    assert config["entity_category"] == "diagnostic" and msgs[1] == (gui.APP_VERSION_TOPIC, "2.6.0")
    live = gui.LiveState()
    live.announce = msgs

    class Client:
        published = []
        subscribe = staticmethod(lambda topic: None)

        def publish(self, topic, payload, qos=0, retain=False):
            self.published.append((topic, payload, retain))
    client = Client()
    live.client = client
    live._on_connect(client, None, None, 0)
    assert [(t, r) for t, _, r in client.published] == [(msgs[0][0], True), (gui.APP_VERSION_TOPIC, True)]


def test_pc_ui_learns_installed_app_version_over_mqtt():
    import types
    live = gui.LiveState()
    assert gui.APP_VERSION_TOPIC in live.TOPICS
    live._on_message(None, None, types.SimpleNamespace(topic=gui.APP_VERSION_TOPIC, payload=b"2.6.1", retain=True))
    assert live.snapshot()["tracker"]["app"] == "2.6.1"


def test_deploy_module_has_no_invalid_escapes():
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("error", SyntaxWarning)
        for name in ("bluecat_deploy.py", "bluecat_gui.py", "hotspots.py"):
            with open(os.path.join(bd.HERE, name), "rb") as handle:
                compile(handle.read(), name, "exec")
