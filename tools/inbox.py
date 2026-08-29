#!/usr/bin/env python3
"""researchagen — входящие лиды от человека.

Смысл: человек — не начальник очереди, а источник сырья. Скинутая из Telegram
идея/ссылка падает в inbox СЫРОЙ и НЕ получает приоритет автоматически.
Агент на следующем тике сам проверяет её через kill-стадию и либо превращает в
гипотезу с карточкой, либо снимает с обоснованием. Так человек не может случайно
сломать ранжирование, но его идеи гарантированно не теряются.

CLI:
  python tools/inbox.py add "текст или ссылка" [--from telegram] [--json]
  python tools/inbox.py list [--all] [--json]
  python tools/inbox.py take <inbox-id> --title "..." [--signals 3] [--hours 4] ...
  python tools/inbox.py drop <inbox-id> --why "..."
"""

from __future__ import annotations

import json
import os
import sys

import core
import hypo

INBOX_PATH = os.path.join(core.INBOX_DIR, "inbox.jsonl")


def _load() -> list[dict]:
    if not os.path.exists(INBOX_PATH):
        return []
    items = []
    with open(INBOX_PATH, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    items.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return items


def _save(items: list[dict]) -> None:
    core.ensure_dirs()
    with open(INBOX_PATH, "w", encoding="utf-8") as fh:
        for item in items:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")


def add(text: str, source: str = "human") -> dict:
    items = _load()
    item = {"id": f"IN-{len(items) + 1:03d}", "text": text.strip(), "source": source,
            "state": "new", "created_at": core.iso()}
    items.append(item)
    _save(items)
    conn = core.db()
    core.log_event(conn, "inbox.add", None, inbox_id=item["id"], source=source)
    return item


def take(inbox_id: str, fields: dict) -> dict:
    items = _load()
    target = next((i for i in items if i["id"] == inbox_id), None)
    if target is None:
        core.fail(f"{inbox_id} не найден")
    if target["state"] != "new":
        core.fail(f"{inbox_id}: уже обработан ({target['state']})")
    conn = core.db()
    created = hypo.create(conn, fields.get("title") or target["text"][:90],
                          {**fields, "source": f"inbox:{inbox_id}"})
    target["state"] = "promoted"
    target["hypo_id"] = created["id"]
    target["handled_at"] = core.iso()
    _save(items)
    return {"inbox_id": inbox_id, **created}


def drop(inbox_id: str, why: str) -> dict:
    items = _load()
    target = next((i for i in items if i["id"] == inbox_id), None)
    if target is None:
        core.fail(f"{inbox_id} не найден")
    if not why.strip():
        core.fail("нужна причина --why: идея человека не снимается без объяснения")
    target["state"] = "dropped"
    target["why"] = why.strip()
    target["handled_at"] = core.iso()
    _save(items)
    conn = core.db()
    core.log_event(conn, "inbox.drop", None, inbox_id=inbox_id, why=why)
    lesson = os.path.join(core.MEMORY_DIR, "dropped-leads.md")
    core.ensure_dirs()
    with open(lesson, "a", encoding="utf-8") as fh:
        fh.write(f"\n## {inbox_id} ({core.iso()[:10]})\nИдея: {target['text']}\n"
                 f"Почему снята: {why.strip()}\n")
    return target


def main(argv: list[str]) -> int:
    core.load_env()
    as_json = core.wants_json(argv)
    cmd = argv[1] if len(argv) > 1 else "list"

    if cmd == "add":
        text = argv[2] if len(argv) > 2 else core.fail("нужен текст")
        item = add(text, core.arg(argv, "from", "human"))
        core.emit(item, as_json,
                  f"{item['id']} принято в inbox. Приоритет НЕ присвоен: агент проверит "
                  "через kill-стадию на ближайшем тике и ответит решением.")
        return 0

    if cmd == "list":
        items = _load()
        if not core.flag(argv, "all"):
            items = [i for i in items if i["state"] == "new"]
        text = core.table([[i["id"], i["state"], i.get("hypo_id", ""),
                            i["text"][:60]] for i in items],
                          ["id", "статус", "гипотеза", "текст"]) if items else "Inbox пуст."
        core.emit(items, as_json, text)
        return 0

    if cmd == "take":
        inbox_id = argv[2] if len(argv) > 2 else core.fail("нужен inbox-id")
        fields = hypo.fields_from_args(argv)
        res = take(inbox_id, fields)
        core.emit(res, as_json,
                  f"{inbox_id} → {res['id']}. Карточка: {res['card_path']}. "
                  f"Гейт ещё не пройден — заполни секции и запусти check.")
        return 0

    if cmd == "drop":
        inbox_id = argv[2] if len(argv) > 2 else core.fail("нужен inbox-id")
        res = drop(inbox_id, core.arg(argv, "why", ""))
        core.emit(res, as_json, f"{inbox_id} снят. Причина записана в memory/dropped-leads.md")
        return 0

    core.fail(f"неизвестная команда {cmd!r}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
