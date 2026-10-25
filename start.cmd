@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" server.py --host 127.0.0.1 --port 8765 --open
  goto finished
)
where py >nul 2>nul
if not errorlevel 1 (
  py -3 server.py --host 127.0.0.1 --port 8765 --open
  goto finished
)
where python >nul 2>nul
if errorlevel 1 (
  echo Python 3.10 or newer is required. Please install Python and try again.
  pause
  exit /b 1
)
python server.py --host 127.0.0.1 --port 8765 --open
:finished
if errorlevel 1 pause
endlocal
