// ---------- config ----------
// The frontend is served by the backend at /app/, so the backend URL is just
// the origin we were loaded from. config.yaml controls the port. If the page
// was opened as file://, we fall back to localhost:1113 for dev.
function resolveBackend() {
  // Explicit override from the login card wins.
  const override = localStorage.getItem('ark_backend');
  if (override) return override;
  // Otherwise use the origin we were served from (config.yaml controls the port).
  if (location.protocol.startsWith('http') && location.host) {
    return location.origin;
  }
  return 'http://localhost:1113';
}

const CONFIG = {
  backend: resolveBackend(),
  token: localStorage.getItem('ark_token') || '',
  userId: localStorage.getItem('ark_user_id') || '',
  username: localStorage.getItem('ark_username') || '',
  model: 'ark-agent',
};

function authHeaders(extra) {
  const h = Object.assign({}, extra || {});
  if (CONFIG.token) h['Authorization'] = 'Bearer ' + CONFIG.token;
  return h;
}

function setUserLabels() {
  document.getElementById('userLabel').textContent = CONFIG.username || '—';
  document.getElementById('backendLabel').textContent = CONFIG.backend.replace(/^https?:\/\//, '');
}
setUserLabels();

// ---------- state ----------
// approvals + tasks come from the DB; watching stays local until we wire a watchers table.
let _lastRenderKey = '';
const _dismissedTasks = new Set(JSON.parse(localStorage.getItem('ark_dismissed_tasks') || '[]'));
const _resolvedPlans = new Set(JSON.parse(localStorage.getItem('ark_resolved_plans') || '[]'));

// kind -> short icon for the activity stream
const KIND_ICON = {
  shell: '$', file: '▤', search: '◉', plan: '≡',
  ask: '?', mcp: '⊕', completed: '✓', failed: '✗', start: '▶',
};

const state = {
  approvals: [],
  tasks: [],
  completed: [],
  computerTasks: [],       // active + recent computer tasks
  computerPath: '/home/user',
  computerFiles: [],
  computerFileContent: null,
  computerFilePath: null,
  computerExpandedTask: null,
  computerTaskEvents: {},
  watching: [
    { id: 'w1', text: 'linear.app / team ark', when: 'every 5m' },
    { id: 'w2', text: 'mail.google.com / inbox', when: 'live' },
    { id: 'w3', text: 'calendar.google.com', when: '15m' },
  ],
  history: [],
  expandedTaskId: null,
  taskEvents: {},   // taskId -> [events]
};

function relTime(iso) {
  if (!iso) return '';
  const t = new Date(iso).getTime();
  const s = Math.max(1, Math.floor((Date.now() - t) / 1000));
  if (s < 60) return s + 's';
  if (s < 3600) return Math.floor(s / 60) + 'm';
  if (s < 86400) return Math.floor(s / 3600) + 'h';
  return Math.floor(s / 86400) + 'd';
}

// ---------- render ----------
function render() {
  // Key on stable fields only — relTime and event content change every poll
  // even when nothing meaningful has changed, causing needless re-renders.
  const key = JSON.stringify({
    a: state.approvals.map((x) => x.approval_id),
    t: state.tasks.map((x) => x.id + '|' + x.status),
    c: state.completed.map((x) => x.id),
    x: state.expandedTaskId,
    evLen: (state.taskEvents[state.expandedTaskId] || []).length,
    ct: state.computerTasks.map((x) => x.task_id + '|' + x.status),
    cx: state.computerExpandedTask,
    cxLen: (state.computerTaskEvents[state.computerExpandedTask] || []).length,
  });
  if (key === _lastRenderKey) return;
  _lastRenderKey = key;

  // Preserve focus — rebuilding tasksList can cause the browser to drop it.
  const focused = document.activeElement;
  const focusedId = focused && focused.id;
  const a = document.getElementById('approvalsList');
  a.innerHTML = '';
  if (!state.approvals.length && !state.completed.length) {
    a.innerHTML = '<div class="empty">no subagents waiting on you</div>';
  } else {
    for (const ap of state.approvals) {
      const el = document.createElement('div');
      el.className = 'card approval-card';
      el.dataset.approvalId = ap.approval_id;
      const controls = ap.kind === 'binary'
        ? `
          <div class="actions">
            <button class="mini" data-approval-no>decline</button>
            <button class="mini primary" data-approval-yes>approve</button>
          </div>
        `
        : `
          <textarea placeholder="your answer..."></textarea>
          <div class="actions">
            <button class="mini primary" data-approval-submit>submit</button>
          </div>
        `;
      el.innerHTML = `
        <div class="meta">
          <span class="tag">${escapeHtml(ap.task_title || 'task')}</span>
          <span>${ap.kind === 'binary' ? 'approve/decline' : 'answer'}</span>
        </div>
        <div class="prompt">${escapeHtml(ap.prompt)}</div>
        ${controls}
      `;
      a.appendChild(el);
    }
  }

  // Completed task result cards — shown in approvals so the user sees the output.
  for (const task of state.completed) {
    const el = document.createElement('div');
    el.className = 'card';
    el.innerHTML = `
      <div class="meta">
        <span class="tag" style="color:var(--ok);border-color:var(--ok)">done</span>
        <span>${escapeHtml(task.when)} ago</span>
      </div>
      <div class="title">${escapeHtml(task.text)}</div>
      ${task.summary ? `<div class="plan">${escapeHtml(task.summary)}</div>` : ''}
      <div class="actions">
        <button class="mini" data-dismiss-task="${task.id}">dismiss</button>
      </div>
    `;
    a.appendChild(el);
  }

  const t = document.getElementById('tasksList');
  t.innerHTML = '';
  if (!state.tasks.length) {
    t.innerHTML = '<div class="empty">buddy is idle</div>';
  } else {
    for (const task of state.tasks) {
      const el = document.createElement('div');
      const isExpanded = state.expandedTaskId === task.id;
      el.className = 'task-row' + (isExpanded ? ' expanded' : '');
      const waiting = task.status === 'awaiting_approval';
      const statusText = waiting ? 'waiting on you' : (task.when || 'running');
      const eventHtml = isExpanded ? renderEvents(task.id) : '';
      el.innerHTML = `
        <div class="row-top">
          <div class="label">
            <span class="spin" style="${waiting ? 'background: var(--fg-mute);' : ''}"></span>
            <span class="text">${escapeHtml(task.text)}</span>
          </div>
          <div class="row-actions">
            <span class="when">${escapeHtml(statusText)}</span>
            <button class="icon" data-task-expand="${task.id}" title="events">${isExpanded ? '▾' : '▸'}</button>
            <button class="icon" data-task-cancel="${task.id}" title="cancel">×</button>
          </div>
        </div>
        ${eventHtml}
      `;
      t.appendChild(el);
    }
  }

  const w = document.getElementById('watchingList');
  w.innerHTML = '';
  if (!state.watching.length) {
    w.innerHTML = '<div class="empty">not watching anything yet</div>';
  } else {
    for (const ws of state.watching) {
      const el = document.createElement('div');
      el.className = 'watch-row';
      el.innerHTML = `
        <div class="label"><span class="text">${escapeHtml(ws.text)}</span></div>
        <span class="when">${escapeHtml(ws.when)}</span>
      `;
      w.appendChild(el);
    }
  }

  renderComputerTasksList();

  document.getElementById('approvalsCount').textContent = state.approvals.length;
  document.getElementById('tasksCount').textContent = state.tasks.length;
  document.getElementById('watchingCount').textContent = state.watching.length;
  document.getElementById('approvalsPill').textContent = state.approvals.length + ' pending';

  renderLog();

  if (focusedId) {
    const el = document.getElementById(focusedId);
    if (el && document.activeElement !== el) el.focus();
  }
}

function renderLog() {
  const log = document.getElementById('log');
  log.innerHTML = '';
  for (const m of state.history) {
    const row = document.createElement('div');
    row.className = 'msg ' + m.role;
    const body = m.role === 'user' ? escapeHtml(m.content) : renderMd(m.content);
    row.innerHTML = `<div class="who">${m.role === 'user' ? 'you' : 'ark'}</div><div class="body">${body}</div>`;
    log.appendChild(row);
  }
  log.scrollTop = log.scrollHeight;
}

function renderEvents(taskId) {
  const events = state.taskEvents[taskId] || [];
  if (!events.length) {
    return `<div class="task-events">(no events yet)</div>`;
  }
  const rows = events.map((e) => {
    const content = (e.content || '').slice(0, 240);
    return `<div class="ev-row"><span class="ev-kind">${escapeHtml(e.kind)}</span>${escapeHtml(content)}</div>`;
  }).join('');
  return `<div class="task-events">${rows}</div>`;
}

// ---------- computer page: task list (left sidebar) ----------
function renderComputerTasksList() {
  const taskList = document.getElementById('cvTaskList');
  const countEl = document.getElementById('computerCount');
  if (countEl) {
    countEl.textContent = state.computerTasks.filter((t) => ['running', 'pending'].includes(t.status)).length;
  }
  if (!taskList) return;
  if (!state.computerTasks.length) {
    taskList.innerHTML = '<div class="empty">no tasks yet. ask buddy to write some code.</div>';
    return;
  }
  taskList.innerHTML = '';
  for (const t of state.computerTasks) {
    const running = ['running', 'pending'].includes(t.status);
    const ok = t.status === 'completed';
    const isExp = state.computerExpandedTask === t.task_id;
    const statusCls = running ? 'running' : (ok ? 'ok' : 'err');
    const el = document.createElement('div');
    el.className = 'cp-task' + (isExp ? ' expanded' : '');
    el.innerHTML = `
      <div class="cp-task-top" data-cv-expand="${t.task_id}">
        <span class="cp-task-status ${statusCls}">${running ? '<span class="spin"></span>' : (ok ? '✓' : '✗')}</span>
        <span class="cp-task-text">${escapeHtml((t.prompt || '').slice(0, 90))}</span>
        <span class="cp-task-when">${relTime(t.updated_at)}</span>
      </div>
      ${t.summary ? `<div class="cp-task-summary">${escapeHtml(t.summary.slice(0, 200))}</div>` : ''}
      ${(t.outputs && t.outputs.length) ? `<div class="cp-task-outputs">${t.outputs.map((p) => `<span>${escapeHtml(p)}</span>`).join('')}</div>` : ''}
      ${isExp ? renderComputerEvents(t.task_id) : ''}
    `;
    taskList.appendChild(el);
  }
}

function renderComputerEvents(taskId) {
  const evs = state.computerTaskEvents[taskId] || [];
  if (!evs.length) return `<div class="cp-events">(no events yet)</div>`;
  const rows = evs.slice(-30).map((e) => {
    const icon = KIND_ICON[e.kind] || '·';
    const content = (e.content || '').slice(0, 160);
    return `<div class="cp-ev"><span class="cp-ev-kind">${escapeHtml(icon)}</span><span>${escapeHtml(content || e.kind)}</span></div>`;
  }).join('');
  return `<div class="cp-events">${rows}</div>`;
}

// ---------- computer page: filesystem ----------
function renderFiles() {
  const entries = document.getElementById('cvEntries');
  const pathEl = document.getElementById('cvPath');
  if (pathEl) pathEl.textContent = state.computerPath;
  if (!entries) return;
  if (!state.computerFiles.length) {
    entries.innerHTML = '<div class="empty">(empty)</div>';
    return;
  }
  entries.innerHTML = '';
  // dirs first, then files, alphabetical
  const sorted = [...state.computerFiles].sort((a, b) =>
    (b.is_dir - a.is_dir) || a.name.localeCompare(b.name));
  for (const f of sorted) {
    const row = document.createElement('div');
    row.className = 'cv-entry' + (f.is_dir ? ' is-dir' : '') +
      (f.path === state.computerFilePath ? ' selected' : '');
    row.dataset.path = f.path;
    row.dataset.isDir = f.is_dir ? '1' : '';
    row.innerHTML = `<span class="cv-name">${f.is_dir ? '▸' : ' '} ${escapeHtml(f.name)}</span>` +
      `<span class="cv-size">${f.is_dir ? '' : fmtSize(f.size)}</span>`;
    entries.appendChild(row);
  }
}

function renderFileContent() {
  const body = document.getElementById('cvFileBody');
  const header = document.getElementById('cvFileHeader');
  if (!body) return;
  if (state.computerFileContent === null) {
    header.textContent = 'no file selected';
    body.innerHTML = '<span style="color:var(--fg-mute)">click a file to read it</span>';
    return;
  }
  header.textContent = state.computerFilePath || '';
  body.textContent = state.computerFileContent;
}

function fmtSize(n) {
  if (n < 1024) return n + 'b';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + 'k';
  return (n / 1024 / 1024).toFixed(1) + 'm';
}

async function browseFiles(path) {
  state.computerPath = path || '/home/user';
  document.getElementById('cvEntries').innerHTML = '<div class="empty">loading...</div>';
  document.getElementById('cvPath').textContent = state.computerPath;
  try {
    const r = await fetch(`${CONFIG.backend}/computer/files?path=${encodeURIComponent(state.computerPath)}`, {
      headers: authHeaders(),
    });
    if (!r.ok) {
      document.getElementById('cvEntries').innerHTML = '<div class="empty">could not read directory</div>';
      return;
    }
    const j = await r.json();
    state.computerFiles = j.entries || [];
    renderFiles();
  } catch (err) {
    console.warn('browseFiles', err);
    document.getElementById('cvEntries').innerHTML = '<div class="empty">error</div>';
  }
}

async function readComputerFile(path) {
  state.computerFilePath = path;
  renderFiles();  // re-mark selection
  document.getElementById('cvFileHeader').textContent = path;
  document.getElementById('cvFileBody').textContent = 'loading...';
  try {
    const r = await fetch(`${CONFIG.backend}/computer/file?path=${encodeURIComponent(path)}`, {
      headers: authHeaders(),
    });
    if (!r.ok) { document.getElementById('cvFileBody').textContent = '(could not read file)'; return; }
    const j = await r.json();
    state.computerFileContent = j.content + (j.truncated ? '\n\n... (truncated, file is ' + j.size + ' bytes)' : '');
    renderFileContent();
  } catch (err) {
    console.warn('readComputerFile', err);
    document.getElementById('cvFileBody').textContent = '(error)';
  }
}

async function refreshComputerTasks() {
  if (!CONFIG.token) return;
  try {
    const r = await fetch(`${CONFIG.backend}/computer/tasks`, { headers: authHeaders() });
    if (!r.ok) return;
    const j = await r.json();
    state.computerTasks = j.tasks || [];
    // Load events for any expanded task.
    if (state.computerExpandedTask) {
      const er = await fetch(
        `${CONFIG.backend}/computer/tasks/${encodeURIComponent(state.computerExpandedTask)}/events`,
        { headers: authHeaders() }
      );
      if (er.ok) {
        const ej = await er.json();
        state.computerTaskEvents[state.computerExpandedTask] = ej.events || [];
      }
    }
  } catch (err) { console.warn('refreshComputerTasks', err); }
}

function escapeHtml(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// Minimal markdown-lite: escape first, then linkify [text](url) and bare
// URLs, then handle **bold**. Used for chat replies so buddy can emit
// clickable connect links from Smithery. Also detects fenced `ark-plan`
// blocks and replaces them with interactive plan cards the user can
// approve/decline inline.
function renderMd(s) {
  const planCards = [];
  // Pull out ```ark-plan\n{json}\n``` blocks first so their contents don't
  // get HTML-escaped.
  let pre = String(s ?? '').replace(/```ark-plan\s*\n([\s\S]*?)\n```/g, (_m, body) => {
    let payload = null;
    try { payload = JSON.parse(body); } catch {}
    if (!payload || !Array.isArray(payload.plan_steps)) {
      return '';  // drop malformed blocks; the human-readable prose above still shows
    }
    const token = `__ARK_PLAN_${planCards.length}__`;
    planCards.push(payload);
    return token;
  });

  let out = escapeHtml(pre);
  // [text](url) ... url is already escaped so &amp; / &#39; etc are safe
  out = out.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
    (_m, t, u) => `<a href="${u}" target="_blank" rel="noopener">${t}</a>`);
  // bare http(s) URLs not already inside an anchor
  out = out.replace(/(^|[\s(])((?:https?:\/\/)[^\s<)"]+)/g,
    (_m, pre, u) => `${pre}<a href="${u}" target="_blank" rel="noopener">${u}</a>`);
  // **bold**
  out = out.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');

  // Swap plan placeholders back in as interactive cards.
  planCards.forEach((p, i) => {
    const token = `__ARK_PLAN_${i}__`;
    const stepsHtml = p.plan_steps.map((st) => `<li>${escapeHtml(st)}</li>`).join('');
    const tools = (p.required_tools || []).join(', ') || 'none';
    // base64 so HTML attribute quoting never collides with the payload
    const b64 = btoa(unescape(encodeURIComponent(JSON.stringify(p))));
    const isResolved = _resolvedPlans.has(b64);
    const card = `
      <div class="plan-card${isResolved ? ' resolved' : ''}" data-plan="${b64}">
        <div class="pc-title">${escapeHtml(p.title || 'plan')}</div>
        <ol>${stepsHtml}</ol>
        <div class="pc-tools">tools: ${escapeHtml(tools)}</div>
        <div class="pc-actions">
          <button class="mini" data-plan-decline>decline</button>
          <button class="mini primary" data-plan-approve>approve &amp; run</button>
        </div>
      </div>`;
    out = out.replace(token, card);
  });
  return out;
}

// ---------- event wiring ----------
document.addEventListener('click', async (e) => {
  // inline plan card in chat
  const planApprove = e.target.closest('[data-plan-approve]');
  const planDecline = e.target.closest('[data-plan-decline]');
  if (planApprove || planDecline) {
    const card = (planApprove || planDecline).closest('.plan-card');
    if (!card) return;
    if (card.classList.contains('resolved')) return;
    const b64 = card.getAttribute('data-plan') || '';
    let plan = null;
    try { plan = JSON.parse(decodeURIComponent(escape(atob(b64)))); } catch {}
    card.classList.add('resolved');
    _resolvedPlans.add(b64);
    localStorage.setItem('ark_resolved_plans', JSON.stringify([..._resolvedPlans]));
    if (planDecline) return;
    if (!plan) return;
    try {
      const r = await fetch(CONFIG.backend + '/tasks', {
        method: 'POST',
        headers: authHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({
          title: plan.title,
          plan_steps: plan.plan_steps,
          required_tools: plan.required_tools || [],
          context_payload: { source: 'chat' },
        }),
      });
      if (!r.ok) console.warn('approve-plan failed', r.status, await r.text());
    } catch (err) {
      console.warn('approve-plan error', err);
    }
    await refreshTasks();
    return;
  }

  // approval cards in the Pending Approvals panel
  const apBinYes = e.target.closest('[data-approval-yes]');
  const apBinNo = e.target.closest('[data-approval-no]');
  const apTxt = e.target.closest('[data-approval-submit]');
  if (apBinYes || apBinNo || apTxt) {
    const card = (apBinYes || apBinNo || apTxt).closest('.approval-card');
    if (!card) return;
    const approvalId = card.dataset.approvalId;
    let body = {};
    if (apBinYes) body = { approved: true };
    else if (apBinNo) body = { approved: false };
    else {
      const ta = card.querySelector('textarea');
      body = { answer: (ta && ta.value) || '' };
    }
    try {
      await fetch(`${CONFIG.backend}/tasks/approvals/${encodeURIComponent(approvalId)}/respond`, {
        method: 'POST',
        headers: authHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify(body),
      });
    } catch (err) {
      console.warn('approval respond error', err);
    }
    await refreshTasks();
    return;
  }

  // dismiss completed task card
  const dismissBtn = e.target.closest('[data-dismiss-task]');
  if (dismissBtn) {
    const id = dismissBtn.dataset.dismissTask;
    _dismissedTasks.add(id);
    localStorage.setItem('ark_dismissed_tasks', JSON.stringify([..._dismissedTasks]));
    state.completed = state.completed.filter((t) => t.id !== id);
    _lastRenderKey = '';
    render();
    return;
  }

  // computer task expand
  const cvExpand = e.target.closest('[data-cv-expand]');
  if (cvExpand) {
    const id = cvExpand.dataset.cvExpand;
    state.computerExpandedTask = state.computerExpandedTask === id ? null : id;
    if (state.computerExpandedTask) {
      const er = await fetch(
        `${CONFIG.backend}/computer/tasks/${encodeURIComponent(id)}/events`,
        { headers: authHeaders() }
      );
      if (er.ok) {
        const ej = await er.json();
        state.computerTaskEvents[id] = ej.events || [];
      }
    }
    _lastRenderKey = '';
    render();
    return;
  }

  // task row controls
  const rowCancel = e.target.closest('[data-task-cancel]');
  const rowExpand = e.target.closest('[data-task-expand]');
  if (rowCancel) {
    const id = rowCancel.dataset.taskCancel;
    try {
      await fetch(`${CONFIG.backend}/tasks/${encodeURIComponent(id)}/cancel`, {
        method: 'POST',
        headers: authHeaders(),
      });
    } catch (err) { console.warn('cancel error', err); }
    await refreshTasks();
    return;
  }
  if (rowExpand) {
    const id = rowExpand.dataset.taskExpand;
    state.expandedTaskId = state.expandedTaskId === id ? null : id;
    if (state.expandedTaskId) {
      await loadTaskEvents(id);
    }
    render();
    return;
  }
});

async function loadTaskEvents(taskId) {
  try {
    const r = await fetch(`${CONFIG.backend}/tasks/${encodeURIComponent(taskId)}/events`, {
      headers: authHeaders(),
    });
    if (!r.ok) return;
    const j = await r.json();
    state.taskEvents[taskId] = j.events || [];
  } catch (err) {
    console.warn('loadTaskEvents error', err);
  }
}

// filesystem navigation
document.getElementById('cvEntries').addEventListener('click', async (e) => {
  const row = e.target.closest('.cv-entry');
  if (!row) return;
  if (row.dataset.isDir) {
    await browseFiles(row.dataset.path);
  } else {
    await readComputerFile(row.dataset.path);
  }
});

document.getElementById('cvUp').addEventListener('click', async () => {
  const parts = state.computerPath.split('/').filter(Boolean);
  parts.pop();
  await browseFiles('/' + parts.join('/') || '/');
});

document.getElementById('themeToggle').addEventListener('click', () => {
  const cur = document.documentElement.getAttribute('data-theme');
  if (cur === 'dark') {
    document.documentElement.removeAttribute('data-theme');
    document.getElementById('themeToggle').textContent = 'dark mode';
  } else {
    document.documentElement.setAttribute('data-theme', 'dark');
    document.getElementById('themeToggle').textContent = 'light mode';
  }
});

// keyboard shortcuts
const input = document.getElementById('inputBox');
const cursor = document.getElementById('cursor');
input.addEventListener('focus', () => (cursor.style.opacity = 0));
input.addEventListener('blur', () => (cursor.style.opacity = 1));
document.addEventListener('keydown', (e) => {
  if (e.key === '/' && document.activeElement !== input) {
    e.preventDefault();
    input.focus();
  } else if (e.key === 'Escape') {
    input.value = '';
    input.blur();
    document.getElementById('drawer').classList.remove('open');
  }
});
input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && input.value.trim()) {
    sendMessage(input.value.trim());
    input.value = '';
  }
});

