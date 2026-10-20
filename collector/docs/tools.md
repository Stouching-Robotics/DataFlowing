# tools/

## 定位

`tools/` 是独立工具包集合，与主程序（`ui/`、`core/`）完全解耦：仓库其余代码不 import 它（全仓 grep `import tools`/`from tools` 无命中），它只单向复用 `tools/stereo_s80m/hand_3d/` 与 `tools/hand_detection/` 的既有组件（只读，不改任何现有文件），输出统一落在 `keypoints_output/` 下。

每个入口脚本自带 `sys.path` shim——把仓库根与 `tools/` 插入 `sys.path`，从而能直接 `from hand_3d_d435.*`、`from stereo_s80m.hand_3d.*` 自导入，**无需安装、无需改环境变量**（唯一例外：`tools/glove_package/` 内脚本要求 cwd 在包目录内，见下）。

## 目录总览

| 子目录/文件 | 一句话作用 |
| --- | --- |
| `tools/diag_color.py` | S80M 子进程 pipe 输出一帧的颜色通道诊断（需 `FAYSSENSE_SDK_DIR`） |
| `tools/diag_frame_layout.py` | ctypes 直连 FaysSense SDK 打印标定/帧布局/双目相关性（需 `FAYSSENSE_SDK_DIR`） |
| `tools/diag_frame_trace.py` | 陈旧帧取证：判「只戳错（H1）」还是「旧帧被重投递（H2）」（离线读取证目录，不需相机） |
| `tools/audit_frame_gaps.py` | 帧空洞全库审计：画面复核判「真丢/画面连续/判不了」+ 双钟定性（只读，见「诊断工具」节） |
| `tools/data_qc.py` | 数据集时间轴质检器：五项基础检查（回退/重复/跳变/帧数不齐/时钟偏移）+ **PDF 报告**（`--pdf`），零配置可复用、自带合成自检（只读，见「诊断工具」节） |
| `tools/trim_episode_tail.py` | 裁掉 episode 尾部 N 秒（parquet + meta + 视频一起，默认预演/自动备份），用于尾部模态缺失的对齐（见「诊断工具」节） |
| `tools/make_qc_report.py` | **薄封装**（已并入 `data_qc.py --pdf`），仅保留旧命令可用 |
| `tools/hand_3d_d435/` | D435 RGB-D 单目 3D 手部关键点独立模块（实时 demo + 离线批处理 + 验收探针） |
| `tools/hand_3d_d435/probes/` | 6 个离线验收探针（对齐/一致性/完整性/标签/传播/骨长） |
| `tools/hand_3d_d435/tools/` | 3 个配套小工具（parquet 渲染、交付版 demo 导出、标定提取） |
| `tools/hand_3d_d435/run_live_d435.sh` | 实时 demo 启动器（venv 选择 + 参数透传） |
| `tools/hand_3d_d435/run_d435.sh` | 离线批处理启动器（venv 选择 + 参数透传） |
| `tools/hand_3d_d435/calibration/` | 固化标定 JSON（`.gitignore` 第 56 行忽略，需先跑提取脚本生成） |
| `tools/hand_3d_d435/新手套接入.md` | 新手套（不同纹路/图案）接入验证说明 |
| `tools/glove_package/` | YOLO-World + RTMPose 手套识别/标注/训练自包含工具包（11 个 .py） |
| `tools/glove_package/使用说明.md` | 工具包安装与用法总说明 |
| `tools/glove_package/yolov8m-worldv2.pt` | YOLO-World 检测器权重（默认，被 `hand_3d_d435` 复用） |
| `tools/glove_package/yolo11n.pt` | 检测器训练基座权重（`train_detector.py` 默认 `--model`） |
| `tools/demos/` | 交付版 demo 与自检脚本（S80M 深度引擎、双目 2D、手套关键点、hdf5） | `docs/demos.md` |
| `tools/hand_detection/` | 手部检测：YOLO 手套检测（`best.pt`、`world_detector.py`）+ MediaPipe 裸手管线 | `docs/hand_detection.md` |
| `tools/stereo_s80m/` | S80M 双目工具：标定/三角化/渲染/离线 SLAM 导出（主程序子进程） | `docs/stereo_s80m.md` |
| `tools/hand_3d_s80c/` | S80C 双目实时裸手/手套关键点 demo（含自包含 SDK `third_party/`、`build_dist.sh` 分发包） | 包内 `README.md` |
| `tools/fayssense_depth_sdk/` | FaysSense VI Kit 深度引擎 SDK（C++，专有；S80C demo 自包含用副本） | 包内 `README.md` |
| `tools/models/` | 模型权重：`hand_landmarker.task`（MediaPipe 裸手关键点） | — |
| `tools/import_gripper_calibration.py` | 夹爪出厂标定导入/生成：手工搬运上位机产物，或 `--generate` 就地读设备重生成（v1.3.4） | `docs/core.md` 夹爪节 |
| `tools/tests/` | 16 个回归/冒烟测试（11 离线 + 5 真机，见文末） | `README.md` 测试节 |
| `tools/weights/clip/` | CLIP ViT-B-32 大权重（gitignore，不随仓库分发） | — |

本文档详解覆盖 33 个 `.py` 文件（2 诊断 + 11 手套包 + 11 D435 模块 + 6 探针 + 3 小工具）；`demos/`、`hand_detection/`、`stereo_s80m/`、`hand_3d_s80c/`、`tests/` 等目录的脚本见各自文档。

## hand_3d_d435 模块

D435 RGB-D 单目 3D 手部关键点独立模块（与主程序解耦）。链路两版同源：

- **离线**（`run_pipeline_d435.py`）：RGB(1280×720) MediaPipe 2D（+ 手性投票）→ 原生深度(848×480 mm PNG) 前向对齐抬升 3D（彩色相机系，米）→ 单目槽位分配 → `HandSlotTracker` 遮挡传播 → tracker 前向+后向填充 + `offline_smooth` → 旋转渲染 + RGB 叠显 + parquet。
- **实时**（`live_demo.py`）：同上，仅把批处理段换成 αβ + OneEuro 在线平滑链，并加 M1/M3/M5/M6 展示路径稳定性修正（见下）。

### 子模块文件详解

#### `__init__.py`

**作用**：包说明文档串，无代码。声明模块只复用 `tools/stereo_s80m/hand_3d/` 组件（detector/identity/track3d/postprocess/renderer_3d/video_writer/io），不改任何现有文件，输出落在 `keypoints_output/`。

#### `depth_align.py`

**作用**：深度→彩色离线前向对齐 + 关键点深度采样。采集链路（`core/d435_camera.py`）不做 `rs2.align`，本模块逐深度像素投影：`P_d = ray·Z_d` → `P_c = R·P_d + t` → 彩色投影，z-buffer 保最近（0=无效哨兵），再空穴回填（848×480→1280×720 是 ~2.12× 上采样，rint 跳列跳行留 ~50% 空穴，3×3 min 邻域 3 轮回填，只写空穴不腐蚀有效值）。

公开接口：

| 接口 | 说明 |
| --- | --- |
| `load_session_depth_intr(session_dir)` | 读录制期 `calibration/head_stereo.json` → 深度内参 dict（fx/fy/cx/cy/width/height），失败返回 None |
| `load_calib(calib_path=None)` | 读固化标定 JSON（默认 `tools/hand_3d_d435/calibration/d435_color_calib.json`），缺失抛 `FileNotFoundError` 并提示先跑提取脚本 |
| `DepthAligner(color_intr, depth_to_color, depth_intr)` | 构造时预计算深度侧单位射线与 R/t（t 米→毫米） |
| `DepthAligner.align_depth_to_color(depth_mm)` | (dh,dw) uint16 毫米深度 → (ch,cw) float32 aligned 深度（0=无效） |
| `DepthAligner.sample_points(aligned, uv, band=None)` | (N,2) 像素坐标 → (N,) 深度 mm；3×3 窗口取中位（有效 ≥2 才出数），`band=(z_lo,z_hi)` 时先按深度带过滤（手缘点抗背景混入） |

**关键数据**：`_MAX_DEPTH_MM = 8000.0`（远背景离群上限）、`_FX_REL_TOL = 0.01`（深度内参交叉核对容差）。`python depth_align.py` 可直接跑合成自测（恒等外参逐点精确、t_x=25mm 平移、中位抗离群、回填不腐蚀）。

**调用关系**：被 `lift3d.py`、`replay_compat.py`、`run_pipeline_d435.py`、`live_demo.py`、`probe_align_overlay.py`、`probe_live_consistency.py` 导入。

#### `lift3d.py`

**作用**：2D `DetectedHand` + aligned 深度 → 彩色相机系 3D（米）。`LiftResult`/`D435Pair` 两个 dataclass mimic 双目管线的 `RefinedPair` 接口，使 `io.pack_3d`/`pack_errors`/`pack_stage2` 零改动可消费。`mean_error` 恒 NaN（单目无重投影概念），真实质量信号走旁挂 metrics。

| 接口 | 说明 |
| --- | --- |
| `LiftResult` | `points_3d`(21,3 米，NaN=无效)、`mean_error`（恒 NaN）、`valid_count`（实测有效点数） |
| `D435Pair` | `result`、`left_label`、`used`、`hand2d`(21,2)、`n_valid`、`det`、`measured`(21, bool：z 来自实测，False=补点) |
| `lift_hand(hand, aligner, aligned, band=True, complete=True)` | 深度采样抬升；`band`=两遍带约束采样（手深中位 zc ± 0.12m 带内取中位）；`complete`=缺失点补到 zc、x,y 按 2D 反投影（"保持 z、调 x,y"） |
| `apply_slot_zc(pair, zc, aligner)` | M5：补点深度锚定到槽级稳定 zc，只动 `measured=False` 的补点，实测点不动 |
| `gate_observations(pts, pred, gate=0.15, wholesale_frac=0.6)` | 时序一致性门：|观测−预测| > 0.15m 的点置 NaN（tracker 对 NaN 走纯预测）；可疑点 ≥60% 时返回 `(原样, True)` 触发槽位重置 |

