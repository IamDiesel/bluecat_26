import datetime as dt
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import hotspots as hs  # noqa: E402

UTC = dt.timezone.utc
HOME = (47.930222, 10.289361)


def ts(text):
    return dt.datetime.fromisoformat(text).replace(tzinfo=UTC).timestamp()


def test_parse_time_accepts_kippy_formats():
    assert hs.parse_time("2026-08-11T23:33:12Z") == ts("2026-08-11T23:33:12")
    assert hs.parse_time("2026-08-11T23:33:12.111323758+00:00") == ts("2026-08-11T23:33:12.111323")
    assert hs.parse_time("kaputt") is None and hs.parse_time(None) is None


def test_dwell_caps_gaps():
    out = hs.dwell([(0, "a"), (30, "b"), (5000, "c")], max_gap=600, until=5060)
    assert [(v, s) for v, s, _ in out] == [("a", 30), ("b", 600), ("c", 60)]


def test_indoor_recorder_counts_time_per_cell(tmp_path):
    clock = {"t": ts("2026-09-29T10:00:00")}
    store = hs.DayStore(str(tmp_path), "indoor")
    rec = hs.IndoorRecorder(store, lambda: UTC, clock=lambda: clock["t"])
    for k in range(11):                                   # 10 s auf dem Sofa (−200 | −400)
        rec.on_live({"state": "aktiv", "x_cm": -200.0, "y_cm": -400.0}, now=clock["t"] + k)
    rec.on_live({"state": "aktiv", "x_cm": 100.0, "y_cm": 10.0}, now=clock["t"] + 11)
    rec.on_live({"state": "aktiv", "x_cm": 100.0, "y_cm": 10.0}, now=clock["t"] + 60)   # Lücke > 10 s: zählt nicht
    rec.on_live({"state": "inaktiv"}, now=clock["t"] + 61)
    rec.flush()
    cells = store.load("2026-09-29")["cells"]
    assert cells[hs.indoor_key(-200, -400)][0] == 11.0      # 10 × 1 s + 1 s bis zum Wechsel
    assert cells[hs.indoor_key(100, 10)][0] == 1.0          # nur die Sekunde bis zum Ende; die 49-s-Lücke nicht
    rec.flush()                                             # zweiter Flush ohne Neues ändert nichts
    assert store.load("2026-09-29")["cells"] == cells


class FakeHA:
    def __init__(self, www, points=None, history=None):
        self.www = www
        self.points = points or []
        self.history = history or []
        self.calls = []

    available = True

    def get(self, path):
        self.calls.append(("GET", path))
        if path == "/config":
            return {"time_zone": "UTC"}
        if path == "/states":
            return [{"entity_id": "device_tracker.lola", "attributes": {"petID": 4711, "petName": "Lola"}},
                    {"entity_id": "device_tracker.lola_trilola", "attributes": {"source": "trilola"}}]
        if path.startswith("/history/period/"):
            start = hs.parse_time(path.split("/")[3].split("?")[0])
            end = hs.parse_time(path.split("end_time=")[1].split("&")[0])
            return [[h for h in self.history if start <= hs.parse_time(h["last_updated"]) < end]]
        return None

    def post(self, path, body):
        self.calls.append(("POST", path, body))
        start, end = hs.parse_time(body["from_date"]), hs.parse_time(body["to_date"])
        feats = [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]},
                  "properties": {"time": t}} for t, lat, lon in self.points if start <= hs.parse_time(t) < end]
        if not feats:
            return {"service_response": {"waypoints": 0, "files": []}}
        os.makedirs(os.path.join(self.www, "www"), exist_ok=True)
        with open(os.path.join(self.www, "www", "kippy_history_4711_points.geojson"), "w") as handle:
            json.dump({"type": "FeatureCollection", "features": feats}, handle)
        return {"service_response": {"waypoints": len(feats),
                                     "files": ["/config/www/kippy_history_4711_points.geojson"]}}


def test_outdoor_fetch_and_query(tmp_path, monkeypatch):
    monkeypatch.setenv("BLUECAT_HA_CONFIG", str(tmp_path / "ha"))
    park = (47.9330, 10.2950)                               # ≈ 520 m vom Haus
    points = [("2026-09-28T08:00:00Z", *HOME), ("2026-09-28T09:00:00Z", *park),
              ("2026-09-28T09:20:00Z", *park), ("2026-09-28T09:40:00Z", HOME[0] + 0.00005, HOME[1])]
    ha = FakeHA(str(tmp_path / "ha"), points)
    now = {"t": ts("2026-09-29T12:00:00")}
    spots = hs.Hotspots(str(tmp_path / "hot"), ha=ha, clock=lambda: now["t"])
    georef = {"lat": HOME[0], "lon": HOME[1]}
    first = spots.query("week", None, None, georef)
    assert first["from"] == "2026-09-23" and first["to"] == "2026-09-29"
    for _ in range(200):                                    # Abruf läuft im Hintergrund
        if not spots.job["running"]:
            break
        import time
        time.sleep(0.01)
    assert not spots.job["error"], spots.job["error"]
    body = [c for c in ha.calls if c[0] == "POST"][0][2]
    assert body["pet_id"] == "4711" and body["formats"] == ["geojson_points"]
    res = spots.query("week", None, None, georef)
    out = res["outdoor"]
    assert out["home_s"] == 3600 + 3600                     # 08–09 Uhr zu Hause, 09:40 bis Tagesende (max. 1 h)
    assert out["total_s"] == 40 * 60                        # 40 min im Park
    assert out["spots"][0]["share"] == 1.0 and 450 < out["spots"][0]["dist_m"] < 600
    day = spots.outdoor.load("2026-09-28")
    assert day["complete"] is True
    today = spots.outdoor.load("2026-09-29")
    assert today["complete"] is False                       # läuft noch → später erneut abrufen
    assert spots.missing_outdoor(["2026-09-28"]) == []
    now["t"] += 700
    assert spots.missing_outdoor(["2026-09-28", "2026-09-29"]) == ["2026-09-29"]


