"""Kalibrierung der Sensoren (Pegelmodell) über MQTT.

Modi
----
[1] Raumkalibrierung (empfohlen): Halsband nacheinander an mehrere bekannte
    Punkte legen; alle Sensoren messen gleichzeitig. Anschließend wird das
    gepoolte Modell (``calibration_model.fit_pooled``) gefittet:
    ``tx_power`` je Sensor, gemeinsames ``n_factor``, ``sigma_db`` aus den
    Residuen, ``r_min``/``r_max`` aus der Streuung; mit ``floorplan.json``
    zusätzlich die Skalierung der Wanddämpfungen.
    Alle Rohdaten landen in ``config/calibration_points.json``.
[2] Aus gespeicherten Punkten neu fitten (keine neue Messung).
[3] Einzelner Sensor (1 m + Fernpunkt) – nur wenn Modus 1 nicht möglich ist.

Tipps: 8–15 Punkte über alle Räume verteilen, je 60–120 s, Halsband so
ablegen, wie die Katze es trägt (Höhe ~20 cm, nicht auf Metall).
"""

from __future__ import annotations

import json
import math
import os
import threading
import time

import numpy as np
import paho.mqtt.client as mqtt

try:
    import secrets_tri as sec
except ModuleNotFoundError:
    import secrets_tri_dummy as sec

from calibration_model import fit_pooled, robust_point_statistics
import tuning
from config_manager import DEFAULT_TAG_HEIGHT_CM, ConfigStore, format_ble_address, sensor_z_cm
from core.floorplan import FloorPlan
from network.payload_parser import MeshBeacon, PayloadParser, SensorReading
from radio_environment import RadioEnvironmentModel

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(BASE_DIR, "config")
POINTS_FILE = os.path.join(CONFIG_DIR, "calibration_points.json")
FLOORPLAN_FILE = os.path.join(BASE_DIR, getattr(sec, "FLOORPLAN_FILE", "config/floorplan.json"))
BASELINE_FILE = os.path.join(BASE_DIR, getattr(sec, "RADIO_BASELINE_FILE", "config/radio_mesh_baseline.json"))

store = ConfigStore(CONFIG_DIR)
data_lock = threading.Lock()
connected_event = threading.Event()
collected = {}        # sensor_id -> [(median, count), ...]
radio_env = None
listen_sensors = set()


def active_sensors():
    store.load()
    return {sid: cfg for sid, cfg in store.sensors.items()
            if cfg.data.get("enabled", True) and cfg.data.get("position_configured", False)}


def resolve_sensor(payload_sensor_id, topic, sensors):
    if payload_sensor_id in sensors:
        return payload_sensor_id
    base = topic.rsplit("/", 1)[0]
    for sid, cfg in sensors.items():
        if cfg.data["topic"] == topic or cfg.data["topic"].rsplit("/", 1)[0] == base:
            return sid
    return None


def sensor_by_mac(mac, sensors):
    try:
        wanted = format_ble_address(mac)
    except ValueError:
        return None
    for sid, cfg in sensors.items():
        if wanted in cfg.data.get("ble_addresses", []):
            return sid
    return None


def make_on_message(sensors):
    def on_message(client, userdata, msg):
        if msg.retain:
            return  # retained Werte sind alt – nicht in die Kalibrierung aufnehmen
        try:
            parsed = PayloadParser.parse(msg.payload.decode("utf-8").strip())
        except UnicodeDecodeError:
            return
        if parsed is None:
            return
        now = time.monotonic()
        if isinstance(parsed, MeshBeacon):
            receiver = resolve_sensor(parsed.receiver_name, msg.topic, sensors)
            transmitter = sensor_by_mac(parsed.transmitter_mac, sensors) if parsed.transmitter_mac else parsed.transmitter_name
            if radio_env and receiver and transmitter:
                radio_env.observe(receiver, transmitter, parsed.rssi, now)
            return
        if isinstance(parsed, SensorReading) and parsed.present:
            sid = resolve_sensor(parsed.sensor_id, msg.topic, sensors)
            if sid is None or sid not in listen_sensors:
                return
            offset = radio_env.get_receiver_offset(sid, now) if radio_env else 0.0
            with data_lock:
                collected.setdefault(sid, []).append((parsed.rssi - offset, max(parsed.sample_count, 1)))
    return on_message


