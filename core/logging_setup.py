#!/usr/bin/env python3
"""Единая настройка логирования (1 копия вместо 4 разрозненных).

- Консоль: цвета + иконки.
- Файл: plain, с ротацией.
- Префикс [AUTO|QUEUE camera/date] для задач через get_camera_logger().

Все логи кладутся в /tmp (log_dir из конфига):
    /tmp/NEYRO_LOGS/manager.log         — обзор работы менеджера (discovery, submit)
    /tmp/NEYRO_LOGS/cameras/<camera>.log — детальный микро-лог по каждой камере
"""

from __future__ import annotations

import logging
import os
import re
from logging.handlers import RotatingFileHandler

_LOG_MAX_BYTES = 3 * 1024 * 1024
_LOG_BACKUPS = 2
_FILE_FMT = "%(asctime)s [%(levelname)s] %(message)s"
_DATE_FMT = "%Y-%m-%d %H:%M:%S"


class ColorFormatter(logging.Formatter):
    COLORS = {
        logging.DEBUG:    "\x1b[38;21m",
        logging.INFO:     "\x1b[32;21m",
        logging.WARNING:  "\x1b[33;21m",
        logging.ERROR:    "\x1b[31;21m",
        logging.CRITICAL: "\x1b[31;1m",
    }
    RESET = "\x1b[0m"
    ICONS = {
        logging.DEBUG:    "🐞",
        logging.INFO:     "ℹ️ ",
        logging.WARNING:  "⚠️ ",
        logging.ERROR:    "❌",
        logging.CRITICAL: "💀",
    }

    def format(self, record):
        color = self.COLORS.get(record.levelno, "")
        icon = self.ICONS.get(record.levelno, "")
        fmt = f"{color}{icon} %(asctime)s | %(message)s{self.RESET}"
        return logging.Formatter(fmt, _DATE_FMT).format(record)


def safe_filename(name: str) -> str:
    return re.sub(r"[^\w.-]", "_", name or "unknown")


def _file_handler(path: str) -> RotatingFileHandler:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fh = RotatingFileHandler(path, maxBytes=_LOG_MAX_BYTES,
                             backupCount=_LOG_BACKUPS, encoding="utf-8")
    fh.setFormatter(logging.Formatter(_FILE_FMT, _DATE_FMT))
    return fh


def setup_root_logger(log_file: str, level=logging.INFO):
    """Настроить корневой логгер: файл (plain) + консоль (цвет)."""
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)

    root.addHandler(_file_handler(log_file))

    ch = logging.StreamHandler()
    ch.setFormatter(ColorFormatter())
    root.addHandler(ch)


def get_named_logger(name: str, log_file: str, level=logging.INFO) -> logging.Logger:
    """Отдельный логгер со своим файлом (например, per-camera worker-лог).

    propagate=False, чтобы не дублировать в корневой.
    """
    logger = logging.getLogger(name)
    logger.handlers.clear()
    logger.setLevel(level)
    logger.propagate = False
    logger.addHandler(_file_handler(log_file))
    ch = logging.StreamHandler()
    ch.setFormatter(ColorFormatter())
    logger.addHandler(ch)
    return logger


class JobLoggerAdapter(logging.LoggerAdapter):
    """Добавляет префикс [SOURCE camera/date] к каждой строке.

    SOURCE — AUTO (автоподсчёт) или QUEUE (по запросу), чтобы в логе сразу
    было видно происхождение задачи.
    """

    def process(self, msg, kwargs):
        prefix = self.extra.get("prefix", "")
        if prefix:
            return f"{prefix} {msg}", kwargs
        return msg, kwargs


_SOURCE_TAG = {"auto": "AUTO", "queue": "QUEUE"}

# Кэш per-camera логгеров (по имени файла), чтобы не плодить хендлеры.
_camera_loggers: dict = {}


def get_camera_logger(camera_logs_dir: str, camera: str, source: str,
                      date_str: str = "", to_console: bool = True
                      ) -> JobLoggerAdapter:
    """Логгер задачи: пишет в отдельный файл per-camera + (опц.) консоль.

    Файл: <camera_logs_dir>/<camera>.log
    Префикс строк: [AUTO camera/date] или [QUEUE camera/date].
    """
    fname = safe_filename(camera)
    log_file = os.path.join(camera_logs_dir, f"{fname}.log")

    logger = _camera_loggers.get(log_file)
    if logger is None:
        logger = logging.getLogger(f"camera.{fname}")
        logger.handlers.clear()
        logger.setLevel(logging.INFO)
        logger.propagate = False
        logger.addHandler(_file_handler(log_file))
        if to_console:
            ch = logging.StreamHandler()
            ch.setFormatter(ColorFormatter())
            logger.addHandler(ch)
        _camera_loggers[log_file] = logger

    tag = _SOURCE_TAG.get(source, source.upper())
    label = camera if not date_str else f"{camera}/{date_str}"
    prefix = f"[{tag} {label}]"
    return JobLoggerAdapter(logger, {"prefix": prefix})
