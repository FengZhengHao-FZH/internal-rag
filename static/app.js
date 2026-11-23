const $ = (id) => document.getElementById(id);
let busy = false;
let currentUser = null;
let departmentsData = { is_admin: false, items: [] };
let pendingFile = null;
let currentConversationId = null;
let isAdmin = false;

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function toast(msg, isError = false) {
  const el = document.createElement('div');
  el.className = 'toast' + (isError ? ' error' : '');
  el.textContent = msg;
  $('toast').appendChild(el);
  setTimeout(() => el.remove(), 4000);
}

async function api(path, options = {}) {
  const token = localStorage.getItem('token');
  options.headers = options.headers || {};
  if (token) options.headers['Authorization'] = 'Bearer ' + token;
  const res = await fetch(path, options);
  const data = await res.json();
  if (data.code === 401) {
    localStorage.removeItem('token');
    localStorage.removeItem('refresh_token');
    showLogin();
  }
  return data;
}

const PAGE_SIZE = 20;
const pageState = { docs: 1, users: 1, audit: 1 };

function renderPager(containerId, key, data, load) {
  const page = data.page || 1;
  const size = data.page_size || PAGE_SIZE;
  const total = data.total || 0;
  const pages = Math.max(1, Math.ceil(total / size));
  pageState[key] = page;
  const el = $(containerId);
  if (!el) return;
  el.innerHTML =
    '<button onclick="' + load + '(1)"' + (page <= 1 ? ' disabled' : '') + '>首页</button>' +
    '<button onclick="' + load + '(' + (page - 1) + ')"' + (page <= 1 ? ' disabled' : '') + '>上一页</button>' +
    '<span class="pager-info">第 ' + page + ' / ' + pages + ' 页</span>' +
    '<button onclick="' + load + '(' + (page + 1) + ')"' + (page >= pages ? ' disabled' : '') + '>下一页</button>' +
    '<button onclick="' + load + '(' + pages + ')"' + (page >= pages ? ' disabled' : '') + '>末页</button>' +
    '<span class="pager-info">共 ' + total + ' 条</span>';
}

function switchView(view) {
  ['chat', 'docs', 'users', 'audit'].forEach(v => {
    $('view-' + v).style.display = v === view ? 'flex' : 'none';
    const nav = $('nav' + v.charAt(0).toUpperCase() + v.slice(1));
    if (nav) nav.classList.toggle('active', v === view);
  });
  $('viewTitle').textContent = { chat: '对话', docs: '文档管理', users: '用户管理', audit: '审计日志' }[view];
  if (view === 'docs') loadDocs();
  if (view === 'users') loadUsers();
  if (view === 'audit') { loadAuditActions(); loadAuditLogs(); }
}

async function loadStatus() {
  try {
    const r = await api('/api/status');
    if (r.code === 200) {
      $('fileCount').textContent = r.data.file_count;
      $('vectorCount').textContent = r.data.vector_count;
      $('kbStatus').textContent = r.data.vector_count > 0 ? '知识库就绪' : '知识库为空';
    } else {
      $('kbStatus').textContent = '状态获取失败';
    }
  } catch (e) {
    $('kbStatus').textContent = '服务未连接';
  }
}

function renderConversations(items) {
  const list = $('convList');
  if (!items || !items.length) {
    list.innerHTML = '<div class="conv-empty">暂无对话记录</div>';
    return;
  }
  list.innerHTML = items.map(c =>
    '<div class="conv-item' + (c.id === currentConversationId ? ' active' : '') + '" onclick="openConversation(' + c.id + ')">' +
    '<span class="conv-title">' + esc(c.title) + '</span>' +
    '<button class="conv-del" title="删除" onclick="event.stopPropagation(); deleteConversation(' + c.id + ')">×</button>' +
    '</div>'
  ).join('');
}

async function loadConversations() {
  const r = await api('/api/conversations');
  if (r.code === 200) renderConversations(r.data.items);
}

function clearMessages() {
  $('messagesInner').innerHTML =
    '<div class="welcome" id="welcome"><div class="welcome-logo">知</div><h1>有什么可以帮你？</h1><p>基于内部文档回答，输入问题开始</p></div>';
  $('question').value = '';
  autoResize($('question'));
}

