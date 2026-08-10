"""Kalibrierung einzelner Sensoren oder aller Sensoren per Raumregression.

Die Kalibrierung bestimmt das Log-Distanz-Modell und die Rausch-Varianzen.
Neu: Nutzt das Mesh-Netzwerk (RadioEnvironmentModel), um Hardware-Offsets
während der Kalibrierung in Echtzeit herauszurechnen!
"""

from __future__ import annotations

import glob
import json
import os
import threading
import time

import numpy as np
import paho.mqtt.client as mqtt

try:
    import secrets_tri as sec
except ModuleNotFoundError:
    import secrets_tri_dummy as sec

from config_manager import ConfigStore, normalize_ble_address
from network.payload_parser import PayloadParser, SensorReading, MeshBeacon
from radio_environment import RadioEnvironmentModel

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(BASE_DIR, "config")
OFFLINE_RSSI_LIMIT = -120.0

rssi_data = {}
active_topics = []
topic_to_sensor_id = {}
radio_env = None
data_lock = threading.Lock()
connected_event = threading.Event()
target_tag_id = getattr(sec, "BLE_TAG_ID", None) or None
selected_tag_id = target_tag_id
CONFIG_STORE = ConfigStore(CONFIG_DIR)


def on_connect(client, userdata, flags, rc):
    if rc == 0:
        # Wir abonnieren ALLES, um das Mesh-Netzwerk belauschen zu können!
        client.subscribe("bluecat/#", qos=1)
        connected_event.set()
    else:
        print(f"Fehler bei der MQTT-Verbindung: Code {rc}")


def on_disconnect(client, userdata, rc):
    connected_event.clear()
    if rc != 0:
        print(f"MQTT-Verbindung während der Messung verloren: Code {rc}")


def accept_tag(reading: SensorReading):
    """Verhindert, dass mehrere Tags in eine Kalibrierung eingehen."""
    global selected_tag_id
    if target_tag_id is not None:
        return reading.tag_id == str(target_tag_id)
    if reading.tag_id is None:
        return selected_tag_id is None
    if selected_tag_id is None:
        selected_tag_id = reading.tag_id
        print(f"BLE-Tag für Kalibrierung ausgewählt: {selected_tag_id}")
        return True
    return reading.tag_id == selected_tag_id


def get_sensor_id_by_mac(mac: str):
    """Löst MAC-Adressen im Mesh in interne IDs auf."""
    if not mac: 
        return None
    mac_clean = mac.replace(":", "").lower()
    for s_id, s_config in CONFIG_STORE.load().items():
        for node_mac in s_config.data.get("ble_addresses", []):
            if node_mac.replace(":", "").lower() == mac_clean:
                return s_id
    return None


def on_message(client, userdata, msg):
    try:
        decoded = msg.payload.decode("utf-8").strip()
        parsed = PayloadParser.parse(decoded)
        if parsed is None:
            return

        now = time.monotonic()
        receiver_id = topic_to_sensor_id.get(msg.topic)
        
        # 1. Mesh Beacons fangen und auswerten
        if isinstance(parsed, MeshBeacon):
            if not radio_env or not receiver_id:
                return
            transmitter_id = parsed.transmitter_name
            if parsed.transmitter_mac:
                mapped_id = get_sensor_id_by_mac(parsed.transmitter_mac)
                if mapped_id:
                    transmitter_id = mapped_id
                    
            if transmitter_id and receiver_id:
                radio_env.observe(receiver_id, transmitter_id, parsed.rssi, now)
            return

        # 2. Tag Readings fangen und korrigieren
        if isinstance(parsed, SensorReading):
            # Nur für die aktuell zu kalibrierenden Sensoren aufzeichnen
            if msg.topic not in active_topics:
                return
            if not parsed.present or not accept_tag(parsed):
                return

            rssi = parsed.rssi
            if np.isfinite(rssi) and rssi > OFFLINE_RSSI_LIMIT:
                hardware_offset = 0.0
                
                # ECHTZEIT KORREKTUR: Den Mesh-Offset herausrechnen!
                if radio_env and receiver_id:
                    hardware_offset = radio_env.get_hardware_offset(receiver_id, now)
                    
                corrected_rssi = rssi - hardware_offset
                
                with data_lock:
                    rssi_data.setdefault(msg.topic, []).append(corrected_rssi)
                    
    except Exception:
        pass