**关键数据**：`BAND_HALF_M = 0.12`、`BAND_MIN_VALID = 4`（第一遍有效点不足退无约束）、`GATE_M = 0.15`。

**调用关系**：被 `fill_track.py`（`gate_observations`）、`mono_assign.py`（自测）、`run_pipeline_d435.py`、`live_demo.py`、`probe_live_consistency.py` 导入。

#### `mono_assign.py`

**作用**：单目双手槽位分配（`hand_0`/`hand_1` 连续身份），仿双目 `_best_slot_for` 的决策层级：

1. 冷启动：标签惯例 Left→slot0 / Right→slot1，无标签按检测序号；
2. 标签唯一命中存活槽 + 几何门（≤0.15m）；
2b. 标签唯一命中困境槽（上帧无真手）→ 不设几何门（恒速外推会漂移，label 是唯一可靠信号）；
3. 贪心几何：剩余手入最近未占用存活槽（质心 ≥4 有效点，不足退 2D 判据；label 冲突守卫）；
4. 互斥守卫：双真实且腕距 <0.10m → 两种排列取总 cost 更小者（须严格优于 ≥5mm 防抖动）；
5. 未见槽冷启（label 惯例/无标签单死槽兜底）；
6. 兜底丢弃。

| 接口 | 说明 |
| --- | --- |
| `assign_mono_slots(pairs, tracker, n, color_intr, lost_counts=(0,0), debug=False)` | `list[D435Pair]`(≤2) → `[slot0_pair|None, slot1_pair|None]`；`lost_counts` 供 2b 规则用 |
| `_cost(pair, slot_pred, color_intr)` | 3D 质心距槽预测（米）；质心不可靠退 2D 判据（预测投影 vs 2D 质心，像素距 ×Z/fx） |
| `_dbg(n, labels, pred, out, evt)` | `HAND3D_SLOT_DEBUG` 环境变量指向文件时追加决策日志（`atexit` 落盘） |

**关键数据**：`UNRELIABLE_GATE = 0.15`、`WRIST_MUTEX = 0.10`、`SWAP_MARGIN = 0.005`、`MIN_VALID_PTS = 4`。`python tools/hand_3d_d435/mono_assign.py` 可跑 7 项构造轨迹自测。

**调用关系**：import `stereo_s80m.hand_3d.track3d.HandSlotTracker`；被 `run_pipeline_d435.py`、`live_demo.py`、`probe_live_consistency.py` 调用。

#### `fill_track.py`

**作用**：离线缺失帧槽的 tracker 前向+后向填充（替代 `fill_gaps` 短桥接）。用与实时版同源的 `HandSlotTracker`（αβ、逐点 NaN 保持纯预测）沿时间轴前向跑一遍（真实观测 observe、缺失帧 predict），再反向跑一遍补首观测前的头部段；合并时真实观测优先、前向预测次之、后向兜底。轨迹是恒速外推，比线性插值更物理；门控与实时版一致（`gate_observations`）。`fill_gaps` 语义保留：被填帧 `present=True`、`propagated=True`、label 取最近 present 帧；从未见过的槽不幻觉 present。

| 接口 | 说明 |
| --- | --- |
| `tracker_fill(rows, max_lost=15)` | 原地修改 rows（parquet 行字典列表），返回填充帧-槽数 |
| `_forward_pass(h3, present, prop, labels, max_lost)` | 单方向 tracker 填充 → (N,2,21,3) 状态轨迹 |

**调用关系**：被 `run_pipeline_d435.py` 调用（`--propagate-max` 透传 max_lost）；自测在 `__main__`。

#### `render_overlay.py`

**作用**：D435 RGB 单视叠加渲染：2D 骨架（分色，复用 `tools/hand_detection` 的 `FINGERS` 色表 + 自含 `PALM_CONNECTIONS`）+ 逐关键点平滑深度 mm 标注 + 槽位 label/propagated HUD；另含伪彩深度叠层。

| 接口 | 说明 |
| --- | --- |
| `draw_overlay(rgb, hands2d, hands3d, labels, propagated, presents, frame_idx, total, title=...)` | 返回叠加帧（拷贝，不改原图）；absent 槽不画 |
| `blend_depth(rgb, aligned_mm, alpha=0.4)` | 伪彩深度叠层：300-1200mm 归一 JET（无效=不叠），α 混合回 BGR |
| `PALM_CONNECTIONS` / `FINGER_CHAINS` | 掌心连接（腕-掌子集）与每指画法链（拇指 1→2→3→4，其余 0→MCP→…→指尖） |

**调用关系**：被 `run_pipeline_d435.py`、`live_demo.py`、`probe_align_overlay.py` 导入。

#### `replay_compat.py`

**作用**：录制会话布局/标定兼容层（`live_demo --replay` 用）。自动探测三类会话：新会话（槽名随 GUI 用户命名 → `videos/D435_depth_rgb/`、`depth/D435_depth/`，`head_stereo.json` 可能是 D405 内参）、旧会话（`videos/d435_rgb` + `depth/d435_depth` + `head_stereo.json` 即 D435 内参）、双 RealSense 并存会话。

| 接口 | 说明 |
| --- | --- |
| `find_video_any(session)` | 依次尝试小写/大写槽名的 RGB 视频（兼容 `chunk-0000/` 布局） |
| `find_depth_dir(session)` | 依次尝试小写/大写槽名的深度 PNG 目录 |
| `load_session_depth_intr_any(session)` | head_stereo 深度内参；两文件并存且 fx 差 >1% 时改读 `D435_depth_rgb_calibration.json`（head_stereo 记录的"最后写入"设备不可靠） |

#### `glove_detector.py`

**作用**：黑手套检测器（YOLO-World 手套框 + RTMPose hand5 21 点 + 帧间稳定层）。检测契约对齐 `DetectedHand`（landmarks (21,2)、MediaPipe 拓扑 0=腕、label、score=框 conf），下游 voter/抬升/槽位/平滑/渲染零改动。

| 接口 | 说明 |
| --- | --- |
| `GloveDetector(weights=None, det_conf=None, ...)` | 权重默认 `tools/glove_package/yolov8m-worldv2.pt`；文件名含 "world" 走 world 后端（conf 0.05），否则普通 YOLO（`best.pt` 回退开关，conf 0.3）；惰性加载（ultralytics/torch/rtmlib 首次构造才 import，裸手启动零开销） |
| `detect(frame_bgr)` | → `list[DetectedHand]`，按 track_id 稳定序输出 |
| `reset()` / `close()` | 清空全部帧间状态 / 释放模型 |

**关键数据（帧间稳定层）**：`HandTracker` 贪心 IoU 匹配 + 框 EMA(α=0.7) + 运动门控 3px（静止帧免 RTMPose 推理、0.33s 强制刷新）+ 丢框持 3 帧；逐点 `OneEuroFilter2D`（freq_min=5.0/beta=0.05/dcutoff=1.0，归一化坐标域，键 (track_id, 点序号)）；退化族过滤（框外点 ≥16 / 唯一点 <15 / span <0.2×框对角线）；per-track 手性锁存（连续 3 票相同锁死；双手框中心距 <0.05×max(w,h) 冻结，镜像 voter latch）；`pose_conf_thr`（默认 0.3）持出低置信骨架；复活继承 `_REVIVE_MAX=90` 帧 / `_REVIVE_DIST=0.3×max(w,h)`（死亡 track 的 OneEuro/锁存/冻结缓冲 re-key 给新 track，消重捕捉闪烁）。

**调用关系**：只读复用 `stereo_s80m.hand_3d.detector.DetectedHand`、`hand_detection.hand_pipeline_mediapipe.OneEuroFilter2D`、`glove_package` 的 `world_detector` 与 `hand_tracker`（shim 把 tools/、glove_package、tools/hand_detection 三目录都插入 sys.path）；被 `live_demo.py` 调用（按 g 键热切换）。

#### `pose_backends.py`

**作用**：姿态关键点后端（黑手套链），把 RTMPose / MediaPipe 两个后端统一到同一契约，`GloveDetector` 只依赖契约、后端可热切换。契约：`backend(frame_bgr, bboxes=None) -> (kpts, scores)`——`kpts` (M,21,2) 全图像素坐标（21 点 MediaPipe 拓扑 0=腕，与 RTMPose hand5 同序，手性几何判据直接可用），全局失败返回 None；`scores` (M,21) 或 None（RTMPose=SimCC 逐点响应、MediaPipe=逐点 visibility，下游只取每手均值对照 pose_conf 门）；`backend.close()` 释放（重建前必调，幂等）；`backend.device` 实际推理设备。

| 接口 | 说明 |
| --- | --- |
| `RtmposePoseBackend(device)` | RTMPose hand5（onnxruntime，SIMCC 256×256）；首次构造模型未缓存时自动从 openmmlab 下载；CUDA EP 初始化失败自动回退 CPU |
| `MediaPipePoseBackend(model_path=None, device)` | HandLandmarker Tasks API（venv mediapipe 1.0.0 无 legacy `mp.solutions`，必须 `from mediapipe.tasks import python`）；默认模型 `tools/models/hand_landmarker.task`；IMAGE 模式整图 `num_hands=2` 检测 + 质心就近关联到入参 bboxes——实测（000005）框外扩 1.25 裁剪喂入 0/5 检出、整图 5/5 检出，输出仍按入参框顺序每框一行，未关联框吐零行 |

**关键数据**：`_MP_PAD = 1.25`（裁剪外扩，与 RTMPose bbox padding 同口径）、`_MP_MIN_CONF = 0.3`（检测/存在阈值放宽，由下游退化过滤 + pose_conf 门兜底保召回）。

**调用关系**：被 `glove_detector.py` 使用（后端热切换）；MediaPipe 后端对黑手套检出率低（整图 5/60 帧 vs world 每帧出框），主要供裸手/效果对比。

