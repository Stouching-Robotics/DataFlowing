/** 「模型输出」页签 —— 每个 checkpoint 一个可展开块 + 文件明细表。
 *
 * 数据全部来自磁盘真实文件(后端 rglob 出来的),没有任何凑数的假行。
 *
 * **下载已经能用了** —— 文件表逐行一个链接,走后端
 * ``GET /runs/{run}/checkpoints/{step}/files/{name:path}``(逐文件而不是整包:
 * 一个 checkpoint 约 600MB,其中 training_state/ 只在续训时有用)。
 *
 * **其余操作按钮仍然 disabled**:删除 / 推理 / 离线部署都还没有端点。做成可点
 * 但没反应是最糟的 —— 用户会反复点、以为坏了。disabled 加上 title 说明,一眼就
 * 知道"还没接",和有端点了再放开是两回事。
 */

import { el, clear } from '../shared/dom.js';
import { bytes, dateTime } from '../shared/format.js';
import { tr } from '../shared/text.js';
import { checkpointFileUrl } from './api.js';

/** 逐个文件一行。文件名可能很长(pretrained_model/xxx.safetensors),
 *  用 break-all 而不是省略号 —— 训练产物看的就是完整名字。 */
function fileRow(runName, checkpoint, file) {
  return el('tr', { class: 'border-b border-gray-800/60 last:border-0' }, [
    el('td', { class: 'py-1.5 pr-4 text-gray-300 font-mono text-[11px] break-all', text: file.name }),
    el('td', { class: 'py-1.5 pr-4 text-gray-400 text-right whitespace-nowrap', text: bytes(file.size) }),
    el('td', { class: 'py-1.5 pr-4 text-gray-500 whitespace-nowrap', text: dateTime(file.mtime) }),
    el('td', { class: 'py-1.5 text-right whitespace-nowrap' }, [
      // 带 download 属性:浏览器直接存盘,不跳转到一个只会显示二进制内容的页面。
      el('a', {
        class: 'text-blue-400 hover:underline',
        href: checkpointFileUrl(runName, checkpoint.name, file.name),
        download: true,
        text: tr('train_btn_download', 'Download'),
      }),
    ]),
  ]);
}

function fileTable(runName, checkpoint) {
  return el('table', { class: 'w-full text-xs mt-3' }, [
    el('thead', {}, [
      el('tr', { class: 'border-b border-gray-800' }, [
        el('th', { class: 'text-left py-1.5 pr-4 font-medium text-gray-500', text: tr('train_col_name', 'Name') }),
        el('th', { class: 'text-right py-1.5 pr-4 font-medium text-gray-500', text: tr('train_col_size', 'Size') }),
        el('th', { class: 'text-left py-1.5 pr-4 font-medium text-gray-500', text: tr('train_col_mtime', 'Modified') }),
        el('th', { class: 'text-right py-1.5 font-medium text-gray-500' }),
      ]),
    ]),
    el('tbody', {}, checkpoint.files.map((f) => fileRow(runName, checkpoint, f))),
  ]);
}

/** **真的还没接端点**的按钮 —— disabled,title 说明原因。 */
function pendingButton(label, hint) {
  return el('button', {
    class: 'text-[11px] px-2 py-1 rounded border border-gray-700 text-gray-600 cursor-not-allowed',
    text: label,
    disabled: true,
    title: hint,
  });
}

/** 能点的按钮。 */
function actionButton(label, hint, onClick) {
  return el('button', {
    class: 'text-[11px] px-2 py-1 rounded border border-gray-600 text-gray-300 '
      + 'hover:border-gray-500 hover:text-gray-100',
    title: hint,
    text: label,
    on: { click: onClick },
  });
}