def _safe_sensor_name(sensor_name):
    sensor_name = str(sensor_name).strip()
    if (
        not sensor_name
        or os.path.basename(sensor_name) != sensor_name
        or sensor_name in {".", ".."}
    ):
        raise ValueError("Ungültiger Sensorname.")
    return sensor_name


def load_or_create_config(sensor_name):
    """Lädt eine Konfiguration oder erstellt ein vollständiges Grundgerüst."""
    sensor_name = _safe_sensor_name(sensor_name)
    os.makedirs(CONFIG_DIR, exist_ok=True)
    filepath = os.path.join(CONFIG_DIR, f"{sensor_name}.json")

    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as file:
            config = json.load(file)
        config.setdefault("sensor_id", sensor_name)
        config.setdefault("name", sensor_name.replace("_", " ").title())
        config.setdefault("implementation", "unknown")
        config.setdefault("enabled", True)
        config.setdefault("calibration_status", "unknown")
        config.setdefault(
            "topic", f"bluecat/{config['sensor_id']}/sensor/state"
        )
        config.setdefault(
            "availability_topic",
            f"bluecat/{config['sensor_id']}/sensor/status",
        )
        config.setdefault("ble_addresses", [])
        print(f"Lade bestehende Konfiguration für '{sensor_name}'...")
        return config, filepath

    print(f"\n[NEUER SENSOR] Lege '{sensor_name}.json' an.")
    sensor_id = input(f"Sensor-ID [ENTER = {sensor_name}]: ").strip()
    sensor_id = sensor_id or sensor_name
    topic_default = f"bluecat/{sensor_id}/sensor/state"
    topic = (
        input(f"MQTT-Topic [ENTER = {topic_default}]: ").strip()
        or topic_default
    )
    pos_x = float(
        input("X-Koordinate in cm: ").strip().replace(",", ".")
    )
    pos_y = float(
        input("Y-Koordinate in cm: ").strip().replace(",", ".")
    )

    config = {
        "sensor_id": sensor_id,
        "name": sensor_name.replace("_", " ").title(),
        "implementation": "unknown",
        "enabled": True,
        "calibration_status": "uncalibrated",
        "topic": topic,
        "availability_topic": f"bluecat/{sensor_id}/sensor/status",
        "ble_addresses": [],
        "pos": [pos_x, pos_y],
        "tx_power": -59.0,
        "n_factor": 3.0,
        "r_min": 5.0,
        "r_max": 20.0,
        "q_variance": 0.05,
        "rssi_limit": -110.0,
    }
    return config, filepath


def save_config(filepath, config):
    """Speichert atomar und mit expliziter UTF-8-Kodierung."""
    temp_path = f"{filepath}.tmp"
    with open(temp_path, "w", encoding="utf-8") as file:
        json.dump(config, file, indent=4, ensure_ascii=False)
    os.replace(temp_path, filepath)
    print(f"Konfiguration erfolgreich gespeichert: {filepath}")


def robust_statistics(values):
    """Berechnet robusten Mittelwert, Varianz und Inlier-Anzahl."""
    values = np.asarray(values, dtype=float)
    if values.size == 0:
        raise ValueError("Keine Werte vorhanden.")

    median = float(np.median(values))
    mad = float(np.median(np.abs(values - median)))
    robust_sigma = max(1.4826 * mad, 0.5)
    threshold = max(3.0 * robust_sigma, 2.0)
    inliers = values[np.abs(values - median) <= threshold]
    if len(inliers) < 3:
        inliers = values

    mean = float(np.mean(inliers))
    variance = float(
        np.var(inliers, ddof=1 if len(inliers) > 1 else 0)
    )
    return mean, max(variance, 0.0), len(inliers)


