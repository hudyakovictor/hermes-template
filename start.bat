@echo off
chcp 65001 >nul
rem ============================================================
rem  researchagen - запуск бота (двойной клик)
rem  Это окно НЕ закрывать, пока бот должен работать.
rem ============================================================
cd /d "%~dp0"

echo.
echo  researchagen — запуск бота...
echo  Это окно НЕ закрывать. Для остановки закрой окно.
echo.

where researchagen >nul 2>nul
if %errorlevel%==0 (
    researchagen gateway start
) else (
    powershell -ExecutionPolicy Bypass -Command "researchagen gateway start"
)

echo.
echo  Шлюз остановлен. Если он не запустился — сначала запустите setup.bat
echo  или обратитесь к владельцу (нужен agent-hermes).
echo.
pause
