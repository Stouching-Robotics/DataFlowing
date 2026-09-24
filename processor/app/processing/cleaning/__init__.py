"""数据清洗 —— 按数据类型检查 episode 质量，异常退回人工审核。

对应《以 Episode 为单位的数据清洗方案》(PDF)。核心设计:

* **按信道声明需求**，不按"这是 UMI 还是 EGO" —— 检查项自己声明需要哪些信道，
  实际有哪些信道由数据决定，两者匹配上才跑（见 ``checks.applicable_checks``）。
* **四层结果** —— 数据流 / 时间区间 / episode / 训练用途（见 ``report.build_report``）。
* **只出结论，不产出数据** —— 原始数据一字不改，也不生成 clean_v1 之类的副本。
  区间信息完整保留在报告里，下游按 ``training_use`` 过滤即可。
* **可插拔** —— 新增检查项 = 在 ``checks/`` 下放一个 .py（与 processing/modules 同款）。

这个包的算法实现与工作流适配器是分开的，与 ``app/processing/black_glove`` 同构:

    app/processing/cleaning/               ← 本包：契约 + 检查项 + 报告组装
    app/processing/modules/data_cleaning.py ← 工作流节点（artifact 契约 + 端口 + 配置）
"""

from app.processing.cleaning.contract import (
    DEVICE_MODALITIES, CAMERA_MODALITIES, CHANNELS, MODALITY_CHANNELS,
    PASS, WARN, FAIL, PENDING, ERROR,
    SEVERITY_ORDER, SEVERITY_LABELS,
    Finding, Range, StreamResult,
    channels_for, normalize_device_modality, worst, is_blocking,
)
from app.processing.cleaning.report import (
    SCHEMA_VERSION, TRAINING_USE_RULES,
    build_report, build_timeline, rollup_episode, rollup_training_use,
    empty_report, passed_for,
)

__all__ = [
    "DEVICE_MODALITIES", "CAMERA_MODALITIES", "CHANNELS", "MODALITY_CHANNELS",
    "PASS", "WARN", "FAIL", "PENDING", "ERROR",
    "SEVERITY_ORDER", "SEVERITY_LABELS",
    "Finding", "Range", "StreamResult",
    "channels_for", "normalize_device_modality", "worst", "is_blocking",
    "SCHEMA_VERSION", "TRAINING_USE_RULES",
    "build_report", "build_timeline", "rollup_episode", "rollup_training_use",
    "empty_report", "passed_for",
]
