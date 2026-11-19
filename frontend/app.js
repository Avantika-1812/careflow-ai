// ---------- State ----------
const state = {
  token: localStorage.getItem('careflow_token') || null,
  user: null,
  patients: [],
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
const appView = el('appView');

// ---------- Auth ----------
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
  loginView.classList.add('hidden');
  appView.classList.remove('hidden');

  el('userName').textContent = state.user.full_name;
  el('userRole').textContent = state.user.role;

  renderTopNav();

  const canSeePatients = state.user.role === 'clinician' || state.user.role === 'admin';
  el('patientSection').classList.toggle('hidden', !canSeePatients);
  el('uploadBtn').classList.toggle('hidden', !canSeePatients);

  if (canSeePatients) await loadPatients();
  await loadConversations();
  renderConversationList();
}

function renderTopNav() {
  const nav = el('topbarNav');
  nav.innerHTML = '';
  const chatBtn = document.createElement('button');
  chatBtn.textContent = 'Chat';
  chatBtn.className = state.view === 'chat' ? 'active' : '';
  chatBtn.onclick = () => switchView('chat');
  nav.appendChild(chatBtn);

  if (state.user.role === 'admin') {
    const auditBtn = document.createElement('button');
    auditBtn.textContent = 'Audit Log';
    auditBtn.className = state.view === 'audit' ? 'active' : '';
    auditBtn.onclick = () => switchView('audit');
    nav.appendChild(auditBtn);

    const usersBtn = document.createElement('button');
    usersBtn.textContent = 'Users';
    usersBtn.className = state.view === 'users' ? 'active' : '';
    usersBtn.onclick = () => switchView('users');
    nav.appendChild(usersBtn);
  }
}

function switchView(view) {
  state.view = view;
  renderTopNav();
  el('chatWorkspace').classList.toggle('hidden', view !== 'chat');
  el('auditWorkspace').classList.toggle('hidden', view !== 'audit');
  el('usersWorkspace').classList.toggle('hidden', view !== 'users');
  if (view === 'audit') loadAuditLog();
  if (view === 'users') loadUsers();
}

// ---------- Patients ----------
async function loadPatients(q) {
  try {
    const qs = q ? `?q=${encodeURIComponent(q)}` : '';
    state.patients = await api(`/patients${qs}`);
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
  const typing = document.createElement('div');
  typing.className = 'typing-indicator';
  typing.textContent = 'CareFlow AI is thinking...';
  container.appendChild(typing);
  container.scrollTop = container.scrollHeight;

  try {
    const result = await api(`/conversations/${state.currentConversationId}/messages`, {
      method: 'POST',
      body: { query },
    });
    typing.remove();
    state.messages.push({ role: 'assistant', content: result.answer, sources: result.sources, confidence: result.confidence });
    renderMessages();
    const idx = state.conversations.findIndex(c => c.id === state.currentConversationId);
    if (idx > -1) {
      const [c] = state.conversations.splice(idx, 1);
      state.conversations.unshift(c);
      renderConversationList();
    }
  } catch (err) {
    typing.remove();
    state.messages.push({ role: 'assistant', content: `Error: ${err.message}`, sources: [], confidence: null });
    renderMessages();
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
        <td>${escapeHtml(log.created_at)}</td>
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
    loginView.classList.remove('hidden');
    appView.classList.add('hidden');
  }
})();
