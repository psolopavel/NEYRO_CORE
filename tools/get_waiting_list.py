#!/usr/bin/env python3
"""get_waiting_list.py — список задач очереди в работе/ожидании для revenue3.php.

Вход (stdin, JSON): {"camers": ["CAM1", ...], "days": ["YYYY-MM-DD", ...]}
Выход (stdout, JSON): { "CAM_YYYY-MM-DD": <code>, ... }

code — числовой статус, который понимает фронт (admin.js _statusDotAttrs):
    1 → "Обробляється" (processing) — зелёная точка
    2 → "В черзі"      (pending)    — оранжевая точка
(done/error не возвращаем — задача больше не «ожидает»).

Ключ "CAM_DATE" совпадает с тем, что ждёт revenue3.php (waitList).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone

import _bootstrap  # noqa: F401
from stores.queue_store import QueueStore

# Коды статусов для фронта (admin.js _statusDotAttrs).
_STATUS_CODE = {
    "processing": 1,   # зелёная точка «Обробляється»
    "pending":    2,   # оранжевая точка «В черзі»
}


def main():
    raw = sys.stdin.read()
    try:
        data = json.loads(raw) if raw else {}
    except Exception:
        print(json.dumps({}))
        return

    cams = set(data.get("camers") or [])
    days = set(data.get("days") or [])

    cfg = _bootstrap.load_config()
    tz = timezone(timedelta(hours=cfg.timezone_offset_hours))
    store = QueueStore(_bootstrap.load_db(cfg))

    result = {}
    for job in store.list_all():
        if job.status not in _STATUS_CODE:
            continue
        if cams and job.cam_name not in cams:
            continue
        ts = job.date_from
        date_str = datetime.fromtimestamp(ts, tz=tz).strftime("%Y-%m-%d") \
            if isinstance(ts, (int, float)) else str(ts)[:10]
        if days and date_str not in days:
            continue
        result[f"{job.cam_name}_{date_str}"] = _STATUS_CODE[job.status]

    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
