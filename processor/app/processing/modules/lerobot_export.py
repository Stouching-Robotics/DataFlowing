"""LeRobot Export — 把批次构建为标准 LeRobot v3 数据集。

真正导出由 app.lerobot_export.build_lerobot_dataset 完成(视频/深度/
传感器/标注 → meta/info.json + 分块 parquet)。本模块把该实现接入
工作流节点:跑完工作流,数据集先生成到 worker 临时目录，完成后交给系统级
导出缓存；不在项目根目录生成 processed/，也不把导出副本混入源数据集。
"""

from app.processing import ProcessingModule, JobContext, ArtifactRef, field
from app.processing.registry import register
from app.processing.theme import EXPORT_COLOR


@register
class LeRobotExportModule(ProcessingModule):
    slug = "lerobot_export"
    version = "1.0"
    category = "export"
    label = "LeRobot Export"
    icon = "ant-design:cloud-server-outlined"
    color = EXPORT_COLOR
    inputs = ({"key": "data", "label": "Exportable Data"},)
    outputs = ({"key": "dataset", "label": "Dataset"},)
    # 默认排除 UMI 触觉力矩阵:每帧 250×250×3(187500 个整数),实测占
    # canonical parquet 体积的九成以上,且 99.99% 是零 —— 训练用不到,
    # 却让单集导出从 2.5 秒涨到 57 秒(写盘 + 逐元素统计 + 结果包上传),
    # 结果 zip 也从几 MB 涨到几十 MB。要保留就把它从配置里删掉。
    _DEFAULT_EXCLUDE = ",".join((
        "observation.gripper_left_force_matrix",
        "observation.gripper_right_force_matrix",
    ))
    default_config = {"version": "v3.0", "split_ratio": 0.9,
                      "shard_size": 100000, "exclude_columns": _DEFAULT_EXCLUDE}
    config_schema = (
        field("version", "select", "Version", "v3.0", options=["v2.1", "v3.0"]),
        field("exclude_columns", "string", "Exclude columns (comma separated)",
              _DEFAULT_EXCLUDE),
    )
    execution_target = "worker"
    capabilities = ("dataset_export", "lerobot")

    def run(self, ctx: JobContext) -> dict[str, ArtifactRef] | None:
        if not ctx.incoming:
            ctx.skip("No upstream data — skipped")
        episode_id = str(ctx.job.get("episode_id") or "")
        if not episode_id:
            ctx.skip("No episode_id in job")

        from app.lerobot_export import build_lerobot_dataset
        split_ratio = float(ctx.config.get("split_ratio", 0.9))
        # 布局版本:config_schema 的 version 选项(v2.1/v3.0)直接驱动
        # 导出版本由卡片配置决定；源项目始终保持本地 canonical v2.1
        # 三目录结构，v3.0 只在系统级导出缓存中生成。
        version = str(ctx.config.get("version") or "v3.0")
        # 导出范围由工作流连接决定:输入卡片(视频)连到本节点 → 数据集
        # 包含该视频;没连的相机 → 默认不导出视频(传感器/标注仍全量)。
        include_video_keys = [
            r.source_key for r in ctx.incoming.values()
            if r.kind == "video" and r.source_key
        ]
        # 手部骨骼产物:处理节点(mediapipe_hand)连到本节点 → 一并写入
        # 数据集(连接驱动);没连 → 不写入。
        # 注意必须 resolve:ref.path 是相对 output_root 的,直接传给
        # build_lerobot_dataset 会因工作目录不同而读不到文件。
        hand_keypoints_paths = [
            str(p) for r in ctx.incoming.values()
            if r.kind == "hand_keypoints" and r.path
            for p in (ctx.resolve(r),) if p
        ]
        # 双目三角化产物(stereo_triangulate 连到本节点)→ hand_*_world
        # 变为米制 3D 坐标(优先于 2D 归一化坐标)。
        hand_3d_paths = [
            str(p) for r in ctx.incoming.values()
            if r.kind == "hand_3d" and r.path
            for p in (ctx.resolve(r),) if p
        ]
        # 右目(辅助视角)骨骼产物(stereo_triangulate 的 hand_3d#right
        # "#" 兄弟键,沿边自动透传)连到本节点 → 以 _rcam 后缀列组写入
        # 数据集(与主数据并存)。
        hand_3d_right_paths = [
            str(p) for r in ctx.incoming.values()
            if r.kind == "hand_3d#right" and r.path
            for p in (ctx.resolve(r),) if p
        ]
        # hand_3d 产物的 unit 语义(manifest 声明,如深度图抬升的 camera_meters)
        # → 透传给数据集 feature 声明(缺省保持 mediapipe_world_relative)。
        hand_3d_unit = next(
            (r.metadata.get("unit") for r in ctx.incoming.values()
             if r.kind == "hand_3d" and getattr(r, "metadata", None)
             and r.metadata.get("unit")),
            None)
        # 数据集先输出到 worker 临时目录，API 完成阶段会把导出产品放到
        # state/exports，不在项目根目录新增 processed/。
        # 排除列:配置里逗号分隔。留空 = 全量导出(旧行为)。
        exclude_columns = [
            name.strip() for name
            in str(ctx.config.get("exclude_columns", "") or "").split(",")
            if name.strip()
        ]
        dataset_dir = build_lerobot_dataset(episode_id, [episode_id],
                                            ctx.output_root, split_ratio,
                                            include_video_keys=include_video_keys,
                                            hand_keypoints_paths=hand_keypoints_paths,
                                            hand_3d_paths=hand_3d_paths,
                                            hand_3d_right_paths=hand_3d_right_paths,
                                            version=version,
                                            hand_3d_unit=hand_3d_unit,
                                            exclude_columns=exclude_columns)
        info = dataset_dir / "meta" / "info.json"
        if not info.exists():
            ctx.skip("Dataset build produced no meta/info.json")
        return {
            "dataset": ctx.ref("dataset", info, metadata={
                "root": str(dataset_dir.relative_to(ctx.output_root)) if dataset_dir != ctx.output_root else ".",
                "episode_id": episode_id,
            }),
        }
