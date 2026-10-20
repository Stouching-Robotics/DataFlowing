# 多设备采集存储结构接口文档

> 适用版本：collector v1.3.x 起的任务级池化布局（`format = "pooled_episodes_v1"`）
> 文档定位：本文是 [`docs/file_format.md`](file_format.md)（规范性契约）的**多设备补充**，
> 回答三件事——**不同设备的数据存哪、叫什么名字、在 meta 里怎么声明**。
> 布局/编号/原子写/上传契约以 `file_format.md` 为准；本文与它冲突时以它为准。
>
> 图例：
> **【现状】** = v1.3.x 已在代码中实现、磁盘上已有数据；
> **【提案】** = 为「双夹爪 + ego 相机三条 SLAM 轨迹」新增的约定，需改代码后才生效。

---

## 1. 存储布局

### 1.1 目录树（一条 episode 的全部文件）

```
data/recordings/<任务名>/
├── data/
│   └── chunk-{c:03d}/
│       └── episode-{f:03d}.parquet          # 本段所有非视频观测（每帧一行）
├── videos/
│   └── chunk-{c:03d}/
│       ├── <image_key_1>/episode-{f:03d}.mp4
│       ├── <image_key_2>/episode-{f:03d}.mp4
│       └── <depth_key>/episode-{f:03d}.mp4  # 深度流（12-bit 灰度；回落 .mkv）
└── meta/
    ├── info.json                            # 任务级 schema/声明（读端入口）
    ├── stats.json                           # 任务级统计（定长列）
    ├── tasks.jsonl
    └── episodes/
        └── chunk-{c:03d}/
            └── episode-{f:03d}.parquet      # 本段一行元数据（含每段权威规格）
```

### 1.2 三条铁律

1. **一条 episode = 一个 `(c, f)` 编号 = 1 个 data parquet + 每路录制流 1 个视频 + 1 个 episodes 行文件。**
2. **所有非视频观测都进那一个 data parquet**：压力矩阵、手套 IMU、手部关键点、双目
   IMU、夹爪力/力矩阵/状态、**所有 SLAM 轨迹**，全部是这张表的列，**没有按设备
   拆分的子目录或轨迹侧车文件**（2026-09-10 定案废除轨迹 txt）。
3. **视频每流一个文件**，按 `<image_key>` 分目录；同一任务的视频槽允许跨 episode
   改名，读端必须按 episodes 行/每段实际文件枚举，不能假设任务级固定集合。

### 1.3 编号规则（磁盘段号 0 基，parquet 列 1 基）

磁盘上的 episode 段号**从 0 开始**：`episode-000` 就是第 1 段。设 `k` 为任务内的
**全局 0 基段号**（0, 1, 2, …），则路径固定为：

```
c = k // 1000        # chunk 号；chunk-000 装 k=0..999
f = k %  1000        # chunk 内文件号，三位零起

data/chunk-{c:03d}/episode-{f:03d}.parquet
```

| 第几段 | `k`（0 基段号） | 文件 | parquet `episode_index` 列 |
|---|---|---|---|
| 1 | 0 | `chunk-000/episode-000` | 1 |
| 2 | 1 | `chunk-000/episode-001` | 2 |
| 1000 | 999 | `chunk-000/episode-999` | 1000 |
| 1001 | 1000 | `chunk-001/episode-000` | 1001 |

- **两套编号不要混**：
  - **文件/段号 `f`（0 基）**：磁盘上的段标识，也是上传表单 `episode_index` 的值
    （`core/uploader.py:581` 用 `episode_file_suffix()` = `k % 1000`）。
  - **parquet `episode_index` 列（1 基）** = `k + 1`；`list_task_episodes()` 按
    `chunk*1000 + f + 1` 还原。实测 `UMI_9_29/data/chunk-000/episode-000.parquet`
    该列为 **1**，不是 0。
  - `next_pooled_episode_index()` 对空目录返回 **1**（即 k=0），生产路径不会取到 0；
    `episode_chunk_file(0)` 会被钳到 `(0,0)`，所以测试里把 `_episode_index=0` 也落到
    `episode-000` —— 这是代码里唯一出现 0 的地方，**别据此认为列也是 0 基**。
- `chunks_size = 1000` 由 `info.json` 声明，读端不得写死。
- 同一 episode 的 data / videos / meta-episodes 三处**共用同一个 `(c, f)`**。
- 跨 chunk 时 `f` 会回绕（第 1001 段是 `chunk-001/episode-000`），**不要**拿
  「最大 `f` + 1」当段号；统一走 `core.helpers.list_task_episodes()`。
- 异常终止不占号（`meta/recycled_episode.json` 标记复用）；正常完成/删除的号永不复用。

### 1.4 这里**不放**什么

| 内容 | 实际位置 | 说明 |
|---|---|---|
| 手部关键点后处理结果 | `keypoints_output/<task>/episode_{episode_index:06d}/`（parquet 列值，1 基；第 1 段是 `episode_000001`） | 不写回录制目录 |
| 标定文件 | `meta/info.json` 的 `calibration` + 每段 episodes 行 | 没有 `calibration/` 目录 |
| 设备命名/曝光配置 | `data/device_names.json` / `data/device_params.json` | 本地配置，不进录制目录 |
| 上传临时切片、锁、回收标记 | 见 `file_format.md` §11 | 读端一律忽略 dotfiles 与临时件 |

---

## 2. 命名规范

### 2.1 三个命名空间，不要混用

| 命名空间 | 形态 | 出现位置 | 谁生成 |
|---|---|---|---|
| **设备 key** | `<kind>:<稳定标识>`，如 `d435:213622075531`、`gripper:CC:BA:97:25:B4:44` | `info.json` 的 `devices[].key`、`data/device_names.json` / `device_params.json` | `core/device_detector.py` |
| **视频槽 image_key** | `<前缀>_rgb` / `<前缀>_depth` / `stereo_left` … | `videos/<image_key>/`、`cameras`、`video_extensions`、`device_names` | UI 打开设备时分配（§2.3） |
| **parquet 列名** | `observation.<...>` / `status.<...>` / 顶层键列 | data parquet schema、`info.json.features` | pipeline + writer |

设备 key 与列名**没有派生关系**：`d435:213622075531` 对应哪一列，只能通过
`devices[].slots` + `features` 查，不能从 key 字符串拼。

### 2.2 列名语法

```
observation.<namespace><signal>      # 观测列（数值/序列/轨迹）
status.<UVC槽|传感器名|夹爪非视频槽>   # 连接状态列（string）
<key_column>                         # 帧键列：episode_index / frame_index / timestamp / …
```

`<namespace>` 取值表：

| namespace | 归属 | 示例 | 现状 |
|---|---|---|---|
| `""`（空） | 第 1 台 UMI 夹爪（历史契约） | `observation.slam_trajectory`、`observation.gripper_state` | 【现状】 |
| `gripper_2_` | 第 2 台 UMI 夹爪 | `observation.gripper_2_slam_trajectory`、`observation.gripper_2_gripper_state` | 【现状】 |
| `ego_` | ego 相机（角色名） | `observation.ego_slam_trajectory` | 【提案】 |
| 传感器名本身（`left_glove` / `right_glove`） | 数据手套（BLE/USB） | `observation.left_glove`、`observation.left_glove_imu_quat` | 【现状】 |
| 无 | 手部关键点（恒写列） | `observation.left_hand_pose` | 【现状】 |

**注意**：夹爪的力/状态列在写入时是「前缀 + 完整旧键」，所以第 2 台是
`observation.gripper_2_gripper_left_force`（两个 `gripper` 不是笔误），而轨迹列因为
信号名本身不含 `gripper`，才是 `observation.gripper_2_slam_trajectory`。写读两侧
都按**前缀拼接**处理，不要手写名字时想当然。

### 2.3 多实例编号规则

| 规则 | 说明 |
|---|---|
| **首台不编号** | 夹爪 rig1 必须是空串前缀（现有 357 段数据依赖），**禁止**改名为 `gripper_1_` |
| **第二台起加 `_2`** | 槽名 `d435_rgb_2`、`gripper_2_rgb`；列前缀 `gripper_2_` |
| **新命名空间首台用角色名** | ego 首台就是 `ego_`（没有历史包袱）；第二台 `ego_2_` |
| **编号 = 打开顺序（夹爪）** | `ui/main_window.py:1625`：先开的是 rig1。落盘 `devices[].key` 含序列号，事后可核对；但「哪条轨迹属于哪只手」只存在于打开顺序里（见 §5.2 建议） |

