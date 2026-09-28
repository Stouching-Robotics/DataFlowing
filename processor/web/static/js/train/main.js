/** 训练页入口 —— 取数据、画任务头、组织页签。
 *
 * 这是 `static/js/train/` 下唯一的入口,由 `templates/train.html` 以
 * `<script type="module" src=".../main.js?v={{ asset_version }}">` 引入。
 * 其余文件都是被它 import 的模块 —— 新页面不再往 app.js 里堆代码。
 *
 * 缓存注意:子模块的相对 import 解析出的 URL 不带查询串,靠 `main.py` 里
 * `static_cache_headers` 对「/static/js/ 下无查询串的 JS」发 no-cache 来保证
 * 不会出现新旧模块混用。改动中间件前先看那段注释。
 */

import { listRuns } from './api.js';
import { el, clear } from '../shared/dom.js';
import { bytes } from '../shared/format.js';
import { tr } from '../shared/text.js';
import { renderCheckpoints } from './checkpoints.js';
import { renderParams } from './params.js';
import { renderNewRun } from './newrun.js';
import { renderInference } from '../inference/view.js';

/**
 * 页签定义。
 *
 * ⚠ **「训练指标」与「实时日志」故意不在这里。** 训练由官方 `lerobot-train`
 * 在平台之外执行,没有保存 loss/lr/grad_norm 曲线,也没有留日志 —— 没有数据
 * 的页签就不显示(摆一个空页签比不摆更让人以为功能坏了)。将来接上训练执行、
 * 有了指标落盘,把这两项加回数组即可,`available()` 会自动判断。
 */
const TABS = [
  {
    id: 'checkpoints',
    label: () => tr('train_tab_checkpoints', 'Model Output'),
    available: (run) => (run.checkpoints || []).length > 0,
    render: (panel, run, ctx) => renderCheckpoints(panel, run, {
      onInfer: (checkpoint) => ctx?.goToInference?.(run.name, checkpoint.name),
    }),
  },
  {
    id: 'params',
    label: () => tr('train_tab_params', 'Training Config'),
    available: (run) => {
      const newest = (run.checkpoints || [])[0];
      return !!newest && Object.keys(newest.train_config || {}).length > 0;
    },
    render: (panel, run) => {
      const newest = run.checkpoints[0];
      renderParams(panel, newest.train_config, { step: newest.step });
    },
  },
];

function field(label, value, extraClass = '') {
  return el('div', { class: `flex items-baseline gap-2 text-xs ${extraClass}` }, [
    el('span', { class: 'text-gray-500', text: `${label}:` }),
    el('span', { class: 'text-gray-300 font-mono break-all', text: value }),
  ]);
}

/** 任务头:名字 + 模型类型 + 产物概况 + 输出目录。 */
function renderRunHeader(run, root) {
  const newest = (run.checkpoints || [])[0];
  return el('div', { class: 'bg-gray-900 rounded-lg border border-gray-800 p-4 mb-4' }, [
    el('div', { class: 'flex items-start flex-wrap gap-4' }, [
      el('div', { class: 'min-w-0' }, [
        el('div', { class: 'text-base font-semibold text-gray-100 font-mono', text: run.name }),
        el('div', {
          class: 'text-xs text-gray-500 mt-1',
          text: `${run.checkpoint_count} 个 checkpoint · ${bytes(run.total_bytes)}`
            + (newest ? ` · 最新 ${newest.name} 步` : ''),
        }),
      ]),
      el('div', { class: 'ml-auto flex flex-col gap-1 items-end' }, [
        field(tr('train_label_model', 'Model'), run.model_type || '—'),
        field(tr('train_label_location', 'Location'), root, 'text-gray-500'),
      ]),
    ]),
  ]);
}

/** 页签态样式。集中在这里,是因为切换时只改 className —— 重建 DOM 会丢掉
 *  各页签自己的展开/折叠状态(文件明细表默认展开最新那个)。 */
const ACTIVE_TAB = 'px-3 py-1.5 text-xs rounded-t border-b-2 -mb-px transition-colors '
  + 'border-blue-500 text-blue-300 font-medium';
const IDLE_TAB = 'px-3 py-1.5 text-xs rounded-t border-b-2 -mb-px transition-colors '
  + 'border-transparent text-gray-500 hover:text-gray-300';

