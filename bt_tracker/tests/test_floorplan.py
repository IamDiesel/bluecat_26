import numpy as np

from core.floorplan import FloorPlan


def plan():
    return FloorPlan.from_dict({
        "default_wall_db": 5,
        "walls": [{"a": [0, -100], "b": [0, 100]}, {"a": [0, 200], "b": [0, 300], "blocking": False, "attenuation_db": 2}],
        "rooms": [{"name": "W", "polygon": [[-100, -100], [0, -100], [0, 100], [-100, 100]]},
                  {"name": "O", "polygon": [[0, -100], [100, -100], [100, 100], [0, 100]]}],
        "wall_scale": 2.0,
    })


def test_attenuation_and_scale():
    fp = plan()
    att = fp.attenuation_db(np.array([[-50.0, 0.0], [50.0, 0.0], [-50.0, 250.0]]), [80.0, 0.0])
    assert list(att) == [10.0, 0.0, 0.0]


def test_blocking_and_rooms():
    fp = plan()
    assert fp.blocked(np.array([[-10.0, 0.0]]), np.array([[10.0, 0.0]]))[0]
    assert not fp.blocked(np.array([[-10.0, 250.0]]), np.array([[10.0, 250.0]]))[0]
    assert fp.room_name((-50, 0)) == "W"
    assert fp.room_name((50, 0)) == "O"
    assert fp.room_name((500, 0)) is None
