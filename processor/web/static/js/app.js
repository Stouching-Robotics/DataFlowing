/* Episode list, info panel, review/delete/export */

function escHtml(s) {
    if (!s) return '';
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

let selectedEpisodes = new Set();  // batch selection
let allEpisodes = [];
let hierarchyData = [];            // Project → Task → Episode tree for the list view
const _expandedProjects = new Set();  // expanded project ids (default: collapsed)
const _collapsedTasks = new Set();     // collapsed task ids
let _loadEpoch = 0;  // stale-response guard — discard out-of-order poll results
let _formatEpoch = 0;  // 导出格式徽标防竞态:快速切换批次时丢弃过期响应
let _silentReloadTimer = null;  // coalesce background list refreshes after actions
let _listLoadController = null; // abort obsolete hierarchy requests on fast navigation
let _episodesLoadedAt = 0;      // snapshot freshness for background reconciliation
let selectMode = false;  // selection mode toggle — hides checkboxes by default

// 视图缓存。筛选结果只依赖 [筛选条件 + 排序模式 + 层级快照],与勾选状态无关;
// 但勾选一行原先要重算三遍(visibleEpisodes → hierarchy → updateBatchUI),
// 每次都 spread 复制全部 episode。缓存后勾选只改 Set 和那一行 DOM。
// _hierarchyRev 在层级数据变化时自增 —— 快照替换、局部增删、局部改字段都要加。
let _hierarchyRev = 0;
let _viewCache = { key: null, groups: [], visible: [], visibleIds: new Set(), reviewedIds: [] };
// 上一次渲染所依据的服务端快照指纹;一致就跳过一次整表重绘。
let _hierarchySignatureAt = null;

// A short-lived cross-navigation snapshot makes the Review page paint
// immediately after leaving Projects/Workflow. The server remains
// authoritative: loadEpisodes still refreshes in the background.
const REVIEW_SNAPSHOT_CACHE_KEY = 'egodata.review.hierarchy.v1';
const REVIEW_SNAPSHOT_CACHE_TTL = 60 * 1000;

function readReviewSnapshotCache() {
    try {
        const cached = JSON.parse(sessionStorage.getItem(REVIEW_SNAPSHOT_CACHE_KEY) || 'null');
        if (!cached || !Array.isArray(cached.projects)
                || Date.now() - Number(cached.savedAt || 0) > REVIEW_SNAPSHOT_CACHE_TTL) return null;
        return cached;
    } catch (_) { return null; }
}

function saveReviewSnapshotCache(projects) {
    try {
        sessionStorage.setItem(REVIEW_SNAPSHOT_CACHE_KEY, JSON.stringify({
            savedAt: Date.now(), projects,
        }));
    } catch (_) { /* storage quota/private mode — network refresh still works */ }
}

// 渲染只依赖这些字段;其余字段(如 duration、camera_streams)变了不影响卡片。
function _episodeRenderSignature(ep) {
    return [ep.id, ep.status, ep.ai_quality_status, ep._uiWorkflowState,
            ep.frame_count, ep.fps, ep.name, ep.episode_index,
            (ep.camera_names || []).length, ep.timestamp, ep.created_at].join(':');
}

function _hierarchySignature(projects) {
    let parts = [];
    (projects || []).forEach(node => {
        parts.push(`P${node.project?.id}:${node.project?.name}`);
        (node.episodes || []).forEach(ep => parts.push(_episodeRenderSignature(ep)));
    });
    return parts.join(';');
}

function applyHierarchySnapshot(projects, savedAt = Date.now()) {
    const incoming = projects || [];
    // 15 秒轮询每次都会走到这里。卡片渲染只依赖上面那些字段,内容没变时
    // 直接返回 —— 否则用户正勾选着,列表被整表重建,会闪、丢焦点、丢点击。
    const signature = _hierarchySignature(incoming);
    if (signature === _hierarchySignatureAt) {
        _episodesLoadedAt = savedAt;
        return;
    }
    _hierarchySignatureAt = signature;
    hierarchyData = incoming;
    _hierarchyRev++;
    _episodesLoadedAt = savedAt;
    allEpisodes = hierarchyData.flatMap(node =>
        (node.episodes || []).map(e => ({ ...e, task_description: node.project.name })));
    // updateTaskFilter 可能清掉已失效的 filter-task —— 必须在算视图之前调用,
    // 否则 currentView() 会按一个已经不存在的筛选条件构建缓存。
    updateTaskFilter(visibleEpisodesForCurrentView());
    updateEpisodeListCount(allEpisodes, '', document.getElementById('filter-task')?.value || '');
    renderEpisodeCards();
}

// ── Nav tree → filter bridge (called from base.html) ──

function applyNavFilter(statusParam, taskName) {
    // Set hidden filter controls
    const statusSel = document.getElementById('filter-status');
    const taskSel = document.getElementById('filter-task');
    const searchInput = document.getElementById('search-input');
    if (statusSel) statusSel.value = statusParam;
    const requestedTask = taskName || '';
    if (taskSel) taskSel.value = requestedTask;
    if (searchInput) searchInput.value = requestedTask;
    // Update URL
    const url = new URL(document.location);
    url.searchParams.set('status', statusParam);
    url.searchParams.set('search', requestedTask);
    history.replaceState({}, '', url);
    // Close detail panel (if open), then filter the in-memory snapshot.
    backToList();
    if (hierarchyData.length) {
        renderEpisodeListFromMemory();
        scheduleSilentEpisodeRefresh(250);
    } else {
        loadEpisodes();
    }
    // Highlight nav tree
    if (typeof window.highlightNavActive === 'function') {
        window.highlightNavActive(statusParam, taskName);
    }
}
window.applyNavFilter = applyNavFilter;

function syncReviewShortcutActive(status) {
    const key = status === 'reviewed' ? 'approved'
        : (status === 'failed' ? 'failed' : 'reviewing');
    document.querySelectorAll('.sidebar-sub[data-review-status]').forEach(link => {
        link.classList.toggle('active', link.dataset.reviewStatus === key);
    });
}

function switchReviewStatus(status, options = {}) {
    const allowed = new Set(['completed', 'to_review', 'reviewed', 'failed']);
    const nextStatus = allowed.has(status) ? status : 'completed';
    const statusSel = document.getElementById('filter-status');
    const taskSel = document.getElementById('filter-task');
    const searchInput = document.getElementById('search-input');
    if (statusSel) statusSel.value = nextStatus;
    if (taskSel) taskSel.value = '';
    if (searchInput) searchInput.value = '';

    const url = new URL(window.location.href);
    url.searchParams.set('status', nextStatus);
    url.searchParams.delete('search');
    if (options.replace) history.replaceState({}, '', url);
    else history.pushState({}, '', url);

    syncReviewShortcutActive(nextStatus);
    backToList();
    if (hierarchyData.length) {
        renderEpisodeListFromMemory();
        // Keep the switch instant; only reconcile in the background when the
        // current snapshot is older than a moment.
        if (Date.now() - _episodesLoadedAt > 1000) scheduleSilentEpisodeRefresh(200);
    } else {
        loadEpisodes();
    }
}
window.switchReviewStatus = switchReviewStatus;

function installReviewStatusNavigation() {
    if (window.location.pathname !== '/review') return;
    const statusByShortcut = { reviewing: 'completed', approved: 'reviewed', failed: 'failed' };
    document.querySelectorAll('.sidebar-sub[data-review-status]').forEach(link => {
        link.addEventListener('click', event => {
            event.preventDefault();
            switchReviewStatus(statusByShortcut[link.dataset.reviewStatus] || 'completed');
        });
    });
    window.addEventListener('popstate', () => {
        const params = new URLSearchParams(window.location.search);
        const status = params.get('status') || 'completed';
        const statusSel = document.getElementById('filter-status');
        const searchInput = document.getElementById('search-input');
        if (statusSel) statusSel.value = status;
        if (searchInput) searchInput.value = params.get('search') || '';
        syncReviewShortcutActive(status);
        if (hierarchyData.length) renderEpisodeListFromMemory();
        else loadEpisodes();
    });
}

// ── Init from URL param ─────────────────────────────

function initStatusFromURL() {
    const params = new URLSearchParams(document.location.search);
    const statusParam = params.get('status') || 'completed';
    const sel = document.getElementById('filter-status');
    if (sel) sel.value = statusParam;

    const searchParam = params.get('search') || '';
    const searchInput = document.getElementById('search-input');
    if (searchInput && searchParam) searchInput.value = searchParam;
}

// ── Sort order ──────────────────────────────────────

// label 存 **i18n key** 而不是文案 —— 这个标签会被 toggleSortOrder() 直接写进
// DOM，写死英文的话切了中文后点一下排序又变回英文。
const SORT_MODES = [
    { dir: 'asc',  labelKey: 'name_1_to_n', icon: 'ant-design:ordered-list-outlined' },
    { dir: 'desc', labelKey: 'name_n_to_1', icon: 'ant-design:ordered-list-outlined' },
];
let sortModeIdx = 0;  // cycles 0→1→2→3→0

function currentSortMode() {
    return SORT_MODES[sortModeIdx];
}

function toggleSortOrder() {
    sortModeIdx = (sortModeIdx + 1) % SORT_MODES.length;
    const mode = currentSortMode();
    const icon = document.getElementById('sort-order-icon');
    const label = document.getElementById('sort-order-label');
    const btn = document.getElementById('btn-sort-order');
    if (icon) icon.setAttribute('icon', mode.icon);
    if (label) label.textContent = t(mode.labelKey);
    if (btn) btn.title = t('sort_label') + t(mode.labelKey);
    // Re-sort in memory (no network round trip) — only refetch if not yet loaded
    if (hierarchyData.length === 0) {
        loadEpisodes();
    } else {
        renderEpisodeListFromMemory();
    }
}

// Extract trailing numeric suffix: "Chew_gum_0005" → 5, "ep_00020" → 20, "no_number" → 0
function extractEpisodeNumber(name) {
    const m = String(name || '').match(/(\d+)\s*$/);
    return m ? parseInt(m[1], 10) : 0;
}

// ── Selection mode ──────────────────────────────────

function toggleSelectMode() {
    selectMode = !selectMode;
    if (!selectMode) {
        // 退出选择模式:清空选择,但**不动项目的展开/折叠状态** ——
        // 用户展开过哪些项目是他自己的布局,不该被模式切换重置。
        selectedEpisodes.clear();
    }
    // 进入选择模式**不再自动展开所有项目**。项目行本身就有三态全选框,
    // 折叠状态下点一下即可选中整批(卡片仍在 DOM 里),所以没有展开的必要;
    // 自动展开会让几十个项目同时铺开,列表瞬间变长、滚动位置也丢了。
    // 退出时的按钮态与批量栏显隐统一交给 updateBatchUI。
    renderEpisodeListFromMemory();
}

// ── Load ────────────────────────────────────────────

function currentReviewStatus() {
    return document.getElementById('filter-status')?.value || 'completed';
}

function isEpisodeTemporarilyHidden(ep) {
    // Reprocessing invalidates the previous review artifact immediately. Keep
    // the episode out of every list until the server publishes the new state;
    // otherwise the old card can remain visible as "Processing…".
    return Boolean(ep && (ep.status === 'processing'
        || ep._uiWorkflowState === 'queued'));
}

function matchesReviewStatus(status, filter = currentReviewStatus()) {
    if (!filter) return true;
    if (filter === 'completed' || filter === 'to_review') {
        if (status === 'completed' || status === 'to_review') return true;
        // processing 由 hierarchyForCurrentView 统一隐藏，避免用户打开
        // 旧产物；处理完成后由下一次静默刷新重新出现。
        return false;
    }
    if (filter === 'reviewed') {
        return status === 'reviewed' || status === 'approved';
    }
    return status === filter;
}

function matchesEpisodeSearch(ep, projectName, rawSearch) {
    const search = String(rawSearch || '').trim().toLowerCase();
    if (!search) return true;
    return (ep.name || '').toLowerCase().includes(search)
        || String(ep.id || '').toLowerCase().includes(search)
        || String(projectName || '').toLowerCase().includes(search);
}

// The server snapshot contains all live episode metadata. Status/search
// filtering happens here, so Reviewing ↔ Approved never starts another scan.
function hierarchyForCurrentView() {
    const status = currentReviewStatus();
    const search = document.getElementById('search-input')?.value || '';
    return hierarchyData.map(node => ({
        ...node,
        episodes: (node.episodes || []).filter(ep =>
            !isEpisodeTemporarilyHidden(ep)
            && matchesReviewStatus(ep.status, status)
            && matchesEpisodeSearch(ep, node.project?.name, search)),
    }));
}

// 缓存后的视图模型:分组(已按 task 筛选并按当前排序模式排好)+ 扁平的
// 可见 / 已审核 id 列表。渲染、勾选、计数全部读这一份,不再各自重算。
function currentView() {
    const status = currentReviewStatus();
    const search = document.getElementById('search-input')?.value || '';
    const taskFilter = document.getElementById('filter-task')?.value || '';
    const dir = currentSortMode().dir === 'asc' ? 1 : -1;
    const key = [_hierarchyRev, status, search, taskFilter, dir].join('|');
    if (_viewCache.key === key) return _viewCache;

    const groups = hierarchyForCurrentView()
        .filter(node => !taskFilter || (node.project?.name || '') === taskFilter)
        .map(node => ({
            project: node.project,
            // 组内按尾部序号排序(Test1_000012 → 12),只排副本不动原始数据
            episodes: [...(node.episodes || [])].sort((a, b) =>
                (extractEpisodeNumber(a.name) - extractEpisodeNumber(b.name)) * dir),
        }));

    const visible = [];
    const visibleIds = new Set();
    const reviewedIds = [];
    const framesById = new Map();
    groups.forEach(group => {
        group.episodes.forEach(ep => {
            visible.push({ ...ep, task_description: group.project?.name });
            visibleIds.add(String(ep.id));
            framesById.set(String(ep.id), Number(ep.frame_count) || 0);
            if (ep.status === 'reviewed' || ep.status === 'approved') {
                reviewedIds.push(String(ep.id));
            }
        });
    });
    _viewCache = { key, groups, visible, visibleIds, reviewedIds, framesById };
    return _viewCache;
}

function visibleEpisodesForCurrentView() {
    return currentView().visible;
}

async function loadEpisodes(options = {}) {
    const silent = Boolean(options.silent);
    if (!silent && _silentReloadTimer) {
        clearTimeout(_silentReloadTimer);
        _silentReloadTimer = null;
    }
    const taskFilter = document.getElementById('filter-task')?.value || '';
    const listEl = document.getElementById('episode-list-inline');
    if (!listEl) return;

    // Paint the last successful hierarchy immediately on a new page load.
    // A normal request below still reconciles status/uploads in the background.
    if (!silent && hierarchyData.length === 0) {
        const cached = readReviewSnapshotCache();
        if (cached) applyHierarchySnapshot(cached.projects, Number(cached.savedAt) || Date.now());
    }

    // Save scroll position before innerHTML rebuild (prevent 15s poll reset)
    const scrollContainer = document.getElementById('episode-list-section');
    const savedScrollTop = scrollContainer ? scrollContainer.scrollTop : 0;

    if (!silent && hierarchyData.length === 0) {
        listEl.innerHTML = `<div class="p-4 text-center text-gray-500 text-sm">${t('loading')}</div>`;
    }

    if (_listLoadController) _listLoadController.abort();
    const controller = new AbortController();
    _listLoadController = controller;
    const fetchEpoch = ++_loadEpoch;
    try {
        // Hierarchy view: Project → Task (upload batch) → Episodes.
        // Status and search are applied after the full snapshot arrives.

        // ``fetchEpoch`` was captured before dispatch so stale responses are ignored.
        const res = await fetch('/api/v1/projects/hierarchy', { signal: controller.signal });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        if (fetchEpoch !== _loadEpoch) return;  // newer request in flight — discard
        saveReviewSnapshotCache(data.projects || []);
        applyHierarchySnapshot(data.projects || []);
        // Restore scroll position after render
        if (scrollContainer) {
            requestAnimationFrame(() => { scrollContainer.scrollTop = savedScrollTop; });
        }
    } catch (err) {
        if (err?.name === 'AbortError') return;
        if (!silent && hierarchyData.length === 0) {
            listEl.innerHTML = `<div class="p-4 text-center text-red-400 text-sm">${t('load_failed')}: ${err.message}</div>`;
        }
    } finally {
        if (_listLoadController === controller) _listLoadController = null;
    }
}


function updateEpisodeListCount(episodes, search, taskFilter) {
    const countEl = document.getElementById('episode-list-count');
    if (!countEl) return;
    let filtered = episodes.filter(ep => !isEpisodeTemporarilyHidden(ep)
        && matchesReviewStatus(ep.status));
    if (taskFilter) filtered = filtered.filter(ep => (ep.task_description || '') === taskFilter);
    if (search) {
        const s = search.toLowerCase();
        filtered = filtered.filter(ep =>
            (ep.name || '').toLowerCase().includes(s) ||
            (ep.task_description || '').toLowerCase().includes(s) ||
            String(ep.id).toLowerCase().includes(s));
    }
    countEl.textContent = filtered.length + ' episodes';
}

function updateTaskFilter(episodes) {
    // filter-task is now a hidden input (not a select). Values are set by applyNavFilter().
    // This function remains as a hook: clear the value if the task no longer exists in results.
    const sel = document.getElementById('filter-task');
    if (!sel) return;
    const current = sel.value;
    if (current) {
        const taskNames = new Set(episodes.map(e => e.task_description || 'unknown'));
        if (!taskNames.has(current)) {
            sel.value = '';
        }
    }
}


function renderEpisodeCards() {
    const listEl = document.getElementById('episode-list-inline');
    if (!listEl) return;

    // Auto-exit selection mode when switching away from Reviewed
    const statusFilter = document.getElementById('filter-status')?.value || '';
    if (selectMode && statusFilter !== 'reviewed') {
        selectMode = false;
        selectedEpisodes.clear();
    }

    // 分组、筛选、排序都来自缓存的视图模型 —— 渲染只负责拼 HTML。
    const view = currentView();

    // 项目文件夹**始终渲染**(含空项目/刚创建的项目):几个项目就显示几个
    // 文件夹,哪怕里面没有批次;只有没有任何项目时才显示 No data。
    if (view.groups.length === 0) {
        listEl.innerHTML = `<div class="p-4 text-center text-gray-500 text-sm">${t('no_data')}</div>`;
        updateBatchUI();
        return;
    }

    // 任务概念已移除:项目 → Episodes 两层(组内排序已在 currentView 完成)
    let html = '';
    view.groups.forEach((group) => {
        const sorted = group.episodes;
        const projectId = String(group.project?.id ?? '');
        // 默认折叠:只在用户点击展开过的项目才展开视频卡片
        const pCollapsed = !_expandedProjects.has(group.project?.id);
        html += `
        <div class="project-group border-b border-gray-800" data-project-id="${escHtml(projectId)}">
            <div class="project-header px-3 py-2 flex items-center gap-2 cursor-pointer hover:bg-gray-800/50 select-none"
                 onclick="toggleProjectCollapse('${escHtml(projectId)}')">
                ${projectSelectBoxHtml(projectId, sorted)}
                <iconify-icon icon="ant-design:folder-outlined" class="text-blue-500"></iconify-icon>
                <span class="text-sm font-medium text-gray-200 truncate">${escHtml(group.project?.name || '')}</span>
                <span class="text-xs text-gray-500 flex-shrink-0">${sorted.length}</span>
                <span class="project-select-count text-xs text-blue-400 flex-shrink-0"></span>
                <iconify-icon icon="ant-design:${pCollapsed ? 'right' : 'down'}-outlined"
                              class="project-caret text-gray-600 ml-auto"></iconify-icon>
            </div>
            <div class="project-body ${pCollapsed ? 'hidden' : ''}">`;
        if (sorted.length === 0) {
            html += `<div class="text-center text-gray-600 text-xs py-2">${t('no_tasks_in_project')}</div>`;
        }
        sorted.forEach(ep => { html += episodeCardHtml(ep, group.project?.name); });
        html += `</div></div>`;
    });
    listEl.innerHTML = html;
    syncProjectSelectDom();
    updateBatchUI();
}

// 项目行左侧的三态全选框。只作用于当前筛选下该项目可见的已审核批次;
// 折叠状态照常可点 —— 卡片仍在 DOM 里,只是 .project-body 被 hidden,
// 所以点一下就能选中折叠中的整批,不必先展开。
function projectSelectBoxHtml(projectId, episodes) {
    const statusFilter = document.getElementById('filter-status')?.value || '';
    if (!selectMode || statusFilter !== 'reviewed') return '';
    const reviewed = episodes.filter(ep => ep.status === 'reviewed' || ep.status === 'approved');
    const disabled = reviewed.length === 0 ? ' disabled' : '';
    return `<input type="checkbox" class="project-checkbox w-3.5 h-3.5 rounded accent-blue-600 flex-shrink-0"
                   data-project-id="${escHtml(projectId)}"${disabled}
                   title="${escHtml(t('select_project_all'))}"
                   onclick="event.stopPropagation();toggleProjectSelect('${escHtml(projectId)}', this.checked)">`;
}

// 该项目在当前视图下可见的、可勾选的批次 id。
function projectReviewedIds(projectId) {
    const group = currentView().groups.find(
        item => String(item.project?.id ?? '') === String(projectId));
    if (!group) return [];
    return group.episodes
        .filter(ep => ep.status === 'reviewed' || ep.status === 'approved')
        .map(ep => String(ep.id));
}

function toggleProjectSelect(projectId, checked) {
    projectReviewedIds(projectId).forEach(id => {
        if (checked) selectedEpisodes.add(id);
        else selectedEpisodes.delete(id);
    });
    syncProjectSelectionDom(projectId);
    updateBatchUI();
}

// 把选中状态同步回 DOM:项目行三态框 + 该项目下每张卡片。只碰受影响的项目,
// 不重建整个列表 —— 这是勾选不再"闪一下"的关键。
function syncProjectSelectionDom(onlyProjectId) {
    document.querySelectorAll('#episode-list-inline .project-group').forEach(group => {
        if (onlyProjectId !== undefined
                && String(group.dataset.projectId) !== String(onlyProjectId)) return;
        group.querySelectorAll('input.batch-checkbox').forEach(box => {
            box.checked = selectedEpisodes.has(String(box.dataset.episodeId));
            box.closest('.episode-card')?.classList.toggle('selected', box.checked);
        });
        syncProjectSelectBox(group);
    });
}

function syncProjectSelectBox(group) {
    const box = group.querySelector('.project-checkbox');
    if (!box) return;
    const ids = projectReviewedIds(box.dataset.projectId);
    const selected = ids.filter(id => selectedEpisodes.has(id)).length;
    box.checked = ids.length > 0 && selected === ids.length;
    box.indeterminate = selected > 0 && selected < ids.length;
    const badge = group.querySelector('.project-select-count');
    if (badge) badge.textContent = selected > 0 ? `${selected}/${ids.length}` : '';
}

function syncProjectSelectDom() {
    syncProjectSelectionDom();
}

function toggleProjectCollapse(projectId) {
    if (_expandedProjects.has(projectId)) _expandedProjects.delete(projectId);
    else _expandedProjects.add(projectId);
    // 只切这一个项目的 body 与箭头:整表重绘会丢掉滚动位置和焦点,
    // 大列表上肉眼可见地闪。
    document.querySelectorAll('#episode-list-inline .project-group').forEach(group => {
        if (String(group.dataset.projectId) !== String(projectId)) return;
        const collapsed = !_expandedProjects.has(projectId);
        group.querySelector('.project-body')?.classList.toggle('hidden', collapsed);
        group.querySelector('.project-caret')
            ?.setAttribute('icon', collapsed ? 'ant-design:right-outlined' : 'ant-design:down-outlined');
    });
}

// Apply small action results locally so the list responds immediately. The
// server remains authoritative; a coalesced silent refresh reconciles state
// after the filesystem-backed hierarchy has caught up.
function renderEpisodeListFromMemory() {
    // updateTaskFilter 可能清掉已失效的 filter-task,所以要在取视图之前调用。
    updateTaskFilter(visibleEpisodesForCurrentView());
    updateEpisodeListCount(allEpisodes, '', document.getElementById('filter-task')?.value || '');
    renderEpisodeCards();
}

function removeEpisodeFromLocalList(episodeId) {
    const id = String(episodeId);
    selectedEpisodes.delete(id);
    _hierarchyRev++;
    hierarchyData.forEach(node => {
        node.episodes = (node.episodes || []).filter(ep => String(ep.id) !== id);
    });
    allEpisodes = allEpisodes.filter(ep => String(ep.id) !== id);
    renderEpisodeListFromMemory();
}

function updateEpisodeInLocalList(episodeId, patch) {
    const id = String(episodeId);
    _hierarchyRev++;
    hierarchyData.forEach(node => {
        const ep = (node.episodes || []).find(item => String(item.id) === id);
        if (ep) Object.assign(ep, patch);
    });
    const flat = allEpisodes.find(ep => String(ep.id) === id);
    if (flat) Object.assign(flat, patch);
    renderEpisodeListFromMemory();
}

function scheduleSilentEpisodeRefresh(delay = 1200) {
    clearTimeout(_silentReloadTimer);
    _silentReloadTimer = setTimeout(() => {
        _silentReloadTimer = null;
        loadEpisodes({ silent: true });
    }, delay);
}

function toggleTaskCollapse(taskId) {
    if (_collapsedTasks.has(taskId)) _collapsedTasks.delete(taskId);
    else _collapsedTasks.add(taskId);
    renderEpisodeListFromMemory();
}

function episodeCardHtml(ep, taskName) {
    const activeClass = currentEpisodeId === ep.id ? 'active' : '';
    // 前端入队标记(点击后立即显示)或后端真实状态(静默刷新后保持显示)
    const isWorkflowQueued = ep._uiWorkflowState === 'queued' || ep.status === 'processing';
    // AI 标注运行中:整批数据等标注完成后才允许查看
    const isAiAnnotating = !isWorkflowQueued && ep.ai_quality_status === 'running';
    const cardClass = (isWorkflowQueued || isAiAnnotating)
        ? 'cursor-default opacity-80'
        : 'cursor-pointer';
    const cardClick = (isWorkflowQueued || isAiAnnotating)
        ? '' : `onclick="selectEpisode('${ep.id}')"`;
    const isReviewed = ep.status === 'reviewed' || ep.status === 'approved';
    const isFailed = ep.status === 'failed';
    const statusDot = (isWorkflowQueued || isAiAnnotating)
        ? '<span class="inline-block w-2 h-2 rounded-full bg-blue-400 mr-1"></span>'
        : (isFailed
        ? '<span class="inline-block w-2 h-2 rounded-full bg-red-400 mr-1"></span>'
        : (isReviewed
            ? '<span class="inline-block w-2 h-2 rounded-full bg-green-400 mr-1"></span>'
            : '<span class="inline-block w-2 h-2 rounded-full bg-yellow-400 mr-1"></span>'));
    const statusText = isWorkflowQueued ? t('processing')
        : (isAiAnnotating ? t('ai_annotating')
        : (isFailed ? t('stat_failed') : (isReviewed ? t('reviewed') : t('reviewing'))));
    const statusColor = (isWorkflowQueued || isAiAnnotating) ? 'text-blue-400'
        : (isFailed ? 'text-red-400' : (isReviewed ? 'text-green-400' : 'text-yellow-400'));
    const cameraCount = (ep.camera_names || []).length;
    const timestamp = ep.timestamp || '';
    const time = timestamp ? timestamp : new Date(ep.created_at).toLocaleDateString('zh-CN');

    const checked = selectedEpisodes.has(ep.id) ? 'checked' : '';
    const checkbox = (isReviewed && selectMode)
        ? `<input type="checkbox" class="batch-checkbox w-3.5 h-3.5 rounded accent-blue-600 flex-shrink-0"
                  data-episode-id="${ep.id}" ${checked}
                  onclick="event.stopPropagation();toggleBatchSelect('${ep.id}', this.checked)">`
        : '';

    const isAnnotationPage = window.EGODATA_PAGE_MODE === 'annotation';
    const statusFilter = document.getElementById('filter-status')?.value
        || new URLSearchParams(document.location.search).get('status')
        || 'completed';
    const isReviewingMode = statusFilter === 'completed' || statusFilter === 'to_review';
    const userRole = String(window.__EGO_USER__?.role || '').toLowerCase();
    const canReprocess = userRole === 'admin' || userRole === 'engineer';
    // processing(含前端入队标记)显示禁用的"处理中"状态按钮,不要求角色;
    // 主动重跑按钮仍只对 admin/engineer 开放且要求 completed/to_review。
    const workflowButton = !isAnnotationPage && isReviewingMode
        ? (isWorkflowQueued
            ? `<button disabled
                       class="mt-1.5 w-full bg-blue-950/60 text-blue-300/80 text-xs px-3 py-1 rounded cursor-wait">
                       <iconify-icon icon="ant-design:loading-outlined" class="icon-sm"></iconify-icon> ${t('processing')}</button>`
            : (canReprocess && (ep.status === 'completed' || ep.status === 'to_review')
                ? `<button onclick="event.stopPropagation();reprocessEpisode('${ep.id}')"
                     title="Re-run the bound workflow for this episode"
                     class="mt-1.5 w-full bg-purple-900/70 hover:bg-purple-800 text-purple-200 text-xs px-3 py-1 rounded">
                     <iconify-icon icon="ant-design:tool-outlined" class="icon-sm"></iconify-icon> ${t('reprocess')}</button>`
                : ''))
        : '';
    const buttons = isAnnotationPage ? '' : (isFailed
        ? `<div class="flex gap-2">
             <button onclick="event.stopPropagation();retryEpisode('${ep.id}')"
                     class="bg-blue-800 hover:bg-blue-700 text-blue-200 text-xs px-3 py-1 rounded flex-1"><iconify-icon icon="ant-design:reload-outlined" class="icon-sm"></iconify-icon> ${t('retry_review')}</button>
             <button onclick="event.stopPropagation();deleteEpisode('${ep.id}')"
                     class="bg-gray-800 hover:bg-red-900 text-gray-400 hover:text-red-300 text-xs px-3 py-1 rounded flex-1"><iconify-icon icon="ant-design:delete-outlined" class="icon-sm"></iconify-icon> ${t('delete')}</button>
           </div>`
        : (isReviewed
            ? `<div class="flex gap-2">
                 <button onclick="event.stopPropagation();downloadEpisode('${ep.id}')"
                         title="${t('export_hint')}"
                         class="bg-blue-800 hover:bg-blue-700 text-blue-200 text-xs px-3 py-1 rounded flex-1"><iconify-icon icon="ant-design:export-outlined" class="icon-sm"></iconify-icon> ${t('export')}</button>
                 <button onclick="event.stopPropagation();unreviewEpisode('${ep.id}')"
                         class="bg-yellow-800 hover:bg-yellow-700 text-yellow-200 text-xs px-3 py-1 rounded flex-1"><iconify-icon icon="ant-design:rollback-outlined" class="icon-sm"></iconify-icon> ${t('unreview')}</button>
               </div>`
            : `<div class="flex gap-2">
                 <button onclick="event.stopPropagation();markReviewed('${ep.id}')"
                         class="bg-green-800 hover:bg-green-700 text-green-200 text-xs px-3 py-1 rounded flex-1"><iconify-icon icon="ant-design:check-outlined" class="icon-sm"></iconify-icon> ${t('approve')}</button>
                 <button onclick="event.stopPropagation();deleteEpisode('${ep.id}')"
                         class="bg-gray-800 hover:bg-red-900 text-gray-400 hover:text-red-300 text-xs px-3 py-1 rounded"><iconify-icon icon="ant-design:delete-outlined" class="icon-sm"></iconify-icon></button>
               </div>${workflowButton}`));

    return `
    <div class="episode-card ${activeClass} p-3 border-b border-gray-800 hover:bg-gray-800/50 ${cardClass} flex gap-2"
         data-episode-id="${ep.id}"
         ${cardClick}
         ${isWorkflowQueued ? `title="${t('processing')}"` : ''}>
        ${checkbox}
        <div class="flex-1 min-w-0">
        <div class="flex items-center gap-1.5 mb-2">
            ${statusDot}
            <span class="text-xs ${statusColor}">${statusText}</span>
        </div>
        <div class="flex items-center gap-1.5 mb-1">
            ${typeof ep.episode_index === 'number'
                ? `<div class="text-sm font-semibold text-blue-300 truncate" title="${ep.name || ''}">#${ep.episode_index}</div>`
                : `<div class="text-sm truncate text-gray-200">${ep.name || taskName || ep.id.slice(0, 8)}</div>`}
        </div>
        <div class="text-xs text-gray-500 space-y-0.5 mb-2">
            <div><iconify-icon icon="ant-design:field-time-outlined" class="icon-sm"></iconify-icon> ${ep.fps || 30}FPS · ${ep.frame_count || 0}${t('frame')}</div>
            ${cameraCount > 0 ? `<div><iconify-icon icon="ant-design:video-camera-outlined" class="icon-sm"></iconify-icon> ${cameraCount} ${t('cameras')}</div>` : ''}
            <div><iconify-icon icon="ant-design:calendar-outlined" class="icon-sm"></iconify-icon> ${time}</div>
        </div>
        ${buttons}
        </div>
    </div>`;
}


// ── Back to list ─────────────────────────────────────

function backToList() {
    // Closing detail must release decoders, 3D canvases, timers, sockets and
    // in-flight media requests. Hiding the panel alone leaves the old video
    // workload alive behind Reviewing/Approved and makes the next click lag.
    if (typeof clearEpisodeWorkspace === 'function') clearEpisodeWorkspace();
    const detail = document.getElementById('episode-detail');
    const listSection = document.getElementById('episode-list-section');
    if (detail) detail.classList.add('hidden');
    if (listSection) listSection.classList.remove('hidden');
    // 返回批次列表 → 隐藏漂浮预览控件(点开视频后才有)
    if (typeof setPreviewPanelsVisible === 'function') setPreviewPanelsVisible(false);
    const warning = document.getElementById('input-warning-banner');
    if (warning) {
        warning.classList.add('hidden');
        warning.innerHTML = '';
    }
}

function renderInputWarning(ep) {
    const warning = document.getElementById('input-warning-banner');
    if (!warning) return;
    const missing = [];
    const matched = [];
    (ep.exceptions || []).filter(ex => ex.kind === 'input_missing').forEach(ex => {
        (ex.missing || []).forEach(value => {
            if (!missing.includes(String(value))) missing.push(String(value));
        });
        (ex.matched || []).forEach(value => {
            if (!matched.includes(String(value))) matched.push(String(value));
        });
    });
    if (!missing.length) {
        warning.classList.add('hidden');
        warning.innerHTML = '';
        return;
    }
    warning.innerHTML =
        '<div class="flex items-start gap-2">' +
            '<iconify-icon icon="ant-design:warning-filled" class="text-amber-300 text-base flex-shrink-0 mt-0.5"></iconify-icon>' +
            '<div class="min-w-0">' +
                '<div class="font-medium text-amber-100">' + escHtml(t('input_missing_title')) + '</div>' +
                '<div class="mt-0.5 text-amber-300/90">' + escHtml(t('input_missing_detail')) + '</div>' +
                '<div class="mt-1 font-mono break-words">' + escHtml(missing.join(', ')) + '</div>' +
                (matched.length ? '<div class="mt-1 text-emerald-300/90">✓ ' + escHtml(matched.join(', ')) + '</div>' : '') +
            '</div>' +
        '</div>';
    warning.classList.remove('hidden');
}

// ── Batch selection ──────────────────────────────────

function toggleBatchSelect(episodeId, checked) {
    const id = String(episodeId);
    if (checked) selectedEpisodes.add(id);
    else selectedEpisodes.delete(id);
    // 只同步这一行与它所属项目的三态框 —— 不重算筛选、不重建列表。
    const card = document.querySelector(
        `#episode-list-inline .episode-card[data-episode-id="${CSS.escape(id)}"]`);
    card?.classList.toggle('selected', checked);
    const group = card?.closest('.project-group');
    if (group) syncProjectSelectBox(group);
    updateBatchUI();
}


