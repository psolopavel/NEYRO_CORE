#!/usr/bin/env python3
"""show_status.py — статус очереди + автоподсчёта + лог менеджера.
Заменяет ShowingNeyroWork.py.

Источник очереди теперь AI_CountingQueue (а не req.json), логи — единые в /tmp.

Вход (stdin, JSON, необязательно): {"auto_days": 7} — за сколько дней
показывать AI_ResultsOfAutoCounting (1..AUTO_DAYS_MAX).

Выход (stdout, JSON) — 5-элементный массив (совместимость индексов с фронтом):
  [
    queue_rows,         # [ {id, cam, imei, name, created_at, date_from,
                        #     date_to, status, error_text, has_video}, ... ]
    auto_rows,          # [ {id, cam, imei, name, date, status, finished_at,
                        #     total_in, total_out, first_start, last_end,
                        #     chunks:[...]}, ... ]
    manager_log_text,   # последние строки manager.log (reversed)
    [],                 # зарезервировано (раньше errors.log)
    []                  # зарезервировано (раньше worker-логи)
  ]

Все даты (created_at/date_from/date_to) форматируются в локальной TZ (UTC+offset).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import _bootstrap  # noqa: F401
from stores.queue_store import QueueStore

AUTO_DAYS_DEFAULT = 7
AUTO_DAYS_MAX = 60


def _tail_reverse(path: str, lines_count: int = 5000) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return f"Файл лога не найден: {path}"
    tail = list(reversed(lines[-lines_count:]))
    return "".join(line.strip() + "\n" for line in tail)


def _load_cam_tracker_map(db, cam_list):
    """cam → {imei, name} из trackers_conf + tracker (как в ShowingNeyroWork)."""
    if not cam_list:
        return {}
    mapping = {}
    rows = db.query(
        "SELECT TC.imei AS imei, TR.name AS tracker_name, TC.cam_list AS cam_list "
        "FROM trackers_conf TC LEFT JOIN tracker TR ON TR.imei = TC.imei "
        "WHERE TC.type = 0",
    )
    # Сопоставляем каждую запрошенную камеру с конфигом по вхождению в cam_list.
    want = set(cam_list)
    for r in rows:
        for cam in (r.get("cam_list") or "").split("|"):
            cam = cam.strip()
            if cam and cam in want:
                mapping[cam] = {"imei": r.get("imei"),
                                "name": r.get("tracker_name")}
    return mapping


def _fmt_ts(ts, tz: timezone):
    """Unix ts / datetime / строка → 'YYYY-MM-DD HH:MM:SS' в локальной TZ."""
    if ts is None or ts == "":
        return None
    if isinstance(ts, bool):
        return None
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, tz=tz).strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(ts, datetime):
        # created_at/updated_at — TIMESTAMP без tz (хранится в UTC сервером).
        aware = ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts
        return aware.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S")
    return str(ts)


def _build_queue_rows(jobs, cam_map: dict, tz: timezone):
    rows = []
    for job in jobs:
        info = cam_map.get(job.cam_name, {})
        params = job.params if isinstance(job.params, dict) else {}
        has_video = bool(params.get("save_video"))

        created_raw = getattr(job, "created_at", None)
        created = _fmt_ts(created_raw, tz) if created_raw is not None else None

        rows.append({
            "id": job.id,
            "cam": job.cam_name,
            "imei": info.get("imei"),
            "name": info.get("name"),
            "created_at": created,
            "date_from": _fmt_ts(job.date_from, tz),
            "date_to": _fmt_ts(job.date_to, tz),
            "status": job.status,
            "queue_priority": job.queue_priority,
            "error_text": job.error_text,
            "has_video": has_video,
        })
    return rows


def _read_auto_days() -> int:
    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
        body = json.loads(raw) if raw.strip() else {}
        days = int(body.get("auto_days", AUTO_DAYS_DEFAULT))
    except (ValueError, TypeError, AttributeError):
        days = AUTO_DAYS_DEFAULT
    return max(1, min(days, AUTO_DAYS_MAX))


def _load_auto_results(db, days: int, tz: timezone):
    since = (datetime.now(tz).date() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    return db.query(
        "SELECT `id`,`name`,`date`,`data` FROM `AI_ResultsOfAutoCounting` "
        "WHERE `date` >= %s ORDER BY `date` DESC, `id` DESC",
        (since,),
    )


def _build_auto_rows(results, cam_map: dict, tz: timezone):
    """Строки AI_ResultsOfAutoCounting → отрезки + итоги.
    status: done — есть _meta.finished=true (день закрыт), иначе processing."""
    rows = []
    for r in results:
        try:
            data = json.loads(r["data"]) if r["data"] else {}
        except (ValueError, TypeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        meta = data.get("_meta") if isinstance(data.get("_meta"), dict) else {}

        chunks = []
        for key, val in data.items():
            if key.startswith("_") or not isinstance(val, dict):
                continue
            parts = key.split("_")
            try:
                start, end = int(parts[0]), int(parts[1])
            except (ValueError, IndexError):
                continue
            chunks.append((start, end, int(val.get("in") or 0), int(val.get("out") or 0)))
        chunks.sort()

        finished = meta.get("finished") is True
        info = cam_map.get(r["name"], {})
        rows.append({
            "id": r["id"],
            "cam": r["name"],
            "imei": info.get("imei"),
            "name": info.get("name"),
            "date": str(r["date"]),
            "status": "done" if finished else "processing",
            "finished_at": meta.get("finished_at") if finished else None,
            "total_in": sum(c[2] for c in chunks),
            "total_out": sum(c[3] for c in chunks),
            "first_start": _fmt_ts(chunks[0][0], tz) if chunks else None,
            "last_end": _fmt_ts(chunks[-1][1], tz) if chunks else None,
            "chunks": [{"from": _fmt_ts(s, tz), "to": _fmt_ts(e, tz),
                        "in": i, "out": o} for s, e, i, o in chunks],
        })
    return rows


def main():
    cfg = _bootstrap.load_config()
    db = _bootstrap.load_db(cfg)
    tz = timezone(timedelta(hours=cfg.timezone_offset_hours))
    auto_days = _read_auto_days()

    store = QueueStore(db)
    jobs = store.list_all()
    auto_results = _load_auto_results(db, auto_days, tz)

    cams = {j.cam_name for j in jobs} | {r["name"] for r in auto_results}
    cam_map = _load_cam_tracker_map(db, list(cams)) if cams else {}

    queue_rows = _build_queue_rows(jobs, cam_map, tz)
    auto_rows = _build_auto_rows(auto_results, cam_map, tz)

    manager_log = os.path.join(cfg.log_dir, "manager.log")

    result = [
        queue_rows,
        auto_rows,
        _tail_reverse(manager_log),
        [],
        [],
    ]
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
