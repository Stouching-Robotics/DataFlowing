import { memo, useMemo, useState, type KeyboardEvent, type FocusEvent } from 'react';
import { Handle, Position, type NodeProps } from '@xyflow/react';
import { IconifyIcon } from '../IconifyIcon';
import { DYNAMIC_INPUT_TYPES, dynamicInputKey, useWorkflowStore } from '../../../store/workflowStore';
import { nodeLabel, portLabel, useLang, useT } from '../../../i18n';
import { useUiStore } from '../../../store/uiStore';
import { getNodeType } from './registry';
import { NodeSettingsModal } from '../NodeSettingsModal';
import { DeviceQualityModal } from '../DeviceQualityModal';
import type { ConfigField, DeviceInputSource, WorkflowNodeData } from '../../../types/workflow';

const CAMERA_TYPES = [
  'mono_camera', 'rgbd_camera', 'rgb_camera', 'fisheye_camera',
  'stereo_camera', 'stereo_rgbd_camera',
];
const DEVICE_INPUT_TYPES = [...CAMERA_TYPES, 'gripper_device'];
const DEPTH_PROCESS_TYPES = ['rgbd_to_3d_bare_hand', 'rgbd_to_3d_black_glove'];

const DEVICE_CATEGORY_LABELS: Record<string, string> = {
  rgbd_camera: 'RGB-D Camera',
  stereo_rgbd_camera: 'Stereo RGB-D Camera',
  mono_rgb: 'RGB Camera',
  stereo_rgb: 'Stereo RGB Camera',
  glove_sensor: 'Glove Sensor',
  gripper_device: 'UMI Gripper',
};

function cameraCategoryLabel(data: WorkflowNodeData): string {
  // The card category is a design-time contract. It must come from the node
  // type, never from a stale physical-device metadata field left by an older
  // project/workflow.
  if (data.nodeType === 'stereo_rgbd_camera') return DEVICE_CATEGORY_LABELS.stereo_rgbd_camera;
  if (data.nodeType === 'stereo_camera') return DEVICE_CATEGORY_LABELS.stereo_rgb;
  if (data.nodeType === 'rgbd_camera') return DEVICE_CATEGORY_LABELS.rgbd_camera;
  if (data.nodeType === 'mono_camera' || data.nodeType === 'rgb_camera'
      || data.nodeType === 'fisheye_camera') return DEVICE_CATEGORY_LABELS.mono_rgb;
  if (data.nodeType === 'gripper_device') return DEVICE_CATEGORY_LABELS.gripper_device;
  const byType = data.device_type ? DEVICE_CATEGORY_LABELS[String(data.device_type)] : '';
  if (byType) return byType;
  const stored = String(data.device_display_name || '');
  if (Object.values(DEVICE_CATEGORY_LABELS).includes(stored)) return stored;
  return DEVICE_CATEGORY_LABELS.mono_rgb;
}

/** 一条边的上游端口名 —— 取不到就回落到 sourceHandle 本身。 */
function upstreamPortLabelOf(
  source: { data?: { nodeType?: string; outputs?: { key: string; label: string }[] } },
  sourceHandle: string | null | undefined,
  lang: 'en' | 'zh',
): string {
  const outputs = source.data?.outputs || [];
  const handle = String(sourceHandle ?? '');
  const port = outputs.find((o) => o.key === handle)
    || (outputs.length === 1 ? outputs[0] : null);
  const raw = port?.label || handle;
  // 用**上游**的类型翻译 —— data 这个 key 在不同卡上叫法不同
  return portLabel(String(source.data?.nodeType ?? ''), port?.key || handle, raw, lang);
}

/**
 * Data Quality 的输入端口是**按入边生成**的：每接一个设备就多一个口，口名就是
 * 上游那个端口的名字。在此之前所有线都挤在同一个 ``data`` 口上，口名只能把几个
 * 上游的名字拼起来（"Glove Sensor Data, RGB Video"），越长越读不出来。
 *
 * 端口 key 用 ``dynamicInputKey(源节点, 源端口)``——稳定且可重复，同一个上游
 * 重连仍是同一个口。老图的边在 ``normalizeWorkflowGraphForEditor`` 里已经迁移过。
 *
 * 返回**字符串**（JSON）而不是数组：zustand 按 Object.is 比较，每次返回新数组会
 * 让整块画布每个节点在任意 store 变动时都重渲染。
 */
