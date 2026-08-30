# researchagen

Автономный исследовательский профиль для [agent-hermes](https://github.com/NousResearch/hermes-agent).
Ищет и доказывает скрытые механизмы в training dynamics: можно ли выделить полезную
логику раньше и дешевле, чем при полном обучении, отделив её от паразитной памяти
и шумового переобучения.

Это **второй профиль**, который ставится рядом с вашим основным агентом и никак его не
трогает: свой каталог, свой Telegram-токен, своя модель, свой терминал.

## Зачем он вообще нужен

На одной RTX 5090 главный дефицит — не идеи, а GPU-часы. Поэтому весь контур
построен вокруг одного числа — **PPI = ценность гипотезы на GPU-час** — и вокруг привычки
убивать гипотезу до того, как она потратит часы. Убитая за десять минут идея здесь
считается успешным результатом, а не неудачей.

## Ключевые свойства

| Свойство | Как сделано |
|---|---|
| Полная автономия | Диспетчер GPU (cron 2 мин), исследовательские тики (cron 25 мин) и общий stdlib governor с SQLite leases |
| Решения о fan-out/GPU принимает код | Dynamic capacity, VRAM/utilization, pause/resume, суточный budget и experiment lock — в `tools/governor.py`/`tools/dispatch.py`, а не только в промпте |
| Ни одного внешнего пакета | Только Python stdlib, `sh` и PowerShell 5.1 |
| Телеметрия и управление в Telegram | Управление — штатный шлюз Hermes; телеметрия — отправка только через Bot API (второго long-polling нет) |
| Mini App — пульт лаборатории | `miniapp/`: 6 зон (пульт GPU, конвейер гипотез, live-графики, экипаж со ставками, подача идей, вердикты) на vanilla JS; stdlib-сервер с демо-симуляцией и live-адаптером к `tools/*` |
| Рабочий чат экипажа (aichat) | `tools/crew.py`: Boss, Скиф, Аналитег, Морг, Гайка, Хроник, iВасёк — обсуждение работы, споры и взаимное ревью косяков, потенциал гипотез и патентов; ~5% реплик про заказчика и «мессию AGI», ~2% «кек/лол»; 0 GPU-ч, 0 токенов (`/aichat`) |
| Два пользователя — одна картина | Единая SQLite в `state/`, любой `/status` читает одни и те же факты |
| Защита от самообмана | Прогноз фиксируется ДО прогона; вердикт без прогноза невозможен; еженедельная калибровка весов |
| Граница двух агентов | `selfcheck.py` проверяет, что токен не совпадает с токеном соседнего профиля |

## Установка

### Вариант 1 — штатный механизм Hermes (рекомендуется)

```bash
hermes profile install github.com/<ваш-логин>/researchagen --alias
```

Затем в каталоге профиля запустите установщик, чтобы заполнить токен, модель и лимиты.

### Вариант 0 — всё уже установлено, только токен (in-place, без Hermes)

Проект уже содержит все инструменты, скиллы, cron, конфиг. Достаточно токена:

```powershell
git clone https://github.com/<ваш-логин>/researchagen
cd researchagen
# создай .env из примера и вставь токен
copy .env.EXAMPLE .env
# отредактируй .env: TELEGRAM_BOT_TOKEN=123:abc
# (chat_id/user_id определятся авто через getUpdates, остальное — дефолты Ollama)
python tools/selfcheck.py all
python tools/rg.py status
python tools/rg.py audit --no-coverage  # 80/80
```

Или одной командой:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1 -BotToken "123:abc" -InPlace -NonInteractive
```

На macOS/Linux:

```bash
sh install.sh --in-place --token=123:abc
```

В этом режиме ничего не копируется в `~/.hermes/profiles/` — всё работает прямо из клона. Cron можно запускать вручную `python tools/rg.py tick` или через `researchagen gateway start` если Hermes установлен.

### Вариант 2 — прямо из терминала (профиль Hermes)

Windows (PowerShell) — быстрый режим (только токен и API):

```powershell
git clone https://github.com/<ваш-логин>/researchagen
cd researchagen
powershell -ExecutionPolicy Bypass -File .\install.ps1
# спросит только TELEGRAM_BOT_TOKEN и модель API (Enter = Ollama локально)
# остальное авто: windows/production 2/1, лимиты, chat_id из getUpdates

# неинтерактивно:
powershell -ExecutionPolicy Bypass -File .\install.ps1 -BotToken "123:abc" -NonInteractive

# полный режим (6 шагов):
powershell -ExecutionPolicy Bypass -File .\install.ps1 -Full
```

macOS / Linux:

```bash
git clone https://github.com/<ваш-логин>/researchagen
cd researchagen
sh install.sh
```

Установщик спросит: ОС, корень Hermes, токен нового бота, chat_id и тему, id двух
пользователей, адрес Ollama и имя модели, лимиты GPU. После записи сам прогоняет
самопроверку и говорит, что именно сломано.

**Двухплатформенность (Windows прод + macOS debug).** Один и тот же токен может
жить в двух профилях — это штатно (см. `INSTALL-macos.md`): на macOS контур
работает в `debug` (dry-run без GPU), на Windows — в `production`. `selfcheck.py`
различает: коллизия токена в двух профилях — `WARN` («запускай только один gateway
за раз»), а токен в корневом `~/.hermes/.env` — `FAIL`. Аналогично `GPU` и
`локальная модель` на macOS — `WARN`/`OK` (dry-run доступен), а на Windows —
`FAIL` только когда `config.yaml` уже установлен (шаблон `<<INSTALLER_>>` → `WARN`
"запусти install.sh"). Логи не содержат `nvidia-smi` ошибок как `FAIL` на Windows
когда карта есть — `gpu.snapshot()` возвращает `OK` с `RTX 5090: свободно XX GB`.

**Обновление вручную.** Не используй `rsync --exclude=config.yaml --exclude=.env`:
он сохраняет onboarding-конфиг `{'onboarding':{'seen':...}}` с капсами `0/0` и
пустой моделью, из-за чего `governor` падает в `unsafe cap` и `selfcheck` даёт
`FAIL`. Правильно — `hermes profile update` или `git pull` + `sh install.sh` /
`install.ps1`, который перезапишет `config.yaml` из шаблона с капсами `2/1`.
На Windows после `install.ps1` с GPU: `selfcheck` → `GPU OK`, `governor OK`,
`model OK` (если Ollama/qwen3:27b) — контур rock.

**Cron:** 5 заданий (`dispatcher`, `research-loop`, `daily-digest`, `weekly-recalib`,
`hygiene`) используют `command` (`python tools/rg.py ...`), а не `script`. Если
создаёшь свой `script`-джоб, путь должен быть относительным к
`~/.hermes/scripts/` (резолвится как `HERMES_HOME/scripts/`, где `HERMES_HOME` =
`/Users/.../.hermes/profiles/researchagen`). Ошибка "script path must be relative
to ~/.hermes/scripts/" — передай только имя файла, например `cron_dispatcher.sh`,
и положи файл в `HERMES_HOME/scripts/`.

Подробно: [docs/WINDOWS.md](docs/WINDOWS.md) — полный deep-research с нуля (80 анализов), [docs/INSTALL-windows.md](docs/INSTALL-windows.md), [docs/INSTALL-macos.md](docs/INSTALL-macos.md), [docs/OPERATIONS.md](docs/OPERATIONS.md).

## Запуск

```bash
researchagen gateway start   # бот + cron в ОТДЕЛЬНОМ терминале
researchagen chat            # ручная сессия, когда нужно вмешаться
```

Ваш основной агент продолжает работать в своём терминале без изменений.

## Команды

Исследование: `/dr` `/mine` `/h` `/kill` `/bottom`
Исполнение: `/pool` `/next` `/launch` `/preempt` `/v`
Управление: `/auto` `/governor` `/panel` `/digest` `/gpu` `/calib` `/patent` `/add` `/board` `/doctor`
Управление и наблюдение: `/panel` — все стадии + пульт; `/aichat` — переписка экипажа

`/bottom` — опциональный гибридный Bottom Detection: дерево регионов,
backtracking, transformations и async evaluators. Он не обходит kill-stage,
очередь, governor или GPU-диспетчер. Подробно: [docs/BOTTOM-DETECTION.md](docs/BOTTOM-DETECTION.md);
дополнительный 30-пунктовый аудит: [docs/BOTTOM-DETECTION-AUDIT.md](docs/BOTTOM-DETECTION-AUDIT.md).
Архитектура bounded delegation и GPU admission описана в [docs/GOVERNOR.md](docs/GOVERNOR.md).

Единая точка входа без модели: `python tools/rg.py <команда>`. Для Bottom Detection:
`python tools/rg.py bottom run --iterations 1`. Перед изменением governor cap используйте
live calibration: `python tools/rg.py benchmark --concurrencies 1,2 --requests-per-level 3`.

## Структура

```
MISSION.md SOUL.md .hermes.md FOCUS.md   — цель, характер, правила, текущий фокус
config.yaml .env.EXAMPLE                 — конфигурация и шаблон секретов
tools/                                   — основной контур, governor/admission + hybrid Bottom Detection (stdlib-only)
skills/                                  — 20 скиллов = слеш-команды
cron/                                    — 5 задач автономного контура
hooks/BOOT.md                            — что делать в начале сессии
miniapp/                                 — Telegram Mini App: пульт, конвейер, графики, экипаж, идеи, вердикты
docs/                                    — архитектура, Telegram, эксплуатация, оценка
tests/                                   — unittest без зависимостей
```

## Mini App (пульт лаборатории)

Текстового бота неудобно читать, когда в живую крутятся графики, очередь и споры
экипажа. Mini App закрывает это: статус за 3 секунды, вмешательство в один тап.

```bash
python3 miniapp/server.py --port 8787   # демо-симуляция, если state/ пуст; иначе live
```

Подробности: [miniapp/README.md](miniapp/README.md).

## Где границы честности

- Сам агент не доказывает механизмы — он строит конвейер, который их проверяет дешёво.
- На macOS GPU-прогоны идут в dry-run: это проверка кода, а не научный результат.
- Часть Telegram-возможностей (темы, меню команд) зависит от версии Hermes;
  см. раздел «Что не проверено» в [docs/SCORING.md](docs/SCORING.md).

Лицензия: MIT.
