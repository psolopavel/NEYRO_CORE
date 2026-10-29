#!/usr/bin/env python3
"""Единый объект задачи Job — то, что объединяет два менеджера.

И AutoSource (по акселерометру), и QueueSource (по таблице AI_CountingQueue)
производят один и тот же Job. Worker обрабатывает его ОДИНАКОВО:
    download → build_video_tasks → YOLO → результат.

Различается только КУДА писать результат — это инкапсулировано в колбэках
on_success / on_error, которые задаёт источник:
    - AutoSource:  пишет чанк в AI_ResultsOfAutoCounting (ResultsStore)
    - QueueSource: пишет день в AI_ResultsOfCounting (CountingResultsStore)
                   + статус задачи в очереди
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

SOURCE_AUTO = "auto"
SOURCE_QUEUE = "queue"


@dataclass
class Job:
    source: str                       # SOURCE_AUTO | SOURCE_QUEUE
    camera_name: str
    date_str: str                     # YYYY-MM-DD
    date_compact: str                 # YYYYMMDD
    segments: List[Tuple[int, int]]   # [(start_ts, end_ts), ...] (epoch sec)
    chunk_start: int                  # начало обрабатываемого окна (epoch)
    chunk_end: int                    # конец окна (epoch)
    region: list
    model_size: str
    person_quality: str
    save_video: bool = False

    # Колбэки результата (worker вызывает один из них).
    # on_success(in_count, out_count, save_video_path|None)
    on_success: Optional[Callable[[int, int, Optional[str]], None]] = None
    # on_error(exc_or_msg)
    on_error: Optional[Callable[[object], None]] = None

    # Приоритет в пуле: запросные задачи (queue) идут первыми.
    priority: int = 0

    # Метаданные источника (например, queue job id) — для логов/heartbeat.
    meta: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        """Уникальный ключ задачи (для дедупликации в active-наборе)."""
        if self.source == SOURCE_QUEUE:
            return f"queue:{self.meta.get('queue_id', self.camera_name)}"
        return f"auto:{self.camera_name}:{self.date_str}"

    @property
    def label(self) -> str:
        return f"{self.camera_name}/{self.date_str}"
