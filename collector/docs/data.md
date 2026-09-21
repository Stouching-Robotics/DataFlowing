# data/ —— 配置文件与数据存储结构

本文档描述 `data/` 目录下的配置文件格式、SQLite 表结构与录制数据的目录布局。
所有结构均从代码（`config/settings.py`、`core/task_record.py`、
`core/egodata_writer.py`、`core/database.py`、`core/recording_record.py`、
`core/helpers.py`）与仓库内模板文件归纳得出。

## 1. 总览：仓库提供什么，本地生成什么

| 文件/目录 | 归属 | 说明 |
|---|---|---|
| `server_config.example.json` | 仓库模板 | 服务器连接配置模板；本地真实配置写在 `server_config.json`（gitignore） |
| `device_names.example.json` | 仓库模板 | 设备命名表模板（空 `{}`）；本地真实配置写在 `device_names.json`（gitignore） |
| `tasks.example.json` | 仓库模板 | 任务列表模板（`{"tasks": []}`）；本地真实数据写在 `tasks.json`（gitignore） |
| `device_params.json` | 仓库内默认文件 | 每设备相机参数（曝光），出厂为空对象 `{}`；key 用设备稳定标识，增长后可能含设备标识，勿提交本地改动 |
| `recordings/` | 本地生成（gitignore） | 录制会话输出根目录 |
| `app.db`、`pipeline.db` | 本地生成（gitignore） | SQLite 数据库；当前代码使用 `pipeline.db`（`settings.DB_PATH`），`app.db` 未被现有代码引用，属历史遗留 |

本地真实文件可能包含服务器地址、凭据、MAC 等敏感信息，**永不提交**；
新环境首次运行由程序按需创建，结构见下文。

## 2. 配置文件

### 2.1 `server_config.json`（模板 `server_config.example.json`）

服务器连接与上传行为配置。字段由 `config/settings.py` 中的读写函数定义：

| 键 | 类型 | 含义 | 默认/回退 |
|---|---|---|---|
| `server_url` | string | 服务器地址；为空时回退出厂默认 `http://127.0.0.1:8000`（`settings.SERVER_URL`） | 空 |
| `username` | string | 上传登录用户名 | 空 |
| `password` | string | 上传登录密码 | 空 |
| `upload_auto_sync` | bool | 录制完成后是否自动上传 | `true` |
| `upload_delete_after` | bool | 上传成功后是否自动删除本地文件 | `false` |

写入采用合并式（merge-write）：`_save_server_config` 只更新传入键，保留其余字段。

### 2.2 `device_names.json`（模板 `device_names.example.json`）

设备命名表。顶层为 JSON 对象：

```json
{
  "<DeviceInfo.stable_key>": { "name": "<用户命名>", "sensor": "<parquet 列名>" }
}
```

- **key**：设备稳定标识 `DeviceInfo.stable_key`，形式如 `uvc:{by-id 前缀}`（同型号同序列号两台并存时 udev 的 by-id 链接名相撞、只有一台拿得到链接，**且谁拿到取决于注册顺序、重枚举时会翻转** ⇒ 两台都退到 `uvc:usb-<USB 拓扑路径>` 如 `uvc:usb-1-5`，同样跨重启稳定）、
  `d435:{serial}`、`ble:{MAC}` 等（含设备序列号/MAC，属本地敏感信息，不入库）。
- **value**：规范形式为 `{"name": str, "sensor"?: str}`；旧版本可能是纯字符串，
  读取时自动兼容升级。
  - `name`：用户在界面中给设备的命名（槽名随 GUI 用户命名）。
  - `sensor`：仅 BLE 数据手套使用，把设备绑定到 parquet 列名
    （`right_glove` / `left_glove`）。按 MAC 持久化，重连保持；首次连接的未知手套
    由 `assign_glove_sensor_role` 按广播名偏好（`L` → `left_glove`、
    `R` → `right_glove`）自动分配一个空闲列名并写回。

### 2.3 `tasks.json`（模板 `tasks.example.json`）

采集任务列表，是任务进度的事实来源（single source of truth）。顶层结构：

