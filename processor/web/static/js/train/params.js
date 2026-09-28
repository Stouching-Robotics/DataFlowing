/** 「训练参数」页签 —— 渲染 train_config.json 里的真实超参。
 *
 * 数据是**训练当时实际用的那份配置**(lerobot-train 自己写进 checkpoint 的),
 * 不是我们事后补的默认值。
 *
 * 缺字段就**整行不渲染**。不同 LeRobot 版本的键名会变(我们比参考实现少
 * `scheduler_state.json`、多四个 pre/postprocessor 文件就是同一类差异),写死
 * "缺了就显示 N/A"会让人误以为训练时那个参数是 null。
 */

import { el, clear } from '../shared/dom.js';
import { thousands } from '../shared/format.js';
import { tr } from '../shared/text.js';

/** 分组 → 每行 [显示名, 取值路径, 可选格式化函数]。
 *
 *  分组标题存的是 **i18n key** 而不是翻好的文字:它是模块级常量,在这里调
 *  `tr()` 会在模块加载那一刻定死,用户之后再切语言就不会跟着变。渲染时再取。 */
const GROUPS = [
  {
    titleKey: 'train_group_train',
    titleFallback: 'Training',
    rows: [
      ['steps', 'steps', thousands],
      ['batch_size', 'batch_size'],
      ['save_freq', 'save_freq', thousands],
      ['seed', 'seed'],
      ['num_workers', 'num_workers'],
    ],
  },
  {
    titleKey: 'train_group_policy',
    titleFallback: 'Policy',
    rows: [
      ['type', 'policy.type'],
      ['dim_model', 'policy.dim_model'],
      ['chunk_size', 'policy.chunk_size'],
      ['n_action_steps', 'policy.n_action_steps'],
      ['vision_backbone', 'policy.vision_backbone'],
      ['kl_weight', 'policy.kl_weight'],
    ],
  },
  {
    titleKey: 'train_group_optimizer',
    titleFallback: 'Optimizer',
    rows: [
      ['type', 'optimizer.type'],
      ['lr', 'optimizer.lr'],
      ['weight_decay', 'optimizer.weight_decay'],
      ['grad_clip_norm', 'optimizer.grad_clip_norm'],
    ],
  },
  {
    titleKey: 'train_group_dataset',
    titleFallback: 'Dataset',
    rows: [
      ['repo_id', 'dataset.repo_id'],
      ['root', 'dataset.root'],
      ['eval_split', 'dataset.eval_split'],
      ['episodes', 'dataset.episodes'],
    ],
  },
];

/** 按点号路径取值;任一层缺失返回 undefined(不抛)。 */
function pick(obj, path) {
  return path.split('.').reduce(
    (acc, key) => (acc === null || acc === undefined ? undefined : acc[key]),
    obj,
  );
}

/** 值 → 显示文本。null/undefined/空串统一成 '—',数组/对象转紧凑 JSON。 */
function display(value, format) {
  if (value === null || value === undefined || value === '') return '—';
  if (format) return format(value);
  if (Array.isArray(value) || typeof value === 'object') {
    return JSON.stringify(value);
  }
  return String(value);
}

function renderGroup(group, config) {
  // 先算一遍哪些行有值 —— 全组都没值的分组直接不渲染,避免出现空卡片。
  const present = group.rows
    .map(([label, path, format]) => [label, pick(config, path), format])
    .filter(([, value]) => value !== undefined && value !== null);

  if (!present.length) return null;

  return el('div', { class: 'bg-gray-900 rounded-lg border border-gray-800 p-4' }, [
    el('div', {
      class: 'text-xs font-semibold text-gray-400 mb-3',
      text: tr(group.titleKey, group.titleFallback),
    }),
    el('div', { class: 'space-y-1.5' }, present.map(([label, value, format]) => (
      el('div', { class: 'flex items-baseline gap-3 text-xs' }, [
        el('span', { class: 'text-gray-500 shrink-0 w-32', text: label }),
        el('span', {
          class: 'text-gray-200 font-mono break-all',
          text: display(value, format),
        }),
      ])
    ))),
  ]);
}

/**
 * @param {HTMLElement} container
 * @param {object} config        train_config.json 的内容
 * @param {{step: number}} meta  这份配置取自哪个 checkpoint
 */
export function renderParams(container, config, meta = {}) {
  clear(container);

  if (!config || Object.keys(config).length === 0) {
    container.appendChild(emptyState(
      tr('train_params_empty',
         'No train_config.json in this checkpoint; parameters unavailable.'),
    ));
    return;
  }

  container.appendChild(el('div', {
    class: 'text-xs text-gray-500 mb-4',
    text: `${tr('train_params_source', 'Source:')} `
      + `checkpoints/${meta.step ?? '?'}/pretrained_model/train_config.json`,
  }));

  const cards = GROUPS.map((g) => renderGroup(g, config)).filter(Boolean);
  container.appendChild(
    el('div', { class: 'grid grid-cols-1 md:grid-cols-2 gap-4' }, cards),
  );
}

function emptyState(message) {
  return el('div', {
    class: 'text-center text-gray-600 text-sm py-10',
    text: message,
  });
}
