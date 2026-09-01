'use strict';

(() => {
  const defs = document.querySelector('.svg-defs');
  defs?.insertAdjacentHTML('beforeend', `
    <symbol id="i-pause" viewBox="0 0 24 24"><path d="M8 5v14M16 5v14"/></symbol>
    <symbol id="i-cloud" viewBox="0 0 24 24"><path d="M7 18h11a4 4 0 0 0 .6-7.9A7 7 0 0 0 5.2 8.2 5 5 0 0 0 7 18Z"/><path d="m9 13 2 2 4-5"/></symbol>
    <symbol id="i-pin" viewBox="0 0 24 24"><path d="m14 4 6 6-3 1-4 4v5l-2 2-1-6-5-5 2-2h5l4-4-2-1Z"/></symbol>
    <symbol id="i-star" viewBox="0 0 24 24"><path d="m12 3 2.7 5.5 6.1.9-4.4 4.3 1 6.1-5.4-2.9-5.4 2.9 1-6.1-4.4-4.3 6.1-.9L12 3Z"/></symbol>
    <symbol id="i-device" viewBox="0 0 24 24"><rect x="4" y="3" width="16" height="14" rx="2"/><path d="M8 21h8M12 17v4"/></symbol>
    <symbol id="i-tag" viewBox="0 0 24 24"><path d="M20 13 13 20 4 11V4h7l9 9Z"/><circle cx="8" cy="8" r="1"/></symbol>
    <symbol id="i-eye" viewBox="0 0 24 24"><path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/></symbol>
    <symbol id="i-edit" viewBox="0 0 24 24"><path d="M4 20h4L19 9l-4-4L4 16v4Z"/><path d="m13 7 4 4"/></symbol>
    <symbol id="i-copy" viewBox="0 0 24 24"><rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V5a1 1 0 0 0-1-1H5a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h3"/></symbol>
    <symbol id="i-filter" viewBox="0 0 24 24"><path d="M4 5h16l-6 7v6l-4 2v-8L4 5Z"/></symbol>
    <symbol id="i-shield" viewBox="0 0 24 24"><path d="M12 3 4 6v6c0 5 3.4 8 8 10 4.6-2 8-5 8-10V6l-8-3Z"/><path d="m9 12 2 2 4-5"/></symbol>
  `);

  const nav = document.querySelector('.side-nav');
  const queueNav = nav?.querySelector('[data-view="queue"] span');
  const historyNav = nav?.querySelector('[data-view="history"] span');
  if (queueNav) queueNav.textContent = 'ศูนย์งาน';
  if (historyNav) historyNav.textContent = 'คลังงาน';
  nav?.querySelector('[data-view="history"]')?.insertAdjacentHTML('afterend', `
    <button class="nav-item" data-view="downloads"><svg><use href="#i-download"/></svg><span>ดาวน์โหลด</span></button>
    <button class="nav-item" data-view="notifications"><svg><use href="#i-bell"/></svg><span>แจ้งเตือน</span><b class="nav-count hidden" id="notification-count">0</b></button>
  `);

  document.querySelector('.background-note')?.insertAdjacentHTML('beforebegin', `
    <button class="sync-card" data-view-jump="settings"><span class="sync-icon"><svg><use href="#i-cloud"/></svg></span><span><strong id="sync-label">กำลังเชื่อมต่อ</strong><small id="sync-time">ซิงก์คิวและประวัติ</small></span></button>
  `);

  const main = document.querySelector('.main-content');
  main?.insertAdjacentHTML('afterbegin', `
    <div class="sync-strip" id="sync-strip"><span class="sync-pulse"></span><strong id="sync-strip-label">ซิงก์ข้อมูลอัตโนมัติ</strong><small id="sync-strip-time">กำลังเชื่อมต่อเซิร์ฟเวอร์</small><button data-view-jump="notifications">${icon('i-bell')}<b id="top-notification-count" class="hidden">0</b></button></div>
  `);

  const queueHeader = document.querySelector('#view-queue .page-header');
  if (queueHeader) queueHeader.querySelector('h1').textContent = 'ศูนย์งานของฉัน';
  if (queueHeader) queueHeader.querySelector('p:last-child').textContent = 'เห็นอัปโหลด คิว ขั้นตอน ETA และงานย้อนหลังในหน้าจอเดียว';
  const filter = document.getElementById('job-filter');
  if (filter) filter.innerHTML = '<button class="active" data-filter="all">ทั้งหมด</button><button data-filter="uploading">อัปโหลด</button><button data-filter="active">กำลังทำ</button><button data-filter="done">เสร็จ</button><button data-filter="failed">ผิดพลาด</button>';

  const historyToolbar = document.querySelector('.history-toolbar');
  if (historyToolbar) historyToolbar.outerHTML = `
    <div class="library-tools">
      <div class="library-tools-main"><div class="search-box"><svg><use href="#i-search"/></svg><input id="history-search" placeholder="ค้นหาชื่องาน แท็ก หรือลูกค้า..."></div><button class="button secondary" id="create-folder-button">${icon('i-folder')}โฟลเดอร์ใหม่</button></div>
      <div class="library-filters">
        <select id="history-mode-filter" class="compact-select"><option value="all">ทุกประเภท</option><option value="autosu_only">ทำซับ / ทำพร้อมกัน</option><option value="silence_trim_only">ตัดเสียงเงียบ</option></select>
        <select id="library-status" class="compact-select"><option value="all">ทุกสถานะ</option><option value="done">พร้อมโหลด</option><option value="active">กำลังทำ</option><option value="failed">ผิดพลาด</option></select>
        <select id="library-date" class="compact-select"><option value="all">ทุกช่วงเวลา</option><option value="today">วันนี้</option><option value="7">7 วัน</option><option value="30">30 วัน</option></select>
        <select id="library-size" class="compact-select"><option value="all">ทุกขนาด</option><option value="small">ต่ำกว่า 100 MB</option><option value="medium">100 MB–1 GB</option><option value="large">มากกว่า 1 GB</option></select>
        <select id="library-folder" class="compact-select"><option value="all">ทุกโฟลเดอร์</option><option value="none">ยังไม่จัดโฟลเดอร์</option></select>
        <select id="library-sort" class="compact-select"><option value="newest">ล่าสุดก่อน</option><option value="oldest">เก่าสุดก่อน</option><option value="name">เรียงตามชื่อ</option><option value="size">ขนาดมากก่อน</option></select>
      </div>
      <div class="bulk-bar hidden" id="library-bulk"><strong><span id="bulk-count">0</span> งานที่เลือก</strong><button data-bulk-action="pin">${icon('i-pin')}ปักหมุด</button><button data-bulk-action="favorite">${icon('i-star')}รายการโปรด</button><button data-bulk-action="move">${icon('i-folder')}ย้าย</button><button data-bulk-action="delete" class="danger">${icon('i-trash')}ลบ</button><button data-bulk-clear>${icon('i-close')}</button></div>
    </div>`;

  document.getElementById('view-history')?.insertAdjacentHTML('afterbegin', `
    <div class="library-summary" id="library-summary"><button class="active" data-library-scope="all"><strong>0</strong><span>งานทั้งหมด</span></button><button data-library-scope="pinned"><strong>0</strong><span>ปักหมุด</span></button><button data-library-scope="favorite"><strong>0</strong><span>รายการโปรด</span></button><button data-library-scope="ready"><strong>0</strong><span>พร้อมโหลด</span></button></div>
  `);

  document.getElementById('view-history')?.insertAdjacentHTML('afterend', `
    <section class="view" id="view-downloads" data-view-panel="downloads">
      <header class="page-header"><div><p class="page-overline">DOWNLOAD CENTER</p><h1>ไฟล์พร้อมดาวน์โหลด</h1><p>รวม MP4, SRT, VTT, ASS และ ZIP ทุกเวอร์ชัน พร้อมวันหมดอายุ</p></div><button class="button secondary" id="refresh-downloads">${icon('i-refresh')}รีเฟรช</button></header>
      <div class="download-layout"><div class="storage-overview" id="storage-overview"></div><div class="download-list" id="download-list"></div></div>
    </section>
    <section class="view" id="view-notifications" data-view-panel="notifications">
      <header class="page-header"><div><p class="page-overline">NOTIFICATION CENTER</p><h1>การแจ้งเตือนทั้งหมด</h1><p>ติดตามงานเสร็จ งานผิดพลาด การอัปโหลด และความปลอดภัยทุกอุปกรณ์</p></div><div class="header-actions"><button class="button secondary" id="test-notification">${icon('i-bell')}ทดสอบ</button><button class="button primary" id="read-all-notifications">${icon('i-check')}อ่านทั้งหมด</button></div></header>
      <div class="notification-shell"><aside class="notification-filters"><button class="active" data-notification-filter="all">ทั้งหมด</button><button data-notification-filter="unread">ยังไม่อ่าน</button><button data-notification-filter="job">งานประมวลผล</button><button data-notification-filter="security">ความปลอดภัย</button></aside><div class="notification-list" id="notification-list"></div></div>
    </section>
  `);

  document.querySelector('.settings-page-grid')?.insertAdjacentHTML('beforeend', `
    <div class="content-card settings-card storage-card" id="storage-card"><div class="card-title"><span class="title-icon cyan"><svg><use href="#i-folder"/></svg></span><div><h2>พื้นที่จัดเก็บ</h2><p>แยก Source, Output, Upload และ Cache</p></div></div><div id="settings-storage"></div></div>
    <div class="content-card settings-card guest-safety-card" id="guest-safety-card"><div class="card-title"><span class="title-icon coral"><svg><use href="#i-shield"/></svg></span><div><h2>ป้องกันงาน Guest สูญหาย</h2><p>สร้าง Recovery Code เพื่อกู้งานเมื่อเปลี่ยน Chrome</p></div></div><div class="guest-safety-copy"><strong id="recovery-status">ยังไม่ได้สร้างรหัสกู้คืน</strong><small>รหัสใช้ครั้งเดียวและมีอายุ 30 วัน ควรเก็บไว้นอกเบราว์เซอร์</small></div><div class="card-actions"><button class="button primary" id="generate-recovery">สร้าง Recovery Code</button><button class="button secondary" id="open-recovery">กู้คืนด้วยรหัส</button></div></div>
    <div class="content-card settings-card notification-channel-card"><div class="card-title"><span class="title-icon violet"><svg><use href="#i-bell"/></svg></span><div><h2>ช่องทางแจ้งเตือน</h2><p>ตั้งค่า Browser, LINE และ Email ต่อบัญชี</p></div></div><div class="channel-grid"><label><input type="checkbox" id="channel-browser"><span>Browser Notification</span></label><label><input type="checkbox" id="channel-line"><span>LINE</span></label><label><input type="checkbox" id="channel-email"><span>Email</span></label></div><div class="form-grid two"><label class="field"><span>LINE User/Group ID</span><input id="line-target" placeholder="Uxxxxxxxx หรือ Cxxxxxxxx"></label><label class="field"><span>อีเมลรับแจ้ง</span><input id="notify-email" type="email" placeholder="name@example.com"></label></div><div class="card-actions"><button class="button primary" id="save-notification-channels">บันทึกช่องทาง</button></div></div>
    <div class="content-card settings-card devices-card"><div class="card-title"><span class="title-icon"><svg><use href="#i-device"/></svg></span><div><h2>อุปกรณ์ที่ซิงก์</h2><p>ดู เปลี่ยนชื่อ หรือนำ Chrome ที่ไม่ใช้แล้วออก</p></div><span class="member-count" id="device-count">0 อุปกรณ์</span></div><div id="device-list" class="device-list"></div></div>
  `);

  document.body.insertAdjacentHTML('beforeend', `
    <aside class="upload-dock collapsed" id="upload-dock"><button class="upload-dock-head" data-upload-dock-toggle><span>${icon('i-upload')}<strong>สถานะอัปโหลด</strong><b id="upload-dock-count">0</b></span><span id="upload-network" class="upload-network online">ออนไลน์</span>${icon('i-chevron')}</button><div class="upload-dock-list" id="upload-dock-list"><div class="upload-empty">ยังไม่มีไฟล์กำลังอัปโหลด</div></div></aside>
    <div class="preview-modal" id="preview-modal" aria-hidden="true"><div class="preview-backdrop" data-preview-close></div><section class="preview-panel"><header><div><small>BEFORE / AFTER PREVIEW</small><h2 id="preview-title">พรีวิวงาน</h2><p id="preview-subtitle">—</p></div><button class="icon-button" data-preview-close>${icon('i-close')}</button></header><div class="preview-tabs"><button class="active" data-preview-kind="output">หลังประมวลผล</button><button data-preview-kind="source">ต้นฉบับ</button><button class="hidden" id="trim-preview-tab" data-preview-kind="sample">ตัวอย่างตัด</button></div><div class="trim-preview-controls"><label>เริ่มที่ <input id="trim-preview-start" type="number" min="0" step="1" value="0"> วิ</label><label>ความยาว <select id="trim-preview-duration"><option value="10">10 วิ</option><option value="15" selected>15 วิ</option><option value="20">20 วิ</option></select></label><button class="button secondary small" id="generate-trim-preview">${icon('i-wave')}ทดลองตัดช่วงนี้</button><span id="trim-preview-status">ยังไม่ได้ทดลอง</span></div><div class="video-stage"><video id="preview-player" controls playsinline></video><div id="preview-caption-demo">ตัวอย่างซับที่เปิด–ปิดได้</div></div><div class="preview-meta"><strong id="preview-file-name">—</strong><label><input type="checkbox" id="preview-subtitles" checked> แสดงซับ</label></div><canvas id="preview-waveform"></canvas><footer><button class="button secondary" data-preview-close>ปิด</button><button class="button primary" data-preview-download>${icon('i-download')}ดาวน์โหลดทั้งหมด</button></footer></section></div>
    <div class="bold-modal subtitle-editor-modal" id="subtitle-editor-modal" aria-hidden="true"><div class="bold-modal-backdrop" data-subtitle-close></div><section class="bold-modal-panel subtitle-editor-panel"><header><div><small>SUBTITLE EDITOR LITE</small><h2 id="subtitle-editor-title">แก้ไขซับไตเติ้ล</h2><p id="subtitle-editor-source">SRT / VTT / ASS</p></div><button type="button" class="icon-button" data-subtitle-close>${icon('i-close')}</button></header><div class="subtitle-toolbar"><input id="subtitle-import-file" type="file" accept=".srt,.vtt,.ass,text/vtt,application/x-subrip" hidden><button class="button secondary small" id="subtitle-import-button">${icon('i-upload')}นำเข้า</button><button class="button secondary small" id="subtitle-add-cue">เพิ่มบรรทัด</button><label class="subtitle-shift"><span>เลื่อนทั้งหมด</span><input id="subtitle-shift-seconds" type="number" step="0.1" value="0"><button id="subtitle-shift-button">ใช้</button></label></div><div class="subtitle-table-head"><span>#</span><span>เริ่ม</span><span>จบ</span><span>ข้อความ</span><span></span></div><div class="subtitle-cue-list" id="subtitle-cue-list"></div><div class="subtitle-editor-empty hidden" id="subtitle-editor-empty"><strong>ยังไม่มีข้อความซับ</strong><p>นำเข้า SRT/VTT/ASS หรือเพิ่มบรรทัดแรกได้ทันที</p><button class="button primary" id="subtitle-empty-add">เพิ่มบรรทัดแรก</button></div><footer><span id="subtitle-editor-status">ยังไม่ได้แก้ไข</span><div class="subtitle-export-buttons"><button class="button secondary small" data-subtitle-export="srt">Export SRT</button><button class="button secondary small" data-subtitle-export="vtt">VTT</button><button class="button secondary small" data-subtitle-export="ass">ASS</button><button class="button primary" id="subtitle-save-all">${icon('i-check')}บันทึกทุกแบบ</button></div></footer></section></div>
    <div class="bold-modal" id="edit-job-modal" aria-hidden="true"><div class="bold-modal-backdrop" data-bold-modal-close></div><form class="bold-modal-panel" id="edit-job-form"><header><div><small>จัดระเบียบงาน</small><h2>แก้ไขข้อมูลงาน</h2></div><button type="button" class="icon-button" data-bold-modal-close>${icon('i-close')}</button></header><input type="hidden" name="job_id"><label class="field"><span>ชื่องาน</span><input name="display_name" maxlength="120" required></label><label class="field"><span>โฟลเดอร์</span><select name="folder_id" id="edit-job-folder"><option value="">ไม่จัดโฟลเดอร์</option></select></label><label class="field"><span>แท็ก คั่นด้วยจุลภาค</span><input name="tags" placeholder="TikTok, ลูกค้า A, EP01"></label><div class="toggle-inline"><label><input type="checkbox" name="pinned"> ปักหมุด</label><label><input type="checkbox" name="favorite"> รายการโปรด</label></div><footer><button type="button" class="button secondary" data-bold-modal-close>ยกเลิก</button><button class="button primary" type="submit">บันทึก</button></footer></form></div>
    <div class="bold-modal" id="recovery-modal" aria-hidden="true"><div class="bold-modal-backdrop" data-bold-modal-close></div><section class="bold-modal-panel"><header><div><small>GUEST RECOVERY</small><h2>กู้คืนงานบน Chrome ใหม่</h2></div><button class="icon-button" data-bold-modal-close>${icon('i-close')}</button></header><div id="recovery-generated" class="recovery-generated hidden"><span>Recovery Code</span><strong id="recovery-code">—</strong><small id="recovery-expiry">—</small><button class="button primary" id="copy-recovery">${icon('i-copy')}คัดลอกรหัส</button></div><form id="recover-browser-form"><label class="field"><span>กรอก Recovery Code</span><input name="code" required placeholder="SJ88-XXXX-XXXX-XXXX-XXXX-XXXX"></label><p>เมื่อกู้คืนสำเร็จ งาน คิว ประวัติ และไฟล์จะซิงก์มายัง Chrome นี้ทันที</p><footer><button type="button" class="button secondary" data-bold-modal-close>ยกเลิก</button><button class="button primary" type="submit">กู้คืนงาน</button></footer></form></section></div>
  `);
})();