### 2.4 字符集与保留词

- 列名点分段只允许 `[a-z0-9_]+`；**全小写、纯 ASCII**。
- 视频槽名由 `core/device_naming.slot_base()` 清洗（非法字符 → `_`，可含中文）；
  列名**不走**清洗，必须由写入方自己守规则。
- 保留后缀（writer 按后缀定类型，`core/egodata_writer.py:808-830`）：
  `slam_trajectory`、`slam_trajectory_ns`、`_force`、`_force_matrix`、`_force_ns`、
  `_force_matrix_ns`、`_state`、`_imu_quat`、`_imu_valid`、`_hand_pose`。
  新增信号名不得以这些后缀结尾，除非语义就是它们。
- 禁用名：`slam_pose`（已废除，writer 静默忽略）、`gripper_1_*`、
  `observation.images.*`（本仓库视频不进 features）。
- 同一任务的列名**不得跨 episode 改名**（`features` 是任务级并集，改名会留下
  两个键，且旧键永久残留）。

---

## 3. 每段都有的公共列

| 列 | 类型 | 语义 |
|---|---|---|
| `episode_index` | int64 | parquet 全局 1 基序号 = 磁盘段号 `k` + 1（§1.3；`episode-000` 里是 1） |
| `frame_index` | int64 | 段内帧号，0 起递增、无空洞 |
| `timestamp` | float32 | 会话相对秒（该帧的写入时间） |
| `task_index` | int64 | 任务号，现恒 0 |
| `wall_time` | float64 | 本段起始墙钟 + 行相对时刻（v1.3.17 起）：采集时刻的绝对 Unix 秒，段内严格递增 |
| `hardware_ns` | int64 | 该视频帧的采集时刻（v1.3.11 起为「锚点 + 序号 × T」的归一序列） |
| `raw_hw_ns` | int64 | **条件列**：归一前的原始设备戳（v1.3.11）；本段至少一帧带原始戳时才写，其余行 0 |
| `action` | list\<float32,1\> | 恒写占位（当前 `[0.0]`） |
| `status.<id>` | string | 该槽位/传感器在本帧的连接状态（`connected` / `disconnected`） |

`status.<id>` 的 `<id>` 范围 = **`CameraWorker` 视频槽**（只有 UVC 走 `add_camera`，
进 `_slots`）+ 传感器列名（`left_glove`/`right_glove`）+ 夹爪非视频槽
（`gripper_pose`、`gripper_force_left/right` 及其 `gripper_2_*`）。**不含**设备 key，
也**不含外部帧源**（D435/S80M/夹爪 RGB 走 `register_external_source`，不在 `_slots`
里，因此没有 `status.<slot>` 列）。

### 3.1 七种时间语义，不能混

| 时间 | 盖戳方 | 时钟 | 用途 |
|---|---|---|---|
| `wall_time` | 写线程 | 宿主墙钟（Unix 秒） | 行落盘时刻，跨设备粗对齐 |
| `timestamp` | 写线程 | 会话相对秒（f32） | 段内相对时间 |
| `hardware_ns` | 采集侧 | 宿主钟链路=采集时刻（原样）；设备钟链路（D435）归一后均匀递增 | 帧的主时间锚；跨模态对齐优先用 `timestamp` |
| `raw_hw_ns` | 采集侧 | 原始设备钟 | 审计真实到达/采集节奏 |
| `*_ns`（力/矩阵/轨迹） | 采集侧回调 | 宿主单调钟 ns | 样本级精确配对 |
| `slam_trajectory[i][0]` | SLAM 进程 | **相机传感器钟**（秒，ENTER 为原点） | 轨迹内部时间，**与 `hardware_ns` 不同源** |
| `imu_ts_ns` | SDK | 设备钟 ns | IMU 样本时刻 |

⚠ `slam_trajectory` 的 `t` 与 `hardware_ns` 之间只差一个「近似常量但带抖动」的
偏移（实测三个反推估计散布 ~104 ms，已超一帧）。**只能靠同序并行的
`slam_trajectory_ns` 配帧，不许拟合偏移，更不许把 `t` 当宿主时间用。**

---

## 4. 设备类型 → 数据落点

### 4.1 总表

| 设备 kind | 设备 key 示例 | 视频槽（videos/） | parquet 数据列 | info 声明 |
|---|---|---|---|---|
| `uvc` | `uvc:usb-DECXIN_…` | `head_left_rgb`(索引0)、`head_right_rgb`(1)、`head_right_rgb_N`(N≥2) | 仅 `status.<slot>` | `cameras`、`video_extensions`、`devices`、`device_names` |
| `d435`（D435/D405） | `d435:<serial>` | `<base>_rgb`、`<base>_depth`（同前缀多台 `_2`） | `hardware_ns`、`raw_hw_ns`、`depth_ns`、`rgb_dup`、`rgb_gap`、`depth_dup`、`depth_gap`、`ts_src`、`host_ns` | `cameras`（仅 RGB）、`video_extensions`（含深度）、`devices`、`calibration` |
| `s80m`（S80M/S80C） | `s80m:<sn>@<usb路径>` 或 `s80m:ftdi` | `stereo_left`、`stereo_right`、`stereo_depth` | `hardware_ns`、`raw_hw_ns`、`imu_ts_ns`、`observation.imu` | `cameras`、`video_extensions`、`devices`、`calibration.head_stereo` |
| `data_ble`（BLE 手套，主程序录制 kind） | `ble:<MAC>` / `usbglove:<serial>` | 无 | `observation.<sensor>`（16×16）、`status.<sensor>` | `sensors`、`sensor_dim`、`devices`、`features` |
| `ble`（其它 BLE，检测兜底） | `ble:<MAC>` | 无 | 主程序占位、不产数据；lite 的「BLE 手套」入口可按手套录制（数据同 `data_ble`） | 主程序不进 `devices`；lite 以 `ble_glove` 进 `devices` |
| `usb_glove`（USB 手套） | `usbglove:<serial>` | 无 | `observation.<sensor>`、`observation.<sensor>_imu_quat`、`…_imu_valid`、`observation.left/right_hand_pose` | `sensors`、`sensor_dim`、`devices`、`features` |
| `gripper`（UMI 夹爪） | `gripper:<MAC>` | `gripper_rgb`（rig1）/ `gripper_2_rgb`（rig2）；`*_stereo_left` **只显示不录制** | 力/力矩阵/状态/轨迹（§4.6）；RGB 行带 `raw_hw_ns` | `cameras`（仅 RGB）、`video_extensions`、`devices`、`features`、`gripper_slam_frame` |
| ego 相机 | 实际是 `d435`/`s80m`/`uvc` 之一 | 该设备的 RGB 槽 | 该设备已有列 + `observation.ego_slam_trajectory(_ns)`【提案】 | 上述 + `slam_frames`【提案】 |

> 「ego 相机」不是独立 kind，而是一个**角色**：本仓库里通常是头戴 S80C/S80M 双目
> 或 D435。角色与轨迹的绑定写在声明里（§6.6），不靠槽名字符串猜。

### 4.2 UVC 相机

- **落点**：`videos/chunk-{c:03d}/<slot>/episode-{f:03d}.mp4`；data parquet 只有
  `status.<slot>`，**没有观测数值列**。
- **槽名**：`settings._camera_slot_name(index)`：0→`head_left_rgb`，1→`head_right_rgb`，
  N≥2→`head_right_rgb_N`。
- **声明**：`cameras[slot] = {height, width, fps?}`、`video_extensions[slot]="mp4"`；
  设备段 `{key, kind:"uvc", name, slots:[slot]}`。
- 多路 UVC：每路独立槽名，互不覆盖；设计上限 `settings.MAX_CAMERAS = 8`。

### 4.3 D435 / D405（RGB + 深度）

- **视频**：RGB 写 `videos/<rgb_slot>/…`；深度**不建 cameras 条目**，但写
  `videos/<depth_slot>/…`（12-bit 灰度 MP4，x265 无能力时回落 FFV1 MKV）。
- **槽名**：`core/device_naming.allocate_slot_names()` 分配。用户命名为 `D405_head`
  → `D405_head_rgb` / `D405_head_depth`；未命名回落 `d435_rgb` / `d435_depth`；
  同前缀多台追加 `_2`（`d435_rgb_2` / `d435_depth_2`）。
