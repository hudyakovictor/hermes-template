# Установка на Windows (основная платформа)

Целевая конфигурация: RTX 5090 (32 ГБ), Qwen3-27B Q6 + KV-cache Q8 в Ollama,
Hermes уже установлен и первый агент работает в другом терминале на OpenRouter.

## 1. Подготовка

```powershell
python -V          # нужен 3.9+
ollama --version
nvidia-smi
hermes --version
```

Обязательно создайте **нового** бота в @BotFather. Переиспользование токена первого
агента приведёт к тому, что оба шлюза будут перебивать друг друга.

## 2. Модель

```powershell
ollama pull qwen3:27b
ollama run qwen3:27b "привет"   # проверка, что веса живые
```

Квантование KV-cache в Q8 задаётся на стороне Ollama (переменные окружения службы),
не в этом профиле. Профиль только потребляет OpenAI-совместимый эндпоинт.

Адрес должен заканчиваться на `/v1`: `http://localhost:11434/v1`.

## 3. Установка

```powershell
git clone https://github.com/<ваш-логин>/researchagen
cd researchagen
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

Установщик спросит шесть блоков данных: ОС, корень Hermes, Telegram
(токен / chat_id / thread_id / два user_id), модель, лимиты GPU, подтверждение.
Ничего не записывается до экрана подтверждения.

Где взять ID:

- `chat_id` группы: добавьте бота в группу, напишите сообщение и откройте
  `getUpdates` в браузере. У супергрупп ID начинается с `-100`.
- `thread_id`: в том же ответе поле `message_thread_id`.
- `user_id`: там же, `from.id`.

## 4. Запуск в отдельном терминале

Откройте **второе** окно PowerShell (первое занято основным агентом):

```powershell
researchagen gateway start
```

Шлюз держит и Telegram, и cron. Закрытие окна останавливает автономию — в этом и смысл
отдельного терминала: одно окно — один агент.

Проверка без модели:

```powershell
cd $env:USERPROFILE\.hermes\profiles\researchagen
python tools\rg.py status
python tools\selfcheck.py all
python -m unittest discover -s tests -q
```

## 5. Автозапуск при входе (опционально)

```powershell
$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
  -Argument '-NoExit -Command researchagen gateway start'
$trigger = New-ScheduledTaskTrigger -AtLogOn
Register-ScheduledTask -TaskName 'researchagen-gateway' -Action $action -Trigger $trigger
```

Не делайте это, пока `selfcheck` не зелёный: автозапуск сломанного контура генерирует
мусор быстрее, чем вы его читаете.

## Типичные проблемы Windows

| Симптом | Причина и решение |
|---|---|
| `install.ps1` не запускается | Политика исполнения. Запускайте с `-ExecutionPolicy Bypass -File` |
| Кракозябры вместо русских букв | `[Console]::OutputEncoding=[Text.Encoding]::UTF8` перед запуском |
| Модель не отвечает | Ollama слушает только 127.0.0.1 либо адрес без `/v1` |
| VRAM всегда занята | Модель висит в памяти. `ollama stop qwen3:27b` или `/gpu` |
| Шлюз не стартует | Тот же токен, что у первого агента. `selfcheck` пишет об этом прямо |
