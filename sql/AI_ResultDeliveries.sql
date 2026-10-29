-- ============================================================
-- AI_ResultDeliveries — очередь писем с результатами подсчёта
-- ============================================================
--
-- Одна строка = одно письмо одному владельцу машины (tracker.owner) с отчётом
-- по ВСЕМ камерам машины за день. Ставит manager.py (mailer.ResultMailer):
--   auto  — когда день завершён у каждой камеры машины, которую обрабатывает
--           авто-подсчёт (заблокированные type=7 и без AutoCounting не ждём);
--   queue — когда у машины не осталось незавершённых запросов за этот день.
-- Дни с нулевым итогом по машине (in = out = 0 у всех камер) не ставятся.
--
-- cameras — снимок результатов на момент постановки (сырые in/out):
--   [{"cam": "CAM1", "from": 1793068800, "to": 1793090000,
--     "parts": [[10, 8], [5, 3]]}, ...]
--   parts — чанки авто-подсчёта / один [in, out] запроса. Режим In/Out/Avr и
--   коэффициент применяются при отправке — как в revenue3.php.
--
-- Адреса владельца — gps.user_settings (type = 1, state = 1, params — через
-- запятую); перечитываются перед каждой попыткой отправки.
--
-- dedup_key (sha1) не даёт поставить то же письмо дважды, хэшируется строка:
--   auto:<imei>:<YYYY-MM-DD>:<owner>   |   queue:<imei>:<YYYY-MM-DD>:<queue_id>:<owner>
-- Новое письмо по той же машине/дню/владельцу отменяет ещё не отправленное
-- (status = cancelled). version = MAX(version отправленных) + 1, присваивается
-- в момент успешной отправки; version > 1 — письмо помечается как «оновлено».
--
-- Все времена — unix ts (как в AI_CountingQueue).
-- Если таблица уже создана по прошлой версии этого файла — DROP и создать заново.

CREATE TABLE IF NOT EXISTS `AI_ResultDeliveries` (
  `id`                  INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  `dedup_key`           CHAR(40)     NOT NULL,
  `source`              ENUM('auto','queue') NOT NULL,
  `imei`                VARCHAR(32)  NOT NULL DEFAULT '',
  `count_date`          DATE NOT NULL,
  `owner`               VARCHAR(255) NOT NULL DEFAULT '',
  `queue_id`            INT NULL COMMENT 'запрос, завершивший день (source=queue)',
  `cameras`             JSON NOT NULL,
  `version`             INT UNSIGNED NULL,
  `recipient_emails`    JSON NULL,
  `skipped_emails`      JSON NULL,
  `status`              ENUM('pending','sending','sent','failed','cancelled','no_recipient')
                          NOT NULL DEFAULT 'pending',
  `attempts`            TINYINT UNSIGNED NOT NULL DEFAULT 0,
  `next_attempt_at`     INT NOT NULL,
  `last_error`          TEXT NULL,
  `provider_message_id` VARCHAR(128) NULL,
  `sent_at`             INT NULL,
  `created_at`          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  `updated_at`          TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY `uq_dedup` (`dedup_key`),
  KEY `idx_due` (`status`, `next_attempt_at`),
  KEY `idx_imei_date_owner` (`imei`, `count_date`, `owner`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
