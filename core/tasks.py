#!/usr/bin/env python3
"""Превращение временных сегментов в диапазоны кадров по видеофайлам.

build_video_tasks (из main.py) — единая реализация. Раньше в request-ветке
этим занимался build_mapping_for_videos в preparationToMeaningfulNeyroRequest.py;
теперь обе ветки используют этот модуль.

Результат: {video_path: [[start_frame, end_frame], ...]} (хронологический порядок).
"""

from __future__ import annotations

import logging
import os
from datetime import timezone
from typing import Dict, List, Tuple

from . import ocr, video
from .intervals import merge_into_covered, subtract_intervals


def build_video_tasks(local_files: List[str],
                      segments: List[Tuple[int, int]],
                      tz: timezone,
                      use_ocr: bool = True) -> Dict[str, List[List[int]]]:
    """Для каждого видео — диапазоны кадров по временным сегментам.

    Перекрывающиеся файлы разрешаются с приоритетом более длинных:
    каждый момент времени обрабатывается ровно одним файлом.
    """
    video_entries: List[dict] = []
    for vpath in local_files:
        if not os.path.exists(vpath):
            logging.warning("Файл не найден: %s", vpath)
            continue

        parsed = video.parse_timestamps_from_name(vpath)
        if not parsed:
            logging.warning("Не удалось распарсить имя: %s", os.path.basename(vpath))
            continue
        vid_start, vid_end = parsed
        if vid_end <= vid_start:
            continue

        if not video.is_readable(vpath):
            logging.warning("Битый/нечитаемый видеофайл, пропуск: %s",
                            os.path.basename(vpath))
            continue

        fps = video.get_fps(vpath)

        if use_ocr:
            real_start = ocr.detect_real_start_ts(vpath, vid_start, tz)
            if real_start is None:
                logging.warning("Битый видеофайл (OCR), пропуск: %s",
                                os.path.basename(vpath))
                continue
        else:
            real_start = vid_start

        frames_count = video.get_frames_count(vpath)
        if frames_count > 0 and fps > 0:
            real_end = int(real_start + frames_count / fps)
        else:
            real_end = real_start + (vid_end - vid_start)

        video_entries.append({
            "path": vpath, "start": real_start, "end": real_end,
            "duration": real_end - real_start, "fps": fps,
        })

    # Приоритет длинным файлам: раздаём время без перекрытий.
    video_entries.sort(key=lambda x: x["duration"], reverse=True)
    covered: List[Tuple[float, float]] = []
    assignments: List[Tuple[dict, List[Tuple[float, float]]]] = []
    for entry in video_entries:
        uncovered = subtract_intervals(entry["start"], entry["end"], covered)
        if not uncovered:
            continue
        assignments.append((entry, uncovered))
        for us, ue in uncovered:
            covered = merge_into_covered(covered, us, ue)

    assignments.sort(key=lambda x: x[1][0][0])

    tasks: Dict[str, List[List[int]]] = {}
    for entry, eff_ranges in assignments:
        frame_ranges: List[List[int]] = []
        for seg_start, seg_end in segments:
            for eff_start, eff_end in eff_ranges:
                overlap_start = max(eff_start, seg_start)
                overlap_end = min(eff_end, seg_end)
                if overlap_end <= overlap_start:
                    continue
                start_frame = int((overlap_start - entry["start"]) * entry["fps"])
                end_frame = int((overlap_end - entry["start"]) * entry["fps"]) - 1
                if end_frame >= start_frame:
                    frame_ranges.append([start_frame, end_frame])
        if frame_ranges:
            frame_ranges.sort(key=lambda x: x[0])
            tasks[entry["path"]] = frame_ranges
    return tasks
