#!/usr/bin/env python3
"""AutoSource — самостоятельный источник задач по акселерометру.

Перенос логики discovery + построения сегментов из auto_counter/main.py.
Результаты пишутся в AI_ResultsOfAutoCounting (через ResultsStore).

Логика discover():
  1. Обнаружить камеры с AutoCounting (AI_carsData type=3, кроме type=7).
  2. Для каждой камеры найти незавершённые дни.
  3. По акселерометру определить необработанный период, сегменты
     (с пропуском длинных остановок ИЛИ по сработкам при countByAlarms).
  4. При type=8 вычесть время пребывания в зонах counting_ignoring (core/zones.py).
  5. Сформировать Job с колбэком сохранения чанка.
  6. Когда день завершён у всех камер машины — отчёт владельцу (mailer.ResultMailer).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, List

from core import alarms, segments, transfer, zones
from core.logging_setup import get_camera_logger
from core.segments import parse_json, parse_region
from job import Job, SOURCE_AUTO
from sources.base import TaskSource


class VehicleInfo:
    def __init__(self, camera_name, imei, tracker_id, tracker_table, region,
                 model_size, person_quality, in_out_config, priority=0,
                 count_by_alarms=False, alarm_padding_left=10.0,
                 alarm_padding_right=10.0, ignore_zones=False):
        self.camera_name = camera_name
        self.imei = imei
        self.tracker_id = tracker_id
        self.tracker_table = tracker_table
        self.region = region
        self.model_size = model_size
        self.person_quality = person_quality
        self.in_out_config = in_out_config
        self.priority = priority
        self.count_by_alarms = count_by_alarms
        self.alarm_padding_left = alarm_padding_left
        self.alarm_padding_right = alarm_padding_right
        self.ignore_zones = ignore_zones

    def __repr__(self):
        return f"Vehicle({self.camera_name}, imei={self.imei}, prio={self.priority})"


def discover_vehicles(db) -> List[VehicleInfo]:
    rows_ai = db.query(
        "SELECT `name`,`type`,`Squares` FROM `AI_carsData` "
        "WHERE `type` IN (0,1,2,3,6,7,8)"
    )

    auto_cameras: Dict[str, dict] = {}
    excluded: set = set()
    ignore_zones_cams: set = set()
    regions: Dict[str, list] = {}
    model_sizes: Dict[str, str] = {}
    person_quals: Dict[str, str] = {}
    alarm_cfgs: Dict[str, dict] = {}

    for r in rows_ai:
        name, t, sq_raw = r["name"], r["type"], r["Squares"]
        if t == 3:
            try:
                if int(str(sq_raw).strip()) == 1:
                    auto_cameras[name] = {"in": 1, "out": 0}
            except (ValueError, TypeError):
                pass
        elif t == 7:
            try:
                if int(str(sq_raw).strip()) == 1:
                    excluded.add(name)
            except (ValueError, TypeError):
                pass
        elif t == 8:
            try:
                if int(str(sq_raw).strip()) == 1:
                    ignore_zones_cams.add(name)
            except (ValueError, TypeError):
                pass
        elif t == 0:
            sq = parse_json(sq_raw)
            if isinstance(sq, dict):
                main = sq.get("main")
                if isinstance(main, list) and main:
                    regions[name] = parse_region(main[0])
        elif t == 1:
            if sq_raw:
                model_sizes[name] = str(sq_raw).strip()
        elif t == 2:
            if sq_raw:
                person_quals[name] = str(sq_raw).strip()
        elif t == 6:
            parsed = parse_json(sq_raw)
            if isinstance(parsed, dict):
                raw_flag = parsed.get("countByAlarms", False)
                enabled = raw_flag if isinstance(raw_flag, bool) \
                    else str(raw_flag).strip().lower() == "true"
                try:
                    pad_l = float(parsed.get("alarmPaddingLeft", 10))
                except (ValueError, TypeError):
                    pad_l = 10.0
                try:
                    pad_r = float(parsed.get("alarmPaddingRight", 10))
                except (ValueError, TypeError):
                    pad_r = 10.0
                alarm_cfgs[name] = {"enabled": enabled, "padL": pad_l, "padR": pad_r}

    for name in excluded:
        if name in auto_cameras:
            logging.info("⛔ Камера %s исключена (type=7)", name)
            auto_cameras.pop(name, None)

    if not auto_cameras:
        return []

    cam_names = list(auto_cameras.keys())

    all_conf = db.query(
        "SELECT `imei`,`cam_list` FROM `trackers_conf` WHERE `type`=0")
    cam_to_imei: Dict[str, str] = {}
    for r in all_conf:
        for cam in (r["cam_list"] or "").split("|"):
            cam = cam.strip()
            if cam:
                cam_to_imei[cam] = str(r["imei"])

    needed_imeis = {cam_to_imei[c] for c in cam_names if c in cam_to_imei}
    if not needed_imeis:
        logging.warning("Не найдены IMEI для камер с AutoCounting")
        return []

    imeis_list = list(needed_imeis)
    ph = ", ".join(["%s"] * len(imeis_list))
    tracker_rows = db.query(
        f"SELECT t.`id`,t.`imei`,t.`tracker_table`,rs.`priority` FROM `trackers` t "
        f"LEFT JOIN `AI_RevenueSettings` rs ON rs.`imei`=t.`imei` "
        f"WHERE t.`imei` IN ({ph})",
        imeis_list,
    )
    imei_to_tracker: Dict[str, dict] = {}
    imei_priority: Dict[str, int] = {}
    for r in tracker_rows:
        imei_str = str(r["imei"])
        imei_to_tracker[imei_str] = {"id": r["id"], "table": r["tracker_table"]}
        try:
            imei_priority[imei_str] = 1 if r["priority"] is not None and int(r["priority"]) else 0
        except (ValueError, TypeError):
            imei_priority[imei_str] = 0

    vehicles: List[VehicleInfo] = []
    for cam_name, io_cfg in auto_cameras.items():
        imei = cam_to_imei.get(cam_name)
        if not imei:
            logging.warning("Камера %s: IMEI не найден в trackers_conf", cam_name)
            continue
        tk = imei_to_tracker.get(imei)
        if not tk:
            logging.warning("Камера %s (IMEI %s): нет в trackers", cam_name, imei)
            continue
        region = regions.get(cam_name, [])
        if not region:
            logging.warning("Камера %s: нет region (type=0)", cam_name)
            continue
        alarm_cfg = alarm_cfgs.get(cam_name) or {}
        vehicles.append(VehicleInfo(
            camera_name=cam_name, imei=imei, tracker_id=tk["id"],
            tracker_table=tk["table"], region=region,
            model_size=model_sizes.get(cam_name, "Medium"),
            person_quality=person_quals.get(cam_name, "0.5"),
            in_out_config=io_cfg, priority=imei_priority.get(imei, 0),
            count_by_alarms=bool(alarm_cfg.get("enabled", False)),
            alarm_padding_left=float(alarm_cfg.get("padL", 10.0)),
            alarm_padding_right=float(alarm_cfg.get("padR", 10.0)),
            ignore_zones=cam_name in ignore_zones_cams,
        ))
    return vehicles


class AutoSource(TaskSource):
    name = SOURCE_AUTO

    def __init__(self, cfg, db, results_store, mailer, tz: timezone, zones_db):
        self.cfg = cfg
        self.db = db
        self.zones_db = zones_db
        self.results = results_store
        self.mailer = mailer
        self.tz = tz
        self.vehicles: List[VehicleInfo] = []
        self._last_discovery = 0.0

    def refresh_vehicles(self, now: float):
        if now - self._last_discovery < self.cfg.discovery_interval_sec:
            return
        try:
            self.vehicles = discover_vehicles(self.db)
            self.vehicles.sort(key=lambda v: v.priority, reverse=True)
            if self.vehicles:
                logging.info("🔍 AutoCounting камеры (prio→): %s",
                             [(v.camera_name, v.priority) for v in self.vehicles])
        except Exception:
            logging.exception("Ошибка discovery камер")
        self._last_discovery = now

    def discover(self, now_epoch: int, available_slots: int) -> List[Job]:
        import time as _time
        self.refresh_vehicles(_time.time())

        now_dt = datetime.now(self.tz)
        today_str = now_dt.strftime("%Y-%m-%d")
        allow_today = now_dt.hour >= self.cfg.today_processing_start_hour

        jobs: List[Job] = []
        for vehicle in self.vehicles:
            if len(jobs) >= available_slots:
                break
            job = self._try_build_job(vehicle, now_epoch, today_str, allow_today)
            if job:
                jobs.append(job)
        return jobs

    def _try_build_job(self, vehicle, now_epoch, today_str, allow_today):
        cam = vehicle.camera_name
        unfinished = self.results.get_unfinished_dates(
            cam, lookback_days=self.cfg.lookback_days)
        for date_str in unfinished:
            job = self._build_for_date(vehicle, date_str, now_epoch,
                                       today_str, allow_today)
            if job:
                return job
        return None

    def _build_for_date(self, vehicle, date_str, now_epoch, today_str, allow_today):
        cam = vehicle.camera_name
        date_compact = date_str.replace("-", "")
        is_past_day = date_str < today_str

        if not is_past_day and not allow_today:
            return None

        clog = get_camera_logger(
            self.cfg.camera_log_dir, cam, SOURCE_AUTO, date_str)

        day_date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=self.tz)
        day_start_epoch = int(day_date.replace(
            hour=self.cfg.day_start_hour, minute=0, second=0, microsecond=0).timestamp())
        day_end_epoch = int(day_date.replace(
            hour=self.cfg.day_end_hour, minute=0, second=0, microsecond=0).timestamp())

        accel_end = day_end_epoch if is_past_day else now_epoch
        try:
            accel = segments.get_accel_data(
                self.db, vehicle.tracker_table, vehicle.tracker_id,
                day_start_epoch, accel_end)
        except segments.AccelColumnMissing:
            clog.warning("InstantMovement не найден — окно 05:00–22:00")
            day_start_epoch = int(day_date.replace(
                hour=5, minute=0, second=0, microsecond=0).timestamp())
            day_end_epoch = int(day_date.replace(
                hour=22, minute=0, second=0, microsecond=0).timestamp())
            accel = []
            mvmt = {"first_move_ts": day_start_epoch, "last_move_ts": day_end_epoch,
                    "last_data_ts": day_end_epoch}
        except Exception:
            clog.exception("Ошибка акселерометра")
            return None
        else:
            mvmt = segments.analyze_movement(accel)
        if mvmt["first_move_ts"] is None:
            if self.cfg.min_daily_km > 0:
                try:
                    gps = segments.get_gps_data(
                        self.db, vehicle.tracker_table, vehicle.tracker_id,
                        day_start_epoch, accel_end)
                    gps_info = segments.analyze_gps_movement(gps)
                    daily_km = gps_info["total_km"]
                    if daily_km >= self.cfg.min_daily_km:
                        first_ts = gps_info["first_ts"] or day_start_epoch
                        last_ts = (gps_info["last_ts"] or day_end_epoch)
                        mvmt = {
                            "first_move_ts": first_ts,
                            "last_move_ts": last_ts,
                            "last_data_ts": last_ts,
                        }
                        clog.info(
                            "📍 GPS %.1f км (порог %.0f) — акселерометр без "
                            "движения, продолжаем",
                            daily_km, self.cfg.min_daily_km)
                    else:
                        clog.info(
                            "📍 GPS %.1f км < порога %.0f — пропуск",
                            daily_km, self.cfg.min_daily_km)
                except segments.GpsColumnMissing:
                    clog.warning("latitude/longtitude не найдены в таблице трекера")
                except Exception:
                    clog.exception("Ошибка GPS-пробега")
            if mvmt["first_move_ts"] is None:
                if is_past_day:
                    self.results.mark_finished(date_str, cam)
                    clog.info("🏁 нет движения, день завершён")
                    self._on_day_finished(cam, date_str)
                return None

        last_end = self.results.get_last_processed_end(date_str, cam)
        process_from = last_end if last_end else mvmt["first_move_ts"]

        try:
            remote_files = transfer.list_remote_videos(
                self.cfg, cam, date_compact, process_from, day_end_epoch)
        except Exception:
            clog.exception("Ошибка списка видео")
            return None

        if not remote_files:
            if is_past_day:
                self._finish_day(date_str, cam)
            return None

        from core.video import parse_video_timestamps
        video_infos = parse_video_timestamps(remote_files)
        if not video_infos:
            if is_past_day:
                self._finish_day(date_str, cam)
            return None

        unprocessed_dur, video_end = segments.calc_unprocessed_video(
            video_infos, process_from)
        if unprocessed_dur < self.cfg.chunk_duration_sec and not is_past_day:
            clog.info("📹 накоплено %d/%dс — ждём",
                      unprocessed_dur, self.cfg.chunk_duration_sec)
            return None

        accel_range = [r for r in accel
                       if process_from <= int(r["epochtime"]) <= video_end]
        stops = segments.find_stops(accel_range)

        effective_end = video_end
        if stops:
            last_stop = stops[-1]
            day_finished = now_epoch >= day_end_epoch
            should_cut = ((not is_past_day) or
                          (day_finished and (last_stop["ongoing"]
                                             or last_stop["end_ts"] >= video_end - 30)))
            if should_cut:
                effective_end = last_stop["start_ts"]
                stops = stops[:-1]

        if effective_end <= process_from + 60:
            if is_past_day:
                clog.info("⏩ после последней остановки обрабатывать нечего")
                self._finish_day(date_str, cam)
            return None

        if vehicle.count_by_alarms:
            try:
                segs = alarms.build_alarm_segments(
                    self.db, cam, process_from, effective_end,
                    vehicle.alarm_padding_left, vehicle.alarm_padding_right)
            except Exception:
                clog.exception("Ошибка LineCrossingEvents")
                return None
            clog.info("🚨 countByAlarms → %d сегм.", len(segs))
        else:
            segs = segments.build_processing_segments(
                process_from, effective_end, stops, self.cfg.long_stop_threshold_sec)

        all_in_zones = False
        if vehicle.ignore_zones and segs:
            try:
                segs = zones.apply_ignore_zones(
                    self.db, self.zones_db, vehicle.imei, vehicle.tracker_table,
                    vehicle.tracker_id, segs, clog)
            except Exception:
                clog.exception("Ошибка фильтра зон игнорирования (type8)")
                return None
            all_in_zones = not segs

        if not segs:
            if all_in_zones:
                reason = "всё время в зонах игнорирования"
            elif vehicle.count_by_alarms:
                reason = "нет сработок"
            else:
                reason = "пропуск длинных остановок"
            clog.info("⏩ %s", reason)
            self.results.save_chunk(date_str, cam, process_from, effective_end, 0, 0)
            if is_past_day:
                self._finish_day(date_str, cam)
            return None

        clog.info("📊 %d сегм. для обработки", len(segs))

        # Колбэк сохранения чанка в AI_ResultsOfAutoCounting.
        def _on_success(in_c, out_c, _save_path, _ds=date_str, _cam=cam,
                        _cs=process_from, _ce=effective_end, _past=is_past_day):
            self.results.save_chunk(_ds, _cam, _cs, _ce, in_c, out_c)

        # Авто-задачи сохраняют видео с визуализацией, если включён auto_save_video
        # и задан каталог save_video_dir.
        save_video = self.cfg.auto_save_video and bool(self.cfg.save_video_dir)

        return Job(
            source=SOURCE_AUTO, camera_name=cam, date_str=date_str,
            date_compact=date_compact, segments=segs,
            chunk_start=process_from, chunk_end=effective_end,
            region=vehicle.region, model_size=vehicle.model_size,
            person_quality=vehicle.person_quality, save_video=save_video,
            on_success=_on_success,
            priority=vehicle.priority,
            meta={"is_past_day": is_past_day},
        )

    def _finish_day(self, date_str, cam):
        self.results.mark_finished(date_str, cam)
        ti, to = self.results.get_totals(date_str, cam)
        clog = get_camera_logger(
            self.cfg.camera_log_dir, cam, SOURCE_AUTO, date_str)
        clog.info("🏁 ДЕНЬ ЗАВЕРШЁН in=%d out=%d", ti, to)
        self._on_day_finished(cam, date_str)

    def _on_day_finished(self, cam, date_str):
        """Отчёт по машине — когда день завершён у всех её камер из self.vehicles.

        Камеры с блокировкой (type=7), без AutoCounting или без региона в
        self.vehicles не попадают — их не ждём.
        """
        imei = next((v.imei for v in self.vehicles if v.camera_name == cam), None)
        if imei is None:
            return
        cams = [v.camera_name for v in self.vehicles if v.imei == imei]
        pending = [c for c in cams if not self.results.is_finished(date_str, c)]
        if pending:
            get_camera_logger(self.cfg.camera_log_dir, cam, SOURCE_AUTO, date_str).info(
                "✉️  отчёт по машине ждёт камеры: %s", ", ".join(pending))
            return
        self.mailer.schedule_auto(imei, cams, date_str, cam)
