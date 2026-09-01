@echo off
setlocal
cd /d "%~dp0\.."
where ffmpeg >nul 2>nul || (echo ไม่พบ ffmpeg ใน PATH & exit /b 1)
where ffprobe >nul 2>nul || (echo ไม่พบ ffprobe ใน PATH & exit /b 1)
py -m venv .venv || exit /b 1
".venv\Scripts\python.exe" -m pip install --upgrade pip wheel setuptools || exit /b 1
".venv\Scripts\python.exe" -m pip install -r app\requirements.txt || exit /b 1
if not exist app\.env copy app\.env.example app\.env >nul
".venv\Scripts\python.exe" scripts\prepare_env.py app\.env || exit /b 1
echo ติดตั้งเสร็จแล้ว
 echo เริ่มแบบเครื่องเดียว: scripts\start.bat
 echo แยก Worker: scripts\start-worker.bat
