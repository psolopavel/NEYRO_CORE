#!/usr/bin/env python3
"""remove_request.py — удаление запроса. Заменяет RemoveRequest.py.

Вход (stdin, JSON):
  {"type":"queue",  "request_key": <id>}                  → удалить из AI_CountingQueue
  {"type":"grafic", "cam_name": "...", "date": <ts|str>}  → удалить из AI_StopsReqs

Выход (stdout, JSON): {"success": bool, "removed": int, "error"?: str}
"""

from __future__ import annotations

import json
import sys

import _bootstrap  # noqa: F401
from stores.queue_store import QueueStore


def _normalize_unix_ts(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        s = value.strip()
        if s.isdigit() or (len(s) > 1 and s[0] == "-" and s[1:].isdigit()):
            return int(s)
    return None


def remove_from_queue(request_key) -> dict:
    try:
        job_id = int(request_key)
    except (ValueError, TypeError):
        return {"success": False, "error": f"Некорректный id {request_key!r}", "removed": 0}
    store = QueueStore(_bootstrap.load_db())
    job = store.get(job_id)
    if job is None:
        return {"success": False, "error": f"Запит {job_id} не знайдено", "removed": 0}
    if job.status == "processing":
        return {"success": False,
                "error": "Неможливо видалити запит, який зараз обробляється",
                "removed": 0}
    removed = store.remove(job_id)
    if removed == 0:
        return {"success": False,
                "error": f"Запит {job_id} не вдалося видалити", "removed": 0}
    return {"success": True, "removed": removed}


def remove_from_grafic(cam_name: str, date) -> dict:
    db = _bootstrap.load_db()
    base_cam = cam_name.split(" | ")[0] if " | " in cam_name else cam_name
    date_param = _normalize_unix_ts(date)
    if date_param is None:
        date_param = date
    try:
        removed = db.execute(
            "DELETE FROM `AI_StopsReqs` WHERE `cam_name`=%s AND `date`=%s",
            (base_cam, date_param),
        )
        return {"success": True, "removed": removed}
    except Exception as e:
        return {"success": False, "error": str(e)}


def main():
    raw = sys.stdin.read()
    try:
        data = json.loads(raw) if raw else {}
    except Exception:
        # fallback на CLI-аргументы (для тестов): type request_key
        if len(sys.argv) >= 3:
            data = {"type": sys.argv[1], "request_key": sys.argv[2]}
        else:
            print(json.dumps({"success": False, "error": "Invalid JSON input"}))
            sys.exit(1)

    type_req = data.get("type")
    request_key = data.get("request_key", data.get("index"))

    if not type_req:
        print(json.dumps({"success": False, "error": "Missing parameter: type"}))
        sys.exit(1)

    if type_req == "queue":
        if request_key is None or (isinstance(request_key, str) and not request_key.strip()):
            print(json.dumps({"success": False, "error": "Missing request_key"}))
            sys.exit(1)
        result = remove_from_queue(request_key)
    elif type_req == "grafic":
        cam_name, date = data.get("cam_name"), data.get("date")
        if not cam_name or not date:
            print(json.dumps({"success": False, "error": "Missing cam_name/date"}))
            sys.exit(1)
        result = remove_from_grafic(cam_name, date)
    else:
        result = {"success": False, "error": f"Unknown type: {type_req}"}

    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
