@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
  py -3 stop.py
  exit /b
)
python stop.py
endlocal
