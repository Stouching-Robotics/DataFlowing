/** 数据质检设置面板 —— 按设备卡片分 tab，每张卡片独立配置检查项与阈值。
 *
 * 为什么不用通用的 `NodeSettingsModal`（它渲染 `config_schema`）：
 * 那套是平铺字段列表，表达不了"按设备分组 + 每项带参数"的嵌套结构。
 * AI Annotation 也是同样的理由走了专用弹窗。
 *
 * 数据来源全是后端的 `GET /api/v1/quality/checks` —— 前端不硬编码任何检查项，
 * 后端加一项这里就自动多一行。tab 只显示【工作流里实际连接的】设备卡片。
 */
import { useEffect, useMemo, useState } from 'react';
import { useLang, useT, type Lang } from '../../i18n';
import { createPortal } from 'react-dom';
import type { Edge, Node } from '@xyflow/react';
import { useWorkflowStore } from '../../store/workflowStore';
import type { WorkflowNodeData } from '../../types/workflow';
import {
  getCheckCatalog,
  type CheckCatalog,
  type CheckSpec,
  type QualityNodeConfig,
} from '../../api/quality';

interface Props {
  nodeId: string;
  onClose: () => void;
}

/** 参数名 → 显示用的标签与单位。后端只给 name，展示层补充。 */
// zh 缺省时回落到英文 —— 新加参数忘了翻译只会显示英文，不会显示成空白。
const PARAM_META: Record<string, { label: string; zh?: string; unit?: string; step?: number }> = {
  tolerance_ratio: { label: 'Tolerance ratio', zh: '容差比例', step: 0.001 },
  tolerance_min: { label: 'Tolerance floor', zh: '容差下限', unit: 'frames' },
  min_sec: { label: 'Min duration', zh: '最短时长', unit: 's', step: 0.1 },
  fail_sec: { label: 'Fail duration', zh: '判失败时长', unit: 's', step: 0.5 },
  fail_ratio: { label: 'Fail ratio', zh: '失败占比阈值', step: 0.01 },
  max_errors: { label: 'Allowed error frames', zh: '允许错误帧数' },
  max_linear_mps: { label: 'Max linear speed', zh: '线速度上限', unit: 'm/s', step: 0.1 },
  max_angular_rps: { label: 'Max angular speed', zh: '角速度上限', unit: 'rad/s', step: 1 },
  warn_jumps: { label: 'Warn jump count', zh: '警告跳变数', unit: 'jumps' },
  fail_jumps: { label: 'Fail jump count', zh: '失败跳变数', unit: 'jumps' },
  warn_ratio: { label: 'Warn ratio', zh: '警告占比', step: 0.01 },
  fail_ratio_ratio: { label: 'Fail ratio', zh: '失败占比', step: 0.01 },
  max_out_of_range_ratio: { label: 'Max out-of-range ratio', zh: '越界占比上限', step: 0.01 },
  max_force_drift: { label: 'Force drift limit', zh: '力漂移阈值', unit: 'mN', step: 10 },
  min_coverage: { label: 'Min coverage', zh: '覆盖率下限', step: 0.01 },
  zero_epsilon: { label: 'Zero epsilon', zh: '零判定容差' },
  tolerance: { label: 'Tolerance', zh: '容差', unit: 'frames' },
  // 手套 —— 阵列是 16×16，门限与帧数阈值都来自实测（见 checks/glove/ 各文件）
  expected_size: { label: 'Array width', zh: '阵列维度' },
  max_bad_ratio: { label: 'Max malformed ratio', zh: '不合格帧占比', step: 0.01 },
  min_contact_value: { label: 'Contact floor', zh: '接触门限', step: 50 },
  warn_frames: { label: 'Warn frozen frames', zh: '警告静止帧数', unit: 'frames', step: 10 },
  fail_frames: { label: 'Fail frozen frames', zh: '失败静止帧数', unit: 'frames', step: 50 },
  min_active_ratio: { label: 'Min active ratio', zh: '最低活跃占比', step: 0.01 },
};

