import pytest

import simulation as sim
from radio_environment import RadioEnvironmentModel


def run(options, shift_all=0.0, seed=3):
    sc = sim.build_scenario(seed, options)
    env = RadioEnvironmentModel({n: p for n, (p, _) in sc.sensors.items()})
    for e in sc.events:
        if e.kind == "mesh":
            env.observe(e.sensor, e.transmitter, e.rssi + (shift_all if e.t > 300 else 0.0), e.t)
    return env, options.duration_s - 1


def test_receiver_drift_is_detected():
    env, t = run(sim.ScenarioOptions(duration_s=700, receiver_drift=("shelly_wohnzimmer", 300, -6.0)))
    assert env.get_receiver_offset("shelly_wohnzimmer", t) == pytest.approx(-6.0, abs=2.0)
    assert abs(env.get_receiver_offset("kunibert", t)) < 1.0


def test_transmitter_drift_does_not_touch_receiver():
    env, t = run(sim.ScenarioOptions(duration_s=700, transmitter_drift=("kunibert", 300, -6.0)))
    assert abs(env.get_receiver_offset("kunibert", t)) < 1.0
    assert env.get_transmitter_offset("kunibert", t) < -3.0


def test_common_mode_shift_is_ignored():
    env, t = run(sim.ScenarioOptions(duration_s=700), shift_all=-7.0)
    assert all(abs(env.get_receiver_offset(n, t)) < 1.0 for n in env.sensor_positions)


def test_baselines_survive_disable_and_reload(tmp_path):
    sc = sim.build_scenario(3, sim.ScenarioOptions(duration_s=200))
    pos = {n: p for n, (p, _) in sc.sensors.items()}
    env = RadioEnvironmentModel(pos)
    for e in sc.events:
        if e.kind == "mesh":
            env.observe(e.sensor, e.transmitter, e.rssi, e.t)
    learned = len(env._baselines)
    without = {k: v for k, v in pos.items() if k != "ron"}
    env.update_positions(without)
    path = tmp_path / "b.json"
    env.save_baseline(str(path))
    env2 = RadioEnvironmentModel(without, baseline_file=str(path))
    env2.update_positions(pos)
    assert len(env2._baselines) == learned
    moved = dict(pos)
    moved["ron"] = (0.0, 0.0)
    env2.update_positions(moved)
    assert len(env2._baselines) < learned
