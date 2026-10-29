#!/usr/bin/env python3
"""Зоны игнорирования подсчёта (AI_carsData type=8, Squares = 1).

Привязка зон к машинам хранится в отдельной БД RoutesAndAdvertisement:
  all_car_routes.zones_id (JSON-массив id) по imei
    → zones с type='counting_ignoring' (и active=1) → geom + radius_m.
По GPS-треку машины находим периоды, когда она была внутри таких зон, и
вычитаем их из временных сегментов обработки.

zones.geom — внутренний формат MySQL/MariaDB: 4 байта SRID + WKB,
x = долгота, y = широта. Point / LineString — круг / коридор радиусом
radius_m; Polygon — сама площадь (+ radius_m вокруг границы, если задан).
"""

from __future__ import annotations

import logging
import math
import struct
from typing import List, Optional, Sequence, Tuple

from .intervals import merge_intervals, subtract_intervals
from .segments import (GpsColumnMissing, get_gps_data, haversine_km,
                       parse_gps_row, parse_json)

ZONE_TYPE_IGNORE = "counting_ignoring"

# Граница входа/выхода — середина между соседними точками трека (внутри/снаружи),
# но не дальше этого порога от крайней точки внутри зоны (защита от пропусков GPS).
ZONE_EDGE_MAX_SEC = 60

_EARTH_RADIUS_M = 6371000.0

Coord = Tuple[float, float]  # (lon, lat)


# ───────────────────────── геометрия ─────────────────────────

class _WkbReader:
    def __init__(self, data: bytes, pos: int):
        self.data = data
        self.pos = pos
        self.endian = "<"

    def _unpack(self, fmt: str):
        full = self.endian + fmt
        vals = struct.unpack_from(full, self.data, self.pos)
        self.pos += struct.calcsize(full)
        return vals

    def uint(self) -> int:
        return self._unpack("I")[0]

    def coord(self) -> Coord:
        x, y = self._unpack("dd")
        return x, y

    def coords(self) -> List[Coord]:
        return [self.coord() for _ in range(self.uint())]

    def geometry(self) -> List[tuple]:
        """WKB → плоский список примитивов ("point"|"line"|"polygon", данные)."""
        self.endian = "<" if self.data[self.pos] == 1 else ">"
        self.pos += 1
        gtype = self.uint() % 1000
        if gtype == 1:
            return [("point", self.coord())]
        if gtype == 2:
            return [("line", self.coords())]
        if gtype == 3:
            return [("polygon", [self.coords() for _ in range(self.uint())])]
        if gtype in (4, 5, 6, 7):  # Multi* / GeometryCollection
            parts: List[tuple] = []
            for _ in range(self.uint()):
                parts.extend(self.geometry())
            return parts
        raise ValueError(f"неподдерживаемый тип WKB: {gtype}")


def parse_geometry(raw) -> List[tuple]:
    """Значение колонки geometry (SRID + WKB) → список примитивов."""
    data = bytes(raw)
    if len(data) < 9:
        raise ValueError("слишком короткая геометрия")
    return _WkbReader(data, 4).geometry()


def _distance_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    return haversine_km(lat1, lon1, lat2, lon2) * 1000.0


def _distance_to_polyline_m(lon: float, lat: float,
                            coords: Sequence[Coord]) -> float:
    """Расстояние от точки до ломаной (локальная равнопромежуточная проекция)."""
    if not coords:
        return math.inf
    if len(coords) == 1:
        return _distance_m(lon, lat, coords[0][0], coords[0][1])
    kx = math.radians(1) * _EARTH_RADIUS_M * math.cos(math.radians(lat))
    ky = math.radians(1) * _EARTH_RADIUS_M
    pts = [((x - lon) * kx, (y - lat) * ky) for x, y in coords]
    best = math.inf
    for (ax, ay), (bx, by) in zip(pts, pts[1:]):
        dx, dy = bx - ax, by - ay
        len2 = dx * dx + dy * dy
        t = 0.0 if len2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / len2))
        best = min(best, math.hypot(ax + t * dx, ay + t * dy))
    return best


def _in_ring(lon: float, lat: float, ring: Sequence[Coord]) -> bool:
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        if (y1 > lat) != (y2 > lat):
            x_cross = x1 + (lat - y1) * (x2 - x1) / (y2 - y1)
            if lon < x_cross:
                inside = not inside
    return inside


def _in_polygon(lon: float, lat: float, rings: Sequence[Sequence[Coord]]) -> bool:
    """Внутри внешнего кольца и вне всех дыр."""
    if not rings or not _in_ring(lon, lat, rings[0]):
        return False
    return not any(_in_ring(lon, lat, hole) for hole in rings[1:])


class Zone:
    def __init__(self, zone_id: int, name: str, parts: List[tuple],
                 radius_m: Optional[float]):
        self.id = zone_id
        self.name = name
        self.parts = parts
        self.radius_m = float(radius_m) if radius_m else 0.0

    @property
    def usable(self) -> bool:
        """Point/LineString без радиуса не покрывают никакой площади."""
        return any(kind == "polygon" for kind, _ in self.parts) or self.radius_m > 0

    def contains(self, lon: float, lat: float) -> bool:
        for kind, geom in self.parts:
            if kind == "polygon":
                if _in_polygon(lon, lat, geom):
                    return True
                if self.radius_m and any(
                        _distance_to_polyline_m(lon, lat, ring) <= self.radius_m
                        for ring in geom):
                    return True
            elif not self.radius_m:
                continue
            elif kind == "point":
                if _distance_m(lon, lat, geom[0], geom[1]) <= self.radius_m:
                    return True
            elif _distance_to_polyline_m(lon, lat, geom) <= self.radius_m:
                return True
        return False

    def __repr__(self):
        return f"Zone#{self.id}({self.name})"


