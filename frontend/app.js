// ---------- State ----------
const state = {
  token: localStorage.getItem('careflow_token') || null,
  user: null,
  patients: [],
  patientsTotal: 0,
  selectedPatientId: null,
  conversations: [],
  currentConversationId: null,
  messages: [],
  view: 'chat', // 'chat' | 'audit'
};

const EXAMPLES_PATIENT = [
  "Summarize this patient's previous visits",
  "What lab results stand out?",
  "What medications is this patient currently on?",
  "Any allergy conflicts I should know about?",
];
const EXAMPLES_GENERAL = [
  "What does the sepsis screening guideline say about antibiotic allergies?",
  "What is the initial management bundle for sepsis?",
];

// ---------- API helper ----------
async function api(path, { method = 'GET', body, isForm = false } = {}) {
  const headers = {};
  if (state.token) headers['Authorization'] = `Bearer ${state.token}`;
  if (!isForm && body !== undefined) headers['Content-Type'] = 'application/json';

  const res = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : (isForm ? body : JSON.stringify(body)),
  });

  let data = null;
  try { data = await res.json(); } catch (e) { /* no body */ }

  if (!res.ok) {
    const msg = (data && data.detail) ? data.detail : `Request failed (${res.status})`;
    throw new Error(msg);
  }
  return data;
}

// ---------- DOM refs ----------
const el = (id) => document.getElementById(id);
const loginView = el('loginView');
const signupView = el('signupView');
const landingView = el('landingView');
const appView = el('appView');

function showOnly(view) {
  [landingView, loginView, signupView, appView].forEach(v => v.classList.add('hidden'));
  view.classList.remove('hidden');
}

el('landingSignInBtn').addEventListener('click', () => showOnly(loginView));
el('heroSignInBtn').addEventListener('click', () => showOnly(loginView));
el('landingSignUpBtn').addEventListener('click', () => showOnly(signupView));
el('heroSignUpBtn').addEventListener('click', () => showOnly(signupView));
el('goToSignup').addEventListener('click', (e) => { e.preventDefault(); showOnly(signupView); });
el('goToLogin').addEventListener('click', (e) => { e.preventDefault(); showOnly(loginView); });
el('loginBackHome').addEventListener('click', (e) => { e.preventDefault(); showOnly(landingView); });
el('signupBackHome').addEventListener('click', (e) => { e.preventDefault(); showOnly(landingView); });

// ---------- Auth ----------
el('signupForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  el('signupError').classList.add('hidden');
  el('signupBtn').disabled = true;
  try {
    const data = await api('/signup', {
      method: 'POST',
      body: {
        organization_name: el('signupOrgName').value.trim(),
        full_name: el('signupFullName').value.trim(),
        username: el('signupUsername').value.trim(),
        password: el('signupPassword').value,
      },
    });
    state.token = data.token;
    state.user = data.user;
    localStorage.setItem('careflow_token', state.token);
    await boot();
  } catch (err) {
    el('signupError').textContent = err.message;
    el('signupError').classList.remove('hidden');
  } finally {
    el('signupBtn').disabled = false;
  }
});

el('loginForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const username = el('loginUsername').value.trim();
  const password = el('loginPassword').value;
  el('loginError').classList.add('hidden');
  el('loginBtn').disabled = true;
  try {
    const data = await api('/auth/login', { method: 'POST', body: { username, password } });
    state.token = data.token;
    state.user = data.user;
    localStorage.setItem('careflow_token', state.token);
    await boot();
  } catch (err) {
    el('loginError').textContent = err.message;
    el('loginError').classList.remove('hidden');
  } finally {
    el('loginBtn').disabled = false;
  }
});

el('logoutBtn').addEventListener('click', () => {
  state.token = null;
  state.user = null;
  localStorage.removeItem('careflow_token');
  location.reload();
});

async function tryResumeSession() {
  if (!state.token) return false;
  try {
    state.user = await api('/auth/me');
    return true;
  } catch (e) {
    state.token = null;
    localStorage.removeItem('careflow_token');
    return false;
  }
}