def test_indoor_backfill_from_ha_history_and_rooms(tmp_path):
    history = [
        {"state": "home", "last_updated": "2026-09-28T10:00:00+00:00", "attributes": {"x_cm": -200, "y_cm": -400}},
        {"state": "home", "last_updated": "2026-09-28T10:30:00+00:00", "attributes": {"x_cm": 150, "y_cm": -50}},
        {"state": "not_home", "last_updated": "2026-09-28T10:40:00+00:00", "attributes": {}},
    ]
    ha = FakeHA(str(tmp_path), history=history)
    spots = hs.Hotspots(str(tmp_path / "hot"), ha=ha, clock=lambda: ts("2026-09-29T12:00:00"))
    rooms = [{"name": "Wohnzimmer", "polygon": [[-400, -600], [0, -600], [0, -200], [-400, -200]]}]
    res = spots.query("week", "2026-09-28", {"rooms": rooms}, None, want_outdoor=False)
    ind = res["indoor"]
    assert ind["total_s"] == 1800 + 600
    assert ind["rooms"][0] == {"name": "Wohnzimmer", "s": 1800, "share": 0.75}
    assert ind["spots"][0]["room"] == "Wohnzimmer" and ind["spots"][0]["share"] == 0.75
    assert spots.indoor.load("2026-09-28")["source"] == "ha"


def test_top_spots_merges_neighbours():
    pts = [(0, 0, 100), (25, 0, 50), (500, 0, 80)]
    spots = hs.top_spots(pts, 75.0)
    assert [round(s[2]) for s in spots] == [150, 80]
    assert abs(spots[0][0] - 25 * 50 / 150) < 1e-9


def test_view_grid_is_chosen_at_query_time(tmp_path, monkeypatch):
    monkeypatch.setenv("BLUECAT_HA_CONFIG", str(tmp_path / "ha"))
    park = (47.9330, 10.2950)
    points = [("2026-09-28T09:00:00Z", *park), ("2026-09-28T09:10:00Z", park[0] + 0.00006, park[1]),
              ("2026-09-28T09:20:00Z", *HOME)]
    ha = FakeHA(str(tmp_path / "ha"), points)
    spots = hs.Hotspots(str(tmp_path / "hot"), ha=ha, clock=lambda: ts("2026-09-29T12:00:00"))
    georef = {"lat": HOME[0], "lon": HOME[1]}
    spots.fetch_outdoor(["2026-09-28"], georef)
    saved = spots.outdoor.load("2026-09-28")
    assert saved["v"] == hs.OUTDOOR_FORMAT and len(saved["raw"]) == 3         # Rohpunkte, kein festes Raster
    fine = spots.query("day", "2026-09-28", None, georef, out_cell=5)["outdoor"]
    coarse = spots.query("day", "2026-09-28", None, georef, out_cell=50)["outdoor"]
    assert fine["cell_m"] == 5 and coarse["cell_m"] == 50
    assert len(fine["cells"]) == 2 and len(coarse["cells"]) == 1                # ~7 m auseinander
    assert fine["total_s"] == coarse["total_s"] == 1200
    assert spots.query("day", "2026-09-28", None, georef, out_cell=7)["outdoor"]["cell_m"] == hs.OUTDOOR_VIEW_DEFAULT


def test_old_outdoor_files_are_fetched_again(tmp_path):
    spots = hs.Hotspots(str(tmp_path / "hot"), ha=FakeHA(str(tmp_path)), clock=lambda: ts("2026-09-29T12:00:00"))
    spots.outdoor.save("2026-09-20", {"cells": {"1,2": [60, 1]}, "home_s": 0, "complete": True, "fetched": 0})
    assert spots.missing_outdoor(["2026-09-20"]) == ["2026-09-20"]


