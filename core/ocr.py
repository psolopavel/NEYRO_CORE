#!/usr/bin/env python3
"""OCR штампа даты/времени на кадре CCTV + определение реального start_ts видео.

Объединяет бывшие ocr_timestamp.py и две копии detect_video_real_start_ts_ocr
(из main.py и counter.py/preparationToMeaningfulNeyroRequest.py).

Публичный API:
    is_available() -> bool
    extract_datetime_from_frame(frame_bgr) -> {"date","time","raw"}
    detect_real_start_ts(video_path, filename_start_ts, tz) -> int | None
        (None => файл повреждён, нужно пропустить)

easyocr/cv2 грузятся лениво. Если недоступны — OCR-подстройка отключается,
возвращается filename_start_ts.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional, Tuple

try:
    import cv2
    import numpy as np
    _HAS_CV2 = True
except ImportError:
    _HAS_CV2 = False

from .video import VIDEO_FPS_FALLBACK

# ── параметры распознавания штампа ──
_OCR_ALLOWLIST = "0123456789-:. /"
_OCR_MIN_CONF = 0.35
_OCR_ROI_HEIGHT_FRAC = 0.30
_OCR_DARK_ROI_MEDIAN_THRESH = 130

# ── параметры подстройки start_ts (скан с конца к началу) ──
OCR_MAX_ATTEMPTS = 10
OCR_STEP_SECONDS = 60
OCR_MAX_DRIFT_SECONDS = 4 * 60

_ocr_reader = None


def _get_reader():
    global _ocr_reader
    if _ocr_reader is not None:
        return _ocr_reader
    try:
        import easyocr  # type: ignore
    except Exception as e:
        logging.warning("easyocr не установлен (%s), OCR-подстройка отключена", e)
        return None
    try:
        _ocr_reader = easyocr.Reader(["en"], gpu=True)
    except Exception as e:
        logging.warning("EasyOCR GPU init failed (%s), пробую CPU", e)
        try:
            _ocr_reader = easyocr.Reader(["en"], gpu=False)
        except Exception as e2:
            logging.warning("EasyOCR init полностью провалился: %s", e2)
            return None
    return _ocr_reader


def is_available() -> bool:
    return _HAS_CV2 and _get_reader() is not None


# ── обработка ROI ──

def _top_strip_roi(bgr):
    h, w = bgr.shape[:2]
    return bgr[0:int(h * _OCR_ROI_HEIGHT_FRAC), 0:w]


def _to_binary(gray):
    gray = cv2.resize(gray, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    if np.median(gray) < _OCR_DARK_ROI_MEDIAN_THRESH:
        gray = cv2.bitwise_not(gray)
    _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if (thresh == 0).sum() > (thresh == 255).sum():
        thresh = cv2.bitwise_not(thresh)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
    thresh = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel)
    return cv2.cvtColor(thresh, cv2.COLOR_GRAY2BGR)


def _read_sorted(reader, bgr) -> str:
    results = reader.readtext(bgr, allowlist=_OCR_ALLOWLIST,
                              paragraph=False, detail=1)
    filtered = [(box, t, c) for box, t, c in results if c >= _OCR_MIN_CONF]
    filtered.sort(key=lambda r: min(p[0] for p in r[0]))
    return " ".join(t for _, t, _ in filtered)


def _clean_for_regex(s: str) -> str:
    s = s.replace(".", ":").replace(" ", "")
    for a, b in (("O", "0"), ("o", "0"), ("Q", "0"), ("l", "1"), ("I", "1")):
        s = s.replace(a, b)
    return s.replace(".:", ":").replace(":.", ":")


def _extract_datetime_parts(cleaned: str) -> Tuple[Optional[str], Optional[str]]:
    m = re.search(r"(\d{2}[/:.\-]\d{2}[/:.\-]\d{4}).{0,3}?(\d{2}:\d{2}:\d{2})", cleaned)
    if m:
        return m.group(1), m.group(2)
    m = re.search(r"(\d{2}:\d{2}:\d{2})", cleaned)
    if m:
        return None, m.group(1)
    return None, None


def extract_datetime_from_frame(frame_bgr) -> dict:
    if frame_bgr is None or not _HAS_CV2:
        return {"date": None, "time": None, "raw": ""}
    reader = _get_reader()
    if reader is None:
        return {"date": None, "time": None, "raw": ""}

    roi_color = _top_strip_roi(frame_bgr)
    gray = cv2.cvtColor(roi_color, cv2.COLOR_BGR2GRAY)
    processed = _to_binary(gray)

    raw = _read_sorted(reader, processed)
    if not raw.strip():
        raw = _read_sorted(reader, roi_color)

    cleaned = _clean_for_regex(raw)
    date_s, time_s = _extract_datetime_parts(cleaned)
    return {"date": date_s, "time": time_s, "raw": raw}


# ── подстройка start_ts ──

def _parse_ocr_datetime_to_ts(date_str: Optional[str], time_str: Optional[str],
                              fallback_date, tz: timezone) -> Optional[int]:
    if not time_str:
        return None
    try:
        hh, mm, ss = (int(x) for x in time_str.split(":"))
    except Exception:
        return None
    if not (0 <= hh < 24 and 0 <= mm < 60 and 0 <= ss < 60):
        return None

    date_obj = fallback_date
    if date_str:
        for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%d:%m:%Y"):
            try:
                date_obj = datetime.strptime(date_str, fmt).date()
                break
            except Exception:
                continue

    return int(datetime(date_obj.year, date_obj.month, date_obj.day,
                        hh, mm, ss, tzinfo=tz).timestamp())


def detect_real_start_ts(
    video_path: str,
    filename_start_ts: int,
    tz: timezone,
    max_attempts: int = OCR_MAX_ATTEMPTS,
    step_seconds: int = OCR_STEP_SECONDS,
    max_drift_seconds: int = OCR_MAX_DRIFT_SECONDS,
) -> Optional[int]:
    """Реальный start_ts видео по штампу на кадре.

    Скан С КОНЦА к началу (в начале записи часы камеры часто не синхронны).
    Возвращает:
      - candidate_start, если drift в пределах допустимого;
      - filename_start_ts, если OCR/cv2 недоступны или ничего не подошло;
      - None, если файл повреждён (cv2.error при чтении) — пропустить файл.
    """
    if not _HAS_CV2:
        return filename_start_ts
    try:
        if not is_available():
            return filename_start_ts
    except Exception:
        return filename_start_ts

    try:
        cap = cv2.VideoCapture(video_path)
    except Exception:
        return filename_start_ts
    if not cap.isOpened():
        return filename_start_ts

    fps = cap.get(cv2.CAP_PROP_FPS) or VIDEO_FPS_FALLBACK
    if fps <= 0:
        fps = VIDEO_FPS_FALLBACK
    frames_total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    duration_sec = frames_total / fps if frames_total > 0 and fps > 0 else 0.0

    base = os.path.basename(video_path)
    fallback_date = datetime.fromtimestamp(filename_start_ts, tz=tz).date()

    if duration_sec <= 0:
        cap.release()
        return filename_start_ts

    chosen_ts: Optional[int] = None
    try:
        for attempt in range(max_attempts):
            offset_sec = duration_sec - attempt * step_seconds
            if attempt == 0:
                offset_sec -= 1.0
            if offset_sec < 0:
                break

            frame_idx = int(round(offset_sec * fps))
            if frames_total > 0:
                frame_idx = max(0, min(frame_idx, frames_total - 1))
            try:
                cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
                ok, frame = cap.read()
            except cv2.error as exc:
                logging.warning("OCR %s: cv2.error кадр %d — пропуск файла: %s",
                                base, frame_idx, exc)
                return None
            if not ok or frame is None:
                continue

            parts = extract_datetime_from_frame(frame)
            time_s = parts.get("time")
            if not time_s:
                continue

            ocr_ts = _parse_ocr_datetime_to_ts(parts.get("date"), time_s,
                                               fallback_date, tz)
            if ocr_ts is None:
                continue

            candidate_start = int(round(ocr_ts - offset_sec))
            if abs(candidate_start - filename_start_ts) <= max_drift_seconds:
                chosen_ts = candidate_start
                break
    finally:
        cap.release()

    if chosen_ts is None:
        logging.warning("OCR %s: валидное время не получено — fallback к имени (%s)",
                        base, filename_start_ts)
        return filename_start_ts
    return chosen_ts
