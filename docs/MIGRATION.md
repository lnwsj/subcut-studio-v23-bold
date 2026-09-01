# Migration จากระบบเดิม

## ขั้นตอน

1. สำรองฐานข้อมูลและ Workspace เดิมเป็นชุดเดียวกัน
2. ติดตั้ง 2.1 ใน Path ใหม่ก่อน อย่าเขียนทับ Runtime เดิมทันที
3. ชี้ `APP_DB_ENGINE`, DB credentials และ `APP_USER_WORKSPACE_ROOT` ไปข้อมูลจริง
4. ตั้ง `APP_LEGACY_UI_JOBS_DIR` หาก Job เก่าอยู่นอก User workspace ปัจจุบัน
5. ใช้ `APP_AUTH_SECRET` เดิมเมื่อต้องการให้ Access token เดิมยังใช้ต่อ
6. กำหนด `APP_BROWSER_IDENTITY_SECRET` ค่าสุ่มยาว **ก่อนเปิด Guest** และเก็บค่านี้ถาวร
7. เริ่มระบบหนึ่งครั้งเพื่อสร้าง `browser_identities` อัตโนมัติ
8. ตรวจ `/api/health`, Guest ใหม่, Member login, History และ Download ก่อนสลับ DNS

## Compatibility

- Schema เดิมไม่ถูกลบ; ระบบเพิ่ม Column/Table ที่จำเป็นแบบ additive
- เพิ่มตาราง `browser_identities` อ้าง `users.id`
- Guest ไม่ถูกนับเป็น Owner bootstrap และไม่แสดงในรายชื่อ Member
- Job เดิมรองรับ `autosu_only` และ `silence_trim_only`; Mode อื่นจะไม่ถูก Worker นี้ Claim
- เมื่อ Login รวม Guest เข้าบัญชี ระบบเปลี่ยน `jobs.user_id` แต่ไม่ย้ายไฟล์ที่อาจกำลังรัน

## Secret rotation

การเปลี่ยน `APP_BROWSER_IDENTITY_SECRET` ทำให้ HMAC ของ Browser key เดิมเปลี่ยนและ Guest ที่ยังไม่สมัครจับคู่ไม่ได้ สมาชิกยัง Login ได้ตามอีเมล/รหัสผ่าน ก่อน Rotate ต้องทำ Migration ตาราง `browser_identities` โดยมี Secret เก่าและใหม่พร้อมกัน

## Rollback

- หยุด Service ใหม่และคืน Reverse Proxy/DNS
- Restore DB snapshot + Workspace snapshot ชุดเดียวกันเมื่อต้อง Rollback เคร่งครัด
- Column/Table ใหม่เป็น Additive จึงไม่ควรขัดระบบเดิม แต่ให้ทดสอบกับสำเนาฐานจริงก่อนเสมอ
