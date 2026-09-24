/* Administrator account management. */
let managedUsers = [];

// 显示时翻译；查不到就回落原始值（后端加了新角色也不会显示成空白）
const ROLE_LABEL_KEYS = { admin: 'role_admin', engineer: 'role_engineer', reviewer: 'role_reviewer' };
const STATUS_LABEL_KEYS = { active: 'status_active', disabled: 'status_disabled', expired: 'status_expired' };
const ROLE_CLASSES = {
    admin: 'text-blue-400 bg-blue-900/30 border-blue-800',
    engineer: 'text-purple-400 bg-purple-900/30 border-purple-800',
    reviewer: 'text-green-400 bg-green-900/30 border-green-800',
};

function escUser(value) {
    return String(value ?? '').replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
}

function showUserAlert(message, error = false) {
    const el = document.getElementById('user-alert');
    if (!el) return;
    el.textContent = message;
    el.className = `mb-4 rounded-lg border px-4 py-3 text-sm ${error
        ? 'text-red-300 bg-red-900/20 border-red-800'
        : 'text-green-300 bg-green-900/20 border-green-800'}`;
    window.clearTimeout(showUserAlert.timer);
    showUserAlert.timer = window.setTimeout(() => el.classList.add('hidden'), 4000);
}

async function apiUsers(url, options = {}) {
    const response = await fetch(url, { headers: { 'Content-Type': 'application/json' }, ...options });
    let data = {};
    try { data = await response.json(); } catch (_) {}
    if (!response.ok) throw new Error(data.detail || `Request failed (${response.status})`);
    return data;
}

async function loadUsers() {
    const role = document.getElementById('user-role-filter')?.value || '';
    const status = document.getElementById('user-status-filter')?.value || '';
    const params = new URLSearchParams();
    if (role) params.set('role', role);
    if (status) params.set('status', status);
    try {
        const data = await apiUsers(`/api/v1/users?${params}`);
        managedUsers = data.users || [];
        updateUserCounts();
        renderUsers();
    } catch (error) {
        showUserAlert(error.message, true);
        document.getElementById('users-body').innerHTML = `<tr><td colspan="6" class="px-4 py-10 text-center text-red-400">${escUser(error.message)}</td></tr>`;
    }
}

function updateUserCounts() {
    const active = managedUsers.filter(user => user.status === 'active').length;
    document.getElementById('count-total').textContent = managedUsers.length;
    document.getElementById('count-active').textContent = active;
    document.getElementById('count-inactive').textContent = managedUsers.length - active;
}

function formatDate(value) {
    if (!value) return 'Never';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString();
}

function renderUsers() {
    const query = (document.getElementById('user-search')?.value || '').trim().toLowerCase();
    const users = managedUsers.filter(user => !query
        || user.username.toLowerCase().includes(query)
        || (user.email || '').toLowerCase().includes(query));
    document.getElementById('user-list-count').textContent = t('users_count').replace('%s', users.length);
    const body = document.getElementById('users-body');
    if (!users.length) {
        body.innerHTML = '<tr><td colspan="6" class="px-4 py-10 text-center text-gray-600">' + t('users_no_found') + '</td></tr>';
        return;
    }
    body.innerHTML = users.map(user => {
        const statusClass = user.status === 'active'
            ? 'text-green-400 bg-green-900/30 border-green-800'
            : user.status === 'expired'
                ? 'text-yellow-400 bg-yellow-900/30 border-yellow-800'
                : 'text-gray-400 bg-gray-800 border-gray-700';
        let action = '';
        if (user.status === 'expired') {
            action = `<button onclick="openUserModal('${escUser(user.id)}')" title="${t('users_extend_hint')}" class="border border-yellow-800 text-yellow-400 hover:bg-yellow-900/30 px-2 py-1 rounded">${t('users_extend')}</button>`;
        } else if (user.account_status === 'active') {
            action = `<button onclick="toggleUser('${escUser(user.id)}', 'disabled')" title="${t('users_disable_hint')}" class="border border-yellow-900 text-yellow-400 hover:bg-yellow-900/30 px-2 py-1 rounded">${t('users_disable')}</button>`;
        } else {
            action = `<button onclick="toggleUser('${escUser(user.id)}', 'active')" title="${t('users_enable_hint')}" class="border border-green-900 text-green-400 hover:bg-green-900/30 px-2 py-1 rounded">${t('users_enable')}</button>`;
        }
        return `<tr class="border-t border-gray-800 hover:bg-gray-800/30">
            <td class="px-4 py-3"><div class="text-gray-200">${escUser(user.username)}</div><div class="text-xs text-gray-600">${escUser(user.email || t('users_no_email'))}</div></td>
            <td class="px-4 py-3"><span class="inline-flex px-2 py-0.5 rounded border text-xs ${ROLE_CLASSES[user.role] || 'text-gray-400 bg-gray-800 border-gray-700'}">${escUser(ROLE_LABEL_KEYS[user.role] ? t(ROLE_LABEL_KEYS[user.role]) : user.role)}</span></td>
            <td class="px-4 py-3"><span class="inline-flex px-2 py-0.5 rounded border text-xs ${statusClass}">${escUser(STATUS_LABEL_KEYS[user.status] ? t(STATUS_LABEL_KEYS[user.status]) : user.status)}</span></td>
            <td class="px-4 py-3 text-xs text-gray-500">${escUser(formatDate(user.last_login_at))}</td>
            <td class="px-4 py-3 text-xs ${user.status === 'expired' ? 'text-yellow-400' : 'text-gray-500'}">${escUser(user.expires_at ? formatDate(user.expires_at) : t('users_never'))}</td>
            <td class="px-4 py-3"><div class="flex justify-end items-center gap-2 whitespace-nowrap text-xs"><button onclick="openUserModal('${escUser(user.id)}')" title="${t('users_edit_hint')}" class="border border-blue-900 text-blue-400 hover:bg-blue-900/30 px-2 py-1 rounded">${t('users_edit')}</button>${action}<button onclick="deleteUser('${escUser(user.id)}')" title="${t('users_delete_hint')}" class="border border-red-900 text-red-400 hover:bg-red-900/30 px-2 py-1 rounded">${t('users_delete')}</button></div></td>
        </tr>`;
    }).join('');
}

