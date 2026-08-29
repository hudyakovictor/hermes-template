# Эксплуатация

## Ежедневный минимум для человека

Прочитать утреннюю сводку в Telegram и ответить на запросы подтверждения. Всё.
Если требуется больше — это дефект контура, а не нормальная работа.

## Регулярные задачи

| Задача | Расписание | Что делает |
|---|---|---|
| `dispatcher` | каждые 2 мин | Запуск/вытеснение/завершение прогонов. Модель не участвует |
| `research-loop` | каждые 25 мин | Одна фаза `/dr` и при необходимости bounded Conclave review |
| `conclave-watch` | каждые 10 мин | CPU-only heartbeat/transcript hint, throttled Telegram delivery |
| `daily-digest` | 09:00 | Сводка в Telegram |
| `weekly-recalib` | вс 20:00 | Калибровка весов + недельный отчёт |
| `hygiene` | 03:30 | Зависшие прогоны, ротация логов, сжатие базы |

Список и статус: `hermes cron list`. Отключить: `hermes cron disable <имя>`.

Если установщик не нашёл `hermes`, задачи не зарегистрированы. Добавьте вручную,
взяв расписание и команды из файлов `cron/*.json`, и обязательно укажите
`--workdir` на каталог профиля.

## Сценарии

### Мне нужна карта для своих задач

`/pause` — новые запуски остановлены, текущий прогон доходит до конца.
Если нужно сейчас — `/preempt`, который ставит флаг чекпойнта и даёт раннеру
корректно сохранить частичные результаты. `/resume` — вернуть как было.

Для управления research/Qwen admission смотри `/governor`:

```bash
python tools/rg.py governor status --json
python tools/rg.py governor mode testing
python tools/rg.py governor mode discover
```

`testing` ставит research cron на паузу, закрывает новые leases и ждёт
checkpoint активных workers. `/v` после `finish` возвращает режим в `discover`;
один `/pause` диспетчера сам по себе research workers не убивает.

### Спорная гипотеза и живая переписка

Parent делает конфликтную ситуацию явной, но спор не запускается ради количества
сообщений:

```bash
cat > reports/context.json <<'JSON'
{"stage":"critique","source_conflict":true,"estimated_gpu_hours":6,
 "reports":[{"confidence":0.88},{"confidence":0.41}]}
JSON
python tools/rg.py conclave open --task-id H-012 --title "H-012: mechanism review" \\
  --stage critique --context-file reports/context.json --json
python tools/rg.py conclave assign --session D-... --json
python tools/rg.py conclave brief --session D-... --assignment A-... --json
```

Поля brief разделены: English internal protocol для child и короткий Russian
public voice для Telegram. После каждого child report parent может публиковать:

```bash
python tools/rg.py conclave speak --session D-... --assignment A-... \\
  --kind critique --round 1 --task "Контроль не пережил удар." \\
  --client "Заказчик опять просит кнопку бабло; считаем counterfactual."
python tools/rg.py conclave transcript --session D-... --send
```

`conclave-watch` только наблюдает открытые комнаты и помечает stall; он не тратит
Qwen и не меняет queue. Закрой комнату через `conclave close`, затем отдельно
применяй `hypo.py`, kill-stage и `/v`. Нудж с 95/90 prior размечай `conclave outcome`;
не называй prior измеренной эффективностью.

### Контур молчит сутки

```bash
python tools/rg.py status
python tools/selfcheck.py all
hermes cron list
```

Почти всегда одна из четырёх причин: шлюз не запущен, пауза не снята,
исчерпан суточный бюджет, очередь пуста.

### Прогон висит

`hygiene` сам пометит его `failed` после 24 ч или если PID мёртв. Срочно:

```bash
python tools/hygiene.py run --max-run-hours 2
```

### Гипотеза висит в состоянии `paused_checkpoint`

Значит, есть результаты без вердикта — самый быстро обесценивающийся актив.
`/v H-XXX` закрывает его.

### Обновление профиля

```bash
hermes profile update researchagen
```

`config.yaml` и `.env` сохраняются. База, логи и результаты не трогаются.

## Резервное копирование

Ценное — три вещи: `state/researchagen.sqlite3`, `hypotheses/`, `results/`.
Остальное восстанавливается из репозитория. Копируйте базу, когда ни один
прогон не идёт (`rg.py running` пуст).

## Чего не делать

- Не запускать два шлюза на один токен.
- Не редактировать базу вручную на ходу — есть CLI на все переходы.
- Не править прогноз после прогона. Это единственный запрет без исключений.
- Не увеличивать `max_parallel_experiments` выше 1 на одной карте.
