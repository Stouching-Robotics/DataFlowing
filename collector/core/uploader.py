"""
上传队列管理器 —— 自动打包 session 为 zip → 上传到服务器。

上传流程:
  1. 用 zipfile 将 session 目录打包（保留 LeRobot v3 内部结构）
  2. 通过 APIClient 将 zip 上传到 POST /api/v1/session/upload
  3. 清理临时 zip 文件
  4. 通过 Qt 信号通知 UI 进度
"""

from __future__ import annotations
import os
import re
import subprocess
import time
import zipfile
import threading
import tempfile
from datetime import datetime, timedelta
from typing import Optional

from PyQt5.QtCore import QObject, pyqtSignal, QMutex, QMutexLocker

from config import settings
from core.database import db
from core.api_client import APIClient
from core.helpers import episode_file_suffix


# ═══════════════════════════════════════════════════════
#  会话名校验（上传后确认服务器是否已入库）
# ═══════════════════════════════════════════════════════

# 尾部 episode 标记：_ep000012 / _episode-12 / _000013
_EP_SUFFIX_RE = re.compile(
    r"^(?P<base>.+?)(?:[_-]ep(?:isode)?[_-]?\d{1,6}|[_-]\d{6,})$", re.I)


def session_name_base(name: str) -> str:
    """会话名去掉尾部 episode 标记与 .zip 后缀 → 任务基名。

    "UMIGripper_AI" / "UMIGripper_AI_000012" / "UMIGripper_AI_ep000012"
    / "UMIGripper_AI_episode-12" / "UMIGripper_AI.zip" → "UMIGripper_AI"
    """
    n = (name or "").strip()
    if n.lower().endswith(".zip"):
        n = n[:-4]
    m = _EP_SUFFIX_RE.match(n)
    return (m.group("base") if m else n).strip()


def session_matches(session_name: str, server_session: dict) -> bool:
    """服务器会话记录是否就是本次上传产生的会话（名字相等或基名相等）。"""
    srv = str((server_session or {}).get("name", "") or "").strip()
    if not srv or not session_name:
        return False
    if srv == session_name.strip():
        return True
    base = session_name_base(session_name)
    return bool(base) and session_name_base(srv) == base


def _parse_iso(value) -> Optional[datetime]:
    """宽松解析时间戳为 naive 本地时间；失败返回 None。"""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def pick_new_session(before_ids: set, sessions: list,
                     session_name: str) -> Optional[dict]:
    """在快照 before_ids 之后新出现的同名会话里取最新一条（无则 None）。"""
    best = None
    for s in sessions or []:
        if str(s.get("id", "")) in before_ids:
            continue
        if not session_matches(session_name, s):
            continue
        key = str(s.get("created_at", "") or "")
        if best is None or key >= best[0]:
            best = (key, s)
    return best[1] if best else None


def pick_session_in_window(sessions: list, session_name: str, since_iso: str,
                           until_iso: str = "", tol_s: float = 60.0
                           ) -> Optional[dict]:
    """名字匹配且 created_at ∈ [since_iso - tol_s, until_iso] 的最新一条。

    续传判据：上次进程被杀时 POST 可能已入库而客户端没收到响应。窗口下界
    是那次 POST 的开始时刻（tol_s 吸收客户端/服务器时钟差），上界是下一个
    POST 的开始时刻——否则"同任务上一条已入库的会话"会被误判成本条的。
    until_iso 为空 = 不限上界。时间戳缺失/不可解析 → None（照常重传）。
    """
    since = _parse_iso(since_iso)
    if since is None:
        return None
    until = _parse_iso(until_iso) if until_iso else None
    best = None
    for s in sessions or []:
        if not session_matches(session_name, s):
            continue
        dt = _parse_iso(s.get("created_at"))
        if dt is None or (since - dt).total_seconds() > tol_s:
            continue
        if until is not None and dt > until:
            continue
        if best is None or dt >= best[0]:
            best = (dt, s)
    return best[1] if best else None


# ═══════════════════════════════════════════════════════
#  上传任务
# ═══════════════════════════════════════════════════════

class UploadTask:
    """单个上传任务的数据对象。"""

    __slots__ = ("id", "session_path", "session_name", "episode_index",
                 "status", "progress", "retry_count", "server_url",
                 "server_session_id", "error_message", "created_at", "updated_at",
                 "resumed")

    def __init__(self, session_path: str, server_url: str, episode_index: int = 0,
                 task_id: str = "", resumed: bool = False, created_at: str = ""):
        import uuid
        self.id = task_id or uuid.uuid4().hex[:12]
        self.session_path = session_path
        # v1.1.0 池化：session_path = 任务目录，session_name 即任务名
        self.session_name = os.path.basename(session_path)
        self.episode_index = episode_index
        self.status = "pending"
        self.progress = 0.0
        self.retry_count = 0
        self.server_url = server_url
        self.server_session_id = ""
        self.error_message = ""
        now = datetime.now().isoformat()
        # 续传复用原行的 created_at —— 它是"上次尝试是否已入库"的时间基准
        self.created_at = created_at or now
        self.updated_at = now
        # True = 由启动续传从 DB 恢复（上传前多一道"服务器是否已入库"检查）
        self.resumed = bool(resumed)

    def to_dict(self) -> dict:
        return {k: getattr(self, k, "") for k in self.__slots__}

    @staticmethod
    def from_row(row: dict) -> "UploadTask":
        t = UploadTask.__new__(UploadTask)
        for k in UploadTask.__slots__:
            setattr(t, k, row.get(k, ""))
        t.resumed = bool(row.get("resumed", False))
        return t