function toLocalInput(value) {
    if (!value) return '';
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return '';
    const pad = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function openUserModal(userId = '') {
    const user = managedUsers.find(item => item.id === userId);
    document.getElementById('user-id').value = user?.id || '';
    document.getElementById('user-modal-title').textContent = user ? t('users_edit_user') : t('users_new');
    document.getElementById('user-save-btn').textContent = user ? t('users_save_changes') : t('users_create');
    document.getElementById('user-username').value = user?.username || '';
    document.getElementById('user-username').disabled = Boolean(user);
    document.getElementById('user-role').value = user?.role || 'engineer';
    document.getElementById('user-email').value = user?.email || '';
    document.getElementById('user-password').value = '';
    document.getElementById('user-password').required = !user;
    document.getElementById('password-hint').textContent = user ? t('users_pw_keep') : t('users_pw_hint');
    document.getElementById('user-status').value = user?.account_status || 'active';
    document.getElementById('user-expires').value = toLocalInput(user?.expires_at);
    document.getElementById('user-modal').classList.remove('hidden');
    setTimeout(() => document.getElementById(user ? 'user-email' : 'user-username').focus(), 0);
}

function closeUserModal() { document.getElementById('user-modal').classList.add('hidden'); }

async function saveUser(event) {
    event.preventDefault();
    const id = document.getElementById('user-id').value;
    const expires = document.getElementById('user-expires').value;
    const payload = {
        email: document.getElementById('user-email').value.trim() || null,
        role: document.getElementById('user-role').value,
        expires_at: expires ? new Date(expires).toISOString() : null,
    };
    const password = document.getElementById('user-password').value;
    if (password) payload.password = password;
    if (id) {
        payload.status = document.getElementById('user-status').value;
    } else {
        payload.username = document.getElementById('user-username').value.trim();
        payload.password = password;
    }
    const button = document.getElementById('user-save-btn');
    button.disabled = true;
    try {
        await apiUsers(id ? `/api/v1/users/${encodeURIComponent(id)}` : '/api/v1/users', {
            method: id ? 'PATCH' : 'POST', body: JSON.stringify(payload)
        });
        closeUserModal();
        showUserAlert(id ? 'User updated' : 'User created');
        await loadUsers();
    } catch (error) {
        showUserAlert(error.message, true);
    } finally { button.disabled = false; }
}

async function toggleUser(id, status) {
    try {
        await apiUsers(`/api/v1/users/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify({ status }) });
        showUserAlert(status === 'active' ? 'User enabled' : 'User disabled');
        await loadUsers();
    } catch (error) { showUserAlert(error.message, true); }
}

async function deleteUser(id) {
    const user = managedUsers.find(item => item.id === id);
    if (!user || !window.confirm(t('users_confirm_delete').replace('%s', user.username))) return;
    try {
        await apiUsers(`/api/v1/users/${encodeURIComponent(id)}`, { method: 'DELETE' });
        showUserAlert('User deleted');
        await loadUsers();
    } catch (error) { showUserAlert(error.message, true); }
}

document.addEventListener('DOMContentLoaded', loadUsers);
