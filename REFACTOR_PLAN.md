# NEYRO_CORE — план рефакторинга

Объединение двух менеджеров подсчёта пассажиров (авто + по запросам) в один проект
с общим ядром, очередью в БД и едиными логами.

- **Локально:** `home/NEYRO_CORE/`
- **На сервере:** `/home/NEYRO_CORE/`
- Старые файлы (`VideoFunctions/`, `YoloStarter/`, скрипты в `last/`) НЕ трогаем —
  они остаются как референс, пока новый проект не заработает.

---

## 1. Архитектура

Единый демон `manager.py` с пулом воркеров (`ThreadPoolExecutor`).
Два «источника задач» поверх общего пайплайна:

- **AutoSource** — самостоятельный, по акселерометру (бывш. `auto_counter/main.py`).
- **QueueSource** — по запросам из таблицы `AI_CountingQueue` (бывш. `req.json`).

Оба источника производят единый объект `Job`. Один `worker.py` обрабатывает Job
одинаково: download → OCR → segments → YOLO → сохранение результата.

`shared_slots.py` УДАЛЯЕТСЯ: один процесс = один пул потоков, межпроцессная
координация GPU-слотов больше не нужна. Приоритет запросных задач реализуется
очередью заданий внутри пула.

```
/home/NEYRO_CORE/
├── core/
│   ├── __init__.py
│   ├── config.py          # единый Config (БД + пути + processing + storage)
│   ├── db.py              # Database: connect/query/execute
│   ├── logging_setup.py   # ColorFormatter + setup_logger() (1 копия вместо 4)
│   ├── intervals.py       # merge/subtract/merge_into_covered
│   ├── video.py           # fps, frames_count, is_readable, parse_video_timestamps
│   ├── ocr.py             # ocr_timestamp + detect_video_real_start_ts_ocr (1 копия)
│   ├── alarms.py          # fetch_line_crossing_events + build_alarm_segments
│   ├── segments.py        # find_stops, analyze_movement, build_processing_segments
│   ├── tasks.py           # build_video_tasks (frame ranges из сегментов)
│   ├── transfer.py        # list_remote_videos, download, upload, compress
│   └── yolo_counter.py    # ЕДИНЫЙ count_people() (заменяет index.py + counter.py)
├── stores/
│   ├── queue_store.py     # CRUD AI_CountingQueue (claim/done/error/heartbeat)
│   └── results_store.py   # ResultsStore (AI_ResultsOfAutoCounting)
├── sources/
│   ├── base.py            # TaskSource.discover() -> list[Job]
│   ├── auto_source.py     # акселерометр → Job
│   └── queue_source.py    # AI_CountingQueue → Job
├── job.py                 # dataclass Job
├── worker.py             # обработка одного Job (общий пайплайн)
├── manager.py            # демон: пул + опрос источников
├── config.ini
├── models/               # yolov8*.engine (перенести сюда)
├── sql/
│   └── AI_CountingQueue.sql
└── tools/
    ├── enqueue_request.py # бывш. saveNewReq.py → пишет в БД
    ├── remove_request.py  # бывш. RemoveRequest.py → БД
    ├── show_status.py     # бывш. ShowingNeyroWork.py → БД + логи
    └── http_router.py     # бывш. InquieryRedirect.py
```

---

## 2. Результаты (две разные таблицы — намеренно!)

`revenue3.php` читает их по-разному, формат менять нельзя:

| Источник     | Таблица                    | Формат                                  | Как пишем |
|--------------|----------------------------|-----------------------------------------|-----------|
| AutoSource   | `AI_ResultsOfAutoCounting` | `{"start_end":{"in":X,"out":Y},"_meta"}`| прямой коннект (ResultsStore) |
| QueueSource  | `AI_ResultsOfCounting`     | `{"in":X,"out":Y}` (одно число)         | прямой коннект (CountingResultsStore) |

Обе таблицы пишет `stores/results_store.py` (общий upsert по `name`+`date`).
Раньше QueueSource слал POST `setRevenue3Data` на revenue3.php — убрано: PHP
не проверял результат запроса и всегда отвечал 200, ошибки записи терялись.

---

## 3. Таблица AI_CountingQueue (заменяет req.json)

```sql
CREATE TABLE AI_CountingQueue (
  id          INT AUTO_INCREMENT PRIMARY KEY,
  cam_name    VARCHAR(255) NOT NULL,
  date_from   INT NOT NULL,            -- unix ts (как в req.json)
  date_to     INT NULL,
  params      JSON NOT NULL,           -- {main, stops?, type1, type2, type5, type6, save_video}
  status      ENUM('pending','processing','done','error') NOT NULL DEFAULT 'pending',
  error_text  TEXT NULL,
  worker_id   VARCHAR(64) NULL,
  heartbeat   INT NULL,
  created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_status (status),
  KEY idx_cam_date (cam_name, date_from)
);
```

Атомарный захват задачи (вместо удаления ключа из файла):
```sql
UPDATE AI_CountingQueue SET status='processing', worker_id=?, heartbeat=UNIX_TIMESTAMP()
WHERE id=? AND status='pending';   -- rowcount=1 => задача наша
```

Контракт enqueue (из SendingAiRequests.php → saveNewReq):
вход `[{cam: [startTs, endTs, cameraSquares, showProcess]}]`,
`cameraSquares = {main, stops?, type1, type2, type5, type6}` → строка очереди.

---

## 4. Логи — единый формат, всё в /tmp

```
/tmp/NEYRO_LOGS/
├── manager.log          # demon: discovery, submit, slots
├── worker/<camera>.log  # download, OCR, YOLO, save для камеры
└── errors.log           # критические ошибки (вместо process_criticalErrors.txt)
```
Один `setup_logger(name, file)` с RotatingFileHandler (3 МБ × 2).
Каждая строка имеет префикс `[camera/date]` через LoggerAdapter.

---

## 5. Этапы (поэтапно, каждый проверяемый)

- [x] **Этап 0 — каркас.** `core/` создан, общие функции перенесены.
- [x] **Этап 1 — единый YOLO.** `core/yolo_counter.py` (index.py + counter.py).
- [x] **Этап 2 — очередь в БД.** `AI_CountingQueue` + `stores/` + `tools/` на БД.
- [x] **Этап 3 — два источника + worker + manager.** shared_slots не используется.
- [x] **Этап 4 — финал.** `tools/http_router.py`, README, requirements.
      send_results убран (воркер сам шлёт POST и видео). Старые файлы оставлены
      как референс (по решению — не удаляем).

---

## 6. Конфиг (всё, что было захардкожено, → config.ini)

- БД: host/port/user/pass/name
- storage: user/host/base (SSH/rsync `notlets@116.202.80.5`)
- paths: python_path, download_base, models_dir, tmp_dir, log_dir
- processing: max_workers, chunk_duration_sec, long_stop_threshold_sec,
  day_start_hour, day_end_hour, discovery_interval_sec, today_processing_start_hour