- **数据列**：D435 链路独有取证列（`extra` 非空才写）：
  `depth_ns`(int64)、`host_ns`(int64)、`rgb_dup`/`depth_dup`/`ts_src`(int8)、
  `rgb_gap`/`depth_gap`(int32)。其它设备链路**没有**这些列。
- **深度节拍**：深度帧随主槽（`master_slot = <rgb_slot>`）每拍落一帧；深度源缺帧
  时重复最近帧，保证深度第 i 帧与 RGB 第 i 帧对齐。
- **声明**：`cameras` 只列 RGB 槽；`video_extensions` 列 RGB + 深度两个 key；
  `devices[].slots` 列两个槽。

### 4.4 S80M / S80C 双目

- **视频**：`stereo_left`、`stereo_right`（每路一个 MP4）；`stereo_depth`（12-bit
  灰度，S80C 深度引擎可用时）。
- **数据列**：
  - `hardware_ns`：SDK 硬件戳（与 IMU 同源）；
  - `imu_ts_ns`(list\<int64\>) + `observation.imu`(list\<list\<float32,6\>\>)：
    **只挂在 `stereo_left` 主槽**（左右目共享同一份，避免重复行）；
  - `raw_hw_ns`：归一前原始戳。
- **限制**：`stereo_left/right` 是全局槽名，**同一时刻只支持一台 S80M**；
  与 D435/D405 冲突（共用 `video0/video2`）。
- **声明**：`cameras` 含左右目；`video_extensions` 再加 `stereo_depth`；
  `calibration.head_stereo`（首个注册了标定的设备，即 §6.5 的 `head_stereo`）；
  `devices` 含 `serial`（紧凑段里**不写** serial，见 §6.4）。

### 4.5 数据手套（BLE `data_ble` / `ble`，USB `usb_glove`）

BLE **不是「无数据」通道**：BLE 手套就是常规数据源，16×16 压力帧照常录进
`observation.<sensor>`（`core/ble_engine.py` 的 `SensorBLEEngine.data_ready` →
pipeline `write_sensor`）。「检测成 `ble`」只说明广播名不像手套、且还没绑定
sensor 列，不等于设备没有数据。

- **无视频**，全部进 data parquet：
  | 列 | 类型 | 说明 |
  |---|---|---|
  | `observation.<sensor>` | list\<float32,256\> | 16×16 压力矩阵展平（`sensor_dim=256`）；**固件原始通道号**，左手需 `core.render_engine.canonical_pressure_matrix()` 还原 |
  | `observation.<sensor>_imu_quat` | list\<float32,64\> | 16×4 XYZW 四元数（USB 手套） |
  | `observation.<sensor>_imu_valid` | list\<float32,16\> | 逐传感器有效掩码 |
  | `observation.left_hand_pose` / `right_hand_pose` | list\<float32,63\> | 21 关节 × xyz，**恒写列**，无解算时全零占位 |
  | `status.<sensor>` | string | 连接状态 |
- **检测态 vs 录制态**：
  - `data_ble`：广播名含 `Matrix`/`L`/`R`，或 `device_names.json` 里已绑定
    `sensor` 列 → **主程序**直接按手套打开并录制（`_open_glove`）。曾用 lite
    绑过一次的 MAC，之后在主程序也会被判成 `data_ble`。
  - `ble`：名字不像手套且未绑定 → 主程序只开占位画面（`_open_ble_placeholder`，
    显示「该设备无可视化数据」）；**lite 的「BLE 手套」入口会把它当手套打开**
    （`ui/lite_window.py:_open_ble_glove`），分配 sensor 列并录制，和 `data_ble`
    落盘内容一致。绑定后该 MAC 再被检测就是 `data_ble`。
  - `usb_glove`：USB-C 手套（串口引擎），主程序与 lite 都录。
- **`devices[].kind` 随程序不同**（读端别只认一个值）：
  - 主程序：BLE 与 USB 手套统一写 `kind: "data_ble"`（`glove_entry` 硬编码）；
  - lite：BLE 写 `"ble_glove"`、USB 写 `"usb_glove"`，且 `slots` 放 sensor 列名
    （`["left_glove"]`），因此 lite 的 `device_names` 里有手套列 → 设备绑定。
- **传感器列名**：`left_glove` / `right_glove`，由 `settings.assign_glove_sensor_role()`
  按 MAC/序列号持久化到 `data/device_names.json`；**第 3 只手套拿不到空闲列名，
  拒绝连接**（历史上曾静默映射成 `left_glove` 导致两路互相覆盖）。
- **声明**：`sensors = ["left_glove","right_glove"]`、`sensor_dim=256`、
  `features` 逐列声明；设备段 `{key, kind, name, slots}`（kind/slots 见上）。
  ⚠ **主程序**的紧凑设备段丢掉了 `sensor_column`、`slots` 为空，所以「哪只手套
  对应哪一列」在磁盘上只能靠 `name` 与列名的约定推断；lite 用 `slots=[sensor]`
  补上了这个绑定（§6.4）。

### 4.6 UMI 夹爪（单台 / 双台）

**视频**：`gripper_rgb`（rig1）/ `gripper_2_rgb`（rig2）是唯一录制的流；
`gripper_stereo_left` / `gripper_2_stereo_left` **只用于 SLAM 与显示，不落视频**
（从未注册外部帧源）。

**数据列**（rig1；rig2 全身加 `gripper_2_` 前缀）：

| 列 | 类型 | 说明 |
|---|---|---|
| `observation.gripper_{left,right}_force` | list\<float32,3\> | `[fx,fy,fz]` mN |
| `observation.gripper_{left,right}_force_ns` | int64 标量 | 该力样本采集时刻（宿主单调钟；无样本填 0） |
| `observation.gripper_{left,right}_force_matrix` | list\<int16\> 或 list\<float32\> | 250×250×3 力矩阵，`250×750` 展平；编码见 info `features.encoding` |
| `observation.gripper_{left,right}_force_matrix_ns` | int64 标量 | 力矩阵样本采集时刻（独立泵线程，与力值各记各的） |
| `observation.gripper_state` | list\<float32,3\> | `[open_pct, gripped, fz_mn]` |
| `observation.slam_trajectory` | list\<float64\> | 轨迹点 `[t,x,y,z,qx,qy,qz,qw]`×N，变长 |
| `observation.slam_trajectory_ns` | list\<int64\> | 与轨迹点**同序等长**的宿主时刻 |
| `status.gripper_pose` / `status.gripper_force_left` / `status.gripper_force_right` | string | 非视频槽的连接状态 |

rig2 的实际列名（易错，照抄）：

```
observation.gripper_2_gripper_left_force
observation.gripper_2_gripper_left_force_ns
observation.gripper_2_gripper_left_force_matrix
observation.gripper_2_gripper_left_force_matrix_ns
observation.gripper_2_gripper_state
observation.gripper_2_slam_trajectory
observation.gripper_2_slam_trajectory_ns
```

**稀疏语义**：列级稀疏（本段真的出现过该列才建）；行级无样本时，定长列填 `[0.0]*3`，
变长轨迹填 `[]`。

**声明**：`features` 逐列（力矩阵必须带 `encoding` + `scale`）；有 SLAM 的任务写
`gripper_slam_frame`，并逐列写 `features[...].frame/frame_axes`；设备段 slots 列
全部 5 个槽（含只显示不录制的双目槽与 pose/force 槽）。

### 4.7 ego 相机与 ego SLAM【提案】

ego 相机的**视频/深度/IMU**沿用其宿主设备（§4.2–4.4）的规则，不新增存储位置。
它独有的是**自己的一条 SLAM 轨迹**：

| 列 | 类型 | 说明 |
|---|---|---|
| `observation.ego_slam_trajectory` | list\<float64\> | 点 `[t,x,y,z,qx,qy,qz,qw]`×N，`[]` = 本行无新点 |
| `observation.ego_slam_trajectory_ns` | list\<int64\> | 与点同序等长的**取样帧宿主时刻** |

- 命名用**角色前缀 `ego_`**，不用槽位名（`d435_rgb_` / `stereo_left_`）——换机时角色
  不变、槽名会变；第二台 ego 用 `ego_2_slam_trajectory`。
- 若 ego SLAM 是**离线后处理**产物：允许事后往同一 episode parquet 补这两列，但
  `_ns` 必须是**取样帧的宿主时刻**（与 `hardware_ns` 同时基），不能写处理时刻。
- ego 轨迹的 `frame`/时基/原点与夹爪**大概率不同**，必须逐列声明（§6.6）。

### 4.8 深度通道规则（所有深度设备统一）