function useDynamicInputs(nodeId: string, enabled: boolean, lang: 'en' | 'zh'): [string, string][] {
  const encoded = useWorkflowStore((s) => {
    if (!enabled) return '';
    const incoming = s.edges.filter((e) => e.target === nodeId);
    if (!incoming.length) return '';
    return JSON.stringify(incoming.map((edge) => {
      const source = s.nodes.find((n) => n.id === edge.source);
      const key = dynamicInputKey(String(edge.source), edge.sourceHandle);
      return [key, source ? upstreamPortLabelOf(source, edge.sourceHandle, lang) : key];
    }));
  });
  return useMemo(
    () => (encoded ? (JSON.parse(encoded) as [string, string][]) : []),
    [encoded],
  );
}

export const WorkflowNodeComponent = memo(function WorkflowNodeComponent({ data, selected, id }: NodeProps) {
  const d = data as unknown as WorkflowNodeData;
  const isDynamicInputs = DYNAMIC_INPUT_TYPES.has(String(d.nodeType));
  const t = useT();
  const lang = useLang();
  const dynamicInputs = useDynamicInputs(id, isDynamicInputs, lang);
  const hdrColor = d.color || '#475569';
  const isCamera = DEVICE_INPUT_TYPES.includes(d.nodeType);
  // 画布节点悬停说明:与侧边栏一致(功能 + 可连接性)
  const desc = getNodeType(d.nodeType)?.description;
  const topField = getTopConfigField(d);
  // 项目级设备命名映射:本项目对该节点的绑定值优先显示(工作流本身不动)
  const binding = useWorkflowStore((s) => s.currentWorkflowBindings[id]);
  const referenceInputs = useWorkflowStore((s) => s.referenceInputs);
  // Concrete device names are project-scoped. Do not fall back to the global
  // online-device directory here: a new/empty workflow must not display a
  // device from another project. The fixed input categories live in the
  // palette and do not depend on this value.
  const availableInputs = referenceInputs;
  const inputContextReady = availableInputs !== null;
  const isDepthProcessor = DEPTH_PROCESS_TYPES.includes(d.nodeType);
  const isBound = !!binding && (!!binding.source_key || !!binding.source_keys);
  const boundValue = isBound
    ? (binding.source_key ?? binding.source_keys ?? '')
    : '';
  const topValue = topField?.name === 'source_key'
    ? (isBound ? boundValue : (d.config?.source_key ?? d.config?.position ?? topField.default ?? ''))
    : (d.config?.[topField?.name || ''] ?? topField?.default ?? '');

  // 绑定态编辑:输入过程只收集草稿(避免每次击键弹确认框),
  // 失焦/回车时一次性提交"仅本项目"
  const [draft, setDraft] = useState<string | null>(null);
  const shownValue = isBound ? (draft ?? boundValue) : topValue;
  // The node title is a fixed physical-device category (for example RGB-D
  // Camera). The editable field is kept independent and shows the concrete
  // source name/key; changing it must not rename the category.
  const nodeTitle = nodeLabel(String(d.nodeType),
    isCamera ? cameraCategoryLabel(d) : d.label, lang);
  const deviceInputValue = isCamera && inputContextReady
    ? getDeviceInputValue(shownValue, availableInputs, d.nodeType, d.device_type)
    : '';
  const deviceOptions = isCamera && inputContextReady
    ? getDeviceOptions(availableInputs, d.nodeType, d.device_type) : [];
  const depthOptions = isDepthProcessor && inputContextReady
    ? getDepthOptions(availableInputs) : [];
  const configuredDepth = String(d.config?.depth_camera || '');
  const selectedDepth = depthOptions.includes(configuredDepth) ? configuredDepth : '';
  const controlValue = isCamera ? deviceInputValue : shownValue;

  // 输入端口 = 声明的口 + 动态口（Data Quality 专有，一条入边一个）。
  //
  // **有动态口时不再渲染那个空的声明口** —— 它只是个连接落点，跟动态口并排
  // 就是多一行噪音（"Data" 下面跟着 "Glove Sensor Data"）。
  // 再加一个设备怎么办：往**任意已有的口**上拖即可，onConnect 会把 targetHandle
  // 重写成新上游自己的 key，于是长出一个新口。
  const inputPorts = useMemo(() => [
    ...(dynamicInputs.length ? [] : (d.inputs || []).map((inp) => ({
      key: inp.key,
      originalLabel: inp.label,
      label: portLabel(String(d.nodeType), inp.key, inp.label, lang),
    }))),
    ...dynamicInputs.map(([key, label]) => ({
      key, originalLabel: label, label,
    })),
  ], [d.inputs, dynamicInputs, d.nodeType, lang]);

  const resolveCameraSource = (value: string): DeviceInputSource | null => {
    const normalized = value.trim().toLowerCase();
    if (!normalized) return null;
    return (availableInputs?.device_sources || []).find((source) =>
      sourceMatchesCameraType(source, d.nodeType, d.device_type)
      && (source.name.toLowerCase() === normalized
        || source.display_name?.toLowerCase() === normalized
        || source.source_keys.some((key) => key.toLowerCase() === normalized)
        || source.source_key?.toLowerCase() === normalized),
    ) || null;
  };

  const canonicalCameraValue = (value: string): string => {
    const source = resolveCameraSource(value);
    if (!source) return value.trim();
    return source.input_type === 'stereo_camera'
      || source.input_type === 'stereo_rgbd_camera'
      ? source.source_keys.join(',')
      : (source.source_key || source.source_keys[0] || source.name);
  };

  const handleChange = (value: unknown) => {
    const raw = String(value ?? '');
    const canonical = isCamera ? canonicalCameraValue(raw) : raw;
    if (isBound) { setDraft(canonical); return; }
    updateTopConfig(canonical, isCamera ? resolveCameraSource(raw) : null);
  };

  const commitBound = async (raw: string) => {
    setDraft(null);
    if (!topField || !isBound) return;
    const value = isCamera ? canonicalCameraValue(raw) : raw.trim();
    if (value === boundValue) return;  // 未改动 → 不弹窗
    const ok = await useUiStore.getState().confirm(
      `"${topField.label}" is overridden for this project ("${boundValue}"). Save "${value}" for this project only?`,
      { title: t('node.projectOverride'), confirmLabel: t('node.saveForProject') },
    );
    if (!ok) return;
    useWorkflowStore.getState().setProjectBinding(id, value || null)
      .catch((e) => useUiStore.getState().pushToast(`Failed to save project binding: ${e?.message || e}`, 'error'));
  };

  const handleBlur = (e: FocusEvent<HTMLInputElement>) => {
    if (isBound) commitBound(e.target.value);
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (!isBound) return;
    if (e.key === 'Enter') commitBound((e.target as HTMLInputElement).value);
    if (e.key === 'Escape') setDraft(null);
  };

  // AI Annotation 卡片统一显示 ⚙,无论 local/api 都从同一设置弹窗配置
  const [showApiModal, setShowApiModal] = useState(false);
  const isAiAnnotation = d.nodeType === 'ai_annotation';

  // Data Quality 走自己的设置弹窗 —— 按设备卡片分 tab,通用的 config_schema
  // 那套平铺字段表达不了嵌套结构(见 DeviceQualityModal 的注释)。
  const [showQualityModal, setShowQualityModal] = useState(false);
  const isDataQuality = d.nodeType === 'data_quality';

  const updateTopConfig = (value: unknown, selectedSource: DeviceInputSource | null = null) => {
    if (!topField) return;
    const config = { ...d.config, [topField.name]: value };
    // Keep old camera graphs compatible with the original position field.
    if (isCamera && topField.name === 'source_key') {
      config.position = value;
      if (selectedSource) {
        config.device_name = selectedSource.name;
        if (selectedSource.input_type === 'gripper_device') {
          config.source_keys = selectedSource.source_keys.join(',');
        } else if (selectedSource.input_type === 'stereo_camera'
            || selectedSource.input_type === 'stereo_rgbd_camera') {
          config.source_keys = selectedSource.source_keys.join(',');
        } else {
          delete config.source_keys;
        }
      }
    }
    $updateNode(id, { config });
  };

  const clearBinding = async () => {
    if (!isBound) return;
    const ok = await useUiStore.getState().confirm(
      'Clear this project override? The workflow default will be used again.',
      { title: t('node.clearOverride'), confirmLabel: t('node.clear') },
    );
    if (!ok) return;
    useWorkflowStore.getState().setProjectBinding(id, null)
      .catch((e) => useUiStore.getState().pushToast(`Failed to clear binding: ${e?.message || e}`, 'error'));
  };

  // 卡片高度按**这张卡自己的端口数**算。
  //
  // 之前高度是画布统一算的（取所有模块声明端口数的最大值），对**运行时**才长出来的
  // 动态口无能为力：Data Quality 接第 5 个设备时它根本没变，卡片被撑破。
  // 改成每张卡按自己实际的口数算，设在自己身上（CSS 自定义属性会继承，
  // 设在本节点上只影响本节点）。
  const portRows = inputPorts.length + (d.outputs || []).length;
  const cardHeight = Math.max(2, portRows) * 22 + 42;   // 22px/行 + 头部与内边距

  return (
    <div className="workflow-node" title={desc} style={{
      borderColor: selected ? '#3b82f6' : d.color || '#334155',
      boxShadow: selected ? `0 0 0 2px ${d.color}33` : undefined,
      '--node-card-height': `${cardHeight}px`,
    } as React.CSSProperties}>
      <div className="node-header" style={{ backgroundColor: `${hdrColor}22`, borderBottom: `1px solid ${hdrColor}44` }}>
        <div className="node-title-row">
          <IconifyIcon icon={d.icon} className="text-[18px]" />
          <span className="text-xs font-semibold" title={nodeTitle}>{nodeTitle}</span>
        </div>
        <div className="node-header-control" onClick={(e) => e.stopPropagation()}>
          {topField && (
            <ConfigControl field={topField} value={controlValue} onChange={handleChange}
                           onBlur={handleBlur} onKeyDown={handleKeyDown}
                           suggestions={deviceOptions} listId={`device-options-${id}`}
                           placeholder={isCamera ? 'Auto-match / enter source' : undefined} />
          )}
          {isAiAnnotation && (
            <button
              title={t('node.aiSettings')}
              onClick={(e) => { e.stopPropagation(); setShowApiModal(true); }}
              className="ml-1 text-[11px] text-cyan-400 border border-cyan-900 rounded px-1 cursor-pointer hover:bg-cyan-900/40 select-none">
              ⚙
            </button>
          )}
          {isDataQuality && (
            <button
              title="质检设置(按设备配置检查项)"
              onClick={(e) => { e.stopPropagation(); setShowQualityModal(true); }}
              className="ml-1 text-[11px] text-cyan-400 border border-cyan-900 rounded px-1 cursor-pointer hover:bg-cyan-900/40 select-none">
              ⚙
            </button>
          )}
          {isBound && (
            <span
              onClick={clearBinding}
              title={t('node.clearOverrideHint')}
              className="ml-1 text-[9px] text-blue-400 border border-blue-800 rounded px-1 cursor-pointer hover:bg-blue-900/40 align-middle select-none">
              P
            </span>
          )}
        </div>
      </div>
      <div className="node-body">
        {/* Input ports: handle on left edge, label next to it (left-aligned) */}
        {inputPorts.map((inp) => (
          <div key={inp.key} className="node-port" style={{ justifyContent: 'flex-start', paddingLeft: 0 }}>
            <Handle type="target" position={Position.Left} id={inp.key} style={{ position: 'relative', left: -6, transform: 'none', flexShrink: 0 }} />
            {/* 泛化端口显示上游端口名；原文案留作 hover 提示。
                动态端口每个口只对应一条边，标签就是那条边的上游端口名。 */}
            <span title={inp.originalLabel}>{inp.label}</span>
          </div>
        ))}
        {/* Output ports: label next to handle, handle on right edge (right-aligned) */}
        {(d.outputs || []).map((out) => (
          <div key={out.key} className="node-port" style={{ justifyContent: 'flex-end', paddingRight: 0 }}>
            {/* 输出端口**保持原文案** —— 之前让透传节点的输出也跟着显示数据种类，
                结果同一张卡上下两行是同一串文字（如 Data Quality 左右各一行
                "tactile, device_status, imu"），看着很乱。端口文字只有输入侧需要
                反映实际流经的数据；输出是本节点产出的什么，用声明文案更清楚。 */}
            <span>{portLabel(String(d.nodeType), out.key, out.label, lang)}</span>
            <Handle type="source" position={Position.Right} id={out.key} style={{ position: 'relative', right: -6, transform: 'none', flexShrink: 0 }} />
          </div>
        ))}
      </div>
      {showApiModal && (
        <NodeSettingsModal nodeId={id} onClose={() => setShowApiModal(false)} />
      )}
      {showQualityModal && (
        <DeviceQualityModal nodeId={id} onClose={() => setShowQualityModal(false)} />
      )}
    </div>
  );
});

