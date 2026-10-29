#!/usr/bin/env python3
"""Перенос видео: SSH ls + rsync (скачивание/загрузка) + ffmpeg-сжатие.

Из main.py: list_remote_videos, download_videos, upload_result_video,
compress_video. Креды берутся из Config (storage_*), ничего не хардкодим.
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
from typing import List


def list_remote_videos(cfg, camera: str, date_compact: str,
                       window_start: int, window_end: int) -> List[str]:
    """Удалённые MP4, пересекающиеся с окном [window_start, window_end]."""
    remote_dir = shlex.quote(f"{cfg.storage_base}/{camera}")
    remote_cmd = f"ls {remote_dir}/{date_compact}*.mp4 2>/dev/null"
    cmd = ["ssh", f"{cfg.storage_user}@{cfg.storage_host}", remote_cmd]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        logging.warning("SSH ls timeout для %s", camera)
        return []
    if res.returncode != 0:
        return []

    filtered = []
    for line in res.stdout.strip().splitlines():
        fname = line.split("/")[-1]
        try:
            parts = fname.replace(".mp4", "").split("_")
            if len(parts) < 3:
                continue
            s, e = int(parts[1]), int(parts[2])
            if s < window_end and e > window_start:
                filtered.append(line)
        except Exception:
            pass
    return filtered


def download_videos(cfg, remote_files: List[str], camera: str) -> List[str]:
    """rsync файлов в локальную папку. Вернуть локальные пути."""
    if not remote_files:
        return []
    dest = os.path.join(cfg.download_base, camera)
    os.makedirs(dest, exist_ok=True)
    srcs = sorted(set(
        f"{cfg.storage_user}@{cfg.storage_host}:{p}" for p in remote_files))
    cmd = (["rsync", "-av", "--no-times", "--ignore-existing", "--partial"]
           + srcs + [dest + "/"])
    try:
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       text=True, timeout=1200)
    except subprocess.TimeoutExpired:
        logging.warning("rsync timeout для %s", camera)
    return [os.path.join(dest, os.path.basename(f)) for f in remote_files]


def compress_video(video_path: str, crf: int = 32,
                   preset: str = "veryfast") -> bool:
    """ffmpeg-сжатие с перезаписью оригинала."""
    if not os.path.exists(video_path):
        return False
    tmp_path = video_path + ".tmp.mp4"
    cmd = ["ffmpeg", "-y", "-i", video_path, "-vcodec", "libx264",
           "-crf", str(crf), "-preset", preset, "-acodec", "aac",
           "-b:a", "128k", tmp_path]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, timeout=3600)
        if proc.returncode != 0:
            logging.error("ffmpeg ошибка сжатия %s: %s",
                          video_path, proc.stderr.strip()[-300:])
            _silent_unlink(tmp_path)
            return False
        os.replace(tmp_path, video_path)
        return True
    except subprocess.TimeoutExpired:
        logging.warning("ffmpeg timeout для %s", video_path)
        _silent_unlink(tmp_path)
        return False
    except Exception:
        logging.exception("Ошибка сжатия видео %s", video_path)
        _silent_unlink(tmp_path)
        return False


def upload_result_video(cfg, video_path: str, camera: str,
                        date_str: str) -> bool:
    """Загрузить обработанное видео в {storage_base}/{camera}_neyro/{date}_auto/."""
    if not os.path.exists(video_path):
        return False
    date_compact = date_str.replace("-", "")
    remote_dir = f"{cfg.storage_base}/{camera}_neyro/{date_compact}_auto"

    mkdir_cmd = ["ssh", f"{cfg.storage_user}@{cfg.storage_host}",
                 f"mkdir -p {shlex.quote(remote_dir)}"]
    try:
        subprocess.run(mkdir_cmd, capture_output=True, text=True, timeout=30)
    except Exception:
        logging.warning("Не удалось создать папку на remote: %s", remote_dir)

    remote_dest = f"{cfg.storage_user}@{cfg.storage_host}:{remote_dir}/"
    cmd = ["rsync", "-av", "--partial", video_path, remote_dest]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, timeout=600)
        if proc.returncode == 0:
            return True
        logging.error("rsync upload ошибка для %s: %s",
                      video_path, proc.stderr.strip()[-300:])
        return False
    except subprocess.TimeoutExpired:
        logging.warning("rsync upload timeout для %s", video_path)
        return False
    except Exception:
        logging.exception("Ошибка загрузки видео %s", video_path)
        return False


def _silent_unlink(path: str):
    try:
        os.unlink(path)
    except Exception:
        pass