def create_mqtt_client():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
    if getattr(sec, "MQTT_USER", "") and getattr(sec, "MQTT_PASSWORD", ""):
        client.username_pw_set(sec.MQTT_USER, sec.MQTT_PASSWORD)
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    return client


def run_mqtt_measurement(duration):
    """Sammelt RSSI-Werte aller aktiven Topics für ``duration`` Sekunden."""
    if "X.X" in str(getattr(sec, "MQTT_BROKER", "")):
        raise RuntimeError(
            "Bitte secrets_tri.py mit einem echten MQTT_BROKER anlegen."
        )
    if duration <= 0:
        raise ValueError("Die Messdauer muss größer als 0 sein.")

    with data_lock:
        rssi_data.clear()
    connected_event.clear()

    client = create_mqtt_client()
    loop_started = False
    try:
        client.connect(sec.MQTT_BROKER, sec.MQTT_PORT, 60)
        client.loop_start()
        loop_started = True
        if not connected_event.wait(timeout=10):
            raise RuntimeError(
                "MQTT-Verbindung/Subscription wurde nicht bestätigt."
            )

        print(f"\nMessung läuft für {duration} Sekunden...")
        for remaining in range(duration, 0, -1):
            if remaining % 10 == 0 or remaining <= 5:
                with data_lock:
                    total_packets = sum(
                        len(values) for values in rssi_data.values()
                    )
                print(
                    f"Noch {remaining} Sekunden... "
                    f"({total_packets} Pakete gesammelt)"
                )
            time.sleep(1)
    finally:
        if client.is_connected():
            client.disconnect()
        if loop_started:
            client.loop_stop()

    with data_lock:
        return {
            topic: list(values) for topic, values in rssi_data.items()
        }


def _read_duration():
    duration_text = input(
        "\nMessdauer in Sekunden (Standard = 120): "
    ).strip()
    duration = int(duration_text) if duration_text else 120
    if duration <= 0:
        raise ValueError
    return duration

def setup_radio_environment(extra_config=None):
    """Lädt die Positionen für das SLAM/Mesh Modell in die Globals."""
    global topic_to_sensor_id, radio_env
    CONFIG_STORE.load()
    configs = load_active_sensor_configs()
    
    if extra_config and extra_config["sensor_id"] not in configs:
        configs[extra_config["sensor_id"]] = {
            "pos": np.asarray(extra_config["pos"], dtype=float),
            "topic": extra_config["topic"],
            "data": extra_config
        }
        
    sensor_positions = {s_id: info["pos"] for s_id, info in configs.items()}
    topic_to_sensor_id = {info["topic"]: s_id for s_id, info in configs.items()}
    
    radio_env = RadioEnvironmentModel(
        sensor_positions,
        baseline_file=os.path.join(CONFIG_DIR, "radio_mesh_baseline.json")
    )