function getDeviceInputValue(
  sourceValue: unknown,
  inputs: {
    devices?: Array<{ name?: string; display_name?: string }>;
    device_sources?: DeviceInputSource[];
  } | null,
  nodeType?: string,
  deviceType?: unknown,
): string {
  const rawKeys = String(sourceValue || '')
    .split(',')
    .map((value) => value.trim())
    .filter(Boolean);
  // During loading, and for a workflow without a project context, hide the
  // persisted source key instead of showing a stale device from another
  // project. The input remains editable and can be selected once project
  // inputs are available.
  if (!inputs) return '';

  const names = rawKeys.map((key) => {
    const source = (inputs.device_sources || []).find((item) =>
      sourceMatchesCameraType(item, nodeType || '', deviceType)
      && (item.source_key?.toLowerCase() === key.toLowerCase()
        || item.source_keys.some((sourceKey) => sourceKey.toLowerCase() === key.toLowerCase())),
    );
    return source?.name || source?.display_name || source?.source_key || '';
  });

  // Empty or stale source_key means auto-detect. If the project has exactly
  // one unambiguous source for this fixed card type, show that physical name
  // even when an older workflow saved a no-longer-valid key such as ``S80M``.
  // The config is not changed until the user selects/edits the input.
  if (!names.length) {
    const sources = (inputs.device_sources || [])
      .filter((source) => !nodeType || sourceMatchesCameraType(source, nodeType, deviceType));
    const fallbackCandidates = sources.length || inputs.device_sources?.length
      ? sources.map((source) => String(source.name || source.display_name || source.source_key || ''))
      : (inputs.devices || []).map((device) => String(device.name || device.display_name || ''));
    const fallback = Array.from(new Set(fallbackCandidates.filter(Boolean)));
    if (fallback.length === 1) names.push(fallback[0]);
  }
  return Array.from(new Set(names)).join(', ');
}