// ---------- Boot ----------
async function boot() {
  showOnly(appView);

  el('userName').textContent = state.user.full_name;
  el('userRole').textContent = state.user.role;
  el('topbarOrg').textContent = state.user.organization_name || '';

  renderTopNav();

  const canSeePatients = state.user.role === 'clinician' || state.user.role === 'admin';
  el('patientSection').classList.toggle('hidden', !canSeePatients);
  el('uploadBtn').classList.toggle('hidden', !canSeePatients);
  el('addPatientBtn').classList.toggle('hidden', state.user.role !== 'admin');

  if (canSeePatients) await loadPatients();
  await loadConversations();
  renderConversationList();
}

const NAV_ICONS = {
  chat: '<path d="M3 5h18v11H9l-4 4V16H3V5z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" fill="none"/>',
  analytics: '<path d="M4 20V11M12 20V4M20 20v-7M3 20h18" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>',
  audit: '<path d="M5 3h14v18H5z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" fill="none"/><path d="M8 8h8M8 12h8M8 16h5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>',
  users: '<circle cx="12" cy="8" r="3.2" stroke="currentColor" stroke-width="1.8" fill="none"/><path d="M5 20c0-3.5 3-6 7-6s7 2.5 7 6" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" fill="none"/>',
};

function railButton(view, label, iconKey) {
  const btn = document.createElement('button');
  btn.className = state.view === view ? 'active' : '';
  btn.title = label;
  btn.setAttribute('aria-label', label);
  btn.innerHTML = `<svg viewBox="0 0 24 24">${NAV_ICONS[iconKey]}</svg>`;
  btn.onclick = () => switchView(view);
  return btn;
}

function renderTopNav() {
  const rail = el('iconRail');
  rail.innerHTML = '';
  rail.appendChild(railButton('chat', 'Chat', 'chat'));

  if (state.user.role === 'admin') {
    rail.appendChild(railButton('analytics', 'Overview', 'analytics'));
    rail.appendChild(railButton('audit', 'Audit Log', 'audit'));
    rail.appendChild(railButton('users', 'Users', 'users'));
  }
}

function switchView(view) {
  state.view = view;
  renderTopNav();
  el('chatWorkspace').classList.toggle('hidden', view !== 'chat');
  el('auditWorkspace').classList.toggle('hidden', view !== 'audit');
  el('usersWorkspace').classList.toggle('hidden', view !== 'users');
  el('analyticsWorkspace').classList.toggle('hidden', view !== 'analytics');
  if (view === 'audit') loadAuditLog();
  if (view === 'users') loadUsers();
  if (view === 'analytics') loadAnalytics();
}

// ---------- Patients ----------
async function loadPatients(q) {
  try {
    const qs = q ? `?q=${encodeURIComponent(q)}` : '';
    const data = await api(`/patients${qs}`);
    state.patients = data.items;
    state.patientsTotal = data.total;
    renderPatientList();
  } catch (err) {
    console.error(err);
  }
}

function renderPatientList() {
  const container = el('patientList');
  container.innerHTML = '';
  if (state.patients.length === 0) {
    container.innerHTML = '<div class="empty-hint">No patients found.</div>';
    return;
  }
  for (const p of state.patients) {
    const div = document.createElement('div');
    div.className = 'patient-item' + (state.selectedPatientId === p.id ? ' selected' : '');
    div.innerHTML = `<span class="patient-item-name">${escapeHtml(p.name)}</span><span class="patient-item-mrn">${escapeHtml(p.mrn)}${p.dob ? ' &middot; DOB ' + escapeHtml(p.dob) : ''}</span>`;
    div.onclick = () => startConversation(p.id);
    container.appendChild(div);
  }
  if (state.patientsTotal > state.patients.length) {
    const hint = document.createElement('div');
    hint.className = 'patient-list-hint';
    hint.textContent = `Showing ${state.patients.length} of ${state.patientsTotal} — search to narrow down`;
    container.appendChild(hint);
  }
}

let searchDebounce;
el('patientSearch').addEventListener('input', (e) => {
  clearTimeout(searchDebounce);
  searchDebounce = setTimeout(() => loadPatients(e.target.value.trim()), 250);
});

