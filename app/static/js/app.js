// Minimalist React-like App
class App {
    constructor() {
        this.profiles = [];
        this.state = { loading: false };
        this.init();
    }

    init() {
        this.setupEventListeners();
        this.loadProfiles();
        setInterval(() => this.loadProfiles(), 5000);
    }

    setupEventListeners() {
        document.getElementById('createBtn').addEventListener('click', () => this.showModal());
        document.getElementById('importBtn').addEventListener('click', () => this.triggerImport());
        document.getElementById('importFileInput').addEventListener('change', (e) => this.handleImport(e));
        document.getElementById('refreshBtn').addEventListener('click', () => this.loadProfiles());
        document.getElementById('closeModal').addEventListener('click', () => this.hideModal());
        document.getElementById('cancelBtn').addEventListener('click', () => this.hideModal());
        document.getElementById('modalBackdrop').addEventListener('click', () => this.hideModal());
        document.getElementById('createForm').addEventListener('submit', (e) => this.handleCreate(e));
    }

    async loadProfiles() {
        try {
            this.setState({ loading: true });
            const res = await fetch('/api/profiles');
            const data = await res.json();
            this.profiles = data.profiles || [];
            this.render();
        } catch (error) {
            this.showToast('Failed to load profiles', 'error');
        } finally {
            this.setState({ loading: false });
        }
    }

    setState(newState) {
        this.state = { ...this.state, ...newState };
        this.render();
    }

    render() {
        const container = document.getElementById('profiles-container');
        
        if (this.state.loading && this.profiles.length === 0) {
            container.innerHTML = this.renderLoading();
            return;
        }

        if (this.profiles.length === 0) {
            container.innerHTML = this.renderEmpty();
            return;
        }

        container.innerHTML = this.profiles.map(p => this.renderProfileCard(p)).join('');
        this.attachCardListeners();
    }

    renderLoading() {
        return `
            <div class="loading-state">
                <div class="spinner"></div>
                <p>Loading profiles...</p>
            </div>
        `;
    }

    renderEmpty() {
        return `
            <div class="empty-state">
                <h3>No profiles</h3>
                <p>Create your first isolated Chrome profile</p>
                <button class="btn btn-primary" onclick="app.showModal()">
                    New Profile
                </button>
            </div>
        `;
    }

    renderProfileCard(profile) {
        const statusText = {
            'running': 'Running',
            'exited': 'Stopped',
            'not_found': 'Not Started'
        }[profile.status] || profile.status;

        return `
            <div class="profile-card" data-name="${this.escape(profile.name)}">
                <div class="profile-header">
                    <div class="profile-name">${this.escape(profile.name)}</div>
                    <span class="status-badge status-${profile.status}">${statusText}</span>
                </div>
                <div class="profile-info">
                    <div>Storage: ${profile.size_mb} MB</div>
                    <div>Desktop: ${profile.has_desktop_entry ? 'Yes' : 'No'}</div>
                </div>
                <div class="profile-actions">
                    ${profile.status === 'running'
                        ? `<button class="btn btn-danger btn-sm" data-action="stop">Stop</button>`
                        : `<button class="btn btn-success btn-sm" data-action="start">Start</button>`
                    }
                    <button class="btn btn-secondary btn-sm" data-action="export">Export</button>
                    <button class="btn btn-danger btn-sm" data-action="delete">Delete</button>
                </div>
            </div>
        `;
    }

    attachCardListeners() {
        document.querySelectorAll('.profile-card').forEach(card => {
            const name = card.dataset.name;
            card.querySelectorAll('[data-action]').forEach(btn => {
                btn.addEventListener('click', (e) => {
                    const action = e.target.dataset.action;
                    this.handleAction(action, name);
                });
            });
        });
    }

    async handleAction(action, name) {
        switch (action) {
            case 'start':
                await this.startProfile(name);
                break;
            case 'stop':
                await this.stopProfile(name);
                break;
            case 'delete':
                await this.deleteProfile(name);
                break;
            case 'export':
                this.exportProfile(name);
                break;
        }
    }

    async startProfile(name) {
        try {
            const res = await fetch(`/api/profiles/${encodeURIComponent(name)}/start`, {
                method: 'POST'
            });
            const data = await res.json();
            if (res.ok) {
                this.showToast(`Profile "${name}" started`, 'success');
                await this.loadProfiles();
            } else {
                this.showToast(data.error || 'Failed to start', 'error');
            }
        } catch (error) {
            this.showToast('Failed to start profile', 'error');
        }
    }