/** 页签栏 + 面板。只渲染 available() 为真的页签。 */
function renderTabs(container, run, ctx = {}) {
  clear(container);

  const usable = TABS.filter((tab) => tab.available(run));
  if (!usable.length) {
    container.appendChild(el('div', {
      class: 'text-center text-gray-600 text-sm py-10',
      text: tr('train_no_output', 'This run has no checkpoints to show.'),
    }));
    return;
  }

  const bar = el('div', { class: 'flex gap-2 px-1 border-b border-gray-800 mb-4' });
  const panels = el('div', {});

  const buttons = usable.map((tab, index) => {
    const panel = el('div', { class: index === 0 ? '' : 'hidden' });
    panels.appendChild(panel);
    tab.render(panel, run, ctx);

    const button = el('button', {
      class: 'px-3 py-1.5 text-xs rounded-t border-b-2 -mb-px transition-colors',
      text: tab.label(),
    });
    button.addEventListener('click', () => {
      // 只改 class,不重建面板 —— 重建会丢掉各页签自己的展开/折叠状态。
      buttons.forEach((other, i) => {
        other.className = i === index ? ACTIVE_TAB : IDLE_TAB;
      });
      usable.forEach((_, i) => {
        panels.children[i].classList.toggle('hidden', i !== index);
      });
    });
    bar.appendChild(button);
    return button;
  });

  buttons[0].className = ACTIVE_TAB;
  container.appendChild(bar);
  container.appendChild(panels);
}

export function emptyState(message) {
  return el('div', { class: 'text-center text-gray-600 text-sm py-16' }, [
    el('div', { class: 'text-3xl text-gray-600 mb-3' }, [
      el('iconify-icon', { icon: 'ant-design:folder-outlined' }),
    ]),
    el('div', { text: message }),
  ]);
}

/** 「模型训练」页签的内容 —— 每次调用重新拉一次数据并重画。
 *  ``ctx.goToInference`` 是 boot() 注入的跨页签跳转能力(检查点行的「推理」按钮用)。 */
async function renderRunsPanel(host, _arg, ctx = {}) {
  clear(host);
  host.appendChild(el('div', {
    class: 'text-center text-gray-600 text-sm py-10',
    text: tr('loading', 'Loading...'),
  }));

  let payload;
  try {
    payload = await listRuns();
  } catch (err) {
    clear(host);
    host.appendChild(el('div', {
      class: 'text-center text-red-400 text-sm py-10',
      text: `${tr('train_load_failed', 'Failed to load training runs')}: ${err.message}`,
    }));
    return;
  }

  clear(host);

  if (!payload.exists) {
    // 目录不存在是正常的(新机器/本机没训过),说清楚路径比说"没有数据"有用 ——
    // 用户能立刻判断是不是路径配错了。
    host.appendChild(emptyState(
      `${tr('train_no_root', 'No training directory on this host')}: ${payload.root}`,
    ));
    return;
  }
  if (!payload.runs.length) {
    host.appendChild(emptyState(
      `${tr('train_no_runs', 'No training output found under')} ${payload.root}`,
    ));
    return;
  }

  for (const run of payload.runs) {
    host.appendChild(renderRunHeader(run, payload.root));
    const tabs = el('div', {});
    renderTabs(tabs, run, ctx);
    host.appendChild(tabs);
  }
}

/** 顶部一级页签 —— 切的是**整块内容**(已有产物 / 新建向导 / 推理),
 *  跟卡片里「模型输出 / 训练参数」那层子页签不是一回事。
 *
 *  推理曾经是侧边栏里独立的一项(独立页面 + 独立路由),后来并进来 —— 它和训练
 *  本来就是同一批产物的上下游,分两个导航项反而要来回跳。侧边栏现在只有
 *  「模型」一个一级项,点进来就是这三个页签。 */
// 顺序即**展示顺序**,也决定**默认打开哪个** —— 下面 boot() 里把第 0 个设为激活。
// 用户指定:新建训练 → 模型训练 → 模型推理。
const PAGE_TABS = [
  {
    id: 'new',
    label: () => tr('train_page_new', 'New Training'),
    icon: 'ant-design:plus-outlined',
    render: renderNewRun,
  },
  {
    id: 'runs',
    label: () => tr('train_page_runs', 'Model Training'),
    icon: 'ant-design:ordered-list-outlined',
    render: renderRunsPanel,
  },
  {
    id: 'inference',
    label: () => tr('train_page_inference', 'Model Inference'),
    icon: 'ant-design:play-circle-outlined',
    render: renderInference,
  },
];

