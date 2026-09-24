"""证据采集 —— 从批次目录读出检查项需要的一切。

检查项**不碰磁盘**（见 ``checks.CheckContext`` 的 IO 注入设计）。所有读取集中在
这里，好处有两个：

* 检查项本体是纯逻辑，测试喂假证据即可，不需要真 mp4 / parquet / cv2；
* "读什么、怎么读"只有一处实现，不会出现两个检查项对同一列用不同口径。

**本模块只读** —— 不写任何文件、不改任何数据。质检出结论，不出数据。
"""

from __future__ import annotations

import glob
import re
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from app.processing.cleaning.checks import StreamEvidence
from app.processing.cleaning.contract import (
    pose_is_valid,
    channels_for, normalize_device_modality,
)

# SLAM 列的命名：``observation.slam_*`` 是默认相机；双目/多把第二路存成
# ``observation.<相机名>_slam_*``。这里按**相机**分组，每组里 trajectory 优先。
#
# 为什么不能写死两个列名：实测 Test94/episode_000003 **只有** gripper_2 前缀的
# 两列，没有不带前缀的 —— 那种集有 SLAM 数据、却一个 SLAM 检查都不跑，且不报
# 任何错。同一项目里不同集的列名不一致是常态（见 MODALITY_CHANNELS 的告诫）。
#
# 两个**不要**匹配进去的近亲：
#   observation.slam_trajectory_ns            —— 时间戳伴生列，不是位姿
#   processing.umi_slam_action.data_4.action  —— 处理缓存列，名字里恰好含 slam
_SLAM_COLUMN_RE = re.compile(r"^observation\.(?:(.+)_)?slam_(trajectory|pose)$")

# 列名 → 信道。值为**可能的列名**（含历史名）。同一信道的每个存在的列都会
# 登记成一条流（左右两路力就是两条），流名取列名最后一段。
#
# ★ 这里**不能**用 `break` 只取第一个 —— 那样左右两路力只剩一路。
#   要与别名列去重的话看 ``_COLUMN_ALIASES``，别在这里提前退出。
#
# slam 不在下表里 —— 它按相机分组解析，见 ``_slam_columns``。
_COLUMN_CHANNELS: dict[str, tuple[str, ...]] = {
    "slam": (
        "observation.slam_trajectory",   # 高频样本缓冲（优先）
        "observation.slam_pose",         # 低频快照（旧批才有）
    ),
    "gripper_state": ("observation.gripper_state",),
    "force": (
        "observation.gripper_left_force",
        "observation.gripper_right_force",
        "observation.gripper_force",
    ),
    "tactile": (
        "observation.left_glove",
        "observation.right_glove",
        "observation.glove",
    ),
    "device_status": (
        "status.left_glove",
        "status.right_glove",
        "status.glove",
    ),
    "depth": ("observation.images.depth",),
    "action": ("action",),
}

# 别名列 → 协议列名。**同一份数据的第二套命名**，不新增信道，只是改名。
#
# 触觉数据有两套名字：
#   采集协议写  observation.left_glove / right_glove
#   导出时统一成 observation.tactile.left / right
#   （见 lerobot_export._TACTILE_RENAME，hdf5_export / ai_annotation 读的是后者）
#
# 原始 session 目录里两套可能同时在（导出产物被合并回 canonical）；而**只跑过
# 导出的树里只有后者**。此前只认协议名 —— 那种树上手套信道直接为空，所有手套
# 检查静默跳过，不报任何错。
_COLUMN_ALIASES: dict[str, str] = {
    "observation.tactile.left": "observation.left_glove",
    "observation.tactile.right": "observation.right_glove",
}