- 深度槽**不建 `cameras` 条目**，但必须有 `video_extensions[<depth_key>]`（`mp4` 或 `mkv`）。
- 存储为**单流**：12-bit 灰度 HEVC（`gray12le`，对数深度码）或 FFV1 `gray16le` 无损 MKV；
  解码走 `core/depth_codec.py` / `core/depth_reader.py`。
- 一个深度槽跟随一个主槽（`master_slot`）同步落盘；多深度设备各自独立槽名。
- 深度**不是** parquet 列，读端要靠视频文件 + `video_extensions` 判断。

---

## 5. 同时接入多个设备

### 5.1 能力与冲突矩阵

| 组合 | 可否同时 | 依据 / 限制 |
|---|---|---|
| UVC × N | ✅ | 每路独立槽名（设计上限 8） |
| D435/D405 × N | ✅ | 槽名按前缀编号；每台独立 RGB + 深度槽 |
| D435 + S80M/S80C | ❌ | 共用 `video0/video2`，后开者被拒绝 |
| S80M × S80M | ❌ | 全局槽名 `stereo_left/right`，只支持单台 |
| UMI 夹爪 + 独立 S80M | ❌ | 夹爪 ORB 桥接独占双目 |
| UMI 夹爪 + D435 | ✅（资源需实测） | 代码只拦 S80M；SLAM 与 RGB 争 CPU |
| UMI 夹爪 × 2 | ✅ | 第二台 `gripper_2_` 前缀；rig 号按打开顺序 |
| 数据手套 × 2 | ✅ | BLE/USB 都录；列名按 MAC/序列号绑定（16×16 压力 + USB 的 IMU/骨架） |
| 数据手套 × 3+ | ❌ | 无空闲列名，拒绝连接 |
| 其它 BLE 设备（`ble`，未识别为手套） | ⚠️ | **BLE 本身有数据**：`ble` 只是检测兜底。主程序开占位、不产数据且不进 `devices`；lite 的「BLE 手套」入口可当手套打开并录制（`observation.<sensor>`，devices kind=`ble_glove`） |

### 5.2 多设备隔离的四条规则

1. **视频槽名唯一**：同一会话内两个设备不能共用一个 image_key（会互相覆盖
   `videos/<key>/episode-NNN.mp4`）。夹爪 rig2 靠 `gripper_2_`，D435 第二台靠 `_2`。
2. **列前缀唯一**：夹爪 rig1 空串 / rig2 `gripper_2_`；手套用传感器名；ego `ego_`。
   同一个 `observation.<x>` 不允许两个写入方（历史上第 3 只手套被映射到
   `left_glove` 就是这种静默坏）。
3. **设备 key 写进 `devices`**：`key` 含序列号/MAC，是事后判断「哪台设备写的哪条
   轨迹」的唯一硬件依据。
4. **rig 号与物理手的绑定**：当前 rig 号 = 打开顺序（先开 = rig1）。建议【提案】
   - 把 rig 号绑定到 `gripper:<MAC>` 并持久化（`data/device_names.json` 加 `rig` 字段），
   - 在轨迹 feature 的 `owner` 里写 `device_key` + `hand`（`left`/`right`），
   - 这样同一个任务目录跨人、跨天采集时，rig1 的物理意义不会漂。

### 5.3 一行一帧的对齐模型（MUST 理解）

写线程以 30 fps tick 建行；每个 tick 对每个视频槽取**队列里最旧的一帧**。因此：

- **行号相同 ≠ 物理时刻相同**。不同设备的画面差一个队列滞留（力样本恒领先 RGB，
  滞留量随段内时长变化）。精确配对一律用 `hardware_ns` / `*_ns`，不要用行号。
- **深度随主槽**：深度第 i 帧与主槽第 i 帧对齐（缺帧重复最近帧）。
- **传感器/力/状态是 latest-wins 快照**：每行取「当下最新」样本，所以必须靠
  `*_ns` 才知道它到底采于何时。
- **SLAM 轨迹是「本行窗口内累积的点列表」**：一行可以有 0 个、1 个或多个轨迹点；
  `slam_trajectory_ns` 与点列表锁步，下标即配对。
- **行数 ≠ 点数**：写线程落后超过一帧会跳 tick，轨迹侧也可能丢点；按时间戳配对后
  剩余的未配对点/帧是正常现象。

### 5.4 多设备时间基速查

| 数据 | 用哪个时间配帧 |
|---|---|
| 视频帧 | `hardware_ns`（归一后） |
| 力 / 力矩阵 | 各自 `*_ns`（宿主单调钟） |
| SLAM 轨迹点 | `slam_trajectory_ns`（宿主单调钟）↔ `hardware_ns` |
| IMU | `imu_ts_ns`（设备钟） |
| 跨设备粗对齐 | `wall_time` |

`slam_trajectory[i][0]` 是**相机传感器钟**，只用于轨迹内部（速度/时间差），
不要拿它直接减 `hardware_ns`。

### 5.5 稀疏列与缺数据语义

| 情况 | 表现 | 读取方处理 |
|---|---|---|
| 本段从未出现某设备 | 该列不在 parquet schema | 按缺列处理（`info.features` 是任务级并集，**不代表本段有**） |
| 本段有列但本行无样本 | 定长 `[0.0]*d`；变长 `[]`；标量 `0` | `0`/`[]` = 本行无新样本，**不是**真实零值 |
| 轨迹点缺宿主戳 | `slam_trajectory_ns` 对应位置为 `0` | 该点不可按时间配帧 |
| 整段轨迹都无戳 | `*_slam_trajectory_ns` 列**不创建** | 只能退回行号粗对齐（不建议） |
| 设备中途断连 | 有 status 列的设备（UVC/手套/夹爪非视频槽）该列变 `disconnected`；D435/S80M/夹爪 RGB 没有状态列 | 外部帧源要看数据列是否停止更新，不能等 status |

### 5.6 典型组合：2 夹爪 + 1 ego（S80C）+ 2 手套

**视频文件**（每段）：

```
videos/chunk-000/stereo_left/episode-000.mp4        # ego 左目（主槽，带 IMU 行）
videos/chunk-000/stereo_right/episode-000.mp4       # ego 右目
videos/chunk-000/stereo_depth/episode-000.mp4       # ego 深度（12-bit 灰度）
videos/chunk-000/gripper_rgb/episode-000.mp4        # 夹爪 rig1 RGB
videos/chunk-000/gripper_2_rgb/episode-000.mp4      # 夹爪 rig2 RGB
```

**data parquet 主要列**（列序以实现为准，此处按来源分组）：

```
episode_index, frame_index, timestamp, task_index, wall_time,
hardware_ns, raw_hw_ns, action,
imu_ts_ns, observation.imu,
observation.left_glove, observation.right_glove,
observation.left_glove_imu_quat, observation.left_glove_imu_valid,
observation.right_glove_imu_quat, observation.right_glove_imu_valid,
observation.left_hand_pose, observation.right_hand_pose,
observation.gripper_state, observation.gripper_left_force, observation.gripper_right_force,
observation.gripper_left_force_ns, observation.gripper_right_force_ns,
observation.gripper_left_force_matrix, observation.gripper_right_force_matrix,
observation.gripper_left_force_matrix_ns, observation.gripper_right_force_matrix_ns,
observation.slam_trajectory, observation.slam_trajectory_ns,
observation.gripper_2_gripper_state, observation.gripper_2_gripper_left_force, …,
observation.gripper_2_slam_trajectory, observation.gripper_2_slam_trajectory_ns,
observation.ego_slam_trajectory, observation.ego_slam_trajectory_ns,
status.gripper_pose, status.gripper_force_left, status.gripper_force_right,
status.gripper_2_pose, status.gripper_2_force_left, status.gripper_2_force_right,
status.left_glove, status.right_glove
```

> 该组合**没有** D435 的取证列（`depth_ns` / `rgb_dup` / `ts_src` 等）；也**没有**
> `status.stereo_left/right`、`status.gripper_rgb`——它们是外部帧源，不建状态列。

---

## 6. `meta/info.json` 声明规范

### 6.1 顶层键

