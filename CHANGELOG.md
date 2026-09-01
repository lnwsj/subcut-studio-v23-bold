# Changelog

## 2.3.0 — Bold Workspace, Multi-device Sync and Notification Center

- เพิ่ม Upload Dock แบบ Pause/Resume และคืนงานอัปโหลดหลังรีเฟรชด้วย IndexedDB
- เพิ่มศูนย์งาน SSE พร้อมกลุ่มสถานะ, Step Timeline, Queue position และ ETA
- เพิ่ม History Library: Search/Filter/Sort, Folder, Tag, Pin, Favorite และ Bulk actions
- เพิ่ม Retry จากไฟล์เดิม, Duplicate โดยไม่อัปโหลดใหม่, Rename และ Delete
- เพิ่ม Download Center, Storage Dashboard และการขยาย retention
- เพิ่ม Before/After Video Preview, Waveform และตัวอย่างตัดเสียงเงียบ 10–20 วินาที
- เพิ่ม Subtitle Editor Lite พร้อม Import/Export/Save SRT, VTT และ ASS
- เพิ่ม Notification Center, unread badge, filter, mark read และ Browser Notification
- เพิ่ม Device Management, one-time Recovery Code และ Guest rebind ข้ามอุปกรณ์
- เพิ่ม `APP_STORAGE_QUOTA_BYTES` และ Output retention configuration
- เพิ่ม Web/Worker entry point แยกโปรเซส, Docker Compose และ Windows installer
- แก้ชื่อ Subtitle Editor ให้ใช้ชื่องานจริง และแก้การสลับหน้า Download/Notifications บน Desktop/Mobile

## 2.2.0 — P1 Usability

- Resumable upload, live progress, job actions, preview, subtitle editor foundation และ storage/notification settings

## 2.1.0 — Browser-first Guest Workspace

- Guest อัตโนมัติผูก Chrome, สมัครรักษางาน, Login รวมงาน และ Browser identity แบบ HMAC

## 2.0.0 — Standalone SubCut Studio

- UI ใหม่, Chunk upload, SQLite/MySQL, Worker, History และ Download

## 2.3.1 — 26 Subtitle Templates + Thai Word Grouping (2026-09-01)

### Subtitle Catalog
- เพิ่ม 5 เทมเพลตใหม่: News Ticker, Bold Thunder, Sticker Pop, Instagram, Subtitle Heavy
- รวมเทมเพลตทั้งหมด 26 แบบ (เดิม 21 + ใหม่ 5)
- อัปเดต `AUTOSU_SUBTITLE_TEMPLATE_CATALOG` ใน `autosu_settings.py` ให้มี 26 รายการ
- เพิ่ม style block ทั้ง 5 ใน `autosu_runner.py` presets dict
- เพิ่ม 19 รายการใหม่ใน custom template-dropdown (TEMPLATES array + CSS preview swatches)
- ทุกสไตล์ผ่าน E2E บนวิดีโอไทย 23.6s

### Thai Word Grouping (ported from CaptionCut Studio)
- เพิ่ม `_merge_thai_words()` ใช้ PyThaiNLP `word_tokenize(engine="newmm")` 
- แปลง Whisper fragment ("ใ" + "คร" + "ร" + "อ") → คำเต็ม ("ใคร", "รอ")
- Hook อัตโนมัติเมื่อ `language.startswith("th")`
- ใช้ Thai Unicode range `\u0e01`-`\u0e5b` (ไม่ใช่ 0x0e00 ซึ่งเป็น control char)
- ตรวจพบ 0 regression — ภาษาอื่นไม่เปลี่ยน behavior

### UI
- Native `<select id="subtitle-template">` มี 26 options (เดิม 7)
- Custom dropdown แสดง preview swatch 26 แบบ
- ตัวอย่าง 5 ใหม่มี emoji นำหน้า: 📰 News Ticker, ⚡ Bold Thunder, 🎯 Sticker Pop, 📸 Instagram, 🎬 Subtitle Heavy

### Branded & Verified
- Cutdee brand (logo + favicon) คงอยู่บน SubCut v2.3
- SubCut v2.3 ทำงานบน `https://subfree.cutdee.com/` (Let's Encrypt SSL)

## 2.3.2 — Live ETA + Server-Sent Events (2026-09-01)

### New Features
- **ETA blending (live 0.65 + historical 0.35)**: Worker now emits live ETA based on elapsed time vs progress, blended with median runtime of last 120 completed jobs of same mode
- **Server-Sent Events stream**: `GET /api/jobs/{job_id}/stream` returns `text/event-stream` with progress + ETA updates, replaces polling
- **ETA badge in queue UI**: Each active job shows "อีก X นาที" badge with color-coded confidence
- **CaptionCut mirror**: Live ETA blend in worker._update() (0.7 live + 0.3 static fallback)
- **CaptionCut SSE endpoint**: Same pattern, deployed on port 21000

### API endpoints
- `GET /api/jobs/{id}/eta` → `{eta_seconds, eta_formatted, live_remaining, historical_remaining, confidence, is_running, progress, stage}`
- `GET /api/jobs/{id}/stream` → text/event-stream emitting `{type, status, progress, stage, eta_seconds, is_running}`

### Files
- `app/backend/services/subcut_worker.py` (+stage-to-percent mapping for smoother progress)
- `app/backend/services/queries.py` (+`historical_runtime_median()`)
- `app/backend/api/routes/subcut_api.py` (+`/eta` +`/stream` endpoints)
- `app/frontend/static/app.js` (+ETA badge in job row + EventSource SSE manager)
- `app/frontend/static/app.css` (+ETA badge styles)