document.getElementById('historyToggle').addEventListener('click', () => {
  document.getElementById('drawer').classList.toggle('open');
});
document.getElementById('drawerClose').addEventListener('click', () => {
  document.getElementById('drawer').classList.remove('open');
});

// ---------- settings modal + connections ----------
const settingsModal = document.getElementById('settingsModal');
const connList = document.getElementById('connList');
let _connPoll = null;

function openSettings() {
  document.getElementById('settingsUser').textContent = CONFIG.username || '—';
  document.getElementById('settingsBackend').textContent = CONFIG.backend;
  settingsModal.classList.add('open');
  refreshConnections();
}
function closeSettings() {
  settingsModal.classList.remove('open');
  if (_connPoll) { clearInterval(_connPoll); _connPoll = null; }
}

document.getElementById('settingsBtn').addEventListener('click', openSettings);
document.getElementById('settingsClose').addEventListener('click', closeSettings);
document.getElementById('settingsSignOut').addEventListener('click', () => {
  if (!confirm('sign out?')) return;
  localStorage.removeItem('ark_token');
  localStorage.removeItem('ark_user_id');
  localStorage.removeItem('ark_username');
  location.reload();
});
// click outside card to close
settingsModal.addEventListener('click', (e) => {
  if (e.target === settingsModal) closeSettings();
});

