#!/usr/bin/env python3
"""Сегменты обработки по сработкам LineCrossingEvents (режим countByAlarms).

Объединяет build_alarm_segments (main.py) и build_mapping_from_alarms-логику
(preparationToMeaningfulNeyroRequest.py). Возвращает временные сегменты
[start_ts, end_ts]; превращение в кадры делает core/tasks.py.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import List, Tuple

from .intervals import merge_intervals

# Соседние сработки ближе этого порога объединяются в одну группу.
ALARM_MERGE_GAP_SEC = 15


def normalize_cam_name(cam: str) -> str:
    """В LineCrossingEvents cam_name без суффикса канала (Z78GE_1 → Z78GE)."""
    return cam.split("_", 1)[0] if "_" in cam else cam


def fetch_line_crossing_events(db, cam: str,
                               window_start_ts_utc: int,
                               window_end_ts_utc: int) -> List[int]:
    """epoch-секунды (UTC) сработок LineCrossingEvents в окне. Колонка time — UTC."""
    start_utc = datetime.fromtimestamp(
        window_start_ts_utc, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    end_utc = datetime.fromtimestamp(
        window_end_ts_utc, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    cam_db = normalize_cam_name(cam)
    rows = db.query(
        "SELECT `time` FROM `LineCrossingEvents` "
        "WHERE `cam_name` = %s AND `time` BETWEEN %s AND %s ORDER BY `time` ASC",
        (cam_db, start_utc, end_utc),
    )

    events_ts: List[int] = []
    for r in rows:
        t = r["time"]
        if isinstance(t, datetime):
            events_ts.append(int(t.replace(tzinfo=timezone.utc).timestamp()))
        else:
            try:
                events_ts.append(int(datetime.strptime(str(t), "%Y-%m-%d %H:%M:%S")
                                     .replace(tzinfo=timezone.utc).timestamp()))
            except Exception:
                logging.warning("Не удалось распарсить time=%r", t)
    return events_ts


def build_alarm_segments(db, cam: str, window_start: int, window_end: int,
                         padding_left_sec: float, padding_right_sec: float,
                         merge_gap_sec: float = ALARM_MERGE_GAP_SEC
                         ) -> List[Tuple[int, int]]:
    """Сегменты [start_ts, end_ts] по сработкам в окне:
    группировка близких → паддинги → merge → обрезка по окну.
    """
    if window_end <= window_start:
        return []

    events_ts = fetch_line_crossing_events(db, cam, window_start, window_end)
    if not events_ts:
        return []

    sorted_ts = sorted(events_ts)
    groups: List[Tuple[int, int]] = []
    g_start = g_end = sorted_ts[0]
    for t in sorted_ts[1:]:
        if t - g_end < merge_gap_sec:
            g_end = t
        else:
            groups.append((g_start, g_end))
            g_start = g_end = t
    groups.append((g_start, g_end))

    padded = [(float(gs) - padding_left_sec, float(ge) + padding_right_sec)
              for gs, ge in groups]
    merged = merge_intervals(padded)

    segments: List[Tuple[int, int]] = []
    for s_f, e_f in merged:
        s_i = max(int(window_start), int(math.floor(s_f)))
        e_i = min(int(window_end), int(math.ceil(e_f)))
        if e_i > s_i:
            segments.append((s_i, e_i))
    return segments
