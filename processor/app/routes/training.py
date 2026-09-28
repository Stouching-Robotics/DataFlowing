"""Training API —— 路由层,薄薄一层。

业务逻辑在 ``app/training/`` 下(产物扫描 / GPU 读取 / 数据集发现),这里只负责
把它们挂到 URL 上。**没有任何训练执行** —— 见那个包的 docstring。

将来接执行时,新端点加在这里(创建 / 停止 / 日志),形状与现有的保持一致;
真正的子进程管理写在 ``app/training/runner.py``。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.training import (
    list_datasets, list_gpus, list_projects_progress, list_runs,
)
from app.training.runs import resolve_checkpoint_file

router = APIRouter(prefix="/api/v1/training", tags=["training"])


@router.get("/gpus")
def gpus():
    """本机 GPU(读 ``nvidia-smi``)。

    **驱动不可用时靠 ``available: false`` 区分**,不要把它当成"利用率 0%" ——
    详见 ``app/training/gpus.py``。
    """
    return list_gpus()


@router.get("/datasets")
def datasets():
    """可训练的数据集 —— 审核页导出后落在 ``state/datasets/`` 的成品。

    空列表是**正常**的(还没导出过),前端据此渲染引导而不是报错。
    """
    return list_datasets()


@router.get("/runs")
def runs():
    """训练产物(checkpoints)。目录不存在时返回空列表,不是 404。"""
    return list_runs()


@router.get("/ready")
def ready():
    """各项目离「可以导出」还有多远 —— 训练页「到点提醒」用。

    判据:**已通过集数达到项目设置的采集上限**(``params.target_episodes``)。
    到点之后只**提醒**,不自动导出 —— 理由见 ``app/training/projects.py``。
    """
    return list_projects_progress()


@router.get("/runs/{run_name}/checkpoints/{step}/files/{name:path}")
def download_checkpoint_file(run_name: str, step: str, name: str):
    """下载 checkpoint 里的单个文件。

    ``{name:path}`` 而不是 ``{name}``:要下的文件在子目录里
    (``pretrained_model/model.safetensors``),默认的路径参数不匹配斜杠。

    **安全性由 ``resolve_checkpoint_file`` 保证** —— 它只在扫描出的真实文件清单里
    按名精确匹配,不做路径拼接,所以构造不出清单外的路径(比如 ``../../etc/passwd``)。
    详见那个函数的 docstring。

    单个文件而不是打包 zip:一个 checkpoint 约 600MB,其中 ``training_state/``
    (优化器状态,约 400MB)**只在续训时有用**,拿去推理/部署只需要
    ``pretrained_model/``(约 200MB)。让用户按需下载,比强制他拉一个 600MB 的包好。
    """
    path = resolve_checkpoint_file(run_name, step, name)
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="No such checkpoint file")
    # filename 只取最后一段 —— 让浏览器存成 model.safetensors 而不是把整条相对路径
    # 当成文件名(带斜杠的文件名在多数系统上写不出来)。
    return FileResponse(path, filename=path.name)