```json
{
  "tasks": [ { <任务条目> }, ... ],
  "updated_at": "<ISO 时间戳>"
}
```

任务条目字段（由 `core/task_record.py` 归纳）：

| 键 | 类型 | 含义 |
|---|---|---|
| `id` | string | 任务唯一 ID（读取时也兼容 `task_id` 键名） |
| `name` | string | 任务名；录制时输入的 `task_name` 按 `name` 精确匹配来累计进度，同时是录制目录名的来源 |
| `description` | string | 任务描述 |
| `total_required` | int | 要求完成的录制总次数（`>0` 才视为有效任务） |
| `assigned_at` | string | 任务分配时间（ISO 8601，用于列表排序，新→旧；空/非法排最后） |
| `params` | object | 任务自定义参数（如对象、用手等） |
| `completed_count` | int | 录制完成次数（持久化权威值，每次录制完成 +1，与本地文件是否被删除无关） |
| `status` | string | `pending` / `in_progress` / `completed`，每次加载时由 `completed_count` 与 `total_required` 重新推算 |
| `hidden` | bool | 删除墓碑：用户删除过的任务标 `true`，防止后端再次推送时“复活” |

行为要点（均来自代码）：

- 首次运行若文件缺失或解析失败，写入内置示例种子任务（`task_001` 起 3 条）后重读。
- `load_tasks` 除旧数据回填外不写回文件；后端推送经 `merge_backend_tasks`
  按 `id` 合并，本地保留 `total_required`/`params` 覆盖字段与
  `completed_count` 权威值；同名多可见任务只保留 `assigned_at` 最新的一条，
  其余标 `hidden`。
- 旧数据缺少 `completed_count` 时，一次性按 `data/recordings/<name>/` 下含
  `meta/info.json` 的会话目录数回填初值并写回，此后以持久化值为准。

### 2.4 `device_params.json`

每设备相机参数（当前为曝光设置）持久化。顶层为 JSON 对象，key 与
`device_names.json` 同为 `DeviceInfo.stable_key`：

```json
{
  "<stable_key>": {
    "exposure": { "auto": true, "value": 0.0 },
    "original": { "auto": true, "value": 0.0 }
  }
}
```

- `exposure`：当前曝光（`auto=true` 时 `value` 被忽略）。
- `original`：设备首次开启时读回的原厂曝光基线，只写一次、永不覆盖，
  供“恢复默认”按钮使用。

仓库内默认文件为空对象 `{}`，随使用增长。注意：key 与 `device_names.json` 同为
`DeviceInfo.stable_key`，增长后可能含设备序列号/MAC 等标识，本地修改过的文件请勿提交。

## 3. SQLite 数据库（`data/pipeline.db`）

`core/database.py` 定义建表 SQL（`CREATE TABLE IF NOT EXISTS`），
连接为线程本地单例，启用 `journal_mode=WAL` 与 `foreign_keys=ON`。

### 表 `recording` —— 录制历史记录

| 列 | 类型 | 含义 |
|---|---|---|
| `id` | TEXT PK | 记录 ID |
| `camera_index` | INTEGER | 触发录制的摄像机槽位索引 |
| `camera_name` | TEXT | 摄像机名 |
| `file_path` | TEXT | 会话目录路径 |
| `file_size_mb` | REAL | 文件大小（MB） |
| `duration_sec` | REAL | 录制时长（秒） |
| `resolution_w` / `resolution_h` | INTEGER | 分辨率 |
| `status` | TEXT | `completed` / `uploaded`（已上传、本地保留）/ `aborted` / `deleted` / `uploaded_deleted`（已上传、本地已删，行保留供历史可查） |
| `started_at` / `finished_at` | TEXT | 起止时间 |

索引：`idx_recording_camera(camera_index)`、`idx_recording_date(started_at)`。
读写封装在 `core/recording_repository.py`（`RecordingRepo`）、数据类在
`core/recording_record.py`（`RecordingRecord`）。

### 表 `upload_task` —— 上传任务记录