#### `run_pipeline_d435.py`

**作用**：离线批处理入口。用法 `./tools/hand_3d_d435/run_d435.sh <session_dir> [选项]`；产物（`keypoints_output/<tag>/<session>/`）：`d435_hand_3d_rotating.mp4`、`d435_rgb_overlay.mp4`、`hand_3d_refined/chunk-000.parquet`、`d435_metrics.csv`（`--track-debug` 追加 `track_events.csv`）。

参数：`--out-dir`、`--calib`、`--mp-delegate`（cpu/gpu，默认 cpu）、`--det-conf`（0.5）、`--track-conf`（0.5）、`--propagate-max`（15，槽位丢失硬顶）、`--depth-overlay`（overlay 视频叠伪彩深度）、`--video-encoder`、`--no-video`、`--no-parquet`、`--track-debug`。

**关键流程**：空帧不喂 voter（identity.py 空帧会清轨迹 → 重建期同 label 闪烁）；`gate_observations` wholesale 时借 `"\x00reset"` 标签触发槽位重置；收尾 `tracker_fill` + `offline_smooth(sg_window=7, sg_poly=3, v0=0.08, fps, still_window=21)`；parquet 列 schema 与双目管线同构（`pack_3d`/`pack_errors`/`pack_stage2`）。汇总打印每槽 present/propagated 比例、label 翻转数、腕位移 p50/p95、腕→中指 MCP 骨长。

内部函数：`_load_episode_task`（pyarrow 直读 episodes parquet + tasks.jsonl）、`_pack_2d_slots`、`_render_videos`、`_summarize`、`_write_metrics`、`_nan_pair`/`_pred_pair` 占位构造。

**调用关系**：import `stereo_s80m.hand_3d` 的 detector/identity/track3d/postprocess/renderer_3d/video_writer/io + 本模块其余全部子模块。

#### `live_demo.py`

**作用**：实时 demo 入口（模块内 1415 行的主体）。三窗口：`D435 live: RGB overlay`（骨架+深度标注+HUD）、`D435 live: 3D view`（3D 骨架，左键拖拽环绕/俯仰、`r` 复位；输入平移到首帧锁定的世界锚点，不拖拽时视角/网格/缩放完全静止）、`D435 live: depth`（aligned 深度 0.3-1.5m 伪彩）。按键：`q`/ESC 退出、`s` 截图（`keypoints_output/live_d435/`，三窗口各一张）、`d` 深度伪彩叠层切换、`g` 裸手/黑手套热切换（切回裸手重建 MediaPipeDetector 实例——`det.reset()` 只 close landmarker 不重建，实机踩过）、`r` 复位视角。

输入源三类：`LiveD435Source`（pyrealsense2 直开，rgb8 1280×720@30 + z16 848×480@30；流组合失败自动回退 1280×720 双流、848×480@60 双流——D405 深度/彩色须同分辨率；对齐用**本机实时内参/外参**，换设备自洽；设备独占，主程序预览开启时 EBUSY）、`ReplaySource`（`--replay <session_dir>`，按录制 fps 步调回放，经 `replay_compat` 兼容新旧布局）、`UvcSource`（`--uvc`：设备号/路径/URL，无深度 → 2D-only 单窗口模式，只产 2D 关键点渲染）。

主要参数：`--replay` / `--replay-pace`（30）、`--uvc`、`--calib`、`--delegate`（auto=GPU 子进程冒烟成功则 GPU）、`--fill`（默认 1 轮填洞：覆盖 91.6% vs 3 轮 94.0%，省 20ms/帧）、`--propagate-max`（15）、`--det-conf`/`--track-conf`（0.4）、`--glove` 及 `--glove-weights/-det-conf/-pose-conf/-nms-iou/-lost-timeout/-box-alpha/-freeze-max`、`--rs-serial`、`--exposure`（0=自动，运动模糊丢手时试 4000-10000µs）、`--stats`、`--depth-overlay`、`--no-window`、`--export`。`--export` 落盘四件套：`keypoints_2d.parquet`（xy 定长 float32[42]，像素）、`keypoints_3d.parquet`（xyz float32[63]，质心锚定 3D 米）、`render.mp4`、`rgb_overlay.mp4`（H.264+faststart，venv cv2 只能写 mp4v 故走共享 sink）。

展示路径稳定性修正（不回灌 tracker）：**M5** 补点深度锚定槽级 zc EMA（换手/首帧取实测，否则 0.5 混合，`apply_slot_zc`）；**M3①** `_SoftSmoother` 包装 `Hand3DSmoother`（label 变化/空→有重建且几何近 <0.1m 时 0.5 混合软衔接防 snap）；**M3②** wholesale 两帧确认（`_ws_agree` tol=0.30，或连续 ≥3 帧强制采信，单帧跳变只走预测显示）；**M1** `_CentroidAnchor` 质心锚定（每槽有效点中位质心走强 OneEuroFilter3D(3.0, 0.3, 0.3)，输出=输入+(ĉ−c)，共模平移在质心层吸收，手内形状原样保留）；**M6** 门控锁死豁免（逐点连续被门控 ≥5 帧且观测恢复有限 → 采信观测，防"预测外推越走越远永不恢复"）。

其他守卫：空帧不喂 voter；两手同 label（voter 重建期）先清空 label 走几何分配；手套模式整体跳过 voter（identity.py 新轨迹同帧双分配缺陷，实测同 label 锁死），身份由 `GloveDetector` per-track 锁存承担。

内部类/函数：`LiveAligner`、`LiveD435Source`、`ReplaySource`、`UvcSource`、`_OrbitControl`、`_SoftSmoother`、`_CentroidAnchor`、`_run_uvc_2d`、`_run_3d_chain`、`_ws_agree`、`_resolve_delegate`、`_write_export`/`_write_export_2d`、`_exp_meta`。

#### `tools/` 子目录（parquet 渲染 / 交付版导出 / 标定提取）

- `tools/hand_3d_d435/tools/render_keypoints_parquet.py`：任意 21 点 3D 关键点 parquet（列名同 `io.pack_3d`：`observation.keypoints.hand_3d`/`hand_3d_smoothed`）→ 3D 视角渲染视频。零相机依赖（不 import pyrealsense2/mediapipe，仅 numpy+cv2+pyarrow，`--smooth` 才需 scipy）。选项：`--col`（hand_3d/hand_3d_smoothed）、`--view`（rotating/static）、`--revolutions`、`--yaw`/`--elev`、`--smooth`（渲染前跑 `offline_smooth` 压抖）、`--scale`/`--shift`（尺度修正/平移）、`--frame N`（单帧 PNG 预览）、`--fps`、`--encoder`；支持多文件通配拼接、(N,2,21,3) 与 (N,21,3) 两种展平、`frame_index` 乱序保险；启动时打印数据体检（腕位移/骨长，骨长明显偏小提示 `--scale`）。
- `tools/hand_3d_d435/tools/demo_export_parquet.py`：跑交付版 `dist/d435_hands_demo_v1.1/d435_hands_demo.py` 并以**钩子**方式抓取每帧数据落 parquet（不复制管线代码、不改交付目录）：`install_hooks` 在 `_SoftSmoother.update`（抓槽位原始 3D/平滑 3D）与 `draw_overlay`（抓 2D/labels/present，`frame_idx-1` 收口成行）上挂钩；`Collector` 攒行，schema 复用 `tools/stereo_s80m/hand_3d/io.py` 的 `write_parquet`，与 `run_pipeline_d435.py` 产物同构、现有 probes 可直接读。用法：`venv/bin/python tools/hand_3d_d435/tools/demo_export_parquet.py <会话目录> --out-dir <dir>`；输出 demo 原三路视频 + `hand_3d_refined/chunk-000.parquet`。
- `tools/hand_3d_d435/tools/extract_d435_color_calib.py`：从真机 D435 提取彩色内参 + 深度内参 + depth→color 外参，固化到 `tools/hand_3d_d435/calibration/d435_color_calib.json`（模块离线对齐的唯一标定来源；外参语义 `P_color = R·P_depth + t`，t 单位米，D435 典型 |t|≈25mm）。必须用 venv python（pyrealsense2 只装在 venv）；`--session` 时与录制 `head_stereo.json` 深度内参交叉核对（差 >1% 告警"可能不是同一台设备录制"）；自带防呆（t 范数不在 [10,60]mm 告警、设备被主程序独占时报 busy）。产物含 serial/firmware 字段（运行时值，文档不转载）。

### probes/ 探针（合表）

全部用 venv python 运行，输入 parquet 的用 `run_pipeline_d435.py` 产物。

| 文件名 | 用途 | 输入 | 输出 |
| --- | --- | --- | --- |
| `probe_align_overlay.py` | 深度↔RGB 对齐验收：覆盖率 + 手部深度一致性 A/B（正确 vs 反号外参）+ 边缘距离（仅信息，实测噪声底之上不作判据） | `--session` 会话目录（+ `--calib`、`--frames` 抽样帧号） | `probe_align/` 下 `frame_XXX_align.png`（伪彩叠层）/`frame_XXX_edges.png`（深度边叠层）+ 覆盖率中位 ≥60% 与 手散布 ≤200mm 且 ≤0.8×反号 的 PASS/FAIL |
| `probe_live_consistency.py` | 重跑 live_demo 在线链（1 轮填洞 + αβ + Hand3DSmoother）与离线 parquet 逐帧比对 | `--session`（离线产物需先存在） | 两者都有限的腕点（点 0）深度差：中位 <10mm、p95 <30mm 判 PASS/FAIL |
| `probe_3d_completeness.py` | 3D 输出完整性与翻面事件（旋转渲染质量直接判据） | `--parquet`（读 `hand_3d_smoothed`） | 整手 21 点全有限帧率 ≥50%、翻面事件（相邻帧 z 跳 >300mm）≤2 起判 PASS/FAIL；另报有效点中位、腕点有限率 |
| `probe_label_stability.py` | 逐槽 label 翻转计数 | `--parquet` | 全片 0 次翻转（连续 present 帧间 Left↔Right）判 PASS/FAIL；退出码 0/1 |
| `probe_propagation.py` | propagated 比例 + absent 缺口直方图 | `--parquet` | 每槽 propagated <15%、absent 缺口全 ≤15 帧判 PASS/FAIL；另报硬 absent（3D 全 NaN 未幻觉）帧数；退出码 0/1 |
| `probe_bone_lengths.py` | 腕→中指 MCP 骨长自检（标定无关硬判据，剔除 propagated 帧） | `--parquet`（读 `hand_3d` 原始值） | 每槽中位 ∈[72,95]mm、IQR<25mm、<5% 帧出 [55,115]mm、两槽中位差 <10mm 判 PASS/FAIL；退出码 0/1 |

