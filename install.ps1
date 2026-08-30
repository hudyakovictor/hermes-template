<#
  researchagen — установщик для Windows (основная платформа: RTX 5090 + Qwen3-27B Q6 / KV Q8).

  Быстрый режим (по умолчанию): спрашивает ТОЛЬКО токен бота и API модели, остальное авто.
  Если .env уже существует (например, владелец прислал готовый блок команд) —
  установщик подхватывает его и НЕ задаёт вопросов вообще.
  Полный режим: install.ps1 -Full — 6 шагов как раньше.

  Только встроенный PowerShell 5.1+ и Python stdlib. Никаких внешних модулей.

  Запуск:
    powershell -ExecutionPolicy Bypass -File .\install.ps1
    powershell -ExecutionPolicy Bypass -File .\install.ps1 -BotToken "123:abc" -ModelBase "http://localhost:11434/v1" -ModelKey "ollama"
    powershell -ExecutionPolicy Bypass -File .\install.ps1 -Full
#>

[CmdletBinding()]
param(
    [string]$BotToken = "",
    [string]$ChatId = "",
    [string]$ThreadId = "",
    [string]$UserId = "",
    [string]$ModelBase = "",
    [string]$ModelName = "",
    [string]$ModelKey = "",
    [switch]$Full,
    [switch]$NonInteractive,
    [switch]$InPlace
)

$ErrorActionPreference = 'Stop'
$ProfileName = 'researchagen'
$SrcDir = Split-Path -Parent $MyInvocation.MyCommand.Path

function Write-Head {
    Clear-Host
    Write-Host ''
    Write-Host '  researchagen' -ForegroundColor White -NoNewline
    Write-Host ' - автономный исследователь training dynamics' -ForegroundColor DarkGray
    Write-Host '  дополнительный профиль для agent-hermes - основной агент не затрагивается' -ForegroundColor DarkGray
    Write-Rule
}
function Write-Rule { Write-Host ('  ' + ('-' * 58)) -ForegroundColor DarkGray }
function Write-Step($t) { Write-Host ''; Write-Host "  $t" -ForegroundColor White }
function Write-Ok($t)   { Write-Host '  OK   ' -ForegroundColor Green -NoNewline; Write-Host $t }
function Write-Warn($t) { Write-Host '  WARN ' -ForegroundColor Yellow -NoNewline; Write-Host $t }
function Write-Bad($t)  { Write-Host '  FAIL ' -ForegroundColor Red -NoNewline; Write-Host $t }
function Write-Hint($t) { Write-Host "       $t" -ForegroundColor DarkGray }

function Ask([string]$Prompt, [string]$Default = '') {
    if ($Default -ne '') { $label = "  $Prompt [$Default]" } else { $label = "  $Prompt" }
    $v = Read-Host $label
    if ([string]::IsNullOrWhiteSpace($v)) { return $Default }
    return $v.Trim()
}
function Ask-Required([string]$Prompt) {
    while ($true) {
        $v = Read-Host "  $Prompt"
        if (-not [string]::IsNullOrWhiteSpace($v)) { return $v.Trim() }
        if ([Console]::IsInputRedirected) {
            Write-Bad "Нет ввода: поле '$Prompt' обязательно."
            Write-Hint 'Запустите установщик в интерактивном окне или передайте значение аргументом (-BotToken "123:abc"), либо создайте .env заранее.'
            exit 1
        }
        Write-Host '       Поле обязательное.' -ForegroundColor Red
    }
}
function Ask-YesNo([string]$Prompt, [bool]$Default = $true) {
    $d = if ($Default) { 'y' } else { 'n' }
    $v = (Ask $Prompt $d).ToLower()
    return @('y', 'yes', 'д', 'да') -contains $v
}

function Read-EnvFile([string]$Path) {
    # Читает .env в hashtable KEY=VALUE (комментарии и пустые строки пропускаются)
    $map = @{}
    if (-not (Test-Path $Path)) { return $map }
    try {
        foreach ($line in [System.IO.File]::ReadAllLines($Path)) {
            $t = $line.Trim()
            if ($t -eq '' -or $t.StartsWith('#')) { continue }
            $i = $t.IndexOf('=')
            if ($i -le 0) { continue }
            $map[$t.Substring(0, $i).Trim()] = $t.Substring($i + 1).Trim()
        }
    } catch { }
    return $map
}

