import os
import time
import json
import signal
import threading
import re
import numpy as np
from datetime import datetime, timezone
from secrets.tri import ORIGIN_LAT, ORIGIN_LON

M_PER_DEG_LAT = 111111.0
M_PER_DEG_LON = 111111.0 * np.cos(np.radians(ORIGIN_LAT))

# Konfiguration und Secrets
try:
    import secrets_tri as sec
except ModuleNotFoundError:
    import secrets_tri_dummy as sec
except SyntaxError as error:
    raise SystemExit("secrets_tri.py enthält einen Syntaxfehler.") from error

from config_manager import (
    ConfigStore, CONFIG_ROOT, TARGET_MAC_STATE_TOPIC, TARGET_MAC_SET_TOPIC, 
    MESH_PEERS_TOPIC, SENSOR_CONFIG_SET_PATTERN, SENSOR_CONFIG_STATE_PATTERN, 
    normalize_ble_address, mesh_peers_payload
)

#modulare Architektur
from core.engine import TrackingEngine
from network.mqtt_client import MQTTController
from network.payload_parser import PayloadParser, SensorReading, MeshBeacon
from network.ha_discovery import HADiscoveryBuilder, STATE_TOPIC_GPS

# --- Globale Initialisierung ---


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(BASE_DIR, "config")
CONFIG_STORE = ConfigStore(CONFIG_DIR)

TARGET_TAG_ID = getattr(sec, "BLE_TAG_ID", None) or None
OBSERVED_TAG_ID = TARGET_TAG_ID

state_lock = threading.RLock()
stop_event = threading.Event()
last_output_was_active = False

# Parameter aus Secrets laden
engine_params = {
    "SENSOR_TIMEOUT_SEC": float(getattr(sec, "SENSOR_TIMEOUT_SEC", 30.0)),
    "MAX_SNAPSHOT_SKEW_SEC": float(getattr(sec, "MAX_SNAPSHOT_SKEW_SEC", 1.0)),
    "POSITION_MEMORY_SEC": float(getattr(sec, "POSITION_MEMORY_SEC", 10.0)),
    "MAX_TRACK_DISTANCE_CM": float(getattr(sec, "MAX_TRACK_DISTANCE_CM", 2000.0)),
    "MAX_POSITION_RADIUS_CM": float(getattr(sec, "MAX_POSITION_RADIUS_CM", 2000.0)),
    "MAX_POSITION_JUMP_CM": float(getattr(sec, "MAX_POSITION_JUMP_CM", 500.0)),
    "MAX_POSITION_SPEED_CM_S": float(getattr(sec, "MAX_POSITION_SPEED_CM_S", 250.0)),
    "PARTICLE_COUNT": int(getattr(sec, "PARTICLE_COUNT", 1500)),
    "PARTICLE_PROCESS_NOISE_CM": float(getattr(sec, "PARTICLE_PROCESS_NOISE_CM", 75.0)),
    "PARTICLE_INITIAL_SPREAD_CM": float(getattr(sec, "PARTICLE_INITIAL_SPREAD_CM", 250.0)),
    "PARTICLE_MINIMUM_SIGMA_CM": float(getattr(sec, "PARTICLE_MINIMUM_SIGMA_CM", 75.0)),
    "PARTICLE_BOUNDS_MARGIN_CM": float(getattr(sec, "PARTICLE_BOUNDS_MARGIN_CM", 500.0)),
    "RADIO_BASELINE_LEARNING_SAMPLES": int(getattr(sec, "RADIO_BASELINE_LEARNING_SAMPLES", 30)),
    "RADIO_LINK_TIMEOUT_SEC": float(getattr(sec, "RADIO_LINK_TIMEOUT_SEC", 15.0)),
    "RADIO_BASELINE_FILE": os.path.join(BASE_DIR, getattr(sec, "RADIO_BASELINE_FILE", "config/radio_mesh_baseline.json"))
}

