# Эксплуатация

## Ежедневный минимум для человека

Прочитать утреннюю сводку в Telegram и ответить на запросы подтверждения. Всё.
Если требуется больше — это дефект контура, а не нормальная работа.

## Регулярные задачи

| Задача | Расписание | Что делает |
|---|---|---|
| `dispatcher` | каждые 2 мин | Запуск/вытеснение/завершение прогонов. Модель не участвует |
| `research-loop` | каждые 25 мин | Одна фаза `/dr` |
| `daily-digest` | 09:00 | Сводка в Telegram |
| `weekly-recalib` | вс 20:00 | Калибровка весов + недельный отчёт |
| `hygiene` | 03:30 | Зависшие прогоны, ротация логов, сжатие базы |

Список и статус: `hermes cron list`. Отключить: `hermes cron disable <имя>`.

**Важно про пути скриптов:** Hermes требует относительный путь к `~/.hermes/scripts/`
(резолвится как `HERMES_HOME/scripts/`, где `HERMES_HOME` = профиль, например
`/Users/.../.hermes/profiles/researchagen`). Наши 5 заданий используют `command`
(`python tools/rg.py ...`), а не `script`, поэтому относительный путь не нужен.
`research-loop` — agent-джоб (без скрипта). Если создаёте свой `script`-джоб,
передавайте только имя файла, например `cron_dispatcher.sh`, и кладите файл в
`HERMES_HOME/scripts/` (профиль) или `$HOME/.hermes/scripts/` (глобально).
Ошибка "script path must be relative to ~/.hermes/scripts/" означает, что вы
передали абсолютный путь — исправьте на относительный.

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

### Контур молчит сутки

```bash
python tools/rg.py status
python tools/selfcheck.py all
hermes cron list
```

Почти всегда одна из четырёх причин: шлюз не запущен, пауза не снята,
исчерпан суточный бюджет, очередь пуста.

### Регулярные проверки контура

```bash
python tools/rg.py doctor            # среда: токен, изоляция путей, секреты в логах, права .env
python tools/rg.py audit             # 35 анализов функционала: ошибки → reports/audit-<дата>.md
python tools/rg.py priors search "тема"   # prior-art по 6 источникам, планка честности 90%
```

Doctor дополнительно ловит: тот же токен в двух профилях — теперь `WARN`
(допустимо для macOS+Windows, запускай один gateway) и `FAIL` только для
корневого `~/.hermes/.env`; токен в логах/чате; права `.env` ≠ 600 (WARN);
GPU: на macOS debug — `OK`/`WARN` dry-run, на Windows production без
`nvidia-smi` — `FAIL` (реальная ошибка), а шаблонный `config.yaml` с
`<<INSTALLER_>>` — `WARN` "запусти install.sh"; модель: `debug` → `WARN`,
`production` → `FAIL`; governor: onboarding `{'onboarding':...}` → `WARN`,
а не `0/0` unsafe. Аудит проверяет и документацию: каждая команда и флаг из
docs/ обязаны существовать в коде. Зона H (a31–a35) — кроссплатформенность.

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

## Автономия на Windows: планировщик задач

`hermes cron` на Windows недоступен — те же пять заданий ставятся в Task
Scheduler (команды те же, что в `cron/*.json`). Запуск от обычной PowerShell
в каталоге профиля:

```powershell
schtasks /Create /TN "researchagen-dispatcher" /TR "python tools\rg.py tick" /SC MINUTE /MO 2
schtasks /Create /TN "researchagen-research-loop" /TR "hermes run researchagen" /SC MINUTE /MO 25
schtasks /Create /TN "researchagen-digest" /TR "python tools\rg.py digest --send" /SC DAILY /ST 09:00
schtasks /Create /TN "researchagen-hygiene" /TR "python tools\hygiene.py run --max-run-hours 24" /SC DAILY /ST 03:30
schtasks /Create /TN "researchagen-recalib" /TR "cmd /c python tools\rg.py recalib && python tools\rg.py weekly --send" /SC WEEKLY /D SUN /ST 20:00
```

Проверка: `schtasks /Query /TN "researchagen-*"`. GPU опрашивается через
`nvidia-smi` (System32 или NVSMI — профиль найдёт сам). Если PowerShell
выполняется не в каталоге профиля, укажите полные пути в `/TR`.

## Чего не делать

- Не запускать два шлюза на один токен.
- Не редактировать базу вручную на ходу — есть CLI на все переходы.
- Не править прогноз после прогона. Это единственный запрет без исключений.
- Не увеличивать `max_parallel_experiments` выше 1 на одной карте.
