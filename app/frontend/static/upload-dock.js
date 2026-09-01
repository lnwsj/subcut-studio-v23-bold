'use strict';

window.UploadDock = (() => {
  const DB_NAME = 'sj88-subcut-uploads-v2';
  const STORE_NAME = 'tasks';
  const runtime = new Map();
  let tasks = [];
  let dbPromise = null;

  function openDb() {
    if (dbPromise) return dbPromise;
    dbPromise = new Promise((resolve, reject) => {
      const request = indexedDB.open(DB_NAME, 1);
      request.onupgradeneeded = () => {
        const db = request.result;
        if (!db.objectStoreNames.contains(STORE_NAME)) db.createObjectStore(STORE_NAME, { keyPath: 'id' });
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
    return dbPromise;
  }

  async function transaction(mode, handler) {
    const db = await openDb();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE_NAME, mode);
      const store = tx.objectStore(STORE_NAME);
      let result;
      try { result = handler(store); } catch (error) { reject(error); return; }
      tx.oncomplete = () => resolve(result?.result ?? result);
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error || new Error('indexeddb_aborted'));
    });
  }

  const save = async (task) => transaction('readwrite', (store) => store.put({ ...task, controller: undefined }));
  const remove = async (id) => transaction('readwrite', (store) => store.delete(id));
  const loadAll = async () => transaction('readonly', (store) => store.getAll());
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  function taskProgress(task) {
    return Math.max(0, Math.min(100, task.totalBytes ? (task.uploadedBytes / task.totalBytes) * 100 : 0));
  }

  function etaText(seconds) {
    if (!Number.isFinite(seconds) || seconds <= 0) return 'กำลังคำนวณเวลา';
    if (seconds < 60) return `เหลือประมาณ ${Math.ceil(seconds)} วินาที`;
    if (seconds < 3600) return `เหลือประมาณ ${Math.ceil(seconds / 60)} นาที`;
    return `เหลือประมาณ ${(seconds / 3600).toFixed(1)} ชม.`;
  }

  function statusText(task) {
    return ({ hashing: 'กำลังตรวจ SHA-256', uploading: 'กำลังอัปโหลด', paused: 'หยุดชั่วคราว', waiting: 'รออินเทอร์เน็ต', finalizing: 'กำลังรวมไฟล์', queued: 'ส่งเข้าคิวแล้ว', error: 'อัปโหลดมีปัญหา' })[task.status] || task.status;
  }

  function render() {
    const dock = document.getElementById('upload-dock');
    const list = document.getElementById('upload-dock-list');
    if (!dock || !list) return;
    dock.classList.toggle('has-tasks', tasks.length > 0);
    document.getElementById('upload-dock-count').textContent = String(tasks.length);
    document.getElementById('upload-network').className = `upload-network ${navigator.onLine ? 'online' : 'offline'}`;
    document.getElementById('upload-network').textContent = navigator.onLine ? 'ออนไลน์' : 'ออฟไลน์';
    if (!tasks.length) {
      list.innerHTML = '<div class="upload-empty">ยังไม่มีไฟล์กำลังอัปโหลด</div>';
      return;
    }
    list.innerHTML = tasks.map((task) => {
      const percent = taskProgress(task);
      const speed = task.speed > 0 ? `${formatBytes(task.speed)}/s` : '—';
      const detail = task.status === 'error' ? task.error : `${formatBytes(task.uploadedBytes)} / ${formatBytes(task.totalBytes)} · ${speed}`;
      const action = ['uploading', 'hashing', 'finalizing'].includes(task.status)
        ? `<button data-upload-pause="${escapeHtml(task.id)}" aria-label="หยุดชั่วคราว">${icon('i-pause')}</button>`
        : ['paused', 'waiting', 'error'].includes(task.status)
          ? `<button data-upload-resume="${escapeHtml(task.id)}" aria-label="ทำต่อ">${icon('i-play')}</button>` : '';
      return `<article class="upload-task ${task.status}">
        <div class="upload-task-head"><span class="upload-file-icon">${icon('i-upload')}</span><div><strong>${escapeHtml(task.name)}</strong><small>${statusText(task)} · ${etaText(task.eta)}</small></div><span class="upload-task-actions">${action}<button data-upload-remove="${escapeHtml(task.id)}" aria-label="นำออก">${icon('i-close')}</button></span></div>
        <div class="upload-task-progress"><span style="width:${percent.toFixed(1)}%"></span></div>
        <div class="upload-task-foot"><small>${escapeHtml(detail || '')}</small><b>${Math.round(percent)}%</b></div>
        ${!task.persisted ? '<p class="upload-warning">ไฟล์นี้ใหญ่เกินพื้นที่เบราว์เซอร์ หากรีเฟรชอาจต้องเลือกไฟล์เดิมอีกครั้ง</p>' : ''}
      </article>`;
    }).join('');
  }

  async function persistTask(task) {
    try {
      await save(task);
      task.persisted = true;
    } catch (error) {
      task.persisted = false;
      console.warn('[upload] unable to persist task', error);
    }
    render();
  }

  function findTask(id) { return tasks.find((task) => String(task.id) === String(id)); }

  async function hashFile(task, fileIndex) {
    const record = task.fileRecords[fileIndex];
    if (record.hashes?.length === record.totalChunks && record.manifestHash) return;
    task.status = 'hashing';
    record.hashes = [];
    for (let chunkIndex = 0; chunkIndex < record.totalChunks; chunkIndex += 1) {
      if (task.status === 'paused') return;
      const start = chunkIndex * CHUNK_SIZE;
      const blob = task.files[fileIndex].slice(start, Math.min(start + CHUNK_SIZE, record.size));
      record.hashes.push(await sha256Blob(blob));
      task.stageDetail = `ตรวจไฟล์ ${fileIndex + 1}/${task.files.length} ส่วน ${chunkIndex + 1}/${record.totalChunks}`;
      render();
    }
    record.manifestHash = await sha256Text(record.hashes.join(''));
    await persistTask(task);
  }

  async function serverState(task) {
    try { return await api(`/api/jobs/${encodeURIComponent(task.jobId)}/upload/status`); }
    catch (error) { return error.status === 404 ? { state: 'none', files: [] } : Promise.reject(error); }
  }

  function applyServerProgress(task, remote) {
    const byIndex = new Map((remote.files || []).map((item) => [Number(item.file_index), item]));
    task.uploadedBytes = task.fileRecords.reduce((sum, record, index) => {
      const item = byIndex.get(index);
      return sum + Math.min(record.size, Number(item?.uploaded_bytes || 0));
    }, 0);
    return byIndex;
  }

  async function uploadChunk(task, fileIndex, chunkIndex, controller) {
    const file = task.files[fileIndex];
    const record = task.fileRecords[fileIndex];
    const start = chunkIndex * CHUNK_SIZE;
    const blob = file.slice(start, Math.min(start + CHUNK_SIZE, record.size));
    const params = new URLSearchParams({
      chunk_index: String(chunkIndex), total_chunks: String(record.totalChunks), total_files: String(task.files.length),
      file_name: record.name, file_size: String(record.size), file_index: String(fileIndex),
      chunk_sha256: record.hashes[chunkIndex], chunk_manifest_sha256: record.manifestHash,
      append: fileIndex > 0 ? '1' : '0',
    });
    const form = new FormData();
    form.append('files', blob, `${record.name}.part${chunkIndex}`);
    const started = performance.now();
    await api(`/api/jobs/${encodeURIComponent(task.jobId)}/upload/chunk?${params}`, { method: 'POST', body: form, signal: controller.signal });
    const elapsed = Math.max(0.1, (performance.now() - started) / 1000);
    const instant = blob.size / elapsed;
    task.speed = task.speed ? (task.speed * 0.65) + (instant * 0.35) : instant;
    task.uploadedBytes = Math.min(task.totalBytes, task.uploadedBytes + blob.size);
    task.eta = task.speed > 0 ? (task.totalBytes - task.uploadedBytes) / task.speed : 0;
  }

  async function run(task) {
    if (runtime.has(task.id) || ['queued'].includes(task.status)) return;
    if (!navigator.onLine) { task.status = 'waiting'; await persistTask(task); return; }
    const marker = {};
    runtime.set(task.id, marker);
    try {
      let remote = await serverState(task);
      let remoteFiles = applyServerProgress(task, remote);
      for (let fileIndex = 0; fileIndex < task.files.length; fileIndex += 1) {
        if (task.status === 'paused') return;
        await hashFile(task, fileIndex);
        if (task.status === 'paused') return;
        const record = task.fileRecords[fileIndex];
        const received = new Set((remoteFiles.get(fileIndex)?.received_indices || []).map(Number));
        if (remoteFiles.get(fileIndex)?.assembled) continue;
        for (let chunkIndex = 0; chunkIndex < record.totalChunks; chunkIndex += 1) {
          if (received.has(chunkIndex)) continue;
          if (task.status === 'paused') return;
          task.status = 'uploading';
          task.stageDetail = `ไฟล์ ${fileIndex + 1}/${task.files.length} · ส่วน ${chunkIndex + 1}/${record.totalChunks}`;
          const controller = new AbortController();
          marker.controller = controller;
          await uploadChunk(task, fileIndex, chunkIndex, controller);
          await persistTask(task);
        }
        remote = await serverState(task);
        remoteFiles = applyServerProgress(task, remote);
      }
      task.status = 'finalizing'; task.eta = 0; await persistTask(task);
      await api(`/api/jobs/${encodeURIComponent(task.jobId)}/upload/chunked/complete`, { method: 'POST' });
      await api(`/api/jobs/${encodeURIComponent(task.jobId)}/process`, { method: 'POST' });
      task.status = 'queued'; task.uploadedBytes = task.totalBytes; task.speed = 0; await persistTask(task);
      toast('อัปโหลดเสร็จและส่งเข้าคิวแล้ว', 'success', task.name);
      window.BoldApp?.refresh?.();
      setTimeout(async () => { tasks = tasks.filter((item) => item.id !== task.id); await remove(task.id); render(); }, 12000);
    } catch (error) {
      if (error.name === 'AbortError' || task.status === 'paused') return;
      task.status = navigator.onLine ? 'error' : 'waiting';
      task.error = error.message || 'อัปโหลดไม่สำเร็จ';
      await persistTask(task);
      toast('อัปโหลดหยุดชั่วคราว', 'error', task.error);
    } finally {
      runtime.delete(task.id);
      render();
    }
  }

  async function start({ jobId, name, files }) {
    try { await navigator.storage?.persist?.(); } catch (_) { /* optional */ }
    const safeFiles = Array.from(files || []);
    const task = {
      id: String(jobId), jobId: String(jobId), name: name || safeFiles[0]?.name || 'SubCut upload',
      files: safeFiles, fileRecords: safeFiles.map((file) => ({ name: file.name, size: file.size, type: file.type, totalChunks: Math.max(1, Math.ceil(file.size / CHUNK_SIZE)), hashes: [], manifestHash: '' })),
      totalBytes: safeFiles.reduce((sum, file) => sum + file.size, 0), uploadedBytes: 0,
      speed: 0, eta: 0, status: 'uploading', error: '', persisted: true, createdAt: new Date().toISOString(),
    };
    tasks.unshift(task); render(); await persistTask(task); run(task);
    return task;
  }

  async function pause(id) {
    const task = findTask(id); if (!task) return;
    task.status = 'paused'; runtime.get(task.id)?.controller?.abort(); await persistTask(task);
  }

  async function resume(id) {
    const task = findTask(id); if (!task) return;
    task.status = navigator.onLine ? 'uploading' : 'waiting'; task.error = ''; await persistTask(task); run(task);
  }

  async function cancel(id) {
    const task = findTask(id); if (!task) return;
    runtime.get(task.id)?.controller?.abort();
    try { await api(`/api/jobs/${encodeURIComponent(task.jobId)}/cancel`, { method: 'POST' }); await api(`/api/jobs/${encodeURIComponent(task.jobId)}`, { method: 'DELETE' }); } catch (_) { /* draft cleanup is best effort */ }
    tasks = tasks.filter((item) => item.id !== task.id); await remove(task.id); render(); window.BoldApp?.refresh?.();
  }

  async function restore() {
    try { tasks = (await loadAll()).filter((task) => task.files?.length && task.status !== 'queued'); }
    catch (_) { tasks = []; }
    render();
    for (const task of tasks) {
      if (['uploading', 'hashing', 'finalizing', 'waiting'].includes(task.status)) { task.status = navigator.onLine ? 'uploading' : 'waiting'; run(task); }
    }
  }

  function bind() {
    document.addEventListener('click', (event) => {
      const pauseButton = event.target.closest('[data-upload-pause]'); if (pauseButton) pause(pauseButton.dataset.uploadPause);
      const resumeButton = event.target.closest('[data-upload-resume]'); if (resumeButton) resume(resumeButton.dataset.uploadResume);
      const removeButton = event.target.closest('[data-upload-remove]'); if (removeButton) cancel(removeButton.dataset.uploadRemove);
      const toggle = event.target.closest('[data-upload-dock-toggle]'); if (toggle) document.getElementById('upload-dock')?.classList.toggle('collapsed');
    });
    window.addEventListener('online', () => { render(); tasks.filter((task) => task.status === 'waiting').forEach((task) => resume(task.id)); });
    window.addEventListener('offline', () => { tasks.filter((task) => ['uploading', 'hashing'].includes(task.status)).forEach((task) => pause(task.id).then(() => { task.status = 'waiting'; persistTask(task); })); render(); });
  }

  return { start, pause, resume, cancel, restore, render, bind, getTasks: () => tasks };
})();
