import { useMemo, useState, useSyncExternalStore } from 'react';
import { nodeLabel, useLang, useT } from '../../i18n';
import { IconifyIcon } from './IconifyIcon';
import {
  getAllNodeTypes, getNodeTypesByCategory,
  getNodeTypesRevision, subscribeNodeTypes,
} from './nodes/registry';
import type { NodeCategory, NodeTypeDescriptor } from '../../types/workflow';

// 旧单目卡只保留给历史工作流渲染/执行,不再出现在新工作流调色板。
const LEGACY_INPUT_TYPES = ['rgb_camera', 'fisheye_camera'];
// 新工作流始终从这组稳定的采集端分类开始。真实设备名称由工作流卡片
// 内部的 source_key 控件从项目上传/心跳数据中选择,不作为调色板卡片。
const FIXED_INPUT_TYPES = [
  'gripper_device', 'glove_sensor', 'mono_camera', 'rgbd_camera', 'stereo_camera', 'stereo_rgbd_camera',
];
// MediaPipe Hand is retained as a backend/old-workflow compatibility node;
// the current hand modules are exposed through the canonical process cards.
const LEGACY_PROCESS_TYPES = ['mediapipe_hand'];
// Human Review 只做标注，不产生任何行为 —— 从调色板移除。
//
// 它的 run() 是纯透传，工作流**不会**在它那里停下等人：真正的审核门在状态层，
// 而且是无条件的（worker 完成回调把每个 run 置 to_review，导出接口只接受
// reviewed/approved）。所以它画在图上的位置是**误导性信息** —— 看图的人会以为
// "数据在这儿被人审过才往下走"，实际那个位置没有关口。
//
// ★ 只是**不显示**，不是删：模块保留注册（worker/runner.py 用 get_module 解析
//   nodeType，删了会让 8 个已保存工作流直接跑失败），老图也照常渲染与执行
//   （label/icon 存在图的节点数据里，不依赖注册表）。与 mediapipe_hand 同款处理。
const LEGACY_REVIEW_TYPES = ['human_review'];

// 顺序 = 调色板里从上到下的分组顺序。
// Review 排在 Process 前面（用户要求）：审核类节点是流程里的"关口"，
// 摆在一起更好找，而 Process 卡片最多、占了面板大半屏。
const CATEGORIES: { key: NodeCategory; labelKey: string; icon: string }[] = [
  { key: 'input', labelKey: 'palette.input', icon: 'ant-design:video-camera-outlined' },
  { key: 'review', labelKey: 'palette.review', icon: 'ant-design:eye-outlined' },
  { key: 'process', labelKey: 'palette.process', icon: 'ant-design:desktop-outlined' },
  { key: 'export', labelKey: 'palette.export', icon: 'ant-design:download-outlined' },
];

export function NodePalette() {
  const [search, setSearch] = useState('');
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  // 悬停说明卡(fixed 定位,避免被滚动容器裁剪):null = 未悬停
  const [tip, setTip] = useState<{ text: string; x: number; y: number } | null>(null);

  const onDragStart = (e: React.DragEvent, nd: NodeTypeDescriptor) => {
    e.dataTransfer.setData('application/reactflow-type', nd.type);
    e.dataTransfer.effectAllowed = 'move';
  };

  const showTip = (text: string, e: React.MouseEvent) => {
    setTip({ text, x: Math.min(e.clientX + 14, window.innerWidth - 256), y: Math.min(e.clientY + 12, window.innerHeight - 100) });
  };
  const hideTip = () => setTip(null);

  // 订阅注册表版本号 —— 否则 App 异步 hydrate 完（补上 UMI Slam Action 这类
  // 不在 BUILTIN 里的节点）不会重渲染，要等别的状态变化才"顺带"刷新。
  const revision = useSyncExternalStore(subscribeNodeTypes, getNodeTypesRevision);
  const t = useT();
  const lang = useLang();

  // 搜索同时匹配英文原名与中文译名 —— 切到中文后输入"手套"要能找到 Glove Sensor
  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return null;
    return getAllNodeTypes().filter((nd) =>
      nd.label.toLowerCase().includes(q)
      || nodeLabel(nd.type, nd.label, lang).toLowerCase().includes(q));
  }, [search, revision, lang]);

  const shouldShow = (nd: NodeTypeDescriptor): boolean => {
    if (LEGACY_INPUT_TYPES.includes(nd.type)) return false;
    if (LEGACY_PROCESS_TYPES.includes(nd.type)) return false;
    if (LEGACY_REVIEW_TYPES.includes(nd.type)) return false;
    if (nd.category === 'input') return FIXED_INPUT_TYPES.includes(nd.type);
    return true;
  };

  const renderNode = (nd: NodeTypeDescriptor) => {
    const cls = 'flex items-center gap-2 px-2 py-1.5 mx-1 mb-0.5 rounded cursor-grab hover:bg-gray-800 active:cursor-grabbing border border-transparent hover:border-gray-700 transition-colors';
    return (
      <div key={nd.type}
        onMouseEnter={(e) => nd.description && showTip(nd.description, e)}
        onMouseMove={(e) => nd.description && showTip(nd.description, e)}
        onMouseLeave={hideTip}>
        <div draggable onDragStart={(e) => onDragStart(e, nd)} className={cls}>
          <span className="w-2.5 h-2.5 rounded-full shrink-0" style={{ backgroundColor: nd.color }} />
          <span className="text-xs text-gray-300 truncate">{nodeLabel(nd.type, nd.label, lang)}</span>
        </div>
      </div>
    );
  };

  return (
    <div className="w-[220px] bg-gray-900 border-r border-gray-800 flex flex-col shrink-0 overflow-hidden relative">
      <div className="p-2">
        <input type="text" placeholder={t('palette.search')} value={search} onChange={(e) => setSearch(e.target.value)}
          className="w-full bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-xs text-gray-200 placeholder-gray-500 focus:outline-none focus:border-blue-500" />
      </div>
      <div className="flex-1 overflow-y-auto px-1 pb-2">
        {filtered
          ? filtered.filter(shouldShow).map(renderNode)
          : CATEGORIES.map((cat) => {
              const nodes = getNodeTypesByCategory(cat.key).filter(shouldShow).sort((a, b) => {
                if (cat.key !== 'input') return 0;
                return FIXED_INPUT_TYPES.indexOf(a.type) - FIXED_INPUT_TYPES.indexOf(b.type);
              });
              const open = !collapsed[cat.key];
              return (
                <div key={cat.key}>
                  <button onClick={() => setCollapsed((p) => ({ ...p, [cat.key]: !p[cat.key] }))}
                    className="w-full flex items-center gap-1.5 px-2 py-1.5 text-[11px] font-semibold text-gray-500 uppercase tracking-wider hover:text-gray-300">
                    <IconifyIcon icon={cat.icon} className="text-[14px]" />
                    <span>{t(cat.labelKey)}</span>
                    <IconifyIcon icon={open ? 'ant-design:caret-down-filled' : 'ant-design:caret-right-outlined'} className="ml-auto text-[10px]" />
                  </button>
                  {open && nodes.map(renderNode)}
                </div>
              );
            })}
      </div>
      <div className="px-2 py-2 border-t border-gray-800 text-[10px] text-gray-600 text-center">{t('palette.hint')}</div>
      {tip && (
        <div className="pointer-events-none fixed z-[200] w-60 rounded border border-gray-700 bg-gray-950/95 p-2 shadow-xl"
          style={{ left: tip.x, top: tip.y }}>
          <p className="text-[11px] leading-snug text-gray-300">{tip.text}</p>
        </div>
      )}
    </div>
  );
}
