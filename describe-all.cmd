@echo off
setlocal enabledelayedexpansion

rem Путь к describe-video.py (в папке /e/raw/)
set SCRIPT=E:\raw\describe-video.py

for %%F in (*.mp4) do (
    echo.
    echo ========================================
    echo Обработка: %%F
    echo ========================================
    python %SCRIPT% "%%F"
    if !errorlevel! neq 0 (
        echo ОШИБКА при обработке %%F (код !errorlevel!)
    )
    echo.
)

echo Готово. Все файлы обработаны.
pause
