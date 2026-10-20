#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""**已并入 `tools/data_qc.py`** —— 本文件只是薄封装，保留旧命令可用。

新代码请直接用质检器的 `--pdf`：

    venv/bin/python tools/data_qc.py <数据集目录> [...] --pdf 报告.pdf

本文件把老参数原样翻译过去，自身不含任何逻辑（版式、采集、渲染全在
`data_qc.py` 的 PDF 段里，只有一份实现）。

    venv/bin/python tools/make_qc_report.py <数据集目录> [...] -o 报告.pdf
    # 等价于
    venv/bin/python tools/data_qc.py <数据集目录> [...] --pdf 报告.pdf
"""

from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import data_qc as qc                                            # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="make_qc_report.py",
        description="（已并入 tools/data_qc.py）用质检器渲染 PDF 报告",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("新代码请直接用质检器的")[-1].strip()[:800])
    ap.add_argument("datasets", nargs="+", help="一个或多个数据集目录")
    ap.add_argument("-o", "--out", help="输出 PDF 路径")
    ap.add_argument("--title", default="数据质检报告")
    ap.add_argument("--date", default=None)
    ap.add_argument("--html", help="中间 HTML 路径")
    ap.add_argument("--chrome", help="chrome 可执行文件")
    ap.add_argument("--keep-html", action="store_true")
    ap.add_argument("--jobs", type=int, default=0)
    args = ap.parse_args(argv)

    dirs = [os.path.abspath(d.rstrip("/")) for d in args.datasets]
    for d in dirs:
        if not os.path.isdir(d):
            print("数据集目录不存在：%s" % d, file=sys.stderr)
            return 2
    jobs = args.jobs if args.jobs > 0 else min(4, os.cpu_count() or 1)
    cfg = qc.deep_merge(qc.DEFAULT_CONFIG, {})
    reps = [qc.run(d, cfg, None, jobs, True, verbose=True) for d in dirs]
    out = args.out or os.path.join(os.path.dirname(dirs[0]), "数据质检报告.pdf")
    qc.write_pdf_report(dirs, reps, out, title=args.title, date_str=args.date,
                        html_path=args.html, chrome=args.chrome,
                        keep_html=args.keep_html)
    return 0


if __name__ == "__main__":
    sys.exit(main())