# ───────────────────────── БД ─────────────────────────

def _parse_zone_ids(raw) -> List[int]:
    parsed = parse_json(raw)
    if not isinstance(parsed, list):
        return []
    ids: List[int] = []
    for v in parsed:
        try:
            ids.append(int(v))
        except (ValueError, TypeError):
            continue
    return ids


def fetch_ignore_zones(zones_db, imei) -> List[Zone]:
    """Активные зоны counting_ignoring, привязанные к машине в all_car_routes."""
    rows = zones_db.query(
        "SELECT `zones_id` FROM `all_car_routes` WHERE `imei` = %s", (str(imei),))
    zone_ids = sorted({zid for r in rows for zid in _parse_zone_ids(r["zones_id"])})
    if not zone_ids:
        return []

    ph = ", ".join(["%s"] * len(zone_ids))
    zone_rows = zones_db.query(
        f"SELECT `id`,`name`,`geom`,`radius_m` FROM `zones` "
        f"WHERE `id` IN ({ph}) AND `type` = %s AND COALESCE(`active`, 1) = 1",
        [*zone_ids, ZONE_TYPE_IGNORE],
    )

    zones: List[Zone] = []
    for r in zone_rows:
        try:
            parts = parse_geometry(r["geom"])
        except Exception as exc:
            logging.warning("Зона #%s: не удалось разобрать geom (%s)", r["id"], exc)
            continue
        zone = Zone(int(r["id"]), r["name"], parts, r["radius_m"])
        if not zone.usable:
            logging.warning("Зона #%s (%s): точка/линия без radius_m — пропуск",
                            zone.id, zone.name)
            continue
        zones.append(zone)
    return zones


def find_camera_tracker(db, cam: str) -> Optional[dict]:
    """Камера → {"imei","tracker_id","tracker_table"} через trackers_conf/trackers."""
    imei = None
    for r in db.query("SELECT `imei`,`cam_list` FROM `trackers_conf` WHERE `type`=0"):
        if cam in (c.strip() for c in (r["cam_list"] or "").split("|")):
            imei = str(r["imei"])
    if not imei:
        return None
    rows = db.query(
        "SELECT `id`,`tracker_table` FROM `trackers` WHERE `imei` = %s LIMIT 1",
        (imei,))
    if not rows:
        return None
    return {"imei": imei, "tracker_id": rows[0]["id"],
            "tracker_table": rows[0]["tracker_table"]}


# ───────────────────────── интервалы ─────────────────────────

def zone_intervals(gps_rows: List[dict],
                   zones: List[Zone]) -> List[Tuple[float, float]]:
    """Периоды [start, end), когда машина по треку находилась в любой из зон."""
    points = sorted(p for p in (parse_gps_row(r) for r in gps_rows) if p)
    inside = [any(z.contains(lon, lat) for z in zones) for _, lat, lon in points]

    intervals: List[Tuple[float, float]] = []
    n = len(points)
    i = 0
    while i < n:
        if not inside[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and inside[j + 1]:
            j += 1
        start = float(points[i][0])
        end = float(points[j][0])
        if i > 0:
            start -= min((start - points[i - 1][0]) / 2, ZONE_EDGE_MAX_SEC)
        if j < n - 1:
            end += min((points[j + 1][0] - end) / 2, ZONE_EDGE_MAX_SEC)
        if end > start:
            intervals.append((start, end))
        i = j + 1
    return merge_intervals(intervals)


def subtract_from_segments(segs: List[Tuple[int, int]],
                           excluded: List[Tuple[float, float]]
                           ) -> List[Tuple[int, int]]:
    result: List[Tuple[int, int]] = []
    for seg_start, seg_end in segs:
        for s, e in subtract_intervals(seg_start, seg_end, excluded):
            s_i, e_i = int(math.ceil(s)), int(math.floor(e))
            if e_i > s_i:
                result.append((s_i, e_i))
    return result


def apply_ignore_zones(db, zones_db, imei, tracker_table: str, tracker_id: int,
                       segs: List[Tuple[int, int]], log) -> List[Tuple[int, int]]:
    """Вычесть из segs время пребывания машины в зонах counting_ignoring.

    Ошибки БД пробрасываются — вызывающий решает, повторить ли задачу позже.
    Нет зон / нет GPS-колонок → сегменты возвращаются без изменений.
    """
    if not segs:
        return segs
    zones = fetch_ignore_zones(zones_db, imei)
    if not zones:
        log.info("🚫 type8: зон %s для IMEI %s нет — фильтр не применяется",
                 ZONE_TYPE_IGNORE, imei)
        return segs

    window_start = min(s for s, _ in segs)
    window_end = max(e for _, e in segs)
    margin = ZONE_EDGE_MAX_SEC * 2
    try:
        gps = get_gps_data(db, tracker_table, tracker_id,
                           window_start - margin, window_end + margin)
    except GpsColumnMissing:
        log.warning("🚫 type8: нет latitude/longtitude в %s — фильтр зон не применяется",
                    tracker_table)
        return segs

    excluded = zone_intervals(gps, zones)
    result = subtract_from_segments(segs, excluded)
    removed = (sum(e - s for s, e in segs) - sum(e - s for s, e in result))
    log.info("🚫 type8: зоны %s → исключено %d с (%d интерв.), сегментов %d → %d",
             [z.id for z in zones], removed, len(excluded), len(segs), len(result))
    return result
