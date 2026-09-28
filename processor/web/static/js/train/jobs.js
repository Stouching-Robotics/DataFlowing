/** 「训练任务」页签 —— 任务列表 + 详情 + 实时日志。
 *
 * 与「模型训练」页签的分工:**那边列的是磁盘上的产物,这边列的是任务**。
 * 一个失败的任务、或者刚提交还没写出任何 checkpoint 的任务,只会出现在这里。
 *
 * ## 轮询与 epoch 守卫
 *
 * 训练几小时,状态要持续刷新。做法和导出页一致:**轮询**,不用 WebSocket
 * (本仓库唯一的 WS 只做"标注变化"通知,不承载进度)。
 *
 * 每次重新渲染本页会生成一个新的 token,旧的轮询循环一发现 ``token`` 已经
 * 不是当前的就自己退出。没有这层守卫的话,用户来回切页签会积下多个循环:
 * 它们同时往**同一个 DOM 节点**里 append 日志,越滚越快,还会互相覆盖状态。
 * (同一个坑在审核页踩过 —— 见 player.js / heatmap.js 的 ``currentEpisodeId`` 守卫。)
 *
 * ## 状态更新只动该动的地方
 *
 * 轮询回来时**不重建详情面板**,只更新徽标和进度条 —— 重建会把用户正在看的
 * 日志清空、滚动位置也丢。所以那两处各留一个**固定的空容器**,引用一直拿着,
 * 不靠 ``children[i]`` 下标去找:``el()`` 会跳过 null 子节点(shared/dom.js:57),
 * 有条件的节点一缺,下标就全错位了。
 */

import { el, clear } from '../shared/dom.js';
import { dateTime } from '../shared/format.js';
import { tr } from '../shared/text.js';
import {
  cancelTrainingJob, getTrainingJob, listTrainingJobs, trainingJobLog,
} from './api.js';

/** 轮到本页渲染的令牌。旧循环发现自己不再是它,就安静退出。 */
let _token = null;

const POLL_MS = 1000;
/** 日志最多在 DOM 里留这么多字符 —— 20000 步的 tqdm 输出能到几 MB,
 *  全塞进 <pre> 会让浏览器越用越卡。只留尾部。 */
const LOG_DOM_LIMIT = 120_000;

const ACTIVE_STATUSES = ['queued', 'running'];

const STATUS_STYLE = {
  queued: ['bg-gray-700 text-gray-300', 'train_job_queued', 'Queued'],
  running: ['bg-blue-900/60 text-blue-300', 'train_job_running', 'Running'],
  completed: ['bg-green-900/50 text-green-300', 'train_job_completed', 'Completed'],
  failed: ['bg-red-900/50 text-red-300', 'train_job_failed', 'Failed'],
  cancelled: ['bg-amber-900/50 text-amber-300', 'train_job_cancelled', 'Cancelled'],
  // 服务重启前后进程结束了,拿不到退出码。**不能说它失败** —— 产物可能是完整的,
  // 只是我们没跟到最后一刻,所以是"未跟踪到结束"而不是"出错"。
  interrupted: ['bg-amber-900/50 text-amber-300', 'train_job_interrupted', 'Untracked'],
};

export function statusBadge(status) {
  const [cls, key, fallback] = STATUS_STYLE[status] || STATUS_STYLE.queued;
  return el('span', {
    class: `text-[10px] px-1.5 py-0.5 rounded shrink-0 ${cls}`,
    text: tr(key, fallback),
  });
}

function row(labelKey, fallback, value) {
  if (!value) return null;
  return el('div', { class: 'flex gap-3 text-xs py-0.5' }, [
    el('span', { class: 'text-gray-500 shrink-0 w-28', text: tr(labelKey, fallback) }),
    el('span', { class: 'text-gray-300 font-mono break-all', text: String(value) }),
  ]);
}

