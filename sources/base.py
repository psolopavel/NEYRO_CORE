#!/usr/bin/env python3
"""TaskSource — общий интерфейс источника задач.

Менеджер опрашивает источники: source.discover() возвращает список Job,
готовых к запуску прямо сейчас. Источник сам решает, что считать готовым
(накоплено ли видео, есть ли движение, есть ли pending-запрос и т.п.).
"""

from __future__ import annotations

from typing import List

from job import Job


class TaskSource:
    name = "base"

    def discover(self, now_epoch: int, available_slots: int) -> List[Job]:
        """Вернуть Job-ы, готовые к запуску (не более available_slots полезно).

        Источник НЕ запускает обработку сам — только формирует задачи.
        Колбэки on_success/on_error в Job задаёт источник.
        """
        raise NotImplementedError