function Update-EnvFile([string]$Path, [hashtable]$Values) {
    # Обновляет .env: меняет известные ключи, НЕ трогает чужие строки
    # (OPENROUTER_API_KEY и т.п.) и дописывает недостающие ключи.
    $lines = @()
    if (Test-Path $Path) { $lines = @([System.IO.File]::ReadAllLines($Path)) }
    $out = New-Object System.Collections.Generic.List[string]
    if ($lines.Count -eq 0) {
        $out.Add('# researchagen — секреты профиля. Не коммитить.')
        $out.Add('# Создано install.ps1. Ваши дополнительные строки сохраняются при переустановке.')
    }
    $seen = @{}
    foreach ($line in $lines) {
        $t = $line.Trim()
        if ($t -eq '' -or $t.StartsWith('#')) { $out.Add($line); continue }
        $i = $t.IndexOf('=')
        if ($i -le 0) { $out.Add($line); continue }
        $k = $t.Substring(0, $i).Trim()
        if ($Values.ContainsKey($k)) {
            $out.Add("$k=$($Values[$k])")
            $seen[$k] = $true
        } else {
            $out.Add($line)
        }
    }
    foreach ($k in $Values.Keys) {
        if (-not $seen.ContainsKey($k)) { $out.Add("$k=$($Values[$k])") }
    }
    [System.IO.File]::WriteAllLines($Path, $out, (New-Object System.Text.UTF8Encoding($false)))
}

function Find-Python {
    foreach ($cand in @('python', 'python3', 'py')) {
        $cmd = Get-Command $cand -ErrorAction SilentlyContinue
        if ($null -eq $cmd) { continue }
        try {
            $ver = & $cand -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>$null
            if ($LASTEXITCODE -eq 0 -and $ver) {
                $parts = $ver.Trim().Split('.')
                if ([int]$parts[0] -ge 3 -and [int]$parts[1] -ge 9) { return $cand }
            }
        } catch { }
    }
    return $null
}

function Get-TelegramAuto([string]$Token) {
    # Пытается получить chat_id и user_id через getUpdates
    try {
        $url = "https://api.telegram.org/bot$Token/getUpdates"
        $resp = Invoke-RestMethod -Uri $url -TimeoutSec 10 -ErrorAction Stop
        if ($resp.ok -and $resp.result.Count -gt 0) {
            $last = $resp.result[-1]
            $chat = $last.message.chat.id
            if (-not $chat) { $chat = $last.channel_post.chat.id }
            if (-not $chat) { $chat = $last.my_chat_member.chat.id }
            $user = $last.message.from.id
            if (-not $user) { $user = $last.channel_post.from.id }
            return @{ chat_id = "$chat"; user_id = "$user" }
        }
    } catch {
        # тихо, автоопределение не критично
    }
    return @{ chat_id = ""; user_id = "" }
}

# ------------------------------------------------------------- 0. Предпроверка
Write-Head
if ($Full) {
    Write-Host '  Режим: полный (6 шагов)' -ForegroundColor Cyan
} else {
    Write-Host '  Режим: быстрый — только токен и API, остальное авто' -ForegroundColor Cyan
    Write-Host '  Для полного опроса: .\install.ps1 -Full' -ForegroundColor DarkGray
}
Write-Rule

Write-Step 'Предпроверка'

$HasWinget = $null -ne (Get-Command winget -ErrorAction SilentlyContinue)

$Py = Find-Python
if (-not $Py) {
    Write-Bad 'Не найден Python 3.9+.'
    if ($HasWinget -and -not $NonInteractive) {
        if (Ask-YesNo 'Установить Python автоматически (winget)?' $true) {
            Write-Hint 'Установка Python 3.12... это займёт пару минут.'
            & winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements | Out-Null
            $Py = Find-Python
        }
    }
    if (-not $Py) {
        Write-Bad 'Python всё ещё не найден.'
        Write-Hint 'Установи вручную: https://www.python.org/downloads/'
        Write-Hint 'ВАЖНО: на первом экране установки отметь галочку "Add python.exe to PATH".'
        Write-Hint 'Затем закрой и снова открой PowerShell и повтори команду.'
        exit 1
    }
}
Write-Ok "Python: $(& $Py -V 2>&1)"

# Git
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Warn 'git не найден — скачать проект командой clone не получится'
    if ($HasWinget -and -not $NonInteractive) {
        if (Ask-YesNo 'Установить Git автоматически (winget)?' $true) {
            & winget install -e --id Git.Git --accept-package-agreements --accept-source-agreements | Out-Null
        }
    }
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        Write-Hint 'Установи Git: https://git-scm.com/download/win (все настройки по умолчанию),'
        Write-Hint 'затем открой НОВОЕ окно PowerShell и повтори.'
    }
}