function getDeviceOptions(
  inputs: { device_sources?: DeviceInputSource[] } | null,
  nodeType: string,
  deviceType?: unknown,
): string[] {
  if (!inputs) return [];
  // Suggestions are editable physical source names, not the fixed category
  // label. On commit they are resolved back to the canonical source_key.
  const names = (inputs.device_sources || [])
    .filter((source) => sourceMatchesCameraType(source, nodeType, deviceType))
    .map((source) => source.name || source.source_key)
    .filter((name): name is string => Boolean(name));
  return Array.from(new Set(names));
}

function getDepthOptions(
  inputs: { device_sources?: DeviceInputSource[] } | null,
): string[] {
  if (!inputs) return [];
  return Array.from(new Set(
    (inputs.device_sources || []).flatMap((source) => source.depth_keys || [])
      .map((value) => String(value || '').trim())
      .filter(Boolean),
  ));
}

function sourceMatchesCameraType(
  source: DeviceInputSource,
  nodeType: string,
  storedDeviceType?: unknown,
): boolean {
  const sourceType = String(source.device_type || '').toLowerCase();
  const sourceInput = String(source.input_type || '').toLowerCase();
  const hasDepth = Boolean(source.depth_keys?.length);
  const category = sourceType
    || (sourceInput === 'glove_sensor' ? 'glove_sensor'
    : sourceInput === 'stereo_rgbd_camera' ? 'stereo_rgbd_camera'
      : sourceInput === 'stereo_camera' ? 'stereo_rgb'
        : hasDepth || sourceInput === 'rgbd_camera' ? 'rgbd_camera' : 'mono_rgb');

  // The node type is authoritative. Stored device metadata may have been
  // copied from a different project and must never move a card into another
  // camera category.
  if (nodeType === 'stereo_rgbd_camera') return category === 'stereo_rgbd_camera';
  if (nodeType === 'rgbd_camera') return category === 'rgbd_camera';
  if (nodeType === 'stereo_camera') return category === 'stereo_rgb';
  if (nodeType === 'glove_sensor') return category === 'glove_sensor';
  if (nodeType === 'gripper_device') return category === 'gripper_device';
  if (storedDeviceType && String(storedDeviceType) === 'rgbd_camera') {
    return category === 'rgbd_camera';
  }
  // A fixed Mono RGB card must only resolve a real Mono RGB source. RGB-D and
  // Stereo are separate contracts because their downstream ports differ.
  return category === 'mono_rgb';
}