function toggleSelectAll(checked) {
    if (!selectMode) return;
    if ((document.getElementById('filter-status')?.value || '') !== 'reviewed') return;

    currentView().reviewedIds.forEach(id => {
        if (checked) selectedEpisodes.add(id);
        else selectedEpisodes.delete(id);
    });
    // 直接写已渲染的 checkbox,不重建整个列表。
    document.querySelectorAll('#episode-list-inline input.batch-checkbox').forEach(box => {
        box.checked = selectedEpisodes.has(String(box.dataset.episodeId));
        box.closest('.episode-card')?.classList.toggle('selected', box.checked);
    });
    syncProjectSelectDom();
    updateBatchUI();
}


function updateBatchUI() {
    const selectAllBar = document.getElementById('select-all-bar');
    const batchBar = document.getElementById('batch-bar');
    const selectBtn = document.getElementById('btn-toggle-select');
    const statusFilter = document.getElementById('filter-status')?.value || '';
    const isReviewedMode = statusFilter === 'reviewed';
    const showSelection = selectMode && isReviewedMode;

    // 「Select」只在非选择模式出现 —— 进入选择模式后由批量栏里的 Cancel 接管。
    if (selectBtn) {
        selectBtn.classList.toggle('hidden', !isReviewedMode || selectMode);
    }
    // Force-exit selection mode when switching away from Reviewed
    if (!isReviewedMode && selectMode) {
        selectMode = false;
        selectedEpisodes.clear();
    }

    if (selectAllBar) {
        selectAllBar.classList.toggle('hidden', !showSelection);
    }
    // 批量栏只要在选择模式就显示(哪怕一集都没选)—— 否则 Cancel 没地方放。
    if (batchBar) {
        batchBar.classList.toggle('hidden', !showSelection);
    }
    if (!showSelection) {
        if (!selectMode) selectedEpisodes.clear();
        return;
    }

    // Update counts
    const view = currentView();
    const reviewedCount = view.reviewedIds.length;
    const selCount = view.reviewedIds.filter(id => selectedEpisodes.has(id)).length;

    const selectCount = document.getElementById('select-count');
    if (selectCount) selectCount.textContent = `(${selCount}/${reviewedCount})`;

    const selectAllCb = document.getElementById('select-all-checkbox');
    if (selectAllCb) {
        selectAllCb.checked = reviewedCount > 0 && selCount === reviewedCount;
        selectAllCb.indeterminate = selCount > 0 && selCount < reviewedCount;
    }

    // 选中规模:让用户在点之前知道这一下要跑多久 —— 打包下载是秒级原样 zip,
    // 导出数据集是整库重建(实测约 30 秒/集)。
    let frames = 0;
    view.reviewedIds.forEach(id => {
        if (selectedEpisodes.has(id)) frames += view.framesById.get(id) || 0;
    });
    const batchCount = document.getElementById('batch-count');
    if (batchCount) {
        batchCount.textContent = t('batch_selected_summary')
            .replace('%s', String(selCount))
            .replace('%f', frames.toLocaleString());
    }
    const exportBtnLabel = document.getElementById('batch-export-label');
    if (exportBtnLabel) {
        const minutes = Math.max(1, Math.round(selCount * 30 / 60));
        exportBtnLabel.textContent = selCount >= 5
            ? `${t('batch_export_dataset')} ~${minutes}${t('minutes_short')}`
            : t('batch_export_dataset');
    }
    // 一集都没选时两个动作按钮禁用(Cancel 仍可点,否则退不出去)。
    const exportBtn = document.getElementById('batch-export-btn');
    if (exportBtn) exportBtn.disabled = selCount === 0;
    const zipBtn = document.getElementById('batch-zip-btn');
    if (zipBtn) zipBtn.disabled = selCount === 0;
}


