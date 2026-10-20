"""config 包 —— 应用配置、国际化与版本号。

版本号集中定义于此：settings.APP_VERSION 及其他模块均从这里取值。
"""

__version__ = "1.3.17"

# v1.3.17 —— 时间轴：不再谎报「什么时候」。两处改动都会改变落盘数值。
#   ①**hardware_ns 只对设备钟链路重排**：均匀化（「锚点 + 序号 × T」）本来是为
#   D435 修的（`rs2` syncer 补帧/跳号，戳出现 Δ=0 / Δ=2T）；一视同仁地套到
#   **宿主戳**链路（夹爪 RGB / S80M 双目 / 裸手 UVC）上，等于把真实到达时刻换成
#   30fps 网格 —— 锁定的 T 可能有偏（实测一段会话 33.1917ms vs 真实均值
#   33.33ms），叠加 30~38ms 双峰到达后残差是随机游走，`hardware_ns` 在一段内能
#   偏离真实采集时刻 **±100ms**，而 `timestamp` 仍是对的，两者对不上。现在宿主钟
#   帧**原样落盘**（与 `raw_hw_ns` 相同），只有 `extra.ts_src ∈ {1,2}`（D435）
#   才重排；判据 `pipeline._stamp_is_device_clock(extra)`，实现在
#   `core/timestamp_norm.UniformTsClock`（T 取干净间隔均值，按槽位 fps 建标称
#   周期；每槽位一个归一器、每段 `begin_segment()` 重锚）。原始值不丢：同写
#   `raw_hw_ns`；补帧/跳帧事实仍由 `rgb_dup`/`rgb_gap`/`depth_dup`/`depth_gap`
#   记录；落盘空洞看门狗吃归一前的原始戳。
#   ②**`wall_time` = 本段起始墙钟 + 行相对时刻**，不再是落行那一刻的
#   `time.time()`：旧值会带上写队列积压，写线程掉一拍就差整整一行（实测 11 段
#   差 33ms）。掉拍现在记进 `drop_stats`（`write_pacing_resync_count` /
#   `write_pacing_lag_max_ms`），此前完全不可见。
#   两条语义都写进 `meta/info.json` 的 `time_axis`。质检器 `tools/data_qc.py`
#   1.1.1 修 `max_segment_step_ms` 单位（原来 ns 当 ms 报）。
#   测试：tools/tests/{test_uniform_ts_clock, test_capture_timestamp,
#   test_uniform_hw_ts_recording, test_rebuild_hw_ts_from_host}.py；
#   文档：docs/{core,file_format,data,tools}.md 与新增的
#   docs/multi_device_storage.md。
#
# v1.3.16 —— 采集端自建任务（任务页新建 / 改名 / 删除本机任务）
#   ①任务页顶栏「＋ 新建任务」→ TaskCreateDialog（名称 + 目标段数 + 描述），
#   落 data/tasks.json：source="local"、id 前缀 local-、created_by 记创建身份。
#   ②**仅创建者可见**：filter_by_identity 对本机任务按 created_by 过滤（assigned_user
#   恒 None，不算指派任务；created_by 缺失视为无主、所有人可见）；merge_backend_tasks
#   的账号撤销清除显式豁免本机任务 —— 否则账号轮询会把自建任务当「后端撤销」删掉。
#   ③**进度只在本机**：pending_sync_tasks 跳过本机任务（服务端无对应任务，上报
#   必被拒 400）；local_count 累计，synced/backend 恒 0；录制数据照常上传，
#   服务器按任务名匹配/自动创建项目（与 lite 的自由任务名同一后端路径）。
#   ④**名字即目录即上传项目名**：只允许字母/数字/汉字/下划线/连字符，拒绝空格/
#   点/斜杠等非法字符（不做隐式清洗）、去首尾空白后 ≤64 字符、拒绝 Windows 保留名
#   （CON/PRN/AUX/NUL/COM1-9/LPT1-9），大小写不敏感查重（含 hidden 墓碑与种子任务）
#   —— 保证 name == task_tag(name)，不会出现「界面 A、目录 B、上传项目 C」。
#   ⑤任务表 4 列 → 5 列（新增「来源」= 服务端/本机，本机绿、服务端灰）；底部新增
#   「✏ 改名」（仅本机任务；**本机已有录制目录时拒绝** —— 改名不迁移数据，避免
#   旧目录失去归属、上传队列路径脱节；新名字目录已存在也拒绝）；名称 tooltip 显示描述。
#   ⑥删除本机任务 = 真删数据 + tasks.json 整条移除（后端不会再推送它，不需要 hidden
#   墓碑；同名任务可重建）；服务端任务仍走 mark_hidden + 墓碑。
#   ⑦顺手修两处既有问题：任务页「🗑 删除」按钮在选中行后一直是禁用的（从未
#   setEnabled(True)，只能靠右键）；右键菜单删除作用于 currentRow 而非右键行，
#   右键未选中行时会删错任务 —— 现在右键先 setCurrentCell 到该行。
#   测试：tools/tests/test_task_create.py（新增 9 节）+ test_task_delete.py 第 4 节；
#   文档 docs/{data,core,ui,index,tools}.md 与使用手册第 5 章（含 5.4 新建本机任务）。
#
# v1.3.15 —— 姿态语义的文档/注释校正 + 夹爪配对报错分型 + lite 取号跨 chunk
#   ①**修一处数学错误**：protocol.finish_publish_pose 的 docstring 把
#   「M·(R0ᵀ·R)·Mᵀ = (M·R0ᵀ·Mᵀ)·(M·R·Mᵀ)·(M·R0ᵀ·Mᵀ)ᵀ」写成恒等式，代数上不成立
#   （数值差 O(1)）。正确的两因子式是 M·(R0ᵀ·R)·Mᵀ = (M·R0ᵀ·Mᵀ)(M·R·Mᵀ)；左乘
#   与右乘两支之间是**共轭**（左乘版 = P·右乘版·Pᵀ，P = M·R0·Mᵀ，R0/M 皆旋转
#   ⇒ P 正交）。同一主题：README 两处 changelog、process_controller 第 329/857
#   行论证「顺序不敏感」用的**左乘支**恒等式换成**右乘支**（与 protocol 第 537
#   行、测试第 5 节一致）；process_controller 第 1018 行记号 R0ᵀ·R 改 R·R0ᵀ。
#   姿态相对化「新旧数据分开看」的整套说明落在 docs/data.md（新帧已按右乘回填；
#   旧帧〔无 frame 键〕同样是左乘、本次未动，要不要重解释仍**待判定**）。
#   ②**无出处数字清理**：docs/ui.md 与 ui/pose_view_qt.py 里「实测 5000 组随机
#   姿态/顶点，最大偏差 2.2e-16 m」没有任何产出物（全仓只这两行，`git log -S`
#   指向 0ab64d7c 手写加入），真回归是 test_pose_view_qt.py 的
#   check_gripper_model_frame_invariant —— **逐像素**断言（2 个固定姿态、
#   mismatch==0），不是 5000 组、不是米制。该恒等式对任意 R、v 代数上精确成立
#   （MᵀM = I），只改一半的实偏差是 0.3–0.4 m：删掉那个计数，改成代数事实 +
#   测试引用。test_pose_publish_deadband.py 把 protocol.py 定性为「假值」的
#   7.2mm 当成实测上限（同文件 docstring 自相矛盾），改回 1.39–4.98mm；
#   process_controller 第 898 行的「≈1.4°」无出处，改用 protocol.py 的
#   p50 0.25°/max 1.80°。protocol.py 的 guard-interval 注释按作者口径写成
#   「封顶在 1.5× 名义帧周期（= 30ms）」，不再把 cap 本身称作名义周期。
#   ③**夹爪配对报错分型**：_associate_group 原来把「这台没报出 side（Flash
#   空/无效）」与「两台报了同一个左右」挤在同一句「身份不唯一」下 —— 前者会被
#   指成拓扑/端口重复，真因却是厂商产线没写 side（仓库无写路径）。拆成两条
#   抬头，前者带 sightac[i]/side/flash_ok/device_type/serial/error。
#   ④**lite 取号跨 chunk**：_episode_max 原来取「最大文件名 f + 1」，而 f
#   每 1000 回绕 ⇒ chunk-001 出现后返回 chunk-000 里**真实存在**的错段
#   （N=1001 → 得 1），上传/删除静默操作错段。改走 list_task_episodes
#   （N = c*1000 + f + 1），顺带删掉随之未用的 import re。
#   测试：test_device_access_selflock.py 第 8/9/10 节、lite_delete_test.py 第 6 节。
#
# v1.3.14 —— 发布位姿死区（最小可报告位移 3mm）：静止时估计器的毫米级游走
#   （严格静止窗实测：游走半径逐会话 p50 1.06-1.85mm / max 1.39-4.98mm，单帧
#   步长 p99 只有 0.5mm；"只卡首末"的宽口径会给到 7.2mm 的假值，见
#   protocol.py 的定义）不再进
#   轨迹线/末端点/落盘。判据是"距上次**发布**值的累积位移"，与跳变门（比相邻
#   样本）是两件事；不足死区时**发布上次的值**，所以采样节拍、时间戳、
#   slam_trajectory 的逐点契约都不变，只是值冻结。详见
#   core/gripper/slam/protocol.py 的 POSE_PUBLISH_DEADBAND_M 注释与
#   tools/tests/test_pose_publish_deadband.py / test_gripper_pose_deadband_publish.py。
#
# v1.3.13 —— 夹爪 SLAM 的**发布坐标帧**改为 +X=下 / +Y=左 / +Z=前
#   （原 +X=右 / +Y=正对 / +Z=上）。位姿视图、`observation.*slam_trajectory`、
#   `orb_pose.json` 是同一份数值 ⇒ 帧只有一处：发布入口，形态是在"姿态相对化
#   到原点"**之后**再叠一次世界重标（换基矩阵 M = R_y(−90°)·R_z(−90°)，
#   位置 (x,y,z)→(−z,−x,y)，四元数 q_M=(0.5,−0.5,−0.5,0.5) 左右各乘一次）。
#   ★姿态是左右**各乘一次** M·R·Mᵀ，右边那次是**夹爪体轴改名**：体轴从此跟
#   世界轴共用一套词（+X=体下 / +Y=体左 / +Z=体前；旧叫法是 体右/体前/体上）。
#   代价与收益：换锚后第一帧发布姿态 = M·Mᵀ = **单位矩阵**，跟随夹爪的三支箭头
#   与世界三箭头在原点那一瞬**重合** —— 只左乘不右乘时它是常数 M，两套箭头差
#   约 120°，用户判"跟随夹爪的坐标轴还没改"。而夹爪**物理朝向一点没变**：
#   ui/pose_view_qt 按同一张 M 把模型顶点从旧体轴坐标搬到新体轴坐标，
#   position + R·(M·v) == position + R_旧·v，逐像素与改前相同。★两处必须同进
#   同退：只改发布侧不改绘制侧（或反过来）夹爪都会被画歪 120°，而两种改法
#   各自都"看起来自洽"，从画面上看不出是谁漏了。
#   左右都乘之后**顺序不再是契约**：M 在两边相遇处整个约掉，
#   (M·R0·Mᵀ)ᵀ(M·R·Mᵀ) == M·R0ᵀ·R·Mᵀ，"先相对化再重标"与"先重标再相对化"
#   逐元素相同（有回归钉住）。⚠这条恒等式写的是**左乘**那一支（`R0ᵀ·R`，锚点体
#   轴系里的 body delta）—— 相对化从 ≤1.3.12 起一直是左乘（先 process_controller、
#   v1.3.13 起搬到 protocol），它后来被判定**错**（位置/幅度看着正常，但读端按它
#   解会让表观旋转轴整体转错），正确口径是**右乘 `R·R0ᵀ`**（世界系相对姿态），修在
#   v1.3.14。两种乘法只差一个常数共轭，而 `frame` 与 `app_version` **都分不出**
#   （两个构建都是 `down_left_forward`、`__version__` 都写 "1.3.13"）⇒ 别按版本猜。
#   **新帧**数据已按右乘回填（UMI_9_29，逐文件 sha256 记在 MIGRATION.json）；
#   **旧帧**数据（无 frame 键）也是左乘、本次没动，要不要重解释待判定 —— 详见
#   docs/data.md 的「⚠ 姿态相对化的语义改过一次」。右乘同样与重标可交换
#   （(M·R·Mᵀ)(M·R0·Mᵀ)ᵀ == M·R·R0ᵀ·Mᵀ），所以「顺序不是契约」的结论不变。
#   ①位姿视图**不做任何换帧补偿**，发布帧就是它的
#   数据帧 —— gui 坐标系与轨迹系是同一个，换帧后画面随之整体改变
#   （场景相对画布转约 120°，世界轴三箭头屏幕位置不动、含义改成下/左/前）。
#   ⚠v1.3.13 第一版曾在视图入口左乘 V = Mᵀ 求"画面逐像素不变"，那等于把换帧
#   在视图里整个抵消 —— 用户在主程序里看到的还是换帧前的画面，判"坐标系还是
#   没改的"。别再改回去。**帧要写在画面上**：位姿视图标题下第二行是
#   `坐标帧 +X下 +Y左 +Z前`，三支世界轴箭头的标签也各带方向词（X 下 / Y 左 /
#   Z 前，旧约定自动变右/正对/上），方向词从 settings 派生、完整帧名在
#   tooltip —— 因为世界轴三箭头的**屏幕位置**换帧前后逐像素相同（tip 是常数
#   e_i，投影公式不吃数据；实测两帧差 0px），能承载"这一屏画的是哪一帧"的
#   只有文字。这也是"用户怎么验收"的落点：他读界面，不读测试。
#   ⚠坐标系有两套并存的"latest pose"：`_latest_pose` / `read_latest_pose()`
#   原先把 **native** 帧存了进去（`_publish_pose` 只重绑定自己的局部名，
#   frozen dataclass + replace ⇒ 入参对象不会被换帧），而 docstring 自称
#   "dedicated control/GUI channel"。当时零消费者所以没显形，现已改成存发布帧，
#   native 只留在 `_last_accepted_pose`（连续性判据按契约要它）。
#   回退开关 `KSQ_GRIPPER_SLAM_FRAME=right_forward_up`（未设/空串
#   ⇒ 新帧；认不出的值出声后回落新帧）。读端（控制器 / 位姿视图）在构造时
#   解析一次、不逐帧读；写端（egodata_writer）每落一段 episode 解析一次。
#   两处都只是读 environ，进程内取值固定 ⇒ 必然同值；关键是**别做成可中途
#   翻转**的：录制中途换帧会让同一个 episode 混进两种帧而磁盘上分不出。
#   本仓库的帧标注只写 `meta/info.json`：`observation.*slam_trajectory.frame` /
#   `frame_axes` + 顶层 `gripper_slam_frame`（任务根目录另有外部页面写的
#   coordinate_frame.json，不是本仓库产的，别混为一谈）。
#   ⚠它是**任务级**的（features 按并集/后写覆盖合并），一个任务目录跨了改帧时
#   早期 episode 会被标错。★分界是**发布版本**不是日历日：v1.3.12 及更早录的
#   **不写这个键** —— 键不在就等于旧帧，别当默认值读。
#   （老数据一律是旧约定、本次原样留着没动：`data/recordings/UMIGripper_Action_AI`
#   一个任务就 153 个 episode（另有 `9_21_Test` 15 / `D435-裸手…` 6 /
#   `S80C-裸手…` 1，都不是 UMI 夹爪的、也不带这个键），最新一段录于
#   2026-09-21 21:01，**都没有这个键**。新帧那边已有
#   `data/recordings/UMI_9_29`（2026-09-29 录，204 段，app_version 1.3.13、
#   gripper_slam_frame=down_left_forward；其姿态已在 2026-10-12 按右乘口径回填）
#   —— 这条线**已经跨过去了**。）
#   按 episode 精确区分需要往 meta/episodes 加列（本次未加）。
#   C++ 侧一字未动（native 帧没变，变的只是 Python 侧 native→发布帧这一映射），
#   所以无需重编、`libORB_SLAM3.so` 的 md5 也不变。
#   `protocol.rotate_pose_z90`（native→中间帧，2026-09-09 真机实测定案）
#   保持原样，新重标是叠在它上面的一层，不是替换。