### launcher 脚本说明

- `tools/hand_3d_d435/run_live_d435.sh`：实时 demo 启动器。`VENV_PY` 环境变量可覆盖解释器（默认 `$REPO_ROOT/venv/bin/python`，即 collector/venv，含 pyrealsense2 2.58.3）；脚本自动 `cd` 回仓库根后 `exec "$VENV_PY" tools/hand_3d_d435/live_demo.py "$@"`——全部参数原样透传给 `live_demo.py`。例：`./tools/hand_3d_d435/run_live_d435.sh`（直连相机）、`... --replay data/recordings/222/222_000011`（回放）。
- `tools/hand_3d_d435/run_d435.sh`：离线批处理启动器，同款 `VENV_PY`/`cd` 逻辑，`exec` 到 `run_pipeline_d435.py`。用法 `./tools/hand_3d_d435/run_d435.sh <session_dir> [选项...]`。

## glove_package 工具包

YOLO-World 检测 + RTMPose 姿态的自包含工具箱（11 个 .py），专为戴手套场景优化（MediaPipe 在手套上完全失效，本方案实测检出 40/40）。**独立运行特性**：

- **cwd 必须在包目录内**：包内脚本用顶层裸导入（`import hand_common` / `import world_detector` / `from world_detector import iou`）互引，不挂 sys.path shim；默认模型路径 `MODEL_PATH` 以包目录 `_HERE` 锚定（`yolov8m-worldv2.pt`）。从包外 `python tools/glove_package/xxx.py` 会 ImportError——需 `cd tools/glove_package` 后再运行（`hand_3d_d435/glove_detector.py` 则不受此限：它自己把 `glove_package` 插进 sys.path）。
- **自包含**：不依赖 mediapipe（`hand_common` 刻意不 import 父目录）；依赖见 `requirements.txt`（torch/torchvision 由 `setup.bat` 装 CUDA 版，numpy、opencv-python、onnxruntime、rtmlib、ultralytics、openai-clip）；首次运行联网下载模型约 450MB（YOLO-World 权重 ~55MB + RTMPose hand5 ~56MB 缓存到 `~/.cache/rtmlib/` + CLIP 文本编码权重约 338MB）。
- **训练产物在包内 `runs/`**：`train_detector.py` 把相对 `--project` 锚定到包目录（ultralytics 会把相对路径解析到全局 settings 的 runs_dir，产物会落到包外），产出 `runs/hand_det/weights/best.pt`。`dataset/`、`captures1/`、`to_label/`、`verify_pose_out/` 等数据目录也在包内。

### 各文件

#### `world_detector.py`

**作用**：YOLO-World 开放词汇检测器——`auto_label.py` 和 `infer.py` 的唯一真值来源（标注框与推理框同一套配置）。

| 接口 | 说明 |
| --- | --- |
| `WorldDetector(model, prompt, imgsz, device, nms_iou, use_onnx)` | `.pt` 走 `YOLOWorld` + `set_classes(prompt)`；`.onnx` 走 `YOLO`（ONNX GPU 不可用——缺系统 CUDA Toolkit，强制 CPU） |
| `WorldDetector.__call__(src, conf, max_boxes, wh, reuse_boxes)` | BGR 帧 → `(boxes_xyxy [N,4], confs [N,])`，已过滤 + 两级 NMS + Top-N；`reuse_boxes` 帧跳过缓存 |
| `WorldDetector.postprocess(boxes, confs, w, h, max_boxes, nms_iou)` | 裁画面 + 丢非法/过小框（8px）+ 两级 NMS（中心距 <框宽 15% 且 IoU>nms_iou 判同一只手；中心有偏移只用 >0.85 极高阈值——解决"两只手靠近误删一只"）+ 按置信度 Top-N |
| `iou(a, b)` / `_center_close(a, b)` | 框 IoU / 中心距离判据（被 `hand_tracker.py`、`glove_detector.py` 复用） |
| `add_args(ap, max_boxes_default)` / `from_args(args, device)` | 给 argparse 挂公共参数（`--weights/--prompt/--imgsz/--conf/--max-boxes/--det-skip`），保证各脚本命令行一致 |

**关键数据（默认值）**：`DEFAULT_MODEL = "yolov8m-worldv2.pt"`、`DEFAULT_PROMPT = ["hand","glove"]`、`DEFAULT_IMGSZ = 320`（手占比大时比 640 更准更快）、`DEFAULT_CONF = 0.05`、`DEFAULT_NMS_IOU = 0.6`。

#### `hand_tracker.py`

**作用**：手部身份追踪——贪心 IoU 跨帧框匹配 + 稳定 track_id，解决"框按置信度排序帧间交换 → 关键点粘连"、"丢一帧就清状态 → ID 跳变"。`HandTrack` dataclass 记录框（EMA 平滑）、`last_good_kpts` 逐点冻结缓存、skip/lost 计数。

| 接口 | 说明 |
| --- | --- |
| `HandTracker(max_hands=2, iou_match_thr=0.3, lost_timeout=3, movement_thresh=3.0, skip_timeout=10, box_smooth_alpha=0.7)` | 每帧先 `update_detections`（贪心匹配 + 老化 + 清理超时 track + 限活跃数） |
| `get_boxes_for_pose()` | 只在运动超阈值/新 track/skip 超时（静止 10 帧强制刷新）时返回需要推理的框 |
| `update_pose_results(indices, kpts, scores)` | RTMPose 结果写回对应 track，记推理时框中心 |
| `get_results()` | → (boxes, kpts, scores, track_ids)；未出过 pose 的 track 吐 (21,2) 零数组（下游注意：`hand_3d_d435/glove_detector.py` 因此额外维护 `_posed` 集合过滤占位） |
| `update_last_good(track_index, point_index, value)` / `get_last_good(...)` | 逐点冻结缓存读写；**注意 `track_index` 是活跃列表下标**（与 `get_boxes_for_pose` 返回的 `self.tracks` 下标不一致，`glove_detector.py` 因此直接改 `track.last_good_kpts` 绕开） |
| `clear()` / `hand_count` | 重置 / 活跃手数 |

#### `hand_common.py`

**作用**：RTMPose 方案共用的关键点定义与绘制（21 点与 MediaPipe 同序：0=腕、1-4 拇指、5-8 食指、9-12 中指、13-16 无名指、17-20 小指）。

| 接口 | 说明 |
| --- | --- |
| `FINGERS` / `PALM_CONNECTIONS` / `JOINT_SPECS` | 五指 id 与 BGR 色表 / 掌心连接 / 关节表（拇指 CMC/MCP/IP，其余 MCP/PIP/DIP） |
| `angle_between()` / `compute_joint_angles(pts)` | 关节角（度，图像平面算——手指透视缩短时偏小） |
| `count_extended_fingers(angles)` | 伸直判定：拇指 MCP>145° 且 IP>150°；其余 PIP>150° 且 DIP>140° |
| `draw_hand(frame, pts, angles, kpt_scores, thr)` | 骨架+关节角绘制，低于 `thr` 的点画空心 |
| `draw_panel(frame, x, y, lines, width)` | 半透明信息面板 |
| `build_pose(device)` | RTMPose hand5（onnxruntime，首次自动下载 ~56MB） |
| `auto_device()` | CUDA > MPS > CPU |
| `RSCapture(serial, width, height)` | pyrealsense2 颜色流包装，接口对齐 `cv2.VideoCapture`（懒加载） |
| `pose_is_glove(kpts, scores, box, ...)` | 误检抑制：kpt 均值 ≥0.45、≥0.3 的高置信点 ≥15、点团 span ≥0.3×框对角线，三条全过才算真手套 |

#### `hand_demo_mmpose.py`

**作用**：摄像头实时手指/关节识别完整 demo（YOLO-World + RTMPose + HandTracker + 逐点冻结 + 数据采集）。快捷键：`q`/ESC 退出、`a` 关节角度、`s` 骨架、`b` 检测框、`h` 帮助、`i` UI 面板、空格暂停、`c` 抓拍、`r` 连续采集、`m` 只存漏检帧（最需要的难样本）。采集目录 `captures1/<时间戳>/`（`session.json` + `frames/000001.jpg` + `labels.jsonl` 预标注——存的是镜像后未画骨架的干净原图）。UVC 源做左右镜像 + 上下翻转（倒装校正），`--realsense` 不翻。遮挡处理：低置信点占比 ≥90% 整手冻结，否则按手指粒度冻结（伸直指/腕/高置信点才更新 `last_good`，阈值 0.2）。检测器可选自训权重（`YOLOWrapper`）；`wd.add_args` 挂载 `--weights`（默认锚定包目录）/`--prompt`/`--imgsz`/`--conf`/`--max-boxes`/`--det-skip`。另有 `--output` 出视频、`--data-out` 出逐帧检测 JSONL。

#### `infer.py`

