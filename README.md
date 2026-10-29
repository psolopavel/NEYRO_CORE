# NEYRO_CORE

Единый сервис подсчёта пассажиров по видеокамерам транспорта.
Объединяет два бывших независимых менеджера (авто-подсчёт по акселерометру и
подсчёт по запросам) в один демон с общим ядром, очередью в БД и едиными логами.

На сервере располагается в `/home/NEYRO_CORE/`.

---

## Структура

```
NEYRO_CORE/
├── manager.py          # ЕДИНЫЙ демон: пул воркеров + опрос источников
├── worker.py           # общий пайплайн: download → frames → YOLO → result
├── mailer.py           # письма владельцам с результатами (поток внутри manager)
├── job.py              # единый объект задачи Job (+ колбэки результата)
├── config.ini          # ВСЕ настройки (БД, storage, пути, processing)
├── requirements.txt
├── core/               # переиспользуемое ядро (single source of truth)
│   ├── config.py db.py logging_setup.py
│   ├── intervals.py video.py ocr.py
│   ├── segments.py alarms.py tasks.py transfer.py
│   ├── zones.py          # зоны игнорирования подсчёта (type=8, БД RoutesAndAdvertisement)
│   ├── result_report.py  # xlsx + HTML/текст письма с результатом
│   ├── sendlayer.py      # клиент SendLayer API
│   └── yolo_counter.py   # подсчёт как subprocess (GPU освобождается)
├── stores/
│   ├── queue_store.py     # AI_CountingQueue (очередь запросов)
│   ├── results_store.py   # AI_ResultsOfAutoCounting (авто) + AI_ResultsOfCounting (запросы)
│   └── delivery_store.py  # AI_ResultDeliveries (очередь писем)
├── sources/
│   ├── base.py
│   ├── auto_source.py     # акселерометр → Job
│   └── queue_source.py    # AI_CountingQueue → Job
├── tools/                 # HTTP-эндпоинты (вызываются роутером как subprocess)
│   ├── http_router.py     # ← InquieryRedirect.py
│   ├── enqueue_request.py # ← saveNewReq.py
│   ├── remove_request.py  # ← RemoveRequest.py
│   ├── show_status.py     # ← ShowingNeyroWork.py
│   └── send_reports.py    # ручной проход очереди писем / предпросмотр
└── sql/
    ├── AI_CountingQueue.sql
    └── AI_ResultDeliveries.sql
```

---

## Поток данных

```
                       ┌──────────────────────────┐
SendingAiRequests.php ─▶ http_router /saveNewReq   │
                       │  → enqueue_request.py      │──▶ AI_CountingQueue (pending)
                       └──────────────────────────┘
                                                          │
            ┌─────────────────────── manager.py ─────────┼───────────────┐
            │  каждый тик:                                ▼               │
            │   1) QueueSource.discover() (приоритет) ── claim() ──┐      │
            │   2) AutoSource.discover()  (если нет pending)        │      │
            │                                                       ▼      │
            │                        worker.process(Job) ── core/yolo_counter.py
            │                                                       │      │
            │   on_success:                                         │      │
            │     auto  → AI_ResultsOfAutoCounting (чанки)          │      │
            │     queue → AI_ResultsOfCounting (день целиком)       │      │
            └──────────────────────────────────────────────────────┘      
```

### Зоны игнорирования подсчёта (AI_carsData type=8)

Если у камеры `type=8` с `Squares = 1` (для запросов — `type8 = "1"` в параметрах
от `SendingAiRequests.php`), из сегментов обработки вычитается время, когда машина
по GPS-треку находилась в своих зонах:

```
RoutesAndAdvertisement.all_car_routes.zones_id (JSON, по imei)
  → zones: type='counting_ignoring', active=1 → geom (+ radius_m)
  → GPS трекера (latitude/longtitude) → периоды «внутри зоны» → вычитание
```

Имя БД — `ZONES_DB_NAME` в `[database]` (хост и учётка те же). Point/LineString
считаются кругом/коридором радиусом `radius_m`, Polygon — своей площадью.
Ошибка доступа к зонам: авто-задача повторится на следующем тике, запросная
получает статус error.

