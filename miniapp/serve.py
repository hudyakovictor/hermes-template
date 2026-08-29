#!/usr/bin/env python3
"""researchagen Mini App — раздача статики (только stdlib).

Запуск:
    python miniapp/serve.py [--port 8899] [--host 0.0.0.0]

Что делает:
  * раздаёт miniapp/index.html и miniapp/static/*;
  * GET /api/ping  — живой ли сервер;
  * GET /api/state — если рядом есть реальный state/researchagen.sqlite3,
    отдаёт лёгкую сводку (счётчики очереди/вердиктов), иначе {"demo": true}.
    Фронтенд работает и без этого эндпоинта — он умеет жить на симуляции.

Для Telegram Mini App нужен HTTPS. Локально проще всего поднять туннель:
    cloudflared tunnel --url http://localhost:8899
    # или: ngrok http 8899
и полученный URL указать BotFather → /newapp (или Menu Button кнопки).
"""
from __future__ import annotations

import argparse
import json
import os
import posixpath
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_DB = os.path.join(ROOT, os.pardir, "state", "researchagen.sqlite3")

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".json": "application/json; charset=utf-8",
    ".ico": "image/x-icon",
}


def db_summary() -> dict | None:
    """Мягкое чтение реального состояния: нет базы — нет проблем."""
    if not os.path.exists(STATE_DB):
        return None
    try:
        conn = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True, timeout=1.0)
        cur = conn.cursor()
        out: dict = {"demo": False}
        for key, sql in {
            "queue": "SELECT COUNT(*) FROM hypotheses WHERE status IN ('queued')",
            "running": "SELECT COUNT(*) FROM hypotheses WHERE status IN ('running','paused_checkpoint')",
            "verdicts": "SELECT COUNT(*) FROM verdicts",
        }.items():
            try:
                out[key] = cur.execute(sql).fetchone()[0]
            except sqlite3.Error:
                pass
        conn.close()
        return out if len(out) > 1 else None
    except sqlite3.Error:
        return None


class Handler(BaseHTTPRequestHandler):
    server_version = "researchagen-miniapp/1.0"

    def log_message(self, fmt, *args):  # тише в консоли
        sys.stderr.write("· %s %s\n" % (self.command if hasattr(self, "command") else "-", fmt % args))

    def _send(self, code: int, body: bytes, ctype: str, cache: bool = False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=60" if cache else "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0].split("#", 1)[0]

        if path == "/api/ping":
            self._send(200, b'{"ok":true}', "application/json; charset=utf-8")
            return
        if path == "/api/state":
            data = db_summary() or {"demo": True}
            self._send(200, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")
            return

        if path in ("/", "/index.html"):
            fp = os.path.join(ROOT, "index.html")
        else:
            fp = os.path.realpath(os.path.join(ROOT, posixpath.normpath(path.lstrip("/"))))
            if not fp.startswith(os.path.realpath(ROOT) + os.sep):
                self._send(403, b"forbidden", "text/plain; charset=utf-8")
                return
        if not os.path.isfile(fp):
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        ext = os.path.splitext(fp)[1].lower()
        with open(fp, "rb") as f:
            body = f.read()
        self._send(200, body, MIME.get(ext, "application/octet-stream"), cache=ext in (".css", ".js", ".svg", ".png"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8899)
    args = ap.parse_args()

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"researchagen Mini App: http://{args.host}:{args.port}")
    print(f"статика: {ROOT}")
    print(f"реальная база: {os.path.abspath(STATE_DB)} ({'есть' if os.path.exists(STATE_DB) else 'нет — демо-режим'})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nостановлено")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