function newChat() {
  switchView('chat');
  currentConversationId = null;
  clearMessages();
  Array.from($('convList').querySelectorAll('.conv-item')).forEach(el => el.classList.remove('active'));
}

async function openConversation(id) {
  const r = await api('/api/conversations/' + id);
  if (r.code !== 200) { toast(r.message, true); return; }
  currentConversationId = id;
  switchView('chat');
  clearMessages();
  $('welcome')?.remove();
  r.data.messages.forEach(m => appendMessage(m.role, m.content));
  renderConversationActive();
  scrollBottom();
}

async function renderConversationActive() {
  const r = await api('/api/conversations');
  if (r.code === 200) renderConversations(r.data.items);
}

async function deleteConversation(id) {
  if (!confirm('确定删除这条对话记录吗？')) return;
  const r = await api('/api/conversations/' + id, { method: 'DELETE' });
  if (r.code !== 200) { toast(r.message, true); return; }
  if (currentConversationId === id) {
    currentConversationId = null;
    clearMessages();
  }
  loadConversations();
}

async function loadDepartments() {
  const r = await api('/api/departments');
  if (r.code !== 200) return;
  departmentsData = r.data;

  const up = $('uploadDept');
  const upCurrent = up.value;
  up.innerHTML = '';
  r.data.items.forEach(d => {
    const opt = document.createElement('option');
    opt.value = d.folder;
    opt.textContent = d.label;
    up.appendChild(opt);
  });
  if (Array.from(up.options).some(o => o.value === upCurrent)) up.value = upCurrent;

  const nd = $('newDept');
  const ndCurrent = nd.value;
  nd.innerHTML = '<option value="">公开</option>';
  r.data.items.filter(d => d.folder !== 'public').forEach(d => {
    const opt = document.createElement('option');
    opt.value = d.label;
    opt.textContent = d.label;
    nd.appendChild(opt);
  });
  if (Array.from(nd.options).some(o => o.value === ndCurrent)) nd.value = ndCurrent;

  const dd = $('docDept');
  const ddCurrent = dd.value;
  dd.innerHTML = '<option value="">全部部门</option>';
  r.data.items.forEach(d => {
    const opt = document.createElement('option');
    opt.value = d.folder;
    opt.textContent = d.label;
    dd.appendChild(opt);
  });
  dd.value = ddCurrent;

  const ud = $('userDept');
  const udCurrent = ud.value;
  ud.innerHTML = '<option value="">全部部门</option>';
  r.data.items.forEach(d => {
    const opt = document.createElement('option');
    opt.value = d.label;
    opt.textContent = d.label;
    ud.appendChild(opt);
  });
  ud.value = udCurrent;
}

let rolesData = [];
const ROLE_LABELS = { user: '只读用户', admin: '管理员' };

async function loadRoles() {
  const r = await api('/api/roles');
  if (r.code !== 200) return;
  rolesData = r.data.items;
  const selects = [$('newRole')].concat(
    Array.from($('userTable').querySelectorAll('select.user-role'))
  );
  selects.forEach(sel => {
    if (!sel) return;
    const current = sel.value;
    sel.innerHTML = rolesData.map(role =>
      '<option value="' + esc(role) + '">' + esc(ROLE_LABELS[role] || role) + '</option>'
    ).join('');
    if (rolesData.includes(current)) sel.value = current;
  });
}

function roleOptionsHTML(current) {
  if (!rolesData.length) return '<option value="' + esc(current) + '">' + esc(current) + '</option>';
  return rolesData.map(role =>
    '<option value="' + esc(role) + '"' + (role === current ? ' selected' : '') + '>' +
    esc(ROLE_LABELS[role] || role) + '</option>'
  ).join('');
}

function deptOptionsHTML(current) {
  let html = '<option value=""' + (current === '' ? ' selected' : '') + '>公开</option>';
  departmentsData.items.filter(d => d.folder !== 'public').forEach(d => {
    const sel = current === d.label ? ' selected' : '';
    html += '<option value="' + esc(d.label) + '"' + sel + '>' + esc(d.label) + '</option>';
  });
  const labels = departmentsData.items.filter(d => d.folder !== 'public').map(d => d.label);
  if (current && !labels.includes(current)) {
    html += '<option value="' + esc(current) + '" selected>' + esc(current) + '</option>';
  }
  return html;
}