`revenue3.php` читает обе таблицы по-разному (`AI_ResultsOfCounting` —
число in/out на день; `AI_ResultsOfAutoCounting` — чанки), формат не менялся.
Обе таблицы пишутся напрямую в БД через `stores/results_store.py`
(эндпоинт `setRevenue3Data` в revenue3.php сервисом больше не используется).

### Письма с результатами (`[mail]` в config.ini)

Владельцу машины уходит один отчёт за день по всем её камерам (строка на
камеру + итог) с xlsx-вложением (SendLayer, оформление как в
PassengersFlowCounting). Дни с итогом 0 по машине не отправляются.

- **авто** — когда день завершён у всех камер машины, которые обрабатывает
  авто-подсчёт. Камеры с блокировкой (`type=7`), без AutoCounting (`type=3`)
  или без региона (`type=0`) не ждём;
- **запрос** — когда у машины не осталось pending/processing запросов за этот
  день; в отчёт попадают все камеры с результатом в `AI_ResultsOfCounting`
  (итог совпадает с aiValue на сайте).

```
камера → trackers_conf.cam_list → imei → tracker.owner (письмо на каждого владельца)
  → user_settings (type=1, state=1): адреса через запятую
```

Число пассажиров — как на сайте: режим In/Out/Avr по камере (`AI_carsData`
type=5), коэффициент `AI_coefficients`, итог — сумма камер. Очередь и статусы — таблица `AI_ResultDeliveries`
(pending → sending → sent | failed | cancelled | no_recipient), повторы с паузой
1/5/15/60/180 мин. Новый результат по машине за тот же день отменяет ещё не
отправленное письмо; если письмо за день уже ушло, следующее помечается «версія N, оновлено».

```bash
python tools/send_reports.py --preview <id> --out /tmp/preview   # без отправки
python tools/send_reports.py --once                              # один проход
```

---

## Установка и запуск (сервер)

```bash
# 1) Зависимости
/ultralytics-env/bin/pip install -r /home/NEYRO_CORE/requirements.txt

# 2) Модели
#    положить yolov8{n,s,m,l,x}.engine в /home/NEYRO_CORE/models/

# 3) Создать таблицы очереди задач и очереди писем
mysql ... < /home/NEYRO_CORE/sql/AI_CountingQueue.sql
mysql ... < /home/NEYRO_CORE/sql/AI_ResultDeliveries.sql

# 4) Заполнить config.ini (DB_PASS, [mail] enabled = true и т.д.)

# 5) Запуск демона
/ultralytics-env/bin/python /home/NEYRO_CORE/manager.py

# 6) Запуск HTTP-роутера (приём запросов от PHP)
/ultralytics-env/bin/python /home/NEYRO_CORE/tools/http_router.py
```

Рекомендуется обернуть оба в systemd-юниты.

---

## Логи (всё в /tmp)

```
/tmp/NEYRO_LOGS/
├── manager.log          # discovery, submit задач
├── worker/<camera>.log  # download / OCR / YOLO / save для камеры
├── router.log           # HTTP-роутер
└── errors.log           # критические ошибки
```

Каждая строка задачи имеет префикс `[камера/дата]`.

---

## Миграция со старой системы

| Старое                                   | Новое                                  |
|------------------------------------------|----------------------------------------|
| `req.json`                               | таблица `AI_CountingQueue`             |
| `procesManager.py` + `auto_counter/main.py` | `manager.py` (один демон)           |
| `index.py` + `counter.py`                | `core/yolo_counter.py`                 |
| `preparationToMeaningfulNeyroRequest.py` | `core/tasks.py` + `sources/queue_source.py` |
| `shared_slots.py`                        | удалён (один пул потоков)               |
| `saveNewReq/RemoveRequest/ShowingNeyroWork` | `tools/*`                            |
| `SendVideosAndCountingResult.py`         | удалён (воркер сам пишет результат в БД и шлёт видео) |

Старые файлы оставлены как референс и не используются новым сервисом.
```
