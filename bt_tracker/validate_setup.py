"""Prüft die lokale TriLola-Grundkonfiguration ohne externe Laufzeitpakete.

Das Skript ist für die Inbetriebnahme gedacht. Es prüft JSON-Dateien,
eindeutige Topics/MACs und die lokale Python-Konfiguration, startet aber
keinen MQTT-, BLE- oder Hardwaretest.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

from config_manager import ConfigStore


BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / "config"


def check_python_file(path: Path, required: bool = False) -> bool:
    if not path.exists():
        level = "FEHLER" if required else "HINWEIS"
        print(f"{level}: {path.relative_to(BASE_DIR)} fehlt.")
        return not required
    try:
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as error:
        print(f"FEHLER: {path.relative_to(BASE_DIR)} ist ungültig: {error}")
        return False
    print(f"OK: {path.relative_to(BASE_DIR)}")
    return True


def main() -> int:
    print("TriLola-Konfigurationsprüfung")
    print("=" * 32)
    store = ConfigStore(str(CONFIG_DIR))
    sensors = store.load()
    if not sensors:
        print("FEHLER: Keine aktivierte Sensorkonfiguration gefunden.")
        return 1

    errors = 0
    warnings = 0
    for sensor_id, sensor in sorted(sensors.items()):
        data = sensor.data
        if not data.get("position_configured", False):
            warnings += 1
            print(
                f"HINWEIS: {sensor_id} hat noch keine konfigurierte Position."
            )
        if not data.get("ble_addresses"):
            warnings += 1
            print(
                f"HINWEIS: {sensor_id} hat noch keine BLE-MAC; "
                "die automatische Identity-Registrierung wird erwartet."
            )
        print(
            f"OK: {sensor_id} | {data['implementation']} | "
            f"{data['topic']} | Position={data['pos']}"
        )

    required_files = [
        (BASE_DIR / "secrets_tri.py", True),
        (BASE_DIR / "bt_sensor" / "raspberry_pi_unix" / "secrets_blue.py", False),
    ]
    for path, required in required_files:
        if not check_python_file(path, required=required):
            errors += 1

    print()
    print(f"Ergebnis: {len(sensors)} Sensor(en), {warnings} Hinweis(e), {errors} Fehler.")
    if errors:
        print("Vor dem Start müssen die Fehler behoben werden.")
        return 1
    print(
        "Die lokale Konfiguration ist syntaktisch plausibel. "
        "MQTT-, BLE-, HA- und Hardwaretests stehen noch aus."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
