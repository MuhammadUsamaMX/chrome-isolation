'use strict';
// ─── Renderer app — talks to backend via window.api (contextBridge) ──────────

const STATUS_LABEL = {
  running:   'Running',
  exited:    'Stopped',
  not_found: 'Not Started',
};

// ── Toast ─────────────────────────────────────────────────────────────────────
function toast(msg, type = 'info', ms = 4000) {
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  el.textContent = msg;
  document.getElementById('toasts').appendChild(el);
  setTimeout(() => el.remove(), ms);
}

// ── Confirm dialog (uses the modal) ──────────────────────────────────────────
function confirm(title, msg) {
  return new Promise((resolve) => {
    document.getElementById('confirmTitle').textContent = title;
    document.getElementById('confirmMsg').textContent = msg;
    const overlay = document.getElementById('modalConfirm');
    overlay.style.display = 'flex';

    const yes = document.getElementById('btnConfirmYes');
    const no  = document.getElementById('btnConfirmNo');

    function cleanup(val) {
      overlay.style.display = 'none';
      yes.replaceWith(yes.cloneNode(true));
      no.replaceWith(no.cloneNode(true));
      resolve(val);
    }
    document.getElementById('btnConfirmYes').addEventListener('click', () => cleanup(true), { once: true });
    document.getElementById('btnConfirmNo').addEventListener('click',  () => cleanup(false), { once: true });
  });
}