/** 单位的中文对照。没有条目的原样显示（mN / rad/s 这类本来就是符号）。 */
const UNIT_ZH: Record<string, string> = { frames: '帧', s: '秒', jumps: '处' };

function paramMeta(name: string, lang: Lang) {
  const meta = PARAM_META[name];
  // 回落分支也要把 unit/step 显式写出来：否则返回的是 `{label}` 这个窄类型，
  // 与有 unit/step 的分支构成联合后，调用处访问 meta.unit 会被 tsc 判为不存在。
  if (!meta) return { label: name, unit: undefined, step: undefined };
  return {
    ...meta,
    label: lang === 'zh' ? (meta.zh ?? meta.label) : meta.label,
    unit: meta.unit && lang === 'zh' ? (UNIT_ZH[meta.unit] ?? meta.unit) : meta.unit,
  };
}

/** 从连线反推这张节点上游连了哪些设备卡片。 */
function connectedDevices(
  nodeId: string, nodes: Node<WorkflowNodeData>[], edges: Edge[],
): string[] {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const seen = new Set<string>();
  const found: string[] = [];
  const queue = edges.filter((e) => e.target === nodeId).map((e) => e.source);
  while (queue.length) {
    const id = queue.shift() as string;
    if (!id || seen.has(id)) continue;
    seen.add(id);
    const node = byId.get(id);
    if (!node) continue;
    const type = String(node.data?.nodeType ?? '');
    // 相机/手套/夹爪三类是"设备卡片"，其余（处理节点）继续往回找
    if (['mono_camera', 'stereo_camera', 'rgb_camera', 'rgbd_camera',
         'stereo_rgbd_camera', 'glove_sensor', 'gripper_device',
         'fisheye_camera'].includes(type)) {
      found.push(type);
      continue;
    }
    for (const edge of edges) if (edge.target === id) queue.push(edge.source);
  }
  return found;
}

/** 前端设备卡片 slug（camera 后缀）→ 后端设备模态（rgb 后缀）。 */
const DEVICE_ALIAS: Record<string, string> = {
  mono_camera: 'mono_rgb',
  stereo_camera: 'stereo_rgb',
  rgb_camera: 'mono_rgb',
  fisheye_camera: 'mono_rgb',
};