def calibrate_single_sensor():
    global active_topics, selected_tag_id

    sensor_name = input(
        "\nName des Sensors (ohne .json): "
    ).strip()
    try:
        sensor_config, filepath = load_or_create_config(sensor_name)
    except (OSError, json.JSONDecodeError, ValueError, KeyError) as error:
        print(f"Konfiguration konnte nicht geladen werden: {error}")
        return
        
    # Bereite Mesh-Tracking vor
    setup_radio_environment(sensor_config)

    active_topics = [str(sensor_config["topic"])]
    selected_tag_id = target_tag_id
    measured_tx_power = sensor_config.get("tx_power")
    try:
        measured_tx_power = (
            float(measured_tx_power)
            if measured_tx_power is not None
            else None
        )
    except (TypeError, ValueError):
        measured_tx_power = None

    while True:
        print(f"\n--- EINZEL-KALIBRIERUNG FÜR: {active_topics[0]} ---")
        print("[1] Nahfeld-Messung (1 Meter) -> tx_power & r_min & q_variance")
        print("[2] Fernfeld-Messung (>1 Meter) -> n_factor & r_max")
        mode = input("Auswahl (1 oder 2): ").strip()

        try:
            if mode == "1":
                distance = 1.0
                tx_reference = measured_tx_power
            elif mode == "2":
                distance = float(
                    input("Exakte Distanz in Metern (>1.0): ")
                    .strip()
                    .replace(",", ".")
                )
                if not np.isfinite(distance) or distance <= 1.0:
                    raise ValueError("Die Distanz muss größer als 1 m sein.")
                if measured_tx_power is None:
                    raise ValueError(
                        "Zuerst muss eine Nahfeldmessung bei 1 m erfolgen."
                    )
                print(f"Letzter RSSI-Wert bei 1 m: {measured_tx_power:.2f}")
                tx_text = input(
                    "ENTER zum Übernehmen oder neu tippen: "
                ).strip()
                tx_reference = (
                    float(tx_text.replace(",", "."))
                    if tx_text
                    else float(measured_tx_power)
                )
            else:
                print("Ungültige Auswahl, bitte nochmal.")
                continue

            duration = _read_duration()
        except ValueError as error:
            print(f"Ungültige Eingabe: {error}")
            continue

        print(
            f"\nBitte platziere das Halsband exakt auf "
            f"{distance} Meter Entfernung."
        )
        input("Drücke ENTER, sobald das Halsband ruhig liegt...")

        try:
            measured = run_mqtt_measurement(duration)
        except Exception as error:
            print(f"Messung fehlgeschlagen: {error}")
            continue

        samples = measured.get(active_topics[0], [])
        if len(samples) < 3:
            print("\nFEHLER: Zu wenige verwertbare Daten empfangen.")
        else:
            mean, variance, inlier_count = robust_statistics(samples)
            print(
                f"Pakete: {len(samples)} | Inlier: {inlier_count} | "
                f"RSSI (korrigiert): {mean:.2f} dBm | Varianz: {variance:.2f} dBm²"
            )

            if mode == "1":
                sensor_config["tx_power"] = round(mean, 2)
                sensor_config["r_min"] = round(variance, 2)
                # NEU: q_variance automatisch aus dem Rauschen ableiten!
                sensor_config["q_variance"] = round(max(variance / 100.0, 0.001), 3)
                sensor_config["calibration_status"] = "partial"
                measured_tx_power = mean
                save_config(filepath, sensor_config)
            else:
                denominator = 10.0 * np.log10(distance)
                n_factor = (tx_reference - mean) / denominator
                if not np.isfinite(n_factor) or n_factor <= 0:
                    print(
                        "FEHLER: Ungültiger n_factor. "
                        "Prüfe Distanz und RSSI-Referenz."
                    )
                else:
                    sensor_config["n_factor"] = round(float(n_factor), 3)
                    sensor_config["r_max"] = round(variance, 2)
                    sensor_config["calibration_status"] = "calibrated"
                    save_config(filepath, sensor_config)

        again = input(
            "\nWeitere Messung für diesen Sensor? (j/n): "
        ).strip().lower()
        if again not in {"j", "ja", "y", "yes"}:
            break


