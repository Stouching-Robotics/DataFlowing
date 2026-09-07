# Pooled Viewer Demo —— 池化录制查看器

单文件自包含的查看器 demo：选中主程序池化录制的 `episode-NNN.parquet` 即可播放
D435 相机视频 + 手套触觉（工具包 tactile.py 同款手掌面板 / 16x16 矩阵网格）+
手套 IMU 四元数姿态面板。手套可视化沿用手套工具包
`apps/rendering/tactile.py` 的实现（左右手手指带/掌心布局 2026-08-29 与实机
逐格核对版本），主程序 GUI 不再显示手套画面。

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

- 触觉列：`observation.<sensor>` fixed_size_list\<float32,256\>（16×16 摊平）；
  传感器名含 left/right 时手掌面板自动按对应手布局渲染
- IMU 列：`observation.<sensor>_imu_quat`（16×4 XYZW 摊平为 64）+
  `observation.<sensor>_imu_valid`（16 个掩码）
- 帧对齐：视频与传感器行按写入帧号一一对应；播放时视频顺序读、
  跳帧 seek，视频帧数不足保持最后一帧
- 深度流（`*_depth` / `.mkv`，12-bit 灰度 HEVC）本 demo 跳过，
  完整回放见主程序回放对话框（Gray12DepthVideo 解码）

## 依赖

numpy / pyarrow / opencv-python / PyQt5 —— 见 requirements.txt。