// ── Escape HTML ───────────────────────────────────────────────────────────────
function esc(s) {
  return String(s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ── Render profile cards ──────────────────────────────────────────────────────
let lastProfiles = [];
let currentFilter = 'all'; // all | running | stopped

function renderProfiles(profiles) {
  const grid = document.getElementById('profilesGrid');
  const empty = document.getElementById('emptyState');
  const loading = document.getElementById('loadingState');
  if (loading) loading.style.display = 'none';

  // Remove all existing cards (not the empty/loading sentinels)
  grid.querySelectorAll('.profile-card').forEach(c => c.remove());

  const query = (document.getElementById('inputSearch').value || '').toLowerCase();
  let filtered = query
    ? profiles.filter(p => p.name.toLowerCase().includes(query))
    : profiles;
  if (currentFilter === 'running') filtered = filtered.filter(p => p.status === 'running');
  else if (currentFilter === 'stopped') filtered = filtered.filter(p => p.status !== 'running');

  if (!filtered.length) {
    empty.style.display = 'flex';
    empty.querySelector('p').textContent = query
      ? `No profiles match "${query}"`
      : 'No profiles yet. Create one to get started.';
    return;
  }
  empty.style.display = 'none';

  // Storage bar scale — relative to the largest profile in the current view
  const maxSize = Math.max(1, ...filtered.map(p => p.size_mb || 0));

  filtered.forEach((p, i) => {
    const card = document.createElement('div');
    card.className = 'profile-card';
    card.dataset.name = p.name;
    card.style.animationDelay = `${Math.min(i * 40, 400)}ms`;

    const status = p.status || 'not_found';
    const label  = STATUS_LABEL[status] || status;
    const isRunning = status === 'running';
    const sizeMb = p.size_mb || 0;
    const barPct = Math.min(100, Math.max(3, (sizeMb / maxSize) * 100));

    // Machine identity chips from the profile's hardware signature
    const m = p.machine || {};
    const chips = [
      m.cpu_cores ? `${m.cpu_cores} cores` : null,
      m.language || null,
      m.timezone ? m.timezone.split('/').pop().replace(/_/g, ' ') : null,
      m.gpu_mode || null,
    ].filter(Boolean);

    card.innerHTML = `
      <div class="card-header">
        <span class="card-name">${esc(p.name)}</span>
        <span class="badge badge-${esc(status)}">${esc(label)}</span>
      </div>
      ${chips.length ? `<div class="card-chips">${chips.map(c => `<span class="chip">${esc(c)}</span>`).join('')}</div>` : ''}
      <div class="card-meta">
        <span>Host: ${p.host_mount ? esc(p.host_mount) : '~'}</span>
        <span>Proxy: ${p.proxy ? esc(p.proxy) : 'None'}</span>
        ${p.created_at ? `<span>Created: ${esc(p.created_at.replace('T',' ').replace('Z',''))}</span>` : ''}
      </div>
      <div class="storage-bar" title="${sizeMb} MB">
        <div class="storage-fill" style="width:${barPct}%"></div>
      </div>
      <div class="card-actions">
        ${isRunning
          ? `<button class="btn btn-danger btn-sm"   data-action="stop">Stop</button>`
          : `<button class="btn btn-success btn-sm"  data-action="start">Launch</button>`}
        <button class="btn btn-secondary btn-sm" data-action="edit">Edit</button>
        <button class="btn btn-secondary btn-sm" data-action="export">Export</button>
        <button class="btn btn-danger btn-sm"    data-action="delete">Delete</button>
      </div>
    `;

    card.querySelectorAll('[data-action]').forEach(btn => {
      btn.addEventListener('click', () => handleAction(btn.dataset.action, p));
    });

    grid.appendChild(card);
  });
}

// ── Dashboard stats ───────────────────────────────────────────────────────────
function renderStats(profiles) {
  const total = profiles.length;
  const running = profiles.filter(p => p.status === 'running').length;
  const stopped = profiles.filter(p => p.status === 'exited').length;
  const storage = profiles.reduce((s, p) => s + (p.size_mb || 0), 0);

  document.getElementById('statTotal').textContent = total;
  document.getElementById('statRunning').textContent = running;
  document.getElementById('statStopped').textContent = stopped;
  document.getElementById('statStorage').textContent =
    storage >= 1024 ? `${(storage / 1024).toFixed(1)} GB` : `${Math.round(storage)} MB`;
  document.getElementById('navCountAll').textContent = total;
  document.getElementById('navCountRunning').textContent = running;
  document.getElementById('navCountStopped').textContent = total - running;
  document.getElementById('headerSub').textContent =
    total ? `${running} running · ${total} total` : 'Isolated browser machines';
}

// ── Sidebar connection status ─────────────────────────────────────────────────
function setStatus(ok) {
  const dot = document.getElementById('statusDot');
  const text = document.getElementById('statusText');
  dot.className = `status-dot ${ok ? 'on' : 'off'}`;
  text.textContent = ok ? 'Connected' : 'Offline';
}

// ── Load profiles ─────────────────────────────────────────────────────────────
async function loadProfiles() {
  try {
    const profiles = await window.api.profiles.list();
    lastProfiles = Array.isArray(profiles) ? profiles : [];
    setStatus(true);
    renderStats(lastProfiles);
    renderProfiles(lastProfiles);
  } catch (e) {
    setStatus(false);
    toast(`Failed to load profiles: ${e.message}`, 'error');
  }
}

// ── Action dispatcher ─────────────────────────────────────────────────────────
async function handleAction(action, profile) {
  const name = profile.name;
  try {
    switch (action) {
      case 'start': {
        const r = await window.api.profiles.start(name);
        if (r && r.status === 'already_running') toast(`"${name}" is already running.`, 'info');
        else toast(`"${name}" launched.`, 'success');
        break;
      }
      case 'stop': {
        await window.api.profiles.stop(name);
        toast(`"${name}" stopped.`, 'info');
        break;
      }
      case 'export': {
        const r = await window.api.profiles.export(name);
        if (r && !r.canceled) toast(`Exported to ${r.path}`, 'success');
        break;
      }
      case 'edit': {
        openEdit(profile);
        return; // no reload needed
      }
      case 'delete': {
        const ok = await confirm('Delete Profile', `Delete "${name}" and all its data? This cannot be undone.`);
        if (!ok) return;
        await window.api.profiles.delete(name);
        toast(`"${name}" deleted.`, 'info');
        break;
      }
    }
    await loadProfiles();
  } catch (e) {
    toast(`Error: ${e.message}`, 'error');
  }
}

// ── Create modal ──────────────────────────────────────────────────────────────
document.getElementById('btnCreate').addEventListener('click', () => {
  document.getElementById('modalCreate').style.display = 'flex';
  document.getElementById('inputName').focus();
});

document.getElementById('btnCreateCancel').addEventListener('click', () => {
  document.getElementById('modalCreate').style.display = 'none';
  document.getElementById('createForm').reset();
});

document.getElementById('createForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const name = document.getElementById('inputName').value.trim();
  const path = document.getElementById('inputPath').value.trim();
  const hostMount = document.getElementById('inputHostMount').value.trim();
  const proxy = document.getElementById('inputProxy').value.trim();
  if (!name) return;
  try {
    await window.api.profiles.create(name, path, hostMount, proxy);
    toast(`Profile "${name}" created.`, 'success');
    document.getElementById('modalCreate').style.display = 'none';
    document.getElementById('createForm').reset();
    await loadProfiles();
  } catch (e) {
    toast(`Failed to create: ${e.message}`, 'error');
  }
});

// ── Browse (native folder picker) ─────────────────────────────────────────────
async function browseFolder(inputId) {
  try {
    const r = await window.api.profiles.pickFolder();
    if (r && !r.canceled) document.getElementById(inputId).value = r.path;
  } catch (e) {
    toast(`Folder picker failed: ${e.message}`, 'error');
  }
}
document.getElementById('btnBrowseCreate').addEventListener('click', () => browseFolder('inputHostMount'));
document.getElementById('btnBrowseEdit').addEventListener('click', () => browseFolder('editHostMount'));

// ── Edit modal ────────────────────────────────────────────────────────────────
let editingName = null;

function openEdit(profile) {
  editingName = profile.name;
  document.getElementById('editTitle').textContent = `Edit ${profile.name}`;
  document.getElementById('editHostMount').value = profile.host_mount || '';
  document.getElementById('editProxy').value = profile.proxy || '';
  document.getElementById('modalEdit').style.display = 'flex';
}

document.getElementById('btnEditCancel').addEventListener('click', () => {
  document.getElementById('modalEdit').style.display = 'none';
  editingName = null;
});

document.getElementById('editForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  if (!editingName) return;
  const hostMount = document.getElementById('editHostMount').value.trim();
  const proxy = document.getElementById('editProxy').value.trim();
  try {
    await window.api.profiles.update(editingName, hostMount, proxy);
    toast(`"${editingName}" updated.`, 'success');
    document.getElementById('modalEdit').style.display = 'none';
    editingName = null;
    await loadProfiles();
  } catch (err) {
    toast(`Update failed: ${err.message}`, 'error');
  }
});