**作用**：RTMPose 实时推理精简版。`--source` 支持摄像头序号/图片目录/视频文件，`--realsense` 走 D435 颜色流；`--detector` 可选 `world`（默认）/`rtmdet`（官方 RTMDet-nano，黑手套上失效，仅基线对照）/自训 `.pt` 路径。含 `--det-skip` 帧跳过（复用上一帧框，CPU 加速）。同一套误检抑制（`pose_is_glove` 连续 2 帧确认）+ 遮挡/逐点冻结（阈值 0.3）。快捷键同 demo 子集：`q`/ESC、`a`、`s`、`b`、`h`、空格。

#### `annotate.py`

**作用**：极简手部框人工标注工具（不用装任何标注软件）。`--dir`（默认 `to_label`）下 `images/` → 同级 `labels/`，保持子目录结构，输出 YOLO 格式（单类别 class id=0）。操作：左键拖拽画框、`u` 撤销、`c` 清空、`d`/空格/→ 下一张、`a`/← 上一张、`g` 跳到第一张未标注的、`q`/ESC 退出（自动保存）。**无框也写空文件**——标记"这张我看过了"，`prepare_dataset.py --merge` 会跳过空文件，无手图不会被当负样本混进训练集。`--max-width` 限制显示宽度（超宽图缩放显示、坐标仍按原图写）。标注要点（文件头）：框住整只手（指尖到手腕含袖口），偏大 10~30% 没问题。

#### `auto_label.py`

**作用**：手部框自动标注（零样本 / 自举双模式）。零样本=YOLO-World 文本提示直接检（默认），自举=`--model best.pt` 用已训检测器标新数据。**每张图只取置信最高的 N 个框**（`--max-boxes`，默认 1）是过滤误检的关键（原始输出 2.6 框/图）。`--eval` 模式不写标注，拿现有 `labels/` 当 ground truth 打分（IoU≥0.5 命中率 + 匹配框平均 IoU + 最差几张），对比图到 `viz_eval/`（文件名以 IoU 开头，最差排前）；`--overwrite` 才覆盖已标注。自动标注完输出核对图 `viz_auto/`，并提示用 `annotate.py` 人工复核。首次运行联网下载 YOLO-World + CLIP 权重（约 400MB）。注意默认 `--device mps`（此文件与其他文件的 auto_device 不一致，非 Apple 机器需显式传）。

#### `prepare_dataset.py`

**作用**：采集数据 → YOLO 检测数据集（单类别 hand）。两阶段：

1. `--captures <dir>` 扫 `labels.jsonl` 采集会话：有检测结果的帧从 21 点直接算 bbox（白捡的标注，`--pad` 外扩 0.12 与人工框风格对齐）；漏检帧导出到 `--to-label` 供人工标（**空标签绝不进训练集**——YOLO 空标签表示"无目标"，会把戴手套的手教成背景）。切分按 session（避免相邻帧泄漏进验证集），只有一个 session 时按时间顺序取末尾做验证集。
2. `--merge <dir>` 把人工标好的合并进 `--out` 数据集（跳过空文件），固定种子随机切 val。

产物 `dataset/`：`images/{train,val}` + `labels/{train,val}` + `data.yaml` + `viz/` 核对图（从写入的 .txt 反解坐标画框——画的就是模型真正读到的框）。

#### `train_detector.py`

**作用**：训练单类别手部检测器（替换在黑手套上输出 0 框的 RTMDet）。只训检测不训关键点（实测 RTMPose 给定正确框误差 3.9px，瓶颈只在"找不到手"）。默认 `yolo11n.pt`、100 轮、imgsz 640、batch 16；`--project`（默认 `runs`）**锚定到包目录**、`--name`（默认 `hand_det`）→ 最佳权重 `runs/hand_det/weights/best.pt`；`--resume` 从 last.pt 续训；`--no-augment-hsv` 关色相/饱和度增强（黑手套没色彩信息，算力让给几何增强，保留 hsv_v=0.4）。训练完提示 `python infer.py --detector <best.pt>`。要求先跑 `prepare_dataset.py` 生成 `dataset/data.yaml`。

#### `verify_pose.py`

**作用**：拿人工标好的框检验 RTMPose 关键点模型在目标手套上到底行不行——**大批量标注前先跑**（整套"只标框不标点"方案的前提验证）。标 10~20 张即可：`--dir`（含 `images/`+`labels/`）→ 逐张画骨架到 `verify_pose_out/`（`--out`），打印关键点置信均值。结论判定**必须逐张打开图看骨架是否贴合手指**（置信度在空白背景上反而更高）；贴合 → 只标框方案成立，不贴合 → 需改方案（标关键点微调或换模型）。

#### `fix_clip.py`

**作用**：修复 openai-clip 在 Python 3.12+ 的兼容问题（`clip.py` 的 `from pkg_resources import packaging` 在新 setuptools 中已移除 → 替换为 `import packaging.version`）。`find_clip()` 在 site-packages 定位 `clip/clip.py`，幂等（已打过补丁或不需要时跳过）。`setup.bat` 装完依赖后自动运行。

## 诊断工具

三个离线取证脚本（都不需要相机/硬件；前两个需 `FAYSSENSE_SDK_DIR`，第三个只读录制产物）。

#### `diag_color.py`

**作用**：验证 S80M 子进程 pipe 输出一帧的颜色通道是否正确。以子进程跑 SDK 的 `read_stereo_rgb.py --pipe -`（cwd 设为 SDK 目录），按 4 字节大端长度前缀协议逐帧解析左右目 JPEG，`cv2.imdecode` 后按上下半分割（与 GUI 一致，竖图上下半各一目），同时存两份 PNG：`/tmp/s80m_left_BGR.png`（OpenCV 原样 BGR 布局）与 `/tmp/s80m_left_RGB.png`（BGR→RGB 转换）——用图片查看器看哪张颜色正常（如红色物体显示为红色）。**须在 DAQ venv 下运行**（`VENV_PY = sys.executable` 直取当前解释器）。

#### `diag_frame_layout.py`

**作用**：ctypes 直连 SDK 动态库，验证帧布局 + 打印标定数据。加载 `$FAYSSENSE_SDK_DIR/lib/fays_atrak/x86_64/Release/libfays_vikit.so`，按 C++ 符号名（`_Z31FAYS_VIK_CreateHandleWithConfigPPvPKc` 等）声明 5 个接口：创建句柄（配置文件 `config/fays_vikit.yaml`）、取版本、取一帧、取标定参数。输出：每个相机的内参/畸变/外参 `T_cn_cnm1` 与基线（mm）；抓一帧后按 2 列×4 行（每行 400 高）统计 8 个子区 mean/std、左右半帧互相关、行 0-1 vs 行 2-3 差异，保存 `/tmp/diag_full_frame.png` 与 `/tmp/diag_grid.png`。启动时预加载 OpenCV `.so.406`/`.so.4.2` 共享库（`RTLD_GLOBAL`）并设置 `QT_QPA_PLATFORM_PLUGIN_PATH` 指向 cv2 自带的 qt plugins，规避 SDK 自带 OpenCV 与本环境 cv2 的冲突。

#### `diag_frame_trace.py`

**作用**：判**陈旧帧**到底是「新图像配了旧戳」（H1）还是「旧帧被重投递」（H2）。
两者在 ts 与 seq 上处处等价（现场 508 个 `[TIME_DROP]` 的 seq 全部严格前进，连 L
直方图都一样，L=4 占 84%），**差别只在像素** —— 所以判据只能是像素：桥接自带的
取证开关把回调**最前面**（早于 3-取-5 抽帧和单调守卫）的每帧原始载荷写进
`samples-NNNN.bin`，本脚本对每个下陷帧比对**往前 1..8 帧**的载荷。

判据用**边缘图**（每帧 `|Δx|+|Δy|`）而不是裸 mad，理由是真机逼出来的：重投递的帧
**不是**上一次那帧的逐字节重播，而是在输出级之前**又过了一遍**（像素值被非均匀地改过：
对往前第 L 帧的残差 2.1/4.7/10.8/12.5/16.9，而正常相邻帧的噪声地板是 4.09），所以裸
mad 与正常帧间差分不开 —— 第一版判据（比 mad 与全场基线的落点）在 09-17 的 4 个事件
里给过 2 个**错误**的「只戳错」。**注意真机上 mad 分不开的原因是场景动得慢**（对照 mad
从 lag1 到 lag4 只涨 1.15 倍，边缘对照 1.15~1.18），不是电平漂移：这 5 个事件整幅电平
的最佳偏移 δ\*=0（AE 根本没动）。整幅电平是加性的、在梯度里抵消，所以结构差只有在边缘
图上才露出来。逐帧算 `e(k)=e(i,i−k)`（k=1..8），与**同窗正常帧**的
`e(j,j−k)/e(j,j−1)` 中位数（对照，随运动快慢而变，不能用固定阈值）比：

| 情形 | 判据 | 结论 |
|---|---|---|
| 与往前第 L 帧逐字节相同 | `exact_back == L` | `H2_EXACT` |
| `e(L) < 0.75×` 对照 且 最小 lag 正是 L | 结构就是 80ms 前那一瞬 | `H2` |
| 最小 lag = 1 且 `e(L)` 落在 `0.8~1.3×` 对照内 | 图像是新的 | `H1` |
| 窗内正常帧已经有逐字节相同的对 / 各 lag 的边缘差挤在 1.15 倍内 | 场景太静，没有分辨力 | `QUIET` |

**几何同源 ≠ 同一幅**：真机 4 个能判的事件全是 `H2` 但**没有**一个逐字节相同
（`e(L)/e(1)` = 0.32 / 0.54 / 0.57 / 0.74，对照 1.15~1.18），即「旧数据在输出级之前
又过了一遍」。结论行按这个措辞写。

- `preflight`：开录前预检 —— C++ 侧 `start()` 失败会让**桥接直接退出、夹爪连不上**，
  所以时长 ≤300s、空闲 >2GiB、CPU 角色、`main.py` 是否在跑都先挡一遍，并打印三行
  `export`（含本次专用、不复用的取证目录：分片名从 `samples-0000.bin` 起编，复用
  目录会**直接覆盖**旧取证）。