# ═══════════════════════════════════════════════════════
#  上传管理器
# ═══════════════════════════════════════════════════════

class UploadManager(QObject):
    """
    上传队列管理器 — 自动打包 + 上传。

    信号:
      task_added(str)                — 任务入队
      task_started(str)              — 任务开始
      task_status(str, str)          — 状态文字更新 (task_id, msg)
      task_progress(str, float)      — 进度 (task_id, 0.0~1.0)
      task_completed(str)            — 任务完成
      task_failed(str, str)          — 任务失败 (task_id, error)
      all_completed()                — 全部任务完成
    """

    task_added = pyqtSignal(str)
    task_started = pyqtSignal(str)
    task_status = pyqtSignal(str, str)
    task_progress = pyqtSignal(str, float)
    task_completed = pyqtSignal(str)
    task_failed = pyqtSignal(str, str)
    all_completed = pyqtSignal()

    def __init__(self, server_url: str = "", session=None):
        super().__init__()
        self._server_url = server_url or settings.SERVER_URL
        self._retry_max = settings.UPLOAD_RETRY_MAX
        self._max_concurrent = settings.UPLOAD_MAX_CONCURRENT
        self._shared_session = session  # 复用已认证的 requests.Session
        self._project_id = settings.load_upload_project_id()  # 目标项目 ID（空=服务器自动匹配）

        self._queue: list[UploadTask] = []
        self._active: dict[str, threading.Thread] = {}
        self._active_tasks: dict[str, UploadTask] = {}  # task_id → 任务对象（防重查询用）
        self._started_at: dict[str, float] = {}         # task_id → 开始时刻（monotonic）
        self._mutex = QMutex()
        self._running = False
        self._worker_thread: Optional[threading.Thread] = None

    # ── 公开接口 ──────────────────────────────────────

    @property
    def server_url(self) -> str:
        return self._server_url

    @server_url.setter
    def server_url(self, value: str):
        self._server_url = value

    @property
    def project_id(self) -> str:
        """上传时携带的目标项目 ID（空 = 服务器按名称自动匹配）。"""
        return self._project_id

    @project_id.setter
    def project_id(self, value: str):
        self._project_id = (value or "").strip()

    def add_task(self, session_path: str, episode_index: int = 0,
                 task_id: str = "", resumed: bool = False,
                 created_at: str = "") -> str:
        """添加一个上传任务，返回 task_id。

        v1.1.0 池化：防重键 (task_dir, episode_index)——同一 episode 已在队列/
        执行中时返回既有任务 id，不重复入队（自动上传与手动上传互防）。

        task_id/resumed：启动续传时复用 DB 里那行的 id 与创建时间，
        临时文件名随之复用（顺带覆盖上次被杀进程的残留临时文件）。
        """
        with QMutexLocker(self._mutex):
            for t in self._queue + list(self._active_tasks.values()):
                if (t.session_path == session_path
                        and t.episode_index == episode_index):
                    return t.id
        task = UploadTask(session_path, self._server_url, episode_index,
                          task_id=task_id, resumed=resumed, created_at=created_at)
        self._save_to_db(task)
        with QMutexLocker(self._mutex):
            self._queue.append(task)
        self.task_added.emit(task.id)
        return task.id

    def add_tasks(self, items: list) -> list[str]:
        """批量入队。items 元素为路径字符串或 (path, episode_index) 元组。"""
        out = []
        for it in items:
            if isinstance(it, (tuple, list)):
                p, n = it[0], it[1]
            else:
                p, n = it, 0
            if os.path.isdir(p):
                out.append(self.add_task(p, n))
        return out

    def has_task(self, session_path: str, episode_index: int = 0) -> bool:
        """(task_dir, episode_index) 是否已在队列/执行中。

        上传对话框复用主窗口共享管理器后，跨对话框会话防重靠此判断：
        上次提交仍在后台进行时，重新打开对话框不会再重复入队。
        """
        with QMutexLocker(self._mutex):
            for t in self._queue + list(self._active_tasks.values()):
                if (t.session_path == session_path
                        and t.episode_index == episode_index):
                    return True
        return False

    def start(self):
        if self._running:
            return
        self._running = True
        self._worker_thread = threading.Thread(
            target=self._worker_loop, daemon=True, name="upload-worker"
        )
        self._worker_thread.start()

    def stop(self):
        self._running = False
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=10)

    def pending_count(self) -> int:
        with QMutexLocker(self._mutex):
            return len(self._queue)

    def active_count(self) -> int:
        with QMutexLocker(self._mutex):
            return len(self._active)

    def all_done(self) -> bool:
        return self.pending_count() == 0 and self.active_count() == 0

    def inflight(self) -> dict:
        """{(session_path, episode_index): {task_id, state, progress, elapsed}}。

        state = "active"（正在打包/上传）| "queued"（排队中）。供上传
        对话框显示 ⏳ 用——DB 状态只在任务结束时才写，实时状态只能问
        管理器。线程安全。
        """
        now = time.monotonic()
        out = {}
        with QMutexLocker(self._mutex):
            for t in self._queue + list(self._active_tasks.values()):
                out[(t.session_path, t.episode_index)] = {
                    "task_id": t.id,
                    "state": "active" if t.id in self._active_tasks else "queued",
                    "progress": float(t.progress or 0.0),
                    "elapsed": max(0.0, now - self._started_at.get(t.id, now)),
                }
        return out

    # ── 启动清理与续传 ────────────────────────────────

    # 上传临时文件（打包 zip / episodes 单行切片 / 预压缩视频）都落在
    # 录制根目录，命名前缀见 _zip_session/_zip_episode/_episode_slice_parquet/
    # _precompress_videos。
    _TMP_RE = re.compile(
        r"^_(?:.+_upload_[0-9a-zA-Z]+\.zip|episodes_.+\.parquet|precomp_.+)$")

    @staticmethod
    def sweep_orphan_temp_files(base_dir: str, max_age_hours: float = 24.0) -> int:
        """删除被杀死进程遗留的上传临时文件，返回删除个数。

        只删 mtime 超过 max_age_hours 的：正在上传的进程可能正在写这些
        文件（另一实例/另一个版本），年龄门槛保证不会误删。
        """
        if not base_dir or not os.path.isdir(base_dir):
            return 0
        cutoff = time.time() - max_age_hours * 3600
        removed = 0
        try:
            names = os.listdir(base_dir)
        except OSError:
            return 0
        for name in names:
            if not UploadManager._TMP_RE.match(name):
                continue
            p = os.path.join(base_dir, name)
            try:
                if not os.path.isfile(p) or os.path.getmtime(p) > cutoff:
                    continue
                os.remove(p)
                removed += 1
            except OSError:
                continue
        return removed

    @staticmethod
    def _local_episode_exists(session_path: str, episode_index: int) -> bool:
        """episode 数据文件是否仍在本地（续传前的存在性检查）。

        文件已被 UPLOAD_DELETE_AFTER 或手动删除时不能再续传：_zip_episode
        会打出一个缺数据的包。
        """
        if not os.path.isdir(session_path):
            return False
        if episode_index <= 0:
            try:
                return bool(os.listdir(session_path))
            except OSError:
                return False
        from core.helpers import list_task_episodes
        return episode_index in list_task_episodes(session_path)

    def _resume_sessions_snapshot(self) -> Optional[list]:
        """续传判据用的服务器会话列表；查询失败返回 None（照常重传）。"""
        try:
            client = APIClient(self._server_url, session=self._shared_session)
        except Exception:
            return None
        try:
            return client.try_get_sessions(
                limit=getattr(settings, "UPLOAD_SESSION_SNAPSHOT_LIMIT", 200))
        finally:
            client.close()

    def resume_pending(self, max_age_hours: float = 24.0) -> tuple:
        """启动续传：把上次进程未完成的上传重新入队。

        Returns:
            (resumed, verified, skipped)
            resumed  = [(task_id, session_path, episode_index), ...]  已重新入队
            verified = [(task_id, session_path, episode_index, session_id), ...]
                       服务器上已存在 → 直接按成功收尾，不再上传
            skipped  = [{"path", "episode_index", "reason"}, ...]
            reason ∈ missing_files | server_changed | already_done | already_queued

        只取每个 (session_path, episode_index) 最近一次尝试：更早的 pending
        行已被后来的行取代，否则每次启动都会重复入队。

        已入库判定（避免"上次 POST 其实成功了"被重复上传）：只有上次
        **POST 已经开始**（行状态 uploading，updated_at = POST 开始时刻）的
        才做预判——打包阶段被杀的肯定没入库，直接重传。判定按时间顺序
        做"一对一分配"：一条服务器会话只认领一次，且必须落在该次 POST 的
        开始时刻与下一次 POST 开始时刻之间（同任务多条续传时防串位）。
        """
        cutoff = (datetime.now() - timedelta(hours=max_age_hours)).isoformat()
        try:
            # 最新在前 → 每个键只保留最近一次尝试（更早的 pending 行已被
            # 后来的行取代；用旧行会拿错 POST 时刻、重复上传已入库的件）
            rows = db.conn.execute(
                "SELECT * FROM upload_task WHERE status IN ('pending', 'uploading') "
                "AND created_at >= ? ORDER BY created_at DESC", (cutoff,)
            ).fetchall()
        except Exception:
            return [], [], []

        cands, skipped, seen = [], [], set()
        for row in rows:
            row = dict(row)
            key = (row.get("session_path", ""), row.get("episode_index", 0))
            if key in seen:
                continue
            seen.add(key)
            path, ep = key
            if str(row.get("server_url", "")).rstrip("/") != \
                    self._server_url.rstrip("/"):
                skipped.append({"path": path, "episode_index": ep,
                                "reason": "server_changed"})
                continue
            if self.get_upload_status(path, ep) == "completed":
                skipped.append({"path": path, "episode_index": ep,
                                "reason": "already_done"})
                continue
            if not self._local_episode_exists(path, ep):
                skipped.append({"path": path, "episode_index": ep,
                                "reason": "missing_files"})
                continue
            if self.has_task(path, ep):
                skipped.append({"path": path, "episode_index": ep,
                                "reason": "already_queued"})
                continue
            cands.append({
                "row": row, "path": path, "episode_index": ep,
                # POST 开始时刻（仅 status=uploading 的行有）
                "anchor": (str(row.get("updated_at", "") or "")
                           if row.get("status") == "uploading" else ""),
            })

        cands.reverse()          # 回到时间正序（一对一分配按 POST 先后）

        sessions = None
        if any(c["anchor"] for c in cands):
            sessions = self._resume_sessions_snapshot()

        resumed, verified, claimed = [], [], set()
        for i, c in enumerate(cands):
            anchor, path, ep = c["anchor"], c["path"], c["episode_index"]
            if sessions is not None and anchor:
                # 上界 = 下一条"POST 已开始"的续传行的开始时刻（没有则不限）
                until = next((c2["anchor"] for c2 in cands[i + 1:]
                              if c2["anchor"]), "")
                hit = pick_session_in_window(
                    sessions, os.path.basename(path), anchor, until,
                    tol_s=getattr(settings, "UPLOAD_RESUME_SKEW_TOL_S", 60.0))
                sid = str((hit or {}).get("id", ""))
                if hit is not None and sid not in claimed:
                    claimed.add(sid)
                    task = UploadTask(
                        path, self._server_url, ep,
                        task_id=str(c["row"].get("id", "")), resumed=True,
                        created_at=str(c["row"].get("created_at", "") or ""))
                    # emit=False：调用方还没登记 task_id → 收尾由它自己做
                    self._mark_upload_verified(task, hit, "上次进程中断前已入库",
                                               emit=False)
                    verified.append((task.id, path, ep, sid))
                    continue
            tid = self.add_task(
                path, ep, task_id=str(c["row"].get("id", "")), resumed=True,
                created_at=str(c["row"].get("created_at", "") or ""))
            resumed.append((tid, path, ep))
        return resumed, verified, skipped

    # ── 工作循环 ──────────────────────────────────────

    def _worker_loop(self):
        while self._running:
            task = None
            with QMutexLocker(self._mutex):
                if len(self._active) < self._max_concurrent and self._queue:
                    task = self._queue.pop(0)
                    self._active[task.id] = None

            if task:
                t = threading.Thread(
                    target=self._upload_one, args=(task,),
                    daemon=True, name=f"upload-{task.id[:8]}"
                )
                with QMutexLocker(self._mutex):
                    self._active[task.id] = t
                    self._active_tasks[task.id] = task
                t.start()
            else:
                time.sleep(0.5)

    def _upload_one(self, task: UploadTask):
        """执行单个任务的完整上传流程。"""
        task_id = task.id
        with QMutexLocker(self._mutex):
            self._started_at[task_id] = time.monotonic()
        self.task_started.emit(task_id)

        client = APIClient(self._server_url, session=self._shared_session)
        zip_path = None
        precomp = {}
        post_started = False     # POST 是否已发出（打包/预压失败不必查服务器）

        try:
            # ── 步骤 1: 视频预压缩（大会话必备）──
            if settings.UPLOAD_PRECOMPRESS_VIDEO:
                precomp = self._precompress_videos(
                    task.session_path, task_id, task.episode_index)

            # ── 步骤 2: 打包为 zip（池化 = 单 episode 切片）──
            self.task_status.emit(task_id, "正在打包…")
            if task.episode_index > 0:
                zip_path = self._zip_episode(
                    task.session_path, task.session_name, task.episode_index,
                    task_id, precomp)
            else:
                zip_path = self._zip_session(
                    task.session_path, task.session_name, task_id, precomp)

            if not zip_path:
                raise RuntimeError("打包失败")

            zip_size = os.path.getsize(zip_path)
            self.task_status.emit(task_id,
                f"打包完成 ({zip_size / 1024 / 1024:.1f} MB)，正在上传…")

            # ── 步骤 3: 上传 zip ──
            last_ratio = [0.0]

            def _progress(uploaded: int, total: int):
                ratio = uploaded / max(total, 1)
                if ratio - last_ratio[0] >= 0.02 or ratio >= 1.0:
                    last_ratio[0] = ratio
                    # 0.12~1.0：预压缩 0~0.08、打包 0.08~0.12、上传 0.12~1.0
                    self.task_progress.emit(task_id, 0.12 + 0.88 * ratio)
                    mb_up = uploaded / 1024 / 1024
                    mb_total = total / 1024 / 1024
                    self.task_status.emit(task_id,
                        f"上传中 {mb_up:.1f}/{mb_total:.1f} MB")

            # 目标项目：优先用户显式配置；未配置时按会话名精确匹配服务器
            # 项目名自动消歧（防 409 "Ambiguous project prefix"），
            # 匹配不到则留空由服务器按名称自动匹配/自动创建。
            project_id = self._project_id or self._resolve_project_by_name(
                client, task.session_name)
            if project_id and not self._project_id:
                self.task_status.emit(task_id, f"已匹配目标项目 {project_id[:8]}…")

            # ── 上传前：记录服务器会话列表快照 ──
            # 快照 + 失败后复查 = "POST 到底有没有入库"的唯一判据
            # （服务器不提供 GET /session/{id}，只能看列表）。
            baseline_ok, before_ids = False, set()
            snap = client.try_get_sessions(
                limit=getattr(settings, "UPLOAD_SESSION_SNAPSHOT_LIMIT", 200))
            if snap is not None:
                baseline_ok = True
                before_ids = {str(s.get("id", "")) for s in snap}

            # 标记"POST 已开始"并落库：进程被杀后，续传（resume_pending）
            # 凭 status=uploading + updated_at 判断服务器上是否已有本次会话
            task.status = "uploading"
            task.updated_at = datetime.now().isoformat()
            self._save_to_db(task)

            post_started = True          # 之后的异常才需要"查服务器"判定
            result = client.upload_session_zip(
                zip_path, task.session_name, progress_cb=_progress,
                name=task.session_name, project_id=project_id,
                # 表单值 = file 号（0 基，与本地 file-NNN 对齐；真实全局
                # 序号 N 在包内 parquet 行的 episode_index 列）
                episode_index=episode_file_suffix(task.episode_index),
            )

            if not result.get("ok"):
                hit = self._verify_after_failure(client, task, before_ids,
                                                 baseline_ok)
                if hit:
                    self._mark_upload_verified(task, hit, "POST 返回失败但服务器已入库")
                    return
                # 已复查过 → 不让 except 分支再查一遍（省一次 GET，
                # 也避免两次判定之间新出现的会话造成误判）
                post_started = False
                raise RuntimeError(result.get("error", "上传失败"))

            # ── 完成 ──
            task.status = "completed"
            task.progress = 1.0
            task.server_session_id = result.get("session_id", "")
            task.error_message = ""          # 清掉此前重试残留的错误文本
            task.retry_count = 0
            task.updated_at = datetime.now().isoformat()
            self._save_to_db(task)
            self.task_progress.emit(task_id, 1.0)
            self.task_status.emit(task_id, "上传完成 ✅")
            self.task_completed.emit(task_id)

        except Exception as e:
            task.error_message = str(e)[:500]
            task.updated_at = datetime.now().isoformat()

            # POST 已发出却抛异常（读超时/连接断开/进程被杀）——服务器可能
            # 其实已经入库，只是响应没回来。先复查会话列表：是本次的会话
            # 就按成功收尾，绝不重传（重传会在服务器上多出一条重复会话）。
            if post_started:
                hit = self._verify_after_failure(client, task, before_ids,
                                                 baseline_ok)
                if hit:
                    self._mark_upload_verified(task, hit, "上传中断但服务器已入库")
                    return

            task.retry_count += 1

            if task.retry_count < self._retry_max:
                task.status = "pending"
                self._save_to_db(task)
                with QMutexLocker(self._mutex):
                    self._queue.append(task)
                self.task_status.emit(task_id,
                    f"重试 {task.retry_count}/{self._retry_max}…")
                self.task_added.emit(task_id)
            else:
                task.status = "failed"
                self._save_to_db(task)
                self.task_status.emit(task_id, f"失败: {task.error_message[:80]}")
                self.task_failed.emit(task_id, task.error_message)

        finally:
            client.close()
            # 清理临时 zip、预压缩临时视频与 episodes 单行切片
            slice_tmp = os.path.join(
                os.path.dirname(task.session_path),
                f"_episodes_{task_id}_{task.episode_index}.parquet")
            for p in [zip_path, slice_tmp] + list(precomp.values()):
                if p and os.path.isfile(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass

            with QMutexLocker(self._mutex):
                self._active.pop(task_id, None)
                self._active_tasks.pop(task_id, None)
                self._started_at.pop(task_id, None)

            if self.all_done():
                self.all_completed.emit()

    # ── 上传结果校验（查服务器）────────────────────────

    def _mark_upload_verified(self, task: UploadTask, server_session: dict,
                              reason: str, emit: bool = True):
        """服务器上已存在本次上传的会话 → 按成功收尾（不再重复上传）。

        emit=False 供 resume_pending 用：启动续传时调用方还没拿到 task_id，
        提前发 task_completed 会让主窗口查不到该任务的 session_path（无法
        标记录制行 / 按开关删本地件），故由调用方拿到返回值后自行收尾。
        """
        task.status = "completed"
        task.progress = 1.0
        task.server_session_id = str((server_session or {}).get("id", "") or "")
        task.error_message = ""
        task.retry_count = 0
        task.updated_at = datetime.now().isoformat()
        self._save_to_db(task)
        if not emit:
            return
        self.task_progress.emit(task.id, 1.0)
        self.task_status.emit(task.id, f"服务器上已存在本次会话，按成功处理（{reason}）")
        self.task_completed.emit(task.id)

    def _verify_after_failure(self, client: APIClient, task: UploadTask,
                              before_ids: set,
                              baseline_ok: bool) -> Optional[dict]:
        """POST 失败/异常后重查会话列表：出现新的同名会话 → 判定已入库。

        快照或复查失败时返回 None（区分不了新旧，宁可照常重试）。
        """
        if not baseline_ok:
            return None
        sessions = client.try_get_sessions(
            limit=getattr(settings, "UPLOAD_SESSION_SNAPSHOT_LIMIT", 200))
        if sessions is None:
            return None
        return pick_new_session(before_ids, sessions, task.session_name)

    # ── 项目消歧 ──────────────────────────────────────

    @staticmethod
    def _strip_episode_suffix(session_name: str) -> str:
        """去掉会话名末尾的 episode 序号（_000001）得到任务/项目名。"""
        import re
        m = re.match(r"^(.*)_(\d{6,})$", session_name)
        return m.group(1) if m else session_name

    @staticmethod
    def _resolve_project_by_name(client: "APIClient",
                                 session_name: str) -> str:
        """按会话名匹配服务器项目，唯一命中时返回 project_id，否则空。

        优先项目名精确相等（服务器为任务名自动创建的项目即此形式）；
        其次 workflow 名相等且唯一（不唯一说明撞名，宁可让用户手动选）。
        """
        stripped = UploadManager._strip_episode_suffix(session_name)
        projects = client.get_projects()
        exact = [p for p in projects if p.get("name") == stripped]
        if len(exact) == 1:
            return exact[0].get("id", "")
        if not exact:
            wf = [p for p in projects
                  if stripped in (p.get("workflow_names") or [])]
            if len(wf) == 1:
                return wf[0].get("id", "")
        return ""

    # ── 打包 ──────────────────────────────────────────

    def _zip_session(self, session_dir: str, session_name: str,
                     task_id: str = "", precomp: Optional[dict] = None
                     ) -> Optional[str]:
        """
        将 session 目录打包为 zip（LeRobot v3 兼容路径）。

        EgoData 视频路径转换为 LeRobot v3 格式:
          旧结构: videos/stereo_left.mp4 → videos/stereo_left/chunk_000000.mp4
          新结构: videos/stereo_left/chunk-0000/stereo_left.mp4 → videos/stereo_left/chunk_000000.mp4

        precomp: 预压缩映射 {原视频路径: 临时压缩视频路径}，打包时用它顶替原视频
                 （视频已是压缩格式，zip 用 ZIP_STORED 免二次压缩）。

        Returns:
            临时 zip 文件路径，失败返回 None
        """
        try:
            # 在 recordings 目录下创建临时 zip（同盘避免跨盘拷贝）；
            # 文件名带 task_id——同进程内并发任务不会互相覆盖
            parent_dir = os.path.dirname(session_dir)
            tmp_path = os.path.join(parent_dir,
                                    f"_{session_name}_upload_{task_id}.zip")

            file_list = []
            for root, dirs, files in os.walk(session_dir):
                for f in files:
                    fp = os.path.join(root, f)
                    arcname = os.path.relpath(fp, session_dir)

                    # EgoData 路径 → LeRobot v3 兼容路径
                    if "chunk-0000" in arcname:
                        parts = arcname.replace("\\", "/").split("/")
                        if parts[0] == "videos" and len(parts) >= 4:
                            # 用目录名作为 camera key，_aux 文件归入主摄像头目录
                            # videos/stereo_left/chunk-0000/stereo_left_aux.mp4
                            # → videos/stereo_left/chunk_0000/stereo_left_aux.mp4
                            cam_name = parts[1]
                            file_name = parts[-1]
                            arcname = f"videos/{cam_name}/chunk_0000/{file_name}"
                        elif parts[0] == "data" and len(parts) >= 4:
                            # data/<sensor>/chunk-0000/<file>.parquet → data/<sensor>/chunk_0000/<file>.parquet
                            sensor_name = parts[1]
                            file_name = parts[-1]
                            arcname = f"data/{sensor_name}/chunk_0000/{file_name}"
                        else:
                            # 其它含 chunk-0000 的路径：只把 chunk-0000 替换为 chunk_0000
                            arcname = arcname.replace("chunk-0000", "chunk_0000")
                    # 旧结构兼容: videos/<file>.mp4 (flat) → videos/<cam>/chunk_0000/file-0000.mp4
                    elif arcname.startswith("videos/") and arcname.count("/") == 1:
                        cam_name = os.path.splitext(f)[0]
                        arcname = f"videos/{cam_name}/chunk_0000/file-0000.mp4"

                    file_list.append((fp, arcname))

            total = len(file_list)
            with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_STORED) as zf:
                for i, (fp, arcname) in enumerate(file_list):
                    zf.write(precomp.get(fp, fp), arcname)
                    ratio = (i + 1) / total
                    # 打包占进度 0.08~0.12（预压缩 0~0.08，上传 0.12~1.0）
                    self.task_progress.emit(task_id, 0.08 + 0.04 * ratio)

            return tmp_path
        except Exception as e:
            self.task_status.emit(task_id, f"打包出错: {e}")
            return None

    # ── 打包（v1.1.0 池化：单 episode 切片）────────────

    def _zip_episode(self, task_dir: str, session_name: str,
                     episode_index: int, task_id: str = "",
                     precomp: Optional[dict] = None) -> Optional[str]:
        """
        将池化任务里的单个 episode 打包为 zip（上传契约）：

          videos/chunk-{c:03d}/{image_key}/episode-{f:03d}.{ext}  本 episode 各 key 视频
          data/chunk-{c:03d}/episode-{f:03d}.parquet              本 episode 数据
          meta/info.json / stats.json 快照 / tasks.jsonl       任务级元数据
          meta/episodes/chunk-{c:03d}/episode-{f:03d}.parquet     本 episode 元数据（每段一文件）

        zip 内相对路径与原池化目录一致，服务器导入器凭
        info.json.format=="pooled_episodes_v1" 识别。
        precomp: 预压缩映射 {原视频路径: 临时压缩视频路径}（ZIP_STORED 顶替）。
        """
        from core.helpers import (
            episode_chunk_file, chunk_dir, episode_video_files,
            pooled_data_parquet_path, pooled_episodes_path,
            pooled_info_path, pooled_stats_path, pooled_tasks_jsonl_path,
        )
        precomp = precomp or {}
        try:
            parent_dir = os.path.dirname(task_dir)
            tmp_path = os.path.join(
                parent_dir,
                f"_{session_name}_ep"
                f"{episode_file_suffix(episode_index):06d}_upload_"
                f"{task_id}.zip")

            c, f = episode_chunk_file(episode_index)
            file_list = []

            # 本 episode 各 key 视频（深度视频——12-bit 灰 mp4 / 旧双流
            # mkv——一并打包，不预压缩）
            for key, vp in sorted(episode_video_files(task_dir, episode_index).items()):
                if not os.path.isfile(vp):
                    continue
                ext = os.path.splitext(vp)[1]
                arcname = f"videos/{chunk_dir(c)}/{key}/episode-{f:03d}{ext}"
                file_list.append((vp, arcname))

            # 本 episode 数据 parquet
            data_pq = pooled_data_parquet_path(task_dir, episode_index)
            if os.path.isfile(data_pq):
                file_list.append((data_pq, os.path.relpath(data_pq, task_dir)))

            # episodes 每段文件直传；旧分片回退=单行切片（临时文件，打完包即删）
            slice_file, slice_arc = self._episode_slice_parquet(
                task_dir, episode_index, parent_dir, task_id)
            if slice_file:
                file_list.append((slice_file, slice_arc))

            # 任务级元数据（stats.json 为 v1.1.1 自含累加器格式）
            for mp in (pooled_info_path(task_dir), pooled_stats_path(task_dir),
                       pooled_tasks_jsonl_path(task_dir)):
                if os.path.isfile(mp):
                    file_list.append((mp, os.path.relpath(mp, task_dir)))

            total = len(file_list)
            with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_STORED) as zf:
                for i, (fp, arcname) in enumerate(file_list):
                    zf.write(precomp.get(fp, fp), arcname)
                    # 打包占进度 0.08~0.12（预压缩 0~0.08，上传 0.12~1.0）
                    self.task_progress.emit(task_id, 0.08 + 0.04 * (i + 1) / total)

            return tmp_path
        except Exception as e:
            self.task_status.emit(task_id, f"打包出错: {e}")
            return None

    def _episode_slice_parquet(self, task_dir: str, episode_index: int,
                               tmp_dir: str, task_id: str):
        """episodes 元数据文件（zip 内 arcname 与新布局同编号）。

        新布局=每段一个文件：返回原文件直传（不做切片）。
        旧分片回退（每 chunk 一个多行文件）：取本 episode 那一行写成
        临时单行 parquet。返回 (源文件路径, zip 内 arcname)；
        文件缺失/行不存在返回 (None, "")。
        """
        from core.helpers import (episode_chunk_file, pooled_episodes_path,
                                  _legacy_episodes_shard_path)
        c, _ = episode_chunk_file(episode_index)
        ep_file = pooled_episodes_path(task_dir, episode_index)
        arcname = os.path.relpath(ep_file, task_dir)
        if os.path.isfile(ep_file):
            try:
                import pyarrow.parquet as pq
                rows = pq.read_table(ep_file).to_pylist()
            except Exception:
                rows = None
            # 单行且正是 N → 直传（旧分片只剩该行的退化情形同样安全）
            if rows is not None and len(rows) == 1 and \
                    rows[0].get("episode_index") == episode_index:
                return ep_file, arcname
        # 旧分片回退（分片名 = chunk 号 file-{c:03d}，v1.1.2 起与每段文件
        # episode-{f:03d} 不再重名）
        legacy = _legacy_episodes_shard_path(task_dir, c)
        if not os.path.isfile(legacy):
            return None, ""
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
            tbl = pq.read_table(legacy)
            mask = pa.array(
                [v == episode_index
                 for v in tbl.column("episode_index").to_pylist()])
            sub = tbl.filter(mask)
            if sub.num_rows == 0:
                return None, ""
            tmp = os.path.join(
                tmp_dir, f"_episodes_{task_id}_{episode_index}.parquet")
            pq.write_table(sub, tmp)
            return tmp, arcname
        except Exception:
            return None, ""

    # ── 视频预压缩 ────────────────────────────────────

    def _find_working_ffmpeg(self) -> Optional[str]:
        """探测可用的 ffmpeg。

        conda base 的 ffmpeg 因 openvino/tbb 库冲突无法运行，
        优先用 lerobot 环境的独立 ffmpeg，再退回系统 PATH。
        """
        candidates = [
            os.path.expanduser("~/miniconda3/envs/lerobot/bin/ffmpeg"),
            os.path.expanduser("~/anaconda3/envs/lerobot/bin/ffmpeg"),
            "ffmpeg",
        ]
        for c in candidates:
            try:
                r = subprocess.run([c, "-version"], capture_output=True,
                                   timeout=10)
                if r.returncode == 0:
                    return c
            except Exception:
                continue
        return None

    @staticmethod
    def _parse_video_codec(stderr_text: str) -> Optional[str]:
        """解析 ffmpeg -i stderr 的 Video 流编码名（小写），失败返回 None。"""
        m = re.search(r"Stream #[^\n]*?Video:\s*([a-zA-Z0-9_]+)", stderr_text)
        return m.group(1).lower() if m else None

    def _probe_video_text(self, ffmpeg: str, path: str) -> Optional[str]:
        """ffmpeg -i 的 stderr 文本（其 rc 恒非 0，不看 rc）；
        异常/超时 → None。"""
        try:
            r = subprocess.run([ffmpeg, "-hide_banner", "-i", path],
                               capture_output=True, timeout=30)
        except Exception:
            return None
        return (r.stderr or b"").decode("utf-8", "ignore")

    def _is_hevc(self, ffmpeg: str, path: str) -> Optional[bool]:
        """码流探测：视频是否已是 HEVC。True=是 / False=否 / None=未知。

        探测失败 → None（=未知，调用方照旧压缩，安全方向）。
        """
        text = self._probe_video_text(ffmpeg, path)
        codec = self._parse_video_codec(text or "")
        if codec is None:
            return None
        if codec in ("hevc", "h265"):
            return True
        return False

    def _is_gray12le(self, ffmpeg: str, path: str) -> bool:
        """码流探测：是否 gray12le 12-bit 灰度（v1.1.2 深度数据轨）。

        深度轨转 8-bit 预压即毁码值——HEVC 探测失败（None）时此检查
        兜底。探测失败保守 False：此时 ffmpeg 不可用，转码同样会失败，
        原文件原样进包，数据不损。
        """
        text = self._probe_video_text(ffmpeg, path)
        return bool(text and "gray12le" in text)

    def _precompress_videos(self, session_dir: str,
                            task_id: str = "",
                            episode_index: int = 0) -> dict:
        """把 session 内 videos 下的视频重编码到低码率（HEVC CRF 档见 settings）。

        保持分辨率/帧率 → 帧数不变，与 parquet/时间戳的帧级对齐不受影响。
        已是 HEVC 的视频（v1.0.9 录制直出）跳过预压 —— 再压一遍纯属浪费。
        v1.1.0 池化：episode_index > 0 时只压本 episode 的视频；深度视频
        有专门的 12-bit 灰度检测跳过预压缩（再编码会破坏对数深度码）。
        重编码产物放在 recordings 同盘临时文件，返回 {原路径: 临时路径}；
        找不到 ffmpeg 或没视频时返回 {}（调用方直接跳过）。
        """
        ffmpeg = self._find_working_ffmpeg()
        if not ffmpeg:
            self.task_status.emit(task_id, "未找到 ffmpeg，跳过视频压缩")
            return {}

        targets = []
        if episode_index > 0:
            from core.helpers import episode_video_files
            targets = [
                vp for vp in sorted(episode_video_files(session_dir,
                                                        episode_index).values())
                if vp.lower().endswith((".mp4", ".avi", ".mov"))
                and os.path.isfile(vp)
            ]
        else:
            for root, dirs, files in os.walk(os.path.join(session_dir, "videos")):
                for f in files:
                    if f.lower().endswith((".mp4", ".avi", ".mov")):
                        targets.append(os.path.join(root, f))
        if not targets:
            return {}

        crf = getattr(settings, "UPLOAD_VIDEO_CRF", 30)
        parent_dir = os.path.dirname(session_dir)
        mapping = {}
        for i, src in enumerate(targets):
            # ★ v1.0.9：已是 HEVC 的录制件跳过预压（录制端直出目标档；
            # 探测失败按"未知"照旧压缩——安全方向，旧 h264 会话行为不变）
            if self._is_hevc(ffmpeg, src) is True:
                self.task_status.emit(
                    task_id, f"{os.path.basename(src)} 已是 HEVC，跳过预压缩")
                continue
            # ★ v1.1.2：深度 12-bit 灰度视频是数据轨不是画面，转 8-bit
            # 预压即毁码值（HEVC 探测失败时此检查兜底）
            if self._is_gray12le(ffmpeg, src):
                self.task_status.emit(
                    task_id, f"{os.path.basename(src)} 是 12-bit 灰度深度视频，跳过预压缩")
                continue
            # 临时文件名带 task_id：并发任务各自的压缩产物互不覆盖
            # （旧写法按进程号+序号命名，同进程两条任务的同名视频
            #   会写同一文件，ffmpeg 互踩 → 打进 zip 的视频损坏）
            dst = os.path.join(
                parent_dir,
                f"_precomp_{task_id}_{i}_{os.path.basename(src)}")
            self.task_status.emit(
                task_id,
                f"压缩视频 {i + 1}/{len(targets)}: {os.path.basename(src)} "
                f"(HEVC CRF {crf})…")
            try:
                r = subprocess.run(
                    [ffmpeg, "-y", "-i", src, "-c:v", "libx265",
                     "-crf", str(crf), "-preset", "veryfast",
                     "-tag:v", "hvc1", "-pix_fmt", "yuv420p", dst],
                    capture_output=True, timeout=3600)
                if r.returncode == 0 and os.path.isfile(dst):
                    mapping[src] = dst
                    src_mb = os.path.getsize(src) / 1048576
                    dst_mb = os.path.getsize(dst) / 1048576
                    self.task_status.emit(
                        task_id,
                        f"压缩 {os.path.basename(src)}: "
                        f"{src_mb:.0f}MB → {dst_mb:.0f}MB")
                elif os.path.isfile(dst):
                    try:
                        os.remove(dst)
                    except OSError:
                        pass
            except Exception:
                if os.path.isfile(dst):
                    try:
                        os.remove(dst)
                    except OSError:
                        pass
        return mapping

    # ── 数据库 ────────────────────────────────────────

    def _save_to_db(self, task: UploadTask):
        d = task.to_dict()
        db.conn.execute("""
            INSERT OR REPLACE INTO upload_task
                (id, session_path, session_name, episode_index, status, progress,
                 retry_count, server_url, server_session_id,
                 error_message, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (d["id"], d["session_path"], d["session_name"],
              d["episode_index"], d["status"], d["progress"],
              d["retry_count"], d["server_url"],
              d["server_session_id"], d["error_message"],
              d["created_at"], d["updated_at"]))
        db.conn.commit()

    @staticmethod
    def get_upload_status(session_path: str, episode_index: int = 0) -> str:
        """(task_dir, episode_index) 键查询最近一次上传状态（v1.1.0 池化）。"""
        row = db.conn.execute(
            "SELECT status FROM upload_task WHERE session_path = ? "
            "AND episode_index = ? "
            "ORDER BY created_at DESC LIMIT 1", (session_path, episode_index)
        ).fetchone()
        return row["status"] if row else "pending"

    @staticmethod
    def list_tasks(session_path: str = "",
                   episode_index: int | None = None) -> list[dict]:
        """episode_index 为 None 时跨 episode 查（旧语义）；int 时精确匹配。"""
        cond, args = "", []
        if session_path:
            cond += "session_path = ?"
            args.append(session_path)
        if episode_index is not None:
            if cond:
                cond += " AND "
            cond += "episode_index = ?"
            args.append(episode_index)
        if cond:
            rows = db.conn.execute(
                f"SELECT * FROM upload_task WHERE {cond} "
                "ORDER BY created_at DESC", args
            ).fetchall()
        else:
            rows = db.conn.execute(
                "SELECT * FROM upload_task ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]
