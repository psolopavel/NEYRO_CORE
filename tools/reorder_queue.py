#!/usr/bin/env python3
"""reorder_queue.py — изменение порядка pending-задач в AI_CountingQueue.

Вход (stdin, JSON):
  {"ordered_ids": [12, 5, 3, 8]}   — id pending-задач сверху вниз

Выход (stdout, JSON): {"success": bool, "updated": int, "error"?: str}
"""

from __future__ import annotations

import json
import sys

import _bootstrap  # noqa: F401
from stores.queue_store import QueueStore


def main():
    raw = sys.stdin.read()
    try:
        data = json.loads(raw) if raw else {}
    except Exception:
        print(json.dumps({"success": False, "error": "Invalid JSON input",
                          "updated": 0}))
        sys.exit(1)

    ordered_ids = data.get("ordered_ids")
    if not isinstance(ordered_ids, list):
        print(json.dumps({"success": False, "error": "Missing ordered_ids list",
                          "updated": 0}))
        sys.exit(1)

    result = QueueStore(_bootstrap.load_db()).reorder_pending(ordered_ids)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