const PAGE_ACTIVE = 'px-4 py-2 text-sm rounded-t border-b-2 -mb-px transition-colors '
  + 'border-blue-500 text-blue-300 font-semibold flex items-center gap-1.5';
const PAGE_IDLE = 'px-4 py-2 text-sm rounded-t border-b-2 -mb-px transition-colors '
  + 'border-transparent text-gray-500 hover:text-gray-300 flex items-center gap-1.5';

function boot() {
  const host = document.getElementById('train-runs');
  if (!host) return;
  clear(host);

  const bar = el('div', { class: 'flex gap-1 border-b border-gray-800 mb-5' });
  const panels = el('div', {});
  const INFERENCE_INDEX = PAGE_TABS.findIndex((t) => t.id === 'inference');

  /** 切到某个页签。
   *
   *  ``arg`` 有值时**强制重渲染**该面板 —— 用于「在检查点行上点『推理』,跳到
   *  推理页签并选中它」:那个面板很可能早就渲染过了(用户先前点过),不重渲染
   *  的话带不进去这个参数。
   *
   *  没有 arg 时仍然是**第一次显示才渲染** —— 否则一进页面就把三个页签的数据
   *  全拉一遍(其中推理页要拉 runs + datasets 两份)。
   */
  function selectTab(index, arg) {
    buttons.forEach((other, i) => {
      other.className = i === index ? PAGE_ACTIVE : PAGE_IDLE;
    });
    for (let i = 0; i < panels.children.length; i += 1) {
      panels.children[i].classList.toggle('hidden', i !== index);
    }
    const panel = panels.children[index];
    if (arg !== undefined || !panel.dataset.rendered) {
      panel.dataset.rendered = '1';
      // 第三个参数是**导航能力**,注入给需要跨页签跳转的面板(见下面的 TABS)。
      PAGE_TABS[index].render(panel, arg, {
        goToInference: (runName, checkpointName) => selectTab(
          INFERENCE_INDEX, { runName, checkpointName }),
      });
    }
  }

  const buttons = PAGE_TABS.map((tab, index) => {
    const panel = el('div', { class: index === 0 ? '' : 'hidden' });
    panels.appendChild(panel);

    const button = el('button', { class: PAGE_IDLE, title: tab.label() }, [
      el('iconify-icon', { icon: tab.icon, class: 'icon-sm' }),
      el('span', { text: tab.label() }),
    ]);
    button.addEventListener('click', () => selectTab(index));
    bar.appendChild(button);
    return button;
  });

  host.appendChild(bar);
  host.appendChild(panels);

  // 首个页签立即渲染;其余等第一次显示
  selectTab(0);
}

// ★ 必须等 DOMContentLoaded,不能在模块求值时直接跑。
//
// ES module 是 **deferred** 的:它在"文档解析完、DOMContentLoaded 还没触发"这个
// 中间态执行,此时 readyState === 'interactive' —— 所以原来那句
// `if (readyState === 'loading')` 为假,直接走了 else 分支**当场渲染**。
//
// 而 i18n.js 要到自己的 DOMContentLoaded 回调里才从 localStorage 读出语言
// (它的 currentLang 默认写死 'en')。于是**同步渲染的那批先出来,是英文**,
// 而 await 之后才渲染的那批(子页签)已经是中文了 —— 同一页上半英半中。
//
// 判据用 !== 'complete' 而不是 === 'loading':interactive 也算"还没 fire",
// 这时挂监听一定会被调到。只有文档已经彻底加载完(动态 import 进来)才直接跑。
if (document.readyState !== 'complete') {
  document.addEventListener('DOMContentLoaded', boot);
} else {
  boot();
}

// 切语言后重画。本页内容全是 JS 现画的,i18n.js 的 setLang() 只会去翻 DOM 上的
// data-i18n 属性,翻不到动态生成的这些节点 —— 不重跑一遍 boot(),页面会半中半英。
// 其它动态页面(dashboard / tasks / users)也是这么接这个事件的。
window.addEventListener('egodata:lang', boot);
