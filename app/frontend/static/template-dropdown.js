/* ============================================================
   Subtitle Template Dropdown — Custom Component
   Replaces the native <select id="subtitle-template"> with a
   richer dropdown that shows a visual preview of each template.

   - Reads initial value from <select id="subtitle-template">
   - Writes back to the native select on change
   - Closes on outside click / Escape
   - Keyboard nav: ↑↓ + Enter + Escape
   - No dependencies on app.js
   ============================================================ */

(function () {
  const TEMPLATES = [
    {
      id: 'default',
      name: 'Clean — อ่านง่าย',
      desc: 'ขาว + ขอบบาง เน้นอ่านง่าย',
      previewHTML: '<div class="preview preview--default">ตัวอย่าง</div>',
    },
    {
      id: 'yellow_pop',
      name: 'Yellow Pop',
      desc: 'เหลืองสด ตัวหนา เด่นชัด',
      previewHTML: '<div class="preview preview--yellow_pop">ว้าว!</div>',
    },
    {
      id: 'box',
      name: 'Box Caption',
      desc: 'กล่องทึบ ขอบโค้ง อ่านง่าย',
      previewHTML: '<div class="preview preview--box"><span>กล่อง</span></div>',
    },
    {
      id: 'karaoke_highlight',
      name: 'Karaoke Highlight',
      desc: 'ขาว→ฟ้า มี pop effect เหมาะ TikTok',
      previewHTML: '<div class="preview preview--karaoke_highlight"><span class="sa">กา</span><span class="mp">รอ</span><span class="le">เก</span></div>',
    },
    {
      id: 'luxury_gold',
      name: 'Luxury Gold',
      desc: 'ทองหรู มีเงา สไตล์พรีเมียม',
      previewHTML: '<div class="preview preview--luxury_gold">Luxury</div>',
    },
    {
      id: 'comic_burst',
      name: 'Comic Burst',
      desc: 'การ์ตูน มี burst แดง เข้มข้น',
      previewHTML: '<div class="preview preview--comic_burst">ฮ่า!</div>',
    },
    {
      id: 'random_mix',
      name: 'สุ่มสไตล์ต่อไฟล์',
      desc: 'มิกซ์ทุกสไตล์ ไม่ซ้ำกันเลย',
      previewHTML: '<div class="preview preview--random_mix">มิกซ์</div>',
    },
  ];

  function byId(id) { return document.getElementById(id); }

  // Build preview HTML for the trigger (smaller, name + desc)
  function buildTriggerHTML(template) {
    return `
      <div class="template-dd-preview">${template.previewHTML}</div>
      <span class="template-dd-label">
        <span class="label-name">${escapeHTML(template.name)}</span>
        <span class="label-desc">${escapeHTML(template.desc)}</span>
      </span>
      <span class="template-dd-caret">
        <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">
          <polyline points="6 9 12 15 18 9"></polyline>
        </svg>
      </span>
    `;
  }

  // Build option HTML (larger preview, name + desc + check)
  function buildOptionHTML(template, selected) {
    return `
      <div class="template-dd-preview">${template.previewHTML}</div>
      <div>
        <div class="label-name">${escapeHTML(template.name)}</div>
        <div class="label-desc">${escapeHTML(template.desc)}</div>
      </div>
      <svg class="check-icon" viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round">
        <polyline points="20 6 9 17 4 12"></polyline>
      </svg>
    `;
  }

  function escapeHTML(s) {
    return String(s || '').replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    })[c]);
  }

  function init() {
    const nativeSelect = byId('subtitle-template');
    if (!nativeSelect) return;
    if (nativeSelect.dataset.ddMounted === '1') return;
    nativeSelect.dataset.ddMounted = '1';

    // Build component
    const root = document.createElement('div');
    root.className = 'template-dd';
    root.setAttribute('data-template-dd', '');

    const trigger = document.createElement('button');
    trigger.type = 'button';
    trigger.className = 'template-dd-trigger';
    trigger.setAttribute('aria-haspopup', 'listbox');
    trigger.setAttribute('aria-expanded', 'false');

    const panel = document.createElement('div');
    panel.className = 'template-dd-panel';
    panel.setAttribute('role', 'listbox');
    panel.setAttribute('aria-label', 'เลือกสไตล์ซับ');

    TEMPLATES.forEach((tpl) => {
      const opt = document.createElement('div');
      opt.className = 'template-dd-option';
      opt.setAttribute('role', 'option');
      opt.setAttribute('data-value', tpl.id);
      opt.innerHTML = buildOptionHTML(tpl, tpl.id === nativeSelect.value);
      opt.addEventListener('click', () => selectValue(tpl.id));
      panel.appendChild(opt);
    });

    trigger.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      toggle();
    });

    root.appendChild(trigger);
    root.appendChild(panel);

    // Insert after the native select (keep the select for form compat)
    nativeSelect.parentNode.insertBefore(root, nativeSelect.nextSibling);
    nativeSelect.style.position = 'absolute';
    nativeSelect.style.left = '-9999px';
    nativeSelect.style.opacity = '0';
    nativeSelect.style.pointerEvents = 'none';
    nativeSelect.tabIndex = -1;

    // Initial trigger
    setTrigger(nativeSelect.value);

    // Outside click
    document.addEventListener('click', (e) => {
      if (!root.contains(e.target)) close();
    });

    // Escape to close, Arrow keys to navigate
    root.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') { close(); trigger.focus(); }
      else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        if (!root.classList.contains('open')) { open(); return; }
        const opts = Array.from(panel.querySelectorAll('.template-dd-option'));
        const idx = opts.findIndex((o) => o.classList.contains('selected'));
        const next = e.key === 'ArrowDown' ? Math.min(opts.length - 1, idx + 1) : Math.max(0, idx - 1);
        opts[next]?.focus();
        selectValue(opts[next].dataset.value);
      } else if (e.key === 'Enter') {
        e.preventDefault();
        if (!root.classList.contains('open')) open();
      }
    });

    function toggle() {
      root.classList.contains('open') ? close() : open();
    }
    function open() {
      root.classList.add('open');
      trigger.setAttribute('aria-expanded', 'true');
    }
    function close() {
      root.classList.remove('open');
      trigger.setAttribute('aria-expanded', 'false');
    }

    function setTrigger(value) {
      const tpl = TEMPLATES.find((t) => t.id === value) || TEMPLATES[0];
      trigger.innerHTML = buildTriggerHTML(tpl);
      // Update selected state in panel
      panel.querySelectorAll('.template-dd-option').forEach((opt) => {
        opt.classList.toggle('selected', opt.dataset.value === value);
      });
    }

    function selectValue(value) {
      // Update native select (for app.js)
      nativeSelect.value = value;
      // Dispatch change event so existing listeners run
      nativeSelect.dispatchEvent(new Event('change', { bubbles: true }));
      setTrigger(value);
      close();
      // Brief visual feedback
      const selected = panel.querySelector(`[data-value="${value}"]`);
      if (selected) {
        selected.classList.add('flash');
        setTimeout(() => selected.classList.remove('flash'), 220);
      }
    }

    // Expose for external reads
    root._getValue = () => nativeSelect.value;
    root._setValue = (v) => selectValue(v);
  }

  // Wait for DOM ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
