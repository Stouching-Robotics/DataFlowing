# collector — Multimodal Data Acquisition SDK · 多模态数据采集 SDK

![Version](https://img.shields.io/badge/version-1.3.4-blue)
![Python](https://img.shields.io/badge/python-3.12-blue)
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
python -m venv venv
venv/bin/pip install -r requirements.txt
```

> Optional dependencies such as `pyrealsense2`, `mediapipe`, `torch` are not
> listed in `requirements.txt` (lazily imported; the corresponding feature is
> unavailable if missing — one-click installs via `start.bat extras` below).

#### Windows one-click deployment (recommended for customers)

Double-click **`start.bat`** in the repo root: it installs Python 3.12
(silent download if absent), creates the venv, installs dependencies, and
launches the main program; already-deployed machines start instantly. Common
commands:

```bat
start.bat               deploy and launch (default)
start.bat reinstall     delete venv and reinstall (first resort when broken)
start.bat extras        additionally install mediapipe / pyrealsense2
start.bat extras-torch  additionally install CPU torch (hand-keypoint RTMPose backend)
start.bat help          open the operation guide and troubleshooting doc
```

> Full operation guide, error-code reference, and intranet offline delivery
> procedure: [使用说明.md](使用说明.md) / [使用说明_EN.md](使用说明_EN.md)
> (offline wheel bundles are produced by `python scripts/pack_wheels.py`).

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
| Intel RealSense D435 / D405 | `pyrealsense2` | RGB + depth dual slots; depth recorded as 12-bit gray HEVC MP4 (log depth codes) + live JET heatmap; built-in stall/framerate watchdog with auto-reconnect; D405 has a dedicated near-range capture profile |
| S80C / S80M stereo | FaysSense VI Kit SDK (bundled in-repo, incl. FT602 bridge driver) | No SDK install needed; camera profile `STEREO_CAM_FPS` (default 50 fps) decimated to 30 fps recording via wall-clock 1/30 s buckets (burst backfill + empty-bucket watchdog, ~3% empty-bucket rate on healthy recordings); callback frame capture (same as the official GUI); carries hardware nanosecond timestamps and IMU samples; since v1.0.11 the subprocess runs the SDK depth engine → third-tile live depth heatmap + 12-bit gray depth video recording |
| BLE data gloves | `bleak` | one per hand; parquet column names bound (`right_glove` / `left_glove`) |

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
  `imu_ts_ns`, `observation.*hand_pose` zero placeholders backfilled by
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
python -m venv venv
venv/bin/pip install -r requirements.txt
```

> `pyrealsense2`、`mediapipe`、`torch` 等可选依赖未列入 `requirements.txt`
> （代码内惰性导入，缺失时对应功能不可用；一键安装见下方 `start.bat extras`）。

#### Windows 一键部署（推荐客户使用）

双击根目录 **`start.bat`**：自动安装 Python 3.12（无则静默下载安装）、创建 venv、
安装依赖并启动主程序；已部署过则秒开。常用命令：

```bat
start.bat               部署并启动（默认）
start.bat reinstall     删除 venv 重装（出问题首选）
start.bat extras        追加安装 mediapipe / pyrealsense2
start.bat extras-torch  追加安装 CPU 版 torch（手部关键点 RTMPose 后端）
start.bat help          打开操作指引与异常排查文档
```

> 完整操作指引、错误码对照与内网离线交付方式见 [使用说明.md](使用说明.md) /
> [使用说明_EN.md](使用说明_EN.md)（离线安装包由 `python scripts/pack_wheels.py` 生成）。

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
| Intel RealSense D435 / D405 | `pyrealsense2` | RGB + depth 双路槽位；深度以 12-bit 灰度 HEVC MP4 录制（对数深度码）+ 实时 JET 热力图；内置停滞/帧率看门狗自动重连；D405 有独立近距采集配置 |
| S80C / S80M 双目 | FaysSense VI Kit SDK（仓库自带，含 FT602 桥驱动） | 无需安装 SDK；相机档 `STEREO_CAM_FPS`（默认 50fps）按 wall 时钟 1/30s 桶抽帧录制 30fps（突发补录 + 空桶看门狗，健康录制空桶率 ~3%），回调取帧（官方 GUI 同款）；携带硬件纳秒时间戳与 IMU 样本；v1.0.11 起子进程内置 SDK 深度引擎 → 第三格实时深度热力图 + 12-bit 灰度深度视频录制 |
| BLE 数据手套 | `bleak` | 左右手各一只，parquet 列名绑定（`right_glove` / `left_glove`） |

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
  `observation.*hand_pose` 恒写占位零由后处理回填）。完整接口契约见
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