| 列 | 类型 | 含义 |
|---|---|---|
| `id` | TEXT PK | 上传任务 ID |
| `session_path` | TEXT | 会话目录路径 |
| `session_name` | TEXT | 会话名 |
| `episode_index` | INTEGER | 池化 episode 全局序号（v1.1.0 起） |
| `status` | TEXT | `pending` / `uploading` / `completed` / `failed` / `skipped`；`uploading` 在 POST 发起前写入，其 `updated_at` 即"POST 开始时刻"（启动续传的时间窗判定依赖它，程序被杀时该行会停在 `uploading`） |
| `progress` | REAL | 进度（0.0–1.0） |
| `retry_count` | INTEGER | 重试次数 |
| `server_url` | TEXT | 目标服务器 |
| `server_session_id` | TEXT | 服务器返回的会话 ID |
| `error_message` | TEXT | 错误信息 |
| `created_at` / `updated_at` | TEXT | 创建/更新时间 |

索引：`idx_upload_status(status)`、`idx_upload_session(session_path)`。

## 4. 录制目录结构（`data/recordings/`）

录制由 `core/pipeline.py` 驱动 `core/egodata_writer.py`（`EgoDataWriter`）
写入。目录布局为 EgoData 标准，并兼容 LeRobot v3 格式消费方。

### 4.1 目录树

```
data/recordings/                          # 录制根目录（settings.RECORDING_DIR）
└── <task_tag>/                           # 任务目录：清洗后的任务名（task_name 为空时叫 "session"）
    └── <task_tag>_000001/                # 会话（episode）目录：任务名 + 6 位序号
                                          #   旧格式兼容：episode_000001/
        ├── metadata.json                 # EgoData 根级元数据（见 4.3）
        ├── timestamps.json               # 逐帧时间戳（见 4.4）
        ├── videos/                       # RGB 视频（每台相机一路 MP4）
        │   └── <camera_name>/            #   槽名，如 head_left_rgb；_aux 后缀相机归入主相机目录
        │       └── chunk-0000/
        │           └── <camera_name>.mp4 #   编码自适应（v1.0.9：HEVC CRF30 直出，性能不足回退 H.264 CRF23；实际见 metadata.video_codec）
        ├── depth/                        # 深度（仅启用深度时创建）
        │   └── <深度槽名>/               #   如 head_depth（S80M 兜底）/ d435_depth（D435）/ stereo_depth（S80C）
        │       ├── <深度槽名>.mp4        #   深度热力图视频（JET 伪彩，可视化用途）
        │       └── 000001.png …          #   原始 uint16 毫米 PNG（16-bit grayscale，
        │                                 #   raw_depth 槽位；v1.0.11 曾改 raw16 bin，
        │                                 #   体积过大且上传失败，v1.0.12 回退 PNG16）
        ├── calibration/                  # 标定（StereoCalibration）
        │   ├── head_stereo.json          #   首台双目型设备标定（服务器/回放/三角化依赖此路径）
        │   └── <槽名前缀>_calibration.json  # 其余设备的标定（槽名前缀去掉 "_N" 消歧编号）
        ├── data/                         # 传感器数据（LeRobot v3 兼容 Parquet）
        │   ├── <sensor>/                 #   每传感器一个目录，如 right_glove / left_glove
        │   │   └── chunk-0000/
        │   │       └── chunk_000000.parquet   # zstd 压缩，schema 见 4.5
        │   └── imu/                      #   双目 IMU（仅双目会话，每帧一行）
        │       └── chunk-0000/
        │           └── chunk_000000.parquet
        └── meta/                         # LeRobot v3 兼容元数据
            ├── info.json                 # 上传服务器严格依赖的字段（见 4.6）
            ├── stats.json                # 各特征 mean/std/min/max（归一化统计）
            ├── tasks.jsonl               # {"task_index": 0, "task": "<任务名，空时 'default recording'>"}，单行 JSON
            └── episodes/
                ├── chunk-000/
                │   └── file-000.parquet  # 每会话一行的 episode 表
                └── chunk_000000.parquet  # 前者的兼容旧路径副本
```