| 键 | 必填 | 说明 |
|---|---|---|
| `format` | ✅ | 恒 `"pooled_episodes_v1"`（格式判别键） |
| `chunks_size` / `data_path` / `video_path` / `episodes_path` | ✅ | 路径模板 |
| `codebase_version` / `app_version` | ✅ | 数据基准版本 / 程序版本 |
| `fps` / `video` / `task_name` | ✅ | 默认帧率 / 是否含视频 / 任务名 |
| `features` | ✅ | **逐列声明**（§6.2）：本任务出现过的所有 parquet 列 |
| `cameras` | ✅ | RGB 槽 → `{height, width, fps?}`；**深度槽不入此表** |
| `video_extensions` | ✅ | **每个 image_key**（含深度）→ `mp4`/`mkv` |
| `devices` | ✅ | 设备紧凑段 `{key,kind,name,slots}` |
| `device_names` | ✅ | `devices[].slots` 的**全部槽位** → 用户命名（含深度/不录制槽） |
| `sensors` / `sensor_dim` | ✅ | 传感器列名列表 / 维度（256） |
| `calibration` | ✅ | 任务级最新标定（§6.5） |
| `total_episodes` | ✅ | 当前段数（删除不回退） |
| `created_at` | ✅ | 任务创建 Unix 秒 |
| `gripper_slam_frame` | 条件 | 有**夹爪** SLAM 轨迹才写；单数，只为兼容 |
| `slam_frames` | 【提案】 | 轨迹列 → frame id 的映射（§6.6） |

**只加不改**：服务器上传严格依赖既有键名/类型/结构。新增信息一律用新键，
既有键的值可以更新，但不能删、不能改类型、不能改语义。

### 6.2 `features` 逐列声明规则

`features` 的键 = **完整列名**（含 `observation.` 前缀）；值是该列的编码描述。

| 列形态 | 声明样板 |
|---|---|
| 定长数值序列 | `{"dtype":"float32","shape":[3]}` |
| 定长矩阵 | `{"dtype":"float32","shape":[16,16]}`（压力矩阵必须是 2D，不能是 `[256]`） |
| 手部关键点 | `{"dtype":"float32","shape":[21,3]}` |
| IMU 样本序列 | `{"dtype":"float32","shape":[6]}`（每样本 6 轴） |
| 手套 IMU | `{"dtype":"float32","shape":[16,4]}` / `{"dtype":"float32","shape":[16]}` |
| SLAM 点列 | `{"dtype":"float64","shape":[8],"encoding":"flat_points_8_txyz_qxyzw","frame":"…","frame_axes":{…}}` |
| SLAM 时刻列 | `{"dtype":"int64","shape":[1],"encoding":"flat_parallel_to_slam_trajectory","clock":"host_monotonic_ns"}` |
| 力矩阵 | `{"dtype":"int16","shape":[250,250,3],"encoding":"row_diff_quantized","scale":10}` |
| 力矩阵（原值档） | `{"dtype":"float32","shape":[250,250,3],"encoding":"row_flat_raw","scale":1}` |
| 力/状态 | `{"dtype":"float32","shape":[3]}` |
| 采集时刻标量 | `{"dtype":"int64","shape":[1],"encoding":"host_monotonic_ns","missing_sentinel":0}` |
| 动作占位 | `{"dtype":"float32","shape":[1]}` |

- `shape` 指**单个元素**的形状，不是每行总长；变长列（list）也写元素形状
  （所以轨迹是 `[8]`、时刻是 `[1]`）。
- `encoding` 是读端解码依据，**不能省**（尤其力矩阵的 `scale`）。
- `frame` / `frame_axes` 逐列独立；**同一任务里不同轨迹可以不同帧**。
- 视频流**不进** `features`（用 `cameras` + `video_extensions`）。

### 6.3 `cameras` 与 `video_extensions`

```json
{
  "cameras": {
    "stereo_left":  {"height": 800, "width": 1280},
    "stereo_right": {"height": 800, "width": 1280, "fps": 25.0},
    "gripper_rgb":  {"height": 960, "width": 1280}
  },
  "video_extensions": {
    "stereo_left": "mp4", "stereo_right": "mp4", "stereo_depth": "mp4",
    "gripper_rgb": "mp4", "gripper_2_rgb": "mp4"
  }
}
```

- `cameras` 只放**有 RGB 视频的槽**；深度槽只在 `video_extensions` 里出现。
- `fps` 仅在该流与默认 `fps` 不同时才写。
- 读端判断文件是否存在/是什么格式**一律先看 `video_extensions`**，不要按扩展名猜。

### 6.4 `devices` / `device_names` / `sensors`

```json
{
  "devices": [
    {"key":"s80m:000000000001@1-3.4.1","kind":"s80m","name":"S80C_head",
     "slots":["stereo_left","stereo_right","stereo_depth"]},
    {"key":"usbglove:364933593535","kind":"data_ble","name":"USB 手套_left","slots":[]},
    {"key":"gripper:CC:BA:97:25:B4:44","kind":"gripper","name":"UMI 夹爪001",
     "slots":["gripper_stereo_left","gripper_rgb","gripper_pose",
              "gripper_force_left","gripper_force_right"]}
  ],
  "device_names": {"stereo_left":"S80C_head","gripper_rgb":"UMI 夹爪001"}
}
```

（示例只列了部分键；实际写入 `devices[].slots` 的**全部**槽位，见附录 B。）

- 紧凑段**有意不写** `serial` / `fays_serial` / `sensor_column`（隐私与上传契约）；
  硬件身份在 `key` 里。
- `slots` 是**设备注册的槽位全集**，包含不录制视频的槽（夹爪双目/pose/force、
  S80M 深度槽）；「哪些真的录了」看 `cameras` + `video_extensions`。
- `device_names` 映射 `devices[].slots` 里的**全部槽位**（含深度槽与不录制的槽），
  不只是视频槽。
- **手套的设备条目分两种写法**（`devices[].kind` 不再是单值）：
  - **主程序**：BLE 与 USB 手套统一 `kind:"data_ble"`、`slots:[]`；`sensor_column`
    被紧凑化丢掉，所以 `observation.left_glove` 与设备 `key` 之间**没有机器可读
    的绑定**，只能靠 `name` 与列名的约定推断。
  - **lite**：`kind:"ble_glove"` / `"usb_glove"`，`slots:["left_glove"]`（放的是
    sensor 列名），因此 `device_names["left_glove"]` 直接给出「哪只设备写哪一列」。
  - 【提案】主程序也把手套的 `"sensor_column"`（`build_device_meta()` 已产出，
    **单数**、字符串）保留进紧凑段；夹爪保留 `"sensor_columns"`（复数、列表）
    并补 `"rig":1`。都是 additive，不破坏既有键。
- `sensors` 只列本任务出现过的传感器列名；`sensor_dim` 恒 256。

### 6.5 `calibration`

- 结构：`{标定键: StereoCalibration.to_dict()}`，内容含
  `type/name/resolution/fps/baseline/left_camera/right_camera/depth_scale/cam_imu_timeshift`，
  相机内参为 `{intrinsic:[fx,fy,cx,cy], distortion:[...]}`。
- 键分配（`core/egodata_writer._plan_calibration_dict`）：
  - **第一个**注册了标定的设备 → `head_stereo`（服务器/回放/stereo_triangulate 兼容路径）；
  - 其余 → `<首槽名去掉 _N 编号>_calibration`，如 `d435_rgb_calibration`。
- 每段 episodes 行的 `calibration` 列保存**该段**的同一份 dict（JSON 字符串），
  任务级 `info.calibration` 是最新值。
- ⚠ `head_stereo` 归属取决于 `devices` 顺序（= 设备打开顺序，仅 D435/S80M 注册
  标定）。两台 D435 时先开的拿 `head_stereo`；ego 双目必须保证排第一，否则
  `head_stereo` 会指到别的设备。读端不要假设 `head_stereo` 一定是头戴相机。

### 6.6 多条 SLAM 轨迹的声明【提案】

三条轨迹共存时，**每列一条完整声明**，顶层再给一个便利映射。权威在 `features`。

