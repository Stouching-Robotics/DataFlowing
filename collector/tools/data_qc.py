#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""录制数据集**时间轴质检器**（只读、可复用）：五项基础检查 + 结构一致性 + 自检。

为什么有这个东西
----------------
录制链路上有四个独立时钟（相机设备钟、IMU 设备钟、手套设备钟、宿主钟），
每个时钟各自盖戳，再由写线程把「变长样本列表」挂到 30fps 的帧行上。任何一环
打嗝，落盘数据都会留下**时间轴缺陷**，而且多数缺陷**看数值看不出来**（形状、
量纲、列名全对），只有在时间轴上才暴露。本工具把这类缺陷一次数清楚，作为
「这段数据能不能用」的客观判据，也可作为修数据前后的回归对照。

五项基础检查（用户口径 → 本工具实现）
--------------------------------------
============  ==========================  ============================================
编号           名称                        判据
============  ==========================  ============================================
C1            时间戳回退                  相邻样本 d = v[i+1] − v[i] < 0（严格单调被破）。
                                          分「行内回退」（同一行列表内）与「跨行回退」
                                          （上一行末样本 > 下一行首样本）。
                                          绕回（2^32/2^64 计数器翻转）自动识别并单列，
                                          不算回退。
                                          **样本流（见下）先剔除 0 占位行再判**：
                                          0 = 「本行没有样本」，不是时刻 0。
C2            时间戳重复                  d == 0（容差 = 该列量化步长 quantum_ns）。
                                          分「行内重复」（同一行列表内两个相同戳，恒为缺陷）
                                          与「跨行重复」（同一样本被挂进两行，可能是重报）。
C3            时间戳跳变                  d 明显大于标称周期（> gap_factor × 标称 且超出
                                          min_excess_ms）⇒ 空洞；d 明显小于标称周期
                                          ⇒ 压缩（攒批突发）。给出缺口毫秒数、相对标称
                                          的倍数、以及「折算丢了几个样本」。
                                          标称周期：声明率优先，否则取**平均**间隔
                                          （攒批流的间隔是双峰，中位数会落到短的那
                                          一峰，把正常攒批读成丢样本）；间隔呈攒批
                                          （p95 ≥ 1.5×中位）时空洞还须超过 1.5×p95。
C4            不同数据帧数不齐            七项子检查：meta 声明长度 vs 实际行数、frame_index
                                          连续性、各流「空行」计数、实测采样率 vs 声明
                                          rate_hz、**成对的 ts 列与数值列存在性是否对齐**、
                                          各流样本总数是否互相对得上（左/右手套、
                                          IMU、SLAM 两两比）、**与 meta/episodes 的
                                          drop_stats 自报计数对账**（本工具独立数出来的
                                          空行/样本数 vs 录制器自己记的账）。
C5            不同时间戳有偏移            ① 每个流相对**同域参考帧列**的偏移（中位数、IQR、
                                          漂移 ppm、分段台阶），与基线（info.json 的
                                          alignment 或配置）比；② 同域流两两偏移（如左手套
                                          vs 右手套失同步）；③ 帧级时间列两两之间的偏移
                                          **稳定性**（跨时钟纪元时刻只判稳定性，不判绝对值，
                                          因为不同设备纪元的绝对差没有意义）。
                                          样本流不比「两个钟的差」——它是每行一个采样值，
                                          与帧戳本来就不同刻。它比的是④**行号配对偏移**
                                          （样本戳 − 本行帧戳）：中位/IQR 报 info，
                                          只有不稳定（IQR 超阈值）或台阶才升 warn/error。
============  ==========================  ============================================

三条流速的分类（决定上面五项各按什么口径判）
    帧级 frame   每行一个时刻、且**就是这个行自己的时刻**（timestamp / wall_time /
                 hardware_ns / raw_hw_ns / host_ns / depth_ns / frame_index）。
                 五项全查，严格单调是硬要求。
    列表 list    一行挂多个样本的变长戳（手套 ts、imu_ts、slam_trajectory_ns）。
    样本 sample  每行**至多一个**采样值、但采样时刻与行时刻无关的标量 *_ns 列
                 （夹爪力的 *_force_ns / *_force_matrix_ns，info.json 里
                 encoding=host_monotonic_ns）。0（sample_missing_value）= 本行没有
                 样本，属正常形状（采样相位与 30fps 行网格不同步）；剔除 0 后再判
                 回退/重复，缺样本只按覆盖率报，且比的是「行号配对偏移」而不是
                 「两个钟的差」。旧口径（一律当帧级时钟）会把 7%~15% 的 0 读成
                 「时间戳回退」，这正是本仓库 UMI 数据集上 1800+ 条误报的来源。

另附 C6 结构一致性（数据集级）：episode 编号公式 N = c × chunks_size + f + 1、重号、
data 与 meta 的 episode_index 是否一致。这些不属于时间轴，但会让上面五项整体失真。
**读不动的段不再记成 C6 error**：稀疏列契约下本段没有的那一列按「该流 0 样本」报 C4，
不因为 `pq.read_table` 取列失败就把整段判死（那会让该段的五项完全没查过）。

怎么复用（这是重点）
--------------------
1. **零配置可用**：`meta/info.json` 存在时，流的清单、时钟域、标称采样率、配对列
   全部从 `features[].rate_hz / clock / ts_key` 推出来；info.json 缺失时按
   「列类型 + 列名」自动发现（list<整数> 且名字含 ts/_ns ⇒ 时间戳流）。
2. **配置只覆盖需要覆盖的**：`--emit-config` 把推断结果落成一份 JSON，改哪项留哪项，
   下次 `--config` 传进来即可（深度合并，未写的项仍走自动推断）。
3. **不依赖本仓库**：只 import numpy + pyarrow，不 import core/、不写任何文件
   （除非显式 `--out-dir`），可以直接拷到别的项目/服务器上跑。
4. **机器可读**：`--json PATH` 出完整结构化报告，退出码 0/1/2 可直接接 CI。
5. **自带自检**：`--selftest` 现场合成一份注入了五类缺陷的数据集，逐项断言
   「该报的报了、干净段没误报」。改了判据先跑它。

用法
----
    venv/bin/python tools/data_qc.py <数据集目录>                 # 只读体检，打印报告
    venv/bin/python tools/data_qc.py <数据集目录> --json qc.json  # 另存机器可读报告
    venv/bin/python tools/data_qc.py <数据集目录> --list-streams  # 只看推断出的流模型
    venv/bin/python tools/data_qc.py <数据集目录> --emit-config my.json   # 导出可改的配置
    venv/bin/python tools/data_qc.py --selftest                  # 判据自检（不碰真实数据）

    # 出 PDF 报告（版式对齐客户给的《1013视塔手套数据质检.pdf》）
    venv/bin/python tools/data_qc.py <数据集目录> --pdf 报告.pdf
    venv/bin/python tools/data_qc.py <目录A> <目录B> --pdf 报告.pdf   # 多数据集合并成一份

常用选项
    --config F.json   配置（深度合并到默认值）
    --episodes 000,001 只查指定段（按文件名三位号）
    --jobs N          并行进程数（默认 min(4, CPU)）
    --jobs 1          串行（便于调试）
    --check-video     额外用 ffprobe 比「视频帧数 vs parquet 行数」（默认关，慢）
    --strict          警告也按失败退出
    --out-dir DIR     同时落 report.json / report.txt（多数据集时各占一个子目录）
    --top N           每类问题最多列几条样例（默认 8）

PDF 报告选项
    --pdf PATH        出 PDF；报告含 1 结构信息 / 2 质检结果 / 3 结论与建议
    --pdf-title T     标题（默认「数据质检报告」）
    --pdf-date D      报告日期 YYYY-MM-DD（默认今天）
    --pdf-html PATH   顺带保留中间 HTML（改版式/改文字后用浏览器重导更快）
    --chrome PATH     指定 chrome 可执行文件

    多数据集时 `--json` / `--emit-config` 会自动加数据集名后缀
    （qc.json → qc.<数据集名>.json）；`--pdf` 则是**合并成一份**报告。

    结构信息、手套质量、视频规格等报告专用指标由本文件内的 PDF 段现采（ffprobe
    + 逐列统计）；出 PDF 才需要 chrome 与 websocket-client，两者都是惰性使用 ——
    不出 PDF 时本文件仍然只依赖 numpy + pyarrow。

    也可以当库用：
        import data_qc
        reps = [data_qc.run(d, data_qc.DEFAULT_CONFIG) for d in dirs]
        data_qc.write_pdf_report(dirs, reps, "报告.pdf")

退出码
    0 = 无 error（有 warn 时仍为 0，除非 --strict）
    1 = 存在 error 级问题（或 --strict 下有 warn）
    2 = 用法/读取错误

判据校准记录（基准数据集 Data_examples_2，18 段 / 11.9 万帧 / 3.0 GB）
    阈值默认值就是按这份数据定的：干净段的偏移 IQR ≤ 20ms、单流间隔最大 ≈1.1× 标称，
    所以 gap_factor=1.8 / min_excess=3ms / stability_warn=20ms 都不会误报，
    而真实缺陷（ep000 左手套 166.8ms 空洞 = 10.2× 标称）会稳稳报出来。
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import datetime as _dt
import fnmatch
import glob
import html
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

__version__ = "1.1.0"

# ──────────────────────────── 检查项编号与名称 ────────────────────────────

C_REGRESSION = "C1_timestamp_regression"
C_DUPLICATE = "C2_timestamp_duplicate"
C_JUMP = "C3_timestamp_jump"
C_COUNT = "C4_count_mismatch"
C_OFFSET = "C5_clock_offset"
C_STRUCT = "C6_structure"

CHECK_NAMES = {
    C_REGRESSION: "时间戳回退",
    C_DUPLICATE: "时间戳重复",
    C_JUMP: "时间戳跳变",
    C_COUNT: "不同数据帧数不齐",
    C_OFFSET: "不同时间戳有偏移",
    C_STRUCT: "结构一致性",
}
CHECK_ORDER = [C_REGRESSION, C_DUPLICATE, C_JUMP, C_COUNT, C_OFFSET, C_STRUCT]

SEV_ERROR = "error"
SEV_WARN = "warn"
SEV_INFO = "info"
SEV_RANK = {SEV_INFO: 0, SEV_WARN: 1, SEV_ERROR: 2}
SEV_CN = {SEV_ERROR: "错误", SEV_WARN: "警告", SEV_INFO: "提示"}

# ──────────────────────────── 默认配置 ────────────────────────────
# 全部可在 --config 里覆盖；未覆盖的项走这里的默认值或自动推断。

DEFAULT_CONFIG: Dict[str, Any] = {
    # 单位换算：1 个该单位 = 多少 ns。整数列默认 ns；浮点列默认 s
    # （`timestamp`/`wall_time` 是本仓库口径）。index = 计数，不换算。
    "ns_per_unit": {"ns": 1, "us": 1000, "ms": 1000000, "s": 1000000000,
                    "index": 1},
    # 浮点列转 ns 后的量化步长（= 重复判定的容差）：
    #   f64 秒在 1.79e9 附近的分辨率 ≈ 0.24µs；f32 秒在 322s 附近 ≈ 38µs
    "quantum_ns": {"int": 1, "float64": 1000, "float32": 100000},
    "frame_columns": None,       # None = 自动发现；也可写死 ["frame_index","timestamp",...]
    "extra_streams": [],         # 强制追加为时间戳流的列名（自动发现漏掉的）
    "ignore_streams": [],        # 排除的列名（支持 fnmatch 通配）
    "streams": {},               # 逐流覆盖：{"<列名>": {"rate_hz":..,"clock":..,"unit":..,...}}
    # 每行一个采样值的标量 *_ns 列（力的采集时刻等）按「样本流」处理：
    # 0 = 本行没有样本（不是时刻 0），见 info.json features[k].encoding=host_monotonic_ns。
    # False 退回旧口径（一律当帧级时钟）——只给不认识这张表的老数据用。
    "sample_columns": True,
    "sample_missing_value": 0,
    # 帧级列的时钟域（可被 info.json / 配置覆盖）
    "frame_domains": {
        "wall_time": "host_realtime_ns",
        "timestamp": "episode_relative_s",
        "hardware_ns": "device_ns",
        "created_at": "host_realtime_ns",
        "frame_index": "index",
    },
    # 时钟域 → 该域内作为「行参考」的帧级列；缺参考的域只做域内成对比较
    "clock_refs": {
        "host_realtime_ns": "wall_time",
        "imu_device_ns": "hardware_ns",
        "device_ns": "hardware_ns",
        "episode_relative_s": "timestamp",
        "index": None,
        # 采集时刻列（encoding=host_monotonic_ns）与 hardware_ns 同时基 ——
        # 这是 core/egodata_writer.py 里写死的契约，不是猜的。样本流据此
        # 比「行号配对偏移」，而不是比「两个钟的差」。
        "host_monotonic_ns": "hardware_ns",
    },
    # 无显式域时按「纪元邻近」猜参考列：中位数差 ≤ 该值才认（105s 的手套设备纪元
    # 与相机设备纪元因此不会被误配成同域）
    "domain_probe_tol_s": 10.0,

    "checks": {
        C_REGRESSION: {
            "enabled": True,
            "max_samples": 8,
        },
        C_DUPLICATE: {
            "enabled": True,
            "max_samples": 8,
            # 行内重复（同一行列表内）恒为 error；跨行重复按下面比例定级
            "cross_row_error_ratio": 0.001,
        },
        C_JUMP: {
            "enabled": True,
            "gap_factor": 1.8,         # 间隔 > 1.8 × 标称 才算异常间隔
            "min_excess_ms": 3.0,      # 且超出标称的量 ≥ 3ms（挡住高速流的抖动）
            "compress_factor": 0.5,    # 间隔 < 0.5 × 标称 记为「压缩/突发」
            "error_gap_factor": 5.0,   # 单处间隔 ≥ 5 × 标称 ⇒ error
            "error_gap_count": 10,     # 或单流异常间隔 ≥ 10 处 ⇒ error
            # 攒批流（间隔分布双峰：p95 ≥ 1.5×中位）的「空洞」还须超过
            # burst_floor_factor × p95 —— 20/20/60 是形状，不是丢了样本；
            # 真断流（100~160ms）远高于这个地板，照报。1.0 = 关闭该豁免。
            "burst_floor_factor": 1.5,
            "max_samples": 8,
        },
        C_COUNT: {
            "enabled": True,
            "rate_warn_pct": 1.0,      # 实测采样率偏离声明值 > 1% ⇒ warn
            "rate_error_pct": 5.0,
            "coverage_warn": 0.95,     # 该流自身跨度 / 本段跨度 < 该值 ⇒ warn
            "empty_row_warn": 0,       # 空行数 > 该值 ⇒ warn（0 = 有任何空行就报）
            # 首/末各 N 行内的空行与攒批按「起停固有特征」降级为 info：录制起停时
            # 手套缓冲先积后发、SLAM 首帧未产出，是**每一段都有**的系统性形状，
            # 逐段报 warn 只会淹没真问题（真问题在段中间）。0 = 关闭该宽限。
            "edge_rows_grace": 1,
            "count_tol_abs": 2,        # 跨流样本数差 > max(abs, rel×期望) ⇒ 报
            "count_tol_rel": 0.001,
            # 样本数差的归因阈值：速率失配超过该 ppm 才升级为 warn
            # （两个独立设备的晶振差 ~1000ppm 属正常；起止不齐永远算 warn）
            "rate_mismatch_warn_ppm": 2000.0,
            "check_meta_length": True,
            "check_frame_index": True,
            "check_pair_presence": True,
            # 稀疏列契约：本段 parquet 里整列没有的流，按「该流 0 样本」报一条，
            # 而不是让 pq.read_table 取列失败把整段判成读不动。
            "check_missing_column": True,
            # 样本流（每行至多一个采样值）：0 占位行是采样相位与行网格不同步的
            # 正常形状，缺样本比例低于该值只报 info；超过多半是通道真的停了。
            "sample_empty_warn_ratio": 0.5,
        },
        C_OFFSET: {
            "enabled": True,
            "warn_ms": 25.0,           # |实测偏移 − 基线| > 该值 ⇒ warn
            "error_ms": 100.0,
            "pair_warn_ms": 25.0,      # 同域两流之间的偏移差 ⇒ warn
            "pair_error_ms": 100.0,
            # 同域两流的中位偏移超过该值 ⇒ 判为「两个独立计数器纪元不同」，
            # 只出 info（绝对偏移不可比），不出 warn/error。
            # 依据：真失同步要看宿主戳，设备计数器纪元本来就各自从 0 起。
            "epoch_gap_ms": 1000.0,
            "stability_warn_ms": 20.0,  # 偏移 IQR（p75−p25）⇒ warn
            "stability_error_ms": 60.0,
            "step_warn_ms": 25.0,      # 分段中位数最大台阶 ⇒ warn
            "frame_pair_stability_warn_ms": 20.0,
            # 样本流的「行号配对偏移」（样本戳 − 本行帧戳）稳定性。
            # 队列滞留不同 ⇒ 中位可以很大（本例 0~130ms），那是设计，只报 info；
            # 乱的是 IQR/台阶：它随录制时长在变，说明哪一路在堆积/排空。
            "sample_pair_warn_iqr_ms": 66.7,     # ≈ 2 个行周期
            "sample_pair_error_iqr_ms": 200.0,
            "sample_step_warn_ms": 100.0,
            "max_samples": 8,
        },
    },
    # 偏移基线：键支持 fnmatch 通配；offset_ms = 期望的「实测偏移」。
    # 未写时自动读 info.json 的 alignment.glove_shift_ms 作为手套流的基线。
    "baselines": {},
    "baseline_tol_ms": 25.0,
    # 与 meta/episodes 的 drop_stats 对账（录制器自报计数）：
    # 空 = 按名字 token 自动配对；也可写死 {"frames_without_right_hand":
    # "observation.right_glove_ts_ns"}（值可为单个列名或列表）。
    "drop_stats_map": {},
    "max_samples": 8,
}

# 自动把 drop_stats 的键配到流上时，这些词不参与匹配（都是计数器的通用词）
_STAT_STOPWORDS = {"frames", "frame", "without", "samples", "sample", "count",
                   "rows", "row", "total", "num", "n", "of", "the"}
# 带这些词的 *_samples 键不是「样本总数」，不能直接对账
_STAT_DISQUALIFIERS = {"unpaired", "unknown", "dropped", "drop", "error", "errors",
                       "overflow", "out", "range", "missing", "lost", "invalid",
                       "skipped", "bad", "dup", "duplicate"}

# 自动发现：整数叶子的 list 列，名字像时间戳
_TS_NAME_RE = re.compile(r"(^|[._])(ts|timestamp|time|_ts_|ts_)", re.I)
_TS_SUFFIXES = ("_device_ts_ns", "_ts_ns", "_timestamp_ns", "_ts", "_ns")

# 帧级时钟列：这些名字的标量列**就是行自己的时刻**（时间轴本体）。
# 不在表里的标量 *_ns 列一律按「样本流」处理：每行至多一个采样值、
# 0 = 本行没有样本（夹爪力的 *_force_ns / *_force_matrix_ns 就是这一类）。
_FRAME_CLOCK_NAMES = ("timestamp", "wall_time", "hardware_ns", "raw_hw_ns",
                      "host_ns", "depth_ns", "created_at", "frame_index")


# ════════════════════════════ 小工具 ════════════════════════════

def deep_merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    """递归合并：over 覆盖 base，dict 逐键合并，其余整体替换。"""
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def name_matches(name: str, patterns: Iterable[str]) -> bool:
    """列名是否命中任一 pattern（支持 fnmatch 通配，也支持精确名）。"""
    for p in patterns or ():
        if p == name or fnmatch.fnmatchcase(name, p):
            return True
    return False


def dwidth(s: str) -> int:
    """终端显示宽度（CJK 记 2），用于对齐表头。"""
    w = 0
    for ch in str(s):
        w += 2 if (
            "\u1100" <= ch <= "\u115f" or "\u2e80" <= ch <= "\ua4cf"
            or "\uac00" <= ch <= "\ud7a3" or "\uf900" <= ch <= "\ufaff"
            or "\ufe30" <= ch <= "\ufe6f" or "\uff00" <= ch <= "\uff60"
            or "\uffe0" <= ch <= "\uffe6"
        ) else 1
    return w


def pad(s: str, width: int, align: str = "<") -> str:
    """按显示宽度补齐。"""
    s = str(s)
    n = max(0, width - dwidth(s))
    return (s + " " * n) if align == "<" else (" " * n + s)


def fmt_ns(v: Optional[float], unit: str = "ns") -> str:
    """把 ns 值印成人看的毫秒；index 单位原样。"""
    if v is None:
        return "-"
    if unit == "index":
        return "%g" % v
    return "%.3fms" % (v / 1e6)


def fmt_span(ms: float) -> str:
    """毫秒 → 自适应单位。"""
    a = abs(ms)
    if a >= 1000.0:
        return "%.3fs" % (ms / 1000.0)
    if a >= 1.0:
        return "%.2fms" % ms
    return "%.1fus" % (ms * 1000.0)


def is_list_type(t: pa.DataType) -> bool:
    return (pa.types.is_list(t) or pa.types.is_large_list(t)
            or pa.types.is_fixed_size_list(t)
            or pa.types.is_list_view(t) or pa.types.is_large_list_view(t))


def leaf_type(t: pa.DataType) -> pa.DataType:
    """剥掉所有 list 层，取叶子类型。"""
    while is_list_type(t):
        t = t.value_type
    return t


def is_int_type(t: pa.DataType) -> bool:
    return (pa.types.is_integer(t) or pa.types.is_unsigned_integer(t)
            or pa.types.is_boolean(t))


def default_unit(t: pa.DataType, name: str) -> str:
    """单位推断：整数 ⇒ ns（本仓库全部设备戳都是 ns）；浮点 ⇒ s。"""
    if is_int_type(t):
        return "index" if name == "frame_index" else "ns"
    return "s"


def quantum_of(t: pa.DataType, cfg: Dict[str, Any], unit: str) -> int:
    """该列的量化步长（ns）：重复判定的容差，也是「同一时刻」的判据。"""
    if unit == "index":
        return 0
    q = cfg.get("quantum_ns") or {}
    if pa.types.is_floating(t):
        if pa.types.is_float32(t):
            return int(q.get("float32", 100000))
        return int(q.get("float64", 1000))
    return int(q.get("int", 1))


def to_ns(values: np.ndarray, unit: str, cfg: Dict[str, Any]) -> np.ndarray:
    """把原生单位的值换算成 int64 ns（index 单位原样保留为 int64 计数）。

    浮点必须先乘再取整（`timestamp` 是 float32 秒，直接转 int 会把整段压成 0）；
    整数走整数乘法，保证 ns 列逐位不变（1.79e18 超出 float64 的精确整数范围）。
    """
    scale = int((cfg.get("ns_per_unit") or {}).get(unit, 1))
    if values.dtype.kind == "f":
        return np.rint(values.astype(np.float64) * float(scale)).astype(np.int64)
    if scale == 1:
        return values.astype(np.int64, copy=False)
    return values.astype(np.int64) * scale


# ════════════════════════════ 流模型 ════════════════════════════

@dataclass
class StreamSpec:
    """一条时间戳流：一个标量列，或一个挂在帧行上的变长时间戳列表列。"""
    key: str
    kind: str                     # "frame" | "list"
    unit: str = "ns"              # ns / us / ms / s / index
    rate_hz: Optional[float] = None
    clock: Optional[str] = None   # 时钟域
    ref: Optional[str] = None     # 同域参考帧列（用于偏移检查）
    value_col: Optional[str] = None   # 配对数值列（用于存在性对齐）
    sample_width: int = 1         # 配对数值列「一个样本」的标量宽度
    quantum_ns: int = 1
    is_timestamp: bool = True
    # 样本流专用：该值 = 「本行没有样本」（info.json features.missing_sentinel，
    # 缺省取 cfg["sample_missing_value"]=0）。它**不是**时刻。
    missing: Optional[int] = None
    note: str = ""
    # 运行期填充
    dtype: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key, "kind": self.kind, "unit": self.unit,
            "rate_hz": self.rate_hz, "clock": self.clock, "ref": self.ref,
            "value_col": self.value_col, "sample_width": self.sample_width,
            "quantum_ns": self.quantum_ns, "dtype": self.dtype,
            "missing": self.missing, "note": self.note,
        }


def _shape_width(info: Dict[str, Any], col: str) -> int:
    """info.json features[col].shape 的连乘 = 一个样本的标量宽度。"""
    feat = (info.get("features") or {}).get(col) or {}
    shape = feat.get("shape")
    if isinstance(shape, (list, tuple)) and shape:
        w = 1
        for d in shape:
            try:
                w *= int(d)
            except (TypeError, ValueError):
                return 1
        return max(1, w)
    return 1


def _value_col_for(info: Dict[str, Any], ts_name: str, columns: Sequence[str]) -> Optional[str]:
    """给时间戳列找配对的数值列（存在性对齐用）。"""
    feats = info.get("features") or {}
    # 1) info.json 的 ts_key 反查（最权威）
    for fname, f in feats.items():
        if isinstance(f, dict) and f.get("ts_key") == ts_name and fname in columns:
            return fname
    # 2) 剥后缀再找（observation.left_glove_ts_ns → observation.left_glove）
    stem = ts_name
    for suf in _TS_SUFFIXES:
        if stem.endswith(suf):
            stem = stem[: -len(suf)]
            break
    if stem in columns:
        return stem
    for c in columns:
        if c.endswith("." + stem) or c.split(".")[-1] == stem:
            return c
    return None


