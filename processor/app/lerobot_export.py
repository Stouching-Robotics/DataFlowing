"""Local-file LeRobot export (v2.1 / v3.0).

The review application keeps the uploaded episode untouched.  This module
creates a derived LeRobot dataset with standard tabular/video metadata.
Depth is carried by the per-camera raw depth-code video (``is_depth_map``)
plus the per-frame ``observation.depth.<source>.valid`` marker; the original
16-bit PNG sequence is not copied into the dataset.

``version`` 决定布局(与官方结构一一对应,两者互不兼容):

- v2.1:每 episode 一个文件 —— ``data/chunk-{episode_chunk:03d}/
  episode_{episode_index:06d}.parquet``、``videos/{video_key}/chunk-
  {episode_chunk:03d}/episode_{episode_index:06d}.mp4``;episode 元数据写在官方旧版
  ``meta/episodes.jsonl``，并附带 ``meta/episodes_stats.jsonl``;info.json 用
  v2.1 路径模板并含
  ``total_videos``/``total_chunks``。
- v3.0(默认):合并分片 —— ``data/chunk-{n}/file-{i:03d}.parquet``(单文件
  含全部 episode)、``videos/chunk-{n}/{video_key}/file-{i:03d}.mp4``;
  episode 元数据在官方的 ``meta/episodes/chunk-{n}/file-{i:03d}.parquet``。
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from app.localstore import get_episode, list_annotations
from app.config import settings
from app.lerobot_v21 import (
    DEPTH_MAX_MM,
    DEPTH_MIN_MM,
    DEPTH_QMAX,
    DEPTH_QP,
    DEPTH_VIDEO_ENCODING,
    depth_video_encoder_args,
    quantize_depth,
)


def _json_default(value: Any):
    if hasattr(value, "tolist"):
        return value.tolist()
    if hasattr(value, "item"):
        return value.item()
    raise TypeError(f"Unsupported value: {type(value)!r}")


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )


def _write_tasks_parquet(output_dir: Path,
                         task_descriptions: dict[int, str]) -> None:
    """Write the v3 task index in the native Parquet representation.

    ``tasks.jsonl`` is kept as a compatibility companion, but v3 consumers
    expect ``meta/tasks.parquet``. 任务文本必须存成 DataFrame index:
    官方加载器按 ``tasks.iloc[task_idx].name`` 取任务名(样本行里的
    ``task`` 字段),存普通列会返回整数下标而非任务文本(实测)。
    """
    import pandas as pd
    items = sorted(task_descriptions.items())
    frame = pd.DataFrame(
        {"task_index": [int(task_id) for task_id, _ in items]},
        index=pd.Index([str(description) for _, description in items],
                       name="task"),
    )
    path = output_dir / "meta" / "tasks.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path)


def _quantiles(arr: "np.ndarray") -> list[float]:
    import numpy as np
    return [float(q) for q in np.nanpercentile(arr, [1, 10, 50, 90, 99])]


def _nanpercentile_axis0(mat: "np.ndarray", qs) -> "np.ndarray":
    """等价于 ``np.nanpercentile(mat, qs, axis=0)``,但整矩阵一次算完。

    numpy 对带 axis 的 nanpercentile 会退化成 ``apply_along_axis`` —— 逐列
    调 Python。触觉力矩阵是 (帧数, 187500),那就是每次 18.75 万次 1-D 调用,
    实测 4.5 秒;两个矩阵就是 9 秒。这里改成"整矩阵排序一次 + 按各列有效值
    个数做向量化索引",全走 C 层,实测 0.30 秒(15 倍),数值逐元素一致
    (linear 插值口径与 numpy 相同,已对无 NaN/含 NaN/单行/整列 NaN/常数列
    逐一比对)。

    NaN 排到每列末尾后不影响取值 —— 有效值个数 counts 把它们排除在外。
    """
    import numpy as np
    qs = np.asarray(qs, dtype=np.float64)
    valid = np.isfinite(mat)
    counts = valid.sum(axis=0)
    safe = np.where(valid, mat, np.inf)
    safe.sort(axis=0)
    n = counts[None, :]
    # linear 插值:virtual_index = q/100 * (n - 1),在 lo/hi 之间按小数部分插值
    pos = (n - 1) * (qs[:, None] / 100.0)
    lo = np.floor(pos)
    frac = pos - lo
    hi = np.ceil(pos)
    last = np.maximum(n - 1, 0)
    lo_i = np.clip(lo, 0, last).astype(np.intp)
    hi_i = np.clip(hi, 0, last).astype(np.intp)
    cols = np.arange(mat.shape[1])[None, :]
    out = safe[lo_i, cols] * (1 - frac) + safe[hi_i, cols] * frac
    out[:, counts == 0] = np.nan  # 与 nanpercentile 一致:整列无效 → NaN
    return out


def _write_stats(output_dir: Path,
                 video_features: dict[str, dict] | None = None,
                 data_table=None) -> None:
    """LeRobot 官方 meta/stats.json:每个数值列 mean/std/min/max(训练归一化用)。

    官方数据集(如 aloha_mobile)在 meta/stats.json 提供统计量,训练管线
    读它做 observation/action 归一化。覆盖三类特征:

    - 标量列:mean/std/min/max + q01-q99 + count;
    - 定长数值向量列(如 hand_left_world 63 维):逐维统计(官方
      pose.head 同款,扁平列表);
    - 视频特征:抽帧解码,逐通道统计(归一化到 [0,1],官方
      observation.images.* 同款 [3,1,1] 嵌套)。

    变长嵌套数组列与字符串列不做数值统计。

    ``data_table``: 调用方刚写进 data/ 的 arrow table。传了就直接转 pandas,
    省掉把整个 data 目录从磁盘再读一遍;不传(旧调用点/表不可用)则退回
    原磁盘路径,行为不变。
    """
    import numpy as np
    import pandas as pd
    try:
        if data_table is not None:
            # ``pd.read_parquet`` 内部就是 ``pq.read_table(...).to_pandas()``,
            # 所以这里转出来的 DataFrame 与重读磁盘逐列同 dtype、同值(实测
            # 校验过),但省掉一次全量往返 —— data/ 实测约 13MB/集,50 集
            # 就是 650MB 从 SSHFS 重新搬进内存再 concat。
            df = data_table.to_pandas()
        else:
            parquet_files = sorted((output_dir / "data").rglob("*.parquet"))
            if not parquet_files:
                return
            frames = [pd.read_parquet(p) for p in parquet_files]
            if not frames:
                return
            df = pd.concat(frames, ignore_index=True)
    except Exception:
        return
    stats: dict[str, dict] = {}
    for col in df.columns:
        s = df[col]
        try:
            if not pd.api.types.is_numeric_dtype(s):
                continue
            arr = s.to_numpy()
            if arr.ndim != 1:
                continue
            if any(isinstance(v, (list, tuple)) for v in arr):
                continue  # 变长嵌套数组列跳过
        except Exception:
            continue
        try:
            num = pd.to_numeric(s, errors="coerce").to_numpy(dtype=float)
        except Exception:
            continue
        if not np.isfinite(num).any():
            continue
        q = _quantiles(num)
        # 只写数值统计量。不要再加 dtype/shape 之类的元数据字段:官方
        # 加载器会把 stats 里的每一项都转成张量做归一化,字符串字段直接
        # 崩在 torch.from_numpy(np.ndarray of type numpy.str_)。官方数据集
        # 的 stats 也只有 min/max/mean/std/count。
        stats[col] = {
            "mean": float(np.nanmean(num)),
            "std": float(np.nanstd(num)),
            "min": float(np.nanmin(num)),
            "max": float(np.nanmax(num)),
            "q01": q[0], "q10": q[1], "q50": q[2], "q90": q[3], "q99": q[4],
            "count": int((~np.isnan(num)).sum()),
        }

    # 定长数值向量列(手部世界坐标 63 维等):逐维统计,官方 pose.head 格式
    try:
        for col in df.columns:
            s = df[col]
            if not isinstance(s.dtype, np.dtype) or s.dtype != object:
                continue
            vals = s.dropna().tolist()
            # pandas 把 pyarrow list 列还原成 ndarray(而非 list),两种都接受
            if not vals or not all(
                    isinstance(v, (list, tuple, np.ndarray)) for v in vals):
                continue
            lengths = {len(v) for v in vals}
            if len(lengths) != 1:
                continue
            try:
                mat = np.array(vals, dtype=float)
            except Exception:
                continue
            if mat.ndim != 2 or not np.isfinite(mat).any():
                continue
            q = _nanpercentile_axis0(mat, [1, 10, 50, 90, 99])
            stats[col] = {
                "min": np.nanmin(mat, axis=0).tolist(),
                "max": np.nanmax(mat, axis=0).tolist(),
                "mean": np.nanmean(mat, axis=0).tolist(),
                "std": np.nanstd(mat, axis=0).tolist(),
                "q01": q[0].tolist(), "q10": q[1].tolist(),
                "q50": q[2].tolist(), "q90": q[3].tolist(),
                "q99": q[4].tolist(),
                "count": int(np.isfinite(mat).all(axis=1).sum()),
            }
    except Exception:
        pass

    # 视频特征:RGB 抽帧逐通道统计(归一化 [0,1],官方
    # observation.images.* 格式)。metric depth 使用 pyav/ffmpeg 读出
    # gray12le 后反量化为毫米；它不是普通 uint8 视频，不能走 OpenCV
    # 解码后统一除以 255 的 RGB 路径。
    try:
        import cv2
        for vkey, meta in (video_features or {}).items():
            if meta.get("dtype") != "video":
                continue
            videos_root = output_dir / "videos"
            videos = sorted(
                path for path in videos_root.rglob("*.mp4")
                if vkey in path.relative_to(videos_root).parts
            )
            if not videos:
                continue
            shape = meta.get("shape") or [0, 0, 3]
            channels = int(shape[2]) if len(shape) > 2 else 3
            video_info = meta.get("video_info") or meta.get("info") or {}
            is_metric_depth = bool(
                video_info.get("video.is_depth_map")
                and str(video_info.get("video.pix_fmt") or "").lower()
                in {"gray12le", "gray16le", "gray10le"}
            )

            # OpenCV converts high-bit-depth gray12le to an 8-bit display
            # image. That loses the metric depth codes and produces stats in
            # an unrelated [0, 1] range after /255. Use the project's depth
            # reader instead, which returns physical millimetres.
            if is_metric_depth:
                from app.lerobot_v21 import DepthVideoReader

                samples: list = []
                for vpath in videos:
                    try:
                        reader = DepthVideoReader(vpath)
                    except Exception as exc:
                        print(f"[lerobot_export] depth stats skipped for {vpath}: {exc}")
                        continue
                    total = int(reader.frame_count or 0)
                    step = max(1, total // 200) if total > 200 else 1
                    read_idx = 0
                    try:
                        while True:
                            depth_mm = reader.read_mm()
                            if depth_mm is None:
                                break
                            if read_idx % step == 0:
                                # Keep the same one-channel shape used by
                                # the RGB statistics below.
                                depth_mm = depth_mm.astype(np.float32, copy=False)
                                # LeRobot's depth dequantizer clamps decoded
                                # values to [depth_min, depth_max].  The raw
                                # reader preserves the transport sentinel 0,
                                # so apply the same rule before calculating
                                # q01/q99; otherwise q01=0 while the GUI sees
                                # those pixels as depth_min (usually 100 mm),
                                # compressing the visible range near yellow.
                                depth_min_m = float(video_info.get("video.depth_min") or 0.1)
                                depth_max_m = float(video_info.get("video.depth_max") or 5.0)
                                depth_min_mm = depth_min_m * 1000.0
                                depth_max_mm = depth_max_m * 1000.0
                                depth_mm = np.nan_to_num(
                                    depth_mm,
                                    nan=depth_min_mm,
                                    posinf=depth_max_mm,
                                    neginf=depth_min_mm,
                                )
                                depth_mm = np.clip(depth_mm, depth_min_mm, depth_max_mm)
                                samples.append(depth_mm[..., None])
                            read_idx += 1
                    finally:
                        reader.close()
                if not samples:
                    continue
                stack = np.stack(samples)  # (T, H, W, 1), values in mm
                per_channel = stack.reshape(-1, stack.shape[-1])

                def _nested(vals):
                    return [[[float(v)] for v in vals]]

                q = np.percentile(per_channel, [1, 10, 50, 90, 99], axis=0)
                stats[vkey] = {
                    "min": _nested(per_channel.min(axis=0)),
                    "max": _nested(per_channel.max(axis=0)),
                    "mean": _nested(per_channel.mean(axis=0)),
                    "std": _nested(per_channel.std(axis=0)),
                    "q01": _nested(q[0]), "q10": _nested(q[1]),
                    "q50": _nested(q[2]), "q90": _nested(q[3]),
                    "q99": _nested(q[4]),
                    "count": [int(per_channel.shape[0])],
                }
                continue

            # 增量累积:全部集的采样帧若一次性留在内存里会爆 —— 960×1280
            # 的一帧 float32 是 14.75MB,34 集 × 约 200 采样帧 ≈ 100GB。
            # 这里只保留跑动统计量(min/max/和/平方和/计数)与一个固定大小的
            # 分位数蓄水池,内存与集数无关。
            ch_sum = np.zeros(channels, dtype=np.float64)
            ch_sq = np.zeros(channels, dtype=np.float64)
            ch_min = np.full(channels, np.inf, dtype=np.float64)
            ch_max = np.full(channels, -np.inf, dtype=np.float64)
            pixel_count = 0
            # 蓄水池上限与每帧贡献:分位数只需近似,但样本必须**跨帧、跨集**
            # 均匀铺开 —— 只取前几帧会让分位数偏向最早的那一集。每帧按固定
            # 步长抽约 1200 个像素,800 万像素上限可覆盖数千帧。
            reservoir_cap = 8_000_000
            per_frame_budget = 1200
            taken = 0
            reservoir: list = []
            for vpath in videos:
                cap = cv2.VideoCapture(str(vpath))
                if not cap.isOpened():
                    continue
                total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
                step = max(1, total // 200) if total > 200 else 1
                # 顺序读、每 step 帧采样一次(随机 seek 在 MP4/HEVC 上
                # 每次都要回溯关键帧,长视频统计会慢一个数量级)
                read_idx = 0
                while True:
                    ok, frame = cap.read()
                    if not ok:
                        break
                    if read_idx % step == 0 and frame.ndim == 3:
                        # 先按步长抽样再转 float:一帧 960×1280 有 120 万像素,
                        # 对整帧做 astype(float32)/255 是这一步的主要开销,
                        # 而统计只需要千级样本。抽样后再运算快约三个数量级。
                        flat_u8 = frame[..., :channels].reshape(-1, channels)
                        stride_u8 = max(1, flat_u8.shape[0] // per_frame_budget)
                        flat = (flat_u8[::stride_u8].astype(np.float32) / 255.0)
                        ch_sum += flat.sum(axis=0, dtype=np.float64)
                        ch_sq += np.square(flat, dtype=np.float64).sum(axis=0)
                        ch_min = np.minimum(ch_min, flat.min(axis=0))
                        ch_max = np.maximum(ch_max, flat.max(axis=0))
                        pixel_count += flat.shape[0]
                        if taken < reservoir_cap:
                            # flat 已是抽样结果,直接入池
                            take = min(reservoir_cap - taken, flat.shape[0])
                            reservoir.append(flat[:take].copy())
                            taken += take
                    read_idx += 1
                cap.release()
            if not pixel_count:
                continue
            def _nested(vals):
                return [[[float(v)] for v in vals]]
            mean = ch_sum / pixel_count
            var = np.maximum(ch_sq / pixel_count - mean * mean, 0.0)
            pooled = np.concatenate(reservoir, axis=0) if reservoir else None
            if pooled is not None and pooled.shape[0]:
                q = np.percentile(pooled, [1, 10, 50, 90, 99], axis=0)
            else:
                q = np.stack([mean] * 5)
            stats[vkey] = {
                "min": _nested(ch_min),
                "max": _nested(ch_max),
                "mean": _nested(mean),
                "std": _nested(np.sqrt(var)),
                "q01": _nested(q[0]), "q10": _nested(q[1]),
                "q50": _nested(q[2]), "q90": _nested(q[3]),
                "q99": _nested(q[4]),
                "count": [int(pixel_count)],
            }
    except Exception:
        pass

    if stats:
        _write_json(output_dir / "meta" / "stats.json", stats)


def _write_episodes_stats_v21(output_dir: Path,
                              episode_row_ranges: list[tuple],
                              rows: list[dict],
                              episode_video_meta_records: list,
                              video_features: dict[str, dict] | None) -> None:
    """v2.1 legacy ``meta/episodes_stats.jsonl``:每集一行 {episode_index, stats}。

    官方 v2.1 每集一份归一化统计(转换脚本/训练管线读取):标量列
    min/max/mean/std/count、定长向量列逐维统计、视频抽帧逐通道统计
    (归一化 [0,1])。count 必须数组格式(官方校验要求)。v3.0 已移除此
    文件(统计收敛进 meta/stats.json + episodes parquet)。
    """
    import numpy as np
    import cv2

    def _nested(vals):
        # 官方转换脚本要求逐通道统计形状 (C,1,1)(实测 (1,C,1) 报
        # "Shape of quantile 'min' must be (3,1,1) or (1,1,1)")
        return [[[float(v)]] for v in vals]

    lines: list[str] = []
    for (ep_index, _task_id, _length, start, end), vmeta in zip(
            episode_row_ranges, episode_video_meta_records):
        ep_rows = rows[start:end]
        if not ep_rows:
            continue
        stats: dict[str, dict] = {}
        for key in ep_rows[0].keys():
            vals = [row.get(key) for row in ep_rows]
            if all(isinstance(v, (int, float)) and not isinstance(v, bool)
                   for v in vals):
                num = np.array(vals, dtype=float)
                if not np.isfinite(num).any():
                    continue
                stats[key] = {
                    "min": float(np.nanmin(num)),
                    "max": float(np.nanmax(num)),
                    "mean": float(np.nanmean(num)),
                    "std": float(np.nanstd(num)),
                    "count": [len(vals)],
                }
            elif all(isinstance(v, (list, tuple, np.ndarray)) for v in vals):
                try:
                    mat = np.array([list(v) for v in vals], dtype=float)
                except Exception:
                    continue
                if mat.ndim == 2 and np.isfinite(mat).any():
                    stats[key] = {
                        "min": np.nanmin(mat, axis=0).tolist(),
                        "max": np.nanmax(mat, axis=0).tolist(),
                        "mean": np.nanmean(mat, axis=0).tolist(),
                        "std": np.nanstd(mat, axis=0).tolist(),
                        "count": [len(vals)],
                    }
        # 该集每路视频一个文件(v2.1 布局),抽帧逐通道统计
        chunk = ep_index // 1000
        for vkey, meta in (video_features or {}).items():
            if meta.get("dtype") != "video":
                continue
            vpath = (output_dir / "videos" / vkey / f"chunk-{chunk:03d}"
                     / f"episode_{ep_index:06d}.mp4")
            if not vpath.exists():
                continue
            shape = meta.get("shape") or [0, 0, 3]
            channels = int(shape[2]) if len(shape) > 2 else 3
            cap = cv2.VideoCapture(str(vpath))
            if not cap.isOpened():
                continue
            samples: list = []
            total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
            step = max(1, total // 200) if total > 200 else 1
            read_idx = 0
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                if read_idx % step == 0 and frame.ndim == 3:
                    samples.append(frame.astype(np.float32)[..., :channels] / 255.0)
                read_idx += 1
            cap.release()
            if not samples:
                continue
            stack = np.stack(samples)
            per_channel = stack.reshape(-1, stack.shape[-1])
            stats[vkey] = {
                "min": _nested(per_channel.min(axis=0)),
                "max": _nested(per_channel.max(axis=0)),
                "mean": _nested(per_channel.mean(axis=0)),
                "std": _nested(per_channel.std(axis=0)),
                "count": [len(samples)],
            }
        lines.append(json.dumps({"episode_index": ep_index, "stats": stats},
                                ensure_ascii=False))
    if lines:
        (output_dir / "meta" / "episodes_stats.jsonl").write_text(
            "\n".join(lines) + "\n", encoding="utf-8")


def _key_matches_any(candidate: str, keys: list[str]) -> bool:
    """候选相机名与任一 key 双向子串匹配(与 find_videos 同规则)。

    输入卡片 config.source_key(如 head_left_rgb)与批次相机名可能
    不完全相等,子串匹配保证连接关系能对上实际视频。
    """
    cl = candidate.lower()
    return any(k.lower() in cl or cl in k.lower() for k in keys)


def _source_path(path_value: str | None) -> Path | None:
    if not path_value:
        return None
    path = Path(path_value)
    if path.is_absolute():
        return path
    return settings.storage_root / path


def _episode_storage_index(session_dir: Path,
                           episode_id: str | None = None) -> int | None:
    if not episode_id:
        return None
    try:
        from app.project_dataset import episode_row
        row = episode_row(Path(session_dir), str(episode_id))
        return int(row["episode_index"]) if row and row.get("episode_index") is not None else None
    except (TypeError, ValueError, OSError):
        return None


def _depth_root(session_dir: Path, episode_id: str | None = None) -> Path:
    root = Path(session_dir) / "depth"
    if root.is_dir():
        return root
    namespaced = Path(session_dir) / "meta" / "depth" / str(episode_id or "")
    if episode_id and namespaced.is_dir():
        return namespaced
    return Path(session_dir) / "meta" / "depth"


def _depth_video_paths(session_dir: Path,
                       episode_id: str | None = None) -> list[tuple[str, Path]]:
    """Discover every uploaded depth video without assuming a device name.

    Depth streams are stored separately from ``camera_streams`` under
    ``depth/<source>/``. They are therefore invisible to the normal RGB
    video loop below. Keep the source name from the directory (or filename
    for a flat layout) so different depth devices use the same export path.
    """
    # Canonical LeRobot v2.1 batches keep depth video beside RGB streams.
    # These streams are included here as already-encoded inputs.  PNG depth
    # sequences are not part of the active format.
    result: list[tuple[str, Path]] = []
    seen: set[str] = set()
    try:
        from app.lerobot_v21 import is_depth_source, iter_video_streams
        episode_index = _episode_storage_index(Path(session_dir), episode_id)
        for source, path in iter_video_streams(Path(session_dir) / "videos"):
            if (is_depth_source(source) and source not in seen
                    and (episode_index is None
                         or path.stem == f"episode_{episode_index:06d}")):
                seen.add(source)
                result.append((source, path))
    except Exception:
        pass

    return result


def _probe_video(path: Path) -> tuple[int, float, int, int]:
    try:
        import cv2
        cap = cv2.VideoCapture(str(path))
        count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        cap.release()
        return count, fps, width, height
    except Exception:
        return 0, 0.0, 0, 0


def _probe_depth_video(path: Path) -> dict:
    """Probe a depth stream with ffprobe first, OpenCV as fallback.

    OpenCV builds without 12-bit HEVC support silently report zeros for
    gray12le streams.  ffprobe returns the true pixel format and dimensions;
    the container duration estimates the frame count when ``nb_frames`` is
    absent from the stream entry (common for HEVC).
    """
    import json
    import subprocess

    result = {"frames": 0, "fps": 0.0, "width": 0, "height": 0, "pix_fmt": ""}
    try:
        completed = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries",
             "stream=codec_name,pix_fmt,width,height,r_frame_rate,nb_frames:"
             "format=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=15, check=False,
        )
        payload = json.loads(completed.stdout or "{}")
        streams = payload.get("streams") or []
        if streams:
            stream = streams[0]
            result["pix_fmt"] = str(stream.get("pix_fmt") or "")
            for name in ("width", "height"):
                try:
                    result[name] = int(stream.get(name) or 0)
                except (TypeError, ValueError):
                    pass
            try:
                result["frames"] = int(stream.get("nb_frames") or 0)
            except (TypeError, ValueError):
                pass
            rate = str(stream.get("r_frame_rate") or "").split("/")
            try:
                if len(rate) == 2 and float(rate[1]) > 0:
                    result["fps"] = float(rate[0]) / float(rate[1])
            except (TypeError, ValueError, ZeroDivisionError):
                pass
        if not result["frames"] and result["fps"] > 0:
            try:
                duration = float((payload.get("format") or {}).get("duration") or 0)
            except (TypeError, ValueError):
                duration = 0.0
            if duration > 0:
                result["frames"] = int(round(duration * result["fps"]))
    except (OSError, subprocess.SubprocessError, ValueError, TypeError,
            json.JSONDecodeError):
        pass
    count, fps, width, height = _probe_video(path)  # OpenCV fallback
    result["frames"] = result["frames"] or count
    result["fps"] = result["fps"] or float(fps or 0.0)
    result["width"] = result["width"] or int(width or 0)
    result["height"] = result["height"] or int(height or 0)
    return result


def _copy_depth_video_canonical(source: Path, destination: Path) -> None:
    """Copy a depth stream and normalize HEVC's MP4 sample entry.

    Some uploaded gray12le streams use ``hev1`` while the project contract
    uses ``hvc1``.  This is a container-only remux (``-c copy``): depth codes
    are not decoded, re-quantized, or re-encoded.  Non-gray12le legacy
    visualization streams are copied unchanged.
    """
    import subprocess

    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name,pix_fmt,codec_tag_string",
             "-of", "json", str(source)],
            capture_output=True, text=True, check=True,
        )
        stream = (json.loads(probe.stdout).get("streams") or [{}])[0]
        is_metric_hevc = (stream.get("codec_name") == "hevc"
                          and stream.get("pix_fmt") == "gray12le")
        already_canonical = stream.get("codec_tag_string") == "hvc1"
    except (OSError, subprocess.SubprocessError, ValueError, TypeError,
            json.JSONDecodeError):
        is_metric_hevc = False
        already_canonical = False

    if not is_metric_hevc or already_canonical:
        shutil.copy2(source, destination)
        return

    tmp = destination.with_suffix(".remux.mp4")
    tmp.unlink(missing_ok=True)
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(source),
             "-map", "0:v:0", "-c:v", "copy", "-tag:v", "hvc1",
             "-movflags", "+faststart", str(tmp)],
            check=True, capture_output=True, text=True,
        )
        if not tmp.exists() or tmp.stat().st_size == 0:
            raise RuntimeError("empty remux output")
        tmp.replace(destination)
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        tmp.unlink(missing_ok=True)
        # Keep export usable for installations without a compatible ffmpeg;
        # metadata still records the actual gray12le stream.
        print(f"[lerobot_export] depth hvc1 remux skipped: {exc}")
        shutil.copy2(source, destination)


def _write_depth_video(dest: Path, pngs: list[Path], frame_count: int,
                       fps: float, depth_scale: float | None,
                       depth_min: float = 0.01,
                       depth_max: float = 10.0,
                       shift: float = 3.5,
                       use_log: bool = True) -> tuple[int, float, int, int]:
    """PNG 深度序列(uint16 毫米)→ canonical metric depth-code 视频。

    深度值本身存进视频(替代 JET 伪彩):每帧 uint16 → 米 → 裁剪 →
    log 量化 12bit 码 → gray12le → libx265 CQP qp=6。
    下游按 canonical 元数据反量化回 metric 深度。缺帧写全 0 码
    (无效值，配合 observation.depth.<src>.valid 列识别)。
    """
    import cv2
    import numpy as np
    import subprocess
    import shutil

    exe = shutil.which("ffmpeg")
    if not exe or not pngs:
        return 0, 0.0, 0, 0
    # 帧号 → PNG 文件名:0-based(000000.png 存在)或 1-based(000001.png)
    offset = 0 if (pngs[0].parent / "000000.png").exists() else 1
    by_stem = {p.stem: p for p in pngs}
    sample = cv2.imread(str(pngs[0]), cv2.IMREAD_UNCHANGED)
    if sample is None:
        return 0, 0.0, 0, 0
    height, width = sample.shape[:2]
    scale = float(depth_scale) if depth_scale else 0.001  # 默认毫米

    def _frame_to_codes(frame_index: int) -> np.ndarray:
        path = by_stem.get(f"{frame_index + offset:06d}")
        if path is None:
            return np.zeros((height, width), dtype=np.uint16)
        arr = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
        if arr is None:
            return np.zeros((height, width), dtype=np.uint16)
        depth_mm = arr.astype(np.float64) * scale * 1000.0
        return quantize_depth(depth_mm)

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_dest = dest.with_suffix(".tmp.mp4")
    tmp_dest.unlink(missing_ok=True)
    proc = subprocess.Popen(
        [exe, "-y", "-loglevel", "error",
         "-f", "rawvideo", "-pix_fmt", "gray12le",
         "-s", f"{width}x{height}", "-r", str(max(1.0, float(fps))),
         "-i", "pipe:0",
         "-c:v", "libx265", *depth_video_encoder_args(),
         "-movflags", "+faststart",
         str(tmp_dest)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    written = 0
    try:
        for frame_index in range(frame_count):
            codes = _frame_to_codes(frame_index)
            proc.stdin.write(codes.tobytes())  # little-endian uint16
            written += 1
        proc.stdin.close()
        stderr = proc.stderr.read()
        rc = proc.wait(timeout=1800)
    except Exception:
        proc.kill()
        tmp_dest.unlink(missing_ok=True)
        return 0, 0.0, 0, 0
    if rc != 0 or not tmp_dest.exists() or tmp_dest.stat().st_size == 0:
        tmp_dest.unlink(missing_ok=True)
        print(f"[lerobot_export] depth video encode failed: {stderr.decode()[-300:]}")
        return 0, 0.0, 0, 0
    tmp_dest.rename(dest)
    return written, max(1.0, float(fps)), height, width


# 手套压力列 → 触觉语义命名:压力/触觉数据与视觉骨骼(hand_* /
# observation.state.hand_*)完全分离。原始采集端文件仍为
# observation.left_glove/right_glove(协议不动),导出数据集统一为
# observation.tactile.left/right。
_TACTILE_COLUMN_MAP = {
    "observation.left_glove": "observation.tactile.left",
    "observation.right_glove": "observation.tactile.right",
}


def _read_sensor_rows(session_dir: Path,
                      episode_id: str | None = None,
                      skip_columns: set[str] | None = None) -> dict[int, dict]:
    """Merge independently stored glove/action parquet rows by frame_index.

    ``skip_columns``: 调用方已排除的列。这些列在读取阶段就被跳过 ——
    触觉力矩阵每帧 0.8MB,整集 14MB,在 SSHFS 上读完要十几秒;既然最终
    不会写进数据集,就不该读。仅按列名裁剪,不改变任何保留列的值。
    """
    rows: dict[int, dict] = {}
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise RuntimeError("pyarrow is required for LeRobot export") from exc

    episode_index = _episode_storage_index(Path(session_dir), episode_id)
    for data_dir in session_dir.rglob("data"):
        if not data_dir.is_dir():
            continue
        # The input root contains only the canonical project data tree, so
        # processing results cannot be mistaken for a second raw dataset.
        for path in sorted(data_dir.rglob("*.parquet")):
            if (episode_index is not None
                    and path.name != f"episode_{episode_index:06d}.parquet"):
                continue
            try:
                if skip_columns:
                    # 先读 schema(只读 footer,便宜),再只取需要的列。
                    available = [str(n) for n in pq.read_schema(path).names]
                    wanted = [n for n in available if n not in skip_columns]
                    table = pq.read_table(path, columns=wanted)
                else:
                    table = pq.read_table(path)
                names = table.schema.names
                if "frame_index" not in names:
                    continue
                for source_row in table.to_pylist():
                    fi = int(source_row.get("frame_index", 0))
                    target = rows.setdefault(fi, {})
                    # 传播帧过滤:对齐官方"只导出真实检测"语义(与
                    # _read_hand_3d_rows 的 state 过滤一致)。传播/插值
                    # 帧(检测短暂丢失时用前后帧插值延续显示)的手部
                    # 列置 None → _sanitize_row 按列语义补 NaN/False,
                    # 训练端以 *_valid 列判定该帧该手无效。
                    sides, top_slots, devices = _propagated_hand_sets(source_row)
                    for key, value in source_row.items():
                        if key == "frame_index" or key in {"timestamp", "episode_index"}:
                            continue
                        if key == "imu_ts_ns":
                            # IMU 采样时间戳(ns,变长,每帧一批)→ 与
                            # observation.imu 同构透传,导出时走变长聚合
                            target["observation.imu_ts_ns"] = value
                            continue
                        if key.startswith("observation.") or key == "action" or key.startswith("action"):
                            out_key = key if key.startswith("observation.") else "action"
                            # 手套压力列重命名为触觉语义(与骨骼完全分离);
                            # 保持 256 扁平(pa.Table.from_pylist 不支持 2 维值),
                            # 16×16 语义在 features note 里声明
                            out_key = _TACTILE_COLUMN_MAP.get(out_key, out_key)
                            if _is_propagated_hand_col(out_key, sides,
                                                       top_slots, devices):
                                target[out_key] = None
                                continue
                            target[out_key] = value
            except Exception:
                continue
        # The authoritative data directory is normally the first one.  Keep
        # scanning sibling roots only when no frame data was found.
        if rows:
            break
    return rows


# 旧 7 类手势索引 → 类名(2D mediapipe_hand 产物;3D 角度手势不经过此表)
LEGACY_GESTURE_NAMES = {
    0: "closed_fist", 1: "open_palm", 2: "pointing_up",
    3: "thumbs_down", 4: "thumbs_up", 5: "victory", 6: "ilove_you",
}

# MediaPipe 21 关键点名称(OpenPose 手部顺序,与 TachinTactileGlove 一致)
_HAND_KP_NAMES = (
    "WRIST",
    "THUMB_CMC", "THUMB_MCP", "THUMB_IP", "THUMB_TIP",
    "INDEX_FINGER_MCP", "INDEX_FINGER_PIP", "INDEX_FINGER_DIP",
    "INDEX_FINGER_TIP",
    "MIDDLE_FINGER_MCP", "MIDDLE_FINGER_PIP", "MIDDLE_FINGER_DIP",
    "MIDDLE_FINGER_TIP",
    "RING_FINGER_MCP", "RING_FINGER_PIP", "RING_FINGER_DIP",
    "RING_FINGER_TIP",
    "PINKY_MCP", "PINKY_PIP", "PINKY_DIP", "PINKY_TIP",
)


def _hand_xyz_names() -> list[str]:
    """63 维扁平手部坐标的逐元素 names:21 关键点 × xyz(官方逐元素
    命名规范,参考 sensexperience pose/state 的 names 字段)。"""
    return [f"{kp}_{axis}" for kp in _HAND_KP_NAMES for axis in ("x", "y", "z")]


def _tactile_names(size: int = 256) -> list[str]:
    """16×16 触觉阵列扁平化的逐元素 names(r<行>c<列>,行主序)。"""
    side = int(round(size ** 0.5))
    if side * side != size:
        return [f"px_{i}" for i in range(size)]
    return [f"r{r}c{c}" for r in range(side) for c in range(side)]


def _gesture_str(value) -> str:
    """手势统一为字符串标签:3D 角度判定标签原样;旧 7 类 int → 类名。"""
    if value is None or (isinstance(value, float) and value != value):  # NaN
        return ""
    if isinstance(value, str):
        return value.strip()
    try:
        iv = int(value)
    except (TypeError, ValueError):
        return ""
    if iv < 0:
        return ""
    return LEGACY_GESTURE_NAMES.get(iv, str(iv))


def _hand_entry(source_row: dict, slot: str) -> dict | None:
    """从 hand_keypoints.parquet 行提取单手记录(无检测 → None)。"""
    import numpy as np
    if source_row.get(f"{slot}_2d_present") is False:
        return None
    kp = source_row.get(f"{slot}_keypoints")
    if kp is None:
        return None
    # 历史 hand_keypoints 产物是 21x3 嵌套列表,RGB_TO_3D 的 canonical
    # hand_3d 产物则是扁平 63 列。两种布局都属于同一套 2D 关键点合同。
    try:
        raw = np.asarray(kp, dtype=np.float32)
        if raw.size != 63:
            return None
        arr = raw.reshape(21, 3)
    except (TypeError, ValueError):
        return None
    if arr.shape != (21, 3):
        return None   # 不是 21×3,视为无效
    if not np.isfinite(arr).all():
        return None
    try:
        confidence = float(source_row.get(f"{slot}_confidence") or float("nan"))
    except (TypeError, ValueError):
        confidence = float("nan")
    return {
        "keypoints": arr.reshape(21, 3).tolist(),
        "handedness": str(source_row.get(f"{slot}_handedness")
                           or source_row.get(f"{slot}_label") or ""),
        "confidence": confidence,
        # 2D 产物可能使用旧的 int 类别,也可能使用 RGB 估计模块的
        # 空字符串/文本标签,统一成导出层的字符串合同。
        "gesture": _gesture_str(source_row.get(f"{slot}_gesture")),
        "gesture_score": float(source_row.get(f"{slot}_gesture_score") or 0.0),
    }


def _read_hand_rows(hand_paths: list[str],
                    hand_3d_paths: list[str] | None = None) -> dict[int, dict]:
    """读手部骨骼产物 → {frame_index: {"hand_0": entry|None, "hand_1": entry|None}}。

    ``hand_3d_paths`` 非空时走三角化分支(米制 3D 坐标,每个槽位直接覆盖
    对应帧);否则走 2D 分支:每路读入,按槽位每帧取置信度最高的那路
    (手部最清晰的画面,左右手可来自不同相机);entry 附带 ``source``
    (相机 key,取自 parquet 文件名)供数据集标注来源列。单路行为与现状
    一致,只是额外带上 source。
    """
    if hand_3d_paths:
        return _read_hand_3d_rows(hand_3d_paths)
    if not hand_paths:
        return {}
    import pandas as pd
    per_camera: list[dict[int, dict]] = []
    for path in hand_paths:
        try:
            df = pd.read_parquet(path)
        except Exception:
            continue
        src = Path(path).stem  # 相机 key(stereo_left / stereo_right)
        rows: dict[int, dict] = {}
        for _, source_row in df.iterrows():
            fi = int(source_row.get("frame_index", 0))
            rows[fi] = {
                "hand_0": _hand_entry(source_row, "hand_0"),
                "hand_1": _hand_entry(source_row, "hand_1"),
            }
        per_camera.append((src, rows))
    merged: dict[int, dict] = {}
    for src, rows in per_camera:
        for fi, hand in rows.items():
            target = merged.setdefault(fi, {"hand_0": None, "hand_1": None})
            for slot in ("hand_0", "hand_1"):
                entry = hand.get(slot)
                if not entry:
                    continue
                cur = target.get(slot)
                if cur is None or (entry.get("confidence") or 0) >= (cur.get("confidence") or 0):
                    entry = dict(entry)
                    entry["source"] = src
                    target[slot] = entry
    return merged


def _read_hand_3d_rows(hand_3d_paths: list[str]) -> dict[int, dict]:
    """读双目三角化产物(hand_3d/*.parquet)→ 与 2D 分支同构的 entry 映射。

    每个槽位为三角化后的一只手(hand_0/1_label 记 handedness,landmarks_3d
    为 63 = 21×3 米制 3D 坐标,左目相机系);entry 带 ``is_3d`` 标记供
    features 声明区分 unit(meter vs normalized_image_coords)。
    """
    import numpy as np
    import pandas as pd  # 函数内引用,模块顶层无全局 pd(缺失会 NameError)
    merged: dict[int, dict] = {}
    for path in hand_3d_paths:
        try:
            df = pd.read_parquet(path)
        except Exception:
            continue
        src = _hand_3d_source(path)  # 物理 RGB source_key(旧产物回退文件名)
        for _, source_row in df.iterrows():
            fi = int(source_row.get("frame_index", 0))
            target = merged.setdefault(fi, {"hand_0": None, "hand_1": None})
            for slot in ("hand_0", "hand_1"):
                if not source_row.get(f"{slot}_present"):
                    continue
                # Black-glove D435 artifacts may contain bounded alpha-beta
                # predictions for preview continuity.  Training exports are
                # real-only by default; the raw hand_3d parquet keeps the
                # propagated values and their explicit state fields.
                state = source_row.get(f"{slot}_state")
                if state not in (None, "", "real"):
                    continue
                landmarks = source_row.get(f"{slot}_landmarks_3d")
                if landmarks is None:
                    continue
                kp = np.asarray(landmarks, dtype=np.float64).reshape(21, 3)
                target[slot] = {
                    "keypoints": kp.tolist(),
                    "handedness": str(source_row.get(f"{slot}_label") or ""),
                    # 新版 hand_3d 带 2D 检测置信度/角度手势列;
                    # 旧产物缺列 → 1.0 / 空 / -1(语义保持兼容)。
                    # fingers 用显式 None 判断:0 = 握拳(合法值),
                    # ``or -1`` 会把它错写成"无数据"再被垃圾列规则误删
                    "confidence": float(source_row.get(f"{slot}_confidence") or 1.0),
                    "gesture": _gesture_str(source_row.get(f"{slot}_gesture")),
                    "fingers": (int(source_row.get(f"{slot}_fingers"))
                                if source_row.get(f"{slot}_fingers") is not None
                                else -1),
                    "gesture_score": 0.0,
                    "source": src,
                    "is_3d": True,
                    "reprojection_error": float(
                        source_row.get(f"{slot}_reprojection_error") or float("nan")),
                }
    return merged


_RGB_ESTIMATED_3D_UNIT = "rgb_estimated_meters"
_METRIC_3D_UNITS = {"camera_meters", "meter", "meters"}


def _hand_3d_manifest(path: str | Path) -> dict:
    """读取 Hand3D 文件旁的 manifest,找不到时返回空字典。"""
    path = Path(path)
    candidates = [path.parent / f"{path.stem}.manifest.json"]
    candidates.extend(sorted(path.parent.glob(f"{path.stem}*.manifest.json")))
    seen: set[Path] = set()
    for manifest in candidates:
        if manifest in seen:
            continue
        seen.add(manifest)
        try:
            value = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if isinstance(value, dict):
            return value
    return {}


def _hand_3d_unit_for_path(path: str | Path) -> str | None:
    value = _hand_3d_manifest(path).get("unit")
    return str(value) if value else None


def _is_rgb_estimated_hand_3d(path: str | Path) -> bool:
    """RGB_TO_3D 的 Hand3D 仅供预览,导出时必须降级为 2D。"""
    manifest = _hand_3d_manifest(path)
    unit = _hand_3d_unit_for_path(path) or ""
    mode = str(manifest.get("mode") or "").lower()
    return (unit == _RGB_ESTIMATED_3D_UNIT
            or "rgb_estimated_3d" in mode
            or "rgb_estimated" in mode)


def _prepare_hand_export_inputs(
        hand_keypoints_paths: list[str] | None,
        hand_3d_paths: list[str] | None,
        hand_3d_right_paths: list[str] | None,
        hand_3d_unit: str | None = None) -> tuple[list[str], list[str], list[str], str | None]:
    """Separate preview-only RGB Hand3D from exportable hand artifacts.

    RGB_TO_3D writes a Hand3D parquet so the review UI can render a spatial
    skeleton. Its parquet also contains the original 2D landmarks, which are
    the only hand coordinates allowed into a dataset export. Only metric depth
    artifacts (``camera_meters``) stay on the 3D path.
    """
    metric_paths: list[str] = []
    estimated_paths: list[str] = []
    for path in hand_3d_paths or []:
        (estimated_paths if _is_rgb_estimated_hand_3d(path)
         else metric_paths).append(str(path))

    metric_right_paths: list[str] = []
    estimated_right_paths: list[str] = []
    for path in hand_3d_right_paths or []:
        (estimated_right_paths if _is_rgb_estimated_hand_3d(path)
         else metric_right_paths).append(str(path))

    # RGB estimated Hand3D is read through its hand_*_keypoints columns.
    keypoint_paths = [str(path) for path in (hand_keypoints_paths or [])]
    keypoint_paths.extend(estimated_paths)
    keypoint_paths.extend(estimated_right_paths)

    # A unit supplied by an in-memory ArtifactRef may describe an RGB path
    # that was filtered out. Never let that stale value select a 3D schema.
    supplied_unit = str(hand_3d_unit or "")
    unit = supplied_unit if supplied_unit in _METRIC_3D_UNITS else None
    unit = unit or _infer_hand_3d_unit(metric_paths)
    return keypoint_paths, metric_paths, metric_right_paths, unit


def _flat63(kps) -> list:
    """hand_*_world 统一为 63 位扁平列表(LeRobot 约定:多维列展平存储,
    features 声明 [21,3],加载器 reshape)。同时规避 pandas/pyarrow 对
    嵌套列表的序列化怪癖(读回变 (21,) object 数组,下游 asarray 报错)。
    """
    import numpy as np
    if kps is None:
        return [float("nan")] * 63
    arr = np.asarray(kps)
    if arr.dtype == object or (arr.ndim == 1 and arr.shape == (21,)):
        arr = np.asarray([np.asarray(p, dtype=np.float64) for p in kps])
    arr = arr.astype(np.float64).reshape(-1)
    if arr.shape[0] != 63:
        arr = np.full(63, np.nan)
    return arr.tolist()


def _force_matrix_semantics(root: Path) -> dict[str, dict]:
    """从源 canonical 数据集读力矩阵的语义元数据。

    力矩阵列存的不是力值本身,而是**行内差分后的定标整数**:采集端先按规格
    把 (250,250,3) 的力值放大 ``scale`` 倍(实测 100)再四舍五入成 int16,
    然后逐行做差分压体积。还原要两步 —— 先 ``cumsum`` 再 ``÷ scale``。

    这两个字段只写在源 ``meta/info.json`` 的 features 里,**parquet 本身
    不带**。导出若不透传,产物就只剩一条裸整数列表:不知道要 cumsum(拿到
    的一列差分值毫无意义),也不知道要除以 100(即使还原了也差两个数量级)。
    数据一个字节没丢,但已经没人能正确解释它了。

    ``encoding``/``scale``/``units``/``names`` 原样搬运,不做解释 —— 消费
    者按源契约解码即可。
    """
    import json

    try:
        info = json.loads((Path(root) / "meta" / "info.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    features = info.get("features")
    if not isinstance(features, dict):
        return {}
    carried_keys = ("encoding", "scale", "units", "names")
    out: dict[str, dict] = {}
    for name, spec in features.items():
        if "force_matrix" not in str(name) or not isinstance(spec, dict):
            continue
        carried = {k: spec[k] for k in carried_keys if k in spec}
        if carried:
            out[str(name)] = carried
    return out


def _passthrough_dtype(key: str, sensor_rows: dict[int, dict]) -> str:
    """透传列的声明 dtype 必须与真实值一致(官方加载器按声明 cast,
    实测 gesture 字符串列被泛型 float32 声明 → ArrowInvalid 崩溃)。
    扫描前 50 帧的真实值推断:字符串 > bool > int > float。
    """
    import numpy as np
    seen_str = seen_bool = seen_int = False
    for frame_rows in list(sensor_rows.values())[:50]:
        value = frame_rows.get(key)
        if value is None or (isinstance(value, (float, np.floating))
                             and np.isnan(value)):
            continue
        if isinstance(value, str):
            seen_str = True
        elif isinstance(value, (bool, np.bool_)):
            seen_bool = True
        elif isinstance(value, (int, np.integer)):
            seen_int = True
        elif isinstance(value, (list, tuple, np.ndarray)):
            kind = np.asarray(value).dtype.kind
            if kind in "US":
                seen_str = True
            elif kind == "b":
                seen_bool = True
            elif kind in "iu":
                seen_int = True
    if seen_str:
        return "string"
    if seen_bool:
        return "bool"
    if seen_int:
        return "int64"
    return "float32"


def _write_video_dense_keyframes(source: Path, destination: Path) -> None:
    """把视频写入数据集,必要时重编码以加密关键帧。

    采集端用 ffmpeg 默认设置编码(GOP=250),10 秒的集只有 2 个关键帧
    (0s 和 8.3s)。lerobot 按时间戳取帧时先 seek 到关键帧再向前解码,当
    查询点紧邻下一个关键帧时 seek 会落到**目标之后**,该帧永远取不到,
    训练直接抛 FrameTimestampError(实测 7 个采样点里坏 2 个)。

    GOP=30(每秒一个关键帧)可让 seek 稳定落在目标之前,实测 7/7 全通过;
    代价是文件约大 2 倍(2.7MB -> 5.7MB/集)。

    编码失败(缺 ffmpeg、编码器不可用)时退回原样复制:宁可保留稀疏关键帧
    也不要让整个导出失败 —— 取不到的帧会让那条样本报错,而不是产出错数据。
    """
    import subprocess
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", str(source),
             "-c:v", "libx264", "-g", "30", "-pix_fmt", "yuv420p",
             "-crf", "20", "-preset", "veryfast", str(destination)],
            capture_output=True, timeout=300)
        if result.returncode == 0 and destination.is_file() and destination.stat().st_size:
            return
    except (OSError, subprocess.SubprocessError):
        pass
    shutil.copy2(source, destination)


def _ensure_observation_state(rows: list[dict]) -> str | None:
    """确保每帧有 ``observation.state``;缺失时由 UMI 列派生。

    ACT 的 VAE 编码器无条件读取 ``batch[observation.state]``
    (modeling_act.py 里那句 ``device=batch[OBS_STATE].device`` 没做
    robot_state_feature 判空),缺这列直接 KeyError。即便抛开这个上游
    问题,单帧观测的 ACT 也需要 proprioception 才知道夹爪在哪。

    派生口径 ``[Δx, Δy, Δz, gripper]``:位置是本帧相对**本集第一帧**的
    偏移。SLAM 的世界原点是每次采集初始化时任意确定的,直接喂绝对坐标
    等于把无关的世界位置当特征。夹爪取开合度 0–1。

    已有 observation.state 时不动(尊重上游的显式声明)。返回派生列名或
    None。
    """
    import numpy as np

    if any("observation.state" in row for row in rows):
        return None
    if not any("observation.slam_pose" in row for row in rows):
        return None

    # 按 episode_index 分组:相对偏移必须在**各自集内**计算,跨集求差
    # 会把两集世界原点的差异当成运动。
    by_episode: dict[int, list[dict]] = {}
    for row in rows:
        try:
            episode = int(row.get("episode_index", 0))
        except (TypeError, ValueError):
            episode = 0
        by_episode.setdefault(episode, []).append(row)

    derived = 0
    for episode_rows in by_episode.values():
        origin = None
        for row in episode_rows:
            pose = row.get("observation.slam_pose")
            try:
                pose_values = np.asarray(pose, dtype=float).reshape(-1)
            except (TypeError, ValueError):
                continue
            if pose_values.size < 3 or not np.isfinite(pose_values[:3]).all():
                continue
            if origin is None:
                origin = pose_values[:3].copy()
            gripper = 0.0
            raw = row.get("observation.gripper_state")
            try:
                gripper_values = np.asarray(raw, dtype=float).reshape(-1)
                if gripper_values.size:
                    gripper = float(np.clip(gripper_values[0] / 100.0, 0.0, 1.0))
            except (TypeError, ValueError):
                pass
            row["observation.state"] = [
                float(value) for value in np.concatenate([
                    pose_values[:3] - origin, [gripper]])
            ]
            derived += 1
    if not derived:
        return None
    print(f"[lerobot_export] derived observation.state for {derived} frames")
    return "observation.state"


def _ensure_slam_pose(rows: list[dict]) -> str | None:
    """确保每帧有 ``observation.slam_pose``;缺失时由 ``slam_trajectory`` 重建。

    采集端从 52 集起不再写 ``observation.slam_pose``,只写高频样本缓冲
    ``observation.slam_trajectory``(旧批两列都有)。ACT 的输入特征里声明了
    slam_pose,整列缺失会让官方加载器与策略直接 KeyError —— 这批数据因此
    完全无法训练或评估。

    重建口径见 ``app.umi_slam_action.poses_from_trajectory``(与派生 action
    用的是同一个函数)。拿旧集(有 slam_pose)做过对照:位置相关系数
    0.998~0.9999、逐帧位置差 std 仅 1~4mm(亚帧采样差),数值范围完全一致,
    四元数 |dot| 中位 1.0000 —— **同一个物理量、同一个世界系**,不是近似。
    重建版反而补上了采集端留的 28.5% 零填充行,质量更好。

    起点偏移的口径与 ``_ensure_observation_state`` 一致,故两者可以串联:本
    函数先跑,缺 state 的集还能继续从重建出的 slam_pose 派生 state。

    已有该列时不动(旧批重导行为完全不变)。返回写入的列名或 None。
    """
    import numpy as np

    if any("observation.slam_pose" in row for row in rows):
        return None
    if not any("observation.slam_trajectory" in row for row in rows):
        return None

    from app.umi_slam_action import poses_from_trajectory

    # 按 episode_index 分组:时间基映射是每集单独拟合的(各集 SLAM 会话
    # 独立),跨集混在一起会把不同集的时间基搅乱,重建出的位姿全错。
    by_episode: dict[int, list[dict]] = {}
    for row in rows:
        try:
            episode = int(row.get("episode_index", 0))
        except (TypeError, ValueError):
            episode = 0
        by_episode.setdefault(episode, []).append(row)

    written = 0
    for episode_rows in by_episode.values():
        count = len(episode_rows)
        trajectories = [row.get("observation.slam_trajectory")
                        for row in episode_rows]
        # 时间戳必须是稠密 float 数组:缺帧补 NaN(poses_from_trajectory 会
        # 把它们标为缺失再插值),混进 None 会让 np.asarray 推断出 object。
        stamps = np.full(count, np.nan, dtype=float)
        for index, row in enumerate(episode_rows):
            try:
                stamps[index] = float(row.get("timestamp"))
            except (TypeError, ValueError):
                continue
        poses = poses_from_trajectory(trajectories, stamps, count)
        if poses is None:
            continue
        for row, pose in zip(episode_rows, poses):
            row["observation.slam_pose"] = [float(value) for value in pose]
            written += 1
    if not written:
        return None
    print(f"[lerobot_export] derived observation.slam_pose for {written} frames")
    return "observation.slam_pose"


def _pad_sparse_matrix_columns(rows: list[dict], nan_fill: float) -> dict[str, int]:
    """定长矩阵/trajectory 列的空帧补齐到声明长度。

    采集端在尚无数据的帧上写空数组(如触觉矩阵首帧、SLAM 丢帧时的
    trajectory),同一列因此长短不一。官方加载器按声明的 shape 转成定长
    Sequence,遇到空值直接 cast 失败:
        TypeError: Couldn't cast array of type list<element: int64>
                   to List(Value('int64'), length=187500)

    只补空值,不动有值帧 —— 缺失语义由列本身的值域表达。

    返回 ``{列名: 补齐后的长度}``:features 声明必须用这个长度,而不是
    首帧样本的长度 —— 首帧常常正是空的那一帧,拿它声明会得到 shape [0],
    与实际写入的长度不符,加载器照样 cast 失败。
    """
    import numpy as np
    targets: dict[str, int] = {}
    for key in {k for row in rows for k in row}:
        if not (key.endswith("_matrix") or key.endswith("_trajectory")):
            continue
        target = 0
        sample_value = None
        for row in rows:
            value = row.get(key)
            if isinstance(value, (list, tuple)) and len(value) > target:
                target = len(value)
                sample_value = value
        if not target or not sample_value:
            continue
        targets[key] = target
        # 补齐值的类型必须与该列已有元素**逐位一致**:触觉矩阵是 int16
        # (声明 int64),补浮点会让 pyarrow 把整列推断成 double,与声明的
        # dtype 冲突 → 加载器 cast 失败。int 列补 0,浮点列补 nan_fill。
        existing = next((v for v in sample_value if v is not None), None)
        if isinstance(existing, (int, np.integer)) and not isinstance(existing, bool):
            fill = 0
        else:
            fill = nan_fill
        for row in rows:
            value = row.get(key)
            if not isinstance(value, (list, tuple)) or len(value) == target:
                continue
            row[key] = list(value) + [fill] * (target - len(value))
    return targets


def _pad_variable_columns(rows: list[dict],
                          nan_fill: float = float("nan")) -> tuple[int, dict[str, int]]:
    """变长/稀疏列固定长度化。返回 ``(imu_cap, {列名: 补齐长度})``。

    - observation.imu:每样本 6 维 → 展平为 cap*6(nan_fill 补尾)
    - observation.imu_ts_ns:补 0 到 cap
    - 矩阵/trajectory 类的空帧:补齐到该列最大长度
      (见 _pad_sparse_matrix_columns)

    第二个返回值供 features 声明使用 —— 声明必须等于**实际写入的长度**,
    否则官方加载器 cast 失败。
    """
    import numpy as np
    matrix_targets = _pad_sparse_matrix_columns(rows, nan_fill)
    imu_cap = 3
    for row in rows:
        for key in ("observation.imu", "observation.imu_ts_ns"):
            value = row.get(key)
            if isinstance(value, (list, tuple)):
                imu_cap = max(imu_cap, len(value))
    if not any("observation.imu" in row for row in rows):
        return imu_cap, matrix_targets
    for row in rows:
        imu = row.get("observation.imu")
        if isinstance(imu, (list, tuple)):
            samples: list[np.ndarray] = []
            for sample in imu:
                if isinstance(sample, (list, tuple)):
                    arr = np.asarray(
                        [nan_fill if v is None else float(v) for v in sample],
                        dtype=float)
                    if arr.shape[0] < 6:
                        arr = np.pad(arr, (0, 6 - arr.shape[0]),
                                     constant_values=nan_fill)
                    samples.append(arr[:6])
                else:
                    samples.append(np.full(6, nan_fill))
            while len(samples) < imu_cap:
                samples.append(np.full(6, nan_fill))
            row["observation.imu"] = np.concatenate(
                samples, dtype=float).astype(float).tolist()
        ts = row.get("observation.imu_ts_ns")
        if isinstance(ts, (list, tuple)):
            row["observation.imu_ts_ns"] = (
                [int(v) for v in ts] + [0] * (imu_cap - len(ts)))[:imu_cap]
    return imu_cap, matrix_targets


def _propagated_hand_sets(source_row: dict) -> tuple[set[str], set[str], dict[str, set[str]]]:
    """该帧处于传播/插值状态(非 real)的手部槽位。

    返回 (sides, top_slots, devices):
    - sides:{"left","right"} 中该帧为传播态的左右手(主命名空间
      observation.state.hand_<side>_* 列据此过滤)
    - top_slots:{"0","1"} 顶层 observation.hand_<n>_* 列的传播槽位
    - devices:{设备 token → 传播槽位集合},键取自
      processing.hand_3d.<token>.hand_<n>_state,与
      observation.state.devices.<token>.hand_<n>_* 同 token
    旧会话无 state 列时全部视为 real(不过滤)。
    """
    sides: set[str] = set()
    top_slots: set[str] = set()
    devices: dict[str, set[str]] = {}
    for slot in ("0", "1"):
        state = source_row.get(f"hand_{slot}_state")
        if state not in (None, "", "real"):
            top_slots.add(slot)
            label = str(source_row.get(f"hand_{slot}_label") or "").lower()
            if label in ("left", "right"):
                sides.add(label)
    for key, value in source_row.items():
        match = re.fullmatch(r"processing\.hand_3d\.(.+)\.hand_([01])_state",
                             key)
        if match and value not in (None, "", "real"):
            devices.setdefault(match.group(1), set()).add(match.group(2))
    return sides, top_slots, devices


def _is_propagated_hand_col(key: str, sides: set[str], top_slots: set[str],
                            devices: dict[str, set[str]]) -> bool:
    """手部列是否属于传播帧槽位(命中则整列置 None,消毒层补语义默认值)。"""
    match = re.fullmatch(r"observation\.hand_([01])_.*", key)
    if match:
        return match.group(1) in top_slots
    match = re.fullmatch(r"observation\.state\.devices\.([\w.]+)\.hand_([01])_.*",
                         key)
    if match:
        return match.group(2) in devices.get(match.group(1), set())
    match = re.fullmatch(r"observation\.state\.hand_(left|right)_.*", key)
    if match:
        return match.group(1) in sides
    return False


def _sanitize_row(row: dict, nan_fill: float = float("nan")) -> None:
    """null 单元格兜底消毒(官方加载器对 null 零容忍:采样时
    ``torch.tensor(None)`` 直接崩,实测上游 hand_3d 处理列带 null)。

    缺失语义按列名填充:bool 列 False、字符串列空串、fingers -1、
    其余(数值/列表)用 ``nan_fill``(v3.0 = NaN;v2.1 = 0.0,因为官方
    v2.1→v3 转换脚本经 pandas/pyarrow 回写时会把 NaN 变 null,官方
    加载器对 null 直接崩——缺失语义由 *_valid 列承载,不受影响);
    嵌套列表内 None 同样置 nan_fill。
    """
    for key, value in list(row.items()):
        if isinstance(value, list):
            # ``None in value`` 走 C 层容器查找,比 ``any(v is None ...)``
            # 的 Python 生成器快约 2.3 倍。语义等价:``in`` 用 ``==``,
            # 而数值/字符串与 None 比较恒为 False。
            # 必须保留 isinstance 前置判断 —— numpy 数组的 ``__eq__``
            # 返回数组,``in`` 会抛 "truth value of an array is ambiguous"。
            if None in value:
                row[key] = [nan_fill if v is None else v for v in value]
        elif value is not None:
            continue
        elif key.endswith("_valid") or key == "next.done":
            row[key] = False
        elif (key.endswith(("_source", "_gesture", "_handedness"))
              or key in {"annotation", "annotation_scope"}):
            row[key] = ""
        elif key.endswith("_fingers"):
            row[key] = -1
        else:
            row[key] = nan_fill


def _junk_columns(rows: list[dict]) -> set[str]:
    """恒定哨兵垃圾列检测:数据从未真实写入的列不导出。

    - reprojection_error:全程 NaN(手部管线未产生重投影误差)
    - fingers:全程 -1(手套传感器未接)
    - gesture_score:全程 0(角度判定路径无分数)
    - gesture:全程空串(无手势标签)
    只要任一帧有真实值(非哨兵),整列保留。
    """
    import numpy as np
    if not rows:
        return set()
    junk: set[str] = set()
    for key in rows[0].keys():
        vals = [row.get(key) for row in rows]
        if key.endswith("_reprojection_error"):
            arr = np.array([v for v in vals if v is not None], dtype=float)
            if arr.size == 0 or np.isnan(arr).all():
                junk.add(key)
        elif key.endswith("_fingers"):
            if all(v == -1 for v in vals):
                junk.add(key)
        elif key.endswith("_gesture_score"):
            if all(v in (0.0, 0, None) for v in vals):
                junk.add(key)
        elif key.endswith("_gesture"):
            if all(not v for v in vals):
                junk.add(key)
        elif key.endswith("_handedness") or (
                key.endswith("_source") and "_world" in key):
            # 恒定手部元数据:槽位左右手标记/坐标来源。单相机、手不换
            # 位的批次全程恒定 → 与按 hand_left/right 拆分的列完全冗余;
            # 只要任一帧值不同(换手/多源混合)整列保留。
            distinct = {v for v in vals if v}
            if len(distinct) <= 1:
                junk.add(key)
        elif key == "action":
            # 恒定 action = 全程同一动作值(采集端占位或机器人静止):
            # 对 imitation learning 零信息,与无 action 列的官方参考
            # 行为一致;任一帧不同 → 真实动作数据,整列保留。
            normalized = [tuple(v) if isinstance(v, (list, tuple))
                          else v for v in vals]
            if len(set(normalized)) <= 1:
                junk.add(key)
        elif isinstance(vals[0], (list, tuple)) and not any(
                isinstance(x, (list, tuple)) for x in vals[0]):
            # 定长标量向量列全程恒定(如手套 hand_pose 采集端占位全零)
            # = 零信息;真实手部/触觉数据在动/有接触,不可能全程恒定。
            # imu 等嵌套列表(每帧样本批)跳过此规则。
            normalized = [tuple(v) if isinstance(v, (list, tuple)) else v
                          for v in vals]
            if len(set(normalized)) <= 1:
                junk.add(key)
    return junk


def _cast_float32(rows: list[dict]) -> None:
    """浮点列统一 cast 成 float32。

    pyarrow 对 Python float 推断 double;features 声明按官方参考用
    float32(pose/timestamp 都是 float32),不 cast 会造成声明与实际
    schema 不一致(lerobot-doctor Feature Consistency 的隐患)。
    使用 np.float32 标量(而非 Python float),pyarrow 才会保留 float32。
    """
    import numpy as np
    for row in rows:
        for key, value in list(row.items()):
            if value is None or isinstance(value, (str, bool)):
                continue
            if isinstance(value, (list, tuple)):
                if value and all(isinstance(x, (list, tuple)) for x in value):
                    # 嵌套列表(imu 每帧样本批):内层 6 维 cast float32
                    row[key] = [
                        [np.float32(v) if isinstance(v, (float, np.floating))
                         else v for v in sample]
                        for sample in value
                    ]
                elif value and all(isinstance(x, (float, np.floating))
                                   and not isinstance(x, bool)
                                   for x in value):
                    # 纯浮点列表(触觉/手部坐标等)cast float32;
                    # int 列表(imu_ts_ns)保持 int64
                    row[key] = [np.float32(x) for x in value]
            elif isinstance(value, (float, np.floating)) and not isinstance(value, bool):
                row[key] = np.float32(value)


def _hand_columns(hand: dict, suffix: str = "", coordinate_key: str = "world",
                  device_namespace: str | None = None) -> dict:
    """手部骨骼 → LeRobot observation 列(缺检测 NaN 填充,shape 固定)。

    左右世界坐标按 handedness 分配(hand_0 槽位未必是左手);handedness
    未知时按槽位兜底(hand_0 → left, hand_1 → right)。

    ``coordinate_key`` separates calibrated camera coordinates (``world``)
    from MediaPipe relative 3D (``3d``) and image-normalized 2D (``2d``).
    The old reader remains compatible with historical files; only newly
    exported datasets use the semantically correct field name.

    ``suffix``: 列名后缀,右目(辅助视角)数据用 "_rcam",与主列并存
    (主数据 = 左目规范视图)。
    ``device_namespace``: 多设备数据写入独立的 LeRobot/HDF5 命名空间:
    ``observation.state.devices.<source_key>.*``。
    """
    import numpy as np
    coordinate_key = coordinate_key if coordinate_key in {"world", "3d", "2d"} else "world"
    safe_namespace = (re.sub(r"[^A-Za-z0-9_.-]+", "_", str(device_namespace)).strip("._")
                      if device_namespace else "")
    root = (f"observation.state.devices.{safe_namespace}"
            if safe_namespace else "")
    scalar_root = root or "observation"
    state_root = root or "observation.state"
    cols: dict = {}
    for slot in ("0", "1"):
        entry = hand.get(f"hand_{slot}")
        cols[f"{scalar_root}.hand_{slot}_gesture{suffix}"] = _gesture_str(entry["gesture"]) if entry else ""
        cols[f"{scalar_root}.hand_{slot}_fingers{suffix}"] = int(entry.get("fingers")) \
            if entry and entry.get("fingers") is not None else -1
        cols[f"{scalar_root}.hand_{slot}_gesture_score{suffix}"] = float(entry["gesture_score"]) if entry else 0.0
        cols[f"{scalar_root}.hand_{slot}_confidence{suffix}"] = float(entry["confidence"]) if entry else float("nan")
        cols[f"{scalar_root}.hand_{slot}_handedness{suffix}"] = entry["handedness"] if entry else ""
    left = right = None
    left_src = right_src = None
    left_err = right_err = float("nan")
    for slot in ("hand_0", "hand_1"):
        entry = hand.get(slot)
        if not entry or entry["keypoints"] is None:
            continue
        h = entry["handedness"].lower()
        err = float(entry.get("reprojection_error")
                    if entry.get("reprojection_error") is not None
                    else float("nan"))
        if h == "left":
            left, left_src = entry["keypoints"], entry.get("source")
            left_err = err
        elif h == "right":
            right, right_src = entry["keypoints"], entry.get("source")
            right_err = err
        elif left is None:
            left, left_src = entry["keypoints"], entry.get("source")
            left_err = err
        elif right is None:
            right, right_src = entry["keypoints"], entry.get("source")
            right_err = err
    prefix = f"{state_root}.hand_left_{coordinate_key}{suffix}"
    cols[prefix] = _flat63(left)
    prefix = f"{state_root}.hand_right_{coordinate_key}{suffix}"
    cols[prefix] = _flat63(right)
    cols[f"{state_root}.hand_left_{coordinate_key}{suffix}_valid"] = left is not None
    cols[f"{state_root}.hand_right_{coordinate_key}{suffix}_valid"] = right is not None
    # 来源相机标注(多路识别时该帧关键点来自哪个相机;单路为空串)
    cols[f"{state_root}.hand_left_{coordinate_key}{suffix}_source"] = str(left_src or "")
    cols[f"{state_root}.hand_right_{coordinate_key}{suffix}_source"] = str(right_src or "")
    # 3D 关键点重投影误差(像素):质量评估/训练时按误差过滤低质量帧
    cols[f"{state_root}.hand_left_{coordinate_key}{suffix}_reprojection_error"] = left_err
    cols[f"{state_root}.hand_right_{coordinate_key}{suffix}_reprojection_error"] = right_err
    return cols


def _hand_3d_source(path: str | Path) -> str:
    """Resolve the physical RGB source from a Hand 3D manifest."""
    path = Path(path)
    for manifest in sorted(path.parent.glob(f"{path.stem}*.manifest.json")):
        try:
            doc = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        source = doc.get("source_key") or doc.get("rgb_source") \
            or doc.get("left_source")
        if source:
            return str(source)
    return path.stem


def _read_hand_3d_rows_by_source(paths: list[str] | None) -> dict[str, dict[int, dict]]:
    """Read independent per-device Hand 3D parquets without overwriting them."""
    result: dict[str, dict[int, dict]] = {}
    for raw in paths or []:
        source = _hand_3d_source(raw)
        result[source] = _read_hand_3d_rows([str(raw)])
    return result


def _device_hand_features(source: str, coordinate_key: str,
                          unit: str, note: str) -> dict[str, dict]:
    """Feature declarations matching ``device_namespace`` row columns."""
    safe_source = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(source)).strip("._") or "device"
    root = f"observation.state.devices.{safe_source}"
    features = {}
    for slot in ("0", "1"):
        features.update({
            f"{root}.hand_{slot}_gesture": {"dtype": "string", "shape": [1]},
            f"{root}.hand_{slot}_fingers": {"dtype": "int64", "shape": [1]},
            f"{root}.hand_{slot}_gesture_score": {"dtype": "float32", "shape": [1]},
            f"{root}.hand_{slot}_confidence": {"dtype": "float32", "shape": [1]},
            f"{root}.hand_{slot}_handedness": {"dtype": "string", "shape": [1]},
        })
    for side in ("left", "right"):
        key = f"{root}.hand_{side}_{coordinate_key}"
        features.update({
            key: {"dtype": "float32", "shape": [63],
                  "names": _hand_xyz_names(),
                  "unit": unit, "note": note},
            f"{key}_valid": {"dtype": "bool", "shape": [1]},
            f"{key}_source": {"dtype": "string", "shape": [1]},
            f"{key}_reprojection_error": {"dtype": "float32", "shape": [1]},
        })
    return features


def _infer_hand_3d_unit(paths: list[str] | None) -> str | None:
    """Read the coordinate unit from a hand_3d manifest for re-export.

    Re-export endpoints may not carry the original ArtifactRef metadata, so
    relying only on the in-memory metadata silently mislabeled depth output
    as MediaPipe-relative 3D.
    """
    for raw_path in paths or []:
        path = Path(raw_path)
        for manifest in sorted(path.parent.glob("*.manifest.json")):
            try:
                value = json.loads(manifest.read_text(encoding="utf-8")).get("unit")
            except Exception:
                continue
            if value:
                return str(value)
    return None


def _depth_assets(session_dir: Path, frame_count: int,
                  episode_id: str | None = None) -> list[dict]:
    depth_root = _depth_root(session_dir, episode_id)
    if not depth_root.is_dir():
        return []

    assets = []
    for source_dir in sorted(depth_root.iterdir()):
        if not source_dir.is_dir():
            continue
        files = sorted(p for p in source_dir.rglob("*.png") if p.stem.isdigit())
        if not files:
            continue
        indices = sorted({int(p.stem) for p in files})

        depth_height = depth_width = 0
        try:
            import cv2
            import numpy as np
            sample = cv2.imdecode(
                np.fromfile(str(files[0]), dtype=np.uint8),
                cv2.IMREAD_UNCHANGED,
            )
            if sample is not None:
                depth_height, depth_width = sample.shape[:2]
        except Exception:
            pass

        calibration: dict = {}
        calibration_dir = session_dir / "calibration"
        namespaced_calibration = (session_dir / "meta" / "calibration"
                                  / str(episode_id or ""))
        if episode_id and namespaced_calibration.is_dir():
            calibration_dir = namespaced_calibration
        if not calibration_dir.is_dir():
            calibration_dir = session_dir / "meta" / "calibration"
        if calibration_dir.is_dir():
            for cal_path in sorted(calibration_dir.glob("*.json")):
                try:
                    value = json.loads(cal_path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if isinstance(value, dict):
                    calibration.update({
                        key: value[key] for key in
                        ("depth_scale", "depth_min", "depth_max") if key in value
                    })

        assets.append({
            "source_key": source_dir.name,
            "dtype": "uint16",
            "unit": "meter" if calibration.get("depth_scale") else "raw",
            "depth_scale": calibration.get("depth_scale"),
            "frame_count": frame_count,
            "available_frame_count": len(indices),
            "missing_frames": [i for i in range(frame_count) if i not in set(indices)],
            "height": depth_height,
            "width": depth_width,
            **calibration,
        })
    return assets


def _device_metadata(session_dir: Path) -> list[dict]:
    """Return physical-device declarations for export-side pairing metadata."""
    for name in ("metadata.json", "meta/info.json"):
        path = Path(session_dir) / name
        if not path.is_file():
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        devices = doc.get("devices") or []
        if isinstance(devices, dict):
            devices = [dict(value or {}, key=key)
                       for key, value in devices.items()]
        if isinstance(devices, list):
            return [value for value in devices if isinstance(value, dict)]
    return []


def _annotation_maps(episode_id: str) -> tuple[dict[int, tuple[str | None, int, list[str]]], list[dict], int]:
    frame_map: dict[int, tuple[str | None, int, list[str]]] = {}
    definitions = []
    skipped_candidates = 0
    for index, segment in enumerate(list_annotations(episode_id)):
        # AI 候选段(未确认)不进入数据集 —— 只有人工确认后才导出
        if str(segment.get("status") or "confirmed") in {
                "candidate", "pending_retry"}:
            skipped_candidates += 1
            continue
        start = int(segment.get("start_frame_index", 0))
        end = int(segment.get("end_frame_index", start))
        scope = segment.get("source_scope") or ["episode"]
        if isinstance(scope, str):
            scope = [scope]
        definition = {
            "id": segment.get("id"),
            "label": segment.get("label", ""),
            "start_frame": start,
            "end_frame": end,
            "source_scope": scope,
            "notes": segment.get("notes"),
            "keyframes": segment.get("keyframes") or [],
        }
        definitions.append(definition)
        for frame in range(start, end + 1):
            frame_map.setdefault(frame, (segment.get("label"), index, scope))
    return frame_map, definitions, skipped_candidates


def _embed_info_metadata(output_dir: Path, info_json: dict) -> None:
    """把序列化 info 写入 parquet 的 schema metadata(huggingface 键,
    官方 pusht v3 同款:data/ 与 meta/episodes/ 下全部 parquet)。

    官方内嵌内容 = {"info": {"features": ...}}(仅 features),且必须
    是 HF datasets Features 可解析的形态(每项带 _type: Value/Sequence
    等)——直接内嵌 info.json 的 lerobot features 会让官方加载器在
    load_episodes 阶段 Features.from_dict 崩溃(实测 CastError)。部分
    查看器/加载器读内嵌 info 而非 meta/info.json。单文件失败不阻塞
    导出。
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    def hf_feature(feature: dict) -> dict:
        """lerobot feature 声明 → HF Features 序列化形态。

        标量(shape [1])→ Value;一维向量 → Sequence(Value);二维 →
        嵌套 Sequence。与 datasets.Features.from_dict 的解析规则对齐。
        """
        dtype = str(feature.get("dtype") or "float32")
        shape = tuple(int(dim) for dim in (feature.get("shape") or [1]))
        node = {"dtype": dtype, "_type": "Value"}
        for dim in reversed(shape[1:]):
            node = {"feature": node, "length": dim, "_type": "Sequence"}
        if len(shape) >= 1 and shape[0] != 1:
            node = {"feature": node, "length": shape[0], "_type": "Sequence"}
        return node

    def arrow_fallback(arrow_type) -> dict:
        """parquet 里有列但 features 未声明的兜底(官方规则:多列=加载失败)。

        形态对齐官方 pusht episodes 内嵌写法:list → 一层 Sequence
        (官方 stats/* 嵌套 list 也只写一层;与字段不匹配时 datasets
        会回退按 arrow 类型生成,不影响加载)。
        """
        if pa.types.is_boolean(arrow_type):
            return {"dtype": "bool", "_type": "Value"}
        if pa.types.is_string(arrow_type):
            return {"dtype": "string", "_type": "Value"}
        if pa.types.is_integer(arrow_type):
            return {"dtype": "int64", "_type": "Value"}
        if pa.types.is_floating(arrow_type):
            return {"dtype": "float64", "_type": "Value"}
        if pa.types.is_list(arrow_type):
            inner = {"dtype": "string", "_type": "Value"}
            return {"feature": inner, "_type": "Sequence"}
        return {"dtype": "float32", "_type": "Value"}

    features = info_json.get("features") or {}
    for pattern in ("data/**/*.parquet", "meta/episodes/**/*.parquet"):
        for path in output_dir.glob(pattern):
            try:
                table = pq.read_table(path)
                columns = set(table.column_names)
                embedded: dict[str, dict] = {}
                for key, feature in features.items():
                    if key not in columns:
                        # video 等流不在 parquet 里,官方内嵌同样不含
                        continue
                    embedded[key] = hf_feature(feature)
                for name in columns - features.keys():
                    embedded[name] = arrow_fallback(
                        table.schema.field(name).type)
                payload = json.dumps({"info": {"features": embedded}},
                                     ensure_ascii=False)
                table = table.replace_schema_metadata({"huggingface": payload})
                pq.write_table(table, path)
            except Exception as exc:
                print(f"[lerobot_export] embed info metadata failed "
                      f"for {path}: {exc}")


def _canonicalize_video_features(features: dict[str, dict]) -> None:
    """Mirror legacy ``video_info`` into the official ``feature.info`` block.

    The reference v3 reader still detects the legacy depth marker, but its
    depth decoder reads quantization parameters from ``features[*].info``.
    Keeping only ``video_info`` therefore silently falls back to the official
    0.01–10 m defaults instead of this project's 100–5000 mm log domain.
    Preserve ``video_info`` for the application while writing the canonical
    fields used by current LeRobot readers.
    """
    for feature in features.values():
        if not isinstance(feature, dict) or feature.get("dtype") != "video":
            continue
        legacy = feature.get("video_info")
        if not isinstance(legacy, dict):
            continue
        info = dict(feature.get("info") or {})
        for key, value in legacy.items():
            if key.startswith("video.") or key == "has_audio":
                info.setdefault(key, value)
        is_depth = bool(
            legacy.get("video.is_depth_map") or legacy.get("is_depth_map")
        )
        if is_depth:
            # Official DepthEncoderConfig uses metres; the project codec uses
            # the mathematically equivalent log domain in millimetres.
            depth_info = {
                "is_depth_map": True,
                "video.is_depth_map": True,
                "video.codec": "hevc",
                "video.pix_fmt": "gray12le",
                "video.channels": 1,
                "video.depth_min": DEPTH_MIN_MM / 1000.0,
                "video.depth_max": DEPTH_MAX_MM / 1000.0,
                "video.shift": 0.0,
                "video.use_log": True,
                "depth_unit": "mm",
            }
            info.update(depth_info)
            # v2.1 keeps this application-side block.  Keep it truthful too:
            # otherwise older readers can interpret the single-plane stream
            # as a 3-channel image before they inspect the canonical v3 info.
            feature["video_info"] = {
                **legacy,
                "video.channels": 1,
                "video.is_depth_map": True,
                "video.is_depth_visualization": False,
                "video.codec": "hevc",
                "video.pix_fmt": "gray12le",
                "video.depth_min": DEPTH_MIN_MM / 1000.0,
                "video.depth_max": DEPTH_MAX_MM / 1000.0,
                "video.shift": 0.0,
                "video.use_log": True,
                "depth_unit": "mm",
            }
            shape = list(feature.get("shape") or [])
            if len(shape) == 3 and shape[2] in (0, 3):
                shape[2] = 1
                feature["shape"] = shape
        else:
            info.setdefault("is_depth_map", False)
        feature["info"] = info


def build_lerobot_dataset(dataset_name: str, episode_ids: list[str], output_dir: Path,
                          split_ratio: float = 0.9,
                          include_video_keys: list[str] | None = None,
                          hand_keypoints_paths: list[str] | None = None,
                          hand_3d_paths: list[str] | None = None,
                          hand_3d_right_paths: list[str] | None = None,
                          version: str = "v3.0",
                          hand_3d_unit: str | None = None,
                          exclude_columns: list[str] | None = None,
                          progress_callback=None) -> Path:
    """Build a LeRobot dataset (``version`` = "v2.1" | "v3.0").

    ``exclude_columns``: 可选,精确列名列表,从数据集中整体剔除。
    用于丢掉训练用不到但体积/计算代价极高的列(如 250×250×3 的触觉
    力矩阵:每帧 0.8MB,且逐元素分位数统计是导出耗时的主要来源)。
    被剔除的列不会出现在 parquet、features 声明和 stats 里。

    Depth sources are exported as their preview video (``is_depth_map``)
    plus the per-frame ``observation.depth.<source>.valid`` marker; the
    original 16-bit PNG sequence stays in the session directory and is not
    copied into the dataset.

    ``include_video_keys``: 可选,只导出这些 source_key 对应的视频
    (工作流中"输入卡片 → 导出节点"的连接决定导出范围)。None = 全量
    导出(导出页面/API 行为不变);空列表 = 不导出任何视频。

    ``hand_keypoints_paths``: 可选,手部骨骼识别产物(hand_keypoints.parquet)
    路径(工作流中处理节点连到导出节点时传入)。写入
    observation.state.hand_left/right_2d;缺检测帧 NaN 填充,保持固定 shape。

    ``hand_3d_paths``: 可选,Hand Skeleton 模块输出的 hand_3d/*.parquet。
    深度抬升产物写入 observation.state.hand_*_world。RGB_TO_3D 产物
    仅用于前端空间预览,导出时自动读取其中的 2D 关键点并写入
    observation.state.hand_*_2d,不会写入任何估计 3D 坐标。

    ``hand_3d_right_paths``: 可选,右目(辅助视角)骨骼产物
    (hand_3d_right/*.parquet)。真实深度产物传入时以 ``_rcam`` 后缀列组
    写入;RGB_TO_3D 的右视图则降级为 2D 关键点,不写入估计 3D。

    ``version``: 数据集布局版本。v2.1 = 每 episode 一个 parquet/mp4 +
    官方 legacy JSONL episode 索引;v3.0 = 合并分片(默认)。
    两版本结构互不兼容,与官方 convert_dataset_v21_to_v30 脚本一致。
    """
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise RuntimeError("pyarrow is required for LeRobot export") from exc

    episodes = [get_episode(str(episode_id)) for episode_id in episode_ids]
    episodes = [episode for episode in episodes if episode]
    if not episodes:
        raise RuntimeError("No valid episodes selected for export")

    output_dir.mkdir(parents=True, exist_ok=True)
    is_v2 = str(version or "v3.0").lower().startswith("v2")
    chunks_size = 1000  # 官方 v2.1 每 chunk 的 episode 数(episode_chunk = index // 1000)
    rows: list[dict] = []
    episode_rows: list[dict] = []
    # v2.1:每集一个 parquet,记录该集 rows 的 [start, end) 范围用于切片
    episode_row_ranges: list[tuple[int, int, int, int, int]] = []
    total_videos = 0
    task_by_description: dict[str, int] = {}
    task_descriptions: dict[int, str] = {}
    episode_video_meta_records: list[dict[str, tuple[int, float]]] = []
    # Device metadata is used only to derive the dataset robot_type.  It is
    # intentionally not exported as a standalone meta/devices.json file.
    device_metadata_by_episode: dict[str, list[dict]] = {}
    # 力矩阵的 scale/encoding 等只存在于源 info.json,按 key 收集后写进
    # 导出产物的 features 声明(见 _force_matrix_semantics)。源根 → 元数据
    # 加一层缓存:同项目几十集共用一份 info.json,不必每集重读。
    force_matrix_meta: dict[str, dict] = {}
    _force_meta_cache: dict[str, dict[str, dict]] = {}
    depth_source_keys: set[str] = set()
    video_features: dict[str, dict] = {}
    # 手部骨骼产物(工作流中处理节点连到导出节点时传入)—— 按 frame 对齐。
    # RGB_TO_3D 的 hand_3d 文件只为前端空间预览而存在;导出层把它拆回
    # hand_*_keypoints(2D)。只有真实深度 hand_3d 才保留在 3D 分支。
    (hand_keypoints_paths, hand_3d_paths, hand_3d_right_paths,
     hand_3d_unit) = _prepare_hand_export_inputs(
         hand_keypoints_paths, hand_3d_paths, hand_3d_right_paths,
         hand_3d_unit)
    hand_rows = _read_hand_rows(hand_keypoints_paths, hand_3d_paths)
    # 新版 Hand 3D 是每个物理设备一份 parquet。保留上面的主字段兼容
    # 读取，同时把每个设备写入 observation.state.devices.<source_key>。
    device_hand_rows = _read_hand_3d_rows_by_source(hand_3d_paths)
    # 右目(辅助视角)手部骨骼:单独列组(_rcam 后缀),与主数据并存;
    # 未连接/无产物 → None,不写任何 _rcam 列(旧工作流行为不变)。
    hand_rows_rcam = (_read_hand_3d_rows(hand_3d_right_paths)
                      if hand_3d_right_paths else None)
    # Re-export endpoints may not carry ArtifactRef metadata. Read the
    # manifest so depth output keeps its camera/world semantics on rebuild.
    hand_3d_unit = hand_3d_unit or _infer_hand_3d_unit(hand_3d_paths)
    coordinate_key = (
        "world" if hand_3d_unit in {"camera_meters", "meter", "meters"} else
        "3d" if hand_3d_paths else "2d"
    )

    for out_episode_index, episode in enumerate(episodes):
        episode_id = str(episode["id"])
        session_dir = Path(episode["path"])
        device_metadata_by_episode[episode_id] = _device_metadata(session_dir)
        # 力矩阵元数据随源数据集而来。跨项目导出可能混到不同录制规格的集
        # (scale 10/100/1000 互不兼容),一个 features 声明只能写一个值 ——
        # 以先出现的为准并出声,不要静默按第一份解读整批数据。
        root_key = str(session_dir)
        if root_key not in _force_meta_cache:
            _force_meta_cache[root_key] = _force_matrix_semantics(session_dir)
        for fm_key, fm_spec in _force_meta_cache[root_key].items():
            previous = force_matrix_meta.get(fm_key)
            if previous is None:
                force_matrix_meta[fm_key] = fm_spec
            elif previous != fm_spec:
                print(f"[Export] 力矩阵规格不一致 {fm_key}: episode {episode_id} "
                      f"为 {fm_spec},此前为 {previous};沿用前者")
        streams = episode.get("camera_streams") or {}
        # 本集实际导出的每路视频(帧数, fps) → episodes parquet 的
        # videos/<key>/from_timestamp 映射(官方 v3 加载器按时间戳定位帧)
        episode_video_meta: dict[str, tuple[int, float]] = {}
        sensor_rows = _read_sensor_rows(
            session_dir, episode_id,
            skip_columns=set(str(name) for name in exclude_columns or []))
        annotation_map, annotation_defs, _skipped_candidates = _annotation_maps(episode_id)

        description = str(episode.get("project") or "").strip()
        if description not in task_by_description:
            task_id = len(task_by_description)
            task_by_description[description] = task_id
            task_descriptions[task_id] = description
        task_id = task_by_description[description]

        # 切片标注文本也进任务注册表:每段一个 task(全局按文本去重),
        # 帧级 task_index 指向该帧所属切片任务 → task-conditioned 训练
        # 可按子任务切片取指令;未标注帧回退 episode 级任务。
        for seg in annotation_defs:
            label = (seg.get("label") or "").strip()
            if label and label not in task_by_description:
                seg_task_id = len(task_by_description)
                task_by_description[label] = seg_task_id
                task_descriptions[seg_task_id] = label

        fps = float(episode.get("fps") or 30)
        frame_count = int(episode.get("frame_count") or 0)
        for source_key, stream in sorted(streams.items()):
            # 工作流连接驱动:只导出连接到导出节点的视频源(双向子串匹配,
            # 与 find_videos 一致);未连接的视频默认不导出
            if include_video_keys is not None and not _key_matches_any(source_key, include_video_keys):
                continue
            source = _source_path(stream.get("path"))
            if source is None or not source.exists():
                continue
            if is_v2:
                # v2.1 官方布局:videos/{video_key}/chunk-{episode_chunk:03d}/
                episode_chunk = out_episode_index // chunks_size
                destination = (output_dir / "videos"
                               / f"observation.images.{source_key}"
                               / f"chunk-{episode_chunk:03d}")
                output_video = destination / f"episode_{out_episode_index:06d}.mp4"
            else:
                destination = (output_dir / "videos" / "chunk-000"
                               / f"observation.images.{source_key}")
                output_video = destination / f"file-{out_episode_index:03d}.mp4"
            destination.mkdir(parents=True, exist_ok=True)
            _write_video_dense_keyframes(source, output_video)
            total_videos += 1
            probed_count, probed_fps, width, height = _probe_video(source)
            episode_video_meta[f"observation.images.{source_key}"] = (
                probed_count, probed_fps or fps)
            frame_count = max(frame_count, probed_count)
            fps = probed_fps or fps
            video_features[f"observation.images.{source_key}"] = {
                "dtype": "video",
                "shape": [height or 0, width or 0, 3],
                "names": ["height", "width", "channel"],
                "video_info": {
                    "video.fps": fps,
                    "video.is_depth_map": False,
                    "has_audio": False,
                    "source_episode": episode_id,
                },
            }

                # 深度双路导出:
        # 1) {source}         = JET 伪彩 h264(浏览器/网页 viewer 可播,
        #                        给人看;HEVC 12bit 浏览器解码不了)
        # 2) {source}_metric  = 官方同款 metric 深度视频(gray12le /
        #                        HEVC 12bit 无损,is_depth_map: true,
        #                        官方加载器按量化参数反量化回米/毫米)
        # 无 PNG 真值的老批次只有 JET → 主键标 is_depth_map: true 兜底。
        depth_assets = _depth_assets(session_dir, frame_count, episode_id)
        depth_source_keys.update(asset["source_key"] for asset in depth_assets)
        png_depth_sources = {
            asset["source_key"] for asset in depth_assets
            if any((session_dir / "depth" / asset["source_key"]).rglob("*.png"))
            or any(_depth_root(session_dir, episode_id)
                   .joinpath(asset["source_key"]).rglob("*.png"))
        }
        generated_depth: set[str] = set()
        for asset in depth_assets:
            depth_source = asset["source_key"]
            if depth_source not in png_depth_sources:
                continue
            metric_key = f"observation.images.{depth_source}_metric"
            if is_v2:
                episode_chunk = out_episode_index // chunks_size
                destination = (output_dir / "videos" / metric_key
                               / f"chunk-{episode_chunk:03d}")
                output_video = destination / f"episode_{out_episode_index:06d}.mp4"
            else:
                destination = (output_dir / "videos" / "chunk-000" / metric_key)
                output_video = destination / f"file-{out_episode_index:03d}.mp4"
            destination.mkdir(parents=True, exist_ok=True)
            png_dir = _depth_root(session_dir, episode_id) / depth_source
            pngs = sorted(p for p in png_dir.rglob("*.png") if p.stem.isdigit())
            d_count, d_fps, d_height, d_width = _write_depth_video(
                output_video, pngs, frame_count, fps, asset.get("depth_scale"))
            if d_count <= 0:
                # 编码失败(缺 ffmpeg/libx265 等):本集 JET 主键将在下方
                # mp4 循环以 is_depth_map: true 兜底,绝不静默丢深度
                continue
            generated_depth.add(depth_source)
            total_videos += 1
            episode_video_meta[metric_key] = (d_count, d_fps)
            video_features[metric_key] = {
                "dtype": "video",
                "shape": [d_height or 0, d_width or 0, 1],
                "names": ["height", "width", "channel"],
                "video_info": {
                    "video.fps": d_fps,
                    "video.height": d_height,
                    "video.width": d_width,
                    "video.channels": 1,
                    "video.codec": "hevc",
                    "video.pix_fmt": "gray12le",
                    "video.is_depth_map": True,
                    "video.depth_min_mm": DEPTH_MIN_MM,
                    "video.depth_max_mm": DEPTH_MAX_MM,
                    "video.depth_qmax": DEPTH_QMAX,
                    "video.depth_qp": DEPTH_QP,
                    "video.depth_quantization": "log",
                    "video.depth_encoding": DEPTH_VIDEO_ENCODING,
                    "video.depth_scale": asset.get("depth_scale") or 0.001,
                    "has_audio": False,
                    "source_episode": episode_id,
                    "depth_source": depth_source,
                },
            }

        # 深度双路导出:
        # 1) {source}         = JET 伪彩 h264(浏览器/网页 viewer 可播,
        #                        给人看;HEVC 12bit 浏览器解码不了)
        # 2) {source}_metric  = 官方同款 metric 深度视频(gray12le /
        #                        HEVC 12bit 无损,is_depth_map: true,
        #                        官方加载器按量化参数反量化回米/毫米)
        # 无 PNG 真值的老批次只有 JET → 主键标 is_depth_map: true 兜底。
        for depth_source, depth_path in _depth_video_paths(session_dir, episode_id):
            if is_v2:
                episode_chunk = out_episode_index // chunks_size
                destination = (output_dir / "videos"
                               / f"observation.images.{depth_source}"
                               / f"chunk-{episode_chunk:03d}")
                output_video = destination / f"episode_{out_episode_index:06d}.mp4"
            else:
                destination = (output_dir / "videos" / "chunk-000"
                               / f"observation.images.{depth_source}")
                output_video = destination / f"file-{out_episode_index:03d}.mp4"
            destination.mkdir(parents=True, exist_ok=True)
            _copy_depth_video_canonical(depth_path, output_video)
            total_videos += 1
            depth_probe = _probe_depth_video(depth_path)
            _depth_count = int(depth_probe.get("frames") or 0)
            depth_fps = float(depth_probe.get("fps") or fps)
            depth_width = int(depth_probe.get("width") or 0)
            depth_height = int(depth_probe.get("height") or 0)
            # The canonical stream is a single-plane gray12le.  Declare the
            # channels from the actual pixel format instead of the RGB-loop
            # default of 3 so the intermediate feature is truthful before
            # _canonicalize_video_features runs downstream.
            depth_channels = (
                1 if depth_probe.get("pix_fmt") in {"gray12le", "gray16le", "gray10le"}
                else 3
            )
            episode_video_meta[f"observation.images.{depth_source}"] = (
                _depth_count, depth_fps or fps)
            has_metric = depth_source in generated_depth
            video_features[f"observation.images.{depth_source}"] = {
                "dtype": "video",
                "shape": [depth_height or 0, depth_width or 0, depth_channels],
                "names": ["height", "width", "channel"],
                "video_info": {
                    "video.fps": depth_fps or fps,
                    "video.height": depth_height or 0,
                    "video.width": depth_width or 0,
                    "video.channels": depth_channels,
                    # 有 metric 流时主键是纯可视化;无 metric(老批次/
                    # 编码失败)才把主键当深度流兜底
                    "video.is_depth_map": not has_metric,
                    "video.is_depth_visualization": has_metric,
                    "has_audio": False,
                    "source_episode": episode_id,
                    "depth_source": depth_source,
                    **({"note": "JET 伪彩可视化(h264,浏览器可播);"
                                f"metric 真值见 {depth_source}_metric"}
                       if has_metric else {}),
                },
            }

        if frame_count <= 0:
            frame_count = max(sensor_rows.keys(), default=-1) + 1

        # 触觉数据只保留 observation.tactile.left/right 的 256 维原始
        # 压力数组。触觉 VIRIDIS 预览视频不是 LeRobot 必需项，训练包不
        # 再生成 observation.images.tactile_left/right，避免把显示层数据
        # 混入最小训练数据集。

        for frame_index in range(frame_count):
            anno, anno_index, scope = annotation_map.get(frame_index, (None, -1, ["episode"]))
            # 帧级 task_index 优先指向该帧所属切片任务(标注文本在任务
            # 注册表里的 id);无标注帧回退 episode 级任务。
            frame_task = task_by_description.get(anno) if anno else None
            row = {
                "episode_index": out_episode_index,
                "frame_index": frame_index,
                "timestamp": frame_index / fps,
                "index": len(rows),
                "task_index": frame_task if frame_task is not None else task_id,
                "annotation": anno or "",
                "annotation_index": anno_index,
                "annotation_scope": json.dumps(scope, ensure_ascii=False),
                # 不再写 language_persistent:官方 v3 该列是结构化消息行
                # 列表(role/content/style…,annotation pipeline 产物),
                # 不是标注文本字符串;逐帧文本已由 annotation 列承载。
                "next.done": frame_index == frame_count - 1,
            }
            row.update(sensor_rows.get(frame_index, {}))
            hand = hand_rows.get(frame_index)
            if hand and not device_hand_rows:
                # 主命名空间 observation.state.hand_* 仅作兜底:有设备命名
                # 空间产物(devices.<source>.hand_*)时不再重复写同一份数据
                row.update(_hand_columns(hand, coordinate_key=coordinate_key))
            for device_source, rows_by_frame in device_hand_rows.items():
                device_hand = rows_by_frame.get(frame_index)
                if device_hand:
                    row.update(_hand_columns(
                        device_hand, coordinate_key=coordinate_key,
                        device_namespace=device_source))
            if hand_rows_rcam:
                hand_rcam = hand_rows_rcam.get(frame_index)
                if hand_rcam:
                    row.update(_hand_columns(hand_rcam, suffix="_rcam",
                                             coordinate_key=coordinate_key))
            for asset in depth_assets:
                row[f"observation.depth.{asset['source_key']}.valid"] = (
                    frame_index not in set(asset.get("missing_frames", []))
                )
            _sanitize_row(row, nan_fill=0.0 if is_v2 else float("nan"))
            rows.append(row)

        episode_row: dict[str, Any] = {
            "episode_index": out_episode_index,
            "episode_id": episode_id,
            "length": frame_count,
            "task_index": task_id,
            "tasks": [task_descriptions[task_id]],
            "split": "train" if out_episode_index < max(1, int(len(episodes) * split_ratio)) else "val",
            # 官方 v3.0 episode 索引列(checker 必需):全局行区间 =
            # 该集在合并 data parquet 中的 [from, to) 偏移;单文件数据集
            # 分片/文件位置恒 0(官方列名带前缀,照写)
            "dataset_from_index": len(rows) - frame_count,
            "dataset_to_index": len(rows),
            "data/chunk_index": 0,
            "data/file_index": 0,
            "meta/episodes/chunk_index": 0,
            "meta/episodes/file_index": 0,
        }
        # 官方 v3.0 每路视频的时间戳映射(单目/双目/多目通用,按实际
        # 导出的 video key 生成):from_timestamp 为该集在视频文件内的
        # 起始秒,to_timestamp 为结束秒。本导出每集一个视频文件 → 从 0
        # 到探测时长;帧在 [from, to) 区间内按 fps 换算帧号。
        for vkey, (vcount, vfps) in sorted(episode_video_meta.items()):
            duration = (vcount / vfps) if (vcount > 0 and vfps) else 0.0
            episode_row[f"videos/{vkey}/chunk_index"] = 0
            episode_row[f"videos/{vkey}/file_index"] = out_episode_index
            episode_row[f"videos/{vkey}/from_timestamp"] = 0.0
            episode_row[f"videos/{vkey}/to_timestamp"] = duration
        episode_rows.append(episode_row)
        # v2.1:该集 rows 切片范围(index, task_id, length, start, end)
        episode_row_ranges.append((out_episode_index, task_id, frame_count,
                                   len(rows) - frame_count, len(rows)))
        episode_video_meta_records.append(episode_video_meta)
        if progress_callback is not None:
            try:
                progress_callback(done=out_episode_index + 1,
                                  total_episodes=len(episodes))
            except Exception:
                pass  # 进度回调失败不影响导出本身

    # 调用方指定剔除的列(如触觉力矩阵):在补齐/统计/写盘之前删掉,
    # 这样 parquet、features 声明与 stats 三处天然一致,不需要各自过滤。
    if exclude_columns:
        dropped = set(str(name) for name in exclude_columns)
        if dropped:
            for row in rows:
                for key in dropped:
                    row.pop(key, None)
            print(f"[lerobot_export] excluded columns: {sorted(dropped)}")

    # SLAM 位姿列:采集端 52 集起只写 slam_trajectory,不再写 slam_pose。
    # 必须赶在 _pad_variable_columns 之前做 —— 补齐会把冲空帧填成 0,而
    # 0 在 slam_trajectory 里是一个"合法样本"(t=0 的位姿),重建时会被
    # 当成真实帧混进去。此处补出的 slam_pose 又是下面 observation.state
    # 的派生依据,故顺序不能颠倒。
    _ensure_slam_pose(rows)

    # ACT 的观测需要 proprioception:单帧图像不足以让模型知道夹爪当前
    # 在哪。数据集若已有 observation.state 则原样保留;否则在 UMI 会话上
    # 由 SLAM 位姿 + 夹爪开合派生 [Δx, Δy, Δz, gripper](位置为相对本集
    # 起点的偏移 —— SLAM 世界原点是每集任意初始化的,绝对坐标是噪声)。
    _ensure_observation_state(rows)

    # 变长异步传感器列固定长度化:官方加载器不支持变长列(实测
    # list<int64> 对声明 int64[1] 直接 cast 失败)。取全数据集最大
    # 样本数补齐,声明 shape 同步为固定长度,变长语义保留在 note。
    imu_cap, padded_lengths = _pad_variable_columns(
        rows, nan_fill=0.0 if is_v2 else float("nan"))

    # 收敛:删恒定哨兵垃圾列(全程 NaN/-1/0/空串 → 数据从未写入)。
    # 有任一帧真实值则整列保留;features 里对应声明一并剔除。
    junk_cols = _junk_columns(rows)
    if junk_cols:
        for row in rows:
            for key in junk_cols:
                row.pop(key, None)
        print(f"[lerobot_export] dropped constant sentinel columns: "
              f"{sorted(junk_cols)}")

    # 浮点列统一 float32(官方参考 pose/timestamp 均为 float32;
    # pyarrow 对 Python float 默认推断 double,会导致声明与 schema 不符)
    _cast_float32(rows)

    # robot_type 从物理设备声明推导(如 d435 / s80m),不再写死 unknown。
    robot_kinds: list[str] = []
    for docs in device_metadata_by_episode.values():
        for doc in docs or []:
            kind = str(doc.get("kind") or "").strip()
            if kind and kind not in robot_kinds:
                robot_kinds.append(kind)
    robot_type = "+".join(robot_kinds) or "unknown"

    # 刚写进 data/ 的表顺手留给 _write_stats:省掉它把整个 data 目录从磁盘
    # 再读回来一遍(见 _write_stats 的 data_table 参数说明)。
    data_table = None
    if is_v2:
        # v2.1: each episode has one data parquet.  The official v2.1
        # converter reads the legacy JSONL files below; do not create a
        # nested v3-style meta/episodes parquet tree in a v2.1 export.
        episode_index_rows = []
        _v2_tables = []
        for ep_index, (_, task_id, length, start, end) in enumerate(
                episode_row_ranges):
            episode_chunk = ep_index // 1000
            data_dir = output_dir / "data" / f"chunk-{episode_chunk:03d}"
            data_dir.mkdir(parents=True, exist_ok=True)
            episode_table = pa.Table.from_pylist(rows[start:end])
            pq.write_table(episode_table,
                           data_dir / f"episode_{ep_index:06d}.parquet")
            _v2_tables.append(episode_table)
            record = {
                "episode_index": ep_index,
                "tasks": [task_descriptions[task_id]],
                "task_index": task_id,
                "length": length,
                "data/chunk_index": episode_chunk,
                "data/file_index": ep_index,
                "dataset_from_index": start,
                "dataset_to_index": end,
            }
            vmeta = (episode_video_meta_records[ep_index]
                     if ep_index < len(episode_video_meta_records) else {})
            for vkey, (vcount, vfps) in sorted(vmeta.items()):
                duration = (vcount / vfps) if (vcount > 0 and vfps) else 0.0
                record[f"videos/{vkey}/chunk_index"] = episode_chunk
                record[f"videos/{vkey}/file_index"] = ep_index
                record[f"videos/{vkey}/from_timestamp"] = 0.0
                record[f"videos/{vkey}/to_timestamp"] = duration
            episode_index_rows.append(record)
        # 官方 v2.1 legacy 索引:convert_dataset_v21_to_v30 转换脚本
        # 硬性读取 meta/episodes.jsonl(缺失直接 FileNotFoundError)。
        meta_dir = output_dir / "meta"
        meta_dir.mkdir(parents=True, exist_ok=True)
        (meta_dir / "episodes.jsonl").write_text(
            "\n".join(json.dumps(record, ensure_ascii=False,
                                 default=_json_default)
                      for record in episode_index_rows) + "\n",
            encoding="utf-8")
        # 逐集表的 schema 可能因某一集整列为空而不同(类型推断随值走),
        # concat 失败就退回磁盘重读 —— 统计量宁可慢,不能缺。
        try:
            data_table = (pa.concat_tables(_v2_tables, promote=True)
                          if _v2_tables else None)
        except Exception:
            data_table = None
        _write_episodes_stats_v21(output_dir, episode_row_ranges, rows,
                                  episode_video_meta_records, video_features)
    else:
        data_dir = output_dir / "data" / "chunk-000"
        data_dir.mkdir(parents=True, exist_ok=True)
        data_table = pa.Table.from_pylist(rows)
        pq.write_table(data_table, data_dir / "file-000.parquet")
        # Official v3 stores the complete episode table in one parquet file
        # (the writer may shard this directory for very large datasets).  A
        # per-episode file here is readable by our UI but is not the official
        # v3 layout and makes third-party loaders miss the index.
        episodes_path = (output_dir / "meta" / "episodes" / "chunk-000"
                         / "file-000.parquet")
        episodes_path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.Table.from_pylist(episode_rows), episodes_path)

    features = {
        **video_features,
        "episode_index": {"dtype": "int64", "shape": [1]},
        "frame_index": {"dtype": "int64", "shape": [1]},
        "timestamp": {"dtype": "float32", "shape": [1]},
        "index": {"dtype": "int64", "shape": [1]},
        "task_index": {"dtype": "int64", "shape": [1]},
        "annotation": {"dtype": "string", "shape": [1]},
        "annotation_index": {"dtype": "int64", "shape": [1]},
        "annotation_scope": {"dtype": "string", "shape": [1]},
        "next.done": {"dtype": "bool", "shape": [1]},
    }
    # 手部骨骼 feature 声明(连接驱动:产物传入才写入)。
    # 字段名必须和坐标语义一致:world=深度相机米制,3d=相对 3D,2d=图像坐标。
    is_3d = any(
        entry and entry.get("is_3d")
        for hand in hand_rows.values()
        for entry in hand.values()
    ) if hand_rows else False
    if hand_rows and not device_hand_rows:
        # 主命名空间 feature 声明仅作兜底(与行写入规则一致:
        # 有设备命名空间产物时主命名空间不写列、不声明)。
        # unit 语义:hand_3d 产物 manifest 显式声明的 unit(如深度图
        # 抬升的 camera_meters)优先;否则 world_landmarks 相对 3D /
        # 2D 归一化坐标按 is_3d 区分(stereo 路径行为不变)。
        world_unit = hand_3d_unit or (
            "mediapipe_world_relative" if coordinate_key == "3d"
            else "normalized_image_coords")
        world_note = (
            "深度图抬升相机系米制 3D(彩色相机系,米)"
            if coordinate_key == "world" else
            "RGB-only 相机相对 3D 估计(无深度真值,近似米制,不可视为测量值)"
            if hand_3d_unit == "rgb_estimated_meters" else
            "MediaPipe world_landmarks 相对 3D(手腕相对、任意尺度)"
            if coordinate_key == "3d" else
            "MediaPipe 21 关键点归一化图像坐标")
        left_key = f"observation.state.hand_left_{coordinate_key}"
        right_key = f"observation.state.hand_right_{coordinate_key}"
        features.update({
            left_key: {
                "dtype": "float32", "shape": [63], "names": _hand_xyz_names(),
                "unit": world_unit,
                "note": world_note,
            },
            right_key: {
                "dtype": "float32", "shape": [63], "names": _hand_xyz_names(),
                "unit": world_unit,
                "note": world_note,
            },
            f"{left_key}_valid": {"dtype": "bool", "shape": [1]},
            f"{right_key}_valid": {"dtype": "bool", "shape": [1]},
            f"{left_key}_source": {
                "dtype": "string", "shape": [1],
                "note": "该帧左手骨骼关键点来源相机 key(多路识别;空 = 单路)",
            },
            f"{right_key}_source": {
                "dtype": "string", "shape": [1],
                "note": "该帧右手骨骼关键点来源相机 key(多路识别;空 = 单路)",
            },
            f"{left_key}_reprojection_error": {
                "dtype": "float32", "shape": [1],
                "note": "该帧左手 3D 关键点重投影误差(像素),质量评估/训练过滤用",
            },
            f"{right_key}_reprojection_error": {
                "dtype": "float32", "shape": [1],
                "note": "该帧右手 3D 关键点重投影误差(像素),质量评估/训练过滤用",
            },
            "observation.hand_0_gesture": {
                "dtype": "string", "shape": [1],
                "note": ("手指伸展标签:3D 三角化路径为角度判定"
                         "(fist / open:index,middle ...);2D 路径为 7 类名"
                         "(closed_fist/open_palm/...);空 = 无手/未知"),
            },
            "observation.hand_1_gesture": {
                "dtype": "string", "shape": [1],
                "note": ("手指伸展标签:3D 三角化路径为角度判定"
                         "(fist / open:index,middle ...);2D 路径为 7 类名"
                         "(closed_fist/open_palm/...);空 = 无手/未知"),
            },
            "observation.hand_0_fingers": {
                "dtype": "int64", "shape": [1],
                "note": ("手指伸展 bitmask(thumb=1,index=2,middle=4,ring=8,"
                         "pinky=16;0=fist;-1=未知/旧数据)"),
            },
            "observation.hand_1_fingers": {
                "dtype": "int64", "shape": [1],
                "note": ("手指伸展 bitmask(thumb=1,index=2,middle=4,ring=8,"
                         "pinky=16;0=fist;-1=未知/旧数据)"),
            },
            "observation.hand_0_gesture_score": {
                "dtype": "float32", "shape": [1],
                "note": "旧 7 类手势模型分数;3D 角度手势路径恒 0",
            },
            "observation.hand_1_gesture_score": {
                "dtype": "float32", "shape": [1],
                "note": "旧 7 类手势模型分数;3D 角度手势路径恒 0",
            },
            "observation.hand_0_confidence": {"dtype": "float32", "shape": [1]},
            "observation.hand_1_confidence": {"dtype": "float32", "shape": [1]},
            "observation.hand_0_handedness": {"dtype": "string", "shape": [1]},
            "observation.hand_1_handedness": {"dtype": "string", "shape": [1]},
        })
    # 右目(辅助视角)手部骨骼 feature 声明(连接驱动:hand_3d_right 产物
    # 传入才写入)。_rcam 列组与主列独立,训练时可按需选用/忽略。
    if hand_rows_rcam:
        rcam_left_key = f"observation.state.hand_left_{coordinate_key}_rcam"
        rcam_right_key = f"observation.state.hand_right_{coordinate_key}_rcam"
        features.update({
            rcam_left_key: {
                "dtype": "float32", "shape": [63], "names": _hand_xyz_names(),
                "unit": "mediapipe_world_relative",
                "note": "右目(辅助视角)相机检测的左手世界坐标;与主列并存,列组独立",
            },
            rcam_right_key: {
                "dtype": "float32", "shape": [63], "names": _hand_xyz_names(),
                "unit": "mediapipe_world_relative",
                "note": "右目(辅助视角)相机检测的右手世界坐标;与主列并存,列组独立",
            },
            f"{rcam_left_key}_valid": {"dtype": "bool", "shape": [1]},
            f"{rcam_right_key}_valid": {"dtype": "bool", "shape": [1]},
            f"{rcam_left_key}_source": {
                "dtype": "string", "shape": [1],
                "note": "该帧左手骨骼关键点来源相机 key(右目数据恒为右目产物名)",
            },
            f"{rcam_right_key}_source": {
                "dtype": "string", "shape": [1],
                "note": "该帧右手骨骼关键点来源相机 key(右目数据恒为右目产物名)",
            },
            f"{rcam_left_key}_reprojection_error": {
                "dtype": "float32", "shape": [1],
                "note": "右目左手 3D 关键点重投影误差(像素),质量评估/训练过滤用",
            },
            f"{rcam_right_key}_reprojection_error": {
                "dtype": "float32", "shape": [1],
                "note": "右目右手 3D 关键点重投影误差(像素),质量评估/训练过滤用",
            },
            "observation.hand_0_gesture_rcam": {
                "dtype": "string", "shape": [1],
                "note": "右目槽位 0 手势标签(角度判定,与主列同规则)",
            },
            "observation.hand_1_gesture_rcam": {
                "dtype": "string", "shape": [1],
                "note": "右目槽位 1 手势标签(角度判定,与主列同规则)",
            },
            "observation.hand_0_fingers_rcam": {
                "dtype": "int64", "shape": [1],
                "note": "右目槽位 0 手指伸展 bitmask(同主列规则)",
            },
            "observation.hand_1_fingers_rcam": {
                "dtype": "int64", "shape": [1],
                "note": "右目槽位 1 手指伸展 bitmask(同主列规则)",
            },
            "observation.hand_0_gesture_score_rcam": {
                "dtype": "float32", "shape": [1],
                "note": "右目槽位 0 手势分数(角度路径恒 0)",
            },
            "observation.hand_1_gesture_score_rcam": {
                "dtype": "float32", "shape": [1],
                "note": "右目槽位 1 手势分数(角度路径恒 0)",
            },
            "observation.hand_0_confidence_rcam": {"dtype": "float32", "shape": [1]},
            "observation.hand_1_confidence_rcam": {"dtype": "float32", "shape": [1]},
            "observation.hand_0_handedness_rcam": {"dtype": "string", "shape": [1]},
            "observation.hand_1_handedness_rcam": {"dtype": "string", "shape": [1]},
        })
    if device_hand_rows:
        world_unit = hand_3d_unit or (
            "mediapipe_world_relative" if coordinate_key == "3d"
            else "normalized_image_coords")
        world_note = (
            "深度图抬升相机系米制 3D(每个物理设备独立坐标系,米)"
            if coordinate_key == "world" else
            "RGB-only 相机相对 3D 估计(每个物理设备独立,无深度真值)"
            if hand_3d_unit == "rgb_estimated_meters" else
            "MediaPipe world_landmarks 相对 3D(设备独立)"
            if coordinate_key == "3d" else
            "MediaPipe 21 关键点归一化图像坐标(设备独立)")
        for device_source in device_hand_rows:
            features.update(_device_hand_features(
                device_source, coordinate_key, world_unit, world_note))
    # 传感器/触觉列声明(采集端上传,透传进数据集)——
    # 列在 parquet 但 features 缺失会让 LeRobot 加载器读不完整。
    # 手套压力 = observation.tactile.left/right(16×16 压力阵列,
    # 与视觉骨骼 observation.state.hand_* 完全分离)。
    if sensor_rows:
        sample = next(iter(sensor_rows.values()))
        for key, value in sample.items():
            if key in features:
                continue
            import numpy as np
            arr = np.asarray(value)
            shape = list(arr.shape) if arr.ndim else [1]
            # 补齐过的列必须以实际写入的长度声明。首帧样本往往正是空的那
            # 一帧(触觉矩阵首帧、SLAM 丢帧),照它声明会得到 shape [0] 或
            # 偏小的长度,与实际写入的数据不符 → 官方加载器 cast 失败。
            if key in padded_lengths:
                shape = [padded_lengths[key]]
            if key in ("observation.tactile.left", "observation.tactile.right"):
                features[key] = {
                    "dtype": "float32", "shape": [256],
                    "names": _tactile_names(256),
                    "note": ("手套压力/触觉阵列(16×16 行主序扁平化,float32,"
                            "与视频帧率对齐,每帧取该帧窗口最后一个样本);"
                            "burst 双读样本仅隔 ~2ms,不做变长聚合"),
                }
            elif key in ("observation.left_hand_pose", "observation.right_hand_pose"):
                features[key] = {
                    "dtype": "float32", "shape": [63],
                    "names": _hand_xyz_names(),
                    "note": ("采集端手套姿态(21 关键点×xyz,每帧取该帧窗口最后"
                            "一个样本)。与相机关键点 "
                            "observation.state.devices.*.hand_* 并存:手套"
                            "姿态关节精度高且遮挡鲁棒,相机关键点绝对 3D 最准"
                            ",训练端自选"),
                }
            elif key == "observation.imu_ts_ns":
                # 固定长度化(每帧补齐到 imu_cap,不足补 0);
                # 有效样本数以 observation.imu 对应段的 NaN 判定
                features[key] = {
                    "dtype": "int64", "shape": [imu_cap],
                    "note": ("IMU 采样时间戳(ns),固定补齐至每帧最多 "
                             f"{imu_cap} 个样本,不足帧尾部补 0;与 "
                             "observation.imu 逐样本对齐"),
                }
            elif key == "observation.imu":
                features[key] = {
                    "dtype": "float32", "shape": [imu_cap * 6],
                    "note": ("IMU 采样批次:每帧固定 "
                             f"{imu_cap}×6 维展平(3 轴加速度+3 轴角速度),"
                             "不足帧尾部样本补 NaN,时间戳见 "
                             "observation.imu_ts_ns"),
                }
            else:
                features[key] = {"dtype": _passthrough_dtype(key, sensor_rows),
                                 "shape": shape}
                # 力矩阵的行内差分/定标倍率不是能从数据本身看出来的属性,
                # 必须跟着声明一起走,否则产物不可解读(见 _force_matrix_semantics)。
                semantics = force_matrix_meta.get(key)
                if semantics:
                    features[key].update(semantics)
    if junk_cols:
        features = {k: v for k, v in features.items() if k not in junk_cols}
    # 调用方剔除的列同样从声明里去掉。features 由 sensor_rows 样本推导,
    # 而剔除只作用于 rows,不过滤这里会留下"声明了但数据里没有"的列,
    # 官方加载器读不出来会报错。
    if exclude_columns:
        excluded = set(str(name) for name in exclude_columns)
        features = {k: v for k, v in features.items() if k not in excluded}
    # 导出的行里有 observation.state 就必须声明,否则官方加载器读不完整。
    # 声明放在剔除之后:调用方显式排除的列不该被这里复活。
    # 补出来的 slam_pose 同样必须声明:features 是从**源 parquet** 采样的
    # (sensor_rows),而这列由 _ensure_slam_pose 在内存里写进 rows,采样
    # 看不到它 —— 不补声明就是"数据里有列、声明里没有",官方加载器读不完整。
    if rows and "observation.slam_pose" in rows[0] and "observation.slam_pose" not in features:
        features["observation.slam_pose"] = {
            "dtype": "float32", "shape": [7],
            "names": ["x", "y", "z", "qx", "qy", "qz", "qw"],
            "note": ("世界系绝对位姿(米 + 四元数 xyzw)。采集端 52 集起不再"
                     "写入该列,导出时由 observation.slam_trajectory 的 50Hz"
                     "样本重建(与采集端原始列实测位置相关 0.998+,同一世界系)。"),
        }
    if rows and "observation.state" in rows[0] and "observation.state" not in features:
        features["observation.state"] = {
            "dtype": "float32", "shape": [4],
            "names": ["delta_x", "delta_y", "delta_z", "gripper"],
            "note": ("proprioception:本帧位置相对**本集第一帧**的偏移(米)"
                     "+ 夹爪开合(0–1)。SLAM 世界原点是每集任意初始化的,"
                     "故用相对量而非绝对坐标。"),
        }
    # 深度有效帧标记列(行循环写入,不声明会导致官方加载器读不完整)
    for depth_key in sorted(depth_source_keys):
        key = f"observation.depth.{depth_key}.valid"
        features[key] = {
            "dtype": "bool", "shape": [1],
            "note": "该帧深度真值(采集端 PNG)是否存在;false 帧的深度视频码值无效",
        }
    train_episode_count = sum(1 for item in episode_rows if item["split"] == "train")
    split_spec = {"train": f"0:{train_episode_count}"}
    if train_episode_count < len(episode_rows):
        split_spec["val"] = f"{train_episode_count}:{len(episode_rows)}"
    _canonicalize_video_features(features)
    if is_v2:
        # v2.1: episode 文件路径模板(episode_chunk/episode_index 占位),并带
        # total_videos/total_chunks(v3.0 中已移除,转换脚本会删)。
        info_json: dict[str, Any] = {
            "codebase_version": "v2.1",
            "robot_type": robot_type,
            "total_episodes": len(episodes),
            "total_frames": len(rows),
            "total_tasks": len(task_descriptions),
            "chunks_size": chunks_size,
            "data_files_size_in_mb": 100,
            "video_files_size_in_mb": 100,
            "fps": float(episodes[0].get("fps") or 30),
            "splits": split_spec,
            "data_path": "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
            "video_path": "videos/{video_key}/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.mp4",
            # Keep video_info in v2.1 as well. Removing it loses
            # video.is_depth_map and makes an exported depth stream
            # indistinguishable from RGB for downstream readers.
            "features": features,
            "total_videos": total_videos,
            "total_chunks": 1,
            "extensions": {
                "episodes_file": "meta/episodes.jsonl",
                "episodes_stats_file": "meta/episodes_stats.jsonl",
            },
        }
    else:
        info_json = {
            "codebase_version": "v3.0",
            "robot_type": robot_type,
            "total_episodes": len(episodes),
            "total_frames": len(rows),
            "total_tasks": len(task_descriptions),
            "chunks_size": chunks_size,
            "data_files_size_in_mb": 100,
            "video_files_size_in_mb": 100,
            "fps": float(episodes[0].get("fps") or 30),
            "splits": split_spec,
            "data_path": "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet",
            "video_path": "videos/chunk-{chunk_index:03d}/{video_key}/file-{file_index:03d}.mp4",
            "features": features,
            "extensions": {
                "episodes_file": "meta/episodes/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet",
            },
        }
    _write_json(output_dir / "meta" / "info.json", info_json)
    # 官方 parquet 内嵌元数据(huggingface 键,官方 pusht 同款):部分
    # 查看器/加载器读 parquet schema metadata 里的内嵌 info 而非
    # meta/info.json —— 缺失时数据集校验报 total_episodes/total_frames
    # 无效(实测 viewer 报错场景)。
    # 先算 stats 再嵌 parquet 元数据。两者读写互不相干(stats 只用到内存里
    # 刚写好的 data_table + video_features + videos/*.mp4;embed 只重写
    # data/ 与 meta/episodes/ 的 schema metadata),换序不改变任何产物。
    # 换序是为了内存:_embed_info_metadata 自己会把整份 data parquet 读进
    # 内存(v3 是单文件,50 集约 650MB),若此时 data_table 还活着,峰值
    # 就凭空多出一整个数据集的量。算完立刻放掉引用。
    _write_stats(output_dir, video_features, data_table)
    data_table = None
    _embed_info_metadata(output_dir, info_json)

    tasks_path = output_dir / "meta" / "tasks.jsonl"
    tasks_path.parent.mkdir(parents=True, exist_ok=True)
    if is_v2:
        with tasks_path.open("w", encoding="utf-8") as handle:
            for task_id, description in sorted(task_descriptions.items()):
                handle.write(json.dumps({"task_index": task_id, "task": description}, ensure_ascii=False) + "\n")
    else:
        # v3 exports use the official Parquet task table and do not carry the
        # internal tasks.jsonl compatibility file.
        tasks_path.unlink(missing_ok=True)
        _write_tasks_parquet(output_dir, task_descriptions)

    # Annotation segments and device provenance are represented by the
    # per-frame parquet columns and info/episode metadata.  Do not create
    # the non-standard standalone meta/annotations.jsonl or meta/devices.json
    # companions in either v2.1 or v3.0 exports.
    (output_dir / "meta" / "annotations.jsonl").unlink(missing_ok=True)
    (output_dir / "meta" / "devices.json").unlink(missing_ok=True)
    return output_dir