// 导出数据集:合并成一个可训练数据集。整库重建 —— 每路视频重编码、
// 逐帧重采样、全量重算 stats,实测约 30 秒/集。
async function batchDownload(button = null) {
    if (selectedEpisodes.size === 0) return;
    const ids = Array.from(selectedEpisodes);
    const done = await startReviewExport(ids, null, null, button);
    if (done && selectMode) toggleSelectMode();
}

// 打包下载:每集一个目录原样 zip,不重建,秒级返回。
// 用原生表单提交而非 fetch + blob —— 浏览器自己流式写盘,不会把整个
// zip 缓冲进内存(几十集的包很容易上 GB)。
function batchZipDownload(button = null) {
    if (selectedEpisodes.size === 0) return;
    const form = document.createElement('form');
    form.method = 'POST';
    form.action = '/api/v1/export/batch-download';
    form.style.display = 'none';
    const field = document.createElement('input');
    field.type = 'hidden';
    field.name = 'episode_ids';
    field.value = Array.from(selectedEpisodes).join(',');
    form.appendChild(field);
    document.body.appendChild(form);
    form.submit();
    form.remove();
    if (button) button.blur();
}


function waitForExportJob(jobId, button) {
    const originalHtml = button ? button.innerHTML : null;
    return new Promise(resolve => {
        const poll = async () => {
            try {
                const res = await fetch(`/api/v1/export/${jobId}`);
                const job = await res.json();
                if (job.status === 'completed') {
                    resolve(true);
                    return;
                }
                if (job.status === 'failed') {
                    alert('Export failed: ' + (job.error || 'unknown'));
                    resolve(false);
                    return;
                }
                if (button) {
                    // 导出期间按钮直接显示百分比进度,不再静默等待
                    const pct = Math.round((Number(job.progress) || 0) * 100);
                    button.textContent = 'Exporting… ' + pct + '%';
                }
            } catch (err) {
                alert('Export status failed: ' + err.message);
                resolve(false);
                return;
            }
            setTimeout(poll, 800);
        };
        poll();
    }).finally(() => {
        if (button) {
            if (originalHtml !== null) button.innerHTML = originalHtml;
            button.disabled = false;
            button.dataset.exporting = '';
            button.classList.remove('opacity-60', 'cursor-wait');
        }
    });
}


