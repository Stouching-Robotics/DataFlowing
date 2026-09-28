/** 模型推理视图 —— 选模型/检查点、看它的训练参数、选测试集,然后跑评估。
 *
 * 与 `static/js/train/` 同构:一个入口 + 若干模块,共用 `static/js/shared/` 下的
 * 纯工具。**不新开一套** —— 推理和训练本来就是同一批产物的上下游。
 *
 * **后端执行未接** —— 「运行推理」按钮和训练页一样是**故意禁用**的,旁边写明原因。
 * 这一页真正能用的是前面三段(选模型 / 看参数 / 选测试集),数据都是真的:
 *   模型与检查点 → GET /api/v1/training/runs
 *   训练参数     → 那些 checkpoint 里的 train_config.json
 *   测试集       → GET /api/v1/training/datasets
 *
 * 没有「实时日志」「指标曲线」之类的空页签 —— 没有数据的页签不摆,理由同训练页。
 *
 * ★ 数据源为什么直接引 `../train/api.js`:runs / datasets 这两个端点属于**训练域**
 *   (推理消费的是训练产物)。等将来有第三个页面也用到它们,再抽到 shared/ 去。
 */

import { el, clear } from '../shared/dom.js';
import { tr } from '../shared/text.js';
import { listDatasets, listRuns } from '../train/api.js';

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

function labeled(label, control, labelClass = 'text-gray-500 shrink-0 w-32') {
  return el('div', { class: 'flex items-center gap-3 text-xs' }, [
    el('span', { class: labelClass, text: label }),
    control,
  ]);
}

function hint(key, fallback) {
  return el('div', { class: 'text-xs text-gray-600 mt-3', text: tr(key, fallback) });
}

/** 一行「参数名 → 值」。推理前最该确认的就是**训练时用的 chunk_size /
 *  n_action_steps** 这类会直接影响推理输出的参数,所以这里只挑这几个关键的,
 *  完整的训练参数在训练页那个「训练参数」页签里有。 */
function paramRow(name, value) {
  return el('div', { class: 'flex items-baseline gap-3 text-xs py-0.5' }, [
    el('span', { class: 'text-gray-500 shrink-0 w-32 font-mono', text: name }),
    el('span', { class: 'text-gray-200 font-mono break-all', text: value ?? '—' }),
  ]);
}

const KEY_PARAMS = [
  'policy.type', 'policy.chunk_size', 'policy.n_action_steps',
  'policy.dim_model', 'policy.vision_backbone',
];

function pick(obj, path) {
  return path.split('.').reduce((a, k) => (a === null || a === undefined ? undefined : a[k]), obj);
}

/**
 * @param {HTMLElement} container
 * @param {{runName?: string, checkpointName?: string}} [preselect]
 *        从「模型训练」页签点某个检查点的「推理」跳过来时带上 —— 直接选中它,
 *        不用再手动在下拉里找一遍。
 */
