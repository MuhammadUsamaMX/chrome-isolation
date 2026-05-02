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
function renderProfiles(profiles) {
  const grid = document.getElementById('profilesGrid');
  const empty = document.getElementById('emptyState');
  const loading = document.getElementById('loadingState');
  if (loading) loading.style.display = 'none';

  // Remove all existing cards (not the empty/loading sentinels)
  grid.querySelectorAll('.profile-card').forEach(c => c.remove());

  if (!profiles.length) {
    empty.style.display = 'flex';
    return;
  }
  empty.style.display = 'none';

  profiles.forEach(p => {
    const card = document.createElement('div');
    card.className = 'profile-card';
    card.dataset.name = p.name;

    const status = p.status || 'not_found';
    const label  = STATUS_LABEL[status] || status;
    const isRunning = status === 'running';

    card.innerHTML = `
      <div class="card-header">
        <span class="card-name">${esc(p.name)}</span>
        <span class="badge badge-${esc(status)}">${esc(label)}</span>
      </div>
      <div class="card-meta">
        <span>Storage: ${p.size_mb ?? 0} MB</span>
        <span>Desktop: ${p.has_desktop_entry ? 'Yes' : 'No'}</span>
        ${p.created_at ? `<span>Created: ${esc(p.created_at.replace('T',' ').replace('Z',''))}</span>` : ''}
      </div>
      <div class="card-actions">
        ${isRunning
          ? `<button class="btn btn-danger btn-sm"   data-action="stop">Stop</button>`
          : `<button class="btn btn-success btn-sm"  data-action="start">Launch</button>`}
        <button class="btn btn-secondary btn-sm" data-action="export">Export</button>
        <button class="btn btn-danger btn-sm"    data-action="delete">Delete</button>
      </div>
    `;

    card.querySelectorAll('[data-action]').forEach(btn => {
      btn.addEventListener('click', () => handleAction(btn.dataset.action, p.name));
    });

    grid.appendChild(card);
  });
}

// ── Load profiles ─────────────────────────────────────────────────────────────
async function loadProfiles() {
  try {
    const profiles = await window.api.profiles.list();
    renderProfiles(Array.isArray(profiles) ? profiles : []);
  } catch (e) {
    toast(`Failed to load profiles: ${e.message}`, 'error');
  }
}

// ── Action dispatcher ─────────────────────────────────────────────────────────
async function handleAction(action, name) {
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
  if (!name) return;
  try {
    await window.api.profiles.create(name, path);
    toast(`Profile "${name}" created.`, 'success');
    document.getElementById('modalCreate').style.display = 'none';
    document.getElementById('createForm').reset();
    await loadProfiles();
  } catch (e) {
    toast(`Failed to create: ${e.message}`, 'error');
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