```json
{
  "features": {
    "observation.slam_trajectory": {
      "dtype": "float64", "shape": [8],
      "encoding": "flat_points_8_txyz_qxyzw",
      "frame": "down_left_forward",
      "frame_axes": {"+X": "down", "+Y": "left", "+Z": "forward"},
      "time": {"t": "sensor_s", "clock": "camera_sensor", "epoch": "enter_press"},
      "owner": {"role": "gripper", "rig": 1, "device_key": "gripper:CC:BA:97:25:B4:44"},
      "source_slot": "gripper_stereo_left"
    },
    "observation.slam_trajectory_ns": {
      "dtype": "int64", "shape": [1],
      "encoding": "flat_parallel_to_slam_trajectory",
      "clock": "host_monotonic_ns"
    },
    "observation.gripper_2_slam_trajectory": {
      "dtype": "float64", "shape": [8],
      "encoding": "flat_points_8_txyz_qxyzw",
      "frame": "down_left_forward",
      "frame_axes": {"+X": "down", "+Y": "left", "+Z": "forward"},
      "time": {"t": "sensor_s", "clock": "camera_sensor", "epoch": "enter_press"},
      "owner": {"role": "gripper", "rig": 2, "device_key": "gripper:68:EE:8F:C6:0D:BC"},
      "source_slot": "gripper_2_stereo_left"
    },
    "observation.gripper_2_slam_trajectory_ns": {
      "dtype": "int64", "shape": [1],
      "encoding": "flat_parallel_to_slam_trajectory",
      "clock": "host_monotonic_ns"
    },
    "observation.ego_slam_trajectory": {
      "dtype": "float64", "shape": [8],
      "encoding": "flat_points_8_txyz_qxyzw",
      "frame": "ego_cam_optical",
      "frame_axes": {"+X": "right", "+Y": "down", "+Z": "forward"},
      "time": {"t": "sensor_s", "clock": "camera_sensor", "epoch": "first_frame"},
      "owner": {"role": "ego", "device_key": "s80m:000000000001@1-3.4.1"},
      "source_slot": "stereo_left"
    },
    "observation.ego_slam_trajectory_ns": {
      "dtype": "int64", "shape": [1],
      "encoding": "flat_parallel_to_slam_trajectory",
      "clock": "host_monotonic_ns"
    }
  },
  "gripper_slam_frame": "down_left_forward",
  "slam_frames": {
    "observation.slam_trajectory": "down_left_forward",
    "observation.gripper_2_slam_trajectory": "down_left_forward",
    "observation.ego_slam_trajectory": "ego_cam_optical"
  }
}
```

字段含义：

| 字段 | 必填 | 说明 |
|---|---|---|
| `frame` | 条件 | 坐标帧 id。**键不在 = 旧帧**（≤v1.3.12），不是取默认值 |
| `frame_axes` | 条件 | 各轴在现实中指向哪；跨帧对齐**只看方向词**，不看轴字母 |
| `time.t` | 【提案】 | `t` 字段的单位（现有恒 `sensor_s`） |
| `time.clock` | 【提案】 | `camera_sensor`（相机传感器钟）或 `host_monotonic`（宿主单调钟） |
| `time.epoch` | 【提案】 | 原点：`enter_press` / `first_frame` / `device_boot` |
| `owner.role` | 【提案】 | `gripper` / `ego` |
| `owner.rig` | 【提案】 | 夹爪 rig 号（1 起） |
| `owner.hand` | 可选 | `left` / `right`，物理手绑定（见 §5.2 第 4 条） |
| `owner.device_key` | 【提案】 | 与 `devices[].key` 对应，用于溯源 |
| `source_slot` | 【提案】 | `_ns` 时刻对应的采样槽位；**可能不录制视频**（如夹爪双目槽），读端据此知道这条轨迹来自哪路画面/哪台设备 |

**为什么必须逐列 `frame`**：v1.3.x 的实现是**一个全局帧**套给所有
`*slam_trajectory` 列（`core/egodata_writer.py:1146`、`:1214`）。三条轨迹共存时，
ego 的坐标帧会被贴成夹爪的 `down_left_forward`——数值正常、语义全错，和
v1.3.13 那次坐标帧改版是同一类坑。落地前必须改成「按列取帧」。

**顶层 `slam_frames` 与 `features[...].frame` 必须由同一张表生成**，不要手写两遍；
`gripper_slam_frame` 保留（值 = rig1 的帧），只为老读端。

**三条轨迹默认不在同一坐标系**：各自的原点/朝向独立（夹爪是 ENTER 时刻，
ego 是首帧或上电）。要统一到某一坐标系属于**派生**（外参放 `calibration`），
不要改写原始轨迹列。

### 6.7 任务级合并语义（坑）

`info.json` 的 `features` / `cameras` / `video_extensions` 是**按并集合并、
值以最新 episode 为准**（`_write_info_json` 的 `merged_features.update(...)`）：

- 某设备只在前几段出现 → 后面的段没有该列，但 `features` 仍声明它；
- 某段 ego SLAM 失败 → 该段没有轨迹列，`features` 仍有；
- 同一任务跨了坐标帧改版 → `frame` 只能记住最后一次写入的值，早期段被标错；
- 力矩阵换档 → 任务级 `scale` 被最后一段覆盖。

**读端 MUST 以「本段 parquet 的实际 schema」判断本段有哪些列**，
`info.json` 只用于解释列的含义与回退默认值。

### 6.8 声明与列的一致性（写入方 MUST）

1. `features` 的键集合 = 本任务出现过的列（可多于本段）。
2. 每一条 `observation.*` 列都有对应 feature；`status.*` 与顶层键列**不进** features。
3. 同一列在 `features` 与 `meta/episodes` 行中的描述不得矛盾（目前只有力矩阵有
   双份声明，权威顺序见 §7.3）。
4. 不认识的键**不要**写进 info 让读端猜（例如已废除的 `slam_pose`）。

---

## 7. `meta/episodes` 每段声明

### 7.1 现有列（每段一行）

`episode_index`、`task_index`、`start_frame_index`、`end_frame_index`、`length`、
`created_at`、`duration_sec`、`drop_stats`、`video_codec`、`calibration`、
`force_matrix_specs`（后四列是 JSON 字符串，读取方须 `json.loads`）。

### 7.2 新增 `slam_specs`【提案】

为了让「每段到底有哪几条轨迹、各自什么帧/时基」可逐段判定，建议在
episodes 行加一列 `slam_specs`（string，JSON），结构与 §6.6 的单列声明同型：

```json
{
  "observation.slam_trajectory": {
    "frame": "down_left_forward",
    "frame_axes": {"+X": "down", "+Y": "left", "+Z": "forward"},
    "time": {"t": "sensor_s", "clock": "camera_sensor", "epoch": "enter_press"},
    "owner": {"role": "gripper", "rig": 1}
  },
  "observation.ego_slam_trajectory": {
    "frame": "ego_cam_optical",
    "frame_axes": {"+X": "right", "+Y": "down", "+Z": "forward"},
    "time": {"t": "sensor_s", "clock": "camera_sensor", "epoch": "first_frame"},
    "owner": {"role": "ego"}
  }
}
```

- 缺列 = 旧 episode，按 `info.json` 解释。
- 加列时旧行必须补默认值 `"{}"`（`_episode_rows_table` 已按 schema 补默认，
  但新增列要同步进 `_EPISODE_COLUMNS`），否则新老行拼不成一张表。

### 7.3 每段级 vs 任务级：权威顺序

| 信息 | 权威 | 回退 |
|---|---|---|
| 力矩阵 `scale`/`encoding` | 本段 `force_matrix_specs` | `info.features[列]` → 1 |
| SLAM `frame`/`time` | 本段 `slam_specs`【提案】 | `info.features[列]` |
| 有无某列 | 本段 parquet schema | 无（不能看 info） |
| 视频扩展名 | `info.video_extensions` | 无 |

先例：力矩阵就是「每段一份」才没被后一段的档位覆盖（`file_format.md` §7.2.1）。

---

## 8. 校验清单

### 8.1 写入方（MUST）

1. 每台设备的视频落到唯一 image_key；深度槽不进 `cameras`、必须进 `video_extensions`。
2. 每台设备的观测列用唯一前缀；第 1 台夹爪保持空串前缀，第 2 台 `gripper_2_`。
3. 变长轨迹：无点写 `[]`；`_ns` 与点锁步（缺戳补 `0`，不跳过）；全段无戳不建 `_ns` 列。
4. 力矩阵列整段只用一种 dtype；`scale` 同时写进 features 与 `force_matrix_specs`。
5. `features` 只声明真实出现过的列；每列带 `dtype`/`shape`，能变长的带 `encoding`。
6. **每条** SLAM 列独立写 `frame`/`frame_axes`（不要全局套一个）；先把
   `gripper_slam_frame` 的写入条件收紧到夹爪前缀，避免只有 ego 的段被贴上夹爪帧。
7. `devices[].slots` 写设备全部槽；`device_names` 写这些槽 → 用户命名；`sensors` 写
   传感器列。手套的设备↔列绑定：主程序靠 `sensor_column`（当前紧凑化会丢，见 §6.4），
   lite 靠 `slots=[sensor]`。
