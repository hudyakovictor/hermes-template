# ============================================================
#  researchagen — установка одной строкой (для друга)
#
#  Использование (в PowerShell, одна строка):
#    powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/<ваш-логин>/researchagen/main/setup.ps1 | iex"
#
#  Или с готовым токеном (сначала скачать, потом запустить с параметрами):
#    irm https://raw.githubusercontent.com/<ваш-логин>/researchagen/main/setup.ps1 -OutFile setup.ps1
#    .\setup.ps1 -Token "123:abc"
#    .\setup.ps1 -Token "123:abc" -ModelBase "http://localhost:11434/v1" -ModelName "qwen3:27b"
#
#  Что делает:
#    1. Проверяет Python (если нет — подсказывает).
#    2. Скачивает/обновляет проект в %USERPROFILE%\researchagen.
#    3. Создаёт .env из переданных параметров ИЛИ подхватывает готовый .env.
#    4. Запускает install.ps1 без вопросов (-NonInteractive), если всё известно.
#    5. Печатает команду запуска.
# ============================================================

param(
    [string]$Token = "",
    [string]$ModelBase = "",
    [string]$ModelName = "",
    [string]$ChatId = "",
    [string]$Users = "",
    [string]$Repo = "https://github.com/<ваш-логин>/researchagen"
)

$ErrorActionPreference = 'Stop'
$ProfileName = 'researchagen'
$AppDir = Join-Path $env:USERPROFILE $ProfileName
$EnvDir  = Join-Path (Join-Path $env:USERPROFILE '.hermes\profiles') $ProfileName
$EnvPath = Join-Path $EnvDir '.env'

# --- 0. Проверка: владелец должен заменить <ваш-логин> в $Repo --------------
if ($Repo -match '<' -or $Repo -match 'ваш-логин') {
    Write-Host ''
    Write-Host '  setup.ps1 ещё не настроен: в $Repo стоит плейсхолдер <ваш-логин>.' -ForegroundColor Yellow
    Write-Host '  Владелец: замените его на адрес вашего репозитория и обновите файл' -ForegroundColor Yellow
    Write-Host '  в GitHub, затем передайте другу новую строку установки.' -ForegroundColor Yellow
    exit 1
}

Write-Host ''
Write-Host '  researchagen — установка' -ForegroundColor White
Write-Host ('  ' + ('-' * 55)) -ForegroundColor DarkGray

# --- 1. Python ---------------------------------------------------------------
$Py = $null
foreach ($cand in @('python', 'py')) {
    $cmd = Get-Command $cand -ErrorAction SilentlyContinue
    if (-not $cmd) { continue }
    try {
        $ver = & $cand -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>$null
        if ($LASTEXITCODE -eq 0 -and $ver) { $Py = $cand; break }
    } catch { }
}
if (-not $Py) {
    Write-Host '  НЕ НАЙДЕН PYTHON' -ForegroundColor Red
    Write-Host '  Скачай и установи: https://www.python.org/downloads/' -ForegroundColor Yellow
    Write-Host '  ВАЖНО: отметь галочку "Add python.exe to PATH", затем закрой' -ForegroundColor Yellow
    Write-Host '  и открой PowerShell заново и повтори эту строку.' -ForegroundColor Yellow
    exit 1
}
Write-Host "  [1/5] Python: $(& $Py -V 2>&1)" -ForegroundColor Green

# --- 2. Проект ---------------------------------------------------------------
New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
Push-Location $AppDir
try {
    if (Test-Path (Join-Path $AppDir 'install.ps1')) {
        Write-Host '  [2/5] Проект уже скачан, обновляю...' -ForegroundColor Green
        if (Get-Command git -ErrorAction SilentlyContinue) { & git pull --ff-only 2>$null | Out-Null }
    } else {
        Write-Host '  [2/5] Скачиваю проект...' -ForegroundColor Green
        if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
            Write-Host '  НЕ НАЙДЕН GIT. Скачай и установи: https://git-scm.com/download/win' -ForegroundColor Yellow
            exit 1
        }
        & git clone --depth 1 $Repo . | Out-Null
    }
} finally {
    Pop-Location
}

# --- 3. .env -----------------------------------------------------------------
$envExists = Test-Path $EnvPath
New-Item -ItemType Directory -Force -Path $EnvDir | Out-Null

