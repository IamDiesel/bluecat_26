"""A/B-Vergleich der Tracking-Modelle im Simulator.

Beispiele (aus ``bt_tracker/``)::

    python -m tools.ab_compare                       # Standard-Vergleich
    python -m tools.ab_compare --seeds 6 --duration 1200
    python -m tools.ab_compare --only pf_floorplan --params '{"PF_PARTICLES": 1000}'

Jede Variante läuft in einem eigenen Prozess (saubere Modul-Imports), auf
Wunsch auch gegen einen anderen Code-Stand (``--legacy-root`` zeigt z. B. auf
einen Checkout des alten Trackers).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

VARIANTS = {
    # name: (engine, calibration, firmware, floorplan)
    "legacy_fixed": ("legacy", "legacy", "current", False),
    "legacy_fixed_pooled": ("legacy", "pooled", "current", False),
    "pf_no_floorplan": ("pf", "pooled", "current", False),
    "pf_floorplan": ("pf", "pooled", "current", True),
    "pf_legacy_firmware": ("pf", "pooled", "legacy", True),
    "pf_legacy_cal": ("pf", "legacy", "current", False),
}
METRICS = ["median_rest", "rmse_rest", "mean_move", "p90_all", "jitter_rest", "room_acc", "acc_calib", "coverage", "runtime_s"]


def run_single(args):
    root = os.path.abspath(args.root)
    sys.path.insert(0, root)
    if HERE != root:
        sys.path.insert(1, HERE)
    import numpy as np
    import simulation as sim
    from calibration_model import fit_pooled

    engine_kind, calib, firmware, use_fp = args.engine, args.calibration, args.firmware, args.floorplan
    params = json.loads(args.params or "{}")
    options = sim.ScenarioOptions(duration_s=args.duration, firmware=firmware)
    for key, value in json.loads(args.scenario or "{}").items():
        setattr(options, key, tuple(value) if isinstance(value, list) else value)
    if args.drift:
        options.receiver_drift = ("shelly_wohnzimmer", args.duration * 0.4, -6.0)
        options.transmitter_drift = ("kunibert", args.duration * 0.6, -6.0)
    sc = sim.build_scenario(args.seed, options)
    samples = sim.simulate_calibration_samples(sc)
    positions = {n: p for n, (p, _) in sc.sensors.items()}
    fp_dict = sim.floorplan_dict() if use_fp else None
    floorplan = None
    if use_fp:
        from core.floorplan import FloorPlan
        floorplan = FloorPlan.from_dict(fp_dict)
    if calib == "legacy":
        cal = sim.legacy_calibration(samples, sc.sensors)
    else:
        heights = {n: sc.height_of(n) for n in sc.sensors} if options.sensor_heights else None
        cal, glob = fit_pooled(samples, positions, floorplan=floorplan, sensor_heights=heights,
                               point_height_cm=options.tag_height_cm)
        if use_fp and glob.get("wall_scale"):
            fp_dict["wall_scale"] = glob["wall_scale"]
            floorplan = FloorPlan.from_dict(fp_dict)
    configs = sim.sensor_configs(sc, cal)
    base_params = {"RADIO_BASELINE_FILE": None, "RANDOM_SEED": args.seed}
    base_params.update(params)
    if engine_kind == "legacy":
        from core.engine import TrackingEngine
        if "MAX_SNAPSHOT_SKEW_SEC" not in base_params and args.orig:
            base_params["MAX_SNAPSHOT_SKEW_SEC"] = 1.0
        if args.orig:
            base_params["RADIO_BASELINE_FILE"] = "/nonexistent.json"
        engine = TrackingEngine(base_params)
    else:
        from core.pf_engine import ParticleEngine
        engine = ParticleEngine(base_params, floorplan=floorplan)
    engine.setup_sensors(configs)
    room_fn = sim.room_of
    result = sim.run_engine(sc, engine, args.name, room_fn=room_fn)
    print(json.dumps(result.metrics()))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seeds", type=int, default=4)
    parser.add_argument("--duration", type=float, default=900.0)
    parser.add_argument("--only", nargs="*")
    parser.add_argument("--params", default="")
    parser.add_argument("--legacy-root", default=None, help="Pfad zu altem bt_tracker für 'orig'")
    parser.add_argument("--drift", action="store_true")
    parser.add_argument("--scenario", default="", help='JSON, z. B. {"dead_sensors": ["ron"]}')
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 2)
    # interne Einzelausführung
    parser.add_argument("--single", action="store_true")
    parser.add_argument("--root", default=HERE)
    parser.add_argument("--engine")
    parser.add_argument("--calibration")
    parser.add_argument("--firmware")
    parser.add_argument("--floorplan", action="store_true")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--name", default="")
    parser.add_argument("--orig", action="store_true")
    args = parser.parse_args()
    if args.single:
        run_single(args)
        return

    variants = dict(VARIANTS)
    if args.legacy_root:
        variants = {"orig": ("legacy", "legacy", "legacy", False),
                    "orig_current_fw": ("legacy", "legacy", "current", False), **variants}
    if args.only:
        variants = {k: v for k, v in variants.items() if k in args.only}

    jobs = []
    for name, (engine, calib, fw, fp) in variants.items():
        for seed in range(args.seeds):
            cmd = [sys.executable, "-m", "tools.ab_compare", "--single", "--engine", engine,
                   "--calibration", calib, "--firmware", fw, "--seed", str(seed), "--name", name,
                   "--duration", str(args.duration)]
            if fp:
                cmd.append("--floorplan")
            if args.drift:
                cmd.append("--drift")
            if args.scenario:
                cmd += ["--scenario", args.scenario]
            if name.startswith("orig"):
                cmd += ["--root", os.path.abspath(args.legacy_root), "--orig"]
            elif args.params:
                cmd += ["--params", args.params]
            jobs.append((name, seed, cmd))

    def run(job):
        name, seed, cmd = job
        out = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True)
        if out.returncode != 0:
            return name, seed, None, out.stderr[-2000:]
        return name, seed, json.loads(out.stdout.strip().splitlines()[-1]), ""

    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        results = list(pool.map(run, jobs))

    import numpy as np
    table = {}
    for name, seed, metrics, err in results:
        if metrics is None:
            print(f"[{name} seed {seed}] FEHLER:\n{err}")
            continue
        table.setdefault(name, []).append(metrics)
    header = f"{'Variante':22s}" + "".join(f"{m:>12s}" for m in METRICS)
    print(header)
    for name in variants:
        rows = table.get(name)
        if not rows:
            continue
        vals = [np.nanmean([r[m] for r in rows]) for m in METRICS]
        print(f"{name:22s}" + "".join(f"{v:12.2f}" for v in vals))


if __name__ == "__main__":
    main()
