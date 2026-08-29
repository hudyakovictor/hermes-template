<#
  researchagen — установщик для Windows (основная платформа: RTX 5090 + Qwen3-27B Q6 / KV Q8).

  Только встроенный PowerShell 5.1+ и Python stdlib. Никаких внешних модулей.

  Запуск:
    powershell -ExecutionPolicy Bypass -File .\install.ps1
#>

[CmdletBinding()]
param()

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
        Write-Host '       Поле обязательное.' -ForegroundColor Red
    }
}

function Ask-YesNo([string]$Prompt, [bool]$Default = $true) {
    $d = if ($Default) { 'y' } else { 'n' }
    $v = (Ask $Prompt $d).ToLower()
    return @('y', 'yes', 'д', 'да') -contains $v
}

# ------------------------------------------------------------- 0. Предпроверка
Write-Head
Write-Step 'Шаг 0/6 - предпроверка'

$Py = $null
foreach ($cand in @('python', 'python3', 'py')) {
    $cmd = Get-Command $cand -ErrorAction SilentlyContinue
    if ($null -eq $cmd) { continue }
    try {
        $ver = & $cand -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>$null
        if ($LASTEXITCODE -eq 0 -and $ver) {
            $parts = $ver.Trim().Split('.')
            if ([int]$parts[0] -ge 3 -and [int]$parts[1] -ge 9) { $Py = $cand; break }
        }
    } catch { }
}
if (-not $Py) {
    Write-Bad 'Не найден Python 3.9+.'
    Write-Hint 'Установите Python с python.org или из Microsoft Store, затем повторите.'
    exit 1
}
Write-Ok "Python: $(& $Py -V 2>&1)"

$HasHermes = $null -ne (Get-Command hermes -ErrorAction SilentlyContinue)
if ($HasHermes) { Write-Ok 'hermes найден в PATH' }
else {
    Write-Warn 'hermes не найден в PATH'
    Write-Hint 'Профиль будет разложен, но cron-задачи придётся добавить вручную (docs/OPERATIONS.md).'
}

# GPU: информативно, не блокирующе
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    try {
        $gpu = (& nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>$null | Select-Object -First 1)
        if ($gpu) { Write-Ok "GPU: $gpu" }
    } catch { Write-Warn 'nvidia-smi есть, но опрос не удался' }
} else {
    Write-Warn 'nvidia-smi не найден - GPU-гейт будет закрывать запуски'
}

# ------------------------------------------------------------- 1. ОС
Write-Step 'Шаг 1/6 - операционная система'
Write-Host '    1) Windows' -NoNewline; Write-Host '  - полный режим: реальные GPU-прогоны на RTX 5090' -ForegroundColor DarkGray
Write-Host '    2) macOS' -NoNewline;   Write-Host '    - режим отладки: контур и бот работают, эксперименты - dry-run' -ForegroundColor DarkGray
Write-Hint 'Вариант 2 здесь только для подготовки конфига; на самом Mac запускайте install.sh.'
$osChoice = Ask 'Выбор' '1'
if ($osChoice -eq '2') { $Platform = 'macos'; $DebugMode = 'true' } else { $Platform = 'windows'; $DebugMode = 'false' }
Write-Ok "Платформа: $Platform (debug_mode=$DebugMode)"

# ------------------------------------------------------------- 2. Путь
Write-Step 'Шаг 2/6 - куда ставим'
$defaultRoot = if ($env:HERMES_HOME) { $env:HERMES_HOME } else { Join-Path $env:USERPROFILE '.hermes' }
$HermesRoot = Ask 'Корень Hermes' $defaultRoot
$Target = Join-Path (Join-Path $HermesRoot 'profiles') $ProfileName
Write-Host "       Профиль: $Target" -ForegroundColor Cyan
if (Test-Path $Target) {
    Write-Warn 'Каталог уже существует - код обновится, .env и база состояния сохранятся'
}

# ------------------------------------------------------------- 3. Telegram
Write-Step 'Шаг 3/6 - Telegram (ОТДЕЛЬНЫЙ бот, не тот, что у первого агента)'
Write-Hint 'Один токен на два процесса не работает: long-polling будет рваться у обоих агентов.'
$TgToken = Ask-Required 'Токен бота (BotFather)'
if ($TgToken -notmatch ':') { Write-Warn 'Токен без двоеточия выглядит неверно - проверьте' }
$TgChat = Ask-Required 'chat_id рабочего чата/группы'
$TgThread = Ask "thread_id темы 'Штаб' (Enter = общий чат)" ''
Write-Hint 'Оба пользователя в списке видят один и тот же статус, очередь и историю.'
$TgUser1 = Ask-Required 'user_id пользователя 1'
$TgUser2 = Ask 'user_id пользователя 2 (Enter = пропустить)' ''
if ($TgUser2 -ne '') { $TgUsers = "$TgUser1,$TgUser2" }
else { $TgUsers = $TgUser1; Write-Warn 'Второй пользователь не указан - добавьте позже в .env' }