# .env уже есть — подхватываем значения, дописываем только переданные
$map = @{}
if ($envExists) {
    foreach ($line in [System.IO.File]::ReadAllLines($EnvPath)) {
        $t = $line.Trim()
        if ($t -eq '' -or $t.StartsWith('#')) { continue }
        $i = $t.IndexOf('=')
        if ($i -le 0) { continue }
        $map[$t.Substring(0, $i).Trim()] = $t.Substring($i + 1).Trim()
    }
}
if ($Token)    { $map['TELEGRAM_BOT_TOKEN'] = $Token }
if ($ChatId)   { $map['TELEGRAM_HOME_CHANNEL'] = $ChatId }
if ($Users)    { $map['TELEGRAM_ALLOWED_USERS'] = $Users }
if ($ModelBase){ $map['RESEARCHAGEN_MODEL_BASE_URL'] = $ModelBase }
if ($ModelName){ $map['RESEARCHAGEN_MODEL_NAME'] = $ModelName }
if (-not $map.ContainsKey('RESEARCHAGEN_MODEL_BASE_URL')) { $map['RESEARCHAGEN_MODEL_BASE_URL'] = 'http://localhost:11434/v1' }
if (-not $map.ContainsKey('RESEARCHAGEN_MODEL_NAME'))     { $map['RESEARCHAGEN_MODEL_NAME'] = 'qwen3:27b' }
if (-not $map.ContainsKey('RESEARCHAGEN_MODEL_API_KEY'))  { $map['RESEARCHAGEN_MODEL_API_KEY'] = 'ollama' }
if (-not $map.ContainsKey('RESEARCHAGEN_HOME'))           { $map['RESEARCHAGEN_HOME'] = $EnvDir }

$lines = New-Object System.Collections.Generic.List[string]
if ($envExists) {
    $seen = @{}
    foreach ($line in [System.IO.File]::ReadAllLines($EnvPath)) {
        $t = $line.Trim()
        if ($t -ne '' -and -not $t.StartsWith('#') -and $t.IndexOf('=') -gt 0) {
            $k = $t.Substring(0, $t.IndexOf('=')).Trim()
            if ($map.ContainsKey($k)) {
                $lines.Add("$k=$($map[$k])")
                $seen[$k] = $true
                continue
            }
        }
        $lines.Add($line)
    }
    foreach ($k in $map.Keys) {
        if (-not $seen.ContainsKey($k)) { $lines.Add("$k=$($map[$k])") }
    }
} else {
    $lines.Add('# researchagen — секреты профиля. Не коммитить.')
    foreach ($k in $map.Keys) { $lines.Add("$k=$($map[$k])") }
}
[System.IO.File]::WriteAllLines($EnvPath, $lines, (New-Object System.Text.UTF8Encoding($false)))

if ($map.ContainsKey('TELEGRAM_BOT_TOKEN') -and $map['TELEGRAM_BOT_TOKEN']) {
    Write-Host '  [3/5] Настройки записаны (или подхвачены из готового .env)' -ForegroundColor Green
} else {
    Write-Host '  [3/5] .env пока без токена — спрошу его дальше' -ForegroundColor DarkGray
}

# --- 4. Установка --------------------------------------------------------------
Write-Host '  [4/5] Установка...' -ForegroundColor Green
$hasToken = $map.ContainsKey('TELEGRAM_BOT_TOKEN') -and $map['TELEGRAM_BOT_TOKEN']
if ($hasToken) {
    & powershell -ExecutionPolicy Bypass -File (Join-Path $AppDir 'install.ps1') -NonInteractive
} else {
    & powershell -ExecutionPolicy Bypass -File (Join-Path $AppDir 'install.ps1')
}

# --- 5. Итог -------------------------------------------------------------------
Write-Host ''
Write-Host '  [5/5] Готово. Запуск — одной командой:' -ForegroundColor White
Write-Host '    researchagen gateway start' -ForegroundColor Cyan
Write-Host '  (или двойной клик по start.bat в папке проекта)' -ForegroundColor DarkGray
Write-Host '  Это окно не закрывай, пока бот должен работать.' -ForegroundColor DarkGray
Write-Host ''