$HasHermes = $null -ne (Get-Command hermes -ErrorAction SilentlyContinue)
if ($HasHermes) { Write-Ok 'hermes найден в PATH' }
else {
    Write-Warn 'hermes не найден в PATH'
    Write-Hint 'Профиль будет разложен, но cron-задачи придётся добавить вручную (docs/OPERATIONS.md).'
}

# GPU: информативно
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    try {
        $gpu = (& nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>$null | Select-Object -First 1)
        if ($gpu) { Write-Ok "GPU: $gpu" }
    } catch { Write-Warn 'nvidia-smi есть, но опрос не удался' }
} else {
    Write-Warn 'nvidia-smi не найден - на Windows prod GPU-гейт будет закрывать запуски (на macOS — dry-run)'
}

# ------------------------------------------------------------- Авто-определения
$Platform = 'windows'
$DebugMode = 'false'
if ($env:OS -like '*Windows*' -or $env:OS -eq $null) {
    # PowerShell на Windows всегда windows
    $Platform = 'windows'
}
# если запустили на macOS через pwsh — определим
if ($IsMacOS) { $Platform = 'macos'; $DebugMode = 'true' }
if ($IsLinux) { $Platform = 'linux'; $DebugMode = 'false' }

$defaultRoot = if ($env:HERMES_HOME) { $env:HERMES_HOME } else { Join-Path $env:USERPROFILE '.hermes' }
$HermesRoot = $defaultRoot
if ($InPlace) {
    $Target = $SrcDir
    Write-Host "  Режим: in-place — всё уже установлено в проекте, только .env" -ForegroundColor Cyan
} else {
    $Target = Join-Path (Join-Path $HermesRoot 'profiles') $ProfileName
}

# Лимиты по умолчанию — безопасные для RTX 5090 (совпадают с дефолтами config.yaml)
$GpuFree = '20'
$DailyBudget = '20'
$Approval = '12'
$Autolaunch = 'true'

# Модель по умолчанию — локальная Ollama
# (CLI-значения запоминаем ДО дефолтов: они имеют приоритет над готовым .env)
$ModelBaseCli = $ModelBase
$ModelNameCli = $ModelName
$ModelKeyCli = $ModelKey
if ([string]::IsNullOrWhiteSpace($ModelBase)) { $ModelBase = 'http://localhost:11434/v1' }
if ([string]::IsNullOrWhiteSpace($ModelName)) { $ModelName = 'qwen3:27b' }
if ([string]::IsNullOrWhiteSpace($ModelKey))  { $ModelKey = 'ollama' }

# Telegram — авто или ручной ввод
$TgToken = $BotToken
$TgChat = $ChatId
$TgThread = $ThreadId
$TgAichatThread = ''
$TgUser1 = $UserId
$TgUser2 = ''
$TgUsers = ''

# ------------------------------------------------------------- Готовый .env
# Если .env уже существует (например, владелец прислал готовый блок команд) —
# подхватываем значения и не задаём лишних вопросов.
# Приоритет: аргументы командной строки > .env > вопросы > значения по умолчанию.
$ModelFromEnv = $false
$envMap = @{}
$envPathPrev = Join-Path $Target '.env'
if (Test-Path $envPathPrev) {
    $envMap = Read-EnvFile $envPathPrev
    if (-not [string]::IsNullOrWhiteSpace($envMap['TELEGRAM_BOT_TOKEN'])) {
        Write-Ok 'Найден готовый .env — токен и настройки подхвачены, вопросов не будет'
    }
    if (-not [string]::IsNullOrWhiteSpace($envMap['TELEGRAM_BOT_TOKEN']) -and [string]::IsNullOrWhiteSpace($TgToken)) {
        $TgToken = $envMap['TELEGRAM_BOT_TOKEN']
    }
    if (-not [string]::IsNullOrWhiteSpace($envMap['TELEGRAM_HOME_CHANNEL']) -and [string]::IsNullOrWhiteSpace($TgChat)) {
        $TgChat = $envMap['TELEGRAM_HOME_CHANNEL']
    }
    if (-not [string]::IsNullOrWhiteSpace($envMap['TELEGRAM_CRON_THREAD_ID']) -and [string]::IsNullOrWhiteSpace($TgThread)) {
        $TgThread = $envMap['TELEGRAM_CRON_THREAD_ID']
    }
    if (-not [string]::IsNullOrWhiteSpace($envMap['TELEGRAM_AICHAT_THREAD_ID']) -and [string]::IsNullOrWhiteSpace($TgAichatThread)) {
        $TgAichatThread = $envMap['TELEGRAM_AICHAT_THREAD_ID']
    }
    if (-not [string]::IsNullOrWhiteSpace($envMap['TELEGRAM_ALLOWED_USERS'])) {
        $usersFromEnv = $envMap['TELEGRAM_ALLOWED_USERS']
        $parts = $usersFromEnv.Split(',')
        if ([string]::IsNullOrWhiteSpace($TgUser1)) { $TgUser1 = $parts[0].Trim() }
        if ($parts.Count -gt 1 -and [string]::IsNullOrWhiteSpace($TgUser2)) { $TgUser2 = $parts[1].Trim() }
    }
    if (-not [string]::IsNullOrWhiteSpace($envMap['RESEARCHAGEN_MODEL_BASE_URL']) -and [string]::IsNullOrWhiteSpace($ModelBaseCli)) {
        $ModelBase = $envMap['RESEARCHAGEN_MODEL_BASE_URL']
        $ModelFromEnv = $true
    }
    if (-not [string]::IsNullOrWhiteSpace($envMap['RESEARCHAGEN_MODEL_NAME']) -and [string]::IsNullOrWhiteSpace($ModelNameCli)) {
        $ModelName = $envMap['RESEARCHAGEN_MODEL_NAME']
    }
    if (-not [string]::IsNullOrWhiteSpace($envMap['RESEARCHAGEN_MODEL_API_KEY']) -and [string]::IsNullOrWhiteSpace($ModelKeyCli)) {
        $ModelKey = $envMap['RESEARCHAGEN_MODEL_API_KEY']
    }
}

