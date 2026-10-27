@echo off
setlocal
chcp 65001 >nul

for %%I in ("%~dp0..") do set "PACKAGE_DIR=%%~fI"
for %%I in ("%PACKAGE_DIR%\..") do set "PROJECT_DIR=%%~fI"
set "PYTHON_EXE=%PACKAGE_DIR%\.venv\Scripts\python.exe"

if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python"

if /I "%GAME_AUTO_DRY_RUN%"=="1" (
    echo MODE=FLET_GUI
    echo PACKAGE_DIR=%PACKAGE_DIR%
    echo PROJECT_DIR=%PROJECT_DIR%
    echo PYTHON_EXE=%PYTHON_EXE%
    echo COMMAND=%PYTHON_EXE% -m game_auto.ui.flet_app
    exit /b 0
)

net session >nul 2>&1
if not "%errorlevel%"=="0" (
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)

cd /d "%PROJECT_DIR%"
"%PYTHON_EXE%" -m game_auto.ui.flet_app
set "EXIT_CODE=%ERRORLEVEL%"

rem 仅在异常退出时 pause，让用户看到错误信息；正常退出直接关闭
if not "%EXIT_CODE%"=="0" if not defined GAME_AUTO_NO_PAUSE pause
exit /b %EXIT_CODE%
