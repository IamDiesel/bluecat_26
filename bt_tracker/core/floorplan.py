"""Grundriss: Wände (Dämpfung, Bewegungssperre) und Räume.

Dateiformat ``config/floorplan.json`` (alle Koordinaten in cm, gleiches
System wie die Sensorpositionen)::

    {
      "default_wall_db": 5.0,
      "walls": [
        {"a": [x1, y1], "b": [x2, y2], "attenuation_db": 6.0, "blocking": true}
      ],
      "rooms": [
        {"name": "Wohnzimmer", "polygon": [[x, y], [x, y], ...]}
      ]
    }

Türen sind einfach Lücken zwischen Wandsegmenten. ``attenuation_db`` fehlt
→ ``default_wall_db``. ``blocking`` fehlt → ``true``. Fehlt die Datei, gibt
es weder Wände noch Räume; das Tracking funktioniert trotzdem.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

import numpy as np


@dataclass
class Room:
    name: str
    polygon: np.ndarray  # (K, 2)

    def contains(self, points: np.ndarray) -> np.ndarray:
        """Punkt-in-Polygon (Ray-Casting), vektorisiert für (N, 2)."""
        pts = np.asarray(points, dtype=float).reshape(-1, 2)
        x, y = pts[:, 0], pts[:, 1]
        inside = np.zeros(len(pts), dtype=bool)
        poly = self.polygon
        j = len(poly) - 1
        for i in range(len(poly)):
            xi, yi = poly[i]
            xj, yj = poly[j]
            crosses = (yi > y) != (yj > y)
            with np.errstate(divide="ignore", invalid="ignore"):
                x_int = (xj - xi) * (y - yi) / (yj - yi) + xi
            inside ^= crosses & (x < x_int)
            j = i
        return inside


@dataclass
class FloorPlan:
    wall_a: np.ndarray = field(default_factory=lambda: np.empty((0, 2)))
    wall_b: np.ndarray = field(default_factory=lambda: np.empty((0, 2)))
    wall_db: np.ndarray = field(default_factory=lambda: np.empty(0))
    wall_blocking: np.ndarray = field(default_factory=lambda: np.empty(0, dtype=bool))
    rooms: List[Room] = field(default_factory=list)
    source: Optional[str] = None

    # ------------------------------------------------------------------
    @classmethod
    def from_dict(cls, data: dict, source: Optional[str] = None) -> "FloorPlan":
        default_db = float(data.get("default_wall_db", 5.0))
        # Von der Kalibrierung geschätzte Skalierung aller Wanddämpfungen
        scale = float(data.get("wall_scale", 1.0) or 1.0)
        a, b, db, blocking = [], [], [], []
        for wall in data.get("walls", []) or []:
            try:
                pa = [float(v) for v in wall["a"]]
                pb = [float(v) for v in wall["b"]]
            except (KeyError, TypeError, ValueError):
                continue
            if len(pa) != 2 or len(pb) != 2 or pa == pb:
                continue
            a.append(pa)
            b.append(pb)
            db.append(float(wall.get("attenuation_db", default_db)) * scale)
            blocking.append(bool(wall.get("blocking", True)))
        rooms = []
        for room in data.get("rooms", []) or []:
            try:
                poly = np.asarray(room["polygon"], dtype=float).reshape(-1, 2)
            except (KeyError, TypeError, ValueError):
                continue
            if len(poly) >= 3:
                rooms.append(Room(str(room.get("name", f"Raum {len(rooms) + 1}")), poly))
        return cls(
            wall_a=np.asarray(a, dtype=float).reshape(-1, 2),
            wall_b=np.asarray(b, dtype=float).reshape(-1, 2),
            wall_db=np.asarray(db, dtype=float),
            wall_blocking=np.asarray(blocking, dtype=bool),
            rooms=rooms,
            source=source,
        )

    @classmethod
    def load(cls, path: Optional[str]) -> "FloorPlan":
        if not path or not os.path.exists(path):
            return cls()
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return cls.from_dict(json.load(handle), source=path)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
            print(f"Grundriss {path} konnte nicht geladen werden: {error}")
            return cls()

    # ------------------------------------------------------------------
    @property
    def has_walls(self) -> bool:
        return len(self.wall_db) > 0

    @property
    def has_rooms(self) -> bool:
        return len(self.rooms) > 0

    def bounds(self):
        pts = [self.wall_a, self.wall_b] + [r.polygon for r in self.rooms]
        pts = [p for p in pts if len(p)]
        if not pts:
            return None
        allp = np.vstack(pts)
        return allp.min(axis=0), allp.max(axis=0)

    # ------------------------------------------------------------------
    def _crossings(self, p: np.ndarray, q: np.ndarray, mask=None) -> np.ndarray:
        """Bool-Matrix (N, W): Strecke p_i→q_i schneidet Wand w."""
        a = self.wall_a if mask is None else self.wall_a[mask]
        b = self.wall_b if mask is None else self.wall_b[mask]
        if len(a) == 0:
            return np.zeros((len(p), 0), dtype=bool)
        p = p[:, None, :]
        q = q[:, None, :]
        a = a[None, :, :]
        b = b[None, :, :]

        def orient(u, v, w):
            return (v[..., 0] - u[..., 0]) * (w[..., 1] - u[..., 1]) - (v[..., 1] - u[..., 1]) * (
                w[..., 0] - u[..., 0]
            )

        d1 = orient(a, b, p)
        d2 = orient(a, b, q)
        d3 = orient(p, q, a)
        d4 = orient(p, q, b)
        return (d1 * d2 < 0) & (d3 * d4 < 0)

    def attenuation_db(self, points: np.ndarray, sensor: Sequence[float]) -> np.ndarray:
        """Summe der Wanddämpfungen zwischen jedem Punkt und dem Sensor."""
        points = np.asarray(points, dtype=float).reshape(-1, 2)
        if not self.has_walls:
            return np.zeros(len(points))
        sensor = np.broadcast_to(np.asarray(sensor, dtype=float).reshape(1, 2), points.shape)
        return self._crossings(points, sensor) @ self.wall_db

    def wall_count(self, points: np.ndarray, sensor: Sequence[float]) -> np.ndarray:
        points = np.asarray(points, dtype=float).reshape(-1, 2)
        if not self.has_walls:
            return np.zeros(len(points))
        sensor = np.broadcast_to(np.asarray(sensor, dtype=float).reshape(1, 2), points.shape)
        return self._crossings(points, sensor).sum(axis=1).astype(float)

    def blocked(self, old: np.ndarray, new: np.ndarray) -> np.ndarray:
        """True, wenn die Bewegung old→new eine blockierende Wand kreuzt."""
        if not self.has_walls or not self.wall_blocking.any():
            return np.zeros(len(old), dtype=bool)
        return self._crossings(np.asarray(old, float), np.asarray(new, float), self.wall_blocking).any(axis=1)

    def room_index(self, points: np.ndarray) -> np.ndarray:
        """Raumindex je Punkt, -1 = außerhalb aller Räume."""
        points = np.asarray(points, dtype=float).reshape(-1, 2)
        idx = np.full(len(points), -1, dtype=int)
        for i, room in enumerate(self.rooms):
            free = idx < 0
            if free.any():
                inside = room.contains(points[free])
                sub = np.where(free)[0][inside]
                idx[sub] = i
        return idx

    def room_name(self, point) -> Optional[str]:
        if not self.has_rooms:
            return None
        i = int(self.room_index(np.asarray(point, dtype=float).reshape(1, 2))[0])
        return self.rooms[i].name if i >= 0 else None
