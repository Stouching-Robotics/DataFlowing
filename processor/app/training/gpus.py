"""本机 GPU 读取 —— 直接问 ``nvidia-smi``。

这一层**不认识 HTTP**:它返回普通 dict,由 ``app/routes/training.py`` 直接
交给 FastAPI 序列化。所以它可以被将来任何地方复用(比如训练 runner 起子进程
前先确认显卡可用),而不必绕道走一遍路由。
"""

from __future__ import annotations

import shutil
import subprocess

# nvidia-smi 的查询字段 —— 顺序即解析顺序,改这里必须同步改 _parse 的索引。
_QUERY = "index,name,memory.used,memory.total,utilization.gpu,temperature.gpu"


def _parse(stdout: str) -> list[dict]:
    gpus: list[dict] = []
    for line in stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 6:
            continue
        try:
            gpus.append({
                "index": int(parts[0]),
                "name": parts[1],
                "memory_used_mb": int(float(parts[2])),
                "memory_total_mb": int(float(parts[3])),
                "utilization": int(float(parts[4])),
                "temperature": int(float(parts[5])),
            })
        except ValueError:
            continue                  # 单卡读不出来不该让整张列表失败
    return gpus


def list_gpus() -> dict:
    """``{"available": bool, "error": str, "gpus": [...]}``。

    **驱动没起来时如实报错,不要返回一排 0。** 实测这台机器上 ``nvidia-smi``
    会以 "couldn't communicate with the NVIDIA driver" 失败;若把它当成
    "利用率 0%、显存 0" 渲染出去,看起来是"显卡闲着可以训",而真相是"根本
    问不到显卡"。前端据此显示明确的错误条 —— `available` 这个字段存在的唯一
    理由就是让调用方**必须**区分这两种情况。
    """
    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi:
        return {"available": False, "error": "nvidia-smi not found", "gpus": []}

    try:
        proc = subprocess.run(
            [nvidia_smi, f"--query-gpu={_QUERY}", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"available": False, "error": str(exc), "gpus": []}

    if proc.returncode != 0:
        # 驱动没起来走的就是这条 —— nvidia-smi 把原因写在 stderr。
        lines = (proc.stderr or proc.stdout or "").strip().splitlines()
        return {
            "available": False,
            "error": lines[0] if lines else f"nvidia-smi exited {proc.returncode}",
            "gpus": [],
        }

    return {"available": True, "error": "", "gpus": _parse(proc.stdout)}
