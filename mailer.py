#!/usr/bin/env python3
"""ResultMailer — письма владельцам машин с результатами подсчёта.

Один отчёт = одна машина (imei) за день, все её камеры сразу.
Две стороны (очередь — таблица AI_ResultDeliveries):
  * schedule_auto / schedule_queue — вызывают источники, когда у машины
    досчитаны все камеры; сбой постановки только логируется и не ломает подсчёт;
  * фоновый поток (start/stop) — раз в mail.interval_sec отправляет pending
    через SendLayer, при сбое — повтор с нарастающей паузой.

Получатели (как passengers_xls_mail.php):
  камера → trackers_conf.cam_list → imei → tracker.owner (по письму на владельца)
  → gps.user_settings (type = 1, state = 1, params — адреса через запятую).

Число пассажиров считается как на сайте (revenue3.php): режим In/Out/Avr из
AI_carsData type=5 по камере, коэффициент AI_coefficients на дату, итог по
машине — сумма камер.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from core import result_report as rr
from core import sendlayer
from core.logging_setup import get_camera_logger
from job import SOURCE_AUTO, SOURCE_QUEUE
from stores.delivery_store import DeliveryStore

log = logging.getLogger("mailer")

USER_SETTINGS_EMAIL_TYPE = 1

# Паузы между повторами (мин) после 1-й, 2-й, ... неудачной попытки.
_RETRY_DELAYS_MIN = (1, 5, 15, 60, 180)

_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9._%+\-]+@"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z]{2,63}$"
)


# ── адреса ───────────────────────────────────────────────────────────

def is_valid_email(email: str) -> bool:
    if not email or len(email) > 254:
        return False
    local, _, _domain = email.rpartition("@")
    if not local or len(local) > 64 or ".." in email:
        return False
    if local.startswith(".") or local.endswith("."):
        return False
    return bool(_EMAIL_RE.match(email))


def parse_emails(raw: Optional[str]) -> Tuple[List[str], List[str]]:
    """'a@x.ua, b@y.com,bad' -> (валидные, невалидные). Дубли убираются."""
    valid: List[str] = []
    invalid: List[str] = []
    seen: set = set()
    for part in re.split(r"[,;\s]+", raw or ""):
        email = part.strip()
        if not email or email.lower() in seen:
            continue
        seen.add(email.lower())
        (valid if is_valid_email(email) else invalid).append(email)
    return valid, invalid


# ── расчёт числа как в revenue3.php ──────────────────────────────────

def _php_round(value: float) -> int:
    """round() из PHP: половина — от нуля (у Python банковское округление)."""
    return int(value + 0.5) if value >= 0 else -int(-value + 0.5)


def resolve_count_mode(config) -> str:
    """Копия revenue3_resolveAutoCountInOut: 'in' | 'out' | 'avr'."""
    if not isinstance(config, dict):
        return "in"
    in_on, out_on, avr_on = bool(config.get("In")), bool(config.get("Out")), bool(config.get("Avr"))
    if out_on and not in_on:
        return "out"
    if in_on:
        return "in"
    if avr_on or (not in_on and not out_on):
        return "avr"
    return "in"


def pick_value(in_count: int, out_count: int, mode: str) -> int:
    if mode == "out":
        return int(out_count)
    if mode == "avr":
        return _php_round((int(in_count) + int(out_count)) / 2)
    return int(in_count)


class ResultMailer:
    def __init__(self, cfg, db, results_store, counting_results_store, queue_store,
                 tz: timezone):
        self.cfg = cfg
        self.db = db
        self.results = results_store
        self.counting_results = counting_results_store
        self.queue = queue_store
        self.tz = tz
        self.deliveries = DeliveryStore(db)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ── постановка в очередь ─────────────────────────────────────────

    def schedule_auto(self, imei: str, cams: List[str], date_str: str, trigger_cam: str):
        """День завершён у всех камер машины, которые обрабатывает авто-подсчёт."""
        if not self.cfg.mail_enabled:
            return
        clog = get_camera_logger(self.cfg.camera_log_dir, trigger_cam, SOURCE_AUTO, date_str)
        try:
            cameras = []
            for cam in cams:
                chunks = self.results.get_chunks(date_str, cam)
                cameras.append({
                    "cam": cam,
                    "from": chunks[0][0] if chunks else None,
                    "to": chunks[-1][1] if chunks else None,
                    "parts": [[c[2], c[3]] for c in chunks],
                })
            self._schedule(SOURCE_AUTO, imei, date_str, None, cameras,
                           f"auto:{imei}:{date_str}", clog)
        except Exception:
            clog.exception("❌ ошибка постановки письма в очередь")

    def schedule_queue(self, cam: str, date_str: str, queue_id: int):
        """Запрос по камере выполнен. Письмо — когда у машины не осталось
        незавершённых запросов за этот день; в отчёт — все камеры машины
        с результатом в AI_ResultsOfCounting (как aiValue на сайте)."""
        if not self.cfg.mail_enabled:
            return
        clog = get_camera_logger(self.cfg.camera_log_dir, cam, SOURCE_QUEUE, date_str)
        try:
            imei, car_cams = self._camera_car(cam)
            day_from, day_to = self._day_bounds(date_str)
            if self.queue.has_active_for(car_cams, day_from, day_to):
                clog.info("✉️  у машины ещё есть запросы за день — письмо позже")
                return
            cameras = []
            for car_cam in car_cams:
                result = self.counting_results.get(date_str, car_cam)
                if result is None:
                    continue
                window = self.queue.last_done_window(car_cam, day_from, day_to)
                cameras.append({
                    "cam": car_cam,
                    "from": window[0] if window else None,
                    "to": window[1] if window else None,
                    "parts": [list(result)],
                })
            dedup_base = f"queue:{imei or cam}:{date_str}:{queue_id}"
            self._schedule(SOURCE_QUEUE, imei, date_str, queue_id, cameras,
                           dedup_base, clog)
        except Exception:
            clog.exception("❌ ошибка постановки письма в очередь")

    def _schedule(self, source, imei, date_str, queue_id, cameras, dedup_base, clog):
        if not any(i or o for cam in cameras for i, o in cam["parts"]):
            clog.info("✉️  итог по машине 0 — письмо не отправляется")
            return
        common = dict(source=source, count_date=date_str, queue_id=queue_id,
                      cameras=cameras, delay_sec=self.cfg.mail_send_delay_min * 60)
        owners = self._owners(imei) if imei else []
        if not owners:
            reason = ("Камеру не знайдено в trackers_conf" if not imei
                      else "У машини немає власника в tracker")
            self.deliveries.enqueue(dedup_raw=f"{dedup_base}:", imei=imei or "", owner="",
                                    status="no_recipient", last_error=reason, **common)
            clog.warning("✉️  письмо не поставлено: %s", reason)
            return
        names = ", ".join(c["cam"] for c in cameras)
        for owner in owners:
            if self.deliveries.enqueue(dedup_raw=f"{dedup_base}:{owner}", imei=imei,
                                       owner=owner, **common):
                clog.info("✉️  отчёт по машине %s (%s) владельцу %s поставлен в очередь",
                          imei, names, owner)

    def _day_bounds(self, date_str: str) -> Tuple[int, int]:
        start = datetime.combine(date.fromisoformat(date_str), dtime(), tzinfo=self.tz)
        return int(start.timestamp()), int((start + timedelta(days=1)).timestamp())

    # ── данные из gps ────────────────────────────────────────────────

    def _camera_car(self, cam: str) -> Tuple[Optional[str], List[str]]:
        """(imei, все камеры машины из cam_list); не нашли — (None, [cam])."""
        rows = self.db.query(
            "SELECT `imei`,`cam_list` FROM `trackers_conf` "
            "WHERE `type`=0 AND `cam_list` LIKE %s",
            (f"%{cam}%",),
        )
        for r in rows:
            cams = [c.strip() for c in (r["cam_list"] or "").split("|") if c.strip()]
            if cam in cams:
                return str(r["imei"]), cams
        return None, [cam]

    def _owners(self, imei: str) -> List[str]:
        rows = self.db.query(
            "SELECT DISTINCT `owner` FROM `tracker` WHERE `imei`=%s ORDER BY `owner`",
            (imei,),
        )
        return [str(r["owner"]).strip() for r in rows if str(r["owner"] or "").strip()]

    def _car_name(self, imei: str, owner: str) -> str:
        rows = self.db.query(
            "SELECT `name` FROM `tracker` WHERE `imei`=%s AND `owner`=%s LIMIT 1",
            (imei, owner),
        )
        name = str(rows[0]["name"] or "").strip() if rows else ""
        return name or imei

    def resolve_recipients(self, owner: str) -> Tuple[List[str], List[str]]:
        rows = self.db.query(
            "SELECT `params` FROM `user_settings` "
            "WHERE `username`=%s AND `type`=%s AND `state`=1",
            (owner.lower(), USER_SETTINGS_EMAIL_TYPE),
        )
        return parse_emails(",".join(str(r["params"]) for r in rows if r["params"]))

    def _count_mode(self, cam: str) -> str:
        rows = self.db.query(
            "SELECT `Squares` FROM `AI_carsData` WHERE `name`=%s AND `type`=5 LIMIT 1",
            (cam,),
        )
        if not rows:
            return "in"
        try:
            return resolve_count_mode(json.loads(rows[0]["Squares"]))
        except (TypeError, ValueError):
            return "in"

    def _coefficient(self, imei: str, date_str: str) -> float:
        rows = self.db.query(
            "SELECT `date`,`percent` FROM `AI_coefficients` WHERE `imei`=%s ORDER BY `date`",
            (imei,),
        )
        coef = 1.0
        for r in rows:
            if date_str >= str(r["date"])[:10]:
                coef = float(r["percent"])
            else:
                break
        return coef

    # ── сборка отчёта ────────────────────────────────────────────────

    def _local(self, ts) -> Optional[datetime]:
        if not ts:
            return None
        return datetime.fromtimestamp(int(ts), tz=self.tz).replace(tzinfo=None)

    @staticmethod
    def _row_day(row: dict) -> date:
        day = row["count_date"]
        return day if isinstance(day, date) else date.fromisoformat(str(day)[:10])

    @staticmethod
    def _row_cameras(row: dict) -> List[dict]:
        raw = row["cameras"]
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        return json.loads(raw) if isinstance(raw, str) else list(raw or [])

    def _camera_passengers(self, source: str, parts, mode: str, coef: float) -> int:
        if source == SOURCE_AUTO:
            # revenue3.php: коэффициент — к in/out каждого чанка, затем режим.
            total_in = sum(_php_round(i * coef) for i, _o in parts)
            total_out = sum(_php_round(o * coef) for _i, o in parts)
            return pick_value(total_in, total_out, mode)
        # revenue3.php: режим к {"in","out"} дня, затем коэффициент.
        in_c, out_c = parts[0] if parts else (0, 0)
        return _php_round(pick_value(in_c, out_c, mode) * coef)

    def build_report(self, row: dict, version: int,
                     previous_sent_at: Optional[datetime]) -> rr.ResultReport:
        day = self._row_day(row)
        imei = row["imei"] or ""
        coef = self._coefficient(imei, day.isoformat()) if imei else 1.0

        cameras = []
        for cam in self._row_cameras(row):
            mode = self._count_mode(cam["cam"])
            cameras.append(rr.CameraRow(
                cam_name=cam["cam"],
                window_start=self._local(cam.get("from")),
                window_end=self._local(cam.get("to")),
                passengers=self._camera_passengers(row["source"], cam.get("parts") or [],
                                                   mode, coef),
                count_mode=mode,
            ))

        return rr.ResultReport(
            source=row["source"],
            car_name=(self._car_name(imei, row["owner"]) if imei
                      else ", ".join(c.cam_name for c in cameras)),
            imei=imei,
            day=day,
            version=version,
            previous_sent_at=previous_sent_at,
            generated_at=datetime.now(self.tz).replace(tzinfo=None),
            cameras=cameras,
        )

    def _version(self, row: dict) -> Tuple[int, Optional[datetime]]:
        previous = self.deliveries.last_sent(row["imei"], self._row_day(row).isoformat(),
                                             row["owner"])
        if not previous:
            return 1, None
        return int(previous["version"] or 0) + 1, self._local(previous["sent_at"])

    def render(self, row: dict):
        """-> (report, filename, subject, html, text, xlsx bytes)."""
        version, previous_sent_at = self._version(row)
        report = self.build_report(row, version, previous_sent_at)
        filename = rr.report_filename(report)
        subject = rr.email_subject(report)
        html = rr.render_html(report, subject=subject, filename=filename,
                              reply_to=self.cfg.mail_reply_to)
        text = rr.render_text(report, filename=filename, reply_to=self.cfg.mail_reply_to)
        return report, filename, subject, html, text, rr.build_xlsx(report)

    # ── отправка ─────────────────────────────────────────────────────

    def _bcc(self, to: List[str]) -> List[str]:
        taken = {e.lower() for e in to}
        valid, _invalid = parse_emails(self.cfg.mail_bcc)
        return [e for e in valid if e.lower() not in taken]

    def _retry(self, row: dict, error: str, *, retryable: bool):
        attempts = int(row.get("attempts") or 0) + 1
        if not retryable or attempts >= self.cfg.mail_max_attempts:
            delay = None
        else:
            delay = _RETRY_DELAYS_MIN[min(attempts, len(_RETRY_DELAYS_MIN)) - 1] * 60
        self.deliveries.schedule_retry(row["id"], error, delay)
        log.warning("✉️  #%s %s/%s: %s%s", row["id"], row["imei"], row["count_date"],
                    error, "" if delay else " — больше не повторяем")

    def _deliver(self, row: dict) -> bool:
        """Одна отправка строки в статусе sending. -> True, если письмо ушло."""
        valid, invalid = self.resolve_recipients(row["owner"])
        if not valid:
            self.deliveries.finish(
                row["id"], "no_recipient",
                f"Немає валідних адрес у user_settings (type = 1, state = 1) "
                f"для {row['owner']}", skipped=invalid)
            return False

        report, filename, subject, html, text, content = self.render(row)
        try:
            message_id = sendlayer.send_email(
                self.cfg, to=valid, subject=subject, html=html, text=text,
                attachments=[sendlayer.Attachment(filename, content, rr.XLSX_MIME)],
                bcc=self._bcc(valid), tags=["neyro-count", row["source"]],
            )
        except sendlayer.SendLayerError as exc:
            self._retry(row, str(exc), retryable=exc.retryable)
            return False

        self.deliveries.mark_sent(row["id"], version=report.version, message_id=message_id,
                                  recipients=valid, skipped=invalid)
        log.info("📧 #%s %s/%s v%d → %s (пасажирів %d)", row["id"], row["imei"],
                 row["count_date"], report.version, ", ".join(valid), report.passengers)
        return True

    def process_due(self, limit: int = 20) -> int:
        """Отправляет pending, чей next_attempt_at наступил. -> число отправленных."""
        sent = 0
        for delivery_id in self.deliveries.list_due(limit):
            if not self.deliveries.claim(delivery_id):
                continue
            row = self.deliveries.get(delivery_id)
            if row is None:
                continue
            try:
                if self._deliver(row):
                    sent += 1
            except Exception as exc:
                log.exception("✉️  #%s: ошибка отправки", delivery_id)
                self._retry(row, f"{type(exc).__name__}: {exc}", retryable=True)
        return sent

    def preview(self, delivery_id: int, out_dir: Path) -> List[Path]:
        """xlsx + письмо по строке очереди в out_dir, без отправки."""
        row = self.deliveries.get(delivery_id)
        if row is None:
            raise ValueError(f"Строки AI_ResultDeliveries id={delivery_id} нет")
        _report, filename, subject, html, text, content = self.render(row)
        out_dir.mkdir(parents=True, exist_ok=True)
        paths = [out_dir / filename, out_dir / "email.html", out_dir / "email.txt"]
        paths[0].write_bytes(content)
        paths[1].write_text(html, encoding="utf-8")
        paths[2].write_text(f"Тема: {subject}\n\n{text}", encoding="utf-8")
        return paths

    # ── фоновый поток ────────────────────────────────────────────────

    def disabled_reason(self) -> str:
        if not self.cfg.mail_enabled:
            return "mail.enabled = false — отправка писем выключена"
        if not self.cfg.mail_api_key:
            return "mail.sendlayer_api_key не задан — отправка писем выключена"
        return ""

    def start(self):
        disabled = self.disabled_reason()
        if disabled:
            log.info("✉️  %s", disabled)
            return
        self._thread = threading.Thread(target=self._loop, name="result-mailer",
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.cfg.mail_timeout_sec + 5)

    def _loop(self):
        try:
            recovered = self.deliveries.recover_stale_sending()
            if recovered:
                log.warning("✉️  возвращено в очередь писем, зависших в sending: %d",
                            recovered)
        except Exception:
            log.exception("✉️  ошибка recover_stale_sending (таблица AI_ResultDeliveries есть?)")
        interval = max(5, self.cfg.mail_interval_sec)
        log.info("✉️  отправка писем запущена, интервал %d с", interval)
        while not self._stop.is_set():
            try:
                sent = self.process_due()
                if sent:
                    log.info("✉️  отправлено писем: %d", sent)
            except Exception:
                log.exception("✉️  ошибка обработки очереди писем")
            self._stop.wait(interval)