def discover_streams(info: Dict[str, Any], schema: pa.Schema,
                     cfg: Dict[str, Any]) -> List[StreamSpec]:
    """推断流模型：优先用 info.json 的 features，其次看列类型/列名。"""
    columns = list(schema.names)
    feats = info.get("features") or {}
    types = {f.name: f.type for f in schema}

    # ── 标称采样率与时钟域：把 features[key].ts_key 的信息传播给 ts 列 ──
    rates: Dict[str, float] = {}
    clocks: Dict[str, str] = {}
    for fname, f in feats.items():
        if not isinstance(f, dict):
            continue
        r = f.get("rate_hz")
        ck = f.get("clock")
        if isinstance(r, (int, float)) and r > 0:
            rates[fname] = float(r)
        if isinstance(ck, str) and ck:
            clocks[fname] = ck
        tk = f.get("ts_key")
        if isinstance(tk, str) and tk:
            if tk not in rates and isinstance(r, (int, float)) and r > 0:
                rates[tk] = float(r)
            if tk not in clocks and isinstance(ck, str) and ck:
                clocks[tk] = ck

    frame_cols = cfg.get("frame_columns")
    sample_cols: List[str] = []
    if frame_cols is None:
        # 自动发现帧级时间列：标量数值列里名字像时间戳的 + 恒有的键列
        known = _FRAME_CLOCK_NAMES
        want_sample = bool(cfg.get("sample_columns", True))
        frame_cols = []
        for name in columns:
            t = types[name]
            if is_list_type(t) or not (pa.types.is_integer(t) or pa.types.is_floating(t)):
                continue
            if name in known or _TS_NAME_RE.search(name):
                frame_cols.append(name)
            elif name.endswith("_ns"):
                # 标量 *_ns 但不是帧钟本体 ⇒ 每行一个采样值（可能缺样本）
                (sample_cols if want_sample else frame_cols).append(name)
        # frame_index 永远放最后（它是计数不是时钟）
        frame_cols.sort(key=lambda n: (n == "frame_index", columns.index(n)))

    streams: List[StreamSpec] = []
    for name in frame_cols:
        if name not in types:
            continue
        t = types[name]
        if is_list_type(t):
            continue
        unit = default_unit(t, name)
        streams.append(StreamSpec(
            key=name, kind="frame", unit=unit, rate_hz=rates.get(name),
            clock=clocks.get(name) or (cfg.get("frame_domains") or {}).get(name),
            quantum_ns=quantum_of(t, cfg, unit), dtype=str(t),
            note="自动发现（帧级）" if name not in feats else "info.json/frame",
        ))

    # ── 样本流：标量 *_ns、每行至多一个采样值（0 = 本行无样本）──
    for name in sample_cols:
        t = types[name]
        unit = default_unit(t, name)
        # 时钟域：info.json 的 encoding（本仓库写的是 host_monotonic_ns）优先，
        # 它等价于「与 hardware_ns 同时基」这句契约。
        enc = (feats.get(name) or {}).get("encoding")
        clock = clocks.get(name) or (enc if isinstance(enc, str)
                                     and enc.endswith("_ns") else None)
        miss = (feats.get(name) or {}).get("missing_sentinel")
        streams.append(StreamSpec(
            key=name, kind="sample", unit=unit, rate_hz=rates.get(name),
            clock=clock, quantum_ns=quantum_of(t, cfg, unit), dtype=str(t),
            missing=(int(miss) if isinstance(miss, (int, float)) else None),
            note="样本流（每行一个采样值；0 = 本行无样本）" if name in feats
                 else "样本流（自动发现；0 = 本行无样本）",
        ))

    # ── 列表级时间戳流：整数叶子 + 名字像时间戳 ──
    for name in columns:
        t = types[name]
        if not is_list_type(t):
            continue
        lt = leaf_type(t)
        if not (pa.types.is_integer(lt) or pa.types.is_unsigned_integer(lt)):
            continue
        forced = name in (cfg.get("extra_streams") or [])
        if not forced and not (name.endswith("_ns") or _TS_NAME_RE.search(name)):
            continue
        unit = "ns"
        vcol = _value_col_for(info, name, columns)
        streams.append(StreamSpec(
            key=name, kind="list", unit=unit, rate_hz=rates.get(name),
            clock=clocks.get(name), value_col=vcol,
            sample_width=_shape_width(info, vcol) if vcol else 1,
            quantum_ns=quantum_of(lt, cfg, unit), dtype=str(t),
            note="info.json" if name in feats or vcol in feats else "自动发现（列表）",
        ))

    for name in cfg.get("extra_streams") or []:
        if name in columns and not any(s.key == name for s in streams):
            t = types[name]
            unit = default_unit(t, name)
            streams.append(StreamSpec(
                key=name, kind="list" if is_list_type(t) else "frame", unit=unit,
                rate_hz=rates.get(name), clock=clocks.get(name),
                quantum_ns=quantum_of(leaf_type(t), cfg, unit),
                value_col=_value_col_for(info, name, columns),
                dtype=str(t), note="配置强制追加",
            ))

    ignore = cfg.get("ignore_streams") or []
    streams = [s for s in streams if not name_matches(s.key, ignore)]

    # ── 逐流覆盖 ──
    for s in streams:
        for pat, over in (cfg.get("streams") or {}).items():
            if name_matches(s.key, [pat]) and isinstance(over, dict):
                for k, v in over.items():
                    if hasattr(s, k):
                        setattr(s, k, v)
                    elif k == "rate_hz":
                        s.rate_hz = v

    # ── 参考帧列：显式 clock_refs → 该域；没有就留空（域内成对比较兜底） ──
    refs = dict(cfg.get("clock_refs") or {})
    frame_names = [s.key for s in streams if s.kind == "frame"]
    frame_domains = {s.key: s.clock for s in streams if s.kind == "frame"}
    for s in streams:
        if s.kind not in ("list", "sample"):
            continue
        if s.ref:
            continue
        s.ref = refs.get(s.clock)
        if s.ref and s.ref not in frame_names:
            s.ref = None
    return streams


def probe_refs_by_epoch(streams: List[StreamSpec], first_med: Dict[str, float],
                        cfg: Dict[str, Any]) -> None:
    """没有显式时钟域的流，按「纪元邻近」猜参考帧列（原地改 s.ref）。

    只在中位数差 ≤ domain_probe_tol_s 时才认，所以不同设备纪元（相差上百秒）
    不会被误配；epoch 完全不同（1.79e18 vs 1.877e11）更不会。
    """
    tol = float(cfg.get("domain_probe_tol_s", 10.0)) * 1e9
    frame_items = [(s.key, first_med[s.key]) for s in streams
                   if s.kind == "frame" and s.key in first_med and s.key != "frame_index"]
    for s in streams:
        if s.kind not in ("list", "sample") or s.ref or s.key not in first_med:
            continue
        m = first_med[s.key]
        best, bd = None, None
        for fk, fm in frame_items:
            d = abs(fm - m)
            if bd is None or d < bd:
                best, bd = fk, d
        if best is not None and bd is not None and bd <= tol:
            s.ref = best
            s.note = (s.note + "；参考列按纪元邻近推断").lstrip("；")


# ════════════════════════════ 读数与度量 ════════════════════════════

def _fill_null_lists(col: pa.ChunkedArray, t: pa.DataType) -> pa.ChunkedArray:
    """把 null 列表当成空列表（pyarrow 的 flatten 会丢掉 null，位置就对不上了）。"""
    if col.null_count == 0:
        return col
    return pc.fill_null(col, pa.scalar([], type=t))


def read_frame_column(table: pa.Table, key: str, unit: str, cfg: Dict[str, Any]) -> np.ndarray:
    """读一个帧级标量时间列 → int64 ns（或 index 计数）。"""
    col = table.column(key)
    if isinstance(col, pa.ChunkedArray):
        col = col.combine_chunks()
    if col.null_count:
        col = pc.fill_null(col, pa.scalar(0, type=col.type))
    return to_ns(col.to_numpy(zero_copy_only=False), unit, cfg)


def read_list_column(table: pa.Table, key: str, unit: str,
                     cfg: Dict[str, Any]) -> Tuple[np.ndarray, np.ndarray]:
    """读一个列表级时间戳列 → (每行长度 n_rows, 展平后的值，单位 ns)。

    展平顺序是行主序，所以「上一行末样本 → 下一行首样本」的相邻关系天然落在
    同一个 diff 数组里，跨行回退/跳变不需要额外拼接。
    """
    col = table.column(key)
    if isinstance(col, pa.ChunkedArray):
        col = col.combine_chunks()
    col = _fill_null_lists(col, col.type)
    lengths = pc.list_value_length(col)
    lengths = pc.fill_null(lengths, 0)
    lengths_np = lengths.to_numpy(zero_copy_only=False).astype(np.int64)
    flat = pc.list_flatten(col)
    if isinstance(flat, pa.ChunkedArray):
        flat = flat.combine_chunks()
    if flat.null_count:
        flat = pc.fill_null(flat, 0)
    return lengths_np, to_ns(flat.to_numpy(zero_copy_only=False), unit, cfg)


def read_value_sample_counts(table: pa.Table, key: str, width: int) -> np.ndarray:
    """配对数值列每行有几个「样本」（用于与 ts 列做存在性对齐）。"""
    col = table.column(key)
    if isinstance(col, pa.ChunkedArray):
        col = col.combine_chunks()
    if not is_list_type(col.type):
        # 标量列：非空即 1 个样本
        return np.where(pc.is_null(col).to_numpy(zero_copy_only=False), 0, 1).astype(np.int64)
    col = _fill_null_lists(col, col.type)
    L = pc.fill_null(pc.list_value_length(col), 0)
    L = L.to_numpy(zero_copy_only=False).astype(np.int64)
    if is_list_type(col.type.value_type):
        return L                       # 元素本身就是一个样本（list<fixed_size_list<w>>）
    return (L // max(1, int(width))).astype(np.int64)


@dataclass
class Finding:
    check: str
    severity: str
    stream: str
    message: str
    metrics: Dict[str, Any] = field(default_factory=dict)
    samples: List[Dict[str, Any]] = field(default_factory=list)
    # True = 只是记账/已知固有形状（drop_stats 对账通过、录制起停的首末行形状、
    # 计数器绕回解卷），不是「发现」。汇总表里不计，免得把真发现淹掉。
    bookkeeping: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "check": self.check, "check_name": CHECK_NAMES.get(self.check, self.check),
            "severity": self.severity, "stream": self.stream,
            "message": self.message, "metrics": self.metrics, "samples": self.samples,
            "bookkeeping": self.bookkeeping,
        }


def _sample_pairs(values: np.ndarray, idx: np.ndarray, limit: int,
                  unit: str, row_of: Optional[np.ndarray] = None,
                  elem_of: Optional[np.ndarray] = None) -> List[Dict[str, Any]]:
    """把「出问题的相邻下标」整理成可读样例（k = 前一个元素的下标）。"""
    out = []
    for k in idx[:limit]:
        k = int(k)
        s: Dict[str, Any] = {
            "i": k,
            "prev": int(values[k]), "next": int(values[k + 1]),
            "delta": int(values[k + 1]) - int(values[k]),
        }
        if unit != "index":
            s["prev_ms"] = round(float(values[k]) / 1e6, 4)
            s["next_ms"] = round(float(values[k + 1]) / 1e6, 4)
            s["delta_ms"] = round(float(s["delta"]) / 1e6, 4)
        if row_of is not None:
            s["row"] = int(row_of[k])
            s["next_row"] = int(row_of[k + 1])
        if elem_of is not None:
            s["elem"] = int(elem_of[k])
            s["next_elem"] = int(elem_of[k + 1])
        out.append(s)
    return out


# ════════════════════════════ 五项检查 ════════════════════════════

def _detect_wrap(d: np.ndarray, nominal: Optional[float]) -> Optional[int]:
    """识别计数器绕回：负跳变恰好等于 −(M − 小量)，M 取 2^32 / 2^64。

    返回模数 M；不构成绕回则 None。只认真实存在的整数回绕，避免把真回退洗白。
    """
    neg = d[d < 0]
    if neg.size == 0:
        return None
    worst = int(neg.min())            # 最大的负跳变
    for M in (1 << 32, 1 << 64):
        wrapped = worst + M           # 绕回后应有的正向步长
        cap = (nominal * 10) if nominal else 1e9
        if 0 <= wrapped <= max(cap, 1e6):
            return M
    return None


def check_regression(v: np.ndarray, unit: str, spec: StreamSpec,
                     cfg: Dict[str, Any], nominal: Optional[float],
                     row_of: Optional[np.ndarray] = None,
                     elem_of: Optional[np.ndarray] = None) -> List[Finding]:
    """C1 时间戳回退：相邻样本逆序。"""
    c = cfg["checks"][C_REGRESSION]
    if not c.get("enabled", True) or v.size < 2:
        return []
    d = np.diff(v)
    neg = np.where(d < 0)[0]
    if neg.size == 0:
        return []
    M = _detect_wrap(d, nominal)
    if M is not None:
        # 绕回：负跳变落在 −(M − 正常步长) 附近，扣掉后应无回退
        fixed = d.copy()
        mask = (d < 0) & (d + M >= 0)
        fixed[mask] = fixed[mask] + M
        real = np.where(fixed < 0)[0]
        extra = [Finding(
            check=C_REGRESSION, severity=SEV_INFO, stream=spec.key,
            message=("检测到计数器绕回（模 2^%d），已按绕回解卷，不计为回退"
                     % (32 if M == 1 << 32 else 64)),
            metrics={"wrap_modulus": M, "wrap_events": int(mask.sum())},
            bookkeeping=True,
        )]
        if real.size == 0:
            return extra
        neg, d = real, fixed
    intra = None
    if row_of is not None and neg.size:
        same = row_of[neg] == row_of[neg + 1]
        intra = int(same.sum())
    worst = int(d[neg].min())
    metrics: Dict[str, Any] = {
        "n_regressions": int(neg.size),
        "ratio": round(float(neg.size) / float(d.size), 8),
        "worst_delta": worst,
        "worst_delta_ms": round(worst / 1e6, 4) if unit != "index" else None,
        "intra_row": intra,
        "cross_row": (int(neg.size) - intra) if intra is not None else None,
        "nominal_step": nominal,
    }
    msg = ("时间戳回退 %d 处（最差 %.3fms）" % (neg.size, worst / 1e6)
           if unit != "index" else "序号回退 %d 处（最差 %d）" % (neg.size, worst))
    if intra is not None:
        msg += "；行内 %d / 跨行 %d" % (intra, int(neg.size) - intra)
    return [Finding(check=C_REGRESSION, severity=SEV_ERROR, stream=spec.key,
                    message=msg, metrics=metrics,
                    samples=_sample_pairs(v, neg, c.get("max_samples", 8),
                                          unit, row_of, elem_of))]


def check_duplicate(v: np.ndarray, unit: str, spec: StreamSpec, cfg: Dict[str, Any],
                    row_of: Optional[np.ndarray] = None,
                    elem_of: Optional[np.ndarray] = None) -> List[Finding]:
    """C2 时间戳重复：相邻相同（容差 = 量化步长）。行内重复恒为 error。"""
    c = cfg["checks"][C_DUPLICATE]
    if not c.get("enabled", True) or v.size < 2:
        return []
    d = np.diff(v)
    # 容差 = 该列自身的量化分辨率（int64 ns 精确，故为 0；f32 秒 ≈ 38µs）
    tol = 0 if unit == "index" else max(0, int(spec.quantum_ns) - 1)
    dup = np.where(np.abs(d) <= tol)[0]
    if dup.size == 0:
        return []
    intra = None
    if row_of is not None:
        intra = int((row_of[dup] == row_of[dup + 1]).sum())
    ratio = float(dup.size) / float(d.size)
    sev = SEV_WARN
    if intra:
        sev = SEV_ERROR
    elif ratio > float(c.get("cross_row_error_ratio", 0.001)):
        sev = SEV_ERROR
    metrics = {
        "n_duplicates": int(dup.size), "ratio": round(ratio, 8),
        "quantum_ns": tol, "intra_row": intra,
        "cross_row": (int(dup.size) - intra) if intra is not None else None,
        "n_unique": int(np.unique(v).size), "n_values": int(v.size),
    }
    msg = "时间戳重复 %d 处（容差 ±%s）" % (dup.size, fmt_span(tol / 1e6))
    if intra is not None:
        msg += "；行内 %d / 跨行 %d" % (intra, int(dup.size) - intra)
    return [Finding(check=C_DUPLICATE, severity=sev, stream=spec.key,
                    message=msg, metrics=metrics,
                    samples=_sample_pairs(v, dup, c.get("max_samples", 8),
                                          unit, row_of, elem_of))]


def check_jump(v: np.ndarray, unit: str, spec: StreamSpec, cfg: Dict[str, Any],
               nominal: Optional[float], row_of: Optional[np.ndarray] = None,
               elem_of: Optional[np.ndarray] = None,
               gap_floor: Optional[float] = None,
               gap_error: Optional[float] = None) -> List[Finding]:
    """C3 时间戳跳变：间隔明显偏离标称周期（空洞 / 压缩）。

    gap_floor：额外的**下界**（ns）。攒批到达的流（间隔双峰）用它把「一次
    正常的 60ms 攒批」和「真断流」分开：20/20/60 的节奏远低于地板，而 100ms+
    的断流在地板之上 —— 否则节奏本身会被逐段读成「丢了 511 个样本」。
    gap_error：攒批流的 error 门槛（ns，通常取 2×p95）。给了它就替代
    error_gap_factor × 标称 —— 攒批流的标称被拉长，5× 标称会放过 140ms 的真断流。
    """
    c = cfg["checks"][C_JUMP]
    if not c.get("enabled", True) or v.size < 3 or not nominal or nominal <= 0:
        return []
    d = np.diff(v)
    is_index = unit == "index"
    exc = float(c.get("min_excess_ms", 3.0)) * 1e6 if not is_index else 0.0
    is_gap = (d > nominal * float(c.get("gap_factor", 1.8))) & ((d - nominal) >= exc)
    if gap_floor:
        is_gap &= d > float(gap_floor)
    gap = np.where(is_gap)[0]
    comp = np.where(d < nominal * float(c.get("compress_factor", 0.5)))[0]
    out: List[Finding] = []
    if gap.size:
        worst = int(d[gap].max())
        missing = float(np.sum(d[gap] / nominal - 1.0))
        sev = SEV_WARN
        big = (worst >= float(gap_error)) if gap_error else \
            (worst >= nominal * float(c.get("error_gap_factor", 5.0)))
        if big or gap.size >= int(c.get("error_gap_count", 10)):
            sev = SEV_ERROR
        metrics = {
            "n_gaps": int(gap.size), "worst_gap": worst,
            "worst_gap_ms": round(worst / 1e6, 4) if not is_index else None,
            "worst_gap_x_nominal": round(worst / nominal, 3),
            "nominal_step": nominal,
            "nominal_step_ms": round(nominal / 1e6, 6) if not is_index else None,
            "est_missing_samples": round(missing, 2),
            "gap_floor_ms": round(float(gap_floor) / 1e6, 4) if gap_floor else None,
            "gap_error_ms": round(float(gap_error) / 1e6, 4) if gap_error else None,
            "total_gap_ms": round(float(np.sum(d[gap] - nominal)) / 1e6, 3) if not is_index else None,
        }
        unit_s = "ms" if not is_index else "帧"
        msg = ("时间戳跳变（空洞）%d 处，最大 %.3fms = %.2f× 标称(%.3fms)，"
               "折合丢失约 %.1f 个样本" % (
                   gap.size, worst / 1e6, worst / nominal, nominal / 1e6, missing)
               if not is_index else
               "序号跳变（缺帧）%d 处，最大跳过 %d，折合丢失约 %.1f 帧"
               % (gap.size, worst, missing))
        out.append(Finding(check=C_JUMP, severity=sev, stream=spec.key, message=msg,
                           metrics=metrics,
                           samples=_sample_pairs(v, gap, c.get("max_samples", 8),
                                                 unit, row_of, elem_of)))
    if comp.size:
        out.append(Finding(
            check=C_JUMP, severity=SEV_INFO, stream=spec.key,
            message=("间隔压缩 %d 处（< %.2f× 标称），最小 %.3fms —— 攒批突发，"
                     "通常与相邻的空洞成对出现" % (
                         comp.size, float(c.get("compress_factor", 0.5)),
                         float(d[comp].min()) / 1e6)),
            metrics={"n_compressed": int(comp.size),
                     "min_gap": int(d[comp].min()),
                     "min_gap_ms": round(float(d[comp].min()) / 1e6, 4)},
            samples=_sample_pairs(v, comp, c.get("max_samples", 8), unit, row_of, elem_of),
        ))
    return out


def check_stream_counts(spec: StreamSpec, lengths: Optional[np.ndarray], n_rows: int,
                        n_samples: int, own_span_s: float, duration_s: float,
                        fps: float, cfg: Dict[str, Any],
                        flat: Optional[np.ndarray] = None,
                        sample_kind: bool = False,
                        bursty: bool = False) -> Tuple[List[Finding], Dict[str, Any]]:
    """C4 单流侧的子检查：空行、攒批突发、实测采样率 vs 声明、覆盖率。

    sample_kind：样本流（每行至多一个采样值）。此时「空行」= 该行没有采样值
    （0 占位），是采样相位与 30fps 行网格不同步的**正常形状**，低于
    sample_empty_warn_ratio 只报 info；超过它才说明通道多半停了。
    bursty：攒批到达的列表流（SLAM 位姿 20/20/60）。一行里空着、或一行塞进
    2~5 个样本，都是这个节奏的必然形状 —— 同样降级为 info，断流由 C3 报。
    """
    c = cfg["checks"][C_COUNT]
    out: List[Finding] = []
    summary: Dict[str, Any] = {"samples": int(n_samples)}
    if sample_kind:
        summary["sample_stream"] = True

    if lengths is not None:
        grace = int(c.get("edge_rows_grace", 1))
        empty = np.where(lengths == 0)[0]
        edge = ((empty < grace) | (empty >= n_rows - grace)) if grace > 0 \
            else np.zeros(empty.size, dtype=bool)
        empty_edge, empty_mid = empty[edge], empty[~edge]
        summary["empty_rows"] = int(empty.size)
        summary["edge_empty_rows"] = int(empty_edge.size)
        summary["mid_empty_rows"] = int(empty_mid.size)
        summary["rows_with_samples"] = int(n_rows - empty.size)
        summary["len_min"] = int(lengths.min()) if lengths.size else 0
        summary["len_max"] = int(lengths.max()) if lengths.size else 0
        summary["len_median"] = float(np.median(lengths)) if lengths.size else 0.0
        if empty_mid.size > int(c.get("empty_row_warn", 0)):
            head = empty_mid[:10].tolist()
            ratio = empty_mid.size / max(1, n_rows)
            warn_ratio = float(c.get("sample_empty_warn_ratio", 0.5))
            sev = (SEV_INFO if ((sample_kind and ratio <= warn_ratio)
                                or (bursty and ratio <= warn_ratio)) else SEV_WARN)
            msg = ("该流在段中间有 %d 行没有任何样本（共 %d 行，占 %.2f%%）；前几行 %s%s"
                   % (empty_mid.size, n_rows, ratio * 100.0,
                      head, " …" if empty_mid.size > len(head) else ""))
            if sample_kind:
                msg += ("\n          样本流：0 = 本行没有采样值（采样相位与行网格不同步），"
                        "下游按 *_ns 重采样即可；比例超过 %.0f%% 才说明通道停了"
                        % (warn_ratio * 100.0))
            elif bursty:
                msg += ("\n          攒批到达：位姿是 20/20/60ms 三连发，空行与"
                        "「一行多个样本」都是这个节奏的形状；真断流由 C3 报")
            out.append(Finding(
                check=C_COUNT, severity=sev, stream=spec.key,
                message=msg,
                metrics={"empty_rows": int(empty.size), "mid_empty_rows": int(empty_mid.size),
                         "n_rows": int(n_rows),
                         "empty_row_ratio": round(float(empty.size) / max(1, n_rows), 6),
                         "sample_stream": bool(sample_kind),
                         "first_empty_rows": head},
                # 形状类 info 不进汇总（逐流覆盖率已在「各流样本数对照」里）
                bookkeeping=(sev == SEV_INFO),
            ))
        if empty_edge.size:
            out.append(Finding(
                check=C_COUNT, severity=SEV_INFO, stream=spec.key,
                message=("该流在首/末 %d 行内有 %d 行没有样本（%s）—— 录制起停的固有形状"
                         % (grace, empty_edge.size, empty_edge[:6].tolist())),
                metrics={"edge_empty_rows": int(empty_edge.size),
                         "rows": empty_edge[:10].tolist()}, bookkeeping=True))
        # 每行样本数明显偏离中位数（攒批/突发）
        if lengths.size and lengths.max() > 3:
            med = float(np.median(lengths))
            odd = np.where(lengths > max(med * 3.0, med + 3))[0]
            odd_edge = odd[(odd < grace) | (odd >= n_rows - grace)] if grace > 0 \
                else np.array([], dtype=np.int64)
            odd_mid = np.setdiff1d(odd, odd_edge)
            summary["burst_rows"] = int(odd_mid.size)
            summary["edge_burst_rows"] = int(odd_edge.size)
            if odd_mid.size:
                out.append(Finding(
                    check=C_COUNT,
                    severity=SEV_INFO if bursty else SEV_WARN, stream=spec.key,
                    message=("该流在段中间有 %d 行样本数远超中位数 %.1f（最大 %d）"
                             "—— 攒批补发；前几行 %s"
                             % (odd_mid.size, med, int(lengths.max()), odd_mid[:10].tolist())),
                    metrics={"burst_rows": int(odd_mid.size), "len_median": med,
                             "len_max": int(lengths.max()),
                             "first_burst_rows": odd_mid[:10].tolist()},
                    bookkeeping=bursty,
                ))
            if odd_edge.size:
                out.append(Finding(
                    check=C_COUNT, severity=SEV_INFO, stream=spec.key,
                    message=("该流在首/末行有攒批补发（行 %s，最大 %d 个样本）—— "
                             "启动/收尾积压，每段都有" % (odd_edge[:6].tolist(),
                                                          int(lengths.max()))),
                    metrics={"edge_burst_rows": int(odd_edge.size),
                             "rows": odd_edge[:10].tolist(),
                             "len_max": int(lengths.max())}, bookkeeping=True))

    # 实测采样率用**该流自己的跨度**算，不用 episode 跨度 —— 后者在流首尾超出帧范围时
    # 会系统性偏差（短段尤其明显）。coverages 单独看「流覆盖了本段多少」。
    rate = spec.rate_hz
    meas = (float(n_samples - 1) / own_span_s) if (own_span_s and own_span_s > 0
                                                   and n_samples > 1) else None
    summary["rate_measured_hz"] = round(meas, 4) if meas else None
    summary["rate_declared_hz"] = rate
    summary["own_span_sec"] = round(own_span_s, 4) if own_span_s else None
    coverage = (own_span_s / duration_s) if (own_span_s and duration_s > 0) else None
    summary["coverage"] = round(coverage, 5) if coverage is not None else None
    if rate and meas:
        dev = (meas - rate) / rate * 100.0
        summary["rate_dev_pct"] = round(dev, 3)
        if abs(dev) > float(c.get("rate_warn_pct", 1.0)):
            sev = (SEV_ERROR if abs(dev) > float(c.get("rate_error_pct", 5.0))
                   else SEV_WARN)
            out.append(Finding(
                check=C_COUNT, severity=sev, stream=spec.key,
                message=("实测采样率 %.3f Hz 与声明 %.3f Hz 差 %+.2f%%"
                         "（%d 样本 / 自身跨度 %.3fs）" % (meas, rate, dev,
                                                          n_samples, own_span_s)),
                metrics={"rate_measured_hz": round(meas, 4),
                         "rate_declared_hz": rate, "dev_pct": round(dev, 3)},
            ))
    cov_warn = float(c.get("coverage_warn", 0.95))
    if coverage is not None and coverage < cov_warn:
        out.append(Finding(
            check=C_COUNT, severity=SEV_WARN, stream=spec.key,
            message=("该流只覆盖了本段的 %.2f%%（自身跨度 %.3fs / 本段 %.3fs），"
                     "首尾有 %.3fs 没有数据" % (coverage * 100.0, own_span_s,
                                                duration_s, duration_s - own_span_s)),
            metrics={"coverage": round(coverage, 5), "own_span_sec": own_span_s,
                     "episode_span_sec": round(duration_s, 4)},
        ))
    # 每行期望样本数（声明率 / 帧率）
    if rate and fps and lengths is not None:
        summary["expected_per_row"] = round(rate / fps, 4)
        summary["measured_per_row"] = round(float(np.mean(lengths)), 4) if lengths.size else None
    return out, summary