要点：

- **序号**：episode 序号 = `max(任务进度 batch_index, 目录扫描最大序号 + 1)`；
  `batch_index` 是“录制完成次数 + 1”（`core/task_record.py` 持久化），
  上传后自动删除本地文件也不会使序号回退。
- **视频/深度/标定**：深度槽位（`depth_slots`）不建视频目录也不走 MP4 合成；
  `start_episode` 中 `_aux` 后缀摄像头归入主摄像头目录。
- **旧格式兼容**：会话目录也可能以 `<tag>_YYYYMMDD_HHMMSS`（`session_dirname`）
  命名且内含 `meta/info.json`（LeRobot v3 旧会话）；`core/helpers.py` 的
  `list_all_sessions` / `detect_session_format` 通过 `metadata.json`（egodata）
  或 `meta/info.json`（lerobot_v3）识别会话，回放支持两种格式。
- **中止录制**：`abort_episode` 直接 `rmtree` 丢弃整个会话目录。
- **关键点数据不写在本目录**：手部关键点输出镜像到仓库根的
  `keypoints_output/<task>/<session>/`（见第 5 节）。

### 4.2 深度通道说明

- **S80M 传统路径**（无显式注册深度槽）：单槽兜底 `head_depth`，
  由 `write_depth_frame` 惰性创建目录并合成热力图 MP4，受
  `settings.DEPTH_ENABLED` 门控（默认关闭，遗留开关勿启用）。
- **D435/D405**（`core/pipeline.py` 经 `set_depth_camera` 显式注册槽位）：
  打开即录深度；槽位配置了 `raw_depth=True` 时额外写原始 uint16 PNG16
  （`egodata_depth_path`：`depth/<槽名>/000001.png`，文件名从 1 起 6 位，
  压缩级 `settings.D435_PNG_COMPRESSION`）。
  热力图支持固定色标（`near_mm`/`far_mm`）、3×3 中值与 EMA 时域平滑
  （仅作用于可视化通道，原始 PNG16 不经过）。
- **S80C**（v1.0.11）：`set_depth_camera(settings.S80M_DEPTH_SLOT,
  raw_depth=True)` 显式注册槽位 `stereo_depth`，深度由 `read_stereo_rgb.py`
  子进程内 SDK 深度引擎计算（~20fps），经管道深度块 → `depth_ready` 信号
  回主程序；录制时热力图 MP4 + PNG16 与 D435 同口径落盘。深度源低于
  录制帧率时热力图 MP4 重复最近帧补拍（时长与 RGB 对齐），PNG16 仅
  记新帧不重复。
- **读取**：PNG16 直接 `cv2.imread(path, cv2.IMREAD_UNCHANGED)`；
  v1.0.11 窗口会话存的是 raw16 bin（`np.fromfile(dtype=np.uint16)
  .reshape(h, w)`，`(h, w)` 取 `metadata.json` `cameras.<槽名>.height/width`），
  读取方已做扩展名回退兼容（`depth_align.load_depth_frame`）。

### 4.3 `metadata.json` 字段概览（EgoData 根级元数据）

