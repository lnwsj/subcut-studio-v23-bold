

'use strict';

const DEMO_MODE = new URLSearchParams(location.search).get('demo') === '1';
const DEMO_SEED = new URLSearchParams(location.search).get('seed') === '1';
const AUTH_STORAGE_KEY = 'sj88_auth_bundle_v1';
const BROWSER_STORAGE_KEY = 'sj88_browser_key_v1';
const PREF_STORAGE_KEY = 'sj88_subcut_preferences_v1';
const CHUNK_SIZE = 64 * 1024 * 1024;
const VIDEO_RE = /\.(mp4|mov|mkv|avi|webm|m4v|flv)$/i;

const MODE_META = {
  subtitle: {
    key: 'subtitle',
    serverMode: 'autosu_only',
    title: 'ทำซับอัตโนมัติ',
    description: 'ถอดเสียงและฝังซับลงวิดีโอ',
    summary: 'ทำซับพร้อมใช้ โดยคงจังหวะเดิม',
    icon: 'i-caption',
    trim: false,
    subtitle: true,
  },
  silence: {
    key: 'silence',
    serverMode: 'silence_trim_only',
    title: 'ตัดเสียงเงียบ',
    description: 'ตัดช่วงนิ่งออกโดยไม่ทำซับ',
    summary: 'ลดช่วงว่างและคงเสียงพูดสำคัญ',
    icon: 'i-wave',
    trim: true,
    subtitle: false,
  },
  combined: {
    key: 'combined',
    serverMode: 'autosu_only',
    title: 'ทำซับ + ตัดเงียบ',
    description: 'ประมวลผลครบในคิวเดียว',
    summary: 'ตัดช่วงว่างก่อนถอดเสียงและฝังซับ',
    icon: 'i-combine',
    trim: true,
    subtitle: true,
  },
};

const PRESETS = {
  conservative: { threshold: -45, minSilence: 0.7, margin: 0, label: 'ถนอมเสียง' },
  keep_quiet: { threshold: -40, minSilence: 0.5, margin: 0, label: 'สมดุล' },
  aggressive: { threshold: -35, minSilence: 0.4, margin: 0, label: 'ตัดมาก' },
  custom: { label: 'กำหนดเอง' },
};

const state = {
  auth: null,
  user: null,
  view: 'workspace',
  mode: 'combined',
  files: [],
  jobs: [],
  history: [],
  members: [],
  memberQuery: '',
  memberStatus: 'all',
  jobFilter: 'all',
  historyQuery: '',
  historyMode: 'all',
  selectedPreset: 'keep_quiet',
  submitting: false,
  pollingTimer: null,
  seenStatuses: new Map(),
  preferences: {
    browserNotifications: false,
    soundNotifications: true,
  },
  trimDefaults: {
    trim_silence: false,
    trim_silence_threshold_db: -40,
    trim_silence_min_silence_sec: 0.5,
    trim_silence_margin_sec: 0,
    trim_silence_min_keep_sec: 0.08,
    trim_silence_min_output_sec: 1,
    trim_silence_preset: 'keep_quiet',
  },
};

const byId = (id) => document.getElementById(id);
const all = (selector, root = document) => Array.from(root.querySelectorAll(selector));

