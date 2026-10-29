#!/usr/bin/env python3
"""http_router.py — HTTP-роутер к tools (заменяет InquieryRedirect.py).

Принимает POST с ?pass=..., читает JSON-тело, вызывает соответствующий
tool-скрипт как subprocess (stdin=тело, stdout→ответ).

Роуты:
    /saveNewReq      → enqueue_request.py   (запись в AI_CountingQueue)
    /RemoveRequest   → remove_request.py    (удаление из очереди / AI_StopsReqs)
    /ReorderQueue    → reorder_queue.py     (порядок pending-задач)
    /ShowingNeyroWork→ show_status.py        (статус очереди + лог менеджера)
    /CamLogs         → cam_logs.py          (список камер / лог по камере)
    /getWaitingList  → show_status.py        (совместимость с revenue3.php)

PORT, PASSWORD, ALLOWED_IPS читаются из окружения с дефолтами.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlparse

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS_DIR = os.path.join(PROJECT_ROOT, "tools")

PORT = int(os.environ.get("NEYRO_ROUTER_PORT", "8080"))
PASSWORD = os.environ.get("NEYRO_ROUTER_PASS", "13092008Zz")
PYTHON = os.environ.get("NEYRO_PYTHON", "/ultralytics-env/bin/python")
LOG_FILE = os.environ.get("NEYRO_ROUTER_LOG", "/tmp/NEYRO_LOGS/router.log")
ALLOWED_IPS = set(
    filter(None, os.environ.get(
        "NEYRO_ROUTER_IPS", "138.201.226.159,127.0.0.1,::1").split(",")))
READ_TIMEOUT = int(os.environ.get("NEYRO_ROUTER_TIMEOUT", "15"))

ROUTES = {
    "/saveNewReq":       os.path.join(TOOLS_DIR, "enqueue_request.py"),
    "/RemoveRequest":    os.path.join(TOOLS_DIR, "remove_request.py"),
    "/ReorderQueue":     os.path.join(TOOLS_DIR, "reorder_queue.py"),
    "/ShowingNeyroWork": os.path.join(TOOLS_DIR, "show_status.py"),
    "/CamLogs":          os.path.join(TOOLS_DIR, "cam_logs.py"),
    "/getWaitingList":   os.path.join(TOOLS_DIR, "get_waiting_list.py"),
}

os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(filename=LOG_FILE, level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("router")
_file_lock = threading.Lock()


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Handler(BaseHTTPRequestHandler):
    def _headers(self, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, fmt, *args):
        log.info("%s - %s", self.client_address[0], fmt % args)

    def _check_ip(self) -> bool:
        if ALLOWED_IPS and self.client_address[0] not in ALLOWED_IPS:
            self._headers(403)
            self.wfile.write(b'{"error":"Forbidden IP"}')
            return False
        return True

    def do_OPTIONS(self):
        self._headers(200)

    def do_POST(self):
        try:
            if not self._check_ip():
                return
            parsed = urlparse(self.path)
            route = parsed.path
            query = parse_qs(parsed.query)
            if query.get("pass", [""])[0] != PASSWORD:
                self._headers(403)
                self.wfile.write(b'{"error":"Unauthorized"}')
                return
            if route not in ROUTES:
                self._headers(404)
                self.wfile.write(b'{"error":"Not Found"}')
                return

            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b""
            try:
                json.loads(body) if body else {}
            except json.JSONDecodeError:
                self._headers(400)
                self.wfile.write(b'{"error":"Invalid JSON"}')
                return

            script = ROUTES[route]
            try:
                result = subprocess.run(
                    [PYTHON, script], input=body,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    timeout=READ_TIMEOUT)
                if result.returncode != 0:
                    raise RuntimeError(result.stderr.decode(errors="ignore"))
                output = result.stdout.decode(errors="ignore").strip()
            except Exception as e:
                log.exception("Handler error in %s", script)
                self._headers(500)
                self.wfile.write(json.dumps({"error": str(e)}).encode())
                return

            self._headers(200)
            self.wfile.write(output.encode())
        except Exception:
            log.exception("Unhandled error")
            try:
                self._headers(500)
                self.wfile.write(b'{"error":"Internal Server Error"}')
            except Exception:
                pass


def run():
    server = ThreadedHTTPServer(("", PORT), Handler)
    log.info("Router started on port %d", PORT)
    print(f"Router started on port {PORT}", file=sys.stderr)
    server.serve_forever()


if __name__ == "__main__":
    run()