function appendMessage(role, text) {
  $('welcome')?.remove();
  const wrap = document.createElement('div');
  wrap.className = 'msg ' + role;
  const avatar = document.createElement('div');
  avatar.className = 'avatar';
  avatar.textContent = role === 'user' ? '我' : 'AI';
  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  bubble.textContent = text;
  wrap.appendChild(avatar);
  wrap.appendChild(bubble);
  $('messagesInner').appendChild(wrap);
  scrollBottom();
  return bubble;
}

function scrollBottom() {
  const el = $('messages');
  el.scrollTop = el.scrollHeight;
}

function setBusy(v) {
  busy = v;
  $('sendBtn').disabled = v;
}

async function sendQuestion() {
  if (busy) return;
  const input = $('question');
  const question = input.value.trim();
  if (!question) return;

  appendMessage('user', question);
  input.value = '';
  autoResize(input);
  setBusy(true);

  const bubble = appendMessage('assistant', '');
  bubble.innerHTML = '<span class="typing"><span></span><span></span><span></span></span>';

  try {
    if (!currentConversationId) {
      const created = await api('/api/conversations', { method: 'POST' });
      if (created.code !== 200) throw new Error(created.message);
      currentConversationId = created.data.id;
    }

    const token = localStorage.getItem('token');
    const res = await fetch('/api/ask/stream', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer ' + token,
      },
      body: JSON.stringify({ question, conversation_id: currentConversationId }),
    });

    const contentType = res.headers.get('content-type') || '';
    if (!res.ok || !contentType.includes('text/event-stream')) {
      const data = await res.json().catch(() => ({}));
      if (data.code === 401) {
        localStorage.removeItem('token');
        localStorage.removeItem('refresh_token');
        showLogin();
        return;
      }
      if (data.code === 429) {
        bubble.textContent = data.message || '提问过于频繁，请稍后再试';
        return;
      }
      throw new Error(data.message || ('请求失败：' + res.status));
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buffer = '';
    let answer = '';
    let started = false;
    let streamError = '';

    const handleEvent = evt => {
      if (!evt || typeof evt.type !== 'string') return;
      if (evt.type === 'delta' && evt.content) {
        if (!started) { bubble.textContent = ''; started = true; }
        answer += evt.content;
        bubble.textContent = answer;
        scrollBottom();
      } else if (evt.type === 'error' || evt.type === 'warning') {
        streamError = evt.message || '';
      }
    };

    while (true) {
      const { value, done } = await reader.read();
      if (done) { buffer += decoder.decode(); break; }
      buffer += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buffer.indexOf('\n\n')) !== -1) {
        const block = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        const line = block.split('\n').find(l => l.startsWith('data:'));
        if (!line) continue;
        try { handleEvent(JSON.parse(line.slice(5).trim())); } catch (_) {}
      }
    }

    if (streamError) {
      answer += (answer ? '\n\n' : '') + '（' + streamError + '）';
      bubble.textContent = answer;
    }
    if (!answer) bubble.textContent = '未收到回答';
    loadConversations();
  } catch (e) {
    bubble.textContent = '网络错误，请确认服务已启动';
  } finally {
    setBusy(false);
    input.focus();
    scrollBottom();
  }
}

function onKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendQuestion();
  }
}

function autoResize(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 160) + 'px';
}