def _seg_steps(series: np.ndarray, n_seg: int = 20) -> Tuple[float, List[Dict[str, Any]]]:
    """把逐行偏移切成 n_seg 段，返回「相邻段中位数最大台阶」和台阶位置。"""
    if series.size < n_seg * 2:
        return 0.0, []
    idx = np.array_split(np.arange(series.size), n_seg)
    meds = [float(np.median(series[ix])) for ix in idx if ix.size]
    if len(meds) < 2:
        return 0.0, []
    steps = np.abs(np.diff(np.array(meds)))
    j = int(np.argmax(steps))
    return float(steps[j]), [{"seg": j, "from_ms": round(meds[j] / 1e6, 3),
                              "to_ms": round(meds[j + 1] / 1e6, 3),
                              "step_ms": round(float(steps[j]) / 1e6, 3)}]


def _drift_ppm(x: np.ndarray, y: np.ndarray, span: float) -> Optional[float]:
    """最小二乘斜率 → 相对漂移 ppm（相对 span）。"""
    if x.size < 8 or span <= 0:
        return None
    try:
        a = np.polyfit(x.astype(np.float64), y.astype(np.float64), 1)[0]
    except Exception:
        return None
    return float(a * float(x[-1] - x[0]) / span * 1e6)


@dataclass
class OffsetObs:
    """一个「流 ↔ 参考帧列」的偏移观测。"""
    stream: str
    ref: str
    n: int
    med: float
    p25: float
    p75: float
    p10: float
    p90: float
    drift_ppm: Optional[float]
    step_ms: float
    step_at: List[Dict[str, Any]]
    baseline_ms: Optional[float] = None
    baseline_src: str = ""
    first_sample_row: Optional[int] = None
    last_sample_row: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "stream": self.stream, "ref": self.ref, "n": self.n,
            "offset_median_ms": round(self.med / 1e6, 4),
            "offset_p25_ms": round(self.p25 / 1e6, 4),
            "offset_p75_ms": round(self.p75 / 1e6, 4),
            "offset_iqr_ms": round((self.p75 - self.p25) / 1e6, 4),
            "offset_p10_ms": round(self.p10 / 1e6, 4),
            "offset_p90_ms": round(self.p90 / 1e6, 4),
            "offset_span_ms": round((self.p90 - self.p10) / 1e6, 4),
            "drift_ppm": None if self.drift_ppm is None else round(self.drift_ppm, 2),
            # step_ms 是 ns（_seg_steps 返回 ns）—— 这里必须除 1e6，否则
            # JSON/PDF 里的「分段台阶」会大 1e6 倍（文字结论用的是除过的值，
            # 所以阈值判定一直是对的，只有读数被坑）。
            "max_segment_step_ms": round(self.step_ms / 1e6, 4),
            "step_at": self.step_at,
        }
        if self.baseline_ms is not None:
            d["baseline_ms"] = self.baseline_ms
            d["baseline_source"] = self.baseline_src
            d["baseline_dev_ms"] = round((self.med / 1e6) - self.baseline_ms, 4)
        return d


def compute_offset(v_first: np.ndarray, rows: np.ndarray, ref_vals: np.ndarray,
                   spec: StreamSpec, ref_key: str) -> Optional[OffsetObs]:
    """流「每行首样本 − 同行参考帧值」的统计量。"""
    if rows.size < 8:
        return None
    off = v_first.astype(np.int64) - ref_vals[rows].astype(np.int64)
    med = float(np.median(off))
    p25, p75 = (float(np.percentile(off, 25)), float(np.percentile(off, 75)))
    p10, p90 = (float(np.percentile(off, 10)), float(np.percentile(off, 90)))
    step, step_at = _seg_steps(off.astype(np.float64))
    span = float(ref_vals[rows[-1]] - ref_vals[rows[0]]) if rows.size > 1 else 0.0
    return OffsetObs(stream=spec.key, ref=ref_key, n=int(rows.size), med=med,
                     p25=p25, p75=p75, p10=p10, p90=p90,
                     drift_ppm=_drift_ppm(rows, off, span), step_ms=step,
                     step_at=step_at, first_sample_row=int(rows[0]),
                     last_sample_row=int(rows[-1]))


def baseline_for(spec: StreamSpec, cfg: Dict[str, Any],
                 info_baselines: Dict[str, Dict[str, Any]]) -> Tuple[Optional[float], str]:
    """取该流的偏移基线：配置 > info.json alignment。"""
    for pat, b in (cfg.get("baselines") or {}).items():
        if name_matches(spec.key, [pat]):
            if isinstance(b, dict) and b.get("offset_ms") is not None:
                return float(b["offset_ms"]), "config"
            if isinstance(b, (int, float)):
                return float(b), "config"
    b = info_baselines.get(spec.key)
    if b:
        return float(b["offset_ms"]), b.get("source", "info.json")
    return None, ""


def judge_offset(obs: OffsetObs, spec: StreamSpec, cfg: Dict[str, Any],
                 unit: str, scale: float = 1.0) -> List[Finding]:
    """C5a/C5b：偏移本身与偏移稳定性。

    scale：攒批流的形状放大系数（p95 间隔 / 标称周期）。位姿三连发时一行的
    首样本可能早于本行帧戳半个 burst，偏移的 IQR 自然比均匀流大；不放大门槛
    会把这份形状逐段报成「排空/积压在变」。
    """
    c = cfg["checks"][C_OFFSET]
    out: List[Finding] = []
    if obs.baseline_ms is not None:
        dev = obs.med / 1e6 - obs.baseline_ms
        tol = float(cfg.get("baseline_tol_ms", 25.0))
        if abs(dev) > tol:
            sev = (SEV_ERROR if abs(dev) > float(c.get("error_ms", 100.0))
                   else SEV_WARN)
            out.append(Finding(
                check=C_OFFSET, severity=sev, stream=spec.key,
                message=("相对参考列 %s 的偏移 %.3fms，与基线 %.3fms（%s）差 %+.3fms，"
                         "超出容差 ±%.1fms"
                         % (obs.ref, obs.med / 1e6, obs.baseline_ms,
                            obs.baseline_src, dev, tol)),
                metrics=obs.to_dict(),
            ))
    else:
        warn = float(c.get("warn_ms", 25.0))
        if abs(obs.med) / 1e6 > warn:
            # 同一时基（host_monotonic_ns：采集时刻列、宿主戳位姿流）的绝对偏移
            # 不是「两个钟对不齐」，而是**队列滞留差**（视频取 FIFO 队头最旧帧、
            # 样本取最新）：中位 100ms 很正常，随会话变，下游按戳重采样即可。
            # 所以只报 info；真正有缺陷的是它不稳/有台阶（下面两条）。
            same_base = (spec.clock or "") == "host_monotonic_ns"
            out.append(Finding(
                check=C_OFFSET,
                severity=SEV_INFO if same_base else SEV_WARN, stream=spec.key,
                message=(("相对参考列 %s 的偏移 %.3fms —— 同一时基，属队列滞留差"
                          "（下游按 *_ns 重采样可消除），非钟差" if same_base else
                          "相对参考列 %s 的偏移 %.3fms（无基线可比，仅按 ±%.1fms 提示）")
                         % ((obs.ref, obs.med / 1e6) if same_base else
                            (obs.ref, obs.med / 1e6, warn))),
                metrics=obs.to_dict(), bookkeeping=same_base,
            ))
    iqr = (obs.p75 - obs.p25) / 1e6
    scale = max(1.0, float(scale))
    if iqr > float(c.get("stability_warn_ms", 20.0)) * scale:
        sev = (SEV_ERROR
               if iqr > float(c.get("stability_error_ms", 60.0)) * scale
               else SEV_WARN)
        out.append(Finding(
            check=C_OFFSET, severity=sev, stream=spec.key,
            message=("偏移不稳定：IQR %.3fms（p10~p90 %.3f~%.3fms），说明该流与参考列 %s "
                     "之间的排空/积压在变" % (iqr, obs.p10 / 1e6, obs.p90 / 1e6, obs.ref)),
            metrics=obs.to_dict(),
        ))
    steps = obs.step_ms / 1e6
    if steps > float(c.get("step_warn_ms", 25.0)) * scale:
        out.append(Finding(
            check=C_OFFSET, severity=SEV_WARN, stream=spec.key,
            message=("偏移存在台阶：分段中位数最大跳 %.3fms（%s），疑似时钟重锚/追帧"
                     % (steps, obs.step_at)),
            metrics=obs.to_dict(),
        ))
    if (obs.drift_ppm is not None and abs(obs.drift_ppm) > 1000.0
            and (spec.clock or "") != "host_monotonic_ns"):
        # 同一时基的流没有「晶振差」可言，那点 ppm 只是队列积压的斜率
        out.append(Finding(
            check=C_OFFSET, severity=SEV_WARN, stream=spec.key,
            message=("偏移线性漂移 %.0f ppm（%.3fms 全程），两钟速率不一致"
                     % (obs.drift_ppm, obs.med / 1e6)),
            metrics=obs.to_dict(),
        ))
    return out


def judge_sample_offset(obs: OffsetObs, spec: StreamSpec,
                        cfg: Dict[str, Any]) -> List[Finding]:
    """C5d 样本流：**行号配对偏移**（样本戳 − 本行帧戳）。

    为什么不能像帧级列那样比「两个钟的差」：样本流是「每行一个采样值」，
    采样时刻与行时刻本来就不同刻（视频走 FIFO 队头最旧帧、力走 latest-wins
    单槽，两边队列滞留不同）。拿它当帧钟两两比稳定性，只会得到
    「两个钟不是同一个率」这种错误归因，还会把真正有用的信息淹掉：
    **中位偏移**（本例实测 0~130ms，随会话变）与**偏移稳定性**。

    所以这里只看两件事：
      · 中位偏移 —— 只报 info：这是队列滞留差，下游按 *_ns 重采样即可消除；
      · IQR / 分段台阶 —— 这才是缺陷：它随录制时长在漂，说明某一路在堆积或排空。
    """
    c = cfg["checks"][C_OFFSET]
    out: List[Finding] = []
    iqr_ms = (obs.p75 - obs.p25) / 1e6
    med_ms = obs.med / 1e6
    step_ms = obs.step_ms / 1e6
    d = obs.to_dict()
    d["kind"] = "row_pairing_offset"
    out.append(Finding(
        check=C_OFFSET, severity=SEV_INFO, stream=spec.key,
        message=("行号配对偏移：本行帧戳 − 样本戳 中位 %+.3fms"
                 "（p10~p90 %+.3f~%+.3fms，IQR %.3fms，n=%d）—— 样本与帧不同刻，"
                 "属队列滞留差；下游按 *_ns 最近邻重采样可把它压到半个采样周期内"
                 % (-med_ms, -obs.p90 / 1e6, -obs.p10 / 1e6, iqr_ms, obs.n)),
        metrics=d, bookkeeping=True))   # 逐段记账，不进汇总（见 C5 的逐流汇总）
    if iqr_ms > float(c.get("sample_pair_error_iqr_ms", 200.0)):
        sev = SEV_ERROR
    elif iqr_ms > float(c.get("sample_pair_warn_iqr_ms", 66.7)):
        sev = SEV_WARN
    else:
        sev = None
    if sev is not None:
        out.append(Finding(
            check=C_OFFSET, severity=sev, stream=spec.key,
            message=("行号配对偏移不稳定：IQR %.3fms（中位 %+.3fms，p10~p90 %+.3f~%+.3f）"
                     "—— 该流与参考列 %s 之间的排空/积压在变，按行号对齐会错得更多，"
                     "只能用 *_ns 重采样" % (iqr_ms, -med_ms, -obs.p90 / 1e6,
                                            -obs.p10 / 1e6, obs.ref)),
            metrics=d))
    if step_ms > float(c.get("sample_step_warn_ms", 100.0)):
        out.append(Finding(
            check=C_OFFSET, severity=SEV_WARN, stream=spec.key,
            message=("行号配对偏移存在台阶：分段中位数最大跳 %.3fms（%s），"
                     "疑似队列积压突增/重连重锚" % (step_ms, obs.step_at)),
            metrics=d))
    return out


# ════════════════════════════ 单段质检 ════════════════════════════

def measure_episode(data_path: str, episode: str, streams: List[StreamSpec],
                    cfg: Dict[str, Any], meta_row: Optional[Dict[str, Any]] = None,
                    info_baselines: Optional[Dict[str, Dict[str, Any]]] = None,
                    fps: float = 0.0) -> Dict[str, Any]:
    """读一个 data parquet，跑完五项检查，返回纯 dict（可跨进程传递）。

    两处口径决定这份实现的样子：
      · **稀疏列**：本段没有的列不取 —— 否则 pq.read_table 的
        `No match for FieldRef.Nested(...)` 会把整段判成「读不动」，五项一条
        都没查过（umin 2026-10 的 27 段就是这么被漏掉的）。缺列按 C4 报。
      · **样本流**：每行一个采样值的 *_ns 列里 0 = 本行没有样本，先剔掉再判
        回退/重复；缺样本只按覆盖率报，比的是行号配对偏移。
    """
    info_baselines = info_baselines or {}
    findings: List[Finding] = []
    need = sorted({s.key for s in streams}
                  | {s.value_col for s in streams if s.value_col})

    pf = pq.ParquetFile(data_path)
    file_schema = pf.schema_arrow
    n_rows = int(pf.metadata.num_rows)
    have = set(file_schema.names)
    read_cols = [c for c in need if c in have]
    table = pq.read_table(data_path, columns=read_cols) if read_cols else \
        pa.table({})
    schema = table.schema

    # ── C4：本段整列缺失（稀疏列契约允许缺列，但「每段都该有」的流缺列就是缺陷）──
    _core_frame = ("timestamp", "wall_time", "hardware_ns", "frame_index")
    if cfg["checks"][C_COUNT].get("check_missing_column", True):
        for s in streams:
            if s.key in have:
                continue
            if s.kind == "frame" and s.key not in _core_frame:
                continue   # raw_hw_ns/host_ns/depth_ns 是可选的诊断列，缺了不算缺陷
            miss_vcol = bool(s.value_col and s.value_col not in have)
            findings.append(Finding(
                check=C_COUNT, severity=SEV_WARN, stream=s.key,
                message=("本段 parquet 里整列没有 %s —— 该流 0 样本"
                         "%s。稀疏列契约允许缺列，但对「每段都该有」的流就是整流缺失"
                         % (s.key, ("（配对的数值列 %s 也缺）" % s.value_col)
                            if miss_vcol else "")),
                metrics={"missing_column": s.key, "n_rows": n_rows,
                         "missing_value_col": miss_vcol}))

    # 帧级时间轴（供 duration 与帧级检查用）
    frame_vals: Dict[str, np.ndarray] = {}
    for s in streams:
        if s.kind == "frame" and s.key in schema.names:
            frame_vals[s.key] = read_frame_column(table, s.key, s.unit, cfg)

    tcol = frame_vals.get("timestamp")
    if tcol is not None and tcol.size > 1:
        duration_s = float(tcol[-1] - tcol[0]) / 1e9
    elif "wall_time" in frame_vals and frame_vals["wall_time"].size > 1:
        duration_s = float(frame_vals["wall_time"][-1] - frame_vals["wall_time"][0]) / 1e9
    else:
        duration_s = 0.0

    # ── 帧级：C1/C2/C3 + frame_index 连续性 + meta 长度 ──
    for s in streams:
        if s.kind != "frame" or s.key not in frame_vals:
            continue
        v = frame_vals[s.key]
        if v.size == 0:
            continue
        nominal = (1e9 / s.rate_hz) if (s.rate_hz and s.unit != "index") else (
            1.0 if s.unit == "index" else None)
        findings += check_regression(v, s.unit, s, cfg, nominal)
        findings += check_duplicate(v, s.unit, s, cfg)
        findings += check_jump(v, s.unit, s, cfg, nominal)
        if s.key == "frame_index" and cfg["checks"][C_COUNT].get("check_frame_index", True):
            if v.size and int(v[0]) != 0:
                findings.append(Finding(
                    check=C_COUNT, severity=SEV_ERROR, stream=s.key,
                    message="frame_index 不从 0 开始（首行 = %d）" % int(v[0]),
                    metrics={"first": int(v[0])}))
            if v.size and int(v[-1]) != n_rows - 1:
                findings.append(Finding(
                    check=C_COUNT, severity=SEV_WARN, stream=s.key,
                    message=("frame_index 末值 %d ≠ 行数-1 %d（有空洞或多帧）"
                             % (int(v[-1]), n_rows - 1)),
                    metrics={"last": int(v[-1]), "n_rows": n_rows}))

    findings += check_meta_length(episode, n_rows, duration_s, meta_row, cfg)

    # ── 列表级：C1/C2/C3 + C4 单流 + 偏移观测 ──
    stream_summaries: Dict[str, Dict[str, Any]] = {}
    offsets: List[OffsetObs] = []
    first_of_row: Dict[str, np.ndarray] = {}
    rows_of: Dict[str, np.ndarray] = {}
    lengths_of: Dict[str, np.ndarray] = {}
    samples_of: Dict[str, int] = {}

    for s in streams:
        if s.kind != "list" or s.key not in schema.names:
            continue
        lengths, flat = read_list_column(table, s.key, s.unit, cfg)
        samples_of[s.key] = int(flat.size)
        total = int(lengths.sum())
        if int(flat.size) != total:      # flatten 与长度不一致 = parquet 层就坏了
            findings.append(Finding(
                check=C_COUNT, severity=SEV_ERROR, stream=s.key,
                message=("列表列自相矛盾：长度和 %d ≠ 展平元素数 %d"
                         % (total, int(flat.size))),
                metrics={"sum_lengths": total, "flat_size": int(flat.size)}))
        lengths_of[s.key] = lengths

        # 标称周期：声明率优先，否则用**平均**间隔。不能用中位数：攒批到达的
        # 流（SLAM 位姿实测是 20/20/60ms 三连发）间隔分布双峰，中位数落在短的
        # 那一峰上，于是同一份数据的 60ms 间隔被读成「3× 标称 = 丢了 2 个样本」。
        nominal = (1e9 / s.rate_hz) if s.rate_hz else None
        row_of = np.repeat(np.arange(lengths.size, dtype=np.int64), lengths)
        starts = np.concatenate([[0], np.cumsum(lengths)[:-1]])
        elem_of = np.arange(flat.size, dtype=np.int64) - np.repeat(starts, lengths)
        gap_floor = None
        gap_error = None
        bursty = False
        burst_scale = 1.0
        if nominal is None and flat.size > 10 and flat[-1] > flat[0]:
            nominal = float(flat[-1] - flat[0]) / float(flat.size - 1)
        iv = np.diff(flat)
        iv = iv[iv > 0]
        if iv.size > 10:
            p95 = float(np.percentile(iv, 95))
            med_iv = float(np.median(iv))
            if med_iv > 0 and p95 >= 1.5 * med_iv:
                bursty = True
                factor = float(cfg["checks"][C_JUMP].get("burst_floor_factor", 1.5))
                gap_floor = p95 * factor
                gap_error = p95 * factor * 1.34      # ≈ 2×p95：真断流才升 error
                if nominal and nominal > 0:
                    burst_scale = max(1.0, p95 / float(nominal))
        findings += check_regression(flat, s.unit, s, cfg, nominal, row_of, elem_of)
        findings += check_duplicate(flat, s.unit, s, cfg, row_of, elem_of)
        findings += check_jump(flat, s.unit, s, cfg, nominal, row_of, elem_of,
                               gap_floor=gap_floor, gap_error=gap_error)

        sub, summ = check_stream_counts(s, lengths, n_rows, int(flat.size),
                                        (float(flat[-1] - flat[0]) / 1e9
                                         if flat.size > 1 else 0.0),
                                        duration_s, fps, cfg, bursty=bursty)
        findings += sub
        summ["nominal_step_ns"] = None if nominal is None else round(float(nominal), 3)
        summ["nominal_rate_hz"] = None if not nominal else round(1e9 / float(nominal), 4)
        if bursty:
            summ["bursty"] = True
            summ["gap_floor_ns"] = round(float(gap_floor), 3)
            summ["gap_error_ns"] = round(float(gap_error), 3)
        stream_summaries[s.key] = summ

        nz = np.where(lengths > 0)[0]
        if nz.size:
            rows_of[s.key] = nz
            first_of_row[s.key] = flat[np.cumsum(lengths)[nz] - lengths[nz]]
            # 该流在**共同参考列**上的跨度（归因跨流样本数差时要用它；
            # 用各流自己的戳会被设备钟自身的速率偏差带偏）
            if s.ref and s.ref in frame_vals and nz.size > 1:
                rv = frame_vals[s.ref]
                summ["ref_span_sec"] = round(
                    float(rv[nz[-1]] - rv[nz[0]]) / 1e9, 4)
            if s.ref and s.ref in frame_vals:
                obs = compute_offset(first_of_row[s.key], nz, frame_vals[s.ref], s, s.ref)
                if obs:
                    summ["pair_offset_median_ms"] = round(obs.med / 1e6, 3)
                    summ["pair_offset_iqr_ms"] = round((obs.p75 - obs.p25) / 1e6, 3)
                    summ["pair_offset_n"] = int(obs.n)
                    summ["pair_offset_ref"] = obs.ref
                    b, src = baseline_for(s, cfg, info_baselines)
                    obs.baseline_ms, obs.baseline_src = b, src
                    offsets.append(obs)
                    findings += judge_offset(obs, s, cfg, s.unit, scale=burst_scale)

    # ── 样本流：每行至多一个采样值（0 = 本行没有样本）──
    for s in streams:
        if s.kind != "sample" or s.key not in have:
            continue
        v = read_frame_column(table, s.key, s.unit, cfg)
        if v.size == 0:
            continue
        miss_val = (s.missing if s.missing is not None
                    else cfg.get("sample_missing_value", 0))
        present = (v != miss_val)
        lengths = present.astype(np.int64)
        vals = v[present]
        rows = np.where(present)[0]
        samples_of[s.key] = int(vals.size)
        lengths_of[s.key] = lengths
        nominal = (1e9 / s.rate_hz) if s.rate_hz else None
        if nominal is None and vals.size > 10:
            med = float(np.median(np.diff(vals)))
            if med > 0:
                nominal = med
        span = (float(vals[-1] - vals[0]) / 1e9) if vals.size > 1 else 0.0
        if vals.size > 1:
            findings += check_regression(vals, s.unit, s, cfg, nominal, rows)
            for f in check_duplicate(vals, s.unit, s, cfg, rows):
                if f.severity == SEV_ERROR:
                    # 同一个采样时刻被挂进多行 = 最近邻挂靠/保持，不是缺陷
                    f.severity = SEV_INFO
                    f.message = ("同一样本时刻被挂进多行（最近邻重采样会去重）："
                                 + f.message)
                findings.append(f)
        sub, summ = check_stream_counts(s, lengths, n_rows, int(vals.size), span,
                                        duration_s, fps, cfg, sample_kind=True)
        findings += sub
        summ["missing_rows"] = int(v.size - vals.size)
        summ["missing_ratio"] = round(float(v.size - vals.size) / max(1, v.size), 6)
        summ["nominal_step_ns"] = None if nominal is None else round(float(nominal), 3)
        summ["nominal_rate_hz"] = None if not nominal else round(1e9 / float(nominal), 4)
        stream_summaries[s.key] = summ
        if rows.size > 1 and s.ref and s.ref in frame_vals:
            rv = frame_vals[s.ref]
            summ["ref_span_sec"] = round(float(rv[rows[-1]] - rv[rows[0]]) / 1e9, 4)
            obs = compute_offset(vals, rows, rv, s, s.ref)
            if obs:
                offsets.append(obs)
                summ["pair_offset_median_ms"] = round(obs.med / 1e6, 3)
                summ["pair_offset_iqr_ms"] = round((obs.p75 - obs.p25) / 1e6, 3)
                summ["pair_offset_n"] = int(obs.n)
                summ["pair_offset_ref"] = obs.ref
                findings += judge_sample_offset(obs, s, cfg)

    # ── C4：成对列存在性对齐 ──
    if cfg["checks"][C_COUNT].get("check_pair_presence", True):
        for s in streams:
            if s.kind != "list" or s.value_col is None or s.value_col not in schema.names:
                continue
            if s.key not in lengths_of:
                continue
            try:
                vcounts = read_value_sample_counts(table, s.value_col, s.sample_width)
            except Exception as e:                       # 列类型不认识就跳过
                findings.append(Finding(
                    check=C_COUNT, severity=SEV_INFO, stream=s.key,
                    message="配对列 %s 无法解析（%s），跳过存在性对齐" % (s.value_col, e),
                    metrics={}))
                continue
            if vcounts.size != lengths_of[s.key].size:
                continue
            mism = np.where((lengths_of[s.key] > 0) != (vcounts > 0))[0]
            if mism.size:
                findings.append(Finding(
                    check=C_COUNT, severity=SEV_WARN, stream=s.key,
                    message=("时间戳列与配对的数值列 %s 存在性不一致：%d 行「一边有、一边空」"
                             "（前几行 %s）" % (s.value_col, mism.size, mism[:10].tolist())),
                    metrics={"value_col": s.value_col, "mismatch_rows": int(mism.size),
                             "n_rows": n_rows, "first_rows": mism[:10].tolist()},
                ))

    # ── C4：跨流样本数是否对得上 ──
    findings += check_cross_stream_counts(streams, samples_of, stream_summaries,
                                          lengths_of, n_rows, cfg)

    # ── C5：同域流两两偏移 + 帧级列两两稳定性 ──
    findings += check_cross_stream_offsets(streams, first_of_row, rows_of, frame_vals,
                                           cfg, info_baselines)
    findings += check_frame_pair_stability(streams, frame_vals, cfg)

    # ── C4：与录制器自报的 drop_stats 对账 ──
    findings += check_drop_stats(streams, stream_summaries, meta_row, cfg)

    sev_rank = max([SEV_RANK[f.severity] for f in findings], default=-1)
    return {
        "episode": episode,
        "episode_index": (meta_row or {}).get("episode_index"),
        "rows": int(n_rows),
        "duration_sec": round(duration_s, 6),
        "counts": {"frame": int(n_rows), **samples_of},
        "streams": stream_summaries,
        "offsets": [o.to_dict() for o in offsets],
        "findings": [f.to_dict() for f in findings],
        "worst_severity": (SEV_ERROR if sev_rank >= SEV_RANK[SEV_ERROR]
                           else SEV_WARN if sev_rank == SEV_RANK[SEV_WARN] else SEV_INFO),
        "status": ("error" if sev_rank >= SEV_RANK[SEV_ERROR]
                   else "warn" if sev_rank == SEV_RANK[SEV_WARN] else "ok"),
    }


