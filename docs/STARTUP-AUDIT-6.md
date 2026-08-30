# Аудит стартового пути — итерация 6 (20 анализов)

Фокус захода: Windows-кодировка вывода (эмодзи при редиректе stdout), чтение
`.env` в cp1251 из всех мест, падающий CLI `hermes`, точные cron-команды,
импорты (только stdlib), miniapp-статика, пути и спецсимволы.

## Найденные и исправленные дефекты

| # | Анализ | Дефект | Статус |
|---|--------|--------|--------|
| 1 | Вывод в файл/pipe на Windows (`PYTHONIOENCODING=cp1251`) | **КРИТИЧНО**: при редиректе stdout (cron, логи, subprocess, файлы) Python берёт locale-кодировку (cp866/cp1251 на русской Windows) → `UnicodeEncodeError` на эмодзи (📊✅❌) → traceback в `core.emit` и selfcheck | 🔧 `sys.stdout/stderr.reconfigure(encoding="utf-8", errors="replace")` в `core.py`; проверено: status/selfcheck/audit + все 176 тестов проходят с `PYTHONIOENCODING=cp1251` |
| 2 | `_discover_root()` читал `.env` как UTF-8 | **КРИТИЧНО**: при запуске из профиля/клона с `.env` в cp1251 (как создаёт `Set-Content`) — `UnicodeDecodeError` на старте любого инструмента; `load_env` уже был починен в итер. 4, а чтение корня — нет | 🔧 `_discover_root()` теперь использует `_read_text()` (BOM/UTF-16/cp1251-детекция); `_read_text` вынесен выше, дубль удалён |
| 3 | `subprocess.run(text=True)` в `board.py` | на Windows декодирует вывод hermes в locale (cp1251) — возможен `UnicodeDecodeError` | 🔧 `encoding="utf-8", errors="replace"` |
| 4 | `setup.bat` / `start.bat` | `chcp 65001` стоял ПОСЛЕ русских `rem`-комментариев — первые строки cmd читает в cp866 (кракозябры) | 🔧 `chcp 65001` перенесён первой строкой после `@echo off` |

## Проверено — ошибок нет

| # | Анализ | Результат |
|---|--------|-----------|
| 5 | Импорты tools: только stdlib | да; `torch` — опциональный импорт в try/except (smoke без него работает) |
| 6 | miniapp статика: `/`, `/app.css`, `/js/app.js`, `/js/charts.js` | все 200; `/favicon.ico` 404 — косметика |
| 7 | `hygiene.py run --max-run-hours 24` (точная cron-команда) | exit 0 |
| 8 | `gpu.py check --need-gb 20` без GPU | понятный отказ «НЕЛЬЗЯ: GPU не обнаружен», exit 1 |
| 9 | `exp_runner --smoke` без `--dry-run` и без модели | завершается за 0.1 с, пишет артефакт, не зависает |
| 10 | `board.py sync` с падающим `hermes` (exit 1) | «Создано 0, перемещено 0, проблем 1», exit 0 — мягко |
| 11 | `install.sh` с падающим `hermes` | cron-задания `SKIP`, не краш |
| 12 | Каталог с точками/дефисами (`my.research-v2`) | работает |
| 13 | Длина путей | макс. 85 символов — далеко от лимита Windows 260 |
| 14 | CRLF в `.py` | нет, всё LF |
| 15 | Коммиченные артефакты `results/H-007/*` | есть в индексе, `.gitignore` их теперь покрывает — не блокер (удалять `git rm --cached` по желанию владельца) |
| 16 | `.env.EXAMPLE` → `.env` вручную (старый способ) | работает; selfcheck честно предупреждает про права 644 |
| 17 | Полный e2e: клон → `.env` в **cp1251** → `install.sh` → `boot/status/selfcheck` | работает без крахов |
| 18 | Юнит-тесты 176 | OK и в обычном, и в cp1251-режиме |
| 19 | `audit --no-coverage` 80 | 80/80, 0 FAIL и в обычном, и в cp1251-режиме |
| 20 | `py_compile` + `sh -n install.sh` | OK |

## Кумулятивный итог шести итераций

Исправлено: 7 + 5 + 3 + 2 + 4 + 4 = **25 дефектов**.

Блокеры старта с нуля (найдены и устранены):
1. `install.sh`: ручной ввод токена писал промпт в значение (итер. 2);
2. `install.sh --in-place`: установка в клон не работала (итер. 3);
3. `core.load_env`: `.env` в cp1251/UTF-16 ронял чтение секретов (итер. 4);
4. `core.load_config`: BOM в config.yaml ломал платформу/режим (итер. 5);
5. **Вывод в cp1251: UnicodeEncodeError на эмодзи при редиректе (итер. 6)**;
6. **`_discover_root`: краш на cp1251 `.env` при старте из профиля (итер. 6)**.

Остаточные риски (только на машине владельца): CLI `hermes` (gateway/cron),
реальный Telegram, GPU + Ollama, исполнение `install.ps1` (в песочнице нет
PowerShell — статический анализ + BOM-фикс).
