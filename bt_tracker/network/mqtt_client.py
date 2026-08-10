import json
import paho.mqtt.client as mqtt
from typing import Callable, Any

class MQTTController:
    """Kapselt die gesamte Paho-MQTT Logik und Netzwerkverbindung."""
    
    def __init__(self, broker: str, port: int, user: str = "", password: str = "", client_id: str = "bluecat_trilola"):
        self.broker = broker
        self.port = port
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=client_id)
        
        if user and password:
            self.client.username_pw_set(user, password)
            
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)
        
        # Callbacks für die Hauptanwendung (main.py)
        self.on_message_callback: Callable[[str, str], None] = None
        self.on_connect_callback: Callable[[], None] = None

    def start(self):
        """Startet den MQTT-Client im Hintergrund-Thread."""
        print(f"Verbinde zu MQTT Broker {self.broker}:{self.port}...")
        self.client.connect(self.broker, self.port, 60)
        self.client.loop_start()

    def stop(self):
        """Stoppt den MQTT-Loop sicher."""
        self.client.loop_stop()
        self.client.disconnect()

    def subscribe(self, topic: str, qos: int = 1):
        self.client.subscribe(topic, qos=qos)

    def publish(self, topic: str, payload: Any, retain: bool = False, qos: int = 1):
        """Nimmt Dictionaries oder Strings und publiziert sie sicher."""
        if not isinstance(payload, str):
            payload = json.dumps(payload)
        self.client.publish(topic, payload, retain=retain, qos=qos)

    def publish_multiple(self, messages: list):
        """Veröffentlicht eine Liste von (topic, payload, retain) Tupeln auf einmal."""
        for topic, payload, retain in messages:
            self.publish(topic, payload, retain=retain)

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            print("MQTT Verbunden!")
            if self.on_connect_callback:
                self.on_connect_callback()
        else:
            print(f"Fehler bei MQTT Verbindung: {rc}")

    def _on_disconnect(self, client, userdata, rc):
        if rc != 0:
            print(f"MQTT getrennt (Code: {rc}), versuche Reconnect...")

    def _on_message(self, client, userdata, msg):
        if self.on_message_callback:
            try:
                decoded = msg.payload.decode("utf-8").strip()
                self.on_message_callback(msg.topic, decoded)
            except Exception as e:
                print(f"Fehler beim Dekodieren der MQTT-Nachricht auf {msg.topic}: {e}")