if ($Full) {
    # ------------------------------------------------------------- 1. ОС (полный режим)
    Write-Step 'Шаг 1/6 - операционная система'
    Write-Host '    1) Windows' -NoNewline; Write-Host '  - полный режим: реальные GPU-прогоны на RTX 5090' -ForegroundColor DarkGray
    Write-Host '    2) macOS' -NoNewline;   Write-Host '    - режим отладки: контур и бот работают, эксперименты - dry-run' -ForegroundColor DarkGray
    $osChoice = Ask 'Выбор' '1'
    if ($osChoice -eq '2') { $Platform = 'macos'; $DebugMode = 'true' } else { $Platform = 'windows'; $DebugMode = 'false' }
    Write-Ok "Платформа: $Platform (debug_mode=$DebugMode)"

    # ------------------------------------------------------------- 2. Путь
    Write-Step 'Шаг 2/6 - куда ставим'
    $HermesRoot = Ask 'Корень Hermes' $defaultRoot
    $Target = Join-Path (Join-Path $HermesRoot 'profiles') $ProfileName
    Write-Host "       Профиль: $Target" -ForegroundColor Cyan
    if (Test-Path $Target) { Write-Warn 'Каталог уже существует - код обновится, .env и база состояния сохранятся' }

    # ------------------------------------------------------------- 3. Telegram
    Write-Step 'Шаг 3/6 - Telegram (ОТДЕЛЬНЫЙ бот, не тот, что у первого агента)'
    Write-Hint 'Один токен на два процесса не работает: long-polling будет рваться у обоих агентов.'
    if ([string]::IsNullOrWhiteSpace($TgToken)) { $TgToken = Ask-Required 'Токен бота (BotFather)' }
    if ($TgToken -notmatch ':') { Write-Warn 'Токен без двоеточия выглядит неверно - проверьте' }
    if ([string]::IsNullOrWhiteSpace($TgChat)) { $TgChat = Ask-Required 'chat_id рабочего чата/группы' }
    $TgThread = Ask "thread_id темы 'Штаб' (Enter = общий чат)" $TgThread
    $TgAichatThread = Ask "thread_id темы 'aichat' для переписки агентов (Enter = только база)" $TgAichatThread
    Write-Hint 'Оба пользователя в списке видят один и тот же статус, очередь и историю.'
    if ([string]::IsNullOrWhiteSpace($TgUser1)) { $TgUser1 = Ask-Required 'user_id пользователя 1' }
    $TgUser2 = Ask 'user_id пользователя 2 (Enter = пропустить)' $TgUser2
    if ($TgUser2 -ne '') { $TgUsers = "$TgUser1,$TgUser2" } else { $TgUsers = $TgUser1; Write-Warn 'Второй пользователь не указан - добавьте позже в .env' }

    # если пользователь сменил корень Hermes — перечитываем .env нового каталога
    $envPathNew = Join-Path $Target '.env'
    if (Test-Path $envPathNew) {
        $envNew = Read-EnvFile $envPathNew
        if (-not [string]::IsNullOrWhiteSpace($envNew['TELEGRAM_BOT_TOKEN'])) {
            if ([string]::IsNullOrWhiteSpace($TgToken)) { $TgToken = $envNew['TELEGRAM_BOT_TOKEN'] }
            if ([string]::IsNullOrWhiteSpace($TgChat)) { $TgChat = $envNew['TELEGRAM_HOME_CHANNEL'] }
            if ([string]::IsNullOrWhiteSpace($TgUser1)) { $TgUser1 = $envNew['TELEGRAM_ALLOWED_USERS'].Split(',')[0].Trim() }
            Write-Ok '.env целевого каталога подхвачен'
        }
    }

    # ------------------------------------------------------------- 4. Модель
    Write-Step 'Шаг 4/6 - локальная модель (Ollama, OpenAI-совместимый эндпоинт)'
    Write-Hint 'Суффикс /v1 обязателен: без него клиент получит 404 на /models.'
    $ModelBase = Ask 'base_url' $ModelBase
    $ModelName = Ask 'имя модели' $ModelName
    $ModelKey  = Ask 'api_key (Ollama игнорирует, но поле нужно)' $ModelKey

    # ------------------------------------------------------------- 5. Лимиты
    Write-Step 'Шаг 5/6 - границы автономии'
    Write-Hint 'Модель 27B Q6 + KV Q8 занимает ~24 ГБ из 32 ГБ; остаток - бюджет экспериментов.'
    $GpuFree = Ask 'Минимум свободной VRAM для запуска, ГБ' $GpuFree
    $DailyBudget = Ask 'Суточный бюджет GPU-часов' $DailyBudget
    $Approval = Ask 'Прогон дороже N часов - спрашивать в Telegram' $Approval
    $Autolaunch = if (Ask-YesNo 'Автозапуск экспериментов без спроса?' $true) { 'true' } else { 'false' }
} else {
    # ------------------------------------------------------------- Быстрый режим: только токен и API
    Write-Step 'Быстрый режим — токен и API'

    if ([string]::IsNullOrWhiteSpace($TgToken)) {
        Write-Host ''
        Write-Host '  Нужен ТОЛЬКО токен нового бота (BotFather).' -ForegroundColor White
        Write-Host '  Остальное — чат, пользователи, лимиты — определится авто.' -ForegroundColor DarkGray
        $TgToken = Ask-Required 'TELEGRAM_BOT_TOKEN'
    } else {
        Write-Ok 'Токен уже задан (аргумент или готовый .env) — вопрос пропущен'
    }
    if ($TgToken -notmatch ':') { Write-Warn 'Токен без двоеточия выглядит неверно' }

    # Авто-определение chat_id / user_id (только если ещё не знаем)
    if ([string]::IsNullOrWhiteSpace($TgChat) -or [string]::IsNullOrWhiteSpace($TgUser1)) {
        Write-Hint 'Пытаюсь авто-определить chat_id через getUpdates (напиши боту /start заранее)...'
        $auto = Get-TelegramAuto $TgToken
        if ($auto.chat_id) {
            Write-Ok "Авто chat_id: $($auto.chat_id)"
            if ([string]::IsNullOrWhiteSpace($TgChat)) { $TgChat = $auto.chat_id }
        } else {
            Write-Warn 'chat_id не удалось авто-определить — будет запрошен ботом после запуска'
            Write-Hint 'После запуска напиши боту /start, затем /status — бот подскажет chat_id'
            if ([string]::IsNullOrWhiteSpace($TgChat)) { $TgChat = '' }
        }
        if ($auto.user_id) {
            Write-Ok "Авто user_id: $($auto.user_id)"
            if ([string]::IsNullOrWhiteSpace($TgUser1)) { $TgUser1 = $auto.user_id }
        }
    }

    # Если chat/user всё ещё пустые — ставим заглушки, бот их попросит позже
    if ([string]::IsNullOrWhiteSpace($TgChat)) { $TgChat = '0' }
    if ([string]::IsNullOrWhiteSpace($TgUser1)) { $TgUser1 = '0' }
    $TgUsers = $TgUser1
    if ($TgUser2) { $TgUsers = "$TgUser1,$TgUser2" }

    # Модель — если уже в .env, не спрашиваем (напечатаем, что подхватили)
    if ($ModelFromEnv) {
        Write-Ok "Модель уже задана в .env: $ModelName @ $ModelBase"
    } else {
        Write-Host ''
        Write-Host '  Модель API (Ollama по умолчанию, Enter = пропустить)' -ForegroundColor White
        Write-Hint 'Если используешь Ollama локально: просто Enter. Если владелец дал другой URL — вставь его.'
        $mb = Ask 'RESEARCHAGEN_MODEL_BASE_URL' $ModelBase
        if ($mb) { $ModelBase = $mb }
        $mn = Ask 'RESEARCHAGEN_MODEL_NAME' $ModelName
        if ($mn) { $ModelName = $mn }
        $mk = Ask 'RESEARCHAGEN_MODEL_API_KEY (Enter = ollama)' $ModelKey
        if ($mk) { $ModelKey = $mk }
    }

    # Путь — авто
    Write-Host "       Профиль: $Target" -ForegroundColor Cyan
    if (Test-Path $Target) { Write-Warn 'Каталог уже существует — обновится, .env и база сохранятся' }

    # Лимиты — авто, не спрашиваем
    Write-Hint "Лимиты авто: VRAM $GpuFree ГБ, бюджет $DailyBudget ч/сут, подтверждение > $Approval ч, автозапуск $Autolaunch"
}