8. 新键只加不改；`episodes` 表加列时同步 `_EPISODE_COLUMNS`。

### 8.2 读取方（MUST）

1. `format == "pooled_episodes_v1"` 才按本文读；未知格式报错不猜。
2. 先读**本段 parquet schema** 判断列存在性，再读 `info.features` 解释含义。
3. 轨迹一律按 `observation.*slam_trajectory` 匹配，**别写死** `gripper_1_`
   （那个键不存在）。
4. 配帧用同名列 + `_ns`，`searchsorted` 找最近邻；`0` 视为无戳。
   力/矩阵的 `_ns` 是**标量**（该行那个样本的时刻），轨迹的 `_ns` 是**同行等长列表**。
5. `frame` 键不在 = 旧帧（`right_forward_up`）；不同轨迹的 `frame` 可能不同，
   按 `frame_axes` 的方向词解释，不按轴字母对齐。
6. 力矩阵先看本段 `force_matrix_specs`，再回退 `features.scale`，默认 1。
7. 视频扩展名看 `video_extensions`；深度走 gray12le/FFV1 专用解码。
8. 忽略 dotfiles、`.lock`、`recycled_episode.json`、临时件。

### 8.3 读三条轨迹的最小伪代码

```python
import json, pyarrow.parquet as pq

info = json.load(open(f"{task}/meta/info.json", encoding="utf-8"))
t = pq.read_table(f"{task}/data/chunk-000/episode-000.parquet")
cols = [c for c in t.column_names if c.endswith("slam_trajectory")]

for col in cols:                                   # 排序稳定，便于逐条处理
    spec = info["features"][col]
    frame = spec.get("frame", "right_forward_up")  # 键不在 = 旧帧
    axes  = spec.get("frame_axes")
    pts   = t.column(col).to_pylist()              # 每行 list[8] 或 []
    ns    = (t.column(col + "_ns").to_pylist()
             if col + "_ns" in t.column_names else None)
    # ns 与 pts 逐行等长；点级配对：zip(每 8 个值为一点, ns 行内列表)
```

---

## 9. 兼容、迁移与落地改动

### 9.1 已有数据

- **不要重命名** `observation.slam_trajectory` / `gripper_*` 任何既有列，
  也不要给 rig1 补 `gripper_1_` 前缀——`UMIGripper_Action_AI`（153 段）、
  `UMI_9_29`（204 段）和全部读端都按旧名。
- `UMIGripper_Action_AI` 里还有历史 `observation.slam_pose` 列：读端可忽略，
  新写入**不再产生**。
- 旧帧数据（无 `frame` 键）与 v1.3.13 左乘/1.3.14 右乘的差异见 `docs/data.md`
  §4.5.1；不要按 `app_version` 猜乘侧。

### 9.2 新增「双夹爪 + ego 三轨迹」的改动点

| 位置 | 改动 |
|---|---|
| `core/pipeline.py:1045` `write_slam_trajectory(prefix=…)` | ego 产出端传 `prefix="ego_"`；rig2 已有 `"gripper_2_"` 通路 |
| `core/egodata_writer.py:1146` | 全局 `slam_frame` → 按列取帧的映射（从采集侧/产出端拿） |
| `core/egodata_writer.py:1289` | `gripper_slam_frame` 只在有**夹爪**轨迹时写 |
| `core/egodata_writer.py:1180-1216` | 每条轨迹补 `time`/`owner`/`source_slot`；新增 `slam_frames` |
| `config/settings.py` | 新增 ego 坐标帧注册（`ego_cam_optical` + 轴表），与夹爪帧表分开；`KSQ_GRIPPER_SLAM_FRAME` 只作用于夹爪 |
| `core/egodata_writer.py:_EPISODE_COLUMNS` | 加 `slam_specs`（JSON 字符串，默认 `"{}"`）【提案】 |
| `tools/tests/test_gripper_slam_columns.py` | 加 ego 用例：三列并存、各自 `frame` 不串、只有 ego 时不写 `gripper_slam_frame`、`_ns` 逐行等长 |

### 9.3 验证命令

```bash
QT_QPA_PLATFORM=offscreen venv/bin/python tools/tests/test_gripper_slam_columns.py
venv/bin/python tools/tests/lite_gripper_smoke_test.py
```

---

## 附录 A · 名称速查

| 设备 | 视频槽 | 关键列 | info 声明 |
|---|---|---|---|
| UVC | `head_left_rgb` / `head_right_rgb` / `head_right_rgb_N` | `status.<slot>` | cameras / video_extensions |
| D435/D405 | `<base>_rgb` / `<base>_depth` | `depth_ns`、`rgb_dup`、`rgb_gap`… | cameras / video_extensions / calibration |
| S80M/S80C | `stereo_left` / `stereo_right` / `stereo_depth` | `imu_ts_ns`、`observation.imu` | cameras / video_extensions / head_stereo |
| 手套（BLE/USB） | 无 | `observation.left_glove`、`…_imu_quat`、`…_imu_valid`、`left/right_hand_pose` | sensors / features / devices（kind 随程序：`data_ble` 或 `ble_glove`/`usb_glove`） |
| 夹爪 rig1 | `gripper_rgb`（双目槽不录） | `gripper_left/right_force(_ns)`、`_force_matrix(_ns)`、`gripper_state`、`slam_trajectory(_ns)` | features / gripper_slam_frame |
| 夹爪 rig2 | `gripper_2_rgb` | `gripper_2_gripper_*`、`gripper_2_slam_trajectory(_ns)` | features |
| ego | 宿主设备的 RGB 槽 | `ego_slam_trajectory(_ns)`【提案】 | features / slam_frames |

## 附录 B · 完整 `info.json` 示例（2 夹爪 + ego S80C + 2 手套）