export function DeviceQualityModal({ nodeId, onClose }: Props) {
  const t = useT();
  const lang = useLang();
  const nodes = useWorkflowStore((s) => s.nodes);
  const edges = useWorkflowStore((s) => s.edges);
  const node = nodes.find((n) => n.id === nodeId);

  const [catalog, setCatalog] = useState<CheckCatalog | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState<QualityNodeConfig>(
    () => ((node?.data?.config as QualityNodeConfig) ?? {}),
  );
  const [active, setActive] = useState<string>('');

  // 只显示**画布上连到本节点**的设备卡片。
  //
  // 未连接的设备不出现在这里 —— 一个工作流只处理它接的那几类设备，把别的摆出来
  // 只是噪音。
  const devices = useMemo(() => {
    const raw = connectedDevices(nodeId, nodes, edges);
    const mapped = raw.map((t) => DEVICE_ALIAS[t] ?? t);
    return Array.from(new Set(mapped));
  }, [nodeId, nodes, edges]);

  useEffect(() => {
    getCheckCatalog()
      .then((data) => {
        setCatalog(data);
        // 默认选中第一个"有检查项"的设备卡片
        const first = devices.find((d) => (data.by_device[d] ?? []).length);
        setActive((prev) => prev || first || devices[0] || '');
      })
      .catch((err) => setError(String(err?.message ?? err)));
  }, [devices]);

  // Esc 关闭。
  //
  // 必须用**捕获阶段** + stopPropagation：React Flow 自己也听 Esc（取消连线、
  // 清空选择）。若在冒泡阶段挂，按一下会「既关弹窗、又顺手清掉画布选择」。
  // window 是捕获路径的第一个节点，在这里拦下，画布收不到。
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return;
      e.stopPropagation();
      e.preventDefault();
      onClose();
    };
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [onClose]);

  const checksOf = (device: string): CheckSpec[] => {
    if (!catalog) return [];
    const slugs = catalog.by_device[device] ?? [];
    return catalog.checks.filter((c) => slugs.includes(c.slug));
  };

  const entryOf = (device: string, slug: string) =>
    draft.devices?.[device]?.checks?.[slug] ?? {};

  const setEntry = (device: string, slug: string, patch: Record<string, unknown>) => {
    setDraft((prev) => ({
      devices: {
        ...(prev.devices ?? {}),
        [device]: {
          ...(prev.devices?.[device] ?? {}),
          checks: {
            ...(prev.devices?.[device]?.checks ?? {}),
            [slug]: { ...entryOf(device, slug), ...patch },
          },
        },
      },
    }));
  };

  const save = () => {
    useWorkflowStore.setState((state) => ({
      nodes: state.nodes.map((n) =>
        n.id === nodeId
          ? { ...n, data: { ...n.data, config: { ...n.data.config, ...draft } } }
          : n,
      ),
    }));
    onClose();
  };

  const byCategory = (checks: CheckSpec[]) => {
    const groups: Record<string, CheckSpec[]> = {};
    for (const c of checks) (groups[c.category] ??= []).push(c);
    return groups;
  };

  // ★ 必须走 portal 挂到 document.body。
  //
  // React Flow 的 viewport 带 ``transform: translate(...) scale(...)``，而 CSS
  // 规范规定 transform 会为后代创建新的**包含块** —— 于是 position:fixed 是相对
  // 那块被缩放平移过的画布定位的：弹窗跟着画布跑、跟着缩放变形、半透明背景也
  // 只盖住画布区域而不是整个屏幕。挂到 body 才脱离这个容器。
  return createPortal(
    <div className="modal-portal flex items-center justify-center bg-black/60"
         onClick={onClose}>
      <div className="w-[min(880px,92vw)] max-h-[85vh] overflow-hidden rounded-lg border border-gray-700
                      bg-[#0f172a] text-gray-200 shadow-2xl flex flex-col"
           onClick={(e) => e.stopPropagation()}>

        <div className="flex items-center justify-between border-b border-gray-700 px-4 py-2">
          <span className="text-sm font-medium">{t('quality.title')}</span>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-200">✕</button>
        </div>

        {/* 设备 tab 栏 —— 只显示工作流里实际连接的卡片 */}
        <div className="flex gap-1 border-b border-gray-700 px-3 pt-2">
          {devices.length === 0 && (
            <span className="px-2 py-1 text-xs text-gray-500">
              {t('quality.noDevice')}
            </span>
          )}
          {devices.map((device) => {
            const count = checksOf(device).length;
            const isActive = device === active;
            return (
              <button
                key={device}
                onClick={() => setActive(device)}
                className={`rounded-t border-b-2 px-3 py-1.5 text-xs transition-colors ${
                  isActive
                    ? 'border-cyan-400 bg-cyan-900/20 text-cyan-300'
                    : 'border-transparent text-gray-400 hover:text-gray-200'
                }`}>
                {catalog?.device_labels[device] ?? device}
                {count > 0 && <span className="ml-1 text-[10px] opacity-60">{count}</span>}
              </button>
            );
          })}
        </div>

        {/* 检查项列表 —— **紧凑行**，不是卡片。
            之前每个检查项是一张卡（开关行 + 说明行 + 参数行 + 边框间距 ≈ 85px），
            9 项就是 765px，加头/tab/底部超过 82vh，「必须滚动」是尺寸算出来的，
            换滚动条样式治不了本。压成一行后每项 ~30px，整块约 320px，不再滚动。
            说明文字移到 label 的 title（hover 出），参数原名移到输入框的 title。 */}
        <div className="dark-scroll min-h-0 flex-1 overflow-y-auto px-3 py-2">
          {error && <div className="text-xs text-red-400">{t('quality.loadFailed')}{error}</div>}
          {!catalog && !error && <div className="text-xs text-gray-500">{t('common.loading')}</div>}

          {catalog && active && (
            <>
              {checksOf(active).length === 0 && (
                <div className="text-xs text-gray-500">
                  {catalog.device_labels[active] ?? active} {t('quality.noChecks')}
                </div>
              )}
              {Object.entries(byCategory(checksOf(active))).map(([category, items]) => (
                <div key={category} className="mb-1.5">
                  <div className="px-1 pb-0.5 pt-1.5 text-[10px] uppercase tracking-wider text-gray-500">
                    {category}
                  </div>
                  {items.map((check) => {
                    const entry = entryOf(active, check.slug);
                    const enabled = entry.enabled !== false;
                    return (
                      <div key={check.slug}
                           className="flex items-center gap-2 rounded px-1 py-[3px] hover:bg-white/[0.04]">
                        <input type="checkbox" checked={enabled}
                               className="shrink-0"
                               onChange={(e) =>
                                 setEntry(active, check.slug, { enabled: e.target.checked })} />

                        {/* 固定宽 + truncate：名称长短不一，不固定的话右边的参数
                            列会左右错开。截断的部分由 title 补全。 */}
                        <span
                          className="w-[104px] shrink-0 truncate text-xs text-gray-200"
                          title={check.description
                            ? `${check.label} — ${check.description}`
                            : check.label}>
                          {check.label}
                        </span>

                        {/* 参数区可伸缩 + 可换行：参数多的检查项（如 SLAM 连续性
                            有线速/角速两个）超宽时换行，行高自动长一点，不会溢出。 */}
                        <div className="flex min-w-0 flex-1 flex-wrap items-center gap-x-2.5 gap-y-1">
                          {enabled && Object.entries(check.default_params).map(([name, fallback]) => {
                            const meta = paramMeta(name, lang);
                            const value = (entry.params?.[name] ?? fallback) as number;
                            return (
                              <label key={name} title={name}
                                     className="flex items-center gap-1 text-[10px] text-gray-400">
                                {meta.label}
                                <input
                                  type="number"
                                  className="w-[68px] rounded border border-gray-700 bg-gray-900 px-1 py-0.5 text-right text-gray-200"
                                  value={value}
                                  step={meta.step ?? 1}
                                  onChange={(e) =>
                                    setEntry(active, check.slug, {
                                      params: {
                                        ...(entry.params ?? {}),
                                        [name]: Number(e.target.value),
                                      },
                                    })} />
                                {meta.unit && <span className="text-gray-600">{meta.unit}</span>}
                              </label>
                            );
                          })}
                        </div>

                        {/* slug 固定宽右对齐 —— 它是报告里的键名，用来对账，
                            最长 "umi.slam_continuity"(19 字符 @10px ≈ 95px)。 */}
                        <span className="w-[104px] shrink-0 truncate text-right text-[10px] text-gray-600"
                              title={check.slug}>
                          {check.slug}
                        </span>
                      </div>
                    );
                  })}
                </div>
              ))}
            </>
          )}
        </div>

        <div className="flex justify-end gap-2 border-t border-gray-700 px-4 py-2">
          <button onClick={onClose}
                  className="rounded border border-gray-700 px-3 py-1 text-xs text-gray-300">
            {t('common.cancel')}
          </button>
          <button onClick={save}
                  className="rounded border border-cyan-700 bg-cyan-900/40 px-3 py-1 text-xs text-cyan-200">
            {t('common.save')}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