def run_measurement(duration, sensors):
    if "X.X" in str(getattr(sec, "MQTT_BROKER", "")):
        raise RuntimeError("Bitte secrets_tri.py mit einem echten MQTT_BROKER anlegen.")
    with data_lock:
        collected.clear()
    connected_event.clear()
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    if getattr(sec, "MQTT_USER", ""):
        client.username_pw_set(sec.MQTT_USER, getattr(sec, "MQTT_PASSWORD", "") or None)

    def on_connect(c, userdata, flags, reason_code, properties=None):
        if not reason_code.is_failure:
            c.subscribe("bluecat/#", qos=1)
            connected_event.set()

    client.on_connect = on_connect
    client.on_message = make_on_message(sensors)
    client.connect(sec.MQTT_BROKER, sec.MQTT_PORT, 60)
    client.loop_start()
    try:
        if not connected_event.wait(timeout=10):
            raise RuntimeError("MQTT-Verbindung wurde nicht bestätigt.")
        print(f"\nMessung läuft {duration} s ...")
        for remaining in range(duration, 0, -1):
            if remaining % 10 == 0 or remaining <= 5:
                with data_lock:
                    total = sum(len(v) for v in collected.values())
                print(f"  noch {remaining:3d} s  ({total} Fenster)")
            time.sleep(1)
    finally:
        client.disconnect()
        client.loop_stop()
    with data_lock:
        return {k: list(v) for k, v in collected.items()}


def read_float(prompt, default=None):
    text = input(prompt).strip().replace(",", ".")
    if not text and default is not None:
        return float(default)
    return float(text)


def read_duration(default=90):
    text = input(f"Messdauer in Sekunden [{default}]: ").strip()
    value = int(text) if text else default
    if value <= 0:
        raise ValueError("Dauer muss > 0 sein.")
    return value


def load_points():
    try:
        with open(POINTS_FILE, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {"points": []}


def save_points(data):
    tmp = POINTS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=1)
    os.replace(tmp, POINTS_FILE)


def apartment_floor_cm():
    try:
        with open(FLOORPLAN_FILE, "r", encoding="utf-8") as handle:
            return float(json.load(handle).get("floor_elevation_cm") or 0.0)
    except (OSError, ValueError, AttributeError):
        return 0.0


def tag_height_cm():
    return float(tuning.load(CONFIG_DIR).get("TAG_HEIGHT_CM", getattr(sec, "TAG_HEIGHT_CM", DEFAULT_TAG_HEIGHT_CM)))


def setup_radio_env(sensors):
    global radio_env
    floor = apartment_floor_cm()
    radio_env = RadioEnvironmentModel({sid: cfg.data["pos"] for sid, cfg in sensors.items()},
                                      baseline_file=BASELINE_FILE,
                                      sensor_heights={sid: sensor_z_cm(cfg.data, floor) for sid, cfg in sensors.items()})


# ---------------------------------------------------------------------------
def room_calibration():
    global listen_sensors
    sensors = active_sensors()
    if not sensors:
        print("Keine aktiven Sensoren mit gesetzter Position gefunden.")
        return
    setup_radio_env(sensors)
    listen_sensors = set(sensors)
    data = load_points()
    print(f"\n{len(sensors)} Sensoren. Gespeicherte Punkte: {len(data['points'])}.")
    if data["points"] and input("Gespeicherte Punkte verwerfen? (j/N): ").strip().lower() in {"j", "ja", "y"}:
        data = {"points": []}
    number = len(data["points"]) + 1
    while True:
        print(f"\n--- Messpunkt {number} ---")
        text = input("X in cm (leer = fertig): ").strip()
        if not text:
            break
        try:
            x = float(text.replace(",", "."))
            y = read_float("Y in cm: ")
            duration = read_duration()
        except ValueError as error:
            print(f"Ungültige Eingabe: {error}")
            continue
        input("Halsband liegt am Punkt? ENTER startet die Messung ...")
        try:
            measured = run_measurement(duration, sensors)
        except Exception as error:
            print(f"Messung fehlgeschlagen: {error}")
            continue
        point = {"x": x, "y": y, "duration_s": duration, "time": time.time(), "samples": measured}
        data["points"].append(point)
        save_points(data)
        for sid in sensors:
            windows = measured.get(sid, [])
            if len(windows) >= 3:
                mean, var, n = robust_point_statistics(windows)
                print(f"  {sid:28s} {mean:7.1f} dBm  σ²={var:5.1f}  ({len(windows)} Fenster)")
            else:
                print(f"  {sid:28s} zu wenig Daten ({len(windows)})")
        number += 1
    fit_and_save(data)