async function fetchServices() {
  try {
    const r = await fetch(CONFIG.backend + '/services', {
      headers: authHeaders({ 'X-User-ID': CONFIG.userId || '' }),
    });
    if (!r.ok) throw new Error('bad status ' + r.status);
    return await r.json();
  } catch (e) {
    return { error: String(e), shared: [], per_user: [] };
  }
}

function renderConnections(data) {
  const services = [...(data.per_user || []), ...(data.shared || [])];
  if (data.error) {
    connList.innerHTML = `<div style="font-size:11px;color:var(--err);padding:8px 2px;">${escapeHtml(data.error)}</div>`;
    return;
  }
  if (!services.length) {
    connList.innerHTML = '<div style="font-size:11px;color:var(--fg-mute);padding:8px 2px;">no services configured. add entries to <code>mcp_servers</code> in config.yaml.</div>';
    return;
  }
  connList.innerHTML = services.map((svc) => {
    const connected = !!svc.connected;
    const perUser = (data.per_user || []).some((s) => s.service === svc.service);
    const statusCls = connected ? 'ok' : (svc.setup_url ? 'pending' : '');
    const statusTxt = connected ? 'connected' : (perUser ? 'not connected' : 'shared');
    const btn = connected
      ? (perUser ? `<button data-disconnect="${escapeHtml(svc.service)}">disconnect</button>` : '<span style="font-size:10px;color:var(--fg-mute);">always on</span>')
      : `<button class="primary" data-connect="${escapeHtml(svc.service)}">connect</button>`;
    return `
      <div class="conn-row" data-service="${escapeHtml(svc.service)}">
        <div>
          <span class="name">${escapeHtml(svc.name || svc.service)}</span>
          <span class="status ${statusCls}">${statusTxt}</span>
        </div>
        <div>${btn}</div>
      </div>
    `;
  }).join('');
}

