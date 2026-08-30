# researchagen

Автономный исследовательский профиль для [agent-hermes](https://github.com/NousResearch/hermes-agent).
Ставится рядом с вашим основным агентом и **не трогает его**: свой каталог, свой
Telegram-бот, своя модель, свой терминал.

Установка — на Windows, в несколько минут. Дальше всё работает само: агент
создаёт субагентов, они разбирают миссию из `MISSION.md` и строят гипотезы,
а диспетчер сам запускает эксперименты на GPU и присылает вердикты в Telegram.

---

## Как это работает (коротко)

```
researchagen gateway start   ← одно окно, живёт постоянно
        │
        ├── cron-диспетчер (каждые 2 мин)  → запускает/вытесняет эксперименты на GPU
        ├── cron-исследование (каждые 25 мин) → агент читает MISSION.md,
        │     создаёт субагентов (до 2 за раз, под контролем governor),
        │     они ищут сигналы и возвращают отчёты, агент собирает из них
        │     карточки гипотез с PASS/FAIL-критериями
        └── Telegram-бот → /status, /dr, /kill, дайджесты, вердикты
```

Весь цикл — из файла `MISSION.md`: субагенты дробят его на задачи, находят
сигналы, сводят их в гипотезы (7 обязательных пунктов, включая дешёвый тест и
критерии опровержения), очередь отбирает лучшие по ценности на GPU-час, а
диспетчер выполняет их по каскаду L0 → L1 → L2 → L3. Человек вмешивается
только когда надо: подтвердить дорогой прогон или посмотреть вердикт.

---

## Установка на Windows

### Шаг 0. Три программы (один раз)

| Программа | Ссылка | Важно |
|---|---|---|
| **Python** | [python.org/downloads](https://www.python.org/downloads/) | ⚠️ отметьте галочку **«Add python.exe to PATH»** |
| **Git** | [git-scm.com/download/win](https://git-scm.com/download/win) | все настройки по умолчанию |
| **Ollama** | [ollama.com/download/windows](https://ollama.com/download/windows) | после установки — запустить (иконка в трее) |

Проверка (PowerShell: **Win+R** → `powershell` → Enter):

```powershell
python -V; git --version; ollama --version
```

Должны появиться три строки с версиями. Если какой-то нет — перечитайте таблицу.

Скачайте модель (один раз, ~20 ГБ, занимает время):

```powershell
ollama pull qwen3:27b
```

### Шаг 1. Установка — 3 варианта, выбирайте любой

**Вариант А — одна строка (самый простой):** вставьте в PowerShell и Enter:

```powershell
powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/<ваш-логин>/researchagen/main/setup.ps1 | iex"
```

Всё скачается и настроится само. Если у вас уже есть готовый блок настроек от
владельца — сначала вставьте его, а потом эту строку: вопросы задаваться не будут.

**Вариант Б — двойной клик:**

```powershell
git clone https://github.com/<ваш-логин>/researchagen
cd researchagen
```

Затем просто **дважды кликните `setup.bat`**. Ответьте на 2–4 вопроса
(Enter = подходит). Если готовый `.env` уже существует — вопросов не будет вообще.

**Вариант В — руками (для тех, кто любит терминал):**

```powershell
git clone https://github.com/<ваш-логин>/researchagen
cd researchagen
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

Вопросы: токен бота (обязательно), URL модели (Enter = локальная Ollama),
имя модели и ключ (Enter = подходит). В конце — `y` и самопроверка:

```
  Готово. Профиль установлен и прошёл проверку.
```

### Шаг 2. Запуск

Откройте новое окно PowerShell и выполните **одну команду** (или дважды кликните
`start.bat` в папке проекта):

```powershell
researchagen gateway start
```

**Это окно не закрывайте** — пока оно открыто, бот и вся автоматика работают.
Закрыли — всё остановилось; откроете снова — всё продолжится, ничего не теряется.

### Шаг 3. Проверка

В Telegram напишите боту:

```
/status
```

В ответ — картина состояния: платформа, модель, GPU, очередь гипотез.
Всё, установка закончена.

---

## Частые проблемы

| Симптом | Решение |
|---|---|
| «python не является внутренней или внешней командой» | Переустановите Python с галочкой **«Add python.exe to PATH»**, откройте **новое** окно PowerShell |
| «git не найден» | Установите Git с [git-scm.com](https://git-scm.com/download/win) |
| «ollama не найден» | Установите и запустите Ollama (иконка в трее), проверьте: `ollama list` |
| «Модель X ещё не скачана» | `ollama pull qwen3:27b` (или то имя, что в `.env`) |
| Бот молчит, в логах `chat_id=0` | Напишите боту `/start`, затем `/status` — бот подскажет, что поправить в `.env` |
| Закрыл окно — бот перестал отвечать | Так и задумано. Откройте новое окно → `researchagen gateway start` |
| «hermes не найден» при установке | Автоматика cron не подключилась. Это значит, agent-hermes не установлен — обратитесь к владельцу |
| Ошибка «token lock» | У профиля должен быть **свой отдельный** бот (создаётся у @BotFather), токен основного агента не подходит |

---

## Команды (шпаргалка)

| Что | Команды |
|---|---|
| Состояние | `/status` |
| Исследование | `/dr` `/mine` `/h` `/kill` `/bottom` |
| Исполнение | `/pool` `/next` `/launch` `/preempt` `/v` |
| Управление | `/auto` `/governor` `/panel` `/digest` `/gpu` `/calib` `/patent` `/add` `/board` `/doctor` |
| Экипаж | `/aichat` — переписка агентов-исследователей |

Без Telegram то же самое: `python tools/rg.py status`, `python tools/rg.py tick`.

---

## Для владельца

- Полный опрос установщика (6 шагов): `powershell -ExecutionPolicy Bypass -File .\install.ps1 -Full`
- Обновление у друга: `cd researchagen; git pull; powershell -ExecutionPolicy Bypass -File .\install.ps1 -NonInteractive`
  (`.env` и база состояния сохраняются, чужие строки в `.env` не трогаются)
- `.env` **никогда не коммитьте** — он в `.gitignore`, установщик пишет его с правами только для текущего пользователя
- Структура: `MISSION.md` (миссия → гипотезы), `SOUL.md` / `.hermes.md` / `FOCUS.md` (характер и правила),
  `tools/` (контур, stdlib-only), `skills/` (слеш-команды), `cron/` (5 задач автономии),
  `miniapp/` (Telegram Mini App — пульт лаборатории), `docs/` (архитектура, эксплуатация)
- Подробно: [docs/INSTALL-windows.md](docs/INSTALL-windows.md), [docs/WINDOWS.md](docs/WINDOWS.md),
  [docs/OPERATIONS.md](docs/OPERATIONS.md), [docs/TELEGRAM.md](docs/TELEGRAM.md), [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

Лицензия: MIT.
