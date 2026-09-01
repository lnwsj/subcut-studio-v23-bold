'use strict';

window.SubtitleEditor = (() => {
  const state = { jobId: '', name: '', source: '', cues: [], dirty: false, loading: false };
  const node = (id) => document.getElementById(id);

  function timeText(seconds) {
    const ms = Math.max(0, Math.round(Number(seconds || 0) * 1000));
    const hours = Math.floor(ms / 3600000);
    const minutes = Math.floor((ms % 3600000) / 60000);
    const secs = Math.floor((ms % 60000) / 1000);
    return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(secs).padStart(2, '0')}.${String(ms % 1000).padStart(3, '0')}`;
  }

  function seconds(value) {
    const text = String(value || '').trim().replace(',', '.');
    if (!text.includes(':')) return Math.max(0, Number(text) || 0);
    const parts = text.split(':').map(Number);
    if (parts.some((value) => !Number.isFinite(value))) return 0;
    if (parts.length === 3) return Math.max(0, parts[0] * 3600 + parts[1] * 60 + parts[2]);
    return Math.max(0, parts[0] * 60 + parts[1]);
  }

  function markDirty(message = 'มีการแก้ไขที่ยังไม่ได้บันทึก') {
    state.dirty = true;
    node('subtitle-editor-status').textContent = message;
  }

  function render() {
    const list = node('subtitle-cue-list');
    const empty = node('subtitle-editor-empty');
    empty.classList.toggle('hidden', state.cues.length > 0);
    list.classList.toggle('hidden', state.cues.length === 0);
    list.innerHTML = state.cues.map((cue, index) => `<article class="subtitle-cue-row" data-cue-index="${index}"><b>${index + 1}</b><input aria-label="เวลาเริ่ม" data-cue-start value="${timeText(cue.start)}"><input aria-label="เวลาจบ" data-cue-end value="${timeText(cue.end)}"><textarea aria-label="ข้อความซับ" data-cue-text rows="2">${escapeHtml(cue.text)}</textarea><button type="button" data-cue-delete title="ลบบรรทัด">${icon('i-trash')}</button></article>`).join('');
    node('subtitle-editor-status').textContent = state.loading ? 'กำลังโหลด…' : state.dirty ? 'มีการแก้ไขที่ยังไม่ได้บันทึก' : `${state.cues.length} บรรทัด · บันทึกแล้ว`;
  }

  function addCue() {
    const last = state.cues.at(-1);
    const start = last ? Number(last.end) + 0.1 : 0;
    state.cues.push({ id: String(state.cues.length + 1), start, end: start + 2, text: 'ข้อความซับใหม่' });
    markDirty(); render();
    node('subtitle-cue-list')?.lastElementChild?.querySelector('textarea')?.focus();
  }

  function currentName() {
    const job = window.BoldApp?.snapshot?.jobs?.find((item) => String(item.id) === String(state.jobId));
    const download = window.BoldApp?.downloads?.find((item) => String(item.job_id) === String(state.jobId));
    return job?.name || download?.name || state.name || 'edited_subtitles';
  }

  async function open(jobId) {
    state.jobId = String(jobId || ''); state.name = currentName(); state.cues = []; state.dirty = false; state.loading = true;
    node('subtitle-editor-modal').classList.add('open'); node('subtitle-editor-modal').setAttribute('aria-hidden', 'false');
    node('subtitle-editor-title').textContent = `แก้ซับ — ${currentName()}`;
    render();
    try {
      const payload = await api(`/api/jobs/${encodeURIComponent(state.jobId)}/subtitles`);
      state.source = payload.file || '';
      state.cues = Array.isArray(payload.cues) ? payload.cues : [];
      node('subtitle-editor-source').textContent = payload.empty ? 'ยังไม่มีไฟล์ซับ · เริ่มใหม่หรือนำเข้าได้' : `${String(payload.format || 'srt').toUpperCase()} · ${payload.file}`;
    } catch (error) {
      toast('โหลดซับไม่สำเร็จ', 'error', error.message);
      node('subtitle-editor-source').textContent = 'โหลดไฟล์ซับไม่สำเร็จ';
    } finally { state.loading = false; render(); }
  }

  function close() {
    if (state.dirty && !confirm('ปิดโดยไม่บันทึกการแก้ไขหรือไม่?')) return;
    node('subtitle-editor-modal')?.classList.remove('open'); node('subtitle-editor-modal')?.setAttribute('aria-hidden', 'true');
    state.jobId = ''; state.cues = []; state.dirty = false;
  }

  function syncCue(target) {
    const row = target.closest('[data-cue-index]');
    const cue = state.cues[Number(row?.dataset.cueIndex)];
    if (!cue) return;
    if (target.matches('[data-cue-start]')) cue.start = seconds(target.value);
    if (target.matches('[data-cue-end]')) cue.end = Math.max(cue.start + 0.05, seconds(target.value));
    if (target.matches('[data-cue-text]')) cue.text = target.value;
    markDirty();
  }

  async function importFile(file) {
    if (!file) return;
    try {
      const content = await file.text();
      const payload = await api(`/api/jobs/${encodeURIComponent(state.jobId)}/subtitles/import`, { method: 'POST', json: { filename: file.name, content } });
      state.cues = payload.cues || []; state.source = file.name; markDirty(`นำเข้า ${state.cues.length} บรรทัดแล้ว`);
      node('subtitle-editor-source').textContent = `${String(payload.format).toUpperCase()} · ${file.name}`; render();
    } catch (error) { toast('นำเข้าซับไม่สำเร็จ', 'error', error.message); }
  }

  async function exportFormat(format) {
    if (!state.cues.length) return toast('ยังไม่มีซับสำหรับส่งออก', 'info');
    try {
      const payload = await api(`/api/jobs/${encodeURIComponent(state.jobId)}/subtitles/export`, { method: 'POST', json: { format, base_name: currentName(), cues: state.cues } });
      const blob = new Blob([payload.content], { type: `${payload.mime};charset=utf-8` });
      const url = URL.createObjectURL(blob); const anchor = document.createElement('a');
      anchor.href = url; anchor.download = payload.filename; anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1500);
      toast(`Export ${format.toUpperCase()} แล้ว`, 'success');
    } catch (error) { toast('ส่งออกซับไม่สำเร็จ', 'error', error.message); }
  }

  async function saveAll() {
    if (!state.cues.length) return toast('เพิ่มข้อความซับอย่างน้อย 1 บรรทัด', 'info');
    const button = node('subtitle-save-all'); button.disabled = true; button.textContent = 'กำลังบันทึก…';
    try {
      const payload = await api(`/api/jobs/${encodeURIComponent(state.jobId)}/subtitles`, { method: 'PUT', json: { base_name: `${currentName()}_edited`, formats: ['srt', 'vtt', 'ass'], cues: state.cues } });
      state.cues = payload.cues || state.cues; state.dirty = false; render();
      toast(`บันทึก ${payload.files?.length || 3} รูปแบบแล้ว`, 'success', 'ไฟล์ใหม่อยู่ใน Download Center');
      await window.BoldApp?.loadDownloads?.(); await window.BoldApp?.refresh?.();
    } catch (error) { toast('บันทึกซับไม่สำเร็จ', 'error', error.message); }
    finally { button.disabled = false; button.innerHTML = `${icon('i-check')}บันทึกทุกแบบ`; }
  }

  function shiftAll() {
    const delta = Number(node('subtitle-shift-seconds').value || 0);
    if (!Number.isFinite(delta) || delta === 0) return;
    state.cues = state.cues.map((cue) => ({ ...cue, start: Math.max(0, Number(cue.start) + delta), end: Math.max(0.05, Number(cue.end) + delta) }));
    markDirty(`เลื่อนเวลาทั้งหมด ${delta > 0 ? '+' : ''}${delta.toFixed(1)} วินาที`); render();
  }

  function bind() {
    document.addEventListener('click', (event) => {
      const opener = event.target.closest('[data-subtitle-job]');
      if (opener) { event.preventDefault(); event.stopPropagation(); open(opener.dataset.subtitleJob); return; }
      if (event.target.closest('[data-subtitle-close]')) { close(); return; }
      if (event.target.closest('#subtitle-import-button')) node('subtitle-import-file').click();
      if (event.target.closest('#subtitle-add-cue') || event.target.closest('#subtitle-empty-add')) addCue();
      if (event.target.closest('#subtitle-shift-button')) shiftAll();
      if (event.target.closest('#subtitle-save-all')) saveAll();
      const exporter = event.target.closest('[data-subtitle-export]'); if (exporter) exportFormat(exporter.dataset.subtitleExport);
      const remove = event.target.closest('[data-cue-delete]');
      if (remove) { state.cues.splice(Number(remove.closest('[data-cue-index]').dataset.cueIndex), 1); markDirty(); render(); }
    });
    node('subtitle-cue-list')?.addEventListener('input', (event) => syncCue(event.target));
    node('subtitle-import-file')?.addEventListener('change', (event) => { importFile(event.target.files?.[0]); event.target.value = ''; });
  }

  return { bind, open, close, get cues() { return state.cues; } };
})();

document.addEventListener('DOMContentLoaded', SubtitleEditor.bind);