async function refreshConnections() {
  renderConnections(await fetchServices());
}

async function connectService(service, btn) {
  if (btn) { btn.disabled = true; btn.textContent = 'opening...'; }
  try {
    const r = await fetch(`${CONFIG.backend}/services/${encodeURIComponent(service)}/connect`, {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json', 'X-User-ID': CONFIG.userId || '' }),
    });
    const j = await r.json();
    if (j.status === 'connected') {
      await refreshConnections();
      return;
    }
    if (j.setup_url) {
      // Open Smithery's OAuth page in a popup, then poll until it flips to connected.
      const popup = window.open(j.setup_url, 'ark_oauth', 'width=560,height=720');
      startConnectionPoll(service, popup);
    } else {
      alert('could not start oauth: ' + (j.error || 'unknown error'));
      await refreshConnections();
    }
  } catch (e) {
    alert('connect failed: ' + e.message);
  } finally {
    if (btn) { btn.disabled = false; }
  }
}

async function disconnectService(service) {
  try {
    await fetch(`${CONFIG.backend}/services/${encodeURIComponent(service)}/disconnect`, {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json', 'X-User-ID': CONFIG.userId || '' }),
    });
  } catch (e) {}
  await refreshConnections();
}

function startConnectionPoll(service, popup) {
  if (_connPoll) clearInterval(_connPoll);
  _connPoll = setInterval(async () => {
    const data = await fetchServices();
    renderConnections(data);
    const svc = (data.per_user || []).find((s) => s.service === service);
    const popupClosed = popup && popup.closed;
    if ((svc && svc.connected) || popupClosed) {
      clearInterval(_connPoll);
      _connPoll = null;
      // one more refresh after popup closes in case connection just landed
      setTimeout(refreshConnections, 400);
    }
  }, 2000);
}

