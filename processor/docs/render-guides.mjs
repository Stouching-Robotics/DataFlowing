import { execFileSync } from 'node:child_process';
import { existsSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const docsDir = dirname(fileURLToPath(import.meta.url));
const imagesDir = join(docsDir, 'images');
const screenshotDir = process.env.DOC_SCREENSHOT_DIR
  || join(docsDir, '..', '..', '文件测试', '图片');
const imageWidth = 2495;
const imageHeight = 1332;

// Each mark is [number, circleX, circleY, targetX, targetY]. The arrowhead is
// always placed on the target, so every label maps to exactly one UI control
// or one clearly bounded display area.
const guides = [
  {
    input: '企业微信截图_17887710623711.png', source: '02-home.png', output: '02-home-annotated.png',
    labels: ['左侧页面菜单', '数据统计', '项目列表', '最近数据', '刷新页面'],
    marks: [
      [1, 95, 570, 95, 430], [2, 850, 285, 850, 190], [3, 620, 920, 620, 800],
      [4, 1760, 920, 1760, 800], [5, 2360, 130, 2420, 43],
    ],
  },
  {
    input: '企业微信截图_17887710942911.png', source: 'projects-list.png', output: 'projects-list-annotated.png',
    labels: ['搜索项目', '新建项目', '项目名称、状态和统计', '编辑项目', '删除项目', '展开接收批次'],
    marks: [
      [1, 2070, 82, 2180, 46], [2, 2460, 92, 2418, 46], [3, 1120, 54, 650, 122],
      [4, 2180, 165, 2288, 124], [5, 2280, 260, 2378, 214], [6, 2375, 350, 2446, 302],
    ],
  },
  {
    input: '企业微信截图_17887711082760.png', source: 'project-new.png', output: 'project-new-annotated.png',
    labels: ['项目名称', '绑定一个工作流', '项目状态', '计划接收数量（0 不限制）', '项目说明（可不填）', '保存项目'],
    marks: [
      [1, 235, 176, 330, 176], [2, 2470, 176, 2375, 176], [3, 235, 241, 330, 241],
      [4, 2470, 241, 2375, 241], [5, 235, 316, 330, 316], [6, 2470, 390, 2415, 377],
    ],
  },
  {
    input: '企业微信截图_17887711739904.png', source: 'project-workflow-select.png', output: 'project-workflow-select-annotated.png',
    labels: ['打开工作流列表', '新建空白工作流', '模板工作流', '绑定已有工作流', '编辑工作流'],
    marks: [
      [1, 2470, 145, 2432, 174], [2, 2470, 205, 1395, 210], [3, 2470, 255, 1515, 240],
      [4, 2470, 305, 1395, 296], [5, 2470, 365, 2422, 240],
    ],
  },
  {
    input: '企业微信截图_17887712673822.png', source: 'workflow-empty.png', output: 'workflow-empty-annotated.png',
    labels: ['选择已有或新建工作流', '修改工作流名称', '连接节点的画布', '模块列表（拖入画布）', '套用处理模板', '保存工作流'],
    marks: [
      [1, 430, 72, 350, 20], [2, 585, 72, 470, 20], [3, 1030, 700, 1030, 560],
      [4, 2100, 475, 2295, 285], [5, 2310, 72, 2374, 20], [6, 2435, 72, 2458, 20],
    ],
  },
  {
    input: '企业微信截图_17887712837205.png', source: 'workflow-template.png', output: 'workflow-template-annotated.png',
    labels: ['RGB-D 处理链模板', '双目 RGB 处理链模板', '取消并关闭'],
    marks: [
      [1, 1000, 650, 1210, 650], [2, 1000, 720, 1210, 720], [3, 1480, 840, 1390, 805],
    ],
  },
  {
    input: '企业微信截图_17887785935712.png', source: 'workflow-editor.png', output: 'workflow-editor-annotated.png',
    labels: ['双目 RGB 输入', '手套传感器输入', 'AI 自动标注', '人工审核', 'LeRobot 导出', '保存工作流'],
    marks: [
      [1, 500, 460, 500, 535], [2, 500, 920, 500, 760], [3, 1050, 920, 1050, 710],
      [4, 1500, 920, 1500, 710], [5, 2000, 850, 2000, 650], [6, 2415, 75, 2418, 20],
    ],
  },
  {
    input: '企业微信截图_17887784357745.png', source: 'workflow-ai-settings.png', output: 'workflow-ai-settings-annotated.png',
    labels: ['标注语言', '已保存的 API 方案', 'API 厂商', 'API 模型', 'API 密钥', 'API 地址', '测试连接', '保存设置'],
    marks: [
      [1, 800, 601, 915, 601], [2, 800, 680, 915, 680], [3, 800, 765, 915, 765],
      [4, 800, 850, 915, 850], [5, 800, 937, 915, 937], [6, 800, 1023, 915, 1023],
      [7, 930, 1130, 1095, 1100], [8, 1285, 1130, 1200, 1100],
    ],
  },
  {
    input: '企业微信截图_17887713752576.png', source: 'annotation-list.png', output: 'annotation-list-annotated.png',
    labels: ['进入标注页面', '展开项目', '选择 Episode'],
    marks: [[1, 205, 330, 72, 280], [2, 2110, 62, 2210, 52], [3, 2110, 135, 2205, 117]],
  },
  {
    input: '企业微信截图_17887720915441.png', source: 'annotation-editor.png', output: 'annotation-editor-annotated.png',
    labels: ['预览显示选项', 'RGB 与 2D 关键点', '3D 手部空间', '深度伪彩色预览', '运行 AI 标注', '选择一个标注片段', '设置开始帧和结束帧', '保存标注修改'],
    marks: [
      [1, 380, 105, 260, 65], [2, 560, 760, 560, 500], [3, 1500, 760, 1500, 545],
      [4, 1100, 1160, 1100, 1010], [5, 2110, 245, 2315, 245], [6, 2110, 395, 2315, 335],
      [7, 2110, 1120, 2315, 1170], [8, 2110, 1270, 2320, 1295],
    ],
  },
  {
    input: '企业微信截图_17887721273258.png', source: 'reviewing-list.png', output: 'reviewing-list-annotated.png',
    labels: ['待审核列表', '展开项目', '选择 Episode 并查看信息', '通过当前 Episode', '重新运行工作流'],
    marks: [
      [1, 205, 380, 75, 365], [2, 2110, 60, 2210, 52], [3, 2110, 135, 2215, 118],
      [4, 2110, 205, 2315, 203], [5, 2110, 245, 2315, 235],
    ],
  },
  {
    source: 'reviewing-player.png', output: 'reviewing-player-annotated.png',
    labels: ['预览显示选项', 'RGB 原图（可叠加关键点）', '3D 手部空间', '深度伪彩色预览', '只读标注片段', '播放和逐帧控制', '通过当前 Episode'],
    marks: [
      [1, 380, 105, 260, 65], [2, 560, 760, 560, 500], [3, 1500, 760, 1500, 545],
      [4, 1100, 1160, 1100, 1010], [5, 2110, 505, 2315, 505],
      [6, 1910, 1180, 1910, 1220], [7, 2110, 205, 2315, 203],
    ],
  },
  {
    input: '企业微信截图_17887721723042.png', source: 'approved-list.png', output: 'approved-list-annotated.png',
    labels: ['已通过列表', '当前处于多选模式', '全选当前列表', '单选或多选 Episode', '导出这一条 Episode', '取消审核'],
    marks: [
      [1, 205, 430, 75, 405], [2, 2110, 34, 2315, 18], [3, 2110, 75, 2190, 50],
      [4, 2110, 125, 2188, 120], [5, 2110, 205, 2280, 235], [6, 2470, 275, 2405, 235],
    ],
  },
  {
    input: '企业微信截图_17887722547409.png', source: 'trash.png', output: 'trash-annotated.png',
    labels: ['垃圾桶入口', '剩余保留时间', '恢复数据', '永久删除', '清空垃圾桶'],
    marks: [
      [1, 205, 1225, 75, 1225], [2, 1160, 180, 1220, 119], [3, 1430, 180, 1395, 119],
      [4, 1535, 180, 1490, 119], [5, 1700, 72, 1660, 42],
    ],
  },
];

function renderSvg(guide) {
  const arrows = guide.marks.map(([number, cx, cy, tx, ty]) => `
    <line class="arrow" x1="${cx}" y1="${cy}" x2="${tx}" y2="${ty}"/>
    <circle class="mark" cx="${cx}" cy="${cy}" r="27"/>
    <text class="num" x="${cx}" y="${cy}">${number}</text>`).join('');

  return `<svg xmlns="http://www.w3.org/2000/svg" width="${imageWidth}" height="${imageHeight}" viewBox="0 0 ${imageWidth} ${imageHeight}">
  <defs>
    <marker id="arrow" markerUnits="userSpaceOnUse" markerWidth="24" markerHeight="24" refX="22" refY="12" orient="auto">
      <path d="M0,0 L24,12 L0,24 Z" fill="#ff5a4f"/>
    </marker>
    <style>
      .arrow{stroke:#ff5a4f;stroke-width:6;fill:none;marker-end:url(#arrow)}
      .mark{fill:#2f80ed;stroke:#fff;stroke-width:3}
      .num{font:700 29px 'Noto Sans CJK SC',sans-serif;fill:#fff;text-anchor:middle;dominant-baseline:central}
    </style>
  </defs>
  ${arrows}
</svg>`;
}

for (const guide of guides) {
  const source = join(imagesDir, guide.source);
  if (guide.input) {
    const original = join(screenshotDir, guide.input);
    if (!existsSync(original)) throw new Error(`Missing source screenshot: ${original}`);
    execFileSync('ffmpeg', [
      '-hide_banner', '-loglevel', 'error', '-y', '-i', original,
      '-frames:v', '1', source,
    ]);
  }
  if (!existsSync(source)) throw new Error(`Missing local source screenshot: ${source}`);

  const overlay = join(imagesDir, guide.output.replace('-annotated.png', '-guide.svg'));
  const output = join(imagesDir, guide.output);
  writeFileSync(overlay, renderSvg(guide));
  execFileSync('ffmpeg', [
    '-hide_banner', '-loglevel', 'error', '-y',
    '-i', source, '-i', overlay,
    '-filter_complex', '[0:v][1:v]overlay=0:0:format=auto',
    '-frames:v', '1', output,
  ]);
}

console.log(`Rendered ${guides.length} documentation images from ${screenshotDir}.`);
