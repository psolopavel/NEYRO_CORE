#!/usr/bin/env python3
"""enqueue_request.py — приём запросов в очередь AI_CountingQueue.

Заменяет saveNewReq.py (req.json → таблица БД).

Вход (stdin, JSON), контракт от SendingAiRequests.php:
  [{cam: [startTs, endTs, cameraSquares, showProcess]}, ...]
  cameraSquares = {main?, stops?, type1, type2, type5, type6}
Может прийти и один объект {cam: [...]} (как в старом saveNewReq).

Выход (stdout, JSON): {"status":"saved", "ids":[...]}
"""

from __future__ import annotations

import json
import sys

import _bootstrap  # noqa: F401  (настраивает sys.path)
from stores.queue_store import QueueStore


def _to_int_ts(val):
    if isinstance(val, bool) or val is None:
        return None
    if isinstance(val, (int, float)):
        return int(val)
    s = str(val).strip()
    if s.isdigit() or (len(s) > 1 and s[0] == "-" and s[1:].isdigit()):
        return int(s)
    return None


def _save_record(record: dict, store: QueueStore) -> list:
    """record = {cam: [startTs, endTs, cameraSquares, showProcess]}."""
    ids = []
    for cam, values in record.items():
        if not isinstance(values, list) or len(values) < 1:
            continue
        date_from = _to_int_ts(values[0])
        if date_from is None:
            continue
        date_to = _to_int_ts(values[1]) if len(values) > 1 else None

        squares = values[2] if len(values) > 2 and isinstance(values[2], dict) else {}
        save_video = False
        if len(values) > 3:
            save_video = str(values[3]).strip().lower() == "true"

        params = dict(squares)
        params["save_video"] = save_video

        ids.append(store.enqueue(cam, date_from, date_to, params))
    return ids


def main():
    try:
        data = json.load(sys.stdin)
    except Exception as e:
        print(json.dumps({"status": "error", "error": f"bad json: {e}"}))
        sys.exit(1)

    store = QueueStore(_bootstrap.load_db())

    records = []
    if isinstance(data, list):
        for item in data:
            # PHP может прислать [{cam:...}] вложенным
            if isinstance(item, list) and len(item) == 1 and isinstance(item[0], dict):
                records.append(item[0])
            elif isinstance(item, dict):
                records.append(item)
    elif isinstance(data, dict):
        records.append(data)

    saved_ids = []
    for rec in records:
        try:
            saved_ids.extend(_save_record(rec, store))
        except Exception as e:
            print(json.dumps({"status": "error", "error": str(e)}))
            sys.exit(1)

    print(json.dumps({"status": "saved", "ids": saved_ids}))


if __name__ == "__main__":
    main()