// Listen for the oauth-callback postMessage from the popup so we refresh
// the moment the callback page loads, without waiting for the next poll tick.
window.addEventListener('message', (event) => {
  const d = event.data || {};
  if (d.type === 'ark-oauth-callback') {
    refreshConnections();
  }
});

// Delegated click handler for connect/disconnect buttons
connList.addEventListener('click', (e) => {
  const connectBtn = e.target.closest('[data-connect]');
  if (connectBtn) {
    connectService(connectBtn.getAttribute('data-connect'), connectBtn);
    return;
  }
  const discBtn = e.target.closest('[data-disconnect]');
  if (discBtn) {
    disconnectService(discBtn.getAttribute('data-disconnect'));
  }
});

// escape key closes modal
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && settingsModal.classList.contains('open')) {
    closeSettings();
  }
});

// side nav routing
document.querySelectorAll('nav.side a[data-view]').forEach((a) => {
  a.addEventListener('click', () => {
    document.querySelectorAll('nav.side a').forEach((x) => x.classList.remove('active'));
    a.classList.add('active');
    const view = a.dataset.view;
    const main = document.getElementById('main');
    const desk = document.getElementById('deskContent');
    const cp = document.getElementById('computerPage');
    if (view === 'computer') {
      // Swap the desk content for the computer page within main (chrome stays).
      desk.classList.add('hidden');
      cp.classList.remove('hidden');
      renderComputerTasksList();
      renderFileContent();
      browseFiles(state.computerPath);
    } else {
      cp.classList.add('hidden');
      desk.classList.remove('hidden');
      if (view === 'chat') {
        document.getElementById('drawer').classList.add('open');
        input.focus();
      } else if (view === 'approvals') {
        document.getElementById('zone-approvals').scrollIntoView({ behavior: 'smooth', block: 'start' });
      } else if (view === 'tasks') {
        document.getElementById('zone-tasks').scrollIntoView({ behavior: 'smooth', block: 'start' });
      } else if (view === 'watching') {
        document.getElementById('zone-watching').scrollIntoView({ behavior: 'smooth', block: 'start' });
      } else {
        main.scrollTo({ top: 0, behavior: 'smooth' });
      }
    }
  });
});