// ---------- Conversations ----------
async function loadConversations() {
  state.conversations = await api('/conversations');
}

function renderConversationList() {
  const container = el('conversationList');
  container.innerHTML = '';
  if (state.conversations.length === 0) {
    container.innerHTML = '<div class="empty-hint">No conversations yet.</div>';
    return;
  }
  for (const c of state.conversations) {
    const div = document.createElement('div');
    div.className = 'conversation-item' + (state.currentConversationId === c.id ? ' selected' : '');
    const when = new Date(c.created_at).toLocaleString();
    div.innerHTML = `<span class="conversation-item-title">${escapeHtml(c.title || 'Conversation')}</span><span class="conversation-item-meta">${when}</span>`;
    div.onclick = () => openConversation(c);
    container.appendChild(div);
  }
}

el('newConvoBtn').addEventListener('click', () => startConversation(state.selectedPatientId));

async function startConversation(patientId) {
  state.selectedPatientId = patientId || null;
  renderPatientList();
  try {
    const conv = await api('/conversations', { method: 'POST', body: { patient_id: patientId || null } });
    state.conversations.unshift(conv);
    await openConversation(conv);
  } catch (err) {
    alert(err.message);
  }
}

async function openConversation(conv) {
  state.currentConversationId = conv.id;
  state.selectedPatientId = conv.patient_id;
  renderPatientList();
  renderConversationList();

  el('chatTitle').textContent = conv.title || 'Conversation';
  el('chatSubtitle').textContent = conv.patient_id
    ? 'Scoped to this patient’s authorized documents + hospital guidelines'
    : 'Scoped to hospital guidelines only';

  await renderPatientBanner(conv.patient_id);

  el('chatForm').classList.remove('hidden');
  el('chatEmptyState').classList.add('hidden');
  el('toggleDocsBtn').classList.toggle('hidden', !conv.patient_id && state.user.role !== 'clinician' && state.user.role !== 'admin');

  renderExamples(conv.patient_id);

  state.messages = await api(`/conversations/${conv.id}/messages`);
  renderMessages();

  if (conv.patient_id) {
    await loadDocuments(conv.patient_id);
  } else {
    await loadDocuments(null);
  }
}

function calcAge(dob) {
  if (!dob) return null;
  const birth = new Date(dob);
  if (isNaN(birth)) return null;
  const now = new Date();
  let age = now.getFullYear() - birth.getFullYear();
  const m = now.getMonth() - birth.getMonth();
  if (m < 0 || (m === 0 && now.getDate() < birth.getDate())) age--;
  return age;
}

async function renderPatientBanner(patientId) {
  const banner = el('patientBanner');
  if (!patientId) {
    banner.classList.add('hidden');
    banner.innerHTML = '';
    return;
  }
  // The sidebar list is capped (see /patients), so the patient for this conversation may not
  // be in it -- e.g. reopening an old conversation about someone who doesn't sort into the
  // first page of results. Fall back to a direct lookup rather than silently showing nothing.
  let p = state.patients.find(pt => pt.id === patientId);
  if (!p) {
    try {
      p = await api(`/patients/${patientId}`);
    } catch (err) {
      banner.classList.add('hidden');
      banner.innerHTML = '';
      return;
    }
  }
  const age = calcAge(p.dob);
  const initials = p.name.split(/\s+/).filter(Boolean).slice(0, 2).map(w => w[0].toUpperCase()).join('');
  banner.innerHTML = `
    <div class="patient-banner-avatar">${escapeHtml(initials)}</div>
    <div class="patient-banner-field">
      <div class="patient-banner-label">Patient</div>
      <div class="patient-banner-value name">${escapeHtml(p.name)}</div>
    </div>
    <div class="patient-banner-field">
      <div class="patient-banner-label">MRN</div>
      <div class="patient-banner-value">${escapeHtml(p.mrn)}</div>
    </div>
    <div class="patient-banner-field">
      <div class="patient-banner-label">DOB</div>
      <div class="patient-banner-value">${escapeHtml(p.dob || '—')}${age !== null ? ` (${age}y)` : ''}</div>
    </div>
    <div class="patient-banner-field">
      <div class="patient-banner-label">Sex</div>
      <div class="patient-banner-value">${escapeHtml(p.sex || '—')}</div>
    </div>
  `;
  banner.classList.remove('hidden');
}