| 键 | 含义 |
|---|---|
| `format` / `format_version` | 固定 `"egodata"` / `"1.0"` |
| `episode_index` | episode 序号 |
| `fps` | 默认录制帧率 |
| `task_name` | 任务名（可为空） |
| `cameras` | `{槽名: {height, width, ...}}`；深度槽带 `type:"depth"`、`unit:"mm"`、`format:"png16"`（v1.0.11 窗口会话为 `"raw16"`）；RGB 槽带独立帧率 `fps`（与默认不同时）；挂设备归属时带 `device_key`/`device` |
| `devices` | 设备段数组：`{key, kind, name, slots, serial?, sensor_column?, resolution, fps, calibration}`；`serial` 仅在设备提供时写入 |
| `sensors` | 传感器名列表（如 `["right_glove", "left_glove"]`） |
| `sensor_dim` | 传感器维度（256 = 16×16 触觉矩阵展平） |
| `created_at` | 创建时间（Unix 时间） |
| `codebase_version` | 程序版本（`config.__version__`） |
| `video_codec` | v1.0.9：本会话视频编码信息 `{encoder, codec, crf, ffmpeg, selected_by, probe}`；`selected_by` = `"auto"` 或显式指定名，`probe` = 录前速度探针结果（x265 路径） |
| `drop_stats` | v1.0.9：录制丢帧统计 `{队列键: 丢弃帧数}` + `imu_overflow`（IMU 防丢缓冲溢出次数）；无丢帧时全为 0。v1.3.10 起另有**带后缀**的键：`*_ms`（时长）/ `*_count`（次数）——夹爪 RGB 的帧空洞与采集侧仪表（`<槽>_gap_ms`、`_readfail_ms`、`_overwrite_count`、`_emit_lag_max_ms`、`_dispatch_lag_max_ms` 等）。**`_ms`/`_count` 后缀的键不是帧数**，加总帧数前必须过 `core.pipeline.is_frame_drop_key`（见 [file_format](file_format.md) §7.3、[postmortem](postmortem_trajectory_and_rgb.md) §4.6） |

### 4.4 `timestamps.json`

```json
{
  "timestamps": [ { "frame_index": 0, "timestamp": 0.0, "wall_time": 1.7e9 }, ... ],
  "total_frames": 1234
}
```

- `timestamp`：会话相对时间（秒，与 parquet 的 `timestamp` 列同源）；`wall_time`：写入时刻的墙上时间。
- `hardware_ns`：仅双目相机帧携带（SDK 硬件纳秒时钟，与 IMU 同源）；含硬件
  时间戳的行在写出时按 `hardware_ns` 稳定排序，保证时间线单调。

### 4.5 Parquet schema（`data/` 与 `meta/episodes/`）

- `data/<sensor>/chunk-0000/chunk_000000.parquet`（每帧一行，zstd 压缩）：

  | 列 | 类型 | 含义 |
  |---|---|---|
  | `episode_index` | int64 | episode 序号 |
  | `frame_index` | int64 | 帧序号 |
  | `timestamp` | float32 | 会话时间（秒） |
  | `task_index` | int64 | 任务序号（当前固定 0） |
  | `observation.<sensor>` | list<float32, 256> | 传感器读数（16×16 展平；缺帧补零） |
  | `observation.left_hand_pose` / `observation.right_hand_pose` | list<float32, 63> | 手部关键点（21 关节 × xyz；USB 手套录制时由 IMU 实时解算回填，无手套或未解算时为零占位） |
  | `action` | list<float32, 1> | 动作（当前固定 `[0.0]`） |
  | `status.<device_id>` | string | 该设备在本帧的连接状态（默认 `"connected"`） |

- `data/imu/chunk-0000/chunk_000000.parquet`（双目 IMU，每帧一行）：

  | 列 | 类型 | 含义 |
  |---|---|---|
  | `episode_index` / `frame_index` / `timestamp` / `task_index` | 同上 | 同上 |
  | `hardware_ns` | int64 | 帧的 SDK 硬件纳秒时间戳 |
  | `imu_ts_ns` | list<int64> | 本帧窗口内 IMU 样本时间戳，与样本一一对应 |
  | `observation.imu` | list<list<float32, 6>> | 样本序列，每样本 `[gx, gy, gz, ax, ay, az]` |

- `meta/episodes/chunk-000/file-000.parquet`（每会话一行）：
  `episode_index`、`task_index`、`start_frame_index`、`end_frame_index`、
  `length`（均为 int64）。

### 4.5.1 UMI 夹爪列与跨模态时间对齐

启用夹爪时 episode parquet 增加下列稀疏列（无样本的行填 0/空）：