- `analyze <目录>`：出逐帧判读 + L 直方图 + 结论。加 `--native-log` 会把
  `[TIME_DROP]` 日志按 seq 与本次取证对上 —— 这是「抓到的就是现场那个毛病」的唯一
  凭证（取证钩子在回调入口、**守卫在它下游**：`fayssense_orb_slam.cc:1572` vs
  `:1806`，队列是 FIFO 只排队不重排；守卫又只看 3-取-5 的 3 帧，所以取证的下陷数
  应约为日志的 **5/3**，量级对不上脚本会直接说「先别下结论」）。
- **取证时必须让夹爪持续运动**：静止时相邻帧本来就几乎相同，H1/H2 都判不出来。
- **空连抓不到这个毛病**：丢帧率随机器负载走（09-17 实测 ldavg ≤5.5 → 0/秒，
  10~12 → 0.03~0.17/秒，16.7 → 0.43~0.61/秒）。120s 空连在基线率下只期望 ~4 个
  事件，**要抓就挑负载高或真在录制的时段，`--seconds 300`**。
- 顺带校正：判「ts 是否在 20ms 格点上」必须看**差值**。真机所有帧的 `ts % 20ms`
  都是同一个恒定相位（09-17 实测 6.05ms），按绝对值判会把 4318/4318 帧全报成
  「偏离」（第一版就是这个误报，已加回归用例）。同一条流里 IMU 戳是 10µs 分辨率。
- 离线自检（不需要 SDK/相机）：`venv/bin/python tools/tests/test_diag_frame_trace.py`
  —— 16 项、约 5 秒。合成场景模型是**量出来的**（相关长度 24 + 每帧平移 1 像素 +
  电平三角波 ±4/帧），参数注释里记着每一版失败的原因（正弦跟像素格拍频、整幅
  `np.roll` 是纯平移、条纹太缓量化成 ±1 台阶都会让判据失明）；除真假两种流外还
  覆盖：旧判据在新模型上**确实会误判**（当场验证它的两个带条件都成立，否则这条
  用例可能因模型退化而假绿）、对照必须取自同窗（跨快慢两段）、电平折返点不得落进
  比对窗、常量与 `fays_raw_client.py` 逐字一致、残尾/多分片/缺 `capture.json`、
  恒定相位偏移、零下陷时的结论行。

#### `audit_frame_gaps.py`

**作用**：把「录下来的视频到底丢了多少」一次数清楚（**只读**，不写任何东西；
背景与全库基线见 [postmortem](postmortem_trajectory_and_rgb.md) 第四节）。

```bash
venv/bin/python tools/audit_frame_gaps.py [任务目录] [选项]   # 默认 data/recordings/UMIGripper_Action_AI
```

选项：`--min-gap-ms 100`（相邻帧间隔 ≥ 该值才算异常）、`--clock-tol-ms 100`
（双钟互证容差）、`--merge-rows 15`（相隔 ≤N 行的异常并成一次事件）、
`--no-video-check`（跳过画面复核，只剩钟口径）、`--all`、`--json`、
`--camera-logs DIR`（关联 `logs/camera_service/` 的留档日志，秒级粗相关，默认关）。

**三层判据**（由强到弱）：

| 层 | 判据 | 结论 |
|---|---|---|
| 1 画面复核 | 事件处两帧差 ÷ 同窗邻帧差中位 ≤2.0 | 画面连续 ⇒ **没丢**（帧到得晚/写得晚） |
| | ≥3.0 或 ≥0.7×远端饱和差 | 画面跳变 ⇒ **丢了** |
| | 其余（含整窗静止 <噪声地板 1.5） | 判不了 ⇒ 回落第 2/3 层，进「待复核」 |
| 2 双钟 | Δhw ≈ Δwall ≥ 阈值 | 两侧都这么说（无画面时按疑似计） |
| 3 单钟 | 只有墙钟跳 | **写侧打嗝**（帧按 33ms 到了、只是写晚） |
| | 只有戳跳 | **读侧停顿**（帧晚到，写侧靠队列垫住） |

**两个戳分别是谁盖的**（不认识这个会得出错结论）：`hardware_ns` 由**采集线程**
在相机 `read()` 返回处盖（量「取到帧的时刻」，宿主单调钟；≤078 段是被截成**有符号
32 位**的同一个钟，每 4.295s 锯齿一次，必须先按 2^32 解卷）；`wall_time` 由**写入
线程**在建行落盘处盖（量「写下来的时刻」，宿主墙钟）。因此 **Δwall − Δhw = 队列
滞留的变化量**：阶跃为正 = 写侧积压、为负 = 读侧空窗把积压排空 —— 这是区分「哪一
侧停了」的判据。

**归账四桶**（互斥，只第 1 桶算「证实丢了」）：

```
画面跳变 → gap_ms（丢了）  只有墙钟跳 → wall_ms（写侧打嗝）
画面连续 → stamp_ms（没丢）  其余 → pending_ms（待复核，绝不计入损失）
```

**退出码**：有画面证实丢帧、或有待复核 → 1（「不上报等于把未知当没事」）；否则 0。
2026-09-18 全库结果：95 段 / **画面证实丢了 25 段 37412.6ms**（23 段早年段 + 098/099，
**全部落在开录后 0.13~1.47s**）/ 钟跳但画面连续 72 段 20076.2ms / 写侧打嗝 43 段
9944.8ms / 待复核 0。

离线自检：`QT_QPA_PLATFORM=offscreen venv/bin/python tools/tests/test_audit_frame_gaps.py`
（10 组、全合成 fixture，不依赖真机数据；含「渐变条」画面模型使一帧运动量与 30 帧
运动量在数值上分得开，以及「静止场景不许判成没丢」的反向用例）。

#### `data_qc.py`

**作用**：录制数据的**时间轴质检器**（只读，不写任何东西）—— 把「这段数据的时间轴
能不能用」变成可量化、可回归、可进 CI 的判据。与 `audit_frame_gaps.py` 的分工：
后者是**像素级取证**（判视频真丢没丢，需 ffmpeg 画面复核）；本脚本是**时间轴体检**
（只看戳，不碰像素，全库 25 段 3.0GB 约 0.8s 跑完），两者互补。

```bash
venv/bin/python tools/data_qc.py <数据集目录>                    # 只读体检，打印报告
venv/bin/python tools/data_qc.py <数据集目录> --json qc.json     # 另存机器可读报告
venv/bin/python tools/data_qc.py --selftest                     # 判据自检（合成数据，不碰真实数据）
```

选项：`--config F.json`（深度合并到默认值）、`--episodes 000,001`（或全局号 1,2）、
`--jobs N`（默认 `min(4,CPU)`，`1` 为串行便于调试）、`--check-video`（额外用
`ffprobe` 读头部比「视频帧数 vs parquet 行数」，默认关）、`--strict`（有 warn 也
返回 1）、`--out-dir DIR`（同时落 `report.json`/`report.txt`）、`--top N`、
`--list-streams`（只打印推断出的流模型）、`--emit-config F.json`（导出可改的配置模板）、
`--pdf PATH`（出 PDF 报告，见下）。

**五项检查**（用户口径 → 实现判据）：

| 编号 | 名称 | 判据 |
|---|---|---|
| C1 | 时间戳回退 | 相邻样本 `d = v[i+1] − v[i] < 0`。分**行内回退**（同一帧行的样本列表内）与**跨行回退**（上一行末样本 > 下一行首样本）。±2^32/2^64 计数器**绕回**自动识别解卷，单列 info、不算回退。**样本流（见下）先剔除 0 占位行再判** |
| C2 | 时间戳重复 | `|d| ≤ 该列量化步长`。分**行内重复**（恒 error）与**跨行重复**（同一样本被挂进两行，可能只是重报，按比例定级 warn/error） |
| C3 | 时间戳跳变 | `d > gap_factor × 标称周期` 且超出量 ≥ `min_excess_ms` ⇒ **空洞**（给出毫秒数、×标称倍数、折算丢几个样本）；`d < compress_factor × 标称` ⇒ **压缩**（攒批突发，与空洞成对出现，info） |
| C4 | 不同数据帧数不齐 | 七项子检查：meta `length` vs 实际行数、`frame_index` 连续性、各流**段中间空行**与**攒批行**、实测采样率 vs 声明 `rate_hz`、流覆盖率、**成对的 ts 列与数值列存在性是否对齐**、同频各流样本数是否互相对得上、**与 `meta/episodes` 的 `drop_stats` 自报计数对账**（见下） |
| C5 | 不同时间戳有偏移 | ① 每个流相对**同域参考帧列**的偏移（中位数/IQR/漂移 ppm/分段台阶），与基线比；② 同域且**锚定同一参考列**的两流相互偏移（如左右手套失同步）；③ 帧级时间列两两之间的偏移**稳定性**（跨时钟纪元时刻只判稳定性，不判绝对值） |

另附 C6 结构一致性（数据集级）：`N = c × chunks_size + f + 1` 编号公式、`episode_index`
重号、data 与 meta 的 `episode_index` 是否一致。

**三条流速（1.1.0 起）**——决定 C1~C5 各按什么口径判：

| 类型 | 是什么 | 怎么判 |
|---|---|---|
| `frame` | 行自己的时刻：`timestamp`/`wall_time`/`hardware_ns`/`raw_hw_ns`/`host_ns`/`depth_ns`/`frame_index` | 五项全查；严格单调是硬要求 |
| `list` | 一行挂多个样本的变长戳：手套 `ts`、`imu_ts_ns`、`slam_trajectory_ns` | 标称周期取**平均**间隔（不是中位——攒批流是双峰，中位会落到短峰上）；间隔呈攒批（p95 ≥ 1.5×中位）时空洞还须超过 1.5×p95，error 门槛取 2×p95 —— SLAM 位姿实测就是 20/20/60 三连发，这不是丢样本 |
| `sample` | 每行**至多一个**采样值、采样时刻与行时刻无关的标量 `*_ns`（夹爪力的 `*_force_ns`/`*_force_matrix_ns`，`encoding=host_monotonic_ns`） | `0`（`features.missing_sentinel`）= **本行没有样本**，不是时刻 0：剔除后再判回退/重复；缺样本只按覆盖率报 info（<50% 属正常相位差），比例过高才 warn；比的是**行号配对偏移**（样本戳 − 本行帧戳），中位报 info（队列滞留差），IQR/台阶才升 warn/error |