# Kern-Komponenten instanziieren
engine = TrackingEngine(engine_params)

mqtt_ctrl = MQTTController(
    broker=getattr(sec, "MQTT_BROKER", ""),
    port=getattr(sec, "MQTT_PORT", 1883),
    user=getattr(sec, "MQTT_USER", ""),
    password=getattr(sec, "MQTT_PASSWORD", "")
)

# ==========================================
#        HILFSFUNKTIONEN
# ==========================================

def get_sensor_id_by_mac(mac: str):
    """Sucht den Sensor-Namen anhand seiner BLE-MAC-Adresse (immun gegen Formatierungsfehler)."""
    if not mac: 
        return None
        
    mac_clean = mac.replace(":", "").lower()
    
    for s_id, node in engine.sensors.items():
        for node_mac in node.ble_addresses:
            if node_mac.replace(":", "").lower() == mac_clean:
                return s_id
    return None

def cm_to_gps(x_cm, y_cm):
    latitude = ORIGIN_LAT + (float(y_cm) / 100.0) / M_PER_DEG_LAT
    longitude = ORIGIN_LON + (float(x_cm) / 100.0) / M_PER_DEG_LON
    return round(latitude, 7), round(longitude, 7)

def get_active_sensors():
    """Lädt die Sensordaten aus dem ConfigStore für die Engine."""
    sensors = {}
    for s_id, s_config in CONFIG_STORE.load().items():
        if s_config.data.get("enabled", True):
            sensors[s_id] = dict(s_config.data)
    return sensors

def check_target_tag(reading: SensorReading) -> bool:
    """Sperrt das System auf ein spezifisches Zielobjekt."""
    global OBSERVED_TAG_ID
    if TARGET_TAG_ID is not None:
        return reading.tag_id == str(TARGET_TAG_ID)
    if reading.tag_id is None:
        return OBSERVED_TAG_ID is None
    if OBSERVED_TAG_ID is None:
        OBSERVED_TAG_ID = reading.tag_id
        print(f"BLE-Tag automatisch ausgewählt: {OBSERVED_TAG_ID}")
        return True
    return reading.tag_id == OBSERVED_TAG_ID

def print_and_publish_result(result):
    """Loggt die Ausgabe schön in die Konsole und publiziert via MQTT."""
    global last_output_was_active

    if result.state == "inaktiv":
        if not last_output_was_active:
            return  # Spam vermeiden
        print("Keine gültige Position. System inaktiv.")
        mqtt_ctrl.publish(STATE_TOPIC_GPS, {
            "state": "inaktiv", 
            "attributes": {
                "active_sensors": 0,
                # Hier nehmen wir jetzt die ECHTEN Daten aus dem Result-Objekt!
                "contributing_sensors": result.contributing_sensors,
                "rejected_sensors": result.rejected_sensors,
                "inactive_sensors": result.inactive_sensors,
                "sensor_measurements": result.sensor_measurements,
                "radio_environment": result.radio_diagnostics,
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            }
        }, retain=True)
        last_output_was_active = False
        return

    # NEU: Sicherheitsleine gegen None-Type Abstürze!
    safe_accuracy = result.accuracy_cm if result.accuracy_cm is not None else 9999.0
    
    # GPS berechnen
    lat, lon = cm_to_gps(result.x_cm, result.y_cm)

    # Konsolen-Ausgabe (Logger)
    print(f"Tracking Lola -> X: {result.x_cm:.1f} cm | Y: {result.y_cm:.1f} cm | "
          f"GPS: {lat}, {lon} | "
          f"Sensoren: {result.active_sensors_count} | Genauigkeit: {safe_accuracy:.0f} cm | "
          f"Beitrag: {', '.join(result.contributing_sensors)}")
    
    for m in result.sensor_measurements:
        used_str = "" if m['used'] else f" ({m['reason']})"
        print(f"  {m['sensor']}: RSSI {m['raw_rssi']} -> {m['filtered_rssi']} dBm, "
              f"{m['distance_cm']} cm, Funkqualität {m['radio_quality']:.2f}{used_str}")
    
    if result.estimate_rejected:
        print(f"  [!] Positionsupdate verworfen: {result.rejection_reason}")

    # MQTT Ausgabe
    mqtt_ctrl.publish(STATE_TOPIC_GPS, {
        "state": "aktiv",
        "attributes": {
            "latitude": lat,
            "longitude": lon,
            "x_cm": round(result.x_cm, 1),
            "y_cm": round(result.y_cm, 1),
            "gps_accuracy": round(safe_accuracy / 100.0, 1),
            "position_uncertainty_cm": round(safe_accuracy, 1),
            "active_sensors": result.active_sensors_count,
            "contributing_sensors": result.contributing_sensors,
            "rejected_sensors": result.rejected_sensors,
            "inactive_sensors": result.inactive_sensors,
            "sensor_measurements": result.sensor_measurements,
            "estimate_rejected": result.estimate_rejected,
            "rejection_reason": result.rejection_reason,
            "radio_environment": result.radio_diagnostics,
            "timestamp_utc": datetime.now(timezone.utc).isoformat()
        }
    }, retain=True)
    last_output_was_active = True

