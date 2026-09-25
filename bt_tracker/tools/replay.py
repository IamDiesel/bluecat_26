"""Aufzeichnung (RECORD_FILE) mit einem oder beiden Modellen nachspielen.

    # im Tracker aufzeichnen: in secrets_tri.py  RECORD_FILE = "recordings/lola.jsonl"
    python -m tools.replay recordings/lola.jsonl                  # pf + legacy
    python -m tools.replay recordings/lola.jsonl --engines pf --csv out.csv

Die Konfiguration wird in ein temporäres Verzeichnis kopiert – das echte
``config/`` bleibt unverändert. Ausgegeben werden Kennzahlen je Modell und
optional eine CSV mit allen publizierten Positionen (für Plots/GUI).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import numpy as np  # noqa: E402

try:
    import secrets_tri as sec  # noqa: E402
except ModuleNotFoundError:
    import secrets_tri_dummy as sec  # noqa: E402

from network.ha_discovery import STATE_TOPIC_GPS  # noqa: E402
from tracker_app import TriLolaApp  # noqa: E402


class CollectingPublisher:
    def __init__(self):
        self.positions = []
        self.clock_value = 0.0

    def subscribe(self, topic, qos=1):
        pass

    def publish(self, topic, payload, retain=False, qos=1):
        if topic == STATE_TOPIC_GPS and isinstance(payload, dict):
            attrs = payload.get("attributes", {})
            self.positions.append((self.clock_value, payload.get("state"), attrs.get("x_cm"), attrs.get("y_cm"),
                                   attrs.get("position_uncertainty_cm"), attrs.get("room")))


def replay(path, engine, config_dir, params_override=None, include_config=False):
    tmp = tempfile.mkdtemp(prefix="trilola_replay_")
    shutil.copytree(config_dir, os.path.join(tmp, "config"))

    class Sec:
        pass

    s = Sec()
    for key in dir(sec):
        if not key.startswith("__"):
            setattr(s, key, getattr(sec, key))
    s.TRACKING_ENGINE = engine
    s.RECORD_FILE = ""
    s.LOG_POSITIONS = False
    s.RADIO_BASELINE_FILE = os.path.join(tmp, "config", "radio_mesh_baseline.json")
    s.FLOORPLAN_FILE = os.path.join(tmp, "config", "floorplan.json")
    for key, value in (params_override or {}).items():
        setattr(s, key, value)
    publisher = CollectingPublisher()
    clock = {"t": 0.0}
    app = TriLolaApp(tmp, s, publisher, config_dir=os.path.join(tmp, "config"), clock=lambda: clock["t"])
    app.store.engine = engine
    app._switch_engine(engine)
    next_tick = None
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("topic", "").startswith("bluecat/config/") and not include_config:
                continue  # Bedienaktionen (Modellwechsel, Kalibrierung …) nicht nachspielen
            t = float(rec["t"])
            if next_tick is None:
                next_tick = t + 1.0
            while next_tick <= t:
                clock["t"] = publisher.clock_value = next_tick
                app.housekeeping(next_tick)
                next_tick += 1.0
            clock["t"] = publisher.clock_value = t
            app.on_message(rec["topic"], rec["payload"], bool(rec.get("retain")), now=t)
    shutil.rmtree(tmp, ignore_errors=True)
    return publisher.positions


def summarize(name, positions):
    active = [p for p in positions if p[1] == "aktiv" and p[2] is not None]
    if not active:
        print(f"{name}: keine aktiven Positionen")
        return
    xy = np.array([[p[2], p[3]] for p in active], dtype=float)
    acc = np.array([p[4] or np.nan for p in active], dtype=float)
    steps = np.linalg.norm(np.diff(xy, axis=0), axis=1) if len(xy) > 1 else np.array([0.0])
    rooms = {}
    for p in active:
        rooms[p[5] or "?"] = rooms.get(p[5] or "?", 0) + 1
    print(f"{name:8s} Positionen {len(active):6d} | Median-Sprung {np.median(steps):6.1f} cm | "
          f"95%-Sprung {np.percentile(steps, 95):6.1f} cm | Median ±{np.nanmedian(acc):5.0f} cm | Räume {rooms}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("recording")
    parser.add_argument("--engines", nargs="+", default=["pf", "legacy"])
    parser.add_argument("--config", default=os.path.join(HERE, "config"))
    parser.add_argument("--csv")
    parser.add_argument("--params", default="", help='JSON, z. B. {"PF_PARTICLES": 800}')
    parser.add_argument("--include-config", action="store_true",
                        help="auch aufgezeichnete Konfigurationsänderungen nachspielen")
    args = parser.parse_args()
    override = json.loads(args.params) if args.params else {}
    results = {engine: replay(args.recording, engine, args.config, override, args.include_config)
               for engine in args.engines}
    for engine, positions in results.items():
        summarize(engine, positions)
    if len(results) == 2:
        a, b = (results[e] for e in args.engines)
        pa = {round(p[0]): (p[2], p[3]) for p in a if p[1] == "aktiv" and p[2] is not None}
        pb = {round(p[0]): (p[2], p[3]) for p in b if p[1] == "aktiv" and p[2] is not None}
        common = sorted(set(pa) & set(pb))
        if common:
            d = [np.hypot(pa[t][0] - pb[t][0], pa[t][1] - pb[t][1]) for t in common]
            print(f"Abstand {args.engines[0]}↔{args.engines[1]}: Median {np.median(d):.0f} cm, 90% {np.percentile(d, 90):.0f} cm")
    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["t", "engine", "state", "x_cm", "y_cm", "accuracy_cm", "room"])
            for engine, positions in results.items():
                for p in positions:
                    writer.writerow([p[0], engine, *p[1:]])
        print(f"CSV geschrieben: {args.csv}")


if __name__ == "__main__":
    main()
