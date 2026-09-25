"""TriLola – zentraler Tracker (MQTT-Verdrahtung).

Die eigentliche Logik steckt in ``tracker_app.TriLolaApp``. Dieses Skript
lädt ``secrets_tri.py``, verbindet MQTT (mit Last Will) und startet den
Housekeeping-Takt.
"""

import os
import signal
import sys
import threading
import time

# Im Home-Assistant-Add-on liegen Zugangsdaten und config/ außerhalb des Programmordners
TRILOLA_HOME = os.environ.get("TRILOLA_HOME")
if TRILOLA_HOME:
    sys.path.insert(0, TRILOLA_HOME)

try:
    import secrets_tri as sec
except ModuleNotFoundError:
    import secrets_tri_dummy as sec
except SyntaxError as error:
    raise SystemExit("secrets_tri.py enthält einen Syntaxfehler.") from error

from network.ha_discovery import AVAILABILITY_TOPIC
from network.mqtt_client import MQTTController
from tracker_app import TriLolaApp

BASE_DIR = TRILOLA_HOME or os.path.dirname(os.path.abspath(__file__))


def main():
    if "X.X" in str(getattr(sec, "MQTT_BROKER", "")):
        raise SystemExit("Bitte secrets_tri.py mit einem echten MQTT_BROKER anlegen.")

    print("Starte TriLola Tracking Engine...")
    mqtt_ctrl = MQTTController(
        broker=getattr(sec, "MQTT_BROKER", ""),
        port=getattr(sec, "MQTT_PORT", 1883),
        user=getattr(sec, "MQTT_USER", ""),
        password=getattr(sec, "MQTT_PASSWORD", ""),
        client_id=getattr(sec, "MQTT_CLIENT_ID", "bluecat_trilola"),
        will_topic=AVAILABILITY_TOPIC,
    )
    app = TriLolaApp(BASE_DIR, sec, mqtt_ctrl)
    print(f"Modell: {app.engine_kind} | Sensoren im Tracking: {len(app.engine.sensors)} "
          f"von {len(app.store.sensors)} konfigurierten")

    stop_event = threading.Event()
    mqtt_ctrl.on_connect_callback = app.on_connect
    mqtt_ctrl.on_message_callback = app.on_message

    def housekeeping_loop():
        while not stop_event.wait(1.0):
            try:
                app.housekeeping()
            except Exception as error:  # Thread darf nie still sterben
                print(f"Unerwarteter Fehler im Housekeeping: {error}")

    def handle_signal(signum, frame):
        print("\nSystem-Stop empfangen. Sicheres Beenden...")
        stop_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    try:
        mqtt_ctrl.start()
        worker = threading.Thread(target=housekeeping_loop, name="housekeeping", daemon=True)
        worker.start()
        while not stop_event.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nAbbruch durch Benutzer.")
    finally:
        stop_event.set()
        print("Speichere Mesh-Baselines...")
        app.shutdown()
        time.sleep(0.3)  # Offline-Status noch zustellen
        mqtt_ctrl.stop()
        print("TriLola erfolgreich beendet.")


if __name__ == "__main__":
    main()
