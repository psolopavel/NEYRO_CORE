#!/usr/bin/env python3
"""cam_logs.py — список камер и лог по конкретной камере.

Два режима (по JSON со stdin):

  1) Список камер (по умолчанию, без cam):
       вход:  {} или {"action":"list"}
       выход: [ {"cam": "...", "imei": "...", "name": "...", "has_log": bool}, ... ]

  2) Лог одной камеры:
       вход:  {"action":"log", "cam":"<имя_камеры>"}
       выход: {"cam": "...", "log": "<последние строки, reversed>"}

Источник списка — trackers_conf (cam_list) + tracker (name по imei).
Лог камеры — <camera_log_dir>/<safe_filename(cam)>.log (по умолчанию
/tmp/NEYRO_LOGS/cameras/<cam>.log).
"""

from __future__ import annotations

import json
import os
import re
import sys

import _bootstrap  # noqa: F401


def _safe_filename(name: str) -> str:
    return re.sub(r"[^\w.-]", "_", name or "unknown")


def _tail_reverse(path: str, lines_count: int = 5000) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return f"Лог по камері не знайдено: {path}"
    tail = list(reversed(lines[-lines_count:]))
    return "".join(line.strip() + "\n" for line in tail)


def _list_cameras(db, camera_log_dir: str):
    rows = db.query(
        "SELECT TC.imei AS imei, TR.name AS tracker_name, TC.cam_list AS cam_list "
        "FROM trackers_conf TC LEFT JOIN tracker TR ON TR.imei = TC.imei "
        "WHERE TC.type = 0",
    )
    cameras = []
    seen = set()
    for r in rows:
        imei = r.get("imei")
        name = r.get("tracker_name")
        for cam in (r.get("cam_list") or "").split("|"):
            cam = cam.strip()
            if not cam or cam in seen:
                continue
            seen.add(cam)
            log_path = os.path.join(camera_log_dir, f"{_safe_filename(cam)}.log")
            # Показываем только камеры, у которых уже есть лог-файл.
            if not os.path.isfile(log_path):
                continue
            cameras.append({
                "cam": cam,
                "imei": str(imei) if imei is not None else None,
                "name": name,
                "has_log": True,
            })
    cameras.sort(key=lambda c: (c["name"] or "", c["cam"]))
    return cameras


def main():
    raw = sys.stdin.read()
    try:
        data = json.loads(raw) if raw else {}
    except Exception:
        data = {}

    cfg = _bootstrap.load_config()
    camera_log_dir = getattr(cfg, "camera_log_dir",
                             os.path.join(cfg.log_dir, "cameras"))

    action = data.get("action")
    cam = data.get("cam")

    if action == "log" or (cam and action != "list"):
        if not cam:
            print(json.dumps({"error": "Не вказано камеру"}, ensure_ascii=False))
            return
        log_path = os.path.join(camera_log_dir, f"{_safe_filename(cam)}.log")
        print(json.dumps(
            {"cam": cam, "log": _tail_reverse(log_path)},
            ensure_ascii=False))
        return

    db = _bootstrap.load_db(cfg)
    print(json.dumps(_list_cameras(db, camera_log_dir), ensure_ascii=False))


if __name__ == "__main__":
    main()
