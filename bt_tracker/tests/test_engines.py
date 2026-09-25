"""Kurze Simulator-Läufe als Regressionstest (je ~10–20 s)."""
import numpy as np
import pytest

import simulation as sim
from calibration_model import fit_pooled
from core.engine import TrackingEngine
from core.floorplan import FloorPlan
from core.pf_engine import ParticleEngine


def setup(seed=11, duration=400.0, floorplan=True):
    sc = sim.build_scenario(seed, sim.ScenarioOptions(duration_s=duration))
    fp_dict = sim.floorplan_dict()
    fp = FloorPlan.from_dict(fp_dict) if floorplan else None
    cal, glob = fit_pooled(sim.simulate_calibration_samples(sc), {n: p for n, (p, _) in sc.sensors.items()}, floorplan=fp)
    if floorplan:
        fp_dict["wall_scale"] = glob["wall_scale"]
        fp = FloorPlan.from_dict(fp_dict)
    return sc, cal, fp


def test_pf_accuracy_and_rooms():
    sc, cal, fp = setup()
    engine = ParticleEngine({"RANDOM_SEED": 1, "PF_PARTICLES": 1000}, floorplan=fp)
    engine.setup_sensors(sim.sensor_configs(sc, cal))
    m = sim.run_engine(sc, engine, "pf", room_fn=sim.room_of).metrics()
    assert m["coverage"] > 0.95
    assert m["median_rest"] < 150
    assert m["room_acc"] > 0.85


def test_legacy_runs():
    sc, cal, _ = setup(floorplan=False)
    engine = TrackingEngine({"RANDOM_SEED": 1, "RADIO_BASELINE_FILE": None})
    engine.setup_sensors(sim.sensor_configs(sc, cal))
    m = sim.run_engine(sc, engine, "legacy", room_fn=sim.room_of).metrics()
    assert m["coverage"] > 0.95
    assert m["median_rest"] < 250


def test_pf_recovers_from_wrong_start():
    sc, cal, fp = setup(duration=120.0)
    engine = ParticleEngine({"RANDOM_SEED": 2, "PF_PARTICLES": 1000}, floorplan=fp)
    engine.setup_sensors(sim.sensor_configs(sc, cal))
    first_tag = next(e for e in sc.events if e.kind == "tag" and e.present)
    engine._initialize(first_tag.t)
    far = np.array([150.0, 100.0]) if sc.true_position(0)[1] < -300 else np.array([-300.0, -900.0])
    engine.particles[:, :2] = far + np.random.default_rng(0).normal(0, 20, (engine.n, 2))
    r = sim.run_engine(sc, engine, "pf", room_fn=sim.room_of)
    err = np.linalg.norm(r.est - r.truth, axis=1)
    assert np.nanmedian(err[60:]) < 200
