#!/usr/bin/env python3
"""manager.py — ЕДИНЫЙ демон подсчёта пассажиров.

Объединяет procesManager.py и auto_counter/main.py в один процесс:
  - один пул воркеров (ThreadPoolExecutor) с общим лимитом GPU-задач;
  - два источника задач (QueueSource — приоритетный, AutoSource);
  - фоновый поток отправки писем с результатами (mailer.ResultMailer);
  - shared_slots больше НЕ нужен (один процесс = один пул).

Запуск:
    python manager.py
    python manager.py --config /path/to/config.ini
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import timedelta, timezone
from pathlib import Path
from typing import Dict, List

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.config import Config
from core.db import Database
from core.logging_setup import setup_root_logger
from job import Job
from mailer import ResultMailer
from sources.auto_source import AutoSource
from sources.queue_source import QueueSource
from stores.queue_store import QueueStore
from stores.results_store import CountingResultsStore, ResultsStore
from worker import Worker


class Manager:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.tz = timezone(timedelta(hours=cfg.timezone_offset_hours))
        self.db = Database(cfg)
        # RoutesAndAdvertisement: привязка зон игнорирования подсчёта (type8).
        self.zones_db = Database(cfg, database=cfg.zones_db_name)

        self.queue_store = QueueStore(self.db)
        self.results_store = ResultsStore(self.db, self.tz)
        self.counting_results_store = CountingResultsStore(self.db)
        self.mailer = ResultMailer(cfg, self.db, self.results_store,
                                   self.counting_results_store, self.queue_store, self.tz)
        self.worker = Worker(cfg, self.tz)

        # Источники: queue первым (приоритет).
        self.queue_source = QueueSource(cfg, self.db, self.queue_store,
                                        self.counting_results_store, self.mailer,
                                        self.tz, self.zones_db)
        self.auto_source = AutoSource(cfg, self.db, self.results_store, self.mailer,
                                      self.tz, self.zones_db)

        self.executor = ThreadPoolExecutor(max_workers=cfg.max_workers)
        self.active: Dict[str, Future] = {}
        self.running = True

        signal.signal(signal.SIGINT, self._stop)
        signal.signal(signal.SIGTERM, self._stop)

    def _stop(self, *_):
        logging.info("🛑 Получен сигнал остановки")
        self.running = False

    def run(self):
        logging.info("🚀 NEYRO_CORE manager запущен (max_workers=%d)",
                     self.cfg.max_workers)
        # Восстановить зависшие задачи очереди.
        try:
            reclaimed = self.queue_store.reclaim_stale()
            if reclaimed:
                logging.info("♻️  Восстановлено зависших задач очереди: %d", reclaimed)
        except Exception:
            logging.exception("Ошибка reclaim_stale (таблица AI_CountingQueue есть?)")
        self.mailer.start()

        while self.running:
            try:
                self._tick()
            except Exception:
                logging.exception("💥 Ошибка в основном цикле")
                time.sleep(10)
            for _ in range(self.cfg.check_interval_sec):
                if not self.running:
                    break
                time.sleep(1)

        logging.info("⏳ Ожидание завершения задач...")
        self.executor.shutdown(wait=True, cancel_futures=False)
        self.mailer.stop()
        logging.info("✅ Manager остановлен")

    def _tick(self):
        now_epoch = int(time.time())
        self._cleanup_finished()

        available = self.cfg.max_workers - len(self.active)
        if available <= 0:
            return

        # 1) Приоритет — очередь запросов.
        jobs: List[Job] = self.queue_source.discover(now_epoch, available)
        available -= len(jobs)

        # 2) Авто-задачи только если PM не занял все слоты И нет pending-запросов
        #    (приоритет PM сохраняется без межпроцессной координации).
        if available > 0 and not self.queue_store.has_pending():
            jobs += self.auto_source.discover(now_epoch, available)

        for job in jobs:
            self._submit(job)

    def _submit(self, job: Job):
        if job.key in self.active:
            return
        logging.info("➕ [%s] взят в работу (source=%s, активно=%d/%d)",
                     job.label, job.source, len(self.active) + 1, self.cfg.max_workers)
        fut = self.executor.submit(self.worker.process, job)
        self.active[job.key] = fut

    def _cleanup_finished(self):
        done = [k for k, f in self.active.items() if f.done()]
        for k in done:
            fut = self.active.pop(k)
            try:
                fut.result()
            except Exception as e:
                logging.error("❌ %s: ошибка в потоке: %s", k, e)


def main():
    parser = argparse.ArgumentParser(description="NEYRO_CORE manager")
    parser.add_argument("--config", default=None)
    args = parser.parse_args()

    config_path = args.config or str(Path(PROJECT_ROOT) / "config.ini")
    cfg = Config(config_path)

    setup_root_logger(os.path.join(cfg.log_dir, "manager.log"))

    Manager(cfg).run()


if __name__ == "__main__":
    main()
