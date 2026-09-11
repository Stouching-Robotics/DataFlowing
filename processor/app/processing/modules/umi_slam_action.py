"""UMI Slam Action — 从 SLAM 位姿派生 imitation learning 的 action 列。

UMI 采集端不录 action(写的是占位全零,导出时被恒定列过滤器剔除),
导致数据集无法训练。本模块读取 UMI 夹爪的 SLAM 位姿与夹爪开合,
按 UMI 官方口径反推出 7 维 action 并合并回 canonical:

    Δp = R_tᵀ · (p_{t+1} − p_t)       # 平移增量, 米
    Δr = rotvec(R_tᵀ · R_{t+1})       # 旋转增量, 弧度
    g  = gripper_state[t+1, 0] / 100  # 夹爪开合, 0–1

纯计算在 app/umi_slam_action.py(不依赖框架,便于单测与跨格式复用)。
"""

from pathlib import Path

from app.processing import ProcessingModule, JobContext, ArtifactRef
from app.processing.registry import register
from app.processing.theme import HAND3D_COLOR

# 本模块产出的 artifact kind。合并白名单在 project_dataset 里单独维护,
# 新增 kind 必须同步加进那张表,否则列不会被并入 canonical。
ACTION_KIND = "umi_slam_action"

# 位姿只读 slam_trajectory:它是采集端的高频样本缓冲,丢帧时相邻格子里
# 仍有真实样本;而 slam_pose 在同样位置直接写零,只能插值硬补。
_TRAJECTORY_COLUMN = "observation.slam_trajectory"
_TIMESTAMP_COLUMN = "timestamp"
_GRIPPER_COLUMN = "observation.gripper_state"