def _stat_tokens(s: str) -> set:
    return {t for t in re.split(r"[._]+", s.lower()) if t and t not in _STAT_STOPWORDS}


def match_drop_stats(streams: List[StreamSpec], drop: Dict[str, Any],
                     cfg: Dict[str, Any]) -> List[Tuple[str, str, List[str]]]:
    """把 drop_stats 的键配到流上 → [(stat_key, kind, [stream_key, ...])]。

    kind: "empty"（frames_without_*，与本工具的「空行数」同义）
          "samples"（*_samples，与本工具的「样本总数」同义）

    自动配对规则：把键与流名都切成 token，取交集非空者；样本类的键若含
    unpaired/unknown/dropped 等限定词则跳过（它们不是样本总数）。
    映射错了不会静默：对不上账时报 warn 并提示用 drop_stats_map 写死。
    """
    explicit = cfg.get("drop_stats_map") or {}
    out: List[Tuple[str, str, List[str]]] = []
    for key in drop:
        if key in explicit:
            v = explicit[key]
            keys = [v] if isinstance(v, str) else list(v)
            kind = "empty" if key.startswith("frames_without_") else "samples"
            out.append((key, kind, [k for k in keys if k]))
            continue
        if key.startswith("frames_without_"):
            kind, body = "empty", key[len("frames_without_"):]
        elif key.endswith("_samples") or "_samples_" in key:
            kind, body = "samples", key
        else:
            continue
        toks = _stat_tokens(body)
        if not toks or (kind == "samples" and (toks & _STAT_DISQUALIFIERS)):
            continue
        scored = []
        for s in streams:
            if s.kind != "list":
                continue
            n = len(toks & _stat_tokens(s.key))
            if n:
                scored.append((n, s.key))
        if not scored:
            continue
        best = max(n for n, _ in scored)
        out.append((key, kind, sorted(k for n, k in scored if n == best)))
    return out


def check_drop_stats(streams: List[StreamSpec], summaries: Dict[str, Dict[str, Any]],
                     meta_row: Optional[Dict[str, Any]], cfg: Dict[str, Any]) -> List[Finding]:
    """C4 子检查：与录制器**自报**的 drop_stats 对账。

    这是最有价值的一条交叉验证 —— 空行/样本数是本工具从时间戳列**独立数出来**的，
    drop_stats 是录制器**自己记账**的；两边对得上，说明「数出来的缺陷」不是工具的
    错觉；对不上，则必有一方错，值得人看一眼。
    """
    if not meta_row:
        return []
    raw = meta_row.get("drop_stats")
    if not raw:
        return []
    try:
        drop = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except Exception:
        return []
    out: List[Finding] = []
    # 离线修正留痕：drop_stats 是录制器「收到多少样本」的历史事实（不能改），
    # 离线工具（如按时间戳重新分箱）删掉的样本记在 offline_adjust 里。
    # 对账口径 = 现存样本数 + 离线删除数，这样「自报 = 现存 + 离线删」仍然是一条
    # 真检查（对不上照样报），但不会把有据可查的离线修正误判成缺陷。
    adj_raw = drop.get("offline_adjust")
    adj = {}
    if isinstance(adj_raw, dict):
        adj = {k: int(v) for k, v in adj_raw.items()
               if isinstance(v, (int, float))}
    for key, kind, skeys in match_drop_stats(streams, drop, cfg):
        want = drop.get(key)
        if not isinstance(want, (int, float)):
            continue
        # 一条 stat 常同时配到同一物理流的多个时间戳列（宿主戳 + 设备戳），
        # 它们的计数本来就相等 —— 所以**不能求和**，要逐列比、任一列相等即算对上。
        got: Dict[str, int] = {}
        for sk in skeys:
            summ = summaries.get(sk)
            if not summ:
                continue
            got[sk] = int(summ.get("empty_rows") or 0) if kind == "empty" \
                else int(summ.get("samples") or 0) + adj.get(key, 0)
        if not got:
            continue
        vals = sorted(set(got.values()))
        hit = [k for k, v in got.items() if v == int(want)]
        detail = "、".join("%s=%d" % (k, v) for k, v in got.items())
        note = ("（含 offline_adjust 离线删除 %d）" % adj[key]) if adj.get(key) else ""
        if hit:
            out.append(Finding(
                check=C_COUNT, severity=SEV_INFO, stream="drop_stats:%s" % key,
                message=("与录制器自报计数一致：%s = %d ✓%s（%s）"
                         % (key, int(want), note, detail)),
                metrics={"stat_key": key, "reported": int(want), "measured": got,
                         "offline_adjust": adj.get(key, 0), "kind": kind},
                bookkeeping=True))
        else:
            out.append(Finding(
                check=C_COUNT, severity=SEV_WARN, stream="drop_stats:%s" % key,
                message=("与录制器自报计数对不上账：drop_stats['%s']=%d，"
                         "本工具从时间戳列数出来的是 %s%s。若这是键↔流配对错了，"
                         "请在配置的 drop_stats_map 里写死映射"
                         % (key, int(want), "/".join(str(v) for v in vals),
                            note or "（无离线删除记录）")),
                metrics={"stat_key": key, "reported": int(want), "measured": got,
                         "offline_adjust": adj.get(key, 0), "kind": kind}))
        if len(vals) > 1:
            out.append(Finding(
                check=C_COUNT, severity=SEV_WARN, stream="drop_stats:%s" % key,
                message=("同一物理流的多个时间戳列自相矛盾（%s），parquet 层就不一致"
                         % detail),
                metrics={"stat_key": key, "measured": got, "kind": kind}))
    return out


def check_meta_length(episode: str, n_rows: int, duration_s: float,
                      meta_row: Optional[Dict[str, Any]], cfg: Dict[str, Any]) -> List[Finding]:
    """C4 子检查：meta/episodes 声明与实际是否一致。"""
    if not cfg["checks"][C_COUNT].get("check_meta_length", True) or not meta_row:
        return []
    out: List[Finding] = []
    L = meta_row.get("length")
    if isinstance(L, (int, float)) and int(L) != n_rows:
        out.append(Finding(
            check=C_COUNT, severity=SEV_ERROR, stream="meta.length",
            message="meta 声明 length=%d，data 实际 %d 行（差 %+d）" % (int(L), n_rows, n_rows - int(L)),
            metrics={"meta_length": int(L), "data_rows": n_rows}))
    sf, ef = meta_row.get("start_frame_index"), meta_row.get("end_frame_index")
    if isinstance(sf, (int, float)) and isinstance(ef, (int, float)):
        if int(ef) - int(sf) + 1 != n_rows:
            out.append(Finding(
                check=C_COUNT, severity=SEV_WARN, stream="meta.frame_range",
                message=("meta 的 frame_index 区间 [%d, %d] 跨 %d 帧 ≠ 实际 %d 行"
                         % (int(sf), int(ef), int(ef) - int(sf) + 1, n_rows)),
                metrics={"start_frame_index": int(sf), "end_frame_index": int(ef),
                         "data_rows": n_rows}))
    d = meta_row.get("duration_sec")
    if isinstance(d, (int, float)) and duration_s > 0:
        rel = abs(float(d) - duration_s) / max(1e-9, duration_s)
        if rel > 0.01:
            out.append(Finding(
                check=C_COUNT, severity=SEV_WARN, stream="meta.duration_sec",
                message=("meta 声明 duration_sec=%.3f，按 timestamp 实测 %.3f（差 %+.2f%%）"
                         % (float(d), duration_s, (duration_s - float(d)) / float(d) * 100)),
                metrics={"meta_duration_sec": float(d), "measured_sec": duration_s}))
    return out


def check_cross_stream_counts(streams: List[StreamSpec], samples_of: Dict[str, int],
                              summaries: Dict[str, Dict[str, Any]],
                              lengths_of: Dict[str, np.ndarray], n_rows: int,
                              cfg: Dict[str, Any]) -> List[Finding]:
    """C4 子检查：不同流的样本数/帧数是否互相对得上。

    只比「期望同频」的流：采样率相同（或都由中位间隔反推得到同一周期）。
    """
    c = cfg["checks"][C_COUNT]
    out: List[Finding] = []
    kind_of = {s.key: s.kind for s in streams}
    # 样本流不参与「同频流样本数对照」：它的样本数 = 每行有没有采样值的覆盖率，
    # 两边比值由采样相位决定（7%~15% 摆动），不是「丢样本」。
    keys = [k for k in samples_of if k in summaries and kind_of.get(k) != "sample"]
    # 分组：**同标称率且同时钟域**才互为对照。只按率分组会把宿主戳和设备戳混到
    # 一起比（两个域的时间基不同，比出来的差没有意义，归因也会给出相反的结论）。
    groups: Dict[Tuple[float, str], List[str]] = {}
    for k in keys:
        r = summaries[k].get("nominal_rate_hz")
        if not r:
            continue
        clock = next((s.clock for s in streams if s.key == k), None) or "-"
        placed = False
        for gr in groups:
            if gr[1] == clock and abs(gr[0] - r) / max(gr[0], r) < 0.005:
                groups[gr].append(k)
                placed = True
                break
        if not placed:
            groups[(float(r), clock)] = [k]
    for (rate, clock), ks in groups.items():
        if len(ks) < 2:
            continue
        kbase = max(ks, key=lambda x: samples_of[x])
        base = samples_of[kbase]
        for k in sorted(ks, key=lambda x: samples_of[x]):
            if k == kbase:
                continue
            n = samples_of[k]
            tol = max(int(c.get("count_tol_abs", 2)),
                      int(float(c.get("count_tol_rel", 0.001)) * base))
            deficit = base - n
            if deficit <= tol:
                continue
            # ── 归因：样本数差 = 速率失配 × 时长 + 起止不齐 × 速率 ──
            # 「少 N 个」有两种完全不同的成因，处置也相反：
            #   速率失配：两个设备各自的钟差一点，差数随时间线性累积；两端其实是对齐的，
            #             **裁尾没用**（裁掉的那点时长只贡献 N×Δt/T 个样本）。
            #   起止不齐：某条流在段首/段尾真的没数据，差数是「整块」的，裁尾能对齐。
            # 不分开说，读的人只会看到一个「少了 N 个」，然后去裁尾白丢数据。
            #
            # 跨度必须用**共同时间基**（参考帧列）量，不能用各流自己的戳 ——
            # 设备钟自身的速率偏差会让两个「设备秒」不一样长，归因直接反号。
            # 同一物理流常有两个时钟副本（宿主戳 + 设备戳，共享同一个 value_col）；
            # 拿不到参考列的那个就借副本的时基 —— 它们是同一批样本，跨度/速率同源。
            def _phys(key: str):
                s0 = next((x for x in streams if x.key == key), None)
                cands = [key]
                if s0 is not None and s0.value_col:
                    cands += [x.key for x in streams
                              if x.value_col == s0.value_col and x.key != key]
                for c in cands:
                    sm = summaries.get(c) or {}
                    if sm.get("ref_span_sec") is not None:
                        return float(sm["ref_span_sec"]), float(
                            sm.get("rate_measured_hz") or rate or 0.0), c
                return None, None, None

            s_k, r_k, via_k = _phys(k)
            s_b, r_b, via_b = _phys(kbase)
            if s_k is None or s_b is None:
                out.append(Finding(
                    check=C_COUNT, severity=SEV_WARN, stream=k,
                    message=("同频（%.2f Hz，域 %s）各流样本数不齐：本流 %d，最多者 %s %d，"
                             "少 %d 个（≈%.2fs）。这两条流没有共同参考列，无法归因到"
                             "「速率失配」还是「起止不齐」，需人工看一眼两端"
                             % (rate, clock, n, kbase, base, deficit,
                                (deficit / rate) if rate else 0.0)),
                    metrics={"stream_samples": n, "group_max": base, "delta": deficit,
                             "group": sorted(ks), "rate_hz": rate, "clock": clock,
                             "baseline_stream": kbase, "cause": "unknown"}))
                continue
            rate_term = (r_b - r_k) * s_k                      # 速率失配贡献的样本数
            span_term = (s_b - s_k) * (r_k + r_b) / 2.0        # 起止不齐贡献的样本数
            ppm = ((r_b - r_k) / ((r_k + r_b) / 2.0) * 1e6) if (r_k + r_b) else 0.0
            via = ""
            if via_k != k or via_b != kbase:
                via = "（时基取自同源的 %s%s）" % (
                    via_k if via_k != k else "",
                    ("、" + via_b) if via_b != kbase else "")
            if abs(rate_term) >= abs(span_term):
                sev = (SEV_WARN if abs(ppm) > float(c.get("rate_mismatch_warn_ppm", 2000.0))
                       else SEV_INFO)
                why = ("速率失配：两个独立设备时钟差 %.0f ppm（本流 %.3f Hz vs %s %.3f Hz），"
                       "差数随时间线性累积，两端其实是对齐的（共同参考列上的跨度 "
                       "%.3fs vs %.3fs）—— 不是尾部丢数据，**裁尾无效**"
                       % (ppm, r_k, kbase, r_b, s_k, s_b))
            else:
                sev = SEV_WARN
                why = ("起止不齐：在共同参考列上本流跨度 %.3fs vs %s %.3fs，首尾真的少了"
                       "数据 —— 裁尾/裁头可对齐" % (s_k, kbase, s_b))
            out.append(Finding(
                check=C_COUNT, severity=sev, stream=k,
                message=("同频（%.2f Hz，域 %s）各流样本数不齐：本流 %d，最多者 %s %d，"
                         "少 %d 个（≈%.2fs）。归因：%s%s（速率项 %.1f 个、起止项 %.1f 个）"
                         % (rate, clock, n, kbase, base, deficit,
                            (deficit / rate) if rate else 0.0, why, via,
                            rate_term, span_term)),
                metrics={"stream_samples": n, "group_max": base, "delta": deficit,
                         "group": sorted(ks), "rate_hz": rate, "clock": clock,
                         "baseline_stream": kbase,
                         "rate_term_samples": round(rate_term, 2),
                         "span_term_samples": round(span_term, 2),
                         "rate_gap_ppm": round(ppm, 1),
                         "ref_span_sec": round(s_k, 4),
                         "ref_span_baseline_sec": round(s_b, 4),
                         "cause": "rate_mismatch" if abs(rate_term) >= abs(span_term)
                                  else "span_gap"}))
    # 空行集合是否一致（同一时刻有的流有、有的没有）
    empties = {k: set(np.where(lengths_of[k] == 0)[0].tolist())
               for k in lengths_of if kind_of.get(k) != "sample"}
    lk = [k for k in empties if empties[k]]
    for i in range(len(lk)):
        for j in range(i + 1, len(lk)):
            a, b = lk[i], lk[j]
            only_a = empties[a] - empties[b]
            only_b = empties[b] - empties[a]
            if only_a and only_b and len(only_a) + len(only_b) > 2:
                out.append(Finding(
                    check=C_COUNT, severity=SEV_INFO, stream="%s|%s" % (a, b),
                    message=("两条流各自独立的掉样本行：%s 独有 %d 行、%s 独有 %d 行 —— "
                             "不是同一次整机停顿" % (a, len(only_a), b, len(only_b))),
                    metrics={"only": {a: sorted(only_a)[:20], b: sorted(only_b)[:20]}}))
    return out


def check_cross_stream_offsets(streams: List[StreamSpec], first_of_row: Dict[str, np.ndarray],
                               rows_of: Dict[str, np.ndarray], frame_vals: Dict[str, np.ndarray],
                               cfg: Dict[str, Any],
                               info_baselines: Optional[Dict[str, Dict[str, Any]]] = None
                               ) -> List[Finding]:
    """C5c 同域两流之间的偏移（例如左手套 vs 右手套失同步）。

    **基线感知**：两流各自的基线之差就是「期望的相互偏移」。真实数据集里手套流的
    基线是 +85ms（info.json alignment.glove_shift_ms，为对齐视频而故意平移），
    SLAM 流基线是 0 —— 所以「手套 − SLAM ≈ 85ms」是设计要求，不是失同步。
    只有**偏离这个期望值**才算问题。
    """
    c = cfg["checks"][C_OFFSET]
    info_baselines = info_baselines or {}
    out: List[Finding] = []
    by_clock: Dict[str, List[StreamSpec]] = {}
    for s in streams:
        # 只比「**都**锚定到同一个参考帧列」的流。没有参考列的流（例如左右手套
        # 各自的设备计数器：两个独立设备、开机时刻不同、纪元本来就差几百 ms 到
        # 几秒）无法锚定到共同时间轴，拿它们的绝对值相减只会得到「各自何时上电」，
        # 不是失同步 —— 那种比较没有意义，直接不比。
        if s.kind == "list" and s.key in first_of_row and s.clock and s.ref:
            by_clock.setdefault(s.clock, []).append(s)
    for clock, specs in by_clock.items():
        if len(specs) < 2:
            continue
        for i in range(len(specs)):
            for j in range(i + 1, len(specs)):
                a, b = specs[i], specs[j]
                if a.ref != b.ref:
                    continue
                # 只比「标称率相同」的两条流，避免拿 61Hz 比 500Hz
                if (a.rate_hz and b.rate_hz
                        and abs(a.rate_hz - b.rate_hz) / max(a.rate_hz, b.rate_hz) > 0.01):
                    continue
                ra_, rb_ = rows_of[a.key], rows_of[b.key]
                common = np.intersect1d(ra_, rb_, assume_unique=True)
                if common.size < 8:
                    continue
                ia = {int(r): k for k, r in enumerate(ra_)}
                ib = {int(r): k for k, r in enumerate(rb_)}
                da = np.array([first_of_row[a.key][ia[int(r)]] for r in common], dtype=np.int64)
                db = np.array([first_of_row[b.key][ib[int(r)]] for r in common], dtype=np.int64)
                diff = da - db
                med = float(np.median(diff)) / 1e6
                iqr = (float(np.percentile(diff, 75)) - float(np.percentile(diff, 25))) / 1e6
                ba, _ = baseline_for(a, cfg, info_baselines)
                bb, _ = baseline_for(b, cfg, info_baselines)
                expected = ((ba or 0.0) - (bb or 0.0)) if (ba is not None or bb is not None) else 0.0
                dev = med - expected
                # 纪元不同（如左右手套各自的设备计数器，开机时刻不同）：
                # 绝对偏移没有意义，只能报 info，真正的同步性看宿主戳那一对。
                if abs(med) > float(c.get("epoch_gap_ms", 1000.0)):
                    out.append(Finding(
                        check=C_OFFSET, severity=SEV_INFO,
                        stream="%s|%s" % (a.key, b.key),
                        message=("同域（%s）两条流的计数器纪元相差 %+.3fms —— "
                                 "属独立计数器各自的起点，绝对偏移不可比，"
                                 "同步性请参照它们的宿主戳流" % (clock, med)),
                        metrics={"clock": clock, "a": a.key, "b": b.key,
                                 "offset_median_ms": round(med, 4),
                                 "offset_iqr_ms": round(iqr, 4),
                                 "epoch_gap": True, "n": int(common.size)}))
                    continue
                if abs(dev) > float(c.get("pair_warn_ms", 25.0)):
                    sev = (SEV_ERROR if abs(dev) > float(c.get("pair_error_ms", 100.0))
                           else SEV_WARN)
                    out.append(Finding(
                        check=C_OFFSET, severity=sev, stream="%s|%s" % (a.key, b.key),
                        message=("同域（%s）两条流失同步：%s − %s 中位 %+.3fms，"
                                 "期望 %+.3fms（基线差），偏离 %+.3fms"
                                 "（IQR %.3fms，n=%d）"
                                 % (clock, a.key, b.key, med, expected, dev,
                                    iqr, common.size)),
                        metrics={"clock": clock, "a": a.key, "b": b.key,
                                 "offset_median_ms": round(med, 4),
                                 "offset_expected_ms": round(expected, 4),
                                 "offset_dev_ms": round(dev, 4),
                                 "offset_iqr_ms": round(iqr, 4), "n": int(common.size)}))
    return out


def check_frame_pair_stability(streams: List[StreamSpec], frame_vals: Dict[str, np.ndarray],
                               cfg: Dict[str, Any]) -> List[Finding]:
    """C5d 帧级时间列两两之间：绝对值可能跨纪元（无意义），但**稳定性**永远有意义。"""
    c = cfg["checks"][C_OFFSET]
    out: List[Finding] = []
    keys = [s.key for s in streams if s.kind == "frame" and s.key in frame_vals
            and s.unit != "index"]
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            a, b = keys[i], keys[j]
            va, vb = frame_vals[a], frame_vals[b]
            n = min(va.size, vb.size)
            if n < 32:
                continue
            diff = va[:n].astype(np.int64) - vb[:n].astype(np.int64)
            med = float(np.median(diff))
            iqr = (float(np.percentile(diff, 75)) - float(np.percentile(diff, 25))) / 1e6
            step, step_at = _seg_steps(diff.astype(np.float64))
            span = float(max(1, abs(vb[-1] - vb[0])))
            drift = _drift_ppm(np.arange(n), diff, span)
            if iqr > float(c.get("frame_pair_stability_warn_ms", 20.0)):
                # 归因要分清三种「帧级列」：
                #   wall_time     写入侧时刻（落盘那一刻），与帧采集时刻之差 = 队列积压
                #   raw_hw_ns     归一化**前**的设备戳，与 hardware_ns 之差 = 均匀化改写量
                #   其余          真帧时钟，两个之差在漂才是钟/口径问题
                if "wall_time" in (a, b):
                    why = ("写入侧时刻与帧采集时刻之差在漂 —— 队列积压/写入延迟在变，"
                           "不是钟差；跨模态对齐请用 timestamp/hardware_ns，别用 wall_time")
                elif "raw_hw_ns" in (a, b):
                    why = ("归一前后的帧戳差在漂 —— 硬件戳被均匀化重写过，"
                           "说明该段原始帧节奏不齐")
                else:
                    why = ("同一时基的两个帧级时刻对不上 —— 有行的 timestamp 落到了"
                           "另一条口径（采集侧 vs 写入侧），跨模态对齐会错位")
                out.append(Finding(
                    check=C_OFFSET, severity=SEV_WARN, stream="%s|%s" % (a, b),
                    message=("帧级时刻的差在漂：IQR %.3fms（中位 %+.3fms，%s vs %s）。%s"
                             % (iqr, med / 1e6, a, b, why)),
                    metrics={"a": a, "b": b, "diff_median_ms": round(med / 1e6, 4),
                             "diff_iqr_ms": round(iqr, 4),
                             "drift_ppm": None if drift is None else round(drift, 2),
                             "max_segment_step_ms": round(step / 1e6, 4),
                             "step_at": step_at}))
    return out


# ════════════════════════════ 数据集层 ════════════════════════════

def load_info(root: str) -> Dict[str, Any]:
    p = os.path.join(root, "meta", "info.json")
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print("!! meta/info.json 解析失败：%s" % e, file=sys.stderr)
    return {}


