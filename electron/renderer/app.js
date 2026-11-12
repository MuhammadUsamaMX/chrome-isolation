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

// ── Host folder display ───────────────────────────────────────────────────────
// T1 (F1): an empty host_mount no longer means "your home directory is
// mounted". It means the container gets a private, empty per-profile share
// folder. Never render '~' as the host — that claim is what the old backend
// default actually did, and it is exactly what we no longer do.
function hostMountIsDefault(p) {
  if (typeof p.host_mount_is_default === 'boolean') return p.host_mount_is_default;
  return !String(p.host_mount || '').trim();
}

function hostMountLabel(p) {
  if (hostMountIsDefault(p)) return 'None (private empty folder)';
  return p.host_mount;
}

function hostMountTitle(p) {
  if (hostMountIsDefault(p)) {
    const dir = p.effective_host_mount || p.host_mount_default_hint || '';
    return dir
      ? `No host folder set. The container gets an empty private folder mounted read-only at /home/chrome/host:\n${dir}\nYour home directory is not mounted.`
      : 'No host folder set. The container gets an empty private folder mounted read-only at /home/chrome/host. Your home directory is not mounted.';
  }
  return `Host folder mounted read-only at /home/chrome/host:\n${p.host_mount}`;
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
        <span title="${esc(hostMountTitle(p))}">Host: ${esc(hostMountLabel(p))}</span>
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
  // Reopen shows whatever the user last typed. Escape/overlay dismissal
  // deliberately does NOT clear the form, so opening it again must not either,
  // or the "dismiss without losing work" behaviour would be a lie. The proxy
  // option list is refreshed in case proxies were added since, but the current
  // selection is carried across.
  const sel = document.getElementById('inputProxySelect');
  const kept = sel.value;
  populateProxySelect('inputProxySelect', kept);
  // populateProxySelect only restores a value that still exists as an option
  // (saved proxies can be deleted while this form sits half-filled).
  if (kept === '__custom__') {
    document.getElementById('inputProxyCustom').style.display = '';
  }
  document.getElementById('modalCreate').style.display = 'flex';
  document.getElementById('inputName').focus();
});

// The only dismissals that clear the form: a deliberate submit (handled in the
// submit listener) and this explicitly-labelled Cancel button. Escape and
// overlay clicks preserve the contents — see closeAllModals().
document.getElementById('btnCreateCancel').addEventListener('click', () => {
  document.getElementById('modalCreate').style.display = 'none';
  document.getElementById('createForm').reset();
  document.getElementById('inputProxyCustom').style.display = 'none';
});

// Proxy select -> reveal the custom URL input
function wireProxySelect(selectId, customId) {
  document.getElementById(selectId).addEventListener('change', () => {
    const custom = document.getElementById(customId);
    custom.style.display = document.getElementById(selectId).value === '__custom__' ? '' : 'none';
  });
}
wireProxySelect('inputProxySelect', 'inputProxyCustom');
wireProxySelect('editProxySelect', 'editProxyCustom');

// Read the effective proxy value from a select + custom input pair
function readProxyValue(selectId, customId) {
  const sel = document.getElementById(selectId).value;
  if (sel === '__custom__') return document.getElementById(customId).value.trim();
  return sel; // '' (none) or a saved proxy name
}