@register
class UmiSlamActionModule(ProcessingModule):
    slug = ACTION_KIND
    version = "1.0"
    category = "process"
    label = "UMI Slam Action"
    # 语义是"位姿 → 增量"的坐标变换,与同组做几何变换的手部模块一致;
    # sync 也贴合"由轨迹反推动作"。必须取自 iconify-preload.js 的白名单
    # (图标离线预载,不在表内的名字渲染为空白)。
    icon = "ant-design:sync-outlined"
    color = HAND3D_COLOR
    # 端口 key 必须与上游 UMI Gripper 的输出 key 逐字一致 —— 前端对
    # 类型化端口按 ``out.key === inp.key`` 校验,改名会导致连线被拒。
    inputs = (
        {"key": "slam_trajectory", "label": "SLAM Trajectory"},
        {"key": "gripper_state", "label": "ESP Gripper State"},
    )
    outputs = ({"key": "action", "label": "Action"},)
    default_config = {}
    config_schema = ()
    execution_target = "worker"
    capabilities = ("umi_gripper", "action_derivation")

    def run(self, ctx: JobContext) -> dict[str, ArtifactRef] | None:
        parquet = self._find_source_parquet(ctx)
        if parquet is None:
            ctx.skip("No parquet with SLAM pose found in batch")

        from app.umi_slam_action import derive_action
        import time
        import numpy as np
        import pandas as pd

        # 读取重试:采集端可能还在往同一项目目录写新集(每上传一集都会重写
        # 项目数据树),worker 恰好读到写了一半的文件就会抛异常。实测
        # ep52/ep71 都因此被误判为"列不可读"而跳过 —— 几秒后同一文件完全
        # 正常。瞬时故障不该等同于永久失败,故重试若干次。
        columns = [_TRAJECTORY_COLUMN, _TIMESTAMP_COLUMN, _GRIPPER_COLUMN]
        frame = None
        last_error: Exception | None = None
        for attempt in range(5):
            try:
                frame = pd.read_parquet(parquet, columns=columns)
                break
            except Exception as exc:
                last_error = exc
                time.sleep(1.0 + attempt)
        if frame is None:
            # 带上路径与大小:同样的 ArrowInvalid 可能来自「文件被截断」或
            # 「根本不是 parquet」,只看异常分不出来,必须知道读的是哪个文件。
            try:
                size = parquet.stat().st_size
                head = parquet.open("rb").read(4)
                detail = f"size={size}B head={head!r}"
            except OSError as exc:
                detail = f"stat failed: {exc}"
            ctx.skip(f"SLAM columns unreadable after retries: "
                     f"{type(last_error).__name__}: {last_error} | "
                     f"file={parquet} ({detail})")

        if _TRAJECTORY_COLUMN not in frame.columns:
            # 非 UMI 批次(如 D435 手套、S80C)没有 SLAM 缓冲 —— 跳过,
            # 不写入任何列,保证其他设备的导出行为完全不变。
            ctx.skip("No SLAM trajectory column — not a UMI gripper batch")

        total = len(frame)
        stamps = (frame[_TIMESTAMP_COLUMN].to_numpy(dtype=float)
                  if _TIMESTAMP_COLUMN in frame.columns else np.arange(total) / 30.0)
        derived = derive_action(
            frame[_TRAJECTORY_COLUMN].tolist(),
            stamps,
            frame[_GRIPPER_COLUMN].tolist()
            if _GRIPPER_COLUMN in frame.columns else [],
            total,
        )
        if not derived:
            ctx.skip("No usable SLAM samples (all frames dropped)")

        # 输出按原 frame_index 对齐并保持升序:_merge_processing_frame 按
        # frame_index 匹配,乱序会让列错位。
        frames = sorted(derived)
        ctx.progress(0.5)
        output = ctx.output_dir / "umi_slam_action.parquet"
        # 必须写 np.float32:pyarrow 对 Python float 推断 double,而 LeRobot
        # 的 action 声明是 float32 —— 声明与实际 schema 不一致会让官方加载器
        # 读出不匹配的类型。与导出端 _cast_float32 的做法保持一致。
        pd.DataFrame({
            "frame_index": [int(f) for f in frames],
            "action": [[np.float32(value) for value in derived[f]["action"]]
                       for f in frames],
            # ACT 的观测需要 proprioception:只有图像时模型不知道夹爪当前
            # 在哪。这里的 observation.state 是相对本集起点的位置 + 夹爪开合。
            "observation.state": [
                [np.float32(value) for value in derived[f]["observation.state"]]
                for f in frames],
        }).to_parquet(output, index=False)
        ctx.progress(1.0)

        covered = len(frames)
        return {
            "action": ctx.ref(ACTION_KIND, output, metadata={
                "column": "action",
                "shape": [7],
                "names": ["delta_x", "delta_y", "delta_z",
                          "rot_x", "rot_y", "rot_z", "gripper"],
                "frames_covered": covered,
                "frames_total": int(total),
                "note": ("UMI 口径 action:当前夹爪局部系增量位姿 + 夹爪开合。"
                         "由 SLAM 位姿派生,零位姿丢帧已插值修复。"),
            }),
        }

    @staticmethod
    def _find_source_parquet(ctx: JobContext) -> Path | None:
        """定位含 SLAM 位姿的 canonical parquet。

        优先用上游 ``slam_trajectory`` 端口连过来的产物(工作流连接驱动);
        未连或产物不可用时回退到 ``ctx.find_parquet()`` 扫描批次,
        保证"只连一根线也能跑"。

        上游 ref **必须验证是 parquet**:worker 组装 incoming 时,若边的
        sourceHandle 对不上上游任何输出端口,会把该节点的**全部输出平铺**
        进来(runner `_incoming_artifacts` 规则 3),视频 ref 也会挂到
        ``slam_trajectory`` 这个键上。实测因此把 mp4 当 parquet 读,报
        ``magic bytes not found in footer`` —— 只看键名不验证就会中招。

        回退扫描**必须按 episode_id 过滤**:批次目录里往往躺着几十集的
        canonical parquet,直接取第一个会静默用错集的数据(实测表现为
        每集算出的 action 完全相同)。worker 打包输入时通常只含一集,
        但本地重跑/整目录传入时就不是了。
        """
        import pyarrow.parquet as pq

        def _is_slam_parquet(candidate: Path) -> bool:
            if candidate.suffix.lower() != ".parquet" or not candidate.is_file():
                return False
            try:
                return _TRAJECTORY_COLUMN in set(pq.read_schema(candidate).names)
            except Exception:
                return False

        for key, ref in (ctx.incoming or {}).items():
            if not str(key).startswith("slam_trajectory"):
                continue
            resolved = ctx.resolve(ref)
            if resolved and _is_slam_parquet(resolved):
                return resolved

        episode_id = str((ctx.job or {}).get("episode_id") or "")
        suffix = ""
        if "_" in episode_id:
            tail = episode_id.rsplit("_", 1)[-1]
            if tail.isdigit():
                suffix = f"episode_{int(tail):06d}.parquet"
        candidates = ctx.find_parquet()
        # 先只看文件名匹配本集的那一个
        ordered = ([c for c in candidates if c.name == suffix] if suffix else [])
        ordered += [c for c in candidates if c.name != suffix]
        for candidate in ordered:
            if _is_slam_parquet(candidate):
                return candidate
        return None
