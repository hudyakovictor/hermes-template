# Аудит стартового пути — итерация 3 (20 анализов)

Повторный заход с целью найти ошибки ранних этапов, из-за которых проект не
стартует «с начала». Фокус: голые вызовы команд, in-place-режим, документация
против реальности, Windows-специфика, повторные сценарии.

## Найденные и исправленные дефекты

| # | Анализ | Дефект | Статус |
|---|--------|--------|--------|
| 1 | `install.sh --in-place` | **КРИТИЧНО**: `TARGET` сначала ставился в `$SRC_DIR`, но позже безусловно перезаписывался на `~/.hermes/profiles/researchagen` — in-place-режим никогда не работал: config.yaml/.env писались в профиль, самопроверка падала «файл не найден» | 🔧 `TARGET` защищён в двух местах; проверено e2e: конфиг настроен в клоне, чужие строки сохранены |
| 2 | `gpu.py snapshot` | Доки (`INSTALL-windows.md`, `WINDOWS.md`) рекомендуют `python tools\gpu.py snapshot`, а такой команды нет (только `show`/`check`) — проверка GPU после установки падала | 🔧 добавлен алиас `snapshot` → `show`; доки приведены к `show` |
| 3 | `rg.py hypo` без подкоманды | Бессмысленное «неизвестная команда 'help'» (вместо справки) | 🔧 печатает usage, exit 0 |
| 4 | `digest --send` при недоступном Telegram | Exit 0 при неудачной доставке (ошибка только в лог) | ⚠️ minor — не блокер, отмечено |
| 5 | miniapp `run_check` без id | `" нет в очереди"` — лишний пробел в начале | ⚠️ косметика |

## Проверено — ошибок нет

| # | Анализ | Результат |
|---|--------|-----------|
| 6 | `py_compile` всех tools + miniapp | 0 ошибок |
| 7 | posix-only конструкции (`os.kill`, `/bin/sh`, `ps`) | `os.kill(pid,0)` уже имеет Windows-ветку через `tasklist` |
| 8 | `rg.py hypo/crew/inbox/signals/priors/board/bottom/verdict/bet` без подкоманд | понятные ответы, ни одного traceback |
| 9 | `audit.py/hygiene.py/crew.py/selfcheck.py/verdict.py/calib.py/report.py` напрямую | exit 0 (selfcheck — ожидаемый 1 на голом клоне) |
| 10 | `governor reserve/heartbeat` на пустой базе без GPU | fail-closed с понятным объяснением |
| 11 | `pause/resume/finish/launch/kill/check/add/idea` без аргументов | понятные ошибки («нужен id» и т.п.) |
| 12 | `bottom evidence/candidates`, `crew emit/test`, `triage`, `priors sources` | ок |
| 13 | in-place e2e: готовый `.env` + `--in-place --non-interactive` | подхват без вопросов, `RESEARCHAGEN_HOME`=клон, `OPENROUTER_API_KEY` сохранён |
| 14 | запуск из клона с `.env` (RESEARCHAGEN_HOME в .env) | `rg.py status` работает |
| 15 | `gpu.py snapshot` после фикса | `GPU недоступен. nvidia-smi не найден` (ожидаемо в песочнице) |
| 16 | `rg.py hypo` после фикса | usage, exit 0 |
| 17 | miniapp POST `type=pause/resume/run_check` | JSON-ответы, без краша |
| 18 | `tg.py test` / `digest --send` с фейк-токеном | мягкая ошибка, без traceback |
| 19 | юнит-тесты | 176/176 OK |
| 20 | `audit run --no-coverage` + `sh -n install.sh` | 80/80, 0 FAIL; синтаксис OK |

## Кумулятивный итог трёх итераций

Исправлено: 7 (итерация 1) + 5 (итерация 2) + 3 (итерация 3) = **15 дефектов**.
Среди них два блокера, которые реально мешали старту с нуля:
- `install.sh`: ручной ввод токена записывал промпт в значение (итерация 2);
- `install.sh --in-place`: установка в клон не работала (итерация 3).

Остаточные риски (проверяются только на машине владельца): CLI `hermes`
(gateway/cron), реальный Telegram, GPU + Ollama.