async function loadDocs(page) {
  const p = page || 1;
  const keyword = ($('docKeyword')?.value || '').trim();
  const dept = ($('docDept')?.value || '');
  const params = '?keyword=' + encodeURIComponent(keyword) +
    '&department=' + encodeURIComponent(dept) +
    '&page=' + p + '&page_size=' + PAGE_SIZE;
  const r = await api('/api/docs' + params);
  const table = $('docTable');
  if (r.code !== 200) {
    table.innerHTML = '';
    $('docCount').textContent = '';
    $('docPager').innerHTML = '';
    return;
  }
  const filtered = keyword || dept;
  $('docCount').textContent = '共 ' + r.data.total + ' 个文档' + (filtered ? '（已筛选）' : '');
  if (!r.data.items.length) {
    table.innerHTML = '<tr><td class="note">' + (filtered ? '没有匹配的文档' : '暂无文档') + '</td></tr>';
    $('docPager').innerHTML = '';
    return;
  }
  table.innerHTML =
    '<tr><th>文件名</th><th>部门</th><th>大小</th><th>修改时间</th>' + (isAdmin ? '<th>操作</th>' : '') + '</tr>' +
    r.data.items.map(d =>
      '<tr><td>' + esc(d.filename) + '</td>' +
      '<td>' + esc(d.department_label) + '</td>' +
      '<td>' + (d.size / 1024).toFixed(1) + ' KB</td>' +
      '<td>' + esc((d.modified_at || '').replace('T', ' ').slice(0, 19)) + '</td>' +
      (isAdmin ? '<td><button class="mini-btn" onclick="deleteDoc(\'' + esc(d.path).replace(/'/g, "\\'") + '\')">删除</button></td>' : '') +
      '</tr>'
    ).join('');
  renderPager('docPager', 'docs', r.data, 'loadDocs');
}

function reloadDocs() { loadDocs(1); }

async function deleteDoc(path) {
  if (!confirm('确定删除文档「' + path + '」吗？删除后索引会同步更新。')) return;
  const r = await api('/api/docs?path=' + encodeURIComponent(path), { method: 'DELETE' });
  toast(r.code === 200 ? '文档已删除' : r.message, r.code !== 200);
  if (r.code === 200) { loadDocs(); loadStatus(); }
}

function openUploadModal(file) {
  pendingFile = file || null;
  const pick = $('filePick');
  if (pendingFile) {
    pick.textContent = pendingFile.name;
    pick.classList.add('has-file');
  } else {
    pick.textContent = '点击选择文件';
    pick.classList.remove('has-file');
  }
  $('uploadOverlay').style.display = 'flex';
}

function closeUploadModal() {
  $('uploadOverlay').style.display = 'none';
  pendingFile = null;
}

function onModalFilePicked(input) {
  const file = input.files[0];
  input.value = '';
  if (file) openUploadModal(file);
}

async function confirmUpload() {
  if (!pendingFile) { toast('请先选择文件', true); return; }
  const department = $('uploadDept').value || 'public';
  const form = new FormData();
  form.append('file', pendingFile);
  form.append('department', department);
  toast('正在上传 ' + pendingFile.name + ' ...');
  try {
    const r = await api('/api/docs/upload', { method: 'POST', body: form });
    if (r.code === 200) {
      toast(r.data.status + '：' + r.data.filename + ' → ' + (departmentsData.items.find(d => d.folder === department)?.label || department));
      closeUploadModal();
      loadStatus();
      loadDocs();
    } else {
      toast(r.message, true);
    }
  } catch (e) {
    toast('上传失败：' + e.message, true);
  }
}

async function rebuildAll() {
  if (busy) return;
  if (!confirm('确定要重新切片所有文档吗？')) return;
  setBusy(true);
  toast('正在重建索引 ...');
  try {
    const r = await api('/api/docs/rebuild', { method: 'POST' });
    toast(r.code === 200 ? ('重建完成，共 ' + r.data.vector_count + ' 个向量') : r.message, r.code !== 200);
    if (r.code === 200) loadStatus();
  } catch (e) {
    toast('重建失败：' + e.message, true);
  } finally {
    setBusy(false);
  }
}

const bigDrop = $('bigDrop');
['dragenter', 'dragover'].forEach(evt =>
  bigDrop.addEventListener(evt, e => { e.preventDefault(); bigDrop.classList.add('dragover'); })
);
['dragleave', 'drop'].forEach(evt =>
  bigDrop.addEventListener(evt, e => { e.preventDefault(); bigDrop.classList.remove('dragover'); })
);
bigDrop.addEventListener('drop', e => {
  const file = e.dataTransfer.files[0];
  if (file) openUploadModal(file);
});

function showLogin() {
  $('loginPage').style.display = 'flex';
  $('app').style.display = 'none';
}
function hideLogin() {
  $('loginPage').style.display = 'none';
  $('app').style.display = 'flex';
}

async function initAuth() {
  const r = await api('/api/auth/me');
  if (r.code !== 200) { showLogin(); return; }
  currentUser = r.data;
  $('currentUser').textContent = r.data.username;
  const perms = r.data.permissions || [];
  isAdmin = perms.includes('user:manage');
  $('navDocs').style.display = perms.includes('doc:upload') ? '' : 'none';
  $('docsUploadArea').style.display = perms.includes('doc:upload') ? '' : 'none';
  $('navRebuild').style.display = perms.includes('doc:rebuild') ? '' : 'none';
  $('navUsers').style.display = perms.includes('user:manage') ? '' : 'none';
  $('navAudit').style.display = perms.includes('user:manage') ? '' : 'none';
  hideLogin();
  loadStatus();
  await loadDepartments();
  if (perms.includes('user:manage')) await loadRoles();

  const convs = await api('/api/conversations');
  if (convs.code === 200) renderConversations(convs.data.items);
  if (convs.code === 200 && convs.data.items.length) {
    await openConversation(convs.data.items[0].id);
  } else {
    await newChat();
  }
}

async function login() {
  const username = $('loginUser').value.trim();
  const password = $('loginPass').value;
  $('loginError').textContent = '';
  try {
    const r = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    }).then(res => res.json());
    if (r.code === 200) {
      localStorage.setItem('token', r.data.access_token);
      localStorage.setItem('refresh_token', r.data.refresh_token);
      initAuth();
    } else {
      $('loginError').textContent = r.message;
    }
  } catch (e) {
    $('loginError').textContent = '无法连接服务';
  }
}

