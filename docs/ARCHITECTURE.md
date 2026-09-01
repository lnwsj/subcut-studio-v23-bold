# Architecture — Bold v2.3

```text
Chrome / Mobile Browser
  ├─ local Browser Key + HttpOnly SameSite cookie
  ├─ IndexedDB resumable upload tasks
  ├─ SSE live hub
  └─ Workspace / Library / Download / Notifications / Settings
                 │
FastAPI Web (APP_ENABLE_WORKER=0 in production)
  ├─ Auth + browser-bound Guest
  ├─ Hub / Library / Devices / Recovery / Notifications
  ├─ Chunk Upload + SHA-256
  ├─ Media/Waveform/Subtitle APIs
  └─ SQLite or MySQL
                 │ shared DB + workspace
Standalone Worker
  ├─ lease + heartbeat + fencing token
  ├─ FFmpeg silence trim
  ├─ Whisper subtitle flow
  └─ output publication and job events
```

## Identity and multi-device

Guest ใช้ user row จริงและ `browser_identities` ที่เก็บเฉพาะ HMAC ของ Browser Key สมาชิกใช้ Login ข้ามเครื่อง ส่วน Guest ใช้ Recovery Code ที่ hash ในฐานข้อมูล ใช้ได้ครั้งเดียวและหมดอายุ จากนั้น Browser identity ใหม่จะถูกผูกกับ `user_id` เดิม

## Upload reliability

ไฟล์ใหญ่ถูกแบ่ง Chunk ฝั่ง Browser งานและ File Blob ถูกเก็บใน IndexedDB แต่ละ Chunk มี SHA-256 และ server มี upload manifest สำหรับถาม `received_indices` หลังรีเฟรช เมื่อครบจึงประกอบแบบ atomic แล้วค่อยส่ง Job เข้าคิว

## Live state

`/api/hub/stream` ส่ง snapshot เฉพาะเมื่อ version เปลี่ยน พร้อม heartbeat เป็นระยะ UI จึงเห็นเปอร์เซ็นต์, Step, ETA, Queue position, storage และ unread notifications โดยไม่ poll ตารางใหญ่ถี่ ๆ

## Data model ที่เพิ่ม

- `job_library_meta`: ชื่อ, Folder, Tag, Pin, Favorite, retention
- `job_folders`: Folder ต่อผู้ใช้
- `notifications`: event แบบ idempotent
- `notification_preferences`: Browser/LINE/Email preferences
- `guest_recovery_codes`: one-time hashed code
- `browser_identities`: อุปกรณ์ที่ผูกบัญชี

## Security boundaries

- ทุก Job route ตรวจ `user_id`
- ทุก path ต้องอยู่ภายใน `APP_USER_WORKSPACE_ROOT`
- Password PBKDF2-HMAC-SHA256; access/refresh token มี expiry/revoke
- Browser Key ไม่เก็บแบบ plaintext
- Download/Media ใช้ token และ ownership check
- Production ต้องใช้ HTTPS, reverse proxy, rate limit และ secret manager