def test_indoor_cells_are_coarsened_and_diagnosed(tmp_path):
    clock = {"t": ts("2026-09-29T10:00:00")}
    ha = FakeHA(str(tmp_path))
    spots = hs.Hotspots(str(tmp_path / "hot"), ha=ha, record=True, clock=lambda: clock["t"])
    rec = spots.recorder
    for k, x in enumerate((10.0, 30.0, 60.0, 60.0)):                      # 25-cm-Zellen 0, 1, 2
        rec.on_live({"state": "aktiv", "x_cm": x, "y_cm": 10.0}, now=clock["t"] + k)
    clock["t"] += 5
    fine = spots.query("day", None, None, None, want_outdoor=False, in_cell=25)["indoor"]
    coarse = spots.query("day", None, None, None, want_outdoor=False, in_cell=100)["indoor"]
    assert len(fine["cells"]) == 3 and fine["cell_cm"] == 25
    assert coarse["cells"] == [[50.0, 50.0, 3]]
    diag = coarse["diag"]
    assert diag["messages"] == 4 and diag["last_state"] == "aktiv" and diag["last_pos_age_s"] == 2
    assert diag["history_entity"] == "device_tracker.lola_trilola"


def test_recorder_never_asks_home_assistant(tmp_path):
    class SlowHA(FakeHA):
        def get(self, path):
            raise AssertionError("im MQTT-Thread darf nichts an HA gehen")
    spots = hs.Hotspots(str(tmp_path / "hot"), ha=SlowHA(str(tmp_path)), record=True,
                        clock=lambda: ts("2026-09-29T10:00:00"))
    spots.recorder.on_live({"state": "aktiv", "x_cm": 1.0, "y_cm": 1.0}, now=ts("2026-09-29T10:00:00"))
    spots.recorder.on_live({"state": "aktiv", "x_cm": 1.0, "y_cm": 1.0}, now=ts("2026-09-29T10:02:00"))
    spots.recorder.flush()


def wait_job(spots):
    import time
    for _ in range(300):
        if not spots.job["running"]:
            return
        time.sleep(0.01)


def test_today_is_only_topped_up_with_new_points(tmp_path, monkeypatch):
    monkeypatch.setenv("BLUECAT_HA_CONFIG", str(tmp_path / "ha"))
    park = (47.9330, 10.2950)
    points = [("2026-09-29T08:00:00Z", *park), ("2026-09-29T08:20:00Z", *park)]
    ha = FakeHA(str(tmp_path / "ha"), points)
    now = {"t": ts("2026-09-29T09:00:00")}
    spots = hs.Hotspots(str(tmp_path / "hot"), ha=ha, clock=lambda: now["t"])
    spots.fetch_outdoor(["2026-09-29"])
    assert len(spots.outdoor.load("2026-09-29")["raw"]) == 2
    points.append(("2026-09-29T09:30:00Z", *HOME))
    now["t"] = ts("2026-09-29T10:00:00")
    assert spots.missing_outdoor(["2026-09-29"]) == ["2026-09-29"]
    spots.fetch_outdoor(["2026-09-29"])
    body = [c for c in ha.calls if c[0] == "POST"][-1][2]
    assert body["from_date"] == "2026-09-29T08:58:00.000Z"          # ab letztem Abruf (−2 min), nicht ab 0 Uhr
    day = spots.outdoor.load("2026-09-29")
    assert [r[0] for r in day["raw"]] == [ts("2026-09-29T08:00:00"), ts("2026-09-29T08:20:00"), ts("2026-09-29T09:30:00")]
    out = spots.query("day", None, None, {"lat": HOME[0], "lon": HOME[1]})["outdoor"]
    assert out["total_s"] == 20 * 60 + 3600 and out["home_s"] == 30 * 60   # 08:20 → 09:30 gedeckelt auf 1 h


def test_older_point_files_still_count(tmp_path):
    spots = hs.Hotspots(str(tmp_path / "hot"), ha=FakeHA(str(tmp_path)), clock=lambda: ts("2026-09-29T12:00:00"))
    spots.outdoor.save("2026-09-27", {"v": 2, "points": [[47.9330, 10.2950, 600.0]], "home_s": 120.0, "complete": True})
    assert spots.missing_outdoor(["2026-09-27"]) == []
    out = spots.query("week", None, None, {"lat": HOME[0], "lon": HOME[1]})["outdoor"]
    assert out["total_s"] == 600 and out["home_s"] == 120


def test_browser_only_gets_changes(tmp_path):
    clock = {"t": ts("2026-09-29T10:00:00")}
    spots = hs.Hotspots(str(tmp_path / "hot"), ha=FakeHA(str(tmp_path)), record=True, clock=lambda: clock["t"])
    first = spots.query("day", None, None, None, want_outdoor=False)
    again = spots.query("day", None, None, None, want_outdoor=False, since=first["etag"])
    assert again["unchanged"] is True and "indoor" not in again
    other = spots.query("day", None, None, None, want_outdoor=False, in_cell=25, since=first["etag"])
    assert "indoor" in other                                           # andere Einstellung → volle Antwort
    for k in range(3):
        spots.recorder.on_live({"state": "aktiv", "x_cm": 5.0, "y_cm": 5.0}, now=clock["t"] + k)
    clock["t"] += 5
    fresh = spots.query("day", None, None, None, want_outdoor=False, since=first["etag"])
    assert "indoor" in fresh and fresh["indoor"]["total_s"] == 2