function checkpointBlock(runName, checkpoint, expanded, handlers) {
  const body = el('div', { class: expanded ? '' : 'hidden' }, [fileTable(runName, checkpoint)]);
  const chevron = el('span', {
    class: 'text-gray-500 text-[10px] transition-transform',
    text: expanded ? '▾' : '▸',
  });

  const toggle = () => {
    body.classList.toggle('hidden');
    const open = !body.classList.contains('hidden');
    chevron.textContent = open ? '▾' : '▸';
  };

  return el('div', { class: 'bg-gray-900 rounded-lg border border-gray-800 p-4' }, [
    el('div', { class: 'flex items-center flex-wrap gap-3' }, [
      el('iconify-icon', { icon: 'ant-design:folder-outlined', class: 'text-base' }),
      el('span', { class: 'text-gray-200 font-mono font-semibold', text: checkpoint.name }),
      el('span', {
        class: 'text-[10px] px-1.5 py-0.5 rounded bg-blue-900/50 text-blue-300',
        text: tr('train_step_badge', 'Step'),
      }),
      el('span', {
        class: 'text-xs text-gray-500',
        text: `${checkpoint.file_count} ${tr('train_files_unit', 'files')}`
        + ` · ${bytes(checkpoint.total_bytes)}`,
      }),
      el('div', { class: 'ml-auto flex gap-2' }, [
        // 「下载」= **展开文件表**(那里每行才有真下载)。不做整包下载:一个
        // checkpoint 约 600MB,其中 training_state/(优化器状态,约 400MB)只在
        // 续训时有用;拿去推理/部署只要 pretrained_model/(约 200MB)。
        // 要"一键整包"得后端加一个压 zip 的端点,本期没有。
        actionButton(tr('train_btn_download', 'Download'),
                     tr('train_btn_hint_download_row',
                        'Expand the file list — each file downloads individually'),
                     () => { if (body.classList.contains('hidden')) toggle(); }),
        // 「推理」= 跳到「模型推理」页签并选中本检查点。**纯前端跳转,不需要后端** ——
        // 那正是"我要拿这个模型去推理"的自然动作。
        actionButton(tr('train_btn_infer', 'Inference'),
                     tr('train_btn_hint_infer_row',
                        'Open the Inference tab with this checkpoint selected'),
                     () => handlers?.onInfer?.(checkpoint)),
        pendingButton(tr('train_btn_delete', 'Delete'),
                      tr('train_btn_hint_delete', 'Destructive; no endpoint yet')),
        pendingButton(tr('train_btn_deploy', 'Offline Deploy'),
                      tr('train_btn_hint_deploy', 'Belongs to the Deployment module')),
      ]),
    ]),
    el('button', {
      class: 'mt-3 text-xs text-gray-500 hover:text-gray-300 flex items-center gap-1.5',
      on: { click: toggle },
    }, [chevron, el('span', { text: tr('train_file_details', 'File details') })]),
    body,
  ]);
}

/**
 * @param {HTMLElement} container
 * @param {object} run  后端给的一个 run(`{name, checkpoints, …}`)—— 需要 name
 *                      才能拼出文件下载地址(`/runs/{name}/checkpoints/{step}/files/…`)
 * @param {{onInfer?: (checkpoint: object) => void}} [handlers]
 *        由 train/main.js 注入 —— 本模块不认识"页签"这回事,只负责喊一声。
 */
export function renderCheckpoints(container, run, handlers = {}) {
  clear(container);
  const checkpoints = run.checkpoints || [];

  if (checkpoints.length === 0) {
    container.appendChild(el('div', {
      class: 'text-center text-gray-600 text-sm py-10',
      text: tr('train_checkpoints_empty', 'No checkpoints under this run.'),
    }));
    return;
  }

  // **全部默认折叠。** 曾经让最新的那个默认展开,但一进页面就被一大堆文件名
  // (每个 checkpoint 11 个文件)顶下去,反而看不到下面还有几个 checkpoint。
  // 折叠状态下每行就是一行摘要,扫一眼能看全;要看细节自己点。
  container.appendChild(el('div', { class: 'space-y-3' },
    checkpoints.map((c) => checkpointBlock(run.name, c, false, handlers))));
}
