"""训练执行 —— 起 ``lerobot-train`` 子进程、跟踪状态、回报进度与日志。

这个模块是 ``app/training/`` 里**唯一会写盘、起进程**的部分;其余几个模块
(gpus / datasets / runs)都只是只读扫描。分工见包 docstring。

## 为什么子进程要 detach

一次训练几小时。API 进程重启(systemd 重启、改代码后 uvicorn --reload)时
**不该把训练杀掉** —— 那等于白烧几小时 GPU。所以:

    子进程 start_new_session=True(自成进程组,脱离 API 的进程组)
    API 重启后靠作业文档里的 ``pid`` 重新挂载监控

代价:重启后我们不再是它的父进程,**拿不到退出码**。所以恢复路径只能靠
``os.kill(pid, 0)`` 判断死活 —— 活着就继续跟,死了就标 ``interrupted``。
这与 ``app/upload_queue.py`` 的 ``_recover_jobs()`` 同构,只是那边恢复的是
"重排队",这边恢复的是"重新挂载"。

## 日志为什么落本地盘

作业文档小、写得少,放 ``state/``(NAS)没问题 —— 跟 upload_queue 一致。
但**训练日志是子进程持续追加的**,20000 步的 tqdm 输出会一直写。走 sshfs
边训边写会持续占用网络文件系统,而训练最怕的就是这种旁路开销。
所以日志走 ``settings.upload_staging_root``(本地 spool,与上传暂存同一处),
作业文档里存绝对路径,前端只走端点读、不关心它在哪块盘。

## 命令行的六个坑

前四个来自 ``lerobot-train --help`` + 真实 train_config.json 的静态比对;
**后两个只有真跑一遍才会暴露** —— 它们是 ``TrainPipelineConfig.validate()`` 在
导入 torch、扫完数据集之后才抛的,光看 ``--help`` 看不出来。``tests/test_training_runner.py``
里给这两个各留了一条回归断言。

1. **``--eval_freq`` 不存在**。``--help`` 里命中 ``eval_freq`` 的全是
   ``env_eval_freq`` 的子串;真实 train_config 的 37 个键里也没有它。
   真正存在的是 ``--eval_steps``(**且要求 ``dataset.eval_split > 0`` 才生效**)。
2. **``--policy.path`` 不存在**。``policy.*`` 下只有 ``--policy.type``。
   续训是 ``--resume=true --config_path=<checkpoint>/pretrained_model/train_config.json``。
   (``app/version.py`` 里"续训需要 --policy.path="那句是旧命令生成器留下的**错误**提示。)
3. **LeRobot 不写训练日志**。checkpoint 目录下只有 ``pretrained_model/`` 与
   ``training_state/``,没有任何 .log/.txt/.jsonl —— 所以必须自己接管 stdout/stderr。
4. **``--output_dir`` 就是 run 目录本身**,不会在它下面再拼 ``job_name``
   (实测:产物直接落在 ``output_dir/checkpoints/<step>/``)。这正好对上
   ``runs.py`` 的 ``<root>/<run>/checkpoints/<step>/`` 布局。
5. **``--dataset.repo_id`` 是必填的**,哪怕数据集就在本地、根本没有 Hub 这回事。
   不给会报 ``Missing required field(s) `dataset` for TrainPipelineConfig``
   —— 注意报的是 ``dataset`` 整个字段缺失,不是"repo_id 缺失",照着字面查会
   查错方向。这里填 ``local/<数据集名>``,与实测那次 ``local/umi`` 同格式。
6. **必须显式 ``--policy.push_to_hub=false``**。默认是 true,而一旦为 true,
   ``validate()`` 就要求顶层 ``repo_id``(要从 Hub 推模型)—— 报错是
   ``'repo_id' argument missing. Please specify it to push the model to the hub.``
   而**真实那次的 train_config 里压根没有顶层 repo_id 键**,只有
   ``policy.push_to_hub: false``。所以正确解法是关掉 push,不是补 repo_id。

   另注:``eval_steps > 0`` 时 ``validate()`` **强制要求** ``dataset.eval_split > 0``
   ("eval_steps > 0 requires dataset.eval_split > 0.0 to hold out eval data"),
   所以 ``build_command`` 里这两个必须成对给 —— 这也是坑 1 的连带后果。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.config import settings
from app.training.datasets import DATASETS_DIR, list_datasets
from app.training.gpus import list_gpus
from app.training.runs import list_runs

# 作业文档走权威 state 目录(小、写得少)。日志走本地 spool —— 见模块 docstring。
_JOBS_ROOT = settings.storage_root / "state" / "train_jobs"
_LOGS_ROOT = settings.upload_staging_root / "train_logs"

_lock = threading.RLock()
_procs: dict[str, subprocess.Popen] = {}      # 只装**本进程起的**子进程
_supervisor_thread: threading.Thread | None = None
_stop_event = threading.Event()

TERMINAL = {"completed", "failed", "cancelled", "interrupted"}
SUPERVISE_INTERVAL = 2.0

# 开启 eval 时要留出的验证集比例。``--eval_steps`` 单给不生效 —— ``--help``:
# "Requires eval_split > 0"。0.1 是 LeRobot 的常见取值。
EVAL_SPLIT = 0.1

# 起训练前要求的最小空闲显存(MB)。ACT batch=16 实测占用远低于此;
# 留这个余量是为了避开"卡上还挂着 vLLM / 旧 worker"的情况 —— 历史上
# 000008 工作流就是因为 CUDA OOM 失败的。
MIN_FREE_VRAM_MB = 16000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _job_path(job_id: str) -> Path:
    return _JOBS_ROOT / f"{job_id}.json"


def _log_path(job_id: str) -> Path:
    return _LOGS_ROOT / f"{job_id}.log"


def _write_job(job: dict) -> None:
    """原子写(临时文件 + replace)—— 与 upload_queue._write_job 同一手法。

    API 可能在任意时刻被杀,半截 JSON 会让作业**读不出来**;先写临时文件
    再 rename 就没有"写了一半"这个状态。
    """
    _JOBS_ROOT.mkdir(parents=True, exist_ok=True)
    path = _job_path(str(job["id"]))
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(job, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def _read_job(job_id: str) -> dict | None:
    try:
        value = json.loads(_job_path(job_id).read_text(encoding="utf-8"))
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _update(job_id: str, **fields) -> dict | None:
    with _lock:
        job = _read_job(job_id)
        if job is None:
            return None
        job.update(fields)
        _write_job(job)
        return job


# ── 校验 ──────────────────────────────────────────────────────────────

_SAFE_NAME = re.compile(r"[^0-9A-Za-z._-]+")


def safe_job_name(raw: str) -> str:
    """把用户输入收成一个安全的目录名。

    与 ``app/routes/export.py:_safe_download_stem`` 同一思路:job_name 会变成
    ``train_root/<job_name>`` 的目录名,同时也是 LeRobot 的 ``--job_name``。
    这里**只允许** ASCII 字母数字与 ``._-``:训练产物最终要跨机器、要进部署包,
    中文/空格目录名会在那些环节制造麻烦(而它本来就只是个标识,不是给人看的标题)。
    """
    cleaned = _SAFE_NAME.sub("_", str(raw or "").strip()).strip("._-")
    return cleaned[:64]


def _dataset_by_name(name: str) -> dict | None:
    """按名字在**平台导出产物**里查数据集。

    ★ 刻意不接受前端传路径。数据集只有这一个合法来源 —— 这是"控制到底训了
    哪些集"的唯一关卡(导出那一步才决定勾了哪些集,留出集的保护也在那里)。
    让调用方直接给路径,等于把这道闸门绕开 —— 实测那次训练就是直接用
    ``文件测试/`` 里的目录跑的,完全跳过了审核页导出。
    """
    for ds in list_datasets().get("datasets") or []:
        if ds.get("name") == name:
            return ds
    return None


def validate_spec(spec: dict) -> dict:
    """校验并归一化一份训练请求。**不通过就抛 ValueError**,消息面向用户。

    返回的是归一化后的 spec(数字已转成 int/bool、路径已解析成绝对路径)——
    后续 ``build_command`` 只认这个形状,不再做类型转换。
    """
    if not isinstance(spec, dict):
        raise ValueError("请求格式不对")

    # ── 显卡:驱动问不到就必须拒绝 ──────────────────────────────
    # gpus.py 的注释说得很清楚:``available: false`` 与"利用率 0%"是两回事,
    # 后者的调用方会以为显卡闲着可以训。这里就是那个"必须区分"的调用方。
    gpu_info = list_gpus()
    if not gpu_info.get("available"):
        raise ValueError(
            f"读不到本机 GPU,无法训练:{gpu_info.get('error') or 'nvidia-smi 不可用'}")

    gpus = spec.get("gpus") or []
    if not isinstance(gpus, (list, tuple)) or not gpus:
        raise ValueError("请至少勾选一块参与训练的 GPU")
    try:
        gpu_ids = sorted({int(g) for g in gpus})
    except (TypeError, ValueError):
        raise ValueError("GPU 编号必须是整数")
    known = {int(g["index"]) for g in gpu_info.get("gpus") or []}
    unknown = [g for g in gpu_ids if g not in known]
    if unknown:
        raise ValueError(f"本机没有这些 GPU:{unknown}")

    free = min(int(g.get("memory_total_mb", 0)) - int(g.get("memory_used_mb", 0))
               for g in gpu_info.get("gpus") or [] if int(g["index"]) in gpu_ids)
    if free < MIN_FREE_VRAM_MB:
        raise ValueError(
            f"所选 GPU 空闲显存只剩 {free}MB,低于 {MIN_FREE_VRAM_MB}MB 的安全线。"
            f"卡上可能还挂着 vLLM 或旧的 worker —— 先腾出显存再开训练。")

    # ── 数据集:只能来自平台导出产物 ────────────────────────────
    wanted = spec.get("dataset")
    if isinstance(wanted, dict):          # 前端 state.dataset 是整个对象,容忍
        wanted = wanted.get("name")
    name = str(wanted or "").strip()
    if not name:
        raise ValueError("请先选择训练数据集")
    ds = _dataset_by_name(name)
    if ds is None:
        raise ValueError(
            f"数据集「{name}」不在平台导出产物里({DATASETS_DIR})。"
            f"请先在视频审核页导出 —— 只有导出过的数据集才能训练。")

    # ── job_name ────────────────────────────────────────────────
    job_name = safe_job_name(spec.get("job_name") or "act")
    if not job_name:
        raise ValueError("训练名称不能为空(只能用字母/数字/._-)")

    out_dir = settings.train_root / job_name
    # --help 原话:同样的 dir 再跑一次会**覆盖**上次的内容(除非 resume)。
    # 覆盖一次就是几小时白跑,所以宁可拦下来让用户改名。
    if out_dir.exists() and not (spec.get("resume") or {}).get("run"):
        raise ValueError(
            f"训练目录已存在:{out_dir}。换个「训练名称」,或勾选「从已有训练继续」。")

    # ── 参数 ────────────────────────────────────────────────────
    raw = spec.get("params") or {}
    params: dict[str, int | bool] = {}
    for key, default in (
        ("steps", 20000), ("batch_size", 16), ("seed", 1000),
        ("num_workers", 4), ("log_freq", 100), ("save_freq", 5000),
        ("eval_steps", 0),
    ):
        try:
            value = int(raw.get(key, default))
        except (TypeError, ValueError):
            raise ValueError(f"训练参数 {key} 必须是整数")
        if value < 0:
            raise ValueError(f"训练参数 {key} 不能是负数")
        params[key] = value
    if params["steps"] <= 0:
        raise ValueError("训练步数必须大于 0")
    if params["batch_size"] <= 0:
        raise ValueError("batch_size 必须大于 0")
    if params["save_freq"] <= 0:
        raise ValueError("保存间隔必须大于 0")

    value = raw.get("save_checkpoint", True)
    if isinstance(value, str):               # 表单的 <select> 给的是字符串
        params["save_checkpoint"] = value.strip().lower() in {"1", "true", "yes", "on"}
    else:
        params["save_checkpoint"] = bool(value)

    # ── 续训 ────────────────────────────────────────────────────
    resume = None
    raw_resume = spec.get("resume") or None
    if raw_resume:
        run_name = str(raw_resume.get("run") or "").strip()
        if not run_name:
            raise ValueError("续训必须指定一个已有的训练")
        step = raw_resume.get("step")
        # step 为 None = 用最新的那个。list_runs() 已按 step **降序**排好。
        found = next((r for r in list_runs().get("runs") or []
                      if r.get("name") == run_name), None)
        if found is None:
            raise ValueError(f"找不到训练「{run_name}」")
        checkpoints = found.get("checkpoints") or []
        if not checkpoints:
            raise ValueError(f"训练「{run_name}」下没有 checkpoint,无从续训")
        chosen = None
        if step is not None:
            try:
                wanted_step = int(step)
            except (TypeError, ValueError):
                raise ValueError("续训的步数必须是整数")
            chosen = next((c for c in checkpoints if int(c["step"]) == wanted_step), None)
            if chosen is None:
                raise ValueError(f"训练「{run_name}」下没有 {step} 步的 checkpoint")
        else:
            chosen = checkpoints[0]
        config_path = (Path(found["path"]) / "checkpoints" / chosen["name"]
                       / "pretrained_model" / "train_config.json")
        if not config_path.is_file():
            raise ValueError(
                f"续训配置不存在:{config_path}(那个 checkpoint 可能不完整)")
        resume = {"run": run_name, "step": int(chosen["step"]),
                  "config_path": str(config_path)}

    return {
        "job_name": job_name,
        "dataset": {"name": ds["name"], "path": str(ds["path"]),
                    "total_episodes": int(ds.get("total_episodes") or 0)},
        "gpus": gpu_ids,
        "params": params,
        "resume": resume,
        "output_dir": str(out_dir),
    }


# ── 命令行构造(纯函数,可单测)───────────────────────────────────────

def build_command(spec: dict) -> list[str]:
    """把归一化后的 spec 拼成 argv。

    **纯函数**:不碰磁盘、不读配置,所以可以拿真实规模的数据集做单测。
    参数名全部对着 ``lerobot-train --help`` 实测过(见模块 docstring 的四个坑)。

    ``spec`` 必须是 ``validate_spec`` 的返回值 —— 这里不做类型转换。
    """
    p = spec["params"]
    cmd = [
        settings.train_cli,
        "--policy.type=act",
        # ★ 这两个不是可选的,少一个 TrainPipelineConfig.validate() 直接抛异常
        #   (2026-09-28 用真命令跑出来的,单测抓不到 —— 见模块 docstring 的坑 5、6):
        #   缺 dataset.repo_id → "Missing required field(s) `dataset`"
        #   不关 push_to_hub  → "'repo_id' argument missing. Please specify it to
        #                       push the model to the hub."
        "--policy.push_to_hub=false",
        f"--dataset.repo_id=local/{spec['dataset']['name']}",
        f"--dataset.root={spec['dataset']['path']}",
        f"--output_dir={spec['output_dir']}",
        f"--job_name={spec['job_name']}",
        f"--steps={p['steps']}",
        f"--batch_size={p['batch_size']}",
        f"--seed={p['seed']}",
        f"--num_workers={p['num_workers']}",
        f"--log_freq={p['log_freq']}",
        f"--save_checkpoint={'true' if p['save_checkpoint'] else 'false'}",
        f"--save_freq={p['save_freq']}",
    ]
    # eval:只在真的开了才给参数。``--eval_steps`` 单给不生效(--help:
    # "Requires eval_split > 0"),所以两个一起给;0 = 关,与实测那次一致。
    if p.get("eval_steps"):
        cmd.append(f"--eval_steps={p['eval_steps']}")
        cmd.append(f"--dataset.eval_split={EVAL_SPLIT}")
    if spec.get("resume"):
        # 续训不是 --policy.path(policy.* 下只有 --policy.type)。
        cmd.append("--resume=true")
        cmd.append(f"--config_path={spec['resume']['config_path']}")
    return cmd


# ── 起进程 ────────────────────────────────────────────────────────────

def _spawn(job: dict, spec: dict) -> subprocess.Popen:
    """起子进程。

    ``start_new_session=True`` 让它自成进程组 —— 这是我们能**整组**杀掉
    dataloader worker 的前提(见 ``cancel_job``),也是 API 重启时训练能活下来的
    原因。stdout/stderr 都进同一个日志文件:LeRobot 不写自己的日志,而 tqdm
    的进度条走 stderr、报错也走 stderr。
    """
    _LOGS_ROOT.mkdir(parents=True, exist_ok=True)
    log = open(_log_path(str(job["id"])), "ab", buffering=0)

    env = dict(os.environ)
    env["CUDA_VISIBLE_DEVICES"] = ",".join(str(g) for g in spec["gpus"])
    # 训练过程要能 import 到 lerobot 自己的依赖,保留原有 PATH/PYTHONPATH。
    env.setdefault("PYTHONUNBUFFERED", "1")

    header = (
        f"# {_now()} 训练启动\n"
        f"# CUDA_VISIBLE_DEVICES={env['CUDA_VISIBLE_DEVICES']}\n"
        f"# {subprocess.list2cmdline(build_command(spec))}\n"
    ).encode("utf-8")
    log.write(header)

    return subprocess.Popen(
        build_command(spec),
        stdout=log, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        env=env,
        cwd=str(settings.train_root if settings.train_root.is_dir() else Path.cwd()),
        start_new_session=True,
        bufsize=0,
    )


def start_job(spec: dict) -> dict:
    """校验 → 落盘 → 起进程。返回作业文档。校验失败抛 ValueError。"""
    normalized = validate_spec(spec)

    # 单飞:24G 卡一次只跑得动一个训练。
    active = [j for j in list_jobs(limit=50)
              if j.get("status") in ("queued", "running")]
    if active:
        raise ValueError(
            f"已有训练在进行中({active[0]['job_name']},"
            f"状态 {active[0]['status']})。等它结束或先取消。")

    job = {
        "id": uuid4().hex,
        "job_name": normalized["job_name"],
        "status": "queued",
        "progress": None,          # None = 还不知道,**不要编一个 0**
        "dataset": normalized["dataset"],
        "gpus": normalized["gpus"],
        "params": normalized["params"],
        "resume": normalized["resume"],
        "output_dir": normalized["output_dir"],
        "run_name": normalized["job_name"],
        "cmd": build_command(normalized),
        "pid": None,
        "log_path": str(_log_path("")),   # 下面补上真实 id
        "created_at": _now(),
        "started_at": None,
        "finished_at": None,
        "error": None,
    }
    job["log_path"] = str(_log_path(job["id"]))
    with _lock:
        _write_job(job)

    try:
        proc = _spawn(job, normalized)
    except Exception as exc:
        _update(job["id"], status="failed", finished_at=_now(),
                error=f"起训练进程失败:{exc}")
        raise ValueError(f"起训练进程失败:{exc}") from exc

    with _lock:
        _procs[job["id"]] = proc
    job = _update(job["id"], status="running", started_at=_now(), pid=proc.pid)
    return job or {}


# ── 监控 ──────────────────────────────────────────────────────────────

# tqdm 往 stderr 打形如 "Training: 12%|█▏ | 2400/20000 [03:21<24:00, 3.2it/s]"。
# 只在**分母等于本次 steps** 时才认 —— 否则会把别的 x/y(比如保存进度)当训练进度。
_PROGRESS = re.compile(r"(\d+)\s*/\s*(\d+)")


def _parse_progress(text: str, steps: int) -> float | None:
    """从日志尾部推一个 0..1 的进度。**推不出来就返回 None。**

    照着 ``gpus.py`` 的态度:宁可如实报"不知道",也不要编一个 0% —— 一个
    永远停在 0% 的进度条比没有进度条更让人以为坏了。
    """
    best: float | None = None
    for line in text.splitlines():
        if "it/s" not in line and "s/it" not in line and "%|" not in line:
            continue
        for done, total in _PROGRESS.findall(line):
            if int(total) == steps and steps > 0:
                best = min(1.0, max(0.0, int(done) / steps))
    return best


def _tail(path: Path, limit: int = 65536) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > limit:
                fh.seek(size - limit)
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _alive(pid: int | None) -> bool:
    """进程还在吗。``os.kill(pid, 0)`` 只探活、不真发信号。

    API 重启后我们不再是父进程,拿不到退出码 —— 这是恢复路径唯一能问的问题。
    注意 pid 复用:理论上可能撞上一个无关的新进程,概率极低,而且我们只用它
    判断"要不要放弃监控",判错的后果是作业卡在 running 直到超时。
    """
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # 存在但不属于我们 —— 仍然算活着
    return True


def _finish(job_id: str, status: str, error: str | None = None) -> None:
    job = _read_job(job_id)
    if job is None:
        return
    fields = {"status": status, "finished_at": _now(), "error": error}
    if status == "completed":
        fields["progress"] = 1.0
        try:
            prune_checkpoints(Path(job["output_dir"]))
        except Exception:
            # 清理失败不该把一次成功的训练标成失败 —— 产物还在,只是没瘦身。
            pass
    _update(job_id, **fields)
    with _lock:
        _procs.pop(job_id, None)


def _supervise() -> None:
    while not _stop_event.is_set():
        try:
            for job in list_jobs(limit=50):
                if job.get("status") != "running":
                    continue
                job_id = str(job["id"])
                proc = _procs.get(job_id)
                if proc is not None:
                    code = proc.poll()
                    if code is None:
                        text = _tail(_log_path(job_id))
                        progress = _parse_progress(text, int(job["params"]["steps"]))
                        if progress is not None and progress != job.get("progress"):
                            _update(job_id, progress=progress)
                    else:
                        # 本进程起的:拿得到真实退出码。
                        if code == 0:
                            _finish(job_id, "completed")
                        elif code < 0:
                            # 被信号杀掉 —— 取消走的是这条路(signal 15/9)。
                            _finish(job_id, "cancelled")
                        else:
                            _finish(job_id, "failed",
                                    f"训练进程退出码 {code}(详见日志)")
                elif not _alive(job.get("pid")):
                    # 恢复路径:进程没了,而我们拿不到退出码。
                    # **标 interrupted 而不是 failed** —— 我们不能断言它失败了,
                    # 只能说"跟踪断了";产物在不在,「模型训练」页自己会列出来。
                    _finish(job_id, "interrupted",
                            "训练进程在服务重启前后结束,未能记录退出码;"
                            "请到「模型训练」页确认产物是否完整。")
            time.sleep(SUPERVISE_INTERVAL)
        except Exception:
            # 监控线程绝不能死:它一死,所有 running 作业就永远悬着了。
            time.sleep(SUPERVISE_INTERVAL)


def start() -> None:
    """在 lifespan 里调 —— 与 ``upload_queue.start()`` 同一起点。"""
    global _supervisor_thread
    with _lock:
        if _supervisor_thread is not None and _supervisor_thread.is_alive():
            return
        _stop_event.clear()
        _JOBS_ROOT.mkdir(parents=True, exist_ok=True)
        _supervisor_thread = threading.Thread(
            target=_supervise, name="egodata-train-supervisor", daemon=True)
        _supervisor_thread.start()


def stop() -> None:
    """**只停监控线程,不动训练子进程。**

    这是刻意的:训练几小时,而 API 会重启。杀掉它等于白烧 GPU,而用户并
    没有要求取消 —— 要取消请走 ``cancel_job``。恢复靠 ``pid``。
    """
    global _supervisor_thread
    _stop_event.set()
    thread = _supervisor_thread
    if thread is not None and thread.is_alive():
        thread.join(timeout=5.0)
    _supervisor_thread = None


# ── 查询 / 取消 ───────────────────────────────────────────────────────

def list_jobs(limit: int = 50) -> list[dict]:
    if not _JOBS_ROOT.is_dir():
        return []
    jobs: list[dict] = []
    for path in _JOBS_ROOT.glob("*.json"):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(job, dict):
            jobs.append(job)
    jobs.sort(key=lambda j: str(j.get("created_at") or ""), reverse=True)
    return jobs[:limit]


def get_job(job_id: str) -> dict | None:
    return _read_job(job_id)


def read_log(job_id: str, offset: int = 0, limit: int = 200_000) -> dict:
    """增量读日志。

    ``offset`` 是字节偏移,前端每次带上次拿到的 ``offset`` 回来 —— 只取新增
    部分。一次最多 ``limit`` 字节,防止 20000 步的日志把浏览器噎住。
    """
    path = _log_path(job_id)
    try:
        size = path.stat().st_size
    except OSError:
        return {"text": "", "offset": 0, "size": 0, "truncated": False}
    start = max(0, min(int(offset), size))
    truncated = size - start > limit
    with path.open("rb") as fh:
        fh.seek(start)
        chunk = fh.read(limit)
    return {
        "text": chunk.decode("utf-8", errors="replace"),
        "offset": start + len(chunk),
        "size": size,
        "truncated": truncated,
    }


def cancel_job(job_id: str) -> dict:
    """取消训练 —— 杀**整个进程组**。

    只杀父进程是不够的:dataloader 的 worker 是它 fork 出来的子进程,父进程
    一死它们会被 init 收养继续跑,**显存不释放**。``_spawn`` 用
    ``start_new_session=True`` 正是为了这里能 ``killpg`` 一锅端。
    """
    job = _read_job(job_id)
    if job is None:
        raise ValueError("没有这个训练任务")
    if job.get("status") in TERMINAL:
        raise ValueError(f"任务已经 {job['status']},不用取消")

    pid = job.get("pid")
    if pid:
        try:
            os.killpg(os.getpgid(int(pid)), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        # 给 5 秒优雅退出;不退就 SIGKILL。ffmpeg 那套"terminate→wait→kill"
        # 的顺序在 app/lerobot_v21.py 里已经踩过一遍(等 5 秒超时才 kill),
        # 这里是同一个教训的直接应用。
        deadline = time.time() + 5.0
        while time.time() < deadline and _alive(int(pid)):
            time.sleep(0.2)
        if _alive(int(pid)):
            try:
                os.killpg(os.getpgid(int(pid)), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass

    with _lock:
        _procs.pop(job_id, None)
    return _update(job_id, status="cancelled", finished_at=_now(),
                   error="用户取消了训练") or job


# ── 保留策略 ──────────────────────────────────────────────────────────

def prune_checkpoints(run_dir: Path, keep: int | None = None) -> list[str]:
    """只留最新的 N 个 checkpoint,返回被删掉的目录名。

    默认 ``keep`` 取 ``settings.TRAIN_KEEP_CHECKPOINTS``(1 = 只留最后一个,
    0 = 全留)。**只在训练成功后调用** —— 失败的 run 要留着现场给人看。

    一个 checkpoint 约 590MB(其中 training_state/ 是优化器状态,占大头只对
    续训有用),所以这一步省的通常是一半以上磁盘。

    删中间态不影响已训完的模型:``pretrained_model/`` 在每个 checkpoint 里
    都是完整的,最后一个就够推理和部署。
    """
    keep = int(settings.TRAIN_KEEP_CHECKPOINTS if keep is None else keep)
    ckpt_dir = Path(run_dir) / "checkpoints"
    if not ckpt_dir.is_dir():
        return []
    steps: list[tuple[int, Path]] = []
    for entry in ckpt_dir.iterdir():
        # 只认纯数字目录:"last" 是个符号链接,删它会连带出问题(它指向被保留
        # 的那个,顺序不对就会指空)。_step_of 的口径与这里保持一致。
        if entry.is_dir() and not entry.is_symlink() and entry.name.isdigit():
            steps.append((int(entry.name), entry))
    steps.sort(key=lambda item: item[0])

    if keep <= 0:
        return []
    removed: list[str] = []
    for _, path in steps[:-keep]:
        shutil.rmtree(path, ignore_errors=True)
        removed.append(path.name)

    # "last" 符号链接要指向还在的那个 —— 否则它指着刚被删掉的目录。
    if removed and steps:
        last = ckpt_dir / "last"
        target = steps[-1][1].name
        try:
            if last.is_symlink() or last.exists():
                last.unlink()
            last.symlink_to(target)
        except OSError:
            pass
    return removed