def load_active_sensor_configs():
    """Lädt nur gültige und aktivierte Sensor-Konfigurationen."""
    configs = {}
    topics = set()
    for filepath in sorted(
        glob.glob(os.path.join(CONFIG_DIR, "*.json"))
    ):
        if os.path.basename(filepath) == "radio_mesh_baseline.json" or os.path.basename(filepath) == "radio_heatmap.json":
            continue
        try:
            with open(filepath, "r", encoding="utf-8") as file:
                config = json.load(file)
            if not isinstance(config, dict):
                raise ValueError("Konfiguration muss ein JSON-Objekt sein.")
            if not bool(config.get("enabled", True)):
                print(f"Überspringe deaktivierten Sensor: {filepath}")
                continue

            filename_id = os.path.splitext(os.path.basename(filepath))[0]
            sensor_id = str(config.get("sensor_id", filename_id)).strip()
            topic = str(
                config.get("topic")
                or f"bluecat/{sensor_id}/sensor/state"
            ).strip()
            position = np.asarray(config["pos"], dtype=float).reshape(-1)
            if len(position) != 2 or np.any(~np.isfinite(position)):
                raise ValueError("pos muss zwei endliche Werte enthalten.")
            if not sensor_id or not topic:
                raise ValueError("sensor_id und topic dürfen nicht leer sein.")
            if sensor_id in configs or topic in topics:
                raise ValueError("sensor_id oder topic ist doppelt.")

            config.setdefault("sensor_id", sensor_id)
            config.setdefault("name", sensor_id.replace("_", " ").title())
            config.setdefault("implementation", "unknown")
            config.setdefault("enabled", True)
            config["topic"] = topic
            config.setdefault(
                "availability_topic",
                f"bluecat/{sensor_id}/sensor/status",
            )
            config.setdefault("ble_addresses", [])
            configs[sensor_id] = {
                "data": config,
                "filepath": filepath,
                "topic": topic,
                "pos": position,
            }
            topics.add(topic)
        except (
            OSError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
        ) as error:
            print(f"Überspringe ungültige Konfiguration {filepath}: {error}")
    return configs


