'use strict';

window.BoldApp = (() => {
  const model = {
    snapshot: { jobs: [], counts: {}, storage: {}, folders: [] }, downloads: [], notifications: [], devices: [],
    selected: new Set(), scope: 'all', notificationFilter: 'all', streamAbort: null, refreshTimer: null,
    filters: { status: 'all', date: 'all', size: 'all', folder: 'all', sort: 'newest' }, seenNotifications: new Set(),
  };

  const statusGroup = (job) => {
    const status = normalizeStatus(job.status);
    if (status === 'created') return 'uploading';
    if (status === 'queued') return 'queued';
    if (status === 'running') return 'processing';
    if (status === 'done') return 'done';
    return 'failed';
  };
  const eta = (seconds) => !seconds ? '—' : seconds < 60 ? `${seconds} วิ` : seconds < 3600 ? `${Math.ceil(seconds / 60)} นาที` : `${(seconds / 3600).toFixed(1)} ชม.`;
  const library = (job) => job.library || { tags: [], pinned: false, favorite: false, folder_id: '' };
  const modeTitle = (job) => MODE_META[inferWorkflow(job)]?.title || 'SubCut';
  const jobBytes = (job) => Number(job.output_bytes || job.input_bytes || job.upload?.total_bytes || 0);
  const folderName = (id) => model.snapshot.folders?.find((item) => item.id === id)?.name || '';

  function stepHtml(job, compact = false) {
    const steps = job.steps || [];
    return `<div class="step-timeline ${compact ? 'compact' : ''}">${steps.map((step) => `<span class="${step.state}" title="${escapeHtml(step.label)}"><i>${step.state === 'done' ? icon('i-check') : ''}</i><b>${escapeHtml(step.label)}</b></span>`).join('')}</div>`;
  }

  function updateSyncUi(ok = true) {
    const now = new Date().toLocaleTimeString('th-TH', { hour: '2-digit', minute: '2-digit' });
    ['sync-label', 'sync-strip-label'].forEach((id) => { const node = byId(id); if (node) node.textContent = ok ? 'ซิงก์แล้วทุกอุปกรณ์' : 'รอเชื่อมต่อ'; });
    ['sync-time', 'sync-strip-time'].forEach((id) => { const node = byId(id); if (node) node.textContent = ok ? `ล่าสุด ${now}` : 'ข้อมูลในเครื่องยังไม่หาย'; });
    byId('sync-strip')?.classList.toggle('offline', !ok);
  }

  function applySnapshot(payload) {
    model.snapshot = payload || model.snapshot;
    state.jobs = Array.isArray(payload.jobs) ? payload.jobs : [];
    model.snapshot.folders = payload.folders || [];
    renderJobs(); renderHistory(); renderStorage(); populateFolderSelects(); updateSyncUi(true);
    const count = Number(payload.unread_notifications || 0);
    ['notification-count', 'top-notification-count'].forEach((id) => { const node = byId(id); if (!node) return; node.textContent = String(count); node.classList.toggle('hidden', count === 0); });
  }

  async function refresh({ silent = true } = {}) {
    if (!state.user || DEMO_MODE) return;
    try { applySnapshot(await api('/api/hub')); }
    catch (error) { updateSyncUi(false); if (!silent) toast('ซิงก์ข้อมูลไม่สำเร็จ', 'error', error.message); }
  }

  function renderJobsBold() {
    const list = byId('jobs-list'); if (!list) return;
    let jobs = [...(model.snapshot.jobs || state.jobs || [])];
    if (state.jobFilter !== 'all') jobs = jobs.filter((job) => state.jobFilter === 'active' ? ['queued', 'processing'].includes(statusGroup(job)) : statusGroup(job) === state.jobFilter);
    const groups = [
      ['uploading', 'กำลังอัปโหลด', 'ไฟล์จะทำต่อหลังรีเฟรชเมื่อเบราว์เซอร์มีสิทธิ์เข้าถึง'],
      ['queued', 'รอคิว', 'แสดงลำดับและเวลารอโดยประมาณ'], ['processing', 'กำลังประมวลผล', 'อัปเดตขั้นตอนสดจากเซิร์ฟเวอร์'],
      ['done', 'พร้อมดาวน์โหลด', 'ดูตัวอย่างก่อนโหลดหรือเก็บไว้นานขึ้น'], ['failed', 'ต้องตรวจสอบ', 'ลองใหม่จากงานเดิมโดยไม่อัปโหลดซ้ำ'],
    ];
    if (!jobs.length) {
      const allJobs = (model.snapshot.jobs || state.jobs || []);
      if (allJobs.length === 0 && state.jobFilter === 'all') {
        list.innerHTML = `<div class="empty-state enhanced">
          <span class="empty-icon-bg">${icon('i-queue')}</span>
          <strong>ยังไม่มีงานในคิว</strong>
          <p>ส่งงานแรกของคุณแล้วระบบจะประมวลผลต่อในพื้นหลัง — ปิดเว็บได้ งานไม่หาย</p>
          <div class="empty-state-actions">
            <button class="button primary" data-empty-sample>${icon('i-play')}ทดลองใช้วิดีโอตัวอย่าง</button>
            <button class="button ghost" data-view-jump="workspace">${icon('i-upload')}อัปโหลดวิดีโอของคุณ</button>
          </div>
          <div class="empty-state-hint">💡 งานจะอัปเดตสดผ่าน SSE — ไม่ต้องรีเฟรช</div>
        </div>`;
        const sampleBtn = list.querySelector('[data-empty-sample]');
        if (sampleBtn && typeof loadSampleVideo === 'function') sampleBtn.addEventListener('click', () => loadSampleVideo());
        return;
      }
      list.innerHTML = `<div class="empty-state"><span class="empty-icon">${icon('i-queue')}</span><strong>ยังไม่มีงานในหมวดนี้</strong><p>เริ่มงานใหม่แล้วสถานะอัปโหลดและขั้นตอนทั้งหมดจะอยู่ที่นี่</p><button class="button primary" data-view-jump="workspace">สร้างงานใหม่</button></div>`;
    } else list.innerHTML = groups.map(([key, title, copy]) => {
    else list.innerHTML = groups.map(([key, title, copy]) => {
      const rows = jobs.filter((job) => statusGroup(job) === key); if (!rows.length) return '';
      return `<section class="job-group"><header><div><h3>${title}<b>${rows.length}</b></h3><p>${copy}</p></div></header>${rows.map(jobRow).join('')}</section>`;
    }).join('');
    const active = jobs.filter((job) => ['uploading', 'queued', 'processing'].includes(statusGroup(job))).length;
    byId('queue-count').textContent = String(active); byId('queue-count').classList.toggle('hidden', !active);
    byId('mobile-queue-dot').classList.toggle('hidden', !active); byId('stat-active').textContent = String(active);
    const today = new Date().toISOString().slice(0, 10);
    byId('stat-done').textContent = String(jobs.filter((job) => statusGroup(job) === 'done' && String(job.finished_at || job.updated_at).slice(0, 10) === today).length);
    const removed = jobs.reduce((sum, job) => sum + Number(job.result?.runtime_metrics?.removed_duration_sec || 0), 0);
    byId('stat-removed').textContent = removed ? formatDuration(removed) : '0 นาที';
    byId('jobs-updated').textContent = 'อัปเดตสดผ่าน SSE';
  }

  function jobRow(job) {
    const group = statusGroup(job), progress = group === 'uploading' ? Number(job.upload?.percent || 0) : jobProgress(job);
    const queue = job.queue_position ? `คิวที่ ${job.queue_position} · ` : '';
    const statusCopy = group === 'uploading' ? `${formatBytes(job.upload?.uploaded_bytes || 0)} / ${formatBytes(job.upload?.total_bytes || 0)}` : `${queue}${job.stage || jobStage(job)} · ETA ${eta(job.eta_seconds)}`;
    const lib = library(job);
    return `<article class="hub-job-row ${group}">
      <label class="select-box"><input type="checkbox" data-select-job="${escapeHtml(job.id)}" ${model.selected.has(job.id) ? 'checked' : ''}><i></i></label>
      <button class="quick-icon ${lib.pinned ? 'active' : ''}" data-pin-job="${escapeHtml(job.id)}" title="ปักหมุด">${icon('i-pin')}</button>
      <span class="job-mode-icon ${inferWorkflow(job) === 'silence' ? 'silence' : ''}">${icon(MODE_META[inferWorkflow(job)]?.icon || 'i-combine')}</span>
      <div class="hub-job-main" data-open-job="${escapeHtml(job.id)}"><div class="hub-job-title"><strong>${escapeHtml(job.name || job.id)}</strong><span class="status-chip ${normalizeStatus(job.status)}">${statusLabel(job.status)}</span></div><small>${escapeHtml(modeTitle(job))} · ${formatDate(job.created_at)}${folderName(lib.folder_id) ? ` · ${escapeHtml(folderName(lib.folder_id))}` : ''}</small>${stepHtml(job, true)}<div class="hub-progress"><span style="width:${progress}%"></span></div><p><span>${escapeHtml(statusCopy)}</span><b>${Math.round(progress)}%</b></p></div>
      <div class="hub-job-actions">${group === 'done' ? `<button data-preview-job="${escapeHtml(job.id)}">${icon('i-eye')}<span>พรีวิว</span></button><button data-download-job="${escapeHtml(job.id)}">${icon('i-download')}<span>โหลด</span></button>` : ''}${group === 'failed' ? `<button data-retry-job="${escapeHtml(job.id)}">${icon('i-refresh')}<span>ลองใหม่</span></button>` : ''}<button data-edit-job="${escapeHtml(job.id)}">${icon('i-edit')}<span>จัดการ</span></button></div>
    </article>`;
  }

  function renderHistoryBold() {
    const list = byId('history-list'); if (!list) return;
    const query = state.historyQuery.trim().toLowerCase(), now = Date.now();
    let items = [...(model.snapshot.jobs || [])].filter((job) => {
      const lib = library(job), group = statusGroup(job), text = `${job.name} ${(lib.tags || []).join(' ')}`.toLowerCase();
      if (query && !text.includes(query)) return false;
      if (state.historyMode !== 'all' && job.mode !== state.historyMode) return false;
      if (model.filters.status !== 'all' && (model.filters.status === 'active' ? !['uploading', 'queued', 'processing'].includes(group) : model.filters.status === 'failed' ? group !== 'failed' : group !== model.filters.status)) return false;
      if (model.scope === 'pinned' && !lib.pinned || model.scope === 'favorite' && !lib.favorite || model.scope === 'ready' && group !== 'done') return false;
      if (model.filters.folder !== 'all' && (model.filters.folder === 'none' ? Boolean(lib.folder_id) : lib.folder_id !== model.filters.folder)) return false;
      const age = now - new Date(job.created_at).getTime(); if (model.filters.date === 'today' && age > 86400000 || /^\d+$/.test(model.filters.date) && age > Number(model.filters.date) * 86400000) return false;
      const bytes = jobBytes(job); if (model.filters.size === 'small' && bytes >= 100e6 || model.filters.size === 'medium' && (bytes < 100e6 || bytes > 1e9) || model.filters.size === 'large' && bytes <= 1e9) return false;
      return true;
    });
    items.sort((a, b) => model.filters.sort === 'oldest' ? new Date(a.created_at) - new Date(b.created_at) : model.filters.sort === 'name' ? String(a.name).localeCompare(String(b.name), 'th') : model.filters.sort === 'size' ? jobBytes(b) - jobBytes(a) : new Date(b.created_at) - new Date(a.created_at));
    const allJobs = model.snapshot.jobs || [], counts = { all: allJobs.length, pinned: allJobs.filter((j) => library(j).pinned).length, favorite: allJobs.filter((j) => library(j).favorite).length, ready: allJobs.filter((j) => statusGroup(j) === 'done').length };
    document.querySelectorAll('[data-library-scope]').forEach((button) => { button.querySelector('strong').textContent = counts[button.dataset.libraryScope] || 0; button.classList.toggle('active', button.dataset.libraryScope === model.scope); });
    if (!items.length) {
      // Two empty-state cases: no jobs at all vs filter doesn't match
      if (allJobs.length === 0 && (state.historyQuery || '').trim() === '' && state.historyMode === 'all' && model.scope === 'all' && model.filters.status === 'all' && model.filters.folder === 'all' && model.filters.date === 'all' && model.filters.size === 'all' && model.filters.sort === 'newest') {
        // truly empty library — show welcoming CTA with sample video
        list.innerHTML = `<div class="empty-state enhanced">
          <span class="empty-icon-bg">${icon('i-history')}</span>
          <strong>ยังไม่มีไฟล์ในประวัติ</strong>
          <p>ลองสร้างงานแรกของคุณได้เลย — ใช้วิดีโอตัวอย่าง 15 วินาที หรือลากไฟล์ของคุณเองมาวาง</p>
          <div class="empty-state-actions">
            <button class="button primary" data-empty-sample>${icon('i-play')}ทดลองใช้วิดีโอตัวอย่าง</button>
            <button class="button ghost" data-view-jump="workspace">${icon('i-upload')}อัปโหลดวิดีโอของคุณ</button>
          </div>
          <div class="empty-state-hint">💡 เริ่มงานแรกใช้เวลาแค่ 1 นาที</div>
        </div>`;
        const sampleBtn = list.querySelector('[data-empty-sample]');
        if (sampleBtn && typeof loadSampleVideo === 'function') sampleBtn.addEventListener('click', () => loadSampleVideo());
        return;
      }
      list.innerHTML = `<div class="empty-state"><span class="empty-icon">${icon('i-filter')}</span><strong>ไม่พบงานตามตัวกรอง</strong><p>ลองเปลี่ยนช่วงเวลา สถานะ หรือโฟลเดอร์</p></div>`;
      return;
    }
    list.innerHTML = `<div class="library-list-head"><span></span><span>งาน</span><span>สถานะ</span><span>วันที่</span><span>ขนาด</span><span>คำสั่ง</span></div>${items.map(libraryRow).join('')}`;
  }

  function libraryRow(job) {
    const lib = library(job), group = statusGroup(job), tags = (lib.tags || []).slice(0, 3);
    return `<article class="library-row"><label class="select-box"><input type="checkbox" data-select-job="${escapeHtml(job.id)}" ${model.selected.has(job.id) ? 'checked' : ''}><i></i></label><div class="library-primary"><div><button class="quick-icon ${lib.pinned ? 'active' : ''}" data-pin-job="${escapeHtml(job.id)}">${icon('i-pin')}</button><button class="quick-icon star ${lib.favorite ? 'active' : ''}" data-favorite-job="${escapeHtml(job.id)}">${icon('i-star')}</button><strong data-open-job="${escapeHtml(job.id)}">${escapeHtml(job.name || job.id)}</strong></div><small>${escapeHtml(modeTitle(job))}${folderName(lib.folder_id) ? ` · ${escapeHtml(folderName(lib.folder_id))}` : ''}</small><span class="tag-line">${tags.map((tag) => `<i>${escapeHtml(tag)}</i>`).join('')}</span></div><span class="status-chip ${normalizeStatus(job.status)}">${group === 'uploading' ? 'อัปโหลด' : statusLabel(job.status)}</span><span class="library-cell">${formatDate(job.finished_at || job.created_at)}</span><span class="library-cell">${formatBytes(jobBytes(job))}</span><div class="library-actions">${group === 'done' ? `<button data-preview-job="${escapeHtml(job.id)}" title="พรีวิว">${icon('i-eye')}</button><button data-subtitle-job="${escapeHtml(job.id)}" title="แก้ซับ">${icon('i-caption')}</button><button data-download-job="${escapeHtml(job.id)}" title="ดาวน์โหลด">${icon('i-download')}</button>` : ''}${group === 'failed' ? `<button data-retry-job="${escapeHtml(job.id)}" title="ลองใหม่">${icon('i-refresh')}</button>` : ''}<button data-edit-job="${escapeHtml(job.id)}" title="แก้ไข">${icon('i-edit')}</button><button data-duplicate-job="${escapeHtml(job.id)}" title="ทำสำเนา">${icon('i-copy')}</button></div></article>`;
  }

  function updateBulk() {
    byId('bulk-count').textContent = String(model.selected.size); byId('library-bulk').classList.toggle('hidden', model.selected.size === 0);
  }

  function populateFolderSelects() {
    const options = (model.snapshot.folders || []).map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)} (${item.job_count || 0})</option>`).join('');
    const filter = byId('library-folder'); if (filter) { const value = filter.value; filter.innerHTML = `<option value="all">ทุกโฟลเดอร์</option><option value="none">ยังไม่จัดโฟลเดอร์</option>${options}`; filter.value = [...filter.options].some((o) => o.value === value) ? value : 'all'; }
    const edit = byId('edit-job-folder'); if (edit) edit.innerHTML = `<option value="">ไม่จัดโฟลเดอร์</option>${options}`;
  }

  function renderStorage() {
    const data = model.snapshot.storage || {}, buckets = data.buckets || {};
    const html = `<div class="storage-meter"><span><i style="width:${Math.min(100, Number(data.percent || 0))}%"></i></span><div><strong>${formatBytes(data.total_bytes || 0)} / ${formatBytes(data.quota_bytes || 0)}</strong><small>เหลือ ${formatBytes(data.remaining_bytes || 0)}</small></div></div><div class="storage-buckets">${Object.entries({ source: 'ต้นฉบับ', output: 'ผลลัพธ์', upload: 'กำลังอัปโหลด', cache: 'แคช', other: 'อื่นๆ' }).map(([key, label]) => `<span><i class="${key}"></i><b>${label}</b><small>${formatBytes(buckets[key] || 0)}</small></span>`).join('')}</div>`;
    if (byId('settings-storage')) byId('settings-storage').innerHTML = html;
  }

  async function loadDownloads() {
    if (DEMO_MODE) return;
    try { const payload = await api('/api/download-center'); model.downloads = payload.items || []; renderDownloads(payload.storage || model.snapshot.storage); }
    catch (error) { toast('โหลด Download Center ไม่สำเร็จ', 'error', error.message); }
  }

  function renderDownloads(storage = {}) {
    const overview = byId('storage-overview'), list = byId('download-list'); if (!overview || !list) return;
    overview.innerHTML = `<div><span class="storage-ring" style="--p:${Math.min(100, storage.percent || 0)}"><b>${Math.round(storage.percent || 0)}%</b></span><p><strong>ใช้พื้นที่ ${formatBytes(storage.total_bytes || 0)}</strong><small>เหลือ ${formatBytes(storage.remaining_bytes || 0)} จาก ${formatBytes(storage.quota_bytes || 0)}</small></p></div><button class="button secondary" data-view-jump="settings">จัดการพื้นที่</button>`;
    list.innerHTML = model.downloads.length ? model.downloads.map((job) => `<section class="download-group"><header><div><strong>${escapeHtml(job.name)}</strong><small>เสร็จ ${formatDate(job.finished_at)} · ${job.files.length} ไฟล์ · ${formatBytes(job.total_bytes)}</small></div><span class="expiry ${job.expires_in_days <= 2 ? 'urgent' : ''}">เก็บอีก ${job.expires_in_days} วัน</span><button data-extend-job="${escapeHtml(job.job_id)}">เก็บไว้อีก 30 วัน</button><button data-subtitle-job="${escapeHtml(job.job_id)}">${icon('i-caption')}แก้ซับ</button><button class="button primary small" data-download-job="${escapeHtml(job.job_id)}">${icon('i-download')}ZIP All</button></header><div class="download-files">${job.files.map((file) => `<article><span>${icon('i-file')}</span><div><strong>${escapeHtml(file.name)}</strong><small>${formatBytes(file.size)} · ${escapeHtml(file.mime)}</small></div>${file.previewable ? `<button data-preview-job="${escapeHtml(job.job_id)}" data-preview-path="${escapeHtml(file.path)}">${icon('i-eye')}พรีวิว</button>` : ''}<button data-download-file="${escapeHtml(file.path)}" data-job-id="${escapeHtml(job.job_id)}">${icon('i-download')}โหลด</button></article>`).join('')}</div></section>`).join('') : `<div class="empty-state"><span class="empty-icon">${icon('i-download')}</span><strong>ยังไม่มีไฟล์พร้อมโหลด</strong><p>เมื่องานเสร็จ ไฟล์ทุกเวอร์ชันจะรวมอยู่หน้านี้</p></div>`;
  }

  async function loadNotifications() { try { const payload = await api('/api/notifications'); model.notifications = payload.items || []; renderNotifications(); } catch (_) { /* retry via hub */ } }
  function renderNotifications() {
    const list = byId('notification-list'); if (!list) return;
    let items = [...model.notifications]; const filter = model.notificationFilter;
    if (filter === 'unread') items = items.filter((item) => !item.read_at); if (filter === 'job') items = items.filter((item) => item.kind.startsWith('job_')); if (filter === 'security') items = items.filter((item) => item.kind === 'security');
    list.innerHTML = items.length ? items.map((item) => `<article class="notification-item ${item.read_at ? 'read' : 'unread'} ${item.severity}"><span class="notification-icon">${icon(item.kind === 'security' ? 'i-shield' : item.severity === 'error' ? 'i-close' : 'i-bell')}</span><div><strong>${escapeHtml(item.title)}</strong><p>${escapeHtml(item.body)}</p><small>${formatDate(item.created_at)}</small></div>${item.job_id ? `<button data-open-job="${escapeHtml(item.job_id)}">เปิดงาน</button>` : ''}${!item.read_at ? `<button class="mark-read" data-read-notification="${escapeHtml(item.id)}">${icon('i-check')}</button>` : ''}</article>`).join('') : `<div class="empty-state"><span class="empty-icon">${icon('i-bell')}</span><strong>ไม่มีการแจ้งเตือนในหมวดนี้</strong></div>`;
    const newest = model.notifications.find((item) => !model.seenNotifications.has(item.id));
    model.notifications.forEach((item) => model.seenNotifications.add(item.id));
    if (newest && state.preferences.browserNotifications && Notification.permission === 'granted') new Notification(newest.title, { body: newest.body, icon: '/static/favicon.svg' });
  }

  async function loadAccountExtras() {
    try {
      const [devices, prefs] = await Promise.all([api('/api/devices'), api('/api/notification-preferences')]);
      model.devices = devices.items || []; renderDevices();
      const p = prefs.preferences || {}; byId('channel-browser').checked = Boolean(p.browser_enabled); byId('channel-line').checked = Boolean(p.line_enabled); byId('channel-email').checked = Boolean(p.email_enabled); byId('line-target').value = p.line_target || ''; byId('notify-email').value = p.email_address || state.user?.email || '';
    } catch (_) { /* optional settings */ }
  }
  function renderDevices() {
    byId('device-count').textContent = `${model.devices.length} อุปกรณ์`;
    byId('device-list').innerHTML = model.devices.map((device, index) => `<article><span>${icon('i-device')}</span><div><strong>${escapeHtml(device.label || `Chrome ${index + 1}`)}</strong><small>ใช้งานล่าสุด ${formatDate(device.last_seen_at)}</small></div><button data-rename-device="${device.id}">${icon('i-edit')}</button>${model.devices.length > 1 ? `<button data-remove-device="${device.id}" class="danger">${icon('i-trash')}</button>` : ''}</article>`).join('');
  }

  async function openJobDetailBold(jobId) {
    const local = model.snapshot.jobs.find((item) => String(item.id) === String(jobId)); if (!local) return;
    drawerOpen(local.name, `<div class="detail-status-card"><div class="detail-status-top"><span class="status-chip ${normalizeStatus(local.status)}">${statusLabel(local.status)}</span><strong>${jobProgress(local)}%</strong></div>${stepHtml(local)}<div class="detail-metrics">${detailMetric('ลำดับคิว', local.queue_position || '—')}${detailMetric('ETA', eta(local.eta_seconds))}${detailMetric('พื้นที่', formatBytes(jobBytes(local)))}</div></div><section class="detail-section"><h3>จัดการงาน</h3><div class="drawer-command-grid">${statusGroup(local) === 'done' ? `<button data-preview-job="${escapeHtml(local.id)}">${icon('i-eye')}ดู Before / After</button><button data-subtitle-job="${escapeHtml(local.id)}">${icon('i-caption')}แก้ซับไตเติ้ล</button><button data-download-job="${escapeHtml(local.id)}">${icon('i-download')}ดาวน์โหลด ZIP</button>` : ''}${statusGroup(local) === 'failed' ? `<button data-retry-job="${escapeHtml(local.id)}">${icon('i-refresh')}ลองใหม่จากขั้นที่พัง</button>` : ''}<button data-edit-job="${escapeHtml(local.id)}">${icon('i-edit')}ชื่อ โฟลเดอร์ แท็ก</button><button data-duplicate-job="${escapeHtml(local.id)}">${icon('i-copy')}ทำสำเนา</button>${['queued', 'processing'].includes(statusGroup(local)) ? `<button data-cancel-job="${escapeHtml(local.id)}" class="danger">${icon('i-close')}ยกเลิก</button>` : ''}${['done', 'failed'].includes(statusGroup(local)) ? `<button data-delete-job="${escapeHtml(local.id)}" class="danger">${icon('i-trash')}ลบงาน</button>` : ''}</div></section>`);
  }

  async function action(path, options, success) { try { await api(path, options); toast(success, 'success'); drawerClose(); await refresh(); } catch (error) { toast('ทำรายการไม่สำเร็จ', 'error', error.message); } }
  async function updateMeta(jobId, values) { return action(`/api/jobs/${encodeURIComponent(jobId)}/meta`, { method: 'PATCH', json: values }, 'บันทึกข้อมูลงานแล้ว'); }

  function openEdit(jobId) {
    const job = model.snapshot.jobs.find((item) => String(item.id) === String(jobId)); if (!job) return;
    const form = byId('edit-job-form'), lib = library(job); form.elements.job_id.value = job.id; form.elements.display_name.value = job.name || ''; form.elements.folder_id.value = lib.folder_id || ''; form.elements.tags.value = (lib.tags || []).join(', '); form.elements.pinned.checked = Boolean(lib.pinned); form.elements.favorite.checked = Boolean(lib.favorite); byId('edit-job-modal').classList.add('open');
  }
  const closeModals = () => document.querySelectorAll('.bold-modal.open').forEach((node) => node.classList.remove('open'));

  async function bulkAction(name) {
    const ids = [...model.selected]; if (!ids.length) return;
    let payload = { job_ids: ids, action: name };
    if (name === 'move') { const choice = prompt(`พิมพ์ชื่อโฟลเดอร์:\n${model.snapshot.folders.map((f) => f.name).join(', ')}`) || ''; const folder = model.snapshot.folders.find((f) => f.name === choice); if (!folder) return; payload.folder_id = folder.id; }
    if (name === 'delete' && !confirm(`ลบ ${ids.length} งานและไฟล์ทั้งหมดหรือไม่?`)) return;
    await action('/api/jobs/bulk', { method: 'POST', json: payload }, `จัดการ ${ids.length} งานแล้ว`); model.selected.clear(); updateBulk();
  }

  async function startRealtime() {
    clearInterval(state.pollingTimer); clearTimeout(model.refreshTimer); model.streamAbort?.abort();
    if (!state.user || DEMO_MODE) return;
    const controller = new AbortController(); model.streamAbort = controller;
    try {
      const response = await fetch('/api/hub/stream', { headers: authHeaders(), signal: controller.signal, credentials: 'same-origin' });
      if (!response.ok || !response.body) throw new Error(`stream_http_${response.status}`);
      const reader = response.body.getReader(), decoder = new TextDecoder(); let buffer = '';
      while (true) { const { value, done } = await reader.read(); if (done) break; buffer += decoder.decode(value, { stream: true }); const blocks = buffer.split('\n\n'); buffer = blocks.pop(); for (const block of blocks) { const event = block.match(/^event:\s*(.+)$/m)?.[1]; const data = block.match(/^data:\s*(.+)$/m)?.[1]; if (event === 'snapshot' && data) applySnapshot(JSON.parse(data)); } }
    } catch (error) { if (error.name !== 'AbortError') { updateSyncUi(false); model.refreshTimer = setTimeout(startRealtime, 3500); } }
  }

  async function submitBold() {
    if (!state.files.length || state.submitting) return; if (!state.user) { showAuth(); return; }
    state.submitting = true; updateSummary(); const button = byId('create-job-button'), files = [...state.files], name = byId('job-name').value.trim() || makeJobName();
    button.innerHTML = `${icon('i-upload')}<span>กำลังสร้างงาน…</span>`;
    try {
      if (DEMO_MODE) createDemoJob(name, buildJobSettings());
      else { const created = await api('/api/jobs', { method: 'POST', json: { name, mode: MODE_META[state.mode].serverMode, settings: buildJobSettings() } }); await UploadDock.start({ jobId: created.job.id, name, files }); }
      state.files = []; renderFiles(); byId('file-input').value = ''; byId('job-name').value = makeJobName(); byId('upload-progress').classList.add('hidden'); byId('upload-dock').classList.remove('collapsed'); toast('เริ่มอัปโหลดแล้ว', 'success', 'เปลี่ยนหน้าได้ สถานะจะติดอยู่มุมจอ'); setView('queue'); await refresh();
    } catch (error) { state.files = files; renderFiles(); toast('สร้างงานไม่สำเร็จ', 'error', error.message); }
    finally { state.submitting = false; button.innerHTML = `${icon('i-play')}<span>ส่งงานเข้าคิว</span>`; updateSummary(); }
  }

  function bindBoldEvents() {
    UploadDock.bind(); UploadDock.restore(); SubCutPreview.bind();
    ['library-status', 'library-date', 'library-size', 'library-folder', 'library-sort'].forEach((id) => byId(id)?.addEventListener('change', (event) => { model.filters[id.replace('library-', '')] = event.target.value; renderHistory(); }));
    document.addEventListener('click', async (event) => {
      const scope = event.target.closest('[data-library-scope]'); if (scope) { model.scope = scope.dataset.libraryScope; renderHistory(); }
      const select = event.target.closest('[data-select-job]'); if (select) { select.checked ? model.selected.add(select.dataset.selectJob) : model.selected.delete(select.dataset.selectJob); updateBulk(); }
      const pin = event.target.closest('[data-pin-job]'); if (pin) { event.stopPropagation(); const job = model.snapshot.jobs.find((j) => j.id === pin.dataset.pinJob); await updateMeta(job.id, { pinned: !library(job).pinned }); }
      const fav = event.target.closest('[data-favorite-job]'); if (fav) { const job = model.snapshot.jobs.find((j) => j.id === fav.dataset.favoriteJob); await updateMeta(job.id, { favorite: !library(job).favorite }); }
      const edit = event.target.closest('[data-edit-job]'); if (edit) { event.stopPropagation(); openEdit(edit.dataset.editJob); }
      const retryBtn = event.target.closest('[data-retry-job]'); if (retryBtn) action(`/api/jobs/${encodeURIComponent(retryBtn.dataset.retryJob)}/retry`, { method: 'POST', json: { stage: 'failed_stage' } }, 'ส่งกลับเข้าคิวแล้ว ใช้ไฟล์เดิม');
      const copy = event.target.closest('[data-duplicate-job]'); if (copy) action(`/api/jobs/${encodeURIComponent(copy.dataset.duplicateJob)}/duplicate`, { method: 'POST', json: {} }, 'สร้างสำเนาพร้อมไฟล์ต้นฉบับแล้ว');
      const del = event.target.closest('[data-delete-job]'); if (del && confirm('ลบงานและไฟล์ทั้งหมดถาวรหรือไม่?')) action(`/api/jobs/${encodeURIComponent(del.dataset.deleteJob)}`, { method: 'DELETE' }, 'ลบงานแล้ว');
      const bulk = event.target.closest('[data-bulk-action]'); if (bulk) bulkAction(bulk.dataset.bulkAction); if (event.target.closest('[data-bulk-clear]')) { model.selected.clear(); renderHistory(); updateBulk(); }
      const extend = event.target.closest('[data-extend-job]'); if (extend) { await action(`/api/jobs/${encodeURIComponent(extend.dataset.extendJob)}/retention/extend`, { method: 'POST', json: { days: 30 } }, 'ขยายเวลาเก็บไฟล์แล้ว'); loadDownloads(); }
      const read = event.target.closest('[data-read-notification]'); if (read) { await api(`/api/notifications/${read.dataset.readNotification}/read`, { method: 'POST' }); await loadNotifications(); await refresh(); }
      const nf = event.target.closest('[data-notification-filter]'); if (nf) { model.notificationFilter = nf.dataset.notificationFilter; document.querySelectorAll('[data-notification-filter]').forEach((b) => b.classList.toggle('active', b === nf)); renderNotifications(); }
      if (event.target.closest('#read-all-notifications')) { await api('/api/notifications/read-all', { method: 'POST' }); await loadNotifications(); await refresh(); }
      if (event.target.closest('#test-notification')) { await api('/api/notifications/test', { method: 'POST' }); await loadNotifications(); await refresh(); }
      if (event.target.closest('#refresh-downloads')) loadDownloads();
      if (event.target.closest('#create-folder-button')) { const name = prompt('ชื่อโฟลเดอร์ใหม่'); if (name) { await api('/api/folders', { method: 'POST', json: { name } }); await refresh(); } }
      if (event.target.closest('[data-bold-modal-close]')) closeModals();
      if (event.target.closest('#generate-recovery')) { const result = await api('/api/account/recovery-code', { method: 'POST' }); byId('recovery-code').textContent = result.code; byId('recovery-expiry').textContent = `หมดอายุ ${formatDate(result.expires_at)}`; byId('recovery-generated').classList.remove('hidden'); byId('recover-browser-form').classList.add('hidden'); byId('recovery-modal').classList.add('open'); }
      if (event.target.closest('#open-recovery')) { byId('recovery-generated').classList.add('hidden'); byId('recover-browser-form').classList.remove('hidden'); byId('recovery-modal').classList.add('open'); }
      if (event.target.closest('#copy-recovery')) { await navigator.clipboard.writeText(byId('recovery-code').textContent); toast('คัดลอก Recovery Code แล้ว'); }
      const rename = event.target.closest('[data-rename-device]'); if (rename) { const device = model.devices.find((d) => String(d.id) === rename.dataset.renameDevice); const label = prompt('ชื่ออุปกรณ์', device?.label || 'Chrome'); if (label) { await api(`/api/devices/${rename.dataset.renameDevice}`, { method: 'PATCH', json: { label } }); await loadAccountExtras(); } }
      const removeDevice = event.target.closest('[data-remove-device]'); if (removeDevice && confirm('นำอุปกรณ์นี้ออกจากการซิงก์หรือไม่?')) { await api(`/api/devices/${removeDevice.dataset.removeDevice}`, { method: 'DELETE' }); await loadAccountExtras(); }
    });
    byId('edit-job-form')?.addEventListener('submit', async (event) => { event.preventDefault(); const form = new FormData(event.currentTarget); await updateMeta(form.get('job_id'), { display_name: form.get('display_name'), folder_id: form.get('folder_id'), tags: String(form.get('tags') || '').split(',').map((x) => x.trim()).filter(Boolean), pinned: event.currentTarget.elements.pinned.checked, favorite: event.currentTarget.elements.favorite.checked }); closeModals(); });
    byId('recover-browser-form')?.addEventListener('submit', async (event) => { event.preventDefault(); try { const result = await api('/api/auth/recover-browser', { method: 'POST', json: { code: new FormData(event.currentTarget).get('code'), browser_key: getBrowserKey(), browser_label: browserLabel() } }); applyAuth(result); closeModals(); await afterAuthenticated(); toast('กู้คืนและซิงก์งานสำเร็จ'); } catch (error) { toast('กู้คืนไม่สำเร็จ', 'error', error.message); } });
    byId('save-notification-channels')?.addEventListener('click', async () => { await api('/api/notification-preferences', { method: 'PATCH', json: { browser_enabled: byId('channel-browser').checked, line_enabled: byId('channel-line').checked, email_enabled: byId('channel-email').checked, sound_enabled: state.preferences.soundNotifications, line_target: byId('line-target').value, email_address: byId('notify-email').value } }); toast('บันทึกช่องทางแจ้งเตือนแล้ว'); });
  }

  return { ...model, refresh, startRealtime, renderJobs: renderJobsBold, renderHistory: renderHistoryBold, loadDownloads, loadNotifications, loadAccountExtras, openJobDetail: openJobDetailBold, submitJob: submitBold, bind: bindBoldEvents, get snapshot() { return model.snapshot; }, get downloads() { return model.downloads; }, set downloads(value) { model.downloads = value; } };
})();

const baseSetView = setView;
setView = function boldSetView(view) {
  const boldViews = ['downloads', 'notifications'];
  if (boldViews.includes(view)) {
    state.view = view;
    all('[data-view-panel]').forEach((panel) => panel.classList.toggle('active', panel.dataset.viewPanel === view));
    all('.nav-item').forEach((button) => button.classList.toggle('active', button.dataset.view === view));
  } else {
    baseSetView(view);
  }
  byId('sidebar')?.classList.remove('open');
  byId('sidebar-backdrop')?.classList.add('hidden');
  if (view === 'downloads') BoldApp.loadDownloads();
  if (view === 'notifications') BoldApp.loadNotifications();
  if (view === 'settings') BoldApp.loadAccountExtras();
};
renderJobs = BoldApp.renderJobs;
renderHistory = BoldApp.renderHistory;
loadJobs = async (options = {}) => BoldApp.refresh(options);
loadHistory = async (options = {}) => BoldApp.refresh(options);
startPolling = BoldApp.startRealtime;
openJobDetail = BoldApp.openJobDetail;
openHistoryDetail = BoldApp.openJobDetail;
submitJob = BoldApp.submitJob;
const baseAfterAuthenticated = afterAuthenticated;
afterAuthenticated = async function boldAfterAuthenticated() { await baseAfterAuthenticated(); await Promise.all([BoldApp.refresh(), BoldApp.loadNotifications(), BoldApp.loadAccountExtras()]); BoldApp.startRealtime(); };
document.addEventListener('DOMContentLoaded', BoldApp.bind);
