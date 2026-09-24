#!/usr/bin/env python3
"""SLAM 桥接 stdout 的「行分类基线」——换核心前后做 A/B。

为什么需要它：桥接与主程序之间靠 stdout 逐行文本协议说话，而
`core/gripper/slam/protocol.py` 里有一条**粘滞**的判据 `_ERROR_RE`
（命中即 ProtocolEvent("error") → state.error → 锁死 wait_sdk_ready 就绪门，
2026-09-10 18:32 有过一次假超时的先例）。换 SLAM 核心之后，日志文本会变，
必须能一眼看出「哪些行改了类」而不是靠肉眼看几十兆日志。

用法：

    # 冻结基线（换核心之前跑）
    python3 tools/audit_slam_line_classes.py --capture baseline.json

    # 换核心之后比对
    python3 tools/audit_slam_line_classes.py --compare baseline.json

比对是**确定性**的：同一批日志跑两次结果逐字节相同。

注意区分两个概念（本工具都记）：
  * `error_classified`：真正走完 parse_slam_line 落到 "error" 分支的行
    —— **这才是锁死就绪门的那个集合**；
  * `error_re_raw`：`_ERROR_RE` 的裸命中集合（超集）。它更大，因为
    parse_slam_line 对 `[FPS_DATA]` 等前缀会**提前返回**，那些行即便含
    "cannot" 也不会变成 error。只看裸命中会高估风险。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.gripper.slam.protocol import (  # noqa: E402
    _ERROR_RE,
    parse_slam_line,
)

DEFAULT_LOG_DIR = REPO_ROOT / "logs" / "slam_native"


def _iter_bridge_lines(path: Path):
    """Yield the raw bridge stdout lines out of an archived JSONL log.

    留档不是纯文本：每行是 `{"time":…, "arrival_monotonic":…, "line": "<原始行>"}`
    （少数归档事件记录没有 `line` 字段）。**必须取内层 `line`**——实时链路
    `parse_slam_line()` 看到的就是它；直接拿整个 JSON 信封去分类，会把
    `"time": …` 这类信封字段也算进去，得到一份假的基线。
    """
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for raw_line in fh:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            if raw_line.startswith("{"):
                try:
                    record = json.loads(raw_line)
                except json.JSONDecodeError:
                    yield raw_line  # 坏 JSON：当纯文本处理，不静默丢
                    continue
                if isinstance(record, dict) and "line" in record:
                    yield str(record["line"])
                # 没有 line 的是归档事件（native_process_started 等），跳过
                continue
            yield raw_line


def classify_file(path: Path):
    """Return (kind_counter, error_lines, raw_regex_lines) for one log."""
    kinds: Counter[str] = Counter()
    errors: Counter[str] = Counter()
    raw_hits: Counter[str] = Counter()
    for raw in _iter_bridge_lines(path):
        raw = raw.strip()
        if not raw:
            continue
        if _ERROR_RE.search(raw):
            raw_hits[raw] += 1
        event = parse_slam_line(raw)
        if event is None:
            continue
        kinds[event.kind] += 1
        if event.kind == "error":
            errors[raw] += 1
    return kinds, errors, raw_hits


def scan(log_dir: Path):
    files = sorted(log_dir.glob("*_slam_stdout.log"))
    if not files:
        raise SystemExit(f"没有找到日志：{log_dir}/*_slam_stdout.log")

    kinds_total: Counter[str] = Counter()
    errors_total: Counter[str] = Counter()
    raw_total: Counter[str] = Counter()
    per_file = {}
    for path in files:
        kinds, errors, raw_hits = classify_file(path)
        kinds_total.update(kinds)
        errors_total.update(errors)
        raw_total.update(raw_hits)
        per_file[path.name] = sum(kinds.values())

    return {
        "log_dir": str(log_dir),
        "file_count": len(files),
        "parsed_lines_total": sum(kinds_total.values()),
        "kinds": dict(sorted(kinds_total.items())),
        "error_classified": dict(sorted(errors_total.items())),
        "error_re_raw": dict(sorted(raw_total.items())),
        "per_file_parsed": per_file,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--capture", type=Path, metavar="OUT.json",
                   help="把当前分类结果写成基线")
    g.add_argument("--compare", type=Path, metavar="BASE.json",
                   help="与基线比对")
    args = ap.parse_args()

    current = scan(args.log_dir)

    if args.capture:
        args.capture.write_text(
            json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        print(f"[基线已冻结] {args.capture}")
        print(f"  日志 {current['file_count']} 份，"
              f"解析 {current['parsed_lines_total']} 行")
        print(f"  kind 分布：{current['kinds']}")
        print(f"  error 分类行：{len(current['error_classified'])} 种")
        print(f"  _ERROR_RE 裸命中：{len(current['error_re_raw'])} 种")
        return 0

    base = json.loads(args.compare.read_text(encoding="utf-8"))
    problems = []

    print(f"=== 与基线比对（{args.compare}）===")
    if base["log_dir"] != current["log_dir"]:
        print(f"  [注] 日志目录不同：基线 {base['log_dir']} -> 现在 {current['log_dir']}")

    base_kinds = Counter(base["kinds"])
    cur_kinds = Counter(current["kinds"])
    for kind in sorted(set(base_kinds) | set(cur_kinds)):
        b, c = base_kinds.get(kind, 0), cur_kinds.get(kind, 0)
        if b != c:
            print(f"  kind {kind!r}: {b} -> {c}")
    if base_kinds == cur_kinds:
        print("  kind 分布：一致")

    print("\n--- 新增的 error 分类行（最要紧）---")
    base_err = Counter(base["error_classified"])
    cur_err = Counter(current["error_classified"])
    new_err = cur_err - base_err
    if new_err:
        for line, n in sorted(new_err.items()):
            print(f"  [新增] x{n}  {line}")
        problems.append(f"{len(new_err)} 种新的 error 分类行")
    else:
        print("  无（好）")

    gone_err = base_err - cur_err
    if gone_err:
        print("\n--- 基线里有、现在没有的 error 行 ---")
        for line, n in sorted(gone_err.items()):
            print(f"  [消失] x{n}  {line}")

    print("\n--- 新增的 _ERROR_RE 裸命中（提示性，未必真的锁门）---")
    new_raw = Counter(current["error_re_raw"]) - Counter(base["error_re_raw"])
    if new_raw:
        for line, n in sorted(new_raw.items()):
            print(f"  [新增] x{n}  {line}")
    else:
        print("  无")

    if problems:
        print(f"\n[需人工判定] {'; '.join(problems)}")
        print("  逐条判：是「新文本恰好含 error/cannot 等词」，还是真出错。")
        print("  前者按 protocol.py 的 _IGNORED_NON_FATAL_RE 补白名单（留证据）。")
        return 1
    print("\n[通过] 没有新增的 error 分类行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
