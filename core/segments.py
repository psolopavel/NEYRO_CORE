#!/usr/bin/env python3
"""Анализ акселерометра, GPS-пробега и построение сегментов обработки.

Из main.py: analyze_movement, find_stops, build_processing_segments,
get_accel_data, parse_region. Используется AutoSource.
"""

from __future__ import annotations

import json
import logging
import math
from typing import List, Optional, Tuple


class AccelColumnMissing(Exception):
    """Колонка InstantMovement отсутствует в таблице трекера."""


class GpsColumnMissing(Exception):
    """Колонки latitude/longtitude отсутствуют в таблице трекера."""


def get_accel_data(db, tracker_table: str, tracker_id: int,
                   start_epoch: int, end_epoch: int) -> List[dict]:
    """Данные акселерометра за период из таблицы трекера."""
    if not all(c.isalnum() or c == "_" for c in tracker_table):
        logging.error("Некорректное имя таблицы: %s", tracker_table)
        return []
    try:
        return db.query(
            f"SELECT `InstantMovement`, `epochtime` FROM `{tracker_table}` "
            f"WHERE `imei` = %s AND `epochtime` BETWEEN %s AND %s "
            f"ORDER BY `epochtime` ASC",
            (tracker_id, start_epoch, end_epoch),
        )
    except Exception as exc:
        err = str(exc).lower()
        if "instantmovement" in err and ("unknown column" in err or "1054" in err):
            raise AccelColumnMissing from exc
        raise


def get_gps_data(db, tracker_table: str, tracker_id: int,
                 start_epoch: int, end_epoch: int) -> List[dict]:
    """GPS-координаты за период из таблицы трекера (формат DDMM.MMMM)."""
    if not all(c.isalnum() or c == "_" for c in tracker_table):
        logging.error("Некорректное имя таблицы: %s", tracker_table)
        return []
    try:
        return db.query(
            f"SELECT `latitude`, `longtitude`, `epochtime` FROM `{tracker_table}` "
            f"WHERE `imei` = %s AND `epochtime` BETWEEN %s AND %s "
            f"ORDER BY `epochtime` ASC",
            (tracker_id, start_epoch, end_epoch),
        )
    except Exception as exc:
        err = str(exc).lower()
        if ("latitude" in err or "longtitude" in err) and (
                "unknown column" in err or "1054" in err):
            raise GpsColumnMissing from exc
        raise


