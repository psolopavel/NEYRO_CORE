#!/usr/bin/env python3
"""Тонкая обёртка над mysql.connector: connect/query/execute.

Каждый вызов открывает и закрывает соединение — просто и потокобезопасно
(connector сам не шарится между потоками).
"""

from __future__ import annotations

from typing import List, Optional


class Database:
    def __init__(self, cfg, database: Optional[str] = None):
        """database — имя другой БД на том же сервере (по умолчанию cfg.db_name)."""
        self._cfg = cfg
        self._database = database or cfg.db_name

    def _connect(self):
        import mysql.connector
        return mysql.connector.connect(
            host=self._cfg.db_host,
            port=self._cfg.db_port,
            user=self._cfg.db_user,
            password=self._cfg.db_pass,
            database=self._database,
            charset="utf8mb4",
            autocommit=True,
            connection_timeout=15,
        )

    def query(self, sql: str, params=None) -> List[dict]:
        conn = self._connect()
        try:
            cur = conn.cursor(dictionary=True)
            cur.execute(sql, params or ())
            rows = cur.fetchall()
            cur.close()
            return rows
        finally:
            conn.close()

    def execute(self, sql: str, params=None) -> int:
        """INSERT/UPDATE/DELETE. Возвращает rowcount (для атомарного claim)."""
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(sql, params or ())
            affected = cur.rowcount
            cur.close()
            return affected
        finally:
            conn.close()