# ------------------------------------------------------------- Подтверждение
if (-not $NonInteractive) {
    Write-Host ''
    Write-Rule
    Write-Host '  Проверьте перед записью' -ForegroundColor White
    $mask = if ($TgToken.Length -gt 8) { $TgToken.Substring(0, 8) + '...' } else { '***' }
    Write-Host "    Профиль       : $Target"
    Write-Host "    Платформа     : $Platform (debug=$DebugMode)"
    Write-Host "    Токен бота    : $mask"
    Write-Host "    Чат / тема     : $TgChat / $(if ($TgThread) { $TgThread } else { '-' })"
    Write-Host "    Пользователи : $TgUsers"
    Write-Host "    Модель        : $ModelName @ $ModelBase"
    Write-Host "    Лимиты       : VRAM >= $GpuFree ГБ, бюджет $DailyBudget ч/сут, подтверждение > $Approval ч"
    Write-Host "    Автозапуск   : $Autolaunch"
    Write-Rule
    if (-not (Ask-YesNo 'Продолжить?' $true)) {
        Write-Host '  Отменено. Ничего не записано.' -ForegroundColor Yellow
        exit 0
    }
}

# ------------------------------------------------------------- 6. Установка
Write-Step 'Установка'

if ($InPlace -or $Target -eq $SrcDir) {
    Write-Ok 'In-place: файлы уже в проекте, копирование пропущено'
    foreach ($d in @('hypotheses', 'signals', 'experiments', 'inbox', 'memory',
                     'reports', 'results', 'logs', 'state')) {
        New-Item -ItemType Directory -Force -Path (Join-Path $Target $d) | Out-Null
    }
} else {
    foreach ($d in @('tools', 'skills', 'skill-bundles', 'cron', 'hooks', 'docs', 'tests',
                     'hypotheses', 'signals', 'experiments', 'inbox', 'memory',
                     'reports', 'results', 'logs', 'state')) {
        New-Item -ItemType Directory -Force -Path (Join-Path $Target $d) | Out-Null
    }

    foreach ($f in @('MISSION.md', 'SOUL.md', '.hermes.md', 'FOCUS.md', 'distribution.yaml',
                     '.env.EXAMPLE', '.gitignore', 'README.md', 'LICENSE')) {
        $p = Join-Path $SrcDir $f
        if (Test-Path $p) { Copy-Item $p (Join-Path $Target $f) -Force }
    }
    foreach ($d in @('tools', 'skills', 'skill-bundles', 'cron', 'hooks', 'docs', 'tests')) {
        $p = Join-Path $SrcDir $d
        if (Test-Path $p) { Copy-Item (Join-Path $p '*') (Join-Path $Target $d) -Recurse -Force }
    }
    Write-Ok 'Файлы разложены'
}