async function startReviewExport(ids, format, datasetName, button = null) {
    if (!ids || ids.length === 0) return false;
    if (button) {
        button.disabled = true;
        button.dataset.exporting = '1';
        button.classList.add('opacity-60', 'cursor-wait');
    }
    try {
        const payload = {
            episode_ids: ids,
            split_ratio: 0.9,
        };
        if (datasetName) payload.dataset_name = datasetName;
        // Normal review-page exports omit the format deliberately: the
        // backend resolves it from the selected project's workflow node.
        if (format) payload.export_format = format;
        const res = await fetch('/api/v1/export/start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            alert('Export failed: ' + (err.detail || 'unknown'));
            if (button) {
                button.disabled = false;
                button.dataset.exporting = '';
                button.classList.remove('opacity-60', 'cursor-wait');
            }
            return false;
        }
        const job = await res.json();
        const done = await waitForExportJob(job.id, button);
        if (done) {
            window.location.href = `/api/v1/export/download/${job.id}`;
            return true;
        }
        return false;
    } catch (err) {
        alert('Export failed: ' + err.message);
        if (button) {
            button.disabled = false;
            button.dataset.exporting = '';
            button.classList.remove('opacity-60', 'cursor-wait');
        }
        return false;
    }
}


// ── Select / Info Panel ──────────────────────────────

