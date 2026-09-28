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
import { renderJobs } from './jobs.js';
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

/** checkpoint 数量的说法 —— 中英的数词位置不同,所以走 ``%s`` 词条而不是拼接。
 *  (这里原先是硬编码的 ``N 个 checkpoint``:在英文界面里会冒出一句中文。) */
function ckptCount(run) {
  return tr('train_ckpt_count', '%s checkpoints').replace('%s', run.checkpoint_count);
}

/** 选中 run 的头部:名字 + 模型类型 + 产物概况。
 *
 *  **不显示位置** —— 那是训练根目录,所有 run 共用一个值,现在提到列表上方显示
 *  一次。原先每个 run 卡片都印一遍,多 run 时看起来像"每个模型各存一处",
 *  其实指向的是同一个路径。 */
function renderRunDetail(run) {
  const newest = (run.checkpoints || [])[0];
  return el('div', { class: 'bg-gray-900 rounded-lg border border-gray-800 p-4 mb-4' }, [
    el('div', { class: 'min-w-0' }, [
      el('div', { class: 'flex items-center gap-2 flex-wrap' }, [
        el('span', { class: 'text-base font-semibold text-gray-100 font-mono', text: run.name }),
        run.model_type
          ? el('span', {
            class: 'text-[10px] px-1.5 py-0.5 rounded bg-blue-900/50 text-blue-300',
            text: run.model_type,
          })
          : null,
      ]),
      el('div', {
        class: 'text-xs text-gray-500 mt-1',
        text: `${ckptCount(run)} · ${bytes(run.total_bytes)}`
          + (newest
            ? ` · ${tr('train_latest_step', 'latest step %s').replace('%s', newest.name)}`
            : ''),
      }),
    ]),
  ]);
}

/** 左侧 run 列表的一项。 */
function runListItem(run, active, onSelect) {
  const item = el('button', {
    class: 'text-left px-3 py-2 rounded border transition-colors shrink-0 '
      + 'w-44 md:w-full '
      + (active ? 'border-blue-600 bg-blue-950/30' : 'border-gray-800 hover:border-gray-700'),
  }, [
    el('div', {
      class: `text-xs font-mono truncate ${active ? 'text-blue-200' : 'text-gray-200'}`,
      text: run.name,
    }),
    el('div', {
      class: 'text-[10px] text-gray-500 mt-0.5',
      text: `${ckptCount(run)} · ${bytes(run.total_bytes)}`,
    }),
  ]);
  item.addEventListener('click', onSelect);
  return item;
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

  // ── 布局:左列表 + 右详情 ────────────────────────────────
  //
  // 曾经是**每个 run 摞一整套**(头部卡 + 子页签 + 检查点表)。模型一多,页面
  // 就无限变长,而且每个头部卡都要印一遍位置和模型类型 —— 位置是整页共用的
  // 训练根目录,重复 N 遍只会让人以为"每个模型各存一处"。
  //
  // 改成主从式之后:位置显示一次、页面长度恒定、只渲染选中的那个 run。
  // 窄屏(<768px)降级成上下堆叠,列表变横向可滚 —— 小屏放不下两栏。
  // 只留标题。**不显示训练根目录** —— 它是服务端配置(settings.TRAIN_OUTPUT_ROOT),
  // 属于部署细节而不是产物信息,每个用户都对着一条绝对路径没有意义。
  // 唯一需要看到它的是"目录不存在"那种排障场景 —— 那里的空态仍然带着完整路径,
  // 因为那时路径本身就是答案(配错了地方)。见下面的 !payload.exists 分支。
  host.appendChild(el('div', { class: 'mb-4' }, [
    el('span', { class: 'text-sm font-semibold text-gray-300', text: tr('train_runs_title', 'Trained models') }),
  ]));

  const listHost = el('div', {
    class: 'flex md:flex-col gap-2 overflow-x-auto md:overflow-visible '
      + 'w-full md:w-48 shrink-0 pb-1 md:pb-0',
  });
  const detailHost = el('div', { class: 'flex-1 min-w-0 w-full' });
  host.appendChild(el('div', {
    class: 'flex flex-col md:flex-row gap-4 items-start',
  }, [listHost, detailHost]));

  // 默认选中第一个。list_runs() 的 run 顺序由后端定(见 app/training/runs.py)。
  let selected = payload.runs[0].name;

  function paint() {
    clear(listHost);
    for (const run of payload.runs) {
      listHost.appendChild(runListItem(run, run.name === selected, () => {
        if (run.name === selected) return;   // 点自己不必重画,免得丢失展开状态
        selected = run.name;
        paint();
      }));
    }
    clear(detailHost);
    const run = payload.runs.find((r) => r.name === selected) || payload.runs[0];
    detailHost.appendChild(renderRunDetail(run));
    const tabs = el('div', {});
    renderTabs(tabs, run, ctx);
    detailHost.appendChild(tabs);
  }

  paint();
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
  {
    // 「训练任务」= **任务**,与上面「模型训练」的**产物**是两回事:一个失败的、
    // 或者刚提交还没写出任何 checkpoint 的任务,只会出现在这里。提交成功后前端
    // 会自动跳过来(见 newrun.js 的 ctx.goToJobs)。
    id: 'jobs',
    label: () => tr('train_page_jobs', 'Training Jobs'),
    icon: 'ant-design:thunderbolt-outlined',
    render: renderJobs,
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
  const JOBS_INDEX = PAGE_TABS.findIndex((t) => t.id === 'jobs');

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
        // 「新建训练」提交成功后跳到任务列表,并把刚提交的那个任务带过去。
        // **必须传 arg**:按 selectTab 的规则,不带 arg 时只在**首次显示**才
        // 渲染 —— 用户之前点过这个页签的话,新任务根本不会出现。传对象
        // 同时满足两件事:强制重渲染 + 让任务页直接展开这一条。
        goToJobs: (jobId) => selectTab(JOBS_INDEX, { focus: jobId || null }),
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
