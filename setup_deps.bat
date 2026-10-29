@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem ── 本脚本必须以普通权限运行 ───────────────────────────────────────────────
rem GUI/CLI 是提权进程（UIPI 要求），在提权会话里装包 = 把任意代码引入高完整性
rem 进程的环境。检测到管理员时用 explorer 以当前登录用户（非提权）重新拉起自己。
net session >nul 2>&1
if not errorlevel 1 (
    echo [提示] 检测到管理员权限，正在以普通权限重新启动安装脚本...
    explorer.exe "%~f0"
    exit /b
)

set "MIRROR=https://pypi.tuna.tsinghua.edu.cn/simple"
set "PY=.venv\Scripts\python.exe"
set "HAS_UV=0"
where uv >nul 2>&1
if not errorlevel 1 set "HAS_UV=1"

echo [1/4] 准备虚拟环境...
if not exist "%PY%" (
    if "%HAS_UV%"=="1" (
        uv venv .venv --python 3.13
    ) else (
        python -m venv .venv
    )
    if errorlevel 1 goto :fail
) else (
    echo       已存在: %PY%
)

if not exist requirements.lock (
    echo [错误] 缺少 requirements.lock，请先在本机生成并提交
    goto :fail
)

echo [2/4] 安装运行时依赖（requirements.lock，--no-deps 精确回放）...
if "%HAS_UV%"=="1" (
    uv pip install --python "%PY%" --no-deps -r requirements.lock --index-url %MIRROR%
) else (
    "%PY%" -m pip install --no-deps -r requirements.lock -i %MIRROR%
)
if errorlevel 1 goto :fail

echo [3/4] 校验 ONNX Runtime provider...
"%PY%" -c "import onnxruntime as o; p=o.get_available_providers(); print('providers:', p); raise SystemExit(0 if 'CUDAExecutionProvider' in p else 2)"
if errorlevel 2 (
    echo [警告] 未检测到 CUDAExecutionProvider，RapidOCR 会退回 CPU。
    echo        请确认装的是 onnxruntime-gpu，且没有被 CPU 版 onnxruntime 覆盖。
)

echo [4/4] 安装开发工具（pytest / ruff）...
if "%HAS_UV%"=="1" (
    uv pip install --python "%PY%" -r requirements-dev.txt --index-url %MIRROR%
) else (
    "%PY%" -m pip install -r requirements-dev.txt -i %MIRROR%
)

echo.
echo 依赖安装完成。之后用 00_START_HERE_GUI_ADMIN.bat 启动 GUI。
pause
exit /b 0

:fail
echo.
echo [失败] 依赖安装未完成，请查看上面的错误信息。
pause
exit /b 1
