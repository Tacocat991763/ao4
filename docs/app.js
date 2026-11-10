/* AO4 — shared API client and utilities */

const DEFAULT_API = 'http://localhost:8000';

function getApiUrl() {
  try { return (localStorage.getItem('ao4_api') || DEFAULT_API).replace(/\/$/, ''); }
  catch { return DEFAULT_API; }
}

function setApiUrl(url) {
  try { localStorage.setItem('ao4_api', url.replace(/\/$/, '')); } catch {}
}

// ---- Auth helpers ----

function getToken() {
  try { return localStorage.getItem('ao4_token') || ''; } catch { return ''; }
}

function setToken(t) {
  try { localStorage.setItem('ao4_token', t); } catch {}
}

function getStoredUser() {
  try { return JSON.parse(localStorage.getItem('ao4_user') || 'null'); } catch { return null; }
}

function setStoredUser(u) {
  try { localStorage.setItem('ao4_user', JSON.stringify(u)); } catch {}
}

function clearAuth() {
  try { localStorage.removeItem('ao4_token'); localStorage.removeItem('ao4_user'); } catch {}
}

async function apiFetch(path, opts = {}) {
  const token = getToken();
  const headers = { ...(opts.headers || {}) };
  if (token) headers['Authorization'] = 'Bearer ' + token;
  const r = await fetch(getApiUrl() + path, { ...opts, headers });
  if (!r.ok) {
    const e = await r.json().catch(() => ({}));
    throw new Error(e.detail || `HTTP ${r.status}`);
  }
  if (r.status === 204) return null;
  return r.json();
}

// ---- Formatting helpers ----

function fmt(n) {
  if (n == null) return '0';
  return Number(n).toLocaleString();
}

function fmtDate(iso) {
  if (!iso) return '';
  return new Date(iso).toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' });
}

function getParam(name) {
  return new URLSearchParams(location.search).get(name) || '';
}