def fit_and_save(data=None):
    data = data or load_points()
    sensors = active_sensors()
    if len(data.get("points", [])) < 3:
        print("Mindestens 3 Messpunkte nötig (empfohlen 8–15).")
        return
    samples = {sid: [] for sid in sensors}
    for point in data["points"]:
        for sid, windows in point["samples"].items():
            if sid in samples:
                samples[sid].append(((point["x"], point["y"]), [tuple(w) for w in windows]))
    fp_data = None
    floorplan = None
    if os.path.exists(FLOORPLAN_FILE):
        with open(FLOORPLAN_FILE, "r", encoding="utf-8") as handle:
            fp_data = json.load(handle)
        floorplan = FloorPlan.from_dict(fp_data)
    per_sensor_n = input("n_factor je Sensor statt gemeinsam? (nur bei ≥ 6 Punkten je Sensor) (j/N): ").strip().lower() in {"j", "ja", "y"}
    # Höhen wie im Tracker: Sensor-Antenne über dem Fußboden der Wohnung, Halsband auf Katzenhöhe
    floor = float((fp_data or {}).get("floor_elevation_cm") or 0.0)
    heights = {sid: sensor_z_cm(cfg.data, floor) for sid, cfg in sensors.items()}
    tag_height = tag_height_cm()
    configs, glob = fit_pooled(samples, {sid: cfg.data["pos"] for sid, cfg in sensors.items()},
                               floorplan=floorplan, per_sensor_n=per_sensor_n,
                               sensor_heights=heights, point_height_cm=tag_height)
    print("\n==================== Ergebnis ====================")
    print(f"Punkte: {glob['points']} | n_factor (gemeinsam): {glob['n_factor']} | "
          f"Shadowing σ: {glob['sigma_db']} dB | Wandskalierung: {glob['wall_scale']}")
    for sid, cal in sorted(configs.items()):
        old = sensors[sid].data
        print(f"{sid:28s} tx {old.get('tx_power'):7.2f} → {cal['tx_power']:7.2f} | n {old.get('n_factor'):5.2f} → "
              f"{cal['n_factor']:5.2f} | σ {cal['sigma_db']:4.1f} dB | Punkte {cal['calibration_points']}")
    missing = sorted(set(sensors) - set(configs))
    if missing:
        print("Ohne verwertbare Daten (unverändert): " + ", ".join(missing))
    if input("\nÜbernehmen und speichern? (J/n): ").strip().lower() in {"n", "nein", "no"}:
        return
    if tracker_online():
        # Läuft der Tracker, übernimmt er die Werte per MQTT (validiert, speichert,
        # meldet an HA). Direktes Schreiben würde er sonst später überschreiben.
        publish_calibration({sid: {**{k: cal[k] for k in ("tx_power", "n_factor", "sigma_db", "r_min", "r_max")},
                                   "calibration_geometry": "3d"}
                             for sid, cal in configs.items()})
        print("Kalibrierung per MQTT an den laufenden Tracker übergeben.")
    else:
        for sid, cal in configs.items():
            calibration = {k: cal[k] for k in ("tx_power", "n_factor", "sigma_db", "r_min", "r_max")}
            calibration["calibration_geometry"] = "3d"
            config = store.update_field(sid, "calibration", calibration)
            config.data["calibration_points"] = cal["calibration_points"]
            config.data["calibration_rms_db"] = cal["calibration_rms_db"]
            store.save(config)
    if fp_data is not None and glob.get("wall_scale"):
        fp_data["wall_scale"] = round(float(fp_data.get("wall_scale", 1.0)) * glob["wall_scale"], 3)
        tmp = FLOORPLAN_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(fp_data, handle, indent=2, ensure_ascii=False)
        os.replace(tmp, FLOORPLAN_FILE)
        print(f"Grundriss: wall_scale = {fp_data['wall_scale']}")
    print("Gespeichert. Tracker neu starten (oder die Werte kommen per MQTT-Set) – und in Home Assistant "
          "„TriLola Mesh-Baseline neu lernen“ drücken, damit die Drift-Korrektur auf die neue Kalibrierung zeigt.")


def _client():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    if getattr(sec, "MQTT_USER", ""):
        client.username_pw_set(sec.MQTT_USER, getattr(sec, "MQTT_PASSWORD", "") or None)
    return client