# ==========================================
#        MQTT CALLBACKS & ROUTING
# ==========================================

def on_mqtt_connect():
    """Wird vom MQTTController aufgerufen, sobald die Verbindung steht."""
    mqtt_ctrl.subscribe("bluecat/#")
    
    # Home Assistant Auto-Discovery senden
    discovery_messages = HADiscoveryBuilder.build_all(get_active_sensors())
    mqtt_ctrl.publish_multiple(discovery_messages)
    
    # Mesh Peers publizieren
    mqtt_ctrl.publish(MESH_PEERS_TOPIC, mesh_peers_payload(CONFIG_STORE.sensors), retain=True)

def handle_config_message(topic: str, payload_str: str):
    """Verarbeitet HA/MQTT-Konfigurationsänderungen."""
    global OBSERVED_TAG_ID
    if topic == TARGET_MAC_STATE_TOPIC:
        return
    if topic.startswith(CONFIG_ROOT + "/") and topic.endswith("/state"):
        return
        
    if topic == TARGET_MAC_SET_TOPIC:
        try:
            target_mac = ""
            if payload_str:
                parsed = json.loads(payload_str) if payload_str.startswith("{") else payload_str
                target_mac = parsed.get("target_mac", parsed.get("mac", "")) if isinstance(parsed, dict) else str(parsed)
                target_mac = ":".join(normalize_ble_address(target_mac)[i:i + 2] for i in range(0, 12, 2))
            
            CONFIG_STORE.target_mac = target_mac
            mqtt_ctrl.publish(TARGET_MAC_STATE_TOPIC, target_mac, retain=True)
            print(f"Zielobjekt-MAC aktualisiert: {target_mac}")
        except Exception as e:
            print(f"Ungültige Zielobjekt-MAC: {e}")
        return

    pattern = re.compile(r"^bluecat/config/sensors/([^/]+)/(ble_mac|position|position_x|position_y|calibration|calibration_[a-z_]+|enabled|name|implementation)/set$")
    match = pattern.match(topic)
    if not match:
        return
        
    sensor_id, field = match.groups()
    try:
        if sensor_id not in CONFIG_STORE.sensors:
            CONFIG_STORE.upsert_identity({"sensor_id": sensor_id, "name": sensor_id})
            
        value = payload_str
        if field not in {"ble_mac", "position_x", "position_y"} and isinstance(payload_str, str) and payload_str.startswith("{"):
            value = json.loads(payload_str)
            
        config_data = CONFIG_STORE.sensors[sensor_id].data
        
        # --- FEHLENDE MAPPING-LOGIK FÜR EINZELNE FELDER WIEDERHERGESTELLT ---
        if field in {"position_x", "position_y"}:
            position = list(config_data.get("pos", [0.0, 0.0]))
            position[0 if field.endswith("_x") else 1] = float(value)
            value = position
            field = "position"
        elif field.startswith("calibration_") and field != "calibration":
            calibration = {
                key: config_data.get(key, 0.0)
                for key in ("tx_power", "n_factor", "r_min", "r_max", "rssi_limit", "q_variance")
            }
            calibration[field[len("calibration_"):]] = float(value)
            value = calibration
            field = "calibration"
        # --------------------------------------------------------------------
            
        config = CONFIG_STORE.update_field(sensor_id, field, value)
        
        # Den neuen Zustand an MQTT/HA zurückmelden
        if field == "ble_mac":
            state_val = config.data["ble_addresses"][0] if config.data["ble_addresses"] else ""
            mqtt_ctrl.publish(MESH_PEERS_TOPIC, mesh_peers_payload(CONFIG_STORE.sensors), retain=True)
        elif field == "position":
            state_val = {"x_cm": float(config.data["pos"][0]), "y_cm": float(config.data["pos"][1])}
        elif field == "calibration":
            state_val = {k: config.data[k] for k in ("tx_power", "n_factor", "r_min", "r_max", "rssi_limit", "q_variance")}
        else:
            state_val = value

        mqtt_ctrl.publish(SENSOR_CONFIG_STATE_PATTERN.format(sensor_id=sensor_id, field=field), state_val, retain=True)

        # WICHTIG: Engine mit neuem Setup füttern!
        engine.setup_sensors(get_active_sensors())
        
        # Discovery aktualisieren
        discovery_messages = HADiscoveryBuilder.build_all(get_active_sensors())
        mqtt_ctrl.publish_multiple(discovery_messages)
        print(f"Konfiguration aktualisiert: {sensor_id}/{field}")
        
    except Exception as e:
        print(f"Ungültige Konfiguration {topic}: {e}")

