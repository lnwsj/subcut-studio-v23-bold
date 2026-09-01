# Operations — v2.3

## Health

```bash
curl -s http://127.0.0.1:8787/api/health
```

Web แบบแยก Worker ต้องเห็น `embedded_workers: 0`; Worker log ต้องมี `Started N worker(s)`

## การรัน

- เครื่องเดียว/Beta: `APP_ENABLE_WORKER=1` แล้ว `scripts/start.sh`
- Production: `APP_ENABLE_WORKER=0`, เปิด `start.sh` และ `start-worker.sh` แยกกัน
- Docker: `docker compose -f deploy/docker-compose.yml up --build -d`

## Backup

สำรองพร้อมกัน:

- SQLite `app/data/jobs.db*` หรือ MySQL dump
- `app/data/user_workspaces/`
- `app/.env` โดยเฉพาะ `APP_BROWSER_IDENTITY_SECRET`
- Whisper model cache เมื่อไม่ต้องการดาวน์โหลดใหม่

## Guest recovery

- Cookie หายแต่ Local Storage อยู่: หน้าเว็บคืน Guest ได้
- Local Storage หายแต่ Cookie อยู่: server คืน Guest ได้
- ทั้งสองหาย: ใช้ Recovery Code หรือ Login สมาชิก
- Recovery Code เป็นแบบครั้งเดียว; การสร้างรหัสใหม่ยกเลิกรหัสเก่า

## Capacity / Storage

- เริ่ม `APP_WORKER_MAX_CONCURRENCY=1` ต่อ GPU
- กำหนด `APP_STORAGE_QUOTA_BYTES`
- Download Center ใช้ `APP_OUTPUT_RETENTION_DAYS`; ผู้ใช้ขยายได้ไม่เกิน `APP_OUTPUT_RETENTION_EXTENSION_MAX_DAYS`
- ตรวจพื้นที่จริงและทำ maintenance/backup policy ของระบบปฏิบัติการควบคู่กัน

## Notification channels

Notification Center และ Browser Notification ใช้งานได้ในตัว ส่วน LINE/Email fields เป็น provider preferences ต้องต่อ sender/credential ของระบบองค์กรก่อนใช้งานส่งออกจริง

## Incident checklist

- Upload ค้าง: `/api/jobs/{id}/upload/status`, `.subcut_upload_session.json`, `.subcut_chunks`
- Job ค้าง: lease/heartbeat, Web/Worker ใช้ DB และ Workspace ชุดเดียวกัน
- Video preview 404: ownership, output path, retention
- ซับเพี้ยน: ตรวจ SRT/VTT/ASS ใน Subtitle Editor ก่อน Burn
- CUDA error: ตรวจ PyTorch/CUDA, ลด model และคง 1 Worker/GPU
