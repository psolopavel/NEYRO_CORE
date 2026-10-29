#!/usr/bin/env python3
"""QueueStore — работа с таблицей AI_CountingQueue (заменяет req.json).

Очередь запросных задач. Главное отличие от файла req.json:
  - атомарный захват задачи через UPDATE ... WHERE status='pending'
    (нет гонок, не нужны файловые блокировки);
  - статусы pending/processing/done/error → виден прогресс и ошибки;
  - ничего не теряется при падении (можно восстановить «зависшие» processing).

params (JSON) хранит то, что раньше лежало в req.json для камеры:
  {main?, stops?, type1, type2, type5, type6, save_video}
"""

from __future__ import annotations

import json
from typing import List, Optional, Tuple

# Задачи в processing с протухшим heartbeat считаются брошенными.
STALE_PROCESSING_SEC = 2 * 60 * 60  # 2 часа


class QueueJob:
    def __init__(self, row: dict):
        self.id        = row["id"]
        self.cam_name  = row["cam_name"]
        self.date_from = row["date_from"]
        self.date_to   = row["date_to"]
        self.status    = row["status"]
        self.queue_priority = int(row.get("queue_priority") or 0)
        self.error_text = row.get("error_text")
        self.created_at = row.get("created_at")
        self.updated_at = row.get("updated_at")
        params = row.get("params")
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except Exception:
                params = {}
        self.params = params or {}

    def __repr__(self):
        return f"QueueJob(id={self.id}, cam={self.cam_name}, status={self.status})"