# config.yaml — подстановка без внешних YAML-библиотек
$cfgSrc = Join-Path $SrcDir 'config.yaml'
$cfgDst = Join-Path $Target 'config.yaml'
$cfg = Get-Content -Raw -Encoding UTF8 $cfgSrc
$cfg = $cfg.Replace('<<INSTALLER_PLATFORM>>', $Platform).
            Replace('<<INSTALLER_MODE>>', $(if ($DebugMode -eq 'true') { 'debug' } else { 'production' })).
            Replace('<<INSTALLER_MODEL_NAME>>', $ModelName).
            Replace('<<INSTALLER_MODEL_BASE_URL>>', $ModelBase).
            Replace('<<INSTALLER_GPU_FREE_GB>>', $GpuFree).
            Replace('<<INSTALLER_DAILY_GPU_HOURS>>', $DailyBudget).
            Replace('<<INSTALLER_APPROVAL_GPU_HOURS>>', $Approval).
            Replace('<<INSTALLER_AUTOLAUNCH>>', $Autolaunch)
[System.IO.File]::WriteAllText($cfgDst, $cfg, (New-Object System.Text.UTF8Encoding($false)))
Write-Ok 'config.yaml настроен (platform windows / mode production / лимиты применены)'

# .env — обновляем, сохраняя чужие строки (OPENROUTER_API_KEY и т.п.)
$envPath = Join-Path $Target '.env'
if (Test-Path $envPath) {
    Copy-Item $envPath "$envPath.bak" -Force
    Write-Warn 'Старый .env сохранён как .env.bak'
}
$envValues = @{
    'TELEGRAM_BOT_TOKEN'          = $TgToken
    'TELEGRAM_HOME_CHANNEL'       = $TgChat
    'TELEGRAM_CRON_THREAD_ID'     = $TgThread
    'TELEGRAM_AICHAT_THREAD_ID'   = $TgAichatThread
    'TELEGRAM_ALLOWED_USERS'      = $TgUsers
    'RESEARCHAGEN_MODEL_BASE_URL' = $ModelBase
    'RESEARCHAGEN_MODEL_NAME'     = $ModelName
    'RESEARCHAGEN_MODEL_API_KEY'  = $ModelKey
    'RESEARCHAGEN_HOME'           = $Target
}
Update-EnvFile $envPath $envValues
try {
    $acl = Get-Acl $envPath
    $acl.SetAccessRuleProtection($true, $false)
    $me = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($me, 'FullControl', 'Allow')
    $acl.SetAccessRule($rule)
    Set-Acl $envPath $acl
    Write-Ok '.env записан (доступ только текущему пользователю)'
} catch {
    Write-Ok '.env записан'
    Write-Warn 'Не удалось сузить ACL - проверьте права вручную'
}

