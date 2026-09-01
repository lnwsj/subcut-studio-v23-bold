# QA Report — SJ88 SubCut Studio 2.3.0 Bold

## ผลรวม

**PASS_WITH_DEPLOYMENT_NOTES** — ฟีเจอร์ Bold, Guest/Member sync, Upload/Queue/History/Download, Notification Center, Subtitle Editor, Trim Preview และ Web/Worker แยกโปรเซสผ่านการทดสอบ

## Functional QA

- Bold API integration: Upload status/resume contract, Library metadata, SSE, ETA, Storage, Download Center, Notifications, 2-device sync, Recovery, Duplicate และ Bulk actions ผ่าน
- ไฟล์จริง 18 วินาที: Upload SHA-256 → Worker FFmpeg → Done → Import SRT → Export VTT → Save SRT/VTT/ASS → Download Center ผ่าน
- Trim Preview 15 วินาที: ตัดช่วงเงียบจริง 3.87 วินาทีและ Stream MP4 กลับได้
- Web/Worker แยกโปรเซส: Web รายงาน `embedded_workers=0`; Worker ภายนอกประมวลผลงานเสร็จ 1 Output
- Subtitle format unit tests: 4/4 ผ่าน
- Python compile และ JavaScript syntax ผ่าน

## Browser / Responsive QA

Playwright Chromium เปิด Live Uvicorn + API จริง โดยใช้ fallback เพราะ `agent-browser` CLI ไม่มีในเครื่อง QA

- Desktop/Mobile horizontal overflow = 0
- Console errors = 0, Page errors = 0, failed requests ที่ไม่คาดหมาย = 0
- Work Center, Upload Dock restore, Library, Download Center, Notification Center, Settings และ Mobile navigation ผ่าน
- Subtitle Editor Desktop/Mobile และ Trim Preview/Waveform ผ่าน

## Fidelity ledger

| จุดตรวจ | หลักฐาน | ผล |
|---|---|---|
| Brand/sidebar/palette เดิม | `preview.png` เทียบ `bold-desktop-work-center-viewport.png` | รักษาระบบภาพเดิม |
| Status hierarchy | Work Center groups + Step Timeline | อ่านสถานะได้ทันที |
| Download/Notification navigation | ภาพหน้าจอแต่ละ View | สลับเนื้อหาจริง ไม่ค้าง View เดิม |
| Modal typography/spacing | Subtitle Editor + Trim Preview | สอดคล้อง UI หลัก |
| Mobile layout | Work Center/Download/Editor 390×844 | ไม่มี overflow และปุ่มหลักไม่ถูกตัด |
| Above-the-fold copy | ตรวจ DOM/ภาพจริง | เพิ่มเฉพาะข้อความที่จำเป็นตามฟีเจอร์ Bold |

## หลักฐานสำคัญ

- `api/bold-v2.3-integration.json`
- `api/bold-browser-qa.json`
- `api/subtitle-editor-trim-preview.json`
- `api/bold-editor-preview-browser-qa.json`
- `api/split-web-worker.json`
- `api/subtitle-format-unit-tests.txt`
- `test_matrix.json`, `pairs/ui_api_mapping.json`, `screenshots/`

## Deployment notes

- Whisper GPU/model จริงไม่ได้รันในรอบ QA นี้; ทดสอบเส้นทาง FFmpeg จริงและทดสอบ Subtitle format โดยตรง
- Docker CLI ไม่มีในเครื่อง QA จึง parse Compose YAML และทดสอบ Web/Worker ด้วยโปรเซสจริงแทน แต่ไม่ได้ Build image
- Notification Center และ Browser Notification ใช้งานจริง; LINE/Email เป็น preference สำหรับเชื่อม sender/credential ขององค์กร