/** 进度条。``progress`` 为 null = 还不知道进度(见后端 runner 的 _parse_progress)。 */
function progressBar(progress, status) {
  if (progress === null || progress === undefined) {
    // **不要画一条 0% 的条** —— 那看起来像卡住了。如实说"还不知道":
    // 训练前几十秒都在 import torch / 扫数据集,日志里确实还没有步数。
    return el('div', {
      class: 'text-xs text-gray-500',
      text: ACTIVE_STATUSES.includes(status)
        ? tr('train_job_progress_unknown', 'Running — no step count in the log yet')
        : '',
    });
  }
  const pct = Math.round(progress * 100);
  return el('div', { class: 'flex items-center gap-3' }, [
    el('div', { class: 'flex-1 h-1.5 bg-gray-800 rounded overflow-hidden' }, [
      el('div', { class: 'h-full bg-blue-500', style: `width:${pct}%` }),
    ]),
    el('span', { class: 'text-xs text-gray-400 w-10 text-right', text: `${pct}%` }),
  ]);
}

/**
 * 一个任务的详情面板。
 *
 * @returns {{detail: HTMLElement, update: Function, pullLog: Function, isLive: Function}}
 */
function jobDetail(job, token) {
  let live = ACTIVE_STATUSES.includes(job.status);

  // 两个固定插槽 —— 轮询时只清空重填这两个,别的地方一律不动。
  const badgeSlot = el('span', { class: 'shrink-0' });
  const barSlot = el('div', { class: 'mt-3' });
  const msgHost = el('div', {});

  const cancelBtn = el('button', {
    class: 'text-xs px-2.5 py-1 rounded border border-gray-700 text-gray-300 '
      + 'hover:border-red-700 hover:text-red-300',
    text: tr('train_job_cancel', 'Cancel training'),
  });
  cancelBtn.addEventListener('click', async () => {
    cancelBtn.disabled = true;
    try {
      await cancelTrainingJob(job.id);
    } catch (err) {
      cancelBtn.disabled = false;
      clear(msgHost);
      msgHost.appendChild(el('div', { class: 'text-xs text-red-400', text: err.message }));
    }
  });

  const logPre = el('pre', {
    class: 'text-[10px] leading-4 text-gray-400 bg-black/40 rounded p-3 mt-2 '
      + 'max-h-80 overflow-auto whitespace-pre-wrap break-all font-mono',
  });
  let logOffset = 0;
  let truncated = false;

  const detail = el('div', { class: 'bg-gray-900 border border-gray-800 rounded-lg p-4 mt-3' }, [
    el('div', { class: 'flex items-center gap-3 flex-wrap' }, [
      el('span', { class: 'text-sm font-semibold text-gray-100 font-mono', text: job.job_name }),
      badgeSlot,
      live ? cancelBtn : null,
    ]),
    barSlot,
    el('div', { class: 'mt-3' }, [
      row('train_job_dataset', 'Dataset', job.dataset?.name),
      // 这个路径是后端拿**名字**去 state/datasets/ 查出来的,所以一定是平台导出
      // 产物 —— 原始项目绕不过校验(见 runner.validate_spec)。
      row('train_job_output', 'Output', job.output_dir),
      row('train_job_gpus', 'GPUs', (job.gpus || []).join(', ')),
      row('train_job_steps', 'Steps', job.params?.steps),
      row('train_job_started', 'Started', job.started_at ? dateTime(job.started_at) : null),
      row('train_job_resume', 'Resumed from',
        job.resume ? `${job.resume.run} @ ${job.resume.step}` : null),
    ]),
    job.error ? el('div', {
      class: 'mt-3 text-xs text-red-400 bg-red-950/30 border border-red-900 rounded '
        + 'px-3 py-2 whitespace-pre-wrap',
      text: job.error,
    }) : null,
    el('div', { class: 'mt-4 text-xs text-gray-500', text: tr('train_job_log', 'Training log') }),
    logPre,
    msgHost,
  ]);

  function update(next) {
    clear(badgeSlot);
    badgeSlot.appendChild(statusBadge(next.status));
    clear(barSlot);
    barSlot.appendChild(progressBar(next.progress, next.status));
    live = ACTIVE_STATUSES.includes(next.status);
    // 任务结束后取消按钮就没意义了 —— 隐藏而不是禁用,少一个死按钮。
    if (!live && cancelBtn.isConnected) cancelBtn.remove();
  }

  /** 增量拉日志。``offset`` 用**服务端返回的字节偏移**,不能自己按字符数累加 ——
   *  日志是 UTF-8,一个中文字符占 3 字节,按字符数走会越读越偏。 */
  async function pullLog() {
    if (token !== _token) return;
    try {
      const chunk = await trainingJobLog(job.id, logOffset);
      if (token !== _token) return;
      if (!chunk.text) return;
      logOffset = chunk.offset;
      if (chunk.truncated) truncated = true;
      logPre.textContent = (truncated ? '…\n' : '') + logPre.textContent + chunk.text;
      if (logPre.textContent.length > LOG_DOM_LIMIT) {
        logPre.textContent = `…\n${logPre.textContent.slice(-LOG_DOM_LIMIT)}`;
        truncated = true;
      }
      // 只在用户没往上翻的时候自动跟到底,否则他查历史输出时会被反复拽回底部。
      const nearBottom = logPre.scrollHeight - logPre.scrollTop - logPre.clientHeight < 60;
      if (nearBottom) logPre.scrollTop = logPre.scrollHeight;
    } catch (err) {
      // 日志读不到不该把整页搞崩 —— 状态面板本身仍然是有效的。
      if (token !== _token) return;
      logPre.textContent += `\n[${tr('train_job_log_failed', 'log unavailable')}: ${err.message}]\n`;
    }
  }

  update(job);
  return { detail, update, pullLog, isLive: () => live };
}