function icon(id) {
  return `<svg aria-hidden="true"><use href="#${id}"></use></svg>`;
}

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function formatBytes(bytes) {
  const value = Math.max(0, Number(bytes) || 0);
  if (value < 1024) return `${value} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let size = value / 1024;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  const digits = size >= 100 ? 0 : size >= 10 ? 1 : 2;
  return `${size.toFixed(digits)} ${units[index]}`;
}

function formatDate(value, withTime = true) {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat('th-TH', {
    day: '2-digit',
    month: 'short',
    year: '2-digit',
    ...(withTime ? { hour: '2-digit', minute: '2-digit' } : {}),
  }).format(date);
}

function formatDuration(seconds) {
  const value = Math.max(0, Number(seconds) || 0);
  if (value < 60) return `${Math.round(value)} วินาที`;
  if (value < 3600) return `${(value / 60).toFixed(value < 600 ? 1 : 0)} นาที`;
  return `${(value / 3600).toFixed(1)} ชม.`;
}

function makeJobName() {
  const now = new Date();
  const stamp = new Intl.DateTimeFormat('th-TH', {
    day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit',
  }).format(now).replaceAll('/', '-').replaceAll(':', '.').replace(', ', '_');
  return `SubCut_${stamp}`;
}

function getPreferences() {
  try {
    const raw = localStorage.getItem(PREF_STORAGE_KEY);
    if (raw) state.preferences = { ...state.preferences, ...JSON.parse(raw) };
  } catch (_) { /* no-op */ }
}

function savePreferences() {
  try { localStorage.setItem(PREF_STORAGE_KEY, JSON.stringify(state.preferences)); } catch (_) { /* no-op */ }
}

function toast(message, type = 'success', detail = '') {
  const stack = byId('toast-stack');
  const node = document.createElement('div');
  node.className = `toast ${type}`;
  const iconId = type === 'error' ? 'i-close' : type === 'info' ? 'i-info' : 'i-check';
  node.innerHTML = `<span class="toast-icon">${icon(iconId)}</span><p><strong>${escapeHtml(message)}</strong>${detail ? `<span>${escapeHtml(detail)}</span>` : ''}</p>`;
  stack.appendChild(node);
  window.setTimeout(() => node.remove(), 4200);
}

function setBusy(isBusy) {
  document.body.classList.toggle('busy', Boolean(isBusy));
}

function authHeaders(extra = {}) {
  const headers = new Headers(extra);
  if (state.auth?.access_token) headers.set('Authorization', `Bearer ${state.auth.access_token}`);
  return headers;
}

function parseApiError(payload, fallback = 'เกิดข้อผิดพลาดจากเซิร์ฟเวอร์') {
  if (!payload) return fallback;
  if (typeof payload === 'string') return payload || fallback;
  const detail = payload.detail;
  if (typeof detail === 'string') return detail;
  if (detail && typeof detail.message === 'string') return detail.message;
  if (typeof payload.error === 'string') return payload.error;
  if (typeof payload.message === 'string') return payload.message;
  return fallback;
}

async function refreshAccessToken() {
  if (!state.auth?.refresh_token || DEMO_MODE) return false;
  try {
    const response = await fetch('/api/auth/refresh', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: state.auth.refresh_token }),
      credentials: 'same-origin',
    });
    if (!response.ok) return false;
    const payload = await response.json();
    applyAuth(payload);
    return true;
  } catch (_) {
    return false;
  }
}

async function api(path, options = {}, allowRefresh = true) {
  if (DEMO_MODE) throw new Error('demo_api_not_available');
  const requestOptions = { ...options };
  requestOptions.headers = authHeaders(options.headers || {});
  if (options.json !== undefined) {
    requestOptions.method = requestOptions.method || 'POST';
    requestOptions.headers.set('Content-Type', 'application/json');
    requestOptions.body = JSON.stringify(options.json);
    delete requestOptions.json;
  }
  requestOptions.credentials = requestOptions.credentials || 'same-origin';
  const response = await fetch(path, requestOptions);
  if (response.status === 401 && allowRefresh && await refreshAccessToken()) {
    return api(path, options, false);
  }
  const contentType = response.headers.get('content-type') || '';
  const payload = contentType.includes('application/json')
    ? await response.json().catch(() => ({}))
    : await response.text().catch(() => '');
  if (!response.ok) {
    const error = new Error(parseApiError(payload, `HTTP ${response.status}`));
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

async function retry(task, attempts = 3) {
  let lastError = null;
  for (let index = 0; index < attempts; index += 1) {
    try { return await task(index); } catch (error) {
      lastError = error;
      const retryable = !Number.isFinite(Number(error?.status)) || Number(error.status) >= 500 || Number(error.status) === 429;
      if (!retryable || index === attempts - 1) throw error;
      await new Promise((resolve) => setTimeout(resolve, 700 * (2 ** index)));
    }
  }
  throw lastError;
}

function getBrowserKey() {
  try { return localStorage.getItem(BROWSER_STORAGE_KEY) || ''; } catch (_) { return ''; }
}

function saveBrowserKey(value) {
  const key = String(value || '').trim();
  if (!key) return;
  try { localStorage.setItem(BROWSER_STORAGE_KEY, key); } catch (_) { /* cookie remains */ }
}

function clearBrowserKey() {
  try { localStorage.removeItem(BROWSER_STORAGE_KEY); } catch (_) { /* no-op */ }
}

function browserLabel() {
  const brands = navigator.userAgentData?.brands?.map((item) => item.brand).join(', ');
  return String(brands || navigator.userAgent || 'Chrome browser').slice(0, 240);
}

function isGuestUser() {
  const user = state.user || {};
  return Boolean(user.is_guest || String(user.role || '').toLowerCase() === 'guest' || user.signup_source === 'browser_guest');
}

function applyAuth(payload) {
  if (!payload?.access_token) return;
  state.auth = {
    access_token: payload.access_token,
    refresh_token: payload.refresh_token || state.auth?.refresh_token || '',
    expires_in: payload.expires_in || 86400,
    user: payload.user || state.user || null,
  };
  state.user = state.auth.user;
  if (payload.browser_key) saveBrowserKey(payload.browser_key);
  try { localStorage.setItem(AUTH_STORAGE_KEY, JSON.stringify(state.auth)); } catch (_) { /* no-op */ }
  updateAccountUi();
}

function clearAuth() {
  state.auth = null;
  state.user = null;
  try { localStorage.removeItem(AUTH_STORAGE_KEY); } catch (_) { /* no-op */ }
}

function restoreAuth() {
  try {
    const raw = localStorage.getItem(AUTH_STORAGE_KEY);
    if (!raw) return;
    const parsed = JSON.parse(raw);
    if (parsed?.access_token) {
      state.auth = parsed;
      state.user = parsed.user || null;
    }
  } catch (_) { clearAuth(); }
}

async function ensureBrowserSession({ forceNew = false } = {}) {
  if (DEMO_MODE) return false;
  const storedKey = forceNew ? '' : getBrowserKey();
  try {
    const response = await fetch('/api/auth/browser', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({
        browser_key: storedKey,
        browser_label: browserLabel(),
        force_new: Boolean(forceNew),
      }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(parseApiError(payload, 'สร้างบัญชีอัตโนมัติไม่สำเร็จ'));
    applyAuth(payload);
    hideAuth();
    return true;
  } catch (error) {
    if (!forceNew && storedKey && ['account_disabled', 'account_rejected'].includes(error.message)) {
      clearAuth(); clearBrowserKey();
      return ensureBrowserSession({ forceNew: true });
    }
    return false;
  }
}

function showAuth(tab = 'login') {
  switchAuthTab(tab);
  byId('auth-screen').classList.remove('is-hidden');
  byId('auth-close').classList.toggle('hidden', !state.user);
  byId('continue-guest').classList.toggle('hidden', !state.user);
}

function hideAuth() {
  byId('auth-screen').classList.add('is-hidden');
  authStatus();
}

function authStatus(message = '', type = '') {
  const box = byId('auth-status');
  box.textContent = message;
  box.className = `auth-status ${type}`.trim();
  box.classList.toggle('hidden', !message);
}

function isAdminUser() {
  return ['owner', 'admin', 'superadmin'].includes(String(state.user?.role || '').toLowerCase());
}

function updateAccountUi() {
  const user = state.user || {};
  const guest = isGuestUser();
  const name = user.display_name || user.name || (DEMO_MODE ? 'น้ำน่าน Demo' : 'Chrome Guest');
  const email = guest ? 'งานผูกกับ Chrome นี้' : (user.email || (DEMO_MODE ? 'demo@subcut.local' : '—'));
  const plan = String(user.plan || (DEMO_MODE ? 'pro' : 'free'));
  const initial = Array.from(name.trim())[0]?.toUpperCase() || 'S';
  byId('profile-name').textContent = name;
  byId('profile-plan').textContent = guest ? 'Guest · Chrome นี้' : `${plan} plan`;
  byId('profile-avatar').textContent = initial;
  byId('settings-avatar').textContent = initial;
  byId('settings-name').textContent = guest ? 'ใช้งานแบบ Guest' : name;
  byId('settings-email').textContent = email;
  byId('settings-plan').textContent = guest ? 'GUEST · CHROME' : `${plan.toUpperCase()} PLAN`;
  byId('guest-banner').classList.toggle('hidden', !guest);
  byId('history-account-copy').textContent = guest
    ? 'งานทั้งหมดจะกลับมาอัตโนมัติเมื่อเปิดด้วย Chrome เดิม'
    : 'ดาวน์โหลดงานย้อนหลังได้จากทุกอุปกรณ์ที่เข้าสู่ระบบบัญชีนี้';
  byId('profile-register').classList.toggle('hidden', !guest);
  byId('profile-login').classList.toggle('hidden', !guest);
  byId('profile-logout').classList.toggle('hidden', guest);
  const accountButton = byId('logout-button');
  accountButton.className = guest ? 'button primary' : 'button danger ghost';
  accountButton.innerHTML = guest
    ? `${icon('i-user')}<span>สมัครสมาชิกและเก็บงานเดิม</span>`
    : `${icon('i-logout')}<span>ออกจากระบบ</span>`;
  byId('member-admin-card').classList.toggle('hidden', !isAdminUser());
}

async function validateSession() {
  if (DEMO_MODE) {
    state.auth = { access_token: 'demo-token', refresh_token: 'demo-refresh' };
    state.user = { id: 88, display_name: 'น้ำน่าน Demo', email: 'demo@subcut.local', plan: 'pro', role: 'owner' };
    state.auth.user = state.user;
    hideAuth();
    updateAccountUi();
    return true;
  }
  if (!state.auth?.access_token) return false;
  try {
    const user = await api('/api/auth/me');
    state.user = user;
    state.auth.user = user;
    try { localStorage.setItem(AUTH_STORAGE_KEY, JSON.stringify(state.auth)); } catch (_) { /* no-op */ }
    hideAuth();
    updateAccountUi();
    return true;
  } catch (error) {
    clearAuth();
    return false;
  }
}

async function exchangeSsoTicket() {
  if (DEMO_MODE) return false;
  const params = new URLSearchParams(location.search);
  const ticket = params.get('sso_ticket') || params.get('ticket');
  if (!ticket) return false;
  try {
    authStatus('กำลังเข้าสู่ระบบผ่านบัญชีชั้นเรียน…');
    const payload = await api('/api/auth/sso/exchange', {
      method: 'POST',
      json: { ticket, browser_key: getBrowserKey() },
    }, false);
    applyAuth(payload);
    params.delete('sso_ticket'); params.delete('ticket');
    history.replaceState(null, '', `${location.pathname}${params.toString() ? `?${params}` : ''}`);
    hideAuth();
    return true;
  } catch (error) {
    authStatus(error.message || 'เข้าสู่ระบบ SSO ไม่สำเร็จ', 'error');
    return false;
  }
}

function switchAuthTab(tab) {
  const login = tab === 'login';
  all('[data-auth-tab]').forEach((button) => button.classList.toggle('active', button.dataset.authTab === tab));
  byId('login-form').classList.toggle('hidden', !login);
  byId('register-form').classList.toggle('hidden', login);
  byId('auth-title').textContent = login ? 'เข้าสู่ระบบบัญชีเดิม' : 'สมัครสมาชิกโดยเก็บงานเดิม';
  byId('auth-subtitle').textContent = login
    ? 'งาน Guest ใน Chrome นี้จะถูกรวมเข้าบัญชีโดยอัตโนมัติ'
    : 'อัปเกรด Guest เดิม งานและคิวทั้งหมดจะไม่หาย';
  authStatus();
}

async function handleLogin(event) {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  authStatus('กำลังตรวจสอบบัญชีและรวมงานเดิม…');
  try {
    const payload = await api('/api/auth/login', {
      method: 'POST',
      json: {
        email: form.get('email'),
        browser_key: getBrowserKey(),
      },
    }, false);
    applyAuth(payload);
    hideAuth();
    event.currentTarget.reset();
    await afterAuthenticated();
    const migrated = Number(payload.guest_jobs_migrated || 0);
    toast('เข้าสู่ระบบสำเร็จ', 'success', migrated ? `ย้ายงาน Guest เดิม ${migrated} งานเข้าบัญชีแล้ว` : 'Chrome นี้จะจำบัญชีให้อัตโนมัติ');
  } catch (error) {
    const messages = {
      account_pending: 'บัญชีกำลังรอผู้ดูแลอนุมัติ',
      account_rejected: 'บัญชีนี้ไม่ได้รับอนุมัติ',
      account_disabled: 'บัญชีนี้ถูกปิดใช้งาน',
    };
    authStatus(messages[error.message] || error.message || 'อีเมลหรือรหัสผ่านไม่ถูกต้อง', 'error');
  }
}

async function handleRegister(event) {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  authStatus('กำลังอัปเกรด Guest และเก็บงานเดิม…');
  try {
    const payload = await api('/api/auth/register', {
      method: 'POST',
      json: {
        display_name: form.get('display_name'),
        email: form.get('email'),
        browser_key: getBrowserKey(),
      },
    }, false);
    if (payload.pending_approval) {
      authStatus('สมัครสำเร็จแล้ว บัญชีกำลังรอผู้ดูแลอนุมัติ คุณยังใช้ Guest ต่อได้', 'success');
      event.currentTarget.reset();
      return;
    }
    applyAuth(payload);
    hideAuth();
    event.currentTarget.reset();
    await afterAuthenticated();
    const preserved = Number(payload.jobs_preserved || 0);
    toast('สมัครสมาชิกสำเร็จ', 'success', preserved ? `เก็บงานเดิมครบ ${preserved} งาน` : 'บัญชีนี้ผูกกับ Chrome แล้ว');
  } catch (error) {
    const message = String(error.message || 'สมัครสมาชิกไม่สำเร็จ');
    authStatus(message.includes('already registered') ? 'อีเมลนี้มีบัญชีแล้ว กรุณาเลือก “เข้าสู่ระบบ”' : message, 'error');
  }
}

async function logout() {
  try {
    if (!DEMO_MODE && state.auth?.refresh_token) {
      await fetch('/api/auth/logout', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({ refresh_token: state.auth.refresh_token }),
      });
    }
  } catch (_) { /* no-op */ }
  clearAuth();
  clearBrowserKey();
  state.jobs = [];
  state.history = [];
  state.members = [];
  byId('profile-menu').classList.add('hidden');
  const guestReady = await ensureBrowserSession({ forceNew: true });
  if (guestReady) {
    await afterAuthenticated();
    setView('workspace');
    toast('ออกจากบัญชีแล้ว', 'info', 'เริ่ม Guest ใหม่สำหรับ Chrome นี้');
  } else {
    showAuth('login');
    authStatus('สร้าง Guest ใหม่ไม่สำเร็จ กรุณาเข้าสู่ระบบ', 'error');
  }
}

function handleAccountButton() {
  if (isGuestUser()) showAuth('register');
  else logout();
}

function setView(view) {
  if (!['workspace', 'queue', 'history', 'settings'].includes(view)) return;
  state.view = view;
  all('[data-view-panel]').forEach((panel) => panel.classList.toggle('active', panel.dataset.viewPanel === view));
  all('.nav-item').forEach((button) => button.classList.toggle('active', button.dataset.view === view));
  byId('sidebar').classList.remove('open');
  byId('sidebar-backdrop').classList.add('hidden');
  window.scrollTo({ top: 0, behavior: 'smooth' });
  if (view === 'queue') loadJobs({ silent: true });
  if (view === 'history') loadHistory({ silent: true });
  if (view === 'settings') {
    loadTrimDefaults({ silent: true });
    if (isAdminUser()) loadMembers({ silent: true });
  }
}

function chooseMode(mode) {
  if (!MODE_META[mode]) return;
  state.mode = mode;
  all('.mode-card').forEach((card) => card.classList.toggle('selected', card.dataset.mode === mode));
  byId('subtitle-settings').classList.toggle('hidden', !MODE_META[mode].subtitle);
  byId('silence-settings').classList.toggle('hidden', !MODE_META[mode].trim);
  updateSummary();
}

function fileTotalBytes() {
  return state.files.reduce((sum, file) => sum + Math.max(0, Number(file.size) || 0), 0);
}

function updateSummary() {
  const mode = MODE_META[state.mode];
  byId('summary-mode').textContent = mode.title;
  byId('summary-description').textContent = mode.summary;
  byId('summary-preview').querySelector('.preview-art').innerHTML = `${icon(mode.icon)}<span class="wave-bars"><i></i><i></i><i></i><i></i><i></i><i></i><i></i></span>`;
  byId('summary-files').textContent = `${state.files.length} ไฟล์`;
  byId('summary-size').textContent = formatBytes(fileTotalBytes());
  byId('summary-subtitle').textContent = mode.subtitle ? 'เปิด' : 'ไม่ใช้';
  const presetLabel = PRESETS[state.selectedPreset]?.label || 'กำหนดเอง';
  byId('summary-silence').textContent = mode.trim ? `เปิด · ${presetLabel}` : 'ไม่ใช้';
  const button = byId('create-job-button');
  button.disabled = state.files.length === 0 || state.submitting;
  byId('submit-note').textContent = state.submitting
    ? 'กำลังส่งงาน กรุณาอย่าปิดหน้านี้จนกว่าอัปโหลดเสร็จ'
    : state.files.length === 0
      ? 'เพิ่มวิดีโออย่างน้อย 1 ไฟล์'
      : `พร้อมส่ง ${state.files.length} ไฟล์เข้าคิวพื้นหลัง`;
  updateStepIndicator();
}

/* 🎯 Visual step indicator: highlights current step (1/2/3) based on what user has done */
function updateStepIndicator() {
  const hasFile = state.files.length > 0;
  const modeSelected = !!state.mode;
  const stepNodes = document.querySelectorAll('.step-number[data-step]');
  stepNodes.forEach((node) => {
    const step = Number(node.dataset.step);
    node.classList.remove('active', 'done');
    if (step === 1 && hasFile) node.classList.add('done');
    else if (step === 1 && !hasFile) node.classList.add('active');
    else if (step === 2 && hasFile && modeSelected) node.classList.add(hasFile ? 'done' : 'active');
    else if (step === 2 && hasFile) node.classList.add('active');
    else if (step === 3 && hasFile) node.classList.add('active');
  });
  // Show "ready" badge on upload section once file is added
  const uploadSection = byId('upload-section');
  if (uploadSection) uploadSection.classList.toggle('has-file', hasFile);

  // Hide quickstart tip once user has done anything
  const quickstartTip = byId('quickstart-tip');
  if (quickstartTip && !quickstartTip.classList.contains('hidden') && hasFile) {
    quickstartTip.classList.add('hidden');
  }
}

/* 🎬 Generate a 15s demo video in-browser using Canvas + MediaRecorder + WebAudio synth speech
   Simulates a video with audio narration + visible text so the system can transcribe it. */
async function loadSampleVideo() {
  const button = byId('try-sample-button');
  if (!button) return;
  const originalHTML = button.innerHTML;
  button.disabled = true;
  button.innerHTML = '<span class="sample-button-icon"><svg class="spin"><use href="#i-refresh"/></svg></span><span class="sample-button-text"><strong>กำลังสร้างวิดีโอตัวอย่าง...</strong><small>รอสักครู่</small></span>';
  toast('กำลังสร้างวิดีโอตัวอย่าง 15 วินาที', 'success', 'จะใช้เวลา 2-3 วินาที');
  try {
    const blob = await generateSampleVideoBlob();
    const file = new File([blob], 'sj88-sample-th-15s.mp4', { type: 'video/mp4' });
    addFiles([file]);
    // Auto-scroll to show the new file
    setTimeout(() => {
      const list = byId('file-list');
      if (list) list.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }, 300);
    toast('เพิ่มวิดีโอตัวอย่างแล้ว', 'success', 'พร้อมกดส่งงานได้เลย');
  } catch (error) {
    console.error('Sample video failed', error);
    toast('สร้างวิดีโอตัวอย่างไม่สำเร็จ', 'error', error.message || 'กรุณาลองใช้ไฟล์ของคุณเอง');
  } finally {
    button.disabled = false;
    button.innerHTML = originalHTML;
  }
}

/* Generate a 15s video with Canvas (1080x1080) + audio narration (TTS-like beep) */
async function generateSampleVideoBlob() {
  const W = 720, H = 720, FPS = 24, DURATION = 15;
  const canvas = document.createElement('canvas');
  canvas.width = W; canvas.height = H;
  const ctx = canvas.getContext('2d');
  const stream = canvas.captureStream(FPS);

  // Audio track with simple synth tones that resemble speech rhythm
  const audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  const dest = audioCtx.createMediaStreamDestination();
  const phrases = [
    { t: 0.5,  d: 1.2, f: 380, label: 'สวัสดีครับ' },
    { t: 2.0,  d: 1.4, f: 420, label: 'ยินดีต้อนรับสู่ SubCut' },
    { t: 3.6,  d: 1.0, f: 460, label: 'ระบบทำซับอัตโนมัติ' },
    { t: 4.8,  d: 0.5, f: 0,   label: '' },          // silence
    { t: 5.5,  d: 1.2, f: 440, label: 'และตัดเสียงเงียบ' },
    { t: 6.9,  d: 1.0, f: 400, label: 'พร้อมกันในคลิกเดียว' },
    { t: 8.0,  d: 0.4, f: 0,   label: '' },          // silence
    { t: 8.5,  d: 1.3, f: 500, label: '�องอัปโหลดวิดีโอ' },
    { t: 10.0, d: 1.2, f: 480, label: 'แล้วเลือกโหมด' },
    { t: 11.4, d: 1.4, f: 450, label: 'ที่ต้องการได้เลย' },
    { t: 13.0, d: 0.5, f: 0,   label: '' },          // silence
    { t: 13.6, d: 1.2, f: 520, label: 'ขอให้สนุกกับการใช้งานครับ' },
  ];
  for (const p of phrases) {
    if (p.f === 0) continue; // silence
    const osc = audioCtx.createOscillator();
    const gain = audioCtx.createGain();
    osc.frequency.value = p.f;
    osc.type = 'sine';
    gain.gain.setValueAtTime(0, audioCtx.currentTime + p.t);
    gain.gain.linearRampToValueAtTime(0.15, audioCtx.currentTime + p.t + 0.05);
    gain.gain.linearRampToValueAtTime(0.15, audioCtx.currentTime + p.t + p.d - 0.05);
    gain.gain.linearRampToValueAtTime(0, audioCtx.currentTime + p.t + p.d);
    osc.connect(gain).connect(dest);
    osc.start(audioCtx.currentTime + p.t);
    osc.stop(audioCtx.currentTime + p.t + p.d);
  }
  stream.addTrack(dest.stream.getAudioTracks()[0]);

  const recorder = new MediaRecorder(stream, { mimeType: 'video/webm;codecs=vp9,opus', videoBitsPerSecond: 800_000 });
  const chunks = [];
  recorder.ondataavailable = (e) => { if (e.data.size > 0) chunks.push(e.data); };
  const done = new Promise((resolve) => { recorder.onstop = () => resolve(new Blob(chunks, { type: 'video/webm' })); });
  recorder.start();

  // Render frames
  const startTime = performance.now();
  const totalMs = DURATION * 1000;
  return new Promise((resolve) => {
    function frame() {
      const elapsed = performance.now() - startTime;
      const t = Math.min(elapsed / totalMs, 1);
      drawSampleFrame(ctx, W, H, t, phrases);
      if (elapsed < totalMs) {
        requestAnimationFrame(frame);
      } else {
        recorder.stop();
        audioCtx.close();
        done.then(resolve);
      }
    }
    requestAnimationFrame(frame);
  });
}

function drawSampleFrame(ctx, W, H, t, phrases) {
  // background gradient
  const grad = ctx.createLinearGradient(0, 0, W, H);
  grad.addColorStop(0, '#7567ff');
  grad.addColorStop(0.5, '#20c6d8');
  grad.addColorStop(1, '#ff6f61');
  ctx.fillStyle = grad;
  ctx.fillRect(0, 0, W, H);

  // decorative circles
  const tnow = t * 15;
  for (let i = 0; i < 6; i++) {
    const x = W / 2 + Math.cos(tnow * 0.6 + i) * 200;
    const y = H / 2 + Math.sin(tnow * 0.4 + i * 1.3) * 200;
    ctx.beginPath();
    ctx.fillStyle = `rgba(255,255,255,${0.06 + 0.04 * Math.sin(tnow + i)})`;
    ctx.arc(x, y, 80 + i * 10, 0, Math.PI * 2);
    ctx.fill();
  }

  // center card
  const cardW = W * 0.78, cardH = H * 0.55;
  const cardX = (W - cardW) / 2, cardY = (H - cardH) / 2;
  ctx.fillStyle = 'rgba(255,255,255,0.96)';
  roundRect(ctx, cardX, cardY, cardW, cardH, 32);
  ctx.fill();

  // title
  ctx.fillStyle = '#1c1f33';
  ctx.font = 'bold 48px "Noto Sans Thai", sans-serif';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText('🎬 SubCut Studio', W / 2, cardY + 70);

  // current phrase (subtitle-like)
  const currentTime = t * 15;
  let currentLabel = '';
  for (const p of phrases) {
    if (currentTime >= p.t && currentTime <= p.t + p.d && p.label) {
      currentLabel = p.label;
      break;
    }
  }
  if (currentLabel) {
    ctx.fillStyle = '#6f61f7';
    ctx.font = 'bold 40px "Noto Sans Thai", sans-serif';
    // subtitle-style with background
    const textW = ctx.measureText(currentLabel).width;
    ctx.fillStyle = 'rgba(111,97,247,0.92)';
    roundRect(ctx, W / 2 - textW / 2 - 20, cardY + 160, textW + 40, 60, 12);
    ctx.fill();
    ctx.fillStyle = '#ffffff';
    ctx.fillText(currentLabel, W / 2, cardY + 190);
  }

  // waveform decoration
  const waveY = cardY + cardH - 80;
  const bars = 24;
  for (let i = 0; i < bars; i++) {
    const h = 6 + Math.abs(Math.sin(tnow * 3 + i * 0.5)) * 30;
    ctx.fillStyle = `hsl(${(i * 14) % 360}, 70%, 60%)`;
    roundRect(ctx, cardX + 40 + i * (cardW - 80) / bars, waveY - h / 2, (cardW - 80) / bars - 4, h, 4);
    ctx.fill();
  }

  // timestamp
  ctx.fillStyle = 'rgba(0,0,0,0.45)';
  ctx.font = '24px "Noto Sans Thai", sans-serif';
  ctx.textAlign = 'left';
  ctx.fillText(`${(t * 15).toFixed(1)}s / 15.0s`, 20, H - 20);
}

function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

function validateFiles(files) {
  const valid = [];
  const rejected = [];
  for (const file of files) {
    if (!VIDEO_RE.test(file.name || '')) rejected.push(`${file.name}: ไม่ใช่ไฟล์วิดีโอที่รองรับ`);
    else if (Number(file.size) <= 0) rejected.push(`${file.name}: ไฟล์ว่าง`);
    else valid.push(file);
  }
  return { valid, rejected };
}

function addFiles(fileList) {
  const { valid, rejected } = validateFiles(Array.from(fileList || []));
  const existing = new Set(state.files.map((file) => `${file.name}:${file.size}:${file.lastModified}`));
  for (const file of valid) {
    const key = `${file.name}:${file.size}:${file.lastModified}`;
    if (!existing.has(key)) {
      state.files.push(file);
      existing.add(key);
    }
  }
  if (rejected.length) toast('มีไฟล์ที่ไม่ถูกเพิ่ม', 'error', rejected.slice(0, 2).join(' · '));
  renderFiles();
  updateSummary();
}

function renderFiles() {
  const list = byId('file-list');
  list.classList.toggle('hidden', state.files.length === 0);
  list.innerHTML = state.files.map((file, index) => `
    <div class="file-row">
      <span class="file-type">${icon('i-file')}</span>
      <span class="file-meta"><strong title="${escapeHtml(file.name)}">${escapeHtml(file.name)}</strong><small>${formatBytes(file.size)} · พร้อมอัปโหลด</small></span>
      <span class="file-status">พร้อม</span>
      <button class="remove-file" data-remove-file="${index}" aria-label="ลบ ${escapeHtml(file.name)}">${icon('i-trash')}</button>
    </div>`).join('');
}

function setPreset(preset, updateInputs = true) {
  if (!PRESETS[preset]) preset = 'custom';
  state.selectedPreset = preset;
  all('#silence-presets button').forEach((button) => button.classList.toggle('active', button.dataset.preset === preset));
  if (updateInputs && preset !== 'custom') {
    const value = PRESETS[preset];
    byId('trim-threshold').value = value.threshold;
    byId('trim-min-silence').value = value.minSilence;
    byId('trim-margin').value = value.margin;
  }
  updateSummary();
}

function resetWorkspaceSettings() {
  byId('subtitle-template').value = 'default';
  byId('subtitle-language').value = 'th';
  byId('subtitle-words').value = '3';
  byId('subtitle-position').value = '88';
  byId('subtitle-font-size').value = '42';
  byId('position-output').value = '88%';
  byId('font-output').value = '42 px';
  const defaults = state.trimDefaults;
  byId('trim-threshold').value = defaults.trim_silence_threshold_db ?? -40;
  byId('trim-min-silence').value = defaults.trim_silence_min_silence_sec ?? 0.5;
  byId('trim-margin').value = defaults.trim_silence_margin_sec ?? 0;
  setPreset(defaults.trim_silence_preset || 'keep_quiet', false);
  toast('คืนค่าแนะนำแล้ว', 'info');
}

function buildJobSettings() {
  const mode = MODE_META[state.mode];
  return {
    workflow: state.mode,
    trim_silence: mode.trim,
    trim_silence_preset: state.selectedPreset,
    trim_silence_threshold_db: Number(byId('trim-threshold').value || -40),
    trim_silence_min_silence_sec: Number(byId('trim-min-silence').value || 0.5),
    trim_silence_margin_sec: Number(byId('trim-margin').value || 0),
    trim_silence_min_keep_sec: Number(state.trimDefaults.trim_silence_min_keep_sec || 0.08),
    trim_silence_min_output_sec: Number(state.trimDefaults.trim_silence_min_output_sec || 1),
    subtitle_template_id: byId('subtitle-template').value,
    subtitle_language: byId('subtitle-language').value,
    subtitle_position_percent: Number(byId('subtitle-position').value || 88),
    subtitle_font_size: Number(byId('subtitle-font-size').value || 42),
    subtitle_max_words_per_line: Number(byId('subtitle-words').value || 3),
    source_app: 'sj88_subcut_studio',
  };
}

function setUploadProgress(percent, title, detail = '') {
  const panel = byId('upload-progress');
  panel.classList.remove('hidden');
  const safe = Math.max(0, Math.min(100, Math.round(Number(percent) || 0)));
  byId('upload-progress-title').textContent = title;
  byId('upload-progress-percent').textContent = `${safe}%`;
  byId('upload-progress-bar').style.width = `${safe}%`;
  byId('upload-progress-detail').textContent = detail;
}

async function sha256Blob(blob) {
  if (!crypto?.subtle) throw new Error('เบราว์เซอร์นี้ไม่รองรับ SHA-256 สำหรับไฟล์ใหญ่');
  const digest = await crypto.subtle.digest('SHA-256', await blob.arrayBuffer());
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('');
}

async function sha256Text(text) {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(String(text || '')));
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('');
}

async function uploadSmallFiles(jobId, files) {
  const totalBytes = files.reduce((sum, file) => sum + file.size, 0);
  let loaded = 0;
  for (let index = 0; index < files.length; index += 1) {
    const file = files[index];
    setUploadProgress((loaded / Math.max(totalBytes, 1)) * 100, `อัปโหลดไฟล์ ${index + 1}/${files.length}`, `${file.name} · ${formatBytes(loaded)} / ${formatBytes(totalBytes)}`);
    const form = new FormData();
    form.append('files', file, file.name);
    const params = new URLSearchParams({
      append: index > 0 ? '1' : '0', file_index: String(index), total_files: String(files.length), file_size: String(file.size),
    });
    await retry(() => api(`/api/jobs/${jobId}/upload?${params}`, { method: 'POST', body: form }));
    loaded += file.size;
    setUploadProgress((loaded / Math.max(totalBytes, 1)) * 100, `อัปโหลดไฟล์ ${index + 1}/${files.length} สำเร็จ`, `${formatBytes(loaded)} / ${formatBytes(totalBytes)}`);
  }
}

async function uploadChunkedFiles(jobId, files) {
  const totalBytes = files.reduce((sum, file) => sum + file.size, 0);
  let uploadedBefore = 0;
  for (let fileIndex = 0; fileIndex < files.length; fileIndex += 1) {
    const file = files[fileIndex];
    const totalChunks = Math.max(1, Math.ceil(file.size / CHUNK_SIZE));
    const hashes = [];
    for (let chunkIndex = 0; chunkIndex < totalChunks; chunkIndex += 1) {
      const start = chunkIndex * CHUNK_SIZE;
      const end = Math.min(start + CHUNK_SIZE, file.size);
      setUploadProgress((uploadedBefore / Math.max(totalBytes, 1)) * 100, `ตรวจสอบไฟล์ ${fileIndex + 1}/${files.length}`, `กำลังคำนวณ SHA-256 ส่วน ${chunkIndex + 1}/${totalChunks}`);
      hashes.push(await sha256Blob(file.slice(start, end)));
    }
    const manifestHash = await sha256Text(hashes.join(''));
    let uploadedInFile = 0;
    for (let chunkIndex = 0; chunkIndex < totalChunks; chunkIndex += 1) {
      const start = chunkIndex * CHUNK_SIZE;
      const end = Math.min(start + CHUNK_SIZE, file.size);
      const blob = file.slice(start, end);
      const params = new URLSearchParams({
        chunk_index: String(chunkIndex), total_chunks: String(totalChunks), total_files: String(files.length),
        file_name: file.name, file_size: String(file.size), file_index: String(fileIndex),
        chunk_sha256: hashes[chunkIndex], chunk_manifest_sha256: manifestHash, append: fileIndex > 0 ? '1' : '0',
      });
      const form = new FormData();
      form.append('files', blob, `${file.name}.part${chunkIndex}`);
      await retry(() => api(`/api/jobs/${jobId}/upload/chunk?${params}`, { method: 'POST', body: form }));
      uploadedInFile += blob.size;
      const loaded = uploadedBefore + uploadedInFile;
      setUploadProgress((loaded / Math.max(totalBytes, 1)) * 100, `อัปโหลดไฟล์ ${fileIndex + 1}/${files.length}`, `ส่วน ${chunkIndex + 1}/${totalChunks} · ${formatBytes(loaded)} / ${formatBytes(totalBytes)}`);
    }
    uploadedBefore += file.size;
  }
  setUploadProgress(100, 'กำลังตรวจสอบและรวมไฟล์บนเซิร์ฟเวอร์', formatBytes(totalBytes));
  await retry(() => api(`/api/jobs/${jobId}/upload/chunked/complete`, { method: 'POST' }));
}

async function uploadFiles(jobId) {
  const requiresChunks = state.files.some((file) => file.size > CHUNK_SIZE);
  if (requiresChunks) return uploadChunkedFiles(jobId, state.files);
  return uploadSmallFiles(jobId, state.files);
}

function createDemoJob(name, settings) {
  const id = `demo-${Date.now().toString(36)}`;
  const job = {
    id, name, mode: MODE_META[state.mode].serverMode, status: 'queued', progress: 6,
    settings, created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
    result: { output_count: 0, runtime_metrics: {} },
    source_files: state.files.map((file) => ({ name: file.name, size: file.size })),
  };
  state.jobs.unshift(job);
  renderJobs();
  window.setTimeout(() => { job.status = 'running'; job.progress = 28; job.updated_at = new Date().toISOString(); renderJobs(); }, 700);
  window.setTimeout(() => { job.progress = 64; renderJobs(); }, 1700);
  window.setTimeout(() => { job.progress = 87; renderJobs(); }, 2700);
  window.setTimeout(() => {
    job.status = 'done'; job.progress = 100; job.updated_at = new Date().toISOString();
    job.result = {
      output_count: job.source_files.length,
      elapsed: '00:49',
      outputs: job.source_files.map((file) => `/demo/output/${file.name.replace(/\.[^.]+$/, '')}_ready.mp4`),
      runtime_metrics: { removed_duration_sec: settings.trim_silence ? 34.8 : 0, trim_applied_count: settings.trim_silence ? job.source_files.length : 0 },
    };
    state.history.unshift({
      id: job.id, name: job.name, mode: job.mode, status: 'done', created_at: job.created_at,
      finished_at: job.updated_at, duration_str: '00:49', total_outputs: job.result.output_count,
      total_input_bytes: job.source_files.reduce((sum, file) => sum + file.size, 0), total_output_bytes: Math.round(job.source_files.reduce((sum, file) => sum + file.size, 0) * .82), outcome: job.result,
    });
    renderJobs(); notifyJob(job);
  }, 3900);
  return job;
}

async function submitJob() {
  if (state.files.length === 0 || state.submitting) return;
  if (!state.user) { showAuth(); return; }
  state.submitting = true;
  updateSummary();
  const button = byId('create-job-button');
  button.innerHTML = `${icon('i-upload')}<span>กำลังอัปโหลด…</span>`;
  const name = byId('job-name').value.trim() || makeJobName();
  const settings = buildJobSettings();
  const filesSnapshot = [...state.files];
  try {
    if (DEMO_MODE) {
      setUploadProgress(18, 'กำลังสร้างงานจำลอง', formatBytes(fileTotalBytes()));
      await new Promise((resolve) => setTimeout(resolve, 450));
      setUploadProgress(100, 'อัปโหลดสำเร็จ', `${state.files.length} ไฟล์`);
      createDemoJob(name, settings);
    } else {
      const created = await api('/api/jobs', {
        method: 'POST',
        json: { name, mode: MODE_META[state.mode].serverMode, settings },
      });
      const jobId = created?.job?.id;
      if (!jobId) throw new Error('เซิร์ฟเวอร์ไม่ได้ส่งรหัสงานกลับมา');
      await uploadFiles(jobId);
      setUploadProgress(100, 'อัปโหลดเสร็จแล้ว กำลังส่งเข้าคิว', `${filesSnapshot.length} ไฟล์`);
      const queued = await api(`/api/jobs/${jobId}/process`, { method: 'POST' });
      if (!queued?.ok) throw new Error('ส่งงานเข้าคิวไม่สำเร็จ');
    }
    toast('ส่งงานเข้าคิวแล้ว', 'success', 'ปิดหน้าเว็บได้ ระบบจะทำงานต่อบนเซิร์ฟเวอร์');
    state.files = [];
    renderFiles();
    byId('file-input').value = '';
    byId('upload-progress').classList.add('hidden');
    byId('job-name').value = makeJobName();
    setView('queue');
    await loadJobs({ silent: true });
  } catch (error) {
    state.files = filesSnapshot;
    renderFiles();
    toast('ส่งงานไม่สำเร็จ', 'error', error.message || 'โปรดลองอีกครั้ง');
  } finally {
    state.submitting = false;
    button.innerHTML = `${icon('i-play')}<span>ส่งงานเข้าคิว</span>`;
    updateSummary();
  }
}

function normalizeStatus(status) {
  const value = String(status || '').toLowerCase();
  return value === 'failed' ? 'error' : value;
}

function statusLabel(status) {
  return ({ created: 'เตรียมงาน', queued: 'รอคิว', running: 'กำลังทำ', done: 'เสร็จแล้ว', error: 'ผิดพลาด', failed: 'ผิดพลาด', cancelled: 'ยกเลิกแล้ว' })[String(status || '').toLowerCase()] || status || '—';
}

function inferWorkflow(job) {
  const settings = job?.settings || {};
  if (job?.mode === 'silence_trim_only') return 'silence';
  if (job?.mode === 'autosu_only' && settings.trim_silence) return 'combined';
  return 'subtitle';
}

function jobProgress(job) {
  const status = normalizeStatus(job.status);
  if (status === 'done') return 100;
  if (status === 'queued' || status === 'created') return Math.max(4, Number(job.progress) || 4);
  if (status === 'running') return Math.max(12, Math.min(96, Number(job.progress) || 50));
  return Number(job.progress) || 0;
}

function jobStage(job) {
  // Prefer detailed stage from backend (Thai, real-time) when running
  const status = normalizeStatus(job.status);
  if (status === 'running' || status === 'processing') {
    const live = (job.stage || '').toString().trim();
    if (live && live.length >= 2) {
      // Append current/total counter if present
      const c = Number(job.current || 0);
      const t = Number(job.total || 0);
      if (c > 0 && t > 0 && t > c) {
        return live + ' (' + c + '/' + t + ')';
      }
      return live;
    }
  }
  const workflow = inferWorkflow(job);
  if (status === 'queued' || status === 'created') return 'กำลังรอทรัพยากรประมวลผล';
  if (status === 'done') return 'พร้อมดาวน์โหลด';
  if (status === 'error') return 'ประมวลผลไม่สำเร็จ';
  if (status === 'cancelled') return 'ยกเลิกงานแล้ว';
  if (workflow === 'silence') return 'กำลังตรวจและตัดช่วงเงียบ';
  if (workflow === 'combined') return 'กำลังตัดช่วงเงียบและทำซับ';
  return 'กำลังถอดเสียงและฝังซับ';
}

function renderJobs() {
  const list = byId('jobs-list');
  let jobs = [...state.jobs];
  if (state.jobFilter === 'active') jobs = jobs.filter((job) => ['created', 'queued', 'running'].includes(normalizeStatus(job.status)));
  if (state.jobFilter === 'done') jobs = jobs.filter((job) => normalizeStatus(job.status) === 'done');
  if (!jobs.length) {
    // Enhanced empty state when truly no jobs exist
    if (state.jobs.length === 0 && state.jobFilter === 'all') {
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
      if (sampleBtn) sampleBtn.addEventListener('click', () => loadSampleVideo());
      return;
    }
    list.innerHTML = `<div class="empty-state"><span class="empty-icon">${icon('i-queue')}</span><strong>ยังไม่มีงานในหมวดนี้</strong><p>เริ่มจากหน้า “สร้างงานใหม่” แล้วงานจะประมวลผลต่อในพื้นหลัง</p><button class="button secondary" data-view-jump="workspace">สร้างงานใหม่</button></div>`;
  } else {
    list.innerHTML = jobs.map((job) => {
      const workflow = inferWorkflow(job);
      const meta = MODE_META[workflow];
      const progress = jobProgress(job);
      const status = normalizeStatus(job.status);
      return `<article class="job-row" data-open-job="${escapeHtml(job.id)}">
        <span class="job-mode-icon ${workflow === 'silence' ? 'silence' : ''}">${icon(meta.icon)}</span>
        <span class="job-primary"><strong>${escapeHtml(job.name || job.id)}</strong><small>${escapeHtml(meta.title)} · ${formatDate(job.created_at)}</small></span>
        <span class="job-progress-wrap"><span class="job-progress-copy"><i>${escapeHtml(jobStage(job))}</i><b>${progress}%</b></span><span class="job-progress-track"><span style="width:${progress}%"></span></span></span>
        <span class="job-time"><strong>${job.result?.elapsed || job.result?.elapsed_str || '—'}</strong><span>เวลาประมวลผล</span></span>
        <span class="status-chip ${status}">${statusLabel(status)}</span>
        <span class="row-actions"><button class="row-menu-button" data-open-job="${escapeHtml(job.id)}" aria-label="รายละเอียด">${icon('i-more')}</button></span>
      </article>`;
    }).join('');
  }
  const active = state.jobs.filter((job) => ['created', 'queued', 'running'].includes(normalizeStatus(job.status))).length;
  byId('queue-count').textContent = String(active);
  byId('queue-count').classList.toggle('hidden', active === 0);
  byId('mobile-queue-dot').classList.toggle('hidden', active === 0);
  byId('stat-active').textContent = String(active);
  const today = new Date().toISOString().slice(0, 10);
  const doneToday = state.jobs.filter((job) => normalizeStatus(job.status) === 'done' && String(job.updated_at || job.finished_at || '').slice(0, 10) === today).length;
  byId('stat-done').textContent = String(doneToday);
  const removed = state.jobs.reduce((sum, job) => sum + Number(job.result?.runtime_metrics?.removed_duration_sec || job.result?.runtime_metrics?.removed_duration || 0), 0);
  byId('stat-removed').textContent = removed > 0 ? formatDuration(removed) : '0 นาที';
  byId('jobs-updated').textContent = `อัปเดตล่าสุด ${new Date().toLocaleTimeString('th-TH', { hour: '2-digit', minute: '2-digit' })}`;
}

function checkJobTransitions(jobs) {
  for (const job of jobs) {
    const status = normalizeStatus(job.status);
    const previous = state.seenStatuses.get(job.id);
    if (previous && previous !== status && ['done', 'error'].includes(status)) notifyJob(job);
    state.seenStatuses.set(job.id, status);
  }
}

async function loadJobs({ silent = false } = {}) {
  try {
    if (!DEMO_MODE) {
      const payload = await api('/api/jobs?limit=100');
      const jobs = Array.isArray(payload?.jobs) ? payload.jobs : [];
      checkJobTransitions(jobs);
      state.jobs = jobs;
    }
    renderJobs();
  } catch (error) {
    if (!silent) toast('โหลดคิวงานไม่สำเร็จ', 'error', error.message);
    if (error.status === 401) showAuth();
  }
}

function renderHistory() {
  const list = byId('history-list');
  const query = state.historyQuery.trim().toLowerCase();
  let items = state.history.filter((item) => {
    if (state.historyMode !== 'all' && item.mode !== state.historyMode) return false;
    return !query || String(item.name || item.id).toLowerCase().includes(query);
  });
  if (!items.length) {
    list.innerHTML = `<div class="empty-state enhanced">
      <span class="empty-icon-bg"><svg><use href="#i-history"/></svg></span>
      <strong>ยังไม่มีไฟล์ในประวัติ</strong>
      <p>ลองสร้างงานแรกของคุณได้เลย — ใช้วิดีโอตัวอย่าง 15 วินาที หรือลากไฟล์ของคุณเองมาวาง</p>
      <div class="empty-state-actions">
        <button class="button primary" id="empty-try-sample">${icon('i-play')}ทดลองใช้วิดีโอตัวอย่าง</button>
        <button class="button ghost" data-view-jump="workspace">${icon('i-upload')}อัปโหลดวิดีโอของคุณ</button>
      </div>
      <div class="empty-state-hint">💡 เริ่มงานแรกใช้เวลาแค่ 1 นาที</div>
    </div>`;
    // Wire up sample button in empty state
    const emptySample = byId('empty-try-sample');
    if (emptySample) emptySample.addEventListener('click', () => loadSampleVideo());
    return;
  }
  list.innerHTML = items.map((item) => {
    const workflow = inferWorkflow(item);
    const meta = MODE_META[workflow];
    return `<article class="history-row" data-open-history="${escapeHtml(item.id)}">
      <span class="job-mode-icon ${workflow === 'silence' ? 'silence' : ''}">${icon(meta.icon)}</span>
      <span class="history-info"><strong>${escapeHtml(item.name || item.id)}</strong><small>${escapeHtml(meta.title)} · ${item.total_outputs || item.outcome?.output_count || 0} ไฟล์</small></span>
      <span class="history-cell"><small>เสร็จเมื่อ</small><strong>${formatDate(item.finished_at || item.created_at)}</strong></span>
      <span class="history-cell"><small>เวลาทำงาน</small><strong>${escapeHtml(item.duration_str || item.outcome?.elapsed_str || '—')}</strong></span>
      <span class="history-cell"><small>ขนาดผลลัพธ์</small><strong>${formatBytes(item.total_output_bytes || 0)}</strong></span>
      <span class="history-actions"><button class="mini-button secondary" data-open-history="${escapeHtml(item.id)}">รายละเอียด</button><button class="mini-button" data-download-job="${escapeHtml(item.id)}">${icon('i-download')}ดาวน์โหลด</button></span>
    </article>`;
  }).join('');
}

async function loadHistory({ silent = false } = {}) {
  try {
    if (!DEMO_MODE) {
      const payload = await api('/api/history?limit=200');
      state.history = Array.isArray(payload?.items) ? payload.items : [];
    }
    renderHistory();
  } catch (error) {
    if (!silent) toast('โหลดประวัติไม่สำเร็จ', 'error', error.message);
  }
}

function withToken(url) {
  if (DEMO_MODE) return url;
  const target = new URL(url, location.origin);
  if (state.auth?.access_token) target.searchParams.set('token', state.auth.access_token);
  return `${target.pathname}${target.search}`;
}

function downloadJob(jobId) {
  if (DEMO_MODE) {
    toast('โหมดตัวอย่างไม่สร้างไฟล์จริง', 'info', 'เมื่อต่อ backend ปุ่มนี้จะดาวน์โหลด ZIP ผลลัพธ์');
    return;
  }
  window.open(withToken(`/api/jobs/${encodeURIComponent(jobId)}/download/output/direct`), '_blank', 'noopener');
}

function downloadFile(jobId, path) {
  if (DEMO_MODE) {
    toast('โหมดตัวอย่างไม่สร้างไฟล์จริง', 'info');
    return;
  }
  window.open(withToken(`/api/history/${encodeURIComponent(jobId)}/files/${String(path).split('/').map(encodeURIComponent).join('/')}`), '_blank', 'noopener');
}

function drawerOpen(title, html) {
  byId('drawer-title').textContent = title || 'รายละเอียดงาน';
  byId('drawer-body').innerHTML = html;
  byId('drawer-backdrop').classList.remove('hidden');
  byId('detail-drawer').classList.add('open');
  byId('detail-drawer').setAttribute('aria-hidden', 'false');
}

function drawerClose() {
  byId('drawer-backdrop').classList.add('hidden');
  byId('detail-drawer').classList.remove('open');
  byId('detail-drawer').setAttribute('aria-hidden', 'true');
}

function detailMetric(label, value) {
  return `<div><strong>${escapeHtml(value)}</strong><small>${escapeHtml(label)}</small></div>`;
}

function renderActiveJobDrawer(job) {
  const status = normalizeStatus(job.status);
  const progress = jobProgress(job);
  const workflow = inferWorkflow(job);
  const logs = Array.isArray(job.logs) ? job.logs : [];
  const result = job.result || {};
  return `<div class="detail-status-card">
    <div class="detail-status-top"><span class="status-chip ${status}">${statusLabel(status)}</span><strong>${progress}%</strong></div>
    <div class="job-progress-track" style="margin-top:12px"><span style="width:${progress}%"></span></div>
    <p>${escapeHtml(jobStage(job))}</p>
    <div class="detail-metrics">${detailMetric('ประเภทงาน', MODE_META[workflow].title)}${detailMetric('ผลลัพธ์', `${result.output_count || 0} ไฟล์`)}${detailMetric('เริ่มเมื่อ', formatDate(job.created_at))}</div>
  </div>
  ${logs.length ? `<section class="detail-section"><h3>บันทึกสถานะ</h3><div class="log-list">${logs.slice(-60).map((line) => `<div>${escapeHtml(line)}</div>`).join('')}</div></section>` : ''}
  <div class="detail-actions">
    ${['created', 'queued', 'running'].includes(status) ? `<button class="button danger ghost" data-cancel-job="${escapeHtml(job.id)}">ยกเลิกงาน</button>` : ''}
    ${status === 'done' ? `<button class="button primary" data-download-job="${escapeHtml(job.id)}">${icon('i-download')}ดาวน์โหลดทั้งหมด</button>` : ''}
  </div>`;
}

async function openJobDetail(jobId) {
  const local = state.jobs.find((item) => String(item.id) === String(jobId));
  drawerOpen(local?.name || jobId, `<div class="empty-state"><span class="empty-icon">${icon('i-refresh')}</span><strong>กำลังโหลดรายละเอียด…</strong></div>`);
  try {
    const job = DEMO_MODE ? local : await api(`/api/jobs/${encodeURIComponent(jobId)}`);
    if (!job) throw new Error('ไม่พบงาน');
    byId('drawer-title').textContent = job.name || job.id;
    byId('drawer-body').innerHTML = renderActiveJobDrawer(job);
  } catch (error) {
    byId('drawer-body').innerHTML = `<div class="empty-state"><span class="empty-icon">${icon('i-close')}</span><strong>โหลดรายละเอียดไม่สำเร็จ</strong><p>${escapeHtml(error.message)}</p></div>`;
  }
}

function renderHistoryDrawer(detail) {
  const job = detail.job || detail;
  const files = Array.isArray(detail.output_files) ? detail.output_files : (job.outcome?.outputs || []).map((path) => ({ name: `output/${String(path).split('/').pop()}`, size: 0 }));
  const metrics = job.outcome?.runtime_metrics || {};
  const removed = Number(metrics.removed_duration_sec || 0);
  return `<div class="detail-status-card">
    <div class="detail-status-top"><span class="status-chip done">เสร็จแล้ว</span><strong>${escapeHtml(job.duration_str || job.outcome?.elapsed_str || '—')}</strong></div>
    <div class="detail-metrics">${detailMetric('ผลลัพธ์', `${job.total_outputs || files.length} ไฟล์`)}${detailMetric('ขนาดรวม', formatBytes(job.total_output_bytes || 0))}${detailMetric('ตัดช่วงเงียบ', removed ? formatDuration(removed) : '—')}</div>
  </div>
  <section class="detail-section"><h3>ไฟล์ผลลัพธ์</h3><div class="output-list">
    ${files.length ? files.map((file) => {
      const path = file.name || file.path || file.relative_path || '';
      const name = String(path).split('/').pop();
      return `<div class="output-file"><span>${icon('i-file')}</span><div><strong title="${escapeHtml(name)}">${escapeHtml(name)}</strong><small>${formatBytes(file.size || 0)}</small></div><button data-download-file="${escapeHtml(path)}" data-job-id="${escapeHtml(job.id)}" aria-label="ดาวน์โหลด">${icon('i-download')}</button></div>`;
    }).join('') : '<div class="empty-state"><p>ไม่พบรายการไฟล์ แต่ยังลองดาวน์โหลด ZIP ทั้งหมดได้</p></div>'}
  </div></section>
  <div class="detail-actions"><button class="button primary large" data-download-job="${escapeHtml(job.id)}">${icon('i-download')}ดาวน์โหลดทั้งหมดเป็น ZIP</button></div>`;
}

async function openHistoryDetail(jobId) {
  const local = state.history.find((item) => String(item.id) === String(jobId));
  drawerOpen(local?.name || jobId, `<div class="empty-state"><span class="empty-icon">${icon('i-refresh')}</span><strong>กำลังโหลดไฟล์…</strong></div>`);
  try {
    let detail;
    if (DEMO_MODE) {
      const outputs = local?.outcome?.outputs || [];
      detail = { job: local, output_files: outputs.map((path, index) => ({ name: `output/${String(path).split('/').pop()}`, size: 8_800_000 + index * 1_200_000 })) };
    } else {
      detail = await api(`/api/history/${encodeURIComponent(jobId)}`);
    }
    byId('drawer-title').textContent = detail.job?.name || local?.name || jobId;
    byId('drawer-body').innerHTML = renderHistoryDrawer(detail);
  } catch (error) {
    byId('drawer-body').innerHTML = `<div class="empty-state"><span class="empty-icon">${icon('i-close')}</span><strong>โหลดไฟล์ไม่สำเร็จ</strong><p>${escapeHtml(error.message)}</p></div>`;
  }
}

async function cancelJob(jobId) {
  try {
    if (DEMO_MODE) {
      const job = state.jobs.find((item) => String(item.id) === String(jobId));
      if (job) { job.status = 'cancelled'; job.progress = 0; }
    } else {
      await api(`/api/jobs/${encodeURIComponent(jobId)}/cancel`, { method: 'POST' });
    }
    drawerClose();
    await loadJobs({ silent: true });
    toast('ยกเลิกงานแล้ว', 'info');
  } catch (error) {
    toast('ยกเลิกงานไม่สำเร็จ', 'error', error.message);
  }
}

async function loadTrimDefaults({ silent = false } = {}) {
  try {
    if (!DEMO_MODE) {
      const payload = await api('/api/subtitle/trim-settings');
      if (payload?.settings) state.trimDefaults = { ...state.trimDefaults, ...payload.settings };
    }
    const value = state.trimDefaults;
    byId('default-threshold').value = value.trim_silence_threshold_db ?? -40;
    byId('default-min-silence').value = value.trim_silence_min_silence_sec ?? 0.5;
    byId('default-margin').value = value.trim_silence_margin_sec ?? 0;
    byId('default-min-keep').value = value.trim_silence_min_keep_sec ?? 0.08;
    if (!state.submitting) {
      byId('trim-threshold').value = value.trim_silence_threshold_db ?? -40;
      byId('trim-min-silence').value = value.trim_silence_min_silence_sec ?? 0.5;
      byId('trim-margin').value = value.trim_silence_margin_sec ?? 0;
      setPreset(value.trim_silence_preset || 'keep_quiet', false);
    }
  } catch (error) {
    if (!silent) toast('โหลดค่าเริ่มต้นไม่สำเร็จ', 'error', error.message);
  }
}

function memberStatusLabel(status) {
  return ({ approved: 'ใช้งานได้', pending: 'รออนุมัติ', disabled: 'ระงับ', rejected: 'ไม่อนุมัติ' })[status] || status || '—';
}

function renderMembers() {
  const root = byId('member-list');
  if (!isAdminUser()) { root.innerHTML = ''; return; }
  const query = state.memberQuery.trim().toLowerCase();
  const rows = state.members.filter((member) => {
    const statusOk = state.memberStatus === 'all' || member.account_status === state.memberStatus;
    const searchOk = !query || `${member.display_name || ''} ${member.email || ''}`.toLowerCase().includes(query);
    return statusOk && searchOk;
  });
  byId('member-count').textContent = `${rows.length} บัญชี`;
  if (!rows.length) {
    root.innerHTML = `<div class="empty-state compact">${icon('i-user')}<strong>ไม่พบบัญชีในเงื่อนไขนี้</strong></div>`;
    return;
  }
  root.innerHTML = rows.map((member) => {
    const current = Number(member.id) === Number(state.user?.id);
    const status = String(member.account_status || 'approved');
    let actions = '';
    if (current) actions = '<span class="member-current">บัญชีปัจจุบัน</span>';
    else if (status === 'pending') actions = `<button class="member-action approve" data-member-action="approve" data-member-id="${member.id}">อนุมัติ</button><button class="member-action reject" data-member-action="reject" data-member-id="${member.id}">ไม่อนุมัติ</button>`;
    else if (status === 'approved') actions = `<button class="member-action disable" data-member-action="disable" data-member-id="${member.id}">ระงับ</button>`;
    else actions = `<button class="member-action approve" data-member-action="restore" data-member-id="${member.id}">คืนสิทธิ์</button>`;
    const name = member.display_name || member.email || 'สมาชิก';
    const initial = Array.from(String(name).trim())[0]?.toUpperCase() || 'S';
    return `<article class="member-row">
      <span class="member-avatar">${escapeHtml(initial)}</span>
      <div class="member-info"><strong>${escapeHtml(name)}</strong><small>${escapeHtml(member.email || '—')} · ${escapeHtml(member.role || 'user')}</small></div>
      <span class="member-status ${escapeHtml(status)}">${escapeHtml(memberStatusLabel(status))}</span>
      <div class="member-actions">${actions}</div>
    </article>`;
  }).join('');
}

async function loadMembers({ silent = false } = {}) {
  if (!isAdminUser()) return;
  if (DEMO_MODE) { renderMembers(); return; }
  try {
    const params = new URLSearchParams({ status: 'all', limit: '500' });
    const payload = await api(`/api/members?${params}`);
    state.members = Array.isArray(payload.members) ? payload.members : [];
    renderMembers();
  } catch (error) {
    if (!silent) toast('โหลดสมาชิกไม่สำเร็จ', 'error', error.message);
  }
}

async function updateMemberStatus(memberId, action) {
  if (!isAdminUser()) return;
  try {
    if (DEMO_MODE) {
      const member = state.members.find((item) => Number(item.id) === Number(memberId));
      if (member) member.account_status = ({ approve: 'approved', restore: 'approved', reject: 'rejected', disable: 'disabled' })[action] || member.account_status;
    } else {
      await api(`/api/members/${encodeURIComponent(memberId)}`, { method: 'PATCH', json: { action } });
      await loadMembers({ silent: true });
    }
    renderMembers();
    toast(action === 'approve' || action === 'restore' ? 'อัปเดตสิทธิ์สมาชิกแล้ว' : 'อัปเดตสถานะสมาชิกแล้ว');
  } catch (error) {
    const messages = { cannot_suspend_last_owner: 'ไม่สามารถระงับ Owner คนสุดท้าย', cannot_suspend_current_account: 'ไม่สามารถระงับบัญชีที่กำลังใช้งาน' };
    toast(messages[error.message] || 'แก้ไขสมาชิกไม่สำเร็จ', 'error', error.message);
  }
}

async function saveTrimDefaults() {
  const settings = {
    trim_silence: true,
    trim_silence_threshold_db: Number(byId('default-threshold').value || -40),
    trim_silence_min_silence_sec: Number(byId('default-min-silence').value || 0.5),
    trim_silence_margin_sec: Number(byId('default-margin').value || 0),
    trim_silence_min_keep_sec: Number(byId('default-min-keep').value || 0.08),
    trim_silence_min_output_sec: Number(state.trimDefaults.trim_silence_min_output_sec || 1),
    trim_silence_preset: 'custom',
  };
  try {
    if (!DEMO_MODE) {
      const payload = await api('/api/subtitle/trim-settings', { method: 'PUT', json: { settings } });
      state.trimDefaults = { ...state.trimDefaults, ...(payload.settings || settings) };
    } else {
      state.trimDefaults = { ...state.trimDefaults, ...settings };
    }
    toast('บันทึกค่าเริ่มต้นแล้ว');
  } catch (error) {
    toast('บันทึกไม่สำเร็จ', 'error', error.message);
  }
}

function notifyJob(job) {
  const status = normalizeStatus(job.status);
  const done = status === 'done';
  const title = done ? 'งาน SubCut เสร็จแล้ว' : 'งาน SubCut มีปัญหา';
  const body = done ? `${job.name || 'งานของคุณ'} พร้อมดาวน์โหลด` : `${job.name || 'งานของคุณ'} ประมวลผลไม่สำเร็จ`;
  toast(title, done ? 'success' : 'error', body);
  if (state.preferences.browserNotifications && 'Notification' in window && Notification.permission === 'granted') {
    new Notification(title, { body, icon: '/static/favicon.svg' });
  }
  if (state.preferences.soundNotifications) playNotificationSound(done);
}

function playNotificationSound(success = true) {
  try {
    const context = new (window.AudioContext || window.webkitAudioContext)();
    const oscillator = context.createOscillator();
    const gain = context.createGain();
    oscillator.frequency.value = success ? 720 : 380;
    oscillator.type = 'sine';
    gain.gain.setValueAtTime(0.0001, context.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.08, context.currentTime + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, context.currentTime + 0.28);
    oscillator.connect(gain); gain.connect(context.destination);
    oscillator.start(); oscillator.stop(context.currentTime + 0.3);
  } catch (_) { /* no-op */ }
}

async function toggleBrowserNotifications(checked) {
  if (!checked) {
    state.preferences.browserNotifications = false;
    savePreferences();
    return;
  }
  if (!('Notification' in window)) {
    byId('browser-notifications').checked = false;
    toast('เบราว์เซอร์นี้ไม่รองรับการแจ้งเตือน', 'error');
    return;
  }
  const permission = await Notification.requestPermission();
  state.preferences.browserNotifications = permission === 'granted';
  byId('browser-notifications').checked = state.preferences.browserNotifications;
  savePreferences();
  if (permission !== 'granted') toast('ยังไม่ได้อนุญาตการแจ้งเตือน', 'info');
}

function seedDemoData() {
  const now = Date.now();
  state.jobs = [
    {
      id: 'demo-running', name: 'คอร์ส AI EP04', mode: 'autosu_only', status: 'running', progress: 64,
      created_at: new Date(now - 6 * 60_000).toISOString(), updated_at: new Date(now - 20_000).toISOString(),
      settings: { workflow: 'combined', trim_silence: true }, result: { output_count: 0, runtime_metrics: {} },
      logs: ['รับไฟล์ครบ 3 ไฟล์', 'ตรวจช่วงเงียบสำเร็จ', 'กำลังถอดเสียงไฟล์ที่ 2/3'],
    },
    {
      id: 'demo-queued', name: 'รีวิวสินค้า_ชุดใหม่', mode: 'silence_trim_only', status: 'queued', progress: 7,
      created_at: new Date(now - 2 * 60_000).toISOString(), settings: { workflow: 'silence', trim_silence: true }, result: {},
    },
    {
      id: 'demo-done', name: 'คลิปสอน TC03', mode: 'autosu_only', status: 'done', progress: 100,
      created_at: new Date(now - 2 * 3600_000).toISOString(), updated_at: new Date(now - 90 * 60_000).toISOString(),
      settings: { workflow: 'subtitle', trim_silence: false },
      result: { output_count: 2, elapsed: '02:18', runtime_metrics: {} },
    },
  ];
  state.members = [
    { id: 88, display_name: 'น้ำน่าน Demo', email: 'demo@subcut.local', role: 'owner', plan: 'pro', account_status: 'approved' },
    { id: 91, display_name: 'วรัญญา', email: 'waranya@example.com', role: 'user', plan: 'free', account_status: 'pending' },
    { id: 90, display_name: 'ทีมตัดต่อ', email: 'editor@example.com', role: 'user', plan: 'pro', account_status: 'approved' },
  ];
  state.history = [
    {
      id: 'demo-history-1', name: 'บทเรียน VDO Workflow', mode: 'autosu_only', status: 'done',
      created_at: new Date(now - 86400_000).toISOString(), finished_at: new Date(now - 23 * 3600_000).toISOString(),
      duration_str: '03:42', total_outputs: 4, total_input_bytes: 624_000_000, total_output_bytes: 498_000_000,
      outcome: { output_count: 4, elapsed_str: '03:42', outputs: ['/demo/lesson_01_ready.mp4', '/demo/lesson_02_ready.mp4', '/demo/lesson_03_ready.mp4', '/demo/lesson_04_ready.mp4'], runtime_metrics: { removed_duration_sec: 52.4 } },
      settings: { workflow: 'combined', trim_silence: true },
    },
    {
      id: 'demo-history-2', name: 'คลิปโปรโมชันแนวตั้ง', mode: 'silence_trim_only', status: 'done',
      created_at: new Date(now - 2 * 86400_000).toISOString(), finished_at: new Date(now - 2 * 86400_000 + 90_000).toISOString(),
      duration_str: '01:30', total_outputs: 2, total_input_bytes: 184_000_000, total_output_bytes: 151_000_000,
      outcome: { output_count: 2, outputs: ['/demo/promo_01_cut.mp4', '/demo/promo_02_cut.mp4'], runtime_metrics: { removed_duration_sec: 38.7 } },
      settings: { workflow: 'silence', trim_silence: true },
    },
  ];
  if (DEMO_SEED) {
    state.files = [
      { name: 'EP05_กล้องหลัก.mp4', size: 248_400_000, lastModified: now, type: 'video/mp4' },
      { name: 'EP05_กล้องเสริม.mov', size: 96_800_000, lastModified: now - 1000, type: 'video/quicktime' },
    ];
  }
}

async function afterAuthenticated() {
  updateAccountUi();
  await Promise.all([
    loadJobs({ silent: true }),
    loadHistory({ silent: true }),
    loadTrimDefaults({ silent: true }),
    ...(isAdminUser() ? [loadMembers({ silent: true })] : []),
  ]);
  startPolling();
}

function startPolling() {
  if (state.pollingTimer) clearInterval(state.pollingTimer);
  state.pollingTimer = window.setInterval(() => {
    if (!state.user || document.hidden) return;
    loadJobs({ silent: true });
    if (state.view === 'history') loadHistory({ silent: true });
  }, 5000);
}

function bindEvents() {
  all('.nav-item').forEach((button) => button.addEventListener('click', () => setView(button.dataset.view)));
  document.addEventListener('click', (event) => {
    const jump = event.target.closest('[data-view-jump]');
    if (jump) { event.preventDefault(); setView(jump.dataset.viewJump); }
  });

  byId('menu-button').addEventListener('click', () => {
    byId('sidebar').classList.add('open'); byId('sidebar-backdrop').classList.remove('hidden');
  });
  byId('sidebar-backdrop').addEventListener('click', () => {
    byId('sidebar').classList.remove('open'); byId('sidebar-backdrop').classList.add('hidden');
  });

  all('.mode-card').forEach((card) => card.addEventListener('click', () => chooseMode(card.dataset.mode)));
  const fileInput = byId('file-input');
  const dropZone = byId('drop-zone');
  dropZone.addEventListener('click', () => fileInput.click());
  fileInput.addEventListener('change', () => addFiles(fileInput.files));
  ['dragenter', 'dragover'].forEach((name) => dropZone.addEventListener(name, (event) => { event.preventDefault(); dropZone.classList.add('dragging'); }));
  ['dragleave', 'drop'].forEach((name) => dropZone.addEventListener(name, (event) => { event.preventDefault(); dropZone.classList.remove('dragging'); }));
  dropZone.addEventListener('drop', (event) => addFiles(event.dataTransfer?.files));
  byId('file-list').addEventListener('click', (event) => {
    const button = event.target.closest('[data-remove-file]');
    if (!button) return;
    state.files.splice(Number(button.dataset.removeFile), 1);
    renderFiles(); updateSummary(); updateStepIndicator();
  });

  // ✨ Quickstart tip dismiss
  const quickstartTip = byId('quickstart-tip');
  const quickstartDismiss = byId('quickstart-dismiss');
  if (quickstartDismiss && quickstartTip) {
    if (localStorage.getItem('subcut.quickstart.dismissed') === '1') {
      quickstartTip.classList.add('hidden');
    } else {
      // Show only if user has no files yet
      if (state.files.length === 0) quickstartTip.classList.remove('hidden');
    }
    quickstartDismiss.addEventListener('click', () => {
      quickstartTip.classList.add('hidden');
      try { localStorage.setItem('subcut.quickstart.dismissed', '1'); } catch (_) {}
    });
  }

  // 🎬 Try sample button — generate a 15s demo video and feed it to the upload flow
  const trySampleButton = byId('try-sample-button');
  if (trySampleButton) {
    trySampleButton.addEventListener('click', async (event) => {
      event.stopPropagation();
      await loadSampleVideo();
    });
  }

  byId('subtitle-position').addEventListener('input', (event) => { byId('position-output').value = `${event.target.value}%`; });
  byId('subtitle-font-size').addEventListener('input', (event) => { byId('font-output').value = `${event.target.value} px`; });
  all('#silence-presets button').forEach((button) => button.addEventListener('click', () => setPreset(button.dataset.preset)));
  ['trim-threshold', 'trim-min-silence', 'trim-margin'].forEach((id) => byId(id).addEventListener('input', () => setPreset('custom', false)));
  byId('reset-settings').addEventListener('click', resetWorkspaceSettings);
  byId('create-job-button').addEventListener('click', submitJob);

  // Initial step indicator update
  updateStepIndicator();

  byId('refresh-jobs').addEventListener('click', () => loadJobs());
  byId('refresh-history').addEventListener('click', () => loadHistory());
  all('#job-filter button').forEach((button) => button.addEventListener('click', () => {
    state.jobFilter = button.dataset.filter;
    all('#job-filter button').forEach((item) => item.classList.toggle('active', item === button));
    renderJobs();
  }));
  byId('history-search').addEventListener('input', (event) => { state.historyQuery = event.target.value; renderHistory(); });
  byId('history-mode-filter').addEventListener('change', (event) => { state.historyMode = event.target.value; renderHistory(); });

  byId('jobs-list').addEventListener('click', (event) => {
    const target = event.target.closest('[data-open-job]');
    if (target) openJobDetail(target.dataset.openJob);
  });
  byId('history-list').addEventListener('click', (event) => {
    const download = event.target.closest('[data-download-job]');
    if (download) { event.stopPropagation(); downloadJob(download.dataset.downloadJob); return; }
    const target = event.target.closest('[data-open-history]');
    if (target) openHistoryDetail(target.dataset.openHistory);
  });
  byId('drawer-body').addEventListener('click', (event) => {
    const downloadJobButton = event.target.closest('[data-download-job]');
    if (downloadJobButton) { downloadJob(downloadJobButton.dataset.downloadJob); return; }
    const downloadFileButton = event.target.closest('[data-download-file]');
    if (downloadFileButton) { downloadFile(downloadFileButton.dataset.jobId, downloadFileButton.dataset.downloadFile); return; }
    const cancelButton = event.target.closest('[data-cancel-job]');
    if (cancelButton) cancelJob(cancelButton.dataset.cancelJob);
  });
  byId('drawer-close').addEventListener('click', drawerClose);
  byId('drawer-backdrop').addEventListener('click', drawerClose);

  byId('save-defaults').addEventListener('click', saveTrimDefaults);
  byId('member-search').addEventListener('input', (event) => { state.memberQuery = event.target.value; renderMembers(); });
  byId('member-status-filter').addEventListener('change', (event) => { state.memberStatus = event.target.value; renderMembers(); });
  byId('refresh-members').addEventListener('click', () => loadMembers());
  byId('member-list').addEventListener('click', (event) => {
    const button = event.target.closest('[data-member-action]');
    if (button) updateMemberStatus(button.dataset.memberId, button.dataset.memberAction);
  });
  byId('browser-notifications').addEventListener('change', (event) => toggleBrowserNotifications(event.target.checked));
  byId('sound-notifications').addEventListener('change', (event) => { state.preferences.soundNotifications = event.target.checked; savePreferences(); });
  byId('logout-button').addEventListener('click', handleAccountButton);
  byId('profile-register').addEventListener('click', () => showAuth('register'));
  byId('profile-login').addEventListener('click', () => showAuth('login'));
  byId('profile-logout').addEventListener('click', logout);
  byId('guest-register').addEventListener('click', () => showAuth('register'));
  byId('guest-login').addEventListener('click', () => showAuth('login'));
  byId('auth-close').addEventListener('click', hideAuth);
  byId('continue-guest').addEventListener('click', hideAuth);

  byId('profile-button').addEventListener('click', (event) => { event.stopPropagation(); byId('profile-menu').classList.toggle('hidden'); });
  document.addEventListener('click', (event) => {
    if (!event.target.closest('#profile-menu') && !event.target.closest('#profile-button')) byId('profile-menu').classList.add('hidden');
  });

  all('[data-auth-tab]').forEach((button) => button.addEventListener('click', () => switchAuthTab(button.dataset.authTab)));
  byId('login-form').addEventListener('submit', handleLogin);
  byId('register-form').addEventListener('submit', handleRegister);
  all('.password-toggle').forEach((button) => button.addEventListener('click', () => {
    const input = button.parentElement.querySelector('input');
    const show = input.type === 'password';
    input.type = show ? 'text' : 'password';
    button.textContent = show ? 'ซ่อน' : 'แสดง';
  }));

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      drawerClose();
      byId('profile-menu').classList.add('hidden');
      byId('sidebar').classList.remove('open');
      byId('sidebar-backdrop').classList.add('hidden');
      if (state.user) hideAuth();
    }
  });
}

async function boot() {
  bindEvents();
  getPreferences();
  byId('browser-notifications').checked = state.preferences.browserNotifications;
  byId('sound-notifications').checked = state.preferences.soundNotifications;
  byId('job-name').value = makeJobName();
  chooseMode('combined');
  restoreAuth();
  if (DEMO_MODE) seedDemoData();
  renderFiles(); updateSummary(); renderJobs(); renderHistory();

  const sso = await exchangeSsoTicket();
  const valid = sso || await validateSession() || await ensureBrowserSession();
  if (valid) {
    await afterAuthenticated();
  } else {
    showAuth('login');
    authStatus('เชื่อมต่อบัญชี Guest ไม่สำเร็จ กรุณาลองเข้าสู่ระบบ', 'error');
  }
}

document.addEventListener('DOMContentLoaded', boot);