function showAiPendingGate(episodeId) {
    // 全屏等待遮罩:AI 标注未完成时批次数据不可查看。每 4 秒轮询层级
    // 接口,标注结束(非 running)后自动移除遮罩并重新进入批次。
    const listSection = document.getElementById('episode-list-section');
    const detail = document.getElementById('episode-detail');
    if (listSection) listSection.classList.add('hidden');
    if (detail) detail.classList.remove('hidden');
    let overlay = document.getElementById('ai-pending-overlay');
    if (!overlay) {
        overlay = document.createElement('div');
        overlay.id = 'ai-pending-overlay';
        overlay.className = 'fixed inset-0 z-[999] flex flex-col items-center justify-center gap-3 bg-black/85';
        document.body.appendChild(overlay);
    }
    overlay.innerHTML = `
        <!-- 图标须取自 iconify-preload.js 白名单(loading-3-quarters-outlined
             不在表内,会走 api.iconify.design → 遮罩出现时卡一下)。
             用表内的 loading-outlined,配合 animate-spin 视觉一致。 -->
        <iconify-icon icon="ant-design:loading-outlined" class="text-blue-400 text-4xl animate-spin"></iconify-icon>
        <div class="text-gray-200 text-sm">${t('ai_annotating')}</div>
        <div class="text-gray-500 text-xs">${t('ai_annotating_hint')}</div>`;
    if (overlay.dataset.polling === '1') return;
    overlay.dataset.polling = '1';
    (async function poll() {
        for (let i = 0; i < 300; i++) {  // 最多约 20 分钟
            await new Promise(resolve => setTimeout(resolve, 4000));
            try {
                const res = await fetch('/api/v1/projects/hierarchy');
                if (!res.ok) continue;
                const data = await res.json();
                const projects = Array.isArray(data)
                    ? data : (data && data.projects) || [];
                let done = false;
                for (const projectNode of projects) {
                    const hit = (projectNode.episodes || [])
                        .find(e => e.id === episodeId);
                    if (!hit) continue;
                    if (hit.ai_quality_status !== 'running') {
                        done = true;  // passed / failed / null → 可查看
                    } else if (hit.status === 'processing') {
                        done = true;  // 回到 processing,由现有门禁接管
                    }
                    break;
                }
                if (done) {
                    overlay.dataset.polling = '';
                    overlay.remove();
                    if (typeof selectEpisode === 'function') {
                        selectEpisode(episodeId);
                    }
                    return;
                }
            } catch (_) { /* 网络抖动,继续轮询 */ }
        }
        overlay.dataset.polling = '';
        overlay.remove();
        if (typeof backToList === 'function') backToList();
    })();
}

