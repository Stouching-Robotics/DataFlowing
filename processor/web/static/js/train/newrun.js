/** 「新建训练」页签 —— 五段式向导。
 *
 *   ① 训练数据集(审核页导出后留下的成品,见 state/datasets/)
 *   ② 选择模型(ACT)+ 训练框架(LeRobot,只读)
 *   ③ 训练位置(私有算力 + 勾选参与训练的 GPU)
 *   ④ 训练参数(每项带一句说明)
 *   ⑤ 从已有训练继续(可选)
 *
 * **本期只做前端 —— 所以这一页是纯表单。**
 *
 * 这里曾经有一个禁用的「创建训练任务」按钮、外加一条生成好的 `lerobot-train`
 * 命令行(带复制 / 下载 .sh)。已经删掉了:后端执行接上之前,那两样要么是假的
 * 按钮,要么是绕路把命令当交付物。等真接上后端时,直接加一个**能点的**按钮,
 * 把下面这些字段 POST 出去即可 —— 表单本身就已经是接口形状了。
 *
 * ★ 相应地,现在这一页的选择(数据集 / GPU / 参数 / 续训)**都不驱动任何输出**。
 *   它们是为将来后端准备的状态,不是装饰;但也别指望现在改一下会有什么反应。
 *
 * 只有 ACT —— 其它策略(π0 / SmolVLA / diffusion…)都是 LeRobot 内置、跑得通,
 * 但没在这套硬件上实测过,所以不摆一排点不动的卡片。
 */

import { el, clear } from '../shared/dom.js';
import { bytes, dateTime, thousands } from '../shared/format.js';
import { tr } from '../shared/text.js';
import { listDatasets, listGpus, listReadyProjects, listRuns } from './api.js';

/** 默认超参 —— 取自**实测那一次**的真实 train_config.json
 *  (checkpoints/020000),不是参考实现的示例值。
 *
 *  参考实现里 batch_size=2、steps=10000 是给它那个 8GB 的 pi0 用的;我们这次
 *  198MB 的 ACT 在这台机器上 batch_size=16 / steps=20000 是跑通过的。首次训练
 *  用跑通过的更稳。 */
const DEFAULTS = {
  job_name: 'act',
  batch_size: 16,
  steps: 20000,
  seed: 1000,
  num_workers: 4,
  eval_freq: 1000,
  log_freq: 100,
  save_checkpoint: true,
  save_freq: 5000,
};

/** 参数说明 —— 直接对应参考实现每项右侧那句话,首次训练的人看得懂。 */
const PARAM_HELP = {
  job_name: ['train_help_job_name', 'Name of this run. It becomes the checkpoint folder name, so keep it short and unique.'],
  batch_size: ['train_help_batch', 'Larger batches train more stably but need more VRAM; smaller ones are faster but less stable. Suggested 1-32.'],
  steps: ['train_help_steps', 'More steps usually improve the policy but risk overfitting. Start at 10000 and adjust from validation.'],
  seed: ['train_help_seed', 'A fixed seed reproduces the same run, which makes debugging and comparison possible. 1000 or 42.'],
  num_workers: ['train_help_workers', 'More workers load data faster but use more memory. About 1/2 to 1x the CPU core count.'],
  eval_freq: ['train_help_eval_freq', 'How often to run an evaluation, in steps. About 10% of total steps.'],
  log_freq: ['train_help_log_freq', 'How often to refresh progress, in steps. 10-100 works well.'],
  save_checkpoint: ['train_help_save_ckpt', 'Save checkpoints while training so a run can be resumed or deployed.'],
  save_freq: ['train_help_save_freq', 'How often to save a checkpoint, in steps. About 30% of total steps.'],
};


const INPUT_CLASS = 'bg-gray-800 text-xs rounded px-2 py-1 border border-gray-700 '
  + 'focus:border-blue-500 focus:outline-none font-mono';
const BTN = 'bg-gray-800 hover:bg-gray-700 text-gray-300 text-xs px-2.5 py-1 '
  + 'rounded border border-gray-700';

const LIVE = 'bg-green-900/50 text-green-300';
const PENDING = 'bg-amber-900/50 text-amber-300';

