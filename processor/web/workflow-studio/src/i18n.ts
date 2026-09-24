/** Workflow Studio 的中英切换。
 *
 * 这是**独立的 React 应用**，不加载 `web/static/js/i18n.js`（那套是给 Jinja 模板
 * 用的）。两边靠两个约定打通：
 *
 *   1. 同一个 localStorage 键 `lang` —— 模板侧 `setLang()` 写、这里读，刷新后一致
 *   2. 自定义事件 `egodata:lang` —— 模板侧切换时派发，这里监听
 *
 * ★ 为什么不用 `storage` 事件：那个**只在别的标签页**触发，同一个标签页里
 *   localStorage 变了它不会响。而语言切换按钮就在本页的侧边栏上，正好是同标签页。
 *
 * 缺词时回落 key 本身（如 `palette.search`）而不是静默显示英文 —— 漏翻译一眼看得见。
 */
import { useEffect, useMemo, useState } from 'react';

export type Lang = 'en' | 'zh';

const LANG_KEY = 'lang';
const LANG_EVENT = 'egodata:lang';

/** 当前语言。默认英文（与模板侧一致）。隐私模式禁 storage 时回落默认。 */
export function currentLang(): Lang {
  try {
    return localStorage.getItem(LANG_KEY) === 'zh' ? 'zh' : 'en';
  } catch {
    return 'en';
  }
}