def handle_identity_message(payload_str: str):
    """Registriert neue Sensoren, die sich im Netzwerk melden."""
    try:
        payload = json.loads(payload_str)
        config = CONFIG_STORE.upsert_identity(payload)
        engine.setup_sensors(get_active_sensors())
        
        discovery_messages = HADiscoveryBuilder.build_all(get_active_sensors())
        mqtt_ctrl.publish_multiple(discovery_messages)
        
        mqtt_ctrl.publish(MESH_PEERS_TOPIC, mesh_peers_payload(CONFIG_STORE.sensors), retain=True)
        print(f"Neuer Sensor registriert: {config.sensor_id}")
    except Exception as e:
        print(f"Ungültige Sensor-Identität: {e}")

def on_mqtt_message(topic: str, payload_str: str):
    """Die zentrale Weiche für alle eingehenden MQTT-Nachrichten."""
    
    if topic.startswith("bluecat/registry/") and topic.endswith("/identity"):
        handle_identity_message(payload_str)
        return
    
    # 1. Konfiguration?
    if topic.startswith(CONFIG_ROOT + "/") or topic.startswith("bluecat/registry/"):
        handle_config_message(topic, payload_str)
        return

    if topic == MESH_PEERS_TOPIC:
        return

    # 2. Payload Parsen! (Keine verschachtelten if-Wüsten mehr)
    parsed = PayloadParser.parse(payload_str)
    if parsed is None:
        return

    with state_lock:
        now = time.monotonic()
        
        # 3. Ist es ein SLAM Mesh Beacon?
        if isinstance(parsed, MeshBeacon):
            # EMPFÄNGER ZWINGEND ÜBER DAS TOPIC BESTIMMEN (wie im Monolithen)
            receiver_id = None
            for s_id, node in engine.sensors.items():
                if node.topic == topic:
                    receiver_id = s_id
                    break
            
            if not receiver_id:
                return  # Ignorieren, wenn Topic zu keinem aktiven Sensor gehört

            # SENDER ÜBER MAC-ADRESSE (bevorzugt) ODER NAME BESTIMMEN
            transmitter_id = parsed.transmitter_name
            if parsed.transmitter_mac:
                mapped_id = get_sensor_id_by_mac(parsed.transmitter_mac)
                if mapped_id:
                    transmitter_id = mapped_id
                    
            if transmitter_id and receiver_id:
                engine.observe_mesh(receiver_id, transmitter_id, parsed.rssi, now)
            return
            # ----------------------------------------------------------
                    
            if transmitter_id and receiver_id:
                engine.observe_mesh(receiver_id, transmitter_id, parsed.rssi, now)
            return

        # 4. Es ist ein Tracking-Ping (SensorReading)
        if isinstance(parsed, SensorReading):
            target_sensor_id = parsed.sensor_id
            
            # Fallback: Über das Topic den Sensor finden, falls die ID fehlt
            if not target_sensor_id:
                for s_id, node in engine.sensors.items():
                    if node.topic == topic:
                        target_sensor_id = s_id
                        break
                        
            if not target_sensor_id or target_sensor_id not in engine.sensors:
                return
                
            if not check_target_tag(parsed):
                return
                
            node = engine.sensors[target_sensor_id]
            is_new = node.apply_reading(parsed.rssi, now, parsed.present, parsed.sequence, parsed.timestamp)
            
            if is_new:
                # Das Gehirn rechnen lassen!
                result = engine.process_tick(now)
                print_and_publish_result(result)

