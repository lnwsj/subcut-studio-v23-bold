'use strict';

window.SubCutPreview = (() => {
  let current = null;
  let peaks = [];

  function modal() { return document.getElementById('preview-modal'); }
  function player() { return document.getElementById('preview-player'); }

  function selectedFile(job, requestedPath = '') {
    const videos = (job?.files || []).filter((file) => String(file.mime || '').startsWith('video/') || /\.(mp4|mov|mkv|webm|m4v)$/i.test(file.name));
    return videos.find((file) => file.path === requestedPath) || videos[0] || job?.files?.[0] || null;
  }

  function mediaUrl(kind, jobId, path = '') {
    if (kind === 'source') return withToken(`/api/jobs/${encodeURIComponent(jobId)}/media/source/0`);
    if (kind === 'sample' && current?.trimPreviewUrl) return withToken(current.trimPreviewUrl);
    const clean = String(path || '').replace(/^output\//, '').split('/').map(encodeURIComponent).join('/');
    return withToken(`/api/jobs/${encodeURIComponent(jobId)}/media/output/${clean}`);
  }

  async function ensureDownloadData(jobId) {
    let job = window.BoldApp?.downloads?.find((item) => String(item.job_id) === String(jobId));
    if (job) return job;
    const payload = await api('/api/download-center');
    window.BoldApp.downloads = payload.items || [];
    return window.BoldApp.downloads.find((item) => String(item.job_id) === String(jobId));
  }

  function drawWaveform() {
    const canvas = document.getElementById('preview-waveform');
    if (!canvas) return;
    const ratio = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    canvas.width = Math.max(1, rect.width * ratio);
    canvas.height = Math.max(1, rect.height * ratio);
    const ctx = canvas.getContext('2d');
    ctx.scale(ratio, ratio);
    const width = rect.width, height = rect.height, middle = height / 2;
    ctx.clearRect(0, 0, width, height);
    const gradient = ctx.createLinearGradient(0, 0, width, 0);
    gradient.addColorStop(0, '#7c5cff'); gradient.addColorStop(1, '#2ac7d6');
    ctx.strokeStyle = gradient; ctx.lineWidth = 2; ctx.beginPath();
    const values = peaks.length ? peaks : Array.from({ length: 160 }, (_, index) => 0.14 + Math.abs(Math.sin(index * 0.42)) * 0.5);
    values.forEach((value, index) => {
      const x = (index / Math.max(1, values.length - 1)) * width;
      const amplitude = Math.max(2, value * (height * 0.43));
      ctx.moveTo(x, middle - amplitude); ctx.lineTo(x, middle + amplitude);
    });
    ctx.stroke();
    const duration = player()?.duration || 0;
    const progress = duration ? (player().currentTime / duration) * width : 0;
    ctx.fillStyle = 'rgba(17,21,42,.12)'; ctx.fillRect(progress, 0, width - progress, height);
    ctx.strokeStyle = '#ff665d'; ctx.lineWidth = 2; ctx.beginPath(); ctx.moveTo(progress, 0); ctx.lineTo(progress, height); ctx.stroke();
  }

  async function loadWaveform(kind, filePath = '') {
    try {
      const params = new URLSearchParams({ source: kind, file_path: filePath || '' });
      const payload = await api(`/api/jobs/${encodeURIComponent(current.job_id)}/waveform?${params}`);
      peaks = payload.peaks || [];
    } catch (_) { peaks = []; }
    drawWaveform();
  }

  async function switchMedia(kind) {
    if (!current) return;
    const file = selectedFile(current, current.requestedPath);
    const target = player();
    target.pause();
    target.src = mediaUrl(kind, current.job_id, file?.path || '');
    target.dataset.kind = kind;
    document.querySelectorAll('[data-preview-kind]').forEach((button) => button.classList.toggle('active', button.dataset.previewKind === kind));
    document.getElementById('preview-file-name').textContent = kind === 'source' ? 'ต้นฉบับก่อนตัด' : kind === 'sample' ? (current.trimPreviewName || 'ตัวอย่างตัด 10–20 วินาที') : (file?.name || 'ไฟล์ผลลัพธ์');
    if (kind === 'sample') { peaks = []; drawWaveform(); } else await loadWaveform(kind, file?.path || '');
    target.load();
  }

  async function open(jobId, requestedPath = '') {
    try {
      const job = await ensureDownloadData(jobId);
      if (!job) throw new Error('ยังไม่มีไฟล์สำหรับพรีวิว');
      current = { ...job, requestedPath };
      document.getElementById('preview-title').textContent = job.name || 'พรีวิวงาน';
      document.getElementById('preview-subtitle').textContent = `${job.files?.length || 0} ไฟล์ · หมดอายุใน ${job.expires_in_days ?? 0} วัน`;
      modal().classList.add('open'); modal().setAttribute('aria-hidden', 'false');
      await switchMedia('output');
    } catch (error) { toast('เปิดพรีวิวไม่สำเร็จ', 'error', error.message); }
  }


  async function generateTrimPreview() {
    if (!current) return;
    const button = document.getElementById('generate-trim-preview');
    const status = document.getElementById('trim-preview-status');
    button.disabled = true; status.textContent = 'กำลังสร้างตัวอย่าง…';
    try {
      const settings = {
        trim_silence_threshold_db: Number(document.getElementById('trim-threshold')?.value || -40),
        trim_silence_min_silence_sec: Number(document.getElementById('trim-min-silence')?.value || .5),
        trim_silence_margin_sec: Number(document.getElementById('trim-margin')?.value || 0),
        trim_silence_min_keep_sec: Number(document.getElementById('default-min-keep')?.value || .08),
      };
      const payload = await api(`/api/jobs/${encodeURIComponent(current.job_id)}/trim-preview`, { method: 'POST', json: {
        start_sec: Number(document.getElementById('trim-preview-start')?.value || 0),
        duration_sec: Number(document.getElementById('trim-preview-duration')?.value || 15), settings,
      } });
      current.trimPreviewUrl = payload.url; current.trimPreviewName = `ตัวอย่างหลังตัด · ลด ${payload.removed_duration_sec.toFixed(2)} วิ`;
      const tab = document.getElementById('trim-preview-tab'); tab.classList.remove('hidden');
      status.textContent = payload.trim_applied ? `ตัดออก ${payload.removed_duration_sec.toFixed(2)} วิ` : 'ช่วงนี้ไม่พบความเงียบที่ตัดได้';
      await switchMedia('sample');
    } catch (error) { status.textContent = 'สร้างตัวอย่างไม่สำเร็จ'; toast('ทดลองตัดไม่สำเร็จ', 'error', error.message); }
    finally { button.disabled = false; }
  }

  function close() {
    player()?.pause();
    if (player()) player().removeAttribute('src');
    modal()?.classList.remove('open'); modal()?.setAttribute('aria-hidden', 'true');
    current = null; peaks = [];
  }

  function bind() {
    document.addEventListener('click', (event) => {
      const openButton = event.target.closest('[data-preview-job]');
      if (openButton) { event.preventDefault(); open(openButton.dataset.previewJob, openButton.dataset.previewPath || ''); return; }
      const tab = event.target.closest('[data-preview-kind]'); if (tab) switchMedia(tab.dataset.previewKind);
      if (event.target.closest('[data-preview-close]')) close();
      if (event.target.closest('#generate-trim-preview')) generateTrimPreview();
      if (event.target.closest('[data-preview-download]') && current) downloadJob(current.job_id);
    });
    const video = player();
    video?.addEventListener('timeupdate', drawWaveform);
    video?.addEventListener('loadedmetadata', drawWaveform);
    document.getElementById('preview-waveform')?.addEventListener('click', (event) => {
      const target = player(); if (!target?.duration) return;
      const rect = event.currentTarget.getBoundingClientRect();
      target.currentTime = ((event.clientX - rect.left) / rect.width) * target.duration;
    });
    document.getElementById('preview-subtitles')?.addEventListener('change', (event) => {
      document.getElementById('preview-caption-demo')?.classList.toggle('hidden', !event.target.checked);
    });
    window.addEventListener('resize', drawWaveform);
  }

  return { open, close, bind };
})();
