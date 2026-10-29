#!/usr/bin/env python3
"""send_reports.py — ручная работа с очередью писем AI_ResultDeliveries.

В работе письма шлёт фоновый поток manager.py; этот tool — для проверки:

    python tools/send_reports.py --once                 # один проход очереди и выход
    python tools/send_reports.py --preview 12 [--out DIR]
                                    # xlsx + письмо строки id=12 в папку, без отправки
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import timedelta, timezone
from pathlib import Path

import _bootstrap
from mailer import ResultMailer
from stores.queue_store import QueueStore
from stores.results_store import CountingResultsStore, ResultsStore


def main() -> int:
    parser = argparse.ArgumentParser(description="Письма с результатами подсчёта")
    parser.add_argument("--once", action="store_true", help="один проход очереди и выход")
    parser.add_argument("--preview", type=int, metavar="ID",
                        help="собрать xlsx и письмо строки AI_ResultDeliveries без отправки")
    parser.add_argument("--out", default="report_preview", help="папка для --preview")
    parser.add_argument("--config", default=None)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    cfg = _bootstrap.load_config(args.config)
    db = _bootstrap.load_db(cfg)
    tz = timezone(timedelta(hours=cfg.timezone_offset_hours))
    mailer = ResultMailer(cfg, db, ResultsStore(db, tz), CountingResultsStore(db),
                          QueueStore(db), tz)

    if args.preview is not None:
        for path in mailer.preview(args.preview, Path(args.out)):
            print(path)
        return 0
    if args.once:
        disabled = mailer.disabled_reason()
        if disabled:
            print(disabled)
            return 1
        print(f"Отправлено: {mailer.process_due()}")
        return 0
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