# ==========================================
#        HOUSEKEEPING & SHUTDOWN
# ==========================================

def housekeeping_loop():
    """Taktgeber für Timeouts und Heatmap-Speicherung."""
    counter = 0
    while not stop_event.wait(1.0):
        with state_lock:
            result = engine.process_tick(time.monotonic())
            if result.state == "inaktiv":
                print_and_publish_result(result)
                
        counter += 1
        if counter >= 300:  # Alle 5 Minuten
            if engine.grid_map:
                engine.grid_map.export_heatmap(os.path.join(CONFIG_DIR, "radio_heatmap.json"))
            counter = 0

def sigterm_handler(signum, frame):
    print("\nSystem-Stop empfangen. Sicheres Beenden...")
    stop_event.set()

# ==========================================
#        START
# ==========================================

if __name__ == "__main__":
    if "X.X" in str(getattr(sec, "MQTT_BROKER", "")):
        raise SystemExit("Bitte secrets_tri.py mit einem echten MQTT_BROKER anlegen.")

    print("Starte TriLola Tracking Engine (OOP Architektur)...")
    
    # Engine mit Daten füttern
    engine.setup_sensors(get_active_sensors())
    
    # MQTT verdrahten und starten
    mqtt_ctrl.on_connect_callback = on_mqtt_connect
    mqtt_ctrl.on_message_callback = on_mqtt_message
    
    signal.signal(signal.SIGTERM, sigterm_handler)
    
    try:
        mqtt_ctrl.start()
        
        # Housekeeping Thread starten
        housekeeping = threading.Thread(target=housekeeping_loop, daemon=True)
        housekeeping.start()
        
        # Haupt-Thread blockieren, bis stop_event gesetzt wird (Strg+C oder SIGTERM)
        while not stop_event.is_set():
            time.sleep(0.5)
            
    except KeyboardInterrupt:
        print("\nAbbruch durch Benutzer.")
    finally:
        stop_event.set()
        mqtt_ctrl.stop()
        
        print("Speichere SLAM Baselines und Heatmap...")
        try:
            if engine.radio_env:
                engine.radio_env.save_baseline()
            if engine.grid_map:
                engine.grid_map.export_heatmap(os.path.join(CONFIG_DIR, "radio_heatmap.json"))
        except OSError as error:
            print(f"Fehler beim Speichern: {error}")
        
        print("TriLola erfolgreich beendet.")