function selectEpisode(episodeId) {
    const ep = allEpisodes.find(e => e.id === episodeId);
    if (!ep) return;
    // Reprocessing keeps the list card visible, but its previous artifacts are
    // no longer a valid review target. Do not reopen stale media while the
    // worker is replacing the processed output.
    if (ep.status === 'processing' || ep._uiWorkflowState === 'queued') {
        backToList();
        return;
    }
    // AI 标注运行中:整批数据等标注完成后才显示(全屏等待遮罩 + 轮询,
    // 完成后自动载入)。
    if (ep.ai_quality_status === 'running') {
        showAiPendingGate(episodeId);
        return;
    }

    // Annotation is a separate workspace: start every selected file with a
    // clean editor while leaving the review page's approval flow untouched.
    if (window.EGODATA_PAGE_MODE === 'annotation' && typeof hideAnnotationForm === 'function') {
        hideAnnotationForm();
    }

    // Highlight card
    document.querySelectorAll('.episode-card').forEach(el => el.classList.remove('active'));
    const card = document.querySelector('.episode-card[data-episode-id="' + episodeId + '"]');
    if (card) card.classList.add('active');

    // Load video
    const cameras = ep.camera_names || [];
    const playbackMeta = { frameCount: ep.frame_count, fps: ep.fps };
    if (typeof loadGroupedEpisodeVideo === 'function') {
        loadGroupedEpisodeVideo(episodeId, cameras, {
            hasSkeleton: Boolean(ep.has_skeleton),
            ...playbackMeta,
        });
    } else {
        loadEpisodeVideo(episodeId, cameras, playbackMeta);
    }

    // Switch to detail view: hide list, show detail panel
    const listSection = document.getElementById('episode-list-section');
    const detail = document.getElementById('episode-detail');
    if (listSection) listSection.classList.add('hidden');
    if (detail) detail.classList.remove('hidden');
    // 进入批次详情(审核/通过/标注页)→ 显示漂浮预览控件
    if (typeof setPreviewPanelsVisible === 'function') setPreviewPanelsVisible(true);
    renderInputWarning(ep);

    // Fill detail header
    const nameEl = document.getElementById('detail-episode-name');
    if (nameEl) {
        const episodeNumber = typeof ep.episode_index === 'number'
            ? `#${ep.episode_index}` : '';
        const title = ep.task_description || ep.name || ep.id.slice(0, 8);
        nameEl.textContent = episodeNumber ? `${episodeNumber} · ${title}` : title;
        nameEl.title = ep.id || title;
    }
    const statusBadge = document.getElementById('detail-status-badge');
    if (statusBadge) {
        const isReviewed = ep.status === 'reviewed' || ep.status === 'approved';
        const isFailed = ep.status === 'failed';
        const isProcessing = ep.status === 'processing';
        statusBadge.className = 'text-xs flex-shrink-0 ' + (isFailed ? 'text-red-400' : (isReviewed ? 'text-green-400' : (isProcessing ? 'text-blue-400' : 'text-yellow-400')));
        statusBadge.textContent = isFailed ? t('stat_failed') : (isReviewed ? t('reviewed') : (isProcessing ? t('processing') : t('reviewing')));
    }

    // Fill detail info
    const infoDiv = document.getElementById('episode-detail-info');
    const meta = ep.meta || {};
    const timestamp = meta.timestamp || '';
    const time = timestamp || new Date(ep.created_at).toLocaleString('zh-CN');
    const isReviewed = ep.status === 'reviewed' || ep.status === 'approved';
    const isFailed = ep.status === 'failed';
    const duration = ep.fps > 0 ? (ep.frame_count / ep.fps).toFixed(1) : '0';
    const cameraList = (ep.camera_names || []).join(', ');

    let cleaningHTML = '';
    if (ep.cleaning_report) {
        const cr = ep.cleaning_report;
        if (cr.passed) {
            cleaningHTML = '<div class="flex items-center gap-1 text-green-400 text-xs"><iconify-icon icon="ant-design:check-circle-filled" class="icon-sm"></iconify-icon> Cleaning passed</div>';
        } else {
            const failList = (cr.checks || []).filter(c => !c.passed).map(c => c.name).join(', ');
            cleaningHTML = '<div class="flex items-center gap-1 text-red-400 text-xs" title="' + failList + '"><iconify-icon icon="ant-design:warning-filled" class="icon-sm"></iconify-icon> Cleaning failed</div>';
        }
    }

    // 上传不匹配/运行失败异常:点击文件后在此详情显示(不在项目列表显示)
    let exceptionHTML = '';
    if (ep.exceptions && ep.exceptions.length) {
        exceptionHTML = ep.exceptions.map(function (ex) {
            const kindLabel = ex.kind === 'run_failed'
                ? t('exception_kind_failed')
                : (ex.kind === 'input_missing' ? t('exception_kind_missing') : t('exception_kind_mismatch'));
            const safeMsg = escHtml(ex.message || '');
            const msg = safeMsg ? ': ' + safeMsg : '';
            const color = ex.kind === 'input_missing' ? 'text-amber-300' : 'text-red-400';
            return '<div class="flex items-center gap-1 ' + color + ' text-xs" title="' + safeMsg + '">' +
                '<iconify-icon icon="ant-design:warning-filled" class="icon-sm"></iconify-icon> ' + kindLabel + msg + '</div>';
        }).join('');
    }

    const isAnnotationPage = window.EGODATA_PAGE_MODE === 'annotation';
    const buttons = isAnnotationPage ? '' : (isFailed
        ? '<button onclick="retryEpisode(\'' + ep.id + '\')" class="flex-1 bg-blue-800 hover:bg-blue-700 text-blue-200 py-1.5 rounded text-xs"><iconify-icon icon="ant-design:reload-outlined" class="icon-sm"></iconify-icon> ' + t('retry_review') + '</button>' +
          '<button onclick="deleteEpisode(\'' + ep.id + '\')" class="bg-gray-800 hover:bg-red-900 text-gray-400 hover:text-red-300 py-1.5 px-3 rounded text-xs"><iconify-icon icon="ant-design:delete-outlined" class="icon-sm"></iconify-icon></button>'
        : (ep.status === 'processing'
        ? '<button disabled class="flex-1 bg-blue-950/60 text-blue-300/80 py-1.5 rounded text-xs cursor-wait"><iconify-icon icon="ant-design:loading-outlined" class="icon-sm"></iconify-icon> ' + t('processing') + '</button>' +
          '<button onclick="deleteEpisode(\'' + ep.id + '\')" class="bg-gray-800 hover:bg-red-900 text-gray-400 hover:text-red-300 py-1.5 px-3 rounded text-xs"><iconify-icon icon="ant-design:delete-outlined" class="icon-sm"></iconify-icon></button>'
        : (isReviewed
        ? '<button onclick="downloadEpisode(\'' + ep.id + '\')" title="' + t('export_hint') + '" class="flex-1 bg-blue-800 hover:bg-blue-700 text-blue-200 py-1.5 rounded text-xs"><iconify-icon icon="ant-design:export-outlined" class="icon-sm"></iconify-icon> ' + t('export') + '</button>' +
          '<button onclick="unreviewEpisode(\'' + ep.id + '\')" class="flex-1 bg-yellow-800 hover:bg-yellow-700 text-yellow-200 py-1.5 rounded text-xs"><iconify-icon icon="ant-design:rollback-outlined" class="icon-sm"></iconify-icon> ' + t('unreview') + '</button>'
        : '<button onclick="markReviewed(\'' + ep.id + '\')" class="flex-1 bg-green-700 hover:bg-green-600 text-white py-1.5 rounded text-xs font-medium"><iconify-icon icon="ant-design:check-outlined" class="icon-sm"></iconify-icon> ' + t('approve') + '</button>' +
          '<button onclick="deleteEpisode(\'' + ep.id + '\')" class="bg-gray-800 hover:bg-red-900 text-gray-400 hover:text-red-300 py-1.5 px-3 rounded text-xs"><iconify-icon icon="ant-design:delete-outlined" class="icon-sm"></iconify-icon></button>')));

    if (infoDiv) {
        infoDiv.innerHTML =
            '<div class="space-y-1.5 text-xs">' +
                (cleaningHTML || '') +
                (exceptionHTML || '') +
                '<div class="text-gray-500 space-y-0.5">' +
                    '<div><iconify-icon icon="ant-design:field-time-outlined" class="icon-sm"></iconify-icon> ' + ep.frame_count + ' frames · ' + ep.fps + ' FPS · ' + duration + 's</div>' +
                    (cameraList ? '<div><iconify-icon icon="ant-design:video-camera-outlined" class="icon-sm"></iconify-icon> ' + cameraList + '</div>' : '') +
                    '<div><iconify-icon icon="ant-design:calendar-outlined" class="icon-sm"></iconify-icon> ' + time + '</div>' +
                    '<div id="episode-detail-sync-status" class="hidden"></div>' +
                    '<div id="episode-detail-ai-status" class="hidden"></div>' +
                '</div>' +
                '<div class="flex items-center gap-2 bg-gray-800 rounded px-3 py-1.5">' +
                    '<span class="text-gray-500">Frame</span>' +
                    '<span id="heatmap-frame" class="text-blue-400 font-mono font-bold text-lg">-</span>' +
                    '<span class="text-gray-600">/ ' + ep.frame_count + '</span>' +
                '</div>' +
                '<div class="flex gap-2">' + buttons + '</div>' +
                '<div id="episode-detail-format"></div>' +
            '</div>';
        if (typeof updateEpisodeDetailSyncStatus === 'function') updateEpisodeDetailSyncStatus();

        // 导出格式徽标(工作流连接驱动:HDF5 / LeRobot v2.1 / v3.0 / Raw 兜底)
        const fmtEl = document.getElementById('episode-detail-format');
        if (fmtEl) {
            fmtEl.innerHTML = '';
            if (!isAnnotationPage && isReviewed) {
                const fmtEpoch = ++_formatEpoch;
                fetch(`/api/v1/export/episode-format/${episodeId}`)
                    .then(r => r.json())
                    .then(d => {
                        if (fmtEpoch !== _formatEpoch) return;  // 已切换批次,丢弃过期响应
                        const el = document.getElementById('episode-detail-format');
                        if (!el) return;
                        const label = escHtml(d.label || 'Raw');
                        const badge = d.available
                            ? '<span class="text-blue-300 font-medium">' + label + '</span>'
                            : '<span class="text-gray-500">' + label + '</span>';
                        el.innerHTML = '<div class="flex items-center gap-1 text-gray-500"><iconify-icon icon="ant-design:export-outlined" class="icon-sm"></iconify-icon> ' +
                            t('export') + ': ' + badge +
                            (d.available ? ' · <button id="btn-re-export" onclick="reExportEpisode()" class="text-blue-400 hover:underline text-xs">' + t('re_export') + '</button>' : '') +
                            '</div>';
                    })
                    .catch(() => {});
            }
        }
    }

// Re-export:只重建导出,不重跑检测(复用最新 run 的检测产物 +
// 当前工作流的导出配置/连线)。Review 详情 Export 徽标旁按钮。
// (挂 window:inline onclick 需要全局可见)
window.reExportEpisode = async function () {
    if (!currentEpisodeId) return;
    const btn = document.getElementById('btn-re-export');
    if (!btn || btn.dataset.busy === '1') return;
    btn.dataset.busy = '1';
    btn.textContent = t('re_exporting') + '…';
    let finalStatus = 'done';
    try {
        const res = await fetch(`/api/v1/export/re-export/${currentEpisodeId}`, { method: 'POST' });
        if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.status);
        for (let i = 0; i < 300; i++) {
            await new Promise(r => setTimeout(r, 1000));
            let st = { status: 'running' };
            try {
                st = await (await fetch(`/api/v1/export/re-export/${currentEpisodeId}/status`)).json();
            } catch (_) { /* keep polling */ }
            if (st.status === 'done' || st.status === 'failed' || st.status === 'interrupted') {
                finalStatus = st.status;
                if (st.status !== 'done') {
                    alert(t('re_export_failed') + ': ' + (st.detail || st.status));
                } else {
                    alert(t('re_export_done') + ': ' + (st.detail || ''));
                }
                break;
            }
        }
    } catch (err) {
        finalStatus = 'failed';
        alert(t('re_export_failed') + ': ' + (err.message || err));
    }
    btn.dataset.busy = '';
    btn.textContent = t('re_export');
    if (finalStatus === 'done') {
        // 刷新徽标(格式可能已变)
        const fmtEpoch = ++_formatEpoch;
        try {
            const d = await (await fetch(`/api/v1/export/episode-format/${currentEpisodeId}`)).json();
            if (fmtEpoch !== _formatEpoch || !currentEpisodeId) return;
            const el = document.getElementById('episode-detail-format');
            if (!el) return;
            const label = escHtml(d.label || 'Raw');
            const badge = d.available
                ? '<span class="text-blue-300 font-medium">' + label + '</span>'
                : '<span class="text-gray-500">' + label + '</span>';
            el.innerHTML = '<div class="flex items-center gap-1 text-gray-500"><iconify-icon icon="ant-design:export-outlined" class="icon-sm"></iconify-icon> ' +
                t('export') + ': ' + badge +
                (d.available ? ' · <button id="btn-re-export" onclick="reExportEpisode()" class="text-blue-400 hover:underline text-xs">' + t('re_export') + '</button>' : '') +
                '</div>';
        } catch (_) { /* 刷新失败下次进入详情自动重取 */ }
    }
}

    // Update frame info header
    const frameHeader = document.getElementById('frame-info-header');
    const totalFramesEl = document.getElementById('detail-total-frames');
    if (frameHeader) frameHeader.classList.remove('hidden');
    if (totalFramesEl) totalFramesEl.textContent = ep.frame_count || 0;

    // Show frame controls
    if (typeof showAnnotationUI === 'function') showAnnotationUI();

    // Annotations are loaded by loadEpisodeVideo after frame data is ready
    // (avoid race: loadAnnotations needs episodeTotalFrames from loadFrameData)
}


