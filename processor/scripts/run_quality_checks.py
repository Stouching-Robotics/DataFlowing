#!/usr/bin/env python3
"""离线跑数据质检 —— 只读数据集，打印真实分布。

**这个脚本不写任何东西**：不落盘、不改数据、不碰 state/、不接工作流。
它的唯一目的是拿真实数据回答"阈值该定多少"——在此之前所有阈值都是猜。

引擎是 ``app.processing.cleaning.engine``，本脚本只负责遍历与展示。

用法::

    # 快速（只看 parquet，不碰视频）
    python scripts/run_quality_checks.py --dataset <LeRobot数据集路径>

    # 含视频检查（慢，每集要解码抽样）
    python scripts/run_quality_checks.py --dataset <路径> --video --limit 5

    # 看某一集的逐项结果
    python scripts/run_quality_checks.py --dataset <路径> --episode 1 -v

    # 用配置文件覆盖阈值（结构见 data_quality 节点的 config）
    python scripts/run_quality_checks.py --dataset <路径> --config cfg.json

数据集可以是 LeRobot v2.1/v3.0 导出产物（meta/info.json + data/*.parquet），
也可以是 sessions/<项目>/ 下的 canonical 数据。
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from app.processing.cleaning.contract import (  # noqa: E402
    PASS, WARN, FAIL, PENDING, ERROR, SEVERITY_LABELS,
)
from app.processing.cleaning.engine import run_checks, ruleset_revision  # noqa: E402

_MARK = {PASS: "✓", WARN: "!", FAIL: "✗", PENDING: "?", ERROR: "E"}


def iter_episodes(dataset: Path) -> list[tuple[int | None, Path]]:
    """列出要跑的 (episode_index, 文件) 组合。

    canonical 一集一文件 → episode_index 为 None（文件本身就是那一集）；
    导出产物一文件多集 → 需要逐个 episode_index 筛。
    """
    out: list[tuple[int | None, Path]] = []
    for path in sorted(glob.glob(str(dataset / "data" / "chunk-*" / "*.parquet"))):
        parquet = Path(path)
        if parquet.name.startswith("episode_"):
            out.append((None, parquet))          # canonical：一集一文件
        else:
            import pyarrow.parquet as pq
            table = pq.ParquetFile(parquet)
            if "episode_index" not in table.schema_arrow.names:
                out.append((None, parquet))
                continue
            for value in sorted(set(table.read(columns=["episode_index"])
                                     ["episode_index"].to_pylist())):
                out.append((int(value), parquet))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", required=True, help="数据集根目录")
    parser.add_argument("--limit", type=int, default=0, help="最多跑几集（0=全部）")
    parser.add_argument("--episode", type=int, default=None, help="只看这一集")
    parser.add_argument("--video", action="store_true", help="跑视频检查（慢）")
    parser.add_argument("--config", default=None, help="阈值配置文件（JSON）")
    parser.add_argument("--modality", default=None,
                        help="设备卡片（如 gripper_device）；不传则按数据推断")
    parser.add_argument("-v", "--verbose", action="store_true", help="逐项打印")
    args = parser.parse_args()

    root = Path(args.dataset).expanduser().resolve()
    if not root.is_dir():
        print(f"找不到数据集目录: {root}")
        return 1

    config = None
    if args.config:
        config = json.loads(Path(args.config).read_text(encoding="utf-8"))

    episodes = iter_episodes(root)
    if args.episode is not None:
        episodes = [item for item in episodes if item[0] == args.episode]
    if args.limit:
        episodes = episodes[: args.limit]
    if not episodes:
        print(f"{root} 下没有找到可跑的 episode")
        return 1

    print(f"数据集: {root}")
    print(f"集数:   {len(episodes)}   （**只读，不写入任何东西**）")
    print(f"规则集: {ruleset_revision()}")
    print()

    per_episode: list[dict] = []
    rule_hits: Counter = Counter()
    metric_samples: dict[str, list] = defaultdict(list)

    started = time.time()
    for index, _parquet in episodes:
        report = run_checks(
            root, episode_index=index,
            episode_id=f"{root.name}_{index if index is not None else 0:06d}",
            project=root.name, config=config,
            probe_video=args.video, modality_hint=args.modality,
        )
        status = report["episode"]["status"]
        per_episode.append({"index": index, "status": status, "report": report})

        for finding in report["findings"]:
            if finding["status"] != PASS:
                rule_hits[finding["rule"]] += 1
            for key, value in (finding["metrics"] or {}).items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    metric_samples[f"{finding['rule']}.{key}"].append(float(value))

        if args.verbose:
            print(f"── 集 {index if index is not None else '-':>5}  "
                  f"{report['modalities'].get('rows', 0):>5} 帧  "
                  f"{SEVERITY_LABELS.get(status, status)} ──")
            for finding in report["findings"]:
                print(f"   {_MARK.get(finding['status'], ' ')} "
                      f"{finding['rule']:24s} "
                      f"{SEVERITY_LABELS.get(finding['status'], finding['status']):6s} "
                      f"{finding['message'][:56]}")
            for item in report["ranges"][:8]:
                print(f"       [{item['start_sec']:7.2f}s] {item['message'][:52]}")
            print()

    # ── 汇总 ────────────────────────────────────────────────
    print("=" * 72)
    print(f"跑了 {len(per_episode)} 集，用时 {time.time() - started:.1f} 秒")
    print("=" * 72)
    print()

    overall = Counter(item["status"] for item in per_episode)
    print("★ 按集统计（取每集最严重的）:")
    for status in (PASS, WARN, FAIL, PENDING, ERROR):
        count = overall.get(status, 0)
        if count:
            print(f"    {SEVERITY_LABELS[status]:8s} {count:>4} 集"
                  f"  ({count / len(per_episode):>5.1%})")
    print()

    print("★ 命中的检查项:")
    if not rule_hits:
        print("    （没有任何检查项报警）")
    for rule, count in rule_hits.most_common():
        print(f"    {rule:28s} {count:>4} 集")
    print()

    print("★ 关键指标的真实分布（定阈值要用这个）:")
    for key in sorted(metric_samples):
        values = np.asarray(metric_samples[key], dtype=float)
        if values.size < 2:
            continue
        print(f"    {key:48s} "
              f"中位 {np.median(values):>10.4f}  "
              f"p90 {np.percentile(values, 90):>10.4f}  "
              f"max {values.max():>10.4f}")
    print()
    print("★ 未写入任何文件。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