**稀疏列**：`pq.read_table` 只取本段真实存在的列；某段整列没有某条流时按 C4
「本段整列缺失（0 样本）」报，**不再**把 `No match for FieldRef.Nested(...)` 记成
C6 结构 error（那会让该段的 C1~C5 一条都没查过）。流模型取自所有段 schema 的**并集**。

**drop_stats 对账（C4 里最有价值的一条）**：`meta/episodes/*.parquet` 的 `drop_stats`
是**录制器自己记的账**（`frames_without_left_hand` / `hand_samples_right` /
`imu_samples` / `frames_without_imu` …），本工具从时间戳列**独立数**出同样的量，两边
逐项比。键↔流按名字 token 自动配对（`frames_without_*` → 空行数，`*_samples` → 样本数；
含 `unpaired/unknown/dropped` 等限定词的键跳过，它们不是总数），也可在配置的
`drop_stats_map` 里写死。**记住不能把配到同一条流的多个时间戳列求和** —— 宿主戳列和
设备戳列是同一条物理流的两个副本，计数本来就相等。

实测（25 段 × 6 项 = 144 项，**全部一致，0 项对不上**）。这条对账的实际用途：
episode-003 右手套尾部掉了 19 帧，本工具数出 19 个空行、录制器自报
`frames_without_right_hand: 19`、`status.right_glove` 在**完全相同的** 1455..1473 行
从 `connected` 翻成 `unmatched` —— 三方独立互证，缺陷是真的，不是工具的口径问题。

**判据里三条最要紧的认知**（不认识会得出错结论）：

1. **三个时钟纪元各不相同**，绝对差没有意义 —— 相机设备钟（`hardware_ns`）、手套设备
   钟（`*_glove_device_ts_ns`）、宿主钟（`wall_time`/`*_glove_ts_ns`/`slam_trajectory_ns`）
   各自从 0 起。所以 C5 只比**同域且锚定同一参考列**的流；跨纪元只判**稳定性**（漂移、
   台阶），因为「差值在变」永远说明有问题，而「差值是多少」不一定。
2. **左右手套的设备计数器是两个独立计数器**（各自开机时刻不同，纪元差几百 ms 到几秒），
   拿它们的绝对值相减得到的是「各自何时上电」，不是失同步 —— 这类无参考列的流直接不
   参与成对比较；左右手套的同步性要看它们的**宿主戳**（实测中位 −0.5ms）。
3. **偏移基线是数据自带的**：手套宿主戳为对齐视频被**故意**整体 +85/+115ms（见
   `meta/info.json` 的 `alignment.glove_shift_ms`）。所以「手套 − SLAM ≈ 基线」是设计
   要求，不是缺陷；只有**偏离基线**才算。基线自动读 `info.json`，也可在 `--config`
   的 `baselines` 里写（支持 fnmatch 通配）。

**复用方式**：零配置可用（流的清单、时钟域、标称率、配对列全部从 `info.json` 的
`features[].rate_hz / clock / ts_key` 推出）；`info.json` 缺失时按「列类型 + 列名」自动
发现（`list<整数>` 且名字像戳）；连 `data/` 目录结构都没有时，接受「目录本身就是一堆
parquet」。只 import `numpy` + `pyarrow`，不 import `core/`，可直接拷到别的项目跑。

**PDF 报告**（`--pdf 报告.pdf`）：把五项检查的结果渲染成 A4 PDF，版式对齐客户给的
《1013视塔手套数据质检.pdf》（GitHub-Markdown 风格：编号检查表 + ✅/⚠/❌ 徽标 +
运行页眉 + 底部居中页码）。报告结构：1 数据结构与基础信息（目录树 / 概览 / 数据字段 /
视频规格）→ 2 质检结果（2.1 文件完整性、2.2 时间戳质量**五项基础检查**、2.3 手套数据
质量、2.4 视频帧数↔行数一致性、2.5 drop_stats 对账）→ 3 结论与建议。

选项：`--pdf-title T`（默认「数据质检报告」）、`--pdf-date D`、`--pdf-html PATH`
（保留中间 HTML）、`--chrome PATH`。多数据集时 `--pdf` 是**合并成一份**报告，而
`--json`/`--emit-config` 会自动加数据集名后缀、`--out-dir` 各占一个子目录。

**为什么走 CDP 而不是 `chrome --print-to-pdf`**：命令行版只能用 Chrome 自带的页眉页脚
（日期在左、标题在右、URL 在左下），改不了；CDP 的 `Page.printToPDF` 收
`headerTemplate`/`footerTemplate`，才能做成参考件那样「标题在左、日期在右、底部居中
N/M」。实现要点：Chrome 必须带 `--remote-allow-origins=*`，否则 CDP 的 WebSocket 握手
会被 403 拒掉；WebSocket 用 `websocket-client`。**这两样都是惰性使用** —— 不出 PDF 时
本文件仍然只依赖 numpy + pyarrow，可以单独拷走。

报告段自己现采的指标（质检器本体不管）：手套 NaN 占比、IMU valid 通道占比、四元数模长
及偏离 >1% 的个数、**活跃传感器数**（16 传感器 × 16 轴分组，任一轴在动即算活跃）、视频
编码/分辨率/像素格式/帧率/帧数、逐段时长、以及每个数据集的**数据指纹**（info.json /
data / meta 三处 mtime）。

**数据指纹与陈旧守卫**：导出管线是边写边替换的（先 data、再 meta/episodes、最后
info.json），报告若落在中间窗口，会拿**新数据**配**旧元数据**，把「读不到时钟域/参考列」
误报成满屏缺陷 —— 2026-10-15 踩过一次（24 段各报 2 条，共 48 条假警告）。现在报告会
比对三处 mtime：差**秒~1 小时**判为「导出中途」（红，结论不可信，请重跑）；差**几小时
以上**判为「元数据没跟数据一起重建」（黄，建议一并重建）。

也可当库用：

```python
import data_qc
reps = [data_qc.run(d, data_qc.DEFAULT_CONFIG) for d in dirs]
data_qc.write_pdf_report(dirs, reps, "报告.pdf")
```

**退出码**：0 = 无 error（有 warn 仍为 0，除非 `--strict`）；1 = 有 error 级问题；
2 = 用法/读取错误。适合直接接 CI 回归。

**自检**：`venv/bin/python tools/data_qc.py --selftest` —— 现场合成 9 段数据（1 段干净 +
5 段各注入 C1~C5 一类缺陷 + 3 段验新口径：样本流 0 占位不算回退/重复、稀疏列不算读不动、
20/20/60 攒批不算丢样本而只报注入的 200ms 断流。时间轴形状刻意贴近真实数据：手套
2.03 样本/行、IMU 16.71 样本/行、`floor` 量化模拟「样本挂到帧行」的抖动），断言
「该报的报了、干净段一个 warn 都没有」。
**改判据先跑它。**

**基线数据实测**（2026-10-15，`/home/stouch/out/offline/Data_examples_2`，25 段 / 78,181 帧 /
3.0GB / 0.8s）：无 C1、无 C2；C3 只有 episode-000 的左手套 7 处空洞（最大 166.8ms =
10.0× 标称，折合丢约 35 个样本）+ 右手套 2 处（最大 66.7ms）；C4 抓到段中间掉样本
（ep000 左手套 14 行、ep003 右手套 18 行）与同频各流样本数差（ep000 少 52 个）；
C5 抓到 episode-002 右手套宿主戳比基线偏 −29.4ms。IMU↔`hardware_ns` 偏移全程
−22.07ms（IQR 1.6ms）、手套↔`wall_time` IQR ~8ms，说明这些流本身是稳的。

#### `trim_episode_tail.py`

**作用**：裁掉 episode 尾部的 N 秒（默认 1 秒），把「尾部先停的模态」对齐掉
——主要给「某流最后若干行没有样本」的段用（例：episode-003 右手套段末 19 帧无样本，
而 RGB/左手套/IMU/SLAM 一直录到最后一帧）。默认**预演**，`--apply` 才落地。

```bash
venv/bin/python tools/trim_episode_tail.py <数据集目录> --episodes 003          # 预演
venv/bin/python tools/trim_episode_tail.py <数据集目录> --episodes 003 --apply   # 落地（自动备份）
```

选项：`--seconds 1.0` / `--frames N`（按 `info.json` 的 fps 折算）、`--episodes`
（三位段号或全局号，默认全部）、`--no-video`、`--no-backup`、`--force`
（已在 `info.json` 记录过也照做）、`--recalc-stats`（**危险**，见下）。

**四件事一起做**（顺序不能反）：data parquet 行切片 → 视频 `-c copy` 裁到同帧数 →
`meta/episodes` 同步 `length`/`end_frame_index`/`duration_sec`/`video_codec.frames`
与 `drop_stats` 里**由保留行决定**的计数（`frames`/`last_user_data_seq`/
`hand_samples_*`/`frames_without_*`/`imu_samples`；会话级累计计数如 `dropped_bytes`
保持原值）→ 在 `info.json` 写 `tail_trim` 记录。备份落在 `_pre_tail_trim/`
（按相对路径镜像，否则 data/ 与 meta/ 下的同名 `episode-NNN.parquet` 会互相覆盖）。

**两条实测口径**：