/**
 * @param {HTMLElement} container
 * @param {{focus?: string}} [arg] 提交成功后由 newrun 带过来的任务 id —— 直接展开它。
 */
export function renderJobs(container, arg) {
  clear(container);
  const token = {};
  _token = token;

  const listHost = el('div', {});
  const detailHost = el('div', {});
  container.appendChild(listHost);
  container.appendChild(detailHost);

  let jobs = [];
  let selectedId = arg?.focus || null;
  let detail = null;
  let lastStatus = null;

  function select(job) {
    selectedId = job.id;
    clear(detailHost);
    detail = jobDetail(job, token);
    lastStatus = job.status;
    detailHost.appendChild(detail.detail);
    detail.pullLog();
    draw();
  }

  function draw() {
    clear(listHost);
    if (!jobs.length) {
      listHost.appendChild(el('div', {
        class: 'text-center text-gray-600 text-sm py-10',
        text: tr('train_jobs_empty',
          'No training job yet. Start one from the "New Training" tab.'),
      }));
      return;
    }
    listHost.appendChild(el('div', {
      class: 'border border-gray-800 rounded-lg divide-y divide-gray-800',
    }, jobs.map((job) => {
      const active = job.id === selectedId;
      const rowEl = el('div', {
        class: `flex items-center gap-3 px-3 py-2 text-xs cursor-pointer ${
          active ? 'bg-gray-800/60' : 'hover:bg-gray-800/40'}`,
      }, [
        statusBadge(job.status),
        el('span', { class: 'text-gray-200 font-mono w-48 truncate', text: job.job_name }),
        el('span', { class: 'text-gray-500 w-56 truncate', text: job.dataset?.name || '' }),
        el('span', {
          class: 'text-gray-500 w-16 text-right',
          text: job.progress === null || job.progress === undefined
            ? '' : `${Math.round(job.progress * 100)}%`,
        }),
        el('span', { class: 'text-gray-600 ml-auto shrink-0', text: dateTime(job.created_at) }),
      ]);
      rowEl.addEventListener('click', () => select(job));
      return rowEl;
    })));
  }

  async function poll() {
    if (token !== _token) return;          // 本页已被重新渲染,这个循环退休
    try {
      const payload = await listTrainingJobs();
      if (token !== _token) return;
      const fresh = payload.jobs || [];
      // 列表本身重画没代价(纯文本节点),详情才需要精打细算。
      jobs = fresh;
      const current = jobs.find((j) => j.id === selectedId);
      if (current && detail) {
        detail.update(current);
        if (ACTIVE_STATUSES.includes(current.status)) {
          await detail.pullLog();
        } else if (lastStatus !== current.status) {
          // 刚结束:重拉一次拿到 error / finished_at,再补最后一截日志。
          const full = await getTrainingJob(selectedId);
          if (token !== _token) return;
          clear(detailHost);
          detail = jobDetail(full, token);
          detailHost.appendChild(detail.detail);
          await detail.pullLog();
        }
        lastStatus = current.status;
      }
      draw();
    } catch {
      /* 轮询失败就下一轮再试 —— 单次网络抖动不该把页面变成错误页 */
    }
    if (token !== _token) return;
    setTimeout(poll, POLL_MS);
  }

  poll();
}
