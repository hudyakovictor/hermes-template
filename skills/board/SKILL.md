---
name: board
description: Отразить очередь гипотез в канбане Hermes для визуального контроля статусов.
version: 1.0.0
---

# /board — канбан

```bash
python tools/board.py sync
python tools/board.py show
hermes kanban list
```

## Зачем это нужно

Истина живёт в SQLite профиля. Канбан — только **одностороннее зеркало** для глаза
человека. Никогда не читай статус из канбана для принятия решений — два источника
правды расходятся всегда.

## Карта статусов

| Гипотеза | Канбан |
|---|---|
| queued | ready |
| running | running |
| paused_checkpoint | review |
| blocked | blocked |
| confirmed / partial | done |
| rejected / killed / archived | archived |

Если `hermes` не найден в PATH — синхронизация молча пропускается: исследование
не должно падать из-за отсутствия украшения.
