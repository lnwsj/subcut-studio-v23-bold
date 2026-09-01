# API Reference — v2.3

ทุก endpoint ข้อมูลส่วนตัวใช้ `Authorization: Bearer <access_token>` หน้าเว็บสร้าง Guest token ให้อัตโนมัติ

## Auth / Device Sync

| Method | Path | หน้าที่ |
|---|---|---|
| POST | `/api/auth/browser` | สร้าง/คืน Guest ที่ผูก Browser key |
| POST | `/api/auth/register` | อัปเกรด Guest เป็น Member โดยรักษางาน |
| POST | `/api/auth/login` | Login และรวม Guest jobs |
| GET | `/api/auth/me` | บัญชีปัจจุบัน |
| POST | `/api/account/recovery-code` | Recovery Code ครั้งเดียว |
| POST | `/api/auth/recover-browser` | ผูก Chrome ใหม่เข้าบัญชีเดิม |
| GET | `/api/devices` | อุปกรณ์ที่ซิงก์ |
| PATCH/DELETE | `/api/devices/{id}` | เปลี่ยนชื่อ/ถอนอุปกรณ์ |

## Job / Upload / Live Hub

| Method | Path | หน้าที่ |
|---|---|---|
| POST | `/api/jobs` | สร้างงาน |
| POST | `/api/jobs/{id}/upload` | อัปโหลดตรง |
| POST | `/api/jobs/{id}/upload/chunk` | อัปโหลด Chunk + SHA-256 |
| POST | `/api/jobs/{id}/upload/chunked/complete` | ตรวจและประกอบ Chunk |
| GET | `/api/jobs/{id}/upload/status` | ชิ้นที่มี/ขาดเพื่อ Resume |
| POST | `/api/jobs/{id}/process` | ส่งเข้าคิว Worker |
| POST | `/api/jobs/{id}/cancel` | ยกเลิก |
| GET | `/api/hub` | Snapshot งาน/โฟลเดอร์/พื้นที่ |
| GET | `/api/hub/stream` | SSE progress, ETA และ notification count |

## Library / Actions

| Method | Path | หน้าที่ |
|---|---|---|
| PATCH | `/api/jobs/{id}/meta` | ชื่อ, Folder, Tag, Pin, Favorite |
| POST | `/api/jobs/{id}/retry` | Retry โดยใช้ไฟล์เดิม |
| POST | `/api/jobs/{id}/duplicate` | Duplicate งานและ Source |
| DELETE | `/api/jobs/{id}` | ลบงานและ Workspace |
| POST | `/api/jobs/bulk` | Bulk pin/favorite/move/tag/retry/delete |
| GET/POST | `/api/folders` | อ่าน/สร้าง Folder |
| DELETE | `/api/folders/{id}` | ลบ Folder |

## Download / Preview

| Method | Path | หน้าที่ |
|---|---|---|
| GET | `/api/download-center` | ไฟล์ทุกเวอร์ชันและ storage |
| POST | `/api/jobs/{id}/retention/extend` | ขยายเวลาเก็บ |
| GET | `/api/jobs/{id}/media/source/{index}` | Stream Source |
| GET | `/api/jobs/{id}/media/output/{path}` | Stream Output |
| GET | `/api/jobs/{id}/waveform` | Waveform peaks |
| POST | `/api/jobs/{id}/trim-preview` | สร้างตัวอย่างตัด 10–20 วินาที |

## Subtitle Editor Lite

| Method | Path | หน้าที่ |
|---|---|---|
| GET | `/api/jobs/{id}/subtitles` | โหลด Cue ล่าสุด |
| POST | `/api/jobs/{id}/subtitles/import` | Parse SRT/VTT/ASS |
| POST | `/api/jobs/{id}/subtitles/export` | Render ไฟล์โดยไม่บันทึก |
| PUT | `/api/jobs/{id}/subtitles` | บันทึกหนึ่งหรือหลาย Format |

## Notification Center

| Method | Path | หน้าที่ |
|---|---|---|
| GET | `/api/notifications` | รายการและ unread count |
| POST | `/api/notifications/{id}/read` | อ่านรายการเดียว |
| POST | `/api/notifications/read-all` | อ่านทั้งหมด |
| POST | `/api/notifications/test` | สร้างรายการทดสอบ |
| GET/PATCH | `/api/notification-preferences` | Browser/LINE/Email preferences |