async function logout() {
  try { await api('/api/auth/logout', { method: 'POST' }); } catch (e) {}
  localStorage.removeItem('token');
  localStorage.removeItem('refresh_token');
  currentUser = null;
  currentConversationId = null;
  $('convList').innerHTML = '';
  clearMessages();
  showLogin();
}

async function loadUsers(page) {
  if (!rolesData.length) await loadRoles();
  const p = page || 1;
  const kw = $('userKeyword') ? $('userKeyword').value.trim() : '';
  const dept = $('userDept') ? $('userDept').value : '';
  const params = '?keyword=' + encodeURIComponent(kw) +
    '&department=' + encodeURIComponent(dept) +
    '&page=' + p + '&page_size=' + PAGE_SIZE;
  const r = await api('/api/users' + params);
  const table = $('userTable');
  if (r.code !== 200) {
    table.innerHTML = '';
    $('userCount').textContent = '';
    $('userPager').innerHTML = '';
    return;
  }
  $('userCount').textContent = '共 ' + r.data.total + ' 个用户';
  if (!r.data.items.length) {
    table.innerHTML = '<tr><td class="note">没有匹配的用户</td></tr>';
    $('userPager').innerHTML = '';
    return;
  }
  table.innerHTML =
    '<tr><th>用户名</th><th>部门</th><th>角色</th><th>状态</th><th>操作</th></tr>' +
    r.data.items.map(u =>
      '<tr><td>' + esc(u.username) + '</td>' +
      '<td><select class="mini-select" onchange="updateUserDept(' + u.id + ', this.value)">' + deptOptionsHTML(u.department || '') + '</select></td>' +
      '<td><select class="mini-select user-role" onchange="updateUserRole(' + u.id + ', this.value)">' +
      roleOptionsHTML((u.roles || [])[0] || 'user') + '</select></td>' +
      '<td>' + (u.is_active ? '正常' : '禁用') + '</td>' +
      '<td><button class="mini-btn" onclick="toggleUser(' + u.id + ',' + !u.is_active + ')">' +
      (u.is_active ? '禁用' : '启用') + '</button>' +
      '<button class="mini-btn" onclick="resetPwd(' + u.id + ')">重置密码</button></td></tr>'
    ).join('');
  renderPager('userPager', 'users', r.data, 'loadUsers');
}

function reloadUsers() { loadUsers(1); }