const DICT: Record<string, { en: string; zh: string }> = {
  // ── 调色板 ──
  'palette.input': { en: 'Input', zh: '输入' },
  'palette.review': { en: 'Review', zh: '审核' },
  'palette.process': { en: 'Process', zh: '处理' },
  'palette.export': { en: 'Export', zh: '导出' },
  'palette.search': { en: 'Search nodes...', zh: '搜索节点...' },
  'palette.hint': { en: 'Drag nodes onto canvas', zh: '拖拽节点到画布' },

  // ── 工具栏 ──
  'toolbar.new': { en: 'New', zh: '新建' },
  'toolbar.save': { en: 'Save', zh: '保存' },
  'toolbar.saveAs': { en: 'Save As', zh: '另存为' },
  'toolbar.export': { en: 'Export', zh: '导出' },
  'toolbar.workflowList': { en: 'Workflow list', zh: '工作流列表' },
  'toolbar.run': { en: 'Run', zh: '运行' },

  // ── 通用 ──
  'common.cancel': { en: 'Cancel', zh: '取消' },
  'common.save': { en: 'Save', zh: '保存' },
  'common.loading': { en: 'Loading...', zh: '加载中...' },

  // ── AI 标注设置弹窗 ──
  'ai.vendor': { en: 'API vendor', zh: 'API 供应商' },
  'ai.model': { en: 'API model', zh: 'API 模型' },
  'ai.key': { en: 'API key (stored with this workflow)', zh: 'API Key（随本工作流保存）' },
  'ai.labelLanguage': { en: 'Label language', zh: '标注语言' },

  // ── 抽屉 / 连线 / 节点 ──
  'drawer.workflows': { en: 'Workflows', zh: '工作流' },
  'drawer.noWorkflows': { en: 'No workflows yet', zh: '暂无工作流' },
  'edge.delete': { en: 'Delete connection', zh: '删除连线' },
  'node.projectOverride': { en: 'Project override', zh: '项目级覆盖' },
  'node.saveForProject': { en: 'Save for this project', zh: '仅保存到本项目' },
  'node.clearOverride': { en: 'Clear project override', zh: '清除项目级覆盖' },
  'node.clear': { en: 'Clear', zh: '清除' },
  'node.clearOverrideHint': { en: 'Project override — click to clear', zh: '项目级覆盖 —— 点击清除' },
  'node.aiSettings': { en: 'AI annotation settings', zh: 'AI 标注设置' },
  'node.aiSettingsShort': { en: 'Data quality settings', zh: '数据质检设置' },

  // ── AI 设置弹窗的测试结果 ──
  'ai.modelRequired': { en: 'Model required', zh: '请先填写模型' },
  'ai.keyRequired': { en: 'API key required', zh: '请先填写 API Key' },
  'ai.testFailed': { en: 'Test failed', zh: '测试失败' },
  'ai.requestFailed': { en: 'Request failed', zh: '请求失败' },
  'ai.networkError': { en: 'network error', zh: '网络错误' },
  'ai.appliesNextRun': { en: 'Applies to the next AI annotation run.', zh: '对下一次 AI 标注生效。' },
  'common.close': { en: 'Close', zh: '关闭' },

  // ── 画布提示 ──
  'toast.dupEdge': { en: 'Already connected: this port pair is already linked.',
                     zh: '已连接：这条端口对已经连过了。' },
  'toast.selfEdge': { en: 'Self-connection is not allowed', zh: '不能连接到自身' },
  'toast.cannotConnect': { en: 'Cannot connect: ', zh: '无法连接：' },
  'toast.portMismatch': {
    en: 'Port mismatch: this input expects different data.',
    zh: '端口类型不匹配：这个输入要的数据类型与上游输出不同。' },
  'toast.stereoPair': { en: 'Stereo pair connected', zh: '双目已配对连接' },

  // ── 数据清洗设置弹窗 ──
  'quality.title': { en: 'Data Cleaning · Settings', zh: '数据清洗 · 设置' },
  'quality.loadFailed': { en: 'Failed to load the check catalog: ', zh: '加载检查项目录失败：' },
  'quality.noDevice': { en: 'This node has no device connected yet', zh: '该节点还没有连接任何设备' },
  'quality.noChecks': { en: 'has no checks available yet', zh: '暂时没有可用的检查项' },

  // ── 顶栏 ──
  'app.template': { en: 'Template', zh: '模板' },
  'app.makeTemplate': { en: 'Make Template', zh: '设为模板' },
  'app.save': { en: 'Save', zh: '保存' },
  'app.del': { en: 'Del', zh: '删除' },
  'app.cancel': { en: 'Cancel', zh: '取消' },
  'app.newWorkflow': { en: 'New Workflow', zh: '新建工作流' },
  'app.startFromTemplate': { en: 'Start from Template', zh: '从模板开始' },
  'app.unsetTemplate': { en: 'Unset Template', zh: '取消模板' },
  'app.noTemplates': { en: 'No templates available — an admin can mark workflows as templates.',
                      zh: '暂无模板 —— 管理员可将工作流标记为模板。' },
  'app.templateHint': { en: 'Applying a template replaces the current canvas — your workflow name and ID stay the same.',
                       zh: '应用模板会替换当前画布 —— 工作流名称与 ID 保持不变。' },
};

export function translate(key: string, lang: Lang): string {
  const entry = DICT[key];
  if (!entry) return key;
  return entry[lang] || entry.en;
}

// ── 节点卡片名 ──────────────────────────────────────────────
//
// 卡片名是**后端声明的**（module_catalog 的 label），前端只是显示。这里给中文
// 对照，key = 模块 slug。查不到就回落到英文原文 —— 后端新增模块时不会显示成
// 空白的 key。
const NODE_LABELS_ZH: Record<string, string> = {
  mono_camera: 'RGB 相机',
  rgb_camera: 'RGB 相机',
  fisheye_camera: 'RGB 相机',
  rgbd_camera: 'RGB-D 相机',
  stereo_camera: '双目 RGB 相机',
  stereo_rgbd_camera: '双目 RGB-D 相机',
  glove_sensor: '触觉手套',
  gripper_device: 'UMI 夹爪',
  ai_annotation: 'AI 标注',
  annotation: '人工标注',
  mediapipe_hand: 'MediaPipe 手部',
  rgb_to_2d_bare_hand: 'RGB_2D_裸手',
  rgb_to_2d_black_glove: 'RGB_2D_黑手套',
  rgbd_to_3d_bare_hand: 'RGB-D_3D_裸手',
  rgbd_to_3d_black_glove: 'RGB-D_3D_黑手套',
  umi_slam_action: 'UMI SLAM 动作',
  data_quality: '数据清洗',
  human_review: '人工审核',
  lerobot_export: 'LeRobot 导出',
  hdf5_export: 'HDF5 导出',
};