class QueueStore:
    def __init__(self, db):
        self._db = db

    # ── запись (enqueue) ──

    def enqueue(self, cam_name: str, date_from: int,
                date_to: Optional[int], params: dict) -> int:
        """Добавить задачу. Если такая (cam+date_from) уже pending — обновить params.

        Возвращает id строки.
        """
        existing = self._db.query(
            "SELECT `id` FROM `AI_CountingQueue` "
            "WHERE `cam_name`=%s AND `date_from`=%s AND `status`='pending' LIMIT 1",
            (cam_name, date_from),
        )
        params_json = json.dumps(params, ensure_ascii=False)
        if existing:
            row_id = existing[0]["id"]
            self._db.execute(
                "UPDATE `AI_CountingQueue` SET `date_to`=%s, `params`=%s WHERE `id`=%s",
                (date_to, params_json, row_id),
            )
            return row_id
        self._db.execute(
            "INSERT INTO `AI_CountingQueue` (`cam_name`,`date_from`,`date_to`,`params`) "
            "VALUES (%s,%s,%s,%s)",
            (cam_name, date_from, date_to, params_json),
        )
        rows = self._db.query("SELECT LAST_INSERT_ID() AS id")
        return rows[0]["id"] if rows else 0

    # ── чтение ──

    def list_pending(self) -> List[QueueJob]:
        rows = self._db.query(
            "SELECT * FROM `AI_CountingQueue` WHERE `status`='pending' "
            "ORDER BY `queue_priority` DESC, `id` ASC"
        )
        return [QueueJob(r) for r in rows]

    def has_pending(self) -> bool:
        rows = self._db.query(
            "SELECT 1 FROM `AI_CountingQueue` WHERE `status`='pending' LIMIT 1"
        )
        return bool(rows)

    def has_active_for(self, cams: List[str], ts_from: int, ts_to: int) -> bool:
        """Есть ли pending/processing по камерам с date_from в [ts_from, ts_to)."""
        if not cams:
            return False
        ph = ", ".join(["%s"] * len(cams))
        rows = self._db.query(
            f"SELECT 1 FROM `AI_CountingQueue` WHERE `cam_name` IN ({ph}) "
            "AND `status` IN ('pending','processing') "
            "AND `date_from`>=%s AND `date_from`<%s LIMIT 1",
            (*cams, ts_from, ts_to),
        )
        return bool(rows)

    def last_done_window(self, cam: str, ts_from: int,
                         ts_to: int) -> Optional[Tuple[int, Optional[int]]]:
        """(date_from, date_to) последней done-задачи камеры с date_from в [ts_from, ts_to)."""
        rows = self._db.query(
            "SELECT `date_from`,`date_to` FROM `AI_CountingQueue` "
            "WHERE `cam_name`=%s AND `status`='done' "
            "AND `date_from`>=%s AND `date_from`<%s ORDER BY `id` DESC LIMIT 1",
            (cam, ts_from, ts_to),
        )
        if not rows:
            return None
        return int(rows[0]["date_from"]), rows[0]["date_to"]

    def list_all(self) -> List[QueueJob]:
        rows = self._db.query(
            "SELECT * FROM `AI_CountingQueue` "
            "ORDER BY "
            "CASE `status` WHEN 'pending' THEN 0 WHEN 'processing' THEN 1 "
            "WHEN 'done' THEN 2 WHEN 'error' THEN 3 ELSE 9 END, "
            "CASE WHEN `status`='pending' THEN `queue_priority` END DESC, "
            "`id` ASC"
        )
        return [QueueJob(r) for r in rows]

    def reorder_pending(self, ordered_ids: List[int]) -> dict:
        """Переставить pending-задачи. ordered_ids[0] — первая в очереди."""
        if not ordered_ids:
            return {"success": False, "error": "ordered_ids порожній", "updated": 0}

        try:
            ordered_ids = [int(x) for x in ordered_ids]
        except (TypeError, ValueError):
            return {"success": False, "error": "ordered_ids містить некоректні id",
                    "updated": 0}

        if len(ordered_ids) != len(set(ordered_ids)):
            return {"success": False, "error": "ordered_ids містить дублікати",
                    "updated": 0}

        pending_ids = {j.id for j in self.list_pending()}
        ordered_set = set(ordered_ids)
        if ordered_set != pending_ids:
            missing = sorted(pending_ids - ordered_set)
            extra = sorted(ordered_set - pending_ids)
            parts = []
            if missing:
                parts.append(f"пропущено pending id: {missing}")
            if extra:
                parts.append(f"невідомі або не pending id: {extra}")
            return {"success": False, "error": "; ".join(parts), "updated": 0}

        n = len(ordered_ids)
        updated = 0
        for i, job_id in enumerate(ordered_ids):
            priority = n - i
            updated += self._db.execute(
                "UPDATE `AI_CountingQueue` SET `queue_priority`=%s "
                "WHERE `id`=%s AND `status`='pending'",
                (priority, job_id),
            )
        return {"success": True, "updated": updated}

    # ── захват/завершение ──

    def claim(self, job_id: int, worker_id: str) -> bool:
        """Атомарно захватить задачу. True, если успешно (status был pending)."""
        affected = self._db.execute(
            "UPDATE `AI_CountingQueue` SET `status`='processing', "
            "`worker_id`=%s, `heartbeat`=UNIX_TIMESTAMP() "
            "WHERE `id`=%s AND `status`='pending'",
            (worker_id, job_id),
        )
        return affected == 1

    def heartbeat(self, job_id: int):
        self._db.execute(
            "UPDATE `AI_CountingQueue` SET `heartbeat`=UNIX_TIMESTAMP() WHERE `id`=%s",
            (job_id,),
        )

    def mark_done(self, job_id: int):
        self._db.execute(
            "UPDATE `AI_CountingQueue` SET `status`='done', `error_text`=NULL "
            "WHERE `id`=%s",
            (job_id,),
        )

    def mark_error(self, job_id: int, error_text: str):
        self._db.execute(
            "UPDATE `AI_CountingQueue` SET `status`='error', `error_text`=%s "
            "WHERE `id`=%s",
            (error_text[:2000], job_id),
        )

    def get(self, job_id: int) -> Optional[QueueJob]:
        rows = self._db.query(
            "SELECT * FROM `AI_CountingQueue` WHERE `id`=%s LIMIT 1", (job_id,)
        )
        return QueueJob(rows[0]) if rows else None

    def remove(self, job_id: int) -> int:
        """Удалить задачу по id. Возвращает число удалённых строк.

        Задачу в статусе processing удалять нельзя (она в работе у воркера).
        """
        return self._db.execute(
            "DELETE FROM `AI_CountingQueue` WHERE `id`=%s AND `status`<>'processing'",
            (job_id,),
        )

    def reclaim_stale(self, stale_sec: int = STALE_PROCESSING_SEC) -> int:
        """Вернуть в pending задачи, зависшие в processing (протух heartbeat).

        Вызывается при старте демона. Возвращает число восстановленных.
        """
        return self._db.execute(
            "UPDATE `AI_CountingQueue` SET `status`='pending', `worker_id`=NULL "
            "WHERE `status`='processing' "
            "AND (`heartbeat` IS NULL OR `heartbeat` < UNIX_TIMESTAMP() - %s)",
            (stale_sec,),
        )
