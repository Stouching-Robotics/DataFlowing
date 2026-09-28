"""后端版本号 —— 发版时只改这一个文件.

约定:
  - 主版本.次版本.修订号 (semver)
  - 修复 bug / 小调整        → 修订号 +1 (0.1.0 → 0.1.1)
  - 新增功能 / 接口变化      → 次版本 +1 (0.1.x → 0.2.0)
  - 不兼容的大改动           → 主版本 +1 (x.0.0)

**这个号只管后端。** 它**不再是**静态资源的缓存键 —— 从 1.19.4 起,模板里的
``?v={{ asset_version }}`` 取自 **web/VERSION**(前端号,见 app/web_version.py)。
改前端不用再动这里;改后端也不用(前端资源没变,不需要失效缓存)。

前端版本独立管理, 见 **web/VERSION** —— 它覆盖 processor/web/ 下的全部前端代码
(templates/ + static/ + workflow-studio/),不只是那个 React SPA。
`web/workflow-studio/package.json` 的 version 只代表 SPA 自己,两者不必同步。
"""
# 1.8.0 审核页选择功能重构(项目级三态全选、批量栏置顶 + 快慢双路径、
#       15s 轮询不再整表重绘)+ 导出性能(力矩阵分位数向量化 15×)
# 1.8.1 修正「打包下载」的说明文案:单个/多个产出的是同一个 LeRobot 结构
#       (data/ + meta/ + videos/),不是「每集一个目录」
# 1.8.2 导出补回力矩阵的语义元数据(encoding/scale/units/names)。此前导出
#       产物只声明 dtype+shape,把「放大 100 倍后取行内差分」的整数当裸值
#       发出去 —— 数据没丢,但没人能正确还原(不知道要 cumsum、要 ÷100)
# 1.8.3 导出不再把刚写完的 data/ 从磁盘重读一遍算 stats:调用方手里的
#       arrow table 直接转 pandas(与 read_parquet 逐列同 dtype 同值,已整个
#       产物目录逐文件回归比对)。_write_stats 内部实测省 0.56s/1 集、
#       2.79s/5 集,但只占整次导出(约 23.5s)的 2-3%,会被 NAS 读源数据的
#       ±3s 波动淹没 —— 不要拿"整次导出省 N 秒"来宣传这个改动。顺带把
#       _write_stats 提到 _embed_info_metadata 之前,避免峰值内存多出一整
#       个数据集的量(50 集约 650MB)
# 1.8.4 移除视频卡片右上角的 ×:卡片比画面宽时(D435_depth 两侧留黑边),
#       它挂在卡片角而非画面角,离内容很远像飘在外面。增删画面的入口仍在
#       源列表复选框(renderPreviewVideoSources)+ All/None,功能未堵死
# 1.8.5 顺带删掉 renderGroupedWorkspaceLegacy(187 行死代码,全仓库零调用,
#       里面还留着一份同样的 × 按钮)。它不渲染,删它纯粹是防止将来有人
#       复活它把 × 带回来 —— 用户可见行为与 1.8.4 完全相同
# 1.8.6 部署加固:systemd 单元入库(scripts/systemd/host-units/,含安装脚本与
#       说明)。修「换网络/休眠后整套服务静默挂掉」:挂载加僵尸态自愈
#       (启动前 fusermount3 -u -z)、API/worker 改 Requires+After 挂载、
#       Restart 一律 always;另加每分钟看门狗自动重挂/重启。纯运维配置,
#       无代码行为与用户可见变化
# 1.8.7 图标离线化收尾。iconify-icon 对**不在** iconify-preload.js 白名单里的
#       图标会去 api.iconify.design 拉,国内网络下表现为页面/面板卡住几秒。
#       替换三处漏网图标:侧边栏 Users(team-outlined → user-outlined,
#       每个页面都渲染)、AI 标注遮罩(loading-3-quarters-outlined →
#       loading-outlined)、以及新增的工作流清洗节点。同时把
#       scripts/check_icon_preload.sh 的扫描范围扩到 app/processing/modules/
#       —— 节点图标是在 Python 里声明的,此前漏扫,改了白名单外的图标
#       检查脚本仍报「全部已预加载」
# 1.9.0 数据质检子系统上线(后端 + 设置面板)。
#       卡片合并:AI Quality Review + Data Cleaning → Data Quality(旧 slug 自动迁移,
#       端口 key 不变因此连线不受影响)。9 个检查项(视频 4 + UMI 5)按设备卡片
#       声明归属,设置面板按连线出的设备分 tab 独立配置检查项与阈值。
#       新 API:/api/v1/quality/{checks,summary,episodes/*}。报告只写
#       episode state 的 cleaning_report,**不碰 status 流转**(只记录不拦截)。
#       阈值只有一处定义(检查项的 default_params),节点只存覆盖值
# 1.9.1 质检**真正跑起来**。1.9.0 只把节点和面板立了起来,``run()`` 还是透传,
#       跑工作流并不会执行任何检查。接法:worker 的 run 完成回调调
#       ``cleaning.trigger.spawn_cleaning_for_run``(与 video_quality 门禁同一
#       挂载点,放后台任务、走线程池)——**不在 ``run()`` 里跑**,因为 DAG 执行期
#       worker 在 staging 目录干活,那时读到的是上一轮数据(action 列会是采集端
#       的零占位)。需要 ``episode_index``:``get_episode()['path']`` 是**项目
#       目录**、多集共享,漏传会拿第 0 集的 parquet 比全部集的视频。
#
#       同时修掉三处「检查项静默不跑」——都不报错、报告照常生成:
#       (1) 配置白名单 bug:前端只在用户**碰过**的项上写 entry,引擎却把"显式
#           启用的那些"当白名单,于是**改一个阈值 = 其余检查项全部停跑**
#           (实测 9 项变 1 项)。配置只该是黑名单。
#       (2) 模态推断推不出 stereo_rgb/stereo_rgbd_camera("立体"不是列名能看见
#           的特征),双目工作流在设置面板配的阈值查不到 tab、静默用默认值。
#           改为「工作流连线的 hint ∪ 数据推断」。
#       (3) 状态覆盖竞态:``read/write_episode_state`` 各自加锁,RMW 跨两步不是
#           原子的;质检与 video_quality 门禁在同一个 run 后并发写同一份 state,
#           后写的会用旧快照把先写的冲掉(``status`` 被冲 = 失败批次看着像已通过)。
#           新增 ``localstore.mutate_episode_state``(锁内 RMW),两条路都改用它。
#           对照实验:12 线程并发下手写 RMW 存活 1/12 且抛 10 次 FileNotFoundError
#           (``_write_json`` 的临时文件名是固定的、多人共用),锁内 RMW 存活 12/12。
# 1.9.2 数据不对 → **卡在人工审核**。质检判 FAIL/ERROR 的批次推回 to_review
#       (审核页的入口,复用既有状态机,不新建状态),WARN 只记录不拦。
#
#       光推状态挡不住:自动批准散在 ai_annotation._set_ai_quality_state 与
#       video_quality._apply_video_quality_result 两处,它们只看**自己的**报告
#       —— 质检判 FAIL、视频判 PASS 时坏数据照样被放行。而且三条门禁是并发跑
#       的(都在 run 完成回调里 create_task),谁先写没有保证。所以质检额外在
#       cleaning_summary.blocking 上留一个**粘性标记**,两条自动批准路径都先
#       查它;标记由下一次质检整份覆盖,数据修好后自动解封。
#
#       拦截只针对**自动**批准:人工批准不受影响(实测 verified)。
#       另外不碰 deleted/rejected —— 质检不能把用户删掉或否决过的批次复活。
#       顺带把 _set_ai_quality_state 也收进锁内 RMW(它此前是手写 read→write,
#       与修复前的视频门禁同一个竞态)。
# 1.9.3 修采集层三处静默失效(为接入手套检查清路)。都不报错、报告照常生成:
#       (1) **非首集读出空帧** —— canonical 是一集一文件,采集却取
#           ``parquets[0]`` 再按 ``episode_index`` 列筛行,于是第 0 个文件只装
#           第 0 集、其余集全筛成空。报告退化成 empty_report(episode.status=
#           ERROR),在 1.9.2 的"数据不对就拦"链路里表现为**除第 0 集外每一集都
#           被卡在人工审核**。改成按布局解析:canonical 选对文件且不再筛行,
#           导出产物(一文件多集)才筛行。全量验证 22 集/16 个不同 episode_index
#           零空报告(含编号有空洞的 000007)。
#       (2) **触觉阵列被截断** —— _CHANNEL_WIDTH 没有 tactile 项,落到 3 的兜底
#           值,16x16=256 个感应点被静默截成 3 个(检查项只看得到 1%)。改为
#           "不在表里的信道一律不截断",兜底成某个具体数字本身就是隐患。
#       (3) **observation.tactile.* 认不出** —— 触觉有两套命名:采集协议写
#           observation.*_glove,导出统一成 observation.tactile.*。只认前者,
#           于是只跑过导出的树上手套信道为空、手套检查整条静默跳过。加别名
#           归一化;两套并存时保留协议名(同一份数据,登记两条流会让缺陷报两次)。
#       顺带核对:同项目内 schema 不一致(实测 D435 第 3 集只有右手)采集层本就
#       按"有什么收什么"处理,无需改;但**写手套检查项时不能假设双手都在**。
# 1.9.4 手套检查项上线(5 项),设置面板的 Glove Sensor tab 不再是空的。
#       阈值不是拍的 —— 全部从真实数据反推,过程写在 checks/glove/ 各文件里:
#         (1) **16×16 空间快照,行主序展平**。证据分两条,强度不同:
#             - 行主序+每行 16 个: 自相关 |v[i]-v[i+k]| 对 k=1..40 在 **k=16**
#               处有尖锐极小值(S80C 左 43.6,而 k=15 是 86、k=17 是 87),
#               四份数据全部如此 → i 与 i+16 是空间邻居 ⇒ 每行 16 个、行主序。
#             - 16×16 这个分解: 试了 6 种(16x16、8x32、32x8、4x64、64x4),
#               16x16 的相邻格平均差最小(187 vs 227+)。**但行主序与列主序的
#               数值完全相同(互为转置,总和对称)—— 这个测试区分不了行列,
#               行列是上面那条自相关定的。**
#             快照 vs 滚动缓冲: 相邻帧做任何格数平移差异都**更大**(不平移 33.0
#             vs 平移 78+,随机帧对 57.6) → 是快照。接触区稳定是 11-13 高 x
#             3-7 宽(沿行索引长、沿列索引窄);把它读成"手指"是**推测**,没证实。
#         (2) **取值被固件门限:要么 0,要么 >= 500.02**,0 到 500 之间一个值都
#             没有(实测最小非零 500.0234)。所以 0 的含义是"未接触"而不是
#             "力为零"。glove.gate_floor 就是盯这个门限的 —— 固件改版/scale
#             应用两次时,列还在、还是 256 维、数还在合理区间,只有值域整体位移。
#         (3) **设备更新约 7.5Hz**(30fps 录制下同一个值连续保持恰好 4 帧) →
#             受压时静止 3-4 帧是**正常**。阈值取 warn 40 / fail 300 帧,留足
#             余量给将来更新率更低的设备。
#         (4) **不做逐点"死点"检查**。整集恒定的感应点占 30-86%,图案是规则
#             条纹/块状(左右手不同、跨项目也不同),那是**手套的传感分区**与
#             手指是否接触,不是坏点 —— 做死点会在每一集误报 136-161/256。
#             跨集基线也救不了:某根手指在全部集里都没用过同样分不出来。
#       顺带加 device_status 信道(字符串列)。**数值化会把字符串列变成全空
#       元组**(vector_series 转 float 抛异常被吞),检查项拿到的是"没有数据"
#       看起来像通过 —— 所以 _column_series 对字符串先分流。
#
#       设置面板的 tab 只显示**画布上连到本节点**的设备卡片(用户明确要求:
#       没连接的设备不显示)。注意与执行侧的口径差异: 检查跑不跑取决于批次数据
#       里有哪些信道(engine 按 evidence.channels 筛),连线只决定"卡片有没有
#       入边(质检跑不跑)"和"显示哪些 tab"。所以理论上存在"数据里有该类设备、
#       但画布上没连到质检卡 —— 检查照跑、阈值却在面板里配不到"的情况。
# 1.9.5 采集层认多相机 SLAM 命名。``observation.slam_*`` 是默认相机,双目/多把
#       第二路存成 ``observation.<相机名>_slam_*``,而采集写死了两个不带前缀的
#       列名**并且循环末尾 break** —— 于是(1)只有前缀命名的集**有 SLAM 数据却一个
#       SLAM 检查都不跑**,(2)双目的第二路从来没被检查过,两处都不报错。
#       改为按相机分组解析(每个相机各一条流,组内 trajectory 优先),列名用正则
#       匹配并排除两个近亲: ``observation.slam_trajectory_ns``(时间戳伴生列) 与
#       ``processing.umi_slam_action.data_4.action``(处理缓存,名字里恰好含 slam)。
#       实测 Test94_000003 立刻暴露真问题: 第二路相机 44.3%(601/1358) 的帧靠插值
#       兜底,之前完全不可见。
#       顺带把 slam_continuity 的注释与实现对齐(注释写"默认判 WARN 只报告",
#       实现一直是分档的: 单次跳变 WARN、>=10 处 FAIL)。**只改注释不改行为**。
# 1.9.6 工作流调色板"进入页面后要等一会才刷新"。节点注册表 registry 是**模块级
#       Map、不是 React state** —— 往里面塞节点不触发重渲染。而 App 挂载后才异步
#       拉 /api/v1/workflows/modules 并 hydrate 一次,且 App() 自身既无 useState 也
#       不订阅 store(它只渲染一次),NodePalette 也没被 memo —— 于是调色板停留在
#       BUILTIN 兜底清单上,要等**用户跟它交互**(打字搜索/点折叠/悬停出提示)才
#       顺带刷新。「UMI Slam Action」这类不在 BUILTIN 里的节点因此迟迟不出现。
#
#       接口本身不慢:实测一次页面加载里 /modules 与其他请求都在 1 秒内返回,
#       所以问题不在网络而在**不重渲染**。
#
#       修法:registry 加版本号+订阅,NodePalette 用 useSyncExternalStore 订阅;
#       hydrateNodeTypes 结束时通知一次(循环内逐条通知会让调色板重渲染 N 次)。
#       纯前端改动,刷新即生效,不用重启后端。
# 1.9.7 质检全链路文案英文化。工作台界面本就是英文的(Home / Projects / Reviewing /
#       Approved …),只有新加的质检是中文 —— 现在对齐:14 个检查项的 label/
#       description、30 条 Finding.message、SEVERITY_LABELS、report 里的训练用途
#       标签与 summary、前端弹窗与参数标签、以及质检的日志输出。
#       代码注释与 docstring **保持中文**(与全仓一致),只改面向用户的字符串。
#       测试里断言文案的 6 处一并同步。
# 1.9.8 画布端口与模块命名（前端为主，后端只有显示名）。
#       (1) **模块显示名 Data Quality → Data Cleaning**（用户要求：它就是数据清洗功能）。
#           slug 仍是 data_quality —— 那是持久化契约，改它会让已保存工作流的
#           nodeType 对不上，要再迁一次。只改显示名，不动 slug。
#       (2) **Data Quality 的输入端口按入边动态生成**：每接一个设备多一个口，口名
#           取上游那个端口的名字。此前所有线挤在同一个 data 口上，口名只能把几个
#           上游拼起来（"Glove Sensor Data, RGB Video"），越长越读不出来。
#           端口 key = `dynamicInputKey(源节点, 源端口)`，稳定可重复；老图的边在
#           normalizeWorkflowGraphForEditor 里迁移（全库实测只影响 1 个工作流 5 条边）。
#           往**任意已有的口**上拖也能新增连接 —— onConnect 会把 targetHandle 重写
#           成新上游的 key。**只对 data_quality 这一张卡生效**（DYNAMIC_INPUT_TYPES），
#           其余节点的端口文字一律保持声明文案。
#       (3) **卡片高度改为按各自端口数算**。此前是画布统一算的（取注册表里静态端口
#           的最大值）—— 对运行时才长出来的动态口无能为力，接第 5 个设备时被撑破。
#           顺带删掉那条全局计算与 getMaxPortRows（已被完全覆盖）。
#       (4) 修三处「拿规范化后的类型去比旧 slug」的**死条目**（永远匹配不上）：
#           canonicalLabelTypes / controlledPorts / _migrateWorkflowGraph 的
#           result→reviewed 改写。前两者导致老图一直显示旧标签 "AI Quality Review"
#           （不走 descriptor.label）；第三处本就该与后端 workflow_types.py 的
#           {"human_review","data_quality"} 对齐。另删 CANONICAL_PORTS 与 BUILTIN 里
#           两个同类的死条目（BUILTIN 那个会让卡片在水合完成前短暂显示旧名）。
#
#       走过又撤掉的弯路（记录在此免得再走）：曾把「输入 key 是 data 就显示上游
#       端口名」应用到**所有**泛化端口节点，以及把设备卡片的数据种类（后端从
#       MODALITY_CHANNELS 派生）展开成标签 —— 两者都被否掉了：前者波及导出/标注卡，
#       后者字长且与上游卡片重复。
# 1.10.0 用户菜单加「界面语言」切换（中/英）。i18n.js 早就写好了一整套
#       （225 个 key × 2 语言、t()/setLang()/localStorage），但**切换入口从来没接**
#       —— updateLangToggle() 找的是 #lang-toggle，所有模板里都没有这个元素，
#       功能等于死的。现在在用户下拉里加了 English / 中文 两个选项。
#
#       顺带修一个真 bug：setLang() 一直写 localStorage，但**没有任何地方读它** ——
#       currentLang 写死 'en'，每次刷新都退回英文，切换等于没切。DOMContentLoaded
#       现在先读回保存的语言（隐私模式禁 storage 时 try/catch 回落默认）。
#
#       ⚠️ 覆盖度仍然不全：9 个模板里只有 4 个有 data-i18n 标注（index 24 / tasks 13 /
#       base 7 / trash 3），exports、login、overview、users **一处都没有**（users 约
#       39 处可见文本）。工作流画布是独立 React 应用，完全不走这套。补齐是第二步。
# 1.10.1 侧边栏与用户菜单补齐中英切换（1.10.0 只接了入口，导航项没标注）。
#       补 6 处：Workflow / Users / Annotation 三个导航项，以及用户菜单里的
#       Profile / Change Password / Logout。侧边栏每个页面都渲染，感知最强。
#       EgoData（品牌名）与 English / 中文（语言自名）**刻意不翻** —— 语言选项
#       写自己的文字，切到中文后英文项不该变成"英语"，否则反而找不到。
#
#       ★ 菜单项的文字必须先包进 <span>：applyTranslations 用的是
#         ``el.textContent = t(...)``，直接在 <a> 上挂 data-i18n 会把同级的
#         iconify-icon 一起冲掉（图标会消失）。
#
#       登录页（login.html）**保持英文**，不进这套 —— 用户要求：登录后再切。
#       剩余未标注：users(45) / overview(14) / index(13) / exports(10) / tasks(2)
#       模板文案，以及 JS 动态生成的约 50-70 处；工作流画布是独立 React 应用，
#       完全不覆盖。
# 1.10.2 工作流画布接中英切换（第一段：语言通道 + 调色板 + 工具栏）。
#       画布是**独立 React 应用**，不加载 web/static/js/i18n.js。两边靠两个约定打通：
#         1. 同一个 localStorage 键 `lang` —— 模板侧 setLang 写、React 侧读
#         2. 自定义事件 `egodata:lang` —— 模板侧切换时派发，React 侧监听
#       ★ 不用 storage 事件：那个**只在别的标签页**触发；而切换按钮就在本页侧边栏。
#       新增 src/i18n.ts：useT() 订阅语言，语言一变所有用到它的组件重渲染。
#       本轮覆盖：调色板（Input/Review/Process/Export 四个分组 + 搜索框 + 底部提示）、
#       PipelineToolbar（New/Save/Save As/Export/Workflow list）、顶栏（Template/
#       设为模板/取消模板/Del/Cancel/新建工作流/从模板开始 + 模板对话框说明）。
#       缺词回落 key 本身（如 palette.search）而不是静默显示英文 —— 漏翻一眼看得见。
#
#       未覆盖（后续）：节点卡片名（后端 20 个模块 label）、节点端口名（registry.ts
#       约 38 处）、DeviceQualityModal 参数标签（26 处）、NodeSettingsModal（11 处）、
#       workflowStore 的 toast（10 处）。
# 1.10.3 工作流画布中文化（第二段：节点卡片名 + 端口名 + 搜索）。
#       新增两张对照表（都放在 src/i18n.ts，查不到回落到英文原文，不显示成空白）：
#         NODE_LABELS_ZH   20 个模块 slug → 中文卡名（后端 label 是英文，只做显示层对照）
#         PORT_LABELS_ZH   端口名，key 用 ``*:端口key`` 表通用、``节点:端口key`` 表特例
#       ★ 端口必须按「节点+端口」查而不是只按端口 key：同一个 ``data`` 在导出卡上
#         是"可导出数据"、质检卡上是"数据"、审核卡上是"待审核数据"，只按 key 会串味。
#       调色板搜索同时匹配英文原名与中文译名 —— 切到中文后输入"手套"要能找到
#       Glove Sensor。已实测覆盖：端口 42/42、模块 20/20，无遗漏。
#       （上一轮 1.10.2 只做了语言通道 + 调色板分组 + 工具栏。）
# 1.10.4 工作流画布中文化收尾（第三段）。补完 1.10.2/1.10.3 漏掉的：
#       PipelineToolbar 的 Run 与"保存中"三元、App 顶栏的 Template/Save、模板与
#       共享对话框、AI 标注设置弹窗（API 供应商/模型/Key/标注语言 + 四处测试结果
#       提示）、质检弹窗的参数标签（23 个）与单位（frames→帧 / s→秒 / jumps→处）、
#       工作流抽屉、连线删除、节点悬停提示、store 里的四条 toast。
#       store 不是 React 组件、不能调 hook —— 那里直接 translate(k, currentLang())。
#
#       ★ 用了两轮才补全的教训：**字符串替换会漏掉特定写法**。`>Save<` 匹配不到
#         `{isSaving ? '...' : 'Save'}` 这种三元，也匹配不到图标后面的裸文本
#         (`<IconifyIcon /> Template`)。第二轮专门扫了这两类才补齐。若将来还要
#         批量改 JSX 文案，先扫这两种模式。
#
#       未覆盖：节点卡片描述（后端 description，仅悬停提示）；后端报告的结论文案
#       （Finding.message）仍是英文 —— 那部分要中文得在后端加语言参数。
# 1.10.5 首页概览（Overview）中文化。词条**早就写好在字典里**（overview_title /
#       stat_reviewing / retry / view_all …）但 overview.html **一处 data-i18n 都没标**
#       —— 和侧边栏同一类问题：翻译写了，标记没做。补 13 处静态标记 + 补 9 个缺的
#       词条（stat_to_review / stat_distribution / daily_uploads / last_30_days /
#       donut_total / stat_processing / stat_received / cleaning_passed / cleaning_failed）。
#
#       环形图、柱状图、状态徽标是 dashboard.js **现画**的，静态标注管不到：
#       label/title 改成 t() 调用，并给 setLang 的重渲染列表补上 refreshDashboard
#       —— 之前那份列表有 loadTasks/loadProjects/… 但没有它，切语言后首页图表
#       不跟着变，要手动刷新才生效。
# 1.10.6 用户管理页中文化。用户相关的词条此前**一个都没有**（只有 nav_users），
#       users.html 也没有 data-i18n —— 整页 35 处文案 + users.js 里 16 处动态生成的
#       （表格行的 Extend/Enable/Disable 按钮、角色与状态徽标、下拉选项、确认框）。
#       新增 56 个词条。角色/状态走 key 映射查表、查不到回落原始值 —— 后端加新角色
#       时显示原始值而不是空白。
#
#       ★ 两处裸文本必须包 <span> 再挂 data-i18n：``Password <span id="password-hint">``
#         与 ``<span>Disable</span> blocks login temporarily...`` —— applyTranslations
#         用 textContent 赋值，直接挂在父元素上会把平级的兄弟元素（提示 span）冲掉。
#
#       同时给 setLang 的重渲染列表补 loadUsers（与 1.10.5 的 refreshDashboard 同一
#       类缺口：表格行是 JS 现画的，不重跑就不跟着切语言）。
#       已核对：users.html 用到的 33 个 data-i18n 键，中英两个字典都齐。
# 1.10.7 模板层中文化收尾：index(12) / exports(10) / tasks(2) 三页补标注，
#       新增 23 个键。**模板层到此 9 个页面全部标注完毕**（login 按要求保持英文）。
#       核对：各模板共引用 123 处 data-i18n 键，中英两个字典都齐。
#
#       ★ 顺带修掉字典里**4 个重复键**（all_status / save_changes / slice_conflict /
#         target_episodes 在每个字典各定义了两次）。后一份静默覆盖前一份，其中
#         slice_conflict 与 target_episodes 两份**值不同** —— 前一份是死代码。
#         去重时我一开始保留了第一份（错的），**那会改变实际显示文案**；已改回生效值。
#         教训：去重"保留哪一份"必须按 JS 语义（后者覆盖前者）来，不能想当然。
#
#       另修一处会回退的文案：SORT_MODES 的 label 存的是英文原文，而
#       toggleSortOrder() 会把它直接写进 DOM —— 切中文后点一次排序就变回英文。
#       改成存 i18n key、写入时翻译。
#
#       剩下：JS 层动态文案（player / slice-preview / annotations / tasks 等约 50 处）。
# 1.10.8 修中英**不匹配**（不是"没翻译"，是两个方向对不上）。审计结果：
#       ① 键集合完全对齐（各 310 个，无单边键）
#       ② 中英**值相同**的 8 个里，4 个是真漏翻：overview_title / cameras /
#          left_hand / right_hand（中文界面一直显示英文）。另外 4 个是品牌名
#          processor / 死键 episodes / 术语 Port / 单位 s —— 保持英文正确。
#       ③ **反方向**：代码里写死中文、英文界面会露出来，9 处。已抽成词条：
#          annotations 的四个状态（切段中/VLM 分析中/写入/写入数据集）、player 的
#          「力 mN」与力场提示、slice-preview 的候选提示、index 的 AI 模式说明。
#          （base.html 的「中文」是语言自名，不翻是对的；index 的「只读」已有
#          data-i18n，只是初始 HTML 写中文，会被覆盖。）
#
#       顺带补一个缺口：applyTranslations **只处理 data-i18n 与 data-i18n-placeholder，
#       不处理 title** —— 带提示的按钮/图标没法靠标记翻译，只能写死。加了
#       data-i18n-title。
# 1.10.9 修 1.10.6 引入的显示 bug：用户表的「操作」列渲染成字面量
#       ``+ t('users_edit') +`` 而不是按钮。
#
#       ★ 根因是**拼接语法用错了上下文**：那段 HTML 在**反引号模板字面量**里，
#         而我在里面写了单引号拼接 ``title="' + t('x') + '"`` —— 在反引号里
#         ``'`` 和 ``+`` 就是普通字符，原样显示。模板字面量里必须用 ``${t('x')}``。
#         反过来，**单引号串**里用 ``${t('x')}`` 也会原样显示。
#
#       口诀：<反引号模板>用 ${t()}；<单引号拼接>用 ' + t() + '。看外层容器是哪个。
#       自查命令（两个方向都能抓）：
#         grep -n "' \+ t(" web/static/js/*.js        # 反引号里的误用
#         grep -n '\${t(' web/static/js/*.js          # 逐个确认外层是反引号
#
#       共修 10 处（users.js 的 Extend/Enable/Disable/Edit/Delete 五个按钮的
#       文字与 title）。已核对：反引号模板里无残留错误拼接，单引号串里的拼接合法。
# 1.11.0 修「回收站清不掉」。
#
#       现象：点「清空回收站」没反应，也不报错。后端日志里是
#       ``POST /api/v1/episodes/purge-trash → 500``。
#
#       根因是一个 9-08 留下的备份文件：两个 D435 项目的 ``meta/`` 里各有
#       ``info.json.bak-20260908-144550``，而 ``verify_project_dataset`` 的 meta
#       白名单是硬编码的 ``{info.json, stats.json, tasks.json, episodes}``，多出
#       任何非点开头的文件都判 unexpected。而 ``delete_episode`` 是**先删文件、
#       后跑校验**，校验失败就抛 RuntimeError；「清空」又是循环处理的，一个项目
#       失败整轮中止 → 500。**一个备份文件让清空彻底不可用。**
#
#       修两处：
#       (1) 校验放过备份/临时文件（``.bak`` / ``.bak-*`` / ``.orig`` / ``.tmp`` /
#           ``.save`` / ``~`` / 隐藏文件）。实测四个项目现在全 passed，而
#           ``junk.txt`` / ``real_data.csv`` 这类真杂项仍然被抓 —— 不是放水。
#       (2) 前端 ``restoreEpisode`` / ``permanentDeleteEpisode`` / ``purgeTrash``
#           **只有 ``if (res.ok)`` 一个分支**，非 2xx 时什么都不做 —— 这就是
#           "点了没反应、也无从查起"的原因。补 else 分支，用新增的
#           ``apiErrorDetail()`` 把后端 ``detail`` 显示出来（取不到就退状态码，
#           **绝不返回空**）。
#
#       ⚠️ 遗留隐患（本次未改）：删除是**先删文件后校验**，校验因真实原因失败时
#       文件已经没了、记录还在。实测 ``D435---black_glove_3d_keypoints_AI`` 的
#       ``episode_000001`` 就是这么没的（索引本身是对的，空洞合法）。
# 1.11.1 修「回收站删不掉」的**真正**根因 —— 项目索引缓存未失效。
#
#       1.11.0 修掉的那个 .bak 只是**第一个**拦路的，放行之后暴露出真问题：
#       ``write_project_episode_index`` 是索引的唯一写入点，却没有失效
#       ``_PROJECT_CACHE``（2026-09-11 commit 2bb9fe 引入缓存时漏的）。于是同一次
#       调用里"写完再读"拿到的是删除前的旧行：
#
#         delete_project_episode:
#           读一次(预热 60s TTL 缓存) → 删文件 → 写索引 → verify_project_dataset
#           → 又调 project_episode_rows → **旧缓存** → 报 "missing data: ..." → 500
#
#       盘上的索引其实一直是对的，纯粹是缓存没跟上。delete_episode 末尾确实会
#       invalidate_session_cache()，但那要等异常之后 —— 永远走不到。
#       **删除功能自 9-11 起就是坏的。**
#
#       修在唯一的写入点，5 个调用方（delete / merge / repack ×2 / …）一并受益。
#       实测：副本上连续删 7 个批次全部成功（5 个留行、2 个清空），校验零失败。
#
#       ★ 教训：加缓存时，**必须同时审所有写方**。这次是"读方加了缓存、写方忘了
#         失效"，症状还是那个老毛病 —— 不报错、报的是**别的**错误（"文件缺失"），
#         把人往数据损坏的方向引，而真相在缓存里。
# 1.11.2 修回收站页的 JS 报错：``TypeError: Cannot set properties of null
#       (setting 'textContent') at trash:248:55``。
#
#       trash.html 的内联脚本在设 ``nav-review`` / ``nav-trash`` 的 textContent，
#       但 base.html 里**从来没有这两个 id**（侧边栏用的是
#       ``<span data-i18n="nav_review">``，由 i18n.js 的 applyTranslations 处理）。
#       getElementById 返回 null → 每次 DOMContentLoaded 必抛。``git log -S`` 查过：
#       该 id 从未存在过，所以是**一直就坏**，不是本轮引入。
#       （app.js / i18n.js 里同样的取值都写了 ``if (x)`` 保护，只有 trash.html 没有。）
#
#       处理：删掉那段多余的导航文案逻辑，本页只保留页面标题；标题在
#       DOMContentLoaded 与 egodata:lang 事件上各更新一次。
# 1.11.3 修双目自动配对把动态端口画重复的 bug（前端为主）。
#
#       现象：Stereo RGB-D Camera 的 3 个输出，在 Data Cleaning 上画出【4 个输入口】，
#       其中 "Left RGB Video" 重复。用户实测截图发现。
#
#       根因在 onConnect 的双目配对：``siblingConn = {...next, sourceHandle: 副目}``
#       —— 只换了源端口，**targetHandle 沿用了主端口的动态 key**。而 isDup() 恰好
#       按 targetHandle 判重，于是用户再手动连"另一目"时判成"没连过"，又加一条。
#       端口 key 只由「源节点+源端口」算出，两条同源端口的边 → 同一个 key → 两个同名口。
#
#       普通节点（targetHandle 恒为 "video"）主副相同，所以这个 bug 一直没暴露 ——
#       是 1.9.8 的动态端口把它揭出来的。
#
#       对照实验（真实 store 驱动，非静态检查）：
#         with bug : 连左目→右目 → 4 条边 / 2 个 key；再连深度 → 6 条边 / 3 个 key
#                    （配对边甚至把 depth 的目标口也抄走了）
#         fixed    : 2 → 2 → 3 条边，key 数恒等于边数
#       同时给 useDynamicInputs 加了按 key 去重 —— 一个 key 只能有一个口，
#       渲染层兜底，防止历史遗留的重复边再画出重复端口。
# 1.11.4 上传后的存储索引改为**沿用名字里的编号**。
#
#       此前 append_project_episode 一律用 max(现有)+1，而公开 ID（批次名）是
#       沿用上传包的 —— 两套编号各走各的，于是"名字 Test94_000005 / 索引 3"
#       这种对不上的情况出现。**实测 119 个带编号批次里 21 个中招**（18%）。
#
#       危害在 _match_episode_row 的名字回退：按 id 匹配不到时会用
#       episode_index_from_name() 反推索引，从而撞上别的批次。模拟确认：
#       删「..._000002」时若它自己的行已缺失，会去删 index=2 那个**别的批次**
#       的数据和视频。
#
#       现在优先采用名字里的编号（编号允许空洞，模块里已写明 gaps are valid）；
#       仅两种情况回落 max+1：名字里没有编号，或该编号已被别的批次占用。
#       副本实测：传 Anything_000005 → 索引 5；传 Anything_000002（撞号）→ 6；
#       传 NoNumberHere → 7。
#
#       顺带修同一处的缓存缺口：append 是在 temp_root 上写完再 rename 换入的，
#       而 write_project_episode_index 失效的是 **temp_root** 的 key —— 换入后
#       project_root 的缓存仍是旧行。后果两条：(1) 紧跟的
#       verify_project_dataset(project_root) 读旧行，**校验在验旧树**（三次
#       append 全部"通过"而盘上早已是 [0,1,2,5,6,7]）；(2) 缓存有 60 秒盲目
#       TTL，上传后前端最长 60 秒看不到新批次。换入后显式失效。
# 1.12.0 新增「模型训练」页 —— 只读展示训练产物;新页面前端改用真 ES module。
#
#       背景:模型由官方 lerobot-train 在本平台**之外**训练(conda 环境
#       lerobot052_base_piper),产物落在 文件测试/umi_act_model/checkpoints/。
#       此前要了解训练结果只能自己去翻文件系统。
#
#       本期**只做界面,不执行训练**:
#         • 后端 app/routes/training.py —— 唯一端点 GET /api/v1/training/runs,
#           只读扫描 checkpoints/ 并读 train_config.json。目录不存在返回
#           runs:[](新机器从没训过是正常情况,不是错误)。
#           两种层级都支持:root 本身是 run,或 root 是装多个 run 的父目录。
#         • 前端 web/static/js/train/ —— 真 ES module(import/export),不再往
#           app.js 里堆。六个文件:api / dom / format / params / checkpoints /
#           main,main.js 是唯一入口,其余是它 import 的模块。
#         • 「训练指标」「实时日志」两个页签**故意不做** —— 训练在平台外执行,
#           没有 loss/lr/grad_norm 落盘,也没留日志。没有数据就不显示:摆一个
#           空页签比不摆更让人以为功能坏了。将来接了训练执行再加回 TABS 数组。
#
#       ★ 顺带堵一个缓存陷阱:static_cache_headers 现在对 /static/js/ 下**无查询
#         串**的 JS 发 no-cache。原因:ES module 的相对 import 解析出的 URL 不带
#         查询串 —— 入口 script 写了 ?v=,`import './api.js'` 也永远是同一个地址,
#         给它任何正数 max-age,升级后都会出现「新 main.js + 旧 api.js」混用,
#         直接抛 TypeError。范围刻意收窄:带 ?v= 的老脚本仍走 86400 那支,
#         /static/ 顶层的第三方库(plyr.js、iconify-*)也不在范围内。
# 1.12.1 侧边栏「模型」改成一级项(与 Video Review 同级同款),不是灰色分组标签。
#
#       位置对了但层级不对:原来是「不可点击的小字标题 + 一个 sidebar-link」,
#       视觉上像个分组标签。改成 **一级项(robot 图标 + 可点击 + 选中蓝底)+
#       二级短名项(训练)**,与 Video Review 的结构一致。
#
#       ★ 顺带修高亮脚本:它原来只遍历 .sidebar-link,而二级项挂的是
#         sidebar-sub —— 不一起遍历的话「训练」永远不会亮。选择器加
#         [data-page] 限定,避开另一段脚本管的 review 状态快捷项。
#       ★ i18n key 改名 nav_group_model → nav_model(它不再是"分组标题")。
#
#       一并 bump 版本号:浏览器已按 ?v=1.12.0 缓存了 i18n.js 24 小时,
#       不换 URL 的话上面这些文案改动根本到不了前端。
# 1.12.2 训练页去掉硬编码中文 —— 英文模式下会露出"模型: act""[下载][删除]"。
#
#       页面标题/页签走了 t(),但 run 头部的「模型/位置」、checkpoint 的
#       「步骤/文件明细/下载/删除/推理/离线部署」、文件表的三个表头、参数页的分组
#       标题与「来源:」全都写死成中文 —— 中文模式看不出来,切英文就半中半英。
#
#       一并两处结构性的:
#         • 新增 static/js/train/text.js:i18n 桥接。应用的词典是**经典脚本**
#           (只有全局 t(),没有 export),ES module import 不到,三个视图各自去
#           摸 window.t 不如集中一处 + 防御性回退(取不到就显示英文,不白屏)。
#         • 参数页的分组标题存 **i18n key** 而非翻好的文字:它原来在模块加载那
#           一刻就定死了,用户之后再切语言不会跟着变。改为渲染时取值。
#         • main.js 接 egodata:lang 重画 —— 本页内容全是 JS 现画的,setLang()
#           只翻 DOM 上的 data-i18n 属性,翻不到动态节点,不重跑就还是旧语言。
# 1.13.0 训练页加顶部一级页签:「模型训练」+「新建训练」。
#
#       右侧内容区顶部两个页签(对齐参考实现):模型训练 = 已有产物清单,
#       新建训练 = 五段式向导(选模型 / 选数据 / 导出设置 / 训练参数 / 生成命令)。
#
#       两个页签都是**真内容**,没有占位:
#         • 模型训练 —— 上一版做的就是它,原样搬进来。
#         • 新建训练 —— 新增 static/js/train/newrun.js。选数据那一段走既有的
#           GET /api/v1/ingestion/episodes(训练数据就是采集来的 episode,没
#           必要另开一套列表)。
#         • 默认超参全部取自**实测那一次**的真实 train_config.json,不是拍的。
#         • **仍然不接后端执行** —— 这一页的产出是一条完整的 lerobot-train
#           命令行(复制 / 下载 .sh)。比放一个点了没反应的"开始训练"有用,而且
#           表单每个字段就是一个 --key=value,天然定义了将来后端的接口契约。
#
#       ★ 留出集防线:UMIGripper_Action_AI 的 52–73 集模型没见过,重训混进去会
#         让泛化评估失效 —— 而代码里**没有任何硬编码排除**。UI 上做成可改的区间
#         输入框(不是写死常数,换数据集时区间就变了):命中行标红 HOLDOUT、默认
#         不勾、「全选」自动跳过、单独勾选要二次确认。
#
#       ★ 懒渲染:面板第一次显示时才拉数据。否则一进页面就同时请求训练产物和
#         几百条 episode 列表,而后者多半没人点。
#       ★ dom.js 的 el() 增加 SET_ATTR:<iconify-icon> 是自定义元素,只监听
#         attribute。`node.icon = x` 不给它挂 attribute,图标画不出来且不报错。
# 1.14.0 「新建训练」按参考实现重排成六段:位置 / 模型 / 参数 / 数据集 / 续训 / 归属。
#
#       • ① 训练位置 —— 只做本机。勾选参与训练的 GPU,命令里落成
#         CUDA_VISIBLE_DEVICES。云端算力(RunPod/AWS/腾讯/阿里)不摆点不动的卡片。
#       • ② 模型 —— 只 ACT 一张;训练框架 LeRobot 做成**只读文本**而不是下拉框
#         (ACT 只有一种实现,给个下拉框是假的)。
#       • ③ 参数 —— 8 项各带一句说明;默认值仍是**实测那一次**的值
#         (batch_size=16 / steps=20000),不是参考实现的示例值(那个 2/10000
#         是给它 8GB 的 pi0 用的)。
#       • ④ 数据集 —— **改成按「项目」选(多集)**。原来按 episode 逐个勾:
#         实测 state/exports 下 168 条记录**每条 total_episodes 都是 1**(单集
#         导出),凑不成训练集;项目树本来就是多集的(UMIGripper_Action_AI 81 集),
#         且被官方加载器验证过。
#       • ⑤ 续训 / ⑥ 项目归属 —— 前者用已有 checkpoint,后者用 /api/v1/projects。
#       • 「创建训练任务」**存在但禁用**,旁边给出真正的产出:完整 lerobot-train
#         命令行(复制 / 下载 .sh)。后端执行以后接,接口契约就是这些 --key=value。
#
#       ★ 留出集防线换了形态。上一版按 episode 勾选,可以"默认不勾 + 全选跳过";
#         现在选的是整个项目,而留出集就在项目内部(UMIGripper_Action_AI 81 集,
#         留出集是其中第 52–73 集)—— 没法"默认不勾",只能选中会覆盖到留出集的
#         项目时**明确报警**。区间仍是可改的输入框,不是写死常数。
#
#       新增两个只读端点:
#         GET /api/v1/training/gpus     —— 读 nvidia-smi。**驱动没起来时如实报错**,
#           不返回一排 0:0% 利用率看起来像"显卡闲着",而真相是"问不到"。
#         GET /api/v1/training/datasets —— 扫 sessions/<项目>,只列 total_episodes>0 的。
# 1.14.1 新建训练页两处文案调整。
#
#       ① 训练位置那张卡片改名:「本机 GPU 服务器」→「私有算力」。
#          原来那个名字只说了"在哪台机器",没说"这是什么性质的算力"。参考实现
#          那条叫「私有云算力」—— 与自家机器相对的是**租来的云算力**,「私有
#          算力」才把这条线划清楚。
#
#       ② 删掉两段说明文字:
#          • 「目前只做 ACT。其它 LeRobot 内置策略同样可跑,但本平台没实测过。」
#          • 「云端算力(RunPod / AWS / 腾讯 / 阿里)本期不做 —— 不摆点不动的卡片。」
#          摆一张卡片只在旁边写一句"别的不做"是噪音;真要扩展时直接加卡片即可。
#
#       ③ 段落顺序改成:数据集 → 模型 → 算力(训练位置)→ 参数 → 续训 → 归属。
#          原来是 位置 → 模型 → 参数 → 数据集。新顺序按**决策依赖**排:先用什么
#          数据、再训什么模型、再在哪训、最后怎么训 —— 数据集本身就决定命令里的
#          --dataset.root,放最后意味着要先看后面的表单才知道上面该填什么。
#          注意 newrun.js 里**构造顺序 ≠ 展示顺序**:各段必须先构造好(GPU 面板、
#          留出集警告、命令生成互有依赖),最后由一个 append 数组决定谁在上面。
#
#       一并清掉一个死 key(train_files_empty),它定义了但没有引用点。
# 1.15.0 训练数据集改为「审核页导出后的成品」;导出不再把数据集删掉。
#
#       原来的链路是断的:审核页导出 → 构建 LeRobot 数据集 → 打包 zip →
#       **rmtree 删掉数据集目录**。用户拿到的只有一个 zip,磁盘上什么都不剩,
#       训练页自然无从列起 —— 只能退而列 sessions/<项目> 原始项目。
#
#       改法(两处,都很小):
#         • export.py 打包后调用 _persist_trainable_dataset(),把数据集落到
#           state/datasets/<数据集名>/,而不是删。**交换式替换**:新的先搬进
#           .staging,旧的挪到 .old,再 rename 上位,最后删 .old —— 中途失败
#           不会让目标位置停在"半个数据集"上(直接 rmtree + move 就会)。
#         • training.py 的 /datasets 改扫 state/datasets/,不再扫 sessions/。
#
#       ★ 为什么这个改法更好:原始项目里混着**未审核**的集,而且**留出集**
#         (UMIGripper_Action_AI 的 52–73)就在里面 —— 按项目选等于同时踩两个
#         坑,UI 上只能报警、拦不住。以导出产物为准之后,两个坑都在导出那一步
#         被排掉:导出本来就校验 status ∈ {reviewed, approved}。
#
#       ★ 留出集防线**从训练页移走**了。导出后的数据集集号会被重排成 0..N-1,
#         训练页已经认不出哪一集原本是 52–73。控制点转到导出那一步 —— 导出时
#         别把留出集勾进去。要加硬校验的话,位置在审核/导出页。相关 UI(区间
#         输入框 + 红色报警)与 parseRanges 一并删除,不留死代码。
#
#       数据集为空时训练页给的是**可点的引导**(跳 /review?status=approved),
#       而不是一句"没有数据" —— 首次使用时这个列表必然是空的。
# 1.15.1 「项目归属」改成名副其实的归档标签 —— 之前承诺了做不到的事。
#
#       那一栏原来写「不选项目时,这次训练是私密的,只有创建者能看到」,是照搬
#       参考实现的说法。**但本系统的项目根本没有属主**:list_projects() 返回的
#       字段只有 id/name/description/status/workflow_id/observed_inputs/
#       uploaded_total/params/name_aliases —— 没有 owner、没有 created_by。权限
#       也是全局角色制(require_roles),不是按项目授权。
#
#       也就是说"私密任务"这句是**空头支票**:选与不选,能看到的人完全一样。
#
#       改成写实的说法:它只是个归档标签,便于日后按项目查找这次训练,并**显式
#       说明没有可见性控制**。将来真要做按项目授权,得先给项目加属主、给训练
#       任务加属主、再改中间件 —— 那是另一个量级的改动。
# 1.16.0 新建训练去掉「⑥ 项目归属」;job_name / output_dir 改成真正的参数。
#
#       ① 删掉项目归属那一栏。上一版已经把它的说明改成写实(只是归档标签、不含
#          可见性控制),但一个**不产生任何行为差异**的下拉框留着就是噪音 ——
#          本系统的项目没有属主字段,选与不选对数据的去向、谁能看到,全都没有
#          影响。真要按项目授权,得先给项目加属主再谈 UI。
#          一并删掉 train_sec_project / train_project_* 四个 key,不留死词条。
#
#       ② output_dir 与 job_name 原来是**从项目名推出来的**
#          (`文件测试/${项目名}` / `${项目名}`)。项目没了就没有来源,而这两项
#          本来就是 lerobot-train 的顶层参数,放进「③ 训练参数」更名副其实:
#            output_dir  默认 文件测试/umi_act_model(实测那次的值)
#            job_name    默认 act
#          output_dir 是路径,给的是宽输入框(窄列里看不全)。
# 1.16.1 前端三处修复 —— Tailwind 类静默失效 / 图标离线化 / 前端版本号。
#
#       ① **训练页一半的 Tailwind 类根本没生效**。tailwind-generated.css 是
#          构建产物,而全仓**没有任何记录说明它怎么生成** —— 唯一的配置文件是
#          workflow-studio 的,content 范围只有 ./src/**,不含 Jinja 模板和
#          static/js。那份 CSS 停在 8/19,没人能复现。后果是 w-28 /
#          text-[10px] / accent-blue-500 / divide-y / break-all 等类全部静默
#          失效(页面能显示,只是样式没上)。
#          修法:用覆盖全部前端的 glob **先生成到临时文件**,逐个核对会被删掉
#          的类(8 个,全部验证为死类 —— 注意别用 grep 直接搜类名,select-all
#          会误命中 id="select-all-bar"),确认是超集后才替换现场。
#          一并修掉 app/version.py 与两个 README 里「前端版本见
#          workflow-studio/package.json」这个错误说法 —— 那只代表 SPA 自己。
#          新增 **web/VERSION**(当前 1.9.x)覆盖整个 processor/web/。
#
#       ② 图标收尾。训练页用了 emoji(📁)与字符(⊙),与全站的 iconify 图标
#          不一致,换成 ant-design:folder-outlined 与 info-circle-outlined。
#          后者白名单里没有 —— 从 iconify **构建期**拉下来内联进
#          iconify-preload.js(59 → 60 个),运行时仍然不联网。
#
#       ③ web/VERSION 与 README/version.py 的版本号对齐(原来 README 写的是
#          后端 1.6.8 / 前端 1.5.6,差了十几个版本)。
#
# 1.16.2 「新建训练」删掉整段「创建训练任务」,checkpoint 改为默认折叠。
#
#       ① 删掉禁用的按钮 + 「后端执行还没接」那句 + 生成的 lerobot-train 命令
#          行 + 复制/下载。后端接上之前,那两样要么是假按钮、要么是绕路把命令
#          当交付物。**这一页因此变成纯表单** —— 数据集/GPU/参数/续训都不再
#          驱动任何输出,它们是为将来后端准备的状态。文件头注释写明了这点,
#          免得下一个人以为坏了。
#          一并清掉 renderCommand / commandBox / LEROBOT_TRAIN / COPYABLE
#          (43 行)、10 个 i18n key(中英双份 20 行)、3 处还写着「六段」
#          「归属」「下面生成的命令」的过期文案。newrun.js 485 → 379 行。
#
#       ② checkpoint 全部默认折叠。原来让最新的那个展开,结果一进页面就被
#          11 个文件名顶下去,反而看不到下面还有几个 checkpoint。
#
# 1.16.3 后端新增 app/training/ 包 —— 训练域从路由层抽出来。
#
#       与前端 static/js/train/ 对称:routes/training.py 退回成薄薄一层路由,
#       域逻辑进包:
#           app/training/gpus.py        nvidia-smi 读取
#           app/training/datasets.py    state/datasets/ 扫描
#           app/training/runs.py        训练产物(checkpoints)扫描
#       三个模块都**不认识 HTTP**,返回普通 dict,所以将来 runner 起子进程前
#       要确认显卡时可以直接复用,不必绕道走一遍路由。
#
#       **这是"新功能先模块化"的第一次实践** —— 域包里的东西没有任何存量引用,
#       抽出来零风险。反例就在旁边:app/ai_annotation.py 长到 180KB 才想起来
#       该拆,而那时候拆要动 64+ 个 import 点。
#
#       注:老的 37 个顶层 .py 按域归位是**另一件事**、风险中高(90+ 个 import
#       点跨 worker/scripts/tests/model_eval 五个地方),不在本次范围内。
# 1.16.4 修训练页半英半中:ES module 在 DOMContentLoaded 之前就渲染了。
#
#       现象:中文模式下,顶部两个页签是英文(Model Training / New Training),
#       而它下面的子页签(模型输出 / 训练参数)是中文 —— 同一页两种语言。
#
#       根因是**渲染时机**。i18n.js 的 currentLang 默认写死 'en',要到它自己的
#       DOMContentLoaded 回调里才从 localStorage 读出 'zh'。而 main.js 原来是:
#
#           if (document.readyState === 'loading') addEventListener(DOMContentLoaded, boot)
#           else boot()
#
#       ES module 是 **deferred** 的 —— 它在"文档解析完、DOMContentLoaded 还没
#       触发"这个中间态执行,那一刻 readyState === 'interactive',所以
#       `=== 'loading'` 为假,直接走 else **当场渲染**。于是:
#         顶部页签(boot 里同步建)→ currentLang 还是 'en' → 英文
#         子页签(await listRuns 之后才建)→ 已经是 'zh' → 中文
#       切换语言后能正常,是因为 setLang() 会派发 egodata:lang,而 main.js
#       接了这个事件重跑 boot —— **只有首次加载是坏的**。
#
#       修法:判据改成 `readyState !== 'complete'`(interactive 也算"还没 fire",
#       这时挂监听一定会被调到)。
#
#       ★ **这是 ES module 独有的坑,老页面不受影响** —— 那 13 个经典脚本都在
#         解析期执行、老老实实挂 DOMContentLoaded。以后往 static/js/ 下加
#         module 时都得留意这个中间态。
#
#       一并删掉 train.html 顶部的页面大标题「模型训练」(四个字)+ 它的 i18n
#       key。下面的两个页签已经说明"你在哪一页",侧边栏也重复说了同一件事。
#       那一行改成 justify-end —— 只剩刷新按钮一个子元素时,space-between 会
#       把它推到左边去。
# 1.16.5 修训练页内容贴左边 —— 又是 Tailwind 构建产物没跟上。
#
#       现象:把 train.html 的 `p-6` 拆成 `px-6 pt-3 pb-6` 想让页签上提,结果
#       **左右内边距直接归零**,内容贴到侧边栏上。原因不是 CSS 写错 —— 是
#       px-6 / pb-6 当时**没人用过**,tailwind-generated.css 里根本没有这两条
#       规则,写了等于没写。而那次改动发生在**上一次重新生成 CSS 之后**。
#
#       ★ 这是同一个坑第二次踩:
#         第一次 → 训练页一半的类(w-28 / text-[10px] / accent-500 / divide-y)
#                   因为 CSS 停在几周前而全部静默失效(1.16.1)
#         第二次 → 本次,padding 归零
#       共同点:**加/改 Tailwind 类之后忘记重新生成,而且全程不报错**。
#
#       所以这次加了防呆脚本 scripts/check_tailwind_build.sh:
#         用同样的 content 范围重新生成到临时文件,和现场那份 cmp 比对;
#         不一致就报错,`--fix` 则就地重新生成。
#       跑法:bash scripts/check_tailwind_build.sh [--fix]
#
#       ★ 改完 CSS 记得 bump 缓存键。**这条在 1.19.4 之后指的已经不是本文件了**
#         —— 缓存键改用 web/VERSION(前端号),见那一条。此处保留原文不改,
#         因为它是当时的事实记录。
# 1.17.0 训练页加「到点提醒」—— 项目已通过集数达到采集上限时提示可以导出。
#
#       判据由用户定:**已通过集数 >= 项目设置的采集上限**(params.target_episodes)。
#       到点只**提醒**,不自动导出 —— 页面上一块绿色提示 + 「去导出」链接。
#
#       ★ 为什么不做全自动(讨论过,用户选了提醒):
#         • 导出一次约 **30 秒/集**(UI 自己的 batch_export_hint 文案),1000 集要
#           跑 8 小时以上;深夜跑、NAS 中途断了没人知道,而现在的导出是全成或全败。
#         • 构建好的数据集会被**固定**下来 —— 已经拿去训练的那份不能悄悄变。
#           同一个路径指向不同内容,训练就没法复现了(这也是自动导出必须先解决
#           版本命名的原因)。
#         • 让人点一下,换到的是"我知道现在在导什么、用哪份数据"。
#
#       实现:
#         app/training/projects.py   list_projects_progress()
#         GET /api/v1/training/ready  {projects:[{name,target,uploaded,approved,ready}], ready_count}
#         前端 newrun.js 的 ① 训练数据集里,到点则绿框列出,没到点则给一行「最接近的」
#         (否则那块永远是空的,看不出进展)。
#
#       ★ 两个刻意的取舍:
#         (1) **target 为 0 的项目永不 ready** —— 没配上限就没有"到点"这回事,
#             不能把"没配"当成"已达"。
#         (2) **只扫一次 scan_sessions()**。它是全量遍历,数据在 NAS/sshfs 上时
#             很贵(见 README 的"打开 episode 慢");按项目各扫一遍就是 O(项目数)
#             次全量。上传数与通过数在同一次遍历里一起数出来。
#
#       ⚠ 实测现状:没有一个项目接近上限(52/10000、4/200、3/1000…),所以
#         ready_count 现在恒为 0,页面只会显示「离采集上限最近的」那一行。
#         这不是 bug —— 现有 target_episodes 是**采集计划量**,而实际工作流里
#         远没到那个量就会先导出。要不要把上限调成"导出阈值"由用户决定。
#
#       顺带:新增 scripts/check_tailwind_build.sh,并且**当场抓到了我自己这次
#       忘了重新生成 CSS**(加了 bg-green-950/40 等新类)。
# 1.17.1 训练页:删掉 output_dir 表单字段;checkpoint 文件支持下载。
#
#       ① 删 `output_dir`。它本来就不该是前端的字段 —— 输出去哪是**服务端配置**
#          (settings.TRAIN_OUTPUT_ROOT,默认 文件测试/umi_act_model,可用环境变量
#          覆盖),而且训练页正是读它来列 checkpoint 的。让前端再选一次,等于
#          让用户有机会选到一个页面上根本列不出来的目录。
#          顺带删掉 WIDE_FIELDS(它只服务这个字段)和相应的宽输入框分支。
#
#       ② checkpoint 文件下载上线(原来按钮是禁用的):
#            GET /api/v1/training/runs/{run}/checkpoints/{step}/files/{name:path}
#          逐文件而不是整包:一个 checkpoint 约 600MB,其中 training_state/
#          (优化器状态,约 400MB)**只在续训时有用**;拿去推理/部署只要
#          pretrained_model/(约 200MB)。按需下载比强制拉一个 600MB 的包合理,
#          所以整包下载那个按钮直接去掉了。
#
#       ★ 安全:路径**不做拼接**。直觉写法 `checkpoint_dir / relative_name` 会让
#         `../../../../etc/passwd` 读到任意文件 —— 这个端点是对外开下载的。
#         改成:先扫一遍拿到磁盘上的真实文件清单,再按名字**精确匹配**。返回的
#         路径必然来自真实条目,构造不出清单外的。实测四种穿越写法(相对、
#         带前缀的相对、绝对路径)全部返回 None。
#         (刻意**不用** resolve()+is_relative_to 那种"先拼出来再检查"的写法 ——
#          清单匹配是从源头上就没有越界的可能。)
#
#       `{name:path}` 而不是 `{name}`:要下的文件在子目录里
#       (pretrained_model/model.safetensors),默认的路径参数不匹配斜杠。前端拼
#       URL 时也要**逐段编码、保留斜杠** —— 整串 encodeURIComponent 会把斜杠也
#       编掉,后端就找不到文件。
# 1.18.0 服务端 i18n —— 修「每次翻页先闪一下英文」。
#
#       现象:中文用户点任何一个导航,新页面先渲染成英文,再由 JS 纠正成中文。
#       每次整页导航都闪一下。
#
#       根因:语言只存在浏览器的 **localStorage** 里 —— 服务端看不到,所以渲染
#       ``data-i18n`` 元素时只能填**默认语言(英文)**,再由 DOMContentLoaded
#       里的 applyTranslations() 改。改的时机在首屏之后,必然闪。
#
#       改法(用户选的「A 方案」):语言同时写 **cookie**,服务端渲染时就用它取词。
#
#         web/static/js/i18n-data.js   ← 词典的**唯一真源**(合法 JSON + 一行赋值)
#         app/i18n.py                  ← 读它 + t(key) + ContextVar + 中间件
#         app/routes/pages.py          ← 注册 Jinja 全局 t / current_lang
#         base.html / login.html       ← <html lang="{{ current_lang() }}">
#         8 个模板 132 处              ← 硬编码文案 → {{ t('key') }}
#         i18n.js 921 → 130 行         ← 只剩逻辑,词典搬走
#
#       ★ 词典为什么单独一个文件:i18n.js 是 JS,而服务端是 Python。写在里面的话
#         Python 要么重复维护一份、要么去解析 JS 源码。现在两边读**同一份**文件,
#         不会分叉。浏览器当普通脚本同步加载(带 ?v= 缓存)。
#
#       ★ 客户端的 currentLang **以 <html lang> 为准**,不再只读 localStorage。
#         服务端是从 cookie 读的,两者可能不一致(清了 localStorage 但 cookie
#         还在);一旦不一致,applyTranslations() 会把服务端刚渲染好的中文**又翻
#         回英文** —— 页面反而变成"先中文后英文",比原来更难看。
#         以 <html lang> 为准还有个好处:它和 DOM 里的文案**必然一致**(服务端就
#         是按它渲染的),所以首次 applyTranslations() 是个空操作。
#
#       ★ Jinja 全局 current_lang 注册的是**函数**,模板里必须写
#         ``{{ current_lang() }}`` 带括号 —— 语言是每请求才定的(ContextVar),
#         全局对象本身是固定的。少写括号不报错,只会把函数 repr 渲进 HTML:
#             <html lang="&lt;function get_current_lang at 0x7f…&gt;">
#
#       ⚠ 改这轮的教训:批量正则改模板**连错两次** ——
#         第一次丢掉了 129 处 `</`(分组用错),第二次又没删掉旧文案(替换函数
#         把原文案原样返回)。两处都是靠"改完做结构校验"发现的,不是靠看:
#           · 每个 data-i18n 元素的 t(key) 是否与 key 一致 → 0 处不符
#           · 标签开闭是否配平(排除 input/img 等空元素)→ 全平
#           · <script> 块里有没有被误伤的 {{ t() }} → 0 处
#           · Jinja 能否解析全部 10 个模板 → 全通过
#         其中 trash.html 是被"漏掉"的那一个:它的 JS 注释里有一个**示例标签**
#         <span data-i18n="nav_review">,被当成真标签处理;而它同时吞掉了
#         </script> 的 `</`,导致扫描脚本没能识别出那个脚本块。
# 1.19.0 新增「推理」模块(模型 → 推理);前端抽出 shared/ 共用工具。
#
#       侧边栏「模型」下现在两项:训练 / 推理,与参考实现一致。
#
#       推理页做什么(本期**只做选型与查看,不执行**):
#         ① 选择模型 + 检查点   → 真实数据(GET /api/v1/training/runs)
#         ② 该检查点的训练参数  → 真实数据(checkpoint 里的 train_config.json)
#                                 只挑 policy.* 那几个关键项 —— chunk_size /
#                                 n_action_steps 这类直接决定推理输出,推理前最该确认
#         ③ 测试集             → 真实数据(GET /api/v1/training/datasets)
#         ④ 运行推理 + 结果     → **故意禁用**,旁边写明原因;结果区是空态
#
#       ★ 不摆空页签:参考图里还有「训练指标」「实时日志」,我们没加 —— 没有数据
#         的页签比不摆更让人以为功能坏了(同训练页的理由)。这两个要等**训练执行
#         后端**,那时候 loss/lr/grad_norm 才有地方来。
#
#       ★ 「离线部署」也没做 —— 参考图里它是 checkpoint 上的另一个按钮,和「推理」
#         不是一回事(推理是跑起来看指标,部署是打包成交给机器人的交付物)。
#         checkpoint 上那个「离线部署」按钮仍是禁用的。
#
#       前端结构:新增 static/js/shared/,把 dom.js / format.js / text.js 从
#       train/ 挪出来 —— 它们是**纯工具、没有领域概念**,两个页面都要用。不这么
#       做的话推理页得写 `import ... from '../train/dom.js'` —— 推理引训练的工具
#       是没道理的依赖方向。
#       推理页的数据源(runs / datasets)仍**直接引 ../train/api.js**:那两个端点
#       属于训练域(推理消费的是训练产物),这个依赖方向是对的。等有第三个页面也
#       用到它们,再抽到 shared/。
#
#       ★ 页面入口的 boot 时机照抄训练页:ES module 在"文档解析完、DOMContentLoaded
#         未触发"的中间态执行,那时 i18n.js 还没定好语言 —— 必须等 DOMContentLoaded。
#         训练页为此踩过一次(顶部页签显示英文而子页签中文),不再踩第二次。
#
#       顺带:tailwind 防呆脚本这次又抓到一次(推理页引入了新类),已 --fix。
#       这是它上线后第二次立功 —— 加/改 Tailwind 类之后忘记重新生成,失败是
#       静默的,没有脚本就只能靠肉眼在页面上发现"这个间距怎么不对"。
# 1.19.1 「推理」并进「模型」页 —— 侧边栏去掉二级项,页内三个页签。
#
#       侧边栏原来是这样,两个二级项指向**两个页面**:
#
#           模型
#             ⚡ 训练    → /train
#             ▶ 推理    → /inference
#
#       改成:
#
#           模型        → /train,页内三个页签:模型训练 / 新建训练 / 模型推理
#
#       理由:训练和推理本来是**同一批产物的上下游**(训练产出 checkpoint,推理
#       消费它),拆成两个导航项反而要来回跳。
#
#       顺带两处:
#         • 删掉训练页右上角的**刷新按钮**。浏览器自带 F5/Ctrl+R,自己做一个是
#           多余的;它一走,顶部那行也就没必要留着,内容直接从顶部开始。
#         • `/inference` 路由与 `templates/inference.html` 一并删除。
#           static/js/inference/main.js → **view.js**,不再是页面入口,而是被
#           train/main.js 当第三个页签 import(`render: renderInference`)——
#           名字跟着职责改,免得下一个看的人以为它还会自己启动。
#
#       配色/结构不变,只是搬家:inference/view.js 的内容一字未改。
#
#       ⚠ `/inference` 这个地址现在 404。它只存在了一小会儿,没有外部引用;
#         要是有人收藏了,重新点侧边栏「模型」即可。
# 1.19.2 模型页三个页签重排:新建训练 → 模型训练 → 模型推理。
#
#       原来是 模型训练 → 新建训练 → 模型推理。**数组顺序同时决定展示顺序和默认
#       打开哪个**(boot() 里把第 0 个设为激活),所以这一改,进页面直接落在
#       「新建训练」上 —— 这是用户要的。
# 1.19.3 「新建训练」删掉三段说明文字;顺带修一处过期兜底。
#
#       ① 「离采集上限最近的 X: n/N」整段删掉(连同它的两个 i18n key)。
#          那行**每次都在**,而绝大多数时候又离得很远(实测 3/100、52/10000),
#          读起来就是一句永远不变的噪音。到点了自然会冒出来,没到点就安静待着。
#
#       ② 「这里列的是从审核页导出的数据集…先选一个,或者先去导出一份」删掉。
#
#       ③ 「续训需要 --policy.path=…;命令里会带上,但本页不会真的启动训练」删掉。
#          ★ 这条其实**早就过期了** —— 生成命令那一整段在 1.16.2 就删了,
#          "命令里会带上"已经无从谈起,当时漏改了这句。
#
#       ④ 顺带修一处同类的过期:GPU 不可用那条提示的**英文兜底**还写着
#          "the command below will fail without it"。字典里的中文值当时改对了,
#          但兜底字符串没跟着改 —— 只有字典缺 key 时才会显出来,平时看不见。
#
#       一并清掉 4 个变成孤儿的 key(train_ready_nearest / train_ready_approved /
#       train_dataset_hint / train_resume_hint),词条 417 → 413。
# 1.19.4 静态资源缓存键改用**前端号**(web/VERSION),不再用后端号。
#
#       起因:用户看到 web/VERSION 没跟后端号一起变,问"这个文件要删吗"。
#       查下来文件是好的(1.12.2→1.12.3 确实涨了),但暴露出一个真问题 ——
#       **改一次前端要 bump 两个号**:后端号给 ?v= 用、前端号给提交信息用。
#       而且两个数一旦对不上(提交信息写 1.12.3、浏览器拿的却是后端号那版),
#       看提交记录的人没法判断线上到底是哪一版。
#
#       改法:`app/routes/pages.py` 的 asset_version 从 `app.version.__version__`
#       换成新模块 `app/web_version.py` 的 `frontend_version()`。
#       于是一个号管两件事 —— 缓存键和提交信息里的「前端 X」是同一个数。
#
#       顺带把散在三处的旧说明一起改了(不改的话下一个人会照着写过期的规矩):
#         app/version.py 文件头、web/VERSION 的注释、README 第 11 节(中英)
#
#       ★ 代价:web/VERSION 在启动时读一次就缓存住(lru_cache),所以 **bump 完
#         要重启服务**才生效 —— 与后端号原来的行为一致(它也是模块级常量)。
#         想让它立刻生效得改成可调用对象、模板写 `{{ asset_version() }}`,
#         那要改 22 处模板,不划算。
#
#       ⚠ 1.16.5 那条变更日志里写的"bump 本文件当缓存键"已经过期,在原文后加了
#         指向本条的说明,没有改写历史记录。
# 1.20.0 训练执行上线 —— 训练页第一次真的能跑训练(前端 1.14.1)。
#
#       此前训练页只能**看产物**:checkpoint 列得出来,但点不动。本期把
#       `lerobot-train` 真正接上。`app/training/runner.py` 是那个包里**唯一会
#       写盘、起进程**的模块;gpus / datasets / runs / projects 仍全是只读扫描。
#
#       页面新增第四个页签「训练任务」(训练页现在是:新建训练 / 模型训练 /
#       模型推理 / 训练任务)。它与「模型训练」的分工是:**那边列磁盘上的产物,
#       这边列任务** —— 一个失败的任务、或者刚提交还没写出任何 checkpoint 的
#       任务,只会出现在这里。详情面板带实时日志,按字节 offset 增量拉。
#
#       ★ 子进程 detach(start_new_session=True)。一次训练几小时,API 重启
#         (systemd 重启、uvicorn --reload)不该把训练杀掉 —— 那等于白烧几小时
#         GPU。代价是重启后我们不再是它的父进程、**拿不到退出码**,恢复只能靠
#         os.kill(pid, 0) 判断死活:活着继续跟,死了标 interrupted。与
#         upload_queue._recover_jobs() 同构,只是那边恢复"重排队"、这边"重新挂载"。
#
#       ★ 训练日志落**本地盘**(upload_staging_root/train_logs),不落 NAS。
#         作业文档小且写得少,照旧放 state/(与 upload_queue 一致);但日志是
#         子进程持续追加的,20000 步的 tqdm 走 sshfs 边训边写会一直占着网络
#         文件系统 —— 训练最怕这种旁路开销。作业文档里存绝对路径,前端只走
#         端点读,不关心它在哪块盘。
#
#       ★ LeRobot **自己不写日志文件**:checkpoint 目录下只有 pretrained_model/
#         与 training_state/,没有任何 .log/.txt/.jsonl。所以 stdout/stderr 由
#         runner 接管 —— 那份日志是训练失败原因的唯一记录。
#
#       ★ 起训练前要求 ≥16GB 空闲显存。ACT batch=16 实测占用远低于此,留这个
#         余量是为了避开卡上还挂着 vLLM / 旧 worker 的情况(历史上 000008 工作流
#         就是 CUDA OOM 失败的)。不够时直接 400,理由原样弹给用户。
#
#       命令行有**六个坑**。前四个能从 `lerobot-train --help` + 真实
#       train_config.json 静态比出来;后两个只有真跑一遍才会暴露 —— 它们是
#       TrainPipelineConfig.validate() 在导入 torch、扫完数据集**之后**才抛的:
#         ① --eval_freq 不存在(--help 里命中的全是 env_eval_freq 的子串);
#            真正有的是 --eval_steps,且要求 dataset.eval_split > 0 才生效
#         ② --policy.path 不存在。续训是
#            --resume=true --config_path=<checkpoint>/pretrained_model/train_config.json
#         ③ LeRobot 不写训练日志(见上),必须自己接管 stdout/stderr
#         ④ --output_dir 就是 run 目录本身,不会在它下面再拼一层 job_name
#         ⑤ --dataset.repo_id 必填,哪怕数据集就在本地、根本没有 Hub 这回事
#            (填 local/<数据集名>)
#         ⑥ 必须显式 --policy.push_to_hub=false:默认是 true,为 true 时
#            validate() 就要求顶层 repo_id,而真实 train_config 里没有这个键
#         另:⑤⑥ 这类 validate() 错误的报文字面会误导(报的是 `dataset` 整个
#         字段缺失,不是"repo_id 缺失"),照字面查会查错方向 —— 六条连同它们
#         的踩坑过程都写在 runner.py 的模块 docstring 里,⑤⑥ 各有回归断言。
#
#       新增两个配置项:TRAIN_CLI(默认指向 conda 环境 lerobot052_base_piper 里的
#       lerobot-train,刻意**不用 conda run** —— 那会把子进程输出攒起来,进度条
#       就流不出来,页面上是个迟迟不动的黑盒)、TRAIN_KEEP_CHECKPOINTS(默认 1,
#       只留最后一个;一个 checkpoint 约 590MB,中间态只对续训有用)。
#
#       新增端点(训练域里唯一会改状态的五个):POST /jobs、GET /jobs、
#       GET /jobs/{id}、GET /jobs/{id}/log、POST /jobs/{id}/cancel。
#       路由层仍**不 import subprocess、不碰磁盘**,只做 HTTP 那层:ValueError
#       翻 400、FileNotFoundError 翻 404,detail 是可直接展示给用户的中文理由
#       (显卡读不到、数据集不在导出产物里、目录已存在、已有任务在跑……)。
#
#       测试:`tests/test_training_runner.py` 15 条(命令拼装 + 状态机 + 日志增量)。
#
#       顺带:`scripts/check_tailwind_build.sh` 与 tailwind-generated.css 跟上新
#       页签用到的类。
__version__ = "1.20.0"