const ACTION_LABELS = {
  login: '登录', logout: '退出', user_create: '创建用户',
  user_update: '更新用户', password_reset: '重置密码',
  doc_upload: '上传文档', doc_delete: '删除文档',
  doc_rebuild: '重建索引', ask: '问答',
};

async function loadAuditActions() {
  const r = await api('/api/audit-actions');
  if (r.code !== 200) return;
  const sel = $('auditAction');
  const current = sel.value;
  sel.innerHTML = '<option value="">全部操作</option>' + r.data.items.map(a =>
    '<option value="' + esc(a) + '">' + esc(ACTION_LABELS[a] || a) + '</option>'
  ).join('');
  if (Array.from(sel.options).some(o => o.value === current)) sel.value = current;
}

async function loadAuditLogs(page) {
  const p = page || 1;
  const kw = $('auditKeyword') ? $('auditKeyword').value.trim() : '';
  const action = $('auditAction') ? $('auditAction').value.trim() : '';
  const params = '?keyword=' + encodeURIComponent(kw) +
    '&action=' + encodeURIComponent(action) +
    '&page=' + p + '&page_size=' + PAGE_SIZE;
  const r = await api('/api/audit-logs' + params);
  const table = $('auditTable');
  if (r.code !== 200) {
    table.innerHTML = '';
    $('auditCount').textContent = '';
    $('auditPager').innerHTML = '';
    return;
  }
  $('auditCount').textContent = '共 ' + r.data.total + ' 条';
  if (!r.data.items.length) {
    table.innerHTML = '<tr><td class="note">没有匹配的日志</td></tr>';
    $('auditPager').innerHTML = '';
    return;
  }
  table.innerHTML =
    '<tr><th>时间</th><th>用户</th><th>操作</th><th>详情</th><th>IP</th></tr>' +
    r.data.items.map(log =>
      '<tr><td>' + esc((log.created_at || '').replace('T', ' ').slice(0, 19)) + '</td>' +
      '<td>' + esc(log.username || '-') + '</td>' +
      '<td>' + esc(ACTION_LABELS[log.action] || log.action) + '</td>' +
      '<td>' + esc(log.detail || '-') + '</td>' +
      '<td>' + esc(log.ip || '-') + '</td></tr>'
    ).join('');
  renderPager('auditPager', 'audit', r.data, 'loadAuditLogs');
}

function reloadAuditLogs() { loadAuditLogs(1); }

function openUserModal() {
  $('newUsername').value = '';
  $('newPassword').value = '';
  $('newDept').value = '';
  $('newRole').value = 'user';
  $('userOverlay').style.display = 'flex';
  $('newUsername').focus();
}

function closeUserModal() {
  $('userOverlay').style.display = 'none';
}

async function createUser() {
  const username = $('newUsername').value.trim();
  const password = $('newPassword').value;
  const department = $('newDept').value;
  const role = $('newRole').value;
  if (!username || password.length < 8) { toast('用户名必填，密码至少 8 位', true); return; }
  const r = await api('/api/users', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password, department, role }),
  });
  toast(r.code === 200 ? ('已创建 ' + username) : r.message, r.code !== 200);
  if (r.code === 200) {
    closeUserModal();
    loadUsers();
  }
}

async function updateUserRole(id, role) {
  const r = await api('/api/users/' + id, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ role }),
  });
  toast(r.code === 200 ? '角色已更新' : r.message, r.code !== 200);
  if (r.code !== 200) loadUsers();
}

async function updateUserDept(id, dept) {
  const r = await api('/api/users/' + id, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ department: dept }),
  });
  toast(r.code === 200 ? '部门已更新' : r.message, r.code !== 200);
  if (r.code !== 200) loadUsers();
}

async function toggleUser(id, active) {
  const r = await api('/api/users/' + id, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ is_active: active }),
  });
  toast(r.code === 200 ? '已更新' : r.message, r.code !== 200);
  if (r.code === 200) loadUsers();
}

async function resetPwd(id) {
  const password = prompt('新密码（至少 8 位）');
  if (!password) return;
  const r = await api('/api/users/' + id + '/password', {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ password }),
  });
  toast(r.code === 200 ? '密码已重置' : r.message, r.code !== 200);
}

if (localStorage.getItem('token')) {
  initAuth();
} else {
  showLogin();
}