// ---------- backend ----------
async function checkHealth() {
  const dot = document.getElementById('statusDot');
  try {
    const r = await fetch(CONFIG.backend + '/health', { method: 'GET' });
    if (r.status !== 200 && r.status !== 503) throw new Error('bad status');
    const j = await r.json();
    const allRunning = j.status === 'ok';
    dot.classList.remove('off', 'err');
    if (!allRunning) dot.classList.add('err');
    dot.title = 'backend ' + (j.status || 'unknown');
  } catch (err) {
    dot.classList.remove('off');
    dot.classList.add('err');
    dot.title = 'backend unreachable at ' + CONFIG.backend;
  }
}

async function refreshTasks() {
  if (!CONFIG.token) return;
  try {
    const [runningRes, waitingRes, approvalsRes, completedRes] = await Promise.all([
      fetch(`${CONFIG.backend}/tasks?status=running`, { headers: authHeaders() }),
      fetch(`${CONFIG.backend}/tasks?status=awaiting_approval`, { headers: authHeaders() }),
      fetch(`${CONFIG.backend}/tasks/approvals/pending`, { headers: authHeaders() }),
      fetch(`${CONFIG.backend}/tasks?status=completed`, { headers: authHeaders() }),
    ]);
    if ([runningRes, waitingRes, approvalsRes, completedRes].some((r) => r.status === 401)) {
      localStorage.removeItem('ark_token');
      location.reload();
      return;
    }
    const running = runningRes.ok ? await runningRes.json() : { tasks: [] };
    const waiting = waitingRes.ok ? await waitingRes.json() : { tasks: [] };
    const approvals = approvalsRes.ok ? await approvalsRes.json() : { approvals: [] };
    const completedAll = completedRes.ok ? await completedRes.json() : { tasks: [] };

    const combined = [...(running.tasks || []), ...(waiting.tasks || [])];
    state.tasks = combined.map((t) => ({
      id: t.task_id,
      text: t.title || 'running task',
      when: relTime(t.updated_at),
      status: t.status,
    }));
    state.approvals = (approvals.approvals || []).map((a) => ({
      approval_id: a.approval_id,
      task_id: a.task_id,
      task_title: a.task_title,
      kind: a.kind,
      prompt: a.prompt,
    }));

    // Show tasks completed in the last 10 minutes so the user sees the result.
    const tenMinAgo = Date.now() - 10 * 60 * 1000;
    state.completed = (completedAll.tasks || [])
      .filter((t) => new Date(t.updated_at).getTime() > tenMinAgo && !_dismissedTasks.has(t.task_id))
      .map((t) => ({
        id: t.task_id,
        text: t.title || 'task',
        summary: t.context_payload?.summary || '',
        when: relTime(t.updated_at),
      }));

    // Refresh events for an expanded task so the log stays live.
    if (state.expandedTaskId) {
      await loadTaskEvents(state.expandedTaskId);
    }
    render();
  } catch (err) {
    console.warn('refreshTasks failed', err);
  }
}

