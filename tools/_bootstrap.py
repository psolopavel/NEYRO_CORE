#!/usr/bin/env python3
"""Общий бутстрап для tools: путь к проекту, конфиг, БД.

Чтобы каждый tool не дублировал sys.path-магию и инициализацию.
"""

from __future__ import annotations

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

DEFAULT_CONFIG = os.path.join(PROJECT_ROOT, "config.ini")


def load_config(path: str | None = None):
    from core.config import Config
    return Config(path or os.environ.get("NEYRO_CONFIG", DEFAULT_CONFIG))


def load_db(cfg=None):
    from core.db import Database
    return Database(cfg or load_config())