function renderExamples(hasPatient) {
  const row = el('examplesRow');
  row.innerHTML = '';
  const examples = hasPatient ? EXAMPLES_PATIENT : EXAMPLES_GENERAL;
  for (const ex of examples) {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'example-chip';
    chip.textContent = ex;
    chip.onclick = () => { el('chatInput').value = ex; el('chatInput').focus(); };
    row.appendChild(chip);
  }
}

// ---------- Messages ----------
function renderMessages() {
  const container = el('chatMessages');
  container.innerHTML = '';
  if (state.messages.length === 0) {
    container.innerHTML = '<div class="empty-state"><p>Ask a question to get started.</p></div>';
    return;
  }
  for (const m of state.messages) {
    container.appendChild(renderMessage(m));
  }
  container.scrollTop = container.scrollHeight;
}

function renderMessage(m) {
  const wrap = document.createElement('div');
  wrap.className = `msg ${m.role}`;

  if (m.role === 'user') {
    const label = document.createElement('div');
    label.className = 'query-line';
    label.textContent = 'Query';
    wrap.appendChild(label);
  }

  const bubble = document.createElement('div');
  bubble.className = 'msg-bubble';
  bubble.textContent = m.content;
  wrap.appendChild(bubble);

  if (m.role === 'assistant') {
    const meta = document.createElement('div');
    meta.className = 'msg-meta';
    if (m.confidence) {
      const badge = document.createElement('span');
      badge.className = `confidence-badge ${m.confidence}`;
      badge.textContent = `${m.confidence} confidence`;
      meta.appendChild(badge);
    }
    wrap.appendChild(meta);

    if (m.sources && m.sources.length) {
      const srcRow = document.createElement('div');
      srcRow.className = 'sources-row';
      const seen = new Set();
      for (const s of m.sources) {
        if (seen.has(s.document_id)) continue;
        seen.add(s.document_id);
        const chip = document.createElement('button');
        chip.type = 'button';
        chip.className = 'source-chip';
        chip.textContent = s.title;
        chip.onclick = () => openDocViewer(s.document_id);
        srcRow.appendChild(chip);
      }
      wrap.appendChild(srcRow);
    }
  }
  return wrap;
}