// ── Import ────────────────────────────────────────────────────────────────────
document.getElementById('btnImport').addEventListener('click', async () => {
  try {
    const r = await window.api.profiles.import();
    if (r && !r.canceled) {
      toast(`Profile "${r.name}" imported.`, 'success');
      await loadProfiles();
    }
  } catch (e) {
    toast(`Import failed: ${e.message}`, 'error');
  }
});

// ── Refresh ───────────────────────────────────────────────────────────────────
document.getElementById('btnRefresh').addEventListener('click', loadProfiles);

// ── Search (filters the cached list locally, no backend round-trip) ──────────
document.getElementById('inputSearch').addEventListener('input', () => {
  renderProfiles(lastProfiles);
});

// ── Sidebar filters (All / Running / Stopped) ─────────────────────────────────
document.querySelectorAll('.nav-item[data-filter]').forEach(btn => {
  btn.addEventListener('click', () => {
    currentFilter = btn.dataset.filter;
    document.querySelectorAll('.nav-item').forEach(b => b.classList.toggle('active', b === btn));
    renderProfiles(lastProfiles);
  });
});

// ── Auto-refresh every 5 s ────────────────────────────────────────────────────
setInterval(loadProfiles, 5000);

// ── Theme toggle ──────────────────────────────────────────────────────────────
(function initTheme() {
  const saved = localStorage.getItem('theme') || 'dark';
  applyTheme(saved);
})();

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme === 'light' ? 'light' : '';
  document.getElementById('iconMoon').style.display = theme === 'light' ? 'none'  : '';
  document.getElementById('iconSun').style.display  = theme === 'light' ? '' : 'none';
  localStorage.setItem('theme', theme);
}

document.getElementById('btnTheme').addEventListener('click', () => {
  const current = localStorage.getItem('theme') || 'dark';
  applyTheme(current === 'dark' ? 'light' : 'dark');
});

// ── Window controls ───────────────────────────────────────────────────────────
document.getElementById('btnWinMin').addEventListener('click',   () => window.api.win.minimize());
document.getElementById('btnWinMax').addEventListener('click',   () => window.api.win.maximize());
document.getElementById('btnWinClose').addEventListener('click', () => window.api.win.close());

// Sync maximize button icon with actual state
window.api.win.isMaximized().then(v => {
  document.getElementById('btnWinMax').classList.toggle('is-maximized', v);
});
window.api.win.onMaximizeChange(isMax => {
  document.getElementById('btnWinMax').classList.toggle('is-maximized', isMax);
});

// ── Initial load ──────────────────────────────────────────────────────────────
loadProfiles();
