"""Kalibrierplan über MQTT: Messpunkte sammeln, gepoolt fitten, übernehmen.

Die Rollout-Oberfläche schickt Befehle an ``bluecat/config/calibration/set``;
der Tracker sammelt während einer Messung alle Halsband-Fenster je Sensor
(Median + Anzahl, wie ``calibrate_sensor.py``) und legt die Punkte in
``config/calibration_points.json`` ab – dasselbe Format wie das Konsolenwerkzeug.
"""

from __future__ import annotations

import json
import math
import os
import time
import uuid
from typing import Dict, Optional

from calibration_model import fit_pooled, robust_point_statistics

MAX_POINTS = 60
MIN_DURATION, MAX_DURATION = 20.0, 600.0


class CalibrationSession:
    def __init__(self, config_dir: str, clock=time.monotonic):
        self.path = os.path.join(config_dir, "calibration_points.json")
        self.clock = clock
        self.data = self._load()
        self.active: Optional[dict] = None
        self.fit: Optional[dict] = None
        self.autocal: Optional[dict] = None
        self.message = ""

    # ------------------------------------------------------------------
    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, dict) or not isinstance(data.get("points"), list):
                raise ValueError
        except (OSError, ValueError, json.JSONDecodeError):
            data = {"points": []}
        for point in data["points"]:
            point.setdefault("id", uuid.uuid4().hex[:8])
        return data

    def _save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(self.data, handle, indent=1)
        os.replace(tmp, self.path)

    # ------------------------------------------------------------------
    def start(self, x, y, duration_s=90.0, point_id=None, label="", z=None):
        """z: Höhe des Halsbands über dem Fußboden (cm); None = Halsbandhöhe aus den Einstellungen."""
        x, y, duration = float(x), float(y), float(duration_s)
        if not all(math.isfinite(v) for v in (x, y, duration)):
            raise ValueError("ungültige Zahl")
        if z is not None:
            z = float(z)
            if not (math.isfinite(z) and 0.0 <= z <= 600.0):
                raise ValueError("ungültige Höhe")
        if len(self.data["points"]) >= MAX_POINTS:
            raise ValueError("zu viele Messpunkte")
        now = self.clock()
        self.active = {
            "id": str(point_id or uuid.uuid4().hex[:8])[:32],
            "label": str(label or "")[:40],
            "x": round(x, 1), "y": round(y, 1), **({"z": round(z, 1)} if z is not None else {}),
            "duration_s": max(MIN_DURATION, min(duration, MAX_DURATION)),
            "started": now, "samples": {},
        }
        self.message = ""

    def cancel(self):
        self.active = None
        self.message = "Messung abgebrochen."

    def on_reading(self, sensor_id: str, rssi, present: bool, sample_count=1):
        if self.active is None or not present or rssi is None:
            return
        try:
            rssi = float(rssi)
        except (TypeError, ValueError):
            return
        if not math.isfinite(rssi) or rssi <= -120.0:
            return
        self.active["samples"].setdefault(sensor_id, []).append([round(rssi, 1), int(max(sample_count or 1, 1))])

    def tick(self) -> bool:
        """True, wenn eine Messung gerade fertig geworden ist."""
        if self.active is None or self.clock() - self.active["started"] < self.active["duration_s"]:
            return False
        active, self.active = self.active, None
        # Punkt mit derselben ID ersetzen (Messung wiederholt)
        self.data["points"] = [p for p in self.data["points"] if p.get("id") != active["id"]]
        self.data["points"].append({
            "id": active["id"], "label": active["label"], "x": active["x"], "y": active["y"],
            **({"z": active["z"]} if "z" in active else {}),
            "duration_s": active["duration_s"], "time": time.time(), "samples": active["samples"],
        })
        self._save()
        seen = sum(1 for v in active["samples"].values() if len(v) >= 3)
        self.message = f"Messpunkt gespeichert: {seen} Sensoren mit genug Daten."
        self.fit = None
        return True

    def delete(self, point_id):
        before = len(self.data["points"])
        self.data["points"] = [p for p in self.data["points"] if p.get("id") != point_id]
        if len(self.data["points"]) != before:
            self._save()
            self.fit = None

    def clear(self):
        self.data = {"points": []}
        self._save()
        self.fit = None

    # ------------------------------------------------------------------
    def run_fit(self, positions: Dict[str, list], sensor_configs: Dict[str, dict], floorplan=None,
                per_sensor_n=False, sensor_heights=None, tag_height_cm=0.0):
        samples = {sid: [] for sid in positions}
        for point in self.data["points"]:
            for sid, windows in (point.get("samples") or {}).items():
                if sid in samples:
                    where = (point["x"], point["y"]) + ((point["z"],) if point.get("z") is not None else ())
                    samples[sid].append((where, [tuple(w) for w in windows]))
        usable_points = sum(1 for p in self.data["points"]
                            if sum(1 for w in (p.get("samples") or {}).values() if len(w) >= 3) >= 2)
        if usable_points < 3:
            raise ValueError("Mindestens 3 Messpunkte mit Daten nötig (empfohlen 8–15).")
        configs, glob = fit_pooled(samples, positions, floorplan=floorplan, per_sensor_n=per_sensor_n,
                                   sensor_heights=sensor_heights, point_height_cm=tag_height_cm)
        result = {"globals": glob, "sensors": {}, "time": time.time()}
        for sid, cal in configs.items():
            old = sensor_configs.get(sid, {})
            result["sensors"][sid] = {
                **cal,
                "current_tx_power": old.get("tx_power"),
                "current_n_factor": old.get("n_factor"),
                "current_sigma_db": old.get("sigma_db"),
            }
        self.fit = result
        return result

    def state(self) -> dict:
        now = self.clock()
        points = []
        for p in self.data["points"]:
            summary = {}
            for sid, windows in (p.get("samples") or {}).items():
                if windows:
                    mean, _, count = robust_point_statistics([tuple(w) for w in windows])
                    summary[sid] = [round(float(mean), 1), int(count)]
            points.append({"id": p.get("id"), "label": p.get("label", ""), "x": p["x"], "y": p["y"], "z": p.get("z"),
                           "time": p.get("time"), "duration_s": p.get("duration_s"), "sensors": summary})
        active = None
        if self.active is not None:
            elapsed = now - self.active["started"]
            active = {"id": self.active["id"], "label": self.active["label"], "x": self.active["x"], "z": self.active.get("z"),
                      "y": self.active["y"], "duration_s": self.active["duration_s"],
                      "remaining_s": round(max(self.active["duration_s"] - elapsed, 0.0), 1),
                      "counts": {sid: len(v) for sid, v in self.active["samples"].items()}}
        return {"points": points, "active": active, "fit": self.fit, "autocal": self.autocal,
                "message": self.message}