function esc(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// ---- HTML builders ----

function ratingBadge(r) {
  const labels = { G: 'G', T: 'T', M: 'M', E: 'E' };
  const titles = { G: 'General Audiences', T: 'Teen & Up', M: 'Mature', E: 'Explicit' };
  const cls = labels[r] ? `rating-${r}` : 'rating-NR';
  return `<span class="rating-badge ${cls}" title="${titles[r] || 'Not Rated'}">${labels[r] || 'NR'}</span>`;
}

function tagLink(name, category, param) {
  const cls = category || 'freeform';
  const url = `browse.html?${param || 'q'}=${encodeURIComponent(name)}`;
  return `<a href="${url}" class="tag ${cls}">${esc(name)}</a>`;
}

function buildWorkCard(w) {
  const fandoms = (w.fandoms || [])
    .map(f => `<a href="browse.html?fandom=${encodeURIComponent(f)}" class="fandom-link">${esc(f)}</a>`)
    .join(', ');

  const ships  = (w.relationships || []).map(t => tagLink(t, 'relationship', 'q')).join(' ');
  const chars  = (w.characters   || []).map(t => tagLink(t, 'character',    'q')).join(' ');
  const extras = (w.tags         || []).map(t => tagLink(t, 'freeform',     'q')).join(' ');
  const allTags = [ships, chars, extras].filter(Boolean).join(' ');

  return `
  <li class="work-item">
    <div class="work-rating-col">${ratingBadge(w.rating)}</div>
    <div class="work-body">
      <div class="work-title-line"><a href="work.html?id=${esc(w.id)}">${esc(w.title)}</a></div>
      <div class="work-author">by <a href="browse.html?q=${encodeURIComponent(w.author)}">${esc(w.author)}</a></div>
      ${fandoms ? `<div class="work-fandoms"><strong>Fandoms:</strong> ${fandoms}</div>` : ''}
      ${allTags ? `<div class="work-tags">${allTags}</div>` : ''}
      <div class="work-meta">
        <span>Words: ${fmt(w.word_count)}</span>
        <span>Chapters: ${fmt(w.chapter_count)}</span>
        <span>Posted: ${fmtDate(w.created_at)}</span>
        ${w.updated_at && w.updated_at !== w.created_at ? `<span>Updated: ${fmtDate(w.updated_at)}</span>` : ''}
      </div>
      ${w.summary ? `<div class="work-summary">${esc(w.summary)}</div>` : ''}
    </div>
  </li>`;
}

function renderContent(text) {
  if (!text) return '<p><em>(empty)</em></p>';
  return text
    .split(/\n{2,}/)
    .map(para => para.trim())
    .filter(Boolean)
    .map(para => `<p>${para.replace(/\n/g, '<br>').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}</p>`)
    .join('\n');
}

// ---- Shared header / footer injection ----

const HEADER_HTML = (activePage) => `
<div id="site-header">
  <div class="header-inner">
    <div class="site-logo">
      <a href="index.html" class="site-title">A<span>O</span>4</a>
      <div class="site-tagline">Archive of Our Four — a fan-created, fan-run fanfiction archive</div>
    </div>
    <div class="header-right">
      <form class="header-search" action="browse.html" method="get">
        <input type="text" name="q" placeholder="Search works, authors, tags…">
        <button type="submit">Search</button>
      </form>
      <div id="header-user" class="header-user"></div>
      <button class="btn-api-settings" id="api-settings-btn" title="Configure backend URL" style="display:none;">⚙ API</button>
    </div>
  </div>
</div>
<nav id="site-nav">
  <div class="nav-inner">
    <a href="browse.html" ${activePage === 'browse' ? 'class="active"' : ''}>Browse</a>
    <a href="browse.html" ${activePage === 'search' ? 'class="active"' : ''}>Search</a>
    <a href="index.html#about">About</a>
    <a href="upload.html" class="nav-upload ${activePage === 'upload' ? 'active' : ''}">+ Upload a Work</a>
  </div>
</nav>
<div id="api-banner" class="api-banner"></div>
`;

const FOOTER_HTML = `
<footer id="site-footer">
  <div class="disclaimer">
    <strong>AO4 is not Archive of Our Own (AO3) and is not affiliated with the Organization for Transformative Works (OTW).</strong><br>
    This site was independently created by Finn Cullen following the AO3 shutdown, as a fan-run archive to preserve and host fan-created works.
    All works remain the property of their respective creators.
  </div>
  <nav class="footer-nav">
    <a href="index.html">Home</a>
    <a href="browse.html">Browse</a>
    <a href="upload.html">Upload</a>
    <a href="login.html">Account</a>
  </nav>
  <div class="footer-credit">AO4 v0.2 · Open source fanfiction archive</div>
</footer>
`;

const MODAL_HTML = `
<div class="modal-overlay" id="api-modal">
  <div class="modal">
    <h3>⚙ Backend API Settings</h3>
    <div class="field">
      <label>Backend URL</label>
      <input type="text" id="api-url-input" placeholder="http://192.168.x.x:8000">
      <div class="hint">The address of the machine running <code>python3 main.py</code> on your LAN. Default is localhost:8000.</div>
    </div>
    <div class="modal-buttons">
      <button class="btn-secondary" id="api-cancel-btn">Cancel</button>
      <button class="btn-submit" id="api-save-btn">Save &amp; Reload</button>
    </div>
  </div>
</div>
`;

function updateHeaderUser() {
  const user = getStoredUser();
  const el = document.getElementById('header-user');
  if (!el) return;
  if (user) {
    const adminLink = user.is_admin ? `<a href="admin.html" class="header-admin">Admin</a>` : '';
    el.innerHTML = `${adminLink}<a href="login.html" class="header-username">${esc(user.username)}</a><a href="#" id="logout-link" class="header-logout">Log out</a>`;
    document.getElementById('logout-link')?.addEventListener('click', async (e) => {
      e.preventDefault();
      try { await apiFetch('/auth/logout', { method: 'POST' }); } catch {}
      clearAuth();
      window.location.href = 'login.html';
    });
  } else {
    el.innerHTML = `<a href="login.html" class="header-login">Log in</a>`;
  }
}

function injectShell(activePage) {
  const header = document.getElementById('shell-header');
  const footer = document.getElementById('shell-footer');
  const modal  = document.getElementById('shell-modal');
  if (header) header.innerHTML = HEADER_HTML(activePage);
  if (footer) footer.innerHTML = FOOTER_HTML;
  if (modal)  modal.innerHTML  = MODAL_HTML;

  updateHeaderUser();

  // Show API settings button only for admins
  const apiBtn = document.getElementById('api-settings-btn');
  if (apiBtn && getStoredUser()?.is_admin) apiBtn.style.display = '';

  // Wire API modal
  document.getElementById('api-settings-btn')?.addEventListener('click', () => {
    const m = document.getElementById('api-modal');
    document.getElementById('api-url-input').value = getApiUrl();
    m?.classList.add('open');
  });
  document.getElementById('api-cancel-btn')?.addEventListener('click', () =>
    document.getElementById('api-modal')?.classList.remove('open'));
  document.getElementById('api-save-btn')?.addEventListener('click', () => {
    const url = document.getElementById('api-url-input')?.value?.trim();
    if (url) { setApiUrl(url); location.reload(); }
  });
  document.getElementById('api-modal')?.addEventListener('click', e => {
    if (e.target === document.getElementById('api-modal'))
      document.getElementById('api-modal').classList.remove('open');
  });
}

function showApiBanner(msg, type = 'error') {
  const b = document.getElementById('api-banner');
  if (!b) return;
  b.textContent = msg;
  b.className = `api-banner ${type}`;
}

function hideApiBanner() {
  const b = document.getElementById('api-banner');
  if (b) b.className = 'api-banner';
}
