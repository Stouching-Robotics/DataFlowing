# collector — Multimodal Data Acquisition SDK · 多模态数据采集 SDK

![Version](https://img.shields.io/badge/version-1.3.11-blue)
![Python](https://img.shields.io/badge/python-3.10-blue)
![License](https://img.shields.io/badge/license-TBD-lightgrey)


## 📚 Navigation · 导航

| Section · 章节 | English | 中文 |
|---|---|---|
| Features · 功能特性 | [Features](#features) | [功能特性](#功能特性) |
| Quick Start · 快速开始 | [Quick Start](#quick-start) | [快速开始](#快速开始) |
| Supported Hardware · 硬件支持 | [Supported Hardware](#supported-hardware) | [硬件支持](#硬件支持) |
| Data Formats · 数据格式 | [Data Formats](#data-formats) | [数据格式](#数据格式) |
| Black Glove Keypoints · 黑手套解算 | [Black Glove Keypoints](#black-glove-keypoints) | [黑手套解算](#黑手套解算) |
| Environment Variables · 环境变量 | [Environment Variables](#environment-variables) | [环境变量](#环境变量) |
| Directory Structure · 目录结构 | [Directory Structure](#directory-structure) | [目录结构](#目录结构) |
| Tests · 测试 | [Tests](#tests) | [测试](#测试) |
| Documentation · 文档 | [Documentation](#documentation) | [文档](#文档) |
| Privacy & Local Config · 隐私与本地配置 | [Privacy & Local Config](#privacy--local-config) | [隐私与本地配置](#隐私与本地配置) |
| Third-Party Components & Licensing · 第三方组件与许可 | [Third-Party Components & Licensing](#third-party-components--licensing) | [第三方组件与许可](#第三方组件与许可) |
| License · 许可证 | [License](#license) | [许可证](#许可证) |
| Contributing · 贡献 | [Contributing](#contributing) | [贡献](#贡献) |
| Changelog · 更新记录 | [Changelog](#changelog) | [更新记录](#更新记录) |

---

## English

A PyQt5-based multimodal data acquisition SDK + GUI: synchronized recording,
playback, and HTTP upload across multiple cameras (UVC webcams, Intel
RealSense D435/D405, S80C / S80M stereo) and BLE data gloves, outputting
[EgoData](https://github.com/facebookresearch/egodata) /
[LeRobot v3](https://github.com/huggingface/lerobot) compatible formats for
robot teleoperation datasets. Ships with 3D hand-keypoint processing
pipelines and offline SLAM dataset export.

**Architecture**: `core/` is the SDK core (capture pipeline, device managers,
recording/upload/playback logic — depends only on PyQt5.QtCore, never imports
ui); `ui/` does widget assembly only; `tools/` is a self-contained toolchain
(never imported by the main program), including standalone demos, model
weights, and tests.

<!-- Screenshot placeholder: main window / 3D keypoint visualization can go here
![Main window](docs/images/main_window.png) -->

### Features

- **Multi-camera synchronized recording**: per-stream record control
  (normal stop saves / abnormal stop discards) with live recording duration
- **Draggable multi-view grid layout**, adjustable in playback view too
- **Device detection panel**: unified UVC / D435 / S80M enumeration, 2 s
  polling, click to display
- **BLE data gloves**: one per hand, tactile data captured in sync with video
- **Recording history & playback**: SQLite persistence, compatible with
  EgoData / LeRobot v3 sessions
- **HTTP upload**: post-recording upload to a self-hosted server, with
  optional auto-sync/delete of local files
- **Chinese / English UI switching** (i18n)
- **3D hand keypoints**: D435 RGB-D pipeline
  ([tools/hand_3d_d435/](tools/hand_3d_d435/)), S80C / S80M stereo
  triangulation, MediaPipe bare-hand pipeline, plus a dedicated
  black-glove pipeline (YOLO-World boxes + RTMPose keypoints — see
  [Black Glove Keypoints](#black-glove-keypoints))
- **Offline SLAM dataset export** (ORB-SLAM compatible formats, with
  validation tooling)
- **12-bit depth video recording**: single-stream gray HEVC MP4 (log depth
  codes, reversibly decoded to millimetres), unified JET heatmap display
  live and in playback
- **Direct HEVC recording**: low-bitrate HEVC at record time with
  multi-encoder fallback; the uploader detects HEVC and skips redundant
  re-compression

### Quick Start

#### Install dependencies

```bash
python -m venv venv            # Python must be exactly 3.10 (glove SDK ABI)
venv/bin/pip install -r requirements.txt
```

> Optional dependencies such as `mediapipe` and `torch` are not listed in
> `requirements.txt` (lazily imported; the corresponding feature is unavailable
> if missing — one-click installs via `start.bat extras` below). `pyrealsense2`
> (D435/D405) and the glove-skeleton solver dependencies are part of the
> default install.

#### Windows one-click deployment (recommended for customers)

Double-click **`start.bat`** in the repo root: it installs Python **3.10**
(silent download if absent — the glove SDK's solver core is encrypted for the
3.10 ABI, so 3.11/3.12 will not do), creates the venv, installs dependencies,
unpacks the bundled glove SDK ([4/7] — serial capture, tactile filtering and
the live hand skeleton; if it is missing only a warning is printed and the
program still starts), and launches the main program; already-deployed machines
start instantly. Common
commands:

```bat
start.bat               deploy and launch (default)
start.bat reinstall     delete venv and reinstall (first resort when broken)
start.bat extras        additionally install mediapipe
start.bat extras-torch  additionally install CPU torch (hand-keypoint RTMPose backend)
start.bat help          open the operation guide and troubleshooting doc
```

> Full operation guide, error-code reference, and intranet offline delivery
> procedure: [使用说明.md](使用说明.md) / [使用说明_EN.md](使用说明_EN.md)
> (offline wheel bundles are produced by `python scripts/pack_wheels.py`,
> which by default also trims the glove toolkit into `wheels/toolkit/glove_toolkit.zip`).

**Lite edition** (capture + upload only — no login / playback / task page /
skeleton solving): double-click **`start_lite.bat`** (Windows) or run
`./start_lite.sh` (Linux); it uses its own `venv_lite/` (~750 MB). Same device
set as above, including the **UMI gripper on Linux** — there it is recorded
without any visualization (RGB video + force/tactile/SLAM columns only), and
the Linux package ships the ~460 MB gripper payload as a *required* resource
(`start_lite.sh` verifies 7 items up front and refuses to start with
`[错误 B]` if any is missing). The Windows package deliberately omits that
payload, so the gripper group there is always empty. See
[使用说明_lite.md](使用说明_lite.md).

#### Launch the main program

```bash
./start.sh                     # Linux one-click deploy (same subcommands: reinstall/extras/...)
./run.sh                       # launch directly when venv already exists
venv/bin/python main.py        # launch directly
run.bat                        # launch directly when venv already exists (start.bat deploys first)
```

No manual config needed on first run: `data/tasks.json` is generated from
built-in seed tasks; other config files are created the first time the
corresponding setting is saved (the repo only ships `*.example.json`
templates).

#### 3D hand keypoints (D435)

```bash
./tools/hand_3d_d435/run_live_d435.sh                        # live demo (direct camera)
./tools/hand_3d_d435/run_live_d435.sh --replay <session_dir> # replay a session
./tools/hand_3d_d435/run_live_d435.sh --glove                # black-glove mode (see Black Glove Keypoints)
./tools/hand_3d_d435/run_d435.sh <session_dir>               # offline pipeline
```

#### Demos

```bash
./tools/demos/run_stereo_depth_demo.sh                     # S80M depth-engine demo (SDK required, see below)
venv/bin/python tools/demos/test_stereo_depth_calib.py     # stereo depth calibration self-check (SDK required)
venv/bin/python tools/hand_detection/demo_stereo_hands.py  # stereo + MediaPipe hand demo
```

### Supported Hardware

| Device | Interface | Notes |
|---|---|---|
| UVC camera | `/dev/videoN` (OpenCV V4L2) | MJPG pixel format; up to 8 cameras |
| Intel RealSense D435 / D405 | `pyrealsense2` (default install) | RGB + depth dual slots; depth recorded as 12-bit gray HEVC MP4 (log depth codes) + live JET heatmap; built-in stall/framerate watchdog with auto-reconnect; D405 has a dedicated near-range capture profile |
| S80C / S80M stereo | FaysSense VI Kit SDK (bundled in-repo, incl. FT602 bridge driver) | No SDK install needed; camera profile `STEREO_CAM_FPS` (default 50 fps) decimated to 30 fps recording via wall-clock 1/30 s buckets (burst backfill + empty-bucket watchdog, ~3% empty-bucket rate on healthy recordings); callback frame capture (same as the official GUI); carries hardware nanosecond timestamps and IMU samples; since v1.0.11 the subprocess runs the SDK depth engine → third-tile live depth heatmap + 12-bit gray depth video recording |
| BLE data gloves | `bleak` | one per hand; parquet column names bound (`right_glove` / `left_glove`) |
| UMI gripper (Fays S80M) | in-repo native stack (`core/gripper/native/`, ~460 MB, not in git) | Linux only (the native stack is ELF x86-64); up to 2 grippers (second one gets the `gripper_2_*` column prefix); records left-lens RGB video + `observation.gripper_{left,right}_force` / `_force_matrix` (10×10×3 force matrix) + `observation.slam_trajectory` + gripper state, no stereo video / IMU; new grippers auto-generate their per-serial factory calibration on open |

### Data Formats

Recordings live in a **task-level pooled layout** (v1.1.0+, LeRobot v3
naming): `data/recordings/<task>/` — the task name doubles as the upload
"project name", one file group per episode:

```
data/recordings/<task>/
├── videos/chunk-NNN/<slot>/episode-NNN.mp4       # one video per stream per episode
│                                                 # RGB = mp4; depth = 12-bit gray mp4 (mkv on fallback)
├── data/chunk-NNN/episode-NNN.parquet            # one parquet per episode (zstd, sparse columns)
└── meta/
    ├── info.json                                 # task header; format="pooled_episodes_v1" is the discriminator
    ├── stats.json                                # task-wide stats accumulator (count/mean/std/min/max per column)
    ├── tasks.jsonl                               # task descriptions (single-line JSONL is contractual)
    └── episodes/chunk-NNN/episode-NNN.parquet    # one row per episode (10 columns)
```

- **Numbering**: episodes are numbered globally from N=1 with
  `chunk = (N-1) // 1000`, `file = (N-1) % 1000` (`chunks_size=1000`,
  declared in info.json); every file of an episode shares the same
  `(chunk, file)`. Abnormally-stopped recordings recycle their number;
  completed episodes never do (in-app deletion is permanent)
- **Row layout**: each data parquet row = one 30 fps frame with key
  columns (`episode_index` / `frame_index` / `timestamp` / `wall_time` /
  `hardware_ns`) plus sparse observations (`observation.<sensor>` per
  frame, `observation.imu` variable-length sample lists aligned by
  `imu_ts_ns`, `observation.*hand_pose` placeholders filled live from the
  glove IMU when solving is available, zero otherwise / backfilled by
  post-processing). The full interface contract lives in
  [docs/file_format.md](docs/file_format.md)
- Device naming follows the EgoData `<location>_<modality>` convention, e.g.
  `head_left_rgb`, `head_right_rgb`, `head_depth`, `right_glove`,
  `right_hand_pose`
- Depth video: single-stream 12-bit gray HEVC MP4 (log depth codes,
  reversibly decoded to millimetres; falls back to FFV1 MKV when x265 is
  unavailable); playback always renders the unified JET heatmap
- Recording history & upload queue: SQLite (`data/pipeline.db`)
- Offline processing output: `keypoints_output/<task>/episode_NNNNNN/`
  (mirrors the pooled key, never written back into the recording
  directory)

### Black Glove Keypoints

Bare-hand MediaPipe detection fails on black gloves (4/68 hands
measured), so gloves are solved with a dedicated YOLO + RTMPose
pipeline (40/40 on black / grey / any-colour gloves):

1. **Detection box** — open-vocabulary YOLO-World
   (`yolov8m-worldv2.pt`, prompts `hand` / `glove`) or the trained
   single-class yolo11n detector (`best.pt`), hot-switchable at runtime
2. **Box tracking** — EMA-smoothed track boxes with dual-threshold
   new-track gating and churn suppression (HandTracker)
3. **Keypoints** — RTMPose hand5 (21 points, ONNX via onnxruntime CUDA)
   cropped-box inference; a MediaPipe cropped-box backend is available
   for comparison (hot-switchable at runtime)
4. **Stabilization** — per-point confidence weighting; on low confidence
   hold the last output and translate it with the smoothed box motion;
   a hold-escape releases after N consecutive low-confidence frames so
   real new poses (grip, fist) are never frozen out; degradation freeze
   cap and handedness voting
5. **3D lift** — D435 RGB-D depth lift or S80C stereo depth engine. On
   S80C the right eye reuses the left eye's smoothed box translated by
   disparity (`x_r = x_l − fx·B/z`) and runs the same stateless pose
   backend; the 2D display stays decoupled from the 3D slot chain

Where it runs:

| Location | Mode | Notes |
|---|---|---|
| Main program post-processing | `HAND_TRACK_MODE=glove` | YOLO box + RTMPose 2D keypoints written back to `keypoints_output/` (92 dim/frame packed); `bare` mode = MediaPipe 2D + 3D world landmarks |
| D435 live demo | `--glove` | realtime 3D on RGB-D |
| S80C live demo | `--glove` | realtime 3D on stereo depth |
| Toolkit | [tools/glove_package/](tools/glove_package/) | annotation, training (`train_detector.py` → `best.pt`), CLIP auto-labeling |

```bash
./tools/hand_3d_d435/run_live_d435.sh --glove   # D435 live glove mode
./tools/hand_3d_s80c/run_live_s80c.sh --glove   # S80C live stereo glove mode
```

Implementation: detection / pose front-ends in
[tools/hand_detection/](tools/hand_detection/) and
[tools/glove_package/](tools/glove_package/); post-recording processing
in [core/hand_tracking.py](core/hand_tracking.py); live demos in
[tools/hand_3d_d435/](tools/hand_3d_d435/) and
[tools/hand_3d_s80c/](tools/hand_3d_s80c/).

### Environment Variables

| Variable | Required | Description |
|---|---|---|
| `FAYSSENSE_SDK_DIR` | S80M demo/diagnostic tools only | Install path of the FaysSense VI Kit SDK (Release directory); those tools exit with an error if unset. The main program's S80C/S80M capture path does not need it (in-repo self-contained libs) |
| `FFMPEG_BIN` | no | ffmpeg executable override (used by render/video-write tooling) |
| `VENV_PY` | no | Python interpreter used by launcher scripts, default `venv/bin/python` |

### Directory Structure

```
collector/
├── main.py                    # program entry (qt-material dark theme)
├── start.bat / start.sh       # one-click deployment (Windows / Linux)
├── run.bat / run.sh           # direct launch once deployed
├── requirements.txt           # required main-program dependencies (start.bat/start.sh self-check matches)
├── .gitignore / .gitattributes # repo exclusions / line-ending rules (bat forced CRLF)
├── core/                      # ★ SDK core (depends only on PyQt5.QtCore and config, never imports ui;
│                              #   listed in data-flow order: capture → write → playback/upload)
│   ├── pipeline.py            # recording main loop / pipeline state machine (data-flow hub)
│   ├── device_manager.py      # unified device worker registry + panel toggle dispatch
│   ├── camera.py              # UVC camera capture (CameraWorker)
│   ├── d435_manager.py        # RealSense D435/D405 capture (heatmap/EMA/record write)
│   ├── s80m_manager.py        # S80C/S80M subprocess capture + 50→30 decimation
│   ├── ble_engine.py          # BLE data-glove capture
│   ├── egodata_writer.py      # EgoData / LeRobot v3 recording writer (pooled layout)
│   ├── depth_codec.py         # 12-bit log depth codec (gray12le HEVC video)
│   ├── encoder_probe.py       # HEVC encoder availability probe (nvenc → x265 → x264)
│   ├── session_catalog.py     # session scanning / metadata / fps resolution
│   ├── session_loader.py      # background playback loader (QtCore signals)
│   ├── session_timeline.py    # playback timeline
│   ├── depth_reader.py        # depth video reader (gray12le MP4 / FFV1 MKV / legacy PNG16)
│   ├── uploader.py            # session upload queue
│   ├── hand_tracking.py       # hand-keypoint processing (glove / bare)
│   ├── helpers.py             # session path/duration/size utilities
│   └── …                      # support modules: calibration, exposure, naming, SQLite
│                              #   history, task polling, rendering (see docs/core.md)
├── ui/                        # PyQt5 UI assembly
│   └── main_window.py         # main window: slots / record control / panel dispatch (data-flow wiring point)
├── config/                    # global config + i18n strings + calibration/sensor JSON
├── scripts/                   # offline processing (process_hands.py) + offline packaging (pack_wheels.py)
├── docs/                      # per-directory module docs (see below)
├── data/                      # local config templates + recordings (both gitignored)
└── tools/                     # toolchain (self-contained, never imported by the main program)
    ├── gongsitubiao.png        # UI logo (ui/main_window.py, ui/task_page.py)
    ├── stereo_s80m/           # S80M stereo tools (read_stereo_rgb.py = main-program capture subprocess)
    ├── hand_detection/        # YOLO glove detection + MediaPipe bare-hand pipeline
    ├── hand_3d_d435/          # D435 RGB-D 3D hand keypoints (standalone module)
    ├── hand_3d_s80c/          # S80C stereo realtime bare-hand/glove keypoint demo (with self-contained SDK)
    ├── glove_package/         # YOLO-World + RTMPose black-glove toolkit
    ├── fayssense_depth_sdk/   # FaysSense VI Kit depth-engine SDK (proprietary)
    ├── models/                # model weights (MediaPipe hand_landmarker.task)
    ├── weights/               # CLIP and other large weights (gitignored, not distributed)
    ├── demos/                 # delivery demos and self-check scripts
    └── tests/                 # regression / smoke tests
```

### Tests

```bash
# hardware-free offline tests (QT_QPA_PLATFORM=offscreen)
venv/bin/python tools/tests/test_playback_multifps.py
venv/bin/python tools/tests/s80m_signal_regression.py
venv/bin/python tools/tests/s80m_50fps_decimation_test.py
venv/bin/python tools/tests/multi_device_registry_test.py
venv/bin/python tools/tests/exposure_control_test.py
venv/bin/python tools/tests/test_meta_devices.py
venv/bin/python tools/tests/test_depth_heatmap.py
venv/bin/python tools/tests/glove_widget_test.py
venv/bin/python tools/tests/grid_drag_fps_test.py
venv/bin/python tools/tests/device_panel_gui_smoke_test.py
venv/bin/python tools/tests/test_device_detector.py
# gripper RGB frame-gap instrumentation (v1.3.10)
venv/bin/python tools/tests/test_frame_gap.py
venv/bin/python tools/tests/test_rgb_quality.py
venv/bin/python tools/tests/test_ext_frame_gap.py
venv/bin/python tools/tests/test_camera_log_archive.py
venv/bin/python tools/tests/test_audit_frame_gaps.py
# whole-library frame-gap audit (read-only; exit 1 when picture loss is proven)
venv/bin/python tools/audit_frame_gaps.py
# glove SDK v2.1.0 migration (v1.3.11)
venv/bin/python tools/tests/test_glove_sdk_boot.py
venv/bin/python tools/tests/test_glove_registry.py
venv/bin/python tools/tests/test_glove_backend_parity.py
venv/bin/python tools/tests/test_glove_engine_sdk_guards.py
venv/bin/python tools/tests/test_sdk_python_version.py
# hardware tests require the corresponding device attached: d405_worker_test,
# d435_e2e_test, d435_gui_smoke_test, mono_regression, d435_playback_test, etc.
```

### Documentation

Per-module details live in [docs/](docs/) (one page per directory: purpose,
file inventory, data flow):

- [docs/index.md](docs/index.md) — repository overview (entry point of the doc set)
- [docs/core.md](docs/core.md) — SDK core (pipeline, device managers, recording/upload/playback)
- [docs/ui.md](docs/ui.md), [docs/config.md](docs/config.md) — UI, configuration
- [docs/data.md](docs/data.md) — config and data storage layout
- [docs/scripts.md](docs/scripts.md) — offline processing and deployment scripts
- [docs/tools.md](docs/tools.md), [docs/demos.md](docs/demos.md) — 3D tools, delivery demos
- [docs/stereo_s80m.md](docs/stereo_s80m.md), [docs/hand_detection.md](docs/hand_detection.md) — S80M, hand detection
- [docs/file_format.md](docs/file_format.md) — data file interface contract (authoritative definition of the v1.1.x pooled layout)
- [使用说明_lite.md](使用说明_lite.md) — Lite edition guide: devices (incl. the UMI gripper), recording, upload, acceptance checklist (Chinese)
- [使用手册.md](使用手册.md) — full operation manual, Chinese + English

### Privacy & Local Config

Real config files **never enter the repo**: `data/server_config.json` (may
contain server address and login credentials), `data/device_names.json`
(keys contain device serials/MACs), `data/tasks.json`, plus `data/*.db` and
`data/recordings/` are all excluded by `.gitignore`; apart from
`data/device_params.json` (factory-default empty config), the repo only
ships `*.example.json` templates. Do not commit modified real config files.

### Third-Party Components & Licensing

- **FaysSense VI Kit SDK**: proprietary software; the main program's
  S80C/S80M capture path uses the in-repo `tools/stereo_s80m/lib` and
  `tools/hand_3d_s80c/third_party` (incl. the FT602 bridge driver
  libft602.so and OpenCV 4.2 dependencies) — a fresh git clone runs with no
  SDK install. `tools/fayssense_depth_sdk/` is an intranet-shared copy (used
  by the S80C demo) and must be purged from history before any public
  open-source release. Standalone demos/diagnostic tools can also point
  `FAYSSENSE_SDK_DIR` at a separately installed SDK
- **Model weights**: `tools/models/hand_landmarker.task` (MediaPipe) and
  others follow their respective upstream licenses; large weights such as
  CLIP live in `tools/weights/` and are not distributed with the repo.
  Verify upstream license terms before use/redistribution
- **ffmpeg**: the recorder prefers the static ffmpeg bundled with
  `imageio-ffmpeg`

### License

The LICENSE of this repository is to be determined (a LICENSE file will be
added before release). Third-party components (SDK, model weights, ffmpeg,
etc.) are governed by their upstream terms.

### Contributing

Issues and merge requests are welcome. Development conventions (version
number defined only in `config/__init__.py`, i18n strings via `tr()`, use
`object` for large ints in PyQt5 signal params, core never imports ui, etc.)
are documented in [docs/index.md](docs/index.md#开发约定).

### Changelog

- **v1.3.11** — the glove path moves to the vendor SDK v2.1.0 (`tools/glove_sdk/`,
  replacing the forked `core/glove_usb`), which pins the whole stack to **Python
  3.10**: the SDK's `algorithm/` is PyArmor-encrypted for the 3.10 ABI, so on
  3.11+ `import sdk.api` fails outright — you lose **the whole glove path**, not
  just the skeleton. The four launchers now demand *exactly* 3.10 and rebuild a
  3.12 venv in place. Four silent failures fixed: skeleton columns permanently
  blank with no error (the SDK swapped the warmup key for the inverted
  `warming_up`); the left-hand tactile picture rotated 90° (left frames are the
  canonical frame's `[::-1, ::-1].T`, not a row mirror); a third glove silently
  taking the left column and overwriting the real left hand; and both venv
  failures of one-click deployment. The engine also guards two SDK transport
  defects: `stop()` does not terminate (an orphan reader holds the tty forever —
  the "must restart the program" symptom) and one shared lock lets the silent
  stream starve. Gripper side (L0/L1): finalize-stage timings, a raw-stream stall
  watchdog, a SLAM divergence audit and a force-matrix schema contract test.
- **v1.3.10** — silent holes in the gripper RGB stream are now instrumented,
  alarmed and archived. `episode-099.mp4`'s frozen opening is not a missing
  stream at the start: the first 45 frames are real (the scene is simply still)
  and **4.68s vanished between row44 and row45** (`hardware_ns` +4680.8ms and
  `wall_time` +4666.4ms jump together) — while *every* counter read 0 and the log
  held nothing. Three different paths leave the **same** hole signature in the
  parquet: the camera having no frame to read, the emit frame slot being
  overwritten, and the GUI main thread stalling. They are counted separately now
  (ten `*_ms`/`*_count` keys in `drop_stats` — seven from the bridge, three from
  the writer — all excluded from the frame-drop totals by
  `core.pipeline.is_frame_drop_key`, with a test that walks the whole snapshot so
  a key can never fall outside that suffix rule again), with a live alarm on the
  acquisition side and a per-episode summary line. The camera service's own log
  — where stalls, altsetting downgrades and USB resets are written — used to die
  with its `runtime_dir` on every clean shutdown, so the sessions most worth
  keeping left no evidence at all; it is archived to
  `logs/camera_service/<time>_<tag>_camera-service.log` (last 20) with a warning
  excerpt in `main.log`. And the read-only `tools/audit_frame_gaps.py` settles
  the whole library: **25 of 95 episodes really lost picture, 37412.6ms total
  (max 4647.5ms @ 099), and all 25 sit 0.13–1.47s after recording start** — the
  window before the ~1s queue buffer has built up. Counting clocks alone would
  have booked 20.1s of "frames arrived late" as loss; the verdict comes from the
  *picture* (boundary mad against a same-window control), and the clock signature
  only names which side stalled (`Δwall − Δhw` = queue-latency change: positive =
  writer backlog, negative = reader gap draining it). Decoupling recording from
  display, camera-side self-healing, and shrinking the external queue are
  explicitly **not** part of this change; the two timestamps' semantics are now
  written down in `docs/file_format.md` §7.3 and the whole post-mortem in
  `docs/postmortem_trajectory_and_rgb.md` §4.
- **v1.3.9** — connecting a gripper now puts every DECXIN's exposure/white balance
  back to *auto*; until now each newly plugged gripper needed a manual
  `v4l2-ctl -c auto_exposure=3,white_balance_automatic=1`. The dark picture is
  not an acquisition-chain problem — it lives **inside the camera body**:
  `auto_exposure=1` (manual) + `white_balance_automatic=0` pins it at the factory
  `156/10000` (1.56% integration) forever, and that state **survives replugging**,
  so fixing camera 001 does nothing for camera 002. The host had no write path at
  all (the libuvc service only exposes `uvc_set_altsetting_override`;
  `core.camera._apply_exposure_to` belongs to the generic OpenCV camera path,
  which the gripper RGB never takes), hence the manual step.
  `core/gripper/decxin_exposure.py` matches `1bcf:2d4f` only, reads every DECXIN
  on **every connect**, and writes nothing at all when it is already auto (saves
  a USB round trip and avoids wiping a manual exposure the user set on purpose).
  The call site is `bridge._open_run`, **before
  `UvcCameraServiceManager.select()`** — that order is mandatory: once the service
  starts, the device is taken from libusb, the kernel `uvcvideo` driver is
  detached and `/dev/videoN` disappears, after which every V4L2 ioctl fails (this
  is exactly why `v4l2-ctl` used to require stopping the app). It is a purely
  additive step and a failure is only logged: a dark picture is a minor problem,
  a gripper that will not connect is not. The Sightac tactile camera
  (`0c45:636f`) is never touched — its AE=1/AWB=0/6500K is the factory-stored
  state. DECXIN's menu is 1=manual / 3=aperture-priority with **no 0** (writing 0
  returns EINVAL), so candidates are tried 3/0/2; the manual entry point remains
  `venv/bin/python -m core.gripper.decxin_exposure [--dry-run]` (stop the app
  first). Also in this version: the ORB-SLAM **build source** moved into the repo
  (`core/gripper/orb_slam_src/`) — it had only ever lived in the un-uploaded
  `online/`, where all four crash-family fixes were made, so deleting `online/`
  would have left binaries and not one editable line; production is **not** yet
  installed from the new tree and is unverified on hardware. Four of the eleven
  contract tests carry no `unittest.main()` entry point, so running them
  directly only imported them and exited rc=0 with no output — a false green
  that in fact "ran green" three of the four known-RED cases while this was
  being checked; `core/gripper/orb_slam_src/tests/run_contract_tests.py` now
  loads them by module, reports import-time SKIPs with their reason, and exits
  1 only when a module fails to import. And `tools/diag_frame_trace.py` finally **settles the
  v1.3.2 stale-frame question as H2** (the old frame is re-delivered, not just
  mis-stamped): the discriminator has to be an edge map plus a same-window
  control, bare mad gets it wrong — all 5 events captured on 09-17 read H2, so
  the guard's drop is correct and **the stamp must not be "fixed" back**.
- **v1.3.8** — fixes a leftover from v1.3.7: the two DECXINs' by-id link
  **ownership flips on re-enumeration**. Both report serial `01.00.00`, so both
  want the same link name, only one gets it — and *which* one depends on
  registration order. v1.3.7 pushed only the **link-less** camera down to the
  topology path and left the link holder on by-id, so when the link flipped
  both cameras changed key at once and the bug survived. Caught live in
  `logs/main.log`: `Connected: DECXIN_head` immediately followed by
  `Disconnected: DECXIN DECXIN CAMERA` — the panel drops the device along with
  the name the user gave it. `list_v4l_devices()` now reports
  `by_id_ambiguous`, and **both** cameras fall back to the topology path, so
  each keeps a single key whoever wins the link.
  The detection deliberately is *not* "count duplicate prefixes": at any
  instant only one camera holds a link, so the other never enters the count and
  that check would never fire. The rule is instead "a link-less physical device
  whose (vendor, model) matches a **linked** one" — two cameras of the same
  model with **different** serials each keep their own link and are not
  downgraded. `by_id_path` is untouched (still a real path or `None`;
  `ui/lite_window.py` opens the device with it). One-time cost: the standalone
  DECXIN keys as `uvc:usb-1-5`, so the `DECXIN_head` entry saved under the old
  by-id-shaped key no longer matches — rename it once.
- **v1.3.7** — a standalone DECXIN camera no longer disappears from the device
  panel while the gripper rig is plugged in.
  **Two independent root causes — fixing only the first looks like it works
  until the rig is plugged in again:**
  (1) v1.3.0 added `1bcf:2d4f` to the gripper-component blacklist wholesale, so
  the camera could never enter the generic UVC list — while the "gripper" group
  that should have claimed it requires the ESP32 control board to be present.
  Rejected by both, it vanished. `data/device_names.json` still carries
  `uvc:usb-DECXIN_DECXIN_CAMERA_01.00.00` = `"DECXIN_head"`, proof that plugged
  in alone it was a plain UVC camera. Ownership is now decided by **USB root
  hub**: the board sits at `1-2.2.1` and the rig's DECXIN at `1-2.2.2` (both
  root hub `1-2`), while a standalone DECXIN sits at `1-5` — only a DECXIN on
  the board's root hub counts as part of the rig. `gripper_root_hubs()` derives
  that set from the board's tty path, and a camera whose own root hub cannot be
  read is conservatively excluded (better hidden than opened twice against the
  rig). Sightac and the FT602 are *not* released: the former has no standalone
  use, the latter is already covered by `is_sdk`.
  (2) more fundamentally, **udev's by-id link names collide**. The two DECXINs
  report identical vendor / model / serial strings (`DECXIN` / `DECXIN CAMERA` /
  `01.00.00`), so their by-id link names are identical too — and a link name is
  unique, so whichever registers last wins and the other becomes an orphan with
  no link at all. `core.camera.list_v4l_devices()` used **by-id as its
  enumeration entry point**, so the orphan never even reached the filter. It now
  groups by **physical USB device** (`_v4l_nodes_by_physical_device`, keyed on
  the topology path such as `1-5`, falling back to the sysfs realpath for
  non-USB nodes) and keeps the lowest stream index per device; with no link the
  display name falls back to the USB vendor/product strings, which decode to the
  same name the by-id path produced. `by_id_path` stays "a real path, or None" —
  `ui/lite_window.py` uses it as the path to *open* the device, so it must never
  be fabricated. Device keys now degrade from the by-id prefix to
  `usb-<topology path>` (also stable across reboots) before falling back to the
  drifting video index. Known side effect: with both cameras plugged in the
  standalone one keys as `uvc:usb-1-5` and does **not** inherit the saved
  `DECXIN_head` name (that key is by-id-shaped and now belongs to the filtered
  rig camera); name it once in each configuration if it must stay stable. The
  Lite edition now scans the gripper first so it has the root hubs before
  filtering UVC.
- **v1.3.6** — the Lite edition records the UMI gripper, and every SLAM point now
  carries the host timestamp of the frame it came from.
  **Lite + gripper:** the Lite edition did not record the gripper at all; that
  exclusion is now reversed. The whole gripper chain (force, 10×10×3 force
  matrix, gripper state, SLAM trajectory) is recorded with **no gripper
  information shown in the UI** — the gripper RGB becomes the main video source
  directly. The payload is a hard constraint: the Linux package ships the
  ~460 MB native stack as a *required* resource and `start_lite.sh` verifies 7
  items up front, refusing to start with `[错误 B]` if any is missing, so an
  upgrade on an older machine is stopped before it fails halfway; the Windows
  package deliberately omits that payload (the native stack is ELF x86-64) and
  its gripper group stays permanently empty. The three distribution shells
  (`start_lite.bat`, `start_lite.sh`, `使用说明_lite.md`) moved from
  `lite_package/` to the **repository root** next to `main_lite.py`, so the root
  is directly runnable; `lite_package/*` is now untracked entirely. Force-matrix
  encoding moved out of `ui/main_window.py` into the new `core/gripper_codec.py`
  (re-exported by the UI) — it is a data contract and must not exist twice, and
  the Lite import blacklist excludes `ui/main_window`, which pulls in the whole
  main UI. That module may depend only on numpy / `config.settings` /
  `config.i18n`, never `core.gripper`: `core/gripper/__init__.py` imports
  `fays_runtime` (top-level `import fcntl`) at import time, and `fcntl` does not
  exist on Windows.
  **SLAM host stamps:** each trajectory point's `t` is the camera *sensor* clock
  while a video row's `hardware_ns` is the *host monotonic* clock — different
  timebases whose offset can only be fitted (three reverse estimates spread
  ~104 ms, more than one 33.3 ms frame interval), so aligning SLAM points to
  video frames 1:1 had been reduced to row-index pairing (median residual
  83 ms). Native now takes `CLOCK_MONOTONIC` once at the `stereoCallback` entry
  (before the deep copy — "the frame reached this process", not "processing
  finished") and carries it through `RawImageFrame` → `PreparedFrame` →
  `OutputFrame` (the prepared-frame slot is latest-wins, so timestamp and stamp
  must travel together or they get paired with the wrong frame) out to the
  `Host:(<ns>)` field on stdout. `protocol.py` parses it as an **optional**
  group — deployed older binaries do not print it and still parse, with
  `host_mono_ns = None` — the bridge adds a 5th **object** signal argument
  (`pyqtSignal(int)` would silently truncate nanoseconds to qint32), and it
  lands as `observation.{prefix}slam_trajectory_ns` (`list<int64>`, declared
  `flat_parallel_to_slam_trajectory` / `host_monotonic_ns`), accumulated in
  lockstep with the point list: one value per point, 0 = unknown, never skipped,
  and no column at all when a whole segment is unstamped. Verified on real
  hardware with episode-085/086 (the first two recorded after the deploy; the
  earlier 128 carry no such column): points = stamps (353/353 and 344/344), zero
  length mismatches, zero all-zero rows, strictly increasing, stamped range
  inside the segment's `hardware_ns` range (same timebase) — nearest-neighbour
  residual median 6.9 ms with **94.6 % / 97.4 % inside half a frame interval**,
  against 83 ms for row-index pairing. Also adds
  `tools/dedup_gripper_native.py`, which recovers 339 MB of `.so` files that a
  ZIP / `cp -rL` transfer had dereferenced from symlinks into full copies (the
  worst case being one libopenblas stored 7 times in orb48_env).
- **v1.3.5** — the camera service heals itself, and gripper scan pairing moved
  from USB root ports to NVS serials.
  **Cameras:** the UVC cameras (DECXIN `1bcf:2d4f` plus two Sightac `0c45:636f`)
  can wedge silently with no process touching them — on 2026-09-15 the right
  Sightac was streaming normally at 21:53 and simply refused to start at 09:24,
  with no usbfs or uvcvideo message in between and the machine awake all night.
  What the user sees ("RGB shows only the first frame", "camera-service did not
  obtain valid MJPG frames from all cameras within the time limit") is really a
  failure to recover, not a failure to stay up. Two different wedges need two
  different cures: a **stall** (stream running, `received` freezes, not a single
  kernel message, 6–82 s at random, load-correlated) recovers from rebuilding
  the libusb context alone (6/6 and 5/5 measured — a USB reset is a wasted
  cost), while a **hard failure** (`uvc_start_streaming failed: I/O error`,
  `UVC format/interface negotiation failed: Invalid mode`, or
  `negotiated_payload` alternating 3072/0) does not (9 consecutive attempts
  failed; the device firmware is wedged) and only a USB reset / re-enumeration
  clears it. Added a 3-second stall watchdog (`STREAM_STALL_TIMEOUT_SECONDS`,
  4 s for the first frame); stalls rebuild the libusb context, hard failures
  escalate to a USB reset after **2 consecutive** attempts (capped at
  `USB_RESET_LIMIT` 3, since without a cap a genuinely dead device would reset
  every 2 seconds and pin the two neighbours on the same hub), and a stall
  clears that counter so stalls can never add up into a spurious reset. The
  reset now happens **before** `uvc_stop_streaming` (`teardown_failed_open`):
  on a wedged device `uvc_stop_streaming` waits for isochronous transfers that
  never return, which measurably burns the client's 2-second grace period and
  gets the process SIGKILLed mid-teardown, leaving the device on an active alt
  setting so the next start fails the same way. Every threshold is derived
  backwards from the client's `SERVICE_START_TIMEOUT_S = 10.0` (second failure
  at t≈2 s → reset → frames by t≈3 s); the old "reset after 20 s of zero
  frames" gate could never fire in a real start, and was attached to the
  zero-frame branch the observed `I/O error` never reaches. Measured reset
  semantics: on a healthy device it is an in-place port reset (kernel logs
  `reset high-speed USB device number N`, devnum unchanged); on a wedged device
  the device drops and re-enumerates (devnum changes) — that is what heals it,
  and libusb reports `rc=-4 (No such device)` during that window, which is
  misleading but harmless. Also fixed the per-second fps reporting baseline
  (it counted from zero, so a restart reported the whole process lifetime as
  one window: 164/207/251 fake fps).
  **RGB disconnects, root cause:** self-healing was only half the story — the
  camera still dropped out afterwards. DECXIN stalls silently every 68–116 s
  while running the high-bandwidth isochronous setting alt7, on **both**
  controllers tested (AMD `1022:43fc`/PCI `0000:0a:00.0` and
  `1022:15b7`/`0000:74:00.4`). The mechanism is **not determined**: measured
  throughput at the moment of the stalls was only 1.66 MB/s mean / 2.63 peak,
  far under the 10.24 MB/s alt7 reserves, so "not enough bandwidth" does not
  explain it. alt6 holds, and both rungs deliver **identical 30.00 fps**, so
  alt7's extra per-frame headroom (327680 vs 241664 B) buys nothing observable.
  Two earlier conclusions are retracted: *"alt7 is fine on bus 1"* — that
  supposedly clean leg never ran at alt7 at all, because the old binary's ini
  fields were inert and the service merely echoed them, so the only trustworthy
  indicator of a leg's real setting is the `[LIBUVC-QUIRK] ... alt=N payload=M
  ... xfers=` line; and the controller-specific claim that followed from it.
  `forced_altsetting`/`forced_payload` are now **authoritative** through a new
  libuvc runtime API `uvc_set_altsetting_override()` (declared in `libuvc.h`,
  implemented in `device.c`) — before this they were only validated and
  printed, so editing the ini changed nothing, which is very easy to misread.
  The starting rung is chosen in Python, not hardcoded per machine: learned >
  the table's most conservative rung > the scanner's preferred one, because the
  rack's bus is not permanent. A ladder then steps down one rung and persists
  the conclusion under `vid:pid × controller PCI path × camera serial` after 2
  consecutive stalls or 3 consecutive failed opens (the latter covers a
  setting that will not fit at STREAMON, where the stream never starts and the
  stall path can never fire); `CLEAN_RUN_SECONDS = 300` clears the stall
  counter. A downgrade must clear **both** counters — the thresholds are
  equality tests, so a counter left standing on its threshold never fires
  again and the failed-open path would jam after a single step. Both log lines
  capture the count *before* the downgrade clears it. The scanner truthfully
  reports the descriptor's preferred rung (alt7); the app checks it is *any*
  rung of the ladder and refuses only on a genuine desync. The C source and its
  build recipe moved into `core/gripper/camera_service_src/` (`./build.sh` is
  the single entry point), and `--test` now runs `ctest` — the ladder test was
  registered with `add_test` but had never actually been executed, and the
  jam-bug above fell exactly inside what it should have covered. Verified on
  hardware: forced alt7 stalled at 78 s and 116 s, the service logged the
  downgrade and recovered to alt6 for 169 s at 30 fps with zero stalls; the
  app-generated ini (alt6 default) then ran **600 s across three legs with 0
  stalls / 0 rebuilds / 0 failed opens / 0 resets** (17513/17496/17487 frames),
  and a post-install 300 s regression on the deployed binary was clean too. The
  ladder test was reverse-verified as non-vacuous: removing the counter reset
  makes it abort.
  **Grippers:** ported the scan chain from the delivery package
  `设备扫描逻辑与程序_20260914`. Pairing used to go through USB root
  ports/controllers — a premise that was simply wrong, since the Fays is a
  USB3/FT602 device on its own 5000M bus and never shared a root with the
  UVC/ESP32 devices. The chain is now: UVC group → the unique ESP32 (path
  starting `bus-outer.`) → send `QF` over serial to read the Fays serial bound
  in its NVS → match against the official SDK's enumerated `device.serial`,
  with a clear refusal when any step is not unique and no fallback to guessing
  by port or enumeration order. The SDK probe is preceded by a
  `validate_fays_superspeed` pre-check and then enumerates serials fast
  (`--serial-only-fast`; a timeout now decodes stdout/stderr and appends the
  last 20 lines instead of reporting a bare timeout). Dual-rig runs skip a
  device another rig holds instead of claiming it. On the serial side the fixed
  boot delay became a 2-second polling `?` handshake, the port is opened
  `exclusive=True`, and `QF` (query) / `WF:<serial>` (write) commands were
  added; `GripperSerial.send()` looked commands up by their first character
  only (`[:1]`), so `"QF"` could never match its own expected list. Prerequisite:
  the ESP32 firmware must understand `QF`/`WF:` — the current `online/esp32_proc`
  firmware does not, and will fail closed with "查询 Fays 序列号失败: ERR".
  Added the offline self-check `tools/tests/test_gripper_fays_pairing.py`.
  **s80c colorize tool:** fixed a duplicated-every-other-frame bug in
  `tools/s80c_arm_convert/convert_arm_dataset.py` (an independent fix committed
  after v1.3.4). `pooled_episodes_v1` recordings write **two rows per frame**
  (`frame_index` = 0,0,1,1,…, with identical `timestamp`/`hardware_ns` in both
  rows) while the video has one frame; the tool used the row count as the frame
  count and then indexed `hw_ns[i]` by video frame number, so every timestamp
  was sent twice — and the offline ISP reuses the previous output for a
  duplicate timestamp, which is what duplicated every other frame. Measured on
  rec001's lossless output: 394 of 833 frames byte-identical to their
  predecessor, all 394 at odd positions (the second of each timestamp pair),
  versus 0 in the source video. The same root cause also **halved the IMU
  sidecar** — `hw_ns[:n_frames]` only reached the 417th timestamp and every
  later IMU sample was dropped by the "later than the last frame" rule, leaving
  416 rows / 13.8 s; after the fix, 832 rows / 27.7 s against a 27.8 s video.
  Redundant rows are now folded by `frame_index`, but **only when every row
  under that index has identical `timestamp` and `hardware_ns`**; otherwise it
  warns and leaves the data untouched. Of the 142 parquets carrying
  `frame_index`, 129 already had one row per frame (no-op), 1 was genuinely
  redundant (rec001, 1666 rows → 833 frames), and 12 had differing timestamps
  between rows (folding would lose them, so they are warn-only). The colorize
  chain itself is untouched: same config, same offline ISP, `--sdk-wb-auto` as
  before, with grey-world gains bit-identical to the pre-fix run.
- **v1.3.4** — a newly plugged-in gripper now generates its own runtime
  calibration and connects, with no manual step. Previously onboarding a new
  Fays S80M took two manual steps — click `device_setup` on the gripper's host
  PC, then run `tools/import_gripper_calibration.py` on the capture machine —
  and missing either one made the app refuse to start ("current Fays is missing
  runtime calibration files … please run device_setup on the gripper host PC").
  Now `SingleFaysLease.acquire()` finds the per-serial YAMLs missing and, under
  the device lock, runs the vendor exporter to read the factory calibration off
  **this gripper itself**, producing both the SDK YAML (port fields rewritten)
  and the ORB YAML (fisheye `stereoRectify` for P1/R1, IMU extrinsics
  `T_b_c1`, plus the five noise/rate fields from the IMU probe), then re-checks
  the on-disk paths and connects. Grippers that already have calibration take
  an `isfile` fast path — zero extra startup cost, no device access. These two
  files are **per-device** factory calibration and cannot be copied between
  units (098 and 099 differ in all three groups), so they can only be read in
  place. Also adds a right-click "re-read factory calibration" entry on gripper
  rows in the device panel (disabled while recording; the gripper is closed
  first and reopened afterwards), and a `--generate` mode for
  `tools/import_gripper_calibration.py` (existing files are backed up to
  `.bak_<timestamp>`) for forced regeneration from the command line. Along the
  way, fixes the provenance comment on line 3 of the generated ORB YAML — the
  vendor template hard-codes `3500000261870088`, so copying it would record
  every new gripper's IMU extrinsics as "taken from another unit"; it now names
  this unit's serial (existing artifacts left untouched). Follows v1.3.3's
  force-matrix / tactile-force timestamp alignment.
- **v1.3.3** — force-matrix / tactile-force timestamps are now recorded and
  aligned to the RGB video. **The force side never had a timestamp at all**:
  within a row, the RGB frame and the force sample are not captured at the
  same instant — RGB takes the *oldest* frame off an external source queue
  while force/matrix take the *newest* sample from a latest-wins slot, so
  "same row index" ≠ "same instant". Force always leads RGB, and the lead
  grows with recording time (RGB source at 30.84 fps vs the 30 fps writer,
  measured ≈1 frame/s — this is where the 3-frame lag seen downstream comes
  from); the force matrix runs on its own pump thread and keeps its own
  instant from the 3-vector force. The fix makes every row self-describing:
  `tactile_ready` gains a fifth `capture_ns` payload taken at the SDK
  callback entry, and `pipeline.write_tactile_force/_force_matrix` persist
  it as separate `observation.gripper_{side}_force_ns` /
  `_force_matrix_ns` sparse int64 columns (same host-monotonic clock as
  `hardware_ns`, declared as `encoding: host_monotonic_ns`), so downstream
  can recover the true pairing by nearest-in-time lookup with a precision
  bound of half a force-sample interval. Also fixed
  `rgb_frame_ready`/`stereo_frame_ready`, where `pyqtSignal(int)` marshalled
  `time.monotonic_ns()` through C++ `qint32` and silently truncated it to a
  negative number that flipped sign every 2.147 s — now marshalled as
  `object`. That truncation is also why un-wrapping `hardware_ns` used to
  invent hundreds of frames of phantom drift and report "median 5.8 frames /
  max ±80 frames" as misalignment (the real figure is ~3 frames; the correct
  rule is **unsigned**: a monotonic clock consumed FIFO means true steps are
  never negative). New `scripts/align_modalities.py` provides the
  nearest-in-time alignment on the processing side and explicitly reports
  "cannot align (must re-record)" for episodes recorded before v1.3.3 rather
  than guessing from a growth rate. Follows v1.3.2's pre-ingest rejection of
  stale SLAM frames and the trajectory deep-copy fix.
- **v1.3.2** — stale SLAM frames (timestamp regression) are now rejected
  before they reach the core library: the bridge counts *consecutive*
  regressions, dropping single-frame glitches but rebasing after 30 — the
  only path that lets it pass a frame the core accepts as a clock jump.
  Previously every regression made the core flush its IMU queue and rebuild
  the map, halting pose output for 0.2–1.34 s (longer when stationary). The
  new `[TIME_DROP]`/`[TIME_REBASE]` lines always reach the GUI log — they
  used to fall into the unmatched-line bucket and get eaten by its 50-line
  budget (measured: 4 regressions in the native log, 0 in the GUI log) —
  and carry a `seq`/`prev_seq` comparison that separates the three causes
  that look identical on timestamps alone (out-of-order delivery / the same
  frame delivered twice / a frame carrying an old timestamp). Per-second
  telemetry `[FPS_DATA]` is now silent while healthy and speaks only on
  anomaly (entry line, recovery line, 30 s repeat while it persists; the
  full per-second stream stays in the native log). `wait_sdk_ready` no
  longer trips over the sticky `state.error` field, so an informational
  line (e.g. ld.so's `cannot be preloaded ... ignored.`) can no longer make
  a run that did print the calibration marker report a misleading "did not
  see [FAYS-CALIB] marker" (the 2026-09-10 18:32 false timeout). The native
  log is archived to `logs/slam_native/` (last 20 kept) before its runtime
  directory is removed — a cleanly exited session's native log used to be
  unrecoverable, which is exactly how the only evidence of a SLAM crash was
  lost. Trajectory display drops the Qt-main-thread deep copy that made the
  `_rendered_trajectory is traj` identity check always false (the direct
  cause of "the view gets slower the longer you record"), raises the
  display cap 400 → 6000, and draws with a vectorised projection plus one
  `drawPolyline` — fixing the visible kinks that appeared after ~13 s
  (decimation doubles its stride, so the vertex count always lands in
  [cap/2, cap]; the data itself was always full 30 Hz). The RGB external
  queues are drained at all three recording boundaries, fixing the previous
  episode's tail plus the start-up window being written as the head of the
  new video (measured ~28 head frames matching the prior episode's last
  frames). Task progress is reported only for tasks present in the
  backend's current list (locally created tasks have no server-side project
  and are rejected 400), and when the backend reports a `"sessions"` basis
  for a project the first report sends this machine's full count as a
  baseline — sending only the increment collapses the backend's count from
  the session count down to the increment itself (seen in the field: a
  3/3/3 project became 1 after one more recording).
- **v1.3.1** — force-matrix storage spec is now a five-way choice
  (int16 / int16×10 / ×100 / ×1000 / float32) switchable from the
  toolbar, defaulting to ×100 — quantisation error down to 0.9 % of the
  sensor's own noise floor while staying at 38 % of float32 size; the
  scale is recorded per episode in the `meta/episodes` row. Tactile
  heat-map display range is now floor-anchored with slow adaptation
  (τ≈8 s), so pressing harder actually changes the colour instead of
  only the contact area. Task-progress reporting retries after a 404 on
  a 10-minute cooldown, recovering without a restart once the backend
  implements the endpoint. The `slam_pose` column is no longer written:
  a fixed 7-value per-frame column cannot be filled by the 20/20/60 ms
  pose burst, and 26.5 % of rows were landing as `[0]*7`, which
  downstream reads as real poses — `slam_trajectory` is now the only
  SLAM pose on disk
- **v1.3.0** — UMI gripper support (Fays S80M stereo SLAM pose/trajectory
  + Sightac left/right tactile force with 250×250 force matrix + DECXIN
  RGB; gripper group in the device panel, hidden when native resources
  are absent), dual-gripper recording (rig1 keeps the legacy slot/column
  contract, rig2 is prefixed `gripper_2_`; per-rig CPU affinity
  partitions with zero shared physical cores + dedicated raw-stream core
  + 30 fps empty-bucket watchdog). SLAM pose convention corrected for
  the new ORB core (X=right / Y=forward / Z=up, pose relativized to the
  origin — no first-frame jump). Stereo preview steady at 15 fps with
  automatic raw-stream reconnect; trajectory merged into the episode
  parquet instead of a txt sidecar. The gripper no longer depends on
  `online/`: native resources mirrored to `core/gripper/native/` (not
  committed), Sightac SDK shipped pyarmor-encrypted,
  `tools/import_gripper_calibration.py` imports per-serial calibration
  for a new gripper. Multiple S80M cameras distinguished by serial /
  USB topology path. Force-matrix scale (int16×10/×100/×1000 / float32)
  is now recorded **per episode** in the `meta/episodes` row
  (`force_matrix_specs`); previously it lived only in the task-level
  `meta/info.json`, where the next recording overwrote it and made
  replay/training read ×10/×100/×1000 values at face value.
  `scripts/repair_force_matrix_scale.py` back-fills the scale of
  already-recorded episodes (meta rows only, data bytes untouched)
- **v1.2.1** — large-file upload no longer killed by the 10 s timeout
  (the send-body phase used the connect timeout; now uses a read-timeout
  window); closing the upload dialog lets the task finish in the
  background (manual uploads share the main window queue, duplicates
  skipped)
- **v1.2.0** — minimal one-click deployment edition (`start_lite.bat/.sh`
  + `venv_lite` whitelist env, ~750 MB): connect devices → record → upload
  only — no login/playback/task page/skeleton solving. Devices: D435
  (1280×720@30 RGB / 848×480@30 depth), one UVC camera (640×480@30
  capture; 640×360@15 preview that auto-switches to the device you open),
  one USB / one BLE glove. Direct x264 (encoder probe skipped).
  `scripts/pack_lite.py` collects the release folder; import-isolation
  guard + hardware-free smoke tests included
- **v1.1.4** — USB Type-C glove support (STM32 CDC serial engine, 60 fps
  IMU + 16×16 tactile); real-time MANO 21-keypoint solving backfills the
  `hand_pose` placeholder columns (with skeleton rendering in the main UI);
  BLE glove dual-serial config update; single-file PyQt5 viewer demo
  (RGB/depth/tactile/skeleton/IMU panels, live playback, draggable progress
  bar) with tests
- **v1.1.3** — dual-directory (`videos/` + `data/`) timeline
  reliability: the S80C/S80M 50→30 decimation now uses wall-clock
  1/30 s buckets with burst backfill (sensor hw-clock jumps no longer
  cause steady frame drops) plus an empty-bucket watchdog (~3%
  empty-bucket rate on healthy recordings, down from 8-15% when
  recording depth). Depth-slot frame drops root-fixed: keep-latest
  depth queue (the old FIFO dropped 35% of engine bursts), x265
  preset=fast, quantization LUT, and alternate-frame engine feed.
  Login & window behavior: closing the startup login now exits the app
  (no more silent guest mode); the upload/playback dialogs get working
  maximize/minimize buttons. Playback: click-on-groove seeking,
  play-after-end restarts, speed-button polish
- **v1.1.2** — upload and playback dialogs reorganized into two-level
  trees; depth storage switched to 12-bit gray HEVC MP4 (gray12le log
  depth codes, same as LeRobot v3; display and storage now share one code
  scale — the old heatmap near/far params are deprecated; falls back to FFV1
  MKV when x265 is unavailable; readers keep legacy MKV/PNG16 fallback).
  Per-episode meta files renamed `file-` → `episode-` (same numbering;
  legacy chunk shards keep the `file-` prefix). In-app deletion now removes
  files outright (no more `_trash/` staging area)
- **v1.1.1** — `stats.json` carries its own `count` accumulator per block;
  the `.stats_state.json` sidecar is abolished
- **v1.1.0** — task-level pooled storage layout (`videos/` / `data/` /
  `meta/` organized as `chunk-NNN/episode-NNN`, one file group per episode);
  one-off migration script for legacy per-session directories

---

## 中文

基于 PyQt5 的多模态数据采集 SDK + GUI：多路相机（UVC 摄像头、Intel
RealSense D435/D405、S80C / S80M 双目）与 BLE 数据手套的同步录制、回放与
HTTP 上传，输出 [EgoData](https://github.com/facebookresearch/egodata) /
[LeRobot v3](https://github.com/huggingface/lerobot) 兼容格式，供机器人
遥操作数据集使用。配套手部 3D 关键点处理链路与离线 SLAM 数据集导出。

**架构定位**：`core/` 是 SDK 核心（采集管线、设备管理器、录制/上传/回放
全部算法口径，只依赖 PyQt5.QtCore，禁止 import ui）；`ui/` 只做界面组装；
`tools/` 是自包含工具链（不被主程序 import），含独立 demo、模型权重、测试。

<!-- 截图占位：界面截图、3D 关键点可视化效果图可放在此处
![主界面](docs/images/main_window.png) -->

### 功能特性

- **多路相机同步录制**：每路独立的录制控制（正常停止保存 / 异常停止丢弃），
  录制时长实时显示
- **可拖拽的多画面网格布局**，回放界面同样支持调位与分割条
- **设备检测面板**：统一枚举 UVC / D435 / S80M 设备，2 秒轮询，点击即显示
- **BLE 数据手套**：左右手各一只，触觉数据与视频同步采集
- **录制历史与回放**：SQLite 持久化，兼容 EgoData / LeRobot v3 会话
- **HTTP 上传**：录制完成后上传至自建服务器，可自动同步/删除本地文件
- **中英文界面切换**（i18n）
- **手部 3D 关键点**：D435 RGB-D 管线（[tools/hand_3d_d435/](tools/hand_3d_d435/)）、
  S80C / S80M 双目三角化、MediaPipe 裸手管线，另有黑手套专用管线
  （YOLO-World 检测框 + RTMPose 关键点，见[黑手套解算](#黑手套解算)）
- **离线 SLAM 数据集导出**（ORB-SLAM 等格式，含校验工具）
- **12-bit 深度视频录制**：单流灰度 HEVC MP4（对数深度码、可逆解码回毫米），
  实时与回放统一 JET 热力图显示
- **录制直出 HEVC**：录制端低码率 HEVC（多编码器自动回退），
  上传端识别 HEVC 后跳过重复压缩

### 快速开始

#### 安装依赖

```bash
python -m venv venv            # Python 必须**恰好** 3.10（手套 SDK 的 ABI 要求）
venv/bin/pip install -r requirements.txt
```

> `mediapipe`、`torch` 等可选依赖未列入 `requirements.txt`
> （代码内惰性导入，缺失时对应功能不可用；一键安装见下方 `start.bat extras`）。
> `pyrealsense2`（D435/D405）与手套骨架解算依赖属默认安装。

#### Windows 一键部署（推荐客户使用）

双击根目录 **`start.bat`**：自动安装 Python **3.10**（无则静默下载安装；手套 SDK 的
解算核心按 3.10 ABI 加密，3.11/3.12 不行）、创建 venv、安装依赖、展开随包的手套
SDK（[4/7]，串口采集 + 触觉降噪 + 实时骨架解算；缺了只打印警告、主程序照常启动）
并启动主程序；已部署过则秒开。常用命令：

```bat
start.bat               部署并启动（默认）
start.bat reinstall     删除 venv 重装（出问题首选）
start.bat extras        追加安装 mediapipe
start.bat extras-torch  追加安装 CPU 版 torch（手部关键点 RTMPose 后端）
start.bat help          打开操作指引与异常排查文档
```

> 完整操作指引、错误码对照与内网离线交付方式见 [使用说明.md](使用说明.md) /
> [使用说明_EN.md](使用说明_EN.md)（离线安装包由 `python scripts/pack_wheels.py` 生成，
> 默认同时把手套工具包裁剪进 `wheels/toolkit/glove_toolkit.zip`）。

**极简版**（只做连接设备 → 采集 → 上传，无登录/回放/任务页/骨架解算）：
Windows 双击 **`start_lite.bat`**、Linux 跑 `./start_lite.sh`，用独立环境
`venv_lite/`（约 750MB）。设备范围同上，**含 UMI 夹爪（仅 Linux）**——夹爪
**只录不显**（只落 RGB 视频 + 力/触觉/力矩阵/SLAM 轨迹列，界面上不显示），
且 Linux 包把约 460MB 的夹爪原生载荷当**必需资源**下发（`start_lite.sh`
启动前逐项校验 7 项，缺了直接报 `[错误 B]` 拒启）；Windows 包有意不带该载荷，
那边的「UMI 夹爪」组框还在、里面永远是空的。详见 [使用说明_lite.md](使用说明_lite.md)。

#### 启动主程序

```bash
./start.sh                     # Linux 一键部署（等价命令：reinstall/extras/...）
./run.sh                       # 已有 venv 时直接启动
venv/bin/python main.py        # 直接启动
run.bat                        # 已有 venv 时直接启动（start.bat 会先部署）
```

首次运行无需手动建配置：`data/tasks.json` 由内置种子任务生成，其余配置文件
在首次保存对应设置时自动创建（仓库只提供 `*.example.json` 模板）。

#### 手部 3D 关键点（D435）

```bash
./tools/hand_3d_d435/run_live_d435.sh                        # 实时 demo（直连相机）
./tools/hand_3d_d435/run_live_d435.sh --replay <会话目录>      # 回放会话
./tools/hand_3d_d435/run_live_d435.sh --glove                # 黑手套模式（见「黑手套解算」）
./tools/hand_3d_d435/run_d435.sh <会话目录>                    # 离线管线
```

#### Demos

```bash
./tools/demos/run_stereo_depth_demo.sh                     # S80M 深度引擎 demo（需 SDK，见下）
venv/bin/python tools/demos/test_stereo_depth_calib.py     # 双目深度标定自检（需 SDK）
venv/bin/python tools/hand_detection/demo_stereo_hands.py  # 双目 + MediaPipe 手部 demo
```

### 硬件支持

| 设备 | 接入方式 | 说明 |
|---|---|---|
| UVC 相机 | `/dev/videoN`（OpenCV V4L2） | MJPG 像素格式；最多 8 路 |
| Intel RealSense D435 / D405 | `pyrealsense2`（默认安装） | RGB + depth 双路槽位；深度以 12-bit 灰度 HEVC MP4 录制（对数深度码）+ 实时 JET 热力图；内置停滞/帧率看门狗自动重连；D405 有独立近距采集配置 |
| S80C / S80M 双目 | FaysSense VI Kit SDK（仓库自带，含 FT602 桥驱动） | 无需安装 SDK；相机档 `STEREO_CAM_FPS`（默认 50fps）按 wall 时钟 1/30s 桶抽帧录制 30fps（突发补录 + 空桶看门狗，健康录制空桶率 ~3%），回调取帧（官方 GUI 同款）；携带硬件纳秒时间戳与 IMU 样本；v1.0.11 起子进程内置 SDK 深度引擎 → 第三格实时深度热力图 + 12-bit 灰度深度视频录制 |
| BLE 数据手套 | `bleak` | 左右手各一只，parquet 列名绑定（`right_glove` / `left_glove`） |
| UMI 夹爪（Fays S80M） | 仓库自带原生栈（`core/gripper/native/`，约 460MB，不入库） | 仅 Linux（原生栈是 ELF x86-64）；最多 2 台（第二台的数据列自动加 `gripper_2_*` 前缀）；录左目 RGB 视频 + `observation.gripper_{left,right}_force` / `_force_matrix`（10×10×3 力矩阵）+ `observation.slam_trajectory` + 夹爪状态，不存双目视频/IMU；新夹爪开启时自动生成逐台出厂标定 |

### 数据格式

录制数据采用**任务级池化布局**（v1.1.0 起，LeRobot v3 命名）：
`data/recordings/<任务>/`——任务名即上传语义的「项目名」，每段 episode
一组文件：

```
data/recordings/<任务>/
├── videos/chunk-NNN/<槽名>/episode-NNN.mp4      # 每段每流一个视频
│                                               # RGB=mp4；深度=12-bit 灰 mp4（回落 mkv）
├── data/chunk-NNN/episode-NNN.parquet           # 每段一个 parquet（zstd，稀疏列）
└── meta/
    ├── info.json                                # 任务级头部；format="pooled_episodes_v1" 为判别键
    ├── stats.json                               # 全任务统计累加器（每列 count/mean/std/min/max）
    ├── tasks.jsonl                              # 任务描述（单行 JSONL 是格式契约）
    └── episodes/chunk-NNN/episode-NNN.parquet   # 每段一行元数据（11 列）
```

- **编号规则**：episode 全局从 N=1 递增，`chunk = (N-1) // 1000`、
  `file = (N-1) % 1000`（`chunks_size=1000`，由 info.json 声明）；同一
  episode 的所有文件共用同一 `(chunk, file)`。异常终止的录制回收其编号
  复用；已完成的编号永不复用（应用内删除即彻底删除）
- **行布局**：data parquet 一行 = 一个 30fps 帧，键列
  （`episode_index` / `frame_index` / `timestamp` / `wall_time` /
  `hardware_ns`）+ 稀疏观测列（`observation.<传感器>` 逐帧、
  `observation.imu` 变长样本列表按 `imu_ts_ns` 对齐、
  `observation.*hand_pose` 解算可用时由手套 IMU 实时回填、否则为零占位，
  也可由后处理回填）。完整接口契约见
  [docs/file_format.md](docs/file_format.md)
- 设备命名遵循 EgoData 标准 `<位置>_<模态>`：如 `head_left_rgb`、
  `head_right_rgb`、`head_depth`、`right_glove`、`right_hand_pose`
- 深度视频：单流 12-bit 灰度 HEVC MP4（对数深度码，可逆解码回毫米；
  x265 不可用时回落 FFV1 MKV），回放显示统一 JET 热力图
- 录制历史与上传队列：SQLite（`data/pipeline.db`）
- 离线处理输出：`keypoints_output/<任务>/episode_NNNNNN/`（镜像池化
  编号，不写回录制目录）

### 黑手套解算

MediaPipe 裸手检测在黑手套上失效（实测 4/68 手），因此黑手套改用专用
的 YOLO + RTMPose 管线解算（黑/灰/任意颜色手套实测 40/40）：

1. **检测框** — 开放词汇 YOLO-World（`yolov8m-worldv2.pt`，提示词
   `hand` / `glove`）或训练的单类 yolo11n 检测器（`best.pt`），运行中
   可热切换
2. **框跟踪** — HandTracker：EMA 平滑跟踪框、新 track 双阈值门控、
   碎片框抑制
3. **关键点** — RTMPose hand5（21 点，ONNX / onnxruntime CUDA）按框
   裁剪推理；可切换 MediaPipe 裁剪后端做对比（运行中热切换）
4. **稳定层** — 逐点置信度加权；低置信时持出上次输出并按平滑框位移
   平移补偿；连续 N 帧低置信后放行本轮骨架（握拳/抓取等真实新姿势
   不被无限冻结）；退化冻结上限与手性票仓防抖
5. **3D 抬升** — D435 RGB-D 深度抬升或 S80C 双目深度引擎。S80C 上
   右目共享左目平滑框按视差平移（`x_r = x_l − fx·B/z`）后复用同一
   无状态 pose 后端；2D 显示与 3D 槽位链解耦

运行位置：

| 位置 | 模式 | 说明 |
|---|---|---|
| 主程序录后处理 | `HAND_TRACK_MODE=glove` | YOLO 框 + RTMPose 2D 关键点写回 `keypoints_output/`（每帧 92 维打包）；`bare` 模式 = MediaPipe 2D + 3D world landmarks |
| D435 实时 demo | `--glove` | RGB-D 实时 3D |
| S80C 实时 demo | `--glove` | 双目深度实时 3D |
| 工具包 | [tools/glove_package/](tools/glove_package/) | 标注、训练（`train_detector.py` → `best.pt`）、CLIP 自动标注 |

```bash
./tools/hand_3d_d435/run_live_d435.sh --glove   # D435 实时手套模式
./tools/hand_3d_s80c/run_live_s80c.sh --glove   # S80C 实时双目手套模式
```

实现位置：检测/关键点前端在 [tools/hand_detection/](tools/hand_detection/)
与 [tools/glove_package/](tools/glove_package/)；录后处理在
[core/hand_tracking.py](core/hand_tracking.py)；实时 demo 在
[tools/hand_3d_d435/](tools/hand_3d_d435/) 与
[tools/hand_3d_s80c/](tools/hand_3d_s80c/)。

### 环境变量

| 变量 | 必需 | 说明 |
|---|---|---|
| `FAYSSENSE_SDK_DIR` | 仅 S80M demo/诊断工具 | FaysSense VI Kit SDK 安装路径（Release 目录），未设置时相关工具直接报错退出；主程序 S80C/S80M 采集链路不需要（仓库内自包含） |
| `FFMPEG_BIN` | 否 | ffmpeg 可执行文件覆盖（渲染/视频写入工具使用） |
| `VENV_PY` | 否 | launcher 脚本使用的 Python 解释器，默认 `venv/bin/python` |

### 目录结构

```
collector/
├── main.py                    # 程序入口（qt-material 暗色主题）
├── start.bat / start.sh       # 一键部署（Windows / Linux）
├── run.bat / run.sh           # 已部署时直接启动
├── requirements.txt           # 主程序必需依赖（start.bat/start.sh 依赖自检与之一致）
├── .gitignore / .gitattributes # 仓库排除规则 / 换行符规范（bat 强制 CRLF）
├── core/                      # ★ SDK 核心（仅依赖 PyQt5.QtCore 与 config，禁止 import ui；
│                              #   下列按数据流排序：采集 → 写入 → 回放/上传）
│   ├── pipeline.py            # 录制主循环 / 管线状态机（数据流中枢）
│   ├── device_manager.py      # 统一设备 worker 注册表 + 面板开关分派
│   ├── camera.py              # UVC 相机采集（CameraWorker）
│   ├── d435_manager.py        # RealSense D435/D405 采集（热力图/EMA/录制写入）
│   ├── s80m_manager.py        # S80C/S80M 子进程采集 + 50→30 抽帧
│   ├── ble_engine.py          # BLE 数据手套采集
│   ├── egodata_writer.py      # EgoData / LeRobot v3 录制写入（池化落盘）
│   ├── depth_codec.py         # 12-bit 对数深度码编码（gray12le HEVC 视频）
│   ├── encoder_probe.py       # HEVC 编码器可用性探测（nvenc → x265 → x264）
│   ├── session_catalog.py     # 会话扫描 / 元数据 / 帧率解析
│   ├── session_loader.py      # 回放后台加载器（QtCore 信号）
│   ├── session_timeline.py    # 回放时间线
│   ├── depth_reader.py        # 深度视频读取（gray12le MP4 / FFV1 MKV / 旧 PNG16）
│   ├── uploader.py            # 会话上传队列
│   ├── hand_tracking.py       # 手部关键点处理（手套 / 裸手）
│   ├── helpers.py             # 会话路径/时长/大小等通用工具
│   └── …                      # 其余辅助模块：标定、曝光、命名、SQLite 历史、
│                              #   任务轮询、渲染等（详见 docs/core.md）
├── ui/                        # PyQt5 界面组装
│   └── main_window.py         # 主窗口：槽位/录制控制/面板分派（数据流接线点）
├── config/                    # 全局配置 + i18n 文案 + 标定/传感器 JSON
├── scripts/                   # 离线处理（process_hands.py）+ 离线部署打包（pack_wheels.py）
├── docs/                      # 模块文档（逐目录说明，见下）
├── data/                      # 本地配置模板 + 录制数据（均 gitignore）
└── tools/                     # 工具链（自包含，不被主程序 import）
    ├── gongsitubiao.png        # 界面 logo（ui/main_window.py、ui/task_page.py 引用）
    ├── stereo_s80m/           # S80M 双目工具（read_stereo_rgb.py = 主程序采集子进程）
    ├── hand_detection/        # YOLO 手套检测 + MediaPipe 裸手管线
    ├── hand_3d_d435/          # D435 RGB-D 3D 手部关键点（独立模块）
    ├── hand_3d_s80c/          # S80C 双目实时裸手/手套关键点 demo（含自包含 SDK）
    ├── glove_package/         # YOLO-World + RTMPose 黑手套工具箱
    ├── fayssense_depth_sdk/   # FaysSense VI Kit 深度引擎 SDK（专有）
    ├── models/                # 模型权重（MediaPipe hand_landmarker.task）
    ├── weights/               # CLIP 等大权重（gitignore，不随仓库分发）
    ├── demos/                 # 交付版 demo 与自检脚本
    └── tests/                 # 回归 / 冒烟测试
```

### 测试

```bash
# 无真机可跑的离线测试（QT_QPA_PLATFORM=offscreen）
venv/bin/python tools/tests/test_playback_multifps.py
venv/bin/python tools/tests/s80m_signal_regression.py
venv/bin/python tools/tests/s80m_50fps_decimation_test.py
venv/bin/python tools/tests/multi_device_registry_test.py
venv/bin/python tools/tests/exposure_control_test.py
venv/bin/python tools/tests/test_meta_devices.py
venv/bin/python tools/tests/test_depth_heatmap.py
venv/bin/python tools/tests/glove_widget_test.py
venv/bin/python tools/tests/grid_drag_fps_test.py
venv/bin/python tools/tests/device_panel_gui_smoke_test.py
venv/bin/python tools/tests/test_device_detector.py
# 夹爪 RGB 帧空洞可见化（v1.3.10）
venv/bin/python tools/tests/test_frame_gap.py
venv/bin/python tools/tests/test_rgb_quality.py
venv/bin/python tools/tests/test_ext_frame_gap.py
venv/bin/python tools/tests/test_camera_log_archive.py
venv/bin/python tools/tests/test_audit_frame_gaps.py
# 全库帧空洞审计（只读；画面证实丢帧时退出码 1）
venv/bin/python tools/audit_frame_gaps.py
# 手套 SDK v2.1.0 迁移（v1.3.11）
venv/bin/python tools/tests/test_glove_sdk_boot.py
venv/bin/python tools/tests/test_glove_registry.py
venv/bin/python tools/tests/test_glove_backend_parity.py
venv/bin/python tools/tests/test_glove_engine_sdk_guards.py
venv/bin/python tools/tests/test_sdk_python_version.py
# 真机相关测试需连接对应设备：d405_worker_test、d435_e2e_test、
# d435_gui_smoke_test、mono_regression、d435_playback_test 等
```

### 文档

各模块细节见 [docs/](docs/)（每个目录一篇说明：定位、文件清单、数据流）：

- [docs/index.md](docs/index.md) — 仓库总览（本文档集的入口）
- [docs/core.md](docs/core.md) — SDK 核心（管线、设备管理器、录制/上传/回放）
- [docs/ui.md](docs/ui.md)、[docs/config.md](docs/config.md) — 界面、配置
- [docs/data.md](docs/data.md) — 配置与数据存储结构
- [docs/scripts.md](docs/scripts.md) — 离线处理与部署脚本
- [docs/tools.md](docs/tools.md)、[docs/demos.md](docs/demos.md) — 3D 工具、交付 demo
- [docs/stereo_s80m.md](docs/stereo_s80m.md)、[docs/hand_detection.md](docs/hand_detection.md) — S80M、手部检测
- [docs/file_format.md](docs/file_format.md) — 数据文件接口契约（v1.1.x 任务池化布局权威定义）
- [使用说明_lite.md](使用说明_lite.md) — 极简版使用说明：设备（含 UMI 夹爪）、录制、上传、验收清单
- [使用手册.md](使用手册.md) — 完整操作手册（中文 + English）

### 隐私与本地配置

真实配置文件**不进仓库**：`data/server_config.json`（可能含服务器地址与登录凭据）、
`data/device_names.json`（key 含设备序列号/MAC）、`data/tasks.json` 以及
`data/*.db`、`data/recordings/` 均被 `.gitignore` 排除；除
`data/device_params.json`（出厂默认空配置）外，仓库只提供
`*.example.json` 模板。请勿将修改过的真实配置文件提交。

### 第三方组件与许可

- **FaysSense VI Kit SDK**：专有软件；主程序 S80C/S80M 采集链路使用仓库自带的
  `tools/stereo_s80m/lib` 与 `tools/hand_3d_s80c/third_party`（含 FT602 桥驱动
  libft602.so 与 OpenCV 4.2 依赖），git 克隆后无需安装 SDK 即可运行；
  `tools/fayssense_depth_sdk/` 为内网共享副本（S80C demo 用），对外开源发布前
  需从历史中移除。独立 demo/诊断工具也可经 `FAYSSENSE_SDK_DIR` 指向自行安装的 SDK
- **模型权重**：`tools/models/hand_landmarker.task`（MediaPipe）等遵循各自上游许可；
  CLIP 等大权重位于 `tools/weights/`，不随仓库分发。使用 / 再分发前请核实上游许可条款
- **ffmpeg**：录制器优先使用 `imageio-ffmpeg` 捆绑的静态 ffmpeg

### 许可证

本仓库 LICENSE 待定（发布前补充 LICENSE 文件）。第三方组件（SDK、模型权重、
ffmpeg 等）的许可以其上游条款为准。

### 贡献

欢迎提交 Issue 与 Merge Request。开发约定（版本号唯一定义在 `config/__init__.py`、
i18n 文案经 `tr()` 翻译、PyQt5 信号参数用 `object` 封送大整数、core 禁止 import ui
等）见 [docs/index.md](docs/index.md#开发约定)。

### 更新记录

- **v1.3.11** — 手套链路整体换厂商 SDK v2.1.0（`tools/glove_sdk/`，取代 fork 的
  `core/glove_usb`），全栈因此锁到 **Python 3.10**：SDK 的 `algorithm/` 是 PyArmor
  按 3.10 ABI 加密的，3.11+ 下 `import sdk.api` 直接失败 ⇒ 丢的是**整条手套链路**，
  不是「少个骨架」。四个分发壳的判据改成「**恰好** 3.10」，客户机上 3.12 的 venv
  自动重建。同一版修掉四个静默坏：①骨架列**永久空白且零报错**（SDK 把 warmup 判据
  换成反极性的 `warming_up`，旧键名 `warmup_completed` 恒 False ⇒ `process()` 永远
  返回 None）；②左手触觉画面**整体转 90°**（左手帧是规范系的 `[::-1, ::-1].T`，不是
  「右手整块行镜像」，现由 `canonical_pressure_matrix()` 反变换、两只手共用同一段
  渲染代码）；③第 3 只手套被判成左手并与真左手**同列**互写（删掉 `SENSOR_NAMES[-1]`
  兜底，改由调用方拒绝连接）；④一键部署的两类 venv 故障。另兜住 SDK 传输层两处
  缺陷：`stop()` 不终结 ⇒ 孤儿读线程永久占 tty（症状是「必须重启程序」）、单锁竞争
  把静默那条流饿死。夹爪侧观测性（L0/L1）：采集收尾分段计时、原始流接收停滞看门狗、
  SLAM 位姿发散审计脚本、力矩阵落盘 schema 契约测试。注册表读取收归
  `core/glove_registry.py`（SDK 的 `load_device_registry` 会抛**裸 `ValueError`**
  炸掉整个设备面板，而 SDK 任何一次绑定写入都会**抹掉注册表里的其它键**），
  `glove_devices.json` 升至 schema v2 复数数组（换机/改号后旧号仍认得出）。
- **v1.3.10** — 夹爪 RGB 的**静默空洞**从此有仪表、有告警、有留档。`episode-099.mp4`
  开头那一段静止不是「开头没流」：前 45 帧是真帧（场景本身静止），真正丢的是
  **row44→row45 之间的 4.68 秒**（`hardware_ns` +4680.8ms 与 `wall_time` +4666.4ms
  同步跳）—— 而当时**所有计数器都是 0**、日志里一行都没有。「相机侧没帧可读」
  「emit 帧槽被顶掉」「GUI 主线程卡顿」这三条路在 parquet 里留下的是**同一个签名**，
  现在各记各的账（`drop_stats` 里十个 `*_ms`/`*_count` 键——桥接七个 + 落盘三个，
  全部由 `core.pipeline.is_frame_drop_key` 挡在「丢帧统计」之外；`test_gripper_bridge`
  逐个键走一遍这份后缀规则，免得再有一个键漏到帧数里去），采集侧当场出声、录制结束
  再打一行汇总。相机服务自己那份日志（停摆/降档/USB 复位全写在那里）原先随
  `runtime_dir` 一起被删——一次干净退出的会话（正是最该留证据的那种）等于没写过；
  现在留档到 `logs/camera_service/<时刻>_<tag>_camera-service.log`（最近 20 份），
  告警行摘录进 `main.log`。只读的 `tools/audit_frame_gaps.py` 把全库一次数清楚：
  **95 段里 25 段画面真的丢了，累计 37412.6ms（最大 4647.5ms @ 099），且 25 起
  全部落在开录后 0.13~1.47s** —— 也就是那 ~1 秒队列缓冲还没垫起来的窗口。只按钟
  算会把 20.1s 的「帧到得晚」记成损失；结论现在由**画面**给出（事件处帧差对同窗
  对照），钟只用来指认**哪一侧停了**（`Δwall − Δhw` = 队列滞留变化：正 = 写侧积压，
  负 = 读侧空窗排空）。录制与显示解耦、相机侧自愈、外部队列改小**本次都不做**；
  两个时间戳的语义写进了 `docs/file_format.md` §7.3，完整复盘见
  `docs/postmortem_trajectory_and_rgb.md` 第四节。
- **v1.3.9** — 连夹爪时自动把 DECXIN 的曝光/白平衡写回「自动」：此前每接一台新
  夹爪都要人工跑一次 `v4l2-ctl -c auto_exposure=3,white_balance_automatic=1`。
  画面暗的根因不在采集链，而在**相机机身**——`auto_exposure=1`（手动）+ AWB=0
  会让它永久停在出厂 `156/10000`（1.56% 积分）的积分上，而这个状态**存在相机
  里、跨重插保持**，所以「修好 001 那台」对 002 一点用都没有。主程序侧此前根本
  没有写入口（libuvc 服务只有 `uvc_set_altsetting_override`；
  `core.camera._apply_exposure_to` 是 OpenCV 通用相机那条路，夹爪 RGB 不经过），
  于是只能人工介入。新的 `core/gripper/decxin_exposure.py` 只认 `1bcf:2d4f`，
  **每次连接**把所有 DECXIN 读一遍，已经是自动档就一个字节都不写（省一次 USB
  往返，也不会每连一次就把用户特意设的手动曝光抹掉）。调用点在
  `bridge._open_run`、**早于 `UvcCameraServiceManager.select()`** —— 这个顺序是
  硬要求：服务一开设备就被 libusb 拿走、内核 uvcvideo 被摘、`/dev/videoN` 随之
  注销，之后所有 V4L2 ioctl 都会失败（这正是「要停掉主程序才能 v4l2-ctl」的
  由来）。纯附加动作，失败只记日志——画面暗是小事，连不上是大事。Sightac 触觉
  相机（`0c45:636f`）绝不触碰：它的 AE=1/AWB=0/6500K 是原厂存储态。DECXIN 的
  `auto_exposure` 菜单是 1=手动 / 3=光圈优先、**没有 0**（写 0 得 EINVAL），故
  候选序 3/0/2；手工入口仍在：`venv/bin/python -m core.gripper.decxin_exposure
  [--dry-run]`（需先停主程序）。
  同版另进三样。**ORB-SLAM 构建源入库**：`core/gripper/orb_slam_src/` 从此是
  唯一真源，它原先只活在不上传的 `online/` 里，而四个崩溃家族的修复全在那份
  源码上——online 一删就只剩二进制、一行都改不动（`pack_lite.py` 按名字整棵排除
  它：树下有 `build/`、`dist/*/bin/` 这些 ELF 产物，收进 Windows 包会撞上
  「零 ELF」硬断言）。生产**尚未**从新树安装、真机未验。**契约测试补加载器**：
  11 份只读 ORB/桥接源码的测试里有 4 份（`test_connect_debug`、
  `test_fays_sdk_shutdown`、`test_orb_stereo_baseline`、
  `test_slam_offline_evaluation`）**没有 `unittest.main()` 入口**，直跑只 import
  一遍就退出 —— `rc=0`、零输出，看着全绿（本次核对时实测把 4 个 RED 里的 3 个
  这样「跑绿」了）；改由 `core/gripper/orb_slam_src/tests/run_contract_tests.py`
  按模块加载，import 期 SkipTest 的如实报 SKIP 与原因，只有模块 import 失败才
  `rc=1`。**陈旧帧取证定案 H2**：`tools/diag_frame_trace.py` 判定下陷
  帧是**往前第 L 帧的旧图像被重新投递**（几何是 80ms 前的、电平是当帧的），
  不是「只戳错」——判据必须用**边缘图 + 同窗对照**（`|Δx|+|Δy|` 抵消整幅电平
  漂移；同窗正常帧给出 `e(L)/e(1)` 的对照带），**裸 mad 会判错**（第一版据此
  给过两个错的「只戳错」）。09-17 17:06 真机抓到的 5 个事件全判 H2 ⇒ v1.3.2
  守卫的「丢帧」处置是对的，**不该把戳修回来**（修回来等于把 80ms 前的
  measurement 当成当前帧喂进跟踪器）。工具需先 `preflight` 打印 export 再重连
  夹爪取证，离线自检 `tools/tests/test_diag_frame_trace.py`（16 项，约 5 秒）。
- **v1.3.8** — 修 v1.3.7 的遗留：两颗 DECXIN 的 by-id 链接**归属会在重新枚举时
  翻转**。两颗序列号都是 `01.00.00`，都要同一个链接名，而只有一颗拿得到——**谁
  拿到取决于注册顺序**。v1.3.7 只把**没链接**那颗压到拓扑路径、拿到链接那颗仍用
  by-id，于是链接一翻转两颗一起换 key，问题照旧。真机 `logs/main.log` 抓到：
  `Connected: DECXIN_head` 紧跟 `Disconnected: DECXIN DECXIN CAMERA`——面板上
  设备连同用户起的名字一起消失。现由 `list_v4l_devices()` 报 `by_id_ambiguous`，
  **两颗都退拓扑路径**，谁拿到链接都各守一个 key。
  判据**刻意不是「数前缀重复」**：任一时刻只有一颗真拿到链接，另一颗压根进不了
  计数，那么数永远数不出来。改为「一台没链接的物理设备，其 (厂商, 型号) 与另一台
  **有链接**的相同」——同型号但**序列号不同**的两台各有各的链接，不会被误判降级。
  `by_id_path` 不动（仍是「真路径或 None」，`ui/lite_window.py` 拿它开设备）。
  一次性代价：单插那颗 DECXIN 的 key 变成 `uvc:usb-1-5`，挂在旧 by-id 形式 key
  下的 `DECXIN_head` 不再匹配——重命名一次即可。
- **v1.3.7** — 单插的 DECXIN 相机在夹爪 rig 插着时不再从设备面板消失。
  **两层独立根因，只修第一层会看起来「好了」、rig 一插就复发**：
  ①v1.3.0 把 `1bcf:2d4f` 整颗加进夹爪组件黑名单，它永远进不了通用 UVC
  列表；而本该收它的「夹爪」分组又要求 ESP32 控制板在场——两边都不收就消失了。
  `data/device_names.json` 里躺着 `uvc:usb-DECXIN_DECXIN_CAMERA_01.00.00`
  = `"DECXIN_head"`，证明它单插时本来就是普通 UVC 相机。改按 **USB 根端口**
  判归属：控制板在 `1-2.2.1`、rig 那颗 DECXIN 在 `1-2.2.2`（同属根端口 `1-2`），
  单插那颗在 `1-5`——只有挂在控制板根端口下的 DECXIN 才算 rig 的。`gripper_root_hubs()`
  从控制板的 tty 路径取该集合；相机自身根端口取不到时**保守排除**（宁可藏，
  不可与 rig 双开）。Sightac 与 FT602 不放开：前者单插无用，后者另有 `is_sdk`
  兜底。
  ②更底层：**udev 的 by-id 链接名会撞**。两颗 DECXIN 的厂商/型号/序列号字符串
  完全相同（`DECXIN` / `DECXIN CAMERA` / `01.00.00`），by-id 链接名也就完全相同
  ——而链接名唯一，**只有后注册那颗有链接，另一颗成孤儿**。
  `core.camera.list_v4l_devices()` 原本**以 by-id 为枚举入口**，孤儿压根进不了
  枚举，轮不到过滤器。改按**物理 USB 设备**分组（`_v4l_nodes_by_physical_device`，
  键取拓扑路径如 `1-5`，非 USB 节点退回 sysfs realpath），每个物理设备只留最小
  流索引；无链接时显示名退回 USB 厂商+型号字符串（与 by-id 解码同名）。
  `by_id_path` 保持「有就是真路径、没有就是 None」——`ui/lite_window.py` 拿它当
  **打开设备**的路径，不能伪造。设备 key 从 by-id 前缀退到 `usb-<拓扑路径>`
  （同样跨重启稳定），最后才退到会漂移的 video 索引。已知副作用：两套都插时
  单插那颗 key 是 `uvc:usb-1-5`、**不会自动接上旧的 `DECXIN_head`**（那个 key
  是 by-id 形式、此时归已被过滤的 rig 那颗）；要名字稳定就在两种配置下各命名
  一次。极简版改为先扫夹爪、拿到根端口再过滤 UVC。
- **v1.3.6** — 极简版能录 UMI 夹爪了；每个 SLAM 点带上了它那张取样帧的宿主时刻。
  **极简版 + 夹爪**：极简版此前完全不录夹爪，本次推翻该排除。夹爪整条链（力 /
  10×10×3 力矩阵 / 夹爪状态 / SLAM 轨迹）全部落盘，但界面**不显示**任何夹爪
  信息——夹爪 RGB 直接当主视频源用。载荷是硬约束：Linux 包把约 460MB 的原生
  栈当**必需资源**下发，`start_lite.sh` 启动前逐项校验 7 项，缺任一项报
  `[错误 B]` 拒启，老机器升级会在跑炸之前就被拦住；Windows 包**有意不带**该
  载荷（原生栈是 ELF x86-64），那边的夹爪组框永远是空的。三个分发壳
  （`start_lite.bat`/`start_lite.sh`/`使用说明_lite.md`）从 `lite_package/`
  搬到**仓库根**、与 `main_lite.py` 同级，根目录直接可运行；`lite_package/*`
  随之整体不入库。力矩阵编码从 `ui/main_window.py` 搬到新模块
  `core/gripper_codec.py`（UI 侧 re-export）——它是数据契约，不能在两份界面
  代码里各存一份，而极简版的导入黑名单不含 `ui/main_window`（它拖进主程序
  全部 UI）。该模块只许依赖 numpy / `config.settings` / `config.i18n`，绝不
  可 import `core.gripper`：`core/gripper/__init__.py` 导入期就拉
  `fays_runtime`（顶层 `import fcntl`），而 fcntl 在 Windows 上不存在。
  **SLAM 宿主戳**：轨迹每点的 `t` 是**相机传感器钟**，视频行的 `hardware_ns`
  是**宿主单调钟**，两者不同源、只差一个只能拟合的偏移（三个反推估计散布
  ~104ms，已超一个 33.3ms 帧间隔），于是把它们 1:1 对齐此前只能退化成按行号
  配（中位残差 83ms）。现在 native 在 `stereoCallback` 入口取一次
  `CLOCK_MONOTONIC`（在深拷贝之前——代表「帧到达本进程」而非「处理完」），
  随帧走完 `RawImageFrame` → `PreparedFrame` → `OutputFrame`（预处理槽是
  「最新覆盖」，所以时间戳与宿主戳必须同进同出，否则会配错帧），与位姿一起
  打在 stdout 的 `Host:(<ns>)` 上。`protocol.py` 按**可选**组解析——现场已
  部署的旧二进制不打印这段、照旧可读，`host_mono_ns = None`；桥接加第 5 个
  **object** 信号参（`pyqtSignal(int)` 会把纳秒按 qint32 静默截断）；最终落成
  `observation.{prefix}slam_trajectory_ns`（`list<int64>`，声明
  `flat_parallel_to_slam_trajectory` / `host_monotonic_ns`），**与点列表锁步**
  累加：每点恒定一个值、无戳补 0、绝不跳过，整段全无戳则不建列。真机验收
  （部署后录的 episode-085/086，正是头两段带该列的；此前 128 段都没有）：
  点数=戳数（353/353、344/344）、零长度不等、零全 0 行、严格递增、戳区间落在
  该段 `hardware_ns` 区间内（同一时基）——最近邻残差中位 6.9ms、**≤半帧占比
  94.6% / 97.4%**，而行号配对是中位 83ms。另新增
  `tools/dedup_gripper_native.py`，回收 339MB 被 ZIP / `cp -rL` 从软链解引用
  成实体副本的 `.so`（最夸张的是 orb48_env 里同一个 libopenblas 存了 7 份）。
- **v1.3.5** — 相机服务能自愈了；夹爪扫描配对从 USB 根端口改走 NVS 序列号。
  **相机侧**：UVC 相机（DECXIN `1bcf:2d4f` 与两个 Sightac `0c45:636f`）会**静默
  卡死、且不需要任何进程碰它**——2026-09-15 实证右目 Sightac 是自己夜里坏的
  （21:53 还在正常推流，09:24 启动就坏，中间没有任何进程打开过它、内核无
  usbfs/uvcvideo 消息，机器整夜没睡）。用户看到的「RGB 只剩第一帧」「未在限定
  时间取得全部相机的有效 MJPG 帧」，根子是**服务不会自愈**，不是**服务会倒**。
  两种卡死要分开治：**停摆**（开着流突然不出帧、`received` 冻住、内核一条消息
  都没有，6~82 秒随机、与负载正相关）**重建 libusb 上下文就够**（实测 6/6、5/5
  全恢复，USB 复位是白付的代价）；**开不起来**（`uvc_start_streaming failed:
  I/O error`、`UVC format/interface negotiation failed: Invalid mode`、
  `negotiated_payload` 在 3072↔0 之间跳）**重建上下文救不回来**（实测连试 9 次
  全败，设备固件侧卡死了），只有 USB 复位/重枚举能洗。新增 3 秒停滞看门狗
  （`STREAM_STALL_TIMEOUT_SECONDS`，首帧 4 秒）；停摆（`rc>0`）重建 libusb
  上下文；开不起来（`rc<0`）**连续 2 次**升级成 USB 复位，`USB_RESET_LIMIT` 3 次
  封顶（设备真坏了时不设上限会变成每 2 秒复位一次、把同 Hub 的另外两路一直按在
  地上），而停摆会清零该计数，免得几次停摆凑数误触发复位去打扰邻居。**复位要抢在
  `uvc_stop_streaming` 之前**（`teardown_failed_open`）——卡死设备上
  `uvc_stop_streaming` 会一直等它那批永远回不来的等时传输，实测能把客户端那 2 秒
  宽限耗光而被 SIGKILL，进程死在这中间、设备就停在激活的 alt setting 上，下一轮
  照样起不来（09:24 的日志正是断在 startup failed 与 usb reset 之间）。所有阈值由
  客户端窗口 `SERVICE_START_TIMEOUT_S = 10.0` 倒推：第 2 次失败在 t≈2 秒触发复位、
  t≈3 秒就能出帧；旧的「重开 20 秒零帧才复位」在真实启动场景**永远够不到**，且只
  挂在零帧分支上。复位语义实测：打在**健康**设备上是原地端口复位（devnum 不变），
  打在**卡死**设备上是掉线重枚举（devnum 变了）**这才会好**，libusb 在那窗口里返回
  `rc=-4 (No such device)` 是报错但事办了。真机验证：第 1 次失败不复位、第 2 次
  `usb reset: rc=0 (Success)`、打满 3 次后停手；三路 90 秒回归 0 停摆 0 误复位
  （2694/2683/2684 帧 ≈29.9fps）；SIGTERM 退出 1.16 秒，不超客户端 2 秒宽限。
  **夹爪侧**：移植交付包「设备扫描逻辑与程序_20260914」。配对从 **USB 根端口/
  控制器**改走 **NVS 序列号**——旧规则（外加「同一 USB2 总线出现双 rig 就拒扫」）
  的前提就是错的：Fays 是 USB3/FT602 挂在配套的 5000M 总线上，跟 UVC/ESP32 本来
  就不共根，只能靠猜。新链路：UVC 组 → 唯一 ESP32（`bus-outer.` 开头）→ 串口发
  `QF` 读它 NVS 里绑定的 Fays 序列号 → 与官方 SDK 枚举出的 `device.serial` 比对，
  任一步不唯一就明确拒绝，不再按端口或枚举顺序猜。SDK 探测前先做超速预检，再以
  `--serial-only-fast` 快速枚举序列号（超时不再只报一句 timeout，解码
  stdout/stderr 并附最后 20 行）；双 rig 会「在用时跳过」另一 rig 正持有的设备
  而不是占用。串口侧：固定 2 秒死等改成轮询 `?` 握手，端口加 `exclusive=True`，
  新增 `QF` 查询 / `WF:<serial>` 写入；顺带修 `send()` 只拿首字符查命令表
  （`[:1]`）导致 `"QF"` 永远匹配不上自己期望表的 bug。**前置条件：ESP32 固件必须
  认识 `QF`/`WF:`，当前 `online/esp32_proc` 的固件还不认识，会失败关闭并报
  「查询 Fays 序列号失败: ERR」，需先烧固件。** 新增离线自检
  `tools/tests/test_gripper_fays_pairing.py`。
- **v1.3.4** — 新夹爪接入即自动生成运行标定并连接，不再需要任何人工步骤。
  此前接入一只新 Fays S80M 要人工两步——先在夹爪上位机点 `device_setup`、
  再在采集机跑 `tools/import_gripper_calibration.py` 导入，漏掉任一步主程序
  直接拒绝启动（「当前 Fays 缺少运行标定文件…请先在夹爪上位机中运行
  device_setup」）。现在 `SingleFaysLease.acquire()` 发现 per-serial YAML
  缺失时，就在设备锁内调厂商导出程序从**这只夹爪自身**读出厂标定，生成
  SDK YAML（端口字段改写）与 ORB YAML（鱼眼 `stereoRectify` 求 P1/R1 +
  IMU 外参 `T_b_c1` + 探针读五项噪声/频率），复核落盘路径后直接连上；已有
  标定的夹爪走 `isfile` 快路径，启动零额外开销、不碰设备。这两个文件是
  **逐设备**出厂标定，不可跨设备复用（实测 098/099 三组全不同），只能现场
  读。同时给设备面板的夹爪条目加右键「重新读取出厂标定」（录制中禁用；
  标定前自动关闭该夹爪、完成后开回），并给
  `tools/import_gripper_calibration.py` 加 `--generate`（旧文件先备份
  `.bak_<时间戳>`）供命令行强制重生成。顺带修掉生成产物 ORB YAML 第 3 行的
  出处注释——厂商模板写死指向 `3500000261870088`，照抄会让每只新夹爪的 IMU
  外参都被记成「取自另一台设备」，现改写成本机序列号（历史产物不动）。
  承接 v1.3.3 的力矩阵/触觉力与 RGB 视频的时间戳对齐。
- **v1.3.3** — 力矩阵/触觉力与 RGB 视频的时间戳对齐。**力侧此前完全没有
  时间戳**：同一行里的 RGB 帧和力样本不是同时刻采集的——RGB 走外部帧源队列
  取**队头最旧帧**、力/矩阵走 latest-wins 单槽取**当下最新**样本，于是
  「行号相同」≠「时刻相同」；力恒领先 RGB，领先量随录制时长增长（RGB 源
  30.84fps vs 写线程 30fps，实测约 1 帧/秒，下游观测到的滞后 3 帧即由此而来），
  力矩阵还走独立泵线程、与 3 向量力各记各的时刻。修法是让每行自描述：
  `tactile_ready` 加第 5 个载荷 `capture_ns`（在 SDK 回调入口取），
  `pipeline.write_tactile_force/_force_matrix` 落成独立的
  `observation.gripper_{side}_force_ns` / `_force_matrix_ns` 稀疏 int64 列
  （与 `hardware_ns` 同宿主单调钟时基，features 里声明
  `encoding: host_monotonic_ns`），下游按时间取最近邻即可还原真实配对，
  精度上界 = 半个力样本间隔。顺带修 `rgb_frame_ready`/`stereo_frame_ready`
  的 `pyqtSignal(int)` 把 `time.monotonic_ns()` 按 C++ qint32 **静默截断成
  负数**（每 2.147s 翻符号），改 `object` 封送——这也解释了此前用
  `hardware_ns` 解回卷时凭空造出上百帧假漂移、把「中位 5.8 帧 / 最大 ±80 帧」
  报成对齐误差（真值是 ~3 帧，正确规则是**无符号**：单调钟 + FIFO 消费 ⇒
  真步长恒非负）。新增 `scripts/align_modalities.py` 处理端最近邻对齐工具，
  对 v1.3.3 之前录的 episode 明确报「无法对齐（只能重录）」而不用增长率去猜。
  承接 v1.3.2 的 SLAM 陈旧帧入库前拦截与轨迹显示深拷贝修复。
- **v1.3.2** — SLAM 陈旧帧（时间戳回退）改在入库前拦截：桥接按「连续回退
  次数」判定——单帧脏读丢弃、连丢 30 帧才判定时钟真跳变并换基准放行（此前
  每一帧回退都会让核心库清空 IMU 队并重建地图、位姿停发 0.2–1.34 s，静止时
  更久）。新增 `[TIME_DROP]`/`[TIME_REBASE]` 现场行并**无条件**打进 GUI
  日志——此前它落进未匹配行、被 50 条上限吃掉（实测原生日志 4 次回退、
  GUI 日志 0 条，真出事时反而看不见）；行内带 `seq`/`prev_seq` 对比结论，
  以区分「SDK 乱序投递 / 重复投递同一帧 / 帧配了旧时间戳」三种成因——它们在
  时间戳上长得一模一样，而对策完全不同。逐秒遥测 `[FPS_DATA]` 改「只在异常
  时出声」：健康静默、进入异常与恢复各一行、持续异常每 30 s 重复一行，全量
  逐秒流仍留在原生日志里。修 `wait_sdk_ready` 就绪门被**粘滞** `state.error`
  锁死——任何信息性输出（如 ld.so 的 `cannot be preloaded ... ignored.`）都会
  让标定明明已完成的这一次返回 False、报出误导的「未等到 [FAYS-CALIB] 标记」
  （2026-09-10 18:32 假超时事故，文案白名单 + 去掉粘滞判断双管修）。原生日志
  在其 runtime 目录被删之前留档到 `logs/slam_native/`（保留最近 20 份），修
  「一次干净退出的会话其原生日志不可恢复」——排查 SLAM 崩溃时唯一的证据曾
  随目录一起被删。轨迹显示去掉 Qt 主线程上的整条深拷贝（正是这次拷贝让
  `_rendered_trajectory is traj` 判等恒为假、每个位姿都在主线程复制随会话
  线性增长的点列，即「画一段时间后帧率变低」的直接原因）；显示上限
  400→6000，并以向量化投影 + 一次 `drawPolyline` 绘制，修「十几秒后轨迹显出
  折角」——降采样按 stride 翻倍使顶点数恒落 [上限/2, 上限]，上限 400 时每段
  跨 67 ms 且随 stride 每翻一倍继续变粗，观感像 SLAM 出点变慢，实际数据一直
  是满 30 Hz，是绘制侧丢的。RGB 外部队列在录制开始/结束/中止三个边界排空，
  修「上一段残留 + 本段启动窗口累积被写成新视频开头」（实测每段开头约 28 帧
  与上一段 episode 末帧逐帧匹配，队列满时还占 ~110 MB/槽）。任务进度只上报
  后端当前任务列表里存在的任务（本地自建任务平台没这个项目，上报必被拒
  400），且后端该项目口径为 `"sessions"`（从没收到过任何上报）时首次送本机
  **全量**当基线——只送增量会把后端计数从 session 数砸成增量本身（现场：
  3/3/3 的项目再录一条变 1）。
- **v1.3.1** — 力矩阵落盘规格改为 5 档可选（int16 / int16×10 / ×100 /
  ×1000 / float32），工具栏「🎚 力矩阵精度」可切、默认 ×100——量化误差降到
  传感器自身噪声的 0.9%，体积仍是 float32 的 38%；倍率记在每段一份的
  `meta/episodes` 行。触觉热力图显示量程改「下限固定 + 慢速自适应」
  （τ≈8s）：此前固定量程下量化把梯度压平，用力大小只剩接触面积在变、
  颜色几乎不动。任务进度上报 404 后带 10 分钟冷却期重探，服务端补上端点后
  无需重启自动恢复。不再落 `slam_pose` 列——定长 7 值列填不满 20/20/60ms
  的位姿突发，实测 26.5% 的行被填成 `[0]*7` 且会被下游当真实位姿读；
  `slam_trajectory` 成为位姿唯一落盘形态，新增
  `tools/tests/test_gripper_slam_columns.py` 守住该契约
- **v1.3.0** — 新增 UMI 夹爪全链接入（Fays S80M 双目 SLAM 位姿/轨迹 +
  Sightac 左右触觉力与 250×250 力矩阵 + DECXIN RGB；设备面板夹爪分组，
  原生资源缺失时自动隐藏），支持双臂双夹爪同录（rig1 旧槽位/数据列契约
  不变，rig2 加 `gripper_2_` 前缀；两 rig 独立 CPU 亲和分区零共享物理核 +
  raw 流专用核 + 30fps 空桶看门狗）。SLAM 位姿坐标约定按新 ORB 核修正
  （X=右 / Y=正对 / Z=上，原点后姿态相对化，起始不跳变）。左目显示稳
  15fps（奇偶抽帧）且 raw 流断线自动重连；轨迹并入 episode parquet 列，
  不再落 txt 侧车。夹爪脱离 `online/` 自持：原生资源镜像到
  `core/gripper/native/`（不入库），Sightac SDK pyarmor 加密随包，
  新增 `tools/import_gripper_calibration.py` 搬入新夹爪 per-serial 标定。
  多台 S80M 按序列号 / USB 拓扑路径区分。力矩阵倍率（int16×10/×100/×1000 /
  float32）改记在**每段一份**的 `meta/episodes` 行（`force_matrix_specs`）——
  此前只写在任务级 `meta/info.json`，会被同任务后一段录制覆盖，回放与训练
  按面值读放大 10/100/1000 倍的数；`scripts/repair_force_matrix_scale.py`
  可按实测比值给已录段补写倍率（只补 meta 行，不碰 data 字节）
- **v1.2.1** — 修复大文件上传被 10 秒误杀（发送阶段 socket 超时错用连接
  超时，现改用读超时窗口）；上传对话框关闭后任务在后台继续完成（手动上传
  并入主窗口共享队列，重复提交自动跳过）
- **v1.2.0** — 新增极简一键部署采集版（`start_lite.bat/.sh` + `venv_lite`
  白名单依赖，约 750MB）：只做连接设备→采集→上传，无登录/回放/任务页/
  骨架解算。设备：D435（1280×720@30 RGB / 848×480@30 深度）、UVC 摄像头
  1 台（640×480@30 采集；预览 640×360@15，开哪台自动切哪台）、USB/BLE
  手套各 1 台。x264 直录（跳过编码器探针）。`scripts/pack_lite.py` 收集
  发布目录，含导入隔离断言与无硬件冒烟自检
- **v1.1.4** — 新增 USB Type-C 手套接入（STM32 CDC 串口引擎，60fps IMU +
  16×16 触觉）；录制时实时解算 MANO 21 关键点回填 hand_pose 占位列（含
  主界面骨架渲染）；BLE 手套双序列号配置更新；新增单文件 PyQt5 查看器
  demo（RGB/深度/触觉/骨架/IMU 面板、实时播放与可拖动的进度条）及配套测试
- **v1.1.3** — 双目录制（`videos/` + `data/` 两树）时间轴可靠性：
  S80C/S80M 50→30 抽帧改 wall 时钟 1/30s 桶 + 突发补录（传感器 hw
  时钟跳变不再造成稳定缺帧）+ 空桶看门狗（健康录制空桶率 ~3%，带
  深度录制从 8-15% 降至 ~3%）。深度槽丢帧根治：keep-latest 深度队列
  （旧 FIFO 在引擎突发期丢 35%）、x265 preset=fast、量化查表、
  worker 隔帧喂深度引擎。登录与窗口行为：启动首屏关闭登录即退出
  应用（不再静默进游客模式）；上传/回放对话框的最大化最小化按钮
  在 GNOME/Mutter 下真正可用。回放体验：点击滑槽直接跳帧、播完再点
  播放从头重播、倍速按钮文字不再截断
- **v1.1.2** — 上传与回放对话框改两级树结构；深度存储改 12-bit 灰度
  HEVC MP4（gray12le 对数深度码，与
  LeRobot v3 同款；显示与存储统一码值，旧 heatmap 近/远参数废弃；x265
  不可用时回落 FFV1 MKV；读取端保留旧 MKV/PNG16 回退）。每段文件名前缀
  `file-` → `episode-`（编号不变，旧分片保留 `file-` 前缀不再重名）。
  应用内删除改为直接彻底删除（不再产生 `_trash/` 回收区）
- **v1.1.1** — `stats.json` 自含 `count` 累加器（每块），
  `.stats_state.json` 边车废除
- **v1.1.0** — 任务级池化存储布局（`videos/` / `data/` / `meta/` 按
  `chunk-NNN/episode-NNN` 组织、每段 episode 一组文件）；旧会话目录
  一次性迁移脚本