| 列 | 类型 | 含义 |
|---|---|---|
| `observation.gripper_{left,right}_force` | list<float32, 3> | 3 向量力 `[fx,fy,fz]` mN |
| `observation.gripper_{left,right}_force_ns` | int64 | 该力样本的采集时刻 |
| `observation.gripper_{left,right}_force_matrix` | list<int16> / list<float32> | 250×250×3 力矩阵（档位见下） |
| `observation.gripper_{left,right}_force_matrix_ns` | int64 | 该力矩阵样本的采集时刻 |
| `observation.slam_trajectory` | list<double> | 轨迹点 `[t,x,y,z,qx,qy,qz,qw]`×N，变长 |
| `observation.slam_trajectory_ns` | list<int64> | 每个轨迹点对应取样帧的**宿主时刻**，与 `slam_trajectory` 同序等长 |
| `observation.gripper_state` | list<float32, 3> | `[open_pct, gripped, fz_mn]` |

力矩阵档位记在**每段一份**的 `meta/episodes` 行（`force_matrix_specs` 列），
不按面值猜——同一任务换档时前一段的倍率不会被后一段顶掉。

**为什么需要 `_ns` 列。** 同一行里的 RGB 帧和力样本**不是同时刻采集的**，
两条支路各走各的路：RGB 走外部帧源队列，写线程每 tick 取**队头最旧帧**；
力/矩阵走 latest-wins 单槽，写线程取到的是**当下最新**样本。于是「行号相同」
≠「时刻相同」——力恒领先 RGB，领先量等于那张 RGB 帧在队列里的滞留时间，
且随录制时长增长（RGB 源实测 30.84 fps vs 写线程 30 fps，约每秒多积 1 帧）。
力矩阵还走独立泵线程，与 3 向量力之间也各记各的时刻，所以两者不共用一列。

v1.3.3 起每行落盘两条支路各自的采集时刻（宿主单调钟纳秒，与 `hardware_ns`
同一时基），下游按时间取最近邻即可还原真实配对：

```bash
venv/bin/python scripts/align_modalities.py <episode.parquet> --residual
```

```python
from align_modalities import align_episode
mapping, residual_ns = align_episode("episode-000.parquet", side="left")
# mapping[k] = 视频第 k 帧应对应的 parquet 行号
```

- 对齐精度上界是**半个力样本间隔**，不是半个行周期。`force_ns == 0` 表示
  「本行窗口内没有新样本」（**不是**时刻为 0，取最近邻时必须排除，否则会把
  一批帧吸到第 0 行）。样本每 k 行才有一个时上界放宽到 k/2 个行周期。
- 早于第一个力样本的帧（段首陈旧帧，见 `rgb-prestart-frames`）与晚于最后
  一个样本的帧在时间上没有对应样本，只能夹到端点——**这不是对齐结果**，
  脚本单列「越界帧」计数，下游应丢弃。
- **v1.3.3 之前录的 episode 无法这样对齐**：那时力侧完全没有时间戳，RGB 侧
  的 `hardware_ns` 还被 `pyqtSignal(int)` 截成 32 位（每 2.147 s 翻符号）。
  脚本对这类 episode 明确报「无法对齐」而不是拿行号或增长率去猜。

### 4.5.2 SLAM 点与视频帧的时间对齐

`slam_trajectory` 每点的第 0 个字段 `t` 是**相机传感器钟**上的时间（native
侧 `img->timestamp`，相对按下 ENTER 设原点那一刻）。它与 RGB 帧的
`hardware_ns`（宿主单调钟）**不同源**，两者之间只差一个近似常量但带抖动的
偏移——只能从数据反推、不能换算。实测同一段数据反推出的偏移有三个估计值
（`arrival_monotonic − t` 中位、行桶中位、逐点拟合最优），散布约 **104 ms**，
已超一个帧间隔（33.3 ms）。

因此 v1.3.6 起 native 在每个取样帧进进程时取一次 `CLOCK_MONOTONIC`
（`RawImageFrame.host_mono_ns`），随帧走完预处理/跟踪/排队全程，最终与位姿
一起打在 stdout 的 `Host:(<ns>)` 字段上，落成
`observation.{prefix}slam_trajectory_ns`。

