#!/usr/bin/env python3
"""Результаты подсчёта — две таблицы, формат которых читает revenue3.php.

ResultsStore — авто-подсчёт, AI_ResultsOfAutoCounting.data (по чанкам):
{
  "1771484808_1771490133": {"in": 27, "out": 20},
  "1771490140_1771532000": {"in": 11, "out": 11},
  "_meta": {"finished": false}
}

CountingResultsStore — запросы, AI_ResultsOfCounting.result (одно число на день):
{"in": 38, "out": 31}
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple


def _upsert_by_name_date(db, table: str, column: str,
                         camera: str, date_str: str, value: str):
    """UPDATE строки (name, date), иначе INSERT.

    Уникального индекса на (name, date) в таблицах нет — ON DUPLICATE KEY
    не сработает; от гонки защищает lock вызывающего store.
    """
    existing = db.query(
        f"SELECT 1 FROM `{table}` WHERE `name`=%s AND `date`=%s LIMIT 1",
        (camera, date_str),
    )
    if existing:
        db.execute(
            f"UPDATE `{table}` SET `{column}`=%s WHERE `name`=%s AND `date`=%s",
            (value, camera, date_str),
        )
    else:
        db.execute(
            f"INSERT INTO `{table}` (`name`,`date`,`{column}`) VALUES (%s,%s,%s)",
            (camera, date_str, value),
        )


class ResultsStore:
    def __init__(self, db, tz: timezone):
        self._db = db
        self._tz = tz
        self._lock = threading.Lock()

    # ── внутренние ──

    def _load_data(self, date_str: str, camera: str) -> dict:
        rows = self._db.query(
            "SELECT `data` FROM `AI_ResultsOfAutoCounting` "
            "WHERE `name`=%s AND `date`=%s LIMIT 1",
            (camera, date_str),
        )
        if rows and rows[0]["data"]:
            try:
                return json.loads(rows[0]["data"])
            except Exception:
                return {}
        return {}

    def _save_data(self, date_str: str, camera: str, data: dict):
        _upsert_by_name_date(self._db, "AI_ResultsOfAutoCounting", "data",
                             camera, date_str, json.dumps(data, ensure_ascii=False))

    # ── public ──

    def save_chunk(self, date_str: str, camera: str,
                   chunk_start: int, chunk_end: int,
                   in_count: int, out_count: int):
        with self._lock:
            data = self._load_data(date_str, camera)
            data[f"{chunk_start}_{chunk_end}"] = {"in": in_count, "out": out_count}
            self._save_data(date_str, camera, data)

    def mark_finished(self, date_str: str, camera: str):
        with self._lock:
            data = self._load_data(date_str, camera)
            meta = data.get("_meta", {})
            meta["finished"] = True
            meta["finished_at"] = datetime.now(self._tz).strftime("%Y-%m-%d %H:%M:%S")
            data["_meta"] = meta
            self._save_data(date_str, camera, data)

    def get_last_processed_end(self, date_str: str, camera: str) -> Optional[int]:
        data = self._load_data(date_str, camera)
        max_end = None
        for key in data:
            if key.startswith("_"):
                continue
            parts = key.split("_")
            if len(parts) == 2:
                try:
                    end_ts = int(parts[1])
                    if max_end is None or end_ts > max_end:
                        max_end = end_ts
                except ValueError:
                    pass
        return max_end

    def is_finished(self, date_str: str, camera: str) -> bool:
        return self._load_data(date_str, camera).get("_meta", {}).get("finished", False)

    def get_totals(self, date_str: str, camera: str) -> Tuple[int, int]:
        data = self._load_data(date_str, camera)
        total_in = total_out = 0
        for key, val in data.items():
            if key.startswith("_"):
                continue
            if isinstance(val, dict):
                total_in += val.get("in", 0)
                total_out += val.get("out", 0)
        return total_in, total_out

    def get_chunks(self, date_str: str, camera: str) -> List[Tuple[int, int, int, int]]:
        """[(start, end, in, out), ...] по возрастанию start."""
        chunks = []
        for key, val in self._load_data(date_str, camera).items():
            if key.startswith("_") or not isinstance(val, dict):
                continue
            parts = key.split("_")
            if len(parts) != 2:
                continue
            try:
                start, end = int(parts[0]), int(parts[1])
            except ValueError:
                continue
            chunks.append((start, end, int(val.get("in", 0)), int(val.get("out", 0))))
        return sorted(chunks)

    def get_unfinished_dates(self, camera: str, lookback_days: int = 0) -> List[str]:
        """Даты (YYYY-MM-DD) за lookback_days дней без finished=True (старые→новые).
        lookback_days=0 — только сегодня; 3 — сегодня и 3 предыдущих дня."""
        today = datetime.now(self._tz).date()
        unfinished: List[str] = []
        for i in range(lookback_days, -1, -1):
            ds = (today - timedelta(days=i)).strftime("%Y-%m-%d")
            if not self.is_finished(ds, camera):
                unfinished.append(ds)
        return unfinished


class CountingResultsStore:
    """Результат запросной задачи: перезаписывает день камеры целиком."""

    def __init__(self, db):
        self._db = db
        self._lock = threading.Lock()

    def save(self, date_str: str, camera: str, in_count: int, out_count: int):
        with self._lock:
            _upsert_by_name_date(self._db, "AI_ResultsOfCounting", "result",
                                 camera, date_str,
                                 json.dumps({"in": in_count, "out": out_count}))

    def get(self, date_str: str, camera: str) -> Optional[Tuple[int, int]]:
        """(in, out) или None. Старый формат — просто число: in = out = число."""
        rows = self._db.query(
            "SELECT `result` FROM `AI_ResultsOfCounting` "
            "WHERE `name`=%s AND `date`=%s LIMIT 1",
            (camera, date_str),
        )
        if not rows or rows[0]["result"] in (None, ""):
            return None
        try:
            decoded = json.loads(rows[0]["result"])
        except (TypeError, ValueError):
            return None
        if isinstance(decoded, dict) and "in" in decoded and "out" in decoded:
            return int(decoded["in"]), int(decoded["out"])
        if isinstance(decoded, (int, float)):
            return int(decoded), int(decoded)
        return None