# v1.3.11 —— 手套链路整体换厂商 SDK v2.1.0（`tools/glove_sdk/`，取代 fork 的
#   `core/glove_usb`），全栈因此锁到 **Python 3.10**：SDK 的 `algorithm/` 是
#   PyArmor 按 3.10 ABI 加密的，3.11+ 下 `import sdk.api` 直接失败 ⇒ **整条手套
#   链路不可用**（不是"少个骨架"）。四个分发壳的判据改成"**恰好** 3.10"，客户机
#   上 3.12 的 venv 自动重建。修掉四个静默坏：①骨架列**永久空白且零报错** ——
#   SDK 把 warmup 判据换成反极性的 `warming_up`，旧键名恒 False ⇒ `process()`
#   永远返回 None；②左手触觉画面**整体转 90°** —— 左手帧是规范系的
#   `[::-1, ::-1].T`，不是"右手整块行镜像"，现由 `canonical_pressure_matrix()`
#   反变换、两只手共用同一段渲染代码；③第 3 只手套被判成左手并与真左手**同列**
#   互写（删掉 `SENSOR_NAMES[-1]` 兜底，改由调用方拒绝连接；注册表序列号压过
#   历史绑定）；④一键部署的两类 venv 故障。另兜住 SDK 传输层两处缺陷：`stop()`
#   不终结 ⇒ 孤儿读线程永久占 tty（症状"必须重启程序"）、单锁竞争把静默那条流
#   饿死（改单读线程整段持锁 + 有界读超时）。
#   夹爪侧观测性（L0/L1）：采集收尾分段计时、原始流接收停滞看门狗、SLAM 位姿
#   发散审计脚本、力矩阵落盘 schema 契约测试。