export function renderInference(container, preselect) {
  clear(container);

  const state = { run: null, checkpoint: null, dataset: null };

  // ── 选择模型 / 检查点 ────────────────────────────────
  const runSelect = el('select', { class: INPUT_CLASS + ' w-64' });
  const stepSelect = el('select', { class: INPUT_CLASS + ' w-40', disabled: true });
  const paramHost = el('div', { class: 'mt-3' });
  let runs = [];

  function renderCheckpointParams() {
    clear(paramHost);
    const config = state.checkpoint?.train_config;
    if (!config || !Object.keys(config).length) {
      paramHost.appendChild(el('div', {
        class: 'text-xs text-gray-600 py-2',
        text: tr('inf_no_params', 'This checkpoint has no train_config.json; parameters unavailable.'),
      }));
      return;
    }
    paramHost.appendChild(el('div', {
      class: 'text-[10px] text-gray-600 mb-2',
      text: `${tr('inf_params_from', 'From')} checkpoints/${state.checkpoint.name}/pretrained_model/train_config.json`,
    }));
    paramHost.appendChild(el('div', { class: 'border border-gray-800 rounded p-3' },
      KEY_PARAMS.map((p) => paramRow(p, pick(config, p)))));
  }

  function fillSteps(wantedStep) {
    clear(stepSelect);
    const run = runs.find((r) => r.name === runSelect.value);
    if (!run) {
      stepSelect.disabled = true;
      stepSelect.appendChild(el('option', { value: '', text: '—' }));
      state.checkpoint = null;
      renderCheckpointParams();
      return;
    }
    stepSelect.disabled = false;
    for (const c of run.checkpoints) {
      stepSelect.appendChild(el('option', { value: c.name, text: `${c.name} (${c.step})` }));
    }
    // 有预选就用预选,否则用最新的那个。预选名可能对不上(比如那个 checkpoint
    // 刚被删),对不上就退回最新 —— 不要因为一个过期的参数让整页空着。
    const pick = (wantedStep && run.checkpoints.find((c) => c.name === wantedStep))
      || run.checkpoints[0] || null;
    state.checkpoint = pick;
    if (pick) stepSelect.value = pick.name;
    renderCheckpointParams();
  }

  runSelect.addEventListener('change', () => {
    state.run = runSelect.value || null;
    fillSteps();
  });
  stepSelect.addEventListener('change', () => {
    const run = runs.find((r) => r.name === runSelect.value);
    state.checkpoint = (run?.checkpoints || []).find((c) => c.name === stepSelect.value) || null;
    renderCheckpointParams();
  });

  const sectionModel = section('inf_sec_model', '1. Model', null, [
    el('div', { class: 'flex gap-6 flex-wrap' }, [
      labeled(tr('inf_label_run', 'Run'), runSelect),
      labeled(tr('inf_label_step', 'Checkpoint'), stepSelect),
    ]),
    paramHost,
  ]);
  paramHost.appendChild(el('div', { class: 'text-xs text-gray-600 py-2', text: tr('loading', 'Loading...') }));

  listRuns()
    .then((payload) => {
      runs = payload.runs || [];
      clear(runSelect);
      if (!runs.length) {
        runSelect.appendChild(el('option', { value: '', text: tr('inf_no_models', 'No trained model yet') }));
        renderCheckpointParams();
        return;
      }
      for (const r of runs) {
        runSelect.appendChild(el('option', { value: r.name, text: `${r.name} (${r.checkpoint_count})` }));
      }
      const chosen = runs.find((r) => r.name === preselect?.runName) || runs[0];
      runSelect.value = chosen.name;
      state.run = chosen.name;
      fillSteps(preselect?.checkpointName);
    })
    .catch((err) => {
      clear(paramHost);
      paramHost.appendChild(el('div', {
        class: 'text-xs text-red-400 py-2',
        text: `${tr('train_load_failed', 'Failed to load')}: ${err.message}`,
      }));
    });

  // ── 测试集 ──────────────────────────────────────────
  const datasetSelect = el('select', { class: INPUT_CLASS + ' w-80' }, [
    el('option', { value: '', text: tr('inf_dataset_none', '— Select a test dataset —') }),
  ]);
  datasetSelect.addEventListener('change', () => {
    const value = datasetSelect.value;
    state.dataset = value ? { name: value } : null;
  });
  const datasetNote = el('div', { class: 'text-xs text-gray-600 mt-2' });

  const sectionData = section('inf_sec_data', '2. Test Data',
    { cls: LIVE, text: tr('inf_live', 'live') }, [
      labeled(tr('inf_label_dataset', 'Test dataset'), datasetSelect),
      datasetNote,
    ]);
  datasetNote.textContent = tr('loading', 'Loading...');

  listDatasets()
    .then((payload) => {
      const items = payload.datasets || [];
      datasetNote.textContent = items.length
        ? `${items.length} ${tr('inf_datasets_available', 'dataset(s) available')}`
        : tr('inf_no_datasets', 'No exported dataset yet — export one from the review page first.');
      for (const d of items) {
        datasetSelect.appendChild(el('option', {
          value: d.name,
          text: `${d.name} — ${d.total_episodes} ${tr('train_episodes', 'episodes')}`,
        }));
      }
    })
    .catch(() => { datasetNote.textContent = ''; });

  // ── 运行 ────────────────────────────────────────────
  const sectionRun = section('inf_sec_run', '3. Run', { cls: PENDING, text: tr('inf_pending', 'not wired') }, [
    el('div', { class: 'flex items-center gap-3' }, [
      el('button', {
        class: 'bg-blue-600/40 text-blue-200/60 text-sm px-5 py-2 rounded cursor-not-allowed',
        text: tr('inf_run_btn', 'Run inference'),
        disabled: true,
        title: tr('inf_run_disabled', 'Backend execution is not wired up yet'),
      }),
      el('span', {
        class: 'text-xs text-amber-400',
        text: tr('inf_run_note', 'Backend execution is not connected yet — this button is intentionally disabled.'),
      }),
    ]),
    hint('inf_run_hint', 'Once wired, this runs the checkpoint over the chosen test set and reports direction cosine, amplitude ratio, gripper MAE and the error curve.'),
  ]);

  // ── 结果 ────────────────────────────────────────────
  const sectionResult = section('inf_sec_result', '4. Results', null, [
    el('div', { class: 'text-center py-8' }, [
      el('div', { class: 'text-xs text-gray-600', text: tr('inf_no_result', 'No evaluation result yet.') }),
    ]),
  ]);

  for (const node of [sectionModel, sectionData, sectionRun, sectionResult]) {
    container.appendChild(node);
  }
}

// 这里**没有** boot/自启动 —— 本模块不再是页面入口,而是被 train/main.js
// 当作第三个页签 import 进去(`render: renderInference`)。
// 页签的 render 签名是 (panel, run),本函数只用到 panel,多收一个参数无妨。
