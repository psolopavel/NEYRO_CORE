#!/usr/bin/env python3
"""Единый конфиг проекта.

Читает config.ini (INI-секции) и поддерживает плоский формат key=value для
блока [database] (совместимость со старым DB_config.conf-стилем).

Все пути, креды БД, SSH-хранилище и параметры обработки собраны здесь — больше
ничего не хардкодим по файлам.
"""

from __future__ import annotations

import configparser
import os


class Config:
    def __init__(self, path: str):
        if not os.path.exists(path):
            raise FileNotFoundError(f"Config not found: {path}")

        self.path = path
        flat = self._parse_flat(path)
        ini = configparser.ConfigParser()
        ini.read(path, encoding="utf-8")

        # ── database ──
        self.db_user = flat.get("DB_USER") or ini.get("database", "DB_USER", fallback="")
        self.db_pass = flat.get("DB_PASS") or ini.get("database", "DB_PASS", fallback="")
        self.db_name = flat.get("DB_NAME") or ini.get("database", "DB_NAME", fallback="gps")
        self.db_host = flat.get("DB_HOST") or ini.get("database", "DB_HOST", fallback="127.0.0.1")
        self.db_port = int(flat.get("DB_PORT") or ini.get("database", "DB_PORT", fallback="3306"))
        # Зоны игнорирования подсчёта (all_car_routes / zones), тот же хост и учётка.
        self.zones_db_name = ini.get("database", "ZONES_DB_NAME",
                                     fallback="RoutesAndAdvertisement")

        # ── storage (SSH/rsync) ──
        self.storage_user = ini.get("storage", "storage_user", fallback="notlets")
        self.storage_host = ini.get("storage", "storage_host", fallback="116.202.80.5")
        self.storage_base = ini.get("storage", "storage_base",
                                    fallback="/home/sdkVideoLoader/data/Recordings")

        # ── paths ──
        self.python_path   = ini.get("paths", "python_path",   fallback="/ultralytics-env/bin/python")
        self.download_base = ini.get("paths", "download_base", fallback="/mnt/ssdVideos")
        self.models_dir    = ini.get("paths", "models_dir",    fallback="/home/NEYRO_CORE/models")
        self.tmp_dir       = ini.get("paths", "tmp_dir",       fallback="/tmp/NEYRO_CORE")
        self.log_dir       = ini.get("paths", "log_dir",       fallback="/tmp/NEYRO_LOGS")
        # Per-camera микро-логи: /tmp/NEYRO_LOGS/cameras/<camera>.log
        self.camera_log_dir = ini.get("paths", "camera_log_dir",
                                      fallback=f"{self.log_dir.rstrip('/')}/cameras")
        self.save_video_dir = ini.get("paths", "save_video_dir",
                                      fallback="/home/NEYRO_CORE/videos")

        # ── processing ──
        self.max_workers             = ini.getint("processing", "max_workers",             fallback=8)
        self.chunk_duration_sec      = ini.getint("processing", "chunk_duration_sec",      fallback=7200)
        self.check_interval_sec      = ini.getint("processing", "check_interval_sec",      fallback=60)
        self.long_stop_threshold_sec = ini.getint("processing", "long_stop_threshold_sec", fallback=5400)
        self.day_start_hour          = ini.getint("processing", "day_start_hour",          fallback=4)
        self.day_end_hour            = ini.getint("processing", "day_end_hour",            fallback=23)
        self.discovery_interval_sec  = ini.getint("processing", "discovery_interval_sec",  fallback=300)
        self.lookback_days           = ini.getint("processing", "lookback_days",           fallback=0)
        self.today_processing_start_hour = ini.getint("processing", "today_processing_start_hour",
                                                      fallback=20)
        self.timezone_offset_hours   = ini.getint("processing", "timezone_offset_hours",   fallback=3)
        # Мин. GPS-пробег за день (км) для автоподсчёта без срабатывания акселерометра (0 = выкл.).
        self.min_daily_km            = ini.getfloat("processing", "min_daily_km",            fallback=50.0)
        # Сохранять видео с визуализацией для автоматического подсчёта (нужен save_video_dir).
        self.auto_save_video         = ini.getboolean("processing", "auto_save_video",       fallback=True)

        # ── mail (письма с результатами через SendLayer, очередь AI_ResultDeliveries) ──
        self.mail_enabled        = ini.getboolean("mail", "enabled", fallback=False)
        self.mail_api_key        = ini.get("mail", "sendlayer_api_key", fallback="")
        self.mail_api_url        = ini.get("mail", "sendlayer_api_url",
                                           fallback="https://console.sendlayer.com/api/v1/email")
        self.mail_timeout_sec    = ini.getint("mail", "timeout_sec", fallback=30)
        self.mail_from_email     = ini.get("mail", "from_email",
                                           fallback="info@mail.vcpserver.net.ua")
        self.mail_from_name      = ini.get("mail", "from_name", fallback="Підрахунок пасажирів")
        self.mail_reply_to       = ini.get("mail", "reply_to", fallback="")
        self.mail_bcc            = ini.get("mail", "bcc", fallback="")
        self.mail_send_delay_min = ini.getint("mail", "send_delay_min", fallback=10)
        self.mail_max_attempts   = ini.getint("mail", "max_attempts", fallback=6)
        self.mail_interval_sec   = ini.getint("mail", "interval_sec", fallback=30)

    def model_path(self, model_size: str) -> str:
        """Полный путь к .engine по размеру модели."""
        key = str(model_size or "Medium").strip().lower()
        mapping = {
            "nano":        "yolov8n.engine",
            "small":       "yolov8s.engine",
            "medium":      "yolov8m.engine",
            "large":       "yolov8l.engine",
            "extra large": "yolov8x.engine",
            "xl":          "yolov8x.engine",
        }
        fname = mapping.get(key, "yolov8m.engine")
        # Пути всегда POSIX (целевая система — Linux), не зависят от ОС разработки.
        return f"{self.models_dir.rstrip('/')}/{fname}"

    @staticmethod
    def _parse_flat(path: str) -> dict:
        """Парсит DB_* из плоских key=value строк (без секций)."""
        data: dict = {}
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith((";", "#", "[")):
                    continue
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key.startswith("DB_"):
                    data[key] = val
        return data