# 历史版本一行一条，完整说明见 README 的「更新记录」：
#   v1.3.10 夹爪 RGB 静默空洞四层仪表 + 全库审计脚本（根因＝rig USB 上行掉线）；
#           普通版一键部署自带实时骨架解算与 D435（工具包按白名单裁 2181MB→6MB）
#   v1.3.9  连夹爪时自动把 DECXIN 的曝光/白平衡写回自动（逐台持久化在机身里）；
#           ORB-SLAM 构建源入库；陈旧帧取证定案 H2（旧帧被重投递）
#   v1.3.8  同型号两颗 DECXIN 的 by-id 链接归属会在重枚举时翻转 ⇒ 两颗都退拓扑路径
#   v1.3.7  单插的 DECXIN 在夹爪 rig 插着时从设备面板消失（1bcf:2d4f 一刀切黑名单
#           + by-id 链接被同型号相机顶掉，两层独立根因）
#   v1.3.6  夹爪并入极简版（只录不显）+ SLAM 点带上取样帧的宿主时刻戳
#   v1.3.5  相机服务静默卡死自愈；夹爪扫描配对改走 ESP32 NVS 序列号
#   v1.3.4  新夹爪接入即自动生成运行标定并连接，不再需要人工两步
#   v1.3.3  力矩阵 / 触觉力与 RGB 视频的时间戳对齐（补 capture_ns 稀疏列）
#   v1.3.2  SLAM 陈旧帧入库前拦截；轨迹显示去掉主线程深拷贝
#   v1.3.1  力矩阵落盘规格改 5 档可选，倍率记在每段一份的 meta 行
#   v1.3.0  UMI 夹爪全链接入（Fays S80M 双目 SLAM 位姿/轨迹 + 触觉力 + RGB）
