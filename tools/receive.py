#!/usr/bin/env python3
"""Одноразовый приёмник файла по HTTP — обходной путь, когда ssh недоступен.

    python3 tools/receive.py .local/running-config.txt

Слушает 0.0.0.0:9000, принимает один POST/PUT, сохраняет тело и завершается.
"""

from __future__ import annotations

import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

TARGET = Path(sys.argv[1] if len(sys.argv) > 1 else ".local/received.txt")
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 9000


class Receiver(BaseHTTPRequestHandler):
    def _store(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else self.rfile.read()
        TARGET.parent.mkdir(parents=True, exist_ok=True)
        TARGET.write_bytes(body)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok\n")
        print(f"принято {len(body)} байт -> {TARGET}", flush=True)
        self.server.received = True

    do_POST = _store
    do_PUT = _store

    def log_message(self, *args) -> None:  # тише в консоли
        pass


server = HTTPServer(("0.0.0.0", PORT), Receiver)
server.received = False
print(f"жду файл на порту {PORT} ...", flush=True)
while not server.received:
    server.handle_request()
