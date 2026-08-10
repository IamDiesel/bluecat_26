from config_manager import (
    SENSOR_CONFIG_SET_PATTERN,
    SENSOR_CONFIG_STATE_PATTERN,
    TARGET_MAC_SET_TOPIC,
    TARGET_MAC_STATE_TOPIC
)

STATE_TOPIC_GPS = "bluecat/trilola/gps/state"
DISCOVERY_TOPIC_GPS = "homeassistant/sensor/bluecat_trilola_gps/config"

HA_DEVICE_CONFIG = {
    "identifiers": ["bluecat_trilola_engine"],
    "name": "Bluecat TriLola",
    "manufacturer": "Raspberry Pi",
}

class HADiscoveryBuilder:
    """Generiert alle Home Assistant Auto-Discovery MQTT-Nachrichten."""
    
    @staticmethod
    def build_all(sensor_configs: dict) -> list:
        messages = []
        
        # 1. Haupt-GPS-Sensor
        gps_payload = {
            "name": "Lola GPS Position",
            "unique_id": "bluecat_trilola_gps_sensor",
            "state_topic": STATE_TOPIC_GPS,
            "value_template": "{{ value_json.state }}",
            "json_attributes_topic": STATE_TOPIC_GPS,
            "json_attributes_template": "{{ value_json.attributes | tojson }}",
            "icon": "mdi:paw",
            "device": HA_DEVICE_CONFIG,
        }
        messages.append((DISCOVERY_TOPIC_GPS, gps_payload, True))

        # 2. Zielobjekt-MAC Config
        target_payload = {
            "name": "TriLola Zielobjekt-MAC",
            "unique_id": "bluecat_trilola_target_mac",
            "command_topic": TARGET_MAC_SET_TOPIC,
            "state_topic": TARGET_MAC_STATE_TOPIC,
            "mode": "text",
            "entity_category": "config",
            "device": HA_DEVICE_CONFIG,
        }
        messages.append(("homeassistant/text/bluecat_trilola_target_mac/config", target_payload, True))

        # 3. Sensoren iterieren
        for sensor_id, data in sensor_configs.items():
            safe_id = str(sensor_id).replace("-", "_")
            device = {
                "identifiers": [f"bluecat_sensor_{safe_id}"],
                "name": data.get("name", sensor_id),
                "manufacturer": f"Bluecat {data.get('implementation', 'Generic')}",
                "model": "BLE RSSI Sensor",
            }
            
            # WICHTIGER FALLBACK: Wenn die JSON unvollständig ist, generieren wir das Topic selbst
            avail_topic = data.get("availability_topic")
            if not avail_topic:
                avail_topic = f"bluecat/{sensor_id}/sensor/status"
            
            state_topic = data.get("topic", f"bluecat/{sensor_id}/sensor/state")

            # BLE-MAC
            ble_mac_entity = {
                "name": f"{data.get('name', sensor_id)} BLE-MAC",
                "unique_id": f"bluecat_{safe_id}_ble_mac",
                "command_topic": SENSOR_CONFIG_SET_PATTERN.format(sensor_id=sensor_id, field="ble_mac"),
                "state_topic": SENSOR_CONFIG_STATE_PATTERN.format(sensor_id=sensor_id, field="ble_mac"),
                "availability_topic": avail_topic,
                "mode": "text",
                "entity_category": "config",
                "device": device,
            }
            messages.append((f"homeassistant/text/bluecat_{safe_id}_ble_mac/config", ble_mac_entity, True))

            # RSSI
            sensor_payload = {
                "name": f"{data.get('name', sensor_id)} RSSI",
                "unique_id": f"bluecat_{safe_id}_rssi",
                "state_topic": state_topic,
                "availability_topic": avail_topic,
                "value_template": "{% if value_json.message_type is not defined or value_json.message_type == 'tag_rssi' %}{{ value_json.rssi }}{% endif %}",
                "unit_of_measurement": "dBm",
                "device_class": "signal_strength",
                "entity_category": "diagnostic",
                "device": device,
            }
            messages.append((f"homeassistant/sensor/bluecat_{safe_id}/rssi/config", sensor_payload, True))

            # Präsenz
            presence_payload = {
                "name": f"{data.get('name', sensor_id)} Präsenz",
                "unique_id": f"bluecat_{safe_id}_presence",
                "state_topic": state_topic,
                "availability_topic": avail_topic,
                "value_template": "{% if value_json.message_type is not defined or value_json.message_type == 'tag_rssi' %}{{ 'ON' if value_json.present is defined and value_json.present else ('ON' if value_json.rssi|float > -120 else 'OFF') }}{% endif %}",
                "payload_on": "ON",
                "payload_off": "OFF",
                "device_class": "presence",
                "entity_category": "diagnostic",
                "device": device,
            }
            messages.append((f"homeassistant/binary_sensor/bluecat_{safe_id}/presence/config", presence_payload, True))

            # Position X / Y
            for axis in ("x", "y"):
                entity = {
                    "name": f"{data.get('name', sensor_id)} Position {axis.upper()}",
                    "unique_id": f"bluecat_{safe_id}_position_{axis}",
                    "command_topic": SENSOR_CONFIG_SET_PATTERN.format(sensor_id=sensor_id, field=f"position_{axis}"),
                    "state_topic": SENSOR_CONFIG_STATE_PATTERN.format(sensor_id=sensor_id, field="position"),
                    "availability_topic": avail_topic,
                    "value_template": f"{{{{ value_json.{axis}_cm }}}}",
                    "unit_of_measurement": "cm",
                    "mode": "box",
                    "min": -100000.0,
                    "max": 100000.0,
                    "step": 0.1,
                    "entity_category": "config",
                    "device": device,
                }
                messages.append((f"homeassistant/number/bluecat_{safe_id}_position_{axis}/config", entity, True))

            # Kalibrierung
            for field in ("tx_power", "n_factor", "r_min", "r_max", "q_variance", "rssi_limit"):
                entity = {
                    "name": f"{data.get('name', sensor_id)} Kalibrierung {field}",
                    "unique_id": f"bluecat_{safe_id}_calibration_{field}",
                    "command_topic": SENSOR_CONFIG_SET_PATTERN.format(sensor_id=sensor_id, field=f"calibration_{field}"),
                    "state_topic": SENSOR_CONFIG_STATE_PATTERN.format(sensor_id=sensor_id, field="calibration"),
                    "availability_topic": avail_topic,
                    "value_template": f"{{{{ value_json.{field} }}}}",
                    "mode": "box",
                    "min": -200.0,
                    "max": 200.0,
                    "step": 0.001,
                    "entity_category": "config",
                    "device": device,
                }
                messages.append((f"homeassistant/number/bluecat_{safe_id}_calibration_{field}/config", entity, True))

        return messages