# 各信道的向量宽度 —— 用于把列值切成定长元组。
#
# ★ 不在表里的信道**不截断**（见 ``vector_series``）。默认截断到某个数字是
#   危险的：触觉阵列是 16×16=256 维，此前因为这里没有 ``tactile`` 项而落到
#   3 的兜底值，256 个感应点被静默截成 3 个 —— 检查项只看得到 1% 的阵列。
#   换手套（或加新阵列通道）时同样会静默丢数据，所以默认必须是"不截断"。
_CHANNEL_WIDTH: dict[str, int] = {
    "gripper_state": 3,
    "force": 3,
    "action": 7,
}


def _apply_column_aliases(frame):
    """把别名列归一成协议列名。

    **只在协议名缺失时改** —— 两套都在时保留协议名：它们是同一份数据，
    登记成两条流会让同一处缺陷被报两次，也让"左右手"的流名对不上。
    （实测 S80C / D435 两套都在时逐元素完全相同。）
    """
    rename = {alias: canonical for alias, canonical in _COLUMN_ALIASES.items()
              if alias in frame.columns and canonical not in frame.columns}
    return frame.rename(columns=rename) if rename else frame

_DEFAULT_FPS = 30.0
_MAX_VIDEO_PROBES = 4


@dataclass(frozen=True)
class Evidence:
    """一个 episode 的全部证据 —— engine 交给检查项的那一份。"""

    batch_dir: Path
    streams: tuple[StreamEvidence, ...] = ()
    channels: frozenset[str] = frozenset()
    modalities: tuple[str, ...] = ()
    fps: float = _DEFAULT_FPS
    rows: int = 0
    columns: tuple[str, ...] = ()
    parquet: str = ""
    notes: dict[str, Any] = dc_field(default_factory=dict)

    @property
    def duration_sec(self) -> float:
        return (self.rows / self.fps) if self.fps else 0.0


# ── 读文件 ────────────────────────────────────────────────────

def _glob(pattern: str, root: Path) -> list[Path]:
    return [Path(item) for item in sorted(glob.glob(str(root / pattern)))]


def _file_holding(parquets: list[Path], episode_index: int) -> Path | None:
    """哪个文件里装着这一集 —— 只读 ``episode_index`` 一列，不整份加载。

    文件名对不上时的兜底（历史命名、手工搬过的目录）。返回 None 表示都没有。
    """
    import pyarrow.parquet as pq

    for path in parquets:
        try:
            column = pq.ParquetFile(path).read(columns=["episode_index"])
        except Exception:
            continue
        values = column.column("episode_index").to_pylist()
        if episode_index in values:
            return path
    return None


def resolve_episode_data(batch_dir: Path,
                         episode_index: int | None = None) -> tuple[Path | None, bool]:
    """定位这一集的数据文件。返回 ``(parquet, 还要不要按行过滤)``。

    两种布局的粒度**不一样**，混了就会读出空帧：

      canonical ``data/chunk-*/episode_NNNNNN.parquet`` —— 一集一文件。
        选对**文件**即可，行全是这一集的。
      导出产物 ``data/chunk-*/file-NNN.parquet`` —— 一文件多集，
        必须再按 ``episode_index`` 列筛行。

    ★ 这里曾经是「取 ``parquets[0]`` 再按列过滤」。canonical 布局下第 0 个文件
      只装第 0 集，于是传任何**别的** ``episode_index`` 都筛出空帧 —— 不报错，
      只是报告变成 ``empty_report``（``episode.status = ERROR``）。在"数据不对
      就拦回人工审核"的链路里，表现是**除第 0 集外每一集都被卡住**。
    """
    root = Path(batch_dir)

    canonical = _glob("data/chunk-*/episode_*.parquet", root)
    if canonical:
        if episode_index is None:
            return canonical[0], False
        # 文件名就是集号（LeRobot canonical 约定，注意编号可能有空洞）
        wanted = f"episode_{int(episode_index):06d}.parquet"
        for path in canonical:
            if path.name == wanted:
                return path, False
        # 文件名对不上 —— 按列找，别静默返回空
        return _file_holding(canonical, int(episode_index)), False

    multi = _glob("data/chunk-*/*.parquet", root)
    if not multi:
        return None, False
    if episode_index is None:
        return multi[0], False
    return _file_holding(multi, int(episode_index)), True