def calibrate_all_sensors_regression():
    global active_topics, selected_tag_id

    os.makedirs(CONFIG_DIR, exist_ok=True)
    configs = load_active_sensor_configs()
    if not configs:
        print("\nFEHLER: Keine gültigen, aktivierten Sensoren gefunden.")
        return
        
    setup_radio_environment()

    active_topics = [info["topic"] for info in configs.values()]
    selected_tag_id = target_tag_id
    print(f"\n{len(configs)} Sensoren für die Simultan-Messung geladen.")
    print(
        "Sammle Messpunkte an verschiedenen Orten "
        "(mindestens 2, idealerweise 3-4)."
    )

    measurements = {
        sensor_id: {"dist": [], "mean": [], "var": [], "inliers": []}
        for sensor_id in configs
    }

    point_number = 1
    while True:
        print(f"\n--- MESSPUNKT {point_number} ---")
        x_text = input(
            "X-Koordinate des Halsbandes in cm (leer = beenden): "
        ).strip()
        if not x_text:
            break
        try:
            x_cm = float(x_text.replace(",", "."))
            y_cm = float(
                input("Y-Koordinate des Halsbandes in cm: ")
                .strip()
                .replace(",", ".")
            )
            if not np.isfinite(x_cm) or not np.isfinite(y_cm):
                raise ValueError("Koordinaten müssen endlich sein.")
            duration = _read_duration()
        except ValueError as error:
            print(f"Ungültige Eingabe: {error}")
            continue

        input("Halsband positioniert? ENTER zum Starten...")
        try:
            measured = run_mqtt_measurement(duration)
        except Exception as error:
            print(f"Messung fehlgeschlagen: {error}")
            continue

        for sensor_id, sensor_info in configs.items():
            sensor_x, sensor_y = sensor_info["pos"]
            distance_m = float(
                np.hypot(sensor_x - x_cm, sensor_y - y_cm) / 100.0
            )
            if distance_m <= 0.0:
                print(
                    f"[{sensor_id}] Messpunkt liegt exakt auf dem Sensor; "
                    "wird übersprungen."
                )
                continue

            samples = measured.get(sensor_info["topic"], [])
            if len(samples) < 3:
                print(f"[{sensor_id}] Zu wenige Daten an diesem Ort.")
                continue

            mean, variance, inlier_count = robust_statistics(samples)
            measurements[sensor_id]["dist"].append(distance_m)
            measurements[sensor_id]["mean"].append(mean)
            measurements[sensor_id]["var"].append(variance)
            measurements[sensor_id]["inliers"].append(inlier_count)
            print(
                f"[{sensor_id}] Distanz: {distance_m:.2f} m | "
                f"RSSI: {mean:.2f} dBm | Varianz: {variance:.2f} | "
                f"Inlier: {inlier_count}"
            )

        point_number += 1
        if input(
            "\nWeiteren Messpunkt aufzeichnen? (j/n): "
        ).strip().lower() not in {"j", "ja", "y", "yes"}:
            break

    print("\n==================================================")
    print("      BERECHNE LINEARE REGRESSIONSMODELLE")
    print("==================================================")

    for sensor_id, data in measurements.items():
        distances = np.asarray(data["dist"], dtype=float)
        means = np.asarray(data["mean"], dtype=float)
        variances = np.asarray(data["var"], dtype=float)

        if len(distances) < 2:
            print(
                f"[{sensor_id}] Übersprungen: "
                "nicht genug gültige Messpunkte."
            )
            continue

        x_values = 10.0 * np.log10(distances)
        if (
            np.any(~np.isfinite(x_values))
            or np.any(~np.isfinite(means))
            or np.ptp(x_values) <= 1.0e-9
        ):
            print(
                f"[{sensor_id}] Übersprungen: "
                "keine ausreichende Distanzvariation."
            )
            continue

        try:
            slope, intercept = np.polyfit(x_values, means, 1)
        except (TypeError, ValueError, np.linalg.LinAlgError) as error:
            print(f"[{sensor_id}] Regression fehlgeschlagen: {error}")
            continue

        tx_power = float(intercept)
        n_factor = float(-slope)
        predicted = slope * x_values + intercept
        residuals = means - predicted
        residual_rms = float(np.sqrt(np.mean(residuals**2)))
        total_sum = float(np.sum((means - np.mean(means)) ** 2))
        r_squared = (
            1.0 - float(np.sum(residuals**2)) / total_sum
            if total_sum > 0.0
            else 1.0
        )

        if not np.isfinite(tx_power) or not np.isfinite(n_factor):
            print(f"[{sensor_id}] Ungültige Regressionsparameter.")
            continue
        if n_factor <= 0.0:
            print(
                f"[{sensor_id}] Ungültiger n_factor={n_factor:.3f}; "
                "Konfiguration wird nicht gespeichert."
            )
            continue

        nearest_index = int(np.argmin(distances))
        farthest_index = int(np.argmax(distances))
        r_min = float(variances[nearest_index])
        r_max = float(variances[farthest_index])
        
        # NEU: q_variance automatisch ableiten
        q_variance = round(max(np.median(variances) / 100.0, 0.001), 3)

        print(f"\n--- Ergebnisse für {sensor_id} ---")
        print(f"tx_power   : {tx_power:.2f} dBm")
        print(f"n_factor   : {n_factor:.3f}")
        print(f"r_min      : {r_min:.2f} dBm²")
        print(f"r_max      : {r_max:.2f} dBm²")
        print(f"q_variance : {q_variance:.3f}")
        print(f"RMS-Fehler : {residual_rms:.2f} dB | R²: {r_squared:.3f}")

        config = configs[sensor_id]["data"]
        config["tx_power"] = round(tx_power, 2)
        config["n_factor"] = round(n_factor, 3)
        config["r_min"] = round(max(r_min, 0.0), 2)
        config["r_max"] = round(max(r_max, 0.0), 2)
        config["q_variance"] = q_variance
        config.setdefault("rssi_limit", -110.0)
        
        config["calibration_status"] = "calibrated"
        config["calibration_points"] = len(distances)
        config["calibration_rms_db"] = round(residual_rms, 3)
        config["calibration_r_squared"] = round(r_squared, 4)
        save_config(configs[sensor_id]["filepath"], config)


def main():
    print("==================================================")
    print("   TriLola Sensor-Kalibrierung (Pro-Edition)     ")
    print("==================================================\n")
    print("Welchen Modus möchtest du starten?")
    print("[1] Einzelnen Sensor kalibrieren")
    print("[2] Gesamtkonfiguration per Raumregression")
    selection = input("Auswahl (1 oder 2): ").strip()

    if selection == "1":
        calibrate_single_sensor()
    elif selection == "2":
        calibrate_all_sensors_regression()
    else:
        print("Ungültige Auswahl. Beende Skript.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nAbbruch durch Benutzer.")