/** 相机卡的分类名走的是另一条路（cameraCategoryLabel 直接返回展示串），
 *  按英文原文再兜一层。 */
const DISPLAY_LABELS_ZH: Record<string, string> = {
  'RGB Camera': 'RGB 相机',
  'Stereo RGB Camera': '双目 RGB 相机',
  'RGB-D Camera': 'RGB-D 相机',
  'Stereo RGB-D Camera': '双目 RGB-D 相机',
  'Glove Sensor': '触觉手套',
  'UMI Gripper': 'UMI 夹爪',
};

export function nodeLabel(type: string, fallback: string, lang: Lang): string {
  if (lang !== 'zh') return fallback;
  return NODE_LABELS_ZH[type] || DISPLAY_LABELS_ZH[fallback] || fallback;
}

// ── 端口名 ──────────────────────────────────────────────────
//
// key 用 ``*:端口key`` 表示"所有节点都这么叫"，``节点:端口key`` 用于同一个
// 端口在不同卡上叫法不同的情况 —— 最典型的是 ``data``：导出卡上是"可导出数据"，
// 质检卡上就是"数据"，审核卡上是"待审核数据"。只按端口 key 查会串味。
const PORT_LABELS_ZH: Record<string, string> = {
  '*:video': 'RGB 视频',
  '*:depth': '深度',
  '*:video_left': '左目 RGB 视频',
  '*:video_right': '右目 RGB 视频',
  '*:rgb_video': 'RGB 视频',
  '*:gripper_state': 'ESP 夹爪状态',
  '*:slam_trajectory': 'SLAM 轨迹',
  '*:tactile_force_matrices': '左右力矩阵',
  '*:sensor_data': '手套传感器数据',
  '*:annotation': '标注',
  '*:hand_keypoints': '手部 2D',
  '*:hand_3d': '手部 3D',
  '*:action': '动作',
  '*:reviewed': '已审核数据',
  '*:dataset': '数据集',
  'data_quality:data': '数据',
  'human_review:data': '待审核数据',
  'lerobot_export:data': '可导出数据',
  'hdf5_export:data': '可导出数据',
  'ai_annotation:data': 'RGB 视频',
  'annotation:data': 'RGB 视频',
};

export function portLabel(
  nodeType: string, key: string, fallback: string, lang: Lang,
): string {
  if (lang !== 'zh') return fallback;
  return PORT_LABELS_ZH[`${nodeType}:${key}`] || PORT_LABELS_ZH[`*:${key}`] || fallback;
}

/**
 * 订阅当前语言，返回一个取词函数。
 *
 * 语言变化时**所有用到它的组件都会重渲染** —— 这正是需要的：调色板、工具栏、
 * 节点卡片都要跟着换文字。
 */
export function useLang(): Lang {
  const [lang, setLang] = useState<Lang>(currentLang);

  useEffect(() => {
    const onChange = () => setLang(currentLang());
    window.addEventListener(LANG_EVENT, onChange);
    // 兜底：万一别处（如 devtools）直接改了 localStorage 而没派发事件
    window.addEventListener('storage', onChange);
    return () => {
      window.removeEventListener(LANG_EVENT, onChange);
      window.removeEventListener('storage', onChange);
    };
  }, []);

  return lang;
}

/** 取词函数。需要节点名/端口名的组件再单独调 useLang()。 */
export function useT(): (key: string) => string {
  const lang = useLang();
  return useMemo(() => (key: string) => translate(key, lang), [lang]);
}
