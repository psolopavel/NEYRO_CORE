#!/usr/bin/env python3
"""QueueSource — источник задач по запросам из таблицы AI_CountingQueue.

Заменяет связку procesManager → preparation → index.
Результат пишется в AI_ResultsOfCounting (через CountingResultsStore),
затем — отчёт владельцу машины, если у неё не осталось незавершённых запросов
за этот день (mailer.ResultMailer).

Сегменты:
  - countByAlarms (type6) → по сработкам LineCrossingEvents в окне [date_from, date_to];
  - иначе → всё окно [date_from, date_to] одним сегментом.
  - type8 = 1 → из сегментов вычитается время в зонах counting_ignoring (core/zones.py).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import List, Optional

from core import alarms, zones
from core.logging_setup import get_camera_logger
from core.segments import parse_region
from job import Job, SOURCE_QUEUE
from sources.base import TaskSource

QUEUE_PRIORITY = 100  # выше авто-задач


class QueueSource(TaskSource):
    name = SOURCE_QUEUE

    def __init__(self, cfg, db, queue_store, results_store, mailer, tz: timezone,
                 zones_db):
        self.cfg = cfg
        self.db = db
        self.zones_db = zones_db
        self.queue = queue_store
        self.results = results_store
        self.mailer = mailer
        self.tz = tz

    def discover(self, now_epoch: int, available_slots: int) -> List[Job]:
        jobs: List[Job] = []
        for qjob in self.queue.list_pending():
            if len(jobs) >= available_slots:
                break
            # Атомарно захватываем; если не удалось — взял другой воркер.
            if not self.queue.claim(qjob.id, worker_id=f"mgr-{now_epoch}"):
                continue
            job = self._build(qjob)
            if job is None:
                self.queue.mark_error(qjob.id, "не удалось построить задачу")
                continue
            jobs.append(job)
        return jobs

    def _build(self, qjob) -> Optional[Job]:
        cam = qjob.cam_name
        params = qjob.params or {}

        window_start = int(qjob.date_from)
        window_end = int(qjob.date_to) if qjob.date_to else window_start

        date_str = datetime.fromtimestamp(
            window_start, tz=self.tz).strftime("%Y-%m-%d")
        date_compact = datetime.fromtimestamp(
            window_start, tz=self.tz).strftime("%Y%m%d")
        clog = get_camera_logger(
            self.cfg.camera_log_dir, cam, SOURCE_QUEUE, date_str)

        if window_end <= window_start:
            clog.warning("queue#%s: некорректное окно", qjob.id)
            return None

        region = parse_region(params.get("main")) if params.get("main") else []
        model_size = params.get("type1") or "Medium"
        person_quality = params.get("type2") or "0.5"
        save_video = bool(params.get("save_video", False))

        # countByAlarms из type6
        count_by_alarms, pad_l, pad_r = self._parse_alarm_cfg(params.get("type6"))

        if count_by_alarms:
            try:
                segs = alarms.build_alarm_segments(
                    self.db, cam, window_start, window_end, pad_l, pad_r)
            except Exception:
                clog.exception("queue#%s: ошибка LineCrossingEvents", qjob.id)
                return None
            if not segs:
                clog.info("queue#%s: нет сработок — результат 0", qjob.id)
        else:
            segs = [(window_start, window_end)]

        if self._is_enabled(params.get("type8")) and segs:
            segs = self._apply_ignore_zones(qjob.id, cam, segs, clog)
            if segs is None:
                return None
            if not segs:
                clog.info("queue#%s: всё время в зонах игнорирования — результат 0",
                          qjob.id)

        def _on_success(in_c, out_c, _save_path, _cam=cam, _ds=date_str,
                        _qid=qjob.id, _clog=clog):
            self.results.save(_ds, _cam, in_c, out_c)
            _clog.info("💾 результат сохранён: in=%d out=%d", in_c, out_c)
            self.queue.mark_done(_qid)
            self.mailer.schedule_queue(_cam, _ds, _qid)

        def _on_error(exc, _qid=qjob.id):
            self.queue.mark_error(_qid, str(exc))

        return Job(
            source=SOURCE_QUEUE, camera_name=cam, date_str=date_str,
            date_compact=date_compact, segments=segs,
            chunk_start=window_start, chunk_end=window_end,
            region=region, model_size=model_size,
            person_quality=person_quality, save_video=save_video,
            on_success=_on_success, on_error=_on_error,
            priority=QUEUE_PRIORITY,
            meta={"queue_id": qjob.id},
        )

    def _apply_ignore_zones(self, qid, cam, segs, clog):
        """Сегменты без времени в зонах counting_ignoring; None — ошибка (задача в error)."""
        try:
            tracker = zones.find_camera_tracker(self.db, cam)
            if not tracker:
                clog.warning("queue#%s: type8 — трекер камеры не найден, "
                             "фильтр зон не применяется", qid)
                return segs
            return zones.apply_ignore_zones(
                self.db, self.zones_db, tracker["imei"], tracker["tracker_table"],
                tracker["tracker_id"], segs, clog)
        except Exception:
            clog.exception("queue#%s: ошибка фильтра зон игнорирования (type8)", qid)
            return None

    @staticmethod
    def _is_enabled(raw) -> bool:
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true")

    @staticmethod
    def _parse_alarm_cfg(type6):
        if type6 is None:
            return False, 10.0, 10.0
        parsed = type6
        if isinstance(type6, str):
            try:
                parsed = json.loads(type6)
            except Exception:
                return False, 10.0, 10.0
        if not isinstance(parsed, dict):
            return False, 10.0, 10.0
        raw_flag = parsed.get("countByAlarms", False)
        enabled = raw_flag if isinstance(raw_flag, bool) \
            else str(raw_flag).strip().lower() == "true"
        try:
            pad_l = float(parsed.get("alarmPaddingLeft", 10))
        except Exception:
            pad_l = 10.0
        try:
            pad_r = float(parsed.get("alarmPaddingRight", 10))
        except Exception:
            pad_r = 10.0
        return enabled, pad_l, pad_r