// ── Review ──────────────────────────────────────────

async function markReviewed(episodeId) {
    try {
        const res = await fetch(`/api/v1/episode/${episodeId}/review`, { method: 'POST' });
        if (res.ok) {
            backToList();
            removeEpisodeFromLocalList(episodeId);
            scheduleSilentEpisodeRefresh();
        }
    } catch (err) {
        alert('Operation failed: ' + err.message);
    }
}


async function unreviewEpisode(episodeId) {
    try {
        const res = await fetch(`/api/v1/episode/${episodeId}/unreview`, { method: 'POST' });
        if (res.ok) {
            backToList();
            removeEpisodeFromLocalList(episodeId);
            scheduleSilentEpisodeRefresh();
        }
    } catch (err) {
        alert('Operation failed: ' + err.message);
    }
}


async function retryEpisode(episodeId) {
    try {
        const res = await fetch(`/api/v1/episode/${episodeId}/retry`, { method: 'POST' });
        if (res.ok) {
            backToList();
            removeEpisodeFromLocalList(episodeId);
            scheduleSilentEpisodeRefresh();
        }
    } catch (err) {
        alert('Operation failed: ' + err.message);
    }
}


// ── Reprocess(主动重新处理)──────────────────────────

async function reprocessEpisode(episodeId) {
    if (!confirm(t('confirm_reprocess'))) return;
    // The worker will replace the canonical parquet. Do not reuse the old
    // full depth/3D browser buffers when this episode is opened again.
    if (typeof window.invalidateEpisodePlaybackCache === 'function') {
        window.invalidateEpisodePlaybackCache(episodeId);
    }
    // Clear the old detail workspace before waiting for the queue response.
    // Reprocessing is asynchronous; the old media must not remain active
    // while the user navigates to another review section.
    backToList();
    // Mark the local snapshot before the POST resolves. This closes the small
    // window in which a fast click could reopen the old completed artifact.
    updateEpisodeInLocalList(episodeId, { _uiWorkflowState: 'queued' });
    try {
        const res = await fetch(`/api/v1/episode/${episodeId}/reprocess`, { method: 'POST' });
        if (res.ok) {
            scheduleSilentEpisodeRefresh();
        } else {
            updateEpisodeInLocalList(episodeId, { _uiWorkflowState: null });
            const err = await res.json();
            alert(t('reprocess_failed') + (err.detail || res.status));
        }
    } catch (err) {
        updateEpisodeInLocalList(episodeId, { _uiWorkflowState: null });
        alert(t('reprocess_failed') + err.message);
    }
}


// ── Delete ──────────────────────────────────────────

async function deleteEpisode(episodeId) {
    if (!confirm(t('confirm_trash'))) return;

    try {
        const res = await fetch(`/api/v1/episode/${episodeId}/delete`, { method: 'POST' });
        if (res.ok) {
            updateTrashBadge();  // 垃圾桶徽标立即 +1,无需刷新页面
            backToList();       // instant feedback — close detail, show list
            if (currentEpisodeId === episodeId) {
                document.getElementById('video-grid').innerHTML =
                    '<div class="flex items-center justify-center h-64 text-gray-600">' + t('deleted_msg') + '</div>';
                // Clear annotations
                annotations = [];
                if (typeof clearAnnotationOverlay === 'function') clearAnnotationOverlay();
            }
            removeEpisodeFromLocalList(episodeId);
            scheduleSilentEpisodeRefresh();
        }
    } catch (err) {
        alert(t('op_failed') + ': ' + err.message);
    }
}


/** 从失败的响应里取出可读的失败原因。
 *
 * FastAPI 的 ``HTTPException`` 把原因放在 ``detail`` 里（如"Episode is not in
 * trash"、"Episode delete verification failed: [...]"）。拿不到就退回状态码，
 * **绝不能返回空** —— 静默的失败比报错更难查。 */
async function apiErrorDetail(res) {
    try {
        const body = await res.json();
        if (body && body.detail) return String(body.detail);
    } catch (e) { /* 非 JSON 响应 */ }
    return `HTTP ${res.status}`;
}