// ---------- auth bootstrap ----------
async function ensureAuthed() {
  // validate existing token
  if (CONFIG.token) {
    try {
      const r = await fetch(CONFIG.backend + '/auth/me', { headers: authHeaders() });
      if (r.ok) {
        const j = await r.json();
        CONFIG.userId = j.user_id;
        CONFIG.username = j.username;
        localStorage.setItem('ark_user_id', j.user_id);
        localStorage.setItem('ark_username', j.username);
        setUserLabels();
        document.getElementById('loginOverlay').classList.add('hidden');
        return true;
      }
    } catch {}
    // token no good
    CONFIG.token = '';
    localStorage.removeItem('ark_token');
  }
  document.getElementById('loginOverlay').classList.remove('hidden');
  document.getElementById('loginUser').focus();
  return false;
}

async function doLogin(username) {
  const errEl = document.getElementById('loginErr');
  errEl.textContent = '';
  const btn = document.getElementById('loginBtn');
  btn.disabled = true;
  try {
    const backendEl = document.getElementById('loginBackend');
    const backendInput = backendEl ? backendEl.value.trim().replace(/\/$/, '') : '';
    if (backendInput) {
      CONFIG.backend = backendInput;
      localStorage.setItem('ark_backend', backendInput);
      setUserLabels();
    }
    const r = await fetch(CONFIG.backend + '/auth/demo-login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username }),
    });
    if (!r.ok) {
      const t = await r.text();
      throw new Error(r.status + ': ' + t);
    }
    const j = await r.json();
    CONFIG.token = j.token;
    CONFIG.userId = j.user_id;
    CONFIG.username = j.username;
    localStorage.setItem('ark_token', j.token);
    localStorage.setItem('ark_user_id', j.user_id);
    localStorage.setItem('ark_username', j.username);
    setUserLabels();
    document.getElementById('loginOverlay').classList.add('hidden');
    await refreshTasks();
  } catch (e) {
    errEl.textContent = 'login failed. ' + (e.message || e);
  } finally {
    btn.disabled = false;
  }
}