> **改这段 native 代码要认准文件**：构建源是
> `core/gripper/orb_slam_src/ORB-SLAM/Examples/fays/fayssense_orb_slam.cc`，
> 由同树 `dist/fays_opencv48/CMakeLists.txt` 的 `FAYS_BRIDGE_SOURCE` 硬指向
> （该值缺省就是 `${KSQ_ROOT}/ORB-SLAM/Examples/fays/fayssense_orb_slam.cc`）。
> 2026-09-17 之前这两份源码只存在于 `online/`（不上传），现已随包入库，
> 可以直接改、有版本控制兜底；重建走 `core/gripper/orb_slam_src/build.sh`。
> `core/gripper/native/ORB-SLAM/Examples/fays/` 下那份是**陈旧副本，不是构建源**
> （只留一份 `SUPERSEDED.md`），改它不会进二进制——它连 2026-09-11 的崩溃修复
> 都没有。`online/` 里那份现在是**同一份源码的第二份拷贝**，不再是真源。

- **与 `slam_trajectory` 同序等长**，下标即配对，所以某帧有 N 个轨迹点就有 N
  个时刻。这要求时刻列表**锁步**累积：某个点没有戳时补 `0`（= 未知，与
  `*_force_ns` 的约定一致）而**不是跳过**，跳过会让后续点全部错位且无从察觉。
- 有了它，配 slam 点↔视频帧退化成一次 `searchsorted`：按 `hardware_ns` 找
  最近的 `slam_trajectory_ns`。**不需要拟合偏移，更不需要插值**——从实测点里
  取最近邻是合法的 1:1，不是造假数据。
- **全段无戳时不建该列**（例如 Python 侧已升级但 native 二进制未重编）。
  下游按列是否存在判断「本段能否按时间对齐」；列存在但个别值为 0 表示那
  一个点缺戳。
- **行数不等于点数。** 写线程在落后超过一帧时会重置时钟并**跳过**那些 tick
  （`core/pipeline.py` 的 `_write_loop`），实测一段 11.4 s 的录制因此少写
  11 行；slam 点侧则约丢 0.22%。所以「slam 点数 == 视频帧数」不成立，
  按时间戳配对后剩下未配对的点/帧是正常现象，不是数据损坏。

### 4.6 `meta/info.json` 字段概览（LeRobot v3 兼容，上传服务器严格依赖）

| 键 | 含义 |
|---|---|
| `codebase_version` | 固定字符串 `"v3.0"` |
| `fps` | 默认录制帧率 |
| `video` | bool：是否有视频 |
| `task_name` | 任务名（空时 `""`） |
| `features` | `{observation.<sensor>: {dtype:"float32", shape:[16,16]}, observation.imu: {dtype:"float32", shape:[6]}, action: {dtype:"float32", shape:[1]}}`；shape 必须是 2D `[16,16]` 而非 1D `[256]` |
| `cameras` | dict 格式 `{槽名: {height, width, fps?}}`（深度槽不在此列） |
| `devices` | 紧凑设备段：`[{key, kind, name, slots}]`（无 serial 等敏感字段） |
| `device_names` | 槽位 → 用户命名映射 |
| `sensors` | 传感器名列表 |
| `sensor_dim` | 传感器维度 |
| `created_at` | 创建时间（Unix 时间） |

`stats.json` 对应 `features` 各键给出 `mean`/`std`/`min`/`max`（IMU 为 6 轴
样本级统计，`action` 为占位值），供归一化使用。

## 5. 相邻输出目录 `keypoints_output/`（不在 `data/` 下）

录制后的手部关键点处理结果**不写回** `data/recordings/`，而是镜像输出到
仓库根的 `keypoints_output/`（`settings.KEYPOINTS_OUTPUT_DIR`，gitignore）：

```
keypoints_output/<task>/<session>/
├── videos/                             # 关键点可视化视频
├── hand_pose/chunk-000.parquet         # 2D 手部关键点
├── hand_pose_3d/chunk-000.parquet      # 3D 手部关键点
└── auto_labels/auto_labels.parquet     # 自动标注
```

读取时有三级回退路径（`core/helpers.py`）：先查 `keypoints_output/` 镜像，
再查会话目录内 `keypoints/`，最后查旧版 `annotations/`（含 `annotations/mmpose/`）。
