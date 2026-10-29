#!/usr/bin/env python3
"""Единый YOLO-подсчёт пассажиров (заменяет index.py + counter.py).

Запуск как subprocess (GPU-память освобождается по завершении процесса):
    python -m core.yolo_counter <payload.json> <result.json>
    или
    python core/yolo_counter.py <payload.json> <result.json>

Payload JSON:
{
    "camera_name":    "7NVTT",
    "video_tasks":    {"/path/video.mp4": [[start_frame, end_frame], ...]},
    "region":         [[x1,y1], ...],
    "model_path":     "/home/NEYRO_CORE/models/yolov8m.engine",
    "person_quality": "0.5",
    "save_video_path": "/path/out.mp4"   # null => не сохранять
}

Result JSON: {"in": int, "out": int, "error": str|null}
"""

from __future__ import annotations

import json
import logging
import os
import sys

# Поддержка запуска и как модуль (-m core.yolo_counter), и как файл.
try:
    from .logging_setup import safe_filename, setup_root_logger
    from .video import scale_region_to_frame
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from core.logging_setup import safe_filename, setup_root_logger
    from core.video import scale_region_to_frame

COUNTER_LOG_DIR = "/tmp/NEYRO_LOGS/worker"


def resolve_conf(raw) -> float:
    try:
        v = float(raw)
        if 0.0 < v <= 1.0:
            return v
    except Exception:
        pass
    return 0.5


def _camera_from_video_tasks(video_tasks: dict) -> str:
    for path in video_tasks:
        parts = path.replace("\\", "/").split("/")
        if len(parts) >= 2:
            return parts[-2]
    return "unknown"


def count_people(video_tasks, region, model_path, conf, save_video_path=None):
    """Прогон YOLO ObjectCounter по диапазонам кадров.

    video_tasks: {path: [[start_frame, end_frame], ...]}
    Возвращает (in_count, out_count, written_frames).
    """
    import cv2
    from ultralytics import solutions

    counter = solutions.ObjectCounter(
        model=model_path, region=region, classes=[0],
        line_width=1, conf=conf, show=False,
    )

    in_count, out_count, written_frames = 0, 0, 0
    res = None
    writer = None
    writer_frame_size = None

    for video_path, frame_ranges in video_tasks.items():
        if not os.path.exists(video_path):
            logging.error("Файл не найден: %s", video_path)
            continue
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            logging.error("Не удалось открыть: %s", video_path)
            continue

        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

        if save_video_path and writer is None:
            fps = cap.get(cv2.CAP_PROP_FPS)
            if not fps or fps <= 0:
                fps = 6.0
            writer_frame_size = (w, h)
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            os.makedirs(os.path.dirname(save_video_path) or ".", exist_ok=True)
            writer = cv2.VideoWriter(save_video_path, fourcc, fps, writer_frame_size)
            if not writer.isOpened():
                logging.error("Не удалось создать видеофайл: %s", save_video_path)
                writer = None

        counter.region = scale_region_to_frame(region, w, h)

        for start_f, end_f in frame_ranges:
            if total_frames > 0 and start_f >= total_frames:
                logging.warning("start_f %d >= total %d, пропуск", start_f, total_frames)
                continue
            if total_frames > 0:
                end_f = min(end_f, total_frames - 1)

            cap.set(cv2.CAP_PROP_POS_FRAMES, start_f)
            current = start_f
            decode_errors = 0
            while current <= end_f:
                # Битый/обрезанный кадр может бросить cv2.error прямо из read()
                # (Assertion _step >= minstep) — пропускаем его, не роняя задачу.
                try:
                    ret, frame = cap.read()
                except cv2.error as exc:
                    decode_errors += 1
                    current += 1
                    if decode_errors <= 3 or decode_errors % 500 == 0:
                        logging.warning("битый кадр @%d (%d-я ошибка): %s",
                                        current, decode_errors, exc)
                    # Поток мог "сломаться" целиком — пробуем переустановить позицию.
                    cap.set(cv2.CAP_PROP_POS_FRAMES, current)
                    continue
                if not ret:
                    current += 1
                    continue
                res = counter(frame)
                if writer is not None and res is not None:
                    im = res.plot_im
                    if (im.shape[1] != writer_frame_size[0]
                            or im.shape[0] != writer_frame_size[1]):
                        im = cv2.resize(im, writer_frame_size,
                                        interpolation=cv2.INTER_AREA)
                    writer.write(im)
                    written_frames += 1
                current += 1

            if decode_errors:
                logging.warning("диапазон %d-%d: пропущено %d битых кадров",
                                start_f, end_f, decode_errors)

        cap.release()
        if res is not None:
            in_count, out_count = res.in_count, res.out_count

    if writer is not None:
        writer.release()
        logging.info("Видео записано: %s (%d кадров)", save_video_path, written_frames)

    return in_count, out_count, written_frames


def _write_result(path, in_count, out_count, error):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"in": in_count, "out": out_count, "error": error}, f)
    os.replace(tmp, path)


def main():
    if len(sys.argv) < 3:
        sys.exit("Usage: python -m core.yolo_counter <payload.json> <result.json>")

    payload_path, result_path = sys.argv[1], sys.argv[2]

    try:
        with open(payload_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as e:
        _write_result(result_path, 0, 0, str(e))
        sys.exit(1)

    video_tasks = payload.get("video_tasks", {})
    region = payload.get("region", [])
    model_path = payload.get("model_path") or "/home/NEYRO_CORE/models/yolov8m.engine"
    conf = resolve_conf(payload.get("person_quality", "0.5"))
    save_video_path = payload.get("save_video_path")
    camera_name = payload.get("camera_name") or _camera_from_video_tasks(video_tasks)

    # Это отдельный subprocess: настраиваем КОРНЕВОЙ логгер на per-camera файл,
    # чтобы все logging.* вызовы (в т.ч. из count_people) реально писались.
    log_file = os.path.join(COUNTER_LOG_DIR, f"{safe_filename(camera_name)}.log")
    setup_root_logger(log_file)

    total_frames = sum(sum(e - s + 1 for s, e in ranges)
                       for ranges in video_tasks.values())
    logging.info(
        "Counter [%s]: %d видео, %d диапазонов, ~%d кадров, model=%s, conf=%.2f, save=%s",
        camera_name, len(video_tasks),
        sum(len(r) for r in video_tasks.values()),
        total_frames, os.path.basename(model_path), conf,
        save_video_path or "off",
    )

    try:
        if not video_tasks:
            logging.warning("Пустой video_tasks — нечего обрабатывать")
            _write_result(result_path, 0, 0, None)
            return
        in_count, out_count, _ = count_people(
            video_tasks, region, model_path, conf, save_video_path=save_video_path)
        logging.info("Result: in=%d, out=%d", in_count, out_count)
        if in_count == 0 and out_count == 0:
            logging.warning("⚠️  Оба счётчика равны 0 (камера %s)", camera_name)
        _write_result(result_path, in_count, out_count, None)
    except Exception as e:
        logging.exception("Ошибка подсчёта")
        _write_result(result_path, 0, 0, str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
