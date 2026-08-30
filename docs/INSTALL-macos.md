# Установка на macOS (режим отладки)

На macOS профиль ставится для двух задач: проверить, что логика работает, и держать
Telegram-штаб под рукой. Реальные GPU-прогоны здесь не идут.

## Что работает и что нет

| Функция | macOS |
|---|---|
| Очередь, PI/PPI, гейты, вердикты, калибровка | Работает полностью |
| Telegram: управление, сводки, статистика | Работает полностью |
| Самопроверка, тесты, канбан, гигиена | Работает полностью |
| Эксперименты L0–L3 | Только dry-run: проверяется код и формат артефактов |
| Проверка VRAM | Нет NVIDIA — гейт возвращает «недоступно», и это не ошибка |

**Главное правило честности:** результат dry-run никогда не закрывает гипотезу
вердиктом. В `summary.json` такие прогоны помечены `dry_run: true`.

## Установка

```bash
git clone https://github.com/<ваш-логин>/researchagen
cd researchagen
sh install.sh
```

В шаге 1 выберите `1) macOS` — тогда в конфиг попадёт `debug_mode: true`.

Токен берите **тот же**, что на Windows — тогда оба пользователя видят один бот.
Но шлюз должен быть запущен **только в одном месте за раз**:

- рабочий режим — шлюз на Windows, Mac только читает чат;
- отладка — шлюз на Mac, на Windows остановлен.

Два шлюза одновременно на один токен — гарантированные пропажи сообщений.

## Проверка функционала

```bash
cd ~/.hermes/profiles/researchagen
python3 -m unittest discover -s tests -q     # логика очереди, гейтов, вердиктов
python3 tools/selfcheck.py all               # среда и изоляция
python3 tools/rg.py status
python3 tools/exp_runner.py --hypo H-001 --level L0 --dry-run
```

Последняя команда — главный смысл macOS-установки: она ловит ошибки в коде
эксперимента до того, как они сожгут часы на 5090.

## Только бот, без исследования

Отключите две тяжёлые задачи — останутся сводки и управление:

```bash
hermes cron disable research-loop
hermes cron disable dispatcher
researchagen gateway start
```

## Типичные проблемы macOS

| Симптом | Причина и решение |
|---|---|
| `selfcheck` ругается на GPU | Ожидаемо: в debug-режиме это `WARN`/`OK` (dry-run), а не `FAIL`. На Windows — `FAIL` |
| `selfcheck` ругается на модель | В `debug` пустая `RESEARCHAGEN_MODEL_BASE_URL` — `WARN` (dry-run доступен, `/dr` требует модель). На Windows — `FAIL` |
| `selfcheck` — изоляция токена | Один токен в `profiles/main` + `profiles/researchagen` — `WARN` (штатно для macOS+Windows, запускай один gateway). Токен в `~/.hermes/.env` (корневой) — `FAIL` |
| `governor` 0/0 после ручного `rsync` | Ты сделал `rsync --exclude=config.yaml` и сохранил onboarding-конфиг `{'onboarding':{'seen':...}}`. Решение: `sh install.sh` (перезапишет `config.yaml` с капсами `2/1`) или `git pull` без `--exclude` |
| Модель отвечает медленно | 27B на Apple Silicon медленная. Для отладки возьмите модель меньше |
| Сообщения дублируются | Запущены два шлюза на один токен. Оставьте один |
| `python` не найден | На macOS команда называется `python3` |

## Обновление без потери секретов

Не используй `rsync --exclude=config.yaml --exclude=.env --exclude=state/` — он
оставляет onboarding-конфиг. Правильно:

```bash
cd ~/.hermes/profiles/researchagen
git pull
sh install.sh   # спросит токен/модель, перезапишет config.yaml, сохранит .env
```

`install.sh` сам пишет `platform: macos / mode: debug` и delegation `2/1`, поэтому
`governor` не упадёт в `unsafe cap`.