document.getElementById('createForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const name = document.getElementById('inputName').value.trim();
  const path = document.getElementById('inputPath').value.trim();
  const hostMount = document.getElementById('inputHostMount').value.trim();
  const proxy = readProxyValue('inputProxySelect', 'inputProxyCustom');
  if (!name) return;
  try {
    await window.api.profiles.create(name, path, hostMount, proxy);
    toast(`Profile "${name}" created.`, 'success');
    document.getElementById('modalCreate').style.display = 'none';
    // Submit is one of the two paths where clearing is unambiguous.
    document.getElementById('createForm').reset();
    document.getElementById('inputProxyCustom').style.display = 'none';
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

// Folder picking is bound to the Browse BUTTONS only.
//
// It used to also be bound to a click on the input itself, back when those
// inputs were `readonly` and could not receive a caret. Now that they are
// editable, a click on the field is a normal text-edit click: it must place the
// caret and select text, not launch a native dialog out from under the user.
// Browse is the convenience; typing and pasting stay available.
document.getElementById('btnBrowsePath')?.addEventListener('click', () => browseFolder('inputPath'));
document.getElementById('btnBrowseCreate')?.addEventListener('click', () => browseFolder('inputHostMount'));
document.getElementById('btnBrowseEdit')?.addEventListener('click', () => browseFolder('editHostMount'));

document.getElementById('btnClearPath')?.addEventListener('click', () => { document.getElementById('inputPath').value = ''; });
document.getElementById('btnClearCreate')?.addEventListener('click', () => { document.getElementById('inputHostMount').value = ''; });
document.getElementById('btnClearEdit')?.addEventListener('click', () => { document.getElementById('editHostMount').value = ''; });

// ── Edit modal ────────────────────────────────────────────────────────────────
let editingName = null;

function openEdit(profile) {
  editingName = profile.name;
  document.getElementById('editTitle').textContent = `Edit ${profile.name}`;
  document.getElementById('editHostMount').value = profile.host_mount || '';
  // Make the empty-means-private-folder semantics explicit where the user
  // sees the cleared field, instead of a placeholder that vanishes on focus.
  if (hostMountIsDefault(profile)) {
    const dir = profile.effective_host_mount || profile.host_mount_default_hint || '';
    document.getElementById('editHostMount').title = dir
      ? `Empty = a private, empty folder mounted read-only at /home/chrome/host.\nCurrently: ${dir}\nYour home directory is NOT mounted.`
      : 'Empty = a private, empty folder mounted read-only at /home/chrome/host. Your home directory is NOT mounted.';
  } else {
    document.getElementById('editHostMount').title =
      `Mounted read-only at /home/chrome/host:\n${profile.host_mount}`;
  }
  document.getElementById('editTimezone').value = (profile.machine && profile.machine.timezone) || '';

  // Proxy: saved name -> select it; custom URL -> Custom… + fill the input
  const proxy = profile.proxy || '';
  const isSaved = savedProxies.some(p => p.name === proxy);
  populateProxySelect('editProxySelect', isSaved ? proxy : '');
  const customInput = document.getElementById('editProxyCustom');
  if (isSaved) {
    customInput.value = '';
    customInput.style.display = 'none';
  } else if (proxy) {
    document.getElementById('editProxySelect').value = '__custom__';
    customInput.value = proxy;
    customInput.style.display = '';
  } else {
    customInput.value = '';
    customInput.style.display = 'none';
  }
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
  const proxy = readProxyValue('editProxySelect', 'editProxyCustom');
  const timezone = document.getElementById('editTimezone').value.trim();
  try {
    await window.api.profiles.update(editingName, hostMount, proxy, timezone);
    toast(`"${editingName}" updated.`, 'success');
    document.getElementById('modalEdit').style.display = 'none';
    editingName = null;
    await loadProfiles();
  } catch (err) {
    toast(`Update failed: ${err.message}`, 'error');
  }
});

// ── Global proxy store ────────────────────────────────────────────────────────
let savedProxies = [];

async function loadProxies() {
  try {
    const list = await window.api.proxies.list();
    savedProxies = Array.isArray(list) ? list : [];
  } catch (e) {
    savedProxies = [];
  }
}

function populateProxySelect(selectId, selected) {
  const sel = document.getElementById(selectId);
  sel.innerHTML = '<option value="">None</option>' +
    savedProxies.map(p => `<option value="${esc(p.name)}">${esc(p.name)}</option>`).join('') +
    '<option value="__custom__">Custom…</option>';
  sel.value = selected;
}

function renderProxyList() {
  const list = document.getElementById('proxyList');
  if (!savedProxies.length) {
    list.innerHTML = '<p class="proxy-empty">No proxies yet. Add one below.</p>';
    return;
  }
  list.innerHTML = savedProxies.map(p => `
    <div class="proxy-row">
      <div class="proxy-info">
        <span class="proxy-name">${esc(p.name)}</span>
        <span class="proxy-url">${esc(p.url)}</span>
      </div>
      <button class="btn btn-danger btn-sm" data-del="${esc(p.name)}">Delete</button>
    </div>
  `).join('');
  list.querySelectorAll('[data-del]').forEach(btn => {
    btn.addEventListener('click', async () => {
      try {
        await window.api.proxies.delete(btn.dataset.del);
        toast(`Proxy "${btn.dataset.del}" deleted.`, 'info');
        await loadProxies();
        renderProxyList();
      } catch (e) {
        toast(`Delete failed: ${e.message}`, 'error');
      }
    });
  });
}

document.getElementById('btnProxies').addEventListener('click', async () => {
  await loadProxies();
  renderProxyList();
  document.getElementById('modalProxies').style.display = 'flex';
});

document.getElementById('btnProxiesClose').addEventListener('click', () => {
  document.getElementById('modalProxies').style.display = 'none';
});

document.getElementById('proxyForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const name = document.getElementById('proxyName').value.trim();
  const url = document.getElementById('proxyUrl').value.trim();
  try {
    await window.api.proxies.add(name, url);
    toast(`Proxy "${name}" added.`, 'success');
    document.getElementById('proxyForm').reset();
    await loadProxies();
    renderProxyList();
  } catch (err) {
    toast(`Add failed: ${err.message}`, 'error');
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

// ═══════════════════════════════════════════════════════════════════
// Chromium update — status surface
//
// THE RULE THIS WHOLE BLOCK EXISTS TO KEEP: `update_available: null`
// and `update_available: false` are DIFFERENT FACTS. false means the
// check ran and the installed version IS current. null means the check
// could not determine — no detector, the network was down, the package
// index was unreadable. Rendering null as "You're up to date" is how a
// browser stays two years stale behind a green indicator, so the two
// get different TONES ('ok' vs 'warn'), different labels, and different
// body copy. `updateTone()` never returns 'ok' for a null.
//
// Consent is VERSIONED. `consentVersion` holds exactly the
// `consent_version` the user was shown; `apply()` sends that back and the
// backend re-detects inside the worker. If the version moved between the
// check and the click nothing is built, and the fresh status is rendered
// as a fresh offer with an explanation — not a bare error.
//
// POLLING. Status is a cheap in-memory dict read, but it is still a poll,
// so it is on a slow timer (30 s) and pauses to 4 s only while a build is
// actually in flight. Nothing here triggers a check: `image:checkUpdate`
// detects only and is wired to the explicit "Check for updates" button.
const UPDATE_POLL_MS        = 30000;   // idle reflection
const UPDATE_POLL_BUSY_MS   = 4000;    // while building/verifying/checking
const UPDATE_TONES          = ['muted', 'ok', 'attention', 'warn', 'error', 'progress'];

let updateStatus     = null;   // last payload from image:updateStatus
let updatePanelOpen  = false;  // the user opened the inline panel
let consentVersion   = null;   // the exact version offered for consent
let consentWarning   = null;   // backend-authored one-way-door text
let applyInFlight    = false;
let updatePollTimer  = null;

// Which backend states mean "do not nag the user with a button": the update
// will happen by itself. `unattended` (CHROME_ISOLATION_AUTO_REBUILD=1) is the
// documented opt-in for exactly that.
const UPDATE_ACTIVE_STATES = new Set(['checking', 'building', 'verifying']);

/**
 * Map a status payload to one of UPDATE_TONES.
 *
 * The whole point is the tri-state. Note what is NOT here: there is no branch
 * that returns 'ok' because `update_available` is falsy. 'ok' is reachable only
 * from the two states the backend reached by POSITIVELY DETERMINING the
 * installed version is current. Anything else that is not positively known
 * current lands on 'warn' or 'muted'.
 */
function updateTone(s) {
  if (!s) return 'muted';
  if (s.policy === 'disabled' || s.state === 'disabled') return 'muted';
  switch (s.state) {
    case 'up_to_date':
      return 'ok';
    case 'ready':
      // A rebuild just landed. Not the same claim as 'up_to_date' (which is
      // re-derived from a fresh check), but not a problem either.
      return 'ok';
    case 'update_available':
      return 'attention';
    case 'base_eol':
      // The image's Alpine branch is frozen. Distinct from, and worse than,
      // "Chromium is behind": the version check will report "up to date"
      // forever on a frozen index while nothing is patched.
      return 'error';
    case 'unknown':
      // COULD NOT DETERMINE. Not 'ok'. This is the conflation the backend
      // refuses to make, so the renderer refuses it too.
      return 'warn';
    case 'failed':
    case 'interrupted':
      return 'error';
    case 'deferred':
      return 'attention';
    case 'checking':
    case 'building':
    case 'verifying':
      return 'progress';
    case 'idle':
    case 'disabled':
    case 'debounced':
    default:
      // Nothing is being claimed: no check has run, checks are switched off,
      // or the floor has not elapsed. Deliberately 'muted' — never 'ok'.
      return 'muted';
  }
}

/** Short chip label. Never says "up to date" for a state that did not prove it. */
function updateChipLabel(s, tone) {
  if (!s) return 'Update status unavailable';
  if (s.policy === 'disabled' || s.state === 'disabled') return 'Updates disabled';
  switch (s.state) {
    case 'up_to_date':      return `Up to date (${s.current_version || 'current'})`;
    case 'ready':           return `Updated to ${s.current_version || 'current'}`;
    case 'update_available': return `Update available: ${s.latest_version || '?'}`;
    case 'base_eol':        return 'Base OS end-of-life';
    case 'unknown':         return 'Could not check';
    case 'failed':          return 'Update failed';
    case 'interrupted':     return 'Update interrupted';
    case 'deferred':        return 'Update deferred';
    case 'checking':        return 'Checking for updates…';
    case 'building':        return 'Building image…';
    case 'verifying':       return 'Verifying image…';
    case 'idle':            return 'Not checked yet';
    case 'debounced':       return 'Check pending';
    case 'disabled':        return 'Updates disabled';
    default:                return 'Update status unknown';
  }
}

/** Panel heading. Again: only 'up_to_date'/'ready' may use the word "current". */
function updatePanelTitle(s, tone) {
  if (!s) return 'Chromium update status unavailable';
  if (s.policy === 'disabled' || s.state === 'disabled') return 'Automatic updates are disabled';
  switch (s.state) {
    case 'up_to_date':      return 'Chromium is up to date';
    case 'ready':           return 'Chromium update installed';
    case 'update_available': return 'A Chromium update is available';
    case 'base_eol':        return 'The base operating system is end-of-life';
    case 'unknown':         return 'Could not determine whether an update exists';
    case 'failed':          return 'The Chromium update failed';
    case 'interrupted':     return 'The Chromium update was interrupted';
    case 'deferred':        return 'The Chromium update was deferred';
    case 'checking':        return 'Checking for a Chromium update';
    case 'building':        return 'Rebuilding the browser image';
    case 'verifying':       return 'Verifying the rebuilt image';
    case 'idle':            return 'Chromium update status';
    case 'debounced':       return 'Chromium update check pending';
    case 'disabled':        return 'Automatic updates are disabled';
    default:                return 'Chromium update status';
  }
}

/**
 * Body copy.
 *
 * `update_available === null` gets its own sentence and never borrows the
 * up-to-date wording. The rest falls back to the backend's own `last_result`,
 * which is authored next to the code that produced the state.
 */
function updatePanelBody(s, tone) {
  if (!s) {
    return 'The update status could not be read from the backend, so this app does not know whether your browser is current.';
  }
  if (s.policy === 'disabled' || s.state === 'disabled') {
    return 'Automatic Chromium updates are switched off. Nothing is checked and nothing is rebuilt until you re-enable them.';
  }

  const parts = [];
  switch (s.state) {
    case 'up_to_date':
      parts.push(`Chromium ${s.current_version || ''} is the newest version available.`.replace(/\s+/g, ' ').trim());
      break;
    case 'ready':
      parts.push(`Chromium ${s.current_version || ''} is now installed. Existing containers still run the image they were created from; relaunch a profile to pick it up.`);
      break;
    case 'update_available':
      parts.push(`Chromium ${s.current_version || '?'} is installed and ${s.latest_version || '?'} is available. Nothing has been rebuilt — this app will not replace your browser without your approval.`);
      break;
    case 'base_eol':
      // The notice is the backend's, with the branch names filled in.
      parts.push(s.base_eol_notice || 'The base operating system is no longer supported.');
      break;
    case 'unknown':
      parts.push('The last check could not determine whether a newer Chromium exists. That is NOT the same as being up to date: this browser may be behind, and nothing here can tell you. Treat this as unverified, not as fine.');
      break;
    case 'failed':
    case 'interrupted':
      parts.push('The rebuild failed. Your existing image was left untouched and is still working — nothing was replaced.');
      break;
    case 'deferred':
      parts.push('The rebuild was postponed because a profile is currently running.');
      break;
    case 'checking':
      parts.push('Asking the package index what the newest Chromium is.');
      break;
    case 'building':
      parts.push('Building a new image under a staging tag. The image you are using is not touched until the new one has been built and verified.');
      break;
    case 'verifying':
      parts.push('Checking the newly built image before it replaces the current one.');
      break;
    default:
      parts.push('No update check has completed yet.');
  }

  if (s.policy === 'unattended') {
    parts.push('Unattended updates are enabled, so this rebuild happens on its own — there is nothing for you to approve.');
  }
  if (s.retry_in_seconds && ['up_to_date', 'ready', 'update_available', 'unknown', 'base_eol', 'debounced'].includes(s.state)) {
    const mins = Math.max(1, Math.round(Number(s.retry_in_seconds) / 60));
    parts.push(`Next automatic check in about ${mins} minute${mins === 1 ? '' : 's'}.`);
  }
  if (s.last_result) parts.push(s.last_result);
  return parts.join(' ');
}

function updateVersionsLabel(s) {
  if (!s) return '';
  const cur = s.current_version;
  const lat = s.latest_version;
  if (cur && lat && cur !== lat) return `${cur} → ${lat}`;
  if (cur) return cur;
  if (lat) return lat;
  return '';
}

/**
 * Fetch status and render. `forceOpen` is used when the user clicked the chip
 * so the panel opens even if the payload came back empty.
 */
async function refreshUpdateStatus(forceOpen) {
  let s = null;
  try {
    s = await window.api.image.status();
  } catch (e) {
    s = null;
  }
  updateStatus = s;

  // Consent is tied to the exact version on offer, and the warning travels
  // with it. Both come from the payload; the renderer keeps no copy of either.
  consentVersion = (s && s.consent_required && s.consent_version) ? s.consent_version : null;
  consentWarning = (s && typeof s.consent_warning === 'string' && s.consent_warning)
    ? s.consent_warning : null;

  if (forceOpen) updatePanelOpen = true;
  renderUpdate();
  scheduleUpdatePoll(s);
}

/**
 * Apply click handler.
 *
 * The version-moved case: the backend refuses to substitute a different
 * version for the one that was approved, and returns a refreshed status.
 * So on ANY refusal we re-read the status and compare what is now on offer
 * with what the user approved. If they differ, the panel says so in words and
 * the Apply button is re-armed for the NEW version — the user consents again
 * to what is actually on offer, rather than being shown a bare error.
 */
async function applyConsentedUpdate() {
  if (!consentVersion || applyInFlight) return;
  const approved = consentVersion;      // exactly what the user was shown
  applyInFlight = true;
  renderUpdate();

  let result = null;
  try {
    result = await window.api.image.apply(approved);
  } catch (e) {
    result = null;
    toast(`Could not apply the update: ${e.message}`, 'error');
  }
  applyInFlight = false;

  // The apply response carries its own status snapshot; prefer it, then fall
  // back to a fresh poll.
  if (result && result.status) updateStatus = result.status;
  await refreshUpdateStatus(false);

  const nowOnOffer = consentVersion;

  if (result && result.started === false) {
    if (nowOnOffer && nowOnOffer !== approved) {
      // THE VERSION MOVED. Nothing was built; say what happened and why, and
      // let the button re-arm for the new version.
      toast(`Chromium ${approved} was approved, but ${nowOnOffer} is what is actually available now. Nothing was rebuilt — review and approve again.`, 'error', 9000);
    } else if (result.reason === 'no_update_available') {
      toast('There is no version available to approve right now, so nothing was rebuilt.', 'info');
    } else if (result.reason === 'disabled') {
      toast('Updates are disabled, so nothing was rebuilt.', 'info');
    } else if (result.reason === 'debounced') {
      toast('Too soon after the last attempt — nothing was rebuilt.', 'info');
    } else if (result.reason === 'in_progress') {
      toast('Another update attempt is already running.', 'info');
    } else {
      toast(`Update not applied: ${result.detail || result.reason || 'the backend refused'}`, 'error', 8000);
    }
    return;
  }

  if (nowOnOffer && nowOnOffer !== approved) {
    toast(`Chromium ${approved} was approved, but ${nowOnOffer} is what is actually available now. Nothing was rebuilt — review and approve again.`, 'error', 9000);
    return;
  }
  toast(`Rebuilding Chromium ${approved}. Your current image stays untouched until the new one is verified.`, 'success');
}

function renderUpdate() {
  const s = updateStatus;
  const tone = updateTone(s);

  const chip = document.getElementById('btnUpdateChip');
  const label = document.getElementById('updateChipLabel');
  if (chip.dataset.tone !== tone) chip.dataset.tone = tone;
  const text = updateChipLabel(s, tone);
  if (label.textContent !== text) label.textContent = text;

  const panel = document.getElementById('updatePanel');
  const open = updatePanelOpen && !!s && tone !== 'muted';
  panel.hidden = !open;
  chip.setAttribute('aria-expanded', open ? 'true' : 'false');
  if (!open) return;

  if (panel.dataset.tone !== tone) panel.dataset.tone = tone;
  document.getElementById('updatePanelTitle').textContent = updatePanelTitle(s, tone);
  document.getElementById('updatePanelVersions').textContent = updateVersionsLabel(s);
  document.getElementById('updatePanelBody').textContent = updatePanelBody(s, tone);

  // The one-way-door warning, verbatim from the payload, shown BEFORE the
  // Apply button can be pressed. Not a rewrite — `esc`/textContent carries it
  // through unaltered.
  const warnEl = document.getElementById('updateConsentWarning');
  if (consentWarning && consentVersion) {
    warnEl.textContent = consentWarning;
    warnEl.hidden = false;
  } else {
    warnEl.textContent = '';
    warnEl.hidden = true;
  }

  // Error / last result detail on a failed attempt.
  const errEl = document.getElementById('updateErrorDetail');
  const detail = (s && (s.error || s.last_result)) || '';
  if (s && (s.state === 'failed' || s.state === 'interrupted') && detail) {
    errEl.textContent = s.error ? s.error : s.last_result;
    errEl.hidden = false;
  } else {
    errEl.textContent = '';
    errEl.hidden = true;
  }

  // The Apply button. Offered ONLY when a specific version is on offer and
  // consented-to, and NOT under `unattended` (it would rebuild by itself) or
  // `disabled` (nothing would happen). Labelled from the backend's own
  // one-line form.
  const applyBtn = document.getElementById('btnApplyUpdate');
  const unattended = s && s.policy === 'unattended';
  const showApply = !!(consentVersion && consentWarning && !unattended
                       && s.policy !== 'disabled' && !applyInFlight);
  applyBtn.hidden = !showApply;
  if (showApply) {
    applyBtn.textContent = s.consent_warning_short
      ? `Apply update to ${consentVersion} — ${s.consent_warning_short}`
      : `Apply update to ${consentVersion}`;
    applyBtn.disabled = false;
  }

  // Manual base-image rebuild. Offered when the base OS is end-of-life (or on
  // demand). It rebuilds from the shipped Dockerfile and promotes only after
  // the new image passes verification, so the running image is never at risk.
  const rebuildBtn = document.getElementById('btnRebuildBase');
  const showRebuild = !!(s && (s.state === 'base_eol' || s.state === 'up_to_date'
                               || s.state === 'update_available' || s.state === 'unknown')
                       && !applyInFlight && !(s && UPDATE_ACTIVE_STATES.has(s.state)));
  rebuildBtn.hidden = !showRebuild;
  rebuildBtn.disabled = !!(s && UPDATE_ACTIVE_STATES.has(s.state)) || applyInFlight;

  document.getElementById('btnCheckUpdate').disabled =
    !!(s && UPDATE_ACTIVE_STATES.has(s.state)) || applyInFlight;
}

/**
 * Poll timer. Slow by default; only tightens while a build is actually
 * running, so the UI shows progress without a permanent fast loop. Never
 * starts a check — reflecting state is all this does.
 */
function scheduleUpdatePoll(s) {
  if (updatePollTimer) clearTimeout(updatePollTimer);
  const busy = !!(s && UPDATE_ACTIVE_STATES.has(s.state));
  const delay = busy ? UPDATE_POLL_BUSY_MS : UPDATE_POLL_MS;
  updatePollTimer = setTimeout(() => {
    refreshUpdateStatus(false);
  }, delay);
}

document.getElementById('btnUpdateChip')?.addEventListener('click', () => {
  updatePanelOpen = !updatePanelOpen;
  renderUpdate();
});

document.getElementById('btnUpdatePanelClose')?.addEventListener('click', () => {
  updatePanelOpen = false;
  renderUpdate();
});

document.getElementById('btnApplyUpdate')?.addEventListener('click', applyConsentedUpdate);

document.getElementById('btnRebuildBase')?.addEventListener('click', async () => {
  if (applyInFlight) return;
  const ok = await confirm('Rebuild base image',
    'This rebuilds the browser image from the shipped Dockerfile (which may move the base OS, e.g. Alpine 3.19 → 3.24) and replaces :latest only after the new image passes verification. Your current image stays untouched until then. Continue?');
  if (!ok) return;
  applyInFlight = true;
  renderUpdate();
  let result = null;
  try {
    result = await window.api.image.rebuild();
  } catch (e) {
    result = null;
    toast(`Rebuild failed: ${e.message}`, 'error');
  }
  applyInFlight = false;
  if (result && result.status) updateStatus = result.status;
  await refreshUpdateStatus(false);
  if (result && result.started === false) {
    toast(`Rebuild not started: ${result.detail || result.reason || 'refused'}`, 'error', 8000);
  } else {
    toast('Rebuilding the base image. Your current image stays untouched until the new one is verified.', 'success');
  }
});

// "Check for updates" DETECTS ONLY. It never builds: a rebuild needs the
// Apply button, which carries the versioned consent.
document.getElementById('btnCheckUpdate')?.addEventListener('click', async () => {
  try {
    const r = await window.api.image.check();
    if (r && r.status) updateStatus = r.status;
  } catch (e) {
    toast(`Update check failed: ${e.message}`, 'error');
  }
  await refreshUpdateStatus(false);
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

// ── Keyboard & Overlay Modal Dismissal ─────────────────────────────────────
// Escape and clicking the overlay dismiss a modal. Neither CLEARS it.
//
// This used to call form.reset() on the Create modal, so a single stray Escape
// silently threw away a half-typed profile with no undo and no warning — the
// keys most likely to be hit by accident. The form is now cleared only on a
// successful submit and on the explicit Cancel button (both wired above).
//
// Note the overlays are NOT aria-modal and there is no focus trap, so Tab can
// still leave an open dialog — which is exactly why the modal ARIA attributes
// were removed rather than kept. See the R7 comment in index.html.
function closeAllModals() {
  const modalIds = ['modalCreate', 'modalEdit', 'modalProxies', 'modalConfirm'];
  modalIds.forEach(id => {
    const m = document.getElementById(id);
    if (m && m.style.display !== 'none') {
      if (id === 'modalConfirm') {
        // Cancel the confirmation rather than orphaning the promise: the
        // confirm() promise must always settle, and its only two exits are
        // these two buttons.
        const noBtn = document.getElementById('btnConfirmNo');
        if (noBtn) noBtn.click();
      } else {
        m.style.display = 'none';
        // Deliberately NOT form.reset() here — see the note above.
        if (id === 'modalEdit') editingName = null;
      }
    }
  });
}

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    closeAllModals();
  }
});

document.querySelectorAll('.modal-overlay').forEach(overlay => {
  overlay.addEventListener('click', (e) => {
    if (e.target === overlay) {
      closeAllModals();
    }
  });
});

// ── Initial load ──────────────────────────────────────────────────────────────
loadProfiles();
loadProxies();
refreshUpdateStatus(false);