def tracker_online(timeout=3.0) -> bool:
    """Prüft den retained Status des Trackers (LWT)."""
    if "X.X" in str(getattr(sec, "MQTT_BROKER", "")):
        return False
    state = {"value": None}
    done = threading.Event()
    client = _client()

    def on_message(c, userdata, msg):
        state["value"] = msg.payload.decode("utf-8", "ignore").strip()
        done.set()

    client.on_connect = lambda c, u, f, rc, p=None: c.subscribe("bluecat/trilola/status", qos=1)
    client.on_message = on_message
    try:
        client.connect(sec.MQTT_BROKER, sec.MQTT_PORT, 30)
    except OSError:
        return False
    client.loop_start()
    done.wait(timeout)
    client.disconnect()
    client.loop_stop()
    return state["value"] == "online"


def publish_calibration(values: dict):
    client = _client()
    client.connect(sec.MQTT_BROKER, sec.MQTT_PORT, 30)
    client.loop_start()
    infos = [client.publish(f"bluecat/config/sensors/{sid}/calibration/set", json.dumps(cal), qos=1)
             for sid, cal in values.items()]
    for info in infos:
        info.wait_for_publish(5)
    client.disconnect()
    client.loop_stop()


def single_sensor_calibration():
    global listen_sensors
    sensors = active_sensors()
    sid = input("Sensor-ID: ").strip()
    if sid not in sensors:
        print("Unbekannter oder inaktiver Sensor (Position gesetzt?).")
        return
    setup_radio_env(sensors)
    listen_sensors = {sid}
    data = sensors[sid].data
    try:
        duration = read_duration(120)
        input("\nHalsband auf Katzenhöhe genau 1 m (am Boden gemessen) vor den Sensor legen, ENTER ...")
        near = run_measurement(duration, sensors).get(sid, [])
        far_m = read_float("\nZweiter Punkt: Entfernung in m (> 2 m empfohlen): ")
        if far_m <= 1.0:
            raise ValueError("Entfernung muss > 1 m sein.")
        input(f"Halsband auf {far_m} m legen, ENTER ...")
        far = run_measurement(duration, sensors).get(sid, [])
    except ValueError as error:
        print(f"Ungültige Eingabe: {error}")
        return
    if len(near) < 3 or len(far) < 3:
        print("Zu wenige Daten.")
        return
    rssi_near, var_near, _ = robust_point_statistics(near)
    rssi_far, var_far, _ = robust_point_statistics(far)
    # schräge Abstände (Sensor hängt höher als das Halsband)
    dz_m = (sensor_z_cm(data, apartment_floor_cm()) - tag_height_cm()) / 100.0
    d_near, d_far = math.hypot(1.0, dz_m), math.hypot(far_m, dz_m)
    n = (rssi_near - rssi_far) / (10.0 * np.log10(d_far / d_near))
    tx = rssi_near + 10.0 * n * np.log10(d_near)  # auf 1 m Schrägabstand umgerechnet
    if not np.isfinite(n) or n <= 0:
        print(f"Unplausibler n_factor ({n:.2f}) – Messung wiederholen.")
        return
    print(f"tx_power {tx:.2f} dBm | n_factor {n:.3f} | r_min {var_near:.2f} | r_max {var_far:.2f}")
    print("Hinweis: sigma_db (Shadowing) lässt sich nur mit der Raumkalibrierung schätzen; es bleibt bei "
          f"{data.get('sigma_db', 4.0)} dB.")
    if input("Speichern? (J/n): ").strip().lower() not in {"n", "nein", "no"}:
        values = {"tx_power": round(tx, 2), "n_factor": round(n, 3), "r_min": round(var_near, 2), "r_max": round(var_far, 2),
                  "calibration_geometry": "3d"}
        if tracker_online():
            publish_calibration({sid: values})
            print("Per MQTT an den laufenden Tracker übergeben.")
        else:
            store.update_field(sid, "calibration", values)
            print("Gespeichert.")


def main():
    print("==================================================")
    print("   TriLola Sensor-Kalibrierung")
    print("==================================================")
    print("[1] Raumkalibrierung – alle Sensoren gemeinsam (empfohlen)")
    print("[2] Aus gespeicherten Punkten neu fitten")
    print("[3] Einzelnen Sensor kalibrieren (1 m + Fernpunkt)")
    choice = input("Auswahl: ").strip()
    if choice == "1":
        room_calibration()
    elif choice == "2":
        fit_and_save()
    elif choice == "3":
        single_sensor_calibration()
    else:
        print("Ungültige Auswahl.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nAbbruch.")
