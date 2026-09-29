"""Hotspots: wo sich Lola aufhält – innen (TriLola-Positionen) und außen (Kippy-GPS).

Innen zeichnet die Home-Assistant-App die Aufenthaltsdauer je 25-cm-Zelle auf (pro Tag eine kleine
JSON-Datei). Die ersten Tage vor Beginn der Aufzeichnung holt sie aus dem HA-Verlauf nach, soweit
Home Assistant ihn noch hat. Außen ruft sie den Dienst ``kippy.export_history`` (Format
``geojson_points``, mit Zeitstempeln) ab und speichert die Rohpunkte je Tag. Abgeschlossene Tage werden
nie erneut abgerufen, der laufende Tag nur ab dem letzten Abruf (höchstens alle 10 Minuten).

Das Raster der Anzeige (drinnen 25 cm – 1 m, draußen 5 – 50 m) wird erst bei der Abfrage gebildet.

Gewichtet wird immer nach Zeit: Ein Messpunkt zählt so lange, bis der nächste kommt (begrenzt, damit
eine Funkpause nicht als stundenlanger Aufenthalt zählt).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Callable, Dict, Iterable, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

INDOOR_CELL_CM = 25.0          # Speicherraster drinnen (Anzeige: Vielfache davon)
INDOOR_VIEW_CM = (25, 50, 100)
INDOOR_VIEW_DEFAULT = 50
OUTDOOR_VIEW_M = (5, 10, 25, 50)
OUTDOOR_VIEW_DEFAULT = 10
OUTDOOR_FORMAT = 3             # 3 = Rohpunkte mit Uhrzeit (nur Neues nachladen); 2 = Punkte mit Dauer (gilt
OUTDOOR_LEGACY = 2             #     weiter, wenn abgeschlossen); 1 = altes 15-m-Raster (wird neu abgerufen)
MAX_OUTDOOR_CELLS = 30000
MAX_GAP_LIVE_S = 10.0        # Live-Meldungen kommen jede Sekunde; längere Pause = Lücke
MAX_GAP_HISTORY_S = 3600.0   # HA-Verlauf: in Ruhe meldet der Tracker selten
MAX_GAP_KIPPY_S = 3600.0     # Kippy meldet alle 5–60 min
RETENTION_DAYS = 400
HOME_RADIUS_M = 30.0         # GPS-Punkte so nah am Haus zählen als „zu Hause“ (innen zeigt TriLola)
TODAY_REFRESH_S = 600.0
PERIOD_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}
KIPPY_CHUNK_DAYS = 7


# ---------------------------------------------------------------------------
# Speicher: eine Datei je Tag und Art
# ---------------------------------------------------------------------------
class DayStore:
    """``<base>/<kind>/YYYY-MM-DD.json`` = {"cells": {"i,j": [sekunden, punkte]}, …}."""

    def __init__(self, base: str, kind: str):
        self.dir = os.path.join(base, kind)

    def path(self, day: str) -> str:
        return os.path.join(self.dir, f"{day}.json")

    def load(self, day: str) -> Optional[dict]:
        try:
            with open(self.path(day), "r", encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    def save(self, day: str, data: dict):
        os.makedirs(self.dir, exist_ok=True)
        tmp = self.path(day) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, separators=(",", ":"))
        os.replace(tmp, self.path(day))

    def days(self) -> List[str]:
        try:
            return sorted(n[:-5] for n in os.listdir(self.dir) if n.endswith(".json") and len(n) == 15)
        except OSError:
            return []

    def prune(self, keep_from: str):
        for day in self.days():
            if day < keep_from:
                try:
                    os.remove(self.path(day))
                except OSError:
                    pass


def add_cell(cells: Dict[str, list], key: str, seconds: float, points: int = 1):
    entry = cells.setdefault(key, [0.0, 0])
    entry[0] = round(entry[0] + seconds, 1)
    entry[1] += points


def indoor_key(x_cm: float, y_cm: float) -> str:
    return f"{math.floor(x_cm / INDOOR_CELL_CM)},{math.floor(y_cm / INDOOR_CELL_CM)}"


class GeoGrid:
    """Gleichmäßiges Raster in Metern um einen festen Breitengrad (für Hotspots genau genug)."""

    def __init__(self, lat0: float, cell_m: float = OUTDOOR_VIEW_DEFAULT):
        self.cell = cell_m
        self.m_lat = 111132.92
        self.m_lon = 111412.84 * math.cos(math.radians(lat0))

    def key(self, lat: float, lon: float) -> str:
        return f"{math.floor(lat * self.m_lat / self.cell)},{math.floor(lon * self.m_lon / self.cell)}"

    def center(self, key: str) -> Tuple[float, float]:
        i, j = (int(v) for v in key.split(","))
        return (i + 0.5) * self.cell / self.m_lat, (j + 0.5) * self.cell / self.m_lon


def distance_m(lat1, lon1, lat2, lon2) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


# ---------------------------------------------------------------------------
# Zeit
# ---------------------------------------------------------------------------
def tzinfo(name: Optional[str]):
    if name and ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:  # noqa: BLE001 – unbekannte Zone
            pass
    return dt.datetime.now().astimezone().tzinfo


def day_of(ts: float, tz) -> str:
    return dt.datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d")


def day_bounds(day: str, tz) -> Tuple[float, float]:
    start = dt.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=tz)
    end = start + dt.timedelta(days=1)
    return start.timestamp(), end.timestamp()


def day_range(end_day: str, days: int) -> List[str]:
    end = dt.date.fromisoformat(end_day)
    return [(end - dt.timedelta(days=k)).isoformat() for k in range(days - 1, -1, -1)]


def iso_utc(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def parse_time(text) -> Optional[float]:
    if not text:
        return None
    text = str(text).strip().replace("Z", "+00:00")
    if "." in text:  # mehr als 6 Nachkommastellen mag fromisoformat nicht
        head, _, tail = text.partition(".")
        frac = "".join(ch for ch in tail if ch.isdigit())
        zone = tail[len(frac):]
        text = f"{head}.{frac[:6]}{zone}"
    try:
        stamp = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    return stamp.timestamp()


def dwell(samples: List[Tuple[float, object]], max_gap: float, until: Optional[float] = None):
    """[(zeit, wert)] → [(wert, sekunden, zeit)]: jeder Punkt gilt bis zum nächsten (höchstens max_gap)."""
    out = []
    samples = sorted(samples, key=lambda s: s[0])
    for k, (t, value) in enumerate(samples):
        nxt = samples[k + 1][0] if k + 1 < len(samples) else (until if until is not None else t)
        out.append((value, max(0.0, min(nxt - t, max_gap)), t))
    return out


# ---------------------------------------------------------------------------
# Home Assistant (nur in der App: Supervisor-Token)
# ---------------------------------------------------------------------------
class HAClient:
    def __init__(self, base: Optional[str] = None, token: Optional[str] = None, timeout: float = 120.0):
        self.token = token if token is not None else os.environ.get("SUPERVISOR_TOKEN", "")
        self.base = (base or os.environ.get("BLUECAT_HA_API") or "http://supervisor/core/api").rstrip("/")
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return bool(self.token)

    def _request(self, method: str, path: str, body=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            raw = resp.read()
        return json.loads(raw.decode("utf-8")) if raw else None

    def get(self, path: str):
        return self._request("GET", path)

    def post(self, path: str, body):
        return self._request("POST", path, body)


# ---------------------------------------------------------------------------
# Innen: Aufzeichnung aus den Live-Meldungen des Trackers
# ---------------------------------------------------------------------------
class IndoorRecorder:
    def __init__(self, store: DayStore, tz_getter: Callable[[], object], clock=time.time):
        self.store = store
        self.tz = tz_getter
        self.clock = clock
        self.lock = threading.Lock()
        self.last: Optional[Tuple[float, float, float]] = None  # x, y, wall_time
        self.pending: Dict[str, Dict[str, list]] = {}
        self.last_flush = clock()
        self.last_msg_at: Optional[float] = None   # Diagnose: kommt überhaupt etwas an?
        self.last_msg_state = ""
        self.last_pos_at: Optional[float] = None
        self.messages = 0

    def on_live(self, live: dict, now: Optional[float] = None):
        now = self.clock() if now is None else now
        with self.lock:
            self.messages += 1
            self.last_msg_at = now
            self.last_msg_state = str((live or {}).get("state") or "") if isinstance(live, dict) else ""
            prev = self.last
            if prev is not None:
                gap = now - prev[2]
                if 0 < gap <= MAX_GAP_LIVE_S:
                    add_cell(self.pending.setdefault(day_of(prev[2], self.tz()), {}), indoor_key(prev[0], prev[1]),
                             gap)
            if isinstance(live, dict) and live.get("state") == "aktiv" and live.get("x_cm") is not None:
                try:
                    self.last = (float(live["x_cm"]), float(live["y_cm"]), now)
                    self.last_pos_at = now
                except (TypeError, ValueError):
                    self.last = None
            else:
                self.last = None
        if now - self.last_flush >= 60.0:
            self.flush()

    def flush(self):
        with self.lock:
            pending, self.pending = self.pending, {}
            self.last_flush = self.clock()
        for day, cells in pending.items():
            data = self.store.load(day) or {"cells": {}, "source": "live"}
            for key, (sec, count) in cells.items():
                add_cell(data.setdefault("cells", {}), key, sec, count)
            data["source"] = "live" if data.get("source") in (None, "live") else "ha+live"
            self.store.save(day, data)


# ---------------------------------------------------------------------------
# Auswertung: Zellen summieren, Lieblingsplätze finden
# ---------------------------------------------------------------------------
def merge_days(store: DayStore, days: Iterable[str]) -> Tuple[Dict[str, list], dict]:
    cells: Dict[str, list] = {}
    extra = {"home_s": 0.0, "days_with_data": 0}
    for day in days:
        data = store.load(day)
        if not data:
            continue
        if data.get("cells"):
            extra["days_with_data"] += 1
        for key, (sec, count) in (data.get("cells") or {}).items():
            add_cell(cells, key, sec, count)
        extra["home_s"] += float(data.get("home_s") or 0.0)
    return cells, extra


def top_spots(points: List[Tuple[float, float, float]], radius: float, limit: int = 6,
              distance=lambda a, b: math.hypot(a[0] - b[0], a[1] - b[1])):
    """Gierig: stärkste Zelle, alles im Umkreis dazu, entfernen, wiederholen. points = [(x, y, sekunden)]."""
    rest = sorted(points, key=lambda p: -p[2])
    spots = []
    while rest and len(spots) < limit:
        seed = rest[0]
        group = [p for p in rest if distance(p, seed) <= radius]
        rest = [p for p in rest if distance(p, seed) > radius]
        total = sum(p[2] for p in group)
        if total <= 0:
            break
        spots.append((sum(p[0] * p[2] for p in group) / total, sum(p[1] * p[2] for p in group) / total, total))
    return spots


def point_in_poly(x, y, poly) -> bool:
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-9) + xi:
            inside = not inside
        j = i
    return inside


def room_of(x, y, rooms) -> str:
    for room in rooms or []:
        poly = room.get("polygon") or []
        if len(poly) >= 3 and point_in_poly(x, y, poly):
            return str(room.get("name") or "Raum")
    return ""


# ---------------------------------------------------------------------------
# Hauptklasse
# ---------------------------------------------------------------------------
class Hotspots:
    def __init__(self, base_dir: str, ha: Optional[HAClient] = None, record: bool = False, clock=time.time,
                 settings: Optional[Callable[[], dict]] = None):
        self.base = base_dir
        self.ha = ha or HAClient()
        self.clock = clock
        self.settings = settings or (lambda: {})
        self.indoor = DayStore(base_dir, "indoor")
        self.outdoor = DayStore(base_dir, "outdoor")
        self.record = record
        self._tz_name = None
        self._tz_checked = 0.0
        # Die Aufzeichnung läuft im MQTT-Thread: dort nie auf Home Assistant warten (nur gemerkte Zeitzone)
        self.recorder = IndoorRecorder(self.indoor, lambda: self.tz(refresh=False), clock) if record else None
        self.lock = threading.Lock()
        self.job = {"running": False, "done": 0, "total": 0, "error": "", "pet_id": "", "pet_name": ""}
        self._history_tried: set = set()
        self._entity_cache: Dict[str, Tuple[float, object]] = {}
        self.history_info = {"entity": "", "days": 0, "error": ""}

    # ---- Zeitzone von Home Assistant --------------------------------------
    def tz(self, refresh: bool = True):
        if refresh and self.ha.available and self.clock() - self._tz_checked > 3600:
            self._tz_checked = self.clock()
            try:
                self._tz_name = (self.ha.get("/config") or {}).get("time_zone") or self._tz_name
            except (OSError, ValueError, urllib.error.URLError):
                pass
        return tzinfo(self._tz_name or os.environ.get("TZ"))

    def today(self) -> str:
        return day_of(self.clock(), self.tz())

    # ---- Entitäten suchen (Kippy-Tier, TriLola-Tracker) ---------------------
    def _states(self) -> list:
        cached = self._entity_cache.get("states")
        if cached and self.clock() - cached[0] < 300:
            return cached[1]  # type: ignore[return-value]
        states = self.ha.get("/states") or []
        self._entity_cache["states"] = (self.clock(), states)
        return states

    def kippy_pet(self) -> Tuple[str, str]:
        pet = str(self.settings().get("kippy_pet_id") or "").strip()
        for state in self._states():
            attrs = state.get("attributes") or {}
            if state.get("entity_id", "").startswith("device_tracker.") and attrs.get("petID") is not None:
                if not pet or str(attrs.get("petID")) == pet:
                    return str(attrs.get("petID")), str(attrs.get("petName") or attrs.get("friendly_name") or "")
        return pet, ""

    def trilola_entity(self) -> str:
        for state in self._states():
            attrs = state.get("attributes") or {}
            if state.get("entity_id", "").startswith("device_tracker.") and attrs.get("source") == "trilola":
                return state["entity_id"]
        return ""

    # ---- Außen: Kippy -------------------------------------------------------
    def _home(self, georef: Optional[dict]) -> Optional[Tuple[float, float]]:
        g = georef or {}
        for lat_key, lon_key in (("reference_lat", "reference_lon"), ("lat", "lon"), ("origin_lat", "origin_lon")):
            if g.get(lat_key) is not None and g.get(lon_key) is not None:
                return float(g[lat_key]), float(g[lon_key])
        return None

    def missing_outdoor(self, days: List[str]) -> List[str]:
        """Tage ohne vollständigen Abruf; der laufende Tag höchstens alle 10 Minuten (dann nur Neues)."""
        out = []
        for day in days:
            data = self.outdoor.load(day)
            version = (data or {}).get("v")
            if data is None or version not in (OUTDOOR_FORMAT, OUTDOOR_LEGACY) or (
                    not data.get("complete") and self.clock() - float(data.get("fetched", 0)) > TODAY_REFRESH_S):
                out.append(day)
        return out

    def fetch_outdoor(self, days: List[str], georef: Optional[dict] = None):
        """Holt fehlende Tage über kippy.export_history (im Hintergrund aufgerufen).

        Ein angefangener Tag (Format 3, nicht abgeschlossen) wird nur ab dem letzten Abruf ergänzt."""
        tz = self.tz()
        pet_id, pet_name = self.kippy_pet()
        if not pet_id:
            raise RuntimeError("Kein Kippy-Tier gefunden – in fleet.toml unter [hotspots] kippy_pet_id eintragen")
        self.job.update({"pet_id": pet_id, "pet_name": pet_name, "total": len(days), "done": 0})
        partial, full = [], []
        for day in sorted(days):
            data = self.outdoor.load(day)
            if data and data.get("v") == OUTDOOR_FORMAT and not data.get("complete") and data.get("until"):
                partial.append((day, data))
            else:
                full.append(day)
        for day, data in partial:  # nur das Neue seit dem letzten Abruf (mit 2 min Überlappung)
            _, day_end = day_bounds(day, tz)
            end = min(day_end, self.clock())
            start = max(day_bounds(day, tz)[0], float(data["until"]) - 120.0)
            fresh = self._kippy_points(pet_id, start, end) if end > start else []
            self._save_raw(day, (data.get("raw") or []) + [[t, lat, lon] for t, (lat, lon) in fresh], end, tz)
            self.job["done"] += 1
        chunks, current = [], []
        for day in full:  # zusammenhängende Tage in Blöcken abfragen
            if current and (len(current) >= KIPPY_CHUNK_DAYS or
                            dt.date.fromisoformat(day) - dt.date.fromisoformat(current[-1]) != dt.timedelta(days=1)):
                chunks.append(current)
                current = []
            current.append(day)
        if current:
            chunks.append(current)
        for chunk in chunks:
            start, _ = day_bounds(chunk[0], tz)
            _, end = day_bounds(chunk[-1], tz)
            end = min(end, self.clock())
            per_day: Dict[str, list] = {d: [] for d in chunk}
            for t, (lat, lon) in self._kippy_points(pet_id, start, end):
                per_day.setdefault(day_of(t, tz), []).append([t, lat, lon])
            for day in chunk:
                self._save_raw(day, per_day.get(day, []), min(day_bounds(day, tz)[1], end), tz)
            self.job["done"] += len(chunk)

    def _save_raw(self, day: str, raw: list, until: float, tz):
        seen, clean = set(), []
        for t, lat, lon in sorted(raw, key=lambda r: r[0]):
            t = int(round(float(t)))
            if t not in seen:
                seen.add(t)
                clean.append([t, round(float(lat), 6), round(float(lon), 6)])
        day_end = day_bounds(day, tz)[1]
        self.outdoor.save(day, {"v": OUTDOOR_FORMAT, "raw": clean, "fetched": self.clock(), "until": until,
                                "complete": self.clock() >= day_end + 3600})

    def outdoor_dwell(self, days: List[str], home: Optional[Tuple[float, float]], radius: float,
                      range_end: float) -> Tuple[List[Tuple[float, float, float]], float]:
        """[(lat, lon, sekunden)] außerhalb des Hauses und die Zeit „laut GPS zu Hause“ über alle Tage."""
        out: List[Tuple[float, float, float]] = []
        home_s = 0.0
        samples = []
        for day in days:
            data = self.outdoor.load(day)
            if not data:
                continue
            if data.get("v") == OUTDOOR_LEGACY:           # älteres Format: Dauer schon berechnet
                home_s += float(data.get("home_s") or 0.0)
                out.extend((float(a), float(b), float(c)) for a, b, c in data.get("points") or [])
            elif data.get("v") == OUTDOOR_FORMAT:
                samples.extend((float(t), (float(a), float(b))) for t, a, b in data.get("raw") or [])
        for (lat, lon), seconds, _ in dwell(samples, MAX_GAP_KIPPY_S, until=range_end):
            if seconds <= 0:
                continue
            if home and distance_m(lat, lon, home[0], home[1]) <= radius:
                home_s += seconds
            else:
                out.append((lat, lon, seconds))
        return out, home_s

    def _kippy_points(self, pet_id: str, start: float, end: float) -> List[Tuple[float, Tuple[float, float]]]:
        body = {"pet_id": pet_id, "from_date": iso_utc(start), "to_date": iso_utc(end), "formats": ["geojson_points"]}
        try:
            result = self.ha.post("/services/kippy/export_history?return_response", body) or {}
        except urllib.error.HTTPError as error:
            if error.code in (400, 404):
                raise RuntimeError("kippy.export_history nicht verfügbar oder zu alt – bitte die Kippy-Integration "
                                   "aktualisieren (braucht formats: geojson_points)") from None
            raise RuntimeError(f"kippy.export_history: HTTP {error.code}") from None
        except urllib.error.URLError as error:
            raise RuntimeError(f"Home Assistant nicht erreichbar: {error.reason}") from None
        response = result.get("service_response", result) if isinstance(result, dict) else {}
        if not response or not response.get("waypoints"):
            return []
        files = [f for f in response.get("files") or [] if str(f).endswith("_points.geojson")]
        if not files:
            raise RuntimeError("Kippy liefert keine Punkte-Datei – bitte die Kippy-Integration aktualisieren")
        path = self._local_path(files[0])
        with open(path, "r", encoding="utf-8") as handle:
            geo = json.load(handle)
        out = []
        for feature in geo.get("features") or []:
            coords = (feature.get("geometry") or {}).get("coordinates") or []
            props = feature.get("properties") or {}
            t = parse_time(props.get("time") or props.get("date"))
            if len(coords) >= 2 and t is not None and start <= t < end:
                out.append((t, (float(coords[1]), float(coords[0]))))
        return out

    @staticmethod
    def _local_path(ha_path: str) -> str:
        # HA schreibt nach /config/www/…; in der App ist das HA-Konfigurationsverzeichnis unter /homeassistant
        name = os.path.basename(ha_path)
        for base in (os.environ.get("BLUECAT_HA_CONFIG") or "/homeassistant", "/config"):
            candidate = os.path.join(base, "www", name)
            if os.path.exists(candidate):
                return candidate
        return os.path.join(os.environ.get("BLUECAT_HA_CONFIG") or "/homeassistant", "www", name)

    # ---- Innen: fehlende Tage aus dem HA-Verlauf -----------------------------
    def backfill_indoor(self, days: List[str]):
        entity = self.trilola_entity()
        self.history_info["entity"] = entity
        if not entity:
            return
        tz = self.tz()
        first_live = next((d for d in self.indoor.days()
                           if "live" in str((self.indoor.load(d) or {}).get("source", ""))), None)
        oldest = (dt.date.fromisoformat(day_of(self.clock(), tz)) - dt.timedelta(days=14)).isoformat()
        for day in days:
            if day < oldest:
                continue  # so weit reicht der HA-Verlauf normalerweise nicht zurück
            if day in self._history_tried or self.indoor.load(day) is not None:
                continue
            if first_live and day >= first_live:
                continue  # ab Beginn der eigenen Aufzeichnung nicht nachladen
            self._history_tried.add(day)
            start, end = day_bounds(day, tz)
            end = min(end, self.clock())
            path = (f"/history/period/{iso_utc(start)}?end_time={iso_utc(end)}&filter_entity_id={entity}"
                    "&significant_changes_only=0")
            try:
                rows = self.ha.get(path) or []
            except (OSError, ValueError, urllib.error.URLError) as error:
                self.history_info["error"] = str(getattr(error, "reason", "") or error)
                continue
            samples = []
            for entry in (rows[0] if rows and isinstance(rows[0], list) else []):
                attrs = entry.get("attributes") or {}
                t = parse_time(entry.get("last_updated") or entry.get("last_changed"))
                if t is None:
                    continue
                if attrs.get("x_cm") is not None and entry.get("state") not in ("not_home", "unavailable", "unknown"):
                    samples.append((t, (float(attrs["x_cm"]), float(attrs["y_cm"]))))
                else:
                    samples.append((t, None))
            if not samples:
                continue
            data = {"cells": {}, "source": "ha"}
            for value, seconds, _ in dwell(samples, MAX_GAP_HISTORY_S, until=end):
                if value is not None and seconds > 0:
                    add_cell(data["cells"], indoor_key(*value), seconds)
            self.indoor.save(day, data)
            self.history_info["days"] += bool(data["cells"])

    # ---- Anfrage der Oberfläche --------------------------------------------
    def query(self, period: str, end_day: Optional[str], floorplan: Optional[dict], georef: Optional[dict],
              want_outdoor: bool = True, in_cell: Optional[float] = None, out_cell: Optional[float] = None,
              since: Optional[str] = None) -> dict:
        days_n = PERIOD_DAYS.get(period, 7)
        in_cell = float(in_cell) if in_cell in INDOOR_VIEW_CM else float(INDOOR_VIEW_DEFAULT)
        out_cell = float(out_cell) if out_cell in OUTDOOR_VIEW_M else float(OUTDOOR_VIEW_DEFAULT)
        today = self.today()
        end_day = end_day if end_day and end_day <= today else today
        try:
            dt.date.fromisoformat(end_day)
        except ValueError:
            end_day = today
        days = day_range(end_day, days_n)
        if self.recorder is not None:
            self.recorder.flush()
        if self.ha.available:
            try:
                self.backfill_indoor(days)
            except Exception as error:  # noqa: BLE001 – Nachladen ist Bonus
                self.history_info["error"] = str(error)
        if want_outdoor and self.ha.available:
            missing = self.missing_outdoor(days)
            if missing:
                self._start_fetch(missing, georef)
        # Hat sich seit der letzten Antwort an den Browser etwas geändert? Sonst nur „unverändert“ melden.
        etag = self.signature(days, [period, end_day, in_cell, out_cell, want_outdoor, self._home(georef),
                                     self.settings().get("home_radius_m"), (floorplan or {}).get("rooms"), today])
        if since and since == etag:
            with self.lock:
                return {"unchanged": True, "etag": etag, "job": dict(self.job)}
        cells, _ = merge_days(self.indoor, days)
        rooms = (floorplan or {}).get("rooms") or []
        # 25-cm-Zellen auf das Anzeigeraster zusammenfassen
        view: Dict[Tuple[int, int], float] = {}
        for key, (sec, _count) in cells.items():
            i, j = (int(v) for v in key.split(","))
            x, y = (i + 0.5) * INDOOR_CELL_CM, (j + 0.5) * INDOOR_CELL_CM
            k = (math.floor(x / in_cell), math.floor(y / in_cell))
            view[k] = view.get(k, 0.0) + sec
        indoor_points = [((i + 0.5) * in_cell, (j + 0.5) * in_cell, sec) for (i, j), sec in view.items()]
        room_s: Dict[str, float] = {}
        for x, y, sec in indoor_points:
            name = room_of(x, y, rooms) or "ohne Raum"
            room_s[name] = room_s.get(name, 0.0) + sec
        indoor_total = sum(p[2] for p in indoor_points)
        spots_in = [{"x": round(x), "y": round(y), "s": round(s), "share": round(s / indoor_total, 3),
                     "room": room_of(x, y, rooms)} for x, y, s in top_spots(indoor_points, max(75.0, 1.5 * in_cell))] \
            if indoor_total else []
        now = self.clock()
        rec = self.recorder
        indoor = {
            "cell_cm": in_cell, "cells": [[round(x, 1), round(y, 1), round(s)] for x, y, s in indoor_points],
            "total_s": round(indoor_total), "spots": spots_in,
            "rooms": sorted(({"name": n, "s": round(s), "share": round(s / indoor_total, 3)} for n, s in room_s.items()),
                            key=lambda r: -r["s"]) if indoor_total else [],
            "recording": rec is not None, "first_day": next(iter(self.indoor.days()), None),
            "diag": {
                "messages": rec.messages if rec else 0,
                "last_msg_age_s": round(now - rec.last_msg_at) if rec and rec.last_msg_at else None,
                "last_state": rec.last_msg_state if rec else "",
                "last_pos_age_s": round(now - rec.last_pos_at) if rec and rec.last_pos_at else None,
                "history_entity": self.history_info["entity"], "history_days": self.history_info["days"],
                "history_error": self.history_info["error"],
            },
        }
        outdoor = {"available": self.ha.available, "cells": [], "total_s": 0, "home_s": 0, "spots": [], "cell_m": out_cell}
        if want_outdoor and self.ha.available:
            home = self._home(georef)
            grid = GeoGrid(home[0] if home else 48.0, out_cell)
            radius = float(self.settings().get("home_radius_m") or HOME_RADIUS_M)
            range_end = min(self.clock(), day_bounds(days[-1], self.tz())[1])
            ocells: Dict[str, float] = {}
            opts, home_s = self.outdoor_dwell(days, home, radius, range_end)
            for lat, lon, sec in opts:
                key = grid.key(lat, lon)
                ocells[key] = ocells.get(key, 0.0) + sec
            opoints = sorted(((*grid.center(k), sec) for k, sec in ocells.items()), key=lambda p: -p[2])
            total = sum(p[2] for p in opoints)
            geo_dist = lambda a, b: distance_m(a[0], a[1], b[0], b[1])  # noqa: E731
            spots = top_spots(opoints, max(40.0, 2 * out_cell), distance=geo_dist) if total else []
            outdoor.update({
                "cells": [[round(a, 6), round(b, 6), round(s)] for a, b, s in opoints[:MAX_OUTDOOR_CELLS]],
                "total_s": round(total), "home_s": round(home_s),
                "spots": [{"lat": round(a, 6), "lon": round(b, 6), "s": round(s), "share": round(s / total, 3),
                           "dist_m": round(distance_m(a, b, home[0], home[1])) if home else None} for a, b, s in spots],
                "home_radius_m": float(self.settings().get("home_radius_m") or HOME_RADIUS_M),
            })
        with self.lock:
            job = dict(self.job)
        return {"period": period, "from": days[0], "to": days[-1], "today": today, "indoor": indoor,
                "outdoor": outdoor, "job": job, "etag": etag}

    def signature(self, days: List[str], params: list) -> str:
        """Fingerabdruck über alle beteiligten Tagesdateien, die Anfrage und den Abrufstand."""
        parts = [json.dumps(params, sort_keys=True, default=str)]
        for store in (self.indoor, self.outdoor):
            for day in days:
                try:
                    st = os.stat(store.path(day))
                    parts.append(f"{store.dir[-7:]}{day}:{st.st_mtime_ns}:{st.st_size}")
                except OSError:
                    pass
        with self.lock:
            job = self.job
            parts.append(f"{job['running']}:{job['done']}:{job['total']}:{job['error']}")
        rec = self.recorder
        if rec is not None:  # Diagnose-Hinweis ändert sich, sobald die erste Meldung/Position kommt
            parts.append(f"{bool(rec.messages)}:{rec.last_pos_at is not None}")
        parts.append(json.dumps(self.history_info, sort_keys=True))
        return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]

    def _start_fetch(self, days: List[str], georef: Optional[dict]):
        with self.lock:
            if self.job["running"]:
                return
            self.job.update({"running": True, "error": "", "done": 0, "total": len(days)})

        def run():
            try:
                self.fetch_outdoor(days, georef)
            except Exception as error:  # noqa: BLE001 – im Status anzeigen
                self.job["error"] = str(error)
            finally:
                with self.lock:
                    self.job["running"] = False
        threading.Thread(target=run, daemon=True).start()

    def prune(self):
        keep = (dt.date.fromisoformat(self.today()) - dt.timedelta(days=RETENTION_DAYS)).isoformat()
        self.indoor.prune(keep)
        self.outdoor.prune(keep)
