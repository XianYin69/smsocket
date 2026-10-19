@echo off
rem SMSocket kernel launcher - cmd.exe entry point.
rem usage: start.cmd [--port 8011] [--config config.yaml] [any run.py flag]
setlocal
cd /d "%~dp0"
set "PY=%SMSSOCKET_PYTHON%"
if "%PY%"=="" if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if "%PY%"=="" set "PY=python"
rem Upstream key env vars (AGNES_KEY_1/AGNES_KEY_2/SCNET_KEY_1) are NOT read
rem from the registry at launch time - they must already exist in the calling
rem process environment, or the gateway starts with 0 usable keys and every
rem /v1/ chat request answers 503 until they are added via the dashboard
rem (/admin/providers/<name>/keys) and the config is reloaded.
echo [start] %PY% run.py %*
"%PY%" run.py %*
endlocal
