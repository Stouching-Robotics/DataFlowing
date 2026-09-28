/* i18n — 中英文切换 */

// 词典**不在这里** —— 见同目录的 i18n-data.js。
// 它同时被服务端读取(app/i18n.py),而服务端是 Python;两边读同一份才不会分叉。
// 那个文件在 base.html 里**先于本文件**同步加载,所以这里读得到。
// 真读不到(比如有人单独引这个脚本)时退化成空字典 —— 页面照常起得来,
// t() 会返回 key 本身,一眼看得出是加载顺序出了问题。
const I18N = (typeof window !== 'undefined' && window.__I18N__) || { zh: {}, en: {} };

/**
 * 客户端语言**以服务端渲染时用的那个为准** —— 它就写在 `<html lang>` 上。
 *
 * ★ 不能只读 localStorage。服务端是从 **cookie** 读语言的,两者可能不一致
 *   (比如清了 localStorage 但 cookie 还在)。一旦不一致,下面的
 *   applyTranslations() 会把服务端刚渲染好的中文**又翻回英文** —— 页面反而
 *   变成了"先中文后英文",比原来的闪烁更难看。
 *
 * 以 <html lang> 为准还有个好处:它和 DOM 里已有的文案**必然一致**(服务端就是
 * 按它渲染的),所以首次 applyTranslations() 是个空操作。
 *
 * <head> 里的脚本执行时 <html> 已经解析完了,所以这里读得到。
 */
function _initialLang() {
    const fromHtml = String(document.documentElement.lang || '').toLowerCase();
    if (I18N[fromHtml]) return fromHtml;
    try {
        const saved = localStorage.getItem('lang');
        if (saved && I18N[saved]) return saved;
    } catch (e) { /* 隐私模式禁 storage */ }
    return 'en';
}

let currentLang = _initialLang();

function t(key) {
    return (I18N[currentLang] && I18N[currentLang][key]) || I18N['zh'][key] || key;
}

/** 把静态标注（data-i18n / data-i18n-placeholder）应用到 DOM。 */
function applyTranslations() {
    document.querySelectorAll('[data-i18n]').forEach(el => {
        el.textContent = t(el.dataset.i18n);
    });
    document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
        el.placeholder = t(el.dataset.i18nPlaceholder);
    });
    // title 属性此前没人处理 —— 只有 data-i18n（写 textContent）和 placeholder 两种，
    // 于是带提示的按钮/图标没法靠标记翻译，只能写死中文。
    document.querySelectorAll('[data-i18n-title]').forEach(el => {
        el.title = t(el.dataset.i18nTitle);
    });
}

function setLang(lang) {
    if (!I18N[lang]) return;                 // 只认字典里有的语言
    currentLang = lang;
    try { localStorage.setItem('lang', lang); } catch (e) { /* 隐私模式禁 storage */ }
    // ★ 同时写 cookie —— **服务端渲染时要读它**,否则每次整页导航都会先渲染
    //   默认语言、再由 JS 纠正,表现为"闪一下另一种语言"。
    //   localStorage 在这里已经不够用了:它是浏览器本地的,服务端看不到。
    //   max-age 一年;SameSite=Lax 免得外站发起的请求也带上这个 cookie。
    try {
        document.cookie = `lang=${encodeURIComponent(lang)}; path=/; max-age=31536000; SameSite=Lax`;
    } catch (e) { /* 禁 cookie:退回旧行为,靠上面的 applyTranslations() 事后翻 */ }
    // 通知**同页面里的其他应用**：工作流画布是独立的 React 应用，不加载本文件，
    // 它靠这个事件跟着切。不能用 storage 事件 —— 那个只在**别的标签页**触发，
    // 而切换按钮就在本页侧边栏上。
    try {
        window.dispatchEvent(new CustomEvent('egodata:lang', { detail: { lang } }));
    } catch (e) { /* 老浏览器不支持 CustomEvent 构造：画布不跟随，其余照常 */ }
    applyTranslations();
    // Update header nav
    const navReview = document.getElementById('nav-review');
    const navTrash = document.getElementById('nav-trash');
    if (navReview) navReview.textContent = t('video_review');
    if (navTrash) navTrash.innerHTML = '<iconify-icon icon="ant-design:delete-outlined" class="icon-sm"></iconify-icon> ' + t('trash');
    // Update trash page
    const trashTitle = document.getElementById('trash-title');
    const trashPurge = document.getElementById('trash-purge-btn');
    if (trashTitle) trashTitle.textContent = t('trash_title');
    if (trashPurge) trashPurge.textContent = t('purge_all');
    // Reload dynamic UI
    if (typeof loadTasks === 'function') loadTasks();
    if (typeof loadProjects === 'function') loadProjects();
    if (typeof loadDevices === 'function') loadDevices();
    if (typeof loadEpisodes === 'function') loadEpisodes();
    if (typeof loadTrashList === 'function') loadTrashList();
    if (typeof renderSourceBar === 'function') renderSourceBar();
    if (typeof updateInfoBar === 'function') updateInfoBar();
    // 首页概览的环形图/柱状图/状态徽标都是 JS 现画的，不重跑就不跟着切语言
    if (typeof refreshDashboard === 'function') refreshDashboard();
    // 用户列表同样是 JS 现画的（表头是静态标注，行内容是动态的）
    if (typeof loadUsers === 'function') loadUsers();
    updateLangToggle();
}

function updateLangToggle() {
    const btn = document.getElementById('lang-toggle');
    if (btn) {
        btn.textContent = currentLang === 'zh' ? 'EN' : '中';
    }
    // 用户菜单里的语言选项：高亮当前项。用的是 class 切换而不是重建 DOM ——
    // 菜单是常驻元素，重建会丢掉挂在它上面的监听。
    document.querySelectorAll('[data-lang-option]').forEach(el => {
        const active = el.dataset.langOption === currentLang;
        el.setAttribute('aria-pressed', active ? 'true' : 'false');
        el.classList.toggle('text-cyan-300', active);
        el.classList.toggle('border-cyan-700', active);
        el.classList.toggle('bg-cyan-900/30', active);
        el.classList.toggle('text-gray-400', !active);
        el.classList.toggle('border-gray-700', !active);
    });
}

// Apply translations on every page at load.
//
// ★ 必须**先读回上次选的语言**：setLang 一直在写 localStorage，但此前没有任何
//   地方读它 —— currentLang 写死 'en'，于是每次刷新都退回英文，切换等于没切。
document.addEventListener('DOMContentLoaded', () => {
    // currentLang 在上面就用 _initialLang() 定好了(以 <html lang> 为准),
    // 这里**不再从 localStorage 覆盖** —— 那正是会把服务端渲染好的语言翻回去的操作。
    // 语言由服务端按 cookie 渲染,客户端只负责后续切换。
    applyTranslations();
    updateLangToggle();
});
