@echo off
REM One-click launcher for the DeepSeek Responses repair proxy.
REM Dependency check is import-based (not venv-existence-based), so a
REM half-failed first install gets retried on the next run.
REM Install uses the TUNA PyPI mirror first (works in CN networks), with
REM official PyPI as fallback.
REM Optional env vars (see README): PROXY_HOST, PROXY_PORT, UPSTREAM_URL,
REM PROXY_API_KEY, ORPHAN_STRATEGY, MISSING_ID_STRATEGY, DEBUG_DUMP, LOG_LEVEL.
cd /d "%~dp0"

if not defined PROXY_HOST set PROXY_HOST=127.0.0.1
if not defined PROXY_PORT set PROXY_PORT=8080

REM --- dependency check: are the runtime deps actually importable? ---
".venv\Scripts\python.exe" -c "import uvicorn, fastapi, httpx" >nul 2>nul
if not errorlevel 1 goto :run

echo [setup] First run (or incomplete install): installing dependencies...

if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
    if errorlevel 1 goto :fail
)

".venv\Scripts\python.exe" -m pip install --index-url https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn -r requirements.txt
if errorlevel 1 (
    echo [setup] Mirror failed, retrying with official PyPI...
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 goto :fail
)

".venv\Scripts\python.exe" -c "import uvicorn, fastapi, httpx" >nul 2>nul
if errorlevel 1 goto :fail

:run
echo [run] Proxy listening on http://%PROXY_HOST%:%PROXY_PORT%
if defined UPSTREAM_URL echo [run] Upstream: %UPSTREAM_URL%
".venv\Scripts\python.exe" -m uvicorn app.main:app --host %PROXY_HOST% --port %PROXY_PORT%
pause
exit /b 0

:fail
echo [error] Dependency installation failed. Check your network connection,
echo [error] or install manually: .venv\Scripts\pip.exe install -r requirements.txt
pause
exit /b 1