# Скиллы и бандлы в общие каталоги Hermes (пропускается в in-place)
if (-not $InPlace) {
    $skillsSrc = Join-Path $SrcDir 'skills'
    if (Test-Path $skillsSrc) {
        $skillsDst = Join-Path $HermesRoot 'skills'
        $bundlesDst = Join-Path $HermesRoot 'skill-bundles'
        New-Item -ItemType Directory -Force -Path $skillsDst, $bundlesDst | Out-Null
        Copy-Item (Join-Path $skillsSrc '*') $skillsDst -Recurse -Force
        $bundle = Join-Path $SrcDir 'skill-bundles\research-os.yaml'
        if (Test-Path $bundle) { Copy-Item $bundle $bundlesDst -Force }
        Write-Ok 'Скиллы и комплект research-os установлены'
    }
} else {
    Write-Ok 'In-place: скиллы уже в проекте, установка в Hermes пропущена'
}

# Cron-задачи (пропускается в in-place, т.к. всё уже в проекте)
if ($InPlace) {
    Write-Ok 'In-place: cron не регистрируется, используй python tools/rg.py tick'
} elseif ($HasHermes) {
    $cronDir = Join-Path $SrcDir 'cron'
    if (Test-Path $cronDir) {
        foreach ($jf in (Get-ChildItem $cronDir -Filter *.json | Sort-Object Name)) {
            try {
                $job = Get-Content -Raw -Encoding UTF8 $jf.FullName | ConvertFrom-Json
                $delivery = [string]$job.delivery
                $delivery = $delivery.Replace('<<INSTALLER_CHAT_ID>>', $TgChat).Replace('<<INSTALLER_THREAD_ID>>', $TgThread)
                $args = @('cron', 'add', $job.name, $job.schedule)
                if ($job.command) { $args += @('--command', $job.command) }
                else { $args += @('--prompt', $job.prompt) }
                if ($job.skill) { $args += @('--skill', $job.skill) }
                if ($delivery -and $delivery -ne 'none') { $args += @('--delivery', $delivery) }
                $args += @('--workdir', $Target)
                $env:HERMES_PROFILE = $ProfileName
                & hermes @args | Out-Null
                if ($LASTEXITCODE -eq 0) { Write-Ok "cron $($job.name)" } else { Write-Warn "cron $($job.name) - пропущено (возможно, уже есть)" }
            } catch {
                Write-Warn "cron $($jf.Name) - ошибка: $($_.Exception.Message)"
            }
        }
    }
} else {
    Write-Warn 'cron не зарегистрирован: hermes не найден. См. docs/OPERATIONS.md'
}