def nmea_to_decimal_degrees(raw) -> Optional[float]:
    """DDMM.MMMM (4918.3225) → десятичные градусы (49.305375)."""
    try:
        if raw is None or str(raw).strip().strip('"') == "":
            return None
        value = float(raw)
        if value == 0.0:
            return None
        degrees = int(value // 100)
        minutes = value - degrees * 100
        if minutes < 0 or minutes >= 60:
            return None
        return degrees + minutes / 60.0
    except (ValueError, TypeError):
        return None


def parse_gps_row(row: dict) -> Optional[Tuple[int, float, float]]:
    """Строка трекера → (epochtime, lat, lon) или None для невалидных координат."""
    lat = nmea_to_decimal_degrees(row.get("latitude"))
    lon = nmea_to_decimal_degrees(row.get("longtitude"))
    if lat is None or lon is None:
        return None
    return int(row["epochtime"]), lat, lon


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Расстояние между двумя точками на сфере, км."""
    r = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


def calc_daily_km(gps_data: List[dict],
                  max_segment_km: float = 10.0) -> float:
    """Суммарный пробег за день по GPS (км).

    Пропускает нулевые координаты и скачки > max_segment_km (артефакты GPS).
    """
    total = 0.0
    prev: Optional[Tuple[float, float]] = None
    for row in gps_data:
        parsed = parse_gps_row(row)
        if parsed is None:
            continue
        _, lat, lon = parsed
        if prev is not None:
            seg = haversine_km(prev[0], prev[1], lat, lon)
            if 0 < seg <= max_segment_km:
                total += seg
        prev = (lat, lon)
    return total


def analyze_gps_movement(gps_data: List[dict]) -> dict:
    """first_ts / last_ts по валидным GPS-точкам + total_km за период."""
    first_ts = last_ts = None
    for row in gps_data:
        parsed = parse_gps_row(row)
        if parsed is None:
            continue
        ts, _, _ = parsed
        if first_ts is None:
            first_ts = ts
        last_ts = ts
    return {
        "first_ts": first_ts,
        "last_ts": last_ts,
        "total_km": calc_daily_km(gps_data),
    }


def _im_value(im) -> float:
    try:
        if im is not None and str(im).strip().strip('"') != "":
            return float(im)
    except (ValueError, TypeError):
        pass
    return 0.0


def analyze_movement(accel_data: List[dict]) -> dict:
    """first_move_ts / last_move_ts / last_data_ts (порог движения IM>70)."""
    first_move = last_move = last_data = None
    for row in accel_data:
        ts = int(row["epochtime"])
        last_data = ts
        if _im_value(row["InstantMovement"]) > 70:
            if first_move is None:
                first_move = ts
            last_move = ts
    return {"first_move_ts": first_move, "last_move_ts": last_move,
            "last_data_ts": last_data}


def find_stops(accel_data: List[dict]) -> List[dict]:
    """Непрерывные периоды остановки (IM<=0).

    Возвращает {"start_ts","end_ts","duration","ongoing"}.
    ongoing=True — остановка не завершена на момент последней точки.
    """
    if not accel_data:
        return []
    stops: List[dict] = []
    stop_start: Optional[int] = None
    for row in accel_data:
        ts = int(row["epochtime"])
        moving = _im_value(row["InstantMovement"]) > 0
        if not moving:
            if stop_start is None:
                stop_start = ts
        else:
            if stop_start is not None:
                stops.append({"start_ts": stop_start, "end_ts": ts,
                              "duration": ts - stop_start, "ongoing": False})
                stop_start = None
    if stop_start is not None:
        last_ts = int(accel_data[-1]["epochtime"])
        stops.append({"start_ts": stop_start, "end_ts": last_ts,
                      "duration": last_ts - stop_start, "ongoing": True})
    return stops


def build_processing_segments(chunk_start: int, chunk_end: int,
                              stops: List[dict],
                              long_stop_threshold: int) -> List[Tuple[int, int]]:
    """Сегменты обработки, пропуская остановки > long_stop_threshold."""
    long_stops = [
        s for s in stops
        if s["duration"] >= long_stop_threshold
        and s["start_ts"] < chunk_end and s["end_ts"] > chunk_start
    ]
    if not long_stops:
        return [(chunk_start, chunk_end)]

    long_stops.sort(key=lambda s: s["start_ts"])
    segments: List[Tuple[int, int]] = []
    current = chunk_start
    for stop in long_stops:
        seg_end = max(current, min(stop["start_ts"], chunk_end))
        if seg_end > current:
            segments.append((current, seg_end))
        current = max(current, stop["end_ts"])
    if chunk_end > current:
        segments.append((current, chunk_end))
    return segments


def calc_unprocessed_video(video_infos: List[dict],
                           process_from: int) -> Tuple[int, int]:
    """Общая длительность необработанного видео → (total_seconds, max_end_ts)."""
    total = 0
    max_end = process_from
    for v in video_infos:
        eff_start = max(v["start"], process_from)
        if v["end"] > eff_start:
            total += v["end"] - eff_start
            max_end = max(max_end, v["end"])
    return total, max_end


def parse_json(raw):
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw))
    except Exception:
        return None


def parse_region(region_raw) -> list:
    """[x1,y1,x2,y2] или [...,'line'/'stop_rectangle'] → список точек.

    line/stop_rectangle → 2 точки; иначе прямоугольник (4 угла).
    """
    if not isinstance(region_raw, (list, tuple)) or len(region_raw) < 4:
        return []
    try:
        if len(region_raw) >= 5 and str(region_raw[4]).lower().startswith(
                ("line", "stop_rectangle")):
            x1, y1, x2, y2 = map(int, region_raw[:4])
            return [[x1, y1], [x2, y2]]
        x1, y1, x2, y2 = map(int, region_raw[:4])
        xn, xx = min(x1, x2), max(x1, x2)
        yn, yx = min(y1, y2), max(y1, y2)
        return [[xn, yn], [xx, yn], [xx, yx], [xn, yx]]
    except Exception:
        return []