document.getElementById('loginBtn').addEventListener('click', () => {
  const u = document.getElementById('loginUser').value.trim();
  if (u.length < 2) {
    document.getElementById('loginErr').textContent = 'username must be at least 2 characters';
    return;
  }
  doLogin(u);
});
document.getElementById('loginUser').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') document.getElementById('loginBtn').click();
});

// boot
(async () => {
  checkHealth();
  setInterval(checkHealth, 15000);
  const ok = await ensureAuthed();
  if (ok) {
    await refreshTasks();
    await refreshComputerTasks();
    setInterval(async () => {
      await refreshTasks();
      await refreshComputerTasks();
      render();
    }, 6000);
  }
})();

// ---------- chat: novel streaming with fold-into-card ----------
let currentFloater = null;

function showUserFloater(text) {
  const layer = document.getElementById('floaterLayer');
  const f = document.createElement('div');
  f.className = 'floater';
  f.innerHTML = '<span class="you">you</span>' + escapeHtml(text);
  layer.appendChild(f);
  setTimeout(() => foldAndRemove(f), 1400);
}

function openAssistantFloater() {
  const layer = document.getElementById('floaterLayer');
  const f = document.createElement('div');
  f.className = 'floater';
  f.innerHTML = '<span class="you">ark</span><span class="content"></span>';
  layer.appendChild(f);
  return f;
}

function foldAndRemove(el) {
  if (!el) return;
  el.classList.add('folding');
  setTimeout(() => el.remove(), 520);
}

function classifyIntent(userText, reply) {
  const t = (userText + ' ' + reply).toLowerCase();
  if (/(watch|monitor|keep an eye|check (on )?)/.test(t)) return 'watching';
  if (/(approv|should i|review|ok to|workshop|plan:)/.test(t)) return 'approvals';
  return 'tasks';
}

async function sendMessage(text) {
  state.history.push({ role: 'user', content: text });
  showUserFloater(text);

  const floater = openAssistantFloater();
  currentFloater = floater;
  const contentEl = floater.querySelector('.content');

  let reply = '';
  try {
    const res = await fetch(CONFIG.backend + '/v1/chat/completions', {
      method: 'POST',
      headers: authHeaders({
        'Content-Type': 'application/json',
        'X-User-ID': CONFIG.userId,
      }),
      body: JSON.stringify({
        model: CONFIG.model,
        stream: true,
        user: CONFIG.userId,
        messages: state.history.map((m) => ({ role: m.role === 'ark' ? 'assistant' : 'user', content: m.content })),
      }),
    });

    if (!res.ok || !res.body) throw new Error('stream failed: ' + res.status);

    const reader = res.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buffer = '';

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop();
      for (const raw of lines) {
        const line = raw.trim();
        if (!line.startsWith('data:')) continue;
        const payload = line.slice(5).trim();
        if (payload === '[DONE]') continue;
        try {
          const j = JSON.parse(payload);
          const delta = j.choices?.[0]?.delta?.content;
          if (delta) {
            reply += delta;
            contentEl.innerHTML = renderMd(reply);
          }
        } catch {}
      }
    }
  } catch (err) {
    reply = '[buddy offline: ' + (err.message || err) + ']\nstart the backend: python base_module/app.py';
    contentEl.innerHTML = renderMd(reply);
  }

  state.history.push({ role: 'ark', content: reply });
  renderLog();

  // Pull fresh state from the DB. Plans are now approved inline in the
  // chat (no DB write on workshop); once the user clicks approve the
  // frontend POSTs /tasks, which spawns a subagent that starts emitting
  // events + approvals visible on the desk.
  setTimeout(async () => {
    await refreshTasks();
    foldAndRemove(floater);
  }, 1400);
}

function summarize(s) {
  return s.length > 80 ? s.slice(0, 78) + '…' : s;
}

render();