# ------------------------------------------------------------- Проверка Ollama
# Локальная модель — главный источник сбоев у новичка, проверяем сразу.
$UsesLocalModel = $ModelBase -match 'localhost|127\.0\.0\.1'
if ($UsesLocalModel) {
    Write-Step 'Проверка локальной модели (Ollama)'
    $OllamaCmd = Get-Command ollama -ErrorAction SilentlyContinue
    if (-not $OllamaCmd) {
        Write-Warn 'ollama не найден — бот не сможет обращаться к локальной модели'
        if ($HasWinget -and -not $NonInteractive) {
            if (Ask-YesNo 'Установить Ollama автоматически (winget)?' $true) {
                Write-Hint 'Установка Ollama...'
                & winget install -e --id Ollama.Ollama --accept-package-agreements --accept-source-agreements | Out-Null
                $OllamaCmd = Get-Command ollama -ErrorAction SilentlyContinue
            }
        }
        if (-not $OllamaCmd) {
            Write-Hint 'Скачай и установи Ollama: https://ollama.com/download/windows'
            Write-Hint 'Затем просто запусти установщик ещё раз — .env уже готов, вопросов не будет.'
        }
    }
    if ($OllamaCmd) {
        try {
            $ollamaList = (& ollama list 2>$null | Out-String)
            if ($ollamaList -match [regex]::Escape($ModelName)) {
                Write-Ok "Модель $ModelName уже скачана"
            } else {
                Write-Warn "Модель $ModelName ещё не скачана"
                $pullHint = "Скачай позже одной командой: ollama pull $ModelName"
                if (-not $NonInteractive) {
                    if (Ask-YesNo "Скачать сейчас (~20 ГБ, может занять долго)? Убедись, что Ollama запущена." $true) {
                        & ollama pull $ModelName
                        if ($LASTEXITCODE -eq 0) { Write-Ok "Модель $ModelName скачана" }
                        else { Write-Hint $pullHint }
                    } else {
                        Write-Hint $pullHint
                    }
                } else {
                    Write-Hint $pullHint
                }
            }
        } catch {
            Write-Warn 'Не удалось опросить Ollama — проверь, что она запущена (иконка в трее), затем запусти установщик ещё раз'
        }
    }
}

# Самопроверка
Write-Host ''
Write-Rule
Write-Host '  Самопроверка' -ForegroundColor White
Push-Location $Target
try { & $Py 'tools/selfcheck.py' 'all'; $rc = $LASTEXITCODE }
catch { $rc = 1; Write-Bad $_.Exception.Message }
finally { Pop-Location }

Write-Host ''
Write-Rule
if ($rc -eq 0) {
    Write-Host '  Готово. Профиль установлен и прошёл проверку.' -ForegroundColor Green
} else {
    Write-Host '  Установлено, но есть замечания выше. Если chat_id=0 — напиши боту /start, затем обнови .env' -ForegroundColor Yellow
}
Write-Host ''
Write-Host '  Дальше — запуск (2 шага):' -ForegroundColor White
Write-Host '    1) Открой НОВОЕ окно PowerShell:  Win+R  ->  powershell  ->  Enter' -ForegroundColor White
Write-Host '    2) Вставь и выполни:' -ForegroundColor White
Write-Host '       researchagen gateway start' -ForegroundColor Cyan
Write-Host '    Это окно больше НЕ закрывай — пока оно открыто, бот и автономия работают.' -ForegroundColor DarkGray
Write-Host '       Проверка: в Telegram напиши боту  /status' -ForegroundColor DarkGray
Write-Host ''
Write-Host '    Остальное (если интересно):' -ForegroundColor DarkGray
Write-Host '      researchagen chat' -ForegroundColor Cyan -NoNewline
Write-Host '                        # ручная сессия с агентом' -ForegroundColor DarkGray
Write-Host "      cd `"$Target`"; $Py tools\rg.py status" -ForegroundColor Cyan -NoNewline
Write-Host '   # состояние без Telegram' -ForegroundColor DarkGray
Write-Host ''
if ($TgChat -eq '0' -or $TgUser1 -eq '0') {
    Write-Host '  Важно: chat_id/user_id не определились авто. После запуска:' -ForegroundColor Yellow
    Write-Host '    1) Напиши боту /start в Telegram' -ForegroundColor White
    Write-Host '    2) Открой https://api.telegram.org/bot<token>/getUpdates и скопируй chat.id' -ForegroundColor White
    Write-Host '    3) Обнови .env: TELEGRAM_HOME_CHANNEL и TELEGRAM_ALLOWED_USERS' -ForegroundColor White
}
Write-Host '  Основной агент не затронут: другой профиль, другой токен, другой терминал.' -ForegroundColor DarkGray
Write-Host ''
exit $rc
