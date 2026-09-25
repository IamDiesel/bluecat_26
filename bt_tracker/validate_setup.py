"""Prüft die lokale TriLola-Grundkonfiguration (ohne MQTT/BLE).

    python validate_setup.py
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

from config_manager import ConfigStore
from core.floorplan import FloorPlan

BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / "config"


def check_python_file(path: Path, required: bool = False) -> bool:
    if not path.exists():
        print(f"{'FEHLER' if required else 'HINWEIS'}: {path.relative_to(BASE_DIR)} fehlt.")
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
    errors = warnings = 0
    if not sensors:
        print("FEHLER: Keine Sensorkonfiguration gefunden.")
        return 1
    positions = {}
    for sensor_id, sensor in sorted(sensors.items()):
        data = sensor.data
        flags = []
        if not data.get("enabled", True):
            flags.append("deaktiviert")
        if not data.get("position_configured", False):
            flags.append("Position fehlt → nicht im Tracking")
            warnings += 1
        if not data.get("ble_addresses"):
            flags.append("keine BLE-MAC (Mesh)")
            warnings += 1
        if data.get("calibration_status") in (None, "uncalibrated", "needs_recalibration"):
            flags.append(f"Kalibrierung: {data.get('calibration_status')}")
            warnings += 1
        if data.get("tx_power") == -59.0 and data.get("n_factor") == 3.0:
            flags.append("Standard-Kalibrierung (-59 dBm / n=3) – kalibrieren")
            warnings += 1
        if data.get("r_min", 0) > data.get("r_max", 0):
            flags.append("r_min > r_max (nah verrauschter als fern – neu kalibrieren?)")
        pos = tuple(data["pos"])
        if data.get("position_configured") and pos in positions:
            flags.append(f"gleiche Position wie {positions[pos]}")
            warnings += 1
        positions.setdefault(pos, sensor_id)
        print(f"{'OK  ' if not flags else 'WARN'} {sensor_id:28s} {data['implementation']:12s} pos={data['pos']} "
              f"tx={data['tx_power']} n={data['n_factor']} σ={data.get('sigma_db')}" + (f"  → {'; '.join(flags)}" if flags else ""))

    fp_path = CONFIG_DIR / "floorplan.json"
    if fp_path.exists():
        fp = FloorPlan.load(str(fp_path))
        print(f"OK: Grundriss mit {len(fp.wall_db)} Wänden und {len(fp.rooms)} Räumen.")
        if fp.has_rooms:
            for sensor_id, sensor in sensors.items():
                if sensor.data.get("position_configured") and fp.room_name(sensor.data["pos"]) is None:
                    print(f"WARN: {sensor_id} liegt außerhalb aller Räume (Grundriss/Koordinaten prüfen).")
                    warnings += 1
    else:
        print("HINWEIS: Kein config/floorplan.json – Tracking ohne Wände/Räume (mit GUI anlegen).")

    if (BASE_DIR / "secrets").is_dir():
        print("WARN: Ordner 'secrets/' verdeckt das Python-Standardmodul 'secrets'. "
              "ORIGIN_LAT/LON nach secrets_tri.py übernehmen und den Ordner entfernen.")
        warnings += 1
    if not check_python_file(BASE_DIR / "secrets_tri.py", required=True):
        errors += 1
    else:
        tree = ast.parse((BASE_DIR / "secrets_tri.py").read_text(encoding="utf-8"))
        names = {t.id for node in ast.walk(tree) if isinstance(node, ast.Assign) for t in node.targets if isinstance(t, ast.Name)}
        if not {"ORIGIN_LAT", "ORIGIN_LON"} <= names:
            print("HINWEIS: ORIGIN_LAT/ORIGIN_LON fehlen in secrets_tri.py (GPS für die HA-Karte).")

    print()
    print(f"Ergebnis: {len(sensors)} Sensor(en), {warnings} Hinweis(e), {errors} Fehler.")
    return 1 if errors else 0


if __name__ == "__main__":
    os.chdir(BASE_DIR)
    raise SystemExit(main())