def load_rows(parquet: Path, filter_episode_index: int | None = None):
    """读 parquet；给了 ``filter_episode_index`` 就只取那一集的行。

    只在**一文件多集**的布局下才该传它 —— canonical 的一集一文件布局下传了会
    把这一集自己的行全筛掉。用 ``resolve_episode_data`` 拿到的第二个返回值决定。
    """
    import pyarrow.parquet as pq

    frame = pq.ParquetFile(parquet).read().to_pandas()
    if filter_episode_index is not None and "episode_index" in frame.columns:
        frame = frame[frame["episode_index"].values == filter_episode_index]
    return frame.reset_index(drop=True)


# ── 列 → 序列 ─────────────────────────────────────────────────

def slam_valid_counts(values) -> tuple[int, ...]:
    """每帧的有效位姿样本数 —— 空帧即为 0。

    slam_trajectory 是扁平的 ``[t,x,y,z,qx,qy,qz,qw]×N``。导出产物会把它补齐到
    定长（补 NaN），所以"有效"= 非 NaN 的 8 元组个数。
    """
    counts: list[int] = []
    for item in values:
        if item is None:
            counts.append(0)
            continue
        array = np.asarray(item, dtype=float).ravel()
        valid = 0
        for index in range(array.size // 8):
            if np.isfinite(array[index * 8:index * 8 + 8]).all():
                valid += 1
        counts.append(valid)
    return tuple(counts)


def slam_poses(values, counts) -> tuple[tuple, ...]:
    """逐帧取【第一个有效样本】的位姿（位置 + 四元数）。

    ★ 刻意不重用 ``umi_slam_action.poses_from_trajectory`` 的插值重建 ——
    那是生产口径（含时间基拟合与插值）。连续性检查要看的是**原始落盘数据**
    有没有跳变；用插值后的平滑轨迹会掩盖真实跳变。
    """
    poses: list[tuple] = []
    for item, count in zip(values, counts):
        if not count:
            poses.append(())
            continue
        array = np.asarray(item, dtype=float).ravel()
        poses.append(tuple(float(v) for v in array[1:8]))
    return tuple(poses)


def _slam_columns(columns) -> list[str]:
    """按**相机**挑出 SLAM 列，每个相机一个：trajectory 优先，回落到 pose。

    相机名 = ``observation.`` 与 ``_slam_`` 之间那段，默认相机没有前缀：

        observation.slam_trajectory           → 相机 ""
        observation.gripper_2_slam_trajectory → 相机 "gripper_2"

    每个相机只取一个 —— trajectory 是高频样本缓冲、pose 是低频快照，
    两个都有时用前者（与旧行为一致，只是现在**每个相机各自**取一个，
    而不是全局只取一个）。
    """
    by_camera: dict[str, dict[str, str]] = {}
    for column in sorted(str(name) for name in columns):
        match = _SLAM_COLUMN_RE.match(column)
        if not match:
            continue
        camera, kind = match.group(1) or "", match.group(2)
        by_camera.setdefault(camera, {})[kind] = column

    picked: list[str] = []
    for camera in sorted(by_camera):
        kinds = by_camera[camera]
        picked.append(kinds.get("trajectory") or kinds["pose"])
    return picked


def _column_series(values, channel: str, width: int | None = None) -> tuple:
    """一列 → 序列。**字符串列原样保留，不试着数值化。**

    ``vector_series`` 对字符串会走 ``np.asarray(..., dtype=float)`` 抛
    ``ValueError``，被 ``except`` 吞掉后**每帧都变成空元组** —— 检查项拿到的是
    "这一列没有数据"，看起来像"检查通过"。设备状态列（``status.left_glove``）
    正是字符串，所以这里必须先分流。
    """
    if len(values) and isinstance(values.iloc[0], str):
        return tuple("" if item is None else str(item) for item in values)
    if width is None:
        width = _CHANNEL_WIDTH.get(channel)
    return vector_series(values.tolist(), width)


def vector_series(values, width: int | None = None) -> tuple[tuple, ...]:
    """把一列向量值转成元组序列；空/坏的写成空元组。

    ``width=None``（默认）**不截断**。截断是静默的 —— 调用方只有显式声明了
    正确宽度时才该发生（如 action 固定 7 维）；未知列宽一律保留整条，
    免得"少了一半感应点"这种问题要等到训练效果不对才发现。
    """
    out: list[tuple] = []
    for item in values:
        if item is None:
            out.append(())
            continue
        try:
            array = np.asarray(item, dtype=float).ravel()
            if width is not None:
                array = array[:width]
            out.append(tuple(float(v) for v in array))
        except (TypeError, ValueError):
            out.append(())
    return tuple(out)


def _video_files(video_dir: Path) -> list[Path]:
    """列出视频目录下的 mp4（按名字排序，稳定可预期）。"""
    root = Path(video_dir)
    if not root.is_dir():
        return []
    return sorted(path for path in root.rglob("*.mp4") if path.is_file())


def videos_for_episode(root: Path, episode_index: int | None) -> list[Path]:
    """**这一集自己的**视频文件。取不到就返回空 —— 绝不猜。

    ★ 不能"把 videos/ 下的 mp4 都拿来用"：v3.0 导出把 52 集的视频全放在
    ``videos/chunk-000/<流名>/file-NNN.mp4`` 同一个目录里，随便取几个会拿
    别的集的画面去比对这一集的数据行数，得出荒谬的丢帧结论（实测出现过
    "视频 9164 帧 / 数据 819 行"）。

    两种布局各有映射来源:
      * canonical —— ``videos/<流名>/chunk-XXX/episode_XXXXXX.mp4``，
        目录本身就对应这一集；有 ``project_dataset.episode_files`` 时用它。
      * 导出产物 —— ``meta/episodes`` 里逐流记着 ``chunk_index``/``file_index``。
    """
    root = Path(root)
    if episode_index is None:
        # 一集一文件的布局：videos/ 下就是这一集的全部视频
        return _video_files(root / "videos")

    # canonical：走既有索引
    try:
        from app.project_dataset import episode_files
        entry = episode_files(root, episode_index) or {}
        found: list[Path] = []
        for item in entry.get("videos") or []:
            # episode_files 返回 (source_key, path) 的形式
            path = item[1] if isinstance(item, (tuple, list)) and len(item) > 1 else item
            candidate = Path(path)
            if candidate.is_file():
                found.append(candidate)
        if found:
            return found
    except Exception:
        pass

    # 导出产物：查 meta/episodes 的逐流索引
    return _videos_from_episode_meta(root, episode_index)


def _videos_from_episode_meta(root: Path, episode_index: int) -> list[Path]:
    """从 ``meta/episodes`` 的逐流 chunk/file index 拼出视频路径。"""
    import pyarrow.parquet as pq

    for path in sorted(glob.glob(str(root / "meta" / "episodes" / "**" / "*.parquet"),
                                 recursive=True)):
        try:
            frame = pq.ParquetFile(path).read().to_pandas()
        except Exception:
            continue
        if "episode_index" not in frame.columns:
            continue
        rows = frame[frame["episode_index"].values == episode_index]
        if rows.empty:
            continue

        # 列名形如 videos/<流名>/chunk_index 与 videos/<流名>/file_index
        columns = {str(name) for name in frame.columns}
        streams = sorted({
            name[len("videos/"):-len("/chunk_index")]
            for name in columns
            if name.startswith("videos/") and name.endswith("/chunk_index")
        })
        row = rows.iloc[0]
        found: list[Path] = []
        for stream in streams:
            try:
                chunk = int(row[f"videos/{stream}/chunk_index"])
                file_index = int(row[f"videos/{stream}/file_index"])
            except (KeyError, TypeError, ValueError):
                continue
            candidate = (root / "videos" / f"chunk-{chunk:03d}" / stream
                         / f"file-{file_index:03d}.mp4")
            if candidate.is_file():
                found.append(candidate)
        if found:
            return found
    return []


def _is_depth_key(name: str) -> bool:
    """这个流名是不是纯深度流。

    复用 ``lerobot_v21.is_depth_source`` —— 深度口径在这个仓库里有好几处实现
    （``device_naming.is_depth_only_key`` 是同一套），但权威定义在那儿。
    导入失败时回落到保守判断：名字含 depth 且不含 rgb/color。
    """
    try:
        from app.lerobot_v21 import is_depth_source
        return bool(is_depth_source(name))
    except Exception:
        low = str(name or "").lower()
        return "depth" in low and not any(
            token in low for token in ("rgb", "color", "video"))


# ── 主入口 ────────────────────────────────────────────────────

def gather_evidence(batch_dir: Path, *,
                    episode_index: int | None = None,
                    probe_video: bool = False,
                    modality_hint: str | Iterable[str] | None = None) -> Evidence:
    """读一个 episode 的全部证据。

    ``probe_video=False``（默认）只读 parquet，毫秒级；视频检查要解码抽样，
    一集几秒到几十秒，按需开。

    ``modality_hint`` 用于告诉采集层"这台设备是什么"（来自工作流连线的设备
    卡片）。**可以是多个** —— 一个质检节点上游同时连相机和夹爪时就有多个。
    给不出时按数据特征推断；两个来源取并集，理由见 ``_detect_modalities``。

    ★ ``batch_dir`` 必须是**单个 episode 的目录**，且 ``episode_index`` 必须
      与它匹配。``localstore.get_episode()`` 返回的 ``path`` 是**项目目录**
      （几十集共享），直接传进来会拿第 0 集的 parquet 去比全部集的视频
      —— 实测过，会得出"视频 9164 帧 / 数据 819 行"。见 ``videos_for_episode``。
    """
    root = Path(batch_dir)
    parquet, filter_rows = resolve_episode_data(root, episode_index)
    if parquet is None:
        return Evidence(batch_dir=root, notes={"reason": "no_parquet"})

    # ★ 按行过滤只在"一文件多集"的布局下做。canonical 是一集一文件，文件已经
    #   选对了，再按 episode_index 列筛会把这一集自己的行全筛空 —— 结果是不报
    #   任何错的 empty_report。见 resolve_episode_data。
    frame = load_rows(parquet, episode_index if filter_rows else None)
    if frame.empty:
        return Evidence(batch_dir=root, parquet=str(parquet.relative_to(root)),
                        notes={"reason": "empty_episode"})

    # 别名列先归一成协议名，再算 columns —— 必须在下面所有列名判断之前，
    # 否则 observation.tactile.* 会被当成"这列不存在"
    frame = _apply_column_aliases(frame)
    columns = {str(name) for name in frame.columns}
    fps = _detect_fps(root)
    rows = len(frame)
    streams: list[StreamEvidence] = []
    channels: set[str] = {"time"}

    def _relative(path: Path) -> str:
        try:
            return str(path.relative_to(root))
        except ValueError:
            return str(path)

    # SLAM —— **每个相机一路**，各取 trajectory 优先（见 _slam_columns）。
    # 这里曾经是 `break`：全局只登记一路，双目集第二路的 SLAM 从来没被采集过。
    for column in _slam_columns(columns):
        values = frame[column].tolist()
        series: dict[str, Any] = {}
        if column.endswith("trajectory"):
            counts = slam_valid_counts(values)
            series["valid_count"] = counts
            series["pose"] = slam_poses(values, counts)
            # 覆盖率必须走生产口径 —— "插值帧"的定义就是 poses_from_trajectory
            # 里本地与相邻格子都够不到的那些，只读原始数据推不出来。
            try:
                from app.umi_slam_action import poses_from_trajectory
                stamps = (frame["timestamp"].to_numpy(dtype=float)
                          if "timestamp" in columns
                          else np.arange(rows) / fps)
                diag: dict[str, Any] = {}
                poses_from_trajectory(values, stamps, rows, diagnostics=diag)
                series["coverage"] = diag
            except Exception as exc:
                series["coverage_error"] = str(exc)[:200]
        else:
            poses = vector_series(values, 7)
            series["valid_count"] = tuple(
                1 if pose_is_valid(item) else 0 for item in poses)
            series["pose"] = poses
        # 流名取列名最后一段 —— 多相机时是 "gripper_2_slam_trajectory"，
        # 单相机时是 "slam_trajectory"，两条流不会撞名。
        streams.append(StreamEvidence(
            key=column.rsplit(".", 1)[-1],
            channel="slam", path=_relative(parquet),
            columns=(column,), rows=rows, fps=fps, series=series,
        ))
        channels.add("slam")

    # 其余标量/向量列
    #
    # ★ series 的键一律用【信道名】。检查项按信道取序列（``series["force"]``），
    #   如果这里用列名（``series["gripper_left_force"]``），检查项就静默地取不到
    #   数据、全部判通过 —— 而且不报任何错。左右两路力共用 ``force`` 键，
    #   靠 stream.key 区分。
    for channel, candidates in _COLUMN_CHANNELS.items():
        if channel == "slam":
            continue
        for column in candidates:
            if column not in columns:
                continue
            streams.append(StreamEvidence(
                key=column.rsplit(".", 1)[-1], channel=channel,
                path=_relative(parquet), columns=(column,), rows=rows, fps=fps,
                # 走 _column_series：宽度取不到就**不截断**（兜底成某个数字会
                # 静默丢数据），字符串列原样保留（数值化会变成全空元组）
                series={channel: _column_series(frame[column], channel)},
            ))
            channels.add(channel)

    # 视频 —— ★ 无论检不检查，都要登记成一条流。
    #
    # 只登记不检查时 ``video`` 是空 dict（视频检查项会自行跳过），但信道
    # ``rgb`` 必须出现 —— 否则报告会写成"未采集 rgb"，而实际上视频就在那儿，
    # 只是这一轮没去解码。那会让"训练用途"给出错误的结论。
    video_root = root / "videos"
    for path in videos_for_episode(root, episode_index)[:_MAX_VIDEO_PROBES]:
        report: dict[str, Any] = {}
        if probe_video:
            try:
                from app.video_quality import _check_stream
                report = _check_stream(path, rows, fps)
            except Exception as exc:
                report = {"passed": False, "reason": "video_check_failed",
                          "error": str(exc)[:200]}
        # 流名 = 流名目录那一层（observation.images.xxx），**不是** chunk-XXX。
        # 两种布局的层级顺序相反，所以不能写死取第几段：
        #   canonical/v2.1  videos/<流名>/chunk-000/episode_000000.mp4
        #   v3.0            videos/chunk-000/<流名>/file-000.mp4
        # 取"路径里第一个不是 chunk-* 的目录段"，两边都成立。
        # 写错的话所有视频流会同名（都叫 chunk-000），报告分不清哪路相机，
        # 且下游按 stream 去重时会互相覆盖。
        try:
            relative = str(path.relative_to(root))
            segments = path.relative_to(video_root).parts[:-1]   # 去掉文件名
            key = next((seg for seg in segments
                        if not seg.startswith("chunk-")), path.stem)
        except ValueError:
            relative, key = str(path), path.stem

        # ★ 深度流不能算作 rgb 信道。之前一律按 rgb 登记，结果 D435 项目的
        # 深度视频引出了"视频 9164 帧 / 数据 819 行"这种荒谬的丢帧结论 ——
        # 那是拿深度流跟 rgb 数据行数比。判定复用 lerobot_v21 的既有口径，
        # 不另起一套字符串匹配。
        channel = "depth" if _is_depth_key(key) else "rgb"
        streams.append(StreamEvidence(
            key=key, channel=channel, path=relative,
            rows=rows, fps=fps, video=report,
        ))
        channels.add(channel)

    modalities = _detect_modalities(channels, modality_hint)
    return Evidence(
        batch_dir=root, streams=tuple(streams), channels=frozenset(channels),
        modalities=modalities, fps=fps, rows=rows,
        columns=tuple(sorted(columns)), parquet=_relative(parquet),
    )


def _detect_fps(root: Path) -> float:
    """从 meta/info.json 取 fps；拿不到就用默认 30。"""
    import json

    for relative in ("meta/info.json", "info.json"):
        candidate = root / relative
        if not candidate.is_file():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
            value = float(data.get("fps") or 0)
            if 0 < value < 1000:
                return value
        except Exception:
            continue
    return _DEFAULT_FPS


def hint_values(hint: str | Iterable[str] | None) -> list[str]:
    """把 hint 归一成字符串列表 —— 调用方有单数（旧调用）也有复数（工作流连线）。"""
    if hint is None:
        return []
    if isinstance(hint, str):
        return [hint] if hint.strip() else []
    try:
        return [str(item) for item in hint]
    except TypeError:
        return [str(hint)]


def _infer_modalities(channels: set[str]) -> list[str]:
    """只看数据特征能推断出什么。

    ★ 永远给不出 ``stereo_rgb`` / ``stereo_rgbd_camera`` —— "立体"不是列名里
      能看见的特征。这两个模态**只能**由工作流连线给出。所以它们是
      ``_detect_modalities`` 取并集、而不是二选一的主要理由。
    """
    if "slam" in channels or "gripper_state" in channels:
        return ["gripper_device"]
    if "tactile" in channels:
        return ["glove_sensor"]
    if "depth" in channels:
        return ["rgbd_camera"]
    if "rgb" in channels:
        return ["mono_rgb"]
    return []


def _detect_modalities(channels: set[str],
                       hint: str | Iterable[str] | None) -> tuple[str, ...]:
    """这台设备是什么 —— hint（工作流连线）与数据特征取**并集**。

    两个来源各有各的盲区，缺一个都会让检查静默不跑：

      只有 hint  → 按数据模态声明的检查项不跑（将来接 ``depth.*`` 时，
                   D435 数据在但模态被写成 stereo_rgb 就查不到）
      只有推断   → 工作流里那张卡片的 tab 键查不到，**用户在设置面板里配的
                   阈值被静默忽略、一律走默认值**（推断不出 stereo_*）

    并集对"配置查找"和"检查项筛除"都更宽 —— 而本项目反复踩的坑恰恰是
    "检查项静默不跑"，所以宁可宽。

    hint 在前：``engine.resolve_config`` 按这个顺序合并各设备卡片的配置，
    "先出现的优先"，工作流连线的顺序才是用户预期的顺序。
    """
    from app.processing.cleaning.contract import DEVICE_MODALITIES

    ordered: dict[str, None] = {}
    for raw in hint_values(hint):
        canonical = normalize_device_modality(raw)
        if canonical in DEVICE_MODALITIES:
            ordered.setdefault(canonical, None)
    for modality in _infer_modalities(channels):
        ordered.setdefault(modality, None)
    return tuple(ordered)


def channels_from_modalities(modalities) -> tuple[str, ...]:
    """设备模态 → 该设备可能提供的信道（转调 contract，供 engine 用）。"""
    return channels_for(modalities)
