import numpy as np

from calibration_model import fit_pooled


def test_pooled_fit_recovers_parameters():
    rng = np.random.default_rng(0)
    sensors = {"a": (0, 0), "b": (500, 0), "c": (0, 500), "d": (500, 500)}
    p0 = {"a": -60.0, "b": -65.0, "c": -58.0, "d": -62.0}
    points = [(x, y) for x in (50, 250, 450) for y in (50, 250, 450)]
    samples = {}
    for name, pos in sensors.items():
        rows = []
        for p in points:
            d = max(np.hypot(p[0] - pos[0], p[1] - pos[1]) / 100.0, 0.3)
            mean = p0[name] - 22.0 * np.log10(d) + rng.normal(0, 2.0)
            rows.append((p, list(mean + rng.normal(0, 3.0, 60))))
        samples[name] = rows
    configs, glob = fit_pooled(samples, sensors)
    assert abs(glob["n_factor"] - 2.2) < 0.3
    for name in sensors:
        assert abs(configs[name]["tx_power"] - p0[name]) < 2.5
        assert 1.0 < configs[name]["sigma_db"] < 4.0
