"""训练产物的发现 —— ``settings.train_root`` 下的 ``checkpoints/<step>/``。

模型由官方 ``lerobot-train`` 在平台**之外**训练,产物就是一棵目录树。这一层
只负责如实把它读出来,不判断好坏、不执行任何训练。
"""

from __future__ import annotations

import json
from pathlib import Path

from app.config import settings


def _step_of(name: str) -> int | None:
    """``checkpoints`` 下的目录名 ⇒ 步数。

    正常的 checkpoint 目录就是零填充的步数(``005000``)。``last/`` 之类的
    非数字目录**跳过** —— 它通常是个空的符号链接残留,列出来只会让人以为
    多了一个模型。无法解析一律返回 None,调用方据此过滤。
    """
    try:
        return int(str(name).strip())
    except (TypeError, ValueError):
        return None


def _collect_files(root: Path) -> tuple[int, list[dict]]:
    """递归列出一个 checkpoint 的全部文件与总字节数。

    用 ``rglob`` 遍历真实磁盘内容而不是写死 ``pretrained_model/`` 与
    ``training_state/`` 两棵子树 —— 不同 LeRobot 版本落盘的文件集不一样
    (我们比参考实现少 ``scheduler_state.json``、多四个 pre/postprocessor),
    写死会在版本升级时静默漏列文件。
    """
    files: list[dict] = []
    total = 0
    for path in sorted(root.rglob("*")):
        try:
            if not path.is_file():
                continue
            st = path.stat()
        except OSError:
            continue                      # 竞态:训练正在写/文件刚被删
        files.append({
            "name": str(path.relative_to(root)),
            "size": st.st_size,
            "mtime": int(st.st_mtime),
        })
        total += st.st_size
    return total, files


def _read_train_config(checkpoint: Path) -> dict:
    """读 checkpoint 的 ``train_config.json``;缺失/损坏返回 {}。

    这份就是「训练参数」页签的数据源 —— 训练当时实际用的超参,而不是我们
    事后猜的默认值。
    """
    path = checkpoint / "pretrained_model" / "train_config.json"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _scan_run(run_dir: Path) -> dict | None:
    """把一个 ``output_dir`` 扫成一个 run;没有 ``checkpoints/`` 就返回 None。"""
    checkpoint_root = run_dir / "checkpoints"
    if not checkpoint_root.is_dir():
        return None

    checkpoints: list[dict] = []
    for entry in sorted(checkpoint_root.iterdir()):
        if not entry.is_dir():
            continue
        step = _step_of(entry.name)
        if step is None:
            continue
        total_bytes, files = _collect_files(entry)
        if not files:
            continue                      # 空目录(如 last/ 的残留)不成其为模型
        checkpoints.append({
            "step": step,
            "name": entry.name,
            "file_count": len(files),
            "total_bytes": total_bytes,
            "files": files,
            # 参数按**该 checkpoint**自己的配置读,不跨 checkpoint 复用 ——
            # 同一个 output_dir 下不同步数的超参可以不同(改过参数续训)。
            "train_config": _read_train_config(entry),
        })

    if not checkpoints:
        return None
    # 最新的排最前 —— 页面按这个顺序展示,「训练参数」页签也取第一份。
    checkpoints.sort(key=lambda item: item["step"], reverse=True)
    newest = checkpoints[0]
    policy = newest["train_config"].get("policy") or {}
    return {
        "name": run_dir.name,
        "path": str(run_dir),
        "model_type": str(policy.get("type") or ""),
        "checkpoint_count": len(checkpoints),
        "total_bytes": sum(c["total_bytes"] for c in checkpoints),
        "checkpoints": checkpoints,
    }


def resolve_checkpoint_file(run_name: str, step: str, relative_name: str) -> Path | None:
    """把 (run 名, checkpoint 名, 相对文件名) 解析成一个可下载的**真实路径**。

    找不到返回 None,由路由层转成 404。

    ★ **安全:不做路径拼接。** 直觉写法是 `checkpoint_dir / relative_name`,但那样
    ``../../../../etc/passwd`` 就能读到任意文件 —— 这个端点是要对外开下载的,
    必须挡住。这里改成:**先扫一遍拿到磁盘上的真实文件清单,再按名字精确匹配**,
    所以返回的路径必然来自真实条目,构造不出清单外的路径。

    (不用 `Path.resolve()` + `is_relative_to` 那种断言式校验 —— 清单匹配是从
    源头上就没有越界的可能,而不是"先拼出来再检查拼对了没"。)
    """
    wanted = str(relative_name or "").strip()
    if not wanted:
        return None

    data = list_runs()
    for run in data.get("runs", []):
        if run.get("name") != run_name:
            continue
        for checkpoint in run.get("checkpoints", []):
            if checkpoint.get("name") != step:
                continue
            for entry in checkpoint.get("files", []):
                if entry.get("name") == wanted:
                    root = Path(run["path"]) / "checkpoints" / step
                    return root / wanted
    return None


def list_runs() -> dict:
    """``{"runs": [...], "root": str, "exists": bool}``。

    **两种层级都支持**,因为两种都很常见、也都会被人配进来:

        root 本身就是 run      <root>/checkpoints/<step>/…
        root 是 run 的父目录   <root>/<run>/checkpoints/<step>/…

    实测默认路径 ``文件测试/umi_act_model`` 属于前者 —— 把它按后者扫会
    一律返回空列表(``umi_act_model/checkpoints/checkpoints`` 当然不存在)。

    目录不存在 ⇒ ``{"runs": []}``(不是错误):新机器、或本机从未跑过训练,
    都是正常情况,前端据此渲染空态而不是把"没有模型"显示成"出错了"。
    """
    root = settings.train_root
    if not root.is_dir():
        return {"runs": [], "root": str(root), "exists": False}

    runs: list[dict] = []
    try:
        own = _scan_run(root)
        if own is not None:
            runs.append(own)
        else:
            for entry in sorted(root.iterdir()):
                if not entry.is_dir():
                    continue
                run = _scan_run(entry)
                if run is not None:
                    runs.append(run)
    except OSError:
        return {"runs": [], "root": str(root), "exists": True}

    return {"runs": runs, "root": str(root), "exists": True}