# ------------------------------------------------------------- 4. Модель
Write-Step 'Шаг 4/6 - локальная модель (Ollama, OpenAI-совместимый эндпоинт)'
Write-Hint 'Суффикс /v1 обязателен: без него клиент получит 404 на /models.'
$ModelBase = Ask 'base_url' 'http://localhost:11434/v1'
$ModelName = Ask 'имя модели' 'qwen3:27b'
$ModelKey  = Ask 'api_key (Ollama игнорирует, но поле нужно)' 'ollama'

# ------------------------------------------------------------- 5. Лимиты
Write-Step 'Шаг 5/6 - границы автономии'
Write-Hint 'Модель 27B Q6 + KV Q8 занимает ~24 ГБ из 32 ГБ; остаток - бюджет экспериментов.'
$GpuFree = Ask 'Минимум свободной VRAM для запуска, ГБ' '6'
$DailyBudget = Ask 'Суточный бюджет GPU-часов' '8'
$Approval = Ask 'Прогон дороже N часов - спрашивать в Telegram' '6'
$Autolaunch = if (Ask-YesNo 'Автозапуск экспериментов без спроса?' $true) { 'true' } else { 'false' }

# ------------------------------------------------------------- Подтверждение
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

# ------------------------------------------------------------- 6. Установка
Write-Step 'Шаг 6/6 - установка'

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

# config.yaml — подстановка без внешних YAML-библиотек
$cfgSrc = Join-Path $SrcDir 'config.yaml'
$cfgDst = Join-Path $Target 'config.yaml'
$cfg = Get-Content -Raw -Encoding UTF8 $cfgSrc
$cfg = $cfg.Replace('<<INSTALLER_PLATFORM>>', $Platform).
            Replace('<<INSTALLER_MODE>>', $(if ($DebugMode -eq 'true') { 'debug' } else { 'production' })).
            Replace('<<INSTALLER_DEBUG_MODE>>', $DebugMode).
            Replace('<<INSTALLER_MODEL_NAME>>', $ModelName).
            Replace('<<INSTALLER_MODEL_BASE_URL>>', $ModelBase).
            Replace('<<INSTALLER_GPU_FREE_GB>>', $GpuFree).
            Replace('<<INSTALLER_DAILY_GPU_HOURS>>', $DailyBudget).
            Replace('<<INSTALLER_APPROVAL_GPU_HOURS>>', $Approval).
            Replace('<<INSTALLER_AUTOLAUNCH>>', $Autolaunch)
[System.IO.File]::WriteAllText($cfgDst, $cfg, (New-Object System.Text.UTF8Encoding($false)))
Write-Ok 'config.yaml настроен'

# .env
$envPath = Join-Path $Target '.env'
if (Test-Path $envPath) {
    Copy-Item $envPath "$envPath.bak" -Force
    Write-Warn 'Старый .env сохранён как .env.bak'
}
$envLines = @(
    '# researchagen - секреты профиля. Не коммитить.',
    "TELEGRAM_BOT_TOKEN=$TgToken",
    "TELEGRAM_HOME_CHANNEL=$TgChat",
    "TELEGRAM_CRON_THREAD_ID=$TgThread",
    "TELEGRAM_ALLOWED_USERS=$TgUsers",
    "RESEARCHAGEN_MODEL_BASE_URL=$ModelBase",
    "RESEARCHAGEN_MODEL_NAME=$ModelName",
    "RESEARCHAGEN_MODEL_API_KEY=$ModelKey",
    "RESEARCHAGEN_HOME=$Target"
)
[System.IO.File]::WriteAllLines($envPath, $envLines, (New-Object System.Text.UTF8Encoding($false)))
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

# Скиллы и бандлы в общие каталоги Hermes
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

# Cron-задачи
if ($HasHermes) {
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
    Write-Host '  Установлено, но есть ошибки выше. Исправьте до включения автономии.' -ForegroundColor Yellow
}
Write-Host ''
Write-Host '  Дальше:'
Write-Host '    researchagen gateway start' -ForegroundColor Cyan -NoNewline
Write-Host '   # бот и cron в ОТДЕЛЬНОМ терминале' -ForegroundColor DarkGray
Write-Host '    researchagen chat' -ForegroundColor Cyan -NoNewline
Write-Host '            # ручная сессия' -ForegroundColor DarkGray
Write-Host "    cd `"$Target`"; $Py tools\rg.py status" -ForegroundColor Cyan
Write-Host ''
Write-Host '  Основной агент не затронут: другой профиль, другой токен, другой терминал.' -ForegroundColor DarkGray
Write-Host ''
exit $rc