function section(titleKey, fallback, badge, children) {
  return el('div', { class: 'bg-gray-900 rounded-lg border border-gray-800 p-4 mb-4' }, [
    el('div', { class: 'flex items-center gap-3 mb-3' }, [
      el('div', { class: 'text-sm font-semibold text-gray-200', text: tr(titleKey, fallback) }),
      badge ? el('span', { class: `text-[10px] px-1.5 py-0.5 rounded ${badge.cls}`, text: badge.text }) : null,
    ]),
    ...children,
  ]);
}

function hint(key, fallback) {
  return el('div', { class: 'text-xs text-gray-600 mt-3', text: tr(key, fallback) });
}

/** 一行「标签 + 控件」,失败/加载中都用它占位。 */
function labeled(label, control, labelClass = 'text-gray-500 shrink-0 w-32') {
  return el('div', { class: 'flex items-center gap-3 text-xs' }, [
    el('span', { class: labelClass, text: label }),
    control,
  ]);
}

function loadingRow(text) {
  return el('div', { class: 'text-xs text-gray-600 py-3', text });
}

function errorRow(text) {
  return el('div', { class: 'text-xs text-red-400 py-3', text });
}

export function renderNewRun(container) {
  clear(container);

  const state = { gpus: new Set(), dataset: null, resume: null };

  // ── ① 训练位置 ──────────────────────────────────────
  const gpuHost = el('div', {});
  const locationCard = el('div', {
    class: 'inline-block border-2 border-blue-500 bg-blue-950/30 rounded-lg px-4 py-3 min-w-[220px]',
  }, [
    el('div', { class: 'text-sm font-semibold text-gray-100', text: tr('train_loc_local', 'Local GPU server') }),
    el('div', { class: 'text-xs text-gray-400 mt-1', text: tr('train_loc_local_desc', 'GPUs on this machine') }),
  ]);

  const sectionLocation = section('train_sec_location', '1. Training Location', null, [
    locationCard,
    el('div', { class: 'mt-4' }, [gpuHost]),
  ]);

  function renderGpus(payload) {
    clear(gpuHost);
    if (!payload.available) {
      // 驱动没起来时**必须区别显示** —— 渲染成"利用率 0%"会让人以为显卡闲着。
      gpuHost.appendChild(el('div', {
        class: 'text-xs text-amber-300 bg-amber-950/40 border border-amber-900 rounded px-3 py-2',
      }, [
        el('div', { text: tr('train_gpu_unavailable', 'Cannot read the GPUs') }),
        el('div', { class: 'text-amber-500/80 mt-1 font-mono break-all', text: payload.error }),
        el('div', { class: 'text-amber-500/80 mt-1', text: tr('train_gpu_unavailable_hint', 'Fix the driver before training — training cannot start without it.') }),
      ]));
      return;
    }
    if (!payload.gpus.length) {
      gpuHost.appendChild(errorRow(tr('train_gpu_none', 'nvidia-smi reported no GPUs.')));
      return;
    }

    gpuHost.appendChild(el('div', { class: 'flex gap-3 flex-wrap' },
      payload.gpus.map((gpu) => {
        const usedRatio = gpu.memory_total_mb
          ? Math.round((gpu.memory_used_mb / gpu.memory_total_mb) * 100) : 0;
        const busy = gpu.utilization > 5 && gpu.index !== 0;

        const checkbox = el('input', {
          type: 'checkbox', class: 'accent-blue-500',
          checked: state.gpus.has(gpu.index),
        });
        checkbox.addEventListener('change', () => {
          if (checkbox.checked) state.gpus.add(gpu.index);
          else state.gpus.delete(gpu.index);
        });

        return el('div', { class: 'bg-gray-900 border border-gray-800 rounded-lg p-3 w-64' }, [
          el('div', { class: 'flex items-center gap-2' }, [
            el('span', { class: 'text-sm font-semibold text-gray-100', text: `GPU ${gpu.index}` }),
            el('span', {
              class: `ml-auto text-[10px] px-1.5 py-0.5 rounded ${busy ? 'bg-amber-900/50 text-amber-300' : 'bg-green-900/50 text-green-300'}`,
              text: busy ? tr('train_gpu_busy', 'busy') : tr('train_gpu_idle', 'idle'),
            }),
          ]),
          el('div', { class: 'text-xs text-gray-400 mt-1', text: gpu.name }),
          el('div', { class: 'mt-2 text-xs text-gray-500' }, [
            el('div', { text: `${tr('train_gpu_mem', 'VRAM')} ${gpu.memory_used_mb} / ${gpu.memory_total_mb} MB (${usedRatio}%)` }),
            el('div', { class: 'h-1.5 bg-gray-800 rounded mt-1' }, [
              el('div', { class: 'h-1.5 bg-blue-500 rounded', style: `width:${usedRatio}%` }),
            ]),
            el('div', { class: 'mt-1', text: `${tr('train_gpu_util', 'Utilization')} ${gpu.utilization}%` }),
            el('div', { text: `${tr('train_gpu_temp', 'Temp')} ${gpu.temperature}°C` }),
          ]),
          el('label', { class: 'flex items-center gap-2 mt-2 text-xs text-gray-400 cursor-pointer' }, [
            checkbox,
            el('span', { text: tr('train_gpu_include', 'Use for training') }),
          ]),
        ]);
      })));
  }

  gpuHost.appendChild(loadingRow(tr('loading', 'Loading...')));
  listGpus()
    .then(renderGpus)
    .catch((err) => { clear(gpuHost); gpuHost.appendChild(errorRow(`${tr('train_load_failed', 'Failed to load')}: ${err.message}`)); });

  // ── ② 选择模型 + 训练框架 ───────────────────────────
  const modelCard = el('div', {
    class: 'inline-block border-2 border-blue-500 bg-blue-950/30 rounded-lg px-4 py-3 min-w-[240px]',
  }, [
    el('div', { class: 'flex items-center gap-2' }, [
      el('span', { class: 'text-sm font-semibold text-gray-100', text: 'ACT' }),
      el('span', { class: 'text-[10px] px-1.5 py-0.5 rounded bg-blue-900/60 text-blue-300', text: tr('train_new_model_tested', 'tested') }),
    ]),
    el('div', { class: 'text-xs text-gray-400 mt-1.5', text: tr('train_new_model_act_desc', 'Transformer action-chunking imitation learning policy') }),
  ]);

  const sectionModel = section('train_sec_model', '2. Model', null, [
    el('div', { class: 'flex items-start gap-6 flex-wrap' }, [
      el('div', {}, [modelCard]),
      el('div', {}, [
        el('div', { class: 'text-xs text-gray-500 mb-1.5', text: tr('train_framework', 'Training framework') }),
        // 只读:ACT 在 LeRobot 里只有一种实现,没有可选项。做成下拉框是假的。
        el('div', {
          class: 'inline-block bg-gray-800 border border-gray-700 rounded px-3 py-1.5 text-xs text-gray-200 font-mono',
          text: 'LeRobot',
        }),
        el('div', { class: 'text-xs text-gray-600 mt-1.5', text: tr('train_framework_hint', 'ACT ships as a single LeRobot implementation, so there is nothing to choose here.') }),
      ]),
    ]),
  ]);

  // ── ③ 训练参数 ──────────────────────────────────────
  const paramInputs = {};
  const paramRows = Object.keys(DEFAULTS).map((name) => {
    const [helpKey, helpFallback] = PARAM_HELP[name] || ['', ''];
    let control;
    if (name === 'save_checkpoint') {
      control = el('select', { class: INPUT_CLASS + ' w-28' }, [
        el('option', { value: 'true', text: 'true' }),
        el('option', { value: 'false', text: 'false' }),
      ]);
    } else {
      control = el('input', { class: INPUT_CLASS + ' w-28', value: String(DEFAULTS[name]) });
    }
    paramInputs[name] = control;

    return el('div', { class: 'flex items-start gap-4 text-xs py-1' }, [
      el('span', { class: 'text-gray-400 shrink-0 w-36 font-mono pt-1.5', text: name }),
      el('div', { class: 'shrink-0' }, [control]),
      el('span', { class: 'text-gray-600 pt-1.5 flex items-start gap-1.5' }, [
        el('iconify-icon', {
          icon: 'ant-design:info-circle-outlined',
          class: 'mt-0.5 shrink-0',
        }),
        el('span', { text: tr(helpKey, helpFallback) }),
      ]),
    ]);
  });

  const sectionParams = section('train_sec_params', '3. Training Parameters',
    { cls: PENDING, text: tr('train_new_pending', 'not wired') }, [
      ...paramRows,
      el('button', {
        class: 'mt-3 ' + BTN,
        text: tr('train_new_reset', 'Restore measured defaults'),
        on: {
          click: () => {
            for (const [name, control] of Object.entries(paramInputs)) control.value = String(DEFAULTS[name]);
          },
        },
      }),
    ]);

  // ── ④ 训练数据集 ────────────────────────────────────
  //
  // 来源是**在视频审核页导出后留下来的成品**(`state/datasets/<名字>`),不是
  // 原始项目。原始项目里混着未审核的集,而且留出集(UMIGripper_Action_AI 的
  // 52–73)就在里面 —— 按项目选只能报警、拦不住。改以导出产物为准之后,两个
  // 坑都在导出那一步被排掉了。
  //
  // ★ 正因如此,这里**不再有留出集防线**:导出后的数据集集号会被重排成
  //   0..N-1,已经认不出哪一集原本是 52–73。控制点转移到了导出那一步 ——
  //   导出时别把留出集勾进去。要加硬校验的话,位置在审核/导出页而不是这里。
  const datasetHost = el('div', {});
  const readyHost = el('div', {});

  const sectionDataset = section('train_sec_dataset', '4. Training Dataset',
    { cls: LIVE, text: tr('train_new_live', 'live') }, [
      datasetHost,
      readyHost,
    ]);

  /** 「到点提醒」—— 项目已通过集数达到它自己设的采集上限时高亮出来。
   *
   *  **只提醒,不自动导出。** 导出一次约 30 秒/集(UI 自己的文案),1000 集要跑
   *  8 小时以上;而且构建好的数据集会被"固定"下来 —— 已经拿去训练的那份不能
   *  悄悄变。让人点一下,换到的是"我知道现在在导什么、用哪份数据"。
   *
   *  没到点时也给一行「最接近的」,否则这块永远是空的,看不出进展。 */
  function renderReady(payload) {
    clear(readyHost);
    const ready = (payload.projects || []).filter((p) => p.ready);

    if (ready.length) {
      readyHost.appendChild(el('div', {
        class: 'mt-3 text-xs bg-green-950/40 border border-green-900 rounded px-3 py-2',
      }, [
        el('div', {
          class: 'text-green-300 font-medium mb-2',
          text: tr('train_ready_title', 'Reached its collection limit — ready to export'),
        }),
        ...ready.map((p) => el('div', { class: 'flex items-center gap-3 py-0.5' }, [
          el('span', { class: 'text-gray-200 font-mono flex-1 truncate', text: p.name }),
          el('span', { class: 'text-gray-400 shrink-0', text: `${p.approved} / ${p.target}` }),
          el('a', {
            href: '/review?status=approved',
            class: 'text-blue-400 hover:underline shrink-0',
            text: tr('train_ready_go', 'Go export'),
          }),
        ])),
      ]));
      return;
    }

    // 没到点就**什么都不显示** —— 曾经在这里给一行「离采集上限最近的 X: n/N」,
    // 但它每次都在,而绝大多数时候又离得很远(实测 3/100、52/10000),读起来像
    // 一句永远不变的噪音。到点了自然会冒出来,没到点就安静待着。
  }

  function datasetEmptyState() {
    return el('div', { class: 'text-center py-6' }, [
      el('div', { class: 'text-xs text-gray-500 mb-3', text: tr('train_dataset_none', 'No exported dataset yet.') }),
      el('a', {
        href: '/review?status=approved',
        class: 'inline-block bg-blue-600 hover:bg-blue-500 text-white text-xs px-3 py-1.5 rounded',
        text: tr('train_dataset_goto_export', 'Go export approved episodes'),
      }),
    ]);
  }

  function renderDatasets(payload) {
    clear(datasetHost);
    if (!payload.datasets.length) {
      datasetHost.appendChild(datasetEmptyState());
      return;
    }
    datasetHost.appendChild(el('div', { class: 'border border-gray-800 rounded divide-y divide-gray-800' },
      payload.datasets.map((ds) => {
        const radio = el('input', { type: 'radio', name: 'train-dataset', class: 'accent-blue-500 shrink-0' });
        radio.addEventListener('change', () => {
          state.dataset = ds;
        });
        return el('label', {
          class: 'flex items-center gap-3 px-3 py-2 text-xs cursor-pointer hover:bg-gray-800/40',
        }, [
          radio,
          el('span', { class: 'text-gray-200 font-mono w-64 truncate', text: ds.name }),
          el('span', { class: 'text-gray-400 w-20 text-right', text: `${ds.total_episodes} ${tr('train_episodes', 'episodes')}` }),
          el('span', { class: 'text-gray-500 w-24 text-right', text: `${thousands(ds.total_frames)} ${tr('train_new_frames', 'frames')}` }),
          el('span', { class: 'text-gray-600 w-12 text-right', text: ds.codebase_version }),
          el('span', { class: 'text-gray-600 ml-auto', text: ds.fps ? `${ds.fps} fps` : '' }),
        ]);
      })));
  }

  datasetHost.appendChild(loadingRow(tr('loading', 'Loading...')));
  listDatasets()
    .then(renderDatasets)
    .catch((err) => { clear(datasetHost); datasetHost.appendChild(errorRow(`${tr('train_load_failed', 'Failed to load')}: ${err.message}`)); });

  // 到点提醒是**加分项**:拿不到(比如扫描超时)就整块不显示,不拦这一页 ——
  // 它只是提示"该导出了",不是这一页的主功能。
  listReadyProjects()
    .then(renderReady)
    .catch(() => clear(readyHost));

  // ── ⑤ 从已有训练继续(可选)─────────────────────────
  const resumeTask = el('select', { class: INPUT_CLASS + ' w-64' }, [
    el('option', { value: '', text: tr('train_resume_none', '— Start fresh —') }),
  ]);
  const resumeStep = el('select', { class: INPUT_CLASS + ' w-40', disabled: true }, [
    el('option', { value: '', text: '—' }),
  ]);
  let runsCache = [];

  resumeTask.addEventListener('change', () => {
    clear(resumeStep);
    const run = runsCache.find((r) => r.name === resumeTask.value);
    if (!run) {
      resumeStep.disabled = true;
      resumeStep.appendChild(el('option', { value: '', text: '—' }));
      state.resume = null;
      return;
    }
    resumeStep.disabled = false;
    resumeStep.appendChild(el('option', { value: '', text: tr('train_resume_latest', '— latest —') }));
    for (const c of run.checkpoints) {
      resumeStep.appendChild(el('option', { value: String(c.step), text: `${c.name} (${bytes(c.total_bytes)})` }));
    }
    state.resume = { run: run.name, step: run.checkpoints[0]?.step ?? null };
  });
  resumeStep.addEventListener('change', () => {
    state.resume = resumeTask.value
      ? { run: resumeTask.value, step: resumeStep.value ? parseInt(resumeStep.value, 10) : null }
      : null;
  });

  const sectionResume = section('train_sec_resume', '5. Continue From a Previous Run',
    { cls: PENDING, text: tr('train_new_pending', 'not wired') }, [
      el('div', { class: 'flex gap-6 flex-wrap' }, [
        labeled(tr('train_resume_task', 'Source run'), resumeTask),
        labeled(tr('train_resume_step', 'Checkpoint'), resumeStep),
      ]),
    ]);

  listRuns()
    .then((payload) => {
      runsCache = payload.runs || [];
      for (const run of runsCache) {
        resumeTask.appendChild(el('option', { value: run.name, text: `${run.name} (${run.checkpoint_count})` }));
      }
    })
    .catch(() => { /* 拿不到就保持"全新训练",不拦整页 */ });



  // 展示顺序 ≠ 构造顺序。构造时必须先定义好各自依赖的变量(GPU 面板、数据集的
  // 留出集警告、命令生成…),这里才决定它们从上到下怎么排。
  //
  // 顺序:数据集 → 模型 → 算力 → 参数。先定"用什么数据",再定"训什么模型",
  // 再定"在哪训",最后"怎么训" —— 决策依赖就是按这个方向走的(比如数据集决定了
  // 命令里的 --dataset.root)。
  for (const node of [sectionDataset, sectionModel, sectionLocation, sectionParams,
                      sectionResume]) {
    container.appendChild(node);
  }
}
