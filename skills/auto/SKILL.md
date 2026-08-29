---
name: auto
description: Автономный режим: самопроверка, запуск research-контура через /goal и диспетчера GPU без участия человека.
version: 1.0.0
---

# /auto — включить автономию

## 1. Самопроверка перед стартом

```bash
python tools/selfcheck.py all
```

Если есть ❌ — НЕ запускай контур. Отправь в Telegram список ошибок и остановись:
автономный агент без связи и без GPU — самый дорогой вид шума.

## 2. Два контура и один governor

**Контур А — диспетчер (без модели, каждые 2 минуты, cron `dispatcher`).**
Детерминированный Python: берёт NEXT, проверяет kill-stage, governor lease,
VRAM, бюджет и запускает ровно один эксперимент. Ничего не решает на глазок —
поэтому может работать вечно.

**Контур B — исследование (с моделью, каждые 25 минут, cron `research-loop`).**
Одна фаза `/dr` за тик. В `discover` parent сам выбирает bounded fan-out после
`governor plan`; в `testing/analyze` cron автоматически паузится, а новые Qwen
calls не допускаются. Длинные сессии запрещены: состояние живёт в базе и в карточках,
а не в контексте модели.

**Governor — общий admission ledger.**
SQLite leases сериализуют experiment и research. Перед запуском эксперимента
активные research leases должны стать `paused/stopped/released`; после
`finish` остаётся `analyze` до `/v`, затем parent переводит контур обратно в
`discover`. Удалённый/обходящий lease worker считается неуправляемым и не
разрешается в production.

Проверить регистрацию: `hermes cron list`. Если заданий нет — скажи человеку запустить
установщик повторно (`install.ps1` / `install.sh` регистрируют их из `cron/*.json`).

## 3. Ручной автономный спринт (когда нужно сейчас)

В терминале профиля:

```
/goal gate add python tools/selfcheck.py all
/goal gate add python tools/hypo.py check $ACTIVE
/goal Довести очередь до трёх гипотез, прошедших kill-стадию, и закрыть вердиктом все гипотезы в статусе checkpoint
```

Контракт завершения формулируй так, чтобы его можно было проверить командой:

- outcome: в `tools/queue.py stats` поле `gate_passed` ≥ 3;
- verification: `python tools/queue.py stats --json`;
- constraints: без новых GPU-прогонов сверх суточного бюджета;
- stop_when: гейт пройден или исчерпан `goals.max_turns`.

Альтернатива без жёсткой цели — self-paced цикл: `/loop --until очередь не пуста`.
Интервал сам растёт с 1 минуты до 15, если работы нет.

## 4. Границы автономии (жёсткие)

Сам без спроса: искать сигналы, собирать гипотезы, снимать их, запускать L0/L1,
писать вердикты, ротировать логи, менять порядок очереди, выбирать число
research workers в пределах governor capacity.

Перед любым fan-out parent обязан получить JSON-план и leases:

```bash
python tools/rg.py governor plan --mode auto --json
python tools/rg.py governor leases --json
```

Не считать child summary доказательством. Использовать `governor report`, затем
проверять evidence и вручную через существующий scientific pipeline продвигать
только валидированные гипотезы.

Только с подтверждением в Telegram: прогон дороже `researchagen.limits.approval_gpu_hours`,
смена `FOCUS.md`, установка любых внешних библиотек, публикация наружу,
удаление результатов.

Никогда: не трогать чужой профиль и его токен, не поднимать второй gateway своего
профиля, не переписывать прогноз после результата.
