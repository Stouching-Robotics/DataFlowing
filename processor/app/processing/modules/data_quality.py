"""Data Cleaning —— 数据清洗（质检）：一个节点统管确定性与 AI 两类检查。

显示名是 Data Cleaning；slug 仍是 ``data_quality``（持久化契约，改它会让已保存
工作流的 nodeType 对不上）。两者不一致是有意的 —— 见下方端口 key 的同类说明。

本节点由两个旧节点合并而来，两者功能高度重叠：

    ai_quality_review  视频检查(黑屏/冻结/丢帧/解码) + AI 标注覆盖率
    data_cleaning      视频检查 + UMI/手套/EGO 检查（第 2 期接入）

历史原因：视频检查最早"寄居"在 ``ai_quality_review`` 里，好让纯视频工作流
也有质量门禁（见 ``ai_annotation.py`` 的 ``video_quality_review_node_config``
docstring）。现在合并成一张卡片，语义与配置都归一处。

**两个旧 slug 都作为别名保留**（``workflow_types.LEGACY_TO_CANONICAL``），
已保存的工作流打开时自动迁移，连线不受影响 —— 端口的 key 刻意保持不变：

    输入  data      所有旧连线都用它
    输出  reviewed  旧连线用它接 lerobot_export

检查分两层，与清洗方案 PDF §4 的两栏一一对应：

    机器可直接判断（确定性、免费、可每集跑）
      ├─ 视频：解码 / 黑屏 / 冻结 / 丢帧
      ├─ UMI：帧对齐 / action / 夹爪量程 / SLAM 连续性
      ├─ 手套：全零 / 死点 / 漂移 / 饱和
      └─ EGO：曝光 / 关键点缺失 / 左右手

    需联合判断或复核（有成本、可选）
      └─ AI 标注覆盖率；将来的 VLM 语义复核

检查项的算法实现与工作流适配器分开，与 ``app/processing/black_glove`` 同构：

    app/processing/cleaning/                ← 契约 + 检查项 + 报告组装
    app/processing/modules/data_quality.py  ← 本文件：artifact 契约 + 端口 + 配置

新增检查项 = 在 ``app/processing/cleaning/checks/`` 下放一个 .py，本文件不用改。

**只出结论，不产出数据** —— 原始数据一字不改，也不生成 clean_v1 之类的副本。
区间信息完整保留在报告的 ``ranges`` 里，下游按 ``training_use`` 过滤即可。

数据不对时**卡在人工审核**：判 FAIL/ERROR 的批次会被推回 ``to_review``
（就是审核页的入口），并在 ``cleaning_summary.blocking`` 上留一个粘性标记，
让 AI 标注 / 视频门禁的自动批准路径让路。WARN 只记录、不拦。
"""

from __future__ import annotations

from app.processing import ProcessingModule, JobContext, ArtifactRef
from app.processing.registry import register
from app.processing.theme import REVIEW_COLOR


@register
class DataQualityModule(ProcessingModule):
    slug = "data_quality"
    version = "1.0"
    category = "review"
    label = "Data Cleaning"
    # 必须取自 web/static/iconify-preload.js 的白名单 —— 图标是离线预载的,
    # 不在表内的名字 iconify-icon 会去 api.iconify.design 拉,调色板一次渲染
    # 全部节点,一个漏网图标就能让整块面板卡住。check-circle 表示"校验",
    # 与同组 review 节点的 eye(human) 不冲突。
    # 改图标后请跑 scripts/check_icon_preload.sh。
    icon = "ant-design:check-circle-outlined"
    color = REVIEW_COLOR

    # 端口 key 是持久化契约 —— 取值必须与合并前的 ai_quality_review 逐字一致,
    # 否则已保存工作流的连线会因 sourceHandle/targetHandle 对不上而失效。
    # 详见 workflow_types.py 里前端对 ``out.key === inp.key`` 的校验。
    inputs = ({"key": "data", "label": "Data"},)
    outputs = ({"key": "reviewed", "label": "Reviewed Data"},)

    # 配置结构（前端设置弹窗按设备卡片写入）::
    #
    #     {"devices": {
    #        "gripper_device": {
    #          "checks": {
    #            "umi.slam_continuity": {"enabled": true,
    #                                    "params": {"max_linear_mps": 3.0}},
    #            "video.freeze":       {"enabled": false}
    #          }}}}
    #
    # 引擎侧由 ``cleaning.engine.resolve_config()`` 解析。
    default_config = {"devices": {}}

    # ★ 刻意留空 —— 配置全部走专用弹窗（DeviceQualityModal），不走通用
    #   config_schema 渲染。
    #
    #   通用那套是**平铺字段列表**，表达不了"按设备卡片分组 + 每项带阈值"的
    #   嵌套结构；硬塞会变成一长串 enable_xxx 开关。曾短暂用过那套，结果是
    #   三个阈值在节点里和检查项里各存一份（frame_tolerance 与
    #   video.frame_drop.tolerance_ratio 同义），迟早不一致。
    #
    #   现在阈值只有一处定义：检查项的 ``default_params``；节点只存"覆盖值"。
    config_schema = ()

    # 真实检查在 run 完成回调里跑（那时才能读到合并后的 episode 数据），
    # 与 human_review / 旧 ai_quality_review 的分工一致。
    # 注：该字段当前无消费方，仅作语义标注（全仓 grep 只见 schema 与文档）。
    execution_target = "server"
    capabilities = ("data_quality", "quality_gate", "quality_review")

    def run(self, ctx: JobContext) -> dict[str, ArtifactRef] | None:
        """透传上游 artifact。上游没连 → skipped。

        **真正的检查不在 ``run()`` 里跑** —— 在 worker 的 run 完成回调里
        （``api/worker.py`` 调 ``cleaning.trigger.spawn_cleaning_for_run``）。

        为什么不在 ``run()``：DAG 执行期 worker 是在 staging 目录里干活、
        产物要等 ``_publish_episode_result`` 才合并回批次目录。在这一步读
        批次目录，拿到的是**上一轮**的数据 —— 尤其 ``action`` 列，它由
        ``umi_slam_action`` 派生产出，读早了会全是采集端的零占位。
        完成回调时数据已经合并完毕，与 ``human_review`` / 旧
        ``ai_quality_review`` 的分工一致。

        检查所需的数据**不靠端口传** —— 完成回调手上就有
        ``localstore.get_episode()`` 的记录（``path`` + ``episode_index``），
        检查项直接读批次目录。

        ★ 进来的是 ``episode_index`` 不是可选的：``path`` 是**项目目录**，
          几十集共享，漏传会拿第 0 集的数据去比全部集的视频。
        """
        if not ctx.incoming:
            ctx.skip("No upstream data — skipped")
        return dict(ctx.incoming)
