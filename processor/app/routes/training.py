"""Training API —— 路由层,薄薄一层。

业务逻辑在 ``app/training/`` 下(产物扫描 / GPU 读取 / 数据集发现 / 训练执行),
这里只负责把它们挂到 URL 上。子进程管理、作业状态、日志解析都在
``app/training/runner.py`` —— 本文件不 import subprocess 也不碰磁盘。
"""

from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import FileResponse

from app.training import (
    list_datasets, list_gpus, list_projects_progress, list_runs,
)
from app.training import runner
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


# ── 训练执行 ──────────────────────────────────────────────────────────
#
# 下面五个是本模块**唯一会改变系统状态**的端点(其余都是只读扫描)。真正的
# 校验规则和子进程管理在 ``app/training/runner.py`` —— 这里只做 HTTP 那层:
# 把 ValueError 翻成 400、把 FileNotFoundError 翻成 404。

@router.post("/jobs", status_code=202)
def create_job(payload: dict = Body(...)):
    """创建一个训练任务并**立即起进程**,返回 202 + 作业文档。

    校验不过一律 400,``detail`` 是可以直接显示给用户的中文理由(显卡读不到、
    数据集不在导出产物里、目录已存在、已有任务在跑……)。前端的做法是把
    ``detail`` 原样弹出来,所以那些消息要写得**能指导下一步动作**,而不只是
    描述哪里不对。
    """
    try:
        job = runner.start_job(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return job


@router.get("/jobs")
def list_jobs():
    """训练任务列表(新的在前)。

    与 ``/runs`` 是两回事:``/runs`` 列的是**磁盘上的产物**,这里列的是
    **任务记录**。一个失败的、或者刚起还没写产物的任务只在前者之外出现。
    """
    return {"jobs": runner.list_jobs()}


@router.get("/jobs/{job_id}")
def get_job(job_id: str):
    job = runner.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="没有这个训练任务")
    return job


@router.get("/jobs/{job_id}/log")
def get_job_log(job_id: str, offset: int = 0):
    """增量读训练日志。``offset`` 是字节偏移,前端带上次的 ``offset`` 回来。

    LeRobot **自己不写日志文件**(见 runner 的模块 docstring),所以这份日志
    是 runner 接管子进程 stdout/stderr 存下来的 —— 它是唯一的训练过程记录,
    失败原因全在这里面。
    """
    if runner.get_job(job_id) is None:
        raise HTTPException(status_code=404, detail="没有这个训练任务")
    return runner.read_log(job_id, offset=offset)


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    """取消训练 —— 杀整个进程组(父进程 + dataloader worker),释放显存。"""
    try:
        return runner.cancel_job(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
