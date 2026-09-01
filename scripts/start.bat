@echo off
setlocal
cd /d "%~dp0\.."
if not exist ".venv\Scripts\python.exe" (
  echo ไม่พบ virtualenv กรุณาสร้างด้วย: py -m venv .venv
  exit /b 1
)
cd app
"..\.venv\Scripts\python.exe" main.py
