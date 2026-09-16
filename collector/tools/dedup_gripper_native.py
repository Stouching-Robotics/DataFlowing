"""回收 core/gripper/native 里被展开成实体文件的软链副本（只动布局，不动内容）。

背景：厂商交付的 kit 在传输途中走过 ZIP/Windows 或 `cp -rL`，标准 `.so`
布局被解引用成实体副本。实测整棵树有 339 MB 是同一内容的重复存放，最夸张
的是 orb48_env 里同一个 libopenblas 存了 7 份（7 × 39.6 MB，md5 全一致），
而正常布局是「一个 libopenblasp-r0.3.34.so + 6 个软链」。

判定规则（保守，三条同时满足才动）：
  1. 同一个目录内（跨目录的重复一律不碰——不同目录可能是各自独立的变体，
     例如 dist/orb_mark_only/lib 与 lib_system_boost 各有一份 libORB_SLAM3.so）
  2. 内容逐字节相同（md5）
  3. 文件名是共享库名（`.so` / `.so.3` / `.so.4.2.0` 这种）

保留哪一份：名字最长的那个（`libopencv_core.so.4.2.0` 胜过 `.so.4.2` 与
`.so`；`libopenblasp-r0.3.34.so` 胜过 `libblas.so.3`）——这与 conda/系统
打包时「实体文件用最长版本名、其余做软链」的实际布局一致。字节既然相同，
即便选错也只是软链朝向问题，不影响任何加载行为。

不动的（只列出来给人看）：
  - 备份文件（`.pre_*_backup` 这类名字，不匹配共享库名）
  - 跨目录重复
  - 可执行文件（bin/ 下的产物没有 .so 后缀）

软链一律用**相对路径**，这样整棵树 `cp -a` 搬走后依然解析。

每次 --apply 都会落一份清单（默认 logs/gripper_native_dedup_<时间戳>.json），
记录每个被替换文件的路径、md5、体积与软链目标，凭它可以原样还原：
  ln -sf 的反操作 = 删软链 + 从 target 复制回来。

用法:
    python tools/dedup_gripper_native.py                # dry-run，只打印清单
    python tools/dedup_gripper_native.py --apply        # 实际执行
    python tools/dedup_gripper_native.py --root <目录>  # 指定其它树
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import re
import sys
import time

ROOT_DEFAULT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "core", "gripper", "native",
)

# 共享库名：xxx.so / xxx.so.3 / xxx.so.4.2.0（可带 -r0.3.34 这类内部版本）
_SHARED_LIB_RE = re.compile(r"^.+\.so(\.[0-9]+)*$")

MIN_SIZE = 300 * 1024   # 小于 300KB 的不值得动（软链本身也要占 inode）


def _md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def scan(root: str):
    """返回 (可回收组, 需人工判断的重复组)。"""
    by_key = collections.defaultdict(list)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in filenames:
            path = os.path.join(dirpath, name)
            try:
                if os.path.islink(path) or os.path.getsize(path) < MIN_SIZE:
                    continue
                by_key[(dirpath, _md5(path))].append((name, path))
            except OSError:
                continue

    reclaimable, review = [], []
    for (dirpath, digest), items in by_key.items():
        if len(items) < 2:
            continue
        size = os.path.getsize(items[0][1])
        group = {
            "dir": dirpath,
            "md5": digest,
            "size": size,
            "names": sorted(n for n, _ in items),
            "paths": sorted(p for _, p in items),
        }
        if all(_SHARED_LIB_RE.match(n) for n, _ in items):
            reclaimable.append(group)
        else:
            review.append(group)

    # 跨目录的重复单独提出来（不进可回收组）
    by_digest = collections.defaultdict(list)
    for (dirpath, digest), items in by_key.items():
        for name, path in items:
            by_digest[digest].append(path)
    cross = []
    for digest, paths in by_digest.items():
        dirs = {os.path.dirname(p) for p in paths}
        if len(dirs) > 1:
            cross.append({
                "md5": digest,
                "size": os.path.getsize(paths[0]),
                "paths": sorted(paths),
            })
    cross.sort(key=lambda g: -g["size"])
    return reclaimable, review, cross


def plan_for(group):
    """选最长名字那份当实体，其余转软链（相对路径）。"""
    paths = sorted(group["paths"], key=lambda p: (-len(os.path.basename(p)),
                                                  os.path.basename(p)))
    target = paths[0]
    return target, paths[1:]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=ROOT_DEFAULT)
    ap.add_argument("--apply", action="store_true", help="实际执行（默认 dry-run）")
    ap.add_argument("--manifest", default="",
                    help="清单输出路径（默认 logs/gripper_native_dedup_<时间戳>.json）")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        sys.exit(f"目录不存在: {root}")

    reclaimable, review, cross = scan(root)

    total = sum(g["size"] * (len(g["paths"]) - 1) for g in reclaimable)
    print(f"扫描: {root}")
    print(f"可回收组: {len(reclaimable)} 组，合计 {total / 1048576:.0f} MB\n")

    entries = []
    for group in sorted(reclaimable, key=lambda g: -g["size"] * len(g["paths"])):
        target, aliases = plan_for(group)
        rel_target = os.path.relpath(target, group["dir"])
        print(f"  {group['size'] * len(aliases) / 1048576:6.1f} MB  "
              f"{os.path.relpath(group['dir'], root)}/")
        print(f"        实体 → {os.path.basename(target)}")
        for alias in aliases:
            print(f"        软链 ← {os.path.basename(alias)}")
            entries.append({
                "path": alias, "action": "symlink",
                "target": rel_target, "size": group["size"],
                "md5": group["md5"],
            })
        print()

    if review:
        wasted = sum(g["size"] * (len(g["paths"]) - 1) for g in review)
        print(f"需人工判断（名字非共享库名，未动）: {len(review)} 组 / {wasted / 1048576:.0f} MB")
        for group in sorted(review, key=lambda g: -g["size"])[:10]:
            print(f"  {group['size'] / 1048576:5.1f} MB × {len(group['paths'])}  "
                  f"— {', '.join(os.path.basename(n) for n in group['names'][:4])}")
        print()

    if cross:
        wasted = sum(g["size"] * (len(g["paths"]) - 1) for g in cross)
        print(f"跨目录重复（可能是各自独立的变体，未动）: {len(cross)} 组 / {wasted / 1048576:.0f} MB")
        for group in cross[:6]:
            print(f"  {group['size'] / 1048576:5.1f} MB —")
            for p in group["paths"]:
                print(f"        {os.path.relpath(p, root)}")
        print()

    if not args.apply:
        print("dry-run 结束，未做任何修改。加 --apply 执行。")
        return

    manifest = args.manifest or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "logs", f"gripper_native_dedup_{time.strftime('%Y%m%d_%H%M%S')}.json")
    os.makedirs(os.path.dirname(manifest), exist_ok=True)

    done = 0
    for entry in entries:
        alias = entry["path"]
        link_dir = os.path.dirname(alias)
        os.remove(alias)                       # 删副本（内容在 target 里还在）
        os.symlink(entry["target"], alias)     # 相对软链
        done += 1
        assert os.path.realpath(alias) == os.path.realpath(
            os.path.join(link_dir, entry["target"])), alias

    with open(manifest, "w", encoding="utf-8") as fh:
        json.dump({"root": root, "applied_at": time.time(),
                   "reclaimed_bytes": total, "entries": entries},
                  fh, ensure_ascii=False, indent=2)

    saved = sum(os.path.getsize(p) for p in [] ) or total
    print(f"[完成] {done} 个副本转成软链，回收 {total / 1048576:.0f} MB")
    print(f"  清单: {manifest}")
    print("  还原: 删软链 + 从 target 复制回原文件（清单里有 md5 可校验）")


if __name__ == "__main__":
    main()
