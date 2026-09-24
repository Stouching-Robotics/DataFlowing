"""后端版本号 —— 发版时只改这一个文件.

约定:
  - 主版本.次版本.修订号 (semver)
  - 修复 bug / 小调整        → 修订号 +1 (0.1.0 → 0.1.1)
  - 新增功能 / 接口变化      → 次版本 +1 (0.1.x → 0.2.0)
  - 不兼容的大改动           → 主版本 +1 (x.0.0)

这个号同时被用作**静态资源的缓存键**(见 app/routes/pages.py 注入的
``asset_version``):改了 JS/CSS 只要 bump 这里,所有模板的 ``?v=`` 一起失效。
不要再逐个模板手写日期串 —— 漏掉一个就会给用户继续发旧脚本
(本次就漏过 exports.html,它加载的还是 202609032200 版 app.js)。

前端版本独立管理, 见 web/workflow-studio/package.json 的 version 字段.
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
__version__ = "1.11.2"