function getTopConfigField(data: WorkflowNodeData): ConfigField | null {
  const schema = data.configSchema || [];
  if (DEVICE_INPUT_TYPES.includes(data.nodeType)) {
    return schema.find((field) => field.name === 'source_key') || {
      name: 'source_key', type: 'string', label: 'Source key',
      default: data.config?.source_key || data.config?.position || '',
    };
  }
  if (data.nodeType === 'lerobot_export') {
    return schema.find((field) => field.name === 'version') || {
      name: 'version', type: 'select', label: 'Version', default: 'v3.0', options: ['v2.1', 'v3.0'],
    };
  }
  if (data.nodeType === 'mediapipe_hand') {
    // 从后端 configSchema 读 device 选项(auto/cpu/cuda:0),避免硬编码与 schema 不一致
    const deviceField = schema.find((f) => f.name === 'device');
    return deviceField || {
      name: 'device', type: 'select', label: 'Device', default: 'auto', options: ['auto', 'cpu', 'cuda:0'],
    };
  }
  if (data.nodeType === 'ai_annotation') {
    // VLM 供应商:local(本地 vLLM)/ api(云端厂商;选 api 后头部
    // 出现 ⚙ 按钮 → API 设置弹窗)
    return schema.find((field) => field.name === 'vlm_provider') || {
      name: 'vlm_provider', type: 'select', label: 'VLM provider',
      default: 'local', options: ['local', 'api'],
    };
  }
  if (data.nodeType === 'data_cleaning') {
    // 异常处理方式,直接放头部 —— 检查哪些类型、阈值多少在 configSchema 里,
    // 由后端 catalog 提供(与 mediapipe_hand 的 device 同款:优先读后端 schema,
    // 读不到才用这里的兜底),避免前端硬编码与后端不一致。
    return schema.find((field) => field.name === 'on_failure') || {
      name: 'on_failure', type: 'select', label: 'On failure', default: 'review',
      options: ['review', 'warn', 'block'],
    };
  }
  return null;
}

