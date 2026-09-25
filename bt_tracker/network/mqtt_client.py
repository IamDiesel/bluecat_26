import json
from typing import Any, Callable, Optional

import paho.mqtt.client as mqtt


class MQTTController:
    """Kapselt die Paho-MQTT-Verbindung (paho-mqtt ≥ 2.0)."""

    def __init__(self, broker: str, port: int, user: str = "", password: str = "",
                 client_id: str = "bluecat_trilola", will_topic: Optional[str] = None,
                 will_payload: str = "offline"):
        self.broker = broker
        self.port = int(port)
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        if user:
            self.client.username_pw_set(user, password or None)
        if will_topic:
            self.client.will_set(will_topic, will_payload, qos=1, retain=True)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)
        self.on_message_callback: Optional[Callable[[str, str, bool], None]] = None
        self.on_connect_callback: Optional[Callable[[], None]] = None

    def start(self):
        """Verbindet asynchron; Paho verbindet bei Ausfällen selbst neu."""
        print(f"Verbinde zu MQTT Broker {self.broker}:{self.port}...")
        self.client.connect_async(self.broker, self.port, 60)
        self.client.loop_start()

    def stop(self):
        self.client.disconnect()
        self.client.loop_stop()

    def subscribe(self, topic: str, qos: int = 1):
        self.client.subscribe(topic, qos=qos)

    def publish(self, topic: str, payload: Any, retain: bool = False, qos: int = 1):
        if payload is None:
            payload = ""
        elif not isinstance(payload, (str, bytes)):
            payload = json.dumps(payload, ensure_ascii=False)
        self.client.publish(topic, payload, retain=retain, qos=qos)

    def publish_multiple(self, messages: list):
        for topic, payload, retain in messages:
            self.publish(topic, payload, retain=retain)

    # ------------------------------------------------------------------
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if not reason_code.is_failure:
            print("MQTT verbunden.")
            if self.on_connect_callback:
                try:
                    self.on_connect_callback()
                except Exception as error:  # pragma: no cover - Schutz des Netzwerk-Threads
                    print(f"Fehler im Connect-Handler: {error}")
        else:
            print(f"Fehler bei MQTT-Verbindung: {reason_code}")

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code != 0:
            print(f"MQTT getrennt ({reason_code}), Reconnect läuft...")

    def _on_message(self, client, userdata, msg):
        if not self.on_message_callback:
            return
        try:
            decoded = msg.payload.decode("utf-8").strip()
        except UnicodeDecodeError:
            return
        try:
            self.on_message_callback(msg.topic, decoded, bool(msg.retain))
        except Exception as error:  # pragma: no cover - Schutz des Netzwerk-Threads
            print(f"Fehler bei der Verarbeitung von {msg.topic}: {error}")
