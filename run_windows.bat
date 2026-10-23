@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONUNBUFFERED=1
title Ирина
if exist "%~dp0mpv\mpv.exe" set "PATH=%~dp0mpv;%PATH%"
if not exist .venv\Scripts\python.exe (
  echo Сначала запустите install_windows.bat
  pause
  exit /b 1
)

echo Адреса этого компьютера ^(его вписывают в клиентах ESP32 как адрес сервера^):
ipconfig | findstr /c:"IPv4"
echo Веб-интерфейс: http://localhost:8765/
echo.

:loop
.venv\Scripts\python runva_rooms.py
rem код 3 — перезапуск из веб-интерфейса
if %errorlevel%==3 (
  echo Перезапуск...
  goto loop
)
echo.
echo Ирина остановилась ^(код %errorlevel%^).
pause
