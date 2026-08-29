#!/usr/bin/env python3
"""researchagen — Telegram-вывод без зависимостей (urllib из stdlib).

Гейтвей Hermes сам доставляет ответы агента и cron-вывод в бота. Этот модуль нужен
для того, что должно прийти ВНЕ хода модели и без трат токенов: карточка прогресса
идущего прогона, аварийный сигнал, файлы с метриками.

Оба пользователя видят один и тот же бот и одни данные: цель доставки — общий
TELEGRAM_HOME_CHANNEL (+ thread_id топика), а не личка конкретного человека.

CLI:
  python tools/tg.py send "текст" [--thread N] [--silent]
  python tools/tg.py file /path/to/metrics.csv [--caption "..."]
  python tools/tg.py progress H-003 --level L1 --pct 40 --note "seed 2/3"
  python tools/tg.py test
"""

from __future__ import annotations

import json
import mimetypes
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

import core

# Собираем базовый URL из частей: так токен никогда не попадает в логи целиком.
TG_HOST = "api.telegram.org"
TIMEOUT = 30


def endpoint(method: str) -> str:
    return "https://" + TG_HOST + "/bot" + _token() + "/" + method


def _token() -> str:
    core.load_env()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        core.fail("TELEGRAM_BOT_TOKEN пуст. Заполни .env (отдельный бот для этого профиля).")
    return token


def _chat() -> str:
    core.load_env()
    chat = os.environ.get("TELEGRAM_HOME_CHANNEL", "").strip()
    if not chat:
        core.fail("TELEGRAM_HOME_CHANNEL пуст — некуда доставлять телеметрию.")
    return chat


def call(method: str, payload: dict) -> dict:
    data = urllib.parse.urlencode(
        {k: v for k, v in payload.items() if v not in (None, "")}
    ).encode()
    req = urllib.request.Request(endpoint(method), data=data)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        core.append_log("telegram.log", f"HTTP {exc.code} {method}: {body[:400]}")
        return {"ok": False, "error": f"HTTP {exc.code}", "body": body[:400]}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        core.append_log("telegram.log", f"NET {method}: {exc}")
        return {"ok": False, "error": str(exc)}


def send(text: str, thread_id: str | None = None, silent: bool = False,
         markdown: bool = True) -> dict:
    thread = thread_id or os.environ.get("TELEGRAM_CRON_THREAD_ID") or None
    chunks = [text[i:i + 3900] for i in range(0, len(text), 3900)] or [""]
    result: dict = {"ok": True, "parts": 0}
    for chunk in chunks:
        result = call("sendMessage", {
            "chat_id": _chat(),
            "message_thread_id": thread,
            "text": chunk,
            "parse_mode": "Markdown" if markdown else None,
            "disable_web_page_preview": "true",
            "disable_notification": "true" if silent else None,
        })
        result["parts"] = result.get("parts", 0) + 1
        if not result.get("ok"):
            break
    return result


def send_file(path: str, caption: str = "", thread_id: str | None = None) -> dict:
    """multipart/form-data вручную — без requests."""
    if not os.path.exists(path):
        core.fail(f"файла нет: {path}")
    boundary = "----researchagen" + uuid.uuid4().hex
    thread = thread_id or os.environ.get("TELEGRAM_CRON_THREAD_ID") or ""
    with open(path, "rb") as fh:
        payload = fh.read()
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    parts: list[bytes] = []

    def field(name: str, value: str) -> None:
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
            f"{value}\r\n".encode()
        )

    field("chat_id", _chat())
    if thread:
        field("message_thread_id", str(thread))
    if caption:
        field("caption", caption[:1000])
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; "
        f"filename=\"{os.path.basename(path)}\"\r\nContent-Type: {mime}\r\n\r\n".encode()
    )
    parts.append(payload)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(parts)
    req = urllib.request.Request(
        endpoint("sendDocument"), data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
        core.append_log("telegram.log", f"sendDocument {path}: {exc}")
        return {"ok": False, "error": str(exc)}


def progress_card(hypo_id: str, level: str, pct: float, note: str = "",
                  extra: dict | None = None) -> str:
    bar_len = 20
    filled = int(max(0.0, min(100.0, float(pct))) / 100 * bar_len)
    bar = "█" * filled + "░" * (bar_len - filled)
    lines = [f"*🧪 {hypo_id} — {level}*", f"`{bar}` {float(pct):.0f}%"]
    if note:
        lines.append(note)
    for key, value in (extra or {}).items():
        lines.append(f"• {key}: {value}")
    lines.append(f"_обновлено {core.iso()}_")
    return "\n".join(lines)


def throttled_progress(conn, hypo_id: str, text: str, config: dict | None = None) -> bool:
    """Не чаще progress_every_seconds — чтобы бот не превратился в шум."""
    every = float(core.cfg("researchagen.telegram.progress_every_seconds", 900, config))
    key = f"tg.last_progress.{hypo_id}"
    last = core.parse_iso(core.setting(conn, key))
    if last and (core.now() - last).total_seconds() < every:
        return False
    send(text, silent=True)
    core.set_setting(conn, key, core.iso())
    return True


def main(argv: list[str]) -> int:
    if argv[1:2] and argv[1] in ("help", "-h", "--help"):
        print(__doc__)
        return 0
    core.load_env()
    as_json = core.wants_json(argv)
    cmd = argv[1] if len(argv) > 1 else "test"

    if cmd == "send":
        text = argv[2] if len(argv) > 2 else core.fail("нужен текст")
        res = send(text, core.arg(argv, "thread"), core.flag(argv, "silent"))
        core.emit(res, as_json, "Отправлено" if res.get("ok") else f"Не отправлено: {res}")
        return 0 if res.get("ok") else 1

    if cmd == "file":
        path = argv[2] if len(argv) > 2 else core.fail("нужен путь")
        res = send_file(path, core.arg(argv, "caption", ""), core.arg(argv, "thread"))
        core.emit(res, as_json, "Файл отправлен" if res.get("ok") else f"Ошибка: {res}")
        return 0 if res.get("ok") else 1

    if cmd == "progress":
        hid = argv[2] if len(argv) > 2 else core.fail("нужен id")
        text = progress_card(hid, core.arg(argv, "level", "L0"),
                             float(core.arg(argv, "pct", 0)), core.arg(argv, "note", ""))
        conn = core.db()
        sent = throttled_progress(conn, hid, text)
        core.emit({"sent": sent, "card": text}, as_json,
                  text if sent else "Пропущено (throttle): карточка была недавно")
        return 0

    if cmd == "test":
        res = call("getMe", {})
        if res.get("ok"):
            who = res["result"]
            text = f"Бот жив: @{who.get('username')} (id {who.get('id')})"
        else:
            text = f"Бот недоступен: {res}"
        core.emit(res, as_json, text)
        return 0 if res.get("ok") else 1

    core.fail(f"неизвестная команда {cmd!r}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
