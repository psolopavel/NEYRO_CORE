-- ============================================================
-- AI_CountingQueue — очередь запросных задач (заменяет req.json)
-- ============================================================
--
-- Контракт params (JSON), из SendingAiRequests.php / saveNewReq:
--   {
--     "main":  [...],          -- регион (опц.)
--     "stops": [...],          -- опц.
--     "type1": "Medium",       -- ModelSize
--     "type2": "0.5",          -- PersonQuality
--     "type5": {...},          -- AdditionalParams (In/Out/...)
--     "type6": {...},          -- countByAlarms + паддинги
--     "save_video": false      -- showProcess
--   }
--
-- Захват задачи воркером (атомарно):
--   UPDATE AI_CountingQueue SET status='processing',
--          worker_id=?, heartbeat=UNIX_TIMESTAMP()
--   WHERE id=? AND status='pending';   -- rowcount=1 => задача наша

CREATE TABLE IF NOT EXISTS `AI_CountingQueue` (
  `id`         INT AUTO_INCREMENT PRIMARY KEY,
  `cam_name`   VARCHAR(255) NOT NULL,
  `date_from`  INT NOT NULL,          -- unix ts (как в req.json)
  `date_to`    INT NULL,
  `params`     JSON NOT NULL,
  `status`     ENUM('pending','processing','done','error') NOT NULL DEFAULT 'pending',
  `queue_priority` INT NOT NULL DEFAULT 0 COMMENT 'Чем больше — тем раньше (только pending)',
  `error_text` TEXT NULL,
  `worker_id`  VARCHAR(64) NULL,
  `heartbeat`  INT NULL,
  `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  `updated_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY `idx_status` (`status`),
  KEY `idx_status_priority` (`status`, `queue_priority`, `id`),
  KEY `idx_cam_date` (`cam_name`, `date_from`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
