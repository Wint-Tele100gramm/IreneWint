@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
title Установка Ирины

rem --- ищем Python: лучше 3.12 (для него есть все нужные пакеты), потом 3.13, 3.11
set "PY="
for %%V in (3.12 3.13 3.11) do (
  if not defined PY (
    py -%%V -c "import sys" >nul 2>nul && set "PY=py -%%V"
  )
)
if not defined PY (
  python -c "import sys" >nul 2>nul && set "PY=python"
)
if not defined PY goto nopython
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) and sys.maxsize > 2**32 else 1)"
if errorlevel 1 goto nopython
echo ==^> Python:
%PY% --version

if not exist .venv\Scripts\python.exe (
  echo ==^> Создаю окружение .venv
  %PY% -m venv .venv
  if errorlevel 1 goto failed
)

echo ==^> Ставлю пакеты (несколько минут)
.venv\Scripts\python -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto failed

.venv\Scripts\python scripts\windows\setup_windows.py
if errorlevel 1 goto failed

echo.
if not exist mpv\mpv.exe (
  echo Для Яндекс Музыки нужен mpv: скачайте mpv для Windows ^(x86_64^) с https://mpv.io/installation/
  echo и положите mpv.exe в папку mpv рядом с этим файлом. Без него всё остальное работает.
)
echo.
echo Установка завершена. Запуск: run_windows.bat
pause
exit /b 0

:nopython
echo Не найден 64-битный Python 3.10-3.13.
echo Скачайте Python 3.12 ^(Windows installer 64-bit^) с https://www.python.org/downloads/windows/
echo и при установке отметьте "Add python.exe to PATH". Потом запустите этот файл ещё раз.
pause
exit /b 1

:failed
echo.
echo Установка прервалась — пришлите текст выше.
pause
exit /b 1
