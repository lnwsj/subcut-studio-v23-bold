@echo off
setlocal
cd /d "%~dp0\.."
if not exist ".venv\Scripts\python.exe" (
  echo ไม่พบ virtualenv กรุณารัน scripts\install.bat ก่อน
  exit /b 1
)
".venv\Scripts\python.exe" app\worker_main.py
