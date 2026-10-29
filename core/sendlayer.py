#!/usr/bin/env python3
"""Тонкий клиент SendLayer API (POST /email) — как в PassengersFlowCounting.

https://developers.sendlayer.com/api-reference/endpoint/email
Адрес отправителя должен быть на верифицированном в SendLayer домене
(mail.vcpserver.net.ua). Лимит SendLayer: 10 МБ на письмо вместе с вложениями.
"""

from __future__ import annotations

import base64
import json
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from functools import lru_cache
from typing import List, Optional

_MAX_ERROR_BODY = 1000


@lru_cache
def _ssl_context() -> ssl.SSLContext:
    # В venv на сервере может не быть системного хранилища сертификатов
    # (CERTIFICATE_VERIFY_FAILED) — берём корневые из certifi, если он есть.
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


class SendLayerError(Exception):
    """Ошибка отправки. retryable=False — повтор не поможет (4xx, кроме 429)."""

    def __init__(self, message: str, *, status: Optional[int] = None,
                 retryable: bool = True):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


@dataclass
class Attachment:
    filename: str
    content: bytes
    mime_type: str


def _addresses(emails: List[str]) -> List[dict]:
    return [{"email": e} for e in emails]


def send_email(cfg, *, to: List[str], subject: str, html: str, text: str = "",
               attachments: Optional[List[Attachment]] = None,
               bcc: Optional[List[str]] = None, tags: Optional[List[str]] = None) -> str:
    """Отправляет письмо, возвращает MessageID SendLayer."""
    if not cfg.mail_api_key:
        raise SendLayerError("mail.sendlayer_api_key не задан", retryable=False)
    if not to:
        raise SendLayerError("Порожній список отримувачів", retryable=False)

    payload: dict = {
        "From": {"name": cfg.mail_from_name, "email": cfg.mail_from_email},
        "To": _addresses(to),
        "Subject": subject,
        "ContentType": "HTML",
        "HTMLContent": html,
    }
    if text:
        payload["PlainContent"] = text
    if bcc:
        payload["BCC"] = _addresses(bcc)
    if cfg.mail_reply_to:
        payload["ReplyTo"] = _addresses([cfg.mail_reply_to])
    if tags:
        payload["Tags"] = tags
    if attachments:
        payload["Attachments"] = [
            {
                "Filename": a.filename,
                "Content": base64.b64encode(a.content).decode("ascii"),
                "Type": a.mime_type,
                "Disposition": "attachment",
            }
            for a in attachments
        ]

    request = urllib.request.Request(
        cfg.mail_api_url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {cfg.mail_api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=cfg.mail_timeout_sec,
                                    context=_ssl_context()) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:_MAX_ERROR_BODY]
        retryable = exc.code == 429 or exc.code >= 500
        raise SendLayerError(f"SendLayer HTTP {exc.code}: {detail}",
                             status=exc.code, retryable=retryable) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SendLayerError(f"SendLayer недоступний: {exc}") from exc

    try:
        message_id = (json.loads(body) or {}).get("MessageID")
    except (json.JSONDecodeError, AttributeError):
        message_id = None
    return str(message_id or "")