    async stopProfile(name) {
        try {
            const res = await fetch(`/api/profiles/${encodeURIComponent(name)}/stop`, {
                method: 'POST'
            });
            const data = await res.json();
            if (res.ok) {
                this.showToast(`Profile "${name}" stopped`, 'success');
                await this.loadProfiles();
            } else {
                this.showToast(data.error || 'Failed to stop', 'error');
            }
        } catch (error) {
            this.showToast('Failed to stop profile', 'error');
        }
    }

    async deleteProfile(name) {
        const confirmed = await this.showConfirm(
            'Delete Profile',
            `Are you sure you want to delete profile "${name}"? This action cannot be undone.`
        );
        if (!confirmed) return;

        try {
            const res = await fetch(`/api/profiles/${encodeURIComponent(name)}`, {
                method: 'DELETE'
            });
            const data = await res.json();
            if (res.ok) {
                this.showToast(`Profile "${name}" deleted`, 'success');
                await this.loadProfiles();
            } else {
                this.showToast(data.error || 'Failed to delete', 'error');
            }
        } catch (error) {
            this.showToast('Failed to delete profile', 'error');
        }
    }

    showConfirm(title, message) {
        return new Promise((resolve) => {
            document.getElementById('confirmTitle').textContent = title;
            document.getElementById('confirmMessage').textContent = message;
            const modal = document.getElementById('confirmModal');
            modal.classList.add('active');

            const handleConfirm = () => {
                modal.classList.remove('active');
                document.getElementById('confirmOk').removeEventListener('click', handleConfirm);
                document.getElementById('confirmCancel').removeEventListener('click', handleCancel);
                document.getElementById('confirmBackdrop').removeEventListener('click', handleCancel);
                resolve(true);
            };

            const handleCancel = () => {
                modal.classList.remove('active');
                document.getElementById('confirmOk').removeEventListener('click', handleConfirm);
                document.getElementById('confirmCancel').removeEventListener('click', handleCancel);
                document.getElementById('confirmBackdrop').removeEventListener('click', handleCancel);
                resolve(false);
            };

            document.getElementById('confirmOk').addEventListener('click', handleConfirm);
            document.getElementById('confirmCancel').addEventListener('click', handleCancel);
            document.getElementById('confirmBackdrop').addEventListener('click', handleCancel);
        });
    }

    exportProfile(name) {
        window.location.href = `/api/profiles/${encodeURIComponent(name)}/export`;
    }

    triggerImport() {
        document.getElementById('importFileInput').click();
    }

    async handleImport(e) {
        const file = e.target.files[0];
        if (!file) return;

        const formData = new FormData();
        formData.append('file', file);

        try {
            this.showToast('Importing profile...', 'success');
            const res = await fetch('/api/profiles/import', {
                method: 'POST',
                body: formData
            });

            const data = await res.json();
            if (res.ok) {
                this.showToast(`Profile "${data.name}" imported successfully`, 'success');
                await this.loadProfiles();
            } else {
                this.showToast(data.error || 'Failed to import profile', 'error');
            }
        } catch (error) {
            this.showToast('Failed to import profile', 'error');
        } finally {
            // Reset file input
            e.target.value = '';
        }
    }

    showModal() {
        document.getElementById('modal').classList.add('active');
        document.getElementById('profileName').focus();
    }

    hideModal() {
        document.getElementById('modal').classList.remove('active');
        document.getElementById('createForm').reset();
    }

    async handleCreate(e) {
        e.preventDefault();
        const name = document.getElementById('profileName').value.trim();
        const location = document.getElementById('profileLocation').value.trim();

        if (!name) {
            this.showToast('Profile name is required', 'error');
            return;
        }

        try {
            const body = { name };
            if (location) body.location = location;

            const res = await fetch('/api/profiles', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body)
            });

            const data = await res.json();
            if (res.ok) {
                this.showToast(`Profile "${name}" created`, 'success');
                this.hideModal();
                await this.loadProfiles();
            } else {
                this.showToast(data.error || 'Failed to create', 'error');
            }
        } catch (error) {
            this.showToast('Failed to create profile', 'error');
        }
    }

    showToast(message, type = 'success') {
        const container = document.getElementById('toastContainer');
        const toast = document.createElement('div');
        toast.className = `toast toast-${type}`;
        toast.innerHTML = `
            <span class="toast-message">${this.escape(message)}</span>
            <span class="toast-close" onclick="this.parentElement.remove()">×</span>
        `;
        container.appendChild(toast);
        setTimeout(() => toast.remove(), 4000);
    }

    escape(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
}

// Initialize app
const app = new App();
