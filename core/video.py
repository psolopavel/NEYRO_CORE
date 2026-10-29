#!/usr/bin/env python3
"""Утилиты для работы с видеофайлами (FPS, число кадров, читаемость,
парсинг timestamps из имени).

cv2 опционален: при отсутствии возвращаются безопасные fallback-значения.
"""

from __future__ import annotations

import logging
import math
import os
from typing import List, Optional, Tuple

try:
    import cv2
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False

VIDEO_FPS_FALLBACK = 6.0


def get_fps(video_path: str) -> float:
    if not HAS_CV2:
        return VIDEO_FPS_FALLBACK
    try:
        cap = cv2.VideoCapture(video_path)
        fps = cap.get(cv2.CAP_PROP_FPS) if cap.isOpened() else 0.0
        cap.release()
        if fps > 0 and math.isfinite(fps):
            return fps
    except Exception:
        pass
    return VIDEO_FPS_FALLBACK


def get_frames_count(video_path: str) -> int:
    if not HAS_CV2:
        return 0
    try:
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            cap.release()
            return 0
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        cap.release()
        return max(0, n)
    except Exception:
        return 0


def is_readable(video_path: str) -> bool:
    """Проверка что видео открывается и кадры читаются без cv2.error."""
    if not HAS_CV2:
        return True
    cap = None
    try:
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            return False
        frames_total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if frames_total <= 0:
            ok, frame = cap.read()
            return bool(ok and frame is not None and frame.size > 0)
        for idx in (0, frames_total // 2, max(0, frames_total - 1)):
            cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ok, frame = cap.read()
            if not ok or frame is None or frame.size == 0:
                return False
        return True
    except cv2.error:
        return False
    except Exception:
        return False
    finally:
        if cap is not None:
            cap.release()


def parse_timestamps_from_name(path: str) -> Optional[Tuple[int, int]]:
    """Имя: YYYYMMDD_startTs_endTs.mp4 → (start, end) или None."""
    name = os.path.basename(path)
    if name.lower().endswith(".mp4"):
        name = name[:-4]
    parts = name.split("_")
    if len(parts) < 3:
        return None
    try:
        start_ts, end_ts = int(parts[1]), int(parts[2])
        return start_ts, end_ts
    except (ValueError, IndexError):
        return None


def parse_video_timestamps(remote_files: List[str]) -> List[dict]:
    """Список {"path","start","end"} отсортированный по start."""
    result: List[dict] = []
    for fpath in remote_files:
        parsed = parse_timestamps_from_name(fpath.split("/")[-1])
        if not parsed:
            continue
        s, e = parsed
        if e > s:
            result.append({"path": fpath, "start": s, "end": e})
    result.sort(key=lambda x: x["start"])
    return result


def scale_region_to_frame(region, frame_w: int, frame_h: int):
    """Масштабировать region если он выходит за пределы кадра."""
    if not region:
        return region
    try:
        max_x = max(p[0] for p in region if len(p) >= 2)
        max_y = max(p[1] for p in region if len(p) >= 2)
    except Exception:
        return region
    if max_x <= 0 or max_y <= 0:
        return region

    sx = min(1.0, frame_w / max_x) if frame_w > 0 else 1.0
    sy = min(1.0, frame_h / max_y) if frame_h > 0 else 1.0
    if sx == 1.0 and sy == 1.0:
        return region
    return [
        [int(round(p[0] * sx)), int(round(p[1] * sy))]
        for p in region if len(p) >= 2
    ]