function ConfigControl({ field, value, onChange, onBlur, onKeyDown, suggestions = [], listId, placeholder }: {
  field: ConfigField;
  value: unknown;
  onChange: (value: unknown) => void;
  onBlur?: (e: FocusEvent<HTMLInputElement>) => void;
  onKeyDown?: (e: KeyboardEvent<HTMLInputElement>) => void;
  suggestions?: string[];
  listId?: string;
  placeholder?: string;
}) {
  const current = value ?? field.default ?? '';
  if (field.type === 'boolean') {
    return <input type="checkbox" checked={Boolean(current)} onChange={(e) => onChange(e.target.checked)} />;
  }
  if (field.type === 'select') {
    const options = field.options || [];
    const selected = options.includes(String(current)) ? String(current) : options[0] || '';
    return <select aria-label={field.label} value={selected} onChange={(e) => onChange(e.target.value)} className="node-header-select">
      {options.map((option) => {
        const label = field.name === 'prompt_language'
          ? ({ zh: '中文', en: 'English' } as Record<string, string>)[option] || option
          : option === 'cuda:0' ? 'GPU' : option.toUpperCase();
        return <option key={option} value={option}>{label}</option>;
      })}
    </select>;
  }
  return <>
    <input
      aria-label={field.label}
      type={field.type === 'number' ? 'number' : 'text'} value={String(current)}
      list={suggestions.length ? listId : undefined}
      placeholder={placeholder}
      min={field.min} max={field.max} step={field.step}
      onClick={(e) => {
        if (suggestions.length) {
          (e.currentTarget as HTMLInputElement & { showPicker?: () => void }).showPicker?.();
        }
      }}
      onChange={(e) => onChange(field.type === 'number' ? Number(e.target.value) : e.target.value)}
      onBlur={onBlur}
      onKeyDown={onKeyDown}
      className="node-header-input"
    />
    {suggestions.length > 0 && listId && (
      <datalist id={listId}>
        {suggestions.map((suggestion) => <option key={suggestion} value={suggestion} />)}
      </datalist>
    )}
  </>;
}

/** Update a single node's data in the Zustand store. */
function $updateNode(nodeId: string, patch: Record<string, unknown>) {
  useWorkflowStore.setState((state) => ({
    nodes: state.nodes.map((n) =>
      n.id === nodeId ? { ...n, data: { ...n.data, ...patch } } : n,
    ) as any,
    isDirty: true,
  }));
}