async function restoreEpisode(episodeId) {
    try {
        const res = await fetch(`/api/v1/episode/${episodeId}/restore`, { method: 'POST' });
        if (res.ok) {
            updateTrashBadge();  // 恢复后徽标立即 -1
            loadTrashList();
            if (typeof refreshReviewTree === 'function') refreshReviewTree();
        } else {
            // ★ 非 2xx 时必须发声。此前只有 if(res.ok) 一个分支 —— 后端返回
            //   409/500 时前端什么都不做，用户看到的是"点了没反应"，也无从查起。
            alert(t('restore_failed') + ': ' + await apiErrorDetail(res));
        }
    } catch (err) {
        alert(t('restore_failed') + ': ' + err.message);
    }
}


async function permanentDeleteEpisode(episodeId) {
    if (!confirm(t('confirm_permanent'))) return;
    try {
        const res = await fetch(`/api/v1/episode/${episodeId}/permanent`, { method: 'DELETE' });
        if (res.ok) {
            updateTrashBadge();
            loadTrashList();
            if (typeof refreshReviewTree === 'function') refreshReviewTree();
        } else {
            alert(t('delete_failed') + ': ' + await apiErrorDetail(res));
        }
    } catch (err) {
        alert(t('delete_failed') + ': ' + err.message);
    }
}


async function purgeTrash() {
    if (!confirm(t('confirm_purge'))) return;
    try {
        const res = await fetch('/api/v1/episodes/purge-trash', { method: 'POST' });
        if (res.ok) {
            updateTrashBadge();
            loadTrashList();
            if (typeof refreshReviewTree === 'function') refreshReviewTree();
        } else {
            alert(t('purge_failed') + ': ' + await apiErrorDetail(res));
        }
    } catch (err) {
        alert(t('purge_failed') + ': ' + err.message);
    }
}


// ── Export ────────────────────────────────────────────

async function downloadEpisode(episodeId) {
    // A single episode already has its workflow-published export product.
    // That endpoint returns the version selected in the workflow export node.
    window.location.href = `/api/v1/export/download-episode/${episodeId}`;
}


// ── Export Page ───────────────────────────────────────

async function loadExportJobs() {
    const listEl = document.getElementById('export-list');
    if (!listEl) return;

    try {
        const res = await fetch('/api/v1/export/list?limit=50');
        const jobs = await res.json();

        if (jobs.length === 0) {
            listEl.innerHTML = '<tr><td colspan="5" class="text-center text-gray-500 py-8">No export jobs</td></tr>';
            return;
        }

        listEl.innerHTML = jobs.map(job => {
            const time = new Date(job.created_at).toLocaleString('zh-CN');
            const statusColors = { pending: 'text-yellow-400', running: 'text-blue-400', completed: 'text-green-400', failed: 'text-red-400' };
            const statusText = { pending: 'Pending', running: 'Running', completed: 'Completed', failed: 'Failed' };
            const progressBar = job.status === 'running'
                ? `<div class="w-full bg-gray-700 rounded h-2"><div class="bg-blue-500 h-2 rounded" style="width:${job.progress}%"></div></div>`
                : job.status === 'completed' ? '100%' : '-';
            const actions = job.status === 'completed'
                ? `<a href="/api/v1/export/download/${job.id}" class="text-blue-400 hover:underline text-xs"><iconify-icon icon="ant-design:download-outlined" class="icon-sm"></iconify-icon> Download</a>`
                : job.status === 'running'
                    ? `<button onclick="loadExportJobs()" class="text-gray-400 hover:underline text-xs">Refresh</button>`
                    : '';

            return `<tr class="border-b border-gray-800">
                <td class="px-4 py-2">${job.dataset_name}</td>
                <td class="px-4 py-2 ${statusColors[job.status]||'text-gray-400'}">${statusText[job.status]||job.status}</td>
                <td class="px-4 py-2">${progressBar}</td>
                <td class="px-4 py-2 text-xs text-gray-400">${time}</td>
                <td class="px-4 py-2">${actions}</td>
            </tr>`;
        }).join('');
    } catch (err) {
        listEl.innerHTML = `<tr><td colspan="5" class="text-center text-red-400 py-8">Load failed</td></tr>`;
    }
}


async function startExport() {
    const name = document.getElementById('new-dataset-name')?.value;
    const ratio = parseFloat(document.getElementById('new-split-ratio')?.value) || 0.9;
    if (!name) return alert('Please enter a dataset name');

    try {
        const res = await fetch('/api/v1/export/start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ dataset_name: name, episode_ids: null, split_ratio: ratio }),
        });
        if (res.ok) { document.getElementById('new-dataset-name').value = ''; loadExportJobs(); }
        else { const err = await res.json(); alert('Export creation failed: ' + (err.detail || 'unknown')); }
    } catch (err) {
        alert('Export creation failed: ' + err.message);
    }
}


// ── Init ────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
    // Apply data-i18n translations
    document.querySelectorAll('[data-i18n]').forEach(el => {
        el.textContent = t(el.dataset.i18n);
    });
    document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
        el.placeholder = t(el.dataset.i18nPlaceholder);
    });
    // Init nav labels
    const navReview = document.getElementById('nav-review');
    const navTrash = document.getElementById('nav-trash');
    if (navReview) navReview.textContent = t('video_review');
    if (navTrash) navTrash.textContent = t('trash');

    if (document.getElementById('episode-list-inline')) {
        initStatusFromURL();
        installReviewStatusNavigation();
        loadEpisodes();
        setInterval(() => { loadEpisodes({ silent: true }); updateTrashBadge(); }, 15000);
        updateTrashBadge();
        // 两个批量按钮的提示:说清一个是重建、一个是原样打包。
        const exportBtn = document.getElementById('batch-export-btn');
        if (exportBtn) exportBtn.title = t('batch_export_hint');
        const zipBtn = document.getElementById('batch-zip-btn');
        if (zipBtn) zipBtn.title = t('batch_zip_hint');
    }
    if (document.getElementById('export-list')) {
        loadExportJobs();
        setInterval(loadExportJobs, 10000);
    }
    if (document.getElementById('trash-list')) {
        loadTrashList();
    }
});


async function loadTrashList() {
    const listEl = document.getElementById('trash-list');
    if (!listEl) return;

    listEl.innerHTML = `<div class="p-4 text-center text-gray-500 text-sm">${t('loading')}</div>`;

    try {
        const res = await fetch('/api/v1/episodes?status=deleted&limit=200');
        const data = await res.json();
        const episodes = data.episodes || [];

        if (episodes.length === 0) {
            listEl.innerHTML = `<div class="p-8 text-center text-gray-500"><iconify-icon icon="ant-design:delete-outlined" class="icon-md"></iconify-icon> ${t('trash_empty')}</div>`;
        } else {
            const now = new Date();
            listEl.innerHTML = episodes.map(ep => {
                const delTime = new Date(ep.deleted_at);
                const daysLeft = Math.max(0, Math.ceil(7 - (now - delTime) / (1000 * 60 * 60 * 24)));
                const time = new Date(ep.created_at).toLocaleString('zh-CN');
                return `
                <div class="bg-gray-800 rounded p-4 flex items-center justify-between">
                    <div class="flex-1">
                        <div class="text-sm font-medium text-gray-200">${ep.task_description || ep.name || 'unknown'}</div>
                        <div class="text-xs text-gray-500 mt-1">
                            <iconify-icon icon="ant-design:field-time-outlined" class="icon-sm"></iconify-icon> ${ep.fps}FPS · ${ep.frame_count}${t('frame')} · <iconify-icon icon="ant-design:video-camera-outlined" class="icon-sm"></iconify-icon> ${(ep.camera_names||[]).join(', ') || t('not_found')}
                        </div>
                        <div class="text-xs text-gray-500"><iconify-icon icon="ant-design:calendar-outlined" class="icon-sm"></iconify-icon> ${time}</div>
                    </div>
                    <div class="text-xs text-gray-400 mr-4"><iconify-icon icon="ant-design:hourglass-outlined" class="icon-sm"></iconify-icon> ${t('days_left')} ${daysLeft} ${t('day_unit')}</div>
                    <div class="flex gap-2">
                        <button onclick="restoreEpisode('${ep.id}')"
                                class="bg-green-700 hover:bg-green-600 text-white text-xs px-3 py-1.5 rounded"><iconify-icon icon="ant-design:rollback-outlined" class="icon-sm"></iconify-icon> ${t('restore')}</button>
                        <button onclick="permanentDeleteEpisode('${ep.id}')"
                                class="bg-red-800 hover:bg-red-700 text-red-200 text-xs px-3 py-1.5 rounded"><iconify-icon icon="ant-design:delete-outlined" class="icon-sm"></iconify-icon> ${t('permanent_delete')}</button>
                    </div>
                </div>`;
            }).join('');
        }
        updateTrashBadge();
    } catch (err) {
        listEl.innerHTML = `<div class="p-4 text-center text-red-400 text-sm">${t('load_failed')}</div>`;
    }
}


// updateTrashBadge 由 base.html 全局提供(所有页面生效),删除/恢复/清空后
// 各操作函数调用它立即刷新徽标,无需刷新页面。