el('chatForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const input = el('chatInput');
  const query = input.value.trim();
  if (!query || !state.currentConversationId) return;

  state.messages.push({ role: 'user', content: query });
  renderMessages();
  input.value = '';
  el('sendBtn').disabled = true;

  const container = el('chatMessages');

  // Live-streaming assistant node: built once, updated in place as tokens arrive.
  const liveMsg = renderMessage({ role: 'assistant', content: '', sources: [], confidence: null });
  const liveBubble = liveMsg.querySelector('.msg-bubble');
  const typing = document.createElement('div');
  typing.className = 'typing-indicator';
  typing.textContent = 'CareFlow AI is thinking...';
  liveMsg.appendChild(typing);
  container.appendChild(liveMsg);
  container.scrollTop = container.scrollHeight;

  let fullAnswer = '';
  let sources = [];
  let confidence = null;
  let firstToken = true;

  try {
    const res = await fetch(`/conversations/${state.currentConversationId}/messages`, {
      method: 'POST',
      headers: { 'Authorization': `Bearer ${state.token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ query }),
    });
    if (!res.ok || !res.body) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.detail || `Request failed (${res.status})`);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const parts = buffer.split('\n\n');
      buffer = parts.pop();
      for (const part of parts) {
        if (!part.startsWith('data: ')) continue;
        const event = JSON.parse(part.slice(6));
        if (event.type === 'sources') {
          sources = event.sources;
        } else if (event.type === 'token') {
          if (firstToken) { typing.remove(); firstToken = false; }
          fullAnswer += event.text;
          liveBubble.textContent = fullAnswer;
          container.scrollTop = container.scrollHeight;
        } else if (event.type === 'done') {
          fullAnswer = event.answer;
          confidence = event.confidence;
          sources = event.sources || sources;
        }
      }
    }

    typing.remove();
    const finalMsg = { role: 'assistant', content: fullAnswer, sources, confidence };
    state.messages.push(finalMsg);
    liveMsg.replaceWith(renderMessage(finalMsg));

    const idx = state.conversations.findIndex(c => c.id === state.currentConversationId);
    if (idx > -1) {
      const [c] = state.conversations.splice(idx, 1);
      state.conversations.unshift(c);
      renderConversationList();
    }
  } catch (err) {
    typing.remove();
    const errMsg = { role: 'assistant', content: `Error: ${err.message}`, sources: [], confidence: null };
    state.messages.push(errMsg);
    liveMsg.replaceWith(renderMessage(errMsg));
  } finally {
    el('sendBtn').disabled = false;
  }
});

el('chatInput').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) el('chatForm').requestSubmit();
});

// ---------- Documents panel ----------
el('toggleDocsBtn').addEventListener('click', () => el('docsPanel').classList.toggle('hidden'));
el('closeDocsBtn').addEventListener('click', () => el('docsPanel').classList.add('hidden'));

async function loadDocuments(patientId) {
  try {
    const qs = patientId ? `?patient_id=${patientId}` : '';
    const docs = await api(`/documents${qs}`);
    renderDocuments(docs, patientId);
  } catch (err) {
    console.error(err);
  }
}

function renderDocuments(docs, patientId) {
  const container = el('docsList');
  container.innerHTML = '';
  const canManage = state.user.role === 'clinician' || state.user.role === 'admin';
  if (docs.length === 0) {
    container.innerHTML = '<div class="empty-hint">No documents visible here.</div>';
  }
  for (const d of docs) {
    const card = document.createElement('div');
    card.className = 'doc-card';
    card.innerHTML = `
      <div class="doc-card-title">${escapeHtml(d.title)}</div>
      <div class="doc-card-meta">
        <span>${escapeHtml(d.doc_type)} &middot; ${d.chunk_count} chunk(s)</span>
        ${state.user.role === 'admin' ? `<button class="doc-delete-btn" data-id="${d.id}">Delete</button>` : ''}
      </div>`;
    card.querySelector('.doc-card-title').onclick = () => openDocViewer(d.id);
    const delBtn = card.querySelector('.doc-delete-btn');
    if (delBtn) delBtn.onclick = async () => {
      if (!confirm(`Delete "${d.title}"? This removes it from the index.`)) return;
      await api(`/documents/${d.id}`, { method: 'DELETE' });
      loadDocuments(patientId);
    };
    container.appendChild(card);
  }
  el('uploadBtn').onclick = () => openUploadModal(patientId);
}

// ---------- Document viewer ----------
async function openDocViewer(documentId) {
  try {
    const doc = await api(`/documents/${documentId}`);
    el('docViewerTitle').textContent = doc.title;
    el('docViewerMeta').textContent = `${doc.doc_type} · uploaded by ${doc.uploaded_by || 'system'} · ${doc.created_at}`;
    el('docViewerText').textContent = doc.full_text;
    el('docViewerModal').classList.remove('hidden');
  } catch (err) {
    alert(err.message);
  }
}
el('closeDocViewerBtn').addEventListener('click', () => el('docViewerModal').classList.add('hidden'));

// ---------- Upload modal ----------
function openUploadModal(patientId) {
  el('uploadForm').reset();
  el('uploadError').classList.add('hidden');
  el('uploadForm').dataset.patientId = patientId || '';
  el('uploadModal').classList.remove('hidden');
}
el('cancelUploadBtn').addEventListener('click', () => el('uploadModal').classList.add('hidden'));

el('uploadForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const fileInput = el('uploadFile');
  if (!fileInput.files.length) return;

  const roles = Array.from(document.querySelectorAll('.role-checks input:checked')).map(i => i.value);
  const form = new FormData();
  form.append('file', fileInput.files[0]);
  form.append('title', el('uploadTitle').value);
  form.append('doc_type', el('uploadDocType').value);
  form.append('allowed_roles', roles.join(','));
  const patientId = el('uploadForm').dataset.patientId;
  if (patientId) form.append('patient_id', patientId);

  try {
    await api('/documents/upload', { method: 'POST', body: form, isForm: true });
    el('uploadModal').classList.add('hidden');
    await loadDocuments(patientId ? Number(patientId) : null);
  } catch (err) {
    el('uploadError').textContent = err.message;
    el('uploadError').classList.remove('hidden');
  }
});

// ---------- Admin audit log ----------
async function loadAuditLog() {
  try {
    const logs = await api('/admin/audit-logs');
    const tbody = el('auditTbody');
    tbody.innerHTML = '';
    for (const log of logs) {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td>${escapeHtml(new Date(log.created_at).toLocaleString())}</td>
        <td>${escapeHtml(log.username)}</td>
        <td>${escapeHtml(log.role)}</td>
        <td>${escapeHtml(log.patient_name || '—')}</td>
        <td class="query-cell">${escapeHtml(log.query)}</td>
        <td>${log.confidence ? `<span class="confidence-badge ${log.confidence}">${log.confidence}</span>` : '—'}</td>
        <td>${log.retrieved_doc_ids.length}</td>
      `;
      tbody.appendChild(tr);
    }
  } catch (err) {
    console.error(err);
  }
}

// ---------- Admin analytics ----------
const CHART_COLOR = '#3142c4';
const CONFIDENCE_COLORS = { high: '#1c6b43', medium: '#946600', low: '#a3291f', unknown: '#5e6a63' };

function svgBarChart(rows, { labelKey, valueKey, color = CHART_COLOR, maxBars = 8 }) {
  rows = rows.slice(0, maxBars);
  if (!rows.length) return '<div class="chart-empty">No data yet.</div>';
  const max = Math.max(...rows.map(r => r[valueKey]), 1);
  const barHeight = 20, gap = 10, labelWidth = 140, chartWidth = 220;
  const height = rows.length * (barHeight + gap);
  let bars = '';
  rows.forEach((r, i) => {
    const y = i * (barHeight + gap);
    const w = Math.max((r[valueKey] / max) * chartWidth, 2);
    const barColor = typeof color === 'function' ? color(r) : color;
    const label = escapeHtml(String(r[labelKey]).slice(0, 20));
    bars += `<text x="0" y="${y + barHeight / 2 + 4}" font-size="11" fill="#1c2420">${label}</text>
      <rect x="${labelWidth}" y="${y}" width="${w}" height="${barHeight}" fill="${barColor}" rx="2" />
      <text x="${labelWidth + w + 6}" y="${y + barHeight / 2 + 4}" font-size="11" fill="#5e6a63">${r[valueKey]}</text>`;
  });
  return `<svg viewBox="0 0 ${labelWidth + chartWidth + 36} ${height}" width="100%" height="${height}">${bars}</svg>`;
}

function svgTimeSeries(rows, { labelKey = 'day', valueKey = 'n', color = CHART_COLOR } = {}) {
  if (!rows.length) return '<div class="chart-empty">No queries in the last 14 days.</div>';
  const max = Math.max(...rows.map(r => r[valueKey]), 1);
  const barWidth = 22, gap = 8, chartHeight = 110;
  const width = rows.length * (barWidth + gap);
  let bars = '';
  rows.forEach((r, i) => {
    const x = i * (barWidth + gap);
    const h = Math.max((r[valueKey] / max) * chartHeight, 2);
    const day = String(r[labelKey]).slice(5).replace('-', '/');
    bars += `<text x="${x + barWidth / 2}" y="${chartHeight - h - 5}" font-size="10" fill="#1c2420" text-anchor="middle">${r[valueKey]}</text>
      <rect x="${x}" y="${chartHeight - h}" width="${barWidth}" height="${h}" fill="${color}" rx="2" />
      <text x="${x + barWidth / 2}" y="${chartHeight + 16}" font-size="9" fill="#5e6a63" text-anchor="middle">${day}</text>`;
  });
  return `<svg viewBox="0 0 ${width + 10} ${chartHeight + 30}" width="100%" height="${chartHeight + 30}">${bars}</svg>`;
}

function renderStatGrid(totals) {
  const stats = [
    { label: 'Total queries', value: totals.total_queries },
    { label: 'Patients', value: totals.total_patients },
    { label: 'Documents indexed', value: totals.total_documents },
    { label: 'Active users', value: totals.active_users },
  ];
  el('statGrid').innerHTML = stats.map(s => `
    <div class="stat-card">
      <div class="stat-value">${s.value}</div>
      <div class="stat-label">${escapeHtml(s.label)}</div>
    </div>
  `).join('');
}

async function loadAnalytics() {
  try {
    const data = await api('/admin/analytics');
    renderStatGrid(data.totals);
    el('chartQueriesPerDay').innerHTML = svgTimeSeries(data.queries_per_day);
    el('chartConfidence').innerHTML = svgBarChart(
      [...data.confidence_breakdown].sort((a, b) => b.n - a.n),
      { labelKey: 'confidence', valueKey: 'n', color: (r) => CONFIDENCE_COLORS[r.confidence] || CHART_COLOR },
    );
    el('chartRole').innerHTML = svgBarChart(data.role_breakdown, { labelKey: 'role', valueKey: 'n' });
    el('chartTopPatients').innerHTML = svgBarChart(data.top_patients, { labelKey: 'name', valueKey: 'n' });
    el('chartDocTypes').innerHTML = svgBarChart(data.doc_type_breakdown, { labelKey: 'doc_type', valueKey: 'n', maxBars: 10 });
  } catch (err) {
    console.error(err);
  }
}

// ---------- Admin user management ----------
async function loadUsers() {
  try {
    const users = await api('/admin/users');
    const tbody = el('usersTbody');
    tbody.innerHTML = '';
    for (const u of users) {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td>${escapeHtml(u.full_name)}</td>
        <td>${escapeHtml(u.username)}</td>
        <td><span class="role-badge">${escapeHtml(u.role)}</span></td>
        <td>${escapeHtml(u.created_at)}</td>
      `;
      tbody.appendChild(tr);
    }
  } catch (err) {
    console.error(err);
  }
}

el('addUserBtn').addEventListener('click', () => {
  el('userForm').reset();
  el('userError').classList.add('hidden');
  el('userModal').classList.remove('hidden');
});
el('cancelUserBtn').addEventListener('click', () => el('userModal').classList.add('hidden'));

el('userForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  el('userError').classList.add('hidden');
  try {
    await api('/admin/users', {
      method: 'POST',
      body: {
        full_name: el('newUserFullName').value.trim(),
        username: el('newUserUsername').value.trim(),
        password: el('newUserPassword').value,
        role: el('newUserRole').value,
      },
    });
    el('userModal').classList.add('hidden');
    await loadUsers();
  } catch (err) {
    el('userError').textContent = err.message;
    el('userError').classList.remove('hidden');
  }
});

// ---------- Admin patient management ----------
el('addPatientBtn').addEventListener('click', () => {
  el('patientForm').reset();
  el('patientError').classList.add('hidden');
  el('patientModal').classList.remove('hidden');
});
el('cancelPatientBtn').addEventListener('click', () => el('patientModal').classList.add('hidden'));

el('patientForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  el('patientError').classList.add('hidden');
  try {
    const patient = await api('/patients', {
      method: 'POST',
      body: {
        name: el('newPatientName').value.trim(),
        mrn: el('newPatientMrn').value.trim(),
        dob: el('newPatientDob').value || null,
        sex: el('newPatientSex').value || null,
      },
    });
    el('patientModal').classList.add('hidden');
    await loadPatients();
    await startConversation(patient.id);
  } catch (err) {
    el('patientError').textContent = err.message;
    el('patientError').classList.remove('hidden');
  }
});

// ---------- Utils ----------
function escapeHtml(str) {
  if (str === null || str === undefined) return '';
  return String(str)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// ---------- Entry point ----------
(async function init() {
  const resumed = await tryResumeSession();
  if (resumed) {
    await boot();
  } else {
    showOnly(landingView);
  }
})();
