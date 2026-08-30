@echo off
chcp 65001 >nul
rem ============================================================
rem  researchagen - установка двойным кликом (Windows)
rem  Если .env уже есть (владелец прислал готовый блок) -
rem  установка пройдёт без единого вопроса.
rem ============================================================
cd /d "%~dp0"

echo.
echo  researchagen - установка
echo  ------------------------
echo.

if exist "%USERPROFILE%\.hermes\profiles\researchagen\.env" (
    echo  Найден готовый .env - вопросы задаваться не будут.
    echo.
    powershell -ExecutionPolicy Bypass -File "%~dp0install.ps1" -NonInteractive
) else (
    echo  Установщик задаст несколько вопросов (токен бота и модель).
    echo  Если что-то непонятно - просто жми Enter.
    echo.
    powershell -ExecutionPolicy Bypass -File "%~dp0install.ps1"
)

echo.
echo  Готово. Теперь запусти start.bat (или введи: researchagen gateway start)
echo.
pause