```json
{
  "format": "pooled_episodes_v1",
  "chunks_size": 1000,
  "data_path": "data/chunk-{c:03d}/episode-{f:03d}.parquet",
  "video_path": "videos/chunk-{c:03d}/{image_key}/episode-{f:03d}.{ext}",
  "episodes_path": "meta/episodes/chunk-{c:03d}/episode-{f:03d}.parquet",
  "codebase_version": "v3.0",
  "app_version": "1.4.0",
  "fps": 30.0,
  "video": true,
  "task_name": "示例任务",
  "features": {
    "observation.left_glove": {"dtype": "float32", "shape": [16, 16]},
    "observation.right_glove": {"dtype": "float32", "shape": [16, 16]},
    "observation.left_glove_imu_quat": {"dtype": "float32", "shape": [16, 4]},
    "observation.left_glove_imu_valid": {"dtype": "float32", "shape": [16]},
    "observation.right_glove_imu_quat": {"dtype": "float32", "shape": [16, 4]},
    "observation.right_glove_imu_valid": {"dtype": "float32", "shape": [16]},
    "observation.left_hand_pose": {"dtype": "float32", "shape": [21, 3]},
    "observation.right_hand_pose": {"dtype": "float32", "shape": [21, 3]},
    "observation.imu": {"dtype": "float32", "shape": [6]},
    "observation.gripper_state": {"dtype": "float32", "shape": [3]},
    "observation.gripper_left_force": {"dtype": "float32", "shape": [3]},
    "observation.gripper_right_force": {"dtype": "float32", "shape": [3]},
    "observation.gripper_left_force_ns": {"dtype": "int64", "shape": [1], "encoding": "host_monotonic_ns"},
    "observation.gripper_right_force_ns": {"dtype": "int64", "shape": [1], "encoding": "host_monotonic_ns"},
    "observation.gripper_left_force_matrix": {"dtype": "int16", "shape": [250, 250, 3], "encoding": "row_diff_quantized", "scale": 10},
    "observation.gripper_right_force_matrix": {"dtype": "int16", "shape": [250, 250, 3], "encoding": "row_diff_quantized", "scale": 10},
    "observation.gripper_left_force_matrix_ns": {"dtype": "int64", "shape": [1], "encoding": "host_monotonic_ns"},
    "observation.gripper_right_force_matrix_ns": {"dtype": "int64", "shape": [1], "encoding": "host_monotonic_ns"},
    "observation.gripper_2_gripper_state": {"dtype": "float32", "shape": [3]},
    "observation.gripper_2_gripper_left_force": {"dtype": "float32", "shape": [3]},
    "observation.gripper_2_gripper_right_force": {"dtype": "float32", "shape": [3]},
    "observation.gripper_2_gripper_left_force_ns": {"dtype": "int64", "shape": [1], "encoding": "host_monotonic_ns"},
    "observation.gripper_2_gripper_right_force_ns": {"dtype": "int64", "shape": [1], "encoding": "host_monotonic_ns"},
    "observation.gripper_2_gripper_left_force_matrix": {"dtype": "int16", "shape": [250, 250, 3], "encoding": "row_diff_quantized", "scale": 10},
    "observation.gripper_2_gripper_right_force_matrix": {"dtype": "int16", "shape": [250, 250, 3], "encoding": "row_diff_quantized", "scale": 10},
    "observation.gripper_2_gripper_left_force_matrix_ns": {"dtype": "int64", "shape": [1], "encoding": "host_monotonic_ns"},
    "observation.gripper_2_gripper_right_force_matrix_ns": {"dtype": "int64", "shape": [1], "encoding": "host_monotonic_ns"},
    "observation.slam_trajectory": {
      "dtype": "float64", "shape": [8], "encoding": "flat_points_8_txyz_qxyzw",
      "frame": "down_left_forward",
      "frame_axes": {"+X": "down", "+Y": "left", "+Z": "forward"},
      "time": {"t": "sensor_s", "clock": "camera_sensor", "epoch": "enter_press"},
      "owner": {"role": "gripper", "rig": 1, "device_key": "gripper:CC:BA:97:25:B4:44"},
      "source_slot": "gripper_stereo_left"
    },
    "observation.slam_trajectory_ns": {"dtype": "int64", "shape": [1], "encoding": "flat_parallel_to_slam_trajectory", "clock": "host_monotonic_ns"},
    "observation.gripper_2_slam_trajectory": {
      "dtype": "float64", "shape": [8], "encoding": "flat_points_8_txyz_qxyzw",
      "frame": "down_left_forward",
      "frame_axes": {"+X": "down", "+Y": "left", "+Z": "forward"},
      "time": {"t": "sensor_s", "clock": "camera_sensor", "epoch": "enter_press"},
      "owner": {"role": "gripper", "rig": 2, "device_key": "gripper:68:EE:8F:C6:0D:BC"},
      "source_slot": "gripper_2_stereo_left"
    },
    "observation.gripper_2_slam_trajectory_ns": {"dtype": "int64", "shape": [1], "encoding": "flat_parallel_to_slam_trajectory", "clock": "host_monotonic_ns"},
    "observation.ego_slam_trajectory": {
      "dtype": "float64", "shape": [8], "encoding": "flat_points_8_txyz_qxyzw",
      "frame": "ego_cam_optical",
      "frame_axes": {"+X": "right", "+Y": "down", "+Z": "forward"},
      "time": {"t": "sensor_s", "clock": "camera_sensor", "epoch": "first_frame"},
      "owner": {"role": "ego", "device_key": "s80m:000000000001@1-3.4.1"},
      "source_slot": "stereo_left"
    },
    "observation.ego_slam_trajectory_ns": {"dtype": "int64", "shape": [1], "encoding": "flat_parallel_to_slam_trajectory", "clock": "host_monotonic_ns"},
    "action": {"dtype": "float32", "shape": [1]}
  },
  "cameras": {
    "stereo_left": {"height": 800, "width": 1280},
    "stereo_right": {"height": 800, "width": 1280},
    "gripper_rgb": {"height": 960, "width": 1280},
    "gripper_2_rgb": {"height": 960, "width": 1280}
  },
  "video_extensions": {
    "stereo_left": "mp4", "stereo_right": "mp4", "stereo_depth": "mp4",
    "gripper_rgb": "mp4", "gripper_2_rgb": "mp4"
  },
  "devices": [
    {"key": "s80m:000000000001@1-3.4.1", "kind": "s80m", "name": "S80C_head",
     "slots": ["stereo_left", "stereo_right", "stereo_depth"]},
    {"key": "gripper:CC:BA:97:25:B4:44", "kind": "gripper", "name": "UMI 夹爪001",
     "slots": ["gripper_stereo_left", "gripper_rgb", "gripper_pose",
               "gripper_force_left", "gripper_force_right"]},
    {"key": "gripper:68:EE:8F:C6:0D:BC", "kind": "gripper", "name": "UMI 夹爪002",
     "slots": ["gripper_2_stereo_left", "gripper_2_rgb", "gripper_2_pose",
               "gripper_2_force_left", "gripper_2_force_right"]},
    {"key": "usbglove:364933593535", "kind": "data_ble", "name": "USB 手套_left", "slots": []},
    {"key": "usbglove:364133593535", "kind": "data_ble", "name": "USB 手套_right", "slots": []}
  ],
  "device_names": {
    "stereo_left": "S80C_head", "stereo_right": "S80C_head", "stereo_depth": "S80C_head",
    "gripper_stereo_left": "UMI 夹爪001", "gripper_rgb": "UMI 夹爪001",
    "gripper_pose": "UMI 夹爪001", "gripper_force_left": "UMI 夹爪001",
    "gripper_force_right": "UMI 夹爪001",
    "gripper_2_stereo_left": "UMI 夹爪002", "gripper_2_rgb": "UMI 夹爪002",
    "gripper_2_pose": "UMI 夹爪002", "gripper_2_force_left": "UMI 夹爪002",
    "gripper_2_force_right": "UMI 夹爪002"
  },
  "sensors": ["left_glove", "right_glove"],
  "sensor_dim": 256,
  "created_at": 1790000000.0,
  "calibration": {
    "head_stereo": {
      "type": "stereo_rgbd_camera",
      "name": "head_stereo",
      "resolution": [1280, 800],
      "fps": 30.0,
      "baseline": 0.095,
      "left_camera": {"intrinsic": [600.0, 600.0, 640.0, 400.0], "distortion": [0.0, 0.0, 0.0, 0.0, 0.0]},
      "right_camera": {"intrinsic": [600.0, 600.0, 640.0, 400.0], "distortion": [0.0, 0.0, 0.0, 0.0, 0.0]},
      "depth_scale": 0.001,
      "cam_imu_timeshift": 0.0
    }
  },
  "total_episodes": 1,
  "gripper_slam_frame": "down_left_forward",
  "slam_frames": {
    "observation.slam_trajectory": "down_left_forward",
    "observation.gripper_2_slam_trajectory": "down_left_forward",
    "observation.ego_slam_trajectory": "ego_cam_optical"
  }
}
```

> 附录 B 的手套段是**主程序**写法（`kind:"data_ble"`、`slots:[]`）。lite 录同一套
> 硬件时会写 `kind:"ble_glove"` / `"usb_glove"`，并把 `slots` 写成 sensor 列名
> （`["left_glove"]`），读端两种都要认（§4.5 / §6.4）。

## 附录 C · data parquet 列与类型（同附录 B 组合）

| 列 | pyarrow 类型 | 产生条件 |
|---|---|---|
| `episode_index` / `frame_index` / `task_index` | int64 | 恒有 |
| `timestamp` | float32 | 恒有 |
| `wall_time` | float64 | 恒有 |
| `hardware_ns` | int64 | 恒有（无硬件戳的槽写 0） |
| `raw_hw_ns` | int64 | 本段至少一帧带原始设备戳时写 |
| `action` | list\<float32,1\> | 恒有 |
| `imu_ts_ns` | list\<int64\> | 有双目 IMU 样本 |
| `observation.imu` | list\<list\<float32,6\>\> | 同上 |
| `observation.left_glove` / `right_glove` | list\<float32,256\> | 该段有手套数据 |
| `observation.<sensor>_imu_quat` | list\<float32,64\> | USB 手套出现 |
| `observation.<sensor>_imu_valid` | list\<float32,16\> | 同上 |
| `observation.left_hand_pose` / `right_hand_pose` | list\<float32,63\> | **恒有**（全零占位） |
| `observation.gripper_*` / `gripper_2_gripper_*` | 见 §4.6 | 该段夹爪产生过对应样本 |
| `observation.slam_trajectory(_ns)` | list\<float64\> / list\<int64\> | 夹爪 SLAM 有点 |
| `observation.gripper_2_slam_trajectory(_ns)` | 同上 | rig2 SLAM 有点 |
| `observation.ego_slam_trajectory(_ns)` | 同上【提案】 | ego SLAM 有点 |
| `status.<id>` | string | UVC 槽 / 传感器列名 / 夹爪非视频槽注册过（外部帧源没有） |

> 列顺序由实现决定（按来源分组构造），**读端必须按列名取，不能按位置**。