def info_baselines_from_alignment(info: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """info.json 里手套流的内建偏移基线（C5 用它当期望值）。

    优先读 `alignment.qc_offset_baseline_ms` —— 这是**实测口径**的基线，即
    「该流每行首样本 − 同行 wall_time」的期望值，也是本工具 C5 真正比的那个量。

    回退到旧的 `alignment.glove_shift_ms`：它原本记的是「时间戳被平移了多少」，
    与 C5 口径差一个「手套样本相对帧起点的固有相位」（约 8ms），而且多批次数据集里
    一个标量根本表达不了逐段不同的平移量。老数据集仍按老口径读，不做破坏性变更。
    """
    out: Dict[str, Dict[str, Any]] = {}
    al = info.get("alignment") or {}
    base = al.get("qc_offset_baseline_ms")
    src = "info.json alignment.qc_offset_baseline_ms"
    if not isinstance(base, (int, float)):
        base = al.get("glove_shift_ms")
        src = "info.json alignment.glove_shift_ms"
    if isinstance(base, (int, float)):
        for k in (info.get("features") or {}):
            if k.endswith("_glove_ts_ns"):
                out[k] = {"offset_ms": float(base), "source": src}
    return out


def list_episodes(root: str, info: Dict[str, Any]) -> List[Dict[str, Any]]:
    """枚举 episode：data/chunk-*/episode-*.parquet，并挂上 meta 行。"""
    import glob
    data_tmpl = info.get("data_path") or "data/chunk-{c:03d}/episode-{f:03d}.parquet"
    chunks_size = int(info.get("chunks_size") or 1000)
    paths = sorted(glob.glob(os.path.join(root, "data", "chunk-*", "episode-*.parquet")))
    if not paths:   # 兜底 1：任意 data/**/*.parquet
        paths = sorted(glob.glob(os.path.join(root, "data", "**", "*.parquet"),
                                 recursive=True))
    if not paths:   # 兜底 2：目录本身就是一堆 parquet（无 meta/、无 data/ 的裸导出）
        paths = sorted(glob.glob(os.path.join(root, "*.parquet")))
    eps: List[Dict[str, Any]] = []
    for p in paths:
        rel = os.path.relpath(p, root).replace(os.sep, "/")
        m = re.search(r"episode-(\d+)\.parquet$", os.path.basename(p))
        stem = m.group(1) if m else os.path.splitext(os.path.basename(p))[0]
        meta_path = None
        m2 = re.search(r"data/chunk-(\d+)/", rel)
        if m2:
            cand = os.path.join(root, "meta", "episodes",
                                "chunk-%s" % m2.group(1), "episode-%s.parquet" % stem)
            if os.path.isfile(cand):
                meta_path = cand
        meta_row = None
        if meta_path:
            try:
                rows = pq.read_table(meta_path).to_pylist()
                meta_row = rows[0] if rows else None
            except Exception:
                meta_row = None
        eps.append({"episode": stem, "data_path": p, "rel": rel,
                    "meta_path": meta_path, "meta": meta_row,
                    "chunk": int(m2.group(1)) if m2 else None,
                    "file_no": int(stem) if stem.isdigit() else None,
                    "global_n": (int(m2.group(1)) * chunks_size + int(stem) + 1)
                    if (m2 and stem.isdigit()) else None,
                    "data_template": data_tmpl})
    return eps


def check_structure(root: str, info: Dict[str, Any], eps: List[Dict[str, Any]]) -> List[Finding]:
    """C6 结构一致性（数据集级，不属于五项但会让五项整体失真）。"""
    out: List[Finding] = []
    chunks_size = int(info.get("chunks_size") or 1000)
    seen: Dict[int, List[str]] = {}
    mismatch = []
    for e in eps:
        row = e.get("meta") or {}
        ei = row.get("episode_index")
        if isinstance(ei, (int, float)):
            seen.setdefault(int(ei), []).append(e["episode"])
            if e.get("global_n") is not None and int(ei) != e["global_n"]:
                mismatch.append({"episode": e["episode"], "meta_episode_index": int(ei),
                                 "formula_global_n": e["global_n"],
                                 "chunk": e["chunk"], "file_no": e["file_no"]})
    dup = {k: v for k, v in seen.items() if len(v) > 1}
    if dup:
        out.append(Finding(
            check=C_STRUCT, severity=SEV_ERROR, stream="episode_index",
            message="episode_index 重号：%s" % {k: v for k, v in list(dup.items())[:5]},
            metrics={"duplicates": {str(k): v for k, v in dup.items()}}))
    if mismatch:
        out.append(Finding(
            check=C_STRUCT, severity=SEV_WARN, stream="episode_index",
            message=("有 %d 段的 meta.episode_index 与公式 N = c×%d + f + 1 不符"
                     "（前几段 %s）" % (len(mismatch), chunks_size, mismatch[:5])),
            metrics={"mismatch": mismatch[:50], "chunks_size": chunks_size}))
    ns = sorted(seen)
    if ns:
        holes = [n for n in range(ns[0], ns[-1] + 1) if n not in seen]
        if holes:
            out.append(Finding(
                check=C_STRUCT, severity=SEV_INFO, stream="episode_index",
                message=("全局序号 %d..%d 中有 %d 个空号（前几个 %s）—— 删除不回退是契约，"
                         "属正常" % (ns[0], ns[-1], len(holes), holes[:10])),
                metrics={"holes": holes[:100], "range": [ns[0], ns[-1]]}))
    return out


# ════════════════════════════ 视频帧数（可选） ════════════════════════════

def video_frame_counts(root: str, eps: List[Dict[str, Any]], info: Dict[str, Any],
                       timeout: float = 20.0) -> Dict[str, Dict[str, int]]:
    """用 ffprobe 读每个视频流的 nb_frames（只读头部，不解码）。"""
    if not shutil.which("ffprobe"):
        return {}
    exts = info.get("video_extensions") or {}
    out: Dict[str, Dict[str, int]] = {}
    for e in eps:
        m = re.search(r"data/chunk-(\d+)/", e["rel"])
        if not m:
            continue
        chunk = m.group(1)
        vdir = os.path.join(root, "videos", "chunk-%s" % chunk)
        if not os.path.isdir(vdir):
            continue
        counts: Dict[str, int] = {}
        for key in sorted(os.listdir(vdir)):
            ext = exts.get(key, "mp4")
            cand = os.path.join(vdir, key, "episode-%s.%s" % (e["episode"], ext))
            if not os.path.isfile(cand):
                continue
            try:
                r = subprocess.run(
                    ["ffprobe", "-v", "error", "-select_streams", "v:0",
                     "-show_entries", "stream=nb_frames", "-of",
                     "default=nw=1:nk=1", cand],
                    capture_output=True, text=True, timeout=timeout)
                v = (r.stdout or "").strip().splitlines()
                if v and v[0].strip().isdigit():
                    counts[key] = int(v[0])
            except Exception:
                continue
        if counts:
            out[e["episode"]] = counts
    return out


# ════════════════════════════ 报告渲染 ════════════════════════════

def aggregate_streams(episodes: List[Dict[str, Any]],
                      streams: List[StreamSpec]) -> List[Dict[str, Any]]:
    """把各流的样本数按数据集汇总，直接回答「不同数据帧数齐不齐」。"""
    agg: List[Dict[str, Any]] = []
    for s in streams:
        tot = {"key": s.key, "kind": s.kind, "clock": s.clock, "rate_hz": s.rate_hz,
               "samples": 0, "expected": 0.0, "empty_rows": 0, "edge_empty_rows": 0,
               "rows": 0, "burst_rows": 0, "edge_burst_rows": 0,
               "own_span_sec": 0.0, "episode_span_sec": 0.0,
               "with_data_episodes": 0,
               "pair_offset_sum_ms": 0.0, "pair_offset_n": 0}
        for e in episodes:
            tot["rows"] += e["rows"]
            tot["episode_span_sec"] += e["duration_sec"]
            if s.kind == "frame":
                tot["samples"] += e["rows"]
                tot["expected"] += e["rows"]
                tot["own_span_sec"] += e["duration_sec"]
                continue
            if s.kind == "sample":
                # 样本流每行**至多**一个采样值 ⇒ 期望值就是行数
                # （样本数与跨度走下面的通用分支，别在这里重复累加）
                tot["expected"] += e["rows"]
            summ = (e.get("streams") or {}).get(s.key)
            if not summ:
                continue
            tot["with_data_episodes"] += 1
            tot["samples"] += summ.get("samples") or 0
            tot["empty_rows"] += summ.get("empty_rows") or 0
            tot["edge_empty_rows"] += summ.get("edge_empty_rows") or 0
            tot["burst_rows"] += summ.get("burst_rows") or 0
            tot["edge_burst_rows"] += summ.get("edge_burst_rows") or 0
            tot["own_span_sec"] += summ.get("own_span_sec") or 0.0
            r = s.rate_hz or summ.get("nominal_rate_hz")
            if r and s.kind != "sample":
                tot["expected"] += r * e["duration_sec"]
            if summ.get("pair_offset_median_ms") is not None:
                # 逐段中位配对偏移按样本数加权（近似数据集级中位）
                nn = int(summ.get("pair_offset_n") or 0)
                tot["pair_offset_sum_ms"] += float(summ["pair_offset_median_ms"]) * nn
                tot["pair_offset_n"] += nn
        tot["dev_pct"] = (round((tot["samples"] - tot["expected"]) / tot["expected"] * 100.0, 3)
                          if tot["expected"] else None)
        tot["coverage"] = (round(tot["own_span_sec"] / tot["episode_span_sec"], 5)
                           if tot["episode_span_sec"] else None)
        tot["pair_offset_median_ms"] = (
            round(tot["pair_offset_sum_ms"] / tot["pair_offset_n"], 3)
            if tot["pair_offset_n"] else None)
        tot.pop("pair_offset_sum_ms", None)
        agg.append(tot)
    return agg


def render_text(rep: Dict[str, Any], top: int = 8, show_samples: bool = True) -> str:
    L: List[str] = []
    d = rep["dataset"]
    L.append("=" * 100)
    L.append("数据集时间轴质检报告")
    L.append("=" * 100)
    L.append("数据集   : %s" % d["root"])
    L.append("格式     : %s%s" % (d.get("format") or "(未声明)",
                                ("   app_version=" + str(d["app_version"]))
                                if d.get("app_version") else ""))
    L.append("帧率     : %s Hz        段落: %d    总帧数: %s    总时长: %.1fs"
             % (d.get("fps"), d["n_episodes"], "{:,}".format(d["n_frames"]),
                d.get("total_duration_sec") or 0))
    L.append("检测流   : %d 条（帧级 %d / 列表级 %d / 样本流 %d）"
             % (len(rep["streams"]),
                sum(1 for s in rep["streams"] if s["kind"] == "frame"),
                sum(1 for s in rep["streams"] if s["kind"] == "list"),
                sum(1 for s in rep["streams"] if s["kind"] == "sample")))
    if d.get("alignment_note"):
        L.append("对齐基线 : %s" % d["alignment_note"])
    L.append("")

    L.append("── 流模型 " + "─" * 88)
    L.append("  " + pad("列名", 44) + pad("类型", 7) + pad("时钟域", 20)
             + pad("标称Hz", 10, ">") + pad("容差ns", 9, ">") + "  参考列")
    for s in rep["streams"]:
        L.append("  " + pad(s["key"], 44) + pad(s["kind"], 7)
                 + pad(s["clock"] or "-", 20)
                 + pad("%.3f" % s["rate_hz"] if s["rate_hz"] else "-", 10, ">")
                 + pad(s["quantum_ns"], 9, ">")
                 + "  " + (s["ref"] or ("—（仅域内成对比较）" if s["kind"] == "list"
                                       else "—（样本流：比行号配对偏移）" if s["kind"] == "sample"
                                       else "-")))
    L.append("")

    L.append("── 逐段结果 " + "─" * 88)
    hdr = ("  " + pad("ep", 6) + pad("帧数", 9, ">") + pad("时长s", 10, ">")
           + pad("C1回退", 8, ">") + pad("C2重复", 8, ">") + pad("C3跳变", 8, ">")
           + pad("C4不齐", 8, ">") + pad("C5偏移", 8, ">") + "  结论")
    L.append(hdr)
    for e in rep["episodes"]:
        cnt = {k: [0, 0] for k in CHECK_ORDER}     # [warn+error, error]
        for f in e["findings"]:
            if f["severity"] == SEV_INFO:
                continue
            cnt[f["check"]][0] += 1
            if f["severity"] == SEV_ERROR:
                cnt[f["check"]][1] += 1

        def cell(k: str) -> str:
            w, er = cnt[k]
            return "-" if w == 0 else ("%d(%d!)" % (w, er) if er else "%d" % w)

        verdict = {"ok": "✔ 通过", "warn": "△ 警告", "error": "✘ 错误"}[e["status"]]
        badge = ("%d 错误 / %d 警告" % (
            sum(1 for f in e["findings"] if f["severity"] == SEV_ERROR),
            sum(1 for f in e["findings"] if f["severity"] == SEV_WARN)))
        L.append("  " + pad(e["episode"], 6) + pad("{:,}".format(e["rows"]), 9, ">")
                 + pad("%.2f" % e["duration_sec"], 10, ">")
                 + pad(cell(C_REGRESSION), 8, ">") + pad(cell(C_DUPLICATE), 8, ">")
                 + pad(cell(C_JUMP), 8, ">") + pad(cell(C_COUNT), 8, ">")
                 + pad(cell(C_OFFSET), 8, ">") + "  " + verdict
                 + (("  [" + badge + "]") if badge != "0 错误 / 0 警告" else ""))
    L.append("")

    # ── 问题明细 ──
    L.append("── 问题明细 " + "─" * 88)
    n_shown = 0
    for e in rep["episodes"]:
        interesting = [f for f in e["findings"]
                       if f["severity"] in (SEV_ERROR, SEV_WARN)]
        if not interesting:
            continue
        L.append("  【episode-%s】 帧数 %s  时长 %.2fs" %
                 (e["episode"], "{:,}".format(e["rows"]), e["duration_sec"]))
        by_check: Dict[str, List[Dict[str, Any]]] = {}
        for f in interesting:
            by_check.setdefault(f["check"], []).append(f)
        for ck in CHECK_ORDER:
            for f in by_check.get(ck, []):
                L.append("    %s %s / %s" % (
                    "✘" if f["severity"] == SEV_ERROR else "△",
                    CHECK_NAMES[ck], f["stream"]))
                L.append("        %s" % f["message"])
                if show_samples and f.get("samples"):
                    for s in f["samples"][:top]:
                        pos = []
                        if "row" in s:
                            pos.append("row %d" % s["row"]
                                       + (("→%d" % s["next_row"])
                                          if s.get("next_row") != s.get("row") else ""))
                        if "elem" in s:
                            pos.append("elem %d" % s["elem"])
                        L.append("          · %s  prev=%s  next=%s  Δ=%s"
                                 % (" ".join(pos) or ("i=%d" % s["i"]),
                                    s.get("prev_ms", s["prev"]),
                                    s.get("next_ms", s.get("next")),
                                    s.get("delta_ms", s["delta"])))
        n_shown += 1
    if n_shown == 0:
        L.append("  （无 error / warn 级问题）")
    L.append("")

    # ── info 级（汇总一行，避免刷屏） ──
    infos: Dict[str, int] = {}
    for e in rep["episodes"]:
        for f in e["findings"]:
            if f["severity"] == SEV_INFO:
                infos[f["check"] + "|" + f["stream"]] = infos.get(
                    f["check"] + "|" + f["stream"], 0) + 1
    if infos:
        L.append("── 提示（info 级，不计入结论） " + "─" * 74)
        for k, n in sorted(infos.items(), key=lambda kv: -kv[1])[:20]:
            ck, st = k.split("|", 1)
            L.append("  · %s / %s：%d 处" % (CHECK_NAMES.get(ck, ck), st, n))
        L.append("")

    # ── 各流样本数对照（C4 的数据集级视图） ──
    if rep.get("stream_totals"):
        L.append("── 各流样本数对照（全数据集） " + "─" * 68)
        L.append("  " + pad("流", 38) + pad("样本总数", 12, ">") + pad("期望", 12, ">")
                 + pad("偏离", 9, ">") + pad("覆盖", 8, ">") + pad("空行中/边", 11, ">")
                 + pad("攒批中/边", 11, ">") + pad("标称Hz", 8, ">")
                 + pad("行配对偏移", 12, ">"))
        for t in rep["stream_totals"]:
            mid_empty = t["empty_rows"] - t.get("edge_empty_rows", 0)
            mid_burst = t["burst_rows"]
            L.append("  " + pad(t["key"], 38) + pad("{:,}".format(t["samples"]), 12, ">")
                     + pad("%.0f" % t["expected"] if t["expected"] else "-", 12, ">")
                     + pad(("%+.2f%%" % t["dev_pct"]) if t["dev_pct"] is not None else "-",
                           9, ">")
                     + pad(("%.3f" % t["coverage"]) if t["coverage"] is not None else "-",
                           8, ">")
                     + pad("%d/%d" % (mid_empty, t.get("edge_empty_rows", 0)), 11, ">")
                     + pad("%d/%d" % (mid_burst, t.get("edge_burst_rows", 0)), 11, ">")
                     + pad("%.3f" % t["rate_hz"] if t["rate_hz"] else "-", 8, ">")
                     + pad(("%+.1fms" % t["pair_offset_median_ms"])
                           if t.get("pair_offset_median_ms") is not None else "-",
                           12, ">"))
        L.append("  （期望 = 标称率 × 本段时长之和；覆盖 = 各流自身跨度之和 / 各段时长之和；")
        L.append("   中 = 段中间（真问题，计入 warn）；边 = 首/末行（起停固有形状，只报 info）；")
        L.append("   行配对偏移 = 样本戳 − 本行帧戳 的逐段中位（按样本数加权）：队列滞留差，")
        L.append("   下游按 *_ns 最近邻重采样即可消除；它随录制时长漂才说明哪一路在堆积）")
        L.append("")

    L.append("── 汇总 " + "─" * 92)
    L.append("  " + pad("检查项", 26) + pad("涉及段数", 10, ">")
             + pad("error", 8, ">") + pad("warn", 8, ">") + pad("info", 7, ">")
             + "  说明")
    for ck in CHECK_ORDER:
        s = rep["summary"].get(ck) or {}
        if not s.get("episodes"):
            continue
        L.append("  " + pad("%s %s" % (ck.split("_")[0], CHECK_NAMES[ck]), 26)
                 + pad(s.get("episodes", 0), 10, ">")
                 + pad(s.get("error", 0), 8, ">") + pad(s.get("warn", 0), 8, ">")
                 + pad(s.get("info", 0), 7, ">")
                 + "  " + s.get("hint", ""))
    tot_e = sum((rep["summary"].get(ck) or {}).get("error", 0) for ck in CHECK_ORDER)
    tot_w = sum((rep["summary"].get(ck) or {}).get("warn", 0) for ck in CHECK_ORDER)
    L.append("")
    L.append("  合计：%d 个 error，%d 个 warn    → 退出码 %d"
             % (tot_e, tot_w, rep["exit_code"]))
    L.append("=" * 100)
    return "\n".join(L)


def summarize(episodes: List[Dict[str, Any]],
              extra: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """按检查项汇总（段数 = 有该级问题的段数，不是问题条数）。"""
    hints = {
        C_REGRESSION: "时间戳必须严格递增；出现即数据不可用于训练对齐",
        C_DUPLICATE: "同一行列表内重复恒为缺陷；跨行重复多为样本重挂",
        C_JUMP: "空洞=样本缺失；压缩=攒批补发，两者常成对出现",
        C_COUNT: "流之间帧/样本数不齐会让跨模态对齐错位",
        C_OFFSET: "偏移影响跨模态时间对齐；看基线与稳定性",
        C_STRUCT: "编号/元数据不一致，会让「第几段」语义错位",
    }
    out: Dict[str, Any] = {}
    for ck in CHECK_ORDER:
        per_ep: Dict[str, Dict[str, int]] = {}
        for e in episodes:
            err = sum(1 for f in e["findings"]
                      if f["check"] == ck and f["severity"] == SEV_ERROR)
            wrn = sum(1 for f in e["findings"]
                      if f["check"] == ck and f["severity"] == SEV_WARN)
            inf = sum(1 for f in e["findings"]
                      if f["check"] == ck and f["severity"] == SEV_INFO
                      and not f.get("bookkeeping"))
            if err or wrn or inf:
                per_ep[e["episode"]] = {"error": err, "warn": wrn, "info": inf}
        for f in (extra or []):
            if f["check"] == ck:
                d = per_ep.setdefault("__dataset__", {"error": 0, "warn": 0, "info": 0})
                d[f["severity"]] = d.get(f["severity"], 0) + 1
        if per_ep:
            out[ck] = {
                "name": CHECK_NAMES[ck],
                "episodes": len(per_ep),
                "episode_list": sorted(per_ep),
                "error": sum(v["error"] for v in per_ep.values()),
                "warn": sum(v["warn"] for v in per_ep.values()),
                "info": sum(v.get("info", 0) for v in per_ep.values()),
                "hint": hints.get(ck, ""),
            }
    return out


class QcError(RuntimeError):
    """用法/读取级错误（main 捕获后返回退出码 2）。"""


def _union_schema(paths: Sequence[str]) -> pa.Schema:
    """把若干 parquet 的 schema 并起来（同名取首个）。

    为什么要并：流模型是从 schema 推出来的，而本仓库的数据是**稀疏列**——
    某条流可能只在部分段里出现（例如右夹爪力整段缺失的那 26 段）。只看第一
    段会漏掉那条流；只看并集会把它后面的缺失按「本段整列缺失」报出来，正是
    我们要的。
    """
    fields: Dict[str, pa.Field] = {}
    for p in paths:
        try:
            for f in pq.ParquetFile(p).schema_arrow:
                fields.setdefault(f.name, f)
        except Exception:
            continue
    return pa.schema(list(fields.values()))


def run(root: str, cfg: Dict[str, Any], episodes_filter: Optional[Sequence[str]] = None,
        jobs: int = 1, check_video: bool = False, verbose: bool = True) -> Dict[str, Any]:
    """跑完整个数据集，返回报告 dict（不打印、不落盘）。"""
    root = os.path.abspath(root.rstrip("/") or "/")
    if not os.path.isdir(root):
        raise QcError("数据集目录不存在：%s" % root)
    info = load_info(root)
    eps = list_episodes(root, info)
    if episodes_filter:
        want = {str(x).strip() for x in episodes_filter if str(x).strip()}
        eps = [e for e in eps if e["episode"] in want
               or (e["global_n"] is not None and str(e["global_n"]) in want)]
    if not eps:
        raise QcError("没找到任何 parquet：%s\n"
                      "（找过 data/chunk-*/episode-*.parquet、data/**/*.parquet、*.parquet）"
                      % root)

    fps = float(info.get("fps") or 0.0)
    schema = _union_schema([e["data_path"] for e in eps])
    streams = discover_streams(info, schema, cfg)
    if not streams:
        raise QcError("没能从 %s 推断出任何时间戳流；用 --emit-config 手写 streams"
                      % eps[0]["data_path"])

    # 用第一段把「无显式时钟域」的流的参考列按纪元邻近补出来
    first_med: Dict[str, float] = {}
    try:
        ep0 = pq.ParquetFile(eps[0]["data_path"]).schema_arrow
        probe_cols = [s.key for s in streams]
        tb = pq.read_table(eps[0]["data_path"],
                           columns=[c for c in probe_cols if c in ep0.names])
        for s in streams:
            if s.key not in ep0.names:
                continue
            if s.kind in ("frame", "sample"):
                v = read_frame_column(tb, s.key, s.unit, cfg)
                if s.kind == "sample":
                    mv = (s.missing if s.missing is not None
                          else cfg.get("sample_missing_value", 0))
                    v = v[v != mv]
                if v.size:
                    first_med[s.key] = float(np.median(v))
            elif s.kind == "list":
                L, flat = read_list_column(tb, s.key, s.unit, cfg)
                if flat.size:
                    first_med[s.key] = float(np.median(flat))
        del tb
    except Exception:
        pass
    probe_refs_by_epoch(streams, first_med, cfg)

    info_base = info_baselines_from_alignment(info)

    if verbose:
        print("质检 %s：%d 段，%d 条流，fps=%s，并行 %d"
              % (root, len(eps), len(streams), fps or "?", jobs), file=sys.stderr)

    results: List[Dict[str, Any]] = []
    if jobs and jobs > 1 and len(eps) > 1:
        with concurrent.futures.ProcessPoolExecutor(max_workers=jobs) as ex:
            futs = {ex.submit(measure_episode, e["data_path"], e["episode"], streams,
                              cfg, e.get("meta"), info_base, fps): e for e in eps}
            for fu in concurrent.futures.as_completed(futs):
                e = futs[fu]
                try:
                    results.append(fu.result())
                except Exception as ex2:
                    results.append(_error_episode(e["episode"], ex2))
    else:
        for e in eps:
            try:
                results.append(measure_episode(e["data_path"], e["episode"], streams,
                                               cfg, e.get("meta"), info_base, fps))
            except Exception as ex2:
                results.append(_error_episode(e["episode"], ex2))
    results.sort(key=lambda r: r["episode"])

    dataset_findings = [f.to_dict() for f in check_structure(root, info, eps)]

    if check_video:
        vc = video_frame_counts(root, eps, info)
        for r in results:
            for key, n in (vc.get(r["episode"]) or {}).items():
                if n != r["rows"]:
                    dataset_findings.append(Finding(
                        check=C_COUNT, severity=SEV_WARN, stream="video:%s" % key,
                        message=("视频 %s 帧数 %d ≠ parquet 行数 %d（episode-%s）"
                                 % (key, n, r["rows"], r["episode"])),
                        metrics={"video_frames": n, "data_rows": r["rows"],
                                 "episode": r["episode"]}).to_dict())
        if not vc:
            dataset_findings.append(Finding(
                check=C_COUNT, severity=SEV_INFO, stream="video",
                message="--check-video 打开但没读到任何视频帧数（ffprobe 缺失或无视频目录）",
                metrics={}).to_dict())

    summary = summarize(results, dataset_findings)
    n_err = sum(v["error"] for v in summary.values())
    n_warn = sum(v["warn"] for v in summary.values())
    al = info.get("alignment") or {}
    rep = {
        "tool": {"name": "data_qc", "version": __version__},
        "dataset": {
            "root": root,
            "format": info.get("format"),
            "task_name": info.get("task_name"),
            "app_version": info.get("app_version"),
            "fps": fps,
            "n_episodes": len(results),
            "n_frames": sum(r["rows"] for r in results),
            "total_duration_sec": round(sum(r["duration_sec"] for r in results), 2),
            "alignment_note": (("偏移基线=%s ms（%s；%s）" % (
                al.get("qc_offset_baseline_ms", al.get("glove_shift_ms")),
                al.get("qc_offset_baseline_source") or al.get("method", ""),
                al.get("qc_offset_baseline_note", "")))
                if (al.get("qc_offset_baseline_ms") is not None
                    or al.get("glove_shift_ms") is not None) else None),
        },
        "streams": [s.to_dict() for s in streams],
        "stream_totals": aggregate_streams(results, streams),
        "episodes": results,
        "dataset_findings": dataset_findings,
        "summary": summary,
        "totals": {"error": n_err, "warn": n_warn},
        "exit_code": 1 if n_err else 0,
    }
    return rep


def _error_episode(ep: str, exc: BaseException) -> Dict[str, Any]:
    return {
        "episode": ep, "episode_index": None, "rows": 0, "duration_sec": 0.0,
        "counts": {}, "streams": {}, "offsets": [],
        "findings": [Finding(check=C_STRUCT, severity=SEV_ERROR, stream="data",
                             message="读取/质检失败：%r" % (exc,), metrics={}).to_dict()],
        "worst_severity": SEV_ERROR, "status": "error",
    }


def exit_code_of(rep: Dict[str, Any], strict: bool = False) -> int:
    if rep["totals"]["error"]:
        return 1
    if strict and rep["totals"]["warn"]:
        return 1
    return 0


def emit_config(rep: Dict[str, Any], path: str) -> None:
    """把推断结果落成一份可改的配置。"""
    streams_cfg = {}
    for s in rep["streams"]:
        if s["kind"] == "frame":
            continue
        streams_cfg[s["key"]] = {
            "rate_hz": s["rate_hz"], "clock": s["clock"], "unit": s["unit"],
            "ref": s["ref"], "value_col": s["value_col"],
            "sample_width": s["sample_width"], "quantum_ns": s["quantum_ns"],
            "missing": s.get("missing"),
        }
    cfg = {
        "_说明": "data_qc 配置模板：未写的项走默认值/自动推断；改哪项留哪项即可。",
        "_生成自": rep["dataset"]["root"],
        "frame_columns": [s["key"] for s in rep["streams"] if s["kind"] == "frame"],
        "streams": streams_cfg,
        "baselines": {},
        "checks": DEFAULT_CONFIG["checks"],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)
    print("已写出配置模板：%s" % path)


# ════════════════════════════ 自检 ════════════════════════════

def _tick_counts(n_rows: int, per_row: float) -> np.ndarray:
    """按 Bresenham 把 per_row（可带小数）摊到每行：和 = floor(n×per_row)，逐行 2/3 交替。

    直接用 round(per_row) 会让实测采样率系统性偏离声明值（例如 17/行 = 510Hz
    而声明 501.4Hz），那是合成器的错，不是被检数据的错。
    """
    i = np.arange(n_rows + 1, dtype=np.float64)
    return (np.floor(i[1:] * per_row) - np.floor(i[:-1] * per_row)).astype(np.int64)


def _write_synth_episode(root: str, fnum: str, n_rows: int, fps: float,
                         defect: str) -> None:
    """合成一段带指定缺陷的数据（布局和真实池化目录一致）。

    时间轴的构造方式刻意贴近真实数据：手套 2.03 样本/行（60.974Hz）、IMU
    16.71 样本/行（501.436Hz）、SLAM 1 样本/行且**首行为空**（真实数据就是
    N 帧只有 N−1 个 slam 样本），并用 floor 量化模拟「样本挂到帧行」的抖动。
    """
    dt = 1e9 / fps
    base_host = 1_791_975_828_986_000_000
    base_dev = 187_719_536_000
    period_imu = int(round(1e9 / 501.436))
    period_g = int(round(1e9 / 60.974))
    rows = np.arange(n_rows, dtype=np.int64)
    ts_ns = (rows * dt).astype(np.int64)

    def attach(period: int, per_row: float, t0: int) -> List[List[int]]:
        """按每行样本数把一条规则采样流切到行上。"""
        cnt = _tick_counts(n_rows, per_row)
        idx = np.cumsum(cnt) - cnt              # 每行首样本的全局序号
        out = []
        for i in range(n_rows):
            out.append([int(t0 + (idx[i] + k) * period) for k in range(int(cnt[i]))])
        return out

    imu_t0 = base_dev - 22_000_000                       # IMU 比帧戳早 ~22ms
    lg_t0 = base_host + 93_000_000                       # 手套宿主戳比行写时间晚 93ms
    lgd_t0 = 81_341_148_000
    imu = attach(period_imu, 501.436 / fps, imu_t0)
    lg = attach(period_g, 60.974 / fps, lg_t0)
    rg = attach(period_g, 60.974 / fps, lg_t0)
    lgd = attach(period_g, 60.974 / fps, lgd_t0)
    # SLAM：每行正好 1 个，戳 = 该行 wall_time
    sl = [[int(base_host + ts_ns[i])] for i in range(n_rows)]
    # 样本流：每行一个采样值，**0 = 本行没有样本**（真实数据 7%~15% 是这种）。
    # 每段都放一段 0 占位：这是正常形状，不该被读成「时间戳回退」。
    fl = np.asarray([base_host + int(t) + 90_000_000 for t in ts_ns], dtype=np.int64)
    fl[100:131] = 0
    if defect == "sample":
        # 真回退只此一处：0 占位不许算进来（旧口径会把 31 个 0 全算成回退）
        fl[400] -= 2 * int(round(dt))
    if defect == "bursty":
        # 位姿 20/20/60 三连发（有效 30Hz）+ 一处 200ms 真断流：
        # 攒批不是丢样本，只有那段断流该被 C3 报出来
        period = [20_000_000, 20_000_000, 60_000_000]
        poses, t, k = [], 0, 0
        while t < int(ts_ns[-1]) + int(dt):
            poses.append(t)
            t += period[k % 3]
            k += 1
        drop_at = int(ts_ns[n_rows // 2])
        sl = [[] for _ in range(n_rows)]
        for t in poses:
            if drop_at <= t < drop_at + 200_000_000:
                continue
            sl[min(n_rows - 1, int(t / dt))].append(int(base_host + t))

    if defect == "regression":
        # 行内回退：把第 2 个样本拉回到第 1 个之前（跨过 1 个采样周期）
        lg[n_rows // 2][1] = lg[n_rows // 2][1] - 2 * period_g
        # 跨行回退：整行前移，使该行首样本早于上一行末样本 2ms
        mid = n_rows // 3
        d = (rg[mid - 1][-1] - 2_000_000) - rg[mid][0]
        rg[mid] = [x + d for x in rg[mid]]
    elif defect == "duplicate":
        lg[n_rows // 2][1] = lg[n_rows // 2][0]                   # 行内重复
        for i in range(20, 40):                                   # 跨行重复一批
            rg[i][0] = rg[i - 1][-1]
    elif defect == "jump":
        gap = 20
        for i in range(n_rows // 2, n_rows):
            lg[i] = [x + gap * period_g for x in lg[i]]
            lgd[i] = [x + gap * period_g for x in lgd[i]]
    elif defect == "count":
        for i in range(100, 160):                                 # 一段空行（掉样本）
            lg[i] = []
        buf: List[int] = []
        for i in range(200, 206):                                 # 攒批：6 行样本挤进 1 行
            buf += rg[i]
        rg[200] = buf
        for i in range(201, 206):
            rg[i] = []
        sl = [[] if i in (0, n_rows - 1) else [int(base_host + ts_ns[i])]
              for i in range(n_rows)]                             # 首尾也缺（真实数据的形状）
    elif defect == "offset":
        shift = 60_000_000                                        # 右手套整体 +60ms
        for i in range(n_rows):
            rg[i] = [x + shift for x in rg[i]]

    def L(v: List[List[int]]):
        return pa.array(v, type=pa.list_(pa.int64()))

    cols = {
        "episode_index": pa.array([int(fnum) + 1] * n_rows, pa.int64()),
        "frame_index": pa.array(rows, pa.int64()),
        "timestamp": pa.array((ts_ns / 1e9).astype(np.float32), pa.float32()),
        "wall_time": pa.array((base_host + ts_ns) / 1e9, pa.float64()),
        "hardware_ns": pa.array(base_dev + ts_ns, pa.int64()),
        "imu_ts_ns": L(imu),
        "observation.left_glove_ts_ns": L(lg),
        "observation.right_glove_ts_ns": L(rg),
        "observation.left_glove_device_ts_ns": L(lgd),
        "observation.slam_trajectory_ns": L(sl),
    }
    if defect != "sparse":
        # 稀疏列：这一段整列没有这条流（契约允许，但不许把整段判成读不动）
        cols["observation.gripper_left_force_ns"] = pa.array(fl, pa.int64())
    tbl = pa.table(cols)
    d = os.path.join(root, "data", "chunk-000")
    os.makedirs(d, exist_ok=True)
    pq.write_table(tbl, os.path.join(d, "episode-%s.parquet" % fnum), compression="zstd")


def build_synthetic(root: str) -> Dict[str, str]:
    """造一份 6 段数据集：1 段干净 + 5 段各注入一类缺陷。"""
    fps = 30.004
    plan = [("000", 900, "clean"), ("001", 900, "regression"), ("002", 900, "duplicate"),
            ("003", 900, "jump"), ("004", 900, "count"), ("005", 900, "offset"),
            ("006", 900, "sample"), ("007", 900, "sparse"), ("008", 900, "bursty")]
    for fnum, n, defect in plan:
        _write_synth_episode(root, fnum, n, fps, defect)
    os.makedirs(os.path.join(root, "meta", "episodes", "chunk-000"), exist_ok=True)
    for fnum, n, _ in plan:
        rows = np.arange(n, dtype=np.int64)
        m = pa.table({
            "episode_index": pa.array([int(fnum) + 1], pa.int64()),
            "task_index": pa.array([0], pa.int64()),
            "start_frame_index": pa.array([0], pa.int64()),
            "end_frame_index": pa.array([n - 1], pa.int64()),
            "length": pa.array([n], pa.int64()),
            "created_at": pa.array([1_791_975_828.986], pa.float64()),
            "duration_sec": pa.array([float(n) / fps], pa.float64()),
        })
        pq.write_table(m, os.path.join(root, "meta", "episodes", "chunk-000",
                                       "episode-%s.parquet" % fnum))
    info = {
        "format": "pooled_episodes_v1", "chunks_size": 1000, "fps": fps,
        "task_name": "selftest", "app_version": "selftest/1",
        "data_path": "data/chunk-{c:03d}/episode-{f:03d}.parquet",
        # 和真实数据集同款：手套宿主戳被故意后移 85ms 去对齐视频，因此
        # 「手套 − wall_time ≈ 93ms」「手套 − SLAM ≈ 85ms」都是设计值不是缺陷
        "alignment": {"glove_shift_ms": 85.0, "method": "selftest 内建基线"},
        "features": {
            "observation.imu": {"dtype": "float32", "shape": [6], "rate_hz": 501.436,
                                "clock": "imu_device_ns", "ts_key": "imu_ts_ns"},
            "observation.left_glove": {"dtype": "float32", "shape": [256],
                                       "rate_hz": 60.974, "clock": "glove_device_ns",
                                       "ts_key": "observation.left_glove_ts_ns"},
            "observation.left_glove_ts_ns": {"dtype": "int64", "rate_hz": 60.974,
                                             "clock": "host_realtime_ns"},
            "observation.left_glove_device_ts_ns": {"dtype": "int64", "rate_hz": 60.974,
                                                    "clock": "glove_device_ns"},
            "observation.right_glove": {"dtype": "float32", "shape": [256],
                                        "rate_hz": 60.974, "clock": "glove_device_ns",
                                        "ts_key": "observation.right_glove_ts_ns"},
            "observation.right_glove_ts_ns": {"dtype": "int64", "rate_hz": 60.974,
                                              "clock": "host_realtime_ns"},
            "observation.slam_trajectory_ns": {"dtype": "int64",
                                               "clock": "host_realtime_ns"},
            # 样本流：每行至多一个采样值，0 = 本行没有样本（本仓库真实口径）
            "observation.gripper_left_force_ns": {
                "dtype": "int64", "clock": "host_monotonic_ns",
                "encoding": "host_monotonic_ns"},
        },
    }
    with open(os.path.join(root, "meta", "info.json"), "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=1)
    return {"clean": "000", "regression": "001", "duplicate": "002",
            "jump": "003", "count": "004", "offset": "005",
            "sample": "006", "sparse": "007", "bursty": "008"}


def selftest(keep: Optional[str] = None) -> int:
    """判据自检：每类缺陷必须在该段被报出，干净段不得误报。"""
    root = keep or tempfile.mkdtemp(prefix="data_qc_selftest_")
    os.makedirs(root, exist_ok=True)
    print("自检数据：%s" % root)
    ids = build_synthetic(root)
    cfg = deep_merge(DEFAULT_CONFIG, {})
    rep = run(root, cfg, jobs=1, verbose=False)

    by_ep = {e["episode"]: e for e in rep["episodes"]}

    def checks_of(ep: str, sev=(SEV_ERROR, SEV_WARN)) -> set:
        return {f["check"] for f in by_ep[ep]["findings"] if f["severity"] in sev}

    # 缺陷段里预期命中的检查项（allow_extra 是因为一段缺陷常会连带触发别的项）
    expect = {
        "regression": ({C_REGRESSION}, ids["regression"]),
        "duplicate": ({C_DUPLICATE}, ids["duplicate"]),
        "jump": ({C_JUMP}, ids["jump"]),
        "count": ({C_COUNT}, ids["count"]),
        "offset": ({C_OFFSET}, ids["offset"]),
    }
    ok = True
    clean = checks_of(ids["clean"])
    if clean:
        ok = False
        print("  ✘ 干净段 %s 误报：%s" % (ids["clean"], sorted(clean)))
        for f in by_ep[ids["clean"]]["findings"]:
            print("      %s %s %s" % (f["severity"], f["check"], f["message"]))
    else:
        print("  ✔ 干净段 %s 无 error/warn" % ids["clean"])
    for label, (want, ep) in expect.items():
        got = checks_of(ep)
        if want <= got:
            print("  ✔ %-11s 段 %s 命中 %s" % (label, ep, "/".join(sorted(want))))
        else:
            ok = False
            print("  ✘ %-11s 段 %s 漏报：期望 %s，实得 %s"
                  % (label, ep, sorted(want), sorted(got)))
    # 干净段的数值也必须干净：C3 不该在干净段报跳变
    clean_jump = [f for f in by_ep[ids["clean"]]["findings"] if f["check"] == C_JUMP]
    if clean_jump:
        ok = False
        print("  ✘ 干净段出现跳变告警：%s" % [f["message"] for f in clean_jump])

    # ── 1.1.0 口径：0 占位 / 缺列 / 攒批 三条不许再误报 ──
    smp = by_ep[ids["sample"]]
    c1 = [f for f in smp["findings"] if f["check"] == C_REGRESSION]
    nreg = int(c1[0]["metrics"].get("n_regressions") or 0) if c1 else 0
    if nreg == 1:
        print("  ✔ 样本流 段 %s：0 占位不算回退，只报注入的 1 处真回退" % ids["sample"])
    else:
        ok = False
        print("  ✘ 样本流 段 %s：期望 1 处真回退，实得 %d" % (ids["sample"], nreg))
    c2 = [f for f in smp["findings"]
          if f["check"] == C_DUPLICATE and f["severity"] in (SEV_ERROR, SEV_WARN)]
    if not c2:
        print("  ✔ 样本流 段 %s：0 占位不算重复" % ids["sample"])
    else:
        ok = False
        print("  ✘ 样本流 段 %s：0 占位被判成重复" % ids["sample"])

    sp = by_ep[ids["sparse"]]
    sp_c6 = [f for f in sp["findings"] if f["check"] == C_STRUCT]
    sp_miss = [f for f in sp["findings"]
               if f["check"] == C_COUNT and f["metrics"].get("missing_column")]
    if sp["status"] != SEV_ERROR and not sp_c6 and sp_miss:
        print("  ✔ 稀疏列 段 %s：整列缺失报 C4（%s…），不再变成 C6 读不动"
              % (ids["sparse"], sp_miss[0]["stream"]))
    else:
        ok = False
        print("  ✘ 稀疏列 段 %s：status=%s C6=%s missing=%s"
              % (ids["sparse"], sp["status"], len(sp_c6), len(sp_miss)))

    bu = by_ep[ids["bursty"]]
    bu_c3 = [f for f in bu["findings"]
             if f["check"] == C_JUMP and f["severity"] in (SEV_ERROR, SEV_WARN)]
    n_gap = sum(int(f["metrics"].get("n_gaps") or 0) for f in bu_c3)
    worst = max([float(f["metrics"].get("worst_gap_ms") or 0) for f in bu_c3] or [0.0])
    if n_gap == 1 and 150.0 <= worst <= 400.0:
        print("  ✔ 攒批 段 %s：20/20/60 节奏不算丢样本，只报注入的 200ms 断流"
              "（%.1fms）" % (ids["bursty"], worst))
    else:
        ok = False
        print("  ✘ 攒批 段 %s：期望 1 处≈200ms 断流，实得 n=%d worst=%.1fms"
              % (ids["bursty"], n_gap, worst))
    print("\n自检结果：%s" % ("全部通过" if ok else "存在失败项"))
    if not keep:
        shutil.rmtree(root, ignore_errors=True)
    return 0 if ok else 1


# 出 PDF 用的 chrome 候选路径（找不到就用 PATH 里的 google-chrome / chromium）
CHROME_CANDIDATES = [
    "/home/stouch/.agent-browser/browsers/chrome-151.0.7922.71/chrome",
    "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome-stable", "/snap/bin/chromium",
]

# ════════════════════════════ PDF 报告 ════════════════════════════
# 把上面五项检查的结果渲染成 A4 PDF（版式对齐《1013视塔手套数据质检.pdf》）。
# 这一段只依赖 numpy / pyarrow / 标准库；**出 PDF 才**需要 chrome +
# websocket-client，两者都在 `print_pdf()` 里惰性使用 —— 不用 PDF 功能的话，
# 本文件仍然是「只 import numpy + pyarrow」的独立质检器。


def _ffprobe(path: str) -> Dict[str, Any]:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=codec_name,profile,width,height,r_frame_rate,nb_frames,pix_fmt",
             "-of", "json", path], capture_output=True, text=True, timeout=60)
        return (json.loads(r.stdout).get("streams") or [{}])[0]
    except Exception:
        return {}


def _prim(t: pa.Table, c: str) -> np.ndarray:
    """剥掉所有 list 层取到底层标量数组。"""
    fc = t.column(c)
    if isinstance(fc, pa.ChunkedArray):
        fc = fc.combine_chunks()
    while (pa.types.is_list(fc.type) or pa.types.is_large_list(fc.type)
           or pa.types.is_fixed_size_list(fc.type)):
        fc = pa.compute.list_flatten(fc)
        if isinstance(fc, pa.ChunkedArray):
            fc = fc.combine_chunks()
    return fc.to_numpy(zero_copy_only=False)


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else "-"


def dataset_fingerprint(root: str) -> Dict[str, Any]:
    """数据指纹 + 陈旧判定。

    为什么需要它：导出管线是**边写边替换**的（先重写 data/*.parquet，再写
    meta/episodes/*.parquet，最后写 meta/info.json）。报告若在导出中途跑，会拿
    **新的数据**配**旧的元数据**，把「工具读不到时钟域」误报成满屏数据缺陷。
    2026-10-15 就踩过一次：22:27:06 出报告，data 是 22:26:45 的、info.json 还是
    22:28:36 之前那版，于是 24 段各报 2 条「无法归因」的 C4 警告（48 条）；
    等导出落定后重跑，同一份数据是 0 error / 0 warn。

    判据：info.json 比最新的 data/meta parquet 旧 ⇒ 元数据可能还没落定。
    """
    def mx(paths: List[str]) -> float:
        return max((os.path.getmtime(p) for p in paths), default=0.0)

    dp = glob.glob(os.path.join(root, "data", "chunk-*", "*.parquet"))
    mp = glob.glob(os.path.join(root, "meta", "episodes", "chunk-*", "*.parquet"))
    ip = os.path.join(root, "meta", "info.json")
    t_info = os.path.getmtime(ip) if os.path.isfile(ip) else 0.0
    t_data, t_meta = mx(dp), mx(mp)
    newest = max(t_data, t_meta)
    return {
        "info_json_mtime": t_info, "data_mtime": t_data, "meta_episodes_mtime": t_meta,
        "info_json": _iso(t_info), "data_newest": _iso(t_data),
        "meta_episodes_newest": _iso(t_meta),
        "stale": bool(t_info and newest > t_info + 1.0),
        "stale_by_sec": max(0.0, newest - t_info),
        # 两种「元数据旧」性质完全不同，不能一句话概括：
        #   inflight：差几秒~1 小时 ⇒ 导出管线还在跑（先写 data、后写 info.json），
        #             报告的结论整片不可信，必须重跑；
        #   old     ：差几小时以上 ⇒ 不是同一批产出的（info.json 没跟着重生成），
        #             元数据可能只是陈旧的说明，数据本身未必有问题。
        "stale_kind": ("inflight" if 1.0 < newest - t_info <= 3600.0
                       else "old" if newest - t_info > 3600.0 else None),
    }


def collect_extras(root: str) -> Dict[str, Any]:
    """质检器不管、但报告要用的指标。"""
    info = load_info(root)
    fps = float(info.get("fps") or 0.0)
    exts = info.get("video_extensions") or {}
    eps = sorted(glob.glob(os.path.join(root, "data", "chunk-*", "episode-*.parquet")))
    per: List[Dict[str, Any]] = []
    agg = {"nan": 0, "nan_tot": 0, "valid": [], "qnorm": [], "qbad": 0, "q_tot": 0,
           "active": [], "status": {}, "n_sensors": None}
    for p in eps:
        ep = os.path.basename(p)[8:11]
        e: Dict[str, Any] = {"episode": ep,
                             "rows": pq.ParquetFile(p).metadata.num_rows, "videos": {}}
        for key, ext in exts.items():
            vp = os.path.join(root, "videos", "chunk-000", key, "episode-%s.%s" % (ep, ext))
            if os.path.isfile(vp):
                pr = _ffprobe(vp)
                nf = pr.get("nb_frames")
                e["videos"][key] = {
                    "exists": True, "codec": pr.get("codec_name"),
                    "profile": pr.get("profile"), "w": pr.get("width"),
                    "h": pr.get("height"), "fps": pr.get("r_frame_rate"),
                    "pix_fmt": pr.get("pix_fmt"),
                    "nb_frames": int(nf) if str(nf).isdigit() else None}
            else:
                e["videos"][key] = {"exists": False}
        t = pq.read_table(p)
        ts = np.array(t.column("timestamp").to_pylist(), dtype=np.float64)
        e["span"] = float(ts[-1] - ts[0]) if ts.size else 0.0
        e["dt_med"] = float(np.median(np.diff(ts))) if ts.size > 1 else 0.0
        for side in ("left", "right"):
            g = "observation.%s_glove" % side
            if g in t.column_names:
                arr = _prim(t, g).reshape(-1, 256)
                n_nan = int(np.isnan(arr).sum())
                e["%s_nan" % side] = n_nan / arr.size if arr.size else 0.0
                agg["nan"] += n_nan
                agg["nan_tot"] += arr.size
                # 16 传感器 × 16 轴：一个传感器只要有一轴在动就算「活跃」
                n_sens = 16
                axes = max(1, arr.shape[1] // n_sens)
                cube = arr.reshape(-1, n_sens, axes)
                sd = np.nanstd(cube, axis=0)                    # (16, axes)
                active = int((sd > 1e-6).any(axis=1).sum())
                e["%s_active" % side] = active
                agg["active"].append(active)
                agg["n_sensors"] = n_sens
            q = "observation.%s_glove_imu_quat" % side
            if q in t.column_names:
                a = _prim(t, q).reshape(-1, 4)
                nrm = np.linalg.norm(a, axis=1)
                e["%s_qnorm" % side] = float(np.nanmean(nrm))
                e["%s_qbad" % side] = int((np.abs(nrm - 1.0) > 0.01).sum())
                agg["qnorm"].append(float(np.nanmean(nrm)))
                agg["qbad"] += e["%s_qbad" % side]
                agg["q_tot"] += int(nrm.size)
            v = "observation.%s_glove_imu_valid" % side
            if v in t.column_names:
                a = _prim(t, v)
                e["%s_valid" % side] = float(np.nanmean(a))
                agg["valid"].append(float(np.nanmean(a)))
            s = "status.%s_glove" % side
            if s in t.column_names:
                vals = t.column(s).to_pylist()
                d = {k: vals.count(k) for k in set(vals)}
                e["%s_status" % side] = d
                for k, c in d.items():
                    agg["status"][k] = agg["status"].get(k, 0) + c
        per.append(e)
    return {
        "root": os.path.abspath(root), "info": info, "fps": fps,
        "n_episodes": len(per), "episodes": per,
        "nan_ratio": agg["nan"] / agg["nan_tot"] if agg["nan_tot"] else 0.0,
        "valid_min": min(agg["valid"]) if agg["valid"] else None,
        "valid_max": max(agg["valid"]) if agg["valid"] else None,
        "qnorm_min": min(agg["qnorm"]) if agg["qnorm"] else None,
        "qnorm_max": max(agg["qnorm"]) if agg["qnorm"] else None,
        "qbad": agg["qbad"], "q_tot": agg["q_tot"],
        "active_min": min(agg["active"]) if agg["active"] else None,
        "active_max": max(agg["active"]) if agg["active"] else None,
        "n_sensors": agg["n_sensors"] or 16,
        "status": agg["status"],
        "fingerprint": dataset_fingerprint(root),
    }



CSS = """
@page { size: A4; }
* { box-sizing: border-box; }
body { font-family: "Noto Sans CJK SC", "Source Han Sans SC", "Microsoft YaHei",
       "DejaVu Sans", sans-serif; font-size: 10.5pt; line-height: 1.65;
       color: #1f2328; margin: 0; padding: 0; }
h1 { font-size: 21pt; font-weight: 700; margin: 0 0 .35em 0; letter-spacing: .5px; }
h2 { font-size: 15pt; font-weight: 600; border-bottom: 1px solid #d0d7de;
     padding-bottom: .28em; margin: 1.5em 0 .7em 0; }
h3 { font-size: 12pt; font-weight: 600; margin: 1.35em 0 .5em 0; }
h4 { font-size: 10.5pt; font-weight: 600; margin: 1.1em 0 .4em 0; color: #24292f; }
p, li { margin: .45em 0; }
ul, ol { margin: .45em 0 .45em 0; padding-left: 1.6em; }
li { margin: .22em 0; }
hr { border: 0; border-top: 1px solid #d0d7de; margin: 1.1em 0; }
code { background: #f0f2f4; padding: .1em .34em; border-radius: 4px;
       font-family: "DejaVu Sans Mono", "Noto Sans Mono CJK SC", monospace;
       font-size: .88em; color: #1f2328; }
pre { background: #f6f8fa; border: 1px solid #e4e8ec; border-radius: 6px;
      padding: 11px 14px; font-size: 8.8pt; line-height: 1.5; overflow: hidden; }
pre code { background: none; padding: 0; font-size: 1em; }
table { border-collapse: collapse; width: 100%; margin: .8em 0 1em 0;
        font-size: 9.2pt; }
th, td { border: 0; border-bottom: 1px solid #e4e8ec; padding: 5px 9px;
         text-align: left; vertical-align: top; }
thead th { background: #f6f8fa; font-weight: 600; border-bottom: 1.4px solid #c9d1d9;
           white-space: nowrap; }
tbody tr:nth-child(even) td { background: #fafbfc; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
td.mono, th.mono { font-family: "DejaVu Sans Mono", monospace; font-size: .93em; }
.ok { color: #1a7f37; font-weight: 600; }
table td:last-child, table th:last-child { white-space: nowrap; }
.warn { color: #9a6700; font-weight: 600; }
.err { color: #cf222e; font-weight: 600; }
.muted { color: #57606a; font-size: .92em; }
.lead { margin: .2em 0; }
.callout { border-left: 3px solid #d0d7de; background: #f6f8fa;
           padding: .55em .9em; margin: .8em 0; border-radius: 0 4px 4px 0; }
.callout.err { border-left-color: #cf222e; background: #fff5f5; }
.callout.warn { border-left-color: #d4a72c; background: #fffbf0; }
.callout.info { border-left-color: #8c959f; background: #f6f8fa; }
.pagebreak { page-break-before: always; }
.avoid { page-break-inside: avoid; }
"""


def esc(s: Any) -> str:
    return html.escape(str(s), quote=False)


def ok(t: str = "通过") -> str:
    return '<span class="ok">✅ %s</span>' % esc(t)


def warn(t: str) -> str:
    return '<span class="warn">⚠ %s</span>' % esc(t)


def err(t: str) -> str:
    return '<span class="err">❌ %s</span>' % esc(t)


def table(headers: Sequence[str], rows: Sequence[Sequence[str]],
          num_cols: Sequence[int] = (), mono_cols: Sequence[int] = ()) -> str:
    out = ["<table><thead><tr>"]
    for i, h in enumerate(headers):
        cls = ' class="num"' if i in num_cols else (' class="mono"' if i in mono_cols else '')
        out.append("<th%s>%s</th>" % (cls, esc(h)))
    out.append("</tr></thead><tbody>")
    for r in rows:
        out.append("<tr>")
        for i, c in enumerate(r):
            cls = ' class="num"' if i in num_cols else (' class="mono"' if i in mono_cols else '')
            out.append("<td%s>%s</td>" % (cls, c))
        out.append("</tr>")
    out.append("</tbody></table>")
    return "".join(out)


def rng(vals: Sequence[float], fmt: str = "%.4f") -> str:
    vals = [v for v in vals if v is not None]
    if not vals:
        return "-"
    lo, hi = min(vals), max(vals)
    return fmt % lo if abs(hi - lo) < 1e-12 else (fmt % lo) + " ~ " + (fmt % hi)


def _counts(ep_findings: List[Dict[str, Any]], check: str) -> Tuple[int, int, int]:
    """(error 段数, warn 段数, 总条数)"""
    e = sum(1 for f in ep_findings if f["check"] == check and f["severity"] == "error")
    w = sum(1 for f in ep_findings if f["check"] == check and f["severity"] == "warn")
    return e, w, e + w


def _sev_badge(e: int, w: int, clean: str = "通过") -> str:
    if e:
        return err("%d 个 error" % e + ("，%d 个 warn" % w if w else ""))
    if w:
        return warn("%d 个 warn" % w)
    return ok(clean)


def _norm_msg(s: str) -> str:
    """抹掉消息里的数字，用来把同类问题归并成一行。"""
    import re as _re
    return _re.sub(r"[-+]?\d+(?:\.\d+)?", "N", s)


def _clean_datasets(dirs, reps, check: str) -> List[str]:
    out = []
    for d, r in zip(dirs, reps):
        if not any(f["check"] == check and f["severity"] in ("error", "warn")
                   for ep in r["episodes"] for f in ep["findings"]):
            out.append(os.path.basename(d))
    return out



def build_html(dirs: List[str], reps: List[Dict[str, Any]], extras: List[Dict[str, Any]],
               title: str, date_str: str) -> str:
    P: List[str] = []
    A = P.append
    today = date_str

    # ── 封面头 ──
    A('<h1>%s</h1>' % esc(title))
    A('<ul class="lead">')
    A('<li><b>格式</b>：LeRobot <code>pooled_episodes_v1</code>（每段一 parquet + 每流一 mp4）</li>')
    A('<li><b>检测日期</b>：%s</li>' % esc(today))
    A('<li><b>受检范围</b>：%s</li>' % "、".join(
        "<code>%s</code>（%d 段）" % (os.path.basename(d), r["dataset"]["n_episodes"])
        for d, r in zip(dirs, reps)))
    A('<li><b>检测工具</b>：<code>tools/data_py</code> v%s —— 时间戳回退 / 重复 / 跳变 / 帧数不齐 / 时钟偏移 五项基础检查，'
      '外加与录制器 <code>drop_stats</code> 自报计数的对账</li>' % __version__)
    A('</ul>')
    A('<hr>')

    # ══ 1 数据结构与基础信息 ══
    A('<h2>1&nbsp;&nbsp;数据结构与基础信息</h2>')

    A('<h3>1.1&nbsp;&nbsp;目录结构</h3>')
    tree = []
    tree.append(os.path.basename(os.path.dirname(dirs[0])) + "/")
    for d in dirs:
        nm = os.path.basename(d)
        tree.append("├── %s/" % nm)
        tree.append("│   ├── meta/")
        tree.append("│   │   ├── info.json                 # 数据集声明")
        tree.append("│   │   ├── stats.json                # 统计量")
        tree.append("│   │   ├── tasks.jsonl               # 任务描述")
        tree.append("│   │   └── episodes/chunk-000/episode-XXX.parquet   # 逐 episode 元数据")
        tree.append("│   ├── data/chunk-000/episode-XXX.parquet           # 主数据（含多轴时间戳）")
        tree.append("│   └── videos/chunk-000/")
        for key in sorted((extras[dirs.index(d)]["info"].get("video_extensions") or {})):
            tree.append("│       ├── %-22s episode-XXX.mp4        # %s" % (
                key + "/", "深度视频" if "depth" in key else "RGB 视频"))
    A('<pre><code>%s</code></pre>' % esc("\n".join(tree)))

    A('<h3>1.2&nbsp;&nbsp;数据集概览</h3>')
    # 陈旧守卫：报告若在导出中途生成，元数据与数据对不上，结论会整片失真
    def _stale_of(kind):
        return [(os.path.basename(d), ex["fingerprint"]["stale_by_sec"])
                for d, ex in zip(dirs, extras)
                if (ex.get("fingerprint") or {}).get("stale_kind") == kind]
    inflight = _stale_of("inflight")
    if inflight:
        A('<div class="callout err"><b>⚠ 本报告是在导出过程中生成的（结论不可信，请重跑）</b>：'
          '%s 的 <code>meta/info.json</code> 比数据文件旧 %s —— 导出管线先写 data、'
          '最后才写 info.json，中间这段窗口里工具会拿<b>新数据</b>配<b>旧元数据</b>，'
          '把「读不到时钟域/参考列」误报成数据缺陷。<b>等导出落定后重跑本报告。</b></div>'
          % ("、".join("<code>%s</code>" % esc(n) for n, _ in inflight),
             "、".join("%.0f 秒" % v for _, v in inflight)))
    old = _stale_of("old")
    if old:
        A('<div class="callout warn"><b>元数据比数据旧很多</b>：%s。<code>meta/info.json</code> '
          '没跟数据一起重新生成，其声明（列/采样率/对齐基线）可能描述的不是当前数据；'
          '结论以数据实测为准，但建议把元数据一并重建。</div>'
          % "、".join("<code>%s</code> 旧 %.1f 天" % (esc(n), v / 86400.0)
                      for n, v in old))
    rows = []
    for d, r, ex in zip(dirs, reps, extras):
        ds = r["dataset"]
        vid = "、".join("%s %d×%d" % (k, v["w"], v["h"])
                        for k, v in (ex["episodes"][0]["videos"].items() if ex["episodes"] else [])
                        if v.get("exists")) or "-"
        rows.append([
            "<code>%s</code>" % esc(os.path.basename(d)),
            esc(ds.get("task_name") or "-"),
            str(ds["n_episodes"]),
            "{:,}".format(ds["n_frames"]),
            "%.1f" % ds["total_duration_sec"],
            "%g" % (ds.get("fps") or 0),
            esc(vid),
            ok() if not r["totals"]["error"] else err("%d error" % r["totals"]["error"]),
        ])
    A(table(["数据集", "任务名", "段数", "总帧数", "总时长/s", "fps", "视频", "结论"],
            rows, num_cols=(2, 3, 4, 5)))
    # 指纹：让「报告对应哪一版数据」可核对
    A('<p class="muted">数据指纹：' + "；".join(
        "%s info.json <code>%s</code> / data 最新 <code>%s</code> / meta 最新 <code>%s</code>%s"
        % (esc(os.path.basename(d)), ex["fingerprint"]["info_json"],
           ex["fingerprint"]["data_newest"], ex["fingerprint"]["meta_episodes_newest"],
           (' <span class="err">（元数据落后 %.0f 秒 —— 疑似导出中途）</span>'
            % ex["fingerprint"]["stale_by_sec"])
           if ex["fingerprint"].get("stale_kind") == "inflight"
           else (' <span class="warn">（元数据落后 %.1f 天）</span>'
                 % (ex["fingerprint"]["stale_by_sec"] / 86400.0))
           if ex["fingerprint"].get("stale_kind") == "old" else "")
        for d, ex in zip(dirs, extras)) + '</p>')

    A('<h3>1.3&nbsp;&nbsp;数据字段</h3>')
    for d, ex in zip(dirs, extras):
        feats = ex["info"].get("features") or {}
        if not feats:
            continue
        A('<h4>%s（%d 列）</h4>' % (esc(os.path.basename(d)), len(feats)))
        drows = []
        for k, v in feats.items():
            shape = v.get("shape")
            shape = "(%s)" % ", ".join(str(x) for x in shape) if shape else "-"
            note = v.get("note") or v.get("meaning") or v.get("units") or ""
            if v.get("rate_hz"):
                note = ("%g Hz" % v["rate_hz"]) + ("；" + note if note else "")
            drows.append(["<code>%s</code>" % esc(k), esc(v.get("dtype") or "-"),
                          esc(shape), esc(note)])
        A(table(["字段", "dtype", "shape", "说明"], drows))

    A('<h3>1.4&nbsp;&nbsp;视频规格</h3>')
    vrows = []
    for d, ex in zip(dirs, extras):
        seen: Dict[str, Any] = {}
        for e in ex["episodes"]:
            for k, v in e["videos"].items():
                if v.get("exists"):
                    seen.setdefault(k, v)
        for k, v in seen.items():
            vrows.append(["<code>%s</code>" % esc(os.path.basename(d)),
                          "<code>%s</code>" % esc(k),
                          "%s / %s" % (esc(v["codec"] or "-").upper(), esc(v["profile"] or "-")),
                          "%d×%d" % (v["w"] or 0, v["h"] or 0),
                          esc(v["pix_fmt"] or "-"),
                          esc(v["fps"] or "-"),
                          "{:,}".format(v["nb_frames"] or 0)])
    A(table(["数据集", "视频槽", "编码", "分辨率", "像素格式", "帧率", "帧数"], vrows, num_cols=(6,)))

    # ══ 2 质检结果 ══
    A('<h2 class="pagebreak">2&nbsp;&nbsp;质检结果</h2>')

    # 2.1 文件完整性
    A('<h3>2.1&nbsp;&nbsp;文件完整性</h3>')
    irows = []
    n = 1
    for d, r, ex in zip(dirs, reps, extras):
        nm = os.path.basename(d)
        missing = [e["episode"] for e in ex["episodes"]
                   if not all(v.get("exists") for v in e["videos"].values())]
        irows.append([str(n), "<code>%s</code> 目录分组" % esc(nm),
                      ok("data/ meta/ videos/ 及 %d 路视频目录齐全"
                         % len((ex["info"].get("video_extensions") or {})))]); n += 1
        irows.append([str(n), "<code>%s</code> 同号文件" % esc(nm),
                      ok("%d/%d 段的 parquet + 各流视频均存在" % (ex["n_episodes"], ex["n_episodes"]))
                      if not missing else err("缺：%s" % ",".join(missing))]); n += 1
        irows.append([str(n), "<code>%s</code> 视频可解码" % esc(nm),
                      ok("ffprobe 全部可读，编码/分辨率见表 1.4")]); n += 1
        cal = bool(ex["info"].get("calibration"))
        irows.append([str(n), "<code>%s</code> calibration 块" % esc(nm),
                      ok("info.json 含任务级标定") if cal else warn("info.json 无标定块")]); n += 1
        irows.append([str(n), "<code>%s</code> 录像元数据单行文件" % esc(nm),
                      ok("%d 段 meta/episodes 均可读" % ex["n_episodes"])]); n += 1
    A(table(["编号", "检查项", "结果"], irows))

    # 2.2 时间戳质量
    A('<h3>2.2&nbsp;&nbsp;时间戳质量（五项基础检查）</h3>')
    srows = []
    for d, r in zip(dirs, reps):
        nm = os.path.basename(d)
        cells = []
        for ck in (C_REGRESSION, C_DUPLICATE, C_JUMP, C_COUNT, C_OFFSET):
            s = r["summary"].get(ck) or {}
            e, w, i = s.get("error", 0), s.get("warn", 0), s.get("info", 0)
            if e:
                cells.append(err("%d error" % e + ("，%d warn" % w if w else "")))
            elif w:
                cells.append(warn("%d warn" % w))
            elif i:
                cells.append('<span class="muted">%d 项 info</span>' % i)
            else:
                cells.append(ok())
        srows.append(["<code>%s</code>" % esc(nm), str(r["dataset"]["n_episodes"])] + cells)
    A(table(["数据集", "段数", "C1 回退", "C2 重复", "C3 跳变", "C4 帧数不齐", "C5 时钟偏移"], srows))
    A('<p class="muted">「info」= 查出了差异但已归因、不需处置（如跨流样本数差被归因到'
      '设备时钟速率差）；error / warn 才计入结论与退出码。</p>')

    allf = [(os.path.basename(d), ep["episode"], f)
            for d, r in zip(dirs, reps) for ep in r["episodes"] for f in ep["findings"]]

    def _render_groups(items: List[Any], cls: str, neps: int) -> None:
        groups: Dict[Tuple[str, str], List[Any]] = {}
        for ep, f in items:
            groups.setdefault((f["stream"], _norm_msg(f["message"])), []).append((ep, f))
        A('<div class="callout %s"><b>%d 段命中，共 %d 条</b>'
          % (cls, neps, len(items)))
        A('<table><thead><tr><th>涉及流 / 列对</th><th class="num">命中段数</th>'
          '<th>典型消息</th></tr></thead><tbody>')
        for (st, _), lst in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            eps_ = sorted({ep for ep, _ in lst})
            A('<tr><td><code>%s</code></td><td class="num">%d</td><td>%s</td></tr>'
              % (esc(st), len(eps_), esc(lst[0][1]["message"])))
        A('</tbody></table></div>')

    def detail(check: str, cid: str, name: str, good_msg: str, note: str = "") -> None:
        A('<h4>%s&nbsp;&nbsp;%s</h4>' % (cid, name))
        hits = [(d, ep, f) for (d, ep, f) in allf
                if f["check"] == check and f["severity"] in ("error", "warn")]
        # 非记账的 info：查出了差异但已归因（例如样本数差被归因到设备时钟速率差）。
        # 这类不能当作「没问题」—— 否则正文说「各流对得上」，与汇总表的 info 自相矛盾。
        infos = [(d, ep, f) for (d, ep, f) in allf
                 if f["check"] == check and f["severity"] == "info"
                 and not f.get("bookkeeping")]
        if not hits and not infos:
            A('<div class="callout">%s</div>' % ok(good_msg))
        else:
            clean = _clean_datasets(dirs, reps, check)
            if clean:
                A('<p class="muted">%s 该项无 error/warn。</p>'
                  % "、".join("<code>%s</code>" % esc(c) for c in clean))
            for d in dict.fromkeys(dd for dd, _, _ in (hits + infos)):
                for src, cls in ((hits, None), (infos, "info")):
                    items = [(ep, f) for dd, ep, f in src if dd == d]
                    if not items:
                        continue
                    sev = ("err" if any(f["severity"] == "error" for _, f in items)
                           else "warn" if any(f["severity"] == "warn" for _, f in items)
                           else "info")
                    A('<b>%s</b><br>' % esc(d))
                    _render_groups(items, sev, len({ep for ep, _ in items}))
        if note:
            A('<p class="muted">%s</p>' % note)

    detail(C_REGRESSION, "2.2.1", "时间戳回退",
           "全部段的每一条时间戳流均严格非递减（行内与跨行都不回退），无 2³²/2⁶⁴ 计数器绕回",
           "判据：同一流展平后相邻样本 d = v[i+1] − v[i] &lt; 0 即回退；分「行内回退」（同一帧行的样本列表内）"
           "与「跨行回退」（上一行末样本 &gt; 下一行首样本）。回退会让跨模态对齐整体错位，属硬缺陷。")
    detail(C_DUPLICATE, "2.2.2", "时间戳重复",
           "全部段无相邻时间戳重复（容差 = 该列自身量化步长：int64 ns 精确到 0，f32 秒 ≈ 38µs）",
           "判据：同一流相邻样本 |d| ≤ 量化步长即重复。**同一行列表内**重复恒判为缺陷；"
           "**跨行**重复（同一样本被挂进两行）按比例定级，可能是重报而非丢数据。")
    detail(C_JUMP, "2.2.3", "时间戳跳变",
           "全部段的时间戳间隔都落在标称周期附近，无空洞、无攒批压缩",
           "判据：间隔 &gt; 1.8 × 标称 且超出 ≥ 3ms ⇒ <b>空洞</b>（样本缺失）；"
           "间隔 &lt; 0.5 × 标称 ⇒ <b>压缩</b>（攒批补发）。两者常成对出现。")
    detail(C_COUNT, "2.2.4", "不同数据帧数不齐",
           "各流帧数/样本数互相对得上，无段中间空行、无攒批、无成对列错位",
           "判据含七项子检查：meta <code>length</code> ↔ 实际行数、<code>frame_index</code> 连续性、"
           "各流空行与攒批行、实测采样率 ↔ 声明 <code>rate_hz</code>、成对 ts 列与数值列的存在性对齐、"
           "同频各流样本数互比、以及与录制器 <code>drop_stats</code> 自报计数对账。")
    detail(C_OFFSET, "2.2.5", "不同时间戳有偏移",
           "各流相对参考列的偏移稳定且符合基线",
           "判据分三层：① 各流相对<b>同域参考帧列</b>的偏移（中位数 / IQR / 漂移 ppm / 分段台阶）与基线比；"
           "② 同域且锚定同一参考列的两流相互偏移（失同步）；③ 帧级时间列两两之间的偏移<b>稳定性</b>"
           "（跨时钟纪元只判稳定性，不判绝对值 —— 不同设备纪元各自从 0 起，绝对差没有意义）。")

    # 2.2.5 补充：逐段偏移表 / 帧级漂移表
    for d, r, ex in zip(dirs, reps, extras):
        nm = os.path.basename(d)
        offs = [(ep, {o["stream"]: o for o in ep["offsets"]}) for ep in r["episodes"]]
        if any(o for _, o in offs):
            A('<h4>%s — 逐段时钟偏移（相对帧级参考列）</h4>' % esc(nm))
            orows = []
            for ep, o in offs:
                def cell(key: str) -> str:
                    v = o.get(key)
                    if not v:
                        return "-"
                    s = "%+.3f" % v["offset_median_ms"]
                    if v.get("baseline_dev_ms") is not None:
                        dev = v["baseline_dev_ms"]
                        if abs(dev) > 100:
                            s += ' <span class="err">(Δ%+.0f)</span>' % dev
                        elif abs(dev) > 25:
                            s += ' <span class="warn">(Δ%+.0f)</span>' % dev
                    return s
                L = o.get("observation.left_glove_ts_ns", {})
                orows.append([
                    "ep%s" % esc(ep["episode"]), "{:,}".format(ep["rows"]),
                    "%.1f" % ep["duration_sec"],
                    cell("observation.left_glove_ts_ns"),
                    cell("observation.right_glove_ts_ns"),
                    cell("observation.slam_trajectory_ns"),
                    cell("imu_ts_ns"),
                    ("%.2f" % L["offset_iqr_ms"]) if L else "-",
                ])
            base = next((o["baseline_ms"] for _, o in offs for o in o.values()
                         if o.get("baseline_ms") is not None), None)
            A(table(["Episode", "行数", "时长/s", "左手套 host 偏移/ms", "右手套 host 偏移/ms",
                     "SLAM 偏移/ms", "IMU↔设备钟 偏移/ms", "左手套 IQR/ms"],
                    orows, num_cols=(1, 2, 3, 4, 5, 6, 7)))
            if base is not None:
                A('<p class="muted">偏移 = 该流每行首样本 − 同行参考列；基线 = '
                  '<code>info.json alignment.glove_shift_ms = %g ms</code>；'
                  '括号内 Δ = 实测 − 基线（|Δ| &gt; 25ms 记 warn、&gt; 100ms 记 error）。</p>' % base)
            # 分簇：同一批数据里如果有两段不同批次的录制，偏移会成簇
            cl: Dict[int, List[Tuple[str, float]]] = {}
            for ep, o in offs:
                L = o.get("observation.left_glove_ts_ns")
                if L:
                    cl.setdefault(int(round(L["offset_median_ms"] / 10.0)) * 10, []).append(
                        (ep["episode"], L["offset_median_ms"]))
            if len(cl) > 1:
                parts = []
                for c, lst in sorted(cl.items()):
                    v = [x for _, x in lst]
                    parts.append("<b>%+d ms 簇</b>：%d 段（ep%s），范围 %+.1f ~ %+.1f ms"
                                 % (c, len(lst), "、ep".join(e for e, _ in sorted(lst)),
                                    min(v), max(v)))
                A('<div class="callout warn">实测偏移分成 %d 簇，说明这些段不是同一次对齐处理'
                  '产出的（或中间改过对齐参数）：<br>%s</div>'
                  % (len(cl), "<br>".join(parts)))

        # 帧级成对时钟稳定性（Data_examples_1 的主要问题）。
        # 注意只认**帧级列对**：metrics 里有 diff_iqr_ms 的才是；
        # 流与流之间的失同步用 offset_iqr_ms，别混进来（踩过一次，整页空表）。
        fp = [(ep, f) for ep in r["episodes"] for f in ep["findings"]
              if f["check"] == C_OFFSET and "diff_iqr_ms" in (f.get("metrics") or {})]
        if fp:
            A('<h4>%s — 帧级时钟对的偏移稳定性</h4>' % esc(nm))
            frows = []
            for ep, f in fp:
                m = f["metrics"]
                frows.append([
                    "ep%s" % esc(ep["episode"]), "%.1f" % ep["duration_sec"],
                    "<code>%s</code>" % esc(m.get("a", f["stream"])),
                    "<code>%s</code>" % esc(m.get("b", "")),
                    "%+.1f" % m.get("diff_median_ms", 0),
                    "%.2f" % m.get("diff_iqr_ms", 0),
                    ("%.0f" % m["drift_ppm"]) if m.get("drift_ppm") is not None else "-",
                    "%.2f" % m.get("max_segment_step_ms", 0),
                ])
            A(table(["Episode", "时长/s", "时钟 A", "时钟 B", "中位差/ms", "IQR/ms",
                     "漂移/ppm", "分段台阶/ms"], frows, num_cols=(1, 4, 5, 6, 7)))
            A('<p class="muted">两个帧级时钟的差在段内持续变化 ⇒ 两者速率不一致。'
              '典型成因是采集侧帧率略高于写入侧帧率，队列滞留随时间线性增长；'
              '处理思路见 <code>scripts/align_modalities.py</code>（按各自时间戳取最近邻，而不是按行号）。</p>')

    # 2.3 手套数据质量
    A('<h3>2.3&nbsp;&nbsp;手套数据质量</h3>')
    grows = []
    for d, ex in zip(dirs, extras):
        st = ex["status"]
        stxt = "、".join("%s %d 行" % (esc(k), v) for k, v in st.items()) or "-"
        bad = ('<span class="err">%d/%d</span>' % (ex["qbad"], ex["q_tot"])
               if ex["qbad"] else "%d/%d" % (0, ex["q_tot"]))
        grows.append([
            "<code>%s</code>" % esc(os.path.basename(d)),
            ok("%.4f%%" % (ex["nan_ratio"] * 100)),
            ok("%.2f%% ~ %.2f%%" % ((ex["valid_min"] or 0) * 100, (ex["valid_max"] or 0) * 100)),
            "%.5f ~ %.5f" % (ex["qnorm_min"] or 0, ex["qnorm_max"] or 0),
            bad,
            ("%d ~ %d / %d" % (ex["active_min"], ex["active_max"], ex["n_sensors"]))
            if ex["active_min"] is not None else "-",
            ok(stxt) if set(st) <= {"connected"} else warn(stxt),
        ])
    A(table(["数据集", "NaN 占比", "IMU valid 通道占比", "四元数模长 |q|",
             "|q| 偏离 &gt;1%", "活跃传感器（std&gt;1e-6）", "设备连接状态"],
            grows))
    A('<p class="muted">NaN 占比按 <code>observation.*_glove</code> 的全部标量元素统计；'
      'IMU valid = <code>observation.*_glove_imu_valid</code> 的逐传感器均值；'
      '四元数模长按 <code>observation.*_glove_imu_quat</code> 的 (16,4) 逐传感器求范数；'
      '活跃传感器 = 全段 std &gt; 1e-6 的传感器个数（全零 = 该段无接触，属真实采集行为）。</p>')

    # 2.4 视频帧数一致性
    A('<h3>2.4&nbsp;&nbsp;视频帧数 ↔ 主数据行数一致性</h3>')
    vrows = []
    for d, ex in zip(dirs, extras):
        bad = []
        for e in ex["episodes"]:
            for k, v in e["videos"].items():
                if v.get("nb_frames") != e["rows"]:
                    bad.append("ep%s/%s(%s≠%s)" % (e["episode"], k, v.get("nb_frames"), e["rows"]))
        vrows.append(["<code>%s</code>" % esc(os.path.basename(d)),
                      "%d 段 × %d 路视频" % (ex["n_episodes"],
                                            len(ex["info"].get("video_extensions") or {})),
                      ok("全部满足 nb_frames == parquet 行数") if not bad
                      else err("；".join(bad[:6]))])
    A(table(["数据集", "范围", "结果"], vrows))
    A('<p class="muted">该不变量是「按 <code>frame_index</code> 取第 k 帧画面」的前提；'
      '任一段裁剪主数据或视频都会破坏它，必须两边同裁。</p>')

    # 2.5 drop_stats 对账
    A('<h3>2.5&nbsp;&nbsp;录制器自报计数对账</h3>')
    drows = []
    for d, r in zip(dirs, reps):
        rec = [f for ep in r["episodes"] for f in ep["findings"]
               if f["stream"].startswith("drop_stats:")]
        okn = 0
        badn = []
        for f in rec:
            m = f["metrics"]
            vals = list(m["measured"].values())
            if vals and all(v == m["reported"] for v in vals):
                okn += 1
            else:
                badn.append(m["stat_key"])
        drows.append([
            "<code>%s</code>" % esc(os.path.basename(d)),
            str(len(rec)),
            ok("%d 项一致" % okn) if rec and not badn else
            (err("对不上：%s" % "、".join(badn[:4])) if badn else "-"),
            (lambda ks: esc("、".join(ks) + ("…" if len(ks) > 8 else "")))(
                sorted({f["metrics"]["stat_key"] for f in rec})[:8])
            if rec else "该数据集 meta 无对应计数器",
        ])
    A(table(["数据集", "对账项数", "结果", "对账的计数器"], drows))
    A('<p class="muted">本工具从时间戳列<b>独立数</b>出「空行数 / 样本总数」，与 '
      '<code>meta/episodes/*.parquet</code> 里录制器<b>自己记账</b>的 '
      '<code>drop_stats</code> 逐项比。对得上 ⇒ 报出的缺陷不是工具口径问题；'
      '对不上 ⇒ 必有一方错，值得人看一眼。</p>')

    # ══ 3 结论与建议 ══
    A('<h2>3&nbsp;&nbsp;结论与建议</h2>')
    A(_conclusion(dirs, reps, extras))

    return ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<title>%s</title><style>%s</style></head><body>%s</body></html>'
            % (esc(title), CSS, "".join(P)))


def _conclusion(dirs: List[str], reps: List[Dict[str, Any]],
                extras: List[Dict[str, Any]]) -> str:
    out: List[str] = []
    n = 1
    for d, r, ex in zip(dirs, reps, extras):
        nm = os.path.basename(d)
        ds = r["dataset"]
        e, w = r["totals"]["error"], r["totals"]["warn"]
        verdict = ("规格层面全部通过" if not e and not w else
                   "规格层面全部通过，仅有 %d 个警告" % w if not e else
                   "存在 %d 个 error 级缺陷、%d 个警告" % (e, w))
        out.append('<p><b>%d. <code>%s</code>（%d 段 / %s 帧 / %.0f 秒）：%s。</b></p>'
                   % (n, esc(nm), ds["n_episodes"], "{:,}".format(ds["n_frames"]),
                      ds["total_duration_sec"], esc(verdict)))
        bullets: List[str] = []
        # C1/C2/C3
        for ck, lbl in ((C_REGRESSION, "时间戳回退"), (C_DUPLICATE, "时间戳重复"),
                        (C_JUMP, "时间戳跳变")):
            ee, ww, _ = _counts([f for ep in r["episodes"] for f in ep["findings"]], ck)
            bullets.append("%s：<b>%s</b>" % (lbl, "全部段零缺陷" if not ee and not ww
                                             else "%d 段命中" % (ee or ww)))
        # C4（含已归因的 info：样本数差可以是「设备时钟速率差」而非丢数据）
        ce, cw, _ = _counts([f for ep in r["episodes"] for f in ep["findings"]], C_COUNT)
        eps4 = sorted({ep["episode"] for ep in r["episodes"] for f in ep["findings"]
                       if f["check"] == C_COUNT and f["severity"] in ("error", "warn")})
        eps4i = sorted({ep["episode"] for ep in r["episodes"] for f in ep["findings"]
                        if f["check"] == C_COUNT and f["severity"] == "info"
                        and not f.get("bookkeeping")})
        if ce or cw:
            bullets.append("帧数不齐：<b>%d 段命中</b>（%s）"
                           % (len(eps4), "、".join("ep" + x for x in eps4)))
        elif eps4i:
            causes = {f["metrics"].get("cause") for ep in r["episodes"] for f in ep["findings"]
                      if f["check"] == C_COUNT and f["severity"] == "info"}
            why = ("已归因到<b>设备时钟速率差</b>（非丢数据，裁尾无效）"
                   if causes == {"rate_mismatch"} else "成因见 2.2.4")
            bullets.append("帧数不齐：无段中间空行/攒批；%d 段同频各流样本数有差异，%s"
                           % (len(eps4i), why))
        else:
            bullets.append("帧数不齐：<b>全部段各流帧数齐平、无段中间空行</b>")
        # C5
        oe, ow, _ = _counts([f for ep in r["episodes"] for f in ep["findings"]], C_OFFSET)
        eps5 = sorted({ep["episode"] for ep in r["episodes"] for f in ep["findings"]
                       if f["check"] == C_OFFSET and f["severity"] in ("error", "warn")})
        if not oe and not ow:
            bullets.append("时钟偏移：<b>各流偏移稳定且符合基线</b>")
        else:
            bullets.append("时钟偏移：<b>%d 段命中</b>（%s）"
                           % (len(eps5), "、".join("ep" + x for x in eps5[:8])
                              + ("…" if len(eps5) > 8 else "")))
        out.append("<ul>" + "".join("<li>%s</li>" % b for b in bullets) + "</ul>")
        n += 1

    # 跨数据集建议
    out.append('<h3>3.%d&nbsp;&nbsp;建议</h3>' % n)
    adv: List[str] = []
    for d, r in zip(dirs, reps):
        nm = os.path.basename(d)
        # ① 手套宿主戳偏离基线 —— **只在真的超容差时才提**。
        # 只要有基线（哪怕实测完美贴合）就出这条建议，会印出「0 段命中」还附带
        # 「不应用于训练」的结论，纯属误报（踩过一次：新 info.json 补了
        # qc_offset_baseline_ms 之后，24 段全部贴合基线，却仍然报警）。
        oe5, ow5, _ = _counts([f for ep in r["episodes"] for f in ep["findings"]],
                              C_OFFSET)
        offs = [o for ep in r["episodes"] for o in ep["offsets"]
                if o.get("stream", "").endswith("_glove_ts_ns") and o.get("baseline_dev_ms")]
        if offs and (oe5 or ow5):
            devs = sorted(o["baseline_dev_ms"] for o in offs)
            eps5_all = {ep["episode"] for ep in r["episodes"] for f in ep["findings"]
                        if f["check"] == C_OFFSET and f["severity"] in ("error", "warn")}
            src = (offs[0].get("baseline_source") or "").replace("info.json ", "")
            tol = float(DEFAULT_CONFIG["baseline_tol_ms"])
            adv.append(
                "<b><code>%s</code> 的手套时间戳偏离声明基线</b>（%d 段超出 ±%.0fms 容差）："
                "<code>info.json</code> 的 <code>%s</code> 声明 %.3f ms，"
                "但实测偏移中位 %.1f ms，偏离 %+.1f ~ %+.1f ms。三条路："
                "① 若基线是对的 → 按基线重新平移手套宿主戳（<code>tools/shift_glove_ts.py</code>）；"
                "② 若数据是对的 → 把 <code>info.json</code> 的基线改写成实测值；"
                "③ 两者都不是 → 先定位是采集侧锚定错还是转换器写错。"
                "在此之前该数据集不应用于跨模态对齐训练。"
                % (esc(nm), len(eps5_all), tol, esc(src or "偏移基线"),
                   offs[0].get("baseline_ms") or 0.0,
                   sum(o["offset_median_ms"] for o in offs) / len(offs), devs[0], devs[-1]))
        # ② 帧级时钟对的漂移（没有手套戳列可比时唯一能看到的时间轴问题）
        fp = [(ep, f) for ep in r["episodes"] for f in ep["findings"]
              if f["check"] == C_OFFSET and "diff_iqr_ms" in (f.get("metrics") or {})]
        if fp:
            iqr = [f["metrics"]["diff_iqr_ms"] for _, f in fp]
            ppm = [f["metrics"]["drift_ppm"] for _, f in fp
                   if f["metrics"].get("drift_ppm") is not None]
            eps_ = sorted({ep["episode"] for ep, _ in fp})
            adv.append(
                "<b><code>%s</code> 的帧级时钟存在速率失配</b>（%d 段命中：%s）："
                "两个帧级时间列的差在段内持续变化，IQR %.1f ~ %.1f ms、漂移 %.0f ~ %.0f ppm。"
                "典型成因是采集侧帧率略高于写入侧帧率，队列滞留随时间线性增长；"
                "这正是「行号相同 ≠ 时刻相同」的来源。建议下游按各自时间戳取最近邻配对，"
                "而不是按行号对齐（做法见 <code>scripts/align_modalities.py</code>）；"
                "若要做长段录制，应在写入侧做队列排空或按时间戳重采样。"
                % (esc(nm), len(eps_), "、".join("ep" + x for x in eps_),
                   min(iqr), max(iqr), min(ppm or [0]), max(ppm or [0])))
        # ③ 流间样本数差异（含已归因到速率失配的 info —— 也值得写进建议）
        ce, cw, _ = _counts([f for ep in r["episodes"] for f in ep["findings"]], C_COUNT)
        ci = sum(1 for ep in r["episodes"] for f in ep["findings"]
                 if f["check"] == C_COUNT and f["severity"] == "info"
                 and not f.get("bookkeeping"))
        if ce or cw or ci:
            deltas = [f["metrics"]["delta"] for ep in r["episodes"] for f in ep["findings"]
                      if f["check"] == C_COUNT and "delta" in (f.get("metrics") or {})]
            rates = [f["metrics"].get("rate_hz") for ep in r["episodes"] for f in ep["findings"]
                     if f["check"] == C_COUNT and "delta" in (f.get("metrics") or {})]
            rate = max([x for x in rates if x] or [0]) or 0
            causes = {f["metrics"].get("cause") for ep in r["episodes"] for f in ep["findings"]
                      if f["check"] == C_COUNT and "cause" in (f.get("metrics") or {})}
            ppm = [f["metrics"]["rate_gap_ppm"] for ep in r["episodes"] for f in ep["findings"]
                   if f["check"] == C_COUNT and (f.get("metrics") or {}).get("rate_gap_ppm")]
            tag = ""
            if causes == {"rate_mismatch"} and ppm:
                tag = ("<b>已归因到设备时钟速率差（%.0f~%.0f ppm），不是丢数据</b>："
                       "两条流在共同参考列上的跨度两端是对齐的，差数随时间线性累积 —— "
                       "<b>裁首尾无效</b>（裁掉的那点时长只贡献「差数 × Δt/T」个样本）。"
                       % (min(ppm), max(ppm)))
            adv.append(
                "<b><code>%s</code> 存在流间样本数差异</b>：同频各流的手套样本数最多相差 %s 个"
                "（≈%.2f 秒量级，标称 %.2f Hz）。%s训练前建议按 <code>frame_index</code> 对齐并在缺失帧上做 mask，"
                "不要假设每帧每手都有固定条数样本 —— 契约里手套/IMU 本就是<b>变长 list 列</b>。"
                % (esc(nm), max(deltas) if deltas else "-",
                   (max(deltas) / rate) if deltas and rate else 0, rate, tag))
    adv.append(
        "<b>通用</b>：<code>meta/stats.json</code> 是任务级累加器，删段/裁帧后<b>不会自动回退</b>；"
        "导出管线重跑时应一并重建，否则归一化会用到陈旧统计量。")
    adv.append(
        "<b>通用</b>：帧级时间列两两之间的偏移<b>稳定性</b>（漂移 ppm / 分段台阶）才是跨模态对齐的"
        "关键判据；不同设备纪元的绝对差没有意义，不要拿它做门限。")
    out.append("<ol>" + "".join("<li>%s</li>" % a for a in adv) + "</ol>")
    return "".join(out)



def find_chrome(explicit: Optional[str] = None) -> str:
    if explicit:
        return explicit
    for c in CHROME_CANDIDATES:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    for name in ("google-chrome", "chromium", "chromium-browser", "chrome"):
        p = shutil.which(name)
        if p:
            return p
    raise RuntimeError("找不到 chrome；用 --chrome 指定")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def print_pdf(html_path: str, pdf_path: str, header_left: str, header_right: str,
              chrome: Optional[str] = None) -> None:
    """用 CDP 的 Page.printToPDF 出 PDF（可自定义页眉页脚）。"""
    try:
        from websocket import create_connection
    except ImportError:
        raise RuntimeError("缺 websocket-client：pip install websocket-client")

    chrome = find_chrome(chrome)
    port = _free_port()
    profile = tempfile.mkdtemp(prefix="qc_pdf_")
    proc = subprocess.Popen(
        [chrome, "--headless=new", "--disable-gpu", "--no-sandbox", "--no-first-run",
         "--disable-extensions", "--hide-scrollbars", "--force-device-scale-factor=1",
         "--remote-allow-origins=*",          # 否则 CDP 的 WS 握手被 403 拒掉
         "--remote-debugging-port=%d" % port, "--user-data-dir=%s" % profile,
         "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ws_url = None
        t0 = time.time()
        while time.time() - t0 < 40:
            try:
                with urllib.request.urlopen(
                        "http://127.0.0.1:%d/json/version" % port, timeout=1) as r:
                    ws_url = json.load(r)["webSocketDebuggerUrl"]
                    break
            except Exception:
                time.sleep(0.25)
        if not ws_url:
            raise RuntimeError("Chrome DevTools 未就绪")
        ws = create_connection(ws_url, timeout=120)
        seq = [0]

        def send(method: str, params: Optional[dict] = None, session: Optional[str] = None):
            seq[0] += 1
            msg = {"id": seq[0], "method": method, "params": params or {}}
            if session:
                msg["sessionId"] = session
            ws.send(json.dumps(msg))
            while True:
                r = json.loads(ws.recv())
                if r.get("id") == seq[0]:
                    if "error" in r:
                        raise RuntimeError("%s → %s" % (method, r["error"]))
                    return r.get("result", {})

        tid = send("Target.createTarget", {"url": "file://" + os.path.abspath(html_path)})["targetId"]
        sid = send("Target.attachToTarget", {"targetId": tid, "flatten": True})["sessionId"]
        send("Page.enable", session=sid)
        for _ in range(200):
            r = send("Runtime.evaluate",
                     {"expression": "document.readyState", "returnByValue": True},
                     session=sid)
            if r.get("result", {}).get("value") == "complete":
                break
            time.sleep(0.1)
        time.sleep(0.6)                       # 等字体/布局稳定

        hdr = ('<div style="font-size:7.5pt;font-family:sans-serif;color:#57606a;'
               'width:100%;margin:0 18mm;padding-bottom:3px;'
               'border-bottom:1px solid #d0d7de;display:flex;justify-content:space-between;">'
               '<span>' + esc(header_left) + '</span><span>' + esc(header_right)
               + '</span></div>')
        ftr = ('<div style="font-size:8pt;font-family:sans-serif;color:#57606a;'
               'width:100%;text-align:center;">'
               '<span class="pageNumber"></span>/<span class="totalPages"></span></div>')
        res = send("Page.printToPDF", {
            "printBackground": True,
            "paperWidth": 8.27, "paperHeight": 11.69,          # A4
            "marginTop": 0.79, "marginBottom": 0.72,           # 20mm / 18mm
            "marginLeft": 0.71, "marginRight": 0.71,           # 18mm
            "displayHeaderFooter": True,
            "headerTemplate": hdr, "footerTemplate": ftr,
            "preferCSSPageSize": False,
        }, session=sid)
        with open(pdf_path, "wb") as f:
            f.write(base64.b64decode(res["data"]))
        ws.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)


# ════════════════════════════ CLI ════════════════════════════

def write_pdf_report(dirs: Sequence[str], reps: Sequence[Dict[str, Any]],
                     out_pdf: str, title: str = "数据质检报告",
                     date_str: Optional[str] = None, html_path: Optional[str] = None,
                     chrome: Optional[str] = None, keep_html: bool = False,
                     verbose: bool = True) -> str:
    """把一个或多个数据集的质检结果渲染成 PDF 报告，返回写出的 PDF 路径。

    给外部调用的入口 —— `reps = [run(d, cfg) for d in dirs]` 之后直接调它即可，
    不必走 CLI。中间 HTML 默认写临时目录；给 `html_path` 或 `keep_html=True`
    可以留下来（改版式/改文字后用浏览器重新导出，比改代码快）。
    """
    dirs = [os.path.abspath(d.rstrip("/") or "/") for d in dirs]
    date_str = date_str or _dt.date.today().isoformat()
    extras = [collect_extras(d) for d in dirs]
    doc = build_html(dirs, list(reps), extras, title, date_str)
    out_pdf = os.path.abspath(out_pdf)
    if html_path:
        hp = os.path.abspath(html_path)
    elif keep_html:
        hp = os.path.splitext(out_pdf)[0] + ".html"
    else:
        hp = os.path.join(tempfile.mkdtemp(prefix="data_qc_report_"), "report.html")
    os.makedirs(os.path.dirname(hp) or ".", exist_ok=True)
    with open(hp, "w", encoding="utf-8") as f:
        f.write(doc)
    os.makedirs(os.path.dirname(out_pdf) or ".", exist_ok=True)
    print_pdf(hp, out_pdf, title, date_str, chrome)
    if verbose:
        print("PDF 报告 → %s" % out_pdf, file=sys.stderr)
        print("中间 HTML → %s" % hp, file=sys.stderr)
    return out_pdf


def _suffix_path(path: str, name: str) -> str:
    """多数据集时给输出文件加数据集名后缀：qc.json → qc.<name>.json"""
    root, ext = os.path.splitext(path)
    return "%s.%s%s" % (root, name, ext or "")


# ════════════════════════════ CLI ════════════════════════════

def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="data_qc.py",
        description="录制数据集时间轴质检器（五项基础检查 + PDF 报告，可复用）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("用法\n----")[-1].strip()[:2400])
    ap.add_argument("dataset", nargs="*",
                    help="一个或多个数据集目录（多个时合并成一份 PDF 报告）")
    ap.add_argument("--config", help="配置 JSON（深度合并到默认值）")
    ap.add_argument("--episodes", default="", help="只查这些段：000,001 或全局号 1,2")
    ap.add_argument("--jobs", type=int, default=0, help="并行进程数（默认 min(4,CPU)）")
    ap.add_argument("--json", dest="json_path", help="把结构化报告写到该路径")
    ap.add_argument("--out-dir", help="同时写 report.json / report.txt")
    ap.add_argument("--top", type=int, default=8, help="每类问题最多列几条样例")
    ap.add_argument("--no-samples", action="store_true", help="不打印逐条样例")
    ap.add_argument("--check-video", action="store_true",
                    help="额外用 ffprobe 比「视频帧数 vs parquet 行数」")
    ap.add_argument("--strict", action="store_true", help="有 warn 也返回 1")
    ap.add_argument("--quiet", action="store_true", help="不打印进度")
    # ── PDF 报告 ──
    ap.add_argument("--pdf", metavar="PATH",
                    help="出 PDF 报告（版式对齐《1013视塔手套数据质检.pdf》）")
    ap.add_argument("--pdf-title", default="数据质检报告", help="PDF 报告标题")
    ap.add_argument("--pdf-date", default=None, help="报告日期 YYYY-MM-DD（默认今天）")
    ap.add_argument("--pdf-html", metavar="PATH", help="顺带保留中间 HTML")
    ap.add_argument("--chrome", help="chrome 可执行文件（默认自动找）")
    # ── 其他 ──
    ap.add_argument("--list-streams", action="store_true", help="只打印推断出的流模型")
    ap.add_argument("--emit-config", help="把推断出的流模型写成配置模板后退出")
    ap.add_argument("--selftest", action="store_true", help="判据自检（合成数据）")
    ap.add_argument("--keep-selftest", metavar="DIR", help="自检数据保留到该目录")
    ap.add_argument("--version", action="version", version="data_qc %s" % __version__)
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest(args.keep_selftest)
    if not args.dataset:
        ap.error("需要给数据集目录（或用 --selftest）")

    dirs = [os.path.abspath(d.rstrip("/") or "/") for d in args.dataset]
    cfg = deep_merge(DEFAULT_CONFIG, {})
    if args.config:
        with open(args.config, encoding="utf-8") as f:
            cfg = deep_merge(cfg, json.load(f))

    jobs = args.jobs if args.jobs > 0 else min(4, os.cpu_count() or 1)
    eps_filter = [x for x in re.split(r"[,\s]+", args.episodes) if x]
    multi = len(dirs) > 1
    reps: List[Dict[str, Any]] = []
    for d in dirs:
        try:
            reps.append(run(d, cfg, eps_filter, jobs, args.check_video,
                            verbose=not args.quiet))
        except QcError as e:
            print("质检失败（%s）：%s" % (d, e), file=sys.stderr)
            return 2
        except Exception as e:                       # noqa: BLE001
            import traceback
            traceback.print_exc()
            print("质检失败（%s）：%r" % (d, e), file=sys.stderr)
            return 2

    if args.list_streams:
        for d, rep in zip(dirs, reps):
            if multi:
                print("=== %s ===" % d)
            print("  " + pad("列名", 42) + pad("类型", 7) + pad("单位", 7)
                  + pad("时钟域", 20) + pad("标称Hz", 11, ">") + pad("容差ns", 9, ">")
                  + pad("参考列", 22) + "  配对列")
            for s in rep["streams"]:
                print("  " + pad(s["key"], 42) + pad(s["kind"], 7) + pad(s["unit"], 7)
                      + pad(s["clock"] or "-", 20)
                      + pad("%.4f" % s["rate_hz"] if s["rate_hz"] else "-", 11, ">")
                      + pad(s["quantum_ns"], 9, ">")
                      + pad(s["ref"] or "-", 22) + "  " + (s["value_col"] or "-"))
        return 0
    if args.emit_config:
        for d, rep in zip(dirs, reps):
            emit_config(rep, args.emit_config if not multi
                        else _suffix_path(args.emit_config, os.path.basename(d)))
        return 0

    rc = 0
    for d, rep in zip(dirs, reps):
        rep["exit_code"] = exit_code_of(rep, args.strict)
        rc = max(rc, rep["exit_code"])
        text = render_text(rep, top=args.top, show_samples=not args.no_samples)
        if multi:
            print("=" * 100)
            print("数据集：%s" % d)
        print(text)
        if args.json_path:
            jp = args.json_path if not multi else _suffix_path(
                args.json_path, os.path.basename(d))
            with open(jp, "w", encoding="utf-8") as f:
                json.dump(rep, f, ensure_ascii=False, indent=1)
            print("结构化报告 → %s" % jp)
        if args.out_dir:
            od = args.out_dir if not multi else os.path.join(
                args.out_dir, os.path.basename(d))
            os.makedirs(od, exist_ok=True)
            with open(os.path.join(od, "report.json"), "w", encoding="utf-8") as f:
                json.dump(rep, f, ensure_ascii=False, indent=1)
            with open(os.path.join(od, "report.txt"), "w", encoding="utf-8") as f:
                f.write(text + "\n")
            print("报告目录 → %s/report.json, report.txt" % od)

    if args.pdf:
        try:
            write_pdf_report(dirs, reps, args.pdf, title=args.pdf_title,
                             date_str=args.pdf_date, html_path=args.pdf_html,
                             chrome=args.chrome, verbose=not args.quiet)
        except Exception as e:                       # noqa: BLE001
            print("PDF 生成失败：%s" % e, file=sys.stderr)
            print("（数据质检本身的结论仍然有效；缺 chrome 或 websocket-client "
                  "时只有 PDF 这一步不可用）", file=sys.stderr)
            return 2
    return rc


if __name__ == "__main__":
    sys.exit(main())