* **`-c copy` 可以精确裁到任意帧，不限于关键帧**。ep003（h264 3200×1200 / GOP=30 /
  1474 帧 / 关键帧在 0,30,…,1440,1470）用 `-c copy -frames:v 1444` 得到恰好 1444 帧，
  保留帧 0/720/1440/1441/1442/1443 与原件**逐像素 md5 相同**（断点之后的 B/P 帧
  回溯引用的关键帧 1440 仍在保留区间内）。耗时 0.07s，无需重编码。
* **`video == rows` 是全库不变量**（24 段无一例外），所以两边必须一起裁，否则
  `data_qc.py --check-video` 会报、按帧号取图的工具会错位。

⚠ **stats.json 默认不动**，两个原因：① `core.helpers.recalc_stats` 走 `np.stack`，
对「每行样本数不等」的列（本数据集 `observation.left_glove` 每行 1/2/72 个样本）
会抛 `ValueError` 被 `except` 静默 `continue`，等于**把该列从 stats.json 删掉**；
② stats.json 由池化转换器产出，本就可能落后于数据（本次实测它记的是上一版
25 段 / 157,825 个手套样本，而数据已是 24 段 / 138,433）。要准请在导出管线里重新生成。

**局限**：裁尾**修不了段中间的缺失**。落地前工具会把「裁后仍存在的空行」逐流打出来
核对（本次结果：`slam_trajectory_ns×1` —— SLAM 首行恒为空，是全库 24 段共有的头部
形状，裁尾不改变它）。

#### `make_qc_report.py`

**已并入 `data_qc.py` 的 `--pdf`**，本文件保留为**薄封装**（把老参数翻译过去，
自身无逻辑，只有一份实现）——旧命令仍可用：

```bash
venv/bin/python tools/make_qc_report.py <数据集目录> [...] -o 报告.pdf
# 等价于
venv/bin/python tools/data_qc.py <数据集目录> [...] --pdf 报告.pdf
```

PDF 的采集/版式/渲染细节全部见上一节 `data_qc.py` 的「PDF 报告」小节。

## tests/ 测试

测试脚本见 `tools/tests/`（当前 79 份；离线测试加 `QT_QPA_PLATFORM=offscreen` 无需任何硬件，真机测试需对应设备在线）。完整运行命令见 `README.md` 测试节。**下表只列了一部分**（早期 16 份 + 帧空洞相关的 5 份 + 夹爪配对/离线入口 + 位姿坐标帧的 5 份 + 删除真删除的 3 份 + 发布死区/跳变门的 3 份），其余按需现查文件名。

⚠️ **任何离屏脚本/一次性探针只要 `MainWindow()`，就必须先把 `settings.SERVER_CONFIG_FILE` 指到临时文件**（`tempfile.NamedTemporaryFile` + 结束时还原）。否则它走的是真实 `closeEvent` → `save_device_dock_prefs()`，会把「设备栏已收起」写进用户真实 `data/server_config.json`，用户下次启动就找不到设备栏（2026-10-13 被验证 agent 与 `d435_gui_smoke_test.py` 实际各踩一次）。`_ui_prefs_writable()` 现在是最后一道闸（离屏 + 默认真实路径 = 拒写 + stderr 告警），但别靠它兜底。

| 文件 | 类型 | 一句话 |
| --- | --- | --- |
| `test_playback_multifps.py` | 离线 | 多帧率会话回放 |
| `s80m_signal_regression.py` | 离线 | S80M 信号回归（object 参数大整数时间戳） |
| `s80m_50fps_decimation_test.py` | 离线 | S80M 50→30 抽帧录制决策 |
| `multi_device_registry_test.py` | 离线 | 多设备 worker 注册表 |
| `exposure_control_test.py` | 离线 | 曝光控制解析/下发 |
| `test_meta_devices.py` | 离线 | 会话元数据设备行 |
| `test_depth_heatmap.py` | 离线 | 深度热力图生成 |
| `glove_widget_test.py` | 离线 | 手套控件配置路由 |
| `grid_drag_fps_test.py` | 离线 | 网格拖拽/分割条 |
| `device_panel_gui_smoke_test.py` | 离线 | 设备面板 GUI 冒烟 |
| `device_dock_collapse_test.py` | 离线 | 设备检测栏 缩小/隐藏（栏宽地板回归哨兵 + ×/菜单收起 + 偏好持久化 + **正常关窗/最小化不得被当成用户收起** + 浮动宽度不污染记忆栏宽 + 配置不可写时退出不 abort + **离屏进程不得写真实配置**） |
| `test_device_detector.py` | 离线 | 设备枚举 |
| `d405_worker_test.py` | 真机 | D405 采集 worker |
| `d435_e2e_test.py` | 真机 | D435 端到端 |
| `d435_gui_smoke_test.py` | 真机 | D435 GUI 冒烟 |
| `d435_playback_test.py` | 真机 | D435 回放 |
| `mono_regression.py` | 真机 | 单目回归 |
| `test_frame_gap.py` | 离线 | 帧空洞看门狗纯类（口径、混源不造假空洞、负值戳） |
| `test_rgb_quality.py` | 离线 | 采集侧停摆告警去抖 + 覆盖/滞后峰值仪表 |
| `test_ext_frame_gap.py` | 离线 | 落盘侧空洞端到端（含 parquet `*_gap_*` 注入） |
| `test_camera_log_archive.py` | 离线 | camera-service 日志留档 + 告警摘录（健康会话零命中） |
| `test_audit_frame_gaps.py` | 离线 | 审计脚本三层判据/四桶/退出码（全合成 fixture） |
| `test_gripper_fays_pairing.py` | 离线 | 夹爪配对链失败关闭（QF/WF、按 USB serial 唯一匹配、不猜端口） |
| `test_gripper_esp_binding.py` | 离线 | 夹爪离线入口：串口诊断只读收集（含写阻塞时仍走通的只读复检、以及卡在第几笔写的留痕日志）、绑定读写与回读、界面三条链路的善后口径、「板子没在服务串口」提示的挂载范围 |
| `test_gripper_pose_axes.py` | 离线 | 发布帧的行/列符号约定、`PUBLISH_RELABEL_MATRIX` 与 `_PUBLISH_RELABEL_QUAT` **必须同一张 M**（正交/det=+1/非镜像）、相对化与重标的次序无关性 |
| `test_gripper_pose_host_stamp.py` | 离线 | 两种时刻的解析契约：位姿点的 `t` 是**相机钟**、native 的 `Host:` 是**宿主单调钟**（两者只差一个只能拟合的偏移，实测散布 ~104ms 已超一帧） |
| `test_gripper_slam_columns.py` | 离线 | SLAM 落盘列契约：**只落 `slam_trajectory`、不再落 `slam_pose`**（后者是被 26.5% 填零的定长 `[0]*7`，下游会把零当真实位姿） |
| `test_gripper_latest_pose_frame.py` | 离线 | 原生帧 → 发布帧的实际差值就是那张 M（位置 `M·Rz(+90°)`、姿态还多一层体轴改名 `Mᵀ`），拿真数据比对而不是复述公式 |
| `test_pose_view_qt.py` | 离线 | 位姿视图：轨迹常驻、世界轴与**跟随夹爪的三箭头**重合且标签带方向词、三箭头取**列**（体轴）不取行、夹爪模型换基后逐像素不变、主窗口那次四元数→矩阵转换把帧带上 |
| `test_pose_publish_deadband.py` | 离线 | 发布死区语义：AR(1) 静止游走被冻结（>95% 帧保持、且保持的是上次的值）、慢速移动照常推进且误差不超死区、真实会话回放（`logs/` 不入库时会 SKIP，别把 SKIP 读成 PASS） |
| `test_gripper_pose_deadband_publish.py` | 离线 | 死区真接在**发布出口**（`_publish_pose`）：静止发旧值、节拍/序号/丢失计数不变、每个发布值都是某帧原始值的发布帧像（替换而非插值/平均）、重锚丢参照、开机日志有那行可验收 |
| `test_pose_guard_limits.py` | 离线 | 跳变门夹在实测包络中间（真实单步 ≤0.0601m 放行 / 异常 ≥0.1075m 拦下）、限值不再随 20/20/60ms 抽帧膨胀、native `kPose*` 与 Python 常量逐字一致 |
| `test_local_delete.py` | 离线 | 删除即真删除：池化一段的三处文件（data/视频/meta）都清掉且不牵连邻居、删不掉时 `False` + 残留清单 + 中文报错而不是假装删掉、旧格式会话目录 rmtree 复检、录制行真删（含尾斜杠归一化、空路径 0 行） |
| `test_task_delete.py` | 离线 | 任务页「🗑 删除」= 真删目录 + 真删录制行 + 只留 `hidden` 墓碑（确认框写明目录与片段数）；目录删不掉时**不隐藏任务**并弹窗报错；第 4 节：本机任务（`source=local`）删除 = 真删目录/录制行 + `tasks.json` 整条移除（无墓碑），确认框标明本机任务 |
| `test_task_create.py` | 离线 | 本机任务（v1.3.16）9 节：创建字段/`source`/`created_by`/进度初值；名称校验（空、超长、空格/点/斜杠/制表、Windows 保留名、大小写不敏感重名、hidden 墓碑重名）；目标段数边界（1~100000，bool/字符串拒绝）；**仅创建者可见** + 账号轮询不清除本机任务 + 合并保留计数；进度只记 `local_count` 且 `pending_sync_tasks` 不含本机任务；改名（无数据可改、重名/非法/已有录制目录/新目录已存在/非本机任务拒绝）；删除整条移除无墓碑；任务页 5 列/来源列/新建后选中/改名/删除；对话框实时校验与已有数据禁用改名 |
| `lite_delete_test.py` | 离线 | lite 上传后删除的编号回归：列表 ep 与删除入参都必须是**全局号 N**（`_episode_max` 要把文件号 +1；`_delete_after_upload` 不许再加工——多 +1 会指向不存在的 N+1，残留复检空集 → 报成功却一个文件没删）、单段目录只删该段不 rmtree、无 parquet 的旧目录整目录删 |
