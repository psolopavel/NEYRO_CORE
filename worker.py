#!/usr/bin/env python3
"""Worker — единый пайплайн обработки Job (заменяет _process_chunk из main.py
и всю цепочку preparation→index из request-менеджера).

Работает одинаково для обоих источников:
    1) список + скачивание видео (rsync)
    2) build_video_tasks: сегменты времени → диапазоны кадров (+ OCR)
    3) запуск core/yolo_counter.py как subprocess (GPU-память освобождается)
    4) чтение результата
    5) если save_video — сжать + загрузить на сервер + удалить локально
    6) колбэк job.on_success / job.on_error (источник решает, куда писать)
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import timezone

from core import tasks, transfer
from core.logging_setup import get_camera_logger
from job import Job


class Worker:
    def __init__(self, cfg, tz: timezone):
        self.cfg = cfg
        self.tz = tz
        os.makedirs(cfg.tmp_dir, exist_ok=True)

    def process(self, job: Job):
        # Per-camera микро-лог с пометкой источника (AUTO/QUEUE).
        log = get_camera_logger(
            self.cfg.camera_log_dir, job.camera_name, job.source, job.date_str)
        t0 = time.time()
        try:
            self._run(job, log)
        except Exception as exc:
            log.exception("❌ [%s] ошибка обработки", job.label)
            if job.on_error:
                job.on_error(exc)
        finally:
            log.info("⏱️  [%s] завершено за %.0fс", job.label, time.time() - t0)

    def _run(self, job: Job, log):
        cam = job.camera_name

        # 1) видео
        remote = transfer.list_remote_videos(
            self.cfg, cam, job.date_compact, job.chunk_start, job.chunk_end)
        if not remote:
            log.warning("⚠️  [%s] нет видео за период", job.label)
            self._success(job, 0, 0, None)
            return
        local_files = transfer.download_videos(self.cfg, remote, cam)

        # 2) кадры
        video_tasks = tasks.build_video_tasks(
            local_files, job.segments, self.tz, use_ocr=True)
        if not video_tasks:
            log.warning("⚠️  [%s] нет подходящих сегментов в видео", job.label)
            self._success(job, 0, 0, None)
            return

        # 3) payload + subprocess
        ts_id = int(time.time() * 1000)
        payload_path = os.path.join(self.cfg.tmp_dir, f"payload_{cam}_{ts_id}.json")
        result_path = os.path.join(self.cfg.tmp_dir, f"result_{cam}_{ts_id}.json")

        save_video_path = None
        if job.save_video and self.cfg.save_video_dir:
            os.makedirs(self.cfg.save_video_dir, exist_ok=True)
            save_video_path = os.path.join(
                self.cfg.save_video_dir,
                f"{cam}_{job.date_compact}_{job.chunk_start}_{job.chunk_end}.mp4")

        payload = {
            "camera_name":     cam,
            "video_tasks":     video_tasks,
            "region":          job.region,
            "model_path":      self.cfg.model_path(job.model_size),
            "person_quality":  job.person_quality,
            "save_video_path": save_video_path,
        }
        with open(payload_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)

        in_count, out_count = self._run_counter(
            job, payload_path, result_path, log)

        # 5) видео: сжать → загрузить → удалить
        if save_video_path and os.path.exists(save_video_path):
            self._handle_save_video(job, save_video_path, log)

        # 6) результат
        self._success(job, in_count, out_count,
                      save_video_path if job.save_video else None)

    def _run_counter(self, job: Job, payload_path, result_path, log):
        cmd = [self.cfg.python_path,
               os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "core", "yolo_counter.py"),
               payload_path, result_path]
        try:
            proc = subprocess.run(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, timeout=None)  # обработка может длиться часами
            if proc.returncode < 0:
                # убит сигналом — не сохраняем, повторим позже
                log.warning("⚠️  [%s] counter убит сигналом %d — повторим позже",
                            job.label, -proc.returncode)
                raise RuntimeError(f"counter killed by signal {-proc.returncode}")
            if proc.returncode != 0:
                log.error("❌ [%s] counter вернул код %d\nstderr: %s",
                          job.label, proc.returncode, proc.stderr.strip()[-500:])
        finally:
            _silent_unlink(payload_path)

        in_count = out_count = 0
        if os.path.exists(result_path):
            try:
                with open(result_path, "r", encoding="utf-8") as f:
                    res = json.load(f)
                in_count = res.get("in", 0)
                out_count = res.get("out", 0)
                if res.get("error"):
                    log.warning("⚠️  [%s] counter ошибка: %s", job.label, res["error"])
            except Exception:
                log.exception("[%s] ошибка чтения результата", job.label)
            finally:
                _silent_unlink(result_path)

        log.info("✅ [%s] in=%d out=%d", job.label, in_count, out_count)
        return in_count, out_count

    def _handle_save_video(self, job: Job, save_video_path, log):
        log.info("🗜️  [%s] сжатие видео %s", job.label,
                 os.path.basename(save_video_path))
        if not transfer.compress_video(save_video_path):
            log.warning("⚠️  [%s] не удалось сжать видео", job.label)
            return
        log.info("📤 [%s] загрузка видео на сервер", job.label)
        if transfer.upload_result_video(self.cfg, save_video_path,
                                        job.camera_name, job.date_str):
            _silent_unlink(save_video_path)
            log.info("🗑️  [%s] локальное видео удалено", job.label)
        else:
            log.warning("⚠️  [%s] не удалось загрузить видео, оставляем локально",
                        job.label)

    @staticmethod
    def _success(job: Job, in_count, out_count, save_video_path):
        if job.on_success:
            job.on_success(in_count, out_count, save_video_path)


def _silent_unlink(path: str):
    try:
        os.unlink(path)
    except Exception:
        pass
