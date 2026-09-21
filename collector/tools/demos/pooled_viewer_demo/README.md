# Pooled Viewer Demo —— 池化录制查看器

单文件自包含的查看器 demo（只依赖 numpy/pyarrow/opencv/PyQt5，不 import 主程序
任何模块）：选中主程序池化录制的 `episode-NNN.parquet` 即可播放 D435 相机视频
与深度伪彩 + 手套触觉矩阵 + 手部骨架 + 手套 IMU 姿态。

各可视化组件的出处（都是**源码级移植**，不是运行时调用）：

| 面板 | 本文件里的实现 | 血缘 |
|---|---|---|
| 触觉矩阵 | `render_tactile_grid` | 厂商 Glove-test V1.4 `glove_qt_visualizer.py` 的 `PressureMatrixCanvas`（QPainter → OpenCV，全仓唯一移植处） |
| 手部骨架 | `render_skeleton` | 主程序 `core/render_engine.py:render_skeleton` ← 工具包 `apps/rendering/replay.py`（主程序实时手套面板用的就是同一血缘那份） |
| 手套 IMU | `render_imu_panel` | 本文件自写 |

注意主程序的触觉可视化**不是**这个矩阵网格：它是 `core/render_engine.py:render_hand`
的仿生手掌（实时面板 + 回放默认模式），`render_grid` 简单网格只是五个可选模式之一。

## 用法

```bash
# 项目 venv（与主程序同一环境）
venv/bin/python tools/demos/pooled_viewer_demo/pooled_viewer_demo.py [episode-NNN.parquet]

# 不带参数启动后点"打开 Parquet"选择文件
```

快捷键：空格 播放/暂停，←/→ 上一帧/下一帧，拖动进度条跳帧。

## 读取的布局（v1.1.2+ 池化，与主程序 core/helpers.py 约定一致）

```
<task>/meta/info.json                        # fps / features / cameras
<task>/data/chunk-NNN/episode-FFF.parquet    # 主时钟 = 行序（30fps 写入节拍）
<task>/videos/chunk-NNN/<image_key>/episode-FFF.mp4   # 每路视频一个文件
```

- 触觉列：`observation.<sensor>` fixed_size_list\<float32,256\>（16×16 摊平）。
  **左右手各一个面板并排、左手在左**，顶栏标传感器名（`left_glove` /
  `right_glove`）；传感器名含 left 时网格行序翻转（左手行序与右手镜像，
  2026-09-04 与实机数据逐格核对）。列是稀疏的 —— 哪只手有数据才有哪列，
  只有单手数据时就只显示那一个面板
- IMU 列：`observation.<sensor>_imu_quat`（16×4 XYZW 摊平为 64）+
  `observation.<sensor>_imu_valid`（16 个掩码）。每传感器一个面板左右并排，
  标题带传感器名；有骨架数据时默认隐藏，勾选"显示 IMU 四元数"切出
- 骨架列：`observation.<side>_hand_pose` fixed_size_list\<float32,63\>（21×3），
  恒写列、全零 = 无数据（会被过滤掉）。左右各一个面板并排
- 帧对齐：视频与传感器行按写入帧号一一对应；播放时视频顺序读、
  跳帧 seek，视频帧数不足保持最后一帧
- 深度流（`*_depth` / `.mkv`，12-bit 灰度 HEVC / FFV1 gray16le）反量化成
  毫米后画 JET 伪彩，与主程序显示口径一致；解码要 ffmpeg（打包版已随包
  带 imageio-ffmpeg，源码运行时装 `pip install imageio-ffmpeg` 即可）

## 依赖

numpy / pyarrow / opencv-python / PyQt5 —— 见 requirements.txt。
