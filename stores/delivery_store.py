#!/usr/bin/env python3
"""DeliveryStore — очередь писем с результатами (таблица AI_ResultDeliveries).

Ставит mailer.ResultMailer.schedule_*, отправляет ResultMailer.process_due.
Подробности статусов и dedup_key — в sql/AI_ResultDeliveries.sql.
"""

from __future__ import annotations

import hashlib
import json
from typing import List, Optional

_MAX_ERROR_LEN = 2000


def _json_or_none(values) -> Optional[str]:
    return json.dumps(values, ensure_ascii=False) if values else None


class DeliveryStore:
    def __init__(self, db):
        self._db = db

    @staticmethod
    def dedup_key(raw: str) -> str:
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    # ── постановка ──

    def enqueue(self, *, dedup_raw: str, source: str, imei: str, count_date: str,
                owner: str, queue_id: Optional[int], cameras: list, delay_sec: int,
                status: str = "pending", last_error: Optional[str] = None) -> bool:
        """INSERT, если такого письма ещё нет. -> True, если строка добавлена.

        Добавленная pending-строка отменяет более старые pending по той же
        машине/дню/владельцу — клиент получит только свежий результат.
        """
        key = self.dedup_key(dedup_raw)
        inserted = self._db.execute(
            "INSERT IGNORE INTO `AI_ResultDeliveries` "
            "(`dedup_key`,`source`,`imei`,`count_date`,`owner`,`queue_id`,`cameras`,"
            "`status`,`next_attempt_at`,`last_error`) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,UNIX_TIMESTAMP()+%s,%s)",
            (key, source, imei, count_date, owner, queue_id,
             json.dumps(cameras, ensure_ascii=False), status, delay_sec, last_error),
        ) == 1
        if inserted and status == "pending":
            self._db.execute(
                "UPDATE `AI_ResultDeliveries` SET `status`='cancelled', "
                "`last_error`='Замінено новішим результатом' "
                "WHERE `imei`=%s AND `count_date`=%s AND `owner`=%s "
                "AND `status`='pending' AND `dedup_key`<>%s",
                (imei, count_date, owner, key),
            )
        return inserted

    # ── чтение ──

    def list_due(self, limit: int = 20) -> List[int]:
        rows = self._db.query(
            "SELECT `id` FROM `AI_ResultDeliveries` "
            "WHERE `status`='pending' AND `next_attempt_at`<=UNIX_TIMESTAMP() "
            "ORDER BY `next_attempt_at`, `id` LIMIT %s",
            (limit,),
        )
        return [r["id"] for r in rows]

    def get(self, delivery_id: int) -> Optional[dict]:
        rows = self._db.query(
            "SELECT * FROM `AI_ResultDeliveries` WHERE `id`=%s LIMIT 1", (delivery_id,))
        return rows[0] if rows else None

    def last_sent(self, imei: str, count_date: str, owner: str) -> Optional[dict]:
        rows = self._db.query(
            "SELECT `version`,`sent_at` FROM `AI_ResultDeliveries` "
            "WHERE `imei`=%s AND `count_date`=%s AND `owner`=%s AND `status`='sent' "
            "ORDER BY `version` DESC, `id` DESC LIMIT 1",
            (imei, count_date, owner),
        )
        return rows[0] if rows else None

    # ── отправка ──

    def claim(self, delivery_id: int) -> bool:
        return self._db.execute(
            "UPDATE `AI_ResultDeliveries` SET `status`='sending' "
            "WHERE `id`=%s AND `status`='pending'",
            (delivery_id,),
        ) == 1

    def mark_sent(self, delivery_id: int, *, version: int, message_id: str,
                  recipients: List[str], skipped: List[str]):
        self._db.execute(
            "UPDATE `AI_ResultDeliveries` SET `status`='sent', `version`=%s, "
            "`provider_message_id`=%s, `recipient_emails`=%s, `skipped_emails`=%s, "
            "`sent_at`=UNIX_TIMESTAMP(), `attempts`=`attempts`+1, `last_error`=NULL "
            "WHERE `id`=%s",
            (version, message_id[:128] or None, _json_or_none(recipients),
             _json_or_none(skipped), delivery_id),
        )

    def finish(self, delivery_id: int, status: str, reason: str, *,
               recipients: Optional[List[str]] = None,
               skipped: Optional[List[str]] = None):
        """Финальный статус без отправки (no_recipient / cancelled / failed)."""
        self._db.execute(
            "UPDATE `AI_ResultDeliveries` SET `status`=%s, `last_error`=%s, "
            "`recipient_emails`=%s, `skipped_emails`=%s WHERE `id`=%s",
            (status, reason[:_MAX_ERROR_LEN], _json_or_none(recipients),
             _json_or_none(skipped), delivery_id),
        )

    def schedule_retry(self, delivery_id: int, error: str, delay_sec: Optional[int]):
        """Неудачная попытка: delay_sec=None — больше не пробуем (failed)."""
        if delay_sec is None:
            self._db.execute(
                "UPDATE `AI_ResultDeliveries` SET `status`='failed', "
                "`attempts`=`attempts`+1, `last_error`=%s WHERE `id`=%s",
                (error[:_MAX_ERROR_LEN], delivery_id),
            )
            return
        self._db.execute(
            "UPDATE `AI_ResultDeliveries` SET `status`='pending', "
            "`attempts`=`attempts`+1, `last_error`=%s, "
            "`next_attempt_at`=UNIX_TIMESTAMP()+%s WHERE `id`=%s",
            (error[:_MAX_ERROR_LEN], delay_sec, delivery_id),
        )

    def recover_stale_sending(self) -> int:
        """sending -> pending после аварийной остановки (вызывать при старте)."""
        return self._db.execute(
            "UPDATE `AI_ResultDeliveries` SET `status`='pending', "
            "`next_attempt_at`=UNIX_TIMESTAMP() WHERE `status`='sending